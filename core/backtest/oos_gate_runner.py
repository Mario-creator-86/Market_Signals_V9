"""Phase 7 runner that turns OOS backtests into Phase 6 gate verdicts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Optional

from core.ml.registry import OosVerdictLabel

from .engine import BacktestEngine
from .overfitting import classify_overfitting, run_overfitting_battery
from .types import BacktestConfig, BacktestMetrics, BacktestResult


@dataclass(frozen=True)
class OosEvaluation:
    """Metrics plus optional independent overfitting evidence for one model."""

    metrics: BacktestMetrics
    fold_metrics: tuple[float, ...] = ()
    in_sample_fold_metrics: Optional[tuple[float, ...]] = None
    param_grid_metrics: Optional[Mapping[Any, float]] = None
    p_values: tuple[float, ...] = ()
    n_trials: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "fold_metrics", tuple(self.fold_metrics))
        if self.in_sample_fold_metrics is not None:
            object.__setattr__(self, "in_sample_fold_metrics", tuple(self.in_sample_fold_metrics))
        if self.param_grid_metrics is not None:
            object.__setattr__(self, "param_grid_metrics", MappingProxyType(dict(self.param_grid_metrics)))
        object.__setattr__(self, "p_values", tuple(self.p_values))
        if self.n_trials < 1:
            raise ValueError("n_trials must be at least one")


class OosGateRunner:
    """Callable compatible with ``OosGate.evaluate_oos_improvement``.

    A provider can return precomputed metrics/evidence or a ``BacktestConfig``
    to execute through the injected engine.  No provider or missing data fails
    closed as ``INSUFFICIENT_EVIDENCE``.
    """

    def __init__(
        self,
        *,
        data_provider: Optional[Callable[[Any, str], Any]] = None,
        engine: Optional[BacktestEngine] = None,
        overfitting_runner: Callable[..., Any] = run_overfitting_battery,
        registry: Any = None,
    ) -> None:
        self._data_provider = data_provider
        self._engine = engine
        self._overfitting_runner = overfitting_runner
        self._registry = registry

    def _resolve(self, record: Any, role: str) -> Optional[OosEvaluation]:
        if self._data_provider is None:
            return None
        value = self._data_provider(record, role)
        if value is None:
            return None
        if isinstance(value, OosEvaluation):
            return value
        if isinstance(value, BacktestMetrics):
            return OosEvaluation(metrics=value)
        if isinstance(value, BacktestResult):
            return OosEvaluation(metrics=value.metrics)
        if isinstance(value, BacktestConfig):
            if self._engine is None:
                raise ValueError("data_provider returned BacktestConfig but no engine is configured")
            return OosEvaluation(metrics=self._engine.run(value).metrics)
        if isinstance(value, Mapping) and isinstance(value.get("metrics"), BacktestMetrics):
            return OosEvaluation(
                metrics=value["metrics"],
                fold_metrics=tuple(value.get("fold_metrics") or ()),
                in_sample_fold_metrics=(
                    tuple(value["in_sample_fold_metrics"])
                    if value.get("in_sample_fold_metrics") is not None
                    else None
                ),
                param_grid_metrics=value.get("param_grid_metrics"),
                p_values=tuple(value.get("p_values") or ()),
                n_trials=int(value.get("n_trials", 1)),
            )
        raise TypeError(f"unsupported OOS data value for {role}: {type(value).__name__}")

    def __call__(self, baseline: Any, challenger: Any) -> str:
        baseline_evaluation = self._resolve(baseline, "baseline")
        challenger_evaluation = self._resolve(challenger, "challenger")
        if baseline_evaluation is None or challenger_evaluation is None:
            return OosVerdictLabel.INSUFFICIENT_EVIDENCE.value

        baseline_metrics = baseline_evaluation.metrics
        challenger_metrics = challenger_evaluation.metrics
        if not baseline_metrics.is_authoritative or not challenger_metrics.is_authoritative:
            return OosVerdictLabel.INSUFFICIENT_EVIDENCE.value
        if challenger_metrics.expectancy_r <= baseline_metrics.expectancy_r:
            return OosVerdictLabel.FRAGILE.value

        if challenger_evaluation.fold_metrics:
            battery = self._overfitting_runner(
                challenger_metrics,
                fold_metrics=challenger_evaluation.fold_metrics,
                in_sample_fold_metrics=challenger_evaluation.in_sample_fold_metrics,
                param_grid_metrics=challenger_evaluation.param_grid_metrics,
                p_values=challenger_evaluation.p_values,
                n_trials=challenger_evaluation.n_trials,
            )
            return str(battery.verdict)
        return classify_overfitting(challenger_metrics)


def default_oos_gate_runner() -> OosGateRunner:
    """Return a fail-closed runner until a caller supplies point-in-time data."""

    return OosGateRunner(data_provider=None)
