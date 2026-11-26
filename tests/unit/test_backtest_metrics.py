import pytest

from core.backtest import Trade
from core.backtest.metrics import compute_metrics, merge_with_legacy


def _trades():
    return [
        Trade(
            entry_bar=1,
            exit_bar=3,
            entry_time=None,
            exit_time=None,
            direction="LONG",
            entry_price=100.0,
            exit_price=103.0,
            sl=98.0,
            tp=105.0,
            size=10.0,
            gross_pnl=30.0,
            commission=2.0,
            slippage_cost=1.0,
            result="WIN",
            r_multiple=1.0,
            exit_reason="tp",
            holding_bars=2,
        ),
        Trade(
            entry_bar=5,
            exit_bar=7,
            entry_time=None,
            exit_time=None,
            direction="LONG",
            entry_price=103.0,
            exit_price=100.0,
            sl=100.0,
            tp=108.0,
            size=10.0,
            gross_pnl=-30.0,
            commission=2.0,
            slippage_cost=1.0,
            result="LOSS",
            r_multiple=-1.0,
            exit_reason="sl",
            holding_bars=2,
        ),
    ]


def test_compute_metrics_full_section_58_list():
    metrics = compute_metrics(
        _trades(),
        initial_capital=10_000.0,
        date_span_days=365,
        validation_scope="test",
        is_authoritative=True,
    )

    assert metrics.num_trades == 2
    assert metrics.win_rate == 0.5
    assert metrics.profit_factor == 1.0
    assert metrics.expectancy_r == 0.0
    assert metrics.median_trade_r == 0.0
    assert metrics.best_trade_r == 1.0
    assert metrics.worst_trade_r == -1.0
    assert metrics.fees_total == 4.0
    assert metrics.slippage_total == 2.0
    assert metrics.final_capital == 9_994.0


def test_compute_metrics_cagr_benchmark_drawdown_and_costs():
    metrics = compute_metrics(
        _trades(),
        initial_capital=10_000.0,
        equity_curve=(10_000.0, 10_500.0, 9_994.0),
        benchmark_equity_curve=(100.0, 105.0),
        date_span_days=365.25,
        validation_scope="test",
        is_authoritative=True,
    )

    assert metrics.net_return_pct == pytest.approx(-0.06)
    assert metrics.cagr == pytest.approx(-0.06)
    assert metrics.benchmark_net_return_pct == pytest.approx(5.0)
    assert metrics.max_drawdown_pct == pytest.approx((10_500.0 - 9_994.0) / 10_500.0 * 100.0)


def test_compute_metrics_exposure_and_turnover_are_ratios():
    metrics = compute_metrics(
        _trades(),
        initial_capital=10_000.0,
        validation_scope="test",
        is_authoritative=False,
    )

    assert metrics.exposure_pct == pytest.approx(4 / 7)
    assert metrics.turnover_pct == pytest.approx((10 * 100 + 10 * 103) / 10_000)


def test_compute_metrics_zero_trades_is_honest():
    metrics = compute_metrics(
        [],
        initial_capital=10_000.0,
        validation_scope="test",
        is_authoritative=False,
    )

    assert metrics.num_trades == 0
    assert metrics.win_rate == 0.0
    assert metrics.profit_factor is None
    assert metrics.benchmark_net_return_pct is None
    assert metrics.equity_curve == (10_000.0,)


def test_compute_metrics_rejects_invalid_inputs():
    with pytest.raises(ValueError, match="initial_capital"):
        compute_metrics([], initial_capital=0, validation_scope="test", is_authoritative=False)
    with pytest.raises(ValueError, match="equity_curve"):
        compute_metrics(
            [],
            initial_capital=10_000,
            equity_curve=(),
            validation_scope="test",
            is_authoritative=False,
        )


def test_merge_with_legacy_prefers_engine_metrics_and_preserves_curve():
    raw = {
        "sharpe_ratio": 1.23,
        "sortino_ratio": 1.5,
        "max_drawdown_pct": 4.0,
        "profit_factor": 1.8,
        "win_rate": 0.6,
        "total_return_pct": 12.0,
        "initial_capital": 10_000.0,
        "final_capital": 11_200.0,
        "equity_curve": [10_000.0, 11_200.0],
    }

    metrics = merge_with_legacy(
        raw,
        _trades(),
        initial_capital=10_000.0,
        validation_scope="legacy",
        is_authoritative=False,
    )

    assert metrics.sharpe == 1.23
    assert metrics.sortino == 1.5
    assert metrics.net_return_pct == 12.0
    assert metrics.num_trades == 2
    assert metrics.equity_curve == (10_000.0, 11_200.0)
