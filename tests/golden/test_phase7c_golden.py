"""GOLD-061 no-forced-strategy and GOLD-054b live OOS gate seam."""

import pytest

from core.backtest import BacktestMetrics, EvidenceLevel
from core.backtest.no_forced_strategy import (
    NO_VALIDATED_TICKER_SPECIFIC_STRATEGY,
    resolve_evidence_level,
)
from core.backtest.oos_gate_runner import OosGateRunner, default_oos_gate_runner
from core.ml.registry import (
    ModelRecord,
    ModelRegistry,
    OosGate,
    OosVerdictLabel,
    TargetSpec,
)
from core.signal_engine import SignalHorizon

pytestmark = pytest.mark.golden


def _metrics(num_trades=50, expectancy=0.3):
    return BacktestMetrics(
        initial_capital=10_000.0,
        final_capital=11_000.0,
        net_return_pct=10.0,
        cagr=10.0,
        benchmark_net_return_pct=None,
        max_drawdown_pct=10.0,
        sharpe=1.5,
        sortino=1.8,
        calmar=1.0,
        win_rate=0.6,
        profit_factor=1.5,
        expectancy_r=expectancy,
        num_trades=num_trades,
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
        validation_scope="golden_oos",
        is_authoritative=True,
    )


def _record(name):
    return ModelRecord(
        model=name,
        version="v1",
        features=("rsi",),
        feature_version="v1",
        target=TargetSpec(SignalHorizon.SWING, "return_5d > 0"),
        horizon=SignalHorizon.SWING,
        training_range=("2024-01-01", "2024-12-31"),
        validation_range=("2025-01-01", "2025-06-30"),
        test_range=("2025-07-01", "2025-12-31"),
        trained_at="2026-01-01T00:00:00Z",
    )


def test_gold061_evidence_falls_through_and_can_return_no_strategy():
    sector = resolve_evidence_level(
        "AAPL",
        "US_STOCKS",
        ticker_metrics=_metrics(5),
        industry_metrics=_metrics(5),
        sector_metrics=_metrics(50),
    )
    assert sector.level is EvidenceLevel.SECTOR
    assert sector.status == "VALIDATED_SECTOR_EVIDENCE"

    none = resolve_evidence_level(
        "AAPL", "US_STOCKS", ticker_metrics=_metrics(5), universe_metrics=_metrics(5)
    )
    assert none.is_sufficient is False
    assert none.status == NO_VALIDATED_TICKER_SPECIFIC_STRATEGY


def test_gold054b_oos_gate_is_available_and_fail_closed_without_data():
    baseline, challenger = _record("baseline"), _record("challenger")
    verdict = OosGate().evaluate_oos_improvement(
        baseline, challenger, backtest_runner=default_oos_gate_runner()
    )
    assert verdict.available is True
    assert verdict.reason == "phase7_runner"
    assert verdict.verdict == OosVerdictLabel.INSUFFICIENT_EVIDENCE.value


def test_gold054b_real_moderate_verdict_can_promote_registered_challenger():
    baseline, challenger = _record("baseline"), _record("challenger")
    runner = OosGateRunner(
        data_provider=lambda record, role: _metrics(expectancy=0.5 if role == "challenger" else 0.1)
    )
    verdict = OosGate().evaluate_oos_improvement(
        baseline, challenger, backtest_runner=runner
    )
    assert verdict.verdict == OosVerdictLabel.MODERATE.value

    registry = ModelRegistry()
    registry.register(challenger)
    promoted = registry.promote(challenger, verdict)
    assert promoted.oos_verdict == OosVerdictLabel.MODERATE.value
