import pytest

from core.backtest import Trade
from core.backtest.engine import BacktestEngine, NoLookAheadError, assert_no_look_ahead


def _trade(entry_bar: int) -> Trade:
    return Trade(
        entry_bar=entry_bar,
        exit_bar=entry_bar + 2,
        entry_time=None,
        exit_time=None,
        direction="LONG",
        entry_price=100.0,
        exit_price=103.0,
        sl=98.0,
        tp=105.0,
        size=10.0,
        gross_pnl=30.0,
        commission=1.0,
        slippage_cost=0.5,
        result="WIN",
        r_multiple=1.0,
        exit_reason="tp",
        holding_bars=2,
    )


def test_backtest_engine_is_runtime_protocol():
    class StructuralEngine:
        def run(self, config):
            raise NotImplementedError

    assert isinstance(StructuralEngine(), BacktestEngine)


def test_assert_no_look_ahead_rejects_same_bar_entry():
    with pytest.raises(NoLookAheadError, match="same-bar"):
        assert_no_look_ahead([_trade(5)], signal_bars={5})


def test_assert_no_look_ahead_rejects_entry_before_signal():
    with pytest.raises(NoLookAheadError, match="precedes"):
        assert_no_look_ahead([_trade(4)], signal_bars={5})


def test_assert_no_look_ahead_accepts_next_bar_entry():
    assert_no_look_ahead([_trade(6)], signal_bars={5})


def test_assert_no_look_ahead_requires_signal_evidence():
    with pytest.raises(NoLookAheadError, match="no signal"):
        assert_no_look_ahead([_trade(6)], signal_bars=set())
