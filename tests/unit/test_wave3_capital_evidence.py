"""Wave 3 — zone "capital-evidence".

1. Equity history: tiered, bounded, migrated in place, resolution exposed.
2. A failed Capital save is impossible to miss and an unsaved open is refused.
3. Unchanged capital state is parsed once; a write is seen by the next read.
4. Outage-delayed exits carry an auditable marker and are left out of evidence.
5. A validation score that was never measured is null + "unavailable".
6. ``exit_condition_since``: since when a position's exit has been due.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

import core.capital_manager as capital_mod  # noqa: E402
from core.capital_manager import (  # noqa: E402
    CapitalManager,
    CapitalPersistenceError,
    read_capital_save_health,
)
from core.portfolio import equity_history as eh  # noqa: E402
from core.portfolio import evidence as ev  # noqa: E402
from core.portfolio.adapter import _peak_equity  # noqa: E402
from core.portfolio.reporting import summarize_closed_trades  # noqa: E402
from core.sqlite_store import SQLiteKVStore  # noqa: E402

NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)


def _quote(price: float) -> dict:
    return {
        "execution_price": float(price),
        "execution_price_quality": {"quality": "verified", "source": "unit_test"},
        "spread_pct": 0.0,
        "slippage_pct": 0.0,
        "commission_rate": 0.0025,
        "total_cost_pct": 0.25,
        "price_timestamp": "2026-11-15T10:00:00+00:00",
        "source": "unit_test",
    }


def _silence_side_effects(monkeypatch) -> None:
    import core.feedback_loop as feedback_loop
    import core.trade_audit as trade_audit

    monkeypatch.setattr(trade_audit, "record_trade_event", lambda *a, **k: None)
    monkeypatch.setattr(feedback_loop, "process_closed_trade", lambda *a, **k: None)


@pytest.fixture
def sqlite_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("MS_DISABLE_CAPITAL_THREADS", "1")
    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", tmp_path / "capital.json")
    store = SQLiteKVStore(tmp_path / "cap.db")
    monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: store)
    _silence_side_effects(monkeypatch)
    CapitalManager._instance = None
    manager = CapitalManager()
    manager.set_balance(10_000.0, reason="test", clear_positions=True)
    yield manager, store
    CapitalManager._instance = None
    store._conn.close()


@pytest.fixture
def json_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("MS_DISABLE_CAPITAL_THREADS", "1")
    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", tmp_path / "capital.json")
    monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: None)
    _silence_side_effects(monkeypatch)
    CapitalManager._instance = None
    manager = CapitalManager()
    manager.set_balance(10_000.0, reason="test", clear_positions=True)
    yield manager
    CapitalManager._instance = None


def _open_position(**overrides) -> dict:
    position = {
        "position_id": "pos_w3",
        "ticker": "BTC-USD",
        "direction": "LONG",
        "position_type": "paper_signal",
        "strategy_type": "S7_TEST",
        "status": "OPEN",
        "entry_price": 100.0,
        "qty": 10.0,
        "usd_size": 1000.0,
        "margin_used": 1000.0,
        "leverage": 1,
        "commission_usd": 2.5,
        "commission_rate": 0.0025,
        "stop_loss_price": 98.0,
        "take_profit_price": 104.0,
        "opened_at": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
    }
    position.update(overrides)
    return position


def _store_positions(manager: CapitalManager, positions: list) -> None:
    manager.positions = positions
    manager.save()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Equity history
# ─────────────────────────────────────────────────────────────────────────────


def _point(moment: datetime, equity: float, **extra) -> dict:
    return {
        "date": moment.isoformat(),
        "calendar_date": moment.date().isoformat(),
        "minute_key": moment.strftime("%Y-%m-%dT%H:%M"),
        "total_equity": equity,
        "open_positions_count": 3,
        **extra,
    }


def _legacy_history() -> list:
    """Shape of the live list on 2026-11-15: bursts of minute points on a few
    old days, then a point per minute for the last hours."""
    history = []
    for day, base in ((57, 9840.0), (56, 9850.0), (50, 9900.0), (24, 9700.0)):
        start = NOW - timedelta(days=day)
        for minute in range(120):
            history.append(_point(start + timedelta(minutes=minute), base + (minute % 7)))
    start = NOW - timedelta(days=3)
    for minute in range(0, 240, 60):
        history.append(_point(start + timedelta(minutes=minute), 9950.0 + minute / 60))
    for minute in range(300, 0, -1):
        history.append(_point(NOW - timedelta(minutes=minute), 9977.0 + (1.0 if minute % 11 == 0 else 0.0)))
    return history


def test_equity_history_is_tiered_bounded_and_keeps_the_daily_shape():
    legacy = _legacy_history()
    compacted = eh.compact_equity_history(legacy, now=NOW)

    assert len(compacted) <= eh.MAX_EQUITY_POINTS
    assert all(point["resolution"] in {"daily", "hourly", "1m"} for point in compacted)
    dates = [point["date"] for point in compacted]
    assert dates == sorted(dates), "oldest first"

    daily = [point for point in compacted if point["resolution"] == "daily"]
    assert [point["calendar_date"] for point in daily] == [
        (NOW - timedelta(days=day)).date().isoformat() for day in (57, 56, 50, 24)
    ], "every old day keeps exactly one point"
    # A daily point is the day's LAST snapshot and remembers the folded range.
    first_day = daily[0]
    assert first_day["total_equity"] == 9840.0 + (119 % 7)
    assert first_day["high_equity"] == 9846.0 and first_day["low_equity"] == 9840.0
    assert first_day["samples"] == 120

    hourly = [point for point in compacted if point["resolution"] == "hourly"]
    hours = [point["date"][:13] for point in hourly]
    assert len(hours) == len(set(hours)), "one point per hour"
    assert all(NOW - datetime.fromisoformat(point["date"]) <= timedelta(days=7) for point in hourly)

    fine = [point for point in compacted if point["resolution"] == "1m"]
    assert fine and all(NOW - datetime.fromisoformat(point["date"]) <= timedelta(hours=2) for point in fine)
    assert compacted[-1]["date"] == legacy[-1]["date"], "the newest point is always kept"

    # The span is months again, not the last two hours.
    span = datetime.fromisoformat(compacted[-1]["date"]) - datetime.fromisoformat(compacted[0]["date"])
    assert span > timedelta(days=50)


def test_equity_compaction_is_idempotent_and_never_lowers_the_peak():
    legacy = _legacy_history()
    legacy.append(_point(NOW - timedelta(days=50, minutes=-30), 10_500.0))  # intraday peak, not a day's last point
    once = eh.compact_equity_history(legacy, now=NOW)
    twice = eh.compact_equity_history(once, now=NOW)
    assert twice == once
    assert _peak_equity(0.0, once) == _peak_equity(0.0, legacy) == 10_500.0
    assert not eh.needs_migration(once) and eh.needs_migration(legacy)


def test_equity_noise_is_collapsed_only_inside_one_bucket():
    hour_start = (NOW - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    flat = [_point(hour_start + timedelta(minutes=m), 9977.55 + (0.004 if m % 2 else 0.0)) for m in range(10)]
    moved = [_point(hour_start + timedelta(minutes=10 + m), 9978.55 + m) for m in range(3)]
    next_hour = [_point(hour_start + timedelta(minutes=60 + m), 9980.55) for m in range(4)]
    compacted = eh.compact_equity_history(flat + moved + next_hour, now=hour_start + timedelta(minutes=70))

    minutes = [datetime.fromisoformat(point["date"]) for point in compacted]
    offsets = [int((moment - hour_start).total_seconds() // 60) for moment in minutes]
    # Flat run 0..9 -> first and last; real moves 10,11,12 all kept; the flat
    # run in the next hour bucket is collapsed on its own (60 and 63), never
    # merged with the previous hour although 12 -> 60 are a cent apart or not.
    assert offsets == [0, 9, 10, 11, 12, 60, 63]
    assert compacted[1]["samples"] == 9, "the dropped middle points are counted"
    assert eh.compact_equity_history(compacted, now=hour_start + timedelta(minutes=70)) == compacted


def test_equity_segments_describe_each_resolution():
    compacted = eh.compact_equity_history(_legacy_history(), now=NOW)
    meta = eh.describe_equity_curve(compacted)
    assert [segment["resolution"] for segment in meta["segments"]] == ["daily", "hourly", "1m"]
    assert sum(segment["samples"] for segment in meta["segments"]) == meta["points"] == len(compacted)
    assert meta["segments"][0]["from"] == compacted[0]["date"]
    assert meta["segments"][-1]["to"] == compacted[-1]["date"]
    assert meta["noise_threshold_usd"] == 0.01


def test_stored_equity_history_is_migrated_in_place_on_first_read(sqlite_manager):
    manager, store = sqlite_manager
    now = datetime.now(timezone.utc)
    legacy = []
    for day in (60, 59, 40):
        for minute in range(100):
            legacy.append(_point(now - timedelta(days=day, minutes=minute), 9000.0 + day + minute))
    for minute in range(200, 0, -1):
        legacy.append(_point(now - timedelta(minutes=minute), 9500.0 + minute))
    store.set_json("equity_history", legacy)

    curve = manager.get_equity_curve(limit=500)
    stored = store.get_json("equity_history", [])
    assert stored == curve, "the compacted list replaced the stored one"
    assert len(stored) < len(legacy) and not eh.needs_migration(stored)
    assert len([p for p in stored if p["resolution"] == "daily"]) >= 3
    # Field names the Capital tab reads today are unchanged.
    assert {"date", "total_equity", "open_positions_count"} <= set(curve[0])

    # A new point keeps the history tiered (was: tail-trim to the newest 500).
    manager.record_equity_point({})
    after = store.get_json("equity_history", [])
    assert not eh.needs_migration(after) and after[-1]["resolution"] == "1m"
    assert [p["date"] for p in after if p["resolution"] == "daily"] == [
        p["date"] for p in stored if p["resolution"] == "daily"
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 3. Parsed-state cache
# ─────────────────────────────────────────────────────────────────────────────


def _count_parses(monkeypatch) -> list:
    parses: list = []
    original = json.loads

    def _counting(text, *args, **kwargs):
        if isinstance(text, str) and '"positions"' in text:
            parses.append(len(text))
        return original(text, *args, **kwargs)

    monkeypatch.setattr(capital_mod.json, "loads", _counting)
    return parses


def test_unchanged_state_is_parsed_once_for_many_reads(sqlite_manager, monkeypatch):
    manager, _store = sqlite_manager
    _store_positions(manager, [_open_position()])
    parses = _count_parses(monkeypatch)

    for _ in range(5):
        assert len(manager.get_positions()) == 1
        assert manager.get_balance() == 10_000.0
        assert manager.get_position("pos_w3")["ticker"] == "BTC-USD"
        manager.get_capital_accounting()
        manager.get_ledger(limit=10)
        manager.get_history()
    assert len(parses) == 1, f"30 reads of unchanged state must parse once, parsed {len(parses)}x"


def test_write_is_seen_by_the_next_read_same_process_and_other_process(sqlite_manager, monkeypatch, tmp_path):
    manager, _store = sqlite_manager
    _store_positions(manager, [_open_position()])
    parses = _count_parses(monkeypatch)
    assert manager.get_positions()[0]["stop_loss_price"] == 98.0
    assert len(parses) == 1

    # Same process: a write invalidates.
    _store_positions(manager, [_open_position(stop_loss_price=99.0)])
    assert manager.get_positions()[0]["stop_loss_price"] == 99.0
    assert manager.get_positions()[0]["stop_loss_price"] == 99.0
    assert len(parses) == 2

    # Another process (its own connection) writes the row.
    other = SQLiteKVStore(tmp_path / "cap.db")
    try:
        state = other.get_json(capital_mod.CAPITAL_STORE_KEY)
        state["positions"][0]["stop_loss_price"] = 97.0
        state["balance"] = 4321.0
        other.set_json(capital_mod.CAPITAL_STORE_KEY, state)
    finally:
        other._conn.close()
    assert manager.get_positions()[0]["stop_loss_price"] == 97.0
    assert manager.get_balance() == 4321.0


def test_reads_never_share_objects_and_mutations_always_reparse(sqlite_manager, monkeypatch):
    manager, _store = sqlite_manager
    _store_positions(manager, [_open_position()])
    first = manager.get_positions()
    first[0]["stop_loss_price"] = 1.0
    first[0]["injected"] = True
    again = manager.get_positions()
    assert again[0]["stop_loss_price"] == 98.0 and "injected" not in again[0]

    parses = _count_parses(monkeypatch)
    manager.get_positions()
    assert parses == []
    with manager._lock:
        manager._sync_from_storage(initialize_if_missing=False)
    assert len(parses) == 1, "a sync that precedes a mutation always starts from freshly parsed state"
    manager.get_positions()
    assert len(parses) == 2, "and the read after it does not trust the possibly-mutated memory"


def test_read_nested_in_a_failed_mutation_does_not_vouch_for_memory(sqlite_manager):
    """open_position reads accounting (a nested read) between its sync and its
    save. If the mutation then fails before saving, the next read must come
    from storage, not from the half-mutated memory."""
    manager, _store = sqlite_manager
    _store_positions(manager, [_open_position()])
    with manager._lock:
        manager._sync_from_storage(initialize_if_missing=False)   # a mutating method starts
        assert len(manager.get_positions()) == 1                  # nested read
        manager.positions.append(_open_position(position_id="ghost"))
        manager.balance -= 500.0
        # ...and the method raises here, before _save_unlocked().
    assert [p["position_id"] for p in manager.get_positions()] == ["pos_w3"]
    assert manager.get_balance() == 10_000.0


def test_json_only_store_is_cached_and_sees_file_changes(json_manager, monkeypatch):
    manager = json_manager
    _store_positions(manager, [_open_position()])
    parses = _count_parses(monkeypatch)
    for _ in range(4):
        manager.get_positions()
    assert len(parses) == 1

    path = capital_mod.CAPITAL_STATE_PATH
    state = json.loads(path.read_text(encoding="utf-8"))
    state["balance"] = 777.0
    path.write_text(json.dumps(state), encoding="utf-8")
    assert manager.get_balance() == 777.0


# ─────────────────────────────────────────────────────────────────────────────
# 2. Save health
# ─────────────────────────────────────────────────────────────────────────────


def _break_store(store, monkeypatch, message="database or disk is full"):
    original = store.set_json
    broken = {"on": True}

    def _set_json(key, value):
        if broken["on"] and key == capital_mod.CAPITAL_STORE_KEY:
            raise OSError(message)
        return original(key, value)

    monkeypatch.setattr(store, "set_json", _set_json)
    return broken


def test_failed_save_is_logged_once_counted_and_exposed(sqlite_manager, monkeypatch, caplog):
    manager, store = sqlite_manager
    assert manager.get_save_health()["status"] == "ok"
    assert manager.get_save_health()["last_save_ok_at"]

    broken = _break_store(store, monkeypatch)
    with caplog.at_level(logging.ERROR, logger="CapitalManager"):
        for _ in range(3):
            manager.save()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and "CAPITAL STATE NOT SAVED" in r.getMessage()]
    assert len(errors) == 1, "ERROR on the first failure, then rate-limited"
    assert "database or disk is full" in errors[0].getMessage()

    health = manager.get_save_health()
    assert health["status"] == "failing"
    assert health["consecutive_save_failures"] == 3 and health["total_save_failures"] == 3
    assert "database or disk is full" in health["last_save_error"]
    assert health["last_save_error_at"] and health["last_save_ok_at"]

    # Cross-process view (what the read model and System Health use).
    shared = read_capital_save_health(manager)
    assert shared["status"] == "failing" and shared["consecutive_save_failures"] == 3
    assert shared["failing_processes"] == [health["process_role"]]
    published = json.loads(
        (capital_mod.CAPITAL_STATE_PATH.parent / f"capital_save_health.{health['process_role']}.json").read_text("utf-8")
    )
    assert published["consecutive_save_failures"] == 3 and published["pid"] == os.getpid()
    # A fresh manager-less reader (the other process) sees it from the file.
    assert read_capital_save_health(None)["status"] == "failing"

    broken["on"] = False
    manager.save()
    health = manager.get_save_health()
    assert health["status"] == "ok" and health["consecutive_save_failures"] == 0
    assert health["total_save_failures"] == 3 and "disk is full" in health["last_save_error"]
    assert read_capital_save_health(None)["status"] == "ok"


def test_mirror_failure_alone_is_recorded_but_state_is_persisted(sqlite_manager, monkeypatch, caplog):
    manager, _store = sqlite_manager

    def _no_replace(*_args, **_kwargs):
        raise OSError("mirror volume read-only")

    monkeypatch.setattr(capital_mod.os, "replace", _no_replace)
    with caplog.at_level(logging.ERROR, logger="CapitalManager"):
        _store_positions(manager, [_open_position()])
    health = manager.get_save_health()
    assert health["status"] == "ok" and health["consecutive_save_failures"] == 0
    assert health["consecutive_mirror_failures"] == 1 and "read-only" in health["last_mirror_error"]
    assert any("JSON mirror not written" in r.getMessage() for r in caplog.records)
    assert len(manager.get_positions()) == 1, "SQLite holds the state"


def test_open_position_is_refused_when_its_save_failed(sqlite_manager, monkeypatch):
    manager, store = sqlite_manager
    monkeypatch.setattr(manager, "_get_execution_price", lambda *a, **k: _quote(100.0))
    audited = []
    import core.trade_audit as trade_audit

    monkeypatch.setattr(trade_audit, "record_trade_event", lambda *a, **k: audited.append(k.get("event_type")))

    _break_store(store, monkeypatch)
    with pytest.raises(CapitalPersistenceError) as raised:
        manager.open_position("BTC-USD", "LONG", 500.0, 0.02, 0.04, signal_id="sig_w3")
    # Not a ValueError/KeyError: the paper broker records FAILED, never FILLED.
    assert not isinstance(raised.value, (ValueError, KeyError))
    assert "not persisted" in str(raised.value)
    assert "position_opened" not in audited
    # Rolled back to what storage holds: no position, no cash debit.
    assert manager.positions == [] and manager.balance == 10_000.0
    assert manager.get_positions() == [] and manager.get_balance() == 10_000.0


def test_paper_broker_records_failed_not_filled_for_an_unsaved_open(sqlite_manager, monkeypatch, tmp_path):
    """The orphaned SOXLB fill: execution.db said FILLED, Capital had no position."""
    from core.execution.broker import PaperBroker
    from core.execution.store import ExecutionStore
    from core.execution.types import OrderAction, OrderIntent

    manager, store = sqlite_manager
    monkeypatch.setattr(manager, "_get_execution_price", lambda *a, **k: _quote(100.0))
    broker = PaperBroker(
        capital_manager=manager,
        store=ExecutionStore(tmp_path / "execution.db"),
        new_risk_authorizer=lambda: (True, "ready"),
    )
    _break_store(store, monkeypatch)
    order = broker.submit(OrderIntent(
        decision_id="decision-w3", order_intent_id="order-w3", idempotency_key="open-w3",
        ticker="BTC-USD", action=OrderAction.OPEN_LONG, signal_id="sig-w3",
        size_usd=500.0, stop_loss_pct=0.02, take_profit_pct=0.04,
        metadata={"decision_context": {"strategy_type": "TEST"}},
    ))
    assert order["state"] == "FAILED"
    assert "not persisted" in str(order.get("error"))
    assert "FILLED" not in [event["to_state"] for event in order["events"]]
    assert manager.get_positions() == []


def test_open_position_still_succeeds_when_the_save_works(sqlite_manager, monkeypatch):
    manager, _store = sqlite_manager
    monkeypatch.setattr(manager, "_get_execution_price", lambda *a, **k: _quote(100.0))
    opened = manager.open_position("BTC-USD", "LONG", 500.0, 0.02, 0.04, signal_id="sig_ok")
    assert opened["status"] == "OPEN" and opened["exit_condition_tracked"] is True
    assert [p["position_id"] for p in manager.get_positions()] == [opened["position_id"]]


# ─────────────────────────────────────────────────────────────────────────────
# 4. Outage-delayed exits
# ─────────────────────────────────────────────────────────────────────────────


def _closed_at(hours_after: float, since: datetime) -> str:
    return (since + timedelta(hours=hours_after)).isoformat()


def test_exit_delay_branch_time_since_first_touch():
    since = datetime(2026, 9, 5, 14, 0, tzinfo=timezone.utc)
    position = _open_position(
        exit_condition_tracked=True,
        exit_condition_since=since.isoformat(),
        exit_condition_since_basis="observed",
    )
    late = ev.assess_exit_delay(position, closed_at=_closed_at(72.5, since), exit_price=104.5)
    assert late["evidence_excluded"] is True
    assert late["evidence_exclusion_reason"] == "exit_delayed_by_outage"
    assert late["evidence_exclusion_basis"] == ev.BASIS_FIRST_TOUCH
    assert late["evidence_exclusion_delay_hours"] == 72.5
    assert late["evidence_exclusion_exit_due_since"] == since.isoformat()

    assert ev.assess_exit_delay(position, closed_at=_closed_at(71.9, since), exit_price=104.5) == {}
    # Configurable threshold.
    assert ev.assess_exit_delay(
        position, closed_at=_closed_at(30, since), exit_price=104.5, threshold_hours=24
    )["evidence_exclusion_delay_hours"] == 30.0


def test_exit_delay_branch_price_only_when_first_touch_is_unknown():
    # Planned distance to target 4, to stop 2.
    legacy = _open_position()  # opened before tracking: no marker, no since
    beyond_target = ev.assess_exit_delay(legacy, closed_at=NOW, exit_price=104.0 + 12.5)
    assert beyond_target["evidence_excluded"] is True
    assert beyond_target["evidence_exclusion_basis"] == ev.BASIS_OVERSHOOT
    assert beyond_target["evidence_exclusion_delay_hours"] is None
    assert beyond_target["evidence_exclusion_overshoot_multiple"] == 3.12
    assert beyond_target["evidence_exclusion_level"] == "take_profit"
    assert ev.assess_exit_delay(legacy, closed_at=NOW, exit_price=104.0 + 12.0) == {}, "exactly 3x is not beyond"

    beyond_stop = ev.assess_exit_delay(legacy, closed_at=NOW, exit_price=98.0 - 6.5)
    assert beyond_stop["evidence_exclusion_level"] == "stop_loss"
    short = _open_position(direction="SHORT", stop_loss_price=102.0, take_profit_price=96.0)
    assert ev.assess_exit_delay(short, closed_at=NOW, exit_price=80.0)["evidence_exclusion_level"] == "take_profit"
    assert ev.assess_exit_delay(short, closed_at=NOW, exit_price=95.0) == {}

    # Tracked from its open: a genuine gap through the stop is strategy
    # evidence and must never be dropped by price alone.
    tracked = _open_position(exit_condition_tracked=True)
    assert ev.assess_exit_delay(tracked, closed_at=NOW, exit_price=80.0) == {}
    assert ev.assess_exit_delay(tracked, closed_at=NOW, exit_price=130.0) == {}


def test_exit_delay_uses_the_scanner_time_stop_when_the_touch_was_never_recorded():
    """MSTR/CNH/PANW/GTLB of the 2026-09 outage: less than 3x beyond the
    target and no recorded first touch, but their 90-minute time stop was due
    for two months."""
    opened = datetime(2026, 9, 3, 16, 59, tzinfo=timezone.utc)
    position = _open_position(
        ticker="MSTR", direction="SHORT", strategy_type="SCANNER_AI_SHORT",
        entry_price=139.75, take_profit_price=131.36, stop_loss_price=145.34,
        opened_at=opened.isoformat(),
        # Legacy record written minutes before the close: an upper bound only.
        exit_condition_since=(NOW - timedelta(hours=3)).isoformat(),
        exit_condition_since_basis=ev.SINCE_BASIS_LEGACY,
    )
    assert ev.assess_exit_delay(position, closed_at=NOW, exit_price=111.6) == {}, (
        "without the time stop the rule cannot see this delay (2.4x overshoot)"
    )
    marked = ev.assess_exit_delay(position, closed_at=NOW, exit_price=111.6, scanner_time_stop_minutes=90)
    assert marked["evidence_excluded"] is True
    assert marked["evidence_exclusion_basis"] == ev.BASIS_TIME_STOP_DUE
    assert marked["evidence_exclusion_delay_hours"] > 24 * 70
    # A scanner trade closed on time is untouched.
    on_time = dict(position, opened_at=(NOW - timedelta(hours=2)).isoformat(), exit_condition_since=None)
    assert ev.assess_exit_delay(on_time, closed_at=NOW, exit_price=131.0, scanner_time_stop_minutes=90) == {}


def test_exit_delay_config_key():
    assert ev.exit_delay_exclusion_hours({}) == 72.0
    assert ev.exit_delay_exclusion_hours({"capital_evidence": {"exit_delay_exclusion_hours": 48}}) == 48.0
    assert ev.exit_delay_exclusion_hours(SimpleNamespace(config={"capital_evidence": {"exit_delay_exclusion_hours": 0}})) == 72.0
    assert ev.exit_delay_exclusion_hours(None) == 72.0


def test_close_marks_a_delayed_exit_and_keeps_it_in_ledger_and_cash(sqlite_manager):
    manager, _store = sqlite_manager
    now = datetime.now(timezone.utc)
    delayed = _open_position(
        position_id="pos_delayed",
        exit_condition_tracked=True,
        exit_condition_since=(now - timedelta(hours=100)).isoformat(),
        exit_condition_since_basis="observed",
        exit_condition="take_profit",
    )
    on_time = _open_position(position_id="pos_on_time", ticker="ETH-USD", exit_condition_tracked=True)
    manager.balance = 8_000.0
    _store_positions(manager, [delayed, on_time])

    closed = manager.close_position(position_id="pos_delayed", exit_price=150.0, reason="take_profit", sync_signal=False)
    assert closed["evidence_excluded"] is True
    assert closed["evidence_exclusion_reason"] == "exit_delayed_by_outage"
    assert 99.9 < closed["evidence_exclusion_delay_hours"] < 100.2
    assert closed["decision_context"]["close_context"]["evidence_excluded"] is True
    # A real paper result: P&L and cash are booked exactly as for any close.
    assert closed["gross_pnl"] == pytest.approx(500.0)
    assert manager.get_balance() == pytest.approx(8_000.0 + 1000.0 + closed["cash_pnl"])

    normal = manager.close_position(position_id="pos_on_time", exit_price=104.0, reason="take_profit", sync_signal=False)
    assert "evidence_excluded" not in normal

    rows = {row["metadata"]["position_id"]: row for row in manager.get_ledger(limit=20) if row["type"] == "CLOSE_POSITION"}
    assert rows["pos_delayed"]["metadata"]["evidence_excluded"] is True
    assert rows["pos_delayed"]["metadata"]["evidence_exclusion_reason"] == "exit_delayed_by_outage"
    assert rows["pos_delayed"]["metadata"]["position_snapshot"]["evidence_excluded"] is True
    assert rows["pos_delayed"]["metadata"]["position_snapshot"]["evidence_exclusion_delay_hours"] == closed["evidence_exclusion_delay_hours"]
    assert "evidence_excluded" not in rows["pos_on_time"]["metadata"]
    assert rows["pos_on_time"]["metadata"]["position_snapshot"]["evidence_excluded"] is False


def test_manual_positions_are_never_marked(sqlite_manager):
    manager, _store = sqlite_manager
    manual = _open_position(
        position_type="manual", exit_management_mode="manual_levels_only",
        exit_condition_since=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
    )
    manual.pop("strategy_type")
    assert manager._is_manual_position(manual)
    assert manager._exit_delay_evidence_marker(dict(manual, closed_at=datetime.now(timezone.utc).isoformat()), 150.0) == {}


def _closed(pnl: float, *, excluded: bool = False, **extra) -> dict:
    row = {
        "status": "CLOSED", "pnl": pnl, "net_pnl": pnl, "pnl_usd": pnl,
        "strategy_type": "SCANNER_AI_SHORT", "direction": "SHORT",
        "closed_at": "2026-11-10T10:00:00+00:00", "position_type": "paper_signal",
    }
    if excluded:
        row.update({"evidence_excluded": True, "evidence_exclusion_reason": "exit_delayed_by_outage"})
    row.update(extra)
    return row


def test_win_rate_summary_leaves_excluded_trades_out_and_reports_them():
    trades = [_closed(10.0), _closed(-5.0), _closed(-5.0), _closed(200.0, excluded=True), _closed(159.0, excluded=True)]
    summary = summarize_closed_trades(trades)
    assert summary["trades"] == 3 and summary["wins"] == 1 and summary["losses"] == 2
    assert summary["win_rate_pct"] == 33.3
    assert summary["breakeven_win_rate_pct"] == 33.3  # avg loss 5 / (avg win 10 + avg loss 5)
    assert summary["profit_factor"] == 1.0 and summary["expectancy_usd"] == 0.0
    assert summary["net_pnl_usd"] == 0.0
    assert summary["excluded_trades"] == {
        "count": 2, "total_pnl_usd": 359.0, "net_pnl": 359.0, "reasons": {"exit_delayed_by_outage": 2},
    }
    assert summarize_closed_trades([_closed(1.0)])["excluded_trades"]["count"] == 0


def test_paper_performance_leaves_excluded_trades_out_and_reports_them():
    from core.paper_performance import summarize_paper_trades

    trades = [_closed(-2.0, closed_at=f"2026-10-{d:02d}T10:00:00+00:00") for d in range(1, 29)]
    trades += [_closed(4.0, closed_at=f"2026-11-{d:02d}T10:00:00+00:00") for d in range(1, 13)]
    excluded = [_closed(180.0, excluded=True), _closed(179.0, excluded=True)]
    report = summarize_paper_trades(trades + excluded)
    clean = summarize_paper_trades(trades)

    for key in ("trades", "net_pnl_usd", "mean_pnl_usd", "win_rate_pct", "breakeven_win_rate_pct", "profit_factor", "verdict"):
        assert report["overall"][key] == clean["overall"][key], key
        assert report["strategies"][0][key] == clean["strategies"][0][key], key
    assert report["overall"]["trades"] == 40 and report["overall"]["net_pnl_usd"] == -8.0
    assert report["overall"]["verdict"] != "PROFITABLE_PROVEN"
    expected = {"count": 2, "total_pnl_usd": 359.0, "net_pnl": 359.0, "reasons": {"exit_delayed_by_outage": 2}}
    assert report["excluded_trades"] == expected
    assert report["overall"]["excluded_trades"] == expected
    assert report["strategies"][0]["excluded_trades"] == expected
    assert "-8.00" in report["headline"]
    assert clean["excluded_trades"]["count"] == 0


def _verified_closed(pnl: float, *, excluded: bool = False, index: int = 0) -> dict:
    quality = {"quality": "VERIFIED", "is_valid": True}
    return _closed(
        pnl, excluded=excluded, pnl_pct_notional=pnl / 10.0,
        entry_price=100.0, exit_price=100.0 + pnl, entry_price_source="unit", exit_price_source="unit",
        entry_price_quality=quality, exit_price_quality=quality,
        closed_at=f"2026-11-{1 + index % 12:02d}T10:{index % 60:02d}:00+00:00",
    )


def test_stopping_rule_sample_leaves_excluded_trades_out_and_reports_them():
    from core.capital_entry_guard import evaluate_paper_experiment_budget

    signal = {"strategy_type": "SCANNER_AI_SHORT", "direction": "SHORT", "autonomous_source": "scanner"}
    config = {"mode": "paper", "capital_entry_gate": {
        "enabled": True,
        "paper_experiment": {"max_sample_without_edge": 5},
        "realized_performance": {"min_sample": 3},
    }}
    losers = [_verified_closed(-5.0, index=i) for i in range(4)]
    unearned = [_verified_closed(180.0, excluded=True, index=20 + i) for i in range(2)]

    counted_all = evaluate_paper_experiment_budget(signal, losers + [dict(u, evidence_excluded=False) for u in unearned], config)
    assert counted_all["sample_size"] == 6 and counted_all["status"] == "validated", (
        "counted as evidence, the two outage trades turn a losing record into a validated one"
    )

    result = evaluate_paper_experiment_budget(signal, losers + unearned, config)
    assert result["sample_size"] == 4
    assert result["realized"]["status"] == "negative_edge"
    assert result["realized"]["metrics"]["net_pnl_usd"] == -20.0
    assert result["excluded_trades"]["count"] == 2 and result["excluded_trades"]["total_pnl_usd"] == 360.0

    concluded = evaluate_paper_experiment_budget(signal, losers + [_verified_closed(-5.0, index=9)] + unearned, config)
    assert concluded["allowed"] is False and concluded["status"] == "experiment_concluded_no_edge"
    assert concluded["excluded_trades"]["count"] == 2


def test_acceptance_scorecard_leaves_excluded_trades_out_and_reports_them():
    from api.ledger_service import _acceptance_performance

    def _entry(pct, excluded=False, pnl=0.0):
        return SimpleNamespace(strategy="SCANNER_AI_SHORT", afterward={
            "basis": "position_close", "forward_return_pct": pct,
            "detail": {"pnl_usd": pnl, "evidence_excluded": excluded},
        })

    report = _acceptance_performance([_entry(1.0), _entry(-1.0), _entry(40.0, excluded=True, pnl=359.0)])
    assert report["evaluated"] == 2 and report["wins"] == 1 and report["win_rate_pct"] == 50.0
    assert report["avg_pnl_pct"] == 0.0
    assert report["by_strategy"]["SCANNER_AI_SHORT"]["n"] == 2
    assert report["excluded_trades"]["count"] == 1 and report["excluded_trades"]["total_pnl_usd"] == 359.0


def test_leverage_boost_never_counts_unearned_profit(sqlite_manager, monkeypatch):
    manager, _store = sqlite_manager
    import core.feedback_loop as feedback_loop

    journal = [
        {"ticker": "HOOD", "net_pnl": 44.0, "closed_at": "2026-11-16T14:30:00+00:00"},
        {"ticker": "BTC-USD", "net_pnl": -3.0, "closed_at": "2026-11-10T10:00:00+00:00"},
    ]
    seen = []
    monkeypatch.setattr(feedback_loop, "get_recent_trades", lambda count=50: list(journal))
    monkeypatch.setattr(
        feedback_loop, "compute_portfolio_metrics",
        lambda trades=None: seen.append(list(trades)) or {"total_trades": 0},
    )
    _store_positions(manager, [dict(
        _open_position(ticker="HOOD", status="CLOSED"),
        closed_at="2026-11-16T14:30:00+00:00", evidence_excluded=True,
    )])
    manager.get_positions()
    assert manager._strategy_is_profitable() is False
    assert [t["ticker"] for t in seen[-1]] == ["BTC-USD"]


# ─────────────────────────────────────────────────────────────────────────────
# 5. validation_status
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "position, expected",
    [
        ({"decision_context": {"confidence": {"validation_score": 0.0}}}, (None, "unavailable")),
        ({"decision_context": {"confidence": {}}}, (None, "unavailable")),
        ({}, (None, "unavailable")),
        ({"strategy_type": "SCANNER_AI_SHORT", "decision_context": {"confidence": {"validation_score": 55.0}}}, (None, "unavailable")),
        ({"strategy_type": "PRE_EARNINGS_AI_LONG", "decision_context": {"confidence": {"validation_score": 55.0}}}, (None, "unavailable")),
        ({"decision_context": {"confidence": {"validation_score": 71.0, "validation_status": "unavailable"}}}, (None, "unavailable")),
        ({"decision_context": {"signal": {"validation_score_status": "unavailable"}, "confidence": {"validation_score": 12.0}}}, (None, "unavailable")),
        ({"strategy_type": "S7_LIQUIDITY_SWEEP_REVERSAL", "decision_context": {"confidence": {"validation_score": 55.0}}}, (55.0, "measured")),
        ({"decision_context": {"confidence": {"validation_score": 68.5, "validation_status": "measured"}}}, (68.5, "measured")),
    ],
)
def test_validation_score_is_null_and_unavailable_when_never_measured(position, expected):
    fields = ev.validation_fields(position)
    assert (fields["validation_score"], fields["validation_status"]) == expected
    scoring = CapitalManager._closed_position_snapshot(position)["scoring"]
    assert (scoring["validation_score"], scoring["validation_status"]) == expected


# ─────────────────────────────────────────────────────────────────────────────
# 6. exit_condition_since
# ─────────────────────────────────────────────────────────────────────────────

MARKET_CLOSED = "Cannot close stock position for HOOD outside US regular market hours (weekend)."


def _skip(manager, reason="take_profit", warning=MARKET_CLOSED, **kwargs):
    manager._exit_skip_recorded_at = {}  # not testing the 15-minute rate limit here
    manager._record_exit_recheck_skip(
        "pos_w3", warning, trigger_price=105.0, final_price=0.0, requested_reason=reason, **kwargs
    )
    return manager.get_position("pos_w3")


def test_first_touch_is_recorded_once_and_kept(sqlite_manager):
    manager, _store = sqlite_manager
    _store_positions(manager, [_open_position(exit_condition_tracked=True)])

    first = _skip(manager)
    assert first["exit_condition_since"] == first["last_exit_recheck"]["timestamp"]
    assert first["exit_condition_since_basis"] == "observed"
    assert first["exit_condition"] == "take_profit" and first["exit_condition_trigger_price"] == 105.0
    assert "exit_blocked_in_session_at" not in first, "the market was closed: nothing failed yet"

    later = _skip(manager, warning="Exit recheck skipped for HOOD: verified live exit price unavailable")
    assert later["exit_condition_since"] == first["exit_condition_since"], "the first touch is never overwritten"
    assert later["last_exit_recheck"]["timestamp"] >= first["last_exit_recheck"]["timestamp"]
    assert later["exit_blocked_in_session_at"] == later["last_exit_recheck"]["timestamp"]


def test_ai_closes_and_unconfirmed_triggers_are_not_exit_conditions(sqlite_manager):
    manager, _store = sqlite_manager
    _store_positions(manager, [_open_position(exit_condition_tracked=True)])
    assert "exit_condition_since" not in _skip(manager, reason="ollama_exit: thesis broken")
    assert "exit_condition_since" not in _skip(
        manager, warning="Exit recheck skipped HOOD take_profit: trigger price 105, live execution 103",
        condition_confirmed=False,
    )
    assert _skip(manager, reason="scanner_time_stop")["exit_condition"] == "time_exit"


def test_legacy_position_keeps_the_earliest_record_and_says_it_is_a_bound(sqlite_manager):
    manager, _store = sqlite_manager
    _store_positions(manager, [_open_position(last_exit_recheck={
        "timestamp": "2026-09-19T15:57:17+03:00", "requested_reason": "take_profit", "trigger_price": 361.76,
    })])
    marked = _skip(manager)
    assert marked["exit_condition_since"] == "2026-09-19T15:57:17+03:00"
    assert marked["exit_condition_since_basis"] == ev.SINCE_BASIS_LEGACY
    # ...which is enough for a decisive answer when it is old enough.
    assert ev.assess_exit_delay(marked, closed_at=NOW, exit_price=104.5)["evidence_exclusion_basis"] == ev.BASIS_FIRST_TOUCH_UPPER_BOUND


def test_condition_met_only_while_closed_lapses_but_an_in_session_failure_is_kept(sqlite_manager):
    manager, _store = sqlite_manager
    _store_positions(manager, [_open_position(exit_condition_tracked=True)])
    since = _skip(manager)["exit_condition_since"]

    manager._clear_lapsed_exit_condition("pos_w3")
    lapsed = manager.get_position("pos_w3")
    assert "exit_condition_since" not in lapsed and "exit_condition" not in lapsed
    assert lapsed["exit_condition_last_lapsed"]["since"] == since
    assert ev.assess_exit_delay(lapsed, closed_at=NOW + timedelta(days=30), exit_price=104.5) == {}

    _skip(manager, warning="Exit recheck failed for HOOD: quote provider down")
    kept = manager.get_position("pos_w3")["exit_condition_since"]
    manager._clear_lapsed_exit_condition("pos_w3")
    assert manager.get_position("pos_w3")["exit_condition_since"] == kept


# ─────────────────────────────────────────────────────────────────────────────
# Read model (api/state_service.py capital functions)
# ─────────────────────────────────────────────────────────────────────────────


def test_capital_payload_exposes_the_new_facts(sqlite_manager, monkeypatch):
    import api.state_service as state_service

    manager, store = sqlite_manager
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=5)).isoformat()
    quality = {"quality": "VERIFIED", "is_valid": True}
    _store_positions(manager, [
        _open_position(
            strategy_type="SCANNER_AI_LONG", exit_condition_tracked=True, exit_condition_since=since,
            exit_condition="take_profit",
            decision_context={"confidence": {"validation_score": 55.0}},
        ),
        dict(_open_position(position_id="c1", status="CLOSED"), pnl=10.0, closed_at="2026-11-10T10:00:00+00:00"),
        dict(_open_position(position_id="c2", status="CLOSED"), pnl=-5.0, closed_at="2026-11-11T10:00:00+00:00"),
        dict(
            _open_position(position_id="c3", status="CLOSED"), pnl=359.0, closed_at="2026-11-12T10:00:00+00:00",
            evidence_excluded=True, evidence_exclusion_reason="exit_delayed_by_outage",
            entry_price_quality=quality,
        ),
    ])
    store.set_json("equity_history", [
        _point(now - timedelta(days=40), 9000.0),
        _point(now - timedelta(days=2), 9500.0),
        _point(now - timedelta(minutes=5), 9900.0),
    ])
    monkeypatch.setattr(state_service, "get_capital_manager", lambda: manager)
    monkeypatch.setattr(state_service, "_equity_point_due", lambda: False)
    monkeypatch.setattr(state_service, "get_shared_config", lambda: SimpleNamespace(
        config={"capital_evidence": {"exit_delay_exclusion_hours": 4}},
        bot={"scanner_time_stop_minutes": 0}, agents={}, ui={},
    ))

    payload = state_service._build_capital_payload([], {"open": {}, "closed": []}, fast_only=True)

    # 1. equity curve: same list of points, plus the resolution of each stretch.
    assert [point["total_equity"] for point in payload["equity_curve"]] == [9000.0, 9500.0, 9900.0]
    assert [point["resolution"] for point in payload["equity_curve"]] == ["daily", "hourly", "1m"]
    assert [s["resolution"] for s in payload["equity_curve_meta"]["segments"]] == ["daily", "hourly", "1m"]
    # 2. save health for System Health.
    assert payload["save_health"]["status"] == "ok"
    assert payload["save_health"]["consecutive_save_failures"] == 0
    assert payload["save_health"]["last_save_ok_at"] and payload["save_health"]["last_save_error"] is None
    # 4. evidence: 2 counted trades, 1 excluded with its P&L.
    assert payload["total_closed_trades"] == 2 and payload["win_rate"] == 50.0
    assert payload["win_rate_summary"]["trades"] == 2
    assert payload["excluded_trades"]["count"] == 1 and payload["excluded_trades"]["total_pnl_usd"] == 359.0
    assert payload["win_rate_summary"]["excluded_trades"] == payload["excluded_trades"]
    # 5 + 6. per open position.
    position = payload["positions"][0]
    assert position["validation_score"] is None and position["validation_status"] == "unavailable"
    assert position["exit_condition_since"] == since and position["exit_condition"] == "take_profit"
    assert position["market_state"]["exit_condition_since"] == since
    # ...and the fact the evidence rule will measure the delay from, with the
    # configured threshold (capital_evidence.exit_delay_exclusion_hours = 4 here).
    assert datetime.fromisoformat(position["exit_due_since"]) == datetime.fromisoformat(since)
    assert position["exit_due_basis"] == ev.BASIS_FIRST_TOUCH
    assert 4.9 < position["exit_delay_hours"] < 5.1
    assert position["evidence_exclusion_expected"] is True
    assert position["evidence_exclusion_threshold_hours"] == 4.0
    # The annotations never reach the manager's own objects.
    assert "market_state" not in manager.get_position("pos_w3")


def test_capital_payload_reports_a_failing_save(sqlite_manager, monkeypatch):
    import api.state_service as state_service

    manager, store = sqlite_manager
    monkeypatch.setattr(state_service, "get_capital_manager", lambda: manager)
    monkeypatch.setattr(state_service, "_equity_point_due", lambda: False)
    _break_store(store, monkeypatch, message="disk I/O error")
    manager.save()
    health = state_service._build_capital_payload([], {"open": {}, "closed": []}, fast_only=True)["save_health"]
    assert health["status"] == "failing" and health["consecutive_save_failures"] == 1
    assert "disk I/O error" in health["last_save_error"]
