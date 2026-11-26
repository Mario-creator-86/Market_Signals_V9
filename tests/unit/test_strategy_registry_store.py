import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.strategy_registry import (
    PromotionEvidence,
    StrategyRecord,
    StrategyStatus,
)
from core.strategy_registry.store import (
    DuplicateStrategyError,
    InvalidTransitionError,
    PromotionRejectedError,
    StrategyRegistry,
    StrategyRegistryCorruptionError,
)


NOW = "2026-08-13T07:00:00+00:00"


def _record(strategy_id="S1", version="1.0.0", status=StrategyStatus.RESEARCH, universe=("US_STOCKS",)):
    return StrategyRecord(
        strategy_id=strategy_id,
        version=version,
        description=f"{strategy_id} description",
        features=("rsi", "atr"),
        entry_rules="enter on confirmed setup",
        exit_rules="stop and target",
        parameters={"risk": 1.0},
        horizon="SWING",
        universe=universe,
        created_by="test",
        created_at=NOW,
        status=status,
    )


def _evidence(**changes):
    values = {
        "oos_verdict": "ROBUST",
        "oos_sample_size": 50,
        "oos_expectancy_r": 0.30,
        "oos_profit_factor": 1.50,
        "oos_max_drawdown_pct": 10.0,
        "oos_sharpe": 1.2,
        "parameter_stable": True,
        "costs_included": True,
        "regime_count": 3,
        "recent_sample_size": 20,
        "recent_expectancy_r": 0.20,
        "shadow_sample_size": 30,
        "shadow_expectancy_r": 0.25,
        "shadow_max_drawdown_pct": 8.0,
        "final_holdout_untouched": True,
        "evaluation_period_locked": True,
        "promotion_criteria_locked": True,
        "generated_at": NOW,
    }
    values.update(changes)
    return PromotionEvidence(**values)


@pytest.fixture
def registry(tmp_path):
    counter = iter(range(1, 100))
    return StrategyRegistry(
        tmp_path / "strategy_registry.json",
        now_provider=lambda: NOW,
        id_provider=lambda: f"evt-{next(counter)}",
    )


def _to_shadow(registry, record):
    registry.register(record)
    registry.transition(record.key, StrategyStatus.CHALLENGER, actor="test", reason="research_passed")
    return registry.transition(record.key, StrategyStatus.SHADOW, actor="test", reason="oos_passed")


def test_missing_registry_is_empty_and_does_not_write(registry):
    assert registry.list_records() == []
    assert registry.history() == []
    assert not registry.path.exists()


def test_register_persists_research_record_and_audit_event(registry):
    record = registry.register(_record(version="vNext"))
    reloaded = StrategyRegistry(registry.path)

    assert reloaded.get("s1", "vNext") == record
    assert reloaded.get(record.key) == record
    assert reloaded.history()[0].action == "REGISTER"
    assert reloaded.history()[0].to_status is StrategyStatus.RESEARCH
    payload = json.loads(registry.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1


def test_register_rejects_duplicate_and_direct_non_research_status(registry):
    registry.register(_record())
    with pytest.raises(DuplicateStrategyError):
        registry.register(_record())
    with pytest.raises(InvalidTransitionError, match="RESEARCH"):
        registry.register(_record("S2", status=StrategyStatus.CHAMPION))


def test_corrupt_or_invalid_registry_fails_closed(tmp_path):
    path = tmp_path / "strategy_registry.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StrategyRegistryCorruptionError):
        StrategyRegistry(path).list_records()

    path.write_text(json.dumps({"schema_version": 99, "records": {}, "events": []}), encoding="utf-8")
    with pytest.raises(StrategyRegistryCorruptionError, match="schema"):
        StrategyRegistry(path).list_records()


def test_legal_transitions_work_and_champion_cannot_be_set_directly(registry):
    record = registry.register(_record())
    challenger = registry.transition(record.key, StrategyStatus.CHALLENGER, actor="research", reason="validated")
    shadow = registry.transition(record.key, StrategyStatus.SHADOW, actor="research", reason="oos_green")

    assert challenger.status is StrategyStatus.CHALLENGER
    assert shadow.status is StrategyStatus.SHADOW
    with pytest.raises(InvalidTransitionError):
        registry.transition(record.key, StrategyStatus.CHAMPION, actor="test", reason="one_backtest")
    with pytest.raises(InvalidTransitionError):
        registry.transition(record.key, StrategyStatus.APPROVED, actor="test", reason="bypass")


def test_bad_challenger_is_rejected_without_mutating_status(registry):
    record = _to_shadow(registry, _record())
    with pytest.raises(PromotionRejectedError) as error:
        registry.promote(record.key, _evidence(shadow_sample_size=1), actor="release")

    assert "shadow_sample_below_minimum" in error.value.decision.reasons
    assert registry.get("S1", "1.0.0").status is StrategyStatus.SHADOW
    assert not any(event.action == "PROMOTE" for event in registry.history())


def test_first_champion_promotion_is_atomic_and_audited(registry):
    record = _to_shadow(registry, _record())
    champion, decision = registry.promote(record.key, _evidence(), actor="release")

    assert decision.approved is True
    assert champion.status is StrategyStatus.CHAMPION
    assert registry.champion_for_scope(champion.selection_scope) == champion
    promote = [event for event in registry.history() if event.action == "PROMOTE"][-1]
    assert promote.details["evidence_fingerprint"] == decision.evidence_fingerprint
    assert promote.details["previous_champion"] is None


def test_promoting_replacement_demotes_old_champion_in_same_transaction(registry):
    first = _to_shadow(registry, _record("S1"))
    first_champion, _ = registry.promote(first.key, _evidence(), actor="release")
    second = _to_shadow(registry, _record("S2"))
    replacement_evidence = _evidence(
        oos_expectancy_r=0.40,
        shadow_expectancy_r=0.35,
        champion_oos_expectancy_r=0.30,
        champion_shadow_expectancy_r=0.25,
    )

    second_champion, _ = registry.promote(second.key, replacement_evidence, actor="release")

    assert second_champion.status is StrategyStatus.CHAMPION
    assert registry.get("S1", "1.0.0").status is StrategyStatus.APPROVED
    assert registry.champion_for_scope(second.selection_scope).key == second.key
    assert len(registry.list_records(status=StrategyStatus.CHAMPION)) == 1
    promote = [event for event in registry.history() if event.action == "PROMOTE"][-1]
    assert promote.details["previous_champion"] == first_champion.key


def test_degraded_champion_can_be_marked_but_not_silently_restored(registry):
    record = _to_shadow(registry, _record())
    champion, _ = registry.promote(record.key, _evidence(), actor="release")
    degraded = registry.transition(champion.key, StrategyStatus.DEGRADED, actor="monitor", reason="drawdown")

    assert degraded.status is StrategyStatus.DEGRADED
    assert registry.champion_for_scope(champion.selection_scope) is None
    with pytest.raises(InvalidTransitionError):
        registry.transition(champion.key, StrategyStatus.CHAMPION, actor="monitor", reason="looks_better")


def test_parallel_registers_do_not_lose_updates(tmp_path):
    path = tmp_path / "strategy_registry.json"

    def register(index):
        StrategyRegistry(path).register(_record(f"S{index}"))

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(register, range(1, 9)))

    assert len(StrategyRegistry(path).list_records()) == 8


def test_registry_has_no_delete_api(registry):
    assert not hasattr(registry, "delete")
    assert not hasattr(registry, "remove")


def test_governance_repair_is_narrow_and_audited(registry):
    record = registry.register(_record())
    registry.transition(
        record.key,
        StrategyStatus.CHALLENGER,
        actor="research_lifecycle_advancer",
        reason="bad proxy evidence",
    )

    repaired = registry.governance_repair_to_research(
        record.key,
        actor="research_lifecycle_guard",
        reason="proxy evidence is not promotable",
    )

    assert repaired.status is StrategyStatus.RESEARCH
    event = registry.history()[-1]
    assert event.action == "GOVERNANCE_REPAIR"
    assert event.from_status is StrategyStatus.CHALLENGER
    with pytest.raises(InvalidTransitionError):
        registry.governance_repair_to_research(
            record.key,
            actor="arbitrary_agent",
            reason="not authorized",
        )
