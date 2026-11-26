# core/confidence_gates.py - Tiered confidence system for autonomous trading

import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional

logger = logging.getLogger("ConfidenceGates")


class ConfidenceGates:
    def __init__(self, config_manager=None):
        self.config_manager = config_manager
        self._daily_trade_count = 0
        self._daily_trade_date: Optional[str] = None
        self._consecutive_losses = 0
        # Monotonic-wallclock end of the post-loss pause (0 = not in cooldown).
        # NOTE: in-memory only — a restart clears it.
        self._loss_cooldown_until = 0.0
        self._same_strategy_counts: Dict[str, int] = {}

    def _get_config(self) -> dict:
        if self.config_manager is not None:
            if isinstance(self.config_manager, dict):
                return self.config_manager
            return self.config_manager.bot
        return {
            "min_edge_score": 75,
            "max_trades_per_day": 5,
            "max_same_strategy_concurrent": 2,
            "consecutive_loss_cooldown": 1,
            "require_strategy_performance": True,
            "min_strategy_sample_size": 30,
            "min_strategy_profit_factor": 1.2,
            "min_strategy_expectancy_r": 0.0,
            "max_strategy_drawdown_pct": 25.0,
            "require_trade_radar_confirmation": True,
        }

    def _get_execution_config(self) -> dict:
        try:
            agents = getattr(self.config_manager, "agents", {}) if self.config_manager is not None else {}
            if isinstance(agents, dict):
                return agents.get("execution", {}) or {}
        except Exception:
            pass
        return {}

    def release_trade_reservation(self, signal_data: dict) -> None:
        """Undo the optimistic confidence reservation when execution does not open a position."""
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if self._daily_trade_date != today:
                self._daily_trade_count = 0
                self._daily_trade_date = today
                self._same_strategy_counts = {}
                return
            strategy_type = str((signal_data or {}).get("strategy_type") or (signal_data or {}).get("signal_mode", "UNKNOWN"))
            asset_class = str((signal_data or {}).get("asset_class", "US_STOCKS")).upper()
            strat_key = f"{strategy_type.upper()}:{asset_class}"
            if self._daily_trade_count > 0:
                self._daily_trade_count -= 1
            if self._same_strategy_counts.get(strat_key, 0) > 0:
                self._same_strategy_counts[strat_key] -= 1
        except Exception:
            logger.debug("Could not release confidence reservation", exc_info=True)

    def evaluate(
        self,
        signal_data: dict,
        perf_cache=None,
    ) -> dict:
        """
        Evaluate signal confidence and return tier assignment.

        Returns: {"tier": "A"|"B"|"C"|"SKIP", "adjusted_size_factor": float, "reasons": list}
        """
        cfg = self._get_config()
        exec_cfg = self._get_execution_config()
        min_edge = float(cfg.get("min_edge_score", 75) or 75)
        max_daily_raw = cfg.get("max_trades_per_day", 5)
        max_same_raw = cfg.get("max_same_strategy_concurrent", 2)
        max_daily = int(max_daily_raw if max_daily_raw is not None else 5)
        max_same_strat = int(max_same_raw if max_same_raw is not None else 2)
        loss_cooldown = int(cfg.get("consecutive_loss_cooldown", 1))

        reasons = []
        autonomous_source = str(signal_data.get("autonomous_source") or "").lower().strip()
        paper_capital_experiment = bool(signal_data.get("paper_capital_experiment"))
        if autonomous_source == "scanner":
            scanner_min_score = float(exec_cfg.get("scanner_min_score", min_edge) or min_edge)
            min_edge = min(min_edge, scanner_min_score)
        elif autonomous_source == "pre_earnings":
            pre_earnings_min_score = float(exec_cfg.get("pre_earnings_min_score", min_edge) or min_edge)
            min_edge = min(min_edge, pre_earnings_min_score)
        allow_unvalidated_autonomous = bool(
            autonomous_source
            and cfg.get("allow_unvalidated_autonomous_fallback", True)
        ) or paper_capital_experiment
        if paper_capital_experiment:
            reasons.append("Paper S7 Capital experiment: unvalidated performance uses reduced size")
        try:
            unvalidated_autonomous_size = float(cfg.get("unvalidated_autonomous_size_factor", 0.25) or 0.25)
        except Exception:
            unvalidated_autonomous_size = 0.25
        unvalidated_autonomous_size = max(0.05, min(0.5, unvalidated_autonomous_size))
        unvalidated_perf_fallback = False
        radar_unavailable_fallback = False

        # Reset daily counter
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._daily_trade_date != today:
            self._daily_trade_count = 0
            self._daily_trade_date = today
            self._same_strategy_counts = {}

        # 1. Score check
        signal_score = float(
            signal_data.get(
                "signal_score",
                signal_data.get("score", signal_data.get("raw_signal_score", signal_data.get("raw_score", 0))),
            )
            or 0
        )
        if signal_score <= 0:
            signal_score = float(signal_data.get("raw_signal_score", signal_data.get("raw_score", 0)) or 0)
        if signal_score < min_edge:
            reasons.append(f"Score {signal_score:.1f} < min_edge {min_edge:g}")
            return {"tier": "SKIP", "adjusted_size_factor": 0.0, "reasons": reasons}

        # 2. V2 validation check
        v2_validation = float(signal_data.get("validation_score", 0))
        strategy_source = str(signal_data.get("strategy_source", "V1")).upper()

        # Check perf cache for strategy performance
        strategy_type = str(signal_data.get("strategy_type") or signal_data.get("signal_mode", ""))
        ticker = str(signal_data.get("ticker", ""))
        is_degraded = False
        is_suspended = False
        perf_entry_found = False
        sample = 0
        pf = None
        expectancy_r = 0.0
        max_drawdown = 0.0

        action = str(signal_data.get("action") or signal_data.get("type") or "").upper().strip()
        direction = "SHORT" if action in {"SHORT", "SELL"} else "LONG"

        if perf_cache is not None and strategy_type and ticker:
            try:
                asset_class = str(signal_data.get("asset_class", "US_STOCKS")).upper()
                perf = perf_cache.get_entry(ticker, asset_class, strategy_type, direction)
                if isinstance(perf, dict):
                    perf_entry_found = bool(perf)
                    if perf.get("suspended"):
                        is_suspended = True
                        reasons.append(f"Strategy {strategy_type} is suspended")
                    if perf.get("degraded"):
                        is_degraded = True
                        reasons.append(f"Strategy {strategy_type} is degraded")
                    sample = int(perf.get("sample_size", 0))
                    wr = float(perf.get("win_rate", 0))
                    raw_pf = perf.get("profit_factor")
                    pf = float(raw_pf) if raw_pf is not None else None
                    expectancy_r = float(
                        perf.get("expectancy_R", perf.get("expectancy_r", perf.get("expectancy", 0.0))) or 0.0
                    )
                    max_drawdown = float(perf.get("max_drawdown", 0.0) or 0.0)
                    if sample >= 30 and v2_validation < 30 and not paper_capital_experiment:
                        is_suspended = True
                        reasons.append(f"V2 validation {v2_validation:.1f} < 30 with sample={sample}")
            except Exception as e:
                logger.debug(f"Perf cache lookup failed: {e}")

        if is_suspended:
            return {"tier": "SKIP", "adjusted_size_factor": 0.0, "reasons": reasons}

        require_perf = bool(cfg.get("require_strategy_performance", True))
        exempt = {
            str(x or "").upper()
            for x in cfg.get("performance_gate_exempt_strategies", ["MANUAL"])
            if str(x or "").strip()
        }
        if require_perf and str(strategy_type or "").upper() not in exempt and perf_cache is None:
            if allow_unvalidated_autonomous:
                unvalidated_perf_fallback = True
                reasons.append(f"Strategy performance cache unavailable for {strategy_type}; using reduced autonomous size")
            else:
                reasons.append(f"Strategy performance cache unavailable for {strategy_type}")
                return {"tier": "SKIP", "adjusted_size_factor": 0.0, "reasons": reasons}

        if require_perf and perf_cache is not None and str(strategy_type or "").upper() not in exempt:
            min_sample = int(cfg.get("min_strategy_sample_size", 30) or 30)
            min_pf = float(cfg.get("min_strategy_profit_factor", 1.2) or 1.2)
            min_expectancy = float(cfg.get("min_strategy_expectancy_r", 0.0) or 0.0)
            max_dd = float(cfg.get("max_strategy_drawdown_pct", 25.0) or 25.0)
            if not perf_entry_found:
                msg = f"No validated performance for {strategy_type} {direction} {ticker}"
                if allow_unvalidated_autonomous:
                    unvalidated_perf_fallback = True
                    reasons.append(f"{msg}; using reduced autonomous size")
                else:
                    reasons.append(msg)
                    return {"tier": "SKIP", "adjusted_size_factor": 0.0, "reasons": reasons}
            else:
                if sample < min_sample:
                    msg = f"Sample size {sample} < min {min_sample}"
                    if allow_unvalidated_autonomous:
                        unvalidated_perf_fallback = True
                        reasons.append(f"{msg}; using reduced autonomous size")
                    else:
                        reasons.append(msg)
                        return {"tier": "SKIP", "adjusted_size_factor": 0.0, "reasons": reasons}
                if not unvalidated_perf_fallback and pf is None:
                    reasons.append("Profit factor unavailable: no losing trade is recorded")
                    return {"tier": "SKIP", "adjusted_size_factor": 0.0, "reasons": reasons}
                if not unvalidated_perf_fallback and pf < min_pf:
                    reasons.append(f"Profit factor {pf:.2f} < min {min_pf:.2f}")
                    return {"tier": "SKIP", "adjusted_size_factor": 0.0, "reasons": reasons}
                if not unvalidated_perf_fallback and expectancy_r <= min_expectancy:
                    reasons.append(f"Expectancy_R {expectancy_r:.3f} <= min {min_expectancy:.3f}")
                    return {"tier": "SKIP", "adjusted_size_factor": 0.0, "reasons": reasons}
                if not unvalidated_perf_fallback and max_drawdown > max_dd:
                    reasons.append(f"Max drawdown {max_drawdown:.1f}% > limit {max_dd:.1f}%")
                    return {"tier": "SKIP", "adjusted_size_factor": 0.0, "reasons": reasons}

        # 3. Daily trade limit
        if max_daily > 0 and self._daily_trade_count >= max_daily:
            reasons.append(f"Daily trade limit reached ({max_daily})")
            return {"tier": "SKIP", "adjusted_size_factor": 0.0, "reasons": reasons}

        # 4. Same-strategy concurrent limit
        asset_class = str(signal_data.get("asset_class", "US_STOCKS")).upper()
        strat_key = f"{str(strategy_type or 'UNKNOWN').upper()}:{asset_class}"
        current_strat_count = self._same_strategy_counts.get(strat_key, 0)
        if max_same_strat > 0 and current_strat_count >= max_same_strat:
            reasons.append(f"Max concurrent {strat_key} trades reached ({max_same_strat})")
            return {"tier": "SKIP", "adjusted_size_factor": 0.0, "reasons": reasons}

        # 5. Sentiment gate
        sentiment_score = float(signal_data.get("sentiment_score", 0.0))
        if direction == "LONG" and sentiment_score < -0.5:
            reasons.append(f"Sentiment {sentiment_score:.2f} contradicts LONG direction")
            return {"tier": "SKIP", "adjusted_size_factor": 0.0, "reasons": reasons}
        if direction == "SHORT" and sentiment_score > 0.5:
            reasons.append(f"Sentiment {sentiment_score:.2f} contradicts SHORT direction")
            return {"tier": "SKIP", "adjusted_size_factor": 0.0, "reasons": reasons}

        # 6. Pre-earnings gap risk gate
        gap_risk = int(signal_data.get("gap_risk", 0))
        is_pre_earnings_strategy = (
            autonomous_source == "pre_earnings"
            or "PRE_EARNINGS" in str(strategy_type or "").upper()
            or "PRE_EARNINGS" in str(signal_data.get("signal_mode") or "").upper()
        )
        if gap_risk > 7 and not is_pre_earnings_strategy:
            reasons.append(f"Gap risk {gap_risk} > 7")
            return {"tier": "SKIP", "adjusted_size_factor": 0.0, "reasons": reasons}
        if gap_risk > 7 and is_pre_earnings_strategy:
            reasons.append(f"Pre-earnings gap risk {gap_risk} treated as event-volatility context")

        # 7. Trade radar opportunity gate
        raw_radar_opp = signal_data.get("trade_radar_opportunity")
        if raw_radar_opp is None and is_pre_earnings_strategy:
            raw_radar_opp = signal_data.get("opportunity_score") or signal_data.get("score")
        opportunity = float(raw_radar_opp or 0.0)
        agent_reasons = " ".join(str(x) for x in (signal_data.get("agent_reasons") or []))
        radar_unavailable = (raw_radar_opp is None) or any(
            token in agent_reasons
            for token in (
                "trade_radar_agent_not_available",
                "trade_radar_agent_not_ready",
                "ticker_not_in_trade_radar",
                "trade_radar_lookup_error",
            )
        )
        require_radar = bool(cfg.get("require_trade_radar_confirmation", True))
        if opportunity < 50 and not radar_unavailable:
            if allow_unvalidated_autonomous:
                radar_unavailable_fallback = True
                reasons.append(f"Trade radar opportunity {opportunity:.1f} < 50; using reduced autonomous size")
            else:
                reasons.append(f"Trade radar opportunity {opportunity:.1f} < 50")
                return {"tier": "SKIP", "adjusted_size_factor": 0.0, "reasons": reasons}
        if opportunity < 50 and radar_unavailable:
            reasons.append("Trade radar unavailable for this ticker")
            if require_radar:
                if allow_unvalidated_autonomous:
                    radar_unavailable_fallback = True
                    reasons.append("Continuing with reduced autonomous size without Trade Radar confirmation")
                else:
                    return {"tier": "SKIP", "adjusted_size_factor": 0.0, "reasons": reasons}

        # 8. Consecutive loss cooldown — a real time-based pause
        # (consecutive_loss_cooldown is interpreted as hours). The previous
        # implementation decremented the loss counter per *evaluation*, so the
        # "cooldown" lasted exactly one skipped candidate.
        if self._consecutive_losses >= 2 and loss_cooldown > 0:
            now_ts = time.time()
            if self._loss_cooldown_until <= 0.0:
                self._loss_cooldown_until = now_ts + loss_cooldown * 3600.0
            if now_ts < self._loss_cooldown_until:
                reasons.append(
                    f"Cooldown after {self._consecutive_losses} consecutive losses "
                    f"({(self._loss_cooldown_until - now_ts) / 60.0:.0f}m remaining)"
                )
                return {"tier": "SKIP", "adjusted_size_factor": 0.0, "reasons": reasons}
            # Cooldown expired: resume trading with a clean slate
            self._loss_cooldown_until = 0.0
            self._consecutive_losses = 0

        # --- Tier assignment ---
        tier = "C"
        size_factor = 0.25

        # Tier A: Full size
        if signal_score >= 85 and v2_validation >= 70:
            tier = "A"
            size_factor = 1.0
        # Tier B: Half size
        elif signal_score >= 75 and (v2_validation >= 50 or strategy_source == "V1"):
            tier = "B"
            size_factor = 0.5

        if unvalidated_perf_fallback or radar_unavailable_fallback:
            tier = "C"
            size_factor = min(size_factor, unvalidated_autonomous_size)
            reasons.append("Autonomous fallback caps confidence tier at C")

        # Downgrade if degraded
        if is_degraded:
            if tier == "A":
                tier = "B"
                size_factor = 0.5
            elif tier == "B":
                tier = "C"
                size_factor = 0.25
            reasons.append("Downgraded due to degraded strategy performance")

        self._daily_trade_count += 1
        self._same_strategy_counts[strat_key] = current_strat_count + 1

        return {"tier": tier, "adjusted_size_factor": size_factor, "reasons": reasons}

    def record_trade_outcome(self, is_win: bool):
        """Record trade outcome for consecutive loss tracking."""
        if is_win:
            self._consecutive_losses = 0
            self._loss_cooldown_until = 0.0
        else:
            self._consecutive_losses += 1
