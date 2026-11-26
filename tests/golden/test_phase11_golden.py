from copy import deepcopy

from core.execution.broker import PaperBroker
from core.execution.store import ExecutionStore
from core.execution.types import OrderAction, OrderIntent


class _Capital:
    def __init__(self):
        self.positions = []
        self.opens = 0

    def get_positions(self):
        return deepcopy(self.positions)

    def get_position(self, position_id):
        return next((deepcopy(item) for item in self.positions if item["position_id"] == position_id), None)

    def open_position(self, **kwargs):
        self.opens += 1
        position = {
            "position_id": "gold-pos",
            "ticker": kwargs["ticker"],
            "signal_id": kwargs["signal_id"],
            "status": "OPEN",
            "decision_context": kwargs["decision_context"],
        }
        self.positions.append(position)
        return deepcopy(position)


def _intent():
    return OrderIntent(
        decision_id="gold-decision",
        order_intent_id="gold-order",
        idempotency_key="gold-idempotency",
        ticker="AAPL",
        action=OrderAction.OPEN_LONG,
        signal_id="gold-signal",
        size_usd=100,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
    )


def test_gold_009_kill_switch_blocks_new_risk(tmp_path):
    capital = _Capital()
    broker = PaperBroker(
        capital_manager=capital,
        store=ExecutionStore(tmp_path / "gold.db"),
        new_risk_authorizer=lambda: (False, "STOP AUTONOMOUS TRADING"),
    )
    assert broker.submit(_intent())["state"] == "REJECTED"
    assert capital.opens == 0


def test_gold_010_duplicate_order_prevention(tmp_path):
    capital = _Capital()
    broker = PaperBroker(
        capital_manager=capital,
        store=ExecutionStore(tmp_path / "gold.db"),
        new_risk_authorizer=lambda: (True, "ready"),
    )
    assert broker.submit(_intent())["state"] == "FILLED"
    assert broker.submit(_intent())["idempotent_replay"] is True
    assert capital.opens == 1


def test_gold_018_db_migration_preserves_order(tmp_path):
    path = tmp_path / "gold.db"
    store = ExecutionStore(path)
    store.create_order(_intent())
    ExecutionStore(path).migrate()
    assert ExecutionStore(path).get_order("gold-order")["state"] == "PROPOSED"


def test_gold_019_broker_reconciliation_conflict(tmp_path):
    capital = _Capital()
    broker = PaperBroker(
        capital_manager=capital,
        store=ExecutionStore(tmp_path / "gold.db"),
        new_risk_authorizer=lambda: (True, "ready"),
    )
    broker.submit(_intent())
    capital.positions.clear()
    assert broker.reconcile()["status"] == "EXECUTION_STATE_CONFLICT"
