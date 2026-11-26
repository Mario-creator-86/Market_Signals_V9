from copy import deepcopy
from datetime import datetime, timedelta, timezone

from core.execution.broker import PaperBroker
from core.execution.store import ExecutionStore
from core.execution.types import OrderAction, OrderIntent
from core.execution.types import OrderState


class _Capital:
    def __init__(self):
        self.positions = []
        self.open_calls = 0
        self.close_calls = 0
        self.reduce_calls = 0

    def get_positions(self):
        return deepcopy(self.positions)

    def get_history(self):
        return deepcopy(getattr(self, "history", []))

    def get_position(self, position_id):
        return next((deepcopy(item) for item in self.positions if item["position_id"] == position_id), None)

    def open_position(self, **kwargs):
        self.open_calls += 1
        position = {
            "position_id": f"pos-{self.open_calls}",
            "ticker": kwargs["ticker"],
            "signal_id": kwargs["signal_id"],
            "status": "OPEN",
            "usd_size": kwargs["size_usd"],
            "decision_context": kwargs["decision_context"],
        }
        self.positions.append(position)
        return deepcopy(position)

    def close_position(self, **kwargs):
        self.close_calls += 1
        position = next(item for item in self.positions if item["position_id"] == kwargs["position_id"])
        position["status"] = "CLOSED"
        return deepcopy(position)

    def reduce_position(self, **kwargs):
        self.reduce_calls += 1
        position = next(item for item in self.positions if item["position_id"] == kwargs["position_id"])
        position["usd_size"] *= 1.0 - kwargs["fraction"]
        return deepcopy(position)


def _open_intent(key="open-key"):
    return OrderIntent(
        decision_id="decision-1",
        order_intent_id=f"order-{key}",
        idempotency_key=key,
        ticker="AAPL",
        action=OrderAction.OPEN_LONG,
        signal_id="signal-1",
        size_usd=100.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        metadata={"decision_context": {"strategy_type": "TEST"}},
    )


def test_duplicate_delivery_creates_one_order_and_one_position(tmp_path):
    capital = _Capital()
    broker = PaperBroker(
        capital_manager=capital,
        store=ExecutionStore(tmp_path / "execution.db"),
        new_risk_authorizer=lambda: (True, "ready"),
    )
    first = broker.submit(_open_intent())
    second = broker.submit(_open_intent())
    assert first["state"] == "FILLED"
    assert second["state"] == "FILLED"
    assert second["idempotent_replay"] is True
    assert capital.open_calls == 1
    assert len(capital.positions) == 1
    assert [event["to_state"] for event in first["events"]] == [
        "PROPOSED", "VALIDATED", "SUBMITTED", "ACKNOWLEDGED", "FILLED"
    ]


def test_kill_switch_authorizer_rejects_open_but_close_still_fills(tmp_path):
    capital = _Capital()
    capital.positions.append({"position_id": "pos-existing", "ticker": "AAPL", "status": "OPEN"})
    broker = PaperBroker(
        capital_manager=capital,
        store=ExecutionStore(tmp_path / "execution.db"),
        new_risk_authorizer=lambda: (False, "STOP AUTONOMOUS TRADING is active"),
    )
    rejected = broker.submit(_open_intent())
    close = broker.submit(
        OrderIntent(
            decision_id="close-1",
            order_intent_id="order-close",
            idempotency_key="close-key",
            ticker="AAPL",
            action=OrderAction.CLOSE,
            position_id="pos-existing",
        )
    )
    assert rejected["state"] == "REJECTED"
    assert capital.open_calls == 0
    assert close["state"] == "FILLED"
    assert capital.close_calls == 1


def test_reduce_emits_partial_fill_and_updates_position(tmp_path):
    capital = _Capital()
    capital.positions.append(
        {"position_id": "pos-existing", "ticker": "AAPL", "status": "OPEN", "usd_size": 100.0}
    )
    broker = PaperBroker(
        capital_manager=capital,
        store=ExecutionStore(tmp_path / "execution.db"),
    )
    order = broker.submit(
        OrderIntent(
            decision_id="reduce-1",
            order_intent_id="order-reduce",
            idempotency_key="reduce-key",
            ticker="AAPL",
            action=OrderAction.REDUCE,
            position_id="pos-existing",
            reduce_fraction=0.25,
        )
    )
    assert order["state"] == "FILLED"
    assert "PARTIALLY_FILLED" in [event["to_state"] for event in order["events"]]
    assert capital.positions[0]["usd_size"] == 75.0


def test_material_reconciliation_disagreement_latches_conflict(tmp_path):
    capital = _Capital()
    broker = PaperBroker(
        capital_manager=capital,
        store=ExecutionStore(tmp_path / "execution.db"),
        new_risk_authorizer=lambda: (True, "ready"),
    )
    broker.submit(_open_intent())
    saved = deepcopy(capital.positions)
    capital.positions.clear()
    conflict = broker.reconcile()
    assert conflict["status"] == "EXECUTION_STATE_CONFLICT"
    assert conflict["conflicts"][0]["code"] == "filled_order_position_missing"
    capital.positions[:] = saved
    assert broker.reconcile()["status"] == "HEALTHY"


def test_orphaned_fill_can_be_rolled_back_with_an_audit_event(tmp_path):
    capital = _Capital()
    broker = PaperBroker(
        capital_manager=capital,
        store=ExecutionStore(tmp_path / "execution.db"),
        new_risk_authorizer=lambda: (True, "ready"),
    )
    filled = broker.submit(_open_intent())
    position_id = filled["result"]["position_id"]
    capital.positions.clear()
    assert broker.reconcile()["status"] == "EXECUTION_STATE_CONFLICT"

    repaired = broker.rollback_orphaned_fill(
        filled["order_intent_id"],
        reason="Capital persistence failed before the position became durable",
    )

    assert repaired["reconciliation"]["status"] == "HEALTHY"
    order = broker.store.get_order(filled["order_intent_id"])
    assert order["state"] == "FILLED"
    assert any(
        event["detail"].get("reconciliation_resolution")
        == "ORPHANED_FILL_ROLLED_BACK"
        for event in order["events"]
    )
    assert position_id not in {item.get("position_id") for item in capital.positions}


def test_capital_reset_audits_and_resolves_filled_positions_it_cleared(tmp_path):
    capital = _Capital()
    capital.history = []
    broker = PaperBroker(
        capital_manager=capital,
        store=ExecutionStore(tmp_path / "execution.db"),
        new_risk_authorizer=lambda: (True, "ready"),
    )
    filled = broker.submit(_open_intent())
    position_id = filled["result"]["position_id"]
    capital.positions.clear()
    capital.history.append({
        "type": "RESET",
        "timestamp": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
        "metadata": {
            "clear_positions": True,
            "removed_position_ids": [position_id],
        },
    })

    result = broker.reconcile()

    assert result["status"] == "HEALTHY"
    order = broker.store.get_order(filled["order_intent_id"])
    assert any(
        event["detail"].get("reconciliation_resolution")
        == "CAPITAL_RESET_POSITION_CLEARED"
        for event in order["events"]
    )


def test_reset_that_keeps_positions_does_not_hide_a_missing_fill(tmp_path):
    capital = _Capital()
    capital.history = []
    broker = PaperBroker(
        capital_manager=capital,
        store=ExecutionStore(tmp_path / "execution.db"),
        new_risk_authorizer=lambda: (True, "ready"),
    )
    broker.submit(_open_intent())
    capital.positions.clear()
    capital.history.append({
        "type": "RESET",
        "timestamp": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
        "metadata": {"clear_positions": False},
    })

    assert broker.reconcile()["status"] == "EXECUTION_STATE_CONFLICT"


def test_unresolved_acknowledged_order_is_a_reconciliation_conflict(tmp_path):
    capital = _Capital()
    store = ExecutionStore(tmp_path / "execution.db")
    intent = _open_intent()
    store.create_order(intent)
    store.transition(intent.order_intent_id, OrderState.VALIDATED)
    store.transition(intent.order_intent_id, OrderState.SUBMITTED)
    store.transition(intent.order_intent_id, OrderState.ACKNOWLEDGED)
    broker = PaperBroker(capital_manager=capital, store=store)

    result = broker.reconcile()

    assert result["status"] == "EXECUTION_STATE_CONFLICT"
    assert result["conflicts"] == [
        {
            "code": "inflight_order_unresolved",
            "order_intent_id": intent.order_intent_id,
            "action": "OPEN_LONG",
        }
    ]
