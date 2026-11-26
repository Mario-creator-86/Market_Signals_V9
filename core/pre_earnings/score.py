"""§50 — PRE-EARNINGS SCORE 0-100 + DATA COVERAGE + DATA CONFIDENCE.

Pure, deterministic, stdlib-only, no I/O. Evolves the V7 proto scoring in
agents/pre_earnings_agent.py:
  * PRE-EARNINGS SCORE <- _compute_opportunity_score formula
    (signal_strength = 0.65*gap_risk + 0.35*|direction_bias|) * (coverage/100),
    promoted to canonical with VISIBLE components. Score is NOT probability.
  * DATA COVERAGE  <- _compute_analysis_coverage (6 weighted components), unchanged.
  * DATA CONFIDENCE <- the historical_samples bands (5083-5097), evolved so
    coverage is a DOWNGRADE-ONLY modifier (never upgrades) — strictly more honest.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


# Direction-score input cap. When most inputs are missing, the few that remain
# used to absorb the whole score: the eToro positioning input (nominal weight
# 0.10 of 1.22) became a third of the result (seen live: GRRR +33 with zero
# news). No single input may carry more than this multiple of the share it has
# when every input is present; the share that nobody may claim counts as
# neutral, which pulls a thinly supported score toward 0.
DIRECTION_INPUT_MAX_SHARE_MULTIPLE = 2.0


def cap_direction_input_shares(
    components: Dict[str, Tuple[Optional[float], float]],
    *,
    neutral: float = 50.0,
    max_share_multiple: float = DIRECTION_INPUT_MAX_SHARE_MULTIPLE,
) -> Dict[str, Any]:
    """Weighted direction score (0..100) with a per-input share cap.

    ``components`` maps a name to ``(value_0_100_or_None, nominal_weight)``.
    An input's nominal share is its weight divided by the weight of ALL inputs
    (present or missing). Its share of the final score is
    ``min(weight / available_weight, max_share_multiple * nominal_share)``;
    whatever the caps leave unassigned is filled with ``neutral``.

    Returns ``score`` (capped), ``uncapped_score``, ``shares``, ``cap_applied``,
    ``neutral_share`` and ``evidence_factor`` (1.0 when no cap applied; the
    fraction of the score that real inputs still carry otherwise).
    """
    usable: Dict[str, Tuple[float, float]] = {}
    nominal_total = 0.0
    for name, pair in (components or {}).items():
        try:
            value, weight = pair
            weight = float(weight)
        except (TypeError, ValueError):
            continue
        if weight != weight or weight <= 0:
            continue
        nominal_total += weight
        if value is None:
            continue
        try:
            usable[name] = (float(value), weight)
        except (TypeError, ValueError):
            continue
    available = sum(weight for _value, weight in usable.values())
    if available <= 0 or nominal_total <= 0:
        return {
            "score": float(neutral),
            "uncapped_score": float(neutral),
            "shares": {},
            "cap_applied": False,
            "neutral_share": 1.0,
            "evidence_factor": 0.0,
            "available_weight": 0.0,
            "nominal_weight": round(nominal_total, 4),
        }
    multiple = max(1.0, float(max_share_multiple))
    uncapped = sum(value * weight for value, weight in usable.values()) / available
    shares: Dict[str, float] = {}
    capped = False
    for name, (_value, weight) in usable.items():
        raw_share = weight / available
        limit = multiple * (weight / nominal_total)
        if raw_share > limit + 1e-12:
            capped = True
            shares[name] = limit
        else:
            shares[name] = raw_share
    assigned = sum(shares.values())
    neutral_share = max(0.0, 1.0 - assigned)
    score = sum(usable[name][0] * share for name, share in shares.items()) + float(neutral) * neutral_share
    return {
        "score": score,
        "uncapped_score": uncapped,
        "shares": {name: round(share, 4) for name, share in shares.items()},
        "cap_applied": capped,
        "neutral_share": round(neutral_share, 4),
        "evidence_factor": round(min(1.0, assigned), 4),
        "available_weight": round(available, 4),
        "nominal_weight": round(nominal_total, 4),
    }


def _coverage_label(score: int) -> str:
    s = int(score or 0)
    if s >= 70:
        return "HIGH"
    if s >= 45:
        return "MEDIUM"
    return "LOW"


def _score_label(score: int) -> str:
    s = int(score or 0)
    if s >= 80:
        return "VERY_HIGH"
    if s >= 60:
        return "HIGH"
    if s >= 40:
        return "MODERATE"
    if s >= 20:
        return "LOW"
    return "VERY_LOW"


def compute_data_confidence(
    *,
    historical_samples: int,
    coverage_score: int,
    implied_available: bool,
    news_count: int,
) -> Tuple[str, str, bool]:
    """§50 DATA CONFIDENCE. Returns (label, reason, is_insufficient).

    Coverage is a DOWNGRADE-ONLY modifier: a thin coverage read can lower
    HIGH->MEDIUM but can never raise LOW->MEDIUM. This is strictly more honest
    than the V7 historical_samples-only bands (a row that was HIGH/MEDIUM/LOW/NONE
    keeps its band unless coverage is weak, in which case it is downgraded).
    is_insufficient is True only when historical_samples == 0 (NONE band),
    preserving the V7 `is_insufficient_data` semantics used by the NEWS_ONLY_CALL
    guard and the gap_risk_unavailable flag.
    """
    s = int(historical_samples or 0)
    cov = int(coverage_score or 0)
    extra = f"news={int(news_count or 0)}, implied={'y' if implied_available else 'n'}"
    if s >= 3:
        if cov >= 45:
            return "HIGH", f"OK ({s} quarters, coverage {cov}; {extra})", False
        return "MEDIUM", f"Partial data: {s} quarters, coverage {cov} ({extra}). GapRisk estimated.", False
    if s == 2:
        return "MEDIUM", f"Partial data: {s} quarters ({extra}). GapRisk estimated.", False
    if s == 1:
        return "LOW", f"Only 1 quarter available ({extra}). GapRisk estimated from news.", False
    # "DirectionBias from news" was printed even with zero articles, where any
    # bias shown can only come from non-news inputs (e.g. retail positioning).
    direction_note = (
        "DirectionBias from news."
        if int(news_count or 0) > 0
        else "No news either: any DirectionBias comes from non-news inputs only."
    )
    return "NONE", f"No historical quarters ({extra}). GapRisk unavailable. {direction_note}", True


@dataclass(frozen=True)
class PreEarningsScoreResult:
    score: int
    label: str
    components: List[Dict[str, Any]] = field(default_factory=list)
    coverage_score: int = 0
    coverage_label: str = "LOW"
    coverage_contributors: List[Dict[str, Any]] = field(default_factory=list)
    data_confidence: str = "NONE"
    data_confidence_reason: str = ""
    is_not_probability: bool = True


def compute_pre_earnings_score(
    *,
    gap_risk: Optional[int],
    direction_bias: Optional[int],
    coverage_score: int,
    coverage_contributors: List[Dict[str, Any]],
    historical_samples: int,
    news_count: int,
    implied_available: bool,
    weight_overrides: Optional[Dict[str, float]] = None,
) -> PreEarningsScoreResult:
    """§50 PRE-EARNINGS SCORE 0-100 with visible components.

    Formula (evolution of _compute_opportunity_score, exact back-compat when
    weight_overrides is None):
        signal_strength = w_gap*gap_risk + w_bias*|direction_bias|   # 0-100
        score = round(clamp(signal_strength * (coverage/100), 0, 100))
    with w_gap=0.65, w_bias=0.35 (sum 1.0). When historical_samples == 0
    (DATA CONFIDENCE NONE) the score is 0 — matching the V7 forced-zero for
    insufficient-data rows. Score is NOT probability (§50): it is a
    signal-strength x coverage composite, never interpreted as P(up).
    """
    conf_label, conf_reason, is_insufficient = compute_data_confidence(
        historical_samples=historical_samples, coverage_score=coverage_score,
        implied_available=implied_available, news_count=news_count,
    )
    cov = int(coverage_score or 0)

    w_gap, w_bias = 0.65, 0.35
    if isinstance(weight_overrides, dict):
        for key, value in weight_overrides.items():
            if key in ("gap_risk", "direction_bias") and isinstance(value, (int, float)) and value >= 0:
                if key == "gap_risk":
                    w_gap = float(value)
                else:
                    w_bias = float(value)

    if is_insufficient:
        score = 0
        gap_val = 0.0
        bias_val = 0.0
    else:
        gap_int = int(gap_risk) if gap_risk is not None else 0
        bias_int = abs(int(direction_bias)) if direction_bias is not None else 0
        gap_val = float(gap_int)
        bias_val = float(bias_int)
        signal_strength = w_gap * gap_int + w_bias * bias_int
        score = int(round(_clamp(signal_strength * (cov / 100.0), 0.0, 100.0)))

    components: List[Dict[str, Any]] = [
        {
            "name": "gap_risk",
            "value": round(gap_val / 100.0, 4),
            "weight": round(w_gap, 4),
            "note": "Gap/expected-move risk axis (compute_gap_risk_score, 0-100)",
        },
        {
            "name": "direction_bias_magnitude",
            "value": round(bias_val / 100.0, 4),
            "weight": round(w_bias, 4),
            "note": "Direction conviction magnitude (compute_direction_bias_score, |-100..100|)",
        },
        {
            "name": "coverage_modulator",
            "value": round(cov / 100.0, 4),
            "weight": 1.0,
            "note": "DATA COVERAGE modulates signal strength. Score is NOT probability.",
        },
    ]

    return PreEarningsScoreResult(
        score=score,
        label=_score_label(score),
        components=components,
        coverage_score=cov,
        coverage_label=_coverage_label(cov),
        coverage_contributors=list(coverage_contributors or []),
        data_confidence=conf_label,
        data_confidence_reason=conf_reason,
        is_not_probability=True,
    )