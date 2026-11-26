"""Execute deterministic Section 67 HOLD/REDUCE/CLOSE assessments."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from core.portfolio import PositionAction, reassess_position

from .broker import ExecutionBroker
from .runtime import build_position_intent
from .types import OrderAction


class PositionManagementService:
    def __init__(self, broker: ExecutionBroker):
        self.broker = broker

    def reassess_and_execute(
        self,
        *,
        position: Mapping[str, Any],
        current_state: Mapping[str, Any],
        decision_id: str,
        reduce_fraction_pct: Optional[float],
    ) -> dict[str, Any]:
        assessment = reassess_position(position, current_state)
        result: dict[str, Any] = {"assessment": assessment.to_dict(), "executed": False, "order": None}
        if assessment.action is PositionAction.HOLD:
            return result

        metadata = {
            key: current_state.get(key)
            for key in (
                "exit_price",
                "exit_execution_data",
                "trigger_price",
                "enforce_exit_reason",
            )
            if key in current_state
        }
        reason = "; ".join(assessment.reasons) or assessment.action.value.lower()
        if assessment.action is PositionAction.REDUCE:
            try:
                percent = float(reduce_fraction_pct) if reduce_fraction_pct is not None else 0.0
            except (TypeError, ValueError):
                percent = 0.0
            if not (0.0 < percent < 100.0):
                result["blocked_reason"] = "owner-selected reduce_fraction_pct is required"
                return result
            action = OrderAction.REDUCE
            fraction = percent / 100.0
        else:
            action = OrderAction.CLOSE
            fraction = None

        intent = build_position_intent(
            decision_id=decision_id,
            position=position,
            action=action,
            reason=reason,
            reduce_fraction=fraction,
            metadata=metadata,
        )
        order = self.broker.submit(intent)
        result["order"] = order
        result["executed"] = order.get("state") == "FILLED"
        return result


__all__ = ["PositionManagementService"]
