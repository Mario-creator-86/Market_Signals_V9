from dataclasses import replace

import pytest

from core.strategy_registry import PromotionEvidence, PromotionThresholds
from core.strategy_registry.policy import evaluate_promotion, fingerprint_promotion_evidence


NOW = "2026-08-13T07:00:00+00:00"


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


def test_green_multifactor_evidence_is_approved_without_existing_champion():
    decision = evaluate_promotion(_evidence(), thresholds=PromotionThresholds())

    assert decision.approved is True
    assert decision.reasons == ()
    assert all(decision.checks.values())
    assert len(decision.evidence_fingerprint) == 64


def test_one_better_backtest_without_shadow_is_rejected():
    decision = evaluate_promotion(
        _evidence(shadow_sample_size=0, shadow_expectancy_r=0.0),
        thresholds=PromotionThresholds(),
    )

    assert decision.approved is False
    assert "shadow_sample_below_minimum" in decision.reasons
    assert "shadow_expectancy_not_positive" in decision.reasons


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"oos_verdict": "FRAGILE"}, "oos_verdict_not_promoting"),
        ({"oos_sample_size": 5}, "oos_sample_below_minimum"),
        ({"oos_profit_factor": 1.0}, "oos_profit_factor_below_minimum"),
        ({"oos_max_drawdown_pct": 25.0}, "oos_drawdown_exceeded"),
        ({"oos_sharpe": 0.1}, "oos_sharpe_below_minimum"),
        ({"parameter_stable": False}, "parameters_unstable"),
        ({"costs_included": False}, "costs_not_included"),
        ({"regime_count": 1}, "regime_coverage_below_minimum"),
        ({"recent_sample_size": 2}, "recent_sample_below_minimum"),
        ({"recent_expectancy_r": 0.0}, "recent_expectancy_not_positive"),
        ({"shadow_max_drawdown_pct": 25.0}, "shadow_drawdown_exceeded"),
        ({"final_holdout_untouched": False}, "final_holdout_was_optimized"),
        ({"evaluation_period_locked": False}, "evaluation_period_not_locked"),
        ({"promotion_criteria_locked": False}, "promotion_criteria_not_locked"),
    ],
)
def test_each_promotion_gate_rejects_independently(changes, reason):
    decision = evaluate_promotion(_evidence(**changes))
    assert decision.approved is False
    assert reason in decision.reasons


def test_existing_champion_requires_predefined_oos_and_shadow_improvement():
    rejected = evaluate_promotion(
        _evidence(champion_oos_expectancy_r=0.35, champion_shadow_expectancy_r=0.30),
        champion_exists=True,
    )
    approved = evaluate_promotion(
        _evidence(champion_oos_expectancy_r=0.20, champion_shadow_expectancy_r=0.20),
        champion_exists=True,
    )

    assert rejected.approved is False
    assert "challenger_does_not_beat_champion_oos" in rejected.reasons
    assert "challenger_does_not_beat_champion_shadow" in rejected.reasons
    assert approved.approved is True


def test_champion_comparison_cannot_be_omitted_when_champion_exists():
    decision = evaluate_promotion(_evidence(), champion_exists=True)
    assert decision.approved is False
    assert "champion_oos_comparison_missing" in decision.reasons
    assert "champion_shadow_comparison_missing" in decision.reasons


def test_evidence_fingerprint_is_canonical_and_policy_sensitive():
    evidence = _evidence()
    first = fingerprint_promotion_evidence(evidence, PromotionThresholds())
    second = fingerprint_promotion_evidence(PromotionEvidence.from_dict(evidence.to_dict()), PromotionThresholds())
    changed = fingerprint_promotion_evidence(evidence, PromotionThresholds(min_oos_sample=30))

    assert first == second
    assert first != changed
