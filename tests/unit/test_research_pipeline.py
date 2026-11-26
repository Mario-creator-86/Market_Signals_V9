"""Phase 14 unit tests — §111 pipeline, §78 memory, §64 Optuna runner, tracking."""
import pytest

from core.research_factory.experiments import ExperimentRegistry
from core.research_factory.hypothesis import HypothesisStore
from core.research_factory.pipeline import (
    build_historical_evaluator,
    default_evaluator,
    run_research_cycle,
)
from core.research_factory.tracking import (
    JsonTracking,
    MlflowTracking,
    _ensure_utf8_standard_streams,
    resolve_tracking,
)
from core.research_factory.weakness import WeaknessObservation


def _weak_trades(strategy="PIPE_A", n=12):
    return [
        {"strategy_type": strategy, "direction": "LONG", "net_pnl": -10.0,
         "invested_amount": 1000.0, "closed_at": f"2026-08-{i + 1:02d}T10:00:00+00:00"}
        for i in range(n)
    ]


def test_pipeline_full_journey_to_shadow(tmp_path):
    store = HypothesisStore(tmp_path / "h.json")
    registry = ExperimentRegistry(tmp_path / "e.json", hypothesis_store=store)

    def validator(observation, hypothesis):
        assert isinstance(observation, WeaknessObservation)
        assert hypothesis.hypothesis_id.startswith("hyp_")
        return {"verdict": "VALIDATED", "actor": "quant_validator",
                "evidence": {"oos_expectancy_delta": 0.31, "sample": 40}}

    result = run_research_cycle(
        trades=_weak_trades(), hypothesis_store=store,
        experiment_registry=registry, evaluator=validator, tracking=JsonTracking(),
    )
    assert result["weaknesses_detected"] >= 1
    assert all(e["final_status"] == "SHADOW" for e in result["experiments"])
    assert result["status_counts"]["SHADOW"] >= 1
    # research-only guarantees
    assert "no promotion" in result["notes"]
    experiment = registry.get(result["experiments"][0]["experiment_id"])
    assert experiment.evidence.get("evaluation", {}).get("oos_expectancy_delta") == 0.31
    assert any(event["to_status"] == "SHADOW" for event in experiment.events)


def test_pipeline_default_evaluator_rejects_honestly(tmp_path):
    store = HypothesisStore(tmp_path / "h.json")
    registry = ExperimentRegistry(tmp_path / "e.json", hypothesis_store=store)
    result = run_research_cycle(trades=_weak_trades(), hypothesis_store=store,
                                experiment_registry=registry)
    assert all(e["final_status"] == "REJECTED" for e in result["experiments"])
    experiment = registry.get(result["experiments"][0]["experiment_id"])
    assert experiment.evidence["evaluation"]["outcome_class"] == "INSUFFICIENT_EVIDENCE"
    assert experiment.evidence["evaluation"]["validation_method"] == "chronological_shadow_filter_v1"


def test_historical_evaluator_uses_strategy_direction_cohort_without_lookahead():
    trades = _weak_trades("OTHER", 80) + _weak_trades("TARGET", 12)
    observation = WeaknessObservation(
        strategy_id="TARGET", direction="LONG", metric="expectancy_pct",
        value=-1.0, threshold=0.0, sample=12, verdict="WEAKNESS",
        window_trades=20, detail={},
    )

    result = build_historical_evaluator(trades)(observation, object())

    assert result["verdict"] == "REJECTED"
    assert result["evidence"]["sample_total"] == 12
    assert result["evidence"]["lookahead_used"] is False
    assert result["evidence"]["outcome_class"] == "INSUFFICIENT_EVIDENCE"


def test_pipeline_no_weakness_no_experiments(tmp_path):
    healthy = [
        {"strategy_type": "HEALTHY", "direction": "LONG", "net_pnl": 30.0,
         "invested_amount": 1000.0, "closed_at": f"2026-08-{i + 1:02d}T10:00:00+00:00"}
        for i in range(12)
    ]
    store = HypothesisStore(tmp_path / "h.json")
    registry = ExperimentRegistry(tmp_path / "e.json", hypothesis_store=store)
    result = run_research_cycle(trades=healthy, hypothesis_store=store,
                                experiment_registry=registry)
    assert result["weaknesses_detected"] == 0
    assert result["experiments"] == []
    assert store.count() == 0  # no vague hypotheses invented without a weakness


def test_optional_tracking_failure_does_not_change_valid_research_outcome(tmp_path):
    class FailingTracking:
        def start_run(self, *_args):
            raise RuntimeError("tracking unavailable")

    store = HypothesisStore(tmp_path / "h.json")
    registry = ExperimentRegistry(tmp_path / "e.json", hypothesis_store=store)

    result = run_research_cycle(
        trades=_weak_trades(), hypothesis_store=store, experiment_registry=registry,
        evaluator=lambda *_args: {
            "verdict": "VALIDATED", "actor": "quant_validator",
            "evidence": {"sample": 40, "oos_expectancy_delta": 0.2},
        },
        tracking=FailingTracking(),
    )

    assert result["status_counts"].get("RUNNING", 0) == 0
    assert result["status_counts"]["SHADOW"] >= 1
    assert result["tracking_errors"][0]["operation"] == "start_run"


def test_evaluator_crash_closes_experiment_as_failed(tmp_path):
    store = HypothesisStore(tmp_path / "h.json")
    registry = ExperimentRegistry(tmp_path / "e.json", hypothesis_store=store)

    def broken_evaluator(*_args):
        raise RuntimeError("bad evaluator input")

    result = run_research_cycle(
        trades=_weak_trades(), hypothesis_store=store, experiment_registry=registry,
        evaluator=broken_evaluator, tracking=JsonTracking(),
    )

    assert result["status_counts"].get("RUNNING", 0) == 0
    assert result["status_counts"]["FAILED"] >= 1
    assert all(item["final_status"] == "FAILED" for item in result["experiments"])






def test_tracking_defaults_to_json_without_mlflow(monkeypatch):
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    tracker = resolve_tracking()
    assert isinstance(tracker, JsonTracking)
    run_id = tracker.start_run("exp_1", "eval")
    tracker.log_metrics(run_id, {"sharpe": 1.2})
    tracker.finish(run_id, "VALIDATED")
    assert tracker.evidence()[run_id]["status"] == "VALIDATED"


def test_mlflow_tracking_normalizes_windows_console_encoding(monkeypatch):
    class LegacyStream:
        encoding = "cp1252"

        def reconfigure(self, *, encoding, errors):
            self.encoding = encoding
            self.errors = errors

        def write(self, value):
            return len(value.encode(self.encoding, errors=self.errors))

    stdout = LegacyStream()
    stderr = LegacyStream()
    monkeypatch.setattr("core.research_factory.tracking.sys.stdout", stdout)
    monkeypatch.setattr("core.research_factory.tracking.sys.stderr", stderr)

    _ensure_utf8_standard_streams()

    assert stdout.encoding == "utf-8"
    assert stderr.encoding == "utf-8"
    assert stdout.write("🏃 View run") > 0


def test_mlflow_tracking_separates_numeric_metrics_from_descriptive_evidence():
    class RunContext:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeMlflow:
        def __init__(self):
            self.metrics = None
            self.tags = None

        def start_run(self, **_kwargs):
            return RunContext()

        def log_metrics(self, values):
            self.metrics = values

        def set_tags(self, values):
            self.tags = values

    tracker = object.__new__(MlflowTracking)
    tracker._mlflow = FakeMlflow()

    tracker.log_metrics("run-1", {
        "sample": 12,
        "expectancy": -0.3,
        "reason": "no deterministic evaluator supplied",
        "details": {"verdict": "INSUFFICIENT_EVIDENCE"},
    })

    assert tracker._mlflow.metrics == {"sample": 12.0, "expectancy": -0.3}
    assert tracker._mlflow.tags["evidence.reason"] == "no deterministic evaluator supplied"
    assert '"verdict": "INSUFFICIENT_EVIDENCE"' in tracker._mlflow.tags["evidence.details"]
