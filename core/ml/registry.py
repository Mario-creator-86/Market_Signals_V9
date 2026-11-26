"""§54 ML Engine — model registry tracking the 10 §54 fields + the OOS-
improvement gate (interface seam; real OOS backtest is Phase 7 §57)."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Mapping, Optional, Tuple

from core.signal_engine import SignalHorizon


@dataclass(frozen=True)
class TargetSpec:
    horizon: SignalHorizon
    condition: str  # precise §55 definition

    def to_dict(self) -> dict:
        return {"horizon": self.horizon.value, "condition": self.condition}


class OosVerdictLabel(str, Enum):
    PENDING = "PENDING"
    DEFERRED_PHASE7 = "DEFERRED_PHASE7"
    ROBUST = "ROBUST"
    MODERATE = "MODERATE"
    FRAGILE = "FRAGILE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


_PHASE6_SETTABLE = frozenset({OosVerdictLabel.PENDING.value, OosVerdictLabel.DEFERRED_PHASE7.value})
_PROMOTING = frozenset({OosVerdictLabel.ROBUST.value, OosVerdictLabel.MODERATE.value})
_ALL_VERDICTS = _PHASE6_SETTABLE | _PROMOTING | frozenset(
    {OosVerdictLabel.FRAGILE.value, OosVerdictLabel.INSUFFICIENT_EVIDENCE.value}
)


@dataclass(frozen=True)
class ModelRecord:
    model: str
    version: str
    features: Tuple[str, ...]
    feature_version: str
    target: TargetSpec
    horizon: SignalHorizon
    training_range: Tuple[str, str]
    validation_range: Tuple[str, str]
    test_range: Tuple[str, str]
    trained_at: str
    is_simple_baseline: bool = False
    oos_verdict: str = OosVerdictLabel.PENDING.value

    def __post_init__(self):
        if self.oos_verdict not in _ALL_VERDICTS:
            raise ValueError(f"unknown oos_verdict: {self.oos_verdict!r}")

    def to_dict(self) -> dict:
        return {
            "model": self.model, "version": self.version,
            "features": list(self.features), "feature_version": self.feature_version,
            "target": self.target.to_dict(), "horizon": self.horizon.value,
            "training_range": list(self.training_range),
            "validation_range": list(self.validation_range),
            "test_range": list(self.test_range), "trained_at": self.trained_at,
            "is_simple_baseline": self.is_simple_baseline, "oos_verdict": self.oos_verdict,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ModelRecord":
        d = dict(d or {})
        tgt = d.get("target") or {}
        return cls(
            model=str(d.get("model", "")), version=str(d.get("version", "")),
            features=tuple(d.get("features") or ()), feature_version=str(d.get("feature_version", "")),
            target=TargetSpec(horizon=SignalHorizon(tgt.get("horizon")),
                              condition=str(tgt.get("condition", ""))),
            horizon=SignalHorizon(d.get("horizon")),
            training_range=tuple(d.get("training_range") or ("", "")),
            validation_range=tuple(d.get("validation_range") or ("", "")),
            test_range=tuple(d.get("test_range") or ("", "")),
            trained_at=str(d.get("trained_at", "")),
            is_simple_baseline=bool(d.get("is_simple_baseline", False)),
            oos_verdict=str(d.get("oos_verdict", OosVerdictLabel.PENDING.value)),
        )


@dataclass(frozen=True)
class OosVerdict:
    available: bool
    verdict: str
    reason: str = ""


class OosGate:
    """§54 'complex models must prove OOS improvement'. Real OOS backtest is
    Phase 7 (§57). With no runner, the gate is an honest DEFERRED stub."""

    def evaluate_oos_improvement(self, baseline: ModelRecord, challenger: ModelRecord,
                                 backtest_runner=None) -> OosVerdict:
        if backtest_runner is None:
            return OosVerdict(available=False,
                              verdict=OosVerdictLabel.DEFERRED_PHASE7.value,
                              reason="phase7_backtest_engine")
        return OosVerdict(available=True,
                          verdict=str(backtest_runner(baseline, challenger)),
                          reason="phase7_runner")


class ModelRegistry:
    def __init__(self):
        self._records: List[ModelRecord] = []

    def register(self, record: ModelRecord) -> ModelRecord:
        if not isinstance(record, ModelRecord):
            raise TypeError("register expects a ModelRecord")
        # §54 gate: a complex model may not be REGISTERED with a promoting verdict;
        # promotion requires promote() with a real available OosVerdict.
        if not record.is_simple_baseline and record.oos_verdict in _PROMOTING:
            raise ValueError(
                "complex model cannot be registered with a promoting oos_verdict; "
                "use promote() with an OosVerdict"
            )
        self._records.append(record)
        return record

    def get(self, model: str, version: str) -> Optional[ModelRecord]:
        for r in self._records:
            if r.model == model and r.version == version:
                return r
        return None

    def list_by_horizon(self, horizon: SignalHorizon) -> List[ModelRecord]:
        return [r for r in self._records if r.horizon is horizon]

    def list_all(self) -> List[ModelRecord]:
        return list(self._records)

    def promote(self, record: ModelRecord, verdict: OosVerdict) -> ModelRecord:
        if not verdict.available:
            raise ValueError("cannot promote with an unavailable OosVerdict")
        if verdict.verdict not in _PROMOTING:
            raise ValueError(f"{verdict.verdict!r} is not a promoting verdict")
        promoted = ModelRecord(
            model=record.model, version=record.version, features=record.features,
            feature_version=record.feature_version, target=record.target, horizon=record.horizon,
            training_range=record.training_range, validation_range=record.validation_range,
            test_range=record.test_range, trained_at=record.trained_at,
            is_simple_baseline=record.is_simple_baseline, oos_verdict=verdict.verdict,
        )
        for i, r in enumerate(self._records):
            if r is record:
                self._records[i] = promoted
                break
        return promoted

    def to_list(self) -> List[dict]:
        return [r.to_dict() for r in self._records]

    def from_list(self, records: List[Mapping[str, Any]]) -> None:
        self._records = [ModelRecord.from_dict(r) for r in records]