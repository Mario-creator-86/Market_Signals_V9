import pandas as pd
import pytest

from core.crypto_research_strategies import _rsi, get_research_strategy, replay_research_strategy


def _uptrend_frame(rows: int = 300) -> pd.DataFrame:
    close = [100.0] * 220 + [100.0 + (index * 1.5) for index in range(rows - 220)]
    return pd.DataFrame(
        {
            "open": close,
            "high": [value + 1.0 for value in close],
            "low": [value - 1.0 for value in close],
            "close": close,
        },
        index=pd.date_range("2026-01-01", periods=rows, freq="h", tz="UTC", name="dt"),
    )


def test_pre_registered_donchian_replay_uses_next_bar_entry_and_closed_target():
    result = replay_research_strategy(
        "BTC-USD",
        df=_uptrend_frame(),
        strategy_id="R1_DONCHIAN_TREND_20_50_200",
    )

    assert result["strategy_id"] == "R1_DONCHIAN_TREND_20_50_200"
    assert result["trade_list"]
    first = result["trade_list"][0]
    assert first["dir"] == "LONG"
    assert first["reason"] == "take_profit"
    assert first["entry_time"] < first["exit_time"]
    assert first["net_pct"] > 0


def test_rsi_is_defined_at_extremes_instead_of_inventing_missing_values():
    rising = _rsi(pd.Series([float(index) for index in range(1, 12)]), 2)
    falling = _rsi(pd.Series([float(12 - index) for index in range(1, 12)]), 2)

    assert rising.iloc[-1] == 100.0
    assert falling.iloc[-1] == 0.0


def test_unknown_research_strategy_is_rejected():
    with pytest.raises(ValueError, match="Unknown pre-registered"):
        get_research_strategy("not-a-strategy")


def test_pullback_candidate_is_explicitly_registered_research_only_spec():
    spec = get_research_strategy("R3_EMA20_100_PULLBACK")

    assert spec.family == "ema_pullback_trend"
    assert spec.fast_ema == 20
    assert spec.slow_ema == 100
    assert spec.target_atr > spec.stop_atr


def test_four_hour_breakout_candidate_has_an_explicit_non_default_interval():
    spec = get_research_strategy("R4_DONCHIAN_4H_20_50")

    assert spec.interval == "4h"
    assert spec.family == "donchian_trend"
    assert spec.slow_ema == 50


def test_slower_four_hour_breakout_is_fixed_before_research_replay():
    spec = get_research_strategy("R5_DONCHIAN_4H_55_20_50")

    assert spec.interval == "4h"
    assert spec.lookback == 55
    assert spec.fast_ema == 20
    assert spec.slow_ema == 50
    assert spec.stop_atr == 2.0
    assert spec.target_atr == 4.0
