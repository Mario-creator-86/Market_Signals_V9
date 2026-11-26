"""Persistent V9 strategy lifecycle and champion/challenger governance."""

from .types import (
    PromotionDecision,
    PromotionEvidence,
    PromotionThresholds,
    StrategyEvent,
    StrategyRecord,
    StrategyStatus,
)
from .policy import evaluate_promotion, fingerprint_promotion_evidence
from .store import (
    DEFAULT_REGISTRY_PATH,
    DuplicateStrategyError,
    InvalidTransitionError,
    PromotionRejectedError,
    StrategyNotFoundError,
    StrategyRegistry,
    StrategyRegistryCorruptionError,
    StrategyRegistryError,
)
from .bridge import V2_LIFECYCLE_VERSION, record_from_v2_spec, seed_v2_registry
from .optuna_policy import (
    OptunaStudyRequest,
    UnsafeOptunaStudyError,
    validate_optuna_study,
)

__all__ = [
    "PromotionDecision",
    "PromotionEvidence",
    "PromotionThresholds",
    "StrategyEvent",
    "StrategyRecord",
    "StrategyStatus",
    "evaluate_promotion",
    "fingerprint_promotion_evidence",
    "DEFAULT_REGISTRY_PATH",
    "DuplicateStrategyError",
    "InvalidTransitionError",
    "PromotionRejectedError",
    "StrategyNotFoundError",
    "StrategyRegistry",
    "StrategyRegistryCorruptionError",
    "StrategyRegistryError",
    "V2_LIFECYCLE_VERSION",
    "record_from_v2_spec",
    "seed_v2_registry",
    "OptunaStudyRequest",
    "UnsafeOptunaStudyError",
    "validate_optuna_study",
]
