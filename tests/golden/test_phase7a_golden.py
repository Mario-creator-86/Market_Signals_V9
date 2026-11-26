"""GOLD-057 domain interface/no-look-ahead and GOLD-058 metric completeness."""

from dataclasses import fields

import pytest

from core.backtest import BacktestMetrics, Trade
from core.backtest.adapters import (
    ExploratoryEngineAdapter,
    FaithfulReplayAdapter,
    LegacyBacktesterAdapter,
    get_default_engine,
)
from core.backtest.engine import BacktestEngine, NoLookAheadError, assert_no_look_ahead
from core.backtest.metrics import compute_metrics

pytestmark = pytest.mark.golden


def _trade(entry_bar=2, r_multiple=1.0, gross_pnl=100.0, result="WIN"):
    return Trade(
        entry_bar=entry_bar,
        exit_bar=entry_bar + 2,
        entry_time=None,
        exit_time=None,
        direction="LONG",
        entry_price=100.0,
        exit_price=101.0,
        sl=99.0,
        tp=102.0,
        size=100.0,
        gross_pnl=gross_pnl,
        commission=2.0,
        slippage_cost=1.0,
        result=result,
        r_multiple=r_multiple,
        exit_reason="take_profit" if result == "WIN" else "stop_loss",
        holding_bars=2,
    )


def test_gold057_engine_protocol_default_and_legacy_boundaries():
    adapters = (
        FaithfulReplayAdapter(),
        ExploratoryEngineAdapter(),
        LegacyBacktesterAdapter(),
    )
    assert all(isinstance(adapter, BacktestEngine) for adapter in adapters)
    assert isinstance(get_default_engine(), FaithfulReplayAdapter)
    assert get_default_engine().is_authoritative is True
    assert ExploratoryEngineAdapter().is_authoritative is False
    assert LegacyBacktesterAdapter().is_authoritative is False


def test_gold057_no_look_ahead_guard_is_fail_closed():
    with pytest.raises(NoLookAheadError):
        assert_no_look_ahead([_trade(entry_bar=5)], signal_bars={5})
    with pytest.raises(NoLookAheadError):
        assert_no_look_ahead([_trade(entry_bar=4)], signal_bars={5})
    assert_no_look_ahead([_trade(entry_bar=5)], signal_bars={4})


def test_gold058_complete_metrics_and_cost_separation():
    metrics = compute_metrics(
        [_trade(), _trade(entry_bar=5, r_multiple=-1.0, gross_pnl=-50.0, result="LOSS")],
        initial_capital=10_000.0,
        date_span_days=365.25,
        validation_scope="golden",
        is_authoritative=True,
    )

    assert len(fields(BacktestMetrics)) == 26
    assert metrics.expectancy_r == 0.0
    assert metrics.median_trade_r == 0.0
    assert metrics.best_trade_r == 1.0
    assert metrics.worst_trade_r == -1.0
    assert metrics.fees_total == 4.0
    assert metrics.slippage_total == 2.0
    assert metrics.profit_factor == 2.0
    assert metrics.benchmark_net_return_pct is None
    assert metrics.is_not_probability is True
