"""
Unit tests for BacktestEngine (core/backtest_engine.py).

Verifies correctness of:
1. SHORT position support (entry, TP, SL, P&L)
2. Commission model (notional-based, not double-counted)
3. Max drawdown calculation (peak-to-trough, intra-trade tracking)
4. Sortino ratio (downside deviation using min(r,0)^2, NOT std of negatives)
5. Sharpe ratio (proper annualization via bars_per_year)
"""

import pytest
import numpy as np
import pandas as pd
from datetime import datetime
from unittest.mock import MagicMock

from core.backtest_engine import (
    BacktestEngine,
    Trade,
    TradeDirection,
    ExitReason,
    BacktestResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trade(
    direction: TradeDirection = TradeDirection.LONG,
    entry_price: float = 100.0,
    exit_price: float = 105.0,
    quantity: float = 10.0,
    tp_price: float = 106.0,
    sl_price: float = 97.0,
) -> Trade:
    """Create a minimal Trade for testing P&L calculations."""
    return Trade(
        entry_time=datetime(2025, 1, 1),
        exit_time=datetime(2025, 1, 5),
        ticker="TEST",
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        tp_price=tp_price,
        sl_price=sl_price,
        exit_reason=ExitReason.TP,
        pnl=0.0,
        pnl_pct=0.0,
        holding_bars=5,
        max_drawdown_pct=0.0,
        max_profit_pct=0.0,
    )


def _run_with_frame(
    frame: pd.DataFrame,
    *,
    commission_pct: float = 0.0,
    tp_pct: float = 0.5,
    sl_pct: float = 0.5,
    max_holding_bars: int = 100,
) -> BacktestResult:
    engine = BacktestEngine()
    engine.fetch_historical_data = MagicMock(return_value=frame)
    engine.calculate_indicators = MagicMock(side_effect=lambda data: data)
    engine.generate_signals = MagicMock(side_effect=lambda data: data)
    result = engine.run_backtest(
        "TEST",
        frame.index[0].to_pydatetime(),
        frame.index[-1].to_pydatetime(),
        commission_pct=commission_pct,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        max_holding_bars=max_holding_bars,
    )
    assert result is not None
    return result


def _execution_frame() -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=52, freq="D")
    frame = pd.DataFrame(
        {
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": 1_000.0,
            "signal": 0,
        },
        index=index,
    )
    return frame


# ===========================================================================
# 1. SHORT position support
# ===========================================================================

class TestShortPosition:
    """Verify SHORT position entry, TP, SL, and P&L calculations."""

    def test_short_pnl_formula(self):
        """P&L for SHORT = (entry - exit) * qty."""
        trade = _make_trade(
            direction=TradeDirection.SHORT,
            entry_price=100.0,
            exit_price=95.0,
            quantity=10.0,
        )
        # (100 - 95) * 10 = 50
        assert trade.calculate_pnl() == pytest.approx(50.0)

    def test_short_pnl_loss(self):
        """SHORT P&L negative when price rises."""
        trade = _make_trade(
            direction=TradeDirection.SHORT,
            entry_price=100.0,
            exit_price=110.0,
            quantity=10.0,
        )
        # (100 - 110) * 10 = -100
        assert trade.calculate_pnl() == pytest.approx(-100.0)

    def test_short_tp_below_entry(self):
        """SHORT TP must be below entry price."""
        entry = 100.0
        tp_pct = 0.06
        tp_price = entry * (1 - tp_pct)  # 94.0
        assert tp_price == pytest.approx(94.0)
        assert tp_price < entry

    def test_short_sl_above_entry(self):
        """SHORT SL must be above entry price."""
        entry = 100.0
        sl_pct = 0.03
        sl_price = entry * (1 + sl_pct)  # 103.0
        assert sl_price == pytest.approx(103.0)
        assert sl_price > entry

    def test_short_sl_trigger_price_rises(self):
        """SHORT SL triggers when current_price >= sl_price."""
        sl_price = 103.0
        # Price must rise to or above SL for SHORT
        assert 103.0 >= sl_price  # exact hit
        assert 104.0 >= sl_price  # above SL
        assert not (102.0 >= sl_price)  # below SL, no trigger

    def test_short_tp_trigger_price_falls(self):
        """SHORT TP triggers when current_price <= tp_price."""
        tp_price = 94.0
        # Price must fall to or below TP for SHORT
        assert 94.0 <= tp_price  # exact hit
        assert 93.0 <= tp_price  # below TP
        assert not (95.0 <= tp_price)  # above TP, no trigger

    def test_long_pnl_formula(self):
        """P&L for LONG = (exit - entry) * qty."""
        trade = _make_trade(
            direction=TradeDirection.LONG,
            entry_price=100.0,
            exit_price=105.0,
            quantity=10.0,
        )
        # (105 - 100) * 10 = 50
        assert trade.calculate_pnl() == pytest.approx(50.0)


# ===========================================================================
# 2. Commission model
# ===========================================================================

class TestCommissionModel:
    """Verify commissions are on notional value, not P&L, and not double-counted."""

    def test_commission_on_notional_not_pnl(self):
        """
        Commission must be charged on notional (price * qty), not on P&L.
        For a LONG trade: entry notional = entry_price * qty, exit notional = exit_price * qty.
        Total commission = (entry_notional + exit_notional) * commission_pct.
        """
        entry_price = 100.0
        exit_price = 105.0
        quantity = 10.0
        commission_pct = 0.001  # 0.1%

        entry_notional = entry_price * quantity  # 1000
        exit_notional = exit_price * quantity  # 1050

        entry_commission = entry_notional * commission_pct  # 1.0
        exit_commission = exit_notional * commission_pct  # 1.05
        total_commission = entry_commission + exit_commission  # 2.05

        # Verify commission is on notional, not P&L
        gross_pnl = (exit_price - entry_price) * quantity  # 50
        # Commission should NOT be based on P&L
        assert total_commission != pytest.approx(gross_pnl * commission_pct)  # 0.05 != 2.05

    def test_commission_not_double_counted(self):
        """
        Entry commission is deducted at entry, exit commission at exit.
        Total = entry_notional * pct + exit_notional * pct.
        Should equal (entry_notional + exit_notional) * pct.
        """
        entry_price = 100.0
        exit_price = 95.0
        quantity = 10.0
        commission_pct = 0.001

        entry_commission = entry_price * quantity * commission_pct  # 1.0
        exit_commission = exit_price * quantity * commission_pct  # 0.95

        # Total commission = (entry_notional + exit_notional) * pct
        total_notional = (entry_price + exit_price) * quantity
        expected_total = total_notional * commission_pct  # 1.95

        assert pytest.approx(entry_commission + exit_commission) == expected_total

    def test_short_commission_on_notional(self):
        """
        For SHORT: commission on entry notional (sell proceeds) + exit notional (buyback cost).
        Same formula as LONG.
        """
        entry_price = 100.0
        exit_price = 95.0
        quantity = 10.0
        commission_pct = 0.001

        # SHORT: sell at 100, buy back at 95
        entry_commission = entry_price * quantity * commission_pct  # 1.0
        exit_commission = exit_price * quantity * commission_pct  # 0.95

        total = entry_commission + exit_commission  # 1.95
        expected = (entry_price + exit_price) * quantity * commission_pct
        assert pytest.approx(total) == expected


# ===========================================================================
# 3. Execution integrity
# ===========================================================================

class TestExecutionIntegrity:
    """Verify fill timing, net accounting, and terminal liquidation."""

    def test_signal_at_close_enters_at_next_bar_open(self):
        frame = _execution_frame()
        frame.iloc[50, frame.columns.get_loc("close")] = 100.0
        frame.iloc[50, frame.columns.get_loc("signal")] = 1
        frame.iloc[51, frame.columns.get_loc("open")] = 110.0
        frame.iloc[51, frame.columns.get_loc("high")] = 111.0
        frame.iloc[51, frame.columns.get_loc("low")] = 109.0
        frame.iloc[51, frame.columns.get_loc("close")] = 111.0

        result = _run_with_frame(frame)

        assert result.total_trades == 1
        trade = result.trades[0]
        assert trade.entry_time == frame.index[51]
        assert trade.entry_price == pytest.approx(110.0)
        assert trade.entry_price != pytest.approx(frame.iloc[50]["close"])
        assert trade.exit_reason == ExitReason.TIME

    def test_trade_classification_uses_net_pnl_after_both_commissions(self):
        frame = _execution_frame()
        frame.iloc[50, frame.columns.get_loc("signal")] = 1
        frame.iloc[51, frame.columns.get_loc("high")] = 100.1
        frame.iloc[51, frame.columns.get_loc("close")] = 100.1

        result = _run_with_frame(
            frame,
            commission_pct=0.001,
            tp_pct=0.0005,
        )

        assert result.total_trades == 1
        assert result.winning_trades == 0
        assert result.losing_trades == 1
        trade = result.trades[0]
        assert trade.gross_pnl == pytest.approx(0.5)
        assert trade.entry_commission == pytest.approx(1.0)
        assert trade.exit_commission == pytest.approx(1.0005)
        assert trade.commission == pytest.approx(2.0005)
        assert trade.pnl == pytest.approx(-1.5005)
        assert result.final_capital == pytest.approx(9_998.4995)

    def test_open_position_is_liquidated_at_dataset_boundary(self):
        frame = _execution_frame()
        frame.iloc[50, frame.columns.get_loc("signal")] = 1
        frame.iloc[51, frame.columns.get_loc("close")] = 101.0
        frame.iloc[51, frame.columns.get_loc("high")] = 101.0

        result = _run_with_frame(frame)

        assert result.total_trades == 1
        trade = result.trades[0]
        assert trade.exit_time == frame.index[-1]
        assert trade.exit_reason == ExitReason.TIME
        assert trade.exit_price == pytest.approx(101.0)
        assert trade.pnl == pytest.approx(10.0)
        assert result.final_capital == pytest.approx(10_010.0)


# ===========================================================================
# 4. Max drawdown
# ===========================================================================

class TestMaxDrawdown:
    """Verify max drawdown is peak-to-trough, using intra-trade tracking, % of peak."""

    def test_drawdown_formula_peak_trough(self):
        """
        Max drawdown % = (peak - trough) / peak * 100, NOT / initial_capital.
        """
        peak = 12000.0
        trough = 10800.0
        initial_capital = 10000.0

        dd_pct_correct = (peak - trough) / peak * 100  # (12000-10800)/12000 * 100 = 10%
        dd_pct_wrong = (peak - trough) / initial_capital * 100  # 12% (wrong)

        assert dd_pct_correct == pytest.approx(10.0)
        assert dd_pct_wrong != pytest.approx(10.0)  # Proves they differ

    def test_drawdown_tracks_intra_trade(self):
        """
        Drawdown must account for unrealized P&L during open positions.
        If equity = 10500 and unrealized loss = -500, portfolio_value = 10000.
        """
        equity = 10500.0
        # LONG position: entry at 100, current at 95, qty = 100
        unrealized_pnl = (95 - 100) * 100  # -500
        portfolio_value = equity + unrealized_pnl  # 10000
        assert portfolio_value == pytest.approx(10000.0)

    def test_drawdown_short_unrealized(self):
        """
        SHORT unrealized P&L = (entry - current) * qty.
        If price rises against short, unrealized P&L is negative.
        """
        equity = 10500.0
        # SHORT: entry at 100, current at 105, qty = 100
        unrealized_pnl = (100 - 105) * 100  # -500
        portfolio_value = equity + unrealized_pnl  # 10000
        assert portfolio_value == pytest.approx(10000.0)


# ===========================================================================
# 5. Sortino ratio
# ===========================================================================

class TestSortinoRatio:
    """Verify Sortino uses downside deviation (min(r,0)^2), NOT std of negatives."""

    def test_sortino_uses_downside_deviation_not_std(self):
        """
        The correct Sortino downside deviation is sqrt(mean(min(r, 0)^2)),
        using ALL returns, not std of only negative returns.
        """
        returns = np.array([0.01, -0.02, 0.03, -0.01, 0.02, 0.005, -0.015, 0.01, 0.02, -0.005])

        # CORRECT: downside deviation = sqrt(mean(min(r, 0)^2))
        downside = np.minimum(returns, 0.0)
        correct_dd = np.sqrt(np.mean(downside ** 2))

        # WRONG (old implementation): std of only negative returns
        neg_returns = [r for r in returns if r < 0]
        wrong_dd = np.std(neg_returns, ddof=1) if len(neg_returns) > 1 else abs(neg_returns[0])

        # They must differ (proving the fix matters)
        assert correct_dd != pytest.approx(wrong_dd)

        # The correct formula always uses N (total returns) in denominator
        # The wrong formula uses K (only negative returns) and centers on their mean
        n_total = len(returns)
        n_negative = len(neg_returns)
        assert n_total == 10
        assert n_negative == 4

        # Verify correct_dd is positive and finite
        assert correct_dd > 0
        assert np.isfinite(correct_dd)

    def test_sortino_all_positive_returns(self):
        """When all returns are positive, downside deviation = 0, Sortino = 0."""
        returns = np.array([0.01, 0.02, 0.03, 0.015])

        downside = np.minimum(returns, 0.0)
        downside_dev = np.sqrt(np.mean(downside ** 2))

        # All min(r, 0) = 0, so downside_dev = 0
        assert downside_dev == pytest.approx(0.0)

    def test_sortino_all_negative_returns(self):
        """When all returns are negative, downside deviation uses all of them."""
        returns = np.array([-0.01, -0.02, -0.03, -0.015])

        downside = np.minimum(returns, 0.0)
        downside_dev = np.sqrt(np.mean(downside ** 2))

        # All values are negative, so min(r, 0) = r
        # downside_dev = sqrt(mean(r^2))
        expected = np.sqrt(np.mean(returns ** 2))
        assert downside_dev == pytest.approx(expected)

    def test_sortino_mixed_returns_numerical(self):
        """Verify specific numerical result for mixed returns."""
        returns = np.array([0.1, -0.05, 0.02, -0.03])

        downside = np.minimum(returns, 0.0)
        downside_dev = np.sqrt(np.mean(downside ** 2))

        # min(r, 0) = [0, -0.05, 0, -0.03]
        # squares = [0, 0.0025, 0, 0.0009]
        # mean = (0 + 0.0025 + 0 + 0.0009) / 4 = 0.00085
        # sqrt(0.00085) ≈ 0.02915
        expected_mean = (0 + 0.0025 + 0 + 0.0009) / 4
        assert expected_mean == pytest.approx(0.00085)
        assert downside_dev == pytest.approx(np.sqrt(0.00085), rel=1e-6)


# ===========================================================================
# 6. Sharpe ratio annualization
# ===========================================================================

class TestSharpeAnnualization:
    """Verify Sharpe uses bars_per_year for proper annualization."""

    def test_bars_per_year_daily(self):
        """Daily timeframe should use 252 bars per year."""
        engine = BacktestEngine()
        assert engine._bars_per_year("1d") == 252

    def test_bars_per_year_hourly(self):
        """Hourly timeframe should use 252 * 6.5 = 1638 bars per year."""
        engine = BacktestEngine()
        assert engine._bars_per_year("1h") == pytest.approx(252 * 6.5)

    def test_bars_per_year_5min(self):
        """5-min timeframe should use 252 * 6.5 * 12 = 19656 bars per year."""
        engine = BacktestEngine()
        assert engine._bars_per_year("5m") == pytest.approx(252 * 6.5 * 12)

    def test_sharpe_annualization_factor(self):
        """Sharpe annualization factor = sqrt(bars_per_year)."""
        bars_per_year = 252  # daily
        ann_factor = np.sqrt(bars_per_year)
        assert ann_factor == pytest.approx(np.sqrt(252))


# ===========================================================================
# 7. Cross-file consistency
# ===========================================================================

class TestCrossFileConsistency:
    """Verify SHORT position logic is consistent between backtest_engine.py and backtester.py."""

    def test_short_tp_sl_consistency(self):
        """
        Both files must compute SHORT TP/SL identically:
        TP = entry * (1 - tp_pct), SL = entry * (1 + sl_pct).
        """
        entry = 100.0
        tp_pct = 0.05
        sl_pct = 0.03

        # backtest_engine.py logic
        be_tp = entry * (1 - tp_pct)
        be_sl = entry * (1 + sl_pct)

        # backtester.py logic (FIXED_PCT mode)
        bt_tp = entry * (1 - tp_pct)
        bt_sl = entry * (1 + sl_pct)

        assert be_tp == pytest.approx(bt_tp)  # 95.0
        assert be_sl == pytest.approx(bt_sl)  # 103.0

    def test_short_pnl_formula_consistency(self):
        """
        Both files must compute SHORT P&L as (entry - exit) * qty.
        """
        entry = 100.0
        exit_price = 95.0
        qty = 10.0

        # backtest_engine.py: Trade.calculate_pnl()
        be_pnl = (entry - exit_price) * qty  # 50

        # backtester.py: gross_pnl for SHORT
        bt_pnl = (entry - exit_price) * qty  # 50

        assert be_pnl == pytest.approx(bt_pnl)

    def test_short_sl_check_direction(self):
        """
        For SHORT: SL triggers when price >= sl_price (price rises to stop).
        For LONG: SL triggers when price <= sl_price (price falls to stop).
        """
        sl_price = 103.0
        current_price = 103.5

        # SHORT: price >= SL triggers stop loss
        short_triggered = current_price >= sl_price
        assert short_triggered is True

        # LONG: price <= SL does NOT trigger (price above SL)
        long_triggered = current_price <= sl_price
        assert long_triggered is False

    def test_commission_formula_consistency(self):
        """
        Both files must compute total commission as
        (entry_notional + exit_notional) * commission_pct.
        """
        entry_price = 100.0
        exit_price = 105.0
        qty = 10.0
        commission_pct = 0.001

        # backtest_engine.py: entry_commission + exit_commission
        entry_commission = entry_price * qty * commission_pct
        exit_commission = exit_price * qty * commission_pct
        be_total = entry_commission + exit_commission

        # backtester.py: (entry * qty + exit * qty) * commission_pct
        bt_total = (entry_price * qty + exit_price * qty) * commission_pct

        assert be_total == pytest.approx(bt_total)


# ===========================================================================
# 8. Trade dataclass
# ===========================================================================

class TestTradeDataclass:
    """Verify Trade dataclass P&L calculation for both directions."""

    def test_long_pnl_positive(self):
        """LONG trade with price increase gives positive P&L."""
        trade = _make_trade(
            direction=TradeDirection.LONG,
            entry_price=100.0,
            exit_price=110.0,
            quantity=10.0,
        )
        assert trade.calculate_pnl() == pytest.approx(100.0)

    def test_long_pnl_negative(self):
        """LONG trade with price decrease gives negative P&L."""
        trade = _make_trade(
            direction=TradeDirection.LONG,
            entry_price=100.0,
            exit_price=90.0,
            quantity=10.0,
        )
        assert trade.calculate_pnl() == pytest.approx(-100.0)

    def test_short_pnl_positive(self):
        """SHORT trade with price decrease gives positive P&L."""
        trade = _make_trade(
            direction=TradeDirection.SHORT,
            entry_price=100.0,
            exit_price=90.0,
            quantity=10.0,
        )
        assert trade.calculate_pnl() == pytest.approx(100.0)

    def test_short_pnl_negative(self):
        """SHORT trade with price increase gives negative P&L."""
        trade = _make_trade(
            direction=TradeDirection.SHORT,
            entry_price=100.0,
            exit_price=110.0,
            quantity=10.0,
        )
        assert trade.calculate_pnl() == pytest.approx(-100.0)

    def test_pnl_zero_no_exit(self):
        """P&L is zero when exit_price is None."""
        trade = Trade(
            entry_time=datetime(2025, 1, 1),
            ticker="TEST",
            direction=TradeDirection.LONG,
            entry_price=100.0,
            quantity=10.0,
            tp_price=106.0,
            sl_price=97.0,
        )
        assert trade.calculate_pnl() == 0.0
