"""Phase 16 — Shadow / Promotion / Rollback (§85/§86/§87, GOLD-017)."""
from core.promotion.lifecycle import (
    ShadowComparison, compare_shadow, evaluate_rollback, promote_from_shadow,
)
from core.promotion.records import (
    PromotionRecord, PromotionRecordError, PromotionRecordStore,
    ROLLBACK_CONDITION_KINDS, validate_rollback_condition,
)

__all__ = [
    "ShadowComparison", "compare_shadow", "promote_from_shadow", "evaluate_rollback",
    "PromotionRecord", "PromotionRecordStore", "PromotionRecordError",
    "ROLLBACK_CONDITION_KINDS", "validate_rollback_condition",
]
