from dataclasses import replace
from datetime import datetime, timezone

import pandas as pd
import pytest

from core.backtest import BacktestConfig, BacktestResult
from core.backtest.adapters import (
    FaithfulReplayAdapter,
    ExploratoryEngineAdapter,
    LegacyBacktesterAdapter,
    get_default_engine,
)
from core.backtest.engine import BacktestEngine, NoLookAheadError


def _config(direction="BOTH"):
    return BacktestConfig(
        ticker="AAPL",
        asset_class="US_STOCKS",
        initial_capital=10_000.0,
        position_size_pct=0.1,
        tp_pct=0.06,
        sl_pct=0.03,
        max_holding_bars=20,
        direction=direction,
    )


def test_adapter_metadata_and_default_engine():
    assert isinstance(get_default_engine(), FaithfulReplayAdapter)
    assert FaithfulReplayAdapter().validation_scope == "faithful_v2_live_replay"
    assert FaithfulReplayAdapter().is_authoritative is True
    assert ExploratoryEngineAdapter().validation_scope == "exploratory_legacy_strategy"
    assert ExploratoryEngineAdapter().is_authoritative is False
    assert LegacyBacktesterAdapter().validation_scope == "legacy_v1_rsi_macd"
    assert LegacyBacktesterAdapter().is_authoritative is False


def test_adapters_satisfy_domain_protocol():
    for adapter in (
        FaithfulReplayAdapter(),
        ExploratoryEngineAdapter(),
        LegacyBacktesterAdapter(),
    ):
        assert isinstance(adapter, BacktestEngine)


def test_faithful_adapter_lifts_real_replay_shape_without_live_data():
    raw = {
        "ticker": "AAPL",
        "days": 30,
        "trades": 1,
        "win_rate": 100.0,
        "expectancy_r": 1.2,
        "profit_factor": None,
        "max_drawdown_pct": 0.0,
        "total_net_pct": 2.0,
        "cost_model": {
            "entry_slippage_bps": 2.0,
            "take_profit_or_time_exit_slippage_bps": 3.0,
        },
        "trade_list": [
            {
                "dir": "LONG",
                "entry": 100.0,
                "exit": 102.1,
                "stop_loss_price": 97.0,
                "take_profit_price": 106.0,
                "entry_bar": 11,
                "exit_bar": 14,
                "entry_time": "2026-01-02T14:30:00+00:00",
                "exit_time": "2026-01-02T14:33:00+00:00",
                "r": 1.3,
                "r_net": 1.2,
                "net_pct": 2.0,
                "commission_cost_pct": 0.1,
                "reason": "take_profit",
            }
        ],
    }
    adapter = FaithfulReplayAdapter(runner=lambda **kwargs: raw)

    result = adapter.run(_config())

    assert isinstance(result, BacktestResult)
    assert result.metrics.is_authoritative is True
    assert result.metrics.num_trades == 1
    assert result.metrics.win_rate == 1.0
    assert result.trades[0].entry_bar == 11
    assert result.trades[0].exit_bar == 14
    assert result.trades[0].commission == pytest.approx(1.0)
    assert result.trades[0].slippage_cost > 0
    assert result.raw["adapter"] == "faithful_replay"


def test_faithful_adapter_filters_direction():
    raw = {
        "trade_list": [
            {"dir": "SHORT", "entry": 100, "exit": 95, "entry_bar": 1, "exit_bar": 2,
             "r_net": 1.0, "net_pct": 1.0, "reason": "take_profit"}
        ]
    }
    result = FaithfulReplayAdapter(runner=lambda **kwargs: raw).run(_config("LONG"))
    assert result.metrics.num_trades == 0


def test_faithful_adapter_slices_context_and_keeps_only_oos_trades():
    dataframe = pd.DataFrame(
        {
            "dt": pd.date_range("2026-01-01", periods=20, freq="min", tz="UTC"),
            "open": [100.0] * 20,
            "high": [101.0] * 20,
            "low": [99.0] * 20,
            "close": [100.0] * 20,
            "volume": [1_000.0] * 20,
        }
    )
    captured = {}

    def runner(**kwargs):
        captured.update(kwargs)
        return {
            "trade_list": [
                {"dir": "LONG", "entry": 100, "exit": 101,
                 "entry_time": dataframe.dt.iloc[7].isoformat(),
                 "exit_time": dataframe.dt.iloc[8].isoformat(), "r_net": 1, "net_pct": 1},
                {"dir": "LONG", "entry": 100, "exit": 101,
                 "entry_time": dataframe.dt.iloc[12].isoformat(),
                 "exit_time": dataframe.dt.iloc[14].isoformat(), "r_net": 1, "net_pct": 1},
            ]
        }

    config = replace(_config(), train_range=(0, 10), out_of_sample_range=(10, 15))
    result = FaithfulReplayAdapter(runner=runner, dataframe=dataframe).run(config)

    assert len(captured["df"]) == 15
    assert len(result.trades) == 1
    assert result.trades[0].entry_bar == 12


def test_legacy_v1_adapter_fails_closed_on_same_bar_execution():
    raw = {
        "signals": [
            {"type": "BUY", "index": 50, "entry": 100.0, "exit_price": 102.0,
             "gross_pnl": 20.0, "net_pnl": 18.0, "commission": 2.0}
        ]
    }
    adapter = LegacyBacktesterAdapter(runner=lambda **kwargs: raw)
    with pytest.raises(NoLookAheadError, match="legacy V1"):
        adapter.run(_config())


def test_exploratory_adapter_lifts_object_result():
    class Direction:
        value = "long"

    class ExitReason:
        value = "take_profit"

    class LegacyTrade:
        entry_time = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
        exit_time = datetime(2026, 1, 2, 14, 35, tzinfo=timezone.utc)
        direction = Direction()
        entry_price = 100.0
        exit_price = 103.0
        sl_price = 97.0
        tp_price = 106.0
        quantity = 10.0
        gross_pnl = 30.0
        commission = 2.0
        pnl = 28.0
        holding_bars = 5
        exit_reason = ExitReason()

    class LegacyResult:
        initial_capital = 10_000.0
        final_capital = 10_028.0
        total_return_pct = 0.28
        max_drawdown_pct = 1.0
        sharpe_ratio = 1.1
        sortino_ratio = 1.3
        calmar_ratio = 0.28
        win_rate = 100.0
        profit_factor = None
        total_trades = 1
        avg_holding_bars = 5.0
        trades = [LegacyTrade()]
        equity_curve = [
            {"timestamp": "2026-01-02T14:30:00+00:00", "equity": 10_000.0},
            {"timestamp": "2026-01-02T14:35:00+00:00", "equity": 10_028.0},
        ]
        cost_model = {"entry_slippage_bps": 1.0, "take_profit_or_time_exit_slippage_bps": 1.0}

    result = ExploratoryEngineAdapter(runner=lambda **kwargs: LegacyResult()).run(_config())

    assert result.metrics.is_authoritative is False
    assert result.metrics.win_rate == 1.0
    assert result.trades[0].entry_bar == 0
    assert result.trades[0].exit_bar == 1
