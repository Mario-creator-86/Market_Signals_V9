# agents/execution_agent.py

import logging
import time
from typing import Any

from core.capital_manager import (
    get_capital_manager,
    is_research_position,
    resolve_capital_equity,
    resolve_deployable_balance,
)
from core.signals import get_signal_manager
from core.ticker_learning import get_ticker_adjustment
from core.strategy_lab.live import is_lab_signal
from core.xs_momentum_basket import is_xs_momentum_signal
from core.trade_audit import record_trade_event
from core.capital_entry_guard import (
    capital_entry_requires_ai_review,
    evaluate_capital_entry,
)
from core.autonomous_candidates import position_time_stop_minutes
from core.learning_mode.settings import is_learning_signal
from core.trade_decision import build_signal_decision_context
from core.entry_chart_snapshot import capture_entry_chart_snapshot
from core.risk_limits import calculate_trade_risk_pct, configured_risk_per_trade_pct

from .base_agent import BaseAgent


logger = logging.getLogger("MarketEngine")


def basket_ticker_adjustment(signal: Any, ticker_adj: Any) -> tuple:
    """(adjustment to apply, adjustment set aside) for one entry.

    A rule basket (the weekly momentum basket, Strategy Lab rules) holds its
    members in equal size: one instrument's own win/loss streak from other
    strategies must not resize, delay or block a leg. Its adjustment is kept
    on record and not applied. Every other signal keeps its adjustment unchanged.
    """
    if isinstance(ticker_adj, dict) and (
        is_xs_momentum_signal(signal) or is_lab_signal(signal) or is_learning_signal(signal)
    ):
        return None, dict(ticker_adj)
    return ticker_adj, None


def carry_learning_context(signal: Any, decision_context: dict) -> None:
    """Keep what Opus decided for a learning-mode trade on the position (side, confidence, model, why),
    so that each result can later be counted by source and by Opus's confidence."""
    if is_learning_signal(signal) and isinstance((signal or {}).get("learning"), dict):
        decision_context["learning"] = dict(signal["learning"])


class ExecutionAgent(BaseAgent):
    def __init__(self, config_manager):
        super().__init__(config_manager, "execution")

    @staticmethod
    def _resolve_balance_usage_pct(
        cfg: dict,
        *,
        strategy_id: str = "",
        direction: str = "",
    ) -> tuple[float, dict]:
        configured_pct = float((cfg or {}).get("balance_usage_pct", 0.5) or 0.5)
        unprofitable_cap_pct = float((cfg or {}).get("unprofitable_balance_usage_cap_pct", 0.5) or 0.5)
        min_sample = int((cfg or {}).get("profitability_min_sample", 30) or 30)
        min_pf = float((cfg or {}).get("profitability_min_profit_factor", 1.2) or 1.2)
        min_expectancy = float((cfg or {}).get("profitability_min_expectancy", 0.0) or 0.0)

        profile = {
            "profitable": False,
            "total_trades": 0,
            "profit_factor": None,
            "expectancy": 0.0,
            "cap_reason": "insufficient_profitable_history",
            "strategy_id": str(strategy_id or "").upper(),
            "direction": str(direction or "").upper(),
        }
        try:
            from core.feedback_loop import compute_portfolio_metrics, get_recent_trades

            # Position sizing must learn from the exact strategy+direction cohort.
            # Global portfolio wins previously masked losing pre-earnings cohorts
            # and incorrectly unlocked larger sizing/leverage.
            history = get_recent_trades(max(500, min_sample * 10))
            wanted_strategy = profile["strategy_id"]
            wanted_direction = profile["direction"]
            trades = [
                trade for trade in history
                if isinstance(trade, dict)
                and (
                    not wanted_strategy
                    or str(trade.get("strategy_type") or trade.get("strategy_id") or "").upper()
                    == wanted_strategy
                )
                and (
                    not wanted_direction
                    or str(trade.get("direction") or "").upper() == wanted_direction
                )
            ]
            metrics = compute_portfolio_metrics(trades)
            profile.update(
                {
                    "total_trades": int(metrics.get("total_trades", 0) or 0),
                    "profit_factor": float(metrics["profit_factor"]) if metrics.get("profit_factor") is not None else None,
                    "expectancy": float(metrics.get("expectancy", 0.0) or 0.0),
                }
            )
            profile["profitable"] = (
                profile["total_trades"] >= min_sample
                and profile["profit_factor"] is not None
                and profile["profit_factor"] >= min_pf
                and profile["expectancy"] > min_expectancy
            )
            if profile["profitable"]:
                profile["cap_reason"] = "validated_profitable_history"
        except Exception as exc:
            profile["cap_reason"] = f"profitability_check_failed:{exc}"

        if profile["profitable"]:
            effective_pct = configured_pct
        else:
            effective_pct = min(configured_pct, unprofitable_cap_pct)
        hard_cap = float((cfg or {}).get("unprofitable_balance_usage_hard_cap_pct", 0.5) or 0.5)
        effective_pct = max(0.05, min(hard_cap if not profile["profitable"] else 20.0, effective_pct))
        profile["configured_balance_usage_pct"] = round(configured_pct, 4)
        profile["effective_balance_usage_pct"] = round(effective_pct, 4)
        return effective_pct / 100.0, profile

    def loop(self):
        # Execution is mostly event-driven (on_signal), but the loop keeps the agent alive.
        while not self._stop_event.is_set():
            self._beat(note="event_driven_keepalive")
            time.sleep(10)

    def process_signal(self, signal: dict, risk_evaluation: dict = None) -> bool:
        bot_cfg = getattr(self.config_manager, "bot", None)
        if isinstance(bot_cfg, dict) and not bool(bot_cfg.get("enabled", False)):
            record_trade_event(
                event_type="execution_skipped",
                status="bot_stopped",
                ticker=signal.get("ticker", ""),
                signal_id=signal.get("id", ""),
                strategy_type=signal.get("strategy_type", ""),
                summary="Bot master switch is off",
                payload={"master_switch": "off"},
            )
            return False
        cfg = self.config_manager.agents.get("execution", {})
        if not cfg.get("enabled", False):
            record_trade_event(
                event_type="execution_skipped",
                status="disabled",
                ticker=signal.get("ticker", ""),
                signal_id=signal.get("id", ""),
                strategy_type=signal.get("strategy_type", ""),
                summary="Execution agent disabled",
                payload={"signal": dict(signal or {})},
            )
            return False

        mode = str(cfg.get("mode", "paper")).lower()
        ticker = str(signal.get("ticker") or "").upper().strip()
        action = str(signal.get("action") or signal.get("type") or "").upper().strip()
        signal_id = str(signal.get("id") or "").strip()

        if mode == "paper":
            return self._process_paper_signal(cfg, ticker, action, signal_id, signal, risk_evaluation=risk_evaluation)
        elif mode == "live":
            logger.warning(f"EXECUTION AGENT (LIVE): Live trading not yet connected for {ticker}")
            return False
        return False

    def _process_paper_signal(self, cfg: dict, ticker: str, action: str, signal_id: str, signal: dict, *, risk_evaluation: dict = None) -> bool:
        if not ticker or action not in {"BUY", "SHORT"} or not signal_id:
            logger.warning("EXECUTION AGENT (PAPER): invalid signal payload")
            record_trade_event(
                event_type="execution_skipped",
                status="invalid_payload",
                ticker=ticker,
                signal_id=signal_id,
                strategy_type=signal.get("strategy_type", ""),
                summary="Invalid signal payload",
                payload={"signal": dict(signal or {})},
            )
            return False

        cm = get_capital_manager()
        # Defence in depth: the manager screens evidence before AI review, and
        # execution repeats it immediately before opening Capital.  This keeps
        # direct callers and delayed queues from bypassing the same standards.
        try:
            signal["_capital_automated_entry"] = True
            root_config = getattr(self.config_manager, "config", None)
            if isinstance(root_config, dict):
                gate_config = dict(root_config)
                agents_config = dict(gate_config.get("agents") or {})
                agents_config["execution"] = dict(cfg or {})
                gate_config["agents"] = agents_config
            else:
                gate_config = cfg
            capital_entry_gate = evaluate_capital_entry(
                signal,
                cm.get_positions(),
                gate_config,
            )
            signal["capital_entry_gate"] = capital_entry_gate
            checks = capital_entry_gate.get("checks", {}) if isinstance(capital_entry_gate, dict) else {}
            if bool(capital_entry_gate.get("paper_capital_experiment")):
                signal["paper_capital_experiment"] = True
                signal["research_only"] = False
                signal["capital_execution_eligible"] = True
                signal["capital_execution_block_reason"] = ""
            if isinstance(signal.get("decision_context"), dict):
                signal["decision_context"]["capital_entry_gate"] = capital_entry_gate
                if bool(capital_entry_gate.get("paper_capital_experiment")):
                    signal["decision_context"]["paper_capital_experiment"] = True
                if isinstance(checks.get("news"), dict):
                    signal["decision_context"]["autonomous_news_gate"] = checks["news"]
                if isinstance(checks.get("realized_performance"), dict):
                    signal["decision_context"]["autonomous_profitability_gate"] = checks["realized_performance"]
            if not capital_entry_gate.get("allowed"):
                failed_check = str(capital_entry_gate.get("check") or "evidence")
                event_status = {
                    "realized_performance": "profitability_gate",
                    "news": "news_gate",
                    "backtest": "backtest_gate",
                    "data": "data_gate",
                }.get(failed_check, "capital_entry_gate")
                record_trade_event(
                    event_type="execution_skipped",
                    status=event_status,
                    ticker=ticker,
                    signal_id=signal_id,
                    strategy_type=signal.get("strategy_type", ""),
                    summary=capital_entry_gate.get("reason", "Capital entry evidence blocked execution"),
                    payload={"signal": dict(signal or {}), "capital_entry_gate": capital_entry_gate},
                )
                logger.info(
                    "EXECUTION AGENT: skipped %s because %s evidence failed: %s",
                    ticker,
                    failed_check,
                    capital_entry_gate.get("reason"),
                )
                return False
        except Exception as exc:
            record_trade_event(
                event_type="execution_skipped",
                status="capital_entry_gate_error",
                ticker=ticker,
                signal_id=signal_id,
                strategy_type=signal.get("strategy_type", ""),
                summary=f"Capital entry evidence unavailable: {exc}",
                payload={"signal": dict(signal or {})},
            )
            logger.error("EXECUTION AGENT: Capital entry evidence failed closed for %s: %s", ticker, exc)
            return False

        if capital_entry_requires_ai_review(cfg):
            decision_context = signal.get("decision_context") if isinstance(signal.get("decision_context"), dict) else {}
            review = signal.get("ollama_decision") if isinstance(signal.get("ollama_decision"), dict) else decision_context.get("ollama")
            if not isinstance(review, dict) or not bool(review.get("available")) or bool(signal.get("ai_fallback_used")):
                record_trade_event(
                    event_type="execution_skipped",
                    status="ai_review_gate",
                    ticker=ticker,
                    signal_id=signal_id,
                    strategy_type=signal.get("strategy_type", ""),
                    summary="A successful AI trade review is required before automated Capital execution",
                    payload={"signal": dict(signal or {})},
                )
                logger.info("EXECUTION AGENT: skipped %s because no verified AI decision is attached", ticker)
                return False
        open_positions = [
            pos for pos in cm.get_positions()
            if str((pos or {}).get("status", "")).upper() == "OPEN"
            and not is_research_position(pos)
        ]
        max_open_raw = cfg.get("max_open_trades", 0)
        max_open = int(max_open_raw if max_open_raw is not None else 0)
        if max_open > 0 and len(open_positions) >= max_open:
            logger.warning(f"EXECUTION AGENT (PAPER): max open trades reached, skipped {ticker}")
            record_trade_event(
                event_type="execution_skipped",
                status="max_open_trades",
                ticker=ticker,
                signal_id=signal_id,
                strategy_type=signal.get("strategy_type", ""),
                summary=f"Max open trades reached ({max_open})",
                payload={"signal": dict(signal or {})},
            )
            return False

        signal_manager = get_signal_manager()
        existing_signal = signal_manager.open_signals.get(signal_id, {})
        if existing_signal.get("paper_position_id"):
            return False

        entry = float(signal.get("entry", 0.0) or 0.0)
        tp = float(signal.get("tp", 0.0) or 0.0)
        sl = float(signal.get("sl", 0.0) or 0.0)
        if entry <= 0 or tp <= 0 or sl <= 0:
            logger.warning(f"EXECUTION AGENT (PAPER): invalid entry/tp/sl for {ticker}")
            record_trade_event(
                event_type="execution_skipped",
                status="invalid_levels",
                ticker=ticker,
                signal_id=signal_id,
                strategy_type=signal.get("strategy_type", ""),
                summary="Invalid entry/tp/sl",
                payload={"signal": dict(signal or {})},
            )
            return False

        # Per-ticker, per-direction learning & safety adjustment.
        # Derived statelessly from the closed-trade history on the capital manager
        # (core/ticker_learning.py). Gates re-entry on consecutive losses (raised bar /
        # reduced size / 5-loss cooldown) and presses the edge on consecutive wins
        # (wider R:R, leverage once globally profitable). See
        # docs/superpowers/specs/2026-07-04-ticker-learning-safety-design.md.
        direction = "LONG" if action == "BUY" else "SHORT"
        try:
            closed_positions = cm.get_positions()
        except Exception:
            closed_positions = []
        try:
            ticker_adj = get_ticker_adjustment(ticker, direction, closed_positions, cfg)
        except Exception:
            ticker_adj = None
        ticker_adj, ignored_adj = basket_ticker_adjustment(signal, ticker_adj)
        if ignored_adj is not None:
            signal["ticker_learning_ignored"] = ignored_adj
        signal["ticker_learning"] = ticker_adj

        if isinstance(ticker_adj, dict) and ticker_adj.get("action") == "BLOCK":
            logger.warning(
                f"EXECUTION AGENT (PAPER): ticker cooldown for {ticker} - "
                f"{ticker_adj.get('reason', '')}"
            )
            record_trade_event(
                event_type="execution_skipped",
                status="ticker_cooldown",
                ticker=ticker,
                signal_id=signal_id,
                strategy_type=signal.get("strategy_type", ""),
                summary=str(ticker_adj.get("reason", "ticker cooldown")),
                payload={"signal": dict(signal or {}), "adjustment": ticker_adj},
            )
            return False

        if isinstance(ticker_adj, dict):
            signal_score = float(signal.get("signal_score") or 0.0)
            min_score_required = float(ticker_adj.get("min_score_required") or 0.0)
            if min_score_required > 0 and signal_score < min_score_required:
                logger.warning(
                    f"EXECUTION AGENT (PAPER): ticker bar raised for {ticker} - "
                    f"score {signal_score} < {min_score_required}"
                )
                record_trade_event(
                    event_type="execution_skipped",
                    status="ticker_bar_raised",
                    ticker=ticker,
                    signal_id=signal_id,
                    strategy_type=signal.get("strategy_type", ""),
                    summary=str(ticker_adj.get("reason", "ticker bar raised")),
                    payload={
                        "signal": dict(signal or {}),
                        "adjustment": ticker_adj,
                        "signal_score": signal_score,
                        "min_score_required": min_score_required,
                    },
                )
                return False

        invested_amount = float(signal.get("invested_amount", 0.0) or 0.0)
        base_pct, profitability_profile = self._resolve_balance_usage_pct(
            cfg,
            strategy_id=str(signal.get("strategy_type") or signal.get("strategy_id") or ""),
            direction=direction,
        )
        size_factor = max(
            0.0,
            min(1.0, float(signal.get("adjusted_size_factor", 1.0) or 1.0)),
        )
        if isinstance(ticker_adj, dict):
            # Loss-streak REDUCE shrinks size; win-streak/baseline is 1.0 (never grows risk).
            size_factor *= float(ticker_adj.get("size_mult", 1.0) or 1.0)
        balance = resolve_deployable_balance(cm)
        learning_size_usd = float(signal.get("learning_size_usd") or 0.0) if is_learning_signal(signal) else 0.0
        if learning_size_usd > 0:
            # Learning mode: a fixed small slice of equity per trade (1%), the same for every call,
            # so that results compare across trades; never above the usual balance cap.
            invested_amount = min(learning_size_usd, balance * base_pct) if balance > 0 else learning_size_usd
            size_factor = 1.0
        elif invested_amount <= 0:
            invested_amount = balance * base_pct * size_factor
        elif balance > 0:
            # AgentManager sizes a candidate conservatively before Risk so the
            # constitution can assess real notional. Confidence is evaluated
            # later, therefore an existing invested_amount has not yet received
            # adjusted_size_factor. Apply it here exactly once at final sizing.
            invested_amount = min(invested_amount, balance * base_pct) * size_factor

        if bool(cfg.get("allow_ai_leverage", True)):
            max_ai_leverage = max(1, min(5, int(cfg.get("max_ai_leverage", 2) or 2)))
            try:
                leverage = int(round(float(signal.get("leverage") or signal.get("ai_leverage") or 1)))
            except Exception:
                leverage = 1
            leverage = max(1, min(max_ai_leverage, leverage))
        else:
            leverage = 1
        if bool(cfg.get("force_one_x_until_profitable", True)) and not profitability_profile.get("profitable"):
            leverage = 1
        # Win-streak leverage bonus: only when the global strategy is profitable AND the
        # user has opted into AI/discretionary leverage (allow_ai_leverage). Respects the
        # existing force_one_x guard; hard-capped at ticker_learning.max_leverage_cap.
        if (
            isinstance(ticker_adj, dict)
            and bool(cfg.get("allow_ai_leverage", True))
            and profitability_profile.get("profitable")
        ):
            lev_bonus = int(ticker_adj.get("leverage_bonus") or 0)
            if lev_bonus > 0:
                tl_cfg = cfg.get("ticker_learning") or {}
                max_lev_cap = int(tl_cfg.get("max_leverage_cap") or 5)
                leverage = max(1, min(max_lev_cap, leverage + lev_bonus))
        notional_usd = invested_amount * float(leverage)

        stop_loss_pct = abs((sl - entry) / entry)
        take_profit_pct = abs((tp - entry) / entry)
        risk_cfg = self.config_manager.agents.get("risk", {}) or {}
        max_risk_per_trade_pct = configured_risk_per_trade_pct(risk_cfg)
        final_equity = resolve_capital_equity(cm)
        final_risk_per_trade_pct = calculate_trade_risk_pct(
            notional_usd=notional_usd,
            stop_loss_fraction=stop_loss_pct,
            equity=final_equity,
        )
        signal["final_risk_per_trade_pct"] = round(final_risk_per_trade_pct, 4)
        signal["max_risk_per_trade_pct"] = round(max_risk_per_trade_pct, 4)
        if isinstance(risk_evaluation, dict):
            risk_metrics = risk_evaluation.setdefault("metrics", {})
            if isinstance(risk_metrics, dict):
                risk_metrics["final_risk_per_trade_pct"] = round(final_risk_per_trade_pct, 4)
                risk_metrics["max_risk_per_trade_pct"] = round(max_risk_per_trade_pct, 4)
                risk_metrics["final_notional_usd"] = round(notional_usd, 6)
                risk_metrics["risk_equity"] = round(final_equity, 6)
        if final_risk_per_trade_pct > max_risk_per_trade_pct:
            detail = (
                f"Final loss-at-stop {final_risk_per_trade_pct:.2f}% exceeds "
                f"the configured {max_risk_per_trade_pct:.2f}% max risk/trade"
            )
            logger.warning("EXECUTION AGENT (PAPER): %s skipped %s", ticker, detail)
            record_trade_event(
                event_type="execution_skipped",
                status="max_risk_per_trade_pct",
                ticker=ticker,
                signal_id=signal_id,
                strategy_type=signal.get("strategy_type", ""),
                summary=detail,
                payload={
                    "signal": dict(signal or {}),
                    "notional_usd": round(notional_usd, 6),
                    "stop_loss_pct": round(stop_loss_pct * 100.0, 4),
                    "equity": round(final_equity, 6),
                    "risk_per_trade_pct": round(final_risk_per_trade_pct, 4),
                    "max_risk_per_trade_pct": round(max_risk_per_trade_pct, 4),
                },
            )
            return False
        if isinstance(ticker_adj, dict):
            # Win streak widens the R:R target; entry/stop unchanged. Baseline is 1.0.
            take_profit_pct *= float(ticker_adj.get("rr_mult", 1.0) or 1.0)
        direction = "LONG" if action == "BUY" else "SHORT"

        decision_context = signal.get("decision_context")
        if not isinstance(decision_context, dict):
            decision_context = build_signal_decision_context(
                signal,
                decision_status="accepted",
                risk_evaluation=risk_evaluation,
            )
        decision_context["decision_status"] = "executed"
        if bool(capital_entry_gate.get("paper_capital_experiment")):
            decision_context["paper_capital_experiment"] = True
        if isinstance(ticker_adj, dict):
            decision_context["ticker_learning"] = ticker_adj
        # Persist the entry regime on the position so closed-trade analysis can measure
        # the fade strategy separately from legacy momentum trades.
        entry_strategy = str(signal.get("entry_strategy") or "").lower() or None
        if entry_strategy:
            decision_context["entry_strategy"] = entry_strategy
        carry_learning_context(signal, decision_context)
        # A regime tested with a time stop keeps it on the position; the exit
        # manager closes the position when it has passed.
        time_stop_minutes = position_time_stop_minutes({"time_stop_minutes": signal.get("time_stop_minutes")})
        if time_stop_minutes > 0:
            decision_context["time_stop_minutes"] = time_stop_minutes
        decision_context["execution_context"] = {
            "mode": "paper",
            "ticker": ticker,
            "direction": direction,
            "leverage": leverage,
            "entry_strategy": entry_strategy,
            "time_stop_minutes": time_stop_minutes or None,
            "profitability_profile": profitability_profile,
            "paper_capital_experiment": bool(capital_entry_gate.get("paper_capital_experiment")),
        }
        sizing_ctx = decision_context.setdefault("sizing", {})
        sizing_ctx.update(
            {
                "invested_amount": round(invested_amount, 6),
                "base_risk_pct": round(base_pct * 100.0, 4),
                "risk_pct_used": round(base_pct * 100.0 * size_factor, 4),
                "position_size_pct": round((invested_amount / balance * 100.0) if balance > 0 else 0.0, 4),
                "size_factor": round(size_factor, 4),
                "usd_size": round(notional_usd, 6),
                "leverage": leverage,
                "profitability_profile": profitability_profile,
            }
        )
        signal["invested_amount"] = round(invested_amount, 6)
        signal["usd_size"] = round(notional_usd, 6)
        signal["leverage"] = leverage

        # Build human-readable reasoning from all agents
        reasons: list[str] = []
        reasons.append(f"Signal: {ticker} {direction} (score={signal.get('signal_score')})")
        if bool(capital_entry_gate.get("paper_capital_experiment")):
            reasons.append(
                f"Capital: {signal.get('strategy_type') or signal.get('strategy_id') or 'strategy'} "
                "paper experiment admitted after price-integrity checks; "
                "OOS and realised-edge evidence is being collected prospectively"
            )
        if risk_evaluation and isinstance(risk_evaluation, dict):
            risk_accepted = bool(risk_evaluation.get("accepted"))
            reasons.append(f"Risk: {'PASS' if risk_accepted else 'FAIL'} — {risk_evaluation.get('reason', 'N/A')}")
            for check in risk_evaluation.get("checks", []):
                if isinstance(check, dict):
                    check_name = check.get("name") or check.get("check") or "check"
                    check_status = check.get("status") or ("PASS" if check.get("passed") else "FAIL")
                    detail = check.get("detail")
                    suffix = f" ({detail})" if detail else ""
                    reasons.append(f"  - {check_name}: {check_status}{suffix}")
        sentiment_score = signal.get("sentiment_score")
        sentiment_label = signal.get("sentiment_label")
        if sentiment_score is not None:
            reasons.append(f"Sentiment: {sentiment_label} ({sentiment_score:.2f})")
        gap_risk = signal.get("gap_risk")
        if gap_risk is not None:
            reasons.append(f"PreEarnings: gap_risk={gap_risk}")
        radar_opp = signal.get("trade_radar_opportunity")
        if radar_opp is not None:
            reasons.append(f"TradeRadar: opportunity={radar_opp:.1f}")
        composite = signal.get("composite_score")
        if composite is not None:
            reasons.append(f"Composite score: {composite:.2f}")
        tier = signal.get("confidence_tier")
        if tier:
            reasons.append(f"Confidence tier: {tier}")
        confidence_reasons = signal.get("confidence_reasons")
        if isinstance(confidence_reasons, list):
            for cr in confidence_reasons:
                reasons.append(f"  - {cr}")
        if isinstance(ticker_adj, dict) and ticker_adj.get("reason"):
            reasons.append(f"TickerLearning: {ticker_adj.get('reason')}")

        decision_explanation = " | ".join(reasons)
        decision_context["decision_explanation"] = decision_explanation

        logger.info(f"EXECUTION AGENT DECISION: {decision_explanation}")

        try:
            from core.execution.runtime import build_open_intent, get_paper_broker

            order_intent = build_open_intent(
                signal=signal,
                direction=direction,
                size_usd=notional_usd,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                leverage=leverage,
                decision_context=decision_context,
            )
            broker = get_paper_broker(config=self.config_manager, capital_manager=cm)
            order = broker.submit(order_intent)
            if str(order.get("state") or "") != "FILLED":
                reason = str(order.get("error") or "PaperBroker rejected the order")
                record_trade_event(
                    event_type="execution_skipped",
                    status="paper_broker_rejected",
                    ticker=ticker,
                    signal_id=signal_id,
                    strategy_type=decision_context.get("strategy_type", ""),
                    summary=reason,
                    payload={"order": order, "decision_context": decision_context},
                )
                logger.info("EXECUTION AGENT (PAPER): PaperBroker blocked %s: %s", ticker, reason)
                return False
            result = order.get("result") if isinstance(order.get("result"), dict) else {}
            position = result.get("position") if isinstance(result.get("position"), dict) else None
            if not isinstance(position, dict):
                raise RuntimeError("PaperBroker filled order without a position payload")
            decision_context["execution_order"] = {
                "decision_id": order.get("decision_id"),
                "order_intent_id": order.get("order_intent_id"),
                "idempotency_key": order.get("idempotency_key"),
                "state": order.get("state"),
            }
            if str(decision_context.get("strategy_type") or "").upper() == "S8_SUPERTREND_BOS":
                try:
                    entry_chart_snapshot = capture_entry_chart_snapshot(
                        position=position,
                        decision_context=decision_context,
                        timeframe="30m",
                    )
                    decision_context["entry_chart_snapshot"] = entry_chart_snapshot
                    position = cm.attach_entry_chart_snapshot(
                        str(position.get("position_id") or ""),
                        entry_chart_snapshot,
                    )
                    logger.info(
                        "S8 ENTRY CHART: %s %s (%s, %s bars)",
                        ticker,
                        entry_chart_snapshot.get("capture_status"),
                        entry_chart_snapshot.get("timeframe", "30m"),
                        entry_chart_snapshot.get("bar_count", 0),
                    )
                except Exception as chart_exc:
                    # Chart evidence must be transparent, but a storage/provider
                    # failure after a verified paper fill must not roll back or
                    # duplicate the already-open simulated position.
                    logger.error("S8 entry chart capture failed for %s: %s", ticker, chart_exc)
            signal_manager.attach_paper_position(signal_id, position)
            logger.info(
                f"EXECUTION AGENT (PAPER): opened {direction} {ticker} "
                f"at {position.get('entry_price')} size=${position.get('usd_size')}"
            )
            record_trade_event(
                event_type="execution_confirmed",
                status="executed",
                ticker=ticker,
                signal_id=signal_id,
                strategy_type=decision_context.get("strategy_type", ""),
                summary=decision_explanation[:500],
                payload={
                    "signal": dict(signal or {}),
                    "decision_context": decision_context,
                    "decision_explanation": decision_explanation,
                    "position": dict(position or {}),
                },
            )
            return True
        except Exception as exc:
            logger.error(f"EXECUTION AGENT (PAPER): failed to open {ticker}: {exc}")
            record_trade_event(
                event_type="execution_failed",
                status="error",
                ticker=ticker,
                signal_id=signal_id,
                strategy_type=decision_context.get("strategy_type", ""),
                summary=str(exc),
                payload={
                    "signal": dict(signal or {}),
                    "decision_context": decision_context,
                    "decision_explanation": decision_explanation,
                },
            )
            return False
