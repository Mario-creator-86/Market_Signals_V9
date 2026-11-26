"""Phase 14 golden tests — freeze the §77 lifecycle and the §111 journey.

test_gold111 freezes the full deterministic research cycle (weakness →
hypothesis → governed experiment → evaluation → SHADOW/REJECTED, no orders,
no promotion, no risk change); test_gold077 freezes the nine-status
experiment lifecycle so autonomous development cannot silently weaken it.
"""
import pytest

from core.research_factory.experiments import (
    ExperimentError, ExperimentRegistry, ExperimentStatus,
)
from core.research_factory.hypothesis import HypothesisStore, draft_hypothesis
from core.research_factory.pipeline import run_research_cycle
from core.research_factory.weakness import WeaknessObservation, detect_weaknesses

pytestmark = pytest.mark.golden


def _trades():
    return [
        {"strategy_type": "GOLD14", "direction": "LONG", "net_pnl": -25.0,
         "invested_amount": 1000.0, "closed_at": f"2026-08-{i + 1:02d}T10:00:00+00:00"}
        for i in range(14)
    ]


def test_gold111_research_journey_frozen(tmp_path):
    store = HypothesisStore(tmp_path / "h.json")
    registry = ExperimentRegistry(tmp_path / "e.json", hypothesis_store=store)

    def validator(observation, hypothesis):
        return {"verdict": "VALIDATED", "actor": "quant_validator",
                "evidence": {"oos_expectancy_delta": 0.5, "sample": 33}}

    result = run_research_cycle(
        trades=_trades(), hypothesis_store=store, experiment_registry=registry,
        evaluator=validator,
    )
    # Deterministic facts frozen:
    assert result["weaknesses_detected"] == 2  # expectancy_pct + win_rate
    assert len(result["experiments"]) == 2
    assert {e["final_status"] for e in result["experiments"]} == {"SHADOW"}
    assert result["status_counts"] == {"SHADOW": 2}
    for created in result["experiments"]:
        experiment = registry.get(created["experiment_id"])
        statuses = [event["to_status"] for event in experiment.events]
        assert statuses == ["PROPOSED", "APPROVED_FOR_RESEARCH", "RUNNING", "VALIDATED", "SHADOW"]
        hypothesis = store.get(experiment.hypothesis_id)
        # §76 completeness of the auto-drafted hypothesis
        assert hypothesis.promotion_criteria and hypothesis.failure_criteria
        assert hypothesis.horizon == "SWING"
        assert hypothesis.source_observation["metric"] == created["metric"]
    # §79 drift on a monotonically losing strategy with one window:
    drift_verdicts = {report["verdict"] for report in result["drift"]["reports"]}
    assert drift_verdicts <= {"INSUFFICIENT_SAMPLE", "STABLE", "DRIFT_SUSPECTED"}
    # Research-only guarantee text is part of the frozen contract:
    assert "no promotion" in result["notes"]


def test_gold077_experiment_lifecycle_frozen(tmp_path):
    store = HypothesisStore(tmp_path / "h.json")
    record = store.append(draft_hypothesis(
        problem="p", observation="o", hypothesis="h", expected_mechanism="m",
        baseline="b", challenger="c", universe="u", horizon="SWING", dataset="d",
        metrics=["oos_return"], promotion_criteria=["x"], failure_criteria=["y"],
    ))
    registry = ExperimentRegistry(tmp_path / "e.json", hypothesis_store=store)
    experiment = registry.propose(hypothesis_id=record.hypothesis_id,
                                  strategy_id="GOLD14", version="v1", description="d")
    identifier = experiment.experiment_id
    # Terminal states are absorbing; SHADOW may only go to PROMOTED/ROLLED_BACK.
    path = [
        (ExperimentStatus.APPROVED_FOR_RESEARCH, "research_director"),
        (ExperimentStatus.RUNNING, "experiment_planner"),
        (ExperimentStatus.FAILED, "system"),
    ]
    for status, actor in path:
        registry.transition(identifier, status, actor=actor)
    with pytest.raises(ExperimentError):
        registry.transition(identifier, ExperimentStatus.VALIDATED, actor="x")  # FAILED is terminal
    # A second experiment reaches PROMOTED only through the legal chain.
    second = registry.propose(hypothesis_id=record.hypothesis_id, strategy_id="GOLD14",
                              version="v2", description="d2")
    for status, actor in [
        (ExperimentStatus.APPROVED_FOR_RESEARCH, "research_director"),
        (ExperimentStatus.RUNNING, "experiment_planner"),
        (ExperimentStatus.VALIDATED, "quant_validator"),
        (ExperimentStatus.SHADOW, "research_director"),
        (ExperimentStatus.PROMOTED, "release_agent"),
        (ExperimentStatus.ROLLED_BACK, "release_agent"),
    ]:
        registry.transition(second.experiment_id, status, actor=actor)
    assert registry.get(second.experiment_id).status == "ROLLED_BACK"
    # The event log is immutable and append-only.
    events = registry.get(second.experiment_id).events
    assert [e["to_status"] for e in events] == [
        "PROPOSED", "APPROVED_FOR_RESEARCH", "RUNNING", "VALIDATED", "SHADOW",
        "PROMOTED", "ROLLED_BACK",
    ]


def test_gold111_weakness_facts_deterministic():
    observations = detect_weaknesses(_trades())
    facts = {(o.strategy_id, o.metric): (o.value, o.verdict) for o in observations}
    assert facts[("GOLD14", "expectancy_pct")] == (-2.5, "WEAKNESS")
    assert facts[("GOLD14", "win_rate")] == (0.0, "WEAKNESS")
