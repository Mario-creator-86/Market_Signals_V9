from dataclasses import replace

import pytest

from core.strategy_registry.optuna_policy import (
    OptunaStudyRequest,
    UnsafeOptunaStudyError,
    validate_optuna_study,
)


def _request(**changes):
    request = OptunaStudyRequest(
        study_name="s2_walk_forward_2026q3",
        objectives=("oos_return", "max_drawdown", "stability", "costs"),
        max_trials=120,
        timeout_seconds=3600,
        storage_uri="sqlite:///data/research/optuna.db",
        data_roles=("TRAIN", "VALIDATION", "OOS"),
    )
    return replace(request, **changes)


def test_safe_multi_objective_research_study_passes():
    request = _request()

    assert validate_optuna_study(request) is request
    assert request.objectives == ("oos_return", "max_drawdown", "stability", "costs")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"research_only": False}, "research-only"),
        ({"uses_final_holdout": True}, "final holdout"),
        ({"data_roles": ("TRAIN", "FINAL_HOLDOUT")}, "final holdout"),
        ({"auto_promote": True}, "auto-promote"),
        ({"evaluation_period_locked": False}, "locked"),
        ({"storage_uri": ""}, "persistent storage"),
        ({"storage_uri": "sqlite:///:memory:"}, "persistent storage"),
        ({"objectives": ("raw_profit",)}, "multiple objectives"),
        ({"objectives": ("oos_return", "sharpe")}, "risk, stability"),
        ({"objectives": ("max_drawdown", "costs")}, "performance"),
        ({"objectives": ("oos_return", "unknown_metric")}, "unsupported"),
        ({"max_trials": 0}, "trial budget"),
        ({"max_trials": 501}, "trial budget"),
        ({"timeout_seconds": 0}, "timeout budget"),
        ({"timeout_seconds": 86_401}, "timeout budget"),
    ],
)
def test_unsafe_study_requests_fail_closed(changes, message):
    with pytest.raises(UnsafeOptunaStudyError, match=message):
        validate_optuna_study(_request(**changes))


def test_raw_profit_may_not_be_the_only_performance_evidence():
    with pytest.raises(UnsafeOptunaStudyError, match="OOS/risk-adjusted performance"):
        validate_optuna_study(
            _request(objectives=("raw_profit", "max_drawdown", "stability"))
        )


def test_custom_policy_budget_is_enforced():
    with pytest.raises(UnsafeOptunaStudyError, match="trial budget"):
        validate_optuna_study(_request(max_trials=51), max_trial_budget=50)
