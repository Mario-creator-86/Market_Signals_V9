"""Phase 13 recorder — decision_context + engine artifacts → one §97 entry.

Called from agent_manager immediately after build_signal_decision_context on
BOTH the accepted and the rejected path (§90 records no-trade decisions).
Fail-open by design: the ledger is an audit surface, never a trading gate —
a ledger failure is logged and swallowed, the trading decision proceeds.

Idempotent on decision_id: scheduler redelivery returns the original entry.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from core.ai.decision_engine import EvaluationResult
from core.decision_ledger.store import DecisionLedgerStore
from core.decision_ledger.types import (
    LedgerEntry, PROMPT_TEXT_CAP, SOURCE_PRE_EARNINGS_PATH, SOURCE_SIGNAL_PATH,
    bounded_text, critic_projection, decision_projection, json_digest,
    sha256_digest, validation_projection,
)

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _horizon_of(signal: Dict[str, Any], decision: Any) -> str:
    horizon = getattr(decision, "horizon", None) if decision is not None else None
    horizon = getattr(horizon, "value", horizon) or (signal or {}).get("horizon") or ""
    return str(horizon or "")


def _action_of(decision: Any, review: Dict[str, Any], status: str) -> str:
    action = getattr(decision, "action", None)
    action = getattr(action, "value", action)
    if action:
        return str(action)
    recommendation = str((review or {}).get("recommendation") or "").upper()
    if recommendation:
        return {"APPROVE": "OPEN", "AVOID": "NO_TRADE", "WATCH": "WATCH", "REDUCE": "REDUCE"}.get(
            recommendation, recommendation
        )
    return "NO_TRADE" if status == "rejected" else "HOLD"


def _source_of(signal: Dict[str, Any]) -> str:
    autonomous_source = str((signal or {}).get("autonomous_source") or "").lower()
    if autonomous_source == "pre_earnings":
        return SOURCE_PRE_EARNINGS_PATH
    return SOURCE_SIGNAL_PATH


def _ai_evidence(eval_result: Optional[EvaluationResult], review: Dict[str, Any]) -> Dict[str, Any]:
    """§97 'ai_model' + 'prompt' — provider/model/version/digests + capped prompt."""
    if eval_result is None and not review:
        # Rejected signals intentionally skip the AI review (no call is burned
        # on a risk-rejected candidate) — record that fact explicitly instead
        # of an empty unavailable shape that reads like an AI failure.
        return {
            "consulted": False,
            "reason": "ai_review_not_consulted_risk_rejected",
            "available": False,
        }
    prompt = getattr(eval_result, "prompt", None) if eval_result is not None else None
    model = str(getattr(eval_result, "model", "") or "") if eval_result is not None else ""
    prompt_version = str(getattr(eval_result, "prompt_version", "") or "") if eval_result is not None else ""
    provider = str((review or {}).get("provider") or "") or "unknown"
    return {
        "consulted": True,
        "available": bool((review or {}).get("available")),
        "provider": provider,
        "model": model,
        "prompt_version": prompt_version,
        "prompt_sha256": sha256_digest(prompt) if prompt else "",
        "prompt_chars": len(prompt or ""),
        "prompt_text": bounded_text(prompt) if prompt else "",
        "response_fallback_used": str((review or {}).get("fallback_used") or ""),
    }


def _data_existed(signal: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """§97 'data_existed' — bounded point-in-time input facts + snapshot digest.

    Scalar facts only; nested evidence is digest-referenced, and external
    News/SEC strings were already quarantined upstream (Phase 10).
    """
    payload = signal or {}
    facts = {
        "signal_id": str(payload.get("id") or ""),
        "entry": payload.get("entry") or payload.get("entry_price"),
        "signal_score": payload.get("signal_score"),
        "horizon": str(getattr(payload.get("horizon"), "value", payload.get("horizon")) or ""),
        "strategy_type": payload.get("strategy_type"),
        "autonomous_source": payload.get("autonomous_source"),
        "data_quality": payload.get("data_quality"),
        "price_integrity_status": (payload.get("price_integrity") or {}).get("status")
        if isinstance(payload.get("price_integrity"), dict) else None,
        "price_verified": payload.get("price_verified"),
        "has_data_conflict": payload.get("has_data_conflict"),
        "strategy_status": payload.get("strategy_status"),
        "average_daily_volume_usd": payload.get("average_daily_volume_usd"),
        "news_score": payload.get("news_score"),
        "pre_earnings_score": payload.get("pre_earnings_score"),
        "scanner_context_keys": sorted((payload.get("scanner_context") or {}).keys())
        if isinstance(payload.get("scanner_context"), dict) else [],
        "strategy_evidence_sha256": json_digest(payload.get("strategy_evidence") or {}),
        "risk_flags": payload.get("risk_flags") or [],
        "captured_at": _utc_now_iso(),
    }
    return {k: v for k, v in facts.items() if v not in (None, "", [])}


def build_ledger_entry(
    signal: Dict[str, Any],
    context: Dict[str, Any],
    *,
    eval_result: Optional[EvaluationResult] = None,
    review: Optional[Dict[str, Any]] = None,
    capital_evidence: Optional[Dict[str, Any]] = None,
    execution_evidence: Optional[Dict[str, Any]] = None,
    created_at: Optional[str] = None,
) -> LedgerEntry:
    """Assemble the §97 record from the app-authoritative decision context."""
    ctx = context or {}
    review_dict = dict(review or {})
    decision = getattr(eval_result, "decision", None) if eval_result is not None else None
    validation_result = getattr(eval_result, "validation", None) if eval_result is not None else None
    critic_result = getattr(eval_result, "critic", None) if eval_result is not None else None
    status = str(ctx.get("decision_status") or ("accepted" if ctx.get("risk_evaluation", {}).get("accepted") else "rejected"))
    risk_evaluation = ctx.get("risk_evaluation") or {}
    ai_evidence = _ai_evidence(eval_result, review_dict)
    critic_evidence = critic_projection(critic_result)
    if critic_result is None and validation_result is not None and not bool(getattr(validation_result, "ok", False)):
        stage = getattr(validation_result, "stage", None)
        stage_value = str(getattr(stage, "value", stage) or "VALIDATION")
        critic_evidence = {
            "available": False,
            "skipped": True,
            "reason": "skipped_after_ai_validation_veto",
            "upstream_stage": stage_value,
            "upstream_reason": bounded_text(getattr(validation_result, "reason", ""), 2000),
        }

    entry = LedgerEntry(
        decision_id=str(ctx.get("decision_id") or ""),
        created_at=str(created_at or ctx.get("timestamp") or _utc_now_iso()),
        ticker=str(ctx.get("ticker") or (signal or {}).get("ticker") or "").upper(),
        action=_action_of(decision, review_dict, status),
        horizon=_horizon_of(signal or {}, decision),
        source=_source_of(signal or {}),
        status=status,
        strategy=str(ctx.get("strategy_type") or ""),
        ai=ai_evidence,
        ai_proposal=decision_projection(decision if decision is not None else review_dict.get("decision")),
        validation=validation_projection(validation_result),
        critic=critic_evidence,
        risk={
            "accepted": bool(risk_evaluation.get("accepted")),
            "reason": bounded_text(risk_evaluation.get("reason"), 2000),
            "veto": bool(
                (getattr(eval_result, "validation", None) is not None
                 and getattr(eval_result.validation, "stage", "") == "RISK_GATE"
                 and not getattr(eval_result.validation, "ok", True))
                or review_dict.get("critic_veto")
            ),
            "checks": risk_evaluation.get("checks") or [],
            "risk_profile": ctx.get("risk") or {},
        },
        capital_allowed=dict(capital_evidence or ctx.get("sizing") or {}),
        executed=dict(execution_evidence or {}),
        data_existed=_data_existed(signal or {}, ctx),
        debate=_debate_projection(ctx.get("ai_debate")),
    )
    if not entry.decision_id:
        raise ValueError("ledger entry requires a decision_id")
    return entry


def _debate_projection(raw: Any) -> Optional[Dict[str, Any]]:
    """Bounded projection of the bull/bear debate stored on the decision context."""
    if not isinstance(raw, dict):
        return None
    verdict = raw.get("verdict") if isinstance(raw.get("verdict"), dict) else {}
    return {
        "available": bool(raw.get("available")),
        "prompt_version": str(raw.get("prompt_version") or ""),
        "bull": bounded_text(raw.get("bull"), 2000),
        "bear": bounded_text(raw.get("bear"), 2000),
        "verdict": {
            "recommendation": str(verdict.get("recommendation") or ""),
            "confidence": verdict.get("confidence"),
            "rationale": bounded_text(verdict.get("rationale"), 1200),
            "bull_score": verdict.get("bull_score"),
            "bear_score": verdict.get("bear_score"),
        },
        "models": dict(raw.get("models") or {}),
        "error": bounded_text(raw.get("error"), 200),
    }


def record_decision(
    store: DecisionLedgerStore,
    signal: Dict[str, Any],
    context: Dict[str, Any],
    *,
    eval_result: Optional[EvaluationResult] = None,
    review: Optional[Dict[str, Any]] = None,
    capital_evidence: Optional[Dict[str, Any]] = None,
    execution_evidence: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[LedgerEntry], bool]:
    """Append one entry; returns (entry, created). Raises ValueError only for
    a missing decision_id (caller bug) — storage errors are logged, not raised."""
    entry = build_ledger_entry(
        signal, context, eval_result=eval_result, review=review,
        capital_evidence=capital_evidence, execution_evidence=execution_evidence,
    )
    try:
        return store.append_entry(entry)
    except Exception:
        logger.exception("decision ledger append failed for %s", entry.decision_id)
        return None, False


def record_decision_fail_open(
    store: DecisionLedgerStore,
    signal: Dict[str, Any],
    context: Dict[str, Any],
    **kwargs: Any,
) -> None:
    """agent_manager hook — audit failure must never block the trading path."""
    try:
        record_decision(store, signal, context, **kwargs)
    except Exception:
        logger.exception("decision ledger recording failed (fail-open)")


__all__ = [
    "build_ledger_entry", "record_decision", "record_decision_fail_open",
    "PROMPT_TEXT_CAP",
]
