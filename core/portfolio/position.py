"""Section 67 position evidence persistence and deterministic reassessment."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from .types import PositionAction, PositionAssessment, PositionEvidence


def _text(value: Any) -> str:
    return str(value or "").strip()


def _unique_text(*values: Any) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            text = _text(item)
            if text and text not in result:
                result.append(text)
    return tuple(result)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _invalidation_rule(position: Mapping[str, Any], context: Mapping[str, Any]) -> tuple[str, ...]:
    configured = context.get("invalidation_rules") or context.get("invalidation_condition")
    evidence = _mapping(context.get("strategy_evidence"))
    configured = configured or evidence.get("invalidation_rules") or evidence.get("invalidation_condition")
    rules = _unique_text(configured)
    if rules:
        return rules
    try:
        stop = float(position.get("stop_loss_price") or 0.0)
    except (TypeError, ValueError):
        stop = 0.0
    direction = _text(position.get("direction") or "LONG").upper()
    if stop > 0:
        comparator = "at or below" if direction == "LONG" else "at or above"
        return (f"close {comparator} {stop:.8f}",)
    return ("close when the recorded app-authoritative invalidation is triggered",)


def normalize_position_evidence(
    position: Mapping[str, Any],
    *,
    decision_context: Optional[Mapping[str, Any]] = None,
    strategy_type: str = "",
    entry_reason: str = "",
) -> dict[str, Any]:
    """Return the exact eight persisted section 67 evidence fields."""

    if not isinstance(position, Mapping):
        raise TypeError("position must be a mapping")
    context = _mapping(decision_context)
    ai_decision = _mapping(context.get("ai_decision"))
    ai_critic = _mapping(context.get("ai_critic"))
    execution = _mapping(context.get("execution"))
    risk_evaluation = _mapping(context.get("risk_evaluation"))
    risk_context = _mapping(context.get("risk"))

    thesis = _text(
        ai_decision.get("thesis")
        or context.get("reason")
        or entry_reason
        or position.get("entry_reason")
        or position.get("note")
        or "system position entered from recorded levels"
    )
    supporting = _unique_text(
        ai_decision.get("supporting_evidence"),
        context.get("reasons"),
    )
    opposing = _unique_text(
        ai_decision.get("opposing_evidence"),
        ai_critic.get("categories"),
    )
    strategy = _text(
        strategy_type
        or context.get("strategy_type")
        or position.get("strategy_type")
        or "SYSTEM"
    ).upper()
    source_ids = position.get("source_signal_ids")
    if not isinstance(source_ids, (list, tuple)):
        source_ids = []
    signal = {
        "signal_id": _text(position.get("signal_id") or context.get("signal_id")),
        "source_signal_id": _text(position.get("source_signal_id")),
        "source_signal_ids": list(_unique_text(source_ids)),
        "ticker": _text(position.get("ticker") or context.get("ticker")).upper(),
        "direction": _text(position.get("direction") or context.get("action")).upper(),
    }
    ai_model = _text(
        ai_decision.get("model")
        or context.get("ai_model")
        or execution.get("model")
        or execution.get("analysis_provider")
    ) or None
    if ai_model and ai_model.lower() in {"manual", "none", "n/a", "unavailable"}:
        ai_model = None
    accepted = risk_evaluation.get("accepted")
    if accepted is False:
        risk_status = "REJECTED"
    elif accepted is True:
        risk_status = "NORMAL"
    else:
        risk_status = _text(risk_context.get("status") or "UNASSESSED").upper()
    risk_state = {
        "status": risk_status,
        "evaluation": risk_evaluation,
        "profile": risk_context,
    }
    evidence = PositionEvidence(
        entry_thesis=thesis,
        supporting_evidence=supporting,
        opposing_evidence=opposing,
        strategy=strategy,
        signal=signal,
        ai_model=ai_model,
        invalidation_rules=_invalidation_rule(position, context),
        risk_state=risk_state,
    )
    return evidence.to_dict()


def reassess_position(
    position: Mapping[str, Any],
    current_state: Mapping[str, Any],
    *,
    assessed_at: Optional[str] = None,
) -> PositionAssessment:
    """Return HOLD/REDUCE/CLOSE from app-authoritative state and recorded rules."""

    if not isinstance(position, Mapping) or not isinstance(current_state, Mapping):
        raise TypeError("position and current_state must be mappings")
    timestamp = assessed_at or datetime.now(timezone.utc).isoformat()
    critical_rules: list[str] = []
    reduce_rules: list[str] = []
    reasons: list[str] = []

    if current_state.get("invalidation_triggered") is True:
        critical_rules.append("invalidation_triggered")
        reasons.append("recorded entry thesis was invalidated")

    risk_status = _text(current_state.get("risk_status") or current_state.get("status")).upper()
    if risk_status in {"CRITICAL", "BREACH", "LIQUIDATION_RISK"}:
        critical_rules.append("critical_risk_state")
        reasons.append(f"risk state is {risk_status}")
    elif risk_status in {"DEGRADED", "WARNING", "ELEVATED"}:
        reduce_rules.append("degraded_risk_state")
        reasons.append(f"risk state is {risk_status}")

    try:
        current_price = float(current_state.get("current_price") or 0.0)
        stop_price = float(position.get("stop_loss_price") or 0.0)
    except (TypeError, ValueError):
        current_price = stop_price = 0.0
    direction = _text(position.get("direction") or "LONG").upper()
    stop_reached = (
        current_price > 0
        and stop_price > 0
        and (
            (direction == "LONG" and current_price <= stop_price)
            or (direction == "SHORT" and current_price >= stop_price)
        )
    )
    if stop_reached:
        critical_rules.append("stop_loss_reached")
        reasons.append("app-authoritative price reached the recorded stop")

    if current_state.get("reduce_required") is True:
        breached = current_state.get("breached_constraints")
        reduce_rules.extend(_unique_text(breached) or ("reduce_required",))
        reasons.append("non-critical portfolio constraint requires exposure reduction")

    if critical_rules:
        return PositionAssessment(
            PositionAction.CLOSE,
            tuple(reasons),
            tuple(dict.fromkeys(critical_rules)),
            timestamp,
        )
    if reduce_rules:
        return PositionAssessment(
            PositionAction.REDUCE,
            tuple(reasons),
            tuple(dict.fromkeys(reduce_rules)),
            timestamp,
        )

    if current_state.get("kill_switch_active") is True:
        reasons.append("kill_switch_monitoring_active")
    reasons.append("no invalidation or risk reduction trigger")
    return PositionAssessment(PositionAction.HOLD, tuple(reasons), (), timestamp)


def reassess_positions(
    positions: Iterable[Mapping[str, Any]],
    state_by_position_id: Mapping[str, Mapping[str, Any]],
    *,
    assessed_at: Optional[str] = None,
) -> dict[str, PositionAssessment]:
    """Reassess every open position without performing execution mutations."""

    results: dict[str, PositionAssessment] = {}
    for position in positions:
        if not isinstance(position, Mapping) or _text(position.get("status")).upper() != "OPEN":
            continue
        position_id = _text(position.get("position_id"))
        if not position_id:
            continue
        state = state_by_position_id.get(position_id, {})
        results[position_id] = reassess_position(position, state, assessed_at=assessed_at)
    return results


__all__ = [
    "normalize_position_evidence",
    "reassess_position",
    "reassess_positions",
]
