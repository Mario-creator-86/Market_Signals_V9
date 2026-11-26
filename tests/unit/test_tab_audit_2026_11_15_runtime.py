"""Regression tests for the 2026-11-15 runtime-zone audit.

Server start/stop, engine loop, shared state serving, data clients, storage.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

# conftest replaces core.content_fetcher.fetch_article_content with a stub for
# every test; keep a reference to the real function.
from core.content_fetcher import fetch_article_content as _real_fetch_article_content

pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────────
# Storage: a failed write is raised and visible, never silently dropped
# ─────────────────────────────────────────────────────────────────────────────


def test_sqlite_set_json_raises_and_records_unserializable_value(tmp_path):
    from core.sqlite_store import SQLiteKVStore, SQLiteStoreWriteError

    store = SQLiteKVStore(tmp_path / "kv.db")
    store.set_json("k", {"v": 1})
    with pytest.raises(SQLiteStoreWriteError):
        store.set_json("k", {"bad": object()})
    # The previous value is intact and the failure is visible to diagnostics.
    assert store.get_json("k") == {"v": 1}
    health = store.write_health()
    assert health["write_failures"] == 1
    assert health["last_write_error_key"] == "k"
    assert health["last_write_error_age_seconds"] is not None


def test_sqlite_reads_do_not_wait_for_the_writer_lock(tmp_path):
    from core.sqlite_store import SQLiteKVStore

    store = SQLiteKVStore(tmp_path / "kv.db")
    store.set_json("k", {"v": 1})
    assert store.write_health()["separate_read_connection"] is True
    result = {}
    with store._lock:  # a long write is in flight
        reader = threading.Thread(target=lambda: result.update(value=store.get_json("k")))
        reader.start()
        reader.join(timeout=5)
        assert not reader.is_alive(), "reader blocked behind the writer lock"
    assert result["value"] == {"v": 1}
    # Read-your-own-writes across the two connections.
    store.set_json("k", {"v": 2})
    assert store.get_json("k") == {"v": 2}


def _quiet_state_store():
    from core.state_store import StateStore

    store = StateStore()
    store._stop_event.set()  # no background flushes during the test
    store._writer_thread.join(timeout=5)
    return store


def test_state_store_keeps_a_write_that_lands_during_a_flush(monkeypatch):
    import core.state_store as state_store_mod

    store = _quiet_state_store()
    store.replace("state", {"n": 1})
    original_write = state_store_mod._atomic_write_text

    def _write_then_race(path, text):
        original_write(path, text)
        # Another thread updates the namespace while the disk write runs.
        store.update("state", {"n": 2})

    monkeypatch.setattr(state_store_mod, "_atomic_write_text", _write_then_race)
    assert store._flush_namespace("state") is True
    # Pre-fix the flush cleared the dirty flag afterwards and n=2 was lost.
    assert "state" in store._dirty
    monkeypatch.setattr(state_store_mod, "_atomic_write_text", original_write)
    store.flush("state")
    from core.sqlite_store import get_sqlite_store

    assert get_sqlite_store().get_json("state")["n"] == 2
    assert "state" not in store._dirty


def test_state_store_failed_flush_stays_dirty_and_is_counted(monkeypatch):
    import core.state_store as state_store_mod

    store = _quiet_state_store()
    store.replace("state", {"n": 1})

    def _boom(path, text):
        raise OSError("disk full")

    monkeypatch.setattr(state_store_mod, "_atomic_write_text", _boom)
    assert store._flush_namespace("state") is False
    assert "state" in store._dirty
    row = store.diagnostics()["namespaces"]["state"]
    assert row["flush_failures"] == 1
    assert "disk full" in row["last_flush_error"]


def test_state_store_sqlite_and_mirror_share_one_serialization():
    """A value json.dumps rejects without default=str used to reach the file
    mirror but not SQLite, so readers (SQLite first) kept the old snapshot."""
    store = _quiet_state_store()
    stamp = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
    store.replace("state", {"when": stamp})
    assert store._flush_namespace("state") is True
    from core.sqlite_store import get_sqlite_store

    assert get_sqlite_store().get_json("state")["when"] == str(stamp)


def test_api_process_state_is_not_frozen_by_its_own_save(monkeypatch):
    """After a manual signal delete the API saved the state once and then
    served that private copy forever, ignoring the engine's newer snapshots."""
    from core import state_persistence as sp
    from core.sqlite_store import get_sqlite_store

    monkeypatch.setenv("MS_PROCESS_ROLE", "api")
    monkeypatch.setenv("MS_ENGINE_MODE", "subprocess")
    assert sp.state_owned_by_this_process() is False

    sp.save_state_payload({"tickers": {"AAPL": {"price": 1.0}}, "activity_log": ["manual delete"]})
    # The engine process writes a newer snapshot to shared storage.
    get_sqlite_store().set_json("state", {"tickers": {"AAPL": {"price": 2.0}}})
    assert sp.load_state_payload({})["tickers"]["AAPL"]["price"] == 2.0

    monkeypatch.setenv("MS_ENGINE_MODE", "thread")
    assert sp.state_owned_by_this_process() is True


# ─────────────────────────────────────────────────────────────────────────────
# StockDataClient: routing, refusal, negative cache, delisted listings
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def sdc(monkeypatch):
    import core.stock_data_client as module

    module.clear_missing()
    fake_etoro = SimpleNamespace(is_available=lambda: False)
    monkeypatch.setattr("core.etoro_client.get_etoro_client", lambda: fake_etoro)
    yield module
    module.clear_missing()


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = b"x"

    def json(self):
        return self._payload


def test_exchange_pairs_are_routed_and_impossible_symbols_refused(sdc):
    assert sdc.yahoo_symbol_for("KAVAUSDT") == "KAVA-USD"
    assert sdc.yahoo_symbol_for("$SHELLUSDT") == "SHELL-USD"
    assert sdc.yahoo_symbol_for("BTC-USDT") == "BTC-USD"
    assert sdc.yahoo_symbol_for("AAPL") == "AAPL"
    assert sdc.yahoo_symbol_for("牛来USDT") is None
    assert sdc.yahoo_symbol_for("") is None


def test_chart_quote_never_requests_a_symbol_yahoo_cannot_have(sdc, monkeypatch):
    calls = []
    monkeypatch.setattr(sdc, "http_get", lambda url, **kw: calls.append(url) or _Resp(404))
    quote = sdc.StockDataClient._fetch_yahoo_chart_quote("牛来USDT")
    assert quote["price"] == 0 and calls == []

    sdc.StockDataClient._fetch_yahoo_chart_quote("KAVAUSDT")
    assert calls and calls[-1].endswith("/KAVA-USD")


def test_not_found_symbol_is_negatively_cached_after_two_misses(sdc, monkeypatch):
    calls = []
    monkeypatch.setattr(sdc, "http_get", lambda url, **kw: calls.append(url) or _Resp(404))
    for _ in range(5):
        sdc.StockDataClient._fetch_yahoo_chart_quote("ZZZZQ")
    assert len(calls) == 2, "third and later lookups must not reach Yahoo"
    assert sdc.is_known_missing("ZZZZQ") is True
    assert "ZZZZQ" in sdc.missing_symbols_snapshot()
    # A different symbol is unaffected.
    assert sdc.is_known_missing("AAPL") is False


def test_stock_only_calls_refuse_crypto_without_a_request(sdc, monkeypatch):
    def _no_yfinance(*args, **kwargs):
        raise AssertionError("yfinance must not be called for a crypto symbol")

    monkeypatch.setattr(sdc.yf, "Ticker", _no_yfinance)
    client = sdc.StockDataClient()
    assert client.get_calendar_earnings("BTC-USD") == []
    assert client.get_balance_sheet("KAVAUSDT") is None
    assert sdc.StockDataClient.get_estimates("SHELLUSDT") is None
    assert sdc.StockDataClient.get_insider_trading("ETH-USD") is None
    assert sdc.StockDataClient.get_market_cap("KAVAUSDT") is None


def _chart_payload(price, market_time):
    return {"chart": {"result": [{"meta": {
        "regularMarketPrice": price,
        "chartPreviousClose": price,
        "regularMarketTime": market_time,
        "exchangeName": "NMS",
    }}]}}


def test_delisted_listing_last_trade_is_not_served_as_a_quote(sdc, monkeypatch):
    """Yahoo still answers EA with its final trade (2026-08-04); pre-fix that
    was returned in November as the current price with timestamp=now."""
    old = int(time.time() - 100 * 86400)
    monkeypatch.setattr(sdc, "http_get", lambda url, **kw: _Resp(200, _chart_payload(209.7, old)))
    quote = sdc.StockDataClient._fetch_yahoo_chart_quote("EA")
    assert quote["price"] == 0
    assert quote["listing_inactive"] is True
    assert quote["last_trade_price"] == 209.7
    assert sdc.StockDataClient.get_quote("EA") is None

    recent = int(time.time() - 2 * 86400)  # a weekend is not a delisting
    monkeypatch.setattr(sdc, "http_get", lambda url, **kw: _Resp(200, _chart_payload(356.27, recent)))
    live = sdc.StockDataClient._fetch_yahoo_chart_quote("AAPL")
    assert live["price"] == 356.27
    assert live["market_time"] == recent
    assert live["market_time_age_seconds"] > 86400


def test_yfinance_noise_feeds_negative_cache_and_is_deduplicated(sdc):
    yf_logger = logging.getLogger("yfinance")
    seen = []

    class _Capture(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    handler = _Capture()
    yf_logger.addHandler(handler)
    try:
        for _ in range(4):
            yf_logger.error("$RTQX: possibly delisted; no price data found  (period=1mo)")
            # A one-day window on a weekend proves nothing about the listing.
            yf_logger.error("$RTQY: possibly delisted; no price data found  (period=1d)")
    finally:
        yf_logger.removeHandler(handler)
    assert sdc.is_known_missing("RTQX") is True
    assert sdc.is_known_missing("RTQY") is False
    assert sum("RTQX" in line for line in seen) == 1


def test_health_stream_stores_a_summary_not_the_dataframe(sdc, monkeypatch):
    from core.data_quality import get_health_dashboard

    frame = pd.DataFrame(
        {"Open": [1.0, 2.0], "High": [1.0, 2.0], "Low": [1.0, 2.0], "Close": [1.0, 2.0], "Volume": [1, 2]},
        index=pd.to_datetime(["2026-11-12", "2026-11-13"]),
    )
    monkeypatch.setattr(sdc.yf, "Ticker", lambda symbol: SimpleNamespace(history=lambda **kw: frame.copy()))
    monkeypatch.setattr(sdc.time, "sleep", lambda *_: None)
    result = sdc.StockDataClient().get_historical("AAPL", period="5d", interval="1d")
    assert len(result) == 2
    value = get_health_dashboard().registry["yf_historical"].value
    assert value == {"rows": 2}


# ─────────────────────────────────────────────────────────────────────────────
# eToro: exact instrument match, stale last execution
# ─────────────────────────────────────────────────────────────────────────────


def _etoro_client(tmp_path):
    from core.etoro_client import EToroClient

    client = EToroClient(config={"etoro": {"public_key": "pub", "private_key": "priv"}})
    client._search_cache = {}
    client._map_file = tmp_path / "etoro_map.json"
    return client


def test_etoro_search_never_takes_an_unrelated_first_hit(tmp_path, monkeypatch):
    client = _etoro_client(tmp_path)
    calls = []

    def _fake_get(path, params=None, timeout=10):
        calls.append(params)
        return {"items": [{"instrumentId": 77, "internalSymbolFull": "SHEL", "internalInstrumentDisplayName": "Shell plc"}]}

    monkeypatch.setattr(client, "_get", _fake_get)
    assert client.search_instrument("SHELL") is None
    # cache_only never touches the network.
    calls.clear()
    assert client.search_instrument("NEWSYM", cache_only=True) is None
    assert calls == []


def test_etoro_search_accepts_punctuation_variants_of_the_same_symbol(tmp_path, monkeypatch):
    client = _etoro_client(tmp_path)
    monkeypatch.setattr(
        client,
        "_get",
        lambda path, params=None, timeout=10: {"items": [{"instrumentId": 12, "internalSymbolFull": "BRK.B"}]},
    )
    found = client.search_instrument("BRK-B")
    assert found and found["instrument_id"] == 12


def test_etoro_rate_price_ignores_a_last_execution_outside_the_book():
    from core.etoro_client import rate_age_seconds, resolve_rate_price

    assert resolve_rate_price({"bid": 10.0, "ask": 10.1, "last": 10.05}) == (10.05, "last_execution")
    price, basis = resolve_rate_price({"bid": 10.0, "ask": 10.1, "last": 7.0})
    assert basis == "mid_last_execution_stale" and price == pytest.approx(10.05)
    assert resolve_rate_price({})[0] is None
    now = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)
    assert rate_age_seconds({"timestamp": "2026-11-13T21:00:00.1234567Z"}, now) == pytest.approx(125999.876544, abs=1)
    assert rate_age_seconds({"timestamp": ""}, now) is None


# ─────────────────────────────────────────────────────────────────────────────
# Content fetcher: every redirect hop is validated before it is requested
# ─────────────────────────────────────────────────────────────────────────────


def _dns(mapping):
    def _resolve(host, *args, **kwargs):
        ip = mapping.get(host)
        if ip is None:
            raise socket.gaierror
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _resolve


def test_redirect_to_loopback_is_refused_before_it_is_requested(monkeypatch):
    import core.content_fetcher as fetcher

    monkeypatch.setattr(fetcher, "_DEV_MODE", False)
    monkeypatch.setattr(
        fetcher.socket,
        "getaddrinfo",
        _dns({"www.reuters.com": "93.184.216.34", "127.0.0.1": "127.0.0.1", "internal.reuters.com": "10.0.0.5"}),
    )
    requested = []

    def _fake_http_get(url, **kwargs):
        requested.append((url, kwargs.get("allow_redirects")))
        return SimpleNamespace(
            status_code=302, headers={"Location": "http://127.0.0.1:8003/api/config"}, url=url, content=b"", text=""
        )

    monkeypatch.setattr(fetcher, "http_get", _fake_http_get)
    assert _real_fetch_article_content("https://www.reuters.com/a") is None
    assert requested == [("https://www.reuters.com/a", False)], "the loopback hop must never be contacted"

    # An allow-listed name that resolves to a private address is refused too.
    requested.clear()

    def _to_internal(url, **kwargs):
        requested.append(url)
        return SimpleNamespace(
            status_code=301, headers={"Location": "https://internal.reuters.com/x"}, url=url, content=b"", text=""
        )

    monkeypatch.setattr(fetcher, "http_get", _to_internal)
    assert _real_fetch_article_content("https://www.reuters.com/b") is None
    assert requested == ["https://www.reuters.com/b"]


def test_public_redirect_chain_is_followed_hop_by_hop(monkeypatch):
    import core.content_fetcher as fetcher

    monkeypatch.setattr(fetcher, "_DEV_MODE", False)
    monkeypatch.setattr(fetcher.socket, "getaddrinfo", _dns({"www.reuters.com": "93.184.216.34"}))
    body = "<html><body><article>" + ("Markets rallied on Friday. " * 5) + "</article></body></html>"
    pages = {
        "https://www.reuters.com/a": SimpleNamespace(status_code=302, headers={"Location": "/b"}, url="https://www.reuters.com/a", content=b"", text=""),
        "https://www.reuters.com/b": SimpleNamespace(status_code=200, headers={}, url="https://www.reuters.com/b", content=body.encode(), text=body),
    }
    requested = []
    monkeypatch.setattr(fetcher, "http_get", lambda url, **kw: requested.append(url) or pages[url])
    text = _real_fetch_article_content("https://www.reuters.com/a")
    assert requested == ["https://www.reuters.com/a", "https://www.reuters.com/b"]
    assert "Markets rallied" in text


def test_ipv4_mapped_ipv6_loopback_is_private(monkeypatch):
    import core.content_fetcher as fetcher

    entries = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:127.0.0.1", 0, 0, 0))]
    monkeypatch.setattr(fetcher.socket, "getaddrinfo", lambda *a, **k: entries)
    assert fetcher._is_private_ip("sneaky.example") is True


# ─────────────────────────────────────────────────────────────────────────────
# Engine: exits independent of slow scan work; stock rows never frozen
# ─────────────────────────────────────────────────────────────────────────────


def _bare_signal_manager(open_signals):
    from core.signals import SignalManager

    manager = object.__new__(SignalManager)
    manager.open_signals = dict(open_signals)
    manager.closed_signals = []
    manager.last_signal_time = {}
    manager.last_stop_time = {}
    manager._lock = threading.RLock()
    manager.save = lambda: None
    manager._sync_closed_signal_capital = lambda **kwargs: None
    return manager


def _bare_engine(open_signals=None, tickers=None, cryptos=("BTC-USD",)):
    from core.engine import MarketEngine

    engine = object.__new__(MarketEngine)
    engine.signals = _bare_signal_manager(open_signals or {})
    engine.config = SimpleNamespace(
        watchlist={"binance": list(cryptos), "yahoo": ["AAPL"]},
        scan={},
        signal={"rsi_period": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9},
    )
    engine.state_cache = {"tickers": dict(tickers or {})}
    engine.state_cache_lock = threading.Lock()
    engine.mtf_cache = {}
    engine.mtf_frame_cache = {}
    engine.mtf_cache_lock = threading.Lock()
    engine._yahoo_cache = {}
    engine._yahoo_cache_lock = threading.Lock()
    engine._yahoo_request_timestamps = deque(maxlen=100)
    engine.yfinance_lock = threading.Lock()
    engine.log_buffer = deque(maxlen=50)
    return engine


_VERIFIED = {"is_verified": True, "status": "verified"}


def _open_buy(ticker="BTC-USD", sl=99.0, tp=110.0):
    return {"sig1": {"ticker": ticker, "action": "BUY", "status": "Open", "entry": 100.0, "sl": sl, "tp": tp}}


def test_exit_watch_takes_a_stop_while_the_scan_is_busy(monkeypatch):
    import core.engine as engine_mod

    engine = _bare_engine(
        open_signals=_open_buy(),
        tickers={"BTC-USD": {"price": 100.0, "price_verified": True, "price_integrity": dict(_VERIFIED)}},
    )
    monkeypatch.setattr(
        engine_mod.BinanceClient, "get_bulk_last_prices", classmethod(lambda cls, tickers, ttl=5: {"BTC-USD": 98.5})
    )
    # The scan loop has been stuck in slow work (heavy scan / AI / news) for a minute.
    engine._scan_started_monotonic = time.monotonic() - 60.0
    outcomes = engine._exit_watch_tick()
    assert outcomes == {"BTC-USD": "LOST"}
    assert engine.signals.open_signals == {}
    closed = engine.signals.closed_signals[0]
    assert closed["close_reason"] == "Stop Loss Hit (BUY)"
    assert closed["outcome_data_valid"] is True
    # The scan picks the early outcome up when it finally reaches its own check.
    assert engine._drain_exit_watch_outcomes() == {"BTC-USD": "LOST"}
    assert engine._drain_exit_watch_outcomes() == {}


def test_exit_watch_is_idle_outside_a_scan_and_keeps_the_price_rule(monkeypatch):
    import core.engine as engine_mod

    engine = _bare_engine(
        open_signals=_open_buy(),
        tickers={"BTC-USD": {"price": 100.0, "price_verified": True, "price_integrity": dict(_VERIFIED)}},
    )
    calls = []

    def _prices(cls, tickers, ttl=5):
        calls.append(list(tickers))
        return {"BTC-USD": 90.0}

    monkeypatch.setattr(engine_mod.BinanceClient, "get_bulk_last_prices", classmethod(_prices))
    engine._scan_started_monotonic = None
    assert engine._exit_watch_tick() == {} and calls == []

    # A 10% jump is not a continuation of the verified price (same 2.5% rule
    # as the scan's fast poll): no settlement on unverified evidence.
    engine._scan_started_monotonic = time.monotonic() - 60.0
    assert engine._exit_watch_tick() == {}
    assert "sig1" in engine.signals.open_signals


def test_run_scan_flags_the_busy_window_and_clears_it(monkeypatch):
    engine = _bare_engine()
    observed = {}

    def _impl():
        observed["during"] = engine._scan_started_monotonic
        raise RuntimeError("scan failed")

    engine._run_scan_impl = _impl
    with pytest.raises(RuntimeError):
        engine.run_scan()
    assert observed["during"] is not None
    assert engine._scan_started_monotonic is None


def test_old_verified_rows_cannot_settle_a_signal():
    """Every stock row in state was written 2026-09-08 and still carried
    price_verified=True in November."""
    stale_stamp = (datetime.now(timezone.utc) - timedelta(days=68)).isoformat()
    fresh_stamp = datetime.now(timezone.utc).isoformat()
    engine = _bare_engine(open_signals={
        "old": {"ticker": "AAPL", "action": "BUY", "status": "Open", "entry": 350.0, "sl": 330.0, "tp": 400.0},
        "new": {"ticker": "BTC-USD", "action": "BUY", "status": "Open", "entry": 100.0, "sl": 99.0, "tp": 110.0},
    })
    outcomes = engine._check_signal_exits({
        "AAPL": {"price": 316.02, "price_verified": True, "price_integrity": dict(_VERIFIED), "last_update": stale_stamp},
        "BTC-USD": {"price": 98.0, "price_verified": True, "price_integrity": dict(_VERIFIED), "last_update": fresh_stamp},
    })
    assert outcomes == {"BTC-USD": "LOST"}
    assert "old" in engine.signals.open_signals


def _last_session_day() -> str:
    """Most recent weekday before today: a fixed date would age past the
    engine's 7-day "listing inactive" limit and flip the row to inactive."""
    day = datetime.now(timezone.utc).date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.isoformat()


def _daily_frame(closes):
    index = pd.date_range(end=_last_session_day(), periods=len(closes), freq="B")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1000] * len(closes)},
        index=index,
    )


def test_closed_session_refreshes_the_stock_row_without_intraday_fetch(monkeypatch):
    import core.market_hours as market_hours

    monkeypatch.setenv("MS_MARKET_HOURS_OVERRIDE", "closed")
    last_close = datetime.now(timezone.utc) - timedelta(hours=40)
    monkeypatch.setattr(
        market_hours,
        "us_equity_session_snapshot",
        lambda now=None: {
            "state": "CLOSED",
            "closed_kind": "weekend",
            "label": "Market closed (weekend)",
            "last_regular_close_utc": last_close.isoformat(),
            "next_regular_open_utc": (datetime.now(timezone.utc) + timedelta(hours=30)).isoformat(),
        },
    )
    old_row = {
        "price": 316.02, "last_update": "2026-09-08T22:56:46+03:00", "price_verified": True,
        "price_integrity": dict(_VERIFIED), "trend_1d": "BULL", "rsi": 47.2, "macd_hist": 1.0,
    }
    engine = _bare_engine(tickers={"AAPL": old_row})
    closes = [300.0 + i for i in range(60)]
    fetched = []

    def _fetch(ticker, interval="1m", period="1d"):
        fetched.append(interval)
        assert interval == "1d", "no intraday download while the session is closed"
        return _daily_frame(closes)

    engine._fetch_market_data = _fetch
    engine._get_mtf_trends = lambda ticker, is_crypto: {
        "1d": "BEAR", "4h": "BEAR", "1h": "BULL", "30m": "N/A", "15m": "N/A", "5m": "N/A",
        "ts_1d": "10:00:00", "raw_macd_1d": -0.42,
    }
    monkeypatch.setattr("core.engine.yf.Ticker", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no live quote")))

    now = datetime.now(timezone.utc)
    result = engine._process_stock_ticker("AAPL", now)
    data = result["data"]
    assert result["signal"] is None
    assert data["session"] == "closed" and data["session_closed_kind"] == "weekend"
    assert data["price"] == closes[-1] and data["price_as_of"] == _last_session_day()
    assert data["price_verified"] is False and data["price_integrity"]["status"] == "market_closed"
    assert data["last_update"] == now.isoformat()
    assert data["trend_1d"] == "BEAR" and data["trend_1h"] == "BULL"
    assert data["macd_hist"] == pytest.approx(-0.42)
    assert data["rsi_timeframe"] == "1d" and data["rsi"] != 47.2
    assert data["change_pct"] == pytest.approx((closes[-1] - closes[-2]) / closes[-2] * 100.0)

    # Candles do not move while the market is closed: no refetch on the next scan.
    engine.state_cache["tickers"]["AAPL"] = data
    fetched.clear()
    assert engine._process_stock_ticker("AAPL", now) is None
    assert fetched == []
    # A closed-session row can never settle a signal.
    engine.signals.open_signals = {
        "s": {"ticker": "AAPL", "action": "BUY", "status": "Open", "entry": 350.0, "sl": 400.0, "tp": 500.0}
    }
    assert engine._check_signal_exits({"AAPL": data}) == {}


def test_lagging_intraday_bars_refresh_the_row_but_block_signals(monkeypatch):
    """Regular session, 1-minute bars 10 minutes behind: pre-fix the ticker was
    dropped (row frozen); now the row is rewritten unverified, with no signal."""
    monkeypatch.setenv("MS_MARKET_HOURS_OVERRIDE", "open")
    engine = _bare_engine(tickers={"AAPL": {"price": 316.02, "last_update": "2026-09-08T22:56:46+03:00", "rsi": 47.2}})
    stale_index = pd.date_range(end=pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=10), periods=120, freq="min")
    intraday = pd.DataFrame(
        {"open": 355.0, "high": 356.0, "low": 354.0, "close": 355.5, "volume": 100}, index=stale_index
    )
    engine._fetch_market_data = lambda ticker, interval="1m", period="1d": intraday
    engine._get_mtf_trends = lambda ticker, is_crypto: {"1d": "BULL", "4h": "BULL", "1h": "BEAR"}
    engine._analyze_ticker = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no signal analysis on stale bars"))
    monkeypatch.setattr(
        "core.engine.yf.Ticker",
        lambda symbol: SimpleNamespace(info={"regularMarketPrice": 356.27, "regularMarketPreviousClose": 350.0}),
    )
    now = datetime.now(timezone.utc)
    result = engine._process_stock_ticker("AAPL", now)
    data = result["data"]
    assert result["signal"] is None
    assert data["price"] == 356.27 and data["last_update"] == now.isoformat()
    assert data["price_verified"] is False
    assert data["price_integrity"]["status"] == "stale_intraday_data"
    assert data["data_quality"] == "stale_intraday" and data["session"] == "regular"
    assert data["trend_1d"] == "BULL" and data["trend_1h"] == "BEAR"


def test_engine_does_not_call_news_stale_when_the_api_process_owns_news(monkeypatch):
    from core.data_guard import get_data_guard

    guard = get_data_guard()
    saved = dict(guard.last_timestamps)
    try:
        guard.last_timestamps.clear()
        guard.update_timestamp("binance")
        guard.update_timestamp("news", time.time() - 3600)
        engine = _bare_engine()
        monkeypatch.setenv("MS_PROCESS_ROLE", "engine")
        state, is_stale = engine._data_guard_state()
        assert state["news"] == "OWNED_BY_API_PROCESS" and is_stale is False
        # A source the engine does fetch still counts.
        guard.update_timestamp("binance", time.time() - 3600)
        assert engine._data_guard_state()[1] is True
        # In a single-process setup stale news is still reported.
        guard.update_timestamp("binance")
        monkeypatch.setenv("MS_PROCESS_ROLE", "api")
        assert engine._data_guard_state()[1] is True
    finally:
        guard.last_timestamps.clear()
        guard.last_timestamps.update(saved)


# ─────────────────────────────────────────────────────────────────────────────
# Shared read model: one capital sync per build
# ─────────────────────────────────────────────────────────────────────────────


def test_capital_read_model_syncs_storage_once(tmp_path, monkeypatch):
    """Seven getters x a full re-read of the capital blob per build cost 2.4 s
    (measured on the 11.7 MB live blob) and serialised every UI request."""
    import api.state_service as state_service
    import core.capital_manager as capital_mod
    from core.sqlite_store import SQLiteKVStore

    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", tmp_path / "capital.json")
    store = SQLiteKVStore(tmp_path / "cap.db")
    monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: store)
    capital_mod.CapitalManager._instance = None
    try:
        manager = capital_mod.CapitalManager()
        manager.set_balance(10_000.0, reason="test", clear_positions=True)
        monkeypatch.setattr(state_service, "get_capital_manager", lambda: manager)
        monkeypatch.setattr(state_service, "_equity_point_due", lambda: False)
        reads = []
        original = manager._read_state_from_storage

        def _counting_read():
            reads.append(1)
            return original()

        monkeypatch.setattr(manager, "_read_state_from_storage", _counting_read)
        payload = state_service._build_capital_payload([], {"open": {}, "closed": []}, fast_only=True)
        assert len(reads) == 1
        assert payload["raw_cash_balance"] == 10_000.0
        assert payload["accounting"]["deployable_cash_balance"] == 10_000.0
        assert isinstance(payload["ledger"], list) and payload["positions"] == []
    finally:
        capital_mod.CapitalManager._instance = None


def test_lean_state_variants_share_one_cache_entry(client, monkeypatch):
    import api.main as main

    builds = []

    def _fake_state(**kwargs):
        builds.append(kwargs)
        return {"tickers": [], "signals": [], "stats": {}, "timestamp": "t"}

    monkeypatch.setattr(main, "get_state", _fake_state)
    main._endpoint_payload_clear_prefix("api_state:")
    try:
        first = client.get("/api/state?include_news=false&include_hotlist=true&hotlist_details=false&lean=true")
        second = client.get("/api/state?include_news=false&include_hotlist=false&hotlist_details=false&lean=true")
        assert first.status_code == 200 and second.status_code == 200
        assert len(builds) == 1, "identical lean payloads must not be built twice"
    finally:
        main._endpoint_payload_clear_prefix("api_state:")


def test_diagnostics_reports_storage_and_runs_off_the_event_loop(client):
    import inspect

    import api.main as main

    assert inspect.iscoroutinefunction(main.api_health_diagnostics)
    assert not inspect.iscoroutinefunction(main._build_health_diagnostics_payload)
    payload = client.get("/api/health/diagnostics?fresh=true").json()
    assert "sqlite" in payload["storage"] and "state_store" in payload["storage"]
    assert isinstance(payload["yahoo_missing_symbols"], dict)


def test_etoro_symbol_match_accepts_same_listing_only():
    """Live map on 2026-11-15 held 60 wrong bindings, e.g. EA -> EAAS.L,
    crypto SAGA/SYN/ONE -> London stocks, TEL -> TEL.OL, NVD -> NVDA."""
    from core.etoro_client import symbols_match

    assert symbols_match("PSN", "PSN.US") and symbols_match("BSX", "BSX.RTH")
    assert symbols_match("PEPE", "PEPExM") and symbols_match("BRK-B", "BRK.B")
    for requested, instrument in (
        ("EA", "EAAS.L"), ("SAGA", "SAGA.L"), ("TEL", "TEL.OL"), ("NVD", "NVDA"),
        ("BBVA", "BBVA.MC"), ("SHELL", "SHELL.NV"), ("PROV", "PROVE"),
    ):
        assert not symbols_match(requested, instrument), (requested, instrument)


def test_etoro_disk_map_drops_bindings_to_a_different_instrument(tmp_path):
    import json

    from core.etoro_client import EToroClient

    client = EToroClient(config={"etoro": {"public_key": "pub", "private_key": "priv"}})
    client._search_cache = {}
    client._map_file = tmp_path / "map.json"
    client._map_file.write_text(json.dumps({
        "EA": {"instrument_id": 1, "symbol": "EAAS.L", "name": "x"},
        "AAPL": {"instrument_id": 2, "symbol": "AAPL", "name": "Apple"},
        "PEPE-USD": {"instrument_id": 3, "symbol": "PEPExM", "name": "Pepe (in millions)"},
        "ZZZ": None,
    }), encoding="utf-8")
    client._load_disk_map()
    assert "EA" not in client._search_cache
    assert client._search_cache["AAPL"][1]["instrument_id"] == 2
    assert client._search_cache["PEPE-USD"][1]["unit_multiplier"] == 1_000_000.0


def test_closed_session_marks_a_listing_without_recent_trades_inactive(monkeypatch):
    """EA: last trade 2026-08-04. Its final print is not 'the last close'."""
    import core.market_hours as market_hours
    import core.stock_data_client as sdc_module

    sdc_module.clear_missing()
    monkeypatch.setenv("MS_MARKET_HOURS_OVERRIDE", "closed")
    monkeypatch.setattr(
        market_hours,
        "us_equity_session_snapshot",
        lambda now=None: {
            "state": "CLOSED", "closed_kind": "weekend", "label": "Market closed (weekend)",
            "last_regular_close_utc": (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat(),
        },
    )
    engine = _bare_engine(tickers={})
    old = pd.DataFrame(
        {"open": 209.0, "high": 210.0, "low": 208.0, "close": [209.5, 209.7], "volume": 10},
        index=pd.to_datetime([datetime.now() - timedelta(days=104), datetime.now() - timedelta(days=103)]),
    )
    engine._fetch_market_data = lambda ticker, interval="1m", period="1d": old
    engine._get_mtf_trends = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no trend work for a dead listing"))
    try:
        result = engine._process_stock_ticker("EA", datetime.now(timezone.utc))
        data = result["data"]
        assert data["session"] == "inactive" and data["data_quality"] == "listing_inactive"
        assert data["price_verified"] is False and data["rsi"] is None
        assert "No trade since" in data["price_integrity"]["reason"]
    finally:
        sdc_module.clear_missing()
