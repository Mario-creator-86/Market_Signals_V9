from dataclasses import replace

from core.backtest import (
    BacktestConfig,
    BacktestMetrics,
    BacktestResult,
    EvidenceLevel,
)
from core.backtest.oos_gate_runner import OosEvaluation, OosGateRunner, default_oos_gate_runner
from core.ml.registry import ModelRecord, OosGate, OosVerdictLabel, TargetSpec
from core.signal_engine import SignalHorizon


def _record(name):
    return ModelRecord(
        model=name,
        version="v1",
        features=("rsi",),
        feature_version="v1",
        target=TargetSpec(horizon=SignalHorizon.SWING, condition="return_5d > 0"),
        horizon=SignalHorizon.SWING,
        training_range=("2024-01-01", "2024-12-31"),
        validation_range=("2025-01-01", "2025-06-30"),
        test_range=("2025-07-01", "2025-12-31"),
        trained_at="2026-01-01T00:00:00Z",
    )


def _metrics(expectancy=0.3, profit_factor=1.5, drawdown=10.0):
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
        expectancy_r=expectancy,
        num_trades=50,
        avg_trade_r=expectancy,
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


def test_default_runner_and_oos_gate_are_honest_without_data():
    runner = default_oos_gate_runner()
    assert runner(_record("baseline"), _record("challenger")) == OosVerdictLabel.INSUFFICIENT_EVIDENCE.value

    verdict = OosGate().evaluate_oos_improvement(
        _record("baseline"), _record("challenger"), backtest_runner=runner
    )
    assert verdict.available is True
    assert verdict.reason == "phase7_runner"
    assert verdict.verdict == OosVerdictLabel.INSUFFICIENT_EVIDENCE.value


def test_runner_returns_moderate_when_challenger_beats_baseline_but_diagnostics_missing():
    provider = lambda record, role: _metrics(0.5) if role == "challenger" else _metrics(0.1)
    runner = OosGateRunner(data_provider=provider)

    assert runner(_record("baseline"), _record("challenger")) == OosVerdictLabel.MODERATE.value


def test_runner_rejects_challenger_that_does_not_beat_baseline():
    provider = lambda record, role: _metrics(0.1) if role == "challenger" else _metrics(0.2)
    runner = OosGateRunner(data_provider=provider)

    assert runner(_record("baseline"), _record("challenger")) == OosVerdictLabel.FRAGILE.value


def test_runner_executes_backtest_config_through_engine():
    base_config = BacktestConfig(
        ticker="AAPL",
        asset_class="US_STOCKS",
        initial_capital=10_000.0,
        position_size_pct=0.1,
        tp_pct=0.06,
        sl_pct=0.03,
        max_holding_bars=20,
    )

    class Engine:
        def run(self, config):
            metrics = _metrics(0.5 if config.strategy_id == "challenger" else 0.1)
            return BacktestResult(config=config, metrics=metrics, trades=(), evidence_level=EvidenceLevel.TICKER)

    provider = lambda record, role: replace(base_config, strategy_id=role)
    runner = OosGateRunner(engine=Engine(), data_provider=provider)

    assert runner(_record("baseline"), _record("challenger")) == OosVerdictLabel.MODERATE.value


def test_runner_uses_overfitting_evidence_and_rejects_rank_reversal():
    baseline = OosEvaluation(metrics=_metrics(0.1))
    challenger = OosEvaluation(
        metrics=_metrics(0.5),
        fold_metrics=(0.1, 0.9, 0.5),
        in_sample_fold_metrics=(0.9, 0.1, 0.5),
        n_trials=3,
    )
    provider = lambda record, role: challenger if role == "challenger" else baseline

    assert OosGateRunner(data_provider=provider)(
        _record("baseline"), _record("challenger")
    ) == OosVerdictLabel.FRAGILE.value
