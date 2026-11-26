import pandas as pd

from core.paper_strategy_validation import run_paper_btc_walk_forward


def _bars(periods: int, frequency: str) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=periods, freq=frequency, tz="UTC")
    close = [100.0 + number for number in range(periods)]
    return pd.DataFrame(
        {
            "open": close,
            "high": [value + 1.0 for value in close],
            "low": [value - 1.0 for value in close],
            "close": close,
            "volume": [1000.0] * periods,
        },
        index=index,
    )


def test_paper_oos_is_research_only_and_never_scores_terminal_open_trade() -> None:
    seen_contexts = []

    def builder(_strategy_id, bars_1h, _bars_4h, _config):
        seen_contexts.append(bars_1h.index)
        positions = pd.Series(0.0, index=bars_1h.index)
        positions.iloc[24:] = -1.0
        return bars_1h, positions

    report = run_paper_btc_walk_forward(
        _bars(50, "1h"),
        _bars(12, "4h"),
        strategy_id="donchian_short_1h",
        train_days=1,
        test_days=1,
        max_windows=1,
        min_windows=1,
        min_sample=1,
        position_builder=builder,
    )

    assert report["status"] == "research_only"
    assert report["promotion_eligible"] is False
    assert report["metrics"]["sample_size"] == 0
    assert report["price_validation_status"] == "insufficient_evidence"
    assert len(seen_contexts) == 1
    assert len(seen_contexts[0]) == 48


def test_paper_oos_builder_receives_only_train_and_current_test_window() -> None:
    seen_contexts = []

    def builder(_strategy_id, bars_1h, _bars_4h, _config):
        seen_contexts.append(bars_1h.index.copy())
        positions = pd.Series(0.0, index=bars_1h.index)
        positions.iloc[24:26] = -1.0
        return bars_1h, positions

    report = run_paper_btc_walk_forward(
        _bars(73, "1h"),
        _bars(20, "4h"),
        strategy_id="donchian_short_1h",
        train_days=1,
        test_days=1,
        max_windows=2,
        min_windows=2,
        min_sample=3,
        position_builder=builder,
    )

    assert len(seen_contexts) == 2
    assert all(len(context) == 48 for context in seen_contexts)
    assert seen_contexts[0].max() < seen_contexts[1].max()
    assert report["metrics"]["sample_size"] == 2
    assert report["price_validation_status"] == "insufficient_evidence"
