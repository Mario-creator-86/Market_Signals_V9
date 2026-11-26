"""Phase 14 — Research Factory (§75/§76/§77/§78/§79/§111 + §64 Optuna).

A dedicated research subsystem, separate from production execution. See
docs/superpowers/specs/2026-08-16-v9-phase14-research-factory-design.md.
"""
from core.research_factory.drift import (
    DriftReport, detect_drift, revalidation_recommendations,
)
from core.research_factory.experiments import (
    ExperimentError, ExperimentRecord, ExperimentRegistry, ExperimentStatus,
    validate_transition,
)
from core.research_factory.hypothesis import (
    HypothesisError, HypothesisRecord, HypothesisStore, draft_hypothesis,
)
from core.research_factory.pipeline import run_research_cycle
from core.research_factory.scheduler import (
    ResearchCycleScheduler, auto_cycle_config, get_research_scheduler,
)
from core.research_factory.tracking import (
    JsonTracking, MlflowTracking, ResearchTracking, resolve_tracking,
)
from core.research_factory.weakness import (
    WeaknessObservation, detect_weaknesses, hypothesis_seed_from_observation,
    load_closed_trades,
)

__all__ = [
    "HypothesisRecord", "HypothesisStore", "HypothesisError", "draft_hypothesis",
    "ExperimentRecord", "ExperimentRegistry", "ExperimentStatus", "ExperimentError",
    "validate_transition", "WeaknessObservation", "detect_weaknesses",
    "hypothesis_seed_from_observation", "load_closed_trades", "DriftReport",
    "detect_drift", "revalidation_recommendations", "run_research_cycle",
    "ResearchCycleScheduler", "auto_cycle_config", "get_research_scheduler",
    "ResearchTracking", "JsonTracking", "MlflowTracking", "resolve_tracking",
]
