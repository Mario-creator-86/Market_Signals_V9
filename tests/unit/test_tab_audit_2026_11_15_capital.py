"""Capital tab audit, 2026-11-15: reconciliation reporting and tab reporting helpers.

Live evidence behind these tests:
* The cockpit showed only "EXECUTION_STATE_CONFLICT (CRITICAL)". The stored
  conflict was one FILLED OPEN_LONG SOXLB-USD order (2026-10-22) whose Capital
  position and cash debit never became durable. Nothing told the owner which
  records disagreed or how to resolve it, and the existing audited repair
  (``rollback_orphaned_fill``) had no API.
* The reconciler scans only the newest 1000 orders (the store bounds the list)
  while Capital keeps closed positions longer; the ledger held 925 orders.
* "Win rate 37.3%" was shown without its sample (the latest 300 closed trades)
  or the break-even bar; stock quotes on a Sunday were labelled LIVE.
"""

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from core.execution.broker import PaperBroker
from core.execution.store import ExecutionStore
from core.execution.types import OrderAction, OrderIntent, OrderState
from core.portfolio.reporting import position_market_state, summarize_closed_trades


class _Capital:
    def __init__(self):
        self.positions = []
        self.history = []
        self.open_calls = 0

    def get_positions(self):
        return deepcopy(self.positions)

    def get_history(self):
        return deepcopy(self.history)

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


def _open_intent(key="open-key", ticker="SOXLB-USD"):
    return OrderIntent(
        decision_id=f"decision-{key}",
        order_intent_id=f"order-{key}",
        idempotency_key=key,
        ticker=ticker,
        action=OrderAction.OPEN_LONG,
        signal_id=f"signal-{key}",
        size_usd=100.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        metadata={"decision_context": {"strategy_type": "TEST"}},
    )


def _broker(tmp_path, capital=None):
    capital = capital or _Capital()
    broker = PaperBroker(
        capital_manager=capital,
        store=ExecutionStore(tmp_path / "execution.db"),
        new_risk_authorizer=lambda: (True, "ready"),
    )
    return broker, capital


def test_conflict_names_the_records_that_disagree_and_how_to_resolve(tmp_path):
    broker, capital = _broker(tmp_path)
    filled = broker.submit(_open_intent())
    position_id = filled["result"]["position_id"]
    capital.positions.clear()

    result = broker.reconcile()

    assert result["status"] == "EXECUTION_STATE_CONFLICT"
    # The persisted compact conflict keeps its pinned shape.
    assert result["conflicts"] == [
        {
            "code": "filled_order_position_missing",
            "order_intent_id": filled["order_intent_id"],
            "position_id": position_id,
        }
    ]
    detail = result["conflict_details"][0]
    assert detail["ticker"] == "SOXLB-USD"
    assert detail["action"] == "OPEN_LONG"
    assert detail["repair_action"] == "ROLLBACK_ORPHANED_FILL"
    for needle in ("OPEN_LONG SOXLB-USD", filled["order_intent_id"], position_id, "FILLED", "no such position"):
        assert needle in detail["message"]
    assert "orphaned-fill rollback" in detail["resolution"]
    assert "not changed" in detail["resolution"]
    assert result["summary"] == detail["message"]
    # A conflict latched by an earlier process is explained from the stored row.
    stored = broker.store.get_reconciliation()
    assert broker.describe_conflicts(stored["conflicts"]) == result["conflict_details"]


def test_healthy_reconciliation_has_no_details(tmp_path):
    broker, _capital = _broker(tmp_path)
    broker.submit(_open_intent())
    result = broker.reconcile()
    assert result["status"] == "HEALTHY"
    assert result["conflict_details"] == []
    assert result["summary"] == ""


def test_order_outside_the_bounded_scan_is_not_reported_missing(tmp_path, monkeypatch):
    """Capital keeps closed positions longer than the newest-1000-orders scan."""
    broker, _capital = _broker(tmp_path)
    broker.submit(_open_intent())
    assert broker.reconcile()["status"] == "HEALTHY"

    # The order has aged out of the bounded listing but still exists in the ledger.
    monkeypatch.setattr(broker.store, "list_orders", lambda *, limit=100: [])
    assert broker.reconcile()["status"] == "HEALTHY"


def test_position_whose_order_is_really_gone_is_still_a_conflict(tmp_path):
    broker, capital = _broker(tmp_path)
    capital.positions.append(
        {
            "position_id": "pos-x",
            "status": "OPEN",
            "decision_context": {"execution": {"order_intent_id": "order-never-existed"}},
        }
    )
    result = broker.reconcile()
    assert result["status"] == "EXECUTION_STATE_CONFLICT"
    assert result["conflicts"][0]["code"] == "capital_position_order_missing"
    assert "does not exist in the order ledger" in result["conflict_details"][0]["message"]


def test_rollback_is_refused_when_capital_cash_was_affected(tmp_path):
    """A debit without a position is a cash discrepancy, not an orphaned fill."""
    broker, capital = _broker(tmp_path)
    filled = broker.submit(_open_intent())
    position_id = filled["result"]["position_id"]
    capital.positions.clear()
    capital.history.append(
        {
            "type": "OPEN_POSITION",
            "amount": -100.04,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": {"position_id": position_id},
        }
    )
    with pytest.raises(ValueError, match="Capital cash was affected"):
        broker.rollback_orphaned_fill(filled["order_intent_id"], reason="never persisted to Capital")
    assert broker.reconcile()["status"] == "EXECUTION_STATE_CONFLICT"


def test_execution_service_explains_conflicts_and_validates_rollback(tmp_path, monkeypatch):
    from api import execution_service

    broker, capital = _broker(tmp_path)
    filled = broker.submit(_open_intent())
    capital.positions.clear()
    broker.reconcile()
    monkeypatch.setattr(execution_service, "get_paper_broker", lambda config=None: broker)

    control = execution_service.get_execution_control({})
    reconciliation = control["reconciliation"]
    assert reconciliation["status"] == "EXECUTION_STATE_CONFLICT"
    assert reconciliation["conflict_details"][0]["order_intent_id"] == filled["order_intent_id"]
    assert "SOXLB-USD" in reconciliation["summary"]

    with pytest.raises(execution_service.ExecutionControlError, match="reason is required"):
        execution_service.rollback_orphaned_fill({}, order_intent_id=filled["order_intent_id"], reason="short")
    with pytest.raises(execution_service.ExecutionControlError, match="order_intent_id is required"):
        execution_service.rollback_orphaned_fill({}, order_intent_id=" ", reason="a long enough reason")
    with pytest.raises(execution_service.ExecutionControlError, match="not found"):
        execution_service.rollback_orphaned_fill({}, order_intent_id="order-unknown", reason="a long enough reason")

    repaired = execution_service.rollback_orphaned_fill(
        {},
        order_intent_id=filled["order_intent_id"],
        reason="Fill never persisted to Capital: no position, no cash debit",
    )
    assert repaired["status"] == "HEALTHY"
    assert repaired["resolution"]["reconciliation_resolution"] == "ORPHANED_FILL_ROLLED_BACK"
    order = broker.store.get_order(filled["order_intent_id"])
    assert order["state"] == OrderState.FILLED.value  # terminal history is not rewritten
    assert capital.positions == []  # Capital is never touched by the repair


def test_rollback_endpoint_maps_refusals_to_422(client, monkeypatch):
    from api.execution_service import ExecutionControlError

    def refuse(config, *, order_intent_id, reason):
        raise ExecutionControlError("Capital position still exists; rollback is not permitted")

    monkeypatch.setattr("api.execution_service.rollback_orphaned_fill", refuse)
    response = client.post(
        "/api/execution/reconcile/rollback-orphaned-fill",
        json={"order_intent_id": "order-1", "reason": "a long enough reason"},
    )
    assert response.status_code == 422
    assert "still exists" in response.json()["detail"]

    monkeypatch.setattr(
        "api.execution_service.rollback_orphaned_fill",
        lambda config, *, order_intent_id, reason: {"status": "HEALTHY", "order": order_intent_id},
    )
    ok = client.post(
        "/api/execution/reconcile/rollback-orphaned-fill",
        json={"order_intent_id": "order-1", "reason": "a long enough reason"},
    )
    assert ok.status_code == 200
    assert ok.json() == {"status": "HEALTHY", "order": "order-1"}
    # Unknown fields are rejected, so a typo cannot silently drop the reason.
    bad = client.post(
        "/api/execution/reconcile/rollback-orphaned-fill",
        json={"order_intent_id": "order-1", "reason": "a long enough reason", "force": True},
    )
    assert bad.status_code == 422


def test_win_rate_summary_carries_sample_period_and_breakeven():
    closed = [
        {"pnl": 8.0, "closed_at": "2026-08-28T20:00:21+03:00"},
        {"pnl": 8.0, "closed_at": "2026-09-10T10:00:00+03:00"},
        {"pnl": -4.0, "closed_at": "2026-10-01T10:00:00+03:00"},
        {"pnl": -4.0, "closed_at": "2026-11-12T09:04:29+02:00"},
        {"pnl": 0.0, "closed_at": "2026-11-01T09:00:00+02:00"},
    ]
    summary = summarize_closed_trades(closed)
    assert summary["trades"] == 5 and summary["wins"] == 2 and summary["losses"] == 2
    assert summary["win_rate_pct"] == 40.0
    # avg loss 4 / (avg win 8 + avg loss 4): same definition as paper_performance.
    assert summary["breakeven_win_rate_pct"] == 33.3
    assert summary["period_start"].startswith("2026-08-28T17:00:21")
    assert summary["period_end"].startswith("2026-11-12T07:04:29")
    assert summary["window_is_capped"] is False
    assert summary["net_pnl_usd"] == 8.0


def test_win_rate_summary_is_honest_about_empty_and_capped_samples():
    empty = summarize_closed_trades([])
    assert empty["trades"] == 0
    assert empty["win_rate_pct"] is None
    assert empty["breakeven_win_rate_pct"] is None

    only_losses = summarize_closed_trades([{"pnl": -1.0}, {"pnl": -2.0}])
    assert only_losses["win_rate_pct"] == 0.0
    assert only_losses["breakeven_win_rate_pct"] is None

    capped = summarize_closed_trades([{"pnl": 1.0}] * 300)
    assert capped["window_is_capped"] is True
    assert capped["retention_limit"] == 300


def test_stock_quote_on_a_weekend_is_not_tradable_and_explains_the_waiting_exit(monkeypatch):
    monkeypatch.delenv("MS_MARKET_HOURS_OVERRIDE", raising=False)  # the suite forces "open"
    sunday = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)
    bull = {
        "ticker": "BULL",
        "direction": "SHORT",
        "entry_price": 10.0047,
        "current_price": 5.18,
        "stop_loss_price": 10.4049,
        "take_profit_price": 9.4044,
    }
    state = position_market_state(bull, now=sunday)
    assert state["market_hours_enforced"] is True
    assert state["market_open"] is False
    assert state["price_is_tradable"] is False
    assert state["market_reason"] == "weekend"
    assert state["exit_trigger"] == "take_profit"
    assert "regular session" in state["exit_note"]

    # Same position during the regular session: tradable, exit simply pending.
    monday = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)  # 10:00 ET
    open_state = position_market_state(bull, now=monday)
    assert open_state["market_open"] is True
    assert open_state["exit_trigger"] == "take_profit"
    assert "next verified price check" in open_state["exit_note"]


def test_crypto_is_always_tradable_and_untriggered_positions_have_no_note(monkeypatch):
    monkeypatch.delenv("MS_MARKET_HOURS_OVERRIDE", raising=False)
    sunday = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)
    crypto = {
        "ticker": "BTC-USD",
        "direction": "LONG",
        "current_price": 100.0,
        "stop_loss_price": 96.0,
        "take_profit_price": 106.0,
    }
    state = position_market_state(crypto, now=sunday)
    assert state["market_hours_enforced"] is False
    assert state["market_open"] is True
    assert state["exit_trigger"] == ""
    assert state["exit_note"] == ""

    stopped = position_market_state({**crypto, "current_price": 95.0}, now=sunday)
    assert stopped["exit_trigger"] == "stop_loss"


def test_capital_payload_publishes_the_reporting_fields():
    """The Capital tab reads these keys; keep the payload builder wired to them."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "api" / "state_service.py").read_text(encoding="utf-8")
    assert '"win_rate_summary": win_rate_summary' in source
    assert 'p["market_state"] = position_market_state(p)' in source


# --- Exit-recheck skip reporting -------------------------------------------
# Live evidence: 27 of the 30 newest rows of /api/capital/decisions were
# "exit_recheck_skipped" with an empty ticker -- every stock position, every
# minute, all weekend -- and each one rewrote the whole Capital state.


@pytest.fixture
def _capital_manager(tmp_path, monkeypatch):
    import core.capital_manager as capital_mod
    from core.capital_manager import CapitalManager

    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", tmp_path / "capital.json")
    monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: None)
    CapitalManager._instance = None
    manager = CapitalManager()
    manager.positions = [
        {"position_id": "pos_bull", "ticker": "BULL", "status": "OPEN", "direction": "SHORT", "entry_price": 10.0},
        {"position_id": "pos_hood", "ticker": "HOOD", "status": "OPEN", "direction": "SHORT", "entry_price": 122.2},
    ]
    manager._save_unlocked()
    yield manager, capital_mod
    CapitalManager._instance = None


def _market_hours_warning(ticker, clock):
    return (
        f"Cannot close stock position for {ticker} outside US regular market hours "
        f"(weekend; now 2026-11-15 {clock} EST, session 09:30-16:00 ET). Crypto positions are allowed 24/7."
    )


def test_repeated_exit_skip_is_recorded_once_and_names_the_ticker(_capital_manager, monkeypatch):
    manager, capital_mod = _capital_manager
    events = []
    monkeypatch.setattr(
        "core.trade_audit.record_trade_event",
        lambda **kwargs: events.append(kwargs) or kwargs,
    )
    saves = []
    original_save = manager._save_unlocked
    monkeypatch.setattr(manager, "_save_unlocked", lambda: (saves.append(1), original_save())[1])

    for clock in ("02:51:07", "02:52:12", "02:53:19"):
        manager._record_exit_recheck_skip(
            "pos_bull",
            _market_hours_warning("BULL", clock),
            trigger_price=5.19,
            final_price=0.0,
            requested_reason="take_profit",
        )

    assert len(events) == 1, "the same market-hours skip must not flood the decision feed"
    assert events[0]["ticker"] == "BULL", "the audit row must name the position's ticker"
    assert events[0]["event_type"] == "exit_recheck_skipped"
    assert len(saves) == 1, "an unchanged skip must not rewrite the Capital state every minute"
    stored = manager.get_position("pos_bull")
    assert "outside US regular market hours" in stored["last_exit_recheck_warning"]

    # A different position, a different reason, or a different cause is still recorded.
    manager._record_exit_recheck_skip(
        "pos_hood", _market_hours_warning("HOOD", "02:53:27"),
        trigger_price=77.88, final_price=0.0, requested_reason="take_profit",
    )
    manager._record_exit_recheck_skip(
        "pos_bull", _market_hours_warning("BULL", "02:54:00"),
        trigger_price=10.5, final_price=0.0, requested_reason="stop_loss",
    )
    manager._record_exit_recheck_skip(
        "pos_bull", "Exit recheck skipped for BULL: verified live exit price unavailable",
        trigger_price=5.19, final_price=0.0, requested_reason="take_profit",
    )
    assert [event["ticker"] for event in events] == ["BULL", "HOOD", "BULL", "BULL"]

    # After the window the unchanged skip is recorded again (still observable).
    for key in list(manager._exit_skip_recorded_at):
        manager._exit_skip_recorded_at[key] -= capital_mod._EXIT_SKIP_RECORD_INTERVAL_SECONDS + 1
    manager._record_exit_recheck_skip(
        "pos_bull", _market_hours_warning("BULL", "03:10:00"),
        trigger_price=5.19, final_price=0.0, requested_reason="take_profit",
    )
    assert len(events) == 5
