import json
from pathlib import Path

import pytest

from core.backtest import BacktestMetrics, EvidenceLevel
from core.backtest.no_forced_strategy import (
    NO_VALIDATED_TICKER_SPECIFIC_STRATEGY,
    EvidenceResolution,
    assert_min_sample,
    resolve_evidence_level,
)


def _metrics(num_trades=24):
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
        validation_scope="evidence",
        is_authoritative=True,
    )


def test_assert_min_sample_is_fail_closed():
    assert assert_min_sample(_metrics(24), min_sample=24) is True
    assert assert_min_sample(_metrics(23), min_sample=24) is False
    assert assert_min_sample(None, min_sample=24) is False
    with pytest.raises(ValueError):
        assert_min_sample(_metrics(), min_sample=0)


def test_resolve_uses_ticker_when_sufficient():
    resolution = resolve_evidence_level(
        "AAPL", "US_STOCKS", ticker_metrics=_metrics(50), min_sample=24
    )
    level, metrics = resolution
    assert isinstance(resolution, EvidenceResolution)
    assert level is EvidenceLevel.TICKER
    assert metrics.num_trades == 50
    assert resolution.is_sufficient is True


def test_resolve_falls_through_to_sector_and_labels_it():
    resolution = resolve_evidence_level(
        "AAPL",
        "US_STOCKS",
        ticker_metrics=_metrics(5),
        industry_metrics=_metrics(5),
        sector_metrics=_metrics(50),
        min_sample=24,
    )
    assert resolution.level is EvidenceLevel.SECTOR
    assert resolution.status == "VALIDATED_SECTOR_EVIDENCE"


def test_resolve_accepts_sufficient_universe_evidence():
    resolution = resolve_evidence_level(
        "AAPL",
        "US_STOCKS",
        ticker_metrics=_metrics(5),
        universe_metrics=_metrics(50),
        min_sample=24,
    )
    assert resolution.level is EvidenceLevel.UNIVERSE
    assert resolution.is_sufficient is True
    assert resolution.status == "VALIDATED_UNIVERSE_EVIDENCE"


def test_resolve_returns_explicit_no_strategy_when_none_sufficient():
    resolution = resolve_evidence_level(
        "AAPL",
        "US_STOCKS",
        ticker_metrics=_metrics(5),
        industry_metrics=_metrics(5),
        sector_metrics=_metrics(5),
        universe_metrics=_metrics(5),
        min_sample=24,
    )
    assert resolution.level is EvidenceLevel.UNIVERSE
    assert resolution.is_sufficient is False
    assert resolution.status == NO_VALIDATED_TICKER_SPECIFIC_STRATEGY


def test_backtest_engine_config_is_mirrored_in_example():
    from core.config import DEFAULT_CONFIG

    example = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
    expected = DEFAULT_CONFIG["backtest_engine"]
    assert example["backtest_engine"] == expected
    assert expected["default_engine"] == "faithful_replay"
    assert expected["overfitting"]["min_oos_trades"] == 24
    assert expected["evidence_min_sample"] == 24
