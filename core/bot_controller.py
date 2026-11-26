# core/bot_controller.py - Autonomous trading loop coordinator

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("BotController")


class BotController:
    _instance = None
    # RLock: get_instance() holds the lock while calling cls(...), whose
    # __new__ re-acquires it on the same thread — a plain Lock deadlocks.
    _lock = threading.RLock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config_manager=None, agent_manager=None):
        if hasattr(self, "initialized") and self.initialized:
            return
        self.config_manager = config_manager
        self.agent_manager = agent_manager
        self._running = False
        self._circuit_breaker_active = False
        self._breaker_thread: Optional[threading.Thread] = None
        self._assessment_thread: Optional[threading.Thread] = None
        self._optimization_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._day_start_balance: Optional[float] = None
        self._day_start_date: Optional[str] = None
        # True after a STOP whose bot.enabled=False write did not reach the
        # settings file: stopped here, but not durable (see stop()).
        self._stop_not_durable = False
        self._persist_exception = ""
        self.initialized = True

    @classmethod
    def get_instance(cls, config_manager=None, agent_manager=None):
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(config_manager, agent_manager)
            elif config_manager is not None or agent_manager is not None:
                if config_manager is not None:
                    cls._instance.config_manager = config_manager
                if agent_manager is not None:
                    cls._instance.agent_manager = agent_manager
        return cls._instance

    def start(self) -> dict:
        if self._running:
            return {"status": "already_running", "message": "Bot is already running"}

        bot_cfg = self._get_bot_config()
        resuming = bool(bot_cfg.get("enabled", False))
        mode = str(bot_cfg.get("mode", "paper")).lower()
        if mode not in ("paper", "live"):
            return {"status": "error", "message": f"Invalid bot mode: {mode}"}

        if mode == "live":
            logger.warning("BOT: Live mode requested but not yet fully connected. Starting in paper mode.")
            mode = "paper"

        self._snapshot_day_start()
        if self._circuit_breaker_active:
            return {
                "status": "blocked",
                "message": "Circuit breaker is active for the current trading day",
            }

        stop_event_was_set = self._stop_event.is_set()
        self._running = True
        self._stop_event.clear()

        # Write bot.enabled to config so subprocess engine picks it up on reload
        persisted = self._set_bot_config("enabled", True)
        if not persisted and not resuming:
            # The switch did not reach the settings file: the engine process
            # never learns the bot is on and the state is lost on restart.
            # Reporting "running" here contradicted the API's own "was NOT
            # saved" answer, so the in-memory start is taken back.
            self._running = False
            if stop_event_was_set:
                self._stop_event.set()
            reason = self._last_persist_error()
            self._force_bot_config_in_memory("enabled", False)
            self._set_bot_config("enabled", False)
            logger.error("BOT START NOT SAVED: %s. The bot was not started.", reason)
            return {
                "status": "error",
                "code": "config_not_saved",
                "persisted": False,
                "message": (
                    "Bot start was NOT saved: the settings file could not be written "
                    f"({reason}). The bot was not started."
                ),
            }
        self._stop_not_durable = False

        if self.agent_manager is not None:
            self.agent_manager.set_bot_mode(True)
        else:
            logger.info("BOT: agent_manager not wired directly — relying on config.bot.enabled for subprocess engine")

        self._start_background_threads()

        logger.info(f"BOT ACTIVE: {mode} mode | min_edge_score={bot_cfg.get('min_edge_score', 75)}")
        return {"status": "resumed" if resuming else "started", "mode": mode}

    def stop(self, reason: str = "manual") -> dict:
        was_running = self.is_running

        self._running = False
        self._stop_event.set()

        # Always clear the persisted switch. The API process can be restarted while
        # the engine process still sees bot.enabled=True, so in-memory state alone is
        # not authoritative here.
        persisted = self._set_bot_config("enabled", False)
        if not persisted:
            # A stop is never taken back: the bot stays stopped in this process
            # whatever happened to the file. What is lost is durability, and
            # that is reported instead of hidden.
            self._force_bot_config_in_memory("enabled", False)
        self._stop_not_durable = not persisted

        if self.agent_manager is not None:
            self.agent_manager.set_bot_mode(False)

        durability: dict = {"durable": bool(persisted)}
        if not persisted:
            persist_error = self._last_persist_error()
            logger.error(
                "BOT STOP NOT SAVED: %s. Stopped in this process only; the engine process "
                "may still read bot.enabled=true and the bot returns on restart.",
                persist_error,
            )
            durability["persisted"] = False
            durability["warning"] = (
                "Bot stop was NOT saved: the settings file could not be written "
                f"({persist_error}). The bot is stopped in this server process, but the stop is "
                "not durable: the engine process may still see it enabled and it comes back "
                "on restart until the settings file can be written."
            )

        if not was_running:
            return {"status": "already_stopped", "message": "Bot is not running", **durability}

        logger.info(f"BOT STOPPED: reason={reason}")
        return {"status": "stopped", "reason": reason, **durability}

    @property
    def is_running(self) -> bool:
        return bool(self._running or self._configured_enabled())

    @staticmethod
    def _portfolio_snapshot(capital_manager) -> tuple[float, float, list]:
        """Return cash, mark-to-market equity, and positions.

        Opening a position reserves margin by debiting cash. Reserved margin is
        still portfolio equity and must never be counted as a trading loss by
        the bot circuit breaker.
        """
        from core.capital_manager import resolve_capital_equity, resolve_deployable_balance

        cash = resolve_deployable_balance(capital_manager)
        positions = capital_manager.get_positions() if hasattr(capital_manager, "get_positions") else []
        equity = resolve_capital_equity(capital_manager, positions)
        return cash, equity, positions

    def get_status(self) -> dict:
        from core.capital_manager import get_capital_manager, is_research_position

        bot_cfg = self._get_bot_config()
        cm = get_capital_manager()
        balance, equity, positions = self._portfolio_snapshot(cm)
        open_positions = [
            p for p in positions
            if str(p.get("status", "")).upper() == "OPEN"
            and not is_research_position(p)
        ]
        research_positions = [
            p for p in positions
            if str(p.get("status", "")).upper() == "OPEN"
            and is_research_position(p)
        ]

        daily_pnl = 0.0
        drawdown_pct = 0.0
        if self._day_start_balance and self._day_start_balance > 0:
            daily_pnl = equity - self._day_start_balance
            drawdown_pct = -daily_pnl / self._day_start_balance * 100.0 if daily_pnl < 0 else 0.0

        self._retry_pending_stop()
        configured_enabled = bool(bot_cfg.get("enabled", False))
        running = bool(self._running or configured_enabled)
        return {
            "running": running,
            "state": "running" if running else "stopped",
            "configured_enabled": configured_enabled,
            "controller_running": self._running,
            "stop_not_durable": bool(self._stop_not_durable),
            "mode": str(bot_cfg.get("mode", "paper")).lower(),
            "circuit_breaker_active": self._circuit_breaker_active,
            "daily_pnl": round(daily_pnl, 2),
            "drawdown_pct": round(drawdown_pct, 2),
            "positions_open": len(open_positions),
            "research_positions_open": len(research_positions),
            "balance": round(balance, 2),
            "equity": round(equity, 2),
            "min_edge_score": bot_cfg.get("min_edge_score", 75),
        }

    def get_control_status(self) -> dict:
        """Lightweight status for control-plane polling (no portfolio I/O)."""
        self._retry_pending_stop()
        bot_cfg = self._get_bot_config()
        configured_enabled = bool(bot_cfg.get("enabled", False))
        running = bool(self._running or configured_enabled)
        return {
            "running": running,
            "state": "running" if running else "stopped",
            "configured_enabled": configured_enabled,
            "controller_running": self._running,
            "stop_not_durable": bool(self._stop_not_durable),
            "mode": str(bot_cfg.get("mode", "paper")).lower(),
            "circuit_breaker_active": self._circuit_breaker_active,
            "min_edge_score": bot_cfg.get("min_edge_score", 75),
        }

    def _get_bot_config(self) -> dict:
        if self.config_manager is not None:
            if isinstance(self.config_manager, dict):
                return self.config_manager
            return self.config_manager.bot
        return {
            "enabled": False,
            "mode": "paper",
            "min_edge_score": 75,
            "circuit_breaker_drawdown_pct": 10.0,
            "circuit_breaker_daily_loss_pct": 5.0,
        }

    def _configured_enabled(self) -> bool:
        try:
            return bool(self._get_bot_config().get("enabled", False))
        except Exception:
            return False

    def _start_background_threads(self) -> None:
        thread_specs = (
            ("_breaker_thread", self._circuit_breaker_loop, "BotCircuitBreaker"),
            ("_assessment_thread", self._self_assessment_loop, "BotSelfAssessment"),
            ("_optimization_thread", self._optimization_loop, "BotParamOptimizer"),
        )
        for attribute, target, name in thread_specs:
            current = getattr(self, attribute, None)
            if current is not None and current.is_alive():
                continue
            thread = threading.Thread(target=target, daemon=True, name=name)
            setattr(self, attribute, thread)
            thread.start()

    def _snapshot_day_start(self):
        from core.capital_manager import get_capital_manager
        from core.config import get_bucharest_time

        today = get_bucharest_time().date().isoformat()
        if self._day_start_date is None:
            # A restart must not forget today's baseline or a tripped breaker.
            stored = self._load_breaker_state()
            if stored.get("date") == today and float(stored.get("balance") or 0.0) > 0:
                self._day_start_balance = float(stored["balance"])
                self._day_start_date = today
                self._circuit_breaker_active = bool(stored.get("active"))
        if self._day_start_date != today:
            cm = get_capital_manager()
            _, equity, _ = self._portfolio_snapshot(cm)
            self._day_start_balance = equity
            self._day_start_date = today
            self._circuit_breaker_active = False
            self._save_breaker_state()

    _BREAKER_STATE_KEY = "bot_circuit_breaker_state"

    def _load_breaker_state(self) -> dict:
        try:
            from core.sqlite_store import get_sqlite_store

            state = get_sqlite_store().get_json(self._BREAKER_STATE_KEY, {})
            return state if isinstance(state, dict) else {}
        except Exception as exc:
            logger.warning("Circuit-breaker state could not be read: %s", exc)
            return {}

    def _save_breaker_state(self) -> None:
        try:
            from core.sqlite_store import get_sqlite_store

            get_sqlite_store().set_json(self._BREAKER_STATE_KEY, {
                "date": self._day_start_date,
                "balance": self._day_start_balance,
                "active": bool(self._circuit_breaker_active),
            })
        except Exception as exc:
            logger.warning("Circuit-breaker state could not be saved: %s", exc)

    @classmethod
    def notify_capital_reset(cls, equity: float) -> bool:
        """Rebase an existing controller after an explicit operator reset.

        A reset is capital administration, not trading PnL. Without rebasing,
        lowering the account amount looks like a loss against the old baseline.
        """
        try:
            baseline = float(equity)
        except (TypeError, ValueError):
            return False
        if baseline <= 0:
            return False

        with cls._lock:
            controller = cls._instance
            from core.config import get_bucharest_time

            if controller is None:
                # No controller yet: still replace the stored baseline, or the
                # next start would compare the new balance with the old one.
                try:
                    from core.sqlite_store import get_sqlite_store

                    get_sqlite_store().set_json(cls._BREAKER_STATE_KEY, {
                        "date": get_bucharest_time().date().isoformat(),
                        "balance": baseline,
                        "active": False,
                    })
                except Exception as exc:
                    logger.warning("Circuit-breaker baseline could not be saved: %s", exc)
                return False

            controller._day_start_balance = baseline
            controller._day_start_date = get_bucharest_time().date().isoformat()
            controller._circuit_breaker_active = False
            controller._save_breaker_state()
        logger.info("Circuit-breaker baseline rebased after Capital reset: %.2f", baseline)
        return True

    def _circuit_breaker_loop(self):
        while not self._stop_event.is_set():
            try:
                if not self._running:
                    break

                self._snapshot_day_start()

                bot_cfg = self._get_bot_config()
                max_drawdown_pct = float(bot_cfg.get("circuit_breaker_drawdown_pct", 10.0))
                from core.risk_limits import configured_daily_loss_pct

                max_daily_loss_pct = configured_daily_loss_pct(
                    {"max_daily_loss_pct": bot_cfg.get("circuit_breaker_daily_loss_pct")}
                )

                from core.capital_manager import get_capital_manager
                cm = get_capital_manager()
                _, equity, _ = self._portfolio_snapshot(cm)

                if self._day_start_balance and self._day_start_balance > 0:
                    daily_loss = self._day_start_balance - equity
                    daily_loss_pct = daily_loss / self._day_start_balance * 100.0

                    if daily_loss_pct >= max_daily_loss_pct:
                        self._circuit_breaker_active = True
                        self._save_breaker_state()
                        logger.warning(
                            f"CIRCUIT BREAKER: Daily loss {daily_loss_pct:.1f}% "
                            f"exceeds limit {max_daily_loss_pct:.1f}%"
                        )
                        self.stop(reason="circuit_breaker_daily_loss")
                        break

                    drawdown_pct = daily_loss_pct if daily_loss > 0 else 0.0
                    if drawdown_pct >= max_drawdown_pct:
                        self._circuit_breaker_active = True
                        self._save_breaker_state()
                        logger.warning(
                            f"CIRCUIT BREAKER: Drawdown {drawdown_pct:.1f}% "
                            f"exceeds limit {max_drawdown_pct:.1f}%"
                        )
                        self.stop(reason="circuit_breaker_drawdown")
                        break

            except Exception as e:
                logger.error(f"Circuit breaker loop error: {e}")

            self._stop_event.wait(30)

    def _self_assessment_loop(self):
        """Periodic self-assessment: adjusts min_edge_score based on performance."""
        while not self._stop_event.is_set():
            try:
                if not self._running:
                    break
                self._run_self_assessment_once()

            except Exception as e:
                logger.error(f"Self-assessment loop error: {e}")

            self._stop_event.wait(21600)  # 6 hours

    def _run_self_assessment_once(self) -> dict:
        from core.feedback_loop import compute_portfolio_metrics, get_verified_capital_trades

        bot_cfg = self._get_bot_config()
        if not bool(bot_cfg.get("self_assessment_enabled", True)):
            return {"status": "disabled"}

        required_sample = max(30, int(bot_cfg.get("self_assessment_sample_size", 100) or 100))
        trades = get_verified_capital_trades(required_sample)
        if len(trades) < required_sample:
            return {"status": "insufficient_sample", "sample_size": len(trades), "required": required_sample}

        metrics = compute_portfolio_metrics(trades)
        raw_pf = metrics.get("profit_factor")
        if raw_pf is None:
            return {"status": "unmeasurable", "reason": "profit_factor_unavailable", "metrics": metrics}

        pf = float(raw_pf)
        win_rate = float(metrics.get("win_rate", 0.0) or 0.0)
        expectancy = float(metrics.get("expectancy", 0.0) or 0.0)
        drawdown = float(metrics.get("max_drawdown_pct", 0.0) or 0.0)
        min_edge = int(bot_cfg.get("min_edge_score", 80) or 80)
        step = max(1, int(bot_cfg.get("self_assessment_step", 5) or 5))
        lower_bound = int(bot_cfg.get("self_assessment_min_edge_score", 65) or 65)
        upper_bound = int(bot_cfg.get("self_assessment_max_edge_score", 90) or 90)

        raise_pf = float(bot_cfg.get("self_assessment_raise_profit_factor", 1.0) or 1.0)
        lower_pf = float(bot_cfg.get("self_assessment_lower_profit_factor", 1.8) or 1.8)
        lower_wr = float(bot_cfg.get("self_assessment_lower_min_win_rate", 0.50) or 0.50)
        lower_expectancy = float(bot_cfg.get("self_assessment_lower_min_expectancy", 0.0) or 0.0)
        lower_max_drawdown = float(bot_cfg.get("self_assessment_lower_max_drawdown_pct", 10.0) or 10.0)

        new_edge = min_edge
        reason = "no_change"
        if pf < raise_pf or expectancy < 0:
            new_edge = min(upper_bound, min_edge + step)
            reason = "negative_edge"
        elif (
            pf >= lower_pf
            and win_rate >= lower_wr
            and expectancy > lower_expectancy
            and drawdown <= lower_max_drawdown
        ):
            new_edge = max(lower_bound, min_edge - step)
            reason = "strong_confirmed_edge"

        if new_edge != min_edge:
            logger.info(
                "SELF-ASSESSMENT: %s (PF=%.2f, WR=%.1f%%, expectancy=%.2f, DD=%.1f%%), "
                "min_edge_score %s -> %s",
                reason,
                pf,
                win_rate * 100.0,
                expectancy,
                drawdown,
                min_edge,
                new_edge,
            )
            self._set_bot_config("min_edge_score", new_edge)

        return {
            "status": "updated" if new_edge != min_edge else "unchanged",
            "reason": reason,
            "previous_min_edge_score": min_edge,
            "min_edge_score": new_edge,
            "metrics": metrics,
        }

    def _set_bot_config(self, key: str, value) -> bool:
        """Update a bot config value at runtime.

        Returns False only when the config manager reported that the write did
        not reach the settings file (an explicit False result, an exception, or
        ``save_ok is False``). Plain dicts and test doubles count as saved.
        """
        if self.config_manager is None:
            return True
        if isinstance(self.config_manager, dict):
            self.config_manager[key] = value
            return True
        try:
            if callable(getattr(type(self.config_manager), "update_value", None)):
                result = self.config_manager.update_value(["bot", key], value)
            else:
                self.config_manager._config.setdefault("bot", {})[key] = value
                result = self.config_manager.save()
        except Exception as exc:
            logger.error("Bot config %s=%r could not be written: %s", key, value, exc)
            self._persist_exception = str(exc)
            return False
        self._persist_exception = ""
        if result is False:
            return False
        return getattr(self.config_manager, "save_ok", True) is not False

    def _force_bot_config_in_memory(self, key: str, value) -> None:
        """Set a bot config value in this process without relying on a save."""
        cm = self.config_manager
        if cm is None:
            return
        try:
            if isinstance(cm, dict):
                cm[key] = value
                return
            raw = getattr(cm, "_config", None)
            if isinstance(raw, dict):
                raw.setdefault("bot", {})[key] = value
            live = getattr(cm, "bot", None)
            if isinstance(live, dict):
                live[key] = value
        except Exception:
            logger.debug("in-memory bot config update failed", exc_info=True)

    def _last_persist_error(self) -> str:
        text = str(getattr(self, "_persist_exception", "") or "").strip()
        if not text:
            raw = getattr(self.config_manager, "last_save_error", "")
            text = raw.strip() if isinstance(raw, str) else ""
        return text or "config.json could not be written"

    def _retry_pending_stop(self) -> None:
        """Retry the bot.enabled=False write after a stop that was not durable."""
        if not getattr(self, "_stop_not_durable", False) or self._running:
            return
        if self._set_bot_config("enabled", False):
            self._stop_not_durable = False
            logger.info("BOT STOP is now durable: bot.enabled=false was written")
        else:
            self._force_bot_config_in_memory("enabled", False)

    def can_go_live(self) -> dict:
        """Check if bot meets requirements for live mode."""
        from core.feedback_loop import compute_portfolio_metrics, get_verified_capital_trades

        trades = get_verified_capital_trades(500)
        metrics = compute_portfolio_metrics(trades)

        reasons = []
        eligible = True

        n = len(trades)
        if n < 100:
            reasons.append(f"Need >= 100 trades (have {n})")
            eligible = False

        wr = float(metrics.get("win_rate", 0))
        if wr < 0.50:
            reasons.append(f"Win rate {wr:.1%} < 50%")
            eligible = False

        raw_pf = metrics.get("profit_factor")
        pf = float(raw_pf) if raw_pf is not None else None
        if pf is None:
            reasons.append("Profit factor unavailable: no losing trade is recorded")
            eligible = False
        elif pf < 1.3:
            reasons.append(f"Profit factor {pf:.2f} < 1.3")
            eligible = False

        dd = float(metrics.get("max_drawdown_pct", 0))
        if dd > 15.0:
            reasons.append(f"Max drawdown {dd:.1f}% > 15%")
            eligible = False

        exp = float(metrics.get("expectancy", 0))
        if exp <= 0:
            reasons.append(f"Expectancy {exp:.2f} not positive")
            eligible = False

        return {
            "eligible": eligible,
            "reasons": reasons if not eligible else ["All criteria met"],
            "metrics": metrics,
        }

    def _optimization_loop(self):
        """Periodic parameter optimization (does NOT auto-apply)."""
        while not self._stop_event.is_set():
            try:
                if not self._running:
                    break

                from core.param_optimizer import ParamOptimizer
                optimizer = ParamOptimizer(self.config_manager)
                result = optimizer.run_optimization()

                if result.get("suggestions"):
                    logger.info(f"PARAM OPTIMIZER: {len(result['suggestions'])} suggestions available")

            except Exception as e:
                logger.error(f"Optimization loop error: {e}")

            self._stop_event.wait(86400)  # 24 hours
