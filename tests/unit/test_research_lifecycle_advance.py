from datetime import datetime, timezone

from core.research_factory.lifecycle_advance import _aggregate_by_strategy, advance_registry_lifecycles
from core.strategy_registry.store import StrategyRegistry
from core.strategy_registry.types import StrategyRecord, StrategyStatus


NOW = "2026-08-26T10:00:00+00:00"


def _record():
    return StrategyRecord(
        strategy_id="S1_TREND_PULLBACK",
        version="2.0.0",
        description="Trend pullback",
        features=("ema", "atr"),
        entry_rules="confirmed pullback",
        exit_rules="stop and target",
        parameters={},
        horizon="SWING",
        universe=("US_STOCKS",),
        created_by="test",
        created_at=NOW,
    )


def _row(symbol, *, method="faithful_v2_walk_forward", status="validated", sample=15):
    return {
        "strategy_id": "S1_TREND_PULLBACK",
        "symbol": symbol,
        "validation_method": method,
        "validation_status": status,
        "is_fresh": True,
        "sample_size": sample,
        "walk_forward": {"windows": 4, "avg_test_expectancy_R": 0.2},
        "last_run": datetime.now(timezone.utc).isoformat(),
    }


def test_proxy_research_rows_are_never_promotion_evidence():
    rows = [
        _row("AAPL", method="legacy_proxy_walk_forward", status="research_only", sample=100),
        _row("MSFT", method="legacy_proxy_walk_forward", status="research_only", sample=100),
        _row("NVDA", method="legacy_proxy_walk_forward", status="research_only", sample=100),
    ]

    assert _aggregate_by_strategy(rows) == {}


def test_faithful_positive_sample_rows_aggregate():
    aggregate = _aggregate_by_strategy([_row("AAPL"), _row("MSFT"), _row("NVDA")])

    evidence = aggregate["S1_TREND_PULLBACK"]
    assert evidence["wf_fresh_symbols"] == 3
    assert evidence["sample_size"] == 45


def test_invalid_automatic_challenger_is_repaired(monkeypatch, tmp_path):
    registry = StrategyRegistry(tmp_path / "registry.json")
    record = registry.register(_record())
    registry.transition(
        record.key,
        StrategyStatus.CHALLENGER,
        actor="research_lifecycle_advancer",
        reason="legacy proxy rows",
    )
    monkeypatch.setattr(
        "core.research_factory.lifecycle_advance._load_research_rows",
        lambda: [_row("AAPL", method="legacy_proxy_walk_forward", status="research_only", sample=0)],
    )

    transitions = advance_registry_lifecycles(registry)

    assert registry.get(record.key).status is StrategyStatus.RESEARCH
    assert transitions[0]["repair"] is True


def test_research_advances_only_after_faithful_sample_threshold(monkeypatch, tmp_path):
    registry = StrategyRegistry(tmp_path / "registry.json")
    record = registry.register(_record())
    monkeypatch.setattr(
        "core.research_factory.lifecycle_advance._load_research_rows",
        lambda: [_row("AAPL"), _row("MSFT"), _row("NVDA")],
    )

    transitions = advance_registry_lifecycles(registry)

    assert registry.get(record.key).status is StrategyStatus.CHALLENGER
    assert transitions[0]["to"] == "CHALLENGER"
