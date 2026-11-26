"""Deterministic overfitting diagnostics and OOS verdict classification."""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional

from core.ml.registry import OosVerdictLabel

from .types import BacktestMetrics

OVERFITTING_THRESHOLDS = {
    "min_oos_trades": 24,
    "pbo_max_robust": 0.25,
    "pbo_max_moderate": 0.50,
    "dsr_min_robust": 1.0,
    "param_relative_std_max": 0.25,
    "pf_min_robust": 1.20,
    "pf_min_fragile_floor": 1.05,
    "dd_max_robust": 20.0,
    "dd_max_fragile": 30.0,
}


def purged_k_fold_indices(
    n_bars: int,
    n_folds: int = 5,
    *,
    purge_bars: int = 0,
    embargo_bars: int = 0,
) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    """Return time-ordered folds with boundary purge and post-test embargo."""

    if not all(isinstance(value, int) for value in (n_bars, n_folds, purge_bars, embargo_bars)):
        raise TypeError("fold inputs must be integers")
    if n_folds < 2 or n_bars < n_folds:
        raise ValueError("n_bars must support at least two non-empty folds")
    if purge_bars < 0 or embargo_bars < 0:
        raise ValueError("purge_bars and embargo_bars must be non-negative")

    base_size, remainder = divmod(n_bars, n_folds)
    folds: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    test_start = 0
    for fold_index in range(n_folds):
        test_size = base_size + (1 if fold_index < remainder else 0)
        test_end = test_start + test_size
        test = tuple(range(test_start, test_end))
        excluded_start = max(0, test_start - purge_bars)
        excluded_end = min(n_bars, test_end + purge_bars + embargo_bars)
        train = tuple(
            index
            for index in range(n_bars)
            if not excluded_start <= index < excluded_end
        )
        folds.append((train, test))
        test_start = test_end
    return folds


def _finite_series(values: Sequence[float], *, name: str) -> tuple[float, ...]:
    series = tuple(float(value) for value in values)
    if any(not math.isfinite(value) for value in series):
        raise ValueError(f"{name} must contain only finite values")
    return series


def parameter_stability(
    metric_series: Sequence[float],
    *,
    max_relative_std: float = OVERFITTING_THRESHOLDS["param_relative_std_max"],
) -> bool:
    """Classify a fold/parameter metric as stable by relative dispersion."""

    series = _finite_series(metric_series, name="metric_series")
    if len(series) < 2 or max_relative_std < 0:
        return False
    mean = statistics.fmean(series)
    if math.isclose(mean, 0.0, abs_tol=1e-12):
        return False
    return statistics.pstdev(series) / abs(mean) <= max_relative_std


def sensitivity_heatmap(
    metrics_at_params: Mapping[Any, float],
    *,
    max_relative_spread: float = 0.25,
) -> dict[str, Any]:
    """Summarize local parameter sensitivity without selecting a best trial."""

    if max_relative_spread < 0:
        raise ValueError("max_relative_spread must be non-negative")
    values = _finite_series(tuple(metrics_at_params.values()), name="metrics_at_params")
    if not values:
        return {"spread": None, "relative_spread": None, "is_robust": False, "sample_size": 0}
    spread = max(values) - min(values)
    center = abs(statistics.fmean(values))
    relative = spread / center if center > 1e-12 else None
    return {
        "spread": spread,
        "relative_spread": relative,
        "is_robust": relative is not None and relative <= max_relative_spread,
        "sample_size": len(values),
    }


def _as_matrix(values: Sequence[Any], *, name: str) -> tuple[tuple[float, ...], ...]:
    if not values:
        raise ValueError(f"{name} must not be empty")
    first = values[0]
    if isinstance(first, Sequence) and not isinstance(first, (str, bytes)):
        rows = tuple(_finite_series(row, name=name) for row in values)
    else:
        rows = (_finite_series(values, name=name),)
    if any(len(row) < 2 for row in rows):
        raise ValueError(f"{name} needs at least two candidate strategies per split")
    return rows


def probability_of_backtest_overfitting(
    in_sample_performance: Sequence[Any],
    out_of_sample_performance: Sequence[Any],
) -> float:
    """Estimate PBO as the fraction of splits with an IS-winner below OOS median.

    Each row represents one independent evaluation split and each column one
    strategy/parameter candidate.  A one-dimensional input is one observed
    split and therefore produces the honest binary estimate 0 or 1.
    """

    in_rows = _as_matrix(in_sample_performance, name="in_sample_performance")
    out_rows = _as_matrix(out_of_sample_performance, name="out_of_sample_performance")
    if len(in_rows) != len(out_rows) or any(
        len(in_row) != len(out_row) for in_row, out_row in zip(in_rows, out_rows)
    ):
        raise ValueError("IS and OOS performance matrices must have the same shape")
    overfit = 0
    for in_row, out_row in zip(in_rows, out_rows):
        in_sample_winner = max(range(len(in_row)), key=in_row.__getitem__)
        if out_row[in_sample_winner] < statistics.median(out_row):
            overfit += 1
    return overfit / len(in_rows)


def deflated_sharpe_ratio(
    sharpe: float,
    n_samples: int,
    n_trials: int = 1,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Return the deflated Sharpe z-statistic after multiple-testing adjustment."""

    values = (sharpe, skew, kurtosis)
    if any(not math.isfinite(float(value)) for value in values):
        raise ValueError("Sharpe, skew and kurtosis must be finite")
    if n_samples < 2 or n_trials < 1:
        raise ValueError("n_samples must be >= 2 and n_trials must be >= 1")

    expected_max = 0.0
    if n_trials > 1:
        normal = statistics.NormalDist()
        gamma = 0.5772156649015329
        first_probability = 1.0 - 1.0 / n_trials
        second_probability = 1.0 - 1.0 / (n_trials * math.e)
        expected_max_standard = (
            (1.0 - gamma) * normal.inv_cdf(first_probability)
            + gamma * normal.inv_cdf(second_probability)
        )
        expected_max = expected_max_standard / math.sqrt(n_samples - 1)

    variance_term = 1.0 - skew * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe**2
    if variance_term <= 0:
        raise ValueError("skew/kurtosis imply a non-positive Sharpe variance")
    standard_error = math.sqrt(variance_term / (n_samples - 1))
    return (sharpe - expected_max) / standard_error


def multiple_testing_adjustment(
    p_values: Sequence[float],
    *,
    method: str = "holm",
) -> list[float]:
    """Adjust p-values with Holm step-down or Bonferroni correction."""

    values = _finite_series(p_values, name="p_values")
    if any(value < 0 or value > 1 for value in values):
        raise ValueError("p_values must be within [0, 1]")
    normalized_method = method.strip().lower()
    if normalized_method not in {"holm", "bonferroni"}:
        raise ValueError("method must be holm or bonferroni")
    count = len(values)
    if normalized_method == "bonferroni":
        return [min(1.0, value * count) for value in values]

    ordered = sorted(enumerate(values), key=lambda item: item[1])
    adjusted = [0.0] * count
    running_max = 0.0
    for rank, (original_index, value) in enumerate(ordered):
        running_max = max(running_max, min(1.0, value * (count - rank)))
        adjusted[original_index] = running_max
    return adjusted


def classify_overfitting(
    oos_metrics: BacktestMetrics,
    *,
    pbo: Optional[float] = None,
    dsr: Optional[float] = None,
    param_stable: Optional[bool] = None,
    min_oos_trades: int = OVERFITTING_THRESHOLDS["min_oos_trades"],
) -> str:
    """Map OOS evidence to the shared Phase 6 verdict labels."""

    thresholds = OVERFITTING_THRESHOLDS
    if oos_metrics.num_trades < min_oos_trades or oos_metrics.profit_factor is None:
        return OosVerdictLabel.INSUFFICIENT_EVIDENCE.value
    profit_factor = oos_metrics.profit_factor
    if (
        (pbo is not None and pbo > thresholds["pbo_max_moderate"])
        or (dsr is not None and dsr < 0)
        or param_stable is False
        or profit_factor < thresholds["pf_min_fragile_floor"]
        or oos_metrics.max_drawdown_pct > thresholds["dd_max_fragile"]
    ):
        return OosVerdictLabel.FRAGILE.value
    if (
        pbo is not None
        and pbo <= thresholds["pbo_max_robust"]
        and dsr is not None
        and dsr >= thresholds["dsr_min_robust"]
        and param_stable is True
        and profit_factor >= thresholds["pf_min_robust"]
        and oos_metrics.max_drawdown_pct <= thresholds["dd_max_robust"]
    ):
        return OosVerdictLabel.ROBUST.value
    return OosVerdictLabel.MODERATE.value


@dataclass(frozen=True)
class OverfittingBatteryResult:
    verdict: str
    pbo: Optional[float]
    dsr: float
    param_stable: Optional[bool]
    sensitivity: Optional[Mapping[str, Any]]
    adjusted_p_values: tuple[float, ...]


def run_overfitting_battery(
    oos_metrics: BacktestMetrics,
    *,
    fold_metrics: Sequence[float],
    in_sample_fold_metrics: Optional[Sequence[float]] = None,
    param_grid_metrics: Optional[Mapping[Any, float]] = None,
    p_values: Optional[Sequence[float]] = None,
    n_trials: int = 1,
) -> OverfittingBatteryResult:
    """Run available diagnostics without fabricating missing IS evidence."""

    oos_folds = _finite_series(fold_metrics, name="fold_metrics")
    pbo = None
    if in_sample_fold_metrics is not None:
        pbo = probability_of_backtest_overfitting(in_sample_fold_metrics, oos_folds)
    stability = parameter_stability(oos_folds) if len(oos_folds) >= 2 else None
    dsr = deflated_sharpe_ratio(
        oos_metrics.sharpe,
        n_samples=max(oos_metrics.num_trades, 2),
        n_trials=n_trials,
    )
    sensitivity = sensitivity_heatmap(param_grid_metrics) if param_grid_metrics is not None else None
    adjusted = tuple(multiple_testing_adjustment(p_values or (), method="holm"))
    verdict = classify_overfitting(
        oos_metrics,
        pbo=pbo,
        dsr=dsr,
        param_stable=stability,
    )
    return OverfittingBatteryResult(
        verdict=verdict,
        pbo=pbo,
        dsr=dsr,
        param_stable=stability,
        sensitivity=sensitivity,
        adjusted_p_values=adjusted,
    )
