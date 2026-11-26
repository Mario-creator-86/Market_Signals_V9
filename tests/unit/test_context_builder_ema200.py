"""
tests/unit/test_context_builder_ema200.py
=========================================
Regression guard for high-finding (f): in ``build_context``'s ``row_context``
branch (taken by BOTH the live engine and the backtester replay), ema200 and
ema_slope must be COMPUTED from ``ohlc_df``, not defaulted to ``price`` / ``0``.

THE BUG (pre-fix)
-----------------
RowContext carries ema20 but NOT ema200/ema_slope, so the row_context branch
did::

    ctx["ema200"] = _f(row.get("ema200"), ctx["price"])   # -> price (watch_row has no ema200)
    ctx["ema_slope"] = _f(row.get("ema_slope"), 0.0)      # -> 0.0

With ema200==price and ema_slope==0:
- regime_engine scored ema_slope as a RANGE component (never TREND) -> biased
  regime toward RANGE;
- TrendPullbackEvaluator's `price > ema200` / `price < ema200` gates always
  failed -> TrendPullback suppressed entirely;
- SessionBreakoutEvaluator's ema200 trend-alignment filter went no-op.

Both live (engine.py) and replay (strategy_backtester.py passes row_context
too) were affected identically, so live<->replay parity is preserved by the fix.

THE FIX
-------
Compute ema200 + a 1-bar ema200 slope from ohlc_df when it has >=2 close rows;
fall back to the old defaults only when df is missing/short.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
__import__("os").environ.setdefault("TEST_MODE", "1")
__import__("os").environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from core.strategy_v2.context_builder import build_context  # noqa: E402


def _row_context(price: float) -> SimpleNamespace:
    """Minimal RowContext stand-in: build_context only reads these attrs in the
    row_context branch (rsi, macd_hist, atr, adx, ema20, bb_width_5m, vol_sma,
    low, high, market_regime)."""
    return SimpleNamespace(
        rsi=55.0, macd_hist=0.03, atr=0.5, adx=25.0, ema20=price - 0.01,
        bb_width_5m=0.02, vol_sma=1000.0, low=price - 0.1, high=price + 0.1,
        market_regime="MIXED",
    )


def _uptrend_df(n: int = 250) -> pd.DataFrame:
    """Smooth uptrend: close rises from 100 -> ~112.25. In an uptrend the
    200-EWMA lags below the last close, and its 1-bar slope is positive."""
    idx = pd.date_range("2025-01-01 09:30", periods=n, freq="1min", name="dt")
    close = pd.Series(100.0 + 0.05 * pd.Series(range(n)))
    # Build on the Series' own RangeIndex, THEN assign the datetime index --
    # passing index=idx directly aligns the RangeIndex'd Series against the
    # datetime index and NaNs out every value.
    df = pd.DataFrame(
        {"open": close - 0.05, "high": close + 0.1, "low": close - 0.1,
         "close": close, "volume": pd.Series([10_000.0] * n)},
    )
    df.index = idx
    return df


class TestBuildContextEma200ComputedFromDf:
    """High-finding (f): ema200/ema_slope must be computed from ohlc_df in the
    row_context branch, not defaulted to price/0."""

    def test_uptrend_ema200_below_price_and_slope_positive(self):
        df = _uptrend_df(250)
        price = float(df["close"].iloc[-1])
        ctx = build_context(
            symbol="AAPL", asset_class="US_STOCKS",
            watch_row={"price": price, "change_pct": 0.0, "volume": 10_000.0},
            ohlc_df=df, row_context=_row_context(price),
        )
        # ema200 must be COMPUTED (differs from price), lag below price in uptrend.
        assert ctx["ema200"] != pytest.approx(price, abs=0.01), (
            "ema200 defaulted to price (pre-fix bug) instead of being computed from ohlc_df"
        )
        assert ctx["ema200"] < price, "uptrend: 200-EWMA should lag below last close"
        # slope must be COMPUTED (non-zero) and positive in an uptrend.
        assert ctx["ema_slope"] != 0.0, "ema_slope defaulted to 0 (pre-fix bug)"
        assert ctx["ema_slope"] > 0.0, "uptrend: ema200 slope should be positive"

    def test_downtrend_ema200_above_price_and_slope_negative(self):
        n = 250
        idx = pd.date_range("2025-01-01 09:30", periods=n, freq="1min", name="dt")
        close = pd.Series(100.0 - 0.05 * pd.Series(range(n)))  # smooth downtrend
        df = pd.DataFrame(
            {"open": close + 0.05, "high": close + 0.1, "low": close - 0.1,
             "close": close, "volume": pd.Series([10_000.0] * n)},
        )
        df.index = idx
        price = float(df["close"].iloc[-1])
        ctx = build_context(
            symbol="AAPL", asset_class="US_STOCKS",
            watch_row={"price": price, "change_pct": 0.0, "volume": 10_000.0},
            ohlc_df=df, row_context=_row_context(price),
        )
        assert ctx["ema200"] > price, "downtrend: 200-EWMA should lag above last close"
        assert ctx["ema_slope"] < 0.0, "downtrend: ema200 slope should be negative"

    def test_short_df_falls_back_gracefully(self):
        """When ohlc_df has <2 close rows, ema200/ema_slope fall back to the
        old defaults (price / 0.0) -- no crash, no NaN."""
        df = _uptrend_df(1)  # only 1 row -> can't compute a 2-point slope
        price = float(df["close"].iloc[-1])
        ctx = build_context(
            symbol="AAPL", asset_class="US_STOCKS",
            watch_row={"price": price, "change_pct": 0.0, "volume": 10_000.0},
            ohlc_df=df, row_context=_row_context(price),
        )
        assert ctx["ema200"] == pytest.approx(price, abs=1e-9)
        assert ctx["ema_slope"] == 0.0

    def test_no_df_falls_back_gracefully(self):
        """ohlc_df=None -> fallback to price/0 (the row_context branch must not
        require a df)."""
        price = 100.0
        ctx = build_context(
            symbol="AAPL", asset_class="US_STOCKS",
            watch_row={"price": price, "change_pct": 0.0, "volume": 10_000.0},
            ohlc_df=None, row_context=_row_context(price),
        )
        assert ctx["ema200"] == pytest.approx(price, abs=1e-9)
        assert ctx["ema_slope"] == 0.0