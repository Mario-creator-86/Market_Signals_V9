"""core.ml — §54 ML Engine + §55 Calibration + §56 Meta-labeling."""
from .registry import (ModelRecord, ModelRegistry, TargetSpec, OosGate, OosVerdict,
                       OosVerdictLabel)
from .baseline import SimpleBaselineModel, TrainedBaseline, train_baseline
from .calibration import (CalibrationResult, CalibrationError, ProbabilityDefinition,
                          calibrate, apply_calibration)
from .meta_label import (MetaLabel, MetaLabeler, MetaLabelEvaluation,
                         evaluate_meta_labeling)
from .qlib_adapter import is_available as qlib_available, adapt as qlib_adapt

__all__ = [
    "ModelRecord", "ModelRegistry", "TargetSpec", "OosGate", "OosVerdict", "OosVerdictLabel",
    "SimpleBaselineModel", "TrainedBaseline", "train_baseline",
    "CalibrationResult", "CalibrationError", "ProbabilityDefinition", "calibrate", "apply_calibration",
    "MetaLabel", "MetaLabeler", "MetaLabelEvaluation", "evaluate_meta_labeling",
    "qlib_available", "qlib_adapt",
]