from datetime import datetime, timedelta, timezone

import pytest

from core.capital_entry_guard import evaluate_capital_entry, evaluate_entry_data
from core.strategy_v2.performance_cache import StrategyPerformanceCache


def _config():
    return {
        "capital_entry_gate": {
            "enabled": True,
            "require_ai_review": True,
            "data": {
                "max_signal_age_seconds": 300,
                "min_risk_reward_ratio": 1.2,
                "require_price_integrity": True,
            },
            "backtest": {
                "required_validation_method": "faithful_v2_walk_forward",
                "min_sample": 30,
                "min_profit_factor": 1.2,
                "min_expectancy_r": 0.0,
                "max_drawdown_pct": 25.0,
                "max_age_seconds": 21600,
            },
            "realized_performance": {
                "enabled": True,
                "min_sample": 3,
                "min_profit_factor": 1.2,
                "min_expectancy_usd": 0.0,
                "max_drawdown_pct": 25.0,
            },
            "news": {
                "enabled": True,
                "require_fresh_context": True,
                "min_articles_24h": 1,
                "require_source_urls": True,
                "require_ai_analysis": True,
                "min_ai_analyzed_articles_24h": 1,
                "block_conflicting_sentiment": True,
            },
        }
    }


def _signal(**overrides):
    value = {
        "id": "sig-1",
        "ticker": "BTC-USD",
        "asset_class": "CRYPTO_FUTURES",
        "strategy_source": "V2",
        "strategy_type": "S2_BREAKOUT_RETEST",
        "action": "BUY",
        "entry": 100.0,
        "tp": 104.0,
        "sl": 98.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "price_verified": True,
        "price_integrity": {
            "status": "verified",
            "is_verified": True,
            "live_price": 100.0,
            "technical_price": 100.0,
        },
    }
    value.update(overrides)
    return value


def _positions():
    return [
        {
            "status": "CLOSED",
            "strategy_type": "S2_BREAKOUT_RETEST",
            "direction": "LONG",
            "net_pnl": pnl,
            "pnl_pct_notional": pnl / 10,
            "entry_price": 100.0,
            "exit_price": 101.0,
            "entry_price_source": "exchange_quote",
            "exit_price_source": "exchange_quote",
            "entry_price_quality": {"level": "VERIFIED", "is_valid": True},
            "exit_price_quality": {"level": "VERIFIED", "is_valid": True},
            "closed_at": f"2026-07-0{index}T10:00:00+00:00",
        }
        for index, pnl in enumerate((20.0, -10.0, 15.0), start=1)
    ]


def _news_pulse():
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "score": 72,
        "sentiment_trend": "bullish",
        "ai_sentiment_trend": "bullish",
        "24h": {"article_count": 1, "source_backed_count": 1, "ai_analyzed_count": 1, "source_backed_ai_analyzed_count": 1},
        "7d": {"article_count": 1},
        "top_headlines": [
            {
                "title": "Verified BTC catalyst",
                "url": "https://example.test/btc",
                "source": "Example News",
                "time": datetime.now(timezone.utc).isoformat(),
                "ai_micro_summary": {"overall_sentiment": "bullish"},
            }
        ],
    }


def _validated_cache(tmp_path, *, as_of=None):
    cache = StrategyPerformanceCache(tmp_path / "performance.json")
    timestamp = as_of or datetime.now(timezone.utc)
    cache.upsert_entry(
        "BTC-USD",
        "CRYPTO_FUTURES",
        "S2_BREAKOUT_RETEST",
        "LONG",
        {
            "sample_size": 36,
            "profit_factor": 1.45,
            "expectancy_R": 0.18,
            "max_drawdown": 8.0,
            "last_run": timestamp.isoformat(),
            "validation_as_of": timestamp.isoformat(),
            "validation_status": "validated",
            "validation_method": "faithful_v2_walk_forward",
            "costs_used": {"commission_bps": 4.0, "entry_slippage_bps": 2.5},
        },
    )
    return cache


def test_capital_entry_requires_the_complete_evidence_bundle(tmp_path):
    result = evaluate_capital_entry(
        _signal(),
        _positions(),
        _config(),
        perf_cache=_validated_cache(tmp_path),
        news_pulse=_news_pulse(),
    )

    assert result["allowed"] is True
    assert result["check"] == "complete"
    assert result["checks"]["backtest"]["status"] == "validated"
    assert result["checks"]["news"]["status"] == "aligned"


def test_capital_entry_rejects_legacy_or_exploratory_backtests_before_news(tmp_path):
    result = evaluate_capital_entry(
        _signal(strategy_source="V1"),
        _positions(),
        _config(),
        perf_cache=_validated_cache(tmp_path),
        news_pulse=_news_pulse(),
    )

    assert result["allowed"] is False
    assert result["check"] == "backtest"
    assert result["status"] == "unfaithful_strategy_source"
    assert "news" not in result["checks"]


def test_capital_entry_rejects_missing_price_provenance_before_backtest(tmp_path):
    result = evaluate_capital_entry(
        _signal(price_verified=False, price_integrity={"status": "conflict", "is_verified": False}),
        _positions(),
        _config(),
        perf_cache=_validated_cache(tmp_path),
        news_pulse=_news_pulse(),
    )

    assert result["allowed"] is False
    assert result["check"] == "data"
    assert result["status"] == "unverified_price_integrity"
    assert "backtest" not in result["checks"]


def test_capital_entry_rejects_stale_oos_validation(tmp_path):
    stale_at = datetime.now(timezone.utc) - timedelta(hours=7)
    result = evaluate_capital_entry(
        _signal(),
        _positions(),
        _config(),
        perf_cache=_validated_cache(tmp_path, as_of=stale_at),
        news_pulse=_news_pulse(),
    )

    assert result["allowed"] is False
    assert result["check"] == "backtest"
    assert result["status"] == "stale_validation"


def test_research_only_signal_is_blocked_even_if_capital_gate_is_disabled(tmp_path):
    result = evaluate_capital_entry(
        _signal(
            research_only=True,
            capital_execution_eligible=False,
            capital_execution_block_reason="S7 forward research is awaiting faithful OOS validation",
        ),
        _positions(),
        {"capital_entry_gate": {"enabled": False}},
        perf_cache=_validated_cache(tmp_path),
        news_pulse=_news_pulse(),
    )

    assert result["allowed"] is False
    assert result["check"] == "research_only"
    assert result["checks"]["research_only"]["status"] == "research_only"


def test_paper_s7_experiment_admits_only_the_configured_forward_source_before_oos(tmp_path):
    config = _config()
    config.update(
        {
            "mode": "paper",
            "paper_capital": {
                "enabled": True,
                "allow_unvalidated_s7": True,
                "strategy_ids": ["S7_LIQUIDITY_SWEEP_REVERSAL"],
                "strategy_sources": ["V2_FORWARD_RESEARCH"],
            },
        }
    )
    result = evaluate_capital_entry(
        _signal(
            strategy_source="V2_FORWARD_RESEARCH",
            strategy_type="S7_LIQUIDITY_SWEEP_REVERSAL",
            research_only=True,
            capital_execution_eligible=False,
        ),
        _positions(),
        config,
        perf_cache=StrategyPerformanceCache(tmp_path / "empty-performance.json"),
        news_pulse=None,
    )

    assert result["allowed"] is True
    assert result["status"] == "paper_only"
    assert result["paper_capital_experiment"] is True
    assert result["checks"]["data"]["allowed"] is True
    assert "backtest" not in result["checks"]
    assert "news" not in result["checks"]


def test_paper_supertrend_bos_experiment_is_admitted_without_weakening_live_mode(tmp_path):
    config = _config()
    config.update(
        {
            "mode": "paper",
            "paper_capital": {
                "enabled": True,
                "allow_unvalidated_strategies": True,
                "strategy_ids": ["S8_SUPERTREND_BOS"],
                "strategy_sources": ["V2"],
            },
        }
    )
    signal = _signal(
        strategy_source="V2",
        strategy_type="S8_SUPERTREND_BOS",
        research_only=True,
        capital_execution_eligible=False,
    )
    paper = evaluate_capital_entry(
        signal,
        [],
        config,
        perf_cache=StrategyPerformanceCache(tmp_path / "empty-s8.json"),
        news_pulse=None,
    )
    assert paper["allowed"] is True
    assert paper["paper_capital_experiment"] is True
    assert "S8_SUPERTREND_BOS" in paper["reason"]

    config["mode"] = "live"
    live = evaluate_capital_entry(
        signal,
        [],
        config,
        perf_cache=StrategyPerformanceCache(tmp_path / "empty-s8-live.json"),
        news_pulse=None,
    )
    assert live["allowed"] is False
    assert live["check"] == "research_only"


@pytest.mark.parametrize(
    ("ticker", "asset_class"),
    [
        ("BTC-USD", "CRYPTO_FUTURES"),
        ("AAPL", "US_STOCKS"),
        ("GC=F", "COMMODITIES"),
    ],
)
def test_paper_s7_experiment_supports_every_enabled_market(tmp_path, ticker, asset_class):
    config = _config()
    config.update(
        {
            "mode": "paper",
            "paper_capital": {
                "enabled": True,
                "allow_unvalidated_s7": True,
                "strategy_ids": ["S7_LIQUIDITY_SWEEP_REVERSAL"],
                "strategy_sources": ["V2_FORWARD_RESEARCH"],
            },
        }
    )

    result = evaluate_capital_entry(
        _signal(
            ticker=ticker,
            asset_class=asset_class,
            strategy_source="V2_FORWARD_RESEARCH",
            strategy_type="S7_LIQUIDITY_SWEEP_REVERSAL",
            research_only=True,
            capital_execution_eligible=False,
        ),
        [],
        config,
        perf_cache=StrategyPerformanceCache(tmp_path / f"{asset_class}.json"),
        news_pulse=None,
    )

    assert result["allowed"] is True
    assert result["status"] == "paper_only"
    assert result["paper_capital_experiment"] is True


def test_paper_s7_experiment_fails_closed_outside_paper_mode(tmp_path):
    config = _config()
    config.update(
        {
            "mode": "live",
            "paper_capital": {
                "enabled": True,
                "allow_unvalidated_s7": True,
                "strategy_ids": ["S7_LIQUIDITY_SWEEP_REVERSAL"],
                "strategy_sources": ["V2_FORWARD_RESEARCH"],
            },
        }
    )
    result = evaluate_capital_entry(
        _signal(
            strategy_source="V2_FORWARD_RESEARCH",
            strategy_type="S7_LIQUIDITY_SWEEP_REVERSAL",
            research_only=True,
            capital_execution_eligible=False,
        ),
        _positions(),
        config,
        perf_cache=StrategyPerformanceCache(tmp_path / "empty-performance.json"),
        news_pulse=None,
    )

    assert result["allowed"] is False
    assert result["check"] == "research_only"
    assert "paper_s7_experiment" not in result["checks"]


def test_capital_data_gate_keeps_positive_rr_floor_when_config_is_lowered():
    config = {"capital_entry_gate": {"enabled": True, "data": {"min_risk_reward_ratio": 0.96}}}

    accepted = evaluate_entry_data(_signal(tp=102.2), config)
    assert accepted["allowed"] is True
    assert accepted["min_risk_reward_ratio"] == 1.1

    rejected = evaluate_entry_data(_signal(tp=102.18), config)
    assert rejected["allowed"] is False
    assert rejected["status"] == "risk_reward_too_low"


@pytest.mark.parametrize(
    ("ticker", "asset_class"),
    [
        ("BTC-USD", "CRYPTO_FUTURES"),
        ("AAPL", "US_STOCKS"),
    ],
)
def test_scanner_price_first_candidate_is_admitted_only_to_paper_capital(
    ticker,
    asset_class,
):
    signal = _signal(
        ticker=ticker,
        asset_class=asset_class,
        autonomous_source="scanner",
        signal_mode="AUTONOMOUS_SCANNER",
        strategy_source="SCANNER_PRICE_FIRST_V1",
        strategy_type="SCANNER_AI_LONG",
    )

    paper = evaluate_capital_entry(signal, [], {"mode": "paper"})

    assert paper["allowed"] is True
    assert paper["check"] == "paper_autonomous_experiment"
    assert paper["status"] == "paper_only"
    assert paper["paper_capital_experiment"] is True
    assert paper["checks"]["data"]["allowed"] is True
    assert "backtest" not in paper["checks"]

    live = evaluate_capital_entry(signal, [], {"mode": "live"})

    assert live["allowed"] is False
    assert live["check"] == "backtest"
    assert live["status"] == "unfaithful_strategy_source"


def test_scanner_paper_experiment_cannot_bypass_price_integrity():
    result = evaluate_capital_entry(
        _signal(
            ticker="BTC-USD",
            asset_class="CRYPTO_FUTURES",
            autonomous_source="scanner",
            signal_mode="AUTONOMOUS_SCANNER",
            strategy_source="SCANNER_PRICE_FIRST_V1",
            strategy_type="SCANNER_AI_LONG",
            price_verified=False,
            price_integrity={"status": "unavailable", "is_verified": False},
        ),
        [],
        {"mode": "paper"},
    )

    assert result["allowed"] is False
    assert result["check"] == "data"
    assert result["status"] == "unverified_price_integrity"


def test_scanner_paper_experiment_accepts_complete_application_config():
    """The AI pipeline passes the full config, unlike the two execution gates."""
    execution = _config()
    execution["mode"] = "paper"
    result = evaluate_capital_entry(
        _signal(
            autonomous_source="scanner",
            signal_mode="AUTONOMOUS_SCANNER",
            strategy_source="SCANNER_PRICE_FIRST_V1",
            strategy_type="SCANNER_AI_LONG",
        ),
        [],
        {"agents": {"execution": execution}, "bot": {"mode": "paper"}},
    )

    assert result["allowed"] is True
    assert result["check"] == "paper_autonomous_experiment"
    assert result["paper_capital_experiment"] is True


def test_pre_earnings_paper_exception_never_weakens_live_mode():
    signal = _signal(
        ticker="NVDA",
        asset_class="US_STOCKS",
        autonomous_source="pre_earnings",
        signal_mode="AUTONOMOUS_PRE_EARNINGS",
        strategy_source="V1",
        strategy_type="PRE_EARNINGS_AI_LONG",
        pre_earnings_context={
            "earnings_time_utc": "2026-08-27T00:00:00+00:00",
            "session": "BMO",
        },
    )

    result = evaluate_capital_entry(signal, [], {"mode": "live"})

    assert result["allowed"] is False
    assert result["check"] == "backtest"
    assert result["status"] == "unfaithful_strategy_source"


# ---------------------------------------------------------------------------
# Paper experiment budget (2026-11-12).
#
# The scanner fade ran 355 paper trades at a 39% win rate against a ~40%
# coin-flip rate for its 4%/6% stop/target geometry, and kept being admitted:
# the paper exemption had no stopping rule.
# ---------------------------------------------------------------------------
def _scanner_signal(**overrides):
    value = dict(
        autonomous_source="scanner",
        signal_mode="AUTONOMOUS_SCANNER",
        strategy_source="SCANNER_PRICE_FIRST_V1",
        strategy_type="SCANNER_AI_SHORT",
        action="SELL",
        tp=96.0,
        sl=102.0,
        entry_strategy="fade",
    )
    value.update(overrides)
    return _signal(**value)


def _scanner_history(pnls, *, entry_strategy="fade", strategy_type="SCANNER_AI_SHORT", direction="SHORT"):
    return [
        {
            "status": "CLOSED",
            "strategy_type": strategy_type,
            "direction": direction,
            "entry_strategy": entry_strategy,
            "net_pnl": pnl,
            "pnl_pct_notional": pnl / 10,
            "entry_price": 100.0,
            "exit_price": 101.0,
            "entry_price_source": "Binance_orderbook",
            "exit_price_source": "Binance_orderbook",
            "entry_price_quality": {"level": "VERIFIED", "is_valid": True},
            "exit_price_quality": {"level": "VERIFIED", "is_valid": True},
            "closed_at": f"2026-09-01T10:{index // 60:02d}:{index % 60:02d}+00:00",
        }
        for index, pnl in enumerate(pnls)
    ]


def _paper_config(max_sample=10):
    return {"mode": "paper", "capital_entry_gate": {
        "enabled": True,
        "realized_performance": {"min_sample": 5, "min_profit_factor": 1.05, "min_expectancy_usd": 0.0, "max_drawdown_pct": 1000.0},
        "paper_experiment": {"max_sample_without_edge": max_sample},
    }}


def test_paper_experiment_stops_after_budget_without_edge():
    losing = _scanner_history([5.0, -7.0] * 5)  # 10 trades, profit factor 0.71

    result = evaluate_capital_entry(_scanner_signal(), losing, _paper_config(max_sample=10))

    assert result["allowed"] is False
    assert result["check"] == "paper_experiment_budget"
    assert result["status"] == "experiment_concluded_no_edge"
    assert result["checks"]["paper_experiment_budget"]["sample_size"] == 10


def test_paper_experiment_keeps_collecting_below_budget():
    losing = _scanner_history([5.0, -7.0] * 4)  # 8 of 10

    result = evaluate_capital_entry(_scanner_signal(), losing, _paper_config(max_sample=10))

    assert result["allowed"] is True
    assert result["check"] == "paper_autonomous_experiment"
    assert result["checks"]["paper_experiment_budget"]["status"] == "collecting_evidence"


def test_paper_experiment_with_proven_edge_continues_past_budget():
    winning = _scanner_history([9.0, -5.0] * 10)  # 20 trades, profit factor 1.8

    result = evaluate_capital_entry(_scanner_signal(), winning, _paper_config(max_sample=10))

    assert result["allowed"] is True
    assert result["checks"]["paper_experiment_budget"]["status"] == "validated"


def test_new_entry_regime_starts_a_fresh_paper_budget():
    old_regime = _scanner_history([5.0, -7.0] * 10, entry_strategy="fade")

    result = evaluate_capital_entry(
        _scanner_signal(entry_strategy="momentum"), old_regime, _paper_config(max_sample=10),
    )

    assert result["allowed"] is True
    assert result["checks"]["paper_experiment_budget"]["sample_size"] == 0


def test_paper_experiment_budget_can_be_disabled():
    config = _paper_config(max_sample=10)
    config["capital_entry_gate"]["paper_experiment"]["enabled"] = False

    result = evaluate_capital_entry(_scanner_signal(), _scanner_history([5.0, -7.0] * 10), config)

    assert result["allowed"] is True
    assert result["checks"]["paper_experiment_budget"]["status"] == "disabled"
