"""Disjoint TRAIN/VALIDATION/OUT_OF_SAMPLE/FINAL_HOLDOUT ranges."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Optional, TypeAlias

Range: TypeAlias = tuple[int, int]


def _validate_range(name: str, value: Optional[Range]) -> None:
    if value is None:
        return
    if len(value) != 2 or any(not isinstance(item, int) for item in value):
        raise ValueError(f"{name} must be a pair of integer bar indexes")
    if value[0] < 0 or value[0] >= value[1]:
        raise ValueError(f"{name} must be a non-empty half-open range")


def _overlaps(left: Range, right: Range) -> bool:
    return left[0] < right[1] and right[0] < left[1]


@dataclass(frozen=True)
class DatasetSplit:
    train_range: Optional[Range]
    validation_range: Optional[Range]
    out_of_sample_range: Optional[Range]
    final_holdout_range: Optional[Range]
    method: str

    def __post_init__(self) -> None:
        assert_disjoint_and_ordered(self)


def assert_disjoint_and_ordered(split: DatasetSplit) -> None:
    """Fail closed if data roles overlap or appear in the wrong chronology."""

    if split.train_range is None:
        raise ValueError("TRAIN range is required")
    if split.out_of_sample_range is None:
        raise ValueError("OUT_OF_SAMPLE range is required")
    if not split.method.strip():
        raise ValueError("split method must not be empty")

    named = (
        ("TRAIN", split.train_range),
        ("VALIDATION", split.validation_range),
        ("OUT_OF_SAMPLE", split.out_of_sample_range),
        ("FINAL_HOLDOUT", split.final_holdout_range),
    )
    for name, value in named:
        _validate_range(name, value)
    present = [(name, value) for name, value in named if value is not None]
    for (left_name, left), (right_name, right) in combinations(present, 2):
        if _overlaps(left, right):
            raise ValueError(f"split ranges overlap: {left_name} {left} and {right_name} {right}")
    for (left_name, left), (right_name, right) in zip(present, present[1:]):
        if left[1] > right[0]:
            raise ValueError(
                f"split ranges are not ordered: {left_name} {left} must precede {right_name} {right}"
            )


def contiguous_walk_forward_split(
    n_bars: int,
    *,
    train_bars: int,
    test_bars: int,
    max_windows: int,
    min_windows: int = 3,
    warmup_bars: int = 60,
) -> list[DatasetSplit]:
    """Reproduce ``run_fixed_walk_forward`` recent-window index math.

    Training ranges roll forward by one OOS block, so training observations may
    overlap across windows; OOS blocks never overlap.  Each individual split
    remains role-disjoint.
    """

    values = (n_bars, train_bars, test_bars, max_windows, min_windows, warmup_bars)
    if any(not isinstance(value, int) for value in values):
        raise TypeError("bar and window counts must be integers")
    if n_bars <= 0 or test_bars <= 0 or max_windows <= 0 or min_windows <= 0:
        raise ValueError("bar and window counts must be positive")
    if warmup_bars < 0 or train_bars <= warmup_bars:
        raise ValueError("train_bars must exceed warmup_bars")

    available_windows = max(0, (n_bars - train_bars) // test_bars)
    window_count = min(max_windows, available_windows)
    if window_count < min_windows:
        return []
    first_start = n_bars - (train_bars + window_count * test_bars)
    return [
        DatasetSplit(
            train_range=(start, start + train_bars),
            validation_range=None,
            out_of_sample_range=(start + train_bars, start + train_bars + test_bars),
            final_holdout_range=None,
            method="contiguous_walk_forward",
        )
        for start in (first_start + index * test_bars for index in range(window_count))
    ]


def add_validation_fold(split: DatasetSplit, *, validation_bars: int) -> DatasetSplit:
    """Carve validation data from the end of TRAIN without touching OOS."""

    if split.validation_range is not None:
        raise ValueError("split already has a VALIDATION range")
    if validation_bars <= 0:
        raise ValueError("validation_bars must be positive")
    train_start, train_end = split.train_range
    if train_end - train_start <= validation_bars:
        raise ValueError("validation fold must be smaller than TRAIN")
    validation_start = train_end - validation_bars
    return DatasetSplit(
        train_range=(train_start, validation_start),
        validation_range=(validation_start, train_end),
        out_of_sample_range=split.out_of_sample_range,
        final_holdout_range=split.final_holdout_range,
        method=f"{split.method}+validation_fold",
    )


def reserve_final_holdout(split: DatasetSplit, *, holdout_bars: int) -> DatasetSplit:
    """Carve one untouched final block from the tail of OOS exactly once."""

    if split.final_holdout_range is not None:
        raise ValueError("split already has a FINAL_HOLDOUT range")
    if holdout_bars <= 0:
        raise ValueError("holdout_bars must be positive")
    oos_start, oos_end = split.out_of_sample_range
    if oos_end - oos_start <= holdout_bars:
        raise ValueError("holdout must be smaller than OUT_OF_SAMPLE")
    holdout_start = oos_end - holdout_bars
    return DatasetSplit(
        train_range=split.train_range,
        validation_range=split.validation_range,
        out_of_sample_range=(oos_start, holdout_start),
        final_holdout_range=(holdout_start, oos_end),
        method=f"{split.method}+reserved_holdout",
    )
