import json
import logging
import threading
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

import core.capital_manager as capital_mod
from core.capital_manager import CapitalManager


@pytest.fixture
def isolated_capital_manager(tmp_path, monkeypatch):
    capital_path = tmp_path / "capital.json"
    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", capital_path)
    monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: None)
    CapitalManager._instance = None
    manager = CapitalManager()
    yield manager, capital_path
    CapitalManager._instance = None


def test_get_balance_refreshes_from_persistent_state(isolated_capital_manager):
    manager, capital_path = isolated_capital_manager
    capital_path.write_text(
        json.dumps({"balance": 7777.0, "history": []}, indent=2),
        encoding="utf-8",
    )

    assert manager.get_balance() == 7777.0


def test_get_balance_preserves_zero_persistent_balance(isolated_capital_manager):
    manager, capital_path = isolated_capital_manager
    capital_path.write_text(
        json.dumps({"balance": 0.0, "history": [], "positions": []}, indent=2),
        encoding="utf-8",
    )

    assert manager.get_balance() == 0.0


def test_add_trade_pnl_uses_latest_persisted_balance_after_reset(isolated_capital_manager):
    manager, capital_path = isolated_capital_manager

    # Simulate stale in-memory balance held by another long-running process.
    manager.balance = 4321.0

    capital_path.write_text(
        json.dumps(
            {
                "balance": 10000.0,
                "history": [
                    {
                        "timestamp": datetime.now().isoformat(),
                        "type": "RESET",
                        "amount": 10000.0,
                        "balance": 10000.0,
                        "details": "Manual Reset",
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    manager.add_trade_pnl(-100.0, "AAPL", "sig-1")

    assert manager.get_balance() == 9900.0
    saved = json.loads(capital_path.read_text(encoding="utf-8"))
    assert saved["balance"] == 9900.0
    assert saved["history"][-1]["type"] == "LOSS"
    assert saved["history"][-1]["amount"] == -100.0


def test_set_balance_notifies_signal_reconciliation_and_breaker(isolated_capital_manager):
    manager, _ = isolated_capital_manager
    opened = manager.open_manual_position(
        ticker="AAPL",
        direction="long",
        leverage=1,
        usd_size=100,
        entry_price=100,
        max_size_pct=0.5,
        fees_bps=0,
    )
    signal_manager = MagicMock()

    with patch("core.signals.get_signal_manager", return_value=signal_manager), patch(
        "core.bot_controller.BotController.notify_capital_reset",
        return_value=True,
    ) as notify:
        manager.set_balance(10000.0, "operator reset", clear_positions=True)

    signal_manager.archive_positions_removed_by_reset.assert_called_once()
    removed = signal_manager.archive_positions_removed_by_reset.call_args.args[0]
    assert removed[0]["position_id"] == opened["position_id"]
    notify.assert_called_once_with(10000.0)


def test_open_close_manual_position_updates_ledger(isolated_capital_manager):
    manager, _ = isolated_capital_manager

    opened = manager.open_manual_position(
        ticker="AAPL",
        direction="long",
        leverage=5,
        usd_size=1000,
        entry_price=100,
        max_size_pct=0.5,
        fees_bps=10,
    )
    assert opened["status"] == "OPEN"
    assert opened["strategy_type"] == "MANUAL"
    assert opened["entry_reason"] == "Manual terminal order"
    assert opened["stop_loss_price"] < opened["entry_price"]
    assert opened["take_profit_price"] > opened["entry_price"]
    assert opened["exit_management_mode"] == "manual_levels_only"
    assert opened["automatic_trailing_enabled"] is False
    assert opened["ai_exit_management_enabled"] is False
    assert opened["trailing_sl"] == 0.0
    assert manager.get_balance() == pytest.approx(799.0, rel=1e-6)

    closed = manager.close_position(position_id=opened["position_id"], exit_price=110)
    assert closed["status"] == "CLOSED"
    # gross=+100, entry_comm=1.0, exit_comm=110*10*0.001=1.1, net=97.9
    assert closed["pnl"] == pytest.approx(97.9, rel=1e-6)
    # balance after open=799, margin release=200, cash_pnl=100-1.1=98.9 → 1097.9
    assert manager.get_balance() == pytest.approx(1097.9, rel=1e-6)
    ledger = manager.get_ledger(limit=10)
    assert {entry["account_scope"] for entry in ledger} == {"capital"}
    assert all(entry["affects_deployable_capital"] is True for entry in ledger)
    assert all(entry["balance_kind"] == "raw_shared_cash" for entry in ledger)
    assert all(entry["raw_balance"] == entry["balance"] for entry in ledger)
    close_entry = next(entry for entry in ledger if entry["type"] == "CLOSE_POSITION")
    snapshot = close_entry["metadata"]["position_snapshot"]
    assert snapshot["position_id"] == opened["position_id"]
    assert snapshot["ticker"] == "AAPL"
    assert snapshot["direction"] == "LONG"
    assert snapshot["entry_price"] == 100
    assert snapshot["exit_price"] == 110
    assert snapshot["net_pnl"] == pytest.approx(97.9, rel=1e-6)
    assert snapshot["holding_seconds"] is not None
    assert {"scoring", "sizing", "risk", "ai_review", "critic"}.issubset(snapshot)


def test_research_position_ledger_is_explicitly_non_deployable(isolated_capital_manager):
    manager, _ = isolated_capital_manager
    manager._get_execution_price = lambda *args, **kwargs: {
        "execution_price": 100.0,
        "source": "test_verified_quote",
        "price_timestamp": datetime.now().astimezone().isoformat(),
        "execution_price_quality": {
            "value": 100.0,
            "level": "VERIFIED",
            "source": "test_verified_quote",
            "is_valid": True,
        },
        "commission_rate": 0.0004,
        "cost_model": {},
    }

    opened = manager.open_position(
        "BTC-USD",
        "long",
        100.0,
        0.02,
        0.04,
        signal_id="research-signal",
        position_type="research_cohort",
        research_only=True,
    )

    entry = manager.get_ledger(limit=1)[0]
    assert opened["research_only"] is True
    assert entry["account_scope"] == "research"
    assert entry["affects_deployable_capital"] is False
    assert entry["metadata"]["position_id"] == opened["position_id"]
    assert entry["metadata"]["position_type"] == "research_cohort"


def test_legacy_ledger_scope_is_reconciled_from_position_timestamp(
    isolated_capital_manager,
):
    manager, capital_path = isolated_capital_manager
    opened_at = datetime.now().astimezone().isoformat()
    capital_path.write_text(
        json.dumps(
            {
                "balance": 900.0,
                "history": [
                    {
                        "timestamp": opened_at,
                        "type": "OPEN_POSITION",
                        "amount": -100.0,
                        "balance": 900.0,
                        "details": "BTC-USD LONG x1 notional=$100.00 margin=$100.00",
                    }
                ],
                "positions": [
                    {
                        "position_id": "legacy-research",
                        "ticker": "BTC-USD",
                        "direction": "LONG",
                        "position_type": "research_cohort",
                        "research_only": True,
                        "opened_at": opened_at,
                        "status": "OPEN",
                        "margin_used": 100.0,
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    entry = manager.get_ledger(limit=1)[0]

    assert entry["account_scope"] == "research"
    assert entry["affects_deployable_capital"] is False
    assert entry["metadata"]["position_id"] == "legacy-research"


def test_manual_level_update_is_persistent_and_disables_automatic_exits(isolated_capital_manager):
    manager, capital_path = isolated_capital_manager
    opened = manager.open_manual_position(
        ticker="AAPL",
        direction="long",
        leverage=2,
        usd_size=200,
        entry_price=100,
        max_size_pct=0.5,
        fees_bps=0,
    )
    balance_before = manager.get_balance()

    with manager._lock:
        manager._sync_from_storage(initialize_if_missing=False)
        for position in manager.positions:
            if position.get("position_id") == opened["position_id"]:
                position["trailing_sl"] = 102.0
                position["automatic_trailing_enabled"] = True
                position["ai_exit_management_enabled"] = True
        manager._save_unlocked()

    updated = manager.update_position_levels(
        position_id=opened["position_id"],
        stop_loss_price=101.0,
        take_profit_price=112.0,
        current_price=105.0,
        reason="Protect open profit",
    )

    assert updated["stop_loss_price"] == pytest.approx(101.0)
    assert updated["take_profit_price"] == pytest.approx(112.0)
    assert updated["trailing_sl"] == 0.0
    assert updated["automatic_trailing_enabled"] is False
    assert updated["ai_exit_management_enabled"] is False
    assert updated["exit_management_mode"] == "manual_levels_only"
    assert updated["manual_risk_override"] is True
    assert updated["risk_context"]["risk_reward_ratio"] == pytest.approx(12.0)
    assert updated["risk_level_history"][-1]["old_trailing_sl"] == pytest.approx(102.0)
    assert manager.get_balance() == pytest.approx(balance_before)
    assert manager.get_history()[-1]["type"] == "UPDATE_RISK_LEVELS"

    persisted = json.loads(capital_path.read_text(encoding="utf-8"))
    saved = next(position for position in persisted["positions"] if position["position_id"] == opened["position_id"])
    assert saved["stop_loss_price"] == pytest.approx(101.0)
    assert saved["take_profit_price"] == pytest.approx(112.0)


@pytest.mark.parametrize(
    ("direction", "stop_price", "target_price", "expected_message"),
    [
        ("long", 105.0, 112.0, "LONG stop loss"),
        ("long", 101.0, 105.0, "LONG take profit"),
        ("short", 105.0, 98.0, "SHORT stop loss"),
        ("short", 110.0, 105.0, "SHORT take profit"),
    ],
)
def test_manual_level_update_rejects_immediate_trigger_levels(
    isolated_capital_manager,
    direction,
    stop_price,
    target_price,
    expected_message,
):
    manager, _ = isolated_capital_manager
    opened = manager.open_manual_position(
        ticker="AAPL",
        direction=direction,
        leverage=1,
        usd_size=100,
        entry_price=105,
        max_size_pct=0.5,
        fees_bps=0,
    )

    with pytest.raises(ValueError, match=expected_message):
        manager.update_position_levels(
            position_id=opened["position_id"],
            stop_loss_price=stop_price,
            take_profit_price=target_price,
            current_price=105.0,
        )


def test_ai_exit_management_is_opt_in_for_manual_positions():
    assert CapitalManager._ai_exit_management_enabled(
        {"position_type": "manual", "strategy_type": "MANUAL_CHART"}
    ) is False
    assert CapitalManager._ai_exit_management_enabled(
        {
            "position_type": "manual",
            "strategy_type": "MANUAL_CHART",
            "ai_exit_management_enabled": True,
        }
    ) is True
    assert CapitalManager._ai_exit_management_enabled(
        {"position_type": "paper_signal", "strategy_type": "SCANNER"}
    ) is True


def test_open_manual_position_enforces_leverage_and_size_limits(isolated_capital_manager):
    manager, _ = isolated_capital_manager

    with pytest.raises(ValueError):
        manager.open_manual_position(
            ticker="AAPL",
            direction="long",
            leverage=6,
            usd_size=1000,
            entry_price=100,
            max_size_pct=0.5,
            fees_bps=10,
        )

    with pytest.raises(ValueError):
        manager.open_manual_position(
            ticker="AAPL",
            direction="long",
            leverage=5,
            usd_size=6000,
            entry_price=100,
            max_size_pct=0.4,
            fees_bps=10,
        )


def test_open_manual_position_rejects_cash_debit_over_available(isolated_capital_manager):
    """High-finding (b): open_manual_position must guard cash_debit (margin +
    fees) > available, mirroring open_position. Pre-fix only margin_used was
    guarded, so margin_used just under `available` + fees pushed balance
    NEGATIVE. Here balance=1000, lev=1, usd_size=1000, max_size_pct=1.0,
    fees_bps=100 -> margin=1000 (not > 1000, passes margin guard), fees=10,
    cash_debit=1010 > 1000 -> must raise AND leave balance/positions untouched."""
    manager, _ = isolated_capital_manager
    balance_before = manager.get_balance()
    with pytest.raises(ValueError):
        manager.open_manual_position(
            ticker="AAPL",
            direction="long",
            leverage=1,
            usd_size=1000,
            entry_price=100,
            max_size_pct=1.0,
            fees_bps=100,
        )
    # No state mutation on rejection: balance unchanged, no position opened.
    assert manager.get_balance() == pytest.approx(balance_before, rel=1e-6)
    assert manager.get_positions() == []


def test_open_manual_position_accepts_cash_debit_within_available(isolated_capital_manager):
    """Positive counterpart: a capital-valid manual position (cash_debit <=
    available) must still succeed after the guard — it must not over-block.
    balance=1000, lev=5, usd_size=1000, max_size_pct=0.5, fees_bps=10 ->
    margin=200, fees=1, cash_debit=201 <= 1000 -> opens, balance=799."""
    manager, _ = isolated_capital_manager
    opened = manager.open_manual_position(
        ticker="AAPL",
        direction="long",
        leverage=5,
        usd_size=1000,
        entry_price=100,
        max_size_pct=0.5,
        fees_bps=10,
    )
    assert opened["status"] == "OPEN"
    assert manager.get_balance() == pytest.approx(799.0, rel=1e-6)


def test_record_exit_recheck_skip_uses_latest_persisted_balance_after_reset(isolated_capital_manager):
    """High-finding (c): the PnL-tracker metadata-save blocks (and
    _record_exit_recheck_skip) must _sync_from_storage before mutating+saving,
    so a concurrent reset/deposit by another process isn't clobbered by this
    thread's stale in-memory balance. Mirrors the canonical
    test_add_trade_pnl_uses_latest_persisted_balance_after_reset pattern.

    Setup: in-memory holds a STALE balance (4321) + an open position; disk has
    been reset to 10000 by another process (with the same open position).
    Pre-fix (no sync): the loop finds the in-memory position, saves, and writes
    the STALE balance 4321 -> clobbers the 10000 reset. Post-fix: sync reloads
    10000 first -> saved balance stays 10000."""
    manager, capital_path = isolated_capital_manager
    pid = "pos_test_skip_1"
    open_pos = {"position_id": pid, "ticker": "AAPL", "status": "OPEN",
                "direction": "LONG", "entry_price": 100.0}
    # Stale in-memory state held by the long-running tracker thread.
    manager.positions = [dict(open_pos)]
    manager.balance = 4321.0
    # Concurrent process reset the persisted balance to 10000 (same open pos).
    capital_path.write_text(
        json.dumps(
            {
                "balance": 10000.0,
                "positions": [open_pos],
                "history": [
                    {
                        "timestamp": datetime.now().isoformat(),
                        "type": "RESET",
                        "amount": 10000.0,
                        "balance": 10000.0,
                        "details": "Manual Reset",
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    manager._record_exit_recheck_skip(
        pid,
        "test warning",
        trigger_price=99.0,
        final_price=99.5,
        requested_reason="stop_loss",
        actual_reason="price_recovered",
        exit_source="tracker",
    )

    saved = json.loads(capital_path.read_text(encoding="utf-8"))
    # The reset balance must be preserved (NOT clobbered by the stale 4321).
    assert saved["balance"] == pytest.approx(10000.0, rel=1e-6)
    # And the position metadata was still updated (the save did fire).
    saved_pos = next(p for p in saved["positions"] if p["position_id"] == pid)
    assert saved_pos["last_exit_recheck_warning"] == "test warning"
    assert saved_pos["last_exit_recheck"]["requested_reason"] == "stop_loss"


def test_capital_json_mirror_uses_concurrency_safe_temp_files(tmp_path, monkeypatch, caplog):
    capital_path = tmp_path / "capital.json"
    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", capital_path)

    def make_writer(balance: float) -> CapitalManager:
        writer = object.__new__(CapitalManager)
        writer.balance = balance
        writer.history = []
        writer.positions = []
        writer._store = None
        return writer

    writers = [make_writer(float(idx)) for idx in range(24)]

    with caplog.at_level(logging.ERROR, logger="CapitalManager"):
        threads = [threading.Thread(target=writer._save_unlocked) for writer in writers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert capital_path.exists()
    saved = json.loads(capital_path.read_text(encoding="utf-8"))
    assert saved["balance"] in {float(idx) for idx in range(24)}
    assert not list(tmp_path.glob("*.tmp"))
    assert "Error saving capital state" not in caplog.text


def test_set_balance_keep_positions_subtracts_open_margin(isolated_capital_manager):
    """C1 capital-integrity: a reset that KEEPS open positions must debit the
    reserved margin from the new balance. open_position already debited margin
    from the old balance and close_position refunds it on close; setting the
    balance to `amount` raw would leave that margin un-debited -> double-available
    (free cash now + refunded again at close). set_balance subtracts
    sum(open margin_used) so the cash balance stays consistent with the live
    reservation. Win-rate-neutral (no selection/scoring/exit change), capital-
    integrity-positive (prevents inflated free balance -> over-trading)."""
    manager, capital_path = isolated_capital_manager
    # Persist an OPEN position to disk: set_balance re-syncs from storage first,
    # which overwrites in-memory positions, so the reservation must come from disk.
    capital_path.write_text(
        json.dumps(
            {
                "balance": 5000.0,
                "history": [],
                "positions": [
                    {
                        "position_id": "p1",
                        "status": "OPEN",
                        "margin_used": 250.0,
                        "ticker": "AAPL",
                        "direction": "long",
                        "usd_size": 250.0,
                        "leverage": 1,
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    manager.set_balance(15000.0, reason="keep", clear_positions=False)
    # New free balance = amount - reserved open margin.
    assert manager.balance == pytest.approx(15000.0 - 250.0)
    # The kept OPEN position is preserved, not cleared.
    assert len(manager.positions) == 1
    assert str(manager.positions[0].get("status", "")).upper() == "OPEN"


def test_set_balance_keep_positions_subtracts_all_open_margins(isolated_capital_manager):
    """C1: subtraction aggregates margin across multiple OPEN positions and
    ignores CLOSED ones (CLOSED margin was already released at close time)."""
    manager, capital_path = isolated_capital_manager
    capital_path.write_text(
        json.dumps(
            {
                "balance": 5000.0,
                "history": [],
                "positions": [
                    {"position_id": "p1", "status": "OPEN", "margin_used": 150.0,
                     "ticker": "AAPL", "direction": "long", "leverage": 1},
                    {"position_id": "p2", "status": "OPEN", "margin_used": 75.0,
                     "ticker": "MSFT", "direction": "long", "leverage": 1},
                    {"position_id": "p3", "status": "CLOSED", "margin_used": 999.0,
                     "ticker": "NVDA", "direction": "long", "leverage": 1},
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    manager.set_balance(10000.0, reason="keep", clear_positions=False)
    # Only OPEN margins (150 + 75) are reserved; CLOSED margin is already released.
    assert manager.balance == pytest.approx(10000.0 - 150.0 - 75.0)


def test_set_balance_clear_positions_resets_to_amount(isolated_capital_manager):
    """Clear-path reset wipes positions, so no margin remains to subtract:
    balance lands exactly on `amount` (the documented reset contract)."""
    manager, capital_path = isolated_capital_manager
    capital_path.write_text(
        json.dumps(
            {
                "balance": 5000.0,
                "history": [],
                "positions": [
                    {"position_id": "p1", "status": "OPEN", "margin_used": 250.0,
                     "ticker": "AAPL", "direction": "long", "leverage": 1}
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    manager.set_balance(12000.0, reason="clear", clear_positions=True)
    assert manager.balance == pytest.approx(12000.0)
    assert manager.positions == []
