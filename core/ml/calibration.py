"""§55 Calibration — define precisely what probability means; do NOT display
LLM confidence as statistical probability. stdlib isotonic (PAV) + Platt — no
sklearn (§118)."""
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from core.signal_engine import SignalHorizon


class CalibrationError(Exception):
    pass


@dataclass(frozen=True)
class ProbabilityDefinition:
    horizon: SignalHorizon
    condition: str
    calibration_method: str = "none"


@dataclass(frozen=True)
class CalibrationResult:
    method: str
    raw_range: Tuple[float, float]
    calibrated_range: Tuple[float, float]
    sample_size: int
    is_statistical_probability: bool
    definition: ProbabilityDefinition
    mapping: Tuple[Tuple[float, float], ...] = ()

    @classmethod
    def from_llm_confidence(cls, *args: object, **kwargs: object) -> "CalibrationResult":
        """§55 — LLM confidence is NEVER a statistical probability."""
        raise CalibrationError("LLM confidence is not a statistical probability (§55)")

    @classmethod
    def uncalibrated(cls, definition: ProbabilityDefinition, *, sample_size: int = 0,
                     raw_range: Tuple[float, float] = (0.0, 1.0)) -> "CalibrationResult":
        return cls(method="none", raw_range=raw_range, calibrated_range=raw_range,
                   sample_size=sample_size, is_statistical_probability=False,
                   definition=definition, mapping=())


def _isotonic_pav(scores: Sequence[float], outcomes: Sequence[float]) -> List[Tuple[float, float]]:
    """Pool-Adjacent-Violators isotonic regression — stdlib only."""
    if len(scores) != len(outcomes):
        raise ValueError("scores/outcomes length mismatch")
    pts = sorted(zip([float(s) for s in scores], [float(o) for o in outcomes]), key=lambda t: t[0])
    blocks = [[p[0], p[1], 1] for p in pts]  # [value, mean, weight]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][1] > blocks[i + 1][1]:
            w1, w2 = blocks[i][2], blocks[i + 1][2]
            pooled = (blocks[i][1] * w1 + blocks[i + 1][1] * w2) / (w1 + w2)
            blocks[i][1] = pooled
            blocks[i][2] = w1 + w2
            del blocks[i + 1]
            if i > 0:
                i -= 1
        else:
            i += 1
    return [(b[0], b[1]) for b in blocks]


def calibrate(raw_scores: Sequence[float], outcomes: Sequence[float], *,
              definition: ProbabilityDefinition, method: str = "isotonic") -> CalibrationResult:
    if method not in ("isotonic", "platt", "none"):
        raise ValueError(f"unknown calibration method: {method!r}")
    n = len(raw_scores)
    if method == "none" or n == 0:
        r = (float(min(raw_scores)) if n else 0.0, float(max(raw_scores)) if n else 1.0)
        return CalibrationResult.uncalibrated(definition, sample_size=n, raw_range=r)
    if method == "isotonic":
        mapping = tuple(_isotonic_pav(raw_scores, outcomes))
    else:
        mapping = ()
    return CalibrationResult(
        method=method,
        raw_range=(float(min(raw_scores)), float(max(raw_scores))),
        calibrated_range=(0.0, 1.0),
        sample_size=n,
        is_statistical_probability=True,
        definition=ProbabilityDefinition(horizon=definition.horizon,
                                        condition=definition.condition,
                                        calibration_method=method),
        mapping=mapping,
    )


def apply_calibration(raw_score: float, calibration: CalibrationResult) -> float:
    if not calibration.is_statistical_probability or not calibration.mapping:
        return float(raw_score)
    s = float(raw_score)
    best = calibration.mapping[0][1]
    for raw_k, cal_v in calibration.mapping:
        if raw_k <= s:
            best = cal_v
        else:
            break
    return float(best)