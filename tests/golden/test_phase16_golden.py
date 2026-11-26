"""Phase 16 golden tests — GOLD-017 rollback + §86 gate freeze."""
import pytest

from core.promotion.lifecycle import evaluate_rollback, promote_from_shadow
from core.promotion.records import PromotionRecordStore
from core.research_factory.experiments import ExperimentRegistry, ExperimentStatus
from core.research_factory.hypothesis import HypothesisStore, draft_hypothesis

pytestmark = pytest.mark.golden


class _Approved:
    approved = True
    reasons = ()
    checks = {"all": True}
    evidence_fingerprint = "2" * 64

    def to_dict(self):
        return {"approved": True}


def _promoted_stack(tmp_path):
    store = HypothesisStore(tmp_path / "h.json")
    record = store.append(draft_hypothesis(
        problem="p", observation="o", hypothesis="h", expected_mechanism="m",
        baseline="b", challenger="c", universe="u", horizon="SWING", dataset="d",
        metrics=["oos_return"], promotion_criteria=["x"], failure_criteria=["y"],
    ))
    registry = ExperimentRegistry(tmp_path / "e.json", hypothesis_store=store)
    experiment = registry.propose(hypothesis_id=record.hypothesis_id,
                                  strategy_id="GOLD16", version="v2", description="d")
    for status in (ExperimentStatus.APPROVED_FOR_RESEARCH, ExperimentStatus.RUNNING,
                   ExperimentStatus.VALIDATED, ExperimentStatus.SHADOW):
        registry.transition(experiment.experiment_id, status, actor="pipeline")
    promotions = PromotionRecordStore(tmp_path / "p.json")
    result = promote_from_shadow(
        experiment_id=experiment.experiment_id, evidence_evaluator=lambda: _Approved(),
        promotion_store=promotions, experiment_registry=registry,
        previous_version="v1", new_version="v2",
        promotion_evidence={"oos_verdict": "ROBUST"},
        rollback_conditions=[
            {"kind": "expectancy_r_below", "threshold": 0.0, "window": 3},
            {"kind": "drawdown_above", "threshold": 15.0, "window": 3},
        ],
    )
    return promotions, registry, result["promotion"]["promotion_id"], experiment.experiment_id


def test_gold_017_automatic_rollback_restores_champion(tmp_path):
    promotions, registry, promotion_id, experiment_id = _promoted_stack(tmp_path)

    # Healthy metrics: no rollback.
    healthy = evaluate_rollback(
        promotion_id, metrics={"expectancy_r_below": [0.4, 0.5, 0.6],
                               "drawdown_above": [5.0, 4.0, 3.0]},
        promotion_store=promotions, experiment_registry=registry,
    )
    assert healthy["rolled_back"] is False

    # Regression breach: rollback fires, previous version restored, idempotent.
    breached = evaluate_rollback(
        promotion_id, metrics={"expectancy_r_below": [-0.2, -0.1, -0.3],
                               "drawdown_above": [5.0, 4.0, 3.0]},
        promotion_store=promotions, experiment_registry=registry,
    )
    assert breached["rolled_back"] is True
    assert breached["restored_version"] == "v1"
    assert breached["risk_limits_changed"] is False  # §87 never touches risk
    experiment = registry.get(experiment_id)
    assert experiment.status == "ROLLED_BACK"
    assert experiment.evidence["restored_version"] == "v1"
    record = promotions.get(promotion_id)
    assert record.status == "ROLLED_BACK" and record.rollback_reason

    # Idempotent second evaluation does not duplicate or fail.
    again = evaluate_rollback(
        promotion_id, metrics={"expectancy_r_below": [-1.0, -1.0, -1.0]},
        promotion_store=promotions, experiment_registry=registry,
    )
    assert again["rolled_back"] is True and again.get("already") is True
    assert len(promotions.list_records()) == 1


def test_gold086_gate_rejection_leaves_no_trace(tmp_path):
    store = HypothesisStore(tmp_path / "h.json")
    record = store.append(draft_hypothesis(
        problem="p", observation="o", hypothesis="h", expected_mechanism="m",
        baseline="b", challenger="c", universe="u", horizon="SWING", dataset="d",
        metrics=["oos_return"], promotion_criteria=["x"], failure_criteria=["y"],
    ))
    registry = ExperimentRegistry(tmp_path / "e.json", hypothesis_store=store)
    experiment = registry.propose(hypothesis_id=record.hypothesis_id,
                                  strategy_id="GOLD16B", version="v9", description="d")
    for status in (ExperimentStatus.APPROVED_FOR_RESEARCH, ExperimentStatus.RUNNING,
                   ExperimentStatus.VALIDATED, ExperimentStatus.SHADOW):
        registry.transition(experiment.experiment_id, status, actor="pipeline")

    class Rejected:
        approved = False
        reasons = ("oos_expectancy below threshold",)
        checks = {"oos_expectancy": False}
        evidence_fingerprint = "3" * 64

        def to_dict(self):
            return {"approved": False}

    promotions = PromotionRecordStore(tmp_path / "p.json")
    result = promote_from_shadow(
        experiment_id=experiment.experiment_id, evidence_evaluator=lambda: Rejected(),
        promotion_store=promotions, experiment_registry=registry,
        previous_version="v1", new_version="v9", promotion_evidence={},
        rollback_conditions=[{"kind": "expectancy_r_below", "threshold": 0.0}],
    )
    assert result["promoted"] is False
    assert promotions.list_records() == []
    assert registry.get(experiment.experiment_id).status == "SHADOW"
