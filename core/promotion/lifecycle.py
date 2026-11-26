"""Phase 16 §85/§86/§87 — shadow comparison, evidence-gated promotion, rollback.

* Shadow (§85): a challenger's decisions are compared against the champion's
  under identical point-in-time conditions (same inputs digest), without
  touching capital.
* Promotion (§86): predefined evidence only — the Phase 8 `evaluate_promotion`
  gates run unchanged; nobody (AI included) can invent a weaker threshold
  after seeing results.
* Rollback (§87 + GOLD-017): when a recorded regression condition breaches,
  the last known valid champion is restored (experiment → ROLLED_BACK,
  promotion record → ROLLED_BACK). Deterministic; risk limits are never
  touched (§48/GOLD-016).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.promotion.records import (
    PromotionRecord, PromotionRecordError, PromotionRecordStore,
)
from core.research_factory.experiments import (
    ExperimentRegistry, ExperimentStatus,
)

# §85 comparison contract
@dataclass(frozen=True)
class ShadowComparison:
    experiment_id: str
    strategy_id: str
    champion_decisions: int
    challenger_decisions: int
    agreement_rate: Optional[float]
    champion_outcome: Optional[str]
    challenger_outcome: Optional[str]
    identical_inputs: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "experiment_id": self.experiment_id, "strategy_id": self.strategy_id,
            "champion_decisions": self.champion_decisions,
            "challenger_decisions": self.challenger_decisions,
            "agreement_rate": self.agreement_rate,
            "champion_outcome": self.champion_outcome,
            "challenger_outcome": self.challenger_outcome,
            "identical_inputs": self.identical_inputs,
        }


def compare_shadow(
    *,
    experiment_id: str,
    strategy_id: str,
    champion_runs: List[Dict[str, Any]],
    challenger_runs: List[Dict[str, Any]],
) -> ShadowComparison:
    """§85 — champion vs challenger under identical conditions.

    Runs are {inputs_sha256, action, outcome?} records; only pairs with the
    SAME inputs digest are comparable (identical conditions by construction).
    """
    champion_by_input = {
        str(run.get("inputs_sha256")): run for run in champion_runs or []
    }
    paired = [
        (champion_by_input[str(run.get("inputs_sha256"))], run)
        for run in challenger_runs or []
        if str(run.get("inputs_sha256")) in champion_by_input
    ]
    agreement = (
        sum(1 for champion, challenger in paired
            if str(champion.get("action")) == str(challenger.get("action")))
        / len(paired)
    ) if paired else None
    return ShadowComparison(
        experiment_id=str(experiment_id), strategy_id=str(strategy_id),
        champion_decisions=len(champion_runs or []),
        challenger_decisions=len(challenger_runs or []),
        agreement_rate=round(agreement, 6) if agreement is not None else None,
        champion_outcome=_majority_outcome([c for c, _ in paired]),
        challenger_outcome=_majority_outcome([ch for _, ch in paired]),
        identical_inputs=bool(paired),
    )


def _majority_outcome(runs: List[Dict[str, Any]]) -> Optional[str]:
    outcomes = [str(run.get("outcome")).upper() for run in runs if run.get("outcome")]
    if not outcomes:
        return None
    return max(set(outcomes), key=outcomes.count)


def promote_from_shadow(
    *,
    experiment_id: str,
    evidence_evaluator,          # () -> PromotionDecision (Phase 8 policy)
    promotion_store: PromotionRecordStore,
    experiment_registry: ExperimentRegistry,
    previous_version: str,
    new_version: str,
    promotion_evidence: Dict[str, Any],
    rollback_conditions: List[Dict[str, Any]],
    actor: str = "release_agent",
) -> Dict[str, Any]:
    """§86 — promote ONLY through the Phase 8 evidence gates.

    The caller injects the Phase 8 `evaluate_promotion(...)` decision; a
    rejected decision aborts without any state change (GOLD-015 semantics).
    """
    experiment = experiment_registry.get(experiment_id)
    if experiment is None:
        raise PromotionRecordError(f"unknown experiment_id: {experiment_id}")
    if experiment.status != ExperimentStatus.SHADOW.value:
        raise PromotionRecordError(
            f"experiment is {experiment.status}; only SHADOW experiments can be promoted"
        )
    decision = evidence_evaluator()
    if not bool(getattr(decision, "approved", False)):
        return {
            "promoted": False,
            "reason": "promotion gates rejected the evidence (§86)",
            "decision": getattr(decision, "to_dict", lambda: {})(),
        }
    record = promotion_store.append(
        experiment_id=experiment_id, strategy_id=experiment.strategy_id,
        previous_version=previous_version, new_version=new_version,
        promotion_evidence={
            **(promotion_evidence or {}),
            "decision": getattr(decision, "to_dict", lambda: {})(),
        },
        rollback_conditions=rollback_conditions,
    )
    experiment_registry.transition(
        experiment_id, ExperimentStatus.PROMOTED, actor=actor,
        reason="§86 evidence-gated promotion", evidence={"promotion_id": record.promotion_id},
    )
    return {"promoted": True, "promotion": record.to_dict()}


def _condition_breached(condition: Dict[str, Any], metrics: Dict[str, Any]) -> Optional[str]:
    kind = condition.get("kind")
    threshold = float(condition.get("threshold", 0.0))
    window = int(condition.get("window", 0) or 0)
    series = metrics.get(str(kind))  # metrics keyed by condition kind
    values = series if isinstance(series, list) else ([series] if series is not None else [])
    recent = values[-window:] if window else values
    if not recent:
        return None
    try:
        numeric = [float(v) for v in recent]
    except (TypeError, ValueError):
        return None
    if kind == "expectancy_r_below":
        if all(v < threshold for v in numeric):
            return f"expectancy_r {numeric} < {threshold}"
    elif kind == "profit_factor_below":
        if all(v < threshold for v in numeric):
            return f"profit_factor {numeric} < {threshold}"
    elif kind == "drawdown_above":
        if all(v > threshold for v in numeric):
            return f"drawdown {numeric} > {threshold}"
    elif kind == "sample_minimum":
        if sum(numeric) < threshold:
            return f"sample {sum(numeric)} < {threshold}"
    return None


def evaluate_rollback(
    promotion_id: str,
    *,
    metrics: Dict[str, Any],
    promotion_store: PromotionRecordStore,
    experiment_registry: Optional[ExperimentRegistry] = None,
) -> Dict[str, Any]:
    """§87/GOLD-017 — restore the last known valid champion on breach.

    Idempotent: an already-rolled-back promotion is returned unchanged.
    """
    record = promotion_store.get(promotion_id)
    if record is None:
        raise PromotionRecordError(f"unknown promotion_id: {promotion_id}")
    if record.status == "ROLLED_BACK":
        return {"rolled_back": True, "promotion_id": promotion_id,
                "reason": record.rollback_reason, "already": True}
    breaches = []
    for condition in record.rollback_conditions:
        breached = _condition_breached(condition, metrics)
        if breached:
            breaches.append({"condition": condition, "detail": breached})
    if not breaches:
        return {"rolled_back": False, "promotion_id": promotion_id, "breaches": []}
    updated = promotion_store.mark_rolled_back(
        promotion_id, reason="; ".join(b["detail"] for b in breaches),
    )
    if experiment_registry is not None and record.experiment_id:
        existing = experiment_registry.get(record.experiment_id)
        if existing is not None and existing.status == ExperimentStatus.PROMOTED.value:
            experiment_registry.transition(
                record.experiment_id, ExperimentStatus.ROLLED_BACK,
                actor="rollback_monitor",
                reason="§87 configured regression threshold breached",
                evidence={"breaches": breaches, "restored_version": record.previous_version},
            )
    return {
        "rolled_back": True, "promotion_id": promotion_id,
        "reason": updated.rollback_reason, "breaches": breaches,
        "restored_version": record.previous_version,
        "risk_limits_changed": False,
    }


__all__ = [
    "ShadowComparison", "compare_shadow", "promote_from_shadow",
    "evaluate_rollback",
]
