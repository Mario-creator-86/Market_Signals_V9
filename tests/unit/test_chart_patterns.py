from __future__ import annotations

import pandas as pd
import pytest

from core.chart_patterns import ChartPatternEngine, analyze_chart_patterns, precompute_chart_patterns
from core.strategy_backtester import _precompute_v2_ohlc_features
from core.strategy_v2.context_builder import build_context


def _double_top_frame(final_close=89.0):
    # Confirmed highs at indices 2/8 (near 100), with the neckline low at 5.
    highs = [96, 98, 100, 98, 96, 95, 97, 99, 100.1, 98, 96, 94]
    lows = [92, 94, 96, 94, 92, 90, 93, 95, 96, 94, 92, 88]
    closes = [(high + low) / 2 for high, low in zip(highs, lows)]
    closes[-1] = final_close
    return pd.DataFrame(
        {"high": highs, "low": lows, "close": closes, "atr": [1.0] * len(highs)},
        index=pd.date_range("2026-01-01", periods=len(highs), freq="5min"),
    )


def test_double_top_requires_confirmed_pivots_and_neckline_break():
    engine = ChartPatternEngine(left_bars=1, right_bars=1)
    states = [engine.update(row.high, row.low, row.close, row.atr, timestamp=index)
              for index, row in _double_top_frame(final_close=89.0).iterrows()]
    assert states[-2]["chart_pattern"] == "DOUBLE_TOP"
    assert states[-2]["chart_pattern_status"] == "FORMING"
    assert states[-1]["chart_pattern_event"] == "DOUBLE_TOP"
    assert states[-1]["chart_pattern_status"] == "CONFIRMED"
    assert states[-1]["chart_pattern_neckline"] == pytest.approx(90.0)


def test_no_confirmation_when_price_does_not_break_neckline():
    result = analyze_chart_patterns(_double_top_frame(final_close=94.0), left_bars=1, right_bars=1)
    assert result["chart_pattern"] == "DOUBLE_TOP"
    assert result["chart_pattern_status"] == "FORMING"
    assert result["chart_pattern_event"] is None


def test_precompute_does_not_emit_before_current_breakout_bar():
    states = precompute_chart_patterns(_double_top_frame(final_close=89.0), left_bars=1, right_bars=1)
    assert pd.isna(states.iloc[-2]["chart_pattern_event"])
    assert states.iloc[-1]["chart_pattern_event"] == "DOUBLE_TOP"


def test_context_and_replay_precompute_expose_pattern_annotations():
    frame = _double_top_frame(final_close=89.0)
    frame["open"] = frame["close"]
    frame["volume"] = 10_000.0
    frame["bb_width_5m"] = 0.02
    # Use the explicit 1/1 unit fixture only to verify the common fields. Live
    # defaults remain 3/3 bars for a more conservative confirmation rule.
    direct = analyze_chart_patterns(frame, left_bars=1, right_bars=1)
    assert direct["chart_pattern_event"] == "DOUBLE_TOP"
    features = _precompute_v2_ohlc_features(frame, "CRYPTO_FUTURES")
    assert "chart_pattern" in features.columns
    assert "volume_ratio" in features.columns
    assert "price_acceptance_direction" in features.columns
    context = build_context(
        symbol="BTCUSDT",
        asset_class="CRYPTO_FUTURES",
        watch_row={"price": 89.0, "volume": 10_000.0},
        ohlc_df=frame,
    )
    assert "chart_pattern_status" in context
    assert "volume_flow_direction" in context
