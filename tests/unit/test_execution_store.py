import sqlite3

import pytest

from core.execution.store import ExecutionStore, SCHEMA_VERSION
from core.execution.types import OrderAction, OrderIntent, OrderState


def _intent(key="same-key"):
    return OrderIntent(
        decision_id="decision-1",
        order_intent_id="order-1",
        idempotency_key=key,
        ticker="AAPL",
        action=OrderAction.OPEN_LONG,
        signal_id="signal-1",
        size_usd=100.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
    )


def test_store_migration_is_repeatable_and_preserves_orders(tmp_path):
    path = tmp_path / "execution.db"
    store = ExecutionStore(path)
    created, is_new = store.create_order(_intent())
    assert is_new is True
    assert created["state"] == "PROPOSED"

    assert store.migrate() == SCHEMA_VERSION
    reopened = ExecutionStore(path)
    assert reopened.get_order("order-1")["idempotency_key"] == "same-key"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_unique_idempotency_key_returns_original_order(tmp_path):
    store = ExecutionStore(tmp_path / "execution.db")
    first, first_created = store.create_order(_intent())
    duplicate, duplicate_created = store.create_order(
        OrderIntent(
            decision_id="decision-2",
            order_intent_id="order-2",
            idempotency_key="same-key",
            ticker="AAPL",
            action=OrderAction.OPEN_LONG,
            signal_id="signal-2",
            size_usd=200.0,
            stop_loss_pct=0.02,
            take_profit_pct=0.04,
        )
    )
    assert first_created is True
    assert duplicate_created is False
    assert duplicate["order_intent_id"] == first["order_intent_id"]
    assert len(store.list_orders()) == 1


def test_state_machine_rejects_invalid_transition(tmp_path):
    store = ExecutionStore(tmp_path / "execution.db")
    store.create_order(_intent())
    store.transition("order-1", OrderState.VALIDATED)
    with pytest.raises(ValueError, match="invalid order transition"):
        store.transition("order-1", OrderState.FILLED)
