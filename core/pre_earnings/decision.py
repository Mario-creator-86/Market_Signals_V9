"""§51 — Pre-Earnings autonomy: the decision is NOT based only on the score.

build_pre_earnings_decision_context() assembles the full §51 input stack
(Pre-Earnings + strategy evidence + signal + backtest + AI + critic +
data quality + portfolio + risk + capital). Later-phase inputs that do not
exist yet are HONEST STUBS (available: False + reason) — never faked numbers
(§4/§43). The engine's §42 AUTHORITATIVE DATA CHECK + §43 already reject AI
that tries to invent them.

evaluate_pre_earnings_decision() bridges to core.ai.decision_engine. Per §109
(Decision -> Risk -> Capital -> Paper order), the Pre-Earnings DECISION is the
AI + critic + policy + autonomy conclusion; the capital/risk §42 stages that
depend on Phase 9 services are injected as permissive stubs here ("deferred to
the downstream order path") so the decision reflects the AI+critic conclusion.
The REAL capital/risk gating stays in agent_manager's order path (its own real
guards + the 4C autonomy order gate) — this surface is a recommendation, not an
order. The autonomy clamp (§45/§74/§46) is the final gate: below PAPER_AUTO, or
with the kill switch active, or LIVE_AUTO without acceptance, an order-bearing
action is clamped to WATCH. NO_TRADE (critic veto / policy) is never relaxed to
WATCH. AI-unavailable maps to WATCH ("keep watching"), not a decisive NO_TRADE.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import time
from typing import Any, Dict, Optional, Tuple


def _load_ai_platform() -> None:
    """Import the ``core.ai`` package itself before any of its submodules.

    ``from core.ai.decision import ...`` as the FIRST touch of the package
    takes the submodule's import lock and only then initialises the parent.
    When another thread is importing a ``core.ai`` submodule at the same moment
    (the AgentManager monitor and the API start at the same time), each thread
    ends up waiting for a lock the other holds; CPython breaks the tie by
    failing one import with ``_DeadlockError`` and the other with
    ``KeyError: 'core.ai'`` — the "pre-earnings autonomous load failed:
    'core.ai'" warning logged at every server start. Importing the package
    first means this thread never holds a submodule lock while waiting, and
    the short retry covers the case where this thread is the one initialising
    the package while another thread holds a submodule it needs.
    """
    for attempt in range(4):
        try:
            importlib.import_module("core.ai")
            return
        except (RuntimeError, KeyError):
            if attempt == 3:
                raise
            time.sleep(0.05 * (attempt + 1))


_load_ai_platform()

from core.ai.decision import Action, Decision
from core.ai.decision_engine import EvaluationResult, evaluate_trade_decision
from core.signal_engine import Signal
from core.autonomy import (
    can_enable_live_auto, kill_switch_active, mode_allows_paper_orders, resolve_mode,
)


def _honest_stub(reason: str) -> Dict[str, Any]:
    return {"available": False, "reason": reason}


def _signal_input(item: Dict[str, Any]) -> Dict[str, Any]:
    """§51 'signal' input — real (Phase 6) when the item carries signal fields,
    else an honest stub. The Signal overlay (§52/§53) is built from the item;
    is_not_probability is always True (§55 — never display as a probability)."""
    keys = ("symbol", "ticker", "direction", "side", "signal_score", "score",
            "total_score", "mode", "strategy_id", "signal_type", "source")
    raw = {k: item[k] for k in keys if k in item}
    if not raw.get("symbol") and not raw.get("ticker"):
        return _honest_stub("signal_not_available_on_item")
    if not (raw.get("direction") or raw.get("signal_score") or raw.get("score")
           or raw.get("total_score") or raw.get("mode") or raw.get("strategy_id")):
        return _honest_stub("signal_not_available_on_item")
    sig = Signal.from_dict(raw)
    return {
        "available": True,
        "signal": sig.to_dict(),
        "horizon": sig.horizon.value,
        "source": sig.source,
        "direction": sig.direction,
        "score": sig.score,
        "is_not_probability": True,
    }


def _pe_default_services() -> Dict[str, Any]:
    """Permissive §42 capital/risk stubs for the Pre-Earnings DECISION surface.

    §109 orders Decision before Risk/Capital. The capital_entry + risk §42
    stages depend on Phase 9 services (capital/portfolio/risk) that are not
    built yet, so they are injected as permissive stubs here — the decision
    reflects the AI+critic+policy+autonomy conclusion, and the REAL capital/risk
    gating runs in agent_manager's order path. Caller-provided services (e.g.
    AI providers in tests) override these via _merged_services.
    """

    class _RA:
        def evaluate_signal(self, s):
            return {"accepted": True, "reason": "risk deferred to downstream order path (Phase 9)",
                    "checks": [], "metrics": {}}

    return {
        "evaluate_capital_entry": lambda s, p, c=None, **k: {
            "allowed": True, "check": "deferred", "status": "deferred",
            "reason": "capital deferred to downstream order path (Phase 9)", "checks": {}},
        "resolve_deployable_balance": lambda cm: 50000.0,
        "compute_size_factor": lambda score, cfg=None, **k: {"size_factor": 0.5},
        "calculate_trade_risk_pct": lambda **k: 0.3,
        "risk_agent": _RA(),
    }


def _merged_services(services: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    base = _pe_default_services()
    if services:
        base.update(services)  # caller overrides (e.g. primary_provider / critic_provider)
    return base


def build_pre_earnings_decision_context(
    item: Dict[str, Any],
    *,
    services: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the §51 input stack for a Pre-Earnings candidate."""
    return {
        "pre_earnings": {
            "available": True,
            "score": item.get("pre_earnings_score"),
            "label": item.get("pre_earnings_score_label"),
            "gap_risk": item.get("gap_risk"),
            "direction_bias": item.get("direction_bias"),
            "data_confidence": item.get("data_confidence"),
            "coverage_score": item.get("coverage_score"),
            "coverage_label": item.get("coverage_label"),
            # One day's high-low range, not an options-implied earnings move.
            "intraday_range_proxy_pct": item.get("expected_move_pct"),
            "historical_median_move_pct": item.get("historical_median_move_pct"),
            "session": item.get("session"),
            "earnings_time_utc": item.get("earnings_time_utc"),
        },
        "strategy_evidence": _honest_stub("strategy registry not built (Phase 8)"),
        "signal": _signal_input(item),
        "backtest": _honest_stub("backtest engine not built (Phase 7)"),
        "ai": {"available": True, "note": "AI Analyst via decision_engine (§41)"},
        "critic": {"available": True, "note": "AI Critic via decision_engine (§44)"},
        "data_quality": {
            "available": True,
            "data_confidence": item.get("data_confidence"),
            "coverage_score": item.get("coverage_score"),
            "news_only_call": bool(item.get("news_only_call")),
        },
        "portfolio": _honest_stub("portfolio service not built (Phase 9)"),
        "risk": {
            "available": bool(item.get("risk_evaluation")),
            "evaluation": item.get("risk_evaluation"),
        },
        "capital": _honest_stub("capital service not built (Phase 9)"),
    }


def _build_engine_signal(item: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ticker": item.get("symbol") or item.get("ticker") or "",
        "entry": item.get("price") or item.get("last_price"),
        "strategy_type": "PRE_EARNINGS",
        "signal_score": (float(item.get("pre_earnings_score") or 0) / 100.0),
        "horizon": "SWING",
        "direction_bias": item.get("direction_bias"),
        "gap_risk": item.get("gap_risk"),
        "pre_earnings_score": item.get("pre_earnings_score"),
        "data_quality": float(item.get("coverage_score") or 0) / 100.0,
        "thesis": (f"Pre-Earnings score {item.get('pre_earnings_score')} "
                   f"({item.get('pre_earnings_score_label')}), "
                   f"confidence {item.get('data_confidence')}, "
                   f"coverage {item.get('coverage_score')}."),
        "risk_flags": ["news_only_call"] if item.get("news_only_call") else [],
        # §51 stack (engine prompt surfaces these; stubs are honest unavailable)
        "pre_earnings_context": ctx["pre_earnings"],
        "strategy_evidence": ctx["strategy_evidence"],
        "signal_evidence": ctx["signal"],
        "backtest_evidence": ctx["backtest"],
        "data_quality_context": ctx["data_quality"],
        "portfolio_context": ctx["portfolio"],
        "capital_context": ctx["capital"],
        "risk_context": ctx["risk"],
    }


def decision_inputs_fingerprint(item: Dict[str, Any], config: Any = None) -> str:
    """Identifies the question the §51 decision answers for this item.

    Covers everything the answer is made from: the signal handed to the
    decision engine (scores, direction bias, gap risk, data quality, the
    earnings event, the risk evaluation), the autonomy inputs that turn an
    OPEN answer into WATCH, and the model assigned to the role. The same
    fingerprint means the model would be asked the very same question again.
    """
    cfg = config if isinstance(config, dict) else {}
    try:
        from core.ai.role_resolution import assigned_model

        model = assigned_model("pre_earnings_analyst", config=cfg, fallback="") or ""
    except Exception:  # noqa: BLE001 - a broken assignment must not stop the decision pass
        model = ""
    question = {
        "signal": _build_engine_signal(item, build_pre_earnings_decision_context(item)),
        "autonomy": [resolve_mode(cfg).value, bool(kill_switch_active(cfg))],
        "model": model,
    }
    canonical = json.dumps(question, sort_keys=True, default=str)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def _with_reason(review: Dict[str, Any], reason: str) -> Dict[str, Any]:
    out = dict(review or {})
    out["reason"] = reason
    out["recommendation"] = "watch"
    return out


def _autonomy_clamp(
    result: EvaluationResult, config: Dict[str, Any],
) -> Tuple[Action, Dict[str, Any]]:
    """Return (effective_action, review) after the Pre-Earnings autonomy clamp.

    Only ORDER-bearing actions (OPEN_LONG/OPEN_SHORT) are gated by autonomy /
    kill switch — they are clamped to WATCH when the mode forbids orders, the
    kill switch is active, or LIVE_AUTO lacks acceptance. NO_TRADE (critic veto
    / policy rejection) and WATCH/HOLD/REDUCE/CLOSE pass through unchanged; a
    decisive NO_TRADE is never relaxed to WATCH. AI-unavailable maps to WATCH.
    """
    review = result.review or {}
    if not result.ai_available:
        return Action.WATCH, _with_reason(review, "ai_unavailable: keep watching")
    eff = review.get("decision")
    if not isinstance(eff, Decision):
        eff = result.decision
    action = eff.action
    if action in (Action.OPEN_LONG, Action.OPEN_SHORT):
        if kill_switch_active(config):
            return Action.WATCH, _with_reason(review, "kill_switch_active")
        mode = resolve_mode(config)
        if not mode_allows_paper_orders(mode):
            return Action.WATCH, _with_reason(review, f"autonomy_mode_{mode.value}")
        if mode.value == "LIVE_AUTO":
            ok, why = can_enable_live_auto(config)
            if not ok:
                return Action.WATCH, _with_reason(review, f"live_auto_blocked:{why}")
    return action, review


def evaluate_pre_earnings_decision(
    item: Dict[str, Any],
    *,
    config: Dict[str, Any],
    services: Optional[Dict[str, Any]] = None,
) -> EvaluationResult:
    """§51 — bridge a Pre-Earnings item to the decision engine + autonomy clamp.

    The returned EvaluationResult.decision is the AUTHORITATIVE final action
    (from the engine's review.decision, possibly clamped by autonomy) — so
    callers read result.decision.action for the §51 action.
    """
    ctx = build_pre_earnings_decision_context(item, services=services)
    signal = _build_engine_signal(item, ctx)
    risk_result = item.get("risk_evaluation")
    result = evaluate_trade_decision(
        signal,
        risk_result,
        config=config,
        services=_merged_services(services),
        role="pre_earnings_analyst",
    )

    clamped_action, clamped_review = _autonomy_clamp(result, config)
    eff = (result.review or {}).get("decision")
    if not isinstance(eff, Decision):
        eff = result.decision

    if clamped_action is not eff.action:
        clamped_decision = Decision(
            action=clamped_action,
            ticker=eff.ticker,
            confidence=eff.confidence,
            thesis=clamped_review.get("reason", eff.thesis),
            raw=eff.raw,
        )
        return EvaluationResult(
            clamped_decision, result.validation, result.critic, clamped_review, result.ai_available,
        )
    # Normalize .decision to the authoritative (review) decision so callers read
    # the final action even when the pipeline rejected (review.decision != result.decision).
    if eff is not result.decision:
        return EvaluationResult(eff, result.validation, result.critic, result.review, result.ai_available)
    return result
