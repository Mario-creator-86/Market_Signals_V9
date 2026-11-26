"""
tests/unit/test_context_builder_composites.py
=============================================
Regression guards for Runda-5 findings C1, H1, M5, M6 in
``core/strategy_v2/context_builder.py``.

C1 -- V2 strategies S2-S6 were DEAD in the row_context branch (taken by BOTH
the live engine and the backtester replay): the branch returned without setting
``retest_hold_long/short``, ``squeeze_active/breakout_up/down``, ``bb_upper/
bb_lower``, ``impulse_up/down``, ``consolidation_break_up/down``,
``session_breakout``, ``asian_high/low``, ``volume_expansion``. S2-S6 evaluators
read ``context.get(field, False)`` / ``price*1.01`` defaults -> entry conditions
impossible -> V2 in BOTH_AUTO_BEST only ever produced S1 TrendPullback
(strategy diversity = 0). Fix: compute the composites from ``ohlc_df`` in the
row_context branch (mirrors the df-branch), look-ahead-safe.

H1 -- ``session_breakout`` was always False even in the df-branch because
``recent_48`` included the current bar (``close <= high <= asian_high`` always)
-> S6 FxSessionBreakout dead in all paths. Fix: exclude the current bar
(``iloc[:-1]``) so a close above the prior Asian range is achievable.

M5 -- row_context branch used ``row_context.low/high`` (current 1m bar) as
structural support/resistance/swing -> 1m noise. Fix: compute from multi-bar
windows of ``ohlc_df`` (matches df-branch).

M6 -- df-branch ``ema_slope`` was derived from ema20 while the row_context
branch derived it from ema200 -> parity divergence + regime thresholds
(0.0008/0.0006) calibrated for ema200. Fix: df-branch uses ema200 too.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
__import__("os").environ.setdefault("TEST_MODE", "1")
__import__("os").environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from core.strategy_v2.context_builder import build_context, infer_asset_class  # noqa: E402


def test_infer_asset_class_keeps_commodities_separate_from_us_stocks():
    assert infer_asset_class("GC=F") == "COMMODITIES"
    assert infer_asset_class("AAPL") == "US_STOCKS"


def _row_context(price: float) -> SimpleNamespace:
    """Minimal RowContext stand-in. ``low``/``high`` are the current 1m bar's
    low/high (the M5 noise source pre-fix)."""
    return SimpleNamespace(
        rsi=55.0, macd_hist=0.03, atr=0.5, adx=25.0, ema20=price - 0.01,
        bb_width_5m=0.02, vol_sma=10000.0, low=price - 0.1, high=price + 0.1,
        market_regime="MIXED",
    )


def _breakout_df(n: int = 60) -> pd.DataFrame:
    """60 bars: 0..n-2 consolidate at 100 (high=100, low=99.5); last bar breaks
    out (close=102, high=102.5, low=100.5) on a volume spike."""
    idx = pd.date_range("2025-01-01 09:30", periods=n, freq="1min", name="dt")
    close = pd.Series([100.0] * (n - 1) + [102.0])
    high = pd.Series([100.0] * (n - 1) + [102.5])
    low = pd.Series([99.5] * (n - 1) + [100.5])
    open_ = pd.Series([100.0] * (n - 1) + [100.5])
    vol = pd.Series([10000.0] * (n - 1) + [20000.0])
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol})
    df.index = idx
    return df


class TestC1CompositeFieldsPopulatedInRowContextBranch:
    """C1: the row_context branch must populate the S2-S6 composite fields from
    ohlc_df, not leave them defaulted/absent."""

    def test_all_composite_keys_present(self):
        df = _breakout_df(60)
        price = float(df["close"].iloc[-1])
        ctx = build_context(
            symbol="AAPL", asset_class="US_STOCKS",
            watch_row={"price": price, "change_pct": 2.0, "volume": 20000.0},
            ohlc_df=df, row_context=_row_context(price),
        )
        for key in (
            "bb_upper", "bb_lower", "bb_mid",
            "support", "resistance", "swing_low", "swing_high",
            "volume_expansion",
            "retest_hold_long", "retest_hold_short",
            "squeeze_active", "squeeze_breakout_up", "squeeze_breakout_down",
            "impulse_up", "impulse_down",
            "consolidation_break_up", "consolidation_break_down",
            "asian_high", "asian_low", "session_breakout",
            "volume_ratio", "volume_flow_direction", "price_acceptance_direction",
            "chart_pattern_name", "chart_pattern_acceptance",
        ):
            assert key in ctx, f"composite field {key!r} missing in row_context branch (C1)"

    def test_bb_bands_computed_not_defaulted(self):
        df = _breakout_df(60)
        price = float(df["close"].iloc[-1])
        ctx = build_context(
            symbol="AAPL", asset_class="US_STOCKS",
            watch_row={"price": price, "change_pct": 2.0, "volume": 20000.0},
            ohlc_df=df, row_context=_row_context(price),
        )
        # bb_upper must be a real bollinger value, not the price*1.01 default.
        assert ctx["bb_upper"] != pytest.approx(price * 1.01, abs=1e-6), (
            "bb_upper defaulted to price*1.01 (C1 pre-fix) instead of computed from ohlc_df"
        )
        assert ctx["bb_lower"] != pytest.approx(price * 0.99, abs=1e-6), (
            "bb_lower defaulted to price*0.99 (C1 pre-fix) instead of computed from ohlc_df"
        )

    def test_no_df_composites_absent_no_crash(self):
        """ohlc_df=None -> row_context branch must not crash; composite fields
        are simply absent (the branch cannot compute them without a df)."""
        price = 100.0
        ctx = build_context(
            symbol="AAPL", asset_class="US_STOCKS",
            watch_row={"price": price, "change_pct": 0.0, "volume": 10000.0},
            ohlc_df=None, row_context=_row_context(price),
        )
        # Must not raise; ctx still has the base fields.
        assert ctx["price"] == pytest.approx(price)
        assert "rsi" in ctx


class TestM5SupportResistanceFromMultiBar:
    """M5: support/resistance must come from multi-bar ohlc_df windows, not the
    current 1m bar's low/high (row_context.low/high)."""

    def test_resistance_from_ohlc_df_not_1m_bar(self):
        df = _breakout_df(60)
        price = float(df["close"].iloc[-1])  # 102.0
        rc = _row_context(price)
        ctx = build_context(
            symbol="AAPL", asset_class="US_STOCKS",
            watch_row={"price": price, "change_pct": 2.0, "volume": 20000.0},
            ohlc_df=df, row_context=rc,
        )
        # Pre-fix: resistance = row_context.high = price + 0.1 = 102.1.
        # Resistance = max high of the 20 bars BEFORE the last one (100.0); the
        # breakout bar's own high (102.5) cannot be the level it breaks.
        assert ctx["resistance"] == pytest.approx(100.0, abs=1e-6), (
            "resistance should come from ohlc_df multi-bar window (M5), not the 1m bar"
        )
        # support should be the recent 20-bar low (99.5), not row_context.low (101.9).
        assert ctx["support"] == pytest.approx(99.5, abs=1e-6), (
            "support should come from ohlc_df multi-bar window (M5), not the 1m bar"
        )


class TestH1SessionBreakoutAchievable:
    """H1: session_breakout must be achievable (close above the prior Asian
    range, current bar excluded) in BOTH branches."""

    def test_df_branch_session_breakout_true_on_breakout(self):
        df = _breakout_df(60)
        ctx = build_context(
            symbol="EURUSD=X", asset_class="FOREX_MAJORS",
            watch_row={"price": 102.0, "change_pct": 2.0, "volume": 20000.0},
            ohlc_df=df, row_context=None,
        )
        # Prior 48 bars (excl. current) high = 100; close 102 > 100*1.0005.
        assert ctx["asian_high"] == pytest.approx(100.0, abs=1e-6), (
            "asian_high should exclude the current bar (H1)"
        )
        assert ctx["session_breakout"] is True, (
            "session_breakout must be achievable post-H1 (was always False pre-fix)"
        )

    def test_row_context_branch_session_breakout_true_on_breakout(self):
        df = _breakout_df(60)
        price = float(df["close"].iloc[-1])
        ctx = build_context(
            symbol="EURUSD=X", asset_class="FOREX_MAJORS",
            watch_row={"price": price, "change_pct": 2.0, "volume": 20000.0},
            ohlc_df=df, row_context=_row_context(price),
        )
        assert ctx["session_breakout"] is True, (
            "row_context branch session_breakout must be achievable (H1 + C1)"
        )

    def test_no_breakout_means_no_session_breakout(self):
        """A df that never breaks above its prior range -> session_breakout False."""
        n = 60
        idx = pd.date_range("2025-01-01 09:30", periods=n, freq="1min", name="dt")
        close = pd.Series([100.0] * n)  # flat, no breakout
        df = pd.DataFrame(
            {"open": close, "high": close + 0.1, "low": close - 0.1,
             "close": close, "volume": pd.Series([10000.0] * n)},
        )
        df.index = idx
        ctx = build_context(
            symbol="EURUSD=X", asset_class="FOREX_MAJORS",
            watch_row={"price": 100.0, "change_pct": 0.0, "volume": 10000.0},
            ohlc_df=df, row_context=None,
        )
        assert ctx["session_breakout"] is False


class TestM6EmaSlopeParityBetweenBranches:
    """M6: df-branch ema_slope must match the row_context branch ema_slope for
    the same ohlc_df (both derived from ema200, not ema20)."""

    def test_df_branch_matches_row_context_branch(self):
        n = 250
        idx = pd.date_range("2025-01-01 09:30", periods=n, freq="1min", name="dt")
        close = pd.Series(100.0 + 0.05 * pd.Series(range(n)))  # smooth uptrend
        df = pd.DataFrame(
            {"open": close - 0.05, "high": close + 0.1, "low": close - 0.1,
             "close": close, "volume": pd.Series([10000.0] * n)},
        )
        df.index = idx
        price = float(df["close"].iloc[-1])
        ctx_df = build_context(
            symbol="AAPL", asset_class="US_STOCKS",
            watch_row={"price": price, "change_pct": 0.0, "volume": 10000.0},
            ohlc_df=df, row_context=None,
        )
        ctx_rc = build_context(
            symbol="AAPL", asset_class="US_STOCKS",
            watch_row={"price": price, "change_pct": 0.0, "volume": 10000.0},
            ohlc_df=df, row_context=_row_context(price),
        )
        # Pre-fix: df-branch used ema20, row_context used ema200 -> diverged.
        # Post-fix: both use ema200 -> equal.
        assert ctx_df["ema_slope"] == pytest.approx(ctx_rc["ema_slope"], rel=1e-6, abs=1e-9), (
            "df-branch ema_slope should match row_context branch (both ema200, M6)"
        )
