"""Phase 14 unit tests — hypothesis/experiment/weakness/drift contracts."""
import pytest

from core.research_factory.drift import detect_drift, revalidation_recommendations
from core.research_factory.experiments import (
    ExperimentError, ExperimentRegistry, ExperimentStatus, validate_transition,
)
from core.research_factory.hypothesis import HypothesisError, HypothesisStore, draft_hypothesis
from core.research_factory.weakness import (
    detect_weaknesses, hypothesis_seed_from_observation,
)


def _hypothesis(**overrides):
    base = dict(
        problem="Expectancy negative on STRAT_A LONG",
        observation="expectancy_pct=-1.0 over 15 trades",
        hypothesis="Conditioning entries on the deteriorating metric improves expectancy",
        expected_mechanism="filter removes unfavorable regime trades",
        baseline="STRAT_A incumbent", challenger="STRAT_A conditioned variant",
        universe="watchlist", horizon="SWING", dataset="trade journal",
        metrics=["oos_expectancy", "max_drawdown"],
        promotion_criteria=["OOS expectancy improvement"], failure_criteria=["no improvement"],
        risks=["overfit"],
    )
    base.update(overrides)
    return draft_hypothesis(**base)


def test_hypothesis_requires_every_mandated_field():
    with pytest.raises(HypothesisError):
        _hypothesis(problem="   ")
    with pytest.raises(HypothesisError):
        _hypothesis(promotion_criteria=[])
    with pytest.raises(HypothesisError):
        _hypothesis(failure_criteria=[])
    with pytest.raises(HypothesisError):
        _hypothesis(metrics=[])
    with pytest.raises(HypothesisError):
        _hypothesis(horizon="INTRADAY_SCALP")
    record = _hypothesis()
    assert record.hypothesis_id.startswith("hyp_")
    assert record.horizon == "SWING"


def test_hypothesis_store_append_is_idempotent(tmp_path):
    store = HypothesisStore(tmp_path / "hyp.json")
    record = _hypothesis()
    store.append(record)
    store.append(record)
    assert store.count() == 1
    assert store.get(record.hypothesis_id).problem == record.problem


def test_experiment_lifecycle_legal_transitions(tmp_path):
    registry = ExperimentRegistry(tmp_path / "exp.json")
    record = _hypothesis()
    HypothesisStore(tmp_path / "hyp.json").append(record)
    registry = ExperimentRegistry(tmp_path / "exp.json", hypothesis_store=HypothesisStore(tmp_path / "hyp.json"))
    experiment = registry.propose(
        hypothesis_id=record.hypothesis_id, strategy_id="STRAT_A",
        version="challenger-1", description="test challenger",
    )
    assert experiment.status == "PROPOSED"
    with pytest.raises(ExperimentError):  # cannot skip straight to VALIDATED
        registry.transition(experiment.experiment_id, ExperimentStatus.VALIDATED, actor="x")
    for destination, actor in (
        (ExperimentStatus.APPROVED_FOR_RESEARCH, "research_director"),
        (ExperimentStatus.RUNNING, "experiment_planner"),
        (ExperimentStatus.VALIDATED, "quant_validator"),
        (ExperimentStatus.SHADOW, "research_director"),
    ):
        registry.transition(experiment.experiment_id, destination, actor=actor)
    updated = registry.get(experiment.experiment_id)
    assert updated.status == "SHADOW"
    assert len(updated.events) == 5  # PROPOSED + 4 transitions, immutable
    with pytest.raises(ExperimentError):  # terminal states reject further moves except PROMOTED/ROLLED_BACK
        registry.transition(experiment.experiment_id, ExperimentStatus.APPROVED_FOR_RESEARCH, actor="x")


def test_all_nine_spec_statuses_exist():
    statuses = {status.value for status in ExperimentStatus}
    assert statuses == {
        "PROPOSED", "APPROVED_FOR_RESEARCH", "RUNNING", "FAILED", "REJECTED",
        "VALIDATED", "SHADOW", "PROMOTED", "ROLLED_BACK",
    }


def test_propose_rejects_unknown_hypothesis(tmp_path):
    store = HypothesisStore(tmp_path / "hyp.json")
    registry = ExperimentRegistry(tmp_path / "exp.json", hypothesis_store=store)
    with pytest.raises(ExperimentError):
        registry.propose(hypothesis_id="hyp_missing", strategy_id="X", version="1", description="d")


def _trade(strategy, pnl, day, direction="LONG", invested=1000.0):
    return {"strategy_type": strategy, "direction": direction, "net_pnl": pnl,
            "invested_amount": invested, "closed_at": f"2026-08-{day:02d}T10:00:00+00:00"}


def test_weakness_detection_structured_facts():
    trades = ([_trade("A", -10.0, i + 1) for i in range(12)]
              + [_trade("B", 25.0, i + 1) for i in range(12)]
              + [_trade("C", 5.0, i + 1) for i in range(3)])
    observations = detect_weaknesses(trades)
    by_key = {(o.strategy_id, o.metric): o for o in observations}
    assert by_key[("A", "expectancy_pct")].verdict == "WEAKNESS"
    assert by_key[("A", "win_rate")].verdict == "WEAKNESS"
    assert by_key[("B", "expectancy_pct")].verdict == "OK"
    assert by_key[("C", "sample")].verdict == "INSUFFICIENT_SAMPLE"
    seed = hypothesis_seed_from_observation(by_key[("A", "expectancy_pct")])
    assert "STRAT" not in seed["problem"] or "A" in seed["problem"]
    assert "expectancy_pct" in seed["problem"]


def test_drift_revalidation_never_risk_change():
    # reference: strong; recent: collapsed -> DRIFT_SUSPECTED
    trades = ([_trade("A", 20.0, i + 1) for i in range(12)]
              + [_trade("A", -15.0, i + 13) for i in range(10)])
    reports = detect_drift(trades)
    suspected = [r for r in reports if r.verdict == "DRIFT_SUSPECTED"]
    assert suspected and suspected[0].strategy_id == "A"
    recommendations = revalidation_recommendations(reports)
    assert recommendations[0]["action"] == "REVALIDATION"
    assert all("risk" not in str(rec.get("action", "")).lower() for rec in recommendations)


def test_drift_insufficient_sample_is_honest():
    trades = [_trade("A", 10.0, 1), _trade("A", -5.0, 2)]
    reports = detect_drift(trades)
    assert all(r.verdict == "INSUFFICIENT_SAMPLE" for r in reports)
