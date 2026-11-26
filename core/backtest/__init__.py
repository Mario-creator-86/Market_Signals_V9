"""Stable V9 backtest and validation domain package."""

from .types import (
    BacktestConfig,
    BacktestDirection,
    BacktestExecutionTiming,
    BacktestMetrics,
    BacktestResult,
    EvidenceLevel,
    Trade,
)
from .engine import BacktestEngine, NoLookAheadError, assert_no_look_ahead
from .metrics import compute_metrics, merge_with_legacy
from .splits import (
    DatasetSplit,
    add_validation_fold,
    assert_disjoint_and_ordered,
    contiguous_walk_forward_split,
    reserve_final_holdout,
)
from .walk_forward import WalkForwardResult, run_walk_forward
from .overfitting import (
    OVERFITTING_THRESHOLDS,
    OverfittingBatteryResult,
    classify_overfitting,
    deflated_sharpe_ratio,
    multiple_testing_adjustment,
    parameter_stability,
    probability_of_backtest_overfitting,
    purged_k_fold_indices,
    run_overfitting_battery,
    sensitivity_heatmap,
)
from .oos_gate_runner import OosEvaluation, OosGateRunner, default_oos_gate_runner
from .no_forced_strategy import (
    NO_VALIDATED_TICKER_SPECIFIC_STRATEGY,
    EvidenceResolution,
    assert_min_sample,
    resolve_evidence_level,
)

__all__ = [
    "BacktestConfig",
    "BacktestDirection",
    "BacktestExecutionTiming",
    "BacktestMetrics",
    "BacktestResult",
    "EvidenceLevel",
    "Trade",
    "BacktestEngine",
    "NoLookAheadError",
    "assert_no_look_ahead",
    "compute_metrics",
    "merge_with_legacy",
    "DatasetSplit",
    "add_validation_fold",
    "assert_disjoint_and_ordered",
    "contiguous_walk_forward_split",
    "reserve_final_holdout",
    "WalkForwardResult",
    "run_walk_forward",
    "OVERFITTING_THRESHOLDS",
    "OverfittingBatteryResult",
    "classify_overfitting",
    "deflated_sharpe_ratio",
    "multiple_testing_adjustment",
    "parameter_stability",
    "probability_of_backtest_overfitting",
    "purged_k_fold_indices",
    "run_overfitting_battery",
    "sensitivity_heatmap",
    "OosEvaluation",
    "OosGateRunner",
    "default_oos_gate_runner",
    "NO_VALIDATED_TICKER_SPECIFIC_STRATEGY",
    "EvidenceResolution",
    "assert_min_sample",
    "resolve_evidence_level",
]
