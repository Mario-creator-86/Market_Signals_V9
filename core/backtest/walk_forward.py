"""Walk-forward orchestration over role-disjoint V9 dataset splits."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import combinations
from types import MappingProxyType
from typing import Any, Optional

from .engine import BacktestEngine
from .metrics import compute_metrics
from .splits import DatasetSplit
from .types import BacktestConfig, BacktestMetrics, BacktestResult


@dataclass(frozen=True)
class WalkForwardResult:
    splits: tuple[DatasetSplit, ...]
    window_results: tuple[BacktestResult, ...]
    oos_metrics: Optional[BacktestMetrics]
    holdout_metrics: Optional[BacktestMetrics]
    validation_method: str
    validation_status: str
    promotion_status: str
    v9_engine: bool = field(default=True, init=False)
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "splits", tuple(self.splits))
        object.__setattr__(self, "window_results", tuple(self.window_results))
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))


def _assert_oos_windows_do_not_overlap(splits: Sequence[DatasetSplit]) -> None:
    for left, right in combinations(splits, 2):
        left_range = left.out_of_sample_range
        right_range = right.out_of_sample_range
        if left_range[0] < right_range[1] and right_range[0] < left_range[1]:
            raise ValueError(f"OOS windows overlap: {left_range} and {right_range}")


def _config_for_split(base: BacktestConfig, split: DatasetSplit) -> BacktestConfig:
    return replace(
        base,
        train_range=split.train_range,
        validation_range=split.validation_range,
        out_of_sample_range=split.out_of_sample_range,
        final_holdout_range=None,
    )


def _config_for_holdout(base: BacktestConfig, split: DatasetSplit) -> BacktestConfig:
    if split.final_holdout_range is None:
        raise ValueError("final_holdout_split must contain a FINAL_HOLDOUT range")
    return replace(
        base,
        train_range=split.train_range,
        validation_range=split.validation_range,
        out_of_sample_range=split.final_holdout_range,
        final_holdout_range=split.final_holdout_range,
    )


def _run_existing_faithful_validator(
    ticker: str,
    dataframe: Any,
    config_manager: Any,
    splits: Sequence[DatasetSplit],
    validator: Optional[Callable[..., Mapping[str, Any]]],
) -> Optional[Mapping[str, Any]]:
    if dataframe is None or config_manager is None or not splits:
        return None
    if validator is None:
        from core.walk_forward_validation import run_faithful_v2_technical_walk_forward

        validator = run_faithful_v2_technical_walk_forward
    first = splits[0]
    train_bars = first.train_range[1] - first.train_range[0]
    test_bars = first.out_of_sample_range[1] - first.out_of_sample_range[0]
    return validator(
        ticker,
        dataframe,
        config_manager=config_manager,
        train_bars=train_bars,
        test_bars=test_bars,
        max_windows=len(splits),
        min_windows=min(3, len(splits)),
    )


def run_walk_forward(
    ticker: str,
    *,
    engine: BacktestEngine,
    splits: Sequence[DatasetSplit],
    base_config: Optional[BacktestConfig] = None,
    final_holdout_split: Optional[DatasetSplit] = None,
    dataframe: Any = None,
    config_manager: Any = None,
    faithful_validator: Optional[Callable[..., Mapping[str, Any]]] = None,
) -> WalkForwardResult:
    """Score fixed OOS windows and an untouched final holdout.

    The existing faithful V2 validator is composed as an audit when its
    dataframe and config manager are supplied.  Domain metrics come from the
    injected ``BacktestEngine`` so the same stable result contract is used by
    OOS gates and later phases.
    """

    normalized_ticker = str(ticker or "").strip().upper()
    if not normalized_ticker:
        raise ValueError("ticker must not be empty")
    split_tuple = tuple(splits)
    if (split_tuple or final_holdout_split is not None) and base_config is None:
        raise ValueError("base_config is required when validation work exists")
    if base_config is not None and base_config.ticker != normalized_ticker:
        raise ValueError("base_config ticker does not match walk-forward ticker")
    _assert_oos_windows_do_not_overlap(split_tuple)

    if final_holdout_split is not None and split_tuple:
        holdout_range = final_holdout_split.final_holdout_range
        if holdout_range is None:
            raise ValueError("final_holdout_split must contain a FINAL_HOLDOUT range")
        latest_oos_end = max(split.out_of_sample_range[1] for split in split_tuple)
        if holdout_range[0] < latest_oos_end:
            raise ValueError("FINAL_HOLDOUT overlaps evaluated OOS data")

    window_results = tuple(
        engine.run(_config_for_split(base_config, split))
        for split in split_tuple
    )
    all_trades = tuple(
        trade
        for result in window_results
        for trade in result.trades
    )
    oos_metrics = None
    if window_results:
        oos_metrics = compute_metrics(
            sorted(all_trades, key=lambda trade: (trade.entry_bar, trade.exit_bar)),
            initial_capital=base_config.initial_capital,
            validation_scope="faithful_v2_walk_forward",
            is_authoritative=all(result.metrics.is_authoritative for result in window_results),
        )

    holdout_metrics = None
    if final_holdout_split is not None:
        holdout_metrics = engine.run(_config_for_holdout(base_config, final_holdout_split)).metrics

    audit: dict[str, Any] = {}
    if getattr(engine, "validation_scope", "") == "faithful_v2_live_replay":
        faithful_audit = _run_existing_faithful_validator(
            normalized_ticker,
            dataframe,
            config_manager,
            split_tuple,
            faithful_validator,
        )
        if faithful_audit is not None:
            audit["existing_faithful_validator"] = dict(faithful_audit)

    if not window_results:
        validation_status = "insufficient_windows"
    elif oos_metrics is not None and oos_metrics.num_trades == 0:
        validation_status = "insufficient_evidence"
    else:
        validation_status = "evaluated"
    return WalkForwardResult(
        splits=split_tuple,
        window_results=window_results,
        oos_metrics=oos_metrics,
        holdout_metrics=holdout_metrics,
        validation_method="faithful_v2_walk_forward",
        validation_status=validation_status,
        promotion_status="research_only",
        raw=audit,
    )
