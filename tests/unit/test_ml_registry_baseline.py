import pytest
from core.signal_engine import SignalHorizon
from core.ml.registry import (ModelRecord, ModelRegistry, TargetSpec, OosGate,
                              OosVerdict, OosVerdictLabel)


def _mk(is_simple=True, verdict=None):
    return ModelRecord(
        model="m", version="v1", features=("rsi",), feature_version="v1",
        target=TargetSpec(horizon=SignalHorizon.SWING, condition="c"),
        horizon=SignalHorizon.SWING, training_range=("a", "b"),
        validation_range=("c", "d"), test_range=("e", "f"), trained_at="t",
        is_simple_baseline=is_simple,
        oos_verdict=verdict or OosVerdictLabel.PENDING.value,
    )


def test_model_record_has_ten_section_54_fields():
    rec = _mk()
    for f in ("model", "version", "features", "feature_version", "target",
              "horizon", "training_range", "validation_range", "test_range", "trained_at"):
        assert hasattr(rec, f)


def test_complex_model_cannot_register_with_promoting_verdict():
    reg = ModelRegistry()
    with pytest.raises(ValueError):
        reg.register(_mk(is_simple=False, verdict=OosVerdictLabel.ROBUST.value))


def test_promote_with_real_verdict_works():
    reg = ModelRegistry()
    pending = _mk(is_simple=False)
    reg.register(pending)
    promoted = reg.promote(pending, OosVerdict(available=True, verdict=OosVerdictLabel.ROBUST.value))
    assert promoted.oos_verdict == OosVerdictLabel.ROBUST.value


def test_oos_gate_deferred_without_runner():
    gate = OosGate()
    v = gate.evaluate_oos_improvement(_mk(), _mk(is_simple=False))
    assert v.available is False
    assert v.verdict == OosVerdictLabel.DEFERRED_PHASE7.value

from core.ml.baseline import SimpleBaselineModel, train_baseline


def test_simple_baseline_trains_and_registers_pending():
    # Monotonic synthetic data: low rsi -> outcome 1, high rsi -> outcome 0.
    feats = [{"rsi": 20.0}, {"rsi": 30.0}, {"rsi": 40.0}, {"rsi": 50.0},
             {"rsi": 60.0}, {"rsi": 70.0}, {"rsi": 80.0}]
    outs = [1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0]
    trained, rec = train_baseline(
        feats, outs, feature_names=("rsi",), horizon=SignalHorizon.SWING,
        target_condition="P(return>0 over next 5-20 sessions)",
        training_range=("2023-01-01", "2023-06-01"),
        validation_range=("2023-06-01", "2023-09-01"),
        test_range=("2023-09-01", "2023-12-01"), trained_at="2026-08-12T00:00:00Z",
    )
    assert rec.is_simple_baseline is True
    assert rec.oos_verdict == OosVerdictLabel.PENDING.value
    assert len(trained.weights) == 2  # bias + 1 feature
    model = SimpleBaselineModel()
    p_low = model.predict_proba(trained, [25.0])
    p_high = model.predict_proba(trained, [75.0])
    assert p_low > p_high  # low rsi -> outcome 1, high rsi -> outcome 0


def test_run_weight_calibration_audit_delegates_to_ml():
    from core.scoring_system import run_weight_calibration_audit
    result = run_weight_calibration_audit()
    assert isinstance(result, dict)
    assert result.get("available") in (True, False)
    assert "summary" in result
