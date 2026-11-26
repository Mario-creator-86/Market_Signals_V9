from __future__ import annotations

import pandas as pd

from core.strategy_backtester import _precompute_v2_ohlc_features
from core.strategy_v2.context_builder import build_context
from core.supertrend import analyze_supertrend, precompute_supertrend


def _reversal_frame() -> pd.DataFrame:
    close = [100.0 - index * 0.5 for index in range(25)]
    close += [88.0 + index * 1.5 for index in range(20)]
    close += [116.0 - index * 1.8 for index in range(20)]
    index = pd.date_range("2026-01-01", periods=len(close), freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [value - 0.1 for value in close],
            "high": [value + 0.4 for value in close],
            "low": [value - 0.4 for value in close],
            "close": close,
            "volume": [2_000_000.0] * len(close),
            "atr": [1.0] * len(close),
            "bb_width_5m": [0.02] * len(close),
        },
        index=index,
    )


def test_supertrend_detects_bullish_and_bearish_flips_without_initial_false_flip():
    states = precompute_supertrend(_reversal_frame())
    flips = [value for value in states["supertrend_flip"].tolist() if isinstance(value, str)]
    assert flips == ["BULLISH", "BEARISH"]
    assert states.iloc[-1]["supertrend_direction"] == "BEARISH"
    assert states.iloc[-1]["supertrend_line"] > _reversal_frame().iloc[-1]["close"]


def test_supertrend_prefix_is_unchanged_when_future_bars_are_appended():
    frame = _reversal_frame()
    prefix = precompute_supertrend(frame.iloc[:45]).reset_index(drop=True)
    full_prefix = precompute_supertrend(frame).iloc[:45].reset_index(drop=True)
    pd.testing.assert_frame_equal(prefix, full_prefix)


def test_context_and_faithful_precompute_expose_same_supertrend_state():
    frame = _reversal_frame()
    analysis = analyze_supertrend(frame)
    context = build_context(
        "BTC-USD",
        "CRYPTO_FUTURES",
        {"price": float(frame.iloc[-1].close), "volume": 2_000_000.0},
        ohlc_df=frame,
    )
    replay = _precompute_v2_ohlc_features(frame, "CRYPTO_FUTURES").iloc[-1]
    assert context["supertrend_direction"] == analysis["supertrend_direction"] == "BEARISH"
    assert replay["supertrend_direction"] == context["supertrend_direction"]
    assert replay["supertrend_last_flip_direction"] == context["supertrend_last_flip_direction"]
    assert int(replay["supertrend_flip_age_bars"]) == int(context["supertrend_flip_age_bars"])

