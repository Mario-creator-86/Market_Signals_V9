import pytest

from core.backtest.splits import (
    DatasetSplit,
    add_validation_fold,
    assert_disjoint_and_ordered,
    contiguous_walk_forward_split,
    reserve_final_holdout,
)


def test_dataset_split_rejects_overlap_and_out_of_order_ranges():
    with pytest.raises(ValueError, match="overlap"):
        DatasetSplit(
            train_range=(0, 100),
            validation_range=None,
            out_of_sample_range=(90, 150),
            final_holdout_range=None,
            method="contiguous_walk_forward",
        )
    with pytest.raises(ValueError, match="ordered"):
        DatasetSplit(
            train_range=(100, 200),
            validation_range=None,
            out_of_sample_range=(0, 50),
            final_holdout_range=None,
            method="contiguous_walk_forward",
        )


def test_dataset_split_requires_train_and_oos():
    with pytest.raises(ValueError, match="TRAIN"):
        DatasetSplit(
            train_range=None,
            validation_range=None,
            out_of_sample_range=(100, 150),
            final_holdout_range=None,
            method="bad",
        )


def test_dataset_split_accepts_disjoint_four_way_split():
    split = DatasetSplit(
        train_range=(0, 100),
        validation_range=(100, 130),
        out_of_sample_range=(130, 160),
        final_holdout_range=(160, 190),
        method="reserved_contiguous_holdout",
    )
    assert_disjoint_and_ordered(split)


def test_contiguous_walk_forward_matches_existing_recent_window_math():
    splits = contiguous_walk_forward_split(
        300,
        train_bars=100,
        test_bars=50,
        max_windows=3,
        min_windows=2,
        warmup_bars=10,
    )

    assert [split.train_range for split in splits] == [(50, 150), (100, 200), (150, 250)]
    assert [split.out_of_sample_range for split in splits] == [(150, 200), (200, 250), (250, 300)]
    assert all(split.validation_range is None for split in splits)


def test_contiguous_walk_forward_returns_empty_when_minimum_not_met():
    assert contiguous_walk_forward_split(
        170,
        train_bars=100,
        test_bars=50,
        max_windows=4,
        min_windows=2,
        warmup_bars=10,
    ) == []


def test_add_validation_fold_carves_tail_of_train_only():
    base = DatasetSplit(
        train_range=(0, 100),
        validation_range=None,
        out_of_sample_range=(100, 150),
        final_holdout_range=None,
        method="contiguous_walk_forward",
    )

    split = add_validation_fold(base, validation_bars=30)

    assert split.train_range == (0, 70)
    assert split.validation_range == (70, 100)
    assert split.out_of_sample_range == (100, 150)


def test_reserve_final_holdout_carves_oos_tail_once():
    base = DatasetSplit(
        train_range=(0, 100),
        validation_range=None,
        out_of_sample_range=(100, 200),
        final_holdout_range=None,
        method="contiguous_walk_forward",
    )

    split = reserve_final_holdout(base, holdout_bars=30)

    assert split.out_of_sample_range == (100, 170)
    assert split.final_holdout_range == (170, 200)
    with pytest.raises(ValueError, match="already has"):
        reserve_final_holdout(split, holdout_bars=10)
