"""Recorder for Binance USD-M positioning data (open interest, long/short ratios, taker flow).

Binance serves these series for the last 30 days only, so anything not saved is
lost. The recorder must backfill what is still available, add only new hours on
later runs, never ask for a start older than the window, and survive one bad symbol.
"""
from __future__ import annotations

import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest

from core.crypto_derivatives_recorder import (
    HOUR_MS,
    SERIES,
    WINDOW_DAYS,
    DerivativesRecorder,
    RateLimited,
)

NOW = 1_795_420_800_000  # an exact hour
DAY = 24 * HOUR_MS


class FakeBinance:
    """Behaves like the public endpoints: hourly rows, 500 per call, 30-day window."""

    def __init__(self, *, now=NOW, fail_symbols=(), rate_limit_after=None):
        self.now = now
        self.fail_symbols = set(fail_symbols)
        self.rate_limit_after = rate_limit_after
        self.calls = []
        self.perps = ["BTCUSDT", "ETHUSDT", "DOGEUSDT", "TINYUSDT"]
        self.volumes = {"BTCUSDT": 9e9, "ETHUSDT": 4e9, "DOGEUSDT": 6e6, "TINYUSDT": 1e5}

    def __call__(self, path):
        self.calls.append(path)
        if self.rate_limit_after is not None and len(self.calls) > self.rate_limit_after:
            return 429, {"code": -1003, "msg": "Too many requests"}
        url = urlparse(path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/fapi/v1/exchangeInfo":
            return 200, {"symbols": [
                {"symbol": s, "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"} for s in self.perps
            ] + [{"symbol": "BTCUSDT_261225", "contractType": "CURRENT_QUARTER", "quoteAsset": "USDT", "status": "TRADING"}]}
        if url.path == "/fapi/v1/ticker/24hr":
            return 200, [{"symbol": s, "quoteVolume": str(v)} for s, v in self.volumes.items()]
        if url.path == "/fapi/v1/premiumIndex":
            return 200, [{"symbol": s, "markPrice": "100.5", "indexPrice": "100.4", "lastFundingRate": "0.0001",
                          "time": self.now} for s in self.perps]
        symbol = q["symbol"]
        if symbol in self.fail_symbols:
            return 500, "upstream error"
        start, end = int(q["startTime"]), int(q["endTime"])
        if start < self.now - WINDOW_DAYS * DAY:
            return 400, {"code": -1130, "msg": "parameter 'startTime' is invalid."}
        first = -(-start // HOUR_MS) * HOUR_MS
        last = (min(end, self.now) - 1) // HOUR_MS * HOUR_MS  # the newest row is the last completed hour
        stamps = list(range(first, last + 1, HOUR_MS))[-int(q.get("limit", 30)):]
        if url.path.endswith("openInterestHist"):
            rows = [{"symbol": symbol, "sumOpenInterest": "1000.5", "sumOpenInterestValue": "61000000.0", "timestamp": t} for t in stamps]
        elif url.path.endswith("takerlongshortRatio"):
            rows = [{"buySellRatio": "1.1", "buyVol": "500.0", "sellVol": "454.5", "timestamp": t} for t in stamps]
        else:
            rows = [{"symbol": symbol, "longShortRatio": "1.5", "longAccount": "0.6", "shortAccount": "0.4", "timestamp": t} for t in stamps]
        return 200, rows


def _recorder(tmp_path, api, **kwargs):
    return DerivativesRecorder(tmp_path / "derivs.db", fetch=api, now_ms=lambda: api.now, pace_seconds=0, **kwargs)


def _count(tmp_path, where="1=1", params=()):
    with sqlite3.connect(tmp_path / "derivs.db") as db:
        return db.execute(f"SELECT COUNT(*) FROM series_1h WHERE {where}", params).fetchone()[0]


def test_first_sync_backfills_the_whole_available_window(tmp_path):
    api = FakeBinance()
    summary = _recorder(tmp_path, api).sync(["BTCUSDT"])

    hours = WINDOW_DAYS * 24 - 2  # the recorder keeps a two-hour margin inside the 30-day window
    for series in SERIES:
        assert _count(tmp_path, "symbol=? AND series=?", ("BTCUSDT", series)) >= hours - 1, series
    assert summary["rows_added"] == _count(tmp_path)
    assert summary["errors"] == [] and summary["symbols"] == 1
    # never asks for a start older than the window (the API answers 400 to that)
    assert all(code != 400 for code in summary["http_statuses"])


def test_second_sync_only_adds_the_new_hours_and_is_idempotent(tmp_path):
    api = FakeBinance()
    recorder = _recorder(tmp_path, api)
    recorder.sync(["BTCUSDT"])
    before = _count(tmp_path)
    calls_before = len(api.calls)

    again = recorder.sync(["BTCUSDT"])
    assert again["rows_added"] == 0 and _count(tmp_path) == before
    assert len(api.calls) - calls_before == len(SERIES) + 1  # one request per series + the mark snapshot

    api.now += 3 * HOUR_MS
    later = recorder.sync(["BTCUSDT"])
    assert later["rows_added"] == 3 * len(SERIES)


def test_values_are_stored_per_series(tmp_path):
    api = FakeBinance()
    _recorder(tmp_path, api).sync(["BTCUSDT"])
    with sqlite3.connect(tmp_path / "derivs.db") as db:
        oi = db.execute("SELECT a, b FROM series_1h WHERE series='open_interest' LIMIT 1").fetchone()
        ratio = db.execute("SELECT a, b, c FROM series_1h WHERE series='ratio_global_account' LIMIT 1").fetchone()
        taker = db.execute("SELECT a, b, c FROM series_1h WHERE series='taker' LIMIT 1").fetchone()
        mark = db.execute("SELECT mark_price, last_funding_rate FROM mark_snapshots WHERE symbol='BTCUSDT'").fetchone()
    assert oi == (1000.5, 61000000.0)
    assert ratio == (1.5, 0.6, 0.4)
    assert taker == (1.1, 500.0, 454.5)
    assert mark == (100.5, 0.0001)


def test_one_failing_symbol_does_not_stop_the_others(tmp_path):
    api = FakeBinance(fail_symbols={"ETHUSDT"})
    summary = _recorder(tmp_path, api).sync(["ETHUSDT", "BTCUSDT"])

    assert _count(tmp_path, "symbol='BTCUSDT'") > 0
    assert _count(tmp_path, "symbol='ETHUSDT'") == 0
    assert summary["errors"] and all("ETHUSDT" in e for e in summary["errors"])


def test_a_rate_limit_answer_stops_the_run_and_keeps_what_was_saved(tmp_path):
    api = FakeBinance(rate_limit_after=3)
    recorder = _recorder(tmp_path, api)
    with pytest.raises(RateLimited):
        recorder.sync(["BTCUSDT", "ETHUSDT"])
    assert _count(tmp_path) > 0  # rows fetched before the limit are committed
    calls = len(api.calls)
    assert calls <= 5  # it did not keep hammering the API


def test_universe_is_liquid_usdt_perpetuals_and_stays_sticky(tmp_path):
    api = FakeBinance()
    recorder = _recorder(tmp_path, api)
    assert recorder.select_universe(min_quote_volume=5e6) == ["BTCUSDT", "DOGEUSDT", "ETHUSDT"]

    recorder.sync(["DOGEUSDT"])
    api.volumes["DOGEUSDT"] = 1e3  # volume dries up: the series must not get a hole
    assert "DOGEUSDT" in recorder.select_universe(min_quote_volume=5e6)

    api.perps.remove("DOGEUSDT")  # delisted: nothing left to record
    assert "DOGEUSDT" not in recorder.select_universe(min_quote_volume=5e6)


def test_sync_without_symbols_uses_the_universe_and_records_the_run(tmp_path):
    api = FakeBinance()
    summary = _recorder(tmp_path, api).sync()
    assert summary["symbols"] == 3
    with sqlite3.connect(tmp_path / "derivs.db") as db:
        runs = db.execute("SELECT symbols, rows_added, error_count FROM runs").fetchall()
    assert runs == [(3, summary["rows_added"], 0)]


def test_a_symbol_with_non_ascii_letters_is_sent_url_encoded(tmp_path):
    """Binance lists perpetuals whose ticker is not ASCII; a raw one cannot be put in a URL."""
    api = FakeBinance()
    symbol = "牛来USDT"
    summary = _recorder(tmp_path, api).sync([symbol])

    assert summary["errors"] == []
    assert _count(tmp_path, "symbol=?", (symbol,)) > 0
    series_calls = [c for c in api.calls if "/futures/data/" in c]
    assert series_calls and all(c.isascii() for c in series_calls)
