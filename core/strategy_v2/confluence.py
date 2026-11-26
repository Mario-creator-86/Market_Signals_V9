"""Independent, directional confluence score for V2 candidates.

It combines only causal OHLCV-derived facts: confirmed market structure,
liquidity sweeps, chart geometry, volume flow and two-close price acceptance.
The score is intentionally separate from a strategy evaluator's setup score so
Capital can explain *why* a setup aligned, rather than hiding those facts in a
single opaque number.
"""

from __future__ import annotations

from typing import Any, Dict


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    return str(value or "").upper().strip()


def _matches_direction(value: Any, direction: str) -> bool:
    expected = "BULLISH" if _text(direction) == "LONG" else "BEARISH"
    return _text(value) == expected


def score_confluence(context: Dict[str, Any], direction: str) -> Dict[str, Any]:
    """Return a transparent 0..100 score plus its factual components.

    Missing OHLCV facts are neutral rather than treated as confirmation.  A
    coverage adjustment prevents a single available component from producing a
    deceptively perfect score.  The function is pure and therefore can be used
    identically by the live selector, backtest and visual replay.
    """
    ctx = context if isinstance(context, dict) else {}
    expected = "BULLISH" if _text(direction) == "LONG" else "BEARISH"
    components: list[Dict[str, Any]] = []

    trend = _text(ctx.get("structure_trend"))
    event = _text(ctx.get("structure_event") or ctx.get("structure_last_event"))
    event_direction = _text(ctx.get("structure_event_direction") or ctx.get("structure_last_event_direction"))
    labels_known = bool(_text(ctx.get("structure_last_high_label")) or _text(ctx.get("structure_last_low_label")))
    structure_available = bool(trend or event or labels_known)
    structure_score = 0.0
    structure_reasons: list[str] = []
    if trend == expected:
        structure_score += 8.0
        structure_reasons.append(f"trend_{trend.lower()}")
    if event in {"BOS", "CHOCH"} and event_direction == expected:
        structure_score += 12.0
        structure_reasons.append(f"{event.lower()}_{expected.lower()}")
    if expected == "BULLISH" and _text(ctx.get("structure_last_high_label")) == "HH" and _text(ctx.get("structure_last_low_label")) == "HL":
        structure_score += 5.0
        structure_reasons.append("hh_hl_sequence")
    if expected == "BEARISH" and _text(ctx.get("structure_last_high_label")) == "LH" and _text(ctx.get("structure_last_low_label")) == "LL":
        structure_score += 5.0
        structure_reasons.append("lh_ll_sequence")
    components.append({"id": "market_structure", "max_score": 25.0, "score": structure_score, "available": structure_available, "reasons": structure_reasons})

    liquidity_direction = _text(ctx.get("liquidity_last_event_direction"))
    liquidity_event = _text(ctx.get("liquidity_last_event"))
    liquidity_age = _f(ctx.get("liquidity_event_age_bars"), 999.0)
    liquidity_available = bool(liquidity_direction or liquidity_event)
    liquidity_score = 0.0
    liquidity_reasons: list[str] = []
    if liquidity_direction == expected and liquidity_event == "SWEEP_RECLAIM":
        liquidity_score += 15.0
        liquidity_reasons.append("sweep_reclaim_aligned")
        if liquidity_age <= 5:
            liquidity_score += 5.0
            liquidity_reasons.append("fresh_sweep")
    components.append({"id": "liquidity", "max_score": 20.0, "score": liquidity_score, "available": liquidity_available, "reasons": liquidity_reasons})

    pattern_direction = _text(ctx.get("chart_pattern_direction"))
    pattern_status = _text(ctx.get("chart_pattern_status"))
    pattern_name = _text(ctx.get("chart_pattern_name") or ctx.get("chart_pattern"))
    pattern_available = bool(pattern_direction or pattern_status or pattern_name)
    pattern_score = 0.0
    pattern_reasons: list[str] = []
    if pattern_direction == expected and pattern_status == "CONFIRMED":
        pattern_score += 15.0
        pattern_reasons.append(f"confirmed_{pattern_name.lower() or 'pattern'}")
    elif pattern_direction == expected and pattern_status == "FORMING":
        pattern_score += 6.0
        pattern_reasons.append(f"forming_{pattern_name.lower() or 'pattern'}")
    components.append({"id": "chart_pattern", "max_score": 15.0, "score": pattern_score, "available": pattern_available, "reasons": pattern_reasons})

    volume_ratio_value = ctx.get("volume_ratio")
    volume_flow_direction = _text(ctx.get("volume_flow_direction"))
    volume_event_direction = _text(ctx.get("volume_last_event_direction"))
    volume_event_age = _f(ctx.get("volume_event_age_bars"), 999.0)
    volume_available = volume_ratio_value is not None or bool(volume_flow_direction) or bool(volume_event_direction)
    volume_ratio = _f(volume_ratio_value, 0.0)
    volume_score = 0.0
    volume_reasons: list[str] = []
    if volume_ratio >= 1.2:
        volume_score += 8.0
        volume_reasons.append(f"volume_{volume_ratio:.2f}x")
    elif volume_ratio >= 1.0:
        volume_score += 4.0
        volume_reasons.append("volume_at_average")
    if volume_flow_direction == expected:
        volume_score += 7.0
        volume_reasons.append("volume_flow_aligned")
    if volume_event_direction == expected and volume_event_age <= 3:
        volume_score += 5.0
        volume_reasons.append("fresh_volume_event")
    components.append({"id": "volume", "max_score": 20.0, "score": volume_score, "available": volume_available, "reasons": volume_reasons})

    acceptance_direction = _text(ctx.get("price_acceptance_direction"))
    acceptance_age = _f(ctx.get("price_acceptance_age_bars"), 999.0)
    acceptance_available = bool(acceptance_direction)
    acceptance_score = 0.0
    acceptance_reasons: list[str] = []
    if acceptance_direction == expected:
        acceptance_score = 20.0 if acceptance_age <= 3 else 12.0
        acceptance_reasons.append("price_accepted_beyond_level")
    components.append({"id": "price_acceptance", "max_score": 20.0, "score": acceptance_score, "available": acceptance_available, "reasons": acceptance_reasons})

    available_components = [item for item in components if item["available"]]
    max_available = sum(float(item["max_score"]) for item in available_components)
    raw_score = sum(float(item["score"]) for item in available_components)
    coverage = max_available / 100.0
    # 50 is the explicit neutral score when no causal structural information was
    # available. It neither confirms nor rejects a legacy setup.
    score = 50.0 if not available_components else (raw_score / max_available * 100.0) * (0.60 + 0.40 * coverage)
    opposing = [
        item["id"]
        for item in components
        if item["available"]
        and item["score"] == 0.0
        and item["id"] in {"liquidity", "chart_pattern", "price_acceptance"}
    ]
    grade = "A" if score >= 80 else ("B" if score >= 65 else ("C" if score >= 50 else "D"))
    reasons = [reason for item in components for reason in item["reasons"]]
    return {
        "score": round(max(0.0, min(100.0, score)), 2),
        "grade": grade,
        "direction": _text(direction),
        "expected_market_direction": expected,
        "coverage": round(coverage, 3),
        "components": components,
        "reasons": reasons,
        "conflicts": opposing,
    }
