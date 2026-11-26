from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from core.strategy_backtester import (
    _precompute_s8_mtf_features,
    _precompute_v2_ohlc_features,
    _s8_context_from_replay_row,
)
from core.strategy_v2.context_builder import build_context


def _ohlc_frame(rows=280):
    index = pd.date_range("2026-01-01", periods=rows, freq="min", tz="UTC")
    base = 100.0 + np.linspace(0.0, 8.0, rows) + (np.sin(np.arange(rows) / 7.0) * 1.5)
    return pd.DataFrame(
        {
            "open": base - 0.2,
            "high": base + 0.8,
            "low": base - 0.9,
            "close": base,
            "volume": 1000.0 + (np.arange(rows) % 30) * 35.0,
            "atr": np.full(rows, 0.75),
            "bb_width_5m": np.full(rows, 0.016),
        },
        index=index,
    )


def test_precomputed_v2_context_matches_causal_dataframe_context():
    df = _ohlc_frame()
    features = _precompute_v2_ohlc_features(df, "CRYPTO")
    index = 240
    row = df.iloc[index]
    row_context = SimpleNamespace(
        rsi=57.0,
        macd_hist=0.13,
        atr=float(row.atr),
        adx=26.0,
        ema20=float(df.close.ewm(span=20, adjust=False).mean().iloc[index]),
        bb_width_5m=float(row.bb_width_5m),
        vol_sma=float(df.volume.rolling(20).mean().iloc[index]),
        low=float(row.low),
        high=float(row.high),
        market_regime="TREND",
    )
    watch_row = {"price": float(row.close), "volume": float(row.volume)}

    causal = build_context(
        "BTC-USD",
        "CRYPTO",
        watch_row,
        ohlc_df=df.iloc[: index + 1],
        row_context=row_context,
    )
    precomputed = build_context(
        "BTC-USD",
        "CRYPTO",
        watch_row,
        row_context=row_context,
        precomputed_ohlc_features=features.iloc[index].to_dict(),
    )

    numeric_fields = {
        "ema200",
        "ema_slope",
        "bb_upper",
        "bb_mid",
        "bb_lower",
        "support",
        "resistance",
        "swing_low",
        "swing_high",
        "asian_high",
        "asian_low",
    }
    boolean_fields = {
        "volume_expansion",
        "retest_hold_long",
        "retest_hold_short",
        "squeeze_active",
        "squeeze_breakout_up",
        "squeeze_breakout_down",
        "impulse_up",
        "impulse_down",
        "consolidation_break_up",
        "consolidation_break_down",
        "session_breakout",
    }

    for field in numeric_fields:
        assert precomputed[field] == pytest.approx(causal[field]), field
    for field in boolean_fields:
        assert bool(precomputed[field]) is bool(causal[field]), field


def test_s8_replay_uses_only_completed_30m_1h_and_4h_bars():
    future = _ohlc_frame(rows=7000)
    prefix = future.iloc[:6000].copy()
    prefix_features = _precompute_s8_mtf_features(prefix)
    full_features = _precompute_s8_mtf_features(future)

    prefix_last = prefix_features.iloc[-1]
    full_same_time = full_features.loc[prefix.index[-1]]
    for column in prefix_features.columns:
        left = prefix_last[column]
        right = full_same_time[column]
        if pd.isna(left) and pd.isna(right):
            continue
        assert left == right, column

    context = _s8_context_from_replay_row(prefix_last)
    assert context["s8_primary_timeframe"] == "30m"
    assert context["s8_confirmation_timeframes"] == ["1h", "4h"]
    assert all(context["s8_timeframes"][tf]["available"] for tf in ("30m", "1h", "4h"))
    for timeframe in ("30m", "1h", "4h"):
        assert pd.Timestamp(context["s8_timeframes"][timeframe]["as_of"]) <= prefix.index[-1]
