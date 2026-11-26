import pandas as pd
from types import SimpleNamespace

from core.walk_forward_validation import (
    run_faithful_v2_technical_walk_forward,
    run_fixed_walk_forward,
    summarize_closed_trades,
)
from tools.run_faithful_v2_technical_validation import _provenance, _scoped_config


def _frame(rows=40):
    return pd.DataFrame(
        {"close": range(rows)},
        index=pd.date_range("2026-01-01", periods=rows, freq="min", tz="UTC"),
    )


def test_walk_forward_passes_only_train_plus_current_test_to_replay():
    seen_ends = []

    def runner(_ticker, *, df, **_kwargs):
        seen_ends.append(df.index[-1])
        test_start = df.index[20]
        return {
            "trade_list": [
                {"entry_time": df.index[19].isoformat(), "exit_time": df.index[21].isoformat(), "reason": "take_profit", "net_pct": 5, "r_net": 1},
                {"entry_time": test_start.isoformat(), "exit_time": df.index[22].isoformat(), "reason": "take_profit", "net_pct": 2, "r_net": 0.5},
                {"entry_time": df.index[23].isoformat(), "exit_time": df.index[-1].isoformat(), "reason": "time_exit", "net_pct": 3, "r_net": 1},
            ]
        }

    result = run_fixed_walk_forward(
        "BTC-USD",
        _frame(),
        train_bars=20,
        test_bars=5,
        max_windows=2,
        min_windows=2,
        min_sample=1,
        warmup_bars=5,
        replay_runner=runner,
    )

    assert len(seen_ends) == 2
    assert seen_ends[-1] == _frame().index[-1]
    assert result["metrics"]["sample_size"] == 2
    assert result["metrics"]["profit_factor"] is None
    assert result["status"] == "unmeasurable_profit_factor"
    assert all("time_exit" not in str(trade.get("reason")) for trade in result["oos_trades"])


def test_walk_forward_negative_edge_uses_only_closed_oos_trade_metrics():
    def runner(_ticker, *, df, **_kwargs):
        return {
            "trade_list": [
                {"entry_time": df.index[20].isoformat(), "exit_time": df.index[21].isoformat(), "reason": "stop_loss", "net_pct": -2, "r_net": -1},
                {"entry_time": df.index[22].isoformat(), "exit_time": df.index[23].isoformat(), "reason": "take_profit", "net_pct": 1, "r_net": 0.5},
            ]
        }

    result = run_fixed_walk_forward(
        "ETH-USD",
        _frame(25),
        train_bars=20,
        test_bars=5,
        max_windows=1,
        min_windows=1,
        min_sample=2,
        warmup_bars=5,
        replay_runner=runner,
    )
    assert result["metrics"]["profit_factor"] == 0.5
    assert result["metrics"]["expectancy_r"] == -0.25
    assert result["status"] == "negative_edge"


def test_fixed_walk_forward_requires_evidence_in_each_counted_oos_window():
    calls = 0

    def runner(_ticker, *, df, **_kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            return {"trade_list": []}
        return {
            "trade_list": [
                {
                    "entry_time": df.index[20].isoformat(),
                    "exit_time": df.index[21].isoformat(),
                    "reason": "take_profit",
                    "net_pct": 2.0,
                    "r_net": 1.0,
                },
                {
                    "entry_time": df.index[22].isoformat(),
                    "exit_time": df.index[23].isoformat(),
                    "reason": "stop_loss",
                    "net_pct": -1.0,
                    "r_net": -0.5,
                },
            ]
        }

    result = run_fixed_walk_forward(
        "BTC-USD",
        _frame(50),
        train_bars=20,
        test_bars=5,
        max_windows=2,
        min_windows=2,
        min_sample=2,
        min_profit_factor=1.2,
        warmup_bars=5,
        replay_runner=runner,
    )

    assert result["metrics"]["sample_size"] == 2
    assert result["observed_oos_windows"] == 1
    assert result["windows"][0]["has_closed_oos_evidence"] is True
    assert result["windows"][1]["has_closed_oos_evidence"] is False
    assert result["status"] == "insufficient_windows"


def test_summarize_closed_trades_never_invents_profit_factor_without_losses():
    summary = summarize_closed_trades([{"net_pct": 1.2, "r_net": 0.8}])
    assert summary["profit_factor"] is None


def test_summarize_closed_trades_uses_explicit_risk_for_capital_drawdown():
    summary = summarize_closed_trades(
        [
            {"net_pct": 2.0, "r_net": 1.0},
            {"net_pct": -1.0, "r_net": -1.0},
        ],
        capital_risk_per_trade_pct=0.5,
    )

    assert summary["net_pct"] == 1.0  # additive trade-notional return, retained for audit
    assert summary["raw_max_drawdown_pct"] == 1.0
    assert summary["capital_risk_per_trade_pct"] == 0.5
    assert summary["capital_equity_end"] == 99.9975
    assert summary["capital_return_pct"] == -0.0025
    assert summary["max_drawdown_pct"] == 0.5


def test_walk_forward_refuses_explicitly_truncated_source_before_replay():
    frame = _frame()
    frame.attrs["source_truncated"] = True

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("truncated source must not be replayed")

    result = run_fixed_walk_forward(
        "BTC-USD",
        frame,
        train_bars=20,
        test_bars=5,
        max_windows=1,
        warmup_bars=5,
        replay_runner=should_not_run,
    )

    assert result["status"] == "source_truncated"
    assert result["metrics"]["sample_size"] == 0


def test_faithful_v2_technical_replay_uses_only_training_perf_and_never_promotes():
    frame = _frame(45)
    v2_cfg = {
        "enabled_strategies_by_asset_class": {"CRYPTO_FUTURES": ["S2_BREAKOUT_RETEST"]},
        "costs_by_asset": {"CRYPTO_FUTURES": {"commission_bps": 4.0}},
    }
    config = SimpleNamespace(signal={}, bot={"min_edge_score": 75}, strategy_v2=v2_cfg)
    observed = {"test_received_training_cache": False}

    def runner(_ticker, *, df, config_manager, v2_only, **kwargs):
        assert v2_only is True
        perf_cache = kwargs.get("v2_perf_cache")
        if perf_cache is None:
            assert kwargs.get("allow_research_only_candidates") is True
            # Bootstrap phase: both closes occur before the OOS boundary.
            return {
                "trade_list": [
                    {"strategy_source": "V2", "strategy_id": "S2_BREAKOUT_RETEST", "dir": "LONG", "entry_time": df.index[3].isoformat(), "exit_time": df.index[4].isoformat(), "reason": "take_profit", "net_pct": 2.0, "r_net": 1.0},
                    {"strategy_source": "V2", "strategy_id": "S2_BREAKOUT_RETEST", "dir": "LONG", "entry_time": df.index[6].isoformat(), "exit_time": df.index[7].isoformat(), "reason": "stop_loss", "net_pct": -1.0, "r_net": -0.5},
                ]
            }

        entries = perf_cache.get_symbol_entries("BTC-USD")
        assert entries
        assert kwargs.get("allow_research_only_candidates") is None
        only_entry = next(iter(entries.values()))
        assert only_entry["sample_size"] == 2
        assert only_entry["validation_status"] == "training_window_only"
        assert config_manager.strategy_v2["_walk_forward_training_performance"] is True
        observed["test_received_training_cache"] = True
        return {
            "trade_list": [
                {"strategy_source": "V2", "strategy_id": "S2_BREAKOUT_RETEST", "dir": "LONG", "entry_time": df.index[20].isoformat(), "exit_time": df.index[21].isoformat(), "reason": "take_profit", "net_pct": 2.0, "r_net": 1.0},
                {"strategy_source": "V2", "strategy_id": "S2_BREAKOUT_RETEST", "dir": "LONG", "entry_time": df.index[22].isoformat(), "exit_time": df.index[23].isoformat(), "reason": "stop_loss", "net_pct": -1.0, "r_net": -0.5},
            ]
        }

    result = run_faithful_v2_technical_walk_forward(
        "BTC-USD",
        frame,
        config_manager=config,
        train_bars=20,
        test_bars=5,
        max_windows=1,
        min_windows=1,
        min_sample=2,
        min_profit_factor=1.2,
        capital_risk_per_trade_pct=0.5,
        warmup_bars=5,
        replay_runner=runner,
    )

    assert observed["test_received_training_cache"] is True
    assert result["status"] == "research_only"
    report = next(item for item in result["strategy_reports"] if item["strategy_id"] == "S2_BREAKOUT_RETEST" and item["direction"] == "LONG")
    assert report["technical_status"] == "validated"
    assert report["promotion_status"] == "research_only"
    assert report["metrics"]["sample_size"] == 2


def test_faithful_walk_forward_requires_trades_in_each_counted_oos_window():
    frame = _frame(50)
    config = SimpleNamespace(
        signal={},
        bot={"min_edge_score": 0},
        strategy_v2={
            "enabled_strategies_by_asset_class": {"CRYPTO_FUTURES": ["S2_BREAKOUT_RETEST"]},
            "costs_by_asset": {"CRYPTO_FUTURES": {"commission_bps": 4.0}},
        },
    )
    test_calls = 0

    def runner(_ticker, *, df, v2_only, **kwargs):
        nonlocal test_calls
        assert v2_only is True
        if kwargs.get("v2_perf_cache") is None:
            return {
                "trade_list": [
                    {
                        "strategy_source": "V2",
                        "strategy_id": "S2_BREAKOUT_RETEST",
                        "dir": "LONG",
                        "entry_time": df.index[2].isoformat(),
                        "exit_time": df.index[3].isoformat(),
                        "reason": "take_profit",
                        "net_pct": 2.0,
                        "r_net": 1.0,
                    },
                    {
                        "strategy_source": "V2",
                        "strategy_id": "S2_BREAKOUT_RETEST",
                        "dir": "LONG",
                        "entry_time": df.index[4].isoformat(),
                        "exit_time": df.index[5].isoformat(),
                        "reason": "stop_loss",
                        "net_pct": -1.0,
                        "r_net": -0.5,
                    },
                ]
            }

        test_calls += 1
        if test_calls > 1:
            return {"trade_list": []}
        test_start = df.index[-5]
        return {
            "trade_list": [
                {
                    "strategy_source": "V2",
                    "strategy_id": "S2_BREAKOUT_RETEST",
                    "dir": "LONG",
                    "entry_time": test_start.isoformat(),
                    "exit_time": df.index[-4].isoformat(),
                    "reason": "take_profit",
                    "net_pct": 2.0,
                    "r_net": 1.0,
                },
                {
                    "strategy_source": "V2",
                    "strategy_id": "S2_BREAKOUT_RETEST",
                    "dir": "LONG",
                    "entry_time": df.index[-3].isoformat(),
                    "exit_time": df.index[-2].isoformat(),
                    "reason": "stop_loss",
                    "net_pct": -1.0,
                    "r_net": -0.5,
                },
            ]
        }

    result = run_faithful_v2_technical_walk_forward(
        "BTC-USD",
        frame,
        config_manager=config,
        train_bars=20,
        test_bars=5,
        max_windows=2,
        min_windows=2,
        min_sample=2,
        min_profit_factor=1.2,
        warmup_bars=5,
        replay_runner=runner,
    )

    report = next(
        item for item in result["strategy_reports"]
        if item["strategy_id"] == "S2_BREAKOUT_RETEST" and item["direction"] == "LONG"
    )
    assert report["eligible_training_windows"] == 2
    assert report["observed_oos_windows"] == 1
    assert report["eligible_windows"] == 1
    assert report["technical_status"] == "insufficient_windows"


def test_validation_strategy_scope_is_in_memory_and_preserves_thresholds():
    config = SimpleNamespace(
        signal={"risk_mode": "MEDIUM"},
        bot={"min_edge_score": 75},
        strategy_v2={
            "min_profit_factor": 1.2,
            "enabled_strategies_by_asset_class": {
                "CRYPTO_FUTURES": ["S2_BREAKOUT_RETEST", "S7_LIQUIDITY_SWEEP_REVERSAL"],
            },
        },
    )

    scoped = _scoped_config(config, "BTC-USD", ["S7_LIQUIDITY_SWEEP_REVERSAL"])

    assert scoped.strategy_v2["enabled_strategies_by_asset_class"]["CRYPTO_FUTURES"] == ["S7_LIQUIDITY_SWEEP_REVERSAL"]
    assert scoped.strategy_v2["min_profit_factor"] == 1.2
    assert config.strategy_v2["enabled_strategies_by_asset_class"]["CRYPTO_FUTURES"] == [
        "S2_BREAKOUT_RETEST", "S7_LIQUIDITY_SWEEP_REVERSAL"
    ]


def test_faithful_validation_report_contains_reproducible_provenance():
    config = SimpleNamespace(
        strategy_v2={
            "costs_by_asset": {
                "CRYPTO_FUTURES": {
                    "commission_bps": 4.0,
                    "entry_slippage_bps": 2.5,
                }
            }
        }
    )

    provenance = _provenance(config)

    assert provenance["replay"] == "core.strategy_backtester.backtest(v2_only=True)"
    assert provenance["promotion_status"] == "research_only"
    assert provenance["historical_ai_news"] == "not_available_and_not_replayed"
    assert provenance["strategy_v2_costs"] == config.strategy_v2["costs_by_asset"]
    assert set(provenance["source_sha256"]) == {
        "strategy_backtester",
        "walk_forward_validation",
        "strategy_selector",
    }
    assert all(
        len(digest) == 64 and set(digest) <= set("0123456789abcdef")
        for digest in provenance["source_sha256"].values()
    )
