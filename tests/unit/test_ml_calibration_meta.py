import pytest
from core.signal_engine import SignalHorizon
from core.ml.calibration import (CalibrationResult, CalibrationError, ProbabilityDefinition,
                                  calibrate, apply_calibration)
from core.ml.meta_label import MetaLabeler, evaluate_meta_labeling
from core.ml import qlib_available, qlib_adapt


def test_calibrate_isotonic_statistical():
    d = ProbabilityDefinition(horizon=SignalHorizon.SWING,
                              condition="P(return > 0 over next 5-20 sessions)")
    res = calibrate([0.1, 0.4, 0.6, 0.9], [0, 0, 1, 1], definition=d, method="isotonic")
    assert res.is_statistical_probability is True
    assert res.sample_size == 4
    assert res.mapping  # PAV blocks


def test_llm_confidence_raises():
    with pytest.raises(CalibrationError):
        CalibrationResult.from_llm_confidence(confidence=0.78)


def test_uncalibrated_not_statistical():
    d = ProbabilityDefinition(horizon=SignalHorizon.SHORT_TERM, condition="uncalibrated")
    res = CalibrationResult.uncalibrated(d, sample_size=100)
    assert res.is_statistical_probability is False
    assert apply_calibration(0.5, res) == 0.5  # pass-through


def test_meta_label_section_56_example():
    assert MetaLabeler.combine("LONG", "DO_NOT_EXECUTE") == "NO_TRADE"
    assert MetaLabeler.combine("LONG", "EXECUTE") == "LONG"
    assert MetaLabeler.combine("LONG", "INSUFFICIENT") == "NO_TRADE"


def test_evaluate_meta_labeling_improves_precision():
    primary = ["LONG", "LONG", "LONG", "LONG"]
    meta = ["EXECUTE", "EXECUTE", "DO_NOT_EXECUTE", "EXECUTE"]
    outcomes = ["LONG", "LONG", "NO_TRADE", "NO_TRADE"]
    ev = evaluate_meta_labeling(primary, meta, outcomes, positive_label="LONG")
    assert ev.sample_size == 4
    assert ev.meta_labeling_improves_precision is True


def test_qlib_adapter_honest_when_unavailable():
    res = qlib_adapt()
    assert res["available"] in (True, False)
    if not qlib_available():
        assert res["available"] is False
        assert "qlib" in res["reason"]