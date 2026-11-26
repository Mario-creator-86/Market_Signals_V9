"""Strict order intent and lifecycle types for Phase 11."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OrderState(str, Enum):
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class OrderAction(str, Enum):
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"

    @property
    def opens_new_risk(self) -> bool:
        return self in {OrderAction.OPEN_LONG, OrderAction.OPEN_SHORT}


TERMINAL_STATES = frozenset(
    {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.FAILED}
)

ALLOWED_TRANSITIONS = {
    OrderState.PROPOSED: frozenset(
        {OrderState.VALIDATED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.FAILED}
    ),
    OrderState.VALIDATED: frozenset(
        {OrderState.SUBMITTED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.FAILED}
    ),
    OrderState.SUBMITTED: frozenset(
        {
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.FAILED,
        }
    ),
    OrderState.ACKNOWLEDGED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.FAILED,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELLED, OrderState.FAILED}
    ),
}


def validate_transition(current: OrderState | str, target: OrderState | str) -> None:
    source = current if isinstance(current, OrderState) else OrderState(str(current))
    destination = target if isinstance(target, OrderState) else OrderState(str(target))
    if destination not in ALLOWED_TRANSITIONS.get(source, frozenset()):
        raise ValueError(f"invalid order transition: {source.value} -> {destination.value}")


@dataclass(frozen=True)
class OrderIntent:
    decision_id: str
    order_intent_id: str
    idempotency_key: str
    ticker: str
    action: OrderAction
    signal_id: str = ""
    position_id: str = ""
    size_usd: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    leverage: int = 1
    reduce_fraction: Optional[float] = None
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        for name in ("decision_id", "order_intent_id", "idempotency_key", "ticker"):
            value = str(getattr(self, name) or "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "ticker", self.ticker.upper())
        action = self.action if isinstance(self.action, OrderAction) else OrderAction(str(self.action))
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "signal_id", str(self.signal_id or "").strip())
        object.__setattr__(self, "position_id", str(self.position_id or "").strip())
        object.__setattr__(self, "leverage", int(self.leverage or 1))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata or {})))

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "order_intent_id": self.order_intent_id,
            "idempotency_key": self.idempotency_key,
            "ticker": self.ticker,
            "action": self.action.value,
            "signal_id": self.signal_id,
            "position_id": self.position_id,
            "size_usd": self.size_usd,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "leverage": self.leverage,
            "reduce_fraction": self.reduce_fraction,
            "reason": str(self.reason or ""),
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


__all__ = [
    "ALLOWED_TRANSITIONS",
    "OrderAction",
    "OrderIntent",
    "OrderState",
    "TERMINAL_STATES",
    "utc_now_iso",
    "validate_transition",
]
