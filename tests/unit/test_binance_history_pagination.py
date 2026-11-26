"""Regression tests for truthful Binance historical-data pagination."""

from __future__ import annotations

import io
import json
import urllib.parse

from core.strategy_backtester import _fetch_klines


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _kline(open_time: int):
    return [open_time, "100", "101", "99", "100.5", "12"]


def test_fetch_klines_paginates_to_the_requested_range(monkeypatch):
    interval_ms = 60_000
    klines = [_kline(index * interval_ms) for index in range(1_500)]

    def fake_urlopen(url, timeout):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        start = int(query["startTime"][0])
        end = int(query["endTime"][0])
        payload = [row for row in klines if start <= int(row[0]) < end][:1_000]
        return _Response(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr("core.strategy_backtester.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("core.strategy_backtester.time.sleep", lambda _seconds: None)

    dataframe = _fetch_klines("BTCUSDT", "1m", 0, len(klines) * interval_ms)

    assert dataframe is not None
    assert len(dataframe) == 1_500
    assert dataframe.attrs["expected_bars"] == 1_500
    assert dataframe.attrs["received_bars"] == 1_500
    assert dataframe.attrs["source_truncated"] is False


def test_fetch_klines_marks_an_explicit_page_cap_as_truncated(monkeypatch):
    interval_ms = 60_000
    klines = [_kline(index * interval_ms) for index in range(1_500)]

    def fake_urlopen(url, timeout):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        start = int(query["startTime"][0])
        end = int(query["endTime"][0])
        payload = [row for row in klines if start <= int(row[0]) < end][:1_000]
        return _Response(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr("core.strategy_backtester.urllib.request.urlopen", fake_urlopen)

    dataframe = _fetch_klines(
        "BTCUSDT",
        "1m",
        0,
        len(klines) * interval_ms,
        max_pages=1,
    )

    assert dataframe is not None
    assert len(dataframe) == 1_000
    assert dataframe.attrs["source_truncated"] is True
