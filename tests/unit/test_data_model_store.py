import sqlite3
from datetime import datetime

import pytest

from core.data_model.entities import OHLCVBar, Symbol, get_exchange
from core.data_model.pit import PointInTime, UNAVAILABLE
from core.data_model.store import HistoricalStore


def _sym(ticker="AAPL"):
    ex = get_exchange("XNYS")
    return Symbol(ticker=ticker, exchange=ex, asset_class=ex.asset_class, currency=ex.currency)


def _bar(ticker, ts_iso, close=100.0, version=1):
    ts = datetime.fromisoformat(ts_iso)
    pit = PointInTime(event_time=ts, available_from=ts, fetched_at=ts, version=version)
    return OHLCVBar(
        symbol=_sym(ticker), timestamp=ts,
        open=close - 1, high=close, low=close - 2, close=close, volume=1000, pit=pit,
    )


def test_append_roundtrip_and_query(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    rid = store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00"), provider="yfinance")
    assert isinstance(rid, int) and rid > 0
    rows = store.query("OHLCVBar", "AAPL")
    assert len(rows) == 1
    assert rows[0]["symbol"]["ticker"] == "AAPL"
    assert rows[0]["close"] == 100.0


def test_append_only_no_update_delete_api(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    assert not hasattr(store, "update")
    assert not hasattr(store, "delete")


def test_unique_constraint_rejects_exact_duplicate(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    bar = _bar("AAPL", "2026-01-05T14:30:00+00:00")
    store.append(bar, provider="yfinance")
    with pytest.raises(sqlite3.IntegrityError):
        store.append(bar, provider="yfinance")


def test_append_requires_available_from(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    with pytest.raises(ValueError):
        store.append({"close": 100.0}, entity_type="OHLCVBar", symbol="AAPL", provider="yfinance")


def test_query_filters_by_symbol_and_provider(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00"), provider="yfinance")
    store.append(_bar("MSFT", "2026-01-05T14:30:00+00:00"), provider="yfinance")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=99), provider="finnhub")
    assert len(store.query("OHLCVBar", "AAPL")) == 2
    assert len(store.query("OHLCVBar", "AAPL", provider="yfinance")) == 1
    assert len(store.query("OHLCVBar", "MSFT")) == 1


def test_as_of_hit_returns_newest_available(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=100), provider="yfinance")
    store.append(_bar("AAPL", "2026-01-05T14:35:00+00:00", close=101), provider="yfinance")
    res = store.as_of("OHLCVBar", "AAPL", datetime.fromisoformat("2026-01-05T14:35:00+00:00"))
    assert res is not UNAVAILABLE
    assert res["close"] == 101


def test_as_of_miss_returns_unavailable(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00"), provider="yfinance")
    res = store.as_of("OHLCVBar", "AAPL", datetime.fromisoformat("2026-01-05T14:00:00+00:00"))
    assert res is UNAVAILABLE
    assert not res  # bool(UNAVAILABLE) is False


def test_as_of_version_tiebreak_on_equal_available_from(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=100, version=1), provider="yfinance")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=99, version=2), provider="yfinance")
    res = store.as_of("OHLCVBar", "AAPL", datetime.fromisoformat("2026-01-05T15:00:00+00:00"))
    assert res is not UNAVAILABLE
    assert res["close"] == 99  # higher version wins on equal available_from


def test_as_of_prefer_event_time(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    ts_event = datetime.fromisoformat("2026-01-05T14:30:00+00:00")
    ts_avail = datetime.fromisoformat("2026-01-05T15:00:00+00:00")
    pit = PointInTime(event_time=ts_event, available_from=ts_avail, fetched_at=ts_avail, version=1)
    bar = OHLCVBar(symbol=_sym("AAPL"), timestamp=ts_event, open=99, high=100, low=98,
                   close=100, volume=1000, pit=pit)
    store.append(bar, provider="yfinance")
    T = datetime.fromisoformat("2026-01-05T14:45:00+00:00")
    # before available_from -> UNAVAILABLE under prefer=available_from
    assert store.as_of("OHLCVBar", "AAPL", T, prefer="available_from") is UNAVAILABLE
    # after event_time -> hit under prefer=event_time
    res = store.as_of("OHLCVBar", "AAPL", T, prefer="event_time")
    assert res is not UNAVAILABLE
    assert res["close"] == 100


def test_as_of_range_window_inclusive(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    for m, close in [(0, 100), (5, 101), (10, 102), (15, 103), (20, 104)]:
        store.append(_bar("AAPL", f"2026-01-05T14:{30 + m:02d}:00+00:00", close=close), provider="yfinance")
    lo = datetime.fromisoformat("2026-01-05T14:35:00+00:00")
    hi = datetime.fromisoformat("2026-01-05T14:45:00+00:00")
    rng = store.as_of_range("OHLCVBar", "AAPL", lo, hi)
    closes = [r["close"] for r in rng]
    assert closes == [101, 102, 103]  # 14:35, 14:40, 14:45 inclusive


def test_as_of_range_empty_when_none_in_window(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00"), provider="yfinance")
    lo = datetime.fromisoformat("2026-01-05T15:00:00+00:00")
    hi = datetime.fromisoformat("2026-01-05T16:00:00+00:00")
    assert store.as_of_range("OHLCVBar", "AAPL", lo, hi) == []


def test_as_of_range_ascending_order(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    # insert out of order; result must be ascending by available_from
    store.append(_bar("AAPL", "2026-01-05T14:40:00+00:00", close=102), provider="yfinance")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=100), provider="yfinance")
    lo = datetime.fromisoformat("2026-01-05T14:00:00+00:00")
    hi = datetime.fromisoformat("2026-01-05T15:00:00+00:00")
    rng = store.as_of_range("OHLCVBar", "AAPL", lo, hi)
    assert [r["close"] for r in rng] == [100, 102]


from core.data_model.quality import Conflict


def test_detect_conflicts_two_provider_disagreement(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=100), provider="yfinance")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=110), provider="finnhub")
    conflicts = store.detect_conflicts(
        "OHLCVBar", "AAPL",
        datetime.fromisoformat("2026-01-05T15:00:00+00:00"),
        tolerance_pct=1.0,
    )
    assert len(conflicts) == 1
    c = conflicts[0]
    assert isinstance(c, Conflict)
    assert c.field == "close"
    assert set(c.sources) == {"yfinance", "finnhub"}
    assert abs(c.deviation_pct - 10.0) < 0.01  # |110-100|/100*100 = 10%


def test_detect_conflicts_agreement_within_tolerance(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=100), provider="yfinance")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=100.5), provider="finnhub")
    assert store.detect_conflicts(
        "OHLCVBar", "AAPL",
        datetime.fromisoformat("2026-01-05T15:00:00+00:00"),
        tolerance_pct=1.0,
    ) == []


def test_detect_conflicts_single_provider_no_conflict(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=100), provider="yfinance")
    assert store.detect_conflicts(
        "OHLCVBar", "AAPL",
        datetime.fromisoformat("2026-01-05T15:00:00+00:00"),
    ) == []


def test_detect_conflicts_empty_store(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    assert store.detect_conflicts(
        "OHLCVBar", "AAPL",
        datetime.fromisoformat("2026-01-05T15:00:00+00:00"),
    ) == []