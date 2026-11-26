"""GOLD-059 four-way data separation and GOLD-060 overfitting controls."""

import pytest

from core.backtest import BacktestMetrics
from core.backtest.overfitting import (
    classify_overfitting,
    deflated_sharpe_ratio,
    probability_of_backtest_overfitting,
    purged_k_fold_indices,
)
from core.backtest.splits import (
    DatasetSplit,
    add_validation_fold,
    assert_disjoint_and_ordered,
    reserve_final_holdout,
)
from core.ml.registry import OosVerdictLabel

pytestmark = pytest.mark.golden


def _metrics(num_trades=50, profit_factor=1.5, drawdown=10.0):
    return BacktestMetrics(
        initial_capital=10_000.0,
        final_capital=11_000.0,
        net_return_pct=10.0,
        cagr=10.0,
        benchmark_net_return_pct=None,
        max_drawdown_pct=drawdown,
        sharpe=1.5,
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
        validation_scope="golden_oos",
        is_authoritative=True,
    )


def test_gold059_train_validation_oos_holdout_are_disjoint_and_ordered():
    base = DatasetSplit((0, 100), None, (100, 160), None, "walk_forward")
    with_validation = add_validation_fold(base, validation_bars=20)
    complete = reserve_final_holdout(with_validation, holdout_bars=20)

    assert complete.train_range == (0, 80)
    assert complete.validation_range == (80, 100)
    assert complete.out_of_sample_range == (100, 140)
    assert complete.final_holdout_range == (140, 160)
    assert_disjoint_and_ordered(complete)


def test_gold059_final_holdout_cannot_be_recarved_or_overlap():
    complete = DatasetSplit((0, 80), (80, 100), (100, 140), (140, 160), "holdout")
    with pytest.raises(ValueError):
        reserve_final_holdout(complete, holdout_bars=10)
    with pytest.raises(ValueError):
        DatasetSplit((0, 100), None, (90, 140), (140, 160), "overlap")


def test_gold060_purged_kfold_and_embargo_remove_test_neighbors():
    folds = purged_k_fold_indices(100, n_folds=5, purge_bars=2, embargo_bars=1)
    assert len(folds) == 5
    assert all(set(train).isdisjoint(test) for train, test in folds)


def test_gold060_pbo_dsr_and_verdicts_are_pinned():
    assert probability_of_backtest_overfitting(
        [0.9, 0.1, 0.5], [0.1, 0.9, 0.5]
    ) == 1.0
    assert deflated_sharpe_ratio(2.0, 100, 10) < deflated_sharpe_ratio(2.0, 100, 1)
    assert classify_overfitting(
        _metrics(num_trades=5), pbo=0.1, dsr=2.0, param_stable=True
    ) == OosVerdictLabel.INSUFFICIENT_EVIDENCE.value
    assert classify_overfitting(
        _metrics(), pbo=0.7, dsr=2.0, param_stable=True
    ) == OosVerdictLabel.FRAGILE.value
    assert classify_overfitting(
        _metrics(), pbo=0.1, dsr=2.0, param_stable=True
    ) == OosVerdictLabel.ROBUST.value
    assert classify_overfitting(
        _metrics(), pbo=None, dsr=2.0, param_stable=True
    ) == OosVerdictLabel.MODERATE.value
