import pandas as pd

from profit_optimizer import backtest_position_series, build_candidate_list


def test_build_candidate_list_respects_limit() -> None:
    candidates = build_candidate_list(
        strategies=["ema_cross_short", "donchian_short"],
        timeframes=["1h", "4h"],
        max_evals=25,
        seed=7,
    )
    assert len(candidates) == 25


def test_backtest_position_series_short_trend_compounds() -> None:
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=6, freq="4h"),
            "close": [100.0, 95.0, 90.0, 85.0, 82.0, 80.0],
        }
    )
    positions = pd.Series([-1.0, -1.0, -1.0, -1.0, -1.0, 0.0])
    stats = backtest_position_series(df, positions, leverage=3.0, cost_bps_per_side=4.0)

    assert stats["trade_count"] == 1
    assert stats["total_return_pct"] > 40.0
    assert stats["profit_factor"] > 1.0
