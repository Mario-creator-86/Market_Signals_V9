"""GOLD-062/063/064 strategy governance and research-search safety."""

import pytest

from core.strategy_registry import (
    InvalidTransitionError,
    OptunaStudyRequest,
    PromotionEvidence,
    PromotionRejectedError,
    StrategyRecord,
    StrategyRegistry,
    StrategyStatus,
    UnsafeOptunaStudyError,
    validate_optuna_study,
)


pytestmark = pytest.mark.golden
NOW = "2026-08-13T09:00:00+00:00"


def _record(strategy_id):
    return StrategyRecord(
        strategy_id=strategy_id,
        version="1.0.0",
        description="golden governed strategy",
        features=("rsi", "atr"),
        entry_rules="causal entry",
        exit_rules="predefined stop and target",
        parameters={"risk": 1.0},
        horizon="SWING",
        universe=("US_STOCKS",),
        created_by="golden",
        created_at=NOW,
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


def _shadow(registry, record):
    registry.register(record)
    registry.transition(record.key, StrategyStatus.CHALLENGER, actor="golden", reason="research")
    return registry.transition(record.key, StrategyStatus.SHADOW, actor="golden", reason="oos")


def test_gold062_registry_contract_and_direct_champion_are_pinned(tmp_path):
    expected_fields = {
        "strategy_id", "version", "description", "features", "entry_rules",
        "exit_rules", "parameters", "horizon", "universe", "created_by",
        "created_at", "status",
    }
    assert set(_record("S1").to_dict()) == expected_fields
    assert {status.value for status in StrategyStatus} == {
        "RESEARCH", "CHALLENGER", "SHADOW", "APPROVED",
        "CHAMPION", "DEGRADED", "RETIRED",
    }

    registry = StrategyRegistry(tmp_path / "registry.json")
    registered = registry.register(_record("S1"))
    with pytest.raises(InvalidTransitionError):
        registry.transition(registered.key, StrategyStatus.CHAMPION, actor="ai", reason="one run")


def test_gold063_bad_challenger_is_rejected_and_better_one_rotates_atomically(tmp_path):
    registry = StrategyRegistry(tmp_path / "registry.json")
    first = _shadow(registry, _record("S1"))
    first, _ = registry.promote(first.key, _evidence(), actor="release")

    second = _shadow(registry, _record("S2"))
    with pytest.raises(PromotionRejectedError):
        registry.promote(second.key, _evidence(shadow_sample_size=2), actor="release")
    assert registry.get(second.key).status is StrategyStatus.SHADOW
    assert registry.get(first.key).status is StrategyStatus.CHAMPION

    promoted, decision = registry.promote(
        second.key,
        _evidence(
            oos_expectancy_r=0.40,
            shadow_expectancy_r=0.35,
            champion_oos_expectancy_r=0.30,
            champion_shadow_expectancy_r=0.25,
        ),
        actor="release",
    )
    assert decision.approved is True
    assert promoted.status is StrategyStatus.CHAMPION
    assert registry.get(first.key).status is StrategyStatus.APPROVED
    assert registry.champion_for_scope(promoted.selection_scope) == promoted


def test_gold064_optuna_never_searches_holdout_or_auto_promotes():
    safe = OptunaStudyRequest(
        study_name="golden_safe_search",
        objectives=("oos_return", "max_drawdown", "stability", "costs"),
        max_trials=100,
        timeout_seconds=3600,
        storage_uri="sqlite:///data/research/optuna.db",
        data_roles=("TRAIN", "VALIDATION", "OOS"),
    )
    assert validate_optuna_study(safe) is safe

    with pytest.raises(UnsafeOptunaStudyError):
        validate_optuna_study(
            OptunaStudyRequest(
                study_name="unsafe_holdout_search",
                objectives=("raw_profit", "max_drawdown"),
                max_trials=100,
                timeout_seconds=3600,
                storage_uri="sqlite:///data/research/optuna.db",
                data_roles=("TRAIN", "FINAL_HOLDOUT"),
                uses_final_holdout=True,
                auto_promote=True,
            )
        )
