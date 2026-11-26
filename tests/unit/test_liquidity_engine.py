from __future__ import annotations

import pandas as pd
import pytest

from core.liquidity_engine import analyze_liquidity, precompute_liquidity
from core.strategy_backtester import _precompute_v2_ohlc_features
from core.strategy_v2.context_builder import build_context


def _liquidity_frame(last_high=100.2, last_low=98.8, last_close=99.5):
    # Peaks at indices 4 and 10 each have three lower bars on both sides, so
    # both are confirmed before the potential sweep at index 14.
    highs = [94, 95, 96, 97, 100, 97, 96, 95, 96, 97, 100.05, 97, 96, 95, last_high]
    lows = [90, 91, 92, 93, 95, 93, 92, 91, 92, 93, 95, 93, 92, 91, last_low]
    closes = [(h + l) / 2 for h, l in zip(highs, lows)]
    closes[-1] = last_close
    frame = pd.DataFrame(
        {"high": highs, "low": lows, "close": closes, "atr": [1.0] * len(highs)},
        index=pd.date_range("2026-01-01", periods=len(highs), freq="5min"),
    )
    return frame


def test_detects_equal_high_buy_side_pool():
    result = analyze_liquidity(_liquidity_frame(last_high=99.0, last_close=98.0))
    buy_pools = [item for item in result["liquidity_pools"] if item["side"] == "BUY_SIDE"]
    assert buy_pools
    assert buy_pools[0]["touches"] >= 2
    assert buy_pools[0]["level"] == pytest.approx(100.025)


def test_buy_side_sweep_and_reclaim_is_bearish():
    result = analyze_liquidity(_liquidity_frame(last_high=101.0, last_close=99.0))
    assert result["liquidity_event"] == "SWEEP_RECLAIM"
    assert result["liquidity_event_direction"] == "BEARISH"
    assert result["liquidity_event_level"] == pytest.approx(100.025)


def test_wick_without_close_reclaim_is_not_a_sweep_signal():
    result = analyze_liquidity(_liquidity_frame(last_high=101.0, last_close=100.5))
    assert result["liquidity_event"] is None


def test_precomputed_liquidity_is_causal():
    frame = _liquidity_frame(last_high=101.0, last_close=99.0)
    states = precompute_liquidity(frame)
    assert pd.isna(states.iloc[-2]["liquidity_event"])
    assert states.iloc[-1]["liquidity_event"] == "SWEEP_RECLAIM"


def test_context_and_replay_precompute_expose_liquidity_features():
    frame = _liquidity_frame(last_high=101.0, last_close=99.0)
    frame["open"] = frame["close"]
    frame["volume"] = 10_000.0
    frame["bb_width_5m"] = 0.02
    context = build_context(
        symbol="BTCUSDT",
        asset_class="CRYPTO_FUTURES",
        watch_row={"price": 99.0, "volume": 10_000.0},
        ohlc_df=frame,
    )
    features = _precompute_v2_ohlc_features(frame, "CRYPTO_FUTURES")
    assert context["liquidity_event"] == "SWEEP_RECLAIM"
    assert context["liquidity_event_direction"] == "BEARISH"
    assert features.iloc[-1]["liquidity_event"] == "SWEEP_RECLAIM"
