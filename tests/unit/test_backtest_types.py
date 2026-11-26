from dataclasses import FrozenInstanceError, fields, replace

import pytest

from core.backtest import (
    BacktestConfig,
    BacktestDirection,
    BacktestExecutionTiming,
    BacktestMetrics,
    EvidenceLevel,
)


def _metrics() -> BacktestMetrics:
    return BacktestMetrics(
        initial_capital=10_000.0,
        final_capital=11_000.0,
        net_return_pct=10.0,
        cagr=10.0,
        benchmark_net_return_pct=None,
        max_drawdown_pct=5.0,
        sharpe=1.2,
        sortino=1.5,
        calmar=2.0,
        win_rate=0.6,
        profit_factor=1.8,
        expectancy_r=0.3,
        num_trades=24,
        avg_trade_r=0.3,
        median_trade_r=0.2,
        best_trade_r=1.5,
        worst_trade_r=-0.5,
        avg_holding_bars=12.0,
        exposure_pct=0.4,
        turnover_pct=2.0,
        fees_total=50.0,
        slippage_total=30.0,
        equity_curve=(10_000.0, 11_000.0),
        validation_scope="test",
        is_authoritative=True,
    )


def test_backtest_config_no_look_ahead_pinned_true():
    config = BacktestConfig(
        ticker="AAPL",
        asset_class="US_STOCKS",
        initial_capital=10_000.0,
        position_size_pct=0.1,
        tp_pct=0.06,
        sl_pct=0.03,
        max_holding_bars=20,
    )

    assert config.no_look_ahead is True
    assert config.execution_timing is BacktestExecutionTiming.NEXT_BAR_OPEN


def test_backtest_config_no_look_ahead_false_raises():
    with pytest.raises(ValueError, match="no-look-ahead"):
        BacktestConfig(
            ticker="AAPL",
            asset_class="US_STOCKS",
            initial_capital=10_000.0,
            position_size_pct=0.1,
            tp_pct=0.06,
            sl_pct=0.03,
            max_holding_bars=20,
            no_look_ahead=False,
        )


def test_backtest_config_validates_financial_inputs():
    with pytest.raises(ValueError, match="initial_capital"):
        BacktestConfig(
            ticker="AAPL",
            asset_class="US_STOCKS",
            initial_capital=0.0,
            position_size_pct=0.1,
            tp_pct=0.06,
            sl_pct=0.03,
            max_holding_bars=20,
        )
    with pytest.raises(ValueError, match="direction"):
        BacktestConfig(
            ticker="AAPL",
            asset_class="US_STOCKS",
            initial_capital=10_000.0,
            position_size_pct=0.1,
            tp_pct=0.06,
            sl_pct=0.03,
            max_holding_bars=20,
            direction="SIDEWAYS",
        )
    with pytest.raises(ValueError, match="finite"):
        BacktestConfig(
            ticker="AAPL",
            asset_class="US_STOCKS",
            initial_capital=float("nan"),
            position_size_pct=0.1,
            tp_pct=0.06,
            sl_pct=0.03,
            max_holding_bars=20,
        )


def test_backtest_metrics_has_complete_section_58_contract():
    metric_fields = {item.name for item in fields(BacktestMetrics)}
    expected = {
        "initial_capital",
        "final_capital",
        "net_return_pct",
        "cagr",
        "benchmark_net_return_pct",
        "max_drawdown_pct",
        "sharpe",
        "sortino",
        "calmar",
        "win_rate",
        "profit_factor",
        "expectancy_r",
        "num_trades",
        "avg_trade_r",
        "median_trade_r",
        "best_trade_r",
        "worst_trade_r",
        "avg_holding_bars",
        "exposure_pct",
        "turnover_pct",
        "fees_total",
        "slippage_total",
        "equity_curve",
        "validation_scope",
        "is_authoritative",
        "is_not_probability",
    }

    assert metric_fields == expected
    assert _metrics().is_not_probability is True


def test_backtest_types_are_frozen():
    with pytest.raises(FrozenInstanceError):
        _metrics().win_rate = 0.99


def test_backtest_metrics_reject_nan_and_invalid_ratios():
    with pytest.raises(ValueError, match="finite"):
        replace(_metrics(), sharpe=float("nan"))
    with pytest.raises(ValueError, match="valid range"):
        replace(_metrics(), win_rate=1.1)
    with pytest.raises(ValueError, match="equity_curve"):
        replace(_metrics(), equity_curve=())


def test_evidence_level_order_is_explicit():
    assert EvidenceLevel.ordered() == (
        EvidenceLevel.TICKER,
        EvidenceLevel.INDUSTRY,
        EvidenceLevel.SECTOR,
        EvidenceLevel.UNIVERSE,
    )
    assert BacktestDirection.LONG.value == "LONG"
