"""Runtime construction and deterministic intent factories."""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Any, Mapping, Optional

from .broker import PaperBroker
from .policy import evaluate_paper_entry_readiness
from .store import ExecutionStore
from .types import OrderAction, OrderIntent


_BROKERS: dict[tuple[str, int, int], PaperBroker] = {}
_LOCK = threading.RLock()


def _root(config: Any) -> dict[str, Any]:
    if isinstance(config, Mapping):
        return dict(config)
    value = getattr(config, "config", {})
    return dict(value) if isinstance(value, Mapping) else {}


def execution_store_path(config: Any = None) -> Path:
    root = _root(config)
    paper = root.get("paper_execution") if isinstance(root.get("paper_execution"), Mapping) else {}
    configured = str(paper.get("store_path") or "").strip()
    override = str(os.getenv("MS_EXECUTION_DB_PATH") or "").strip()
    if override:
        return Path(override)
    # "data/execution.db" (the configured default) means the configured data
    # folder (MS_DATA_DIR), never a path relative to the working directory.
    from core.config import data_path, resolve_state_path

    return resolve_state_path(configured, data_path("execution.db"))


def get_paper_broker(
    *,
    config: Any = None,
    capital_manager: Any = None,
    store: Optional[ExecutionStore] = None,
) -> PaperBroker:
    if capital_manager is None:
        from core.capital_manager import get_capital_manager

        capital_manager = get_capital_manager()
    ledger = store or ExecutionStore(execution_store_path(config))
    key = (str(ledger.path.resolve()), id(capital_manager), id(config))
    with _LOCK:
        existing = _BROKERS.get(key)
        if existing is not None:
            return existing

        def authorize() -> tuple[bool, str]:
            reconciliation = ledger.get_reconciliation()
            readiness = evaluate_paper_entry_readiness(
                config,
                reconciliation_status=str(reconciliation.get("status") or "UNVERIFIED"),
            )
            return readiness.ready, "; ".join(readiness.reasons)

        broker = PaperBroker(
            capital_manager=capital_manager,
            store=ledger,
            new_risk_authorizer=authorize,
        )
        _BROKERS[key] = broker
        return broker


def _identifier(prefix: str, *parts: Any) -> str:
    material = "|".join(str(part or "").strip() for part in parts)
    return f"{prefix}_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def build_open_intent(
    *,
    signal: Mapping[str, Any],
    direction: str,
    size_usd: float,
    stop_loss_pct: float,
    take_profit_pct: float,
    leverage: int,
    decision_context: Mapping[str, Any],
) -> OrderIntent:
    signal_id = str(signal.get("id") or "").strip()
    ticker = str(signal.get("ticker") or "").upper().strip()
    context = dict(decision_context or {})
    decision_id = str(context.get("decision_id") or signal.get("decision_id") or signal_id).strip()
    idempotency_key = f"paper:open:{decision_id}:{signal_id}:{ticker}:{str(direction).upper()}"
    order_intent_id = _identifier("ord", idempotency_key)
    return OrderIntent(
        decision_id=decision_id,
        order_intent_id=order_intent_id,
        idempotency_key=idempotency_key,
        ticker=ticker,
        action=OrderAction.OPEN_LONG if str(direction).upper() == "LONG" else OrderAction.OPEN_SHORT,
        signal_id=signal_id,
        size_usd=size_usd,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        leverage=leverage,
        reason=str(context.get("reason") or "paper entry"),
        metadata={
            "decision_context": context,
            "note": str(signal.get("strategy_id") or signal.get("signal_mode") or "paper"),
            "strategy_type": str(context.get("strategy_type") or signal.get("strategy_type") or ""),
            "entry_reason": str(context.get("reason") or ""),
            "expected_entry_price": signal.get("expected_entry_price"),
            "max_entry_deviation_pct": signal.get("max_entry_deviation_pct"),
        },
    )


def build_position_intent(
    *,
    decision_id: str,
    position: Mapping[str, Any],
    action: OrderAction,
    reason: str,
    reduce_fraction: Optional[float] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> OrderIntent:
    position_id = str(position.get("position_id") or "").strip()
    ticker = str(position.get("ticker") or "").upper().strip()
    normalized_decision = str(decision_id or "").strip()
    idempotency_key = f"paper:{action.value.lower()}:{normalized_decision}:{position_id}"
    intent_metadata = dict(metadata or {})
    intent_metadata.setdefault(
        "position_before",
        {
            "status": str(position.get("status") or ""),
            "usd_size": position.get("usd_size"),
            "qty": position.get("qty"),
        },
    )
    return OrderIntent(
        decision_id=normalized_decision,
        order_intent_id=_identifier("ord", idempotency_key),
        idempotency_key=idempotency_key,
        ticker=ticker,
        action=action,
        signal_id=str(position.get("signal_id") or ""),
        position_id=position_id,
        reduce_fraction=reduce_fraction,
        reason=reason,
        metadata=intent_metadata,
    )


__all__ = [
    "build_open_intent",
    "build_position_intent",
    "execution_store_path",
    "get_paper_broker",
]
