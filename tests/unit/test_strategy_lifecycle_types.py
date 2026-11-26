from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone

import pytest

from core.strategy_registry import (
    PromotionDecision,
    PromotionEvidence,
    PromotionThresholds,
    StrategyEvent,
    StrategyRecord,
    StrategyStatus,
)


NOW = "2026-08-13T07:00:00+00:00"


def _record(**changes):
    values = {
        "strategy_id": "S1_TREND_PULLBACK",
        "version": "1.0.0",
        "description": "Trend pullback",
        "features": ("ema20", "adx"),
        "entry_rules": "pullback and confirmation",
        "exit_rules": "stop below swing; target 2R",
        "parameters": {"US_STOCKS": {"adx_min": 20}},
        "horizon": "SWING",
        "universe": ("US_STOCKS",),
        "created_by": "v7_migration",
        "created_at": NOW,
        "status": StrategyStatus.RESEARCH,
    }
    values.update(changes)
    return StrategyRecord(**values)


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


def test_strategy_record_contains_all_section_62_fields_and_scope():
    expected = {
        "strategy_id", "version", "description", "features", "entry_rules", "exit_rules",
        "parameters", "horizon", "universe", "created_by", "created_at", "status",
    }
    record = _record()

    assert {item.name for item in fields(StrategyRecord)} == expected
    assert record.selection_scope == "SWING|US_STOCKS"
    assert record.key == "S1_TREND_PULLBACK@1.0.0"


def test_strategy_record_is_frozen_deeply_and_round_trips():
    record = _record()
    with pytest.raises(FrozenInstanceError):
        record.status = StrategyStatus.CHAMPION
    with pytest.raises(TypeError):
        record.parameters["US_STOCKS"] = {}
    with pytest.raises(TypeError):
        record.parameters["US_STOCKS"]["adx_min"] = 10

    assert StrategyRecord.from_dict(record.to_dict()) == record


@pytest.mark.parametrize(
    "changes",
    [
        {"strategy_id": ""},
        {"version": ""},
        {"features": ()},
        {"entry_rules": ""},
        {"exit_rules": ""},
        {"horizon": "INTRADAY_UNKNOWN"},
        {"universe": ()},
        {"created_at": "not-a-time"},
        {"status": "INVALID"},
    ],
)
def test_strategy_record_rejects_invalid_identity(changes):
    with pytest.raises((TypeError, ValueError)):
        _record(**changes)


def test_promotion_evidence_validates_and_round_trips():
    evidence = _evidence()
    assert PromotionEvidence.from_dict(evidence.to_dict()) == evidence
    with pytest.raises(ValueError, match="finite"):
        _evidence(oos_sharpe=float("nan"))
    with pytest.raises(ValueError, match="non-negative"):
        _evidence(shadow_sample_size=-1)
    with pytest.raises(ValueError, match="oos_verdict"):
        _evidence(oos_verdict="PENDING")


def test_thresholds_are_frozen_and_validate():
    thresholds = PromotionThresholds()
    assert thresholds.min_oos_sample == 24
    assert thresholds.min_shadow_sample == 24
    with pytest.raises(FrozenInstanceError):
        thresholds.min_oos_sample = 1
    with pytest.raises(ValueError):
        PromotionThresholds(min_oos_sample=0)


def test_decision_and_event_are_frozen_serializable_audit_values():
    decision = PromotionDecision(
        approved=False,
        reasons=("shadow_sample_below_minimum",),
        checks={"shadow_sample": False},
        evidence_fingerprint="a" * 64,
    )
    event = StrategyEvent(
        event_id="evt-1",
        occurred_at=NOW,
        action="TRANSITION",
        strategy_id="S1_TREND_PULLBACK",
        version="1.0.0",
        from_status=StrategyStatus.RESEARCH,
        to_status=StrategyStatus.CHALLENGER,
        details={"actor": "test"},
    )

    assert decision.checks["shadow_sample"] is False
    assert StrategyEvent.from_dict(event.to_dict()) == event
    with pytest.raises(TypeError):
        event.details["actor"] = "changed"


def test_strategy_status_set_matches_section_62():
    assert {item.value for item in StrategyStatus} == {
        "RESEARCH", "CHALLENGER", "SHADOW", "APPROVED", "CHAMPION", "DEGRADED", "RETIRED"
    }
