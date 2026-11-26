"""Phase 14 §111 — the research E2E journey as one deterministic pipeline.

weakness detected → hypothesis → experiment → evaluation (OOS/robustness,
via an injected deterministic evaluator over the Phase 7/8 seams) →
REJECT / SHADOW. Research-only by construction: the pipeline touches no
production execution, no risk, no autonomy; promotion stays gated by the
Phase 8 policy and completes in Phase 16.
"""
from __future__ import annotations

from math import isfinite
from typing import Any, Callable, Dict, List, Optional

from core.research_factory.drift import (
    DriftReport, detect_drift, revalidation_recommendations,
)
from core.research_factory.experiments import (
    ExperimentRegistry, ExperimentStatus,
)
from core.research_factory.hypothesis import (
    HypothesisError, HypothesisStore, draft_hypothesis,
)
from core.research_factory.tracking import JsonTracking, resolve_tracking
from core.research_factory.weakness import (
    WeaknessObservation, detect_weaknesses, hypothesis_seed_from_observation,
    load_closed_trades,
)

# §111 evaluator contract: (observation, hypothesis) -> {
#   verdict: "VALIDATED" | "REJECTED", evidence: {...}, actor: "quant_validator"
# }
Evaluator = Callable[[WeaknessObservation, Any], Dict[str, Any]]


def _tracking_call(errors: List[Dict[str, str]], operation: str,
                   callback: Callable[[], Any]) -> Any:
    """Keep optional experiment telemetry from changing research outcomes."""
    try:
        return callback()
    except Exception as exc:
        errors.append({
            "operation": operation,
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        })
        return None


def default_evaluator(observation: WeaknessObservation, hypothesis: Any) -> Dict[str, Any]:
    """Honest default: without caller-supplied deterministic backtest evidence
    the experiment cannot be validated — it is REJECTED for insufficient
    evidence, never guessed (§61 INSUFFICIENT_EVIDENCE is a valid outcome)."""
    return {
        "verdict": "REJECTED",
        "actor": "quant_validator",
        "evidence": {
            "reason": "no deterministic evaluator supplied",
            "outcome_class": "INSUFFICIENT_EVIDENCE",
        },
    }


def _finite_number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _max_drawdown(pnls: List[float]) -> float:
    equity = peak = 0.0
    drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def build_historical_evaluator(
    trades: List[Dict[str, Any]],
    *,
    min_total_sample: int = 40,
    min_oos_sample: int = 12,
) -> Evaluator:
    """Build a deterministic, chronological evaluator over closed outcomes.

    The challenger is an executable research policy: observe every signal in
    shadow, but admit exposure only while the preceding rolling outcome window
    is above the weakness threshold. This is evaluated without look-ahead and
    can only advance to SHADOW; Capital promotion remains outside this pipeline.
    """
    history = sorted(
        [dict(trade) for trade in trades or [] if isinstance(trade, dict)],
        key=lambda trade: str(trade.get("closed_at") or ""),
    )

    def evaluate(observation: WeaknessObservation, _hypothesis: Any) -> Dict[str, Any]:
        cohort = [
            trade for trade in history
            if str(trade.get("strategy_type") or trade.get("strategy_id") or "UNKNOWN").upper()
            == observation.strategy_id.upper()
            and str(trade.get("direction") or "").upper() == observation.direction.upper()
        ]
        sample = len(cohort)
        split = max(1, int(sample * 0.60))
        oos = cohort[split:]
        evidence: Dict[str, Any] = {
            "validation_method": "chronological_shadow_filter_v1",
            "strategy_id": observation.strategy_id,
            "direction": observation.direction,
            "metric": observation.metric,
            "threshold": observation.threshold,
            "sample_total": sample,
            "sample_train": split if sample else 0,
            "sample_oos": len(oos),
            "lookahead_used": False,
            "capital_promotion_allowed": False,
        }
        if sample < max(2, int(min_total_sample)) or len(oos) < max(1, int(min_oos_sample)):
            evidence.update({
                "reason": "chronological holdout sample is below the validation minimum",
                "outcome_class": "INSUFFICIENT_EVIDENCE",
            })
            return {"verdict": "REJECTED", "actor": "quant_validator", "evidence": evidence}

        pnls = [_finite_number(trade.get("net_pnl", trade.get("pnl"))) for trade in cohort]
        rolling_window = max(5, min(20, int(observation.window_trades or 20)))
        accepted: List[float] = []
        accepted_by_half = [[], []]
        for offset, pnl in enumerate(pnls[split:]):
            absolute_index = split + offset
            prior = pnls[max(0, absolute_index - rolling_window):absolute_index]
            if observation.metric == "win_rate":
                prior_metric = sum(1 for value in prior if value > 0) / len(prior)
            else:
                prior_trades = cohort[max(0, absolute_index - rolling_window):absolute_index]
                exposures = [
                    _finite_number(item.get("invested_amount"))
                    for item in prior_trades
                    if _finite_number(item.get("invested_amount")) > 0
                ]
                expectancy = sum(prior) / len(prior)
                avg_exposure = sum(exposures) / len(exposures) if exposures else 0.0
                prior_metric = expectancy / avg_exposure * 100.0 if avg_exposure > 0 else expectancy
            if prior_metric >= observation.threshold:
                accepted.append(pnl)
                accepted_by_half[0 if offset < len(oos) / 2 else 1].append(pnl)

        baseline = pnls[split:]
        baseline_expectancy = sum(baseline) / len(baseline)
        challenger_expectancy = sum(accepted) / len(accepted) if accepted else 0.0
        minimum_admitted = max(5, int(len(oos) * 0.25))
        stable_halves = all(
            values and (sum(values) / len(values)) >= baseline_expectancy
            for values in accepted_by_half
        )
        validated = (
            len(accepted) >= minimum_admitted
            and challenger_expectancy > baseline_expectancy
            and _max_drawdown(accepted) <= _max_drawdown(baseline)
            and stable_halves
        )
        evidence.update({
            "sample_challenger": len(accepted),
            "minimum_challenger_sample": minimum_admitted,
            "baseline_expectancy": round(baseline_expectancy, 6),
            "challenger_expectancy": round(challenger_expectancy, 6),
            "expectancy_delta": round(challenger_expectancy - baseline_expectancy, 6),
            "baseline_max_drawdown": round(_max_drawdown(baseline), 6),
            "challenger_max_drawdown": round(_max_drawdown(accepted), 6),
            "stable_across_oos_halves": stable_halves,
            "outcome_class": "VALIDATED_OOS" if validated else "NEGATIVE_OR_UNSTABLE_EDGE",
            "reason": (
                "chronological shadow filter improved expectancy without worsening drawdown"
                if validated else
                "challenger failed sample, expectancy, drawdown, or stability criteria"
            ),
        })
        return {
            "verdict": "VALIDATED" if validated else "REJECTED",
            "actor": "quant_validator",
            "evidence": evidence,
        }

    return evaluate


def run_research_cycle(
    *,
    trades: Optional[List[Dict[str, Any]]] = None,
    hypothesis_store: Optional[HypothesisStore] = None,
    experiment_registry: Optional[ExperimentRegistry] = None,
    evaluator: Optional[Evaluator] = None,
    max_experiments: int = 3,
    weakness_thresholds: Optional[Dict[str, Any]] = None,
    tracking: Optional[Any] = None,
) -> Dict[str, Any]:
    """One bounded §111 cycle. Every stage output is persisted/structured."""
    store = hypothesis_store or HypothesisStore()
    registry = experiment_registry or ExperimentRegistry(hypothesis_store=store)
    tracker = tracking or resolve_tracking()
    trades = load_closed_trades() if trades is None else trades
    evaluate: Evaluator = evaluator or build_historical_evaluator(trades)

    drift_reports: List[DriftReport] = detect_drift(trades)
    revalidations = revalidation_recommendations(drift_reports)
    observations = detect_weaknesses(trades, thresholds=weakness_thresholds)
    # Only WEAKNESS observations anchor hypotheses; INSUFFICIENT_SAMPLE is
    # reported, never acted on.
    actionable = [
        observation for observation in observations
        if observation.verdict == "WEAKNESS"
    ][:max(0, int(max_experiments))]

    created: List[Dict[str, Any]] = []
    tracking_errors: List[Dict[str, str]] = []
    for observation in actionable:
        seed = hypothesis_seed_from_observation(observation)
        try:
            hypothesis = draft_hypothesis(
                problem=seed["problem"],
                observation=seed["observation"],
                hypothesis=(
                    f"Reducing exposure for {observation.strategy_id} {observation.direction} "
                    f"when {observation.metric} deteriorates will improve net expectancy."
                ),
                expected_mechanism=(
                    f"{observation.metric}={observation.value} vs threshold "
                    f"{observation.threshold} over {observation.sample} trades suggests "
                    "the regime/parameters no longer fit; conditioning entries should "
                    "raise per-trade expectancy."
                ),
                baseline=f"{observation.strategy_id} current rules (v incumbent)",
                challenger="conditioned variant with the deteriorating-metric filter",
                universe="watchlist", horizon="SWING",
                dataset="closed-trade journal (research roles only)",
                metrics=["oos_expectancy", "max_drawdown", "stability"],
                promotion_criteria=[
                    "OOS expectancy improvement over baseline with locked period",
                    "drawdown not worse than baseline",
                    "sample >= 30 OOS trades",
                ],
                failure_criteria=[
                    "OOS expectancy not improved",
                    "insufficient OOS sample",
                ],
                risks=["overfitting to one window", "regime change invalidates filter"],
                created_by="hypothesis_agent",
                source_observation=seed["source_observation"],
            )
            store.append(hypothesis)
        except HypothesisError:
            continue

        experiment = registry.propose(
            hypothesis_id=hypothesis.hypothesis_id,
            strategy_id=observation.strategy_id,
            version="challenger-1",
            description=(
                f"Test challenger for {observation.strategy_id} {observation.direction} "
                f"weakness: {observation.metric}={observation.value}."
            ),
            created_by="experiment_planner",
        )
        registry.transition(
            experiment.experiment_id, ExperimentStatus.APPROVED_FOR_RESEARCH,
            actor="research_director", reason="structured weakness + complete hypothesis",
        )
        registry.transition(
            experiment.experiment_id, ExperimentStatus.RUNNING,
            actor="experiment_planner", reason="evaluation started",
        )
        run_id = _tracking_call(
            tracking_errors, "start_run",
            lambda: tracker.start_run(experiment.experiment_id, "evaluation"),
        )
        try:
            result = evaluate(observation, hypothesis)
            if not isinstance(result, dict) or not str(result.get("verdict") or "").strip():
                raise ValueError("evaluator must return a verdict")
            if run_id:
                _tracking_call(
                    tracking_errors, "log_params",
                    lambda: tracker.log_params(run_id, {
                        "strategy_id": observation.strategy_id,
                        "metric": observation.metric,
                    }),
                )
                _tracking_call(
                    tracking_errors, "log_metrics",
                    lambda: tracker.log_metrics(run_id, dict(result.get("evidence") or {})),
                )
                _tracking_call(
                    tracking_errors, "finish",
                    lambda: tracker.finish(run_id, str(result["verdict"])),
                )
            verdict = str(result["verdict"]).upper()
            if verdict == "VALIDATED":
                registry.transition(
                    experiment.experiment_id, ExperimentStatus.VALIDATED,
                    actor=result.get("actor", "quant_validator"),
                    reason="evaluation verdict VALIDATED", evidence={"evaluation": result.get("evidence")},
                )
                registry.transition(
                    experiment.experiment_id, ExperimentStatus.SHADOW,
                    actor="research_director",
                    reason="§111 handoff — shadow comparison (promotion is Phase 16)",
                    evidence={"shadow_started_by": "phase14_pipeline"},
                )
                final_status = ExperimentStatus.SHADOW.value
            else:
                registry.transition(
                    experiment.experiment_id, ExperimentStatus.REJECTED,
                    actor=result.get("actor", "quant_validator"),
                    reason=f"evaluation verdict {verdict}",
                    evidence={"evaluation": result.get("evidence")},
                )
                final_status = ExperimentStatus.REJECTED.value
        except Exception as exc:
            failure = f"{type(exc).__name__}: {str(exc)[:500]}"
            if run_id:
                _tracking_call(
                    tracking_errors, "finish_failed",
                    lambda: tracker.finish(run_id, ExperimentStatus.FAILED.value),
                )
            current = registry.get(experiment.experiment_id)
            if current and current.status == ExperimentStatus.RUNNING.value:
                current = registry.transition(
                    experiment.experiment_id, ExperimentStatus.FAILED,
                    actor="research_director", reason="evaluation pipeline failed",
                    evidence={"pipeline_error": failure},
                )
            elif current and current.status == ExperimentStatus.VALIDATED.value:
                current = registry.transition(
                    experiment.experiment_id, ExperimentStatus.REJECTED,
                    actor="research_director", reason="research handoff failed",
                    evidence={"pipeline_error": failure},
                )
            final_status = current.status if current else ExperimentStatus.FAILED.value
        created.append({
            "experiment_id": experiment.experiment_id,
            "hypothesis_id": hypothesis.hypothesis_id,
            "strategy_id": observation.strategy_id,
            "metric": observation.metric,
            "final_status": final_status,
        })

    evidence = tracker.evidence() if isinstance(tracker, JsonTracking) else {}
    return {
        "weaknesses_detected": len(actionable),
        "observations_total": len(observations),
        "experiments": created,
        "drift": {
            "reports": [report.to_dict() for report in drift_reports],
            "revalidations": revalidations,
        },
        "status_counts": registry.status_counts(),
        "tracking_evidence": evidence,
        "tracking_errors": tracking_errors,
        "notes": "research-only: no orders, no risk changes, no promotion (Phase 16)",
    }


__all__ = ["run_research_cycle", "default_evaluator", "build_historical_evaluator"]
