from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from agents.strategy_research_agent import StrategyResearchAgent, _legacy_proxy_oos_metrics
from core.request_context import ExternalCallBudget, get_request_context, request_context
from core.strategy_v2.performance_cache import (
    STRATEGY_PERF_CACHE_PATH,
    STRATEGY_RESEARCH_CACHE_PATH,
    StrategyPerformanceCache,
)


def test_legacy_proxy_walk_forward_is_research_only_and_not_promotable(tmp_path):
    metrics = _legacy_proxy_oos_metrics(
        {
            "avg_train_expectancy_R": 0.8,
            "avg_test_expectancy_R": 0.5,
            "overfit_penalty": 30.0,
            "windows": [
                {"test": {"sample_size": 12, "wins": 8, "losses": 4, "expectancy_R": 0.4, "max_drawdown": 6}},
                {"test": {"sample_size": 18, "wins": 11, "losses": 7, "expectancy_R": 0.6, "max_drawdown": 8}},
            ],
        },
        {"commission_bps": 4.0},
    )

    assert metrics["sample_size"] == 30
    assert metrics["win_rate"] == 19 / 30
    assert metrics["expectancy_R"] == 0.52
    assert metrics["profit_factor"] == 0.0
    assert metrics["validation_status"] == "research_only"
    assert metrics["validation_method"] == "legacy_proxy_walk_forward"

    cache = StrategyPerformanceCache(Path(tmp_path) / "performance.json")
    cache.upsert_entry("BTC-USD", "CRYPTO_FUTURES", "S2_BREAKOUT_RETEST", "LONG", metrics)
    stored = cache.get_entry("BTC-USD", "CRYPTO_FUTURES", "S2_BREAKOUT_RETEST", "LONG")
    assert stored["validation_status"] == "research_only"
    assert stored["validation_method"] == "legacy_proxy_walk_forward"


def test_selector_rejects_profitable_numbers_without_faithful_provenance():
    from core.strategy_v2.selector import StrategySelector

    selector = StrategySelector(
        config={
            "require_profitable_performance": True,
            "min_sample_size": 30,
            "min_profit_factor": 1.2,
            "min_expectancy_R": 0.0,
            "max_drawdown_pct": 25.0,
            "required_validation_method": "faithful_v2_walk_forward",
        }
    )
    reason = selector._should_reject_for_validation(
        {
            "sample_size": 100,
            "profit_factor": 3.0,
            "expectancy_R": 0.5,
            "max_drawdown": 4.0,
            "validation_status": "research_only",
            "validation_method": "legacy_proxy_walk_forward",
        },
        90.0,
    )
    assert reason == "validation_unverified:research_only"


def test_research_agent_store_is_separate_from_live_capital_performance_store():
    # Construct only the field under test; the full agent starts a backtest
    # service and is unnecessary for this path-level safety contract.
    agent = StrategyResearchAgent.__new__(StrategyResearchAgent)
    agent.research_cache = StrategyPerformanceCache(STRATEGY_RESEARCH_CACHE_PATH)

    assert agent.research_cache.path == STRATEGY_RESEARCH_CACHE_PATH
    assert agent.research_cache.path != STRATEGY_PERF_CACHE_PATH


def test_selector_accepts_explicit_faithful_provenance_when_metrics_pass():
    from core.strategy_v2.selector import StrategySelector

    selector = StrategySelector(
        config={
            "require_profitable_performance": True,
            "min_sample_size": 30,
            "min_profit_factor": 1.2,
            "min_expectancy_R": 0.0,
            "max_drawdown_pct": 25.0,
            "required_validation_method": "faithful_v2_walk_forward",
        }
    )
    reason = selector._should_reject_for_validation(
        {
            "sample_size": 30,
            "profit_factor": 1.3,
            "expectancy_R": 0.1,
            "max_drawdown": 10.0,
            "validation_status": "validated",
            "validation_method": "faithful_v2_walk_forward",
            "last_run": datetime.now(timezone.utc).isoformat(),
        },
        90.0,
    )
    assert reason is None


def test_research_agent_defers_before_budget_gate_blocks_provider_calls():
    class FakeCache:
        def __init__(self):
            self.rows = []

        def get_entry(self, *_args):
            return None

        def upsert_entry(self, *args):
            self.rows.append(args)

    class FakeBacktest:
        def __init__(self):
            self.calls = 0

        def run_v2_walk_forward(self, **_kwargs):
            budget = get_request_context().external_budget
            assert budget.reserve("binance") is True
            self.calls += 1
            return {"windows": []}

    strategy_v2 = {
        "research_agent": {
            "enabled": True,
            "max_symbols_per_cycle": 1,
            "selection_days": 90,
            "walk_forward_train_days": 20,
            "walk_forward_test_days": 10,
            "max_windows": 4,
        },
        "enabled_strategies_by_asset_class": {
            "CRYPTO_FUTURES": ["S2_BREAKOUT_RETEST", "S3_VOL_SQUEEZE"],
        },
        "costs_by_asset": {"CRYPTO_FUTURES": {}},
        "performance_ttl_seconds": {"DEFAULT": 0},
    }
    manager = SimpleNamespace(
        config={},
        strategy_v2=strategy_v2,
        watchlist={},
        reload_if_changed=lambda: None,
        reload_watchlist_if_changed=lambda: None,
    )
    agent = StrategyResearchAgent.__new__(StrategyResearchAgent)
    agent.config_manager = manager
    agent.backtest_agent = FakeBacktest()
    agent.research_cache = FakeCache()

    budget = ExternalCallBudget(2)
    with request_context(
        reason="AGENT_REFRESH",
        path="/agent/strategy-research",
        agent_name="strategy_research",
        external_budget=budget,
    ):
        result = agent.run_cycle(force=True, symbols=["BTC-USD"])

    assert agent.backtest_agent.calls == 2
    assert result["external_budget_deferred"] is True
    assert result["skipped_external_budget"] == 1
    assert budget.snapshot()["used"] == 2
    assert budget.snapshot()["blocked"] == 0
