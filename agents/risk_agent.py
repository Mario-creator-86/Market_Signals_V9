# agents/risk_agent.py

import math
import logging
from datetime import datetime, timezone

from core.risk_limits import (
    calculate_trade_risk_pct,
    configured_daily_loss_pct,
    configured_risk_per_trade_pct,
)

from .base_agent import BaseAgent

logger = logging.getLogger("MarketEngine")


# ---------------------------------------------------------------------------
# Position limits: one meaning per key
# ---------------------------------------------------------------------------
# agents.risk.max_risk_per_trade_pct  - max % of equity LOST AT THE STOP.
# agents.risk.max_position_size_pct   - max position NOTIONAL as % of equity.
# agents.risk.position_size_pct       - LEGACY key behind the "Risk per trade
#   (%)" field. Two readers disagreed about it: the signal engine labelled it
#   "risk %" (equity * pct / stop distance), while every reader that actually
#   sizes a paper entry (signals.add_signal, AgentManager size caps, the Risk
#   Agent fallback) uses it as the position notional in % of the deployable
#   balance. Notional is the stricter reading (10% notional with a 5% stop
#   loses 0.5% of equity; 10% "risk" would need a 200% position), so that is
#   the single meaning: the BASE position notional, never above the max
#   position notional. It is not a risk-per-trade figure.
LEGACY_POSITION_SIZE_KEY = "position_size_pct"
LEGACY_POSITION_SIZE_MEANING = "base_position_notional_pct"


def _positive_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def resolve_position_limits(risk_cfg, *, default_base_notional_pct=None) -> dict:
    """The position limits of `agents.risk`, each with one explicit meaning.

    Returns:
      max_risk_per_trade_pct      % of equity lost at the stop (bounded).
      max_position_notional_pct   max notional % of equity, or None when
                                  `max_position_size_pct` is not configured.
      base_position_notional_pct  the legacy `position_size_pct`, read as
                                  notional and capped at the max notional.
      legacy_position_size_pct    the raw legacy value (None when unset).
    `default_base_notional_pct` is used only when the legacy key is unset or
    invalid; when that is None too, the max notional (or 0.5) is used.
    """
    cfg = risk_cfg if isinstance(risk_cfg, dict) else {}
    max_notional = None
    if "max_position_size_pct" in cfg:
        try:
            max_notional = max(0.05, float(cfg.get("max_position_size_pct") or 0.0))
        except (TypeError, ValueError):
            max_notional = None
        if max_notional is not None and not math.isfinite(max_notional):
            max_notional = None
    legacy = _positive_number(cfg.get(LEGACY_POSITION_SIZE_KEY))
    base = legacy
    if base is None:
        base = _positive_number(default_base_notional_pct)
    if base is None:
        base = max_notional if max_notional is not None else 0.5
    if max_notional is not None:
        base = min(base, max_notional)
    return {
        "max_risk_per_trade_pct": configured_risk_per_trade_pct(cfg),
        "max_position_notional_pct": max_notional,
        "base_position_notional_pct": base,
        "legacy_position_size_pct": legacy,
        "legacy_key": f"agents.risk.{LEGACY_POSITION_SIZE_KEY}",
        "legacy_meaning": LEGACY_POSITION_SIZE_MEANING,
    }


class RiskAgent(BaseAgent):
    def __init__(self, config_manager):
        super().__init__(config_manager, "risk")

    def loop(self):
        """Monitor portfolio risk periodically."""
        while not self._stop_event.is_set():
            try:
                self._beat()
                self._check_portfolio_risk()
            except Exception as e:
                logger.error(f"RISK AGENT: error in risk check: {e}")
            cfg = self.config_manager.agents.get("risk", {})
            interval = int(cfg.get("check_interval_seconds", 60) or 60)
            self._stop_event.wait(timeout=max(10, interval))

    def _check_portfolio_risk(self):
        """Check current portfolio risk and log warnings."""
        try:
            from core.capital_manager import (
                get_capital_manager,
                is_research_position,
                resolve_capital_equity,
            )
            cm = get_capital_manager()
            positions = cm.get_positions() or []
            balance = resolve_capital_equity(cm, positions)
            open_positions = [
                pos for pos in positions
                if str((pos or {}).get("status", "")).upper() == "OPEN"
                and not is_research_position(pos)
            ]
            if balance <= 0:
                return

            # Calculate total exposure
            total_exposure = sum(
                float((pos or {}).get("usd_size", 0.0) or 0.0)
                for pos in open_positions
            )
            exposure_pct = (total_exposure / balance) * 100 if balance > 0 else 0

            # Calculate unrealized PnL
            total_pnl = sum(
                float((pos or {}).get("pnl_pct", 0.0) or 0.0)
                for pos in open_positions
            )

            cfg = self.config_manager.agents.get("risk", {})
            max_exposure_pct = float(cfg.get("max_exposure_pct", 50.0) or 50.0)

            if exposure_pct > max_exposure_pct:
                logger.warning(
                    f"RISK AGENT: High exposure {exposure_pct:.1f}% "
                    f"(limit {max_exposure_pct}%) | {len(open_positions)} open positions"
                )

            # Phase 11: an owner-confirmed Constitution may require a real
            # partial paper fill. The owner also selects the reduction
            # percentage in UI; absent either value, no percentage is invented.
            application_config = getattr(self.config_manager, "config", {})
            if isinstance(application_config, dict):
                try:
                    from core.execution.position_manager import PositionManagementService
                    from core.execution.runtime import get_paper_broker
                    from core.portfolio.constraints import resolve_confirmed_risk_constitution

                    constitution = resolve_confirmed_risk_constitution(application_config)
                    paper_config = application_config.get("paper_execution") or {}
                    reduce_fraction_pct = paper_config.get("reduce_fraction_pct")
                    if (
                        constitution is not None
                        and open_positions
                        and exposure_pct > float(constitution.max_total_exposure_pct)
                    ):
                        largest = max(
                            open_positions,
                            key=lambda item: float((item or {}).get("usd_size") or 0.0),
                        )
                        position_id = str(largest.get("position_id") or "")
                        position_size = float(largest.get("usd_size") or 0.0)
                        decision_id = (
                            f"risk-reduce:{constitution.fingerprint}:{position_id}:"
                            f"{position_size:.6f}"
                        )
                        service = PositionManagementService(
                            get_paper_broker(
                                config=self.config_manager,
                                capital_manager=cm,
                            )
                        )
                        result = service.reassess_and_execute(
                            position=largest,
                            current_state={
                                "risk_status": "DEGRADED",
                                "reduce_required": True,
                                "breached_constraints": ["max_total_exposure_pct"],
                            },
                            decision_id=decision_id,
                            reduce_fraction_pct=reduce_fraction_pct,
                        )
                        if result.get("executed"):
                            logger.warning(
                                "RISK AGENT: reduced %s by owner-selected %s%% after exposure breach",
                                largest.get("ticker"),
                                reduce_fraction_pct,
                            )
                        elif result.get("blocked_reason"):
                            logger.warning(
                                "RISK AGENT: exposure reduction blocked: %s",
                                result.get("blocked_reason"),
                            )
                except Exception as exc:
                    logger.error("RISK AGENT: Phase 11 exposure reassessment failed closed: %s", exc)

            if total_pnl < -5.0:
                logger.warning(
                    f"RISK AGENT: Portfolio unrealized loss {total_pnl:.1f}%"
                )

        except Exception as e:
            logger.error(f"RISK AGENT: portfolio check failed: {e}")

    def validate_signal(self, signal: dict) -> bool:
        """Return True if signal passes risk checks, False otherwise."""
        return bool(self.evaluate_signal(signal).get("accepted", False))

    def evaluate_signal(self, signal: dict) -> dict:
        """Return structured risk evaluation with explicit reject reasons."""
        cfg = self.config_manager.agents.get("risk", {})
        if not cfg.get("enabled", True):
            return {
                "accepted": True,
                "reason": "risk_agent_disabled",
                "reasons": [],
                "checks": [],
                "metrics": {},
            }

        application_config = getattr(self.config_manager, "config", {})
        if not isinstance(application_config, dict):
            application_config = {}
        try:
            from core.portfolio.constraints import resolve_confirmed_risk_constitution

            constitution = resolve_confirmed_risk_constitution(application_config)
        except Exception as exc:
            logger.error(f"RISK AGENT: invalid confirmed Risk Constitution: {exc}")
            return {
                "accepted": False,
                "reason": "risk_constitution_invalid",
                "reasons": [str(exc)],
                "checks": [
                    {
                        "check": "risk_constitution",
                        "passed": False,
                        "detail": str(exc),
                    }
                ],
                "metrics": {},
            }

        try:
            from core.capital_manager import (
                get_capital_manager,
                is_research_position,
                resolve_capital_equity,
                resolve_deployable_balance,
            )
            cm = get_capital_manager()
            positions = cm.get_positions() or []
            balance = resolve_capital_equity(cm, positions)
            ticker = str((signal or {}).get("ticker", "") or "").upper().strip()
            invested_amount = float((signal or {}).get("invested_amount", 0.0) or 0.0)
            signal_score = 0.0
            for score_key in ("signal_score", "score", "raw_signal_score", "raw_score"):
                try:
                    candidate = float((signal or {}).get(score_key, 0.0) or 0.0)
                    if candidate > 0:
                        signal_score = candidate
                        break
                except Exception:
                    continue
            entry = float((signal or {}).get("entry") or (signal or {}).get("entry_price") or (signal or {}).get("price") or 0.0)
            tp = float((signal or {}).get("tp") or (signal or {}).get("take_profit_price") or (signal or {}).get("take_profit") or 0.0)
            sl = float((signal or {}).get("sl") or (signal or {}).get("stop_loss_price") or (signal or {}).get("stop_loss") or 0.0)
            action = str((signal or {}).get("action") or (signal or {}).get("direction") or (signal or {}).get("type") or "BUY").upper().strip()

            if entry > 0:
                is_short = action in ("SHORT", "SELL")
                if tp <= 0:
                    tp_pct = float((signal or {}).get("tp_pct") or (signal or {}).get("take_profit_pct") or 0.0)
                    if tp_pct > 0:
                        frac = tp_pct / 100.0 if tp_pct > 1.0 else tp_pct
                        tp = entry * (1 - frac) if is_short else entry * (1 + frac)
                if sl <= 0:
                    sl_pct = float((signal or {}).get("sl_pct") or (signal or {}).get("stop_loss_pct") or 0.0)
                    if sl_pct > 0:
                        frac = sl_pct / 100.0 if sl_pct > 1.0 else sl_pct
                        sl = entry * (1 + frac) if is_short else entry * (1 - frac)

            # Check max daily loss. Only trades closed during the current app
            # day count; historical closed trades must not lock the bot forever.
            max_loss_pct = configured_daily_loss_pct(cfg)
            max_daily_loss_trades = int(cfg.get("max_daily_loss_trades", 2) or 2)
            try:
                from core.config import BUCHAREST_TZ, get_bucharest_time
                today = get_bucharest_time().date()
                risk_tz = BUCHAREST_TZ
            except Exception:
                today = datetime.now(timezone.utc).date()
                risk_tz = timezone.utc

            def closed_on_risk_day(pos: dict) -> bool:
                closed_at = str((pos or {}).get("closed_at") or "").strip()
                if not closed_at:
                    return False
                try:
                    closed_dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
                    if closed_dt.tzinfo is None:
                        closed_dt = closed_dt.replace(tzinfo=timezone.utc)
                    return closed_dt.astimezone(risk_tz).date() == today
                except Exception:
                    return False

            closed_today = [
                pos for pos in positions
                if str((pos or {}).get("status", "")).upper() == "CLOSED"
                and not is_research_position(pos)
                and closed_on_risk_day(pos)
            ]
            daily_loss_trades = sum(
                1 for pos in closed_today
                if float((pos or {}).get("net_pnl", (pos or {}).get("pnl", 0.0)) or 0.0) < 0
            )
            daily_loss_pct = 0.0
            daily_realized_pnl = 0.0
            if balance > 0:
                daily_realized_pnl = sum(
                    float((pos or {}).get("net_pnl", (pos or {}).get("pnl", 0.0)) or 0.0)
                    for pos in closed_today
                )
                # Open losers count too: five positions under water are a
                # daily loss even before any of them is closed.
                open_unrealized = 0.0
                for pos in positions:
                    if str((pos or {}).get("status", "")).upper() != "OPEN" or is_research_position(pos):
                        continue
                    try:
                        value = float(pos.get("unrealized_pnl", pos.get("current_pnl", 0.0)) or 0.0)
                    except (TypeError, ValueError):
                        value = 0.0
                    if math.isfinite(value):
                        open_unrealized += value
                daily_loss = min(0.0, daily_realized_pnl + min(0.0, open_unrealized))
                daily_loss_pct = abs(daily_loss / balance) * 100

            # Check max open positions. A value <= 0 means unlimited; exposure/risk caps still apply.
            max_open_raw = cfg.get("max_open_trades", 0)
            max_open = int(max_open_raw if max_open_raw is not None else 0)
            open_positions = [
                pos for pos in positions
                if str((pos or {}).get("status", "")).upper() == "OPEN"
                and not is_research_position(pos)
            ]

            # Check max exposure
            max_exposure_pct = float(cfg.get("max_exposure_pct", 50.0) or 50.0)
            total_exposure = sum(
                float((pos or {}).get("usd_size", 0.0) or 0.0)
                for pos in open_positions
            )
            exposure_pct = (total_exposure / balance) * 100 if balance > 0 else 0
            leverage = max(1, int(float(signal.get("leverage", 1) or 1)))
            proposed_notional_usd = float((signal or {}).get("usd_size", 0.0) or 0.0)
            if proposed_notional_usd <= 0:
                proposed_notional_usd = invested_amount * leverage
            projected_exposure_pct = ((total_exposure + max(0.0, proposed_notional_usd)) / balance) * 100 if balance > 0 else 0.0

            min_signal_score = float(cfg.get("min_signal_score", 0.0) or 0.0)
            min_risk_reward_ratio = float(cfg.get("min_risk_reward_ratio", 1.2) or 1.2)
            # Max position NOTIONAL (% of equity). Without an explicit
            # max_position_size_pct the legacy position_size_pct is the cap:
            # it is a notional figure too (see resolve_position_limits).
            max_position_size_pct = float(
                cfg.get("max_position_size_pct", cfg.get(LEGACY_POSITION_SIZE_KEY, 0.5)) or 0.5
            )
            max_position_size_pct = max(0.05, max_position_size_pct)
            # Max % of equity lost at the stop: its own key, never the legacy one.
            max_risk_per_trade_pct = configured_risk_per_trade_pct(cfg)
            position_size_pct = (invested_amount / balance * 100.0) if balance > 0 and invested_amount > 0 else 0.0
            risk_reward_ratio = None
            risk_per_trade_pct = 0.0
            if entry > 0 and tp > 0 and sl > 0:
                if action == "SHORT":
                    reward = abs(entry - tp)
                    risk = abs(sl - entry)
                else:
                    reward = abs(tp - entry)
                    risk = abs(entry - sl)
                if risk > 0:
                    risk_reward_ratio = reward / risk
                    risk_per_trade_pct = calculate_trade_risk_pct(
                        notional_usd=proposed_notional_usd,
                        stop_loss_fraction=risk / entry,
                        equity=balance,
                    )

            reasons = []
            checks = []
            constitution_fingerprint = None

            def metrics() -> dict:
                result = {
                    "balance": round(balance, 4),
                    "daily_loss_pct": round(daily_loss_pct, 4),
                    "daily_loss_trades": daily_loss_trades,
                    "open_positions": len(open_positions),
                    "exposure_pct": round(exposure_pct, 4),
                    "projected_exposure_pct": round(projected_exposure_pct, 4),
                    "position_size_pct": round(position_size_pct, 4),
                    "max_position_size_pct": round(max_position_size_pct, 4),
                    "risk_per_trade_pct": round(risk_per_trade_pct, 4),
                    "max_risk_per_trade_pct": round(max_risk_per_trade_pct, 4),
                    "signal_score": round(signal_score, 4),
                    "risk_reward_ratio": round(risk_reward_ratio, 4) if risk_reward_ratio is not None else None,
                }
                if constitution_fingerprint:
                    result["risk_constitution_fingerprint"] = constitution_fingerprint
                return result

            def reject(code: str, detail: str) -> dict:
                logger.warning(f"RISK AGENT: Blocked {ticker or 'UNKNOWN'} | {detail}")
                reasons.append(detail)
                checks.append({"check": code, "passed": False, "detail": detail})
                return {
                    "accepted": False,
                    "reason": code,
                    "reasons": reasons,
                    "checks": checks,
                    "metrics": metrics(),
                }

            # Fail before portfolio sizing when another strategy already owns
            # the same ticker in the opposite direction. A reversal must be an
            # explicit close-then-open decision; silently creating a hedge lets
            # Scanner and Pre-Earnings contradict each other inside Capital.
            from core.position_conflicts import conflict_summary, opposing_open_positions

            proposed_direction = "SHORT" if action in ("SHORT", "SELL") else "LONG"
            opposing = opposing_open_positions(
                open_positions,
                ticker=ticker,
                proposed_direction=proposed_direction,
            )
            if opposing:
                detail = conflict_summary(
                    opposing,
                    ticker=ticker,
                    proposed_direction=proposed_direction,
                )
                return reject("opposite_open_position", detail)

            if balance <= 0:
                return reject("no_capital", "Balance unavailable or <= 0")

            if constitution is not None:
                try:
                    from core.portfolio.adapter import (
                        PortfolioAdapterError,
                        build_v7_constraint_inputs,
                    )
                    from core.portfolio.constraints import evaluate_portfolio_constraints

                    equity_curve_method = getattr(cm, "get_equity_curve", None)
                    equity_curve = (
                        equity_curve_method(limit=500)
                        if callable(equity_curve_method)
                        else []
                    )
                    snapshot, proposal = build_v7_constraint_inputs(
                        equity_usd=balance,
                        cash_usd=resolve_deployable_balance(cm),
                        open_positions=open_positions,
                        daily_realized_pnl_usd=daily_realized_pnl,
                        equity_curve=equity_curve or [],
                        signal=signal or {},
                        application_config=application_config,
                        constitution=constitution,
                    )
                    decision = evaluate_portfolio_constraints(
                        snapshot,
                        proposal,
                        constitution,
                    )
                except (PortfolioAdapterError, ValueError) as exc:
                    return reject("risk_constitution_evidence_error", str(exc))
                constitution_fingerprint = constitution.fingerprint
                checks.extend(
                    {
                        "check": item.name,
                        "passed": item.passed,
                        "actual": item.actual,
                        "limit": item.limit,
                        "detail": item.detail,
                    }
                    for item in decision.checks
                )
                if not decision.allowed:
                    veto_reasons = list(decision.reasons)
                    logger.warning(
                        f"RISK AGENT: Risk Constitution veto for {ticker or 'UNKNOWN'} | "
                        + ", ".join(veto_reasons)
                    )
                    return {
                        "accepted": False,
                        "reason": "risk_constitution_veto",
                        "reasons": veto_reasons,
                        "checks": checks,
                        "metrics": metrics(),
                    }

            checks.append({"check": "daily_loss_limit", "passed": daily_loss_pct < max_loss_pct, "detail": f"{daily_loss_pct:.2f}% < {max_loss_pct:.2f}%"})
            if daily_loss_pct >= max_loss_pct:
                return reject("daily_loss_limit", f"Daily loss {daily_loss_pct:.1f}% >= limit {max_loss_pct}%")

            checks.append({"check": "daily_loss_count", "passed": daily_loss_trades < max_daily_loss_trades, "detail": f"{daily_loss_trades} < {max_daily_loss_trades}"})
            if daily_loss_trades >= max_daily_loss_trades:
                return reject("daily_loss_count", f"{daily_loss_trades} losing trades today >= limit {max_daily_loss_trades}")

            if max_open > 0:
                checks.append({"check": "max_open_trades", "passed": len(open_positions) < max_open, "detail": f"{len(open_positions)} < {max_open}"})
                if len(open_positions) >= max_open:
                    return reject("max_open_trades", f"{len(open_positions)} open positions >= limit {max_open}")
            else:
                checks.append({"check": "max_open_trades", "passed": True, "detail": "unlimited"})

            checks.append({"check": "max_exposure_pct", "passed": projected_exposure_pct <= max_exposure_pct, "detail": f"{projected_exposure_pct:.2f}% <= {max_exposure_pct:.2f}%"})
            if projected_exposure_pct > max_exposure_pct:
                return reject("max_exposure_pct", f"Projected exposure {projected_exposure_pct:.1f}% > limit {max_exposure_pct}%")

            if invested_amount > 0:
                checks.append({"check": "max_position_size_pct", "passed": position_size_pct <= max_position_size_pct, "detail": f"{position_size_pct:.2f}% <= {max_position_size_pct:.2f}%"})
                if position_size_pct > max_position_size_pct:
                    return reject(
                        "max_position_size_pct",
                        f"Position size {position_size_pct:.2f}% > conservative cap {max_position_size_pct:.2f}%",
                    )

            if entry <= 0 or tp <= 0 or sl <= 0:
                return reject("invalid_trade_levels", "Missing valid entry/tp/sl levels")

            if risk_reward_ratio is None or risk_reward_ratio <= 0:
                return reject("invalid_risk_reward", "Could not compute a positive risk/reward ratio")

            if proposed_notional_usd > 0:
                checks.append({"check": "max_risk_per_trade_pct", "passed": risk_per_trade_pct <= max_risk_per_trade_pct, "detail": f"{risk_per_trade_pct:.2f}% <= {max_risk_per_trade_pct:.2f}%"})
                if risk_per_trade_pct > max_risk_per_trade_pct:
                    return reject(
                        "max_risk_per_trade_pct",
                        f"Trade risk {risk_per_trade_pct:.2f}% > configured cap {max_risk_per_trade_pct:.2f}%",
                    )

            checks.append({"check": "min_risk_reward_ratio", "passed": risk_reward_ratio >= min_risk_reward_ratio, "detail": f"{risk_reward_ratio:.2f} >= {min_risk_reward_ratio:.2f}"})
            if risk_reward_ratio < min_risk_reward_ratio:
                return reject("min_risk_reward_ratio", f"Risk/reward {risk_reward_ratio:.2f} < minimum {min_risk_reward_ratio:.2f}")

            if min_signal_score > 0:
                checks.append({"check": "min_signal_score", "passed": signal_score >= min_signal_score, "detail": f"{signal_score:.2f} >= {min_signal_score:.2f}"})
                if signal_score < min_signal_score:
                    return reject("min_signal_score", f"Signal score {signal_score:.1f} < minimum {min_signal_score:.1f}")

        except Exception as e:
            logger.error(f"RISK AGENT: validation check failed: {e}")
            return {
                "accepted": False,
                "reason": "risk_evaluation_error",
                "reasons": [str(e)],
                "checks": [{"check": "risk_evaluation", "passed": False, "detail": str(e)}],
                "metrics": {},
            }

        return {
            "accepted": True,
            "reason": "passed",
            "reasons": [],
            "checks": checks,
            "metrics": metrics(),
        }
