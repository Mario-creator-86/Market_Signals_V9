import json
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from core import execution_archive as ea


@pytest.fixture
def isolated_archive(tmp_path, monkeypatch):
    """Point the archive at a temp dir so tests never touch real data files."""
    base = tmp_path / "data"
    base.mkdir()
    monkeypatch.setattr(ea, "ARCHIVE_DIR", base)
    monkeypatch.setattr(ea, "STOCKS_FILE", base / "executions_stocks.json")
    monkeypatch.setattr(ea, "CRYPTO_FILE", base / "executions_crypto.json")
    monkeypatch.setattr(ea, "_BACKFILL_MARKER", base / ".execution_archive_backfilled")
    # reset per-file locks (they are module-level)
    monkeypatch.setattr(ea, "_FILE_LOCKS", {})
    return base


def _rec(ticker, event_type="execution_confirmed", status="executed", event_id="e1", ts="2026-07-06T10:00:00+00:00"):
    return {
        "event_id": event_id,
        "timestamp": ts,
        "event_type": event_type,
        "status": status,
        "ticker": ticker,
        "signal_id": "s1",
        "position_id": "p1",
        "strategy_type": "SYSTEM",
        "summary": "",
        "payload": {},
    }


def test_append_routes_crypto_to_crypto_file(isolated_archive):
    ea.append_execution(_rec("BTC-USD"))
    rec = ea.read_executions(asset_class="CRYPTO_FUTURES")[-1]
    assert rec["ticker"] == "BTC-USD"
    assert not ea.STOCKS_FILE.exists()
    assert rec["asset_class"] == "CRYPTO_FUTURES"


def test_append_routes_stocks_to_stocks_file(isolated_archive):
    ea.append_execution(_rec("AAPL"))
    assert ea.read_executions(asset_class="US_STOCKS")[-1]["ticker"] == "AAPL"
    assert not ea.CRYPTO_FILE.exists()
    rec = ea.read_executions(asset_class="US_STOCKS")[-1]
    assert rec["asset_class"] == "US_STOCKS"


def test_append_routes_forex_and_commodities_to_stocks_file(isolated_archive):
    ea.append_execution(_rec("EURUSD=X"))
    ea.append_execution(_rec("GC=F"))
    rows = ea.read_executions(asset_class="US_STOCKS")
    assert {r["asset_class"] for r in rows} == {"FOREX_MAJORS", "COMMODITIES"}


def test_append_stamps_asset_class_when_missing(isolated_archive):
    rec = _rec("AAPL")
    rec.pop("asset_class", None)
    ea.append_execution(rec)
    row = ea.read_executions(asset_class="US_STOCKS")[-1]
    assert row["asset_class"] == "US_STOCKS"


def test_append_preserves_existing_asset_class(isolated_archive):
    rec = _rec("AAPL")
    rec["asset_class"] = "US_STOCKS"
    ea.append_execution(rec)
    row = ea.read_executions(asset_class="US_STOCKS")[-1]
    assert row["asset_class"] == "US_STOCKS"


def test_append_is_uncapped(isolated_archive):
    for i in range(3000):
        ea.append_execution(_rec("AAPL", event_id=f"e{i}"))
    rows = ea.read_executions(asset_class="US_STOCKS")
    assert len(rows) == 3000
    assert ea.compact_execution_archive(asset_class="US_STOCKS") == 3000
    assert len(json.loads(ea.STOCKS_FILE.read_text(encoding="utf-8"))) == 3000


def test_concurrent_appends_lose_no_records(isolated_archive):
    def worker(start):
        for i in range(start, start + 200):
            ea.append_execution(_rec("AAPL", event_id=f"e{i}"))

    threads = [threading.Thread(target=worker, args=(k * 200,)) for k in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = ea.read_executions(asset_class="US_STOCKS")
    assert len(rows) == 1000
    assert len({r["event_id"] for r in rows}) == 1000


def test_read_executions_filters_by_asset_class(isolated_archive):
    ea.append_execution(_rec("AAPL"))
    ea.append_execution(_rec("BTC-USD"))
    crypto = ea.read_executions(asset_class="CRYPTO_FUTURES")
    stocks = ea.read_executions(asset_class="US_STOCKS")
    all_rows = ea.read_executions()
    assert len(crypto) == 1 and crypto[0]["ticker"] == "BTC-USD"
    assert len(stocks) == 1 and stocks[0]["ticker"] == "AAPL"
    assert len(all_rows) == 2


def test_read_executions_filters_by_time_window(isolated_archive):
    ea.append_execution(_rec("AAPL", event_id="a", ts="2026-07-01T00:00:00+00:00"))
    ea.append_execution(_rec("AAPL", event_id="b", ts="2026-07-10T00:00:00+00:00"))
    ea.append_execution(_rec("AAPL", event_id="c", ts="2026-07-20T00:00:00+00:00"))
    since = datetime(2026, 7, 5, tzinfo=timezone.utc)
    until = datetime(2026, 7, 15, tzinfo=timezone.utc)
    rows = ea.read_executions(since=since, until=until)
    assert {r["event_id"] for r in rows} == {"b"}


def test_read_executions_handles_missing_files(isolated_archive):
    assert ea.read_executions() == []


def test_recent_reader_is_bounded_and_survives_compaction(isolated_archive):
    ea.append_execution(_rec("BTC-USD", event_type="autonomous_news_evaluated", event_id="news-old", ts="2026-07-01T00:00:00+00:00"))
    assert ea.compact_execution_archive(asset_class="CRYPTO_FUTURES") == 1
    ea.append_execution(_rec("BTC-USD", event_type="autonomous_news_evaluated", event_id="news-new", ts="2026-07-02T00:00:00+00:00"))
    ea.append_execution(_rec("BTC-USD", event_type="position_opened", event_id="open-new", ts="2026-07-03T00:00:00+00:00"))

    rows = ea.read_recent_executions(limit=2, event_types={"autonomous_news_evaluated"})

    assert [row["event_id"] for row in rows] == ["news-new", "news-old"]


def test_corrupt_file_reinitializes_and_preserves_old(isolated_archive):
    ea.append_execution(_rec("AAPL"))
    ea.CRYPTO_FILE.write_text("{not json", encoding="utf-8")
    rows = ea.read_executions(asset_class="CRYPTO_FUTURES")
    assert rows == []
    corrupt_backups = list(isolated_archive.glob("executions_crypto.json.corrupt-*"))
    assert len(corrupt_backups) == 1


def test_backfill_is_idempotent_and_dedups(isolated_archive, monkeypatch):
    legacy_journal = isolated_archive / "trade_journal.json"
    legacy_journal.write_text(json.dumps([
        {"ticker": "AAPL", "asset_class": "US_STOCKS", "net_pnl": 10.0,
         "close_reason": "take_profit", "closed_at": "2026-07-06T10:00:00+00:00",
         "strategy_type": "SYSTEM", "direction": "LONG", "pnl_pct": 0.02},
        {"ticker": "BTC-USD", "asset_class": "CRYPTO_FUTURES", "net_pnl": -5.0,
         "close_reason": "stop_loss", "closed_at": "2026-07-06T11:00:00+00:00",
         "strategy_type": "SYSTEM", "direction": "LONG", "pnl_pct": -0.02},
    ]), encoding="utf-8")
    monkeypatch.setattr(ea, "FEEDBACK_JOURNAL_PATH", legacy_journal)
    monkeypatch.setattr(ea, "TRADE_AUDIT_PATH", isolated_archive / "trade_decisions.json")

    n1 = ea.backfill_from_legacy()
    n2 = ea.backfill_from_legacy()
    assert n1 == 2 and n2 == 0
    stocks = ea.read_executions(asset_class="US_STOCKS")
    crypto = ea.read_executions(asset_class="CRYPTO_FUTURES")
    assert len(stocks) == 1 and stocks[0]["ticker"] == "AAPL"
    assert len(crypto) == 1 and crypto[0]["ticker"] == "BTC-USD"
    assert stocks[0]["event_type"] == "position_closed"


def test_backfill_marker_prevents_rerun(isolated_archive, monkeypatch):
    legacy = isolated_archive / "trade_journal.json"
    legacy.write_text(json.dumps([
        {"ticker": "AAPL", "asset_class": "US_STOCKS", "net_pnl": 1.0,
         "close_reason": "take_profit", "closed_at": "2026-07-06T10:00:00+00:00"}
    ]), encoding="utf-8")
    monkeypatch.setattr(ea, "FEEDBACK_JOURNAL_PATH", legacy)
    monkeypatch.setattr(ea, "TRADE_AUDIT_PATH", isolated_archive / "trade_decisions.json")
    ea._BACKFILL_MARKER.write_text("1", encoding="utf-8")
    assert ea.backfill_from_legacy() == 0
