from __future__ import annotations

import pandas as pd
import pytest

from core.market_structure import (
    MarketStructureEngine,
    analyze_market_structure,
    precompute_market_structure,
)
from core.strategy_backtester import _precompute_v2_ohlc_features
from core.strategy_v2.context_builder import build_context


def _frame(highs, lows, closes=None):
    closes = closes or [(high + low) / 2 for high, low in zip(highs, lows)]
    return pd.DataFrame(
        {"high": highs, "low": lows, "close": closes},
        index=pd.date_range("2026-01-01", periods=len(highs), freq="5min"),
    )


def test_pivot_is_not_visible_before_right_side_confirmation():
    engine = MarketStructureEngine(left_bars=2, right_bars=2)
    bars = [(1, 0, 0.5), (2, 1, 1.5), (5, 2, 3), (3, 1.5, 2), (2, 1, 1.5)]
    for high, low, close in bars[:-1]:
        state = engine.update(high, low, close)
    assert state["confirmed_pivot_count"] == 0

    state = engine.update(*bars[-1])
    assert state["confirmed_pivot_count"] == 1
    assert state["structure_last_high"] == pytest.approx(5.0)
    assert engine.pivots[0].index == 2
    assert engine.pivots[0].confirmed_index == 4


def test_classifies_hh_hl_and_bullish_structure():
    df = _frame(
        highs=[1, 2, 5, 3, 2, 4, 3, 6, 4, 3],
        lows=[0, 1, 2, 1, 0, 2, 1.5, 3, 2, 1.5],
    )
    result = analyze_market_structure(df, left_bars=1, right_bars=1)
    high_labels = [p["label"] for p in result["pivots"] if p["kind"] == "HIGH"]
    low_labels = [p["label"] for p in result["pivots"] if p["kind"] == "LOW"]
    assert "HH" in high_labels
    assert "HL" in low_labels
    assert result["structure_trend"] == "BULLISH"


def test_bullish_break_after_bearish_structure_is_choch():
    engine = MarketStructureEngine(left_bars=1, right_bars=1)
    bars = [
        (10, 8, 9), (12, 9, 11), (10, 7, 8),
        (11, 8, 10), (9, 6, 7), (10, 7, 9),
        (13, 8, 12.5),
    ]
    states = [engine.update(*bar) for bar in bars]
    assert any(p.label == "LH" for p in engine.pivots if p.kind == "HIGH")
    assert any(p.label == "LL" for p in engine.pivots if p.kind == "LOW")
    assert states[-1]["structure_event"] == "CHOCH"
    assert states[-1]["structure_event_direction"] == "BULLISH"


def test_precompute_matches_prefix_analysis_without_lookahead():
    df = _frame(
        highs=[1, 2, 5, 3, 2, 4, 3, 6, 4, 7, 5],
        lows=[0, 1, 2, 1, 0, 2, 1, 3, 2, 4, 3],
    )
    computed = precompute_market_structure(df, left_bars=1, right_bars=1)
    for position in range(len(df)):
        prefix = analyze_market_structure(df.iloc[: position + 1], left_bars=1, right_bars=1)
        row = computed.iloc[position]
        assert row["structure_trend"] == prefix["structure_trend"]
        assert row["confirmed_pivot_count"] == prefix["confirmed_pivot_count"]
        expected_high = prefix["structure_last_high"]
        if expected_high is None:
            assert pd.isna(row["structure_last_high"])
        else:
            assert row["structure_last_high"] == pytest.approx(expected_high)


def test_rejects_missing_ohlc_columns():
    with pytest.raises(ValueError, match="Missing OHLC"):
        analyze_market_structure(pd.DataFrame({"close": [1.0]}))


def test_context_builder_exposes_structural_features():
    df = _frame(
        highs=[1, 2, 5, 3, 2, 4, 3, 6, 4, 7],
        lows=[0, 1, 2, 1, 0, 2, 1, 3, 2, 4],
    )
    df["open"] = df["close"]
    df["volume"] = 1000.0
    context = build_context(
        symbol="BTCUSDT",
        asset_class="CRYPTO_FUTURES",
        watch_row={"price": float(df.close.iloc[-1]), "volume": 1000.0},
        ohlc_df=df,
    )
    assert context["confirmed_pivot_count"] > 0
    assert context["structure_trend"] in {"RANGE", "BULLISH", "BEARISH"}
    assert context["structural_resistance"] == context["structure_last_high"]


def test_replay_precompute_contains_same_structure_as_prefix_context():
    df = _frame(
        highs=[1, 2, 5, 3, 2, 4, 3, 6, 4, 7, 5, 8],
        lows=[0, 1, 2, 1, 0, 2, 1, 3, 2, 4, 3, 5],
    )
    df["open"] = df["close"]
    df["volume"] = 1000.0
    df["atr"] = 0.5
    df["bb_width_5m"] = 0.02
    features = _precompute_v2_ohlc_features(df, "CRYPTO_FUTURES")
    expected = analyze_market_structure(df, left_bars=3, right_bars=3)
    assert features.iloc[-1]["structure_trend"] == expected["structure_trend"]
    assert features.iloc[-1]["confirmed_pivot_count"] == expected["confirmed_pivot_count"]
