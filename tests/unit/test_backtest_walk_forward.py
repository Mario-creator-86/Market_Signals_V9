from dataclasses import FrozenInstanceError

import pytest

from core.backtest import (
    BacktestConfig,
    BacktestResult,
    DatasetSplit,
    EvidenceLevel,
    Trade,
    compute_metrics,
)
from core.backtest.walk_forward import WalkForwardResult, run_walk_forward


def _base_config():
    return BacktestConfig(
        ticker="AAPL",
        asset_class="US_STOCKS",
        initial_capital=10_000.0,
        position_size_pct=0.1,
        tp_pct=0.06,
        sl_pct=0.03,
        max_holding_bars=20,
    )


class RecordingEngine:
    is_authoritative = True

    def __init__(self):
        self.configs = []

    def run(self, config):
        self.configs.append(config)
        start, end = config.out_of_sample_range
        trade = Trade(
            entry_bar=start + 1,
            exit_bar=min(start + 3, end - 1),
            entry_time=None,
            exit_time=None,
            direction="LONG",
            entry_price=100.0,
            exit_price=102.0,
            sl=98.0,
            tp=104.0,
            size=10.0,
            gross_pnl=20.0,
            commission=1.0,
            slippage_cost=0.5,
            result="WIN",
            r_multiple=1.0,
            exit_reason="take_profit",
            holding_bars=2,
        )
        metrics = compute_metrics(
            [trade],
            initial_capital=config.initial_capital,
            validation_scope="recording",
            is_authoritative=True,
        )
        return BacktestResult(
            config=config,
            metrics=metrics,
            trades=(trade,),
            evidence_level=EvidenceLevel.TICKER,
        )


def _splits():
    return [
        DatasetSplit((0, 100), None, (100, 120), None, "walk_forward"),
        DatasetSplit((20, 120), None, (120, 140), None, "walk_forward"),
    ]


def test_walk_forward_result_is_frozen_and_backcompat_method_exact():
    result = WalkForwardResult(
        splits=(),
        window_results=(),
        oos_metrics=None,
        holdout_metrics=None,
        validation_method="faithful_v2_walk_forward",
        validation_status="insufficient_windows",
        promotion_status="research_only",
    )
    assert result.validation_method == "faithful_v2_walk_forward"
    assert result.v9_engine is True
    with pytest.raises(FrozenInstanceError):
        result.validation_status = "changed"


def test_run_walk_forward_scores_each_oos_window_and_aggregates():
    engine = RecordingEngine()
    result = run_walk_forward(
        "AAPL",
        engine=engine,
        splits=_splits(),
        base_config=_base_config(),
    )

    assert [config.out_of_sample_range for config in engine.configs] == [(100, 120), (120, 140)]
    assert result.oos_metrics.num_trades == 2
    assert result.validation_status == "evaluated"
    assert result.promotion_status == "research_only"
    assert result.validation_method == "faithful_v2_walk_forward"


def test_run_walk_forward_scores_final_holdout_separately():
    engine = RecordingEngine()
    holdout = DatasetSplit((0, 100), (100, 110), (110, 140), (140, 160), "reserved_holdout")

    result = run_walk_forward(
        "AAPL",
        engine=engine,
        splits=_splits(),
        base_config=_base_config(),
        final_holdout_split=holdout,
    )

    assert engine.configs[-1].out_of_sample_range == (140, 160)
    assert engine.configs[-1].final_holdout_range == (140, 160)
    assert result.holdout_metrics.num_trades == 1


def test_run_walk_forward_requires_config_when_work_exists():
    with pytest.raises(ValueError, match="base_config"):
        run_walk_forward("AAPL", engine=RecordingEngine(), splits=_splits())


def test_run_walk_forward_rejects_overlapping_oos_windows():
    overlapping = [
        DatasetSplit((0, 100), None, (100, 130), None, "walk_forward"),
        DatasetSplit((20, 120), None, (120, 150), None, "walk_forward"),
    ]
    with pytest.raises(ValueError, match="OOS windows overlap"):
        run_walk_forward(
            "AAPL",
            engine=RecordingEngine(),
            splits=overlapping,
            base_config=_base_config(),
        )
