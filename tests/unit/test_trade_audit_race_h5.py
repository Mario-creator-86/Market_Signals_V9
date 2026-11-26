"""Regression: record_trade_event concurrency (H5).

Bug (H5): ``record_trade_event`` did load -> append -> save WITHOUT holding the
audit lock across the RMW. On a cache hit (TTL 30s) two concurrent callers
received the SAME cached payload snapshot, each appended its record to its own
copy, and the second ``_save_payload`` overwrote the first — silently losing a
compliance audit record. The TRADE_AUDIT_LIMIT=500 log is a compliance surface.

Fix: ``_AUDIT_CACHE_LOCK`` is now an RLock and ``record_trade_event`` wraps the
whole load->append->save in it (``_load_payload``/``_save_payload`` re-acquire
reentrantly). This test fires many concurrent events and asserts NONE are lost.
"""
import sys
import threading
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def isolated_audit(tmp_path, monkeypatch):
    import core.trade_audit as ta
    from core.sqlite_store import SQLiteKVStore

    db_path = tmp_path / "audit.db"
    store = SQLiteKVStore(path=db_path)
    file_path = tmp_path / "trade_decisions.json"

    monkeypatch.setattr(ta, "get_sqlite_store", lambda: store)
    monkeypatch.setattr(ta, "TRADE_AUDIT_PATH", file_path)
    # Reset the module-level cache so each test starts cold (forces a real load).
    with ta._AUDIT_CACHE_LOCK:
        ta._AUDIT_CACHE["payload"] = None
        ta._AUDIT_CACHE["ts"] = 0.0
    yield ta
    with ta._AUDIT_CACHE_LOCK:
        ta._AUDIT_CACHE["payload"] = None
        ta._AUDIT_CACHE["ts"] = 0.0


def test_concurrent_record_trade_event_loses_no_records(isolated_audit):
    """N threads each record K events; every one must appear in the persisted log.
    Pre-fix, concurrent cache-hit loads + overwriting saves dropped a large share."""
    ta = isolated_audit
    N_THREADS = 12
    PER_THREAD = 15
    TOTAL = N_THREADS * PER_THREAD
    errors: list = []

    def worker(tid):
        try:
            for k in range(PER_THREAD):
                ta.record_trade_event(
                    event_type="OPEN_POSITION",
                    status="ok",
                    ticker=f"T{tid}_{k}",
                    signal_id=f"sig_{tid}_{k}",
                    summary=f"event {tid}/{k}",
                )
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"record_trade_event raised: {errors[:3]}"

    # Cold-load the persisted payload (bypass cache) directly from the store.
    payload = ta.get_sqlite_store().get_json(ta.TRADE_AUDIT_KEY, {"records": []})
    records = payload.get("records", [])
    assert isinstance(records, list)

    # Every signal_id we wrote must be present — none lost to a race.
    seen = {r.get("signal_id") for r in records}
    missing = [f"sig_{i}_{k}" for i in range(N_THREADS) for k in range(PER_THREAD)
               if f"sig_{i}_{k}" not in seen]
    assert not missing, f"H5 race: {len(missing)}/{TOTAL} audit records lost. e.g. {missing[:5]}"
    assert len(records) >= TOTAL  # (== TOTAL unless prior test state leaked; isolated fixture prevents that)


def test_record_trade_event_is_atomic_under_lock(isolated_audit):
    """Sequential sanity: two events both persisted, order-independent presence."""
    ta = isolated_audit
    ta.record_trade_event(event_type="A", status="ok", ticker="AAA", signal_id="s1")
    ta.record_trade_event(event_type="B", status="ok", ticker="BBB", signal_id="s2")
    payload = ta.get_sqlite_store().get_json(ta.TRADE_AUDIT_KEY, {"records": []})
    ids = {r.get("signal_id") for r in payload.get("records", [])}
    assert {"s1", "s2"} <= ids