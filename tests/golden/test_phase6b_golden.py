"""GOLD-054 §54 ML registry + baseline + OosGate seam; GOLD-055 §55 calibration
+ not-LLM-confidence; GOLD-056 §56 meta-labeling."""
import pytest
from core.signal_engine import SignalHorizon
from core.ml import (ModelRecord, ModelRegistry, TargetSpec, OosGate, OosVerdict,
                     OosVerdictLabel, train_baseline, CalibrationResult, CalibrationError,
                     ProbabilityDefinition, calibrate, MetaLabeler, evaluate_meta_labeling)

pytestmark = pytest.mark.golden


def test_gold054_model_record_has_ten_section_54_fields():
    rec = ModelRecord(model="m", version="v1", features=("rsi",), feature_version="v1",
                     target=TargetSpec(horizon=SignalHorizon.SWING, condition="c"),
                     horizon=SignalHorizon.SWING, training_range=("a", "b"),
                     validation_range=("c", "d"), test_range=("e", "f"), trained_at="t",
                     is_simple_baseline=True)
    for f in ("model", "version", "features", "feature_version", "target", "horizon",
              "training_range", "validation_range", "test_range", "trained_at"):
        assert hasattr(rec, f)


def test_gold054_simple_baseline_trains_and_registers_pending():
    feats = [{"rsi": 20.0}, {"rsi": 30.0}, {"rsi": 40.0}, {"rsi": 50.0},
             {"rsi": 60.0}, {"rsi": 70.0}, {"rsi": 80.0}]
    outs = [1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0]
    _, rec = train_baseline(feats, outs, feature_names=("rsi",), horizon=SignalHorizon.SWING,
                            target_condition="P(return>0 over next 5-20 sessions)",
                            training_range=("a", "b"), validation_range=("c", "d"),
                            test_range=("e", "f"), trained_at="t")
    assert rec.is_simple_baseline is True
    assert rec.oos_verdict == OosVerdictLabel.PENDING.value


def test_gold054_oos_gate_deferred_without_phase7_runner():
    gate = OosGate()
    base = ModelRecord(model="b", version="1", features=(), feature_version="v1",
                     target=TargetSpec(horizon=SignalHorizon.SWING, condition="c"),
                     horizon=SignalHorizon.SWING, training_range=("", ""), validation_range=("", ""),
                     test_range=("", ""), trained_at="t", is_simple_baseline=True)
    chal = ModelRecord(model="c", version="1", features=(), feature_version="v1",
                     target=TargetSpec(horizon=SignalHorizon.SWING, condition="c"),
                     horizon=SignalHorizon.SWING, training_range=("", ""), validation_range=("", ""),
                     test_range=("", ""), trained_at="t", is_simple_baseline=False)
    v = gate.evaluate_oos_improvement(base, chal)
    assert v.available is False and v.verdict == OosVerdictLabel.DEFERRED_PHASE7.value


def test_gold054_complex_model_cannot_register_promoted_but_promote_works():
    reg = ModelRegistry()
    complex_rec = ModelRecord(model="c", version="1", features=(), feature_version="v1",
                              target=TargetSpec(horizon=SignalHorizon.SWING, condition="c"),
                              horizon=SignalHorizon.SWING, training_range=("", ""), validation_range=("", ""),
                              test_range=("", ""), trained_at="t", is_simple_baseline=False,
                              oos_verdict=OosVerdictLabel.ROBUST.value)
    with pytest.raises(ValueError):
        reg.register(complex_rec)
    pending = ModelRecord(model="c", version="1", features=(), feature_version="v1",
                         target=TargetSpec(horizon=SignalHorizon.SWING, condition="c"),
                         horizon=SignalHorizon.SWING, training_range=("", ""), validation_range=("", ""),
                         test_range=("", ""), trained_at="t", is_simple_baseline=False)
    reg.register(pending)
    promoted = reg.promote(pending, OosVerdict(available=True, verdict=OosVerdictLabel.ROBUST.value))
    assert promoted.oos_verdict == OosVerdictLabel.ROBUST.value


def test_gold055_calibrate_is_statistical_with_definition():
    d = ProbabilityDefinition(horizon=SignalHorizon.SWING,
                             condition="P(return > 0 over next 5-20 sessions)")
    res = calibrate([0.1, 0.4, 0.6, 0.9], [0, 0, 1, 1], definition=d, method="isotonic")
    assert res.is_statistical_probability is True
    assert res.sample_size == 4 and "P(return > 0" in res.definition.condition


def test_gold055_llm_confidence_not_statistical():
    with pytest.raises(CalibrationError):
        CalibrationResult.from_llm_confidence(confidence=0.78)


def test_gold055_uncalibrated_not_statistical():
    d = ProbabilityDefinition(horizon=SignalHorizon.SHORT_TERM, condition="uncalibrated logistic")
    res = CalibrationResult.uncalibrated(d, sample_size=100)
    assert res.is_statistical_probability is False and res.method == "none"


def test_gold056_section_56_example_and_evaluation():
    assert MetaLabeler.combine("LONG", "DO_NOT_EXECUTE") == "NO_TRADE"
    assert MetaLabeler.combine("LONG", "EXECUTE") == "LONG"
    assert MetaLabeler.combine("LONG", "INSUFFICIENT") == "NO_TRADE"
    ev = evaluate_meta_labeling(["LONG"] * 4, ["EXECUTE", "EXECUTE", "DO_NOT_EXECUTE", "EXECUTE"],
                                ["LONG", "LONG", "NO_TRADE", "NO_TRADE"], positive_label="LONG")
    assert ev.sample_size == 4 and ev.meta_labeling_improves_precision is True