from __future__ import annotations

import time
from unittest.mock import patch

import pandas as pd
import pytest

from api.main import _build_chart_payload, _chart_cache, api_chart, api_entry_chart


def _ohlcv(rows: int = 40) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=rows, freq="5min", tz="UTC")
    close = pd.Series([100.0 + index_ * 0.1 for index_ in range(rows)], index=index)
    return pd.DataFrame(
        {
            "open": close - 0.05,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 1000.0,
        },
        index=index,
    )


def test_chart_payload_has_bars_and_common_annotation_schema():
    with patch("api.main._fetch_chart_frame", return_value=(_ohlcv(), "test")):
        payload = _build_chart_payload("BTCUSDT", "5m")
    assert payload["available"] is True
    assert len(payload["bars"]) == 40
    assert {
        "pivots", "structure_events", "liquidity_pools", "liquidity_events", "chart_patterns", "volume_events",
        "supertrend_events",
    } == set(payload["annotations"])
    assert payload["bars"][0]["time"] > 0
    assert len(payload["replay_states"]) == len(payload["bars"])
    assert payload["asset_class"] == "CRYPTO"
    assert payload["state"]["supertrend_period"] == 10
    assert len(payload["indicators"]["supertrend"]) > 0


def test_chart_endpoint_validates_timeframe():
    try:
        api_chart("BTCUSDT", timeframe="2m")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 400
    else:
        raise AssertionError("Unsupported timeframe should be rejected")


def test_chart_endpoint_supports_s8_minimum_30m_timeframe():
    _chart_cache.clear()
    fake = {"symbol": "AAPL", "timeframe": "30m", "bars": [], "available": False, "source": "test"}
    with patch("api.main._build_chart_payload", return_value=fake) as build:
        payload = api_chart("AAPL", timeframe="30m", refresh=True)
    assert payload["timeframe"] == "30m"
    build.assert_called_once_with("AAPL", "30m")


def test_entry_chart_endpoint_serves_immutable_sandboxed_svg(tmp_path):
    path = tmp_path / ("entry_" + "a" * 24 + ".svg")
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>', encoding="utf-8")
    with patch("core.entry_chart_snapshot.resolve_entry_chart_path", return_value=path):
        response = api_entry_chart("entry_" + "a" * 24)
    assert response.media_type == "image/svg+xml"
    assert "immutable" in response.headers["cache-control"]
    assert "sandbox" in response.headers["content-security-policy"]


def test_chart_endpoint_uses_cache():
    fake = {"symbol": "BTCUSDT", "timeframe": "5m", "bars": [], "available": False, "source": "test"}
    with patch("api.main._build_chart_payload", return_value=fake) as build:
        first = api_chart("btcusdt", timeframe="5m", refresh=True)
        second = api_chart("btcusdt", timeframe="5m")
    assert first["cache"] == "miss"
    assert second["cache"] == "hit"
    build.assert_called_once()


def test_chart_endpoint_serves_stale_payload_while_refreshing():
    fake = {
        "symbol": "BTC-USD",
        "timeframe": "5m",
        "bars": [{"time": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
        "available": True,
        "source": "test",
    }
    _chart_cache.clear()
    _chart_cache["BTC-USD:5m"] = {
        "created_at": time.time() - 120,
        "payload": fake,
    }

    with patch("api.main._schedule_chart_refresh", return_value=True) as schedule, \
         patch("api.main._build_chart_payload") as build:
        payload = api_chart("BTCUSDT", timeframe="5m")

    assert payload["cache"] == "stale"
    assert payload["refreshing"] is True
    assert payload["cache_age_seconds"] >= 100
    schedule.assert_called_once_with("BTC-USD:5m", "BTC-USD", "5m")
    build.assert_not_called()


def test_chart_endpoint_restores_persistent_payload_without_blocking():
    fake = {
        "symbol": "AAPL",
        "timeframe": "5m",
        "bars": [{"time": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
        "available": True,
        "source": "test",
    }
    persisted = {"created_at": time.time() - 120, "payload": fake}
    _chart_cache.clear()

    with patch("api.main._load_persisted_chart_payload", return_value=persisted) as load, \
         patch("api.main._schedule_chart_refresh", return_value=True), \
         patch("api.main._build_chart_payload") as build:
        payload = api_chart("AAPL", timeframe="5m")

    assert payload["cache"] == "stale"
    load.assert_called_once_with("AAPL:5m")
    build.assert_not_called()


@pytest.mark.parametrize(
    "requested,canonical,asset_class",
    [
        ("btcusdt", "BTC-USD", "CRYPTO"),
        ("GOLD", "GC=F", "COMMODITY"),
        ("oil", "CL=F", "COMMODITY"),
        ("aapl", "AAPL", "STOCK"),
    ],
)
def test_chart_endpoint_normalizes_multi_asset_symbols(requested, canonical, asset_class):
    _chart_cache.clear()

    def fake_builder(symbol, timeframe):
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "bars": [],
            "available": False,
            "source": "test",
            "asset_class": asset_class,
        }

    with patch("api.main._build_chart_payload", side_effect=fake_builder) as build:
        payload = api_chart(requested, timeframe="5m", refresh=True)

    assert payload["symbol"] == canonical
    assert payload["asset_class"] == asset_class
    build.assert_called_once_with(canonical, "5m")


def test_chart_replay_hides_future_bars_and_annotations():
    frame = _ohlcv(40)
    # Make the final candles a deterministic two-close volume acceptance event.
    frame.loc[frame.index[-2:], "close"] = [105.0, 106.0]
    frame.loc[frame.index[-2:], "high"] = [105.2, 106.2]
    frame.loc[frame.index[-2:], "low"] = [104.6, 105.6]
    frame.loc[frame.index[-2:], "volume"] = [4_000.0, 4_500.0]
    with patch("api.main._fetch_chart_frame", return_value=(frame, "test")):
        full = _build_chart_payload("BTCUSDT", "5m")
    from core.chart_replay import apply_visual_replay

    replay = apply_visual_replay(full, 20)
    assert len(replay["bars"]) == 21
    assert replay["replay"]["lookahead_safe"] is True
    assert all(item["index"] <= 20 for item in replay["annotations"]["volume_events"])
    assert all(item["index"] <= 20 for item in replay["indicators"]["supertrend"])
