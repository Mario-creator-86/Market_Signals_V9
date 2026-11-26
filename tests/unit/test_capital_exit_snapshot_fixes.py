"""
tests/unit/test_capital_exit_snapshot_fixes.py
==============================================
Regression guards for Runda-5 capital_manager fixes EX2 and EX6.

EX2 -- ``_close_position_from_exit_decision`` called ``close_position`` with
``enforce_exit_reason=True`` and no try/except. ``close_position`` reloads the
position from storage inside its lock (:1535) and re-runs
``_resolve_close_reason_for_execution``; a ``trailing_sl`` update by another
thread between the trigger-price snapshot check (:1855) and that reload flips
``actual_hard``, so the enforced close raised ``ValueError("...no longer
matched final execution price...")``. That propagated past the PnL tracker's
for-position loop (no per-position guard), aborting the ENTIRE iteration -- so
every OTHER position that also needed closing this tick was skipped for 3s --
AND left the triggering position OPEN even though its exit decision was valid
at trigger time. Fix: catch the enforce-mismatch ValueError and fall back to a
market close with ``enforce_exit_reason=False`` (the position should not stay
open because the reason label drifted).

EX6 -- ``_daily_snapshot_loop``'s ``last_snapshot_date`` guard is per-process
and in-memory, so at UTC midnight BOTH the api process and the engine
subprocess append a snapshot for the same day, and a process restart within
the midnight window resets that guard and appends again -- duplicate rows
inflating the cockpit equity chart. Original Runda-5 fix: append-or-replace
by calendar date (one snapshot per day). REVISED 2026-07: ``_append_equity_
snapshot`` now dedups by UTC *minute* (capital_manager.py:2153-2159) so the
cockpit chart shows a real intraday curve, not a 1-point-per-day line. Under
the minute-key model a legacy same-DAY entry in a different minute is a
distinct intraday point and is correctly PRESERVED (test_ex6_legacy_entry_
preserved_as_distinct_intraday_point); same-MINUTE writes still collapse to
one (test_ex6_two_snapshots_same_day_dedup_to_one), and different days are
both retained (test_ex6_different_days_both_kept).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
__import__("os").environ.setdefault("TEST_MODE", "1")
__import__("os").environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

import core.capital_manager as capital_mod  # noqa: E402
from core.capital_manager import CapitalManager, resolve_capital_equity  # noqa: E402
from core.sqlite_store import SQLiteKVStore  # noqa: E402


def _verified_quote(price: float) -> dict:
    return {
        "execution_price": float(price),
        "execution_price_quality": {"quality": "verified", "source": "unit_test"},
        "spread_pct": 0.0,
        "slippage_pct": 0.0,
        "commission_rate": 0.0025,
        "total_cost_pct": 0.25,
        "price_timestamp": "2026-07-03T10:00:00+00:00",
        "source": "unit_test",
    }


@pytest.fixture
def fresh_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", tmp_path / "capital.json")
    store = SQLiteKVStore(tmp_path / "cap.db")
    monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: store)
    try:
        import core.trade_audit as trade_audit

        monkeypatch.setattr(trade_audit, "record_trade_event", lambda *a, **k: None)
    except Exception:
        pass
    CapitalManager._instance = None
    CapitalManager._price_cache = {}
    CapitalManager._atr_cache = {}
    manager = CapitalManager()
    manager.set_balance(10_000.0, reason="test", clear_positions=True)
    yield manager, store
    CapitalManager._instance = None
    CapitalManager._price_cache = {}
    CapitalManager._atr_cache = {}
    store._conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# EX2
# ─────────────────────────────────────────────────────────────────────────────


def test_ex2_enforce_mismatch_falls_back_to_market_close(fresh_manager, monkeypatch):
    """When close_position(enforce=True) raises the enforce-mismatch ValueError,
    _close_position_from_exit_decision must retry with enforce=False rather than
    propagate (which would abort the whole PnL-tracker iteration + leave the
    position OPEN)."""
    manager, _ = fresh_manager

    position = {
        "position_id": "pos_ex2",
        "ticker": "AAPL",
        "direction": "LONG",
        "entry_price": 100.0,
        "take_profit_price": 110.0,
        "stop_loss_price": 95.0,
        "status": "OPEN",
    }

    monkeypatch.setattr(manager, "_get_execution_price", lambda t, s, **k: _verified_quote(115.0))
    # actual_hard for 115 >= tp 110 -> "take_profit" == requested_hard -> pre-check OK.

    calls = []

    def fake_close(position_id, *, exit_price, reason, enforce_exit_reason, **kwargs):
        calls.append({"pid": position_id, "enforce": bool(enforce_exit_reason), "reason": reason})
        if enforce_exit_reason:
            raise ValueError(
                "Requested take_profit no longer matched final execution price "
                "115.00000000; gross_pnl=-1.000000"
            )
        return {"position_id": position_id, "status": "CLOSED", "reason": reason}

    monkeypatch.setattr(manager, "close_position", fake_close)
    with manager._lock:
        manager.positions = [dict(position)]
        manager._save_unlocked()

    ok = manager._close_position_from_exit_decision(
        position, reason="take_profit", trigger_price=112.0
    )

    assert ok is True, "must return True after the fallback close succeeds"
    assert len(calls) == 2, (
        "must call close_position twice: first enforce=True (raises), then the "
        f"fallback enforce=False; got {len(calls)} calls"
    )
    assert calls[0]["enforce"] is True, "first attempt must enforce the hard reason"
    assert calls[1]["enforce"] is False, (
        "EX2 fallback: second attempt must use enforce_exit_reason=False (market close)"
    )
    assert calls[1]["pid"] == "pos_ex2"


def test_ex2_unrelated_value_error_still_propagates(fresh_manager, monkeypatch):
    """A ValueError that is NOT the enforce-mismatch (e.g. 'position already
    closed') must still propagate -- we must not silently retry/suppress real
    close failures."""
    manager, _ = fresh_manager
    position = {
        "position_id": "pos_ex2b", "ticker": "AAPL", "direction": "LONG",
        "entry_price": 100.0, "take_profit_price": 110.0, "stop_loss_price": 95.0,
        "status": "OPEN",
    }
    monkeypatch.setattr(manager, "_get_execution_price", lambda t, s, **k: _verified_quote(115.0))

    def fake_close(position_id, *, exit_price, reason, enforce_exit_reason, **kwargs):
        raise ValueError("position already closed")

    monkeypatch.setattr(manager, "close_position", fake_close)
    with manager._lock:
        manager.positions = [dict(position)]
        manager._save_unlocked()

    with pytest.raises(ValueError, match="position already closed"):
        manager._close_position_from_exit_decision(
            position, reason="take_profit", trigger_price=112.0
        )


# ─────────────────────────────────────────────────────────────────────────────
# EX6
# ─────────────────────────────────────────────────────────────────────────────


def test_ex6_two_snapshots_same_day_dedup_to_one(fresh_manager, monkeypatch):
    """Two _snapshot_daily_equity calls on the same calendar day (simulating the
    api + engine subprocess both firing at midnight, or a restart within the
    midnight window) must leave exactly ONE entry for today in equity_history."""
    manager, store = fresh_manager
    # No open positions -> no HTTP fetch in phase 1; total_equity == balance.
    manager._snapshot_daily_equity()
    manager._snapshot_daily_equity()

    history = store.get_json("equity_history", [])
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date().isoformat()
    todays = [h for h in history if isinstance(h, dict) and h.get("calendar_date") == today]
    assert len(todays) == 1, (
        f"EX6: expected exactly one snapshot per calendar day, got {len(todays)} for {today}; "
        f"full history len={len(history)}"
    )
    assert todays[0]["total_equity"] == pytest.approx(10_000.0, abs=0.01)
    assert todays[0]["metric"] == "deployable_capital_equity"
    assert todays[0]["research_excluded"] is True
    assert todays[0]["deployable_cash"] == pytest.approx(10_000.0, abs=0.01)
    assert todays[0]["open_margin"] == pytest.approx(0.0, abs=0.01)
    assert todays[0]["mark_to_market_pnl"] == pytest.approx(0.0, abs=0.01)
    assert todays[0]["pnl_basis"] == "gross_mark_to_market_before_future_exit_costs"


def test_ex6_legacy_entry_preserved_as_distinct_intraday_point(fresh_manager, monkeypatch):
    """EX6 (revised for minute-key dedup, 2026-07): _append_equity_snapshot now
    dedups by UTC minute (not calendar date) so the cockpit chart shows a real
    intraday curve (see capital_manager.py:2153-2159 docstring). A legacy entry
    (no calendar_date, only an isoformat date) in a DIFFERENT minute from the new
    snapshot is a distinct intraday point -> preserved, not destroyed. The new
    snapshot is added with calendar_date populated. Same-minute dedup (a legacy
    entry replaced when the new snapshot lands in the same minute, via the _mkey
    date-fallback) is covered by test_ex6_two_snapshots_same_day_dedup_to_one."""
    manager, store = fresh_manager
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date().isoformat()
    # Legacy 00:01 entry -- a different minute from any realistic test-time
    # snapshot, so it must survive as a distinct intraday point.
    legacy = [{"date": f"{today}T00:01:00+00:00", "total_equity": 1234.0, "balance": 1234.0}]
    store.set_json("equity_history", legacy)

    manager._snapshot_daily_equity()

    history = store.get_json("equity_history", [])
    todays = [h for h in history if isinstance(h, dict) and (h.get("calendar_date") == today or str(h.get("date", ""))[:10] == today)]
    assert len(todays) == 2, (
        f"EX6: legacy 00:01 entry must be preserved as a distinct intraday point "
        f"and the new snapshot added; got {len(todays)} todays (expected 2)"
    )
    new_snap = [h for h in todays if h.get("calendar_date") == today]
    assert len(new_snap) == 1, "EX6: new snapshot must carry calendar_date"
    assert new_snap[0]["total_equity"] == pytest.approx(10_000.0, abs=0.01)
    # Legacy 00:01 point must still be present (intraday curve keeps history).
    assert any(str(h.get("date", ""))[:16] == f"{today}T00:01" for h in todays), (
        "EX6: legacy intraday point must be retained, not overwritten"
    )


def test_ex6_different_days_both_kept(fresh_manager, monkeypatch):
    """Snapshots on different calendar days must both be retained (dedup is
    per-day, not global)."""
    manager, store = fresh_manager
    store.set_json("equity_history", [
        {"calendar_date": "2026-07-01", "date": "2026-07-01T00:01:00+00:00", "total_equity": 9000.0},
        {"calendar_date": "2026-07-02", "date": "2026-07-02T00:01:00+00:00", "total_equity": 9500.0},
    ])
    manager._snapshot_daily_equity()

    history = store.get_json("equity_history", [])
    days = sorted(h.get("calendar_date") for h in history if isinstance(h, dict))
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date().isoformat()
    assert days.count(today) == 1
    assert "2026-07-01" in days and "2026-07-02" in days


def test_equity_curve_measures_deployable_capital_and_excludes_research(fresh_manager):
    manager, store = fresh_manager
    manager.balance = 8_500.0
    manager.positions = [
        {
            "position_id": "capital-open",
            "ticker": "CAP-USD",
            "direction": "LONG",
            "status": "OPEN",
            "position_type": "paper_signal",
            "entry_price": 100.0,
            "qty": 10.0,
            "margin_used": 1_000.0,
        },
        {
            "position_id": "research-open",
            "ticker": "RES-USD",
            "direction": "LONG",
            "status": "OPEN",
            "position_type": "research_cohort",
            "research_only": True,
            "entry_price": 100.0,
            "qty": 5.0,
            "margin_used": 500.0,
        },
    ]
    manager.save()

    manager.record_equity_point({"CAP-USD": 110.0, "RES-USD": 200.0})

    snapshot = store.get_json("equity_history", [])[-1]
    # Raw shared cash 8,500 + research reserve 500 = deployable cash 9,000.
    # Only Capital contributes margin (1,000) and mark-to-market P&L (100).
    assert snapshot["deployable_cash"] == pytest.approx(9_000.0)
    assert snapshot["open_margin"] == pytest.approx(1_000.0)
    assert snapshot["mark_to_market_pnl"] == pytest.approx(100.0)
    assert snapshot["total_equity"] == pytest.approx(10_100.0)
    assert snapshot["open_positions_count"] == 1
    assert snapshot["research_excluded"] is True


def test_account_equity_does_not_double_count_paid_entry_commission():
    class FakeManager:
        @staticmethod
        def get_deployable_balance():
            return 8_999.0  # 10,000 - 1,000 margin - 1 paid entry fee

    positions = [{
        "status": "OPEN",
        "position_type": "paper_signal",
        "margin_used": 1_000.0,
        "unrealized_pnl_gross": 0.0,
        "unrealized_pnl": -2.0,  # entry + estimated future exit fee
    }]

    assert resolve_capital_equity(FakeManager(), positions) == pytest.approx(9_999.0)


# ─────────────────────────────────────────────────────────────────────────────
# EX5 -- trailing_stop request that captured TP must be relabeled take_profit
# ─────────────────────────────────────────────────────────────────────────────


def test_ex5_trailing_stop_request_capturing_tp_relabels_to_take_profit_long(fresh_manager):
    """EX5: a trailing_stop close request whose fill landed at/above TP (LONG)
    captured TP-or-better. Pre-fix the resolver fell through every guard and
    returned the requested "trailing_stop" -> the trade record mislabeled a
    TP-capturing exit. Post-fix it relabels to "take_profit" (the canonical
    _hard_exit_reason_for_price verdict for that fill). Win-rate-neutral:
    win/loss is pnl-based, not reason-based."""
    manager, _ = fresh_manager
    position = {
        "position_id": "pos_ex5_long", "ticker": "AAPL", "direction": "LONG",
        "entry_price": 100.0, "take_profit_price": 105.0,
        "stop_loss_price": 95.0, "trailing_sl": 104.0,  # trailing ratcheted near TP
        "status": "OPEN",
    }
    # Fill at 105.5 -> price >= tp 105 -> actual_hard="take_profit"
    reason, warning = manager._resolve_close_reason_for_execution(
        position, exit_price=105.5, requested_reason="trailing_stop",
        gross_pnl=5.5, enforce_exit_reason=True,
    )
    assert reason == "take_profit", (
        f"EX5: trailing_stop that captured TP must relabel to take_profit; "
        f"got {reason!r}"
    )
    assert "take_profit" in warning, f"EX5: warning must mention the relabel; got {warning!r}"


def test_ex5_trailing_stop_request_capturing_tp_relabels_to_take_profit_short(fresh_manager):
    """EX5 SHORT mirror: fill at/below TP -> actual_hard="take_profit"."""
    manager, _ = fresh_manager
    position = {
        "position_id": "pos_ex5_short", "ticker": "AAPL", "direction": "SHORT",
        "entry_price": 100.0, "take_profit_price": 95.0,
        "stop_loss_price": 105.0, "trailing_sl": 96.0,
        "status": "OPEN",
    }
    # SHORT TP fires when price <= tp 95; fill at 94.5
    reason, warning = manager._resolve_close_reason_for_execution(
        position, exit_price=94.5, requested_reason="trailing_stop",
        gross_pnl=5.5, enforce_exit_reason=True,
    )
    assert reason == "take_profit", (
        f"EX5 SHORT: trailing_stop that captured TP must relabel to take_profit; "
        f"got {reason!r}"
    )


def test_ex5_genuine_trailing_stop_below_tp_stays_trailing_stop(fresh_manager):
    """EX5 negative: when the trailing fill is genuinely BELOW TP (LONG), the
    trailing stop did NOT capture TP -> reason must stay "trailing_stop" (no
    false relabel). This guards against over-eager relabeling."""
    manager, _ = fresh_manager
    position = {
        "position_id": "pos_ex5_neg", "ticker": "AAPL", "direction": "LONG",
        "entry_price": 100.0, "take_profit_price": 108.0,
        "stop_loss_price": 95.0, "trailing_sl": 103.0,
        "status": "OPEN",
    }
    # Fill at 103 (the trailing_sl) -> below tp 108 -> actual_hard="trailing_stop"
    reason, warning = manager._resolve_close_reason_for_execution(
        position, exit_price=103.0, requested_reason="trailing_stop",
        gross_pnl=3.0, enforce_exit_reason=True,
    )
    assert reason == "trailing_stop", (
        f"EX5: a trailing stop that did NOT reach TP must stay trailing_stop; "
        f"got {reason!r}"
    )
    assert warning == "", (
        f"EX5: no relabel warning when reason matches the realized outcome; "
        f"got {warning!r}"
    )
