import pytest

from core.backtest import BacktestMetrics
from core.backtest.overfitting import (
    OVERFITTING_THRESHOLDS,
    classify_overfitting,
    deflated_sharpe_ratio,
    multiple_testing_adjustment,
    parameter_stability,
    probability_of_backtest_overfitting,
    purged_k_fold_indices,
    run_overfitting_battery,
    sensitivity_heatmap,
)
from core.ml.registry import OosVerdictLabel


def _metrics(num_trades=50, profit_factor=1.5, drawdown=10.0, sharpe=1.5):
    return BacktestMetrics(
        initial_capital=10_000.0,
        final_capital=11_000.0,
        net_return_pct=10.0,
        cagr=10.0,
        benchmark_net_return_pct=None,
        max_drawdown_pct=drawdown,
        sharpe=sharpe,
        sortino=1.8,
        calmar=1.0,
        win_rate=0.6,
        profit_factor=profit_factor,
        expectancy_r=0.3,
        num_trades=num_trades,
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
        validation_scope="oos",
        is_authoritative=True,
    )


def test_purged_k_fold_removes_purge_and_embargo_from_train():
    folds = purged_k_fold_indices(20, n_folds=4, purge_bars=2, embargo_bars=1)
    assert len(folds) == 4
    train, test = folds[1]
    assert test == (5, 6, 7, 8, 9)
    assert set(train).isdisjoint(test)
    assert 3 not in train and 4 not in train
    assert 10 not in train and 11 not in train and 12 not in train


def test_purged_k_fold_validates_inputs():
    with pytest.raises(ValueError):
        purged_k_fold_indices(5, n_folds=6)
    with pytest.raises(ValueError):
        purged_k_fold_indices(20, n_folds=4, purge_bars=-1)


def test_parameter_stability_and_sensitivity_are_scale_relative():
    assert parameter_stability([1.45, 1.5, 1.55]) is True
    assert parameter_stability([0.2, 1.5, 3.0]) is False
    assert sensitivity_heatmap({("tp", 1): 1.45, ("tp", 2): 1.5})["is_robust"] is True
    assert sensitivity_heatmap({("tp", 1): 0.2, ("tp", 2): 2.0})["is_robust"] is False


def test_pbo_detects_rank_reversal_and_agreement():
    in_sample = [0.9, 0.1, 0.5, 0.4, 0.3]
    reversed_oos = [0.1, 0.9, 0.5, 0.4, 0.3]
    agreeing_oos = [0.9, 0.1, 0.5, 0.4, 0.3]
    assert probability_of_backtest_overfitting(in_sample, reversed_oos) == 1.0
    assert probability_of_backtest_overfitting(in_sample, agreeing_oos) == 0.0


def test_pbo_accepts_multiple_evaluation_splits():
    in_sample = [[0.9, 0.1, 0.5], [0.2, 0.8, 0.5]]
    oos = [[0.1, 0.9, 0.5], [0.2, 0.8, 0.5]]
    assert probability_of_backtest_overfitting(in_sample, oos) == 0.5


def test_deflated_sharpe_decreases_as_trials_increase():
    one_trial = deflated_sharpe_ratio(2.0, n_samples=100, n_trials=1)
    ten_trials = deflated_sharpe_ratio(2.0, n_samples=100, n_trials=10)
    assert ten_trials < one_trial


def test_holm_adjustment_is_monotonic_in_rank_order():
    assert multiple_testing_adjustment([0.01, 0.04, 0.03], method="holm") == pytest.approx(
        [0.03, 0.06, 0.06]
    )
    with pytest.raises(ValueError):
        multiple_testing_adjustment([1.1])


@pytest.mark.parametrize(
    ("metrics", "kwargs", "expected"),
    [
        (_metrics(num_trades=5), {"pbo": 0.1, "dsr": 2.0, "param_stable": True}, OosVerdictLabel.INSUFFICIENT_EVIDENCE.value),
        (_metrics(profit_factor=None), {"pbo": 0.1, "dsr": 2.0, "param_stable": True}, OosVerdictLabel.INSUFFICIENT_EVIDENCE.value),
        (_metrics(), {"pbo": 0.7, "dsr": 2.0, "param_stable": True}, OosVerdictLabel.FRAGILE.value),
        (_metrics(), {"pbo": 0.1, "dsr": -0.1, "param_stable": True}, OosVerdictLabel.FRAGILE.value),
        (_metrics(), {"pbo": None, "dsr": 2.0, "param_stable": True}, OosVerdictLabel.MODERATE.value),
        (_metrics(), {"pbo": 0.1, "dsr": 2.0, "param_stable": True}, OosVerdictLabel.ROBUST.value),
    ],
)
def test_classify_overfitting(metrics, kwargs, expected):
    assert classify_overfitting(metrics, **kwargs) == expected


def test_overfitting_battery_does_not_invent_missing_is_pbo():
    result = run_overfitting_battery(
        _metrics(),
        fold_metrics=[1.4, 1.5, 1.6],
        n_trials=3,
    )
    assert result.pbo is None
    assert result.verdict == OosVerdictLabel.MODERATE.value


def test_overfitting_battery_computes_pbo_when_is_and_oos_exist():
    result = run_overfitting_battery(
        _metrics(),
        fold_metrics=[0.1, 0.9, 0.5],
        in_sample_fold_metrics=[0.9, 0.1, 0.5],
        param_grid_metrics={"a": 1.45, "b": 1.5},
        p_values=[0.01, 0.04],
        n_trials=3,
    )
    assert result.pbo == 1.0
    assert result.verdict == OosVerdictLabel.FRAGILE.value
    assert result.adjusted_p_values == pytest.approx((0.02, 0.04))
    assert OVERFITTING_THRESHOLDS["min_oos_trades"] == 24
