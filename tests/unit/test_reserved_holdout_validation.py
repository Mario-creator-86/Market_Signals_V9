import pandas as pd

from core.reserved_holdout_validation import run_reserved_holdout


def _frame(rows: int = 20) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=rows, freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
        index=index,
    )


def test_reserved_holdout_excludes_development_and_terminal_time_exit_trades():
    frame = _frame()

    def runner(_ticker, *, df, **_kwargs):
        start = df.index[0]
        end = df.index[-1]
        return {
            "cost_model": {"provenance": "test"},
            "trade_list": [
                {"entry_time": frame.index[6].isoformat(), "exit_time": frame.index[7].isoformat(), "reason": "take_profit", "net_pct": 2.0, "r_net": 1.0},
                {"entry_time": frame.index[15].isoformat(), "exit_time": frame.index[16].isoformat(), "reason": "stop_loss", "net_pct": -1.0, "r_net": -0.5},
                {"entry_time": frame.index[17].isoformat(), "exit_time": end.isoformat(), "reason": "time_exit", "net_pct": 99.0, "r_net": 99.0},
            ],
        }

    result = run_reserved_holdout(
        "BTC-USD",
        frame,
        holdout_bars=5,
        warmup_bars=5,
        replay_runner=runner,
        min_sample=1,
    )

    assert result["split"]["overlap"] is False
    assert result["development_metrics"]["sample_size"] == 1
    assert result["holdout_metrics"]["sample_size"] == 1
    assert result["holdout_trades"][0]["entry_time"] == frame.index[15].isoformat()
    assert result["status"] == "negative_edge"
    assert result["promotion_eligible"] is False


def test_reserved_holdout_requires_sufficient_contiguous_data():
    result = run_reserved_holdout(
        "BTC-USD",
        _frame(8),
        holdout_bars=4,
        warmup_bars=5,
        replay_runner=lambda *_args, **_kwargs: {"trade_list": []},
    )

    assert result["status"] == "insufficient_data"
    assert result["promotion_eligible"] is False


def test_reserved_holdout_refuses_explicitly_truncated_source_before_replay():
    frame = _frame()
    frame.attrs["source_truncated"] = True

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("truncated source must not be replayed")

    result = run_reserved_holdout(
        "BTC-USD",
        frame,
        holdout_bars=5,
        warmup_bars=5,
        replay_runner=should_not_run,
    )

    assert result["status"] == "source_truncated"
    assert result["promotion_eligible"] is False
