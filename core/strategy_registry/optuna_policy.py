"""Fail-closed Optuna research policy from V9 sections 18 and 64."""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_MAX_TRIALS = 500
DEFAULT_MAX_TIMEOUT_SECONDS = 86_400

PERFORMANCE_OBJECTIVES = frozenset({"oos_return", "sharpe", "sortino"})
RISK_OBJECTIVES = frozenset({"max_drawdown", "stability", "turnover", "costs"})
SUPPORTED_OBJECTIVES = PERFORMANCE_OBJECTIVES | RISK_OBJECTIVES | {"raw_profit"}
FINAL_HOLDOUT_ROLES = frozenset({"FINAL_HOLDOUT", "HOLDOUT_FINAL", "UNTOUCHED_HOLDOUT"})


class UnsafeOptunaStudyError(ValueError):
    """Raised when a requested study violates locked research policy."""


@dataclass(frozen=True)
class OptunaStudyRequest:
    study_name: str
    objectives: tuple[str, ...]
    max_trials: int
    timeout_seconds: int
    storage_uri: str
    data_roles: tuple[str, ...]
    research_only: bool = True
    uses_final_holdout: bool = False
    auto_promote: bool = False
    evaluation_period_locked: bool = True

    def __post_init__(self) -> None:
        name = str(self.study_name or "").strip()
        objectives = tuple(
            dict.fromkeys(
                str(objective or "").strip().lower()
                for objective in self.objectives
                if str(objective or "").strip()
            )
        )
        roles = tuple(
            dict.fromkeys(
                str(role or "").strip().upper()
                for role in self.data_roles
                if str(role or "").strip()
            )
        )
        storage = str(self.storage_uri or "").strip()
        if not name:
            raise ValueError("study_name is required")
        if not objectives:
            raise ValueError("at least one objective is required")
        if not roles:
            raise ValueError("at least one data role is required")
        object.__setattr__(self, "study_name", name)
        object.__setattr__(self, "objectives", objectives)
        object.__setattr__(self, "storage_uri", storage)
        object.__setattr__(self, "data_roles", roles)


def validate_optuna_study(
    request: OptunaStudyRequest,
    *,
    max_trial_budget: int = DEFAULT_MAX_TRIALS,
    max_timeout_seconds: int = DEFAULT_MAX_TIMEOUT_SECONDS,
) -> OptunaStudyRequest:
    """Validate a proposed study before an Optuna study is created."""

    if not isinstance(request, OptunaStudyRequest):
        raise TypeError("request must be an OptunaStudyRequest")
    if not isinstance(max_trial_budget, int) or max_trial_budget <= 0:
        raise ValueError("max_trial_budget must be a positive integer")
    if not isinstance(max_timeout_seconds, int) or max_timeout_seconds <= 0:
        raise ValueError("max_timeout_seconds must be a positive integer")
    if request.research_only is not True:
        raise UnsafeOptunaStudyError("Optuna studies must be research-only")
    if request.uses_final_holdout or FINAL_HOLDOUT_ROLES.intersection(request.data_roles):
        raise UnsafeOptunaStudyError("Optuna must never optimize the final holdout")
    if request.auto_promote:
        raise UnsafeOptunaStudyError("the best Optuna trial cannot auto-promote")
    if request.evaluation_period_locked is not True:
        raise UnsafeOptunaStudyError("the evaluation period must be locked before search")
    storage_lower = request.storage_uri.lower()
    if not request.storage_uri or storage_lower in {
        "memory", ":memory:", "sqlite:///:memory:"
    }:
        raise UnsafeOptunaStudyError("every Optuna study requires persistent storage")
    if len(request.objectives) < 2:
        raise UnsafeOptunaStudyError("Optuna research requires multiple objectives")
    unsupported = sorted(set(request.objectives) - SUPPORTED_OBJECTIVES)
    if unsupported:
        raise UnsafeOptunaStudyError(f"unsupported Optuna objectives: {', '.join(unsupported)}")
    if not PERFORMANCE_OBJECTIVES.intersection(request.objectives):
        if "raw_profit" in request.objectives:
            raise UnsafeOptunaStudyError(
                "raw profit needs OOS/risk-adjusted performance evidence"
            )
        raise UnsafeOptunaStudyError("at least one performance objective is required")
    if not RISK_OBJECTIVES.intersection(request.objectives):
        raise UnsafeOptunaStudyError(
            "at least one risk, stability, turnover, or costs objective is required"
        )
    if not isinstance(request.max_trials, int) or not 1 <= request.max_trials <= max_trial_budget:
        raise UnsafeOptunaStudyError(
            f"trial budget must be between 1 and {max_trial_budget}"
        )
    if (
        not isinstance(request.timeout_seconds, int)
        or not 1 <= request.timeout_seconds <= max_timeout_seconds
    ):
        raise UnsafeOptunaStudyError(
            f"timeout budget must be between 1 and {max_timeout_seconds} seconds"
        )
    return request


__all__ = [
    "DEFAULT_MAX_TIMEOUT_SECONDS",
    "DEFAULT_MAX_TRIALS",
    "FINAL_HOLDOUT_ROLES",
    "OptunaStudyRequest",
    "PERFORMANCE_OBJECTIVES",
    "RISK_OBJECTIVES",
    "SUPPORTED_OBJECTIVES",
    "UnsafeOptunaStudyError",
    "validate_optuna_study",
]
