"""Phase 16 unit tests — §85 shadow comparison and §86/§87 promotion records."""
import pytest

from core.promotion.lifecycle import compare_shadow, evaluate_rollback, promote_from_shadow
from core.promotion.records import (
    PromotionRecordError, PromotionRecordStore, validate_rollback_condition,
)
from core.research_factory.experiments import ExperimentRegistry, ExperimentStatus
from core.research_factory.hypothesis import HypothesisStore, draft_hypothesis


def _stores(tmp_path):
    store = HypothesisStore(tmp_path / "h.json")
    record = store.append(draft_hypothesis(
        problem="p", observation="o", hypothesis="h", expected_mechanism="m",
        baseline="b", challenger="c", universe="u", horizon="SWING", dataset="d",
        metrics=["oos_return"], promotion_criteria=["x"], failure_criteria=["y"],
    ))
    registry = ExperimentRegistry(tmp_path / "e.json", hypothesis_store=store)
    experiment = registry.propose(hypothesis_id=record.hypothesis_id,
                                  strategy_id="STRAT", version="v2", description="d")
    for status, actor in ((ExperimentStatus.APPROVED_FOR_RESEARCH, "rd"),
                          (ExperimentStatus.RUNNING, "ep"),
                          (ExperimentStatus.VALIDATED, "qv"),
                          (ExperimentStatus.SHADOW, "rd")):
        registry.transition(experiment.experiment_id, status, actor=actor)
    return store, registry, experiment.experiment_id


def test_shadow_comparison_pairs_identical_inputs_only():
    champion = [
        {"inputs_sha256": "aaa", "action": "OPEN_LONG", "outcome": "FAVORABLE"},
        {"inputs_sha256": "bbb", "action": "NO_TRADE", "outcome": "ADVERSE"},
    ]
    challenger = [
        {"inputs_sha256": "aaa", "action": "OPEN_LONG", "outcome": "FAVORABLE"},
        {"inputs_sha256": "zzz", "action": "OPEN_LONG", "outcome": "FAVORABLE"},
    ]
    comparison = compare_shadow(experiment_id="exp_1", strategy_id="STRAT",
                                champion_runs=champion, challenger_runs=challenger)
    assert comparison.identical_inputs is True
    assert comparison.agreement_rate == 1.0  # the one comparable pair agreed
    assert comparison.champion_decisions == 2 and comparison.challenger_decisions == 2


def test_shadow_comparison_without_common_inputs_is_honest():
    comparison = compare_shadow(experiment_id="exp", strategy_id="S",
                                champion_runs=[{"inputs_sha256": "x", "action": "A"}],
                                challenger_runs=[{"inputs_sha256": "y", "action": "A"}])
    assert comparison.agreement_rate is None
    assert comparison.identical_inputs is False


def test_rollback_condition_validation():
    assert validate_rollback_condition({"kind": "expectancy_r_below", "threshold": 0.0})["kind"] == "expectancy_r_below"
    with pytest.raises(PromotionRecordError):
        validate_rollback_condition({"kind": "vibes_bad", "threshold": 1})
    with pytest.raises(PromotionRecordError):
        validate_rollback_condition({"kind": "drawdown_above"})


def test_promotion_requires_shadow_experiment_and_valid_conditions(tmp_path):
    _, registry, experiment_id = _stores(tmp_path)
    promotions = PromotionRecordStore(tmp_path / "p.json")

    class Approved:
        approved = True
        reasons = ()
        checks = {}
        evidence_fingerprint = "0" * 64
        def to_dict(self):
            return {"approved": True}

    with pytest.raises(PromotionRecordError):  # rollback conditions mandatory (§87)
        promote_from_shadow(
            experiment_id=experiment_id, evidence_evaluator=lambda: Approved(),
            promotion_store=promotions, experiment_registry=registry,
            previous_version="v1", new_version="v2", promotion_evidence={},
            rollback_conditions=[],
        )
    result = promote_from_shadow(
        experiment_id=experiment_id, evidence_evaluator=lambda: Approved(),
        promotion_store=promotions, experiment_registry=registry,
        previous_version="v1", new_version="v2", promotion_evidence={"oos": "robust"},
        rollback_conditions=[{"kind": "expectancy_r_below", "threshold": 0.0, "window": 5}],
    )
    assert result["promoted"] is True
    assert registry.get(experiment_id).status == "PROMOTED"


def test_promotion_blocked_when_evidence_gates_reject(tmp_path):
    _, registry, experiment_id = _stores(tmp_path)
    promotions = PromotionRecordStore(tmp_path / "p.json")

    class Rejected:
        approved = False
        reasons = ("oos sample too small",)
        checks = {"oos_sample": False}
        evidence_fingerprint = "1" * 64
        def to_dict(self):
            return {"approved": False, "reasons": list(self.reasons)}

    result = promote_from_shadow(
        experiment_id=experiment_id, evidence_evaluator=lambda: Rejected(),
        promotion_store=promotions, experiment_registry=registry,
        previous_version="v1", new_version="v2", promotion_evidence={},
        rollback_conditions=[{"kind": "expectancy_r_below", "threshold": 0.0}],
    )
    assert result["promoted"] is False
    assert promotions.list_records() == []  # no state change on rejection (§86)
    assert registry.get(experiment_id).status == "SHADOW"
