import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _first_present_float(*values: Any, default: float = 0.0) -> float:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except Exception:
            continue
    return default


def _first_positive_float(*values: Any, default: float = 0.0) -> float:
    for value in values:
        parsed = _safe_float(value, default)
        if parsed > 0:
            return parsed
    return default


def _safe_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _normalize_reasons(raw: Any) -> List[str]:
    reasons: List[str] = []
    if isinstance(raw, list):
        for item in raw:
            text = _safe_text(item)
            if text and text not in reasons:
                reasons.append(text)
    elif isinstance(raw, str):
        text = _safe_text(raw)
        if text:
            reasons.append(text)
    return reasons


def _fmt_number(value: Any, digits: int = 2, default: str = "n/a") -> str:
    try:
        parsed = float(value)
    except Exception:
        return default
    return f"{parsed:.{digits}f}"


def _fmt_money(value: Any, default: str = "n/a") -> str:
    parsed = _safe_float(value, 0.0)
    if parsed <= 0:
        return default
    return f"${parsed:,.2f}"


def _fmt_score(value: Any, digits: int = 1, default: str = "n/a") -> str:
    parsed = _safe_float(value, 0.0)
    if parsed <= 0:
        return default
    if parsed <= 1:
        parsed *= 100.0
    return f"{parsed:.{digits}f}"


# `probability_score` is a heuristic setup score (the scoring module itself
# marks it `probability_calibrated = False`). The field name is kept for
# compatibility; these two fields say what the number is.
SCORE_KIND_HEURISTIC = "heuristic_score"
SCORE_KIND_CALIBRATED = "calibrated_probability"
VALIDATION_STATUS_MEASURED = "measured"
VALIDATION_STATUS_UNAVAILABLE = "unavailable"
VALIDATION_UNAVAILABLE_NOTE = (
    "Validation score unavailable: it was not measured for this signal. "
    "It counts as failing any validation gate; it is not a measured 0."
)


def _probability_is_calibrated(source: Any) -> bool:
    """True only when the producer explicitly declared a calibrated probability."""
    return isinstance(source, dict) and source.get("probability_calibrated") is True


def _setup_score_text(probability_score: Any, calibrated: bool = False) -> str:
    """Human-readable form of `probability_score`."""
    if calibrated:
        return f"calibrated probability {_fmt_score(probability_score)}%"
    return f"setup score {_fmt_score(probability_score, 0)}/100 (not a probability)"


def _optional_score(value: Any) -> Optional[float]:
    """A measured number, or None when the value is missing / not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def _validation_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """`validation_score` as measured, or None with an explicit status.

    A missing score used to be stored as 0.0, which reads as "measured, and it
    was zero". Gates treat a missing score exactly as before (it fails them);
    only the record now says that nothing was measured.
    """
    score = _optional_score(payload.get("validation_score"))
    declared = _safe_text(payload.get("validation_score_status")).lower()
    if score is None or declared == VALIDATION_STATUS_UNAVAILABLE:
        return {
            "validation_score": None,
            "validation_status": VALIDATION_STATUS_UNAVAILABLE,
            "validation_note": VALIDATION_UNAVAILABLE_NOTE,
        }
    return {"validation_score": score, "validation_status": VALIDATION_STATUS_MEASURED}


def _append_unique(items: List[str], value: Any, limit: int = 10) -> None:
    text = _safe_text(value)
    if text and text not in items and len(items) < limit:
        items.append(text)


def _decision_factor(label: str, value: Any, detail: Any = "") -> Dict[str, str]:
    return {
        "label": _safe_text(label, "Factor"),
        "value": _safe_text(value, "n/a"),
        "detail": _safe_text(detail, ""),
    }


def _warning_item(
    source: str,
    message: Any,
    *,
    severity: str = "attention",
    detail: Any = "",
) -> Dict[str, str]:
    return {
        "source": _safe_text(source, "AI"),
        "message": _safe_text(message, ""),
        "severity": _safe_text(severity, "attention"),
        "detail": _safe_text(detail, ""),
    }


def _strategy_evidence_factors(evidence: Any) -> List[Dict[str, str]]:
    """Render persisted strategy facts without generating or inferring data."""
    if not isinstance(evidence, dict):
        return []

    factors: List[Dict[str, str]] = []
    strategy_name = _safe_text(evidence.get("strategy_name") or evidence.get("strategy_id"))
    sequence = evidence.get("sequence") if isinstance(evidence.get("sequence"), list) else []
    details = [
        _safe_text(item.get("detail"))
        for item in sequence
        if isinstance(item, dict) and _safe_text(item.get("detail"))
    ]
    if strategy_name or details:
        factors.append(_decision_factor(
            "Strategy trigger chain",
            strategy_name or "Structured strategy evidence",
            " -> ".join(details[:4]),
        ))

    liquidity = evidence.get("liquidity") if isinstance(evidence.get("liquidity"), dict) else {}
    if liquidity:
        factors.append(_decision_factor(
            "Liquidity event",
            f"{_safe_text(liquidity.get('direction'), 'unknown')} / {_safe_text(liquidity.get('event'), 'sweep')}",
            f"Swept level {_fmt_number(liquidity.get('sweep_level'), 8)}; age {_fmt_number(liquidity.get('age_bars'), 0)} bar(s).",
        ))

    supertrend = evidence.get("supertrend") if isinstance(evidence.get("supertrend"), dict) else {}
    if supertrend:
        factors.append(_decision_factor(
            "SuperTrend confirmation",
            f"{_safe_text(supertrend.get('direction'), 'unknown')} / flip {_safe_text(supertrend.get('flip_direction'), 'unknown')}",
            (
                f"Line {_fmt_number(supertrend.get('line'), 8)}; flip age "
                f"{_fmt_number(supertrend.get('flip_age_bars'), 0)} bar(s); parameters "
                f"ATR({_fmt_number(supertrend.get('period'), 0)}) x"
                f"{_fmt_number(supertrend.get('multiplier'), 2)}."
            ),
        ))

    multi_timeframe = evidence.get("multi_timeframe") if isinstance(evidence.get("multi_timeframe"), dict) else {}
    if multi_timeframe:
        states = multi_timeframe.get("states") if isinstance(multi_timeframe.get("states"), dict) else {}
        state_labels = []
        for timeframe in ("30m", "1h", "4h"):
            state = states.get(timeframe) if isinstance(states.get(timeframe), dict) else {}
            state_labels.append(
                f"{timeframe}={_safe_text(state.get('supertrend_direction'), 'unavailable').upper()}"
            )
        aligned = multi_timeframe.get("aligned") if isinstance(multi_timeframe.get("aligned"), list) else []
        factors.append(_decision_factor(
            "Multi-timeframe confirmation",
            " / ".join(state_labels),
            (
                f"30m is the minimum entry timeframe; 1h and 4h were evaluated independently. "
                f"Aligned higher timeframe(s): {', '.join(str(item) for item in aligned) or 'none'}."
            ),
        ))

    structure = evidence.get("market_structure") if isinstance(evidence.get("market_structure"), dict) else {}
    if structure:
        labels = " / ".join(
            item for item in (
                _safe_text(structure.get("last_high_label")),
                _safe_text(structure.get("last_low_label")),
            ) if item
        )
        detail = (
            f"Trend {_safe_text(structure.get('trend'), 'unknown')}; support "
            f"{_fmt_number(structure.get('support'), 8)}, resistance "
            f"{_fmt_number(structure.get('resistance'), 8)}."
        )
        if labels:
            detail += f" Pivots {labels}."
        factors.append(_decision_factor(
            "Market structure",
            f"{_safe_text(structure.get('direction'), 'unknown')} {_safe_text(structure.get('event'), 'event')}",
            detail,
        ))

    confluence = evidence.get("confluence") if isinstance(evidence.get("confluence"), dict) else {}
    if confluence:
        reasons = confluence.get("reasons") if isinstance(confluence.get("reasons"), list) else []
        conflicts = confluence.get("conflicts") if isinstance(confluence.get("conflicts"), list) else []
        detail = ", ".join(_safe_text(item) for item in reasons[:5] if _safe_text(item)) or "No aligned causal component was retained."
        if conflicts:
            detail += f" Conflicts: {', '.join(_safe_text(item) for item in conflicts[:3] if _safe_text(item))}."
        factors.append(_decision_factor(
            "Causal confluence",
            f"{_safe_text(confluence.get('grade'), 'n/a')} / {_fmt_number(confluence.get('score'), 1)}",
            f"Coverage {_fmt_number(_safe_float(confluence.get('coverage'), 0.0) * 100, 0)}%. {detail}",
        ))

    volume = evidence.get("volume") if isinstance(evidence.get("volume"), dict) else {}
    if volume:
        factors.append(_decision_factor(
            "Volume confirmation",
            f"{_fmt_number(volume.get('ratio'), 2)}x average",
            f"Current {_fmt_number(volume.get('current'), 2)} vs average {_fmt_number(volume.get('average'), 2)}.",
        ))

    pattern = evidence.get("chart_pattern") if isinstance(evidence.get("chart_pattern"), dict) else {}
    if _safe_text(pattern.get("status")).upper() not in {"", "NONE"}:
        factors.append(_decision_factor(
            "Chart-pattern overlay",
            f"{_safe_text(pattern.get('status'))} / {_safe_text(pattern.get('direction'), 'n/a')}",
            _safe_text(pattern.get("name"), "No named pattern was supplied."),
        ))
    acceptance = evidence.get("price_acceptance") if isinstance(evidence.get("price_acceptance"), dict) else {}
    if _safe_text(acceptance.get("direction")):
        factors.append(_decision_factor(
            "Price acceptance",
            _safe_text(acceptance.get("direction")),
            f"Accepted beyond {_fmt_number(acceptance.get('level'), 8)}; age {_fmt_number(acceptance.get('age_bars'), 0)} bar(s).",
        ))
    return factors


def _news_evidence_from_context(decision_context: Dict[str, Any]) -> Dict[str, Any]:
    """Return the persisted, source-backed News verdict in a UI-safe shape."""
    gate = decision_context.get("autonomous_news_gate") if isinstance(decision_context, dict) else None
    if not isinstance(gate, dict):
        return {}
    context = gate.get("context") if isinstance(gate.get("context"), dict) else {}
    headlines: List[Dict[str, Any]] = []
    for item in context.get("top_headlines") if isinstance(context.get("top_headlines"), list) else []:
        if not isinstance(item, dict):
            continue
        micro = item.get("ai_micro_summary") if isinstance(item.get("ai_micro_summary"), dict) else {}
        deep = item.get("ai_deep_analysis") if isinstance(item.get("ai_deep_analysis"), dict) else {}
        headlines.append({
            "article_id": _safe_text(item.get("article_id")),
            "title": _safe_text(item.get("title")),
            "published_at": _safe_text(item.get("time") or item.get("published_at")),
            "url": _safe_text(item.get("url")),
            "source": _safe_text(item.get("source")),
            "impact": _safe_text(item.get("impact")),
            "sentiment": _safe_text(item.get("sentiment")),
            "ai_sentiment": _safe_text(micro.get("overall_sentiment") or deep.get("overall_sentiment")),
            "ai_tldr": _safe_text(micro.get("tldr") or deep.get("tldr")),
        })
    return {
        "allowed": bool(gate.get("allowed")),
        "status": _safe_text(gate.get("status")),
        "reason": _safe_text(gate.get("reason")),
        "evaluated_at": _safe_text(gate.get("evaluated_at")),
        "pulse_generated_at": _safe_text(context.get("pulse_generated_at")),
        "sentiment": _safe_text(context.get("sentiment")),
        "ai_sentiment": _safe_text(context.get("ai_sentiment")),
        "article_count_24h": int(_safe_float(context.get("article_count_24h"), 0.0)),
        "source_backed_count_24h": int(_safe_float(context.get("source_backed_count_24h"), 0.0)),
        "ai_analyzed_count_24h": int(_safe_float(context.get("ai_analyzed_count_24h"), 0.0)),
        "source_backed_ai_analyzed_count_24h": int(_safe_float(context.get("source_backed_ai_analyzed_count_24h"), 0.0)),
        "headlines": headlines[:3],
    }


def _extract_detail_reasons(details: Any) -> List[str]:
    reasons: List[str] = []
    if not isinstance(details, dict):
        return reasons
    for key in ("mtf", "rsi", "vwap", "ema", "macd", "price_action", "threshold", "probability_gate"):
        value = details.get(key)
        text = _safe_text(value)
        if text:
            reasons.append(f"{key}: {text}")
    return reasons


def extract_signal_reasons(signal: Optional[Dict[str, Any]]) -> List[str]:
    payload = signal or {}
    reasons = _normalize_reasons(payload.get("decision_reasons"))
    reasons.extend([r for r in _normalize_reasons(payload.get("reasons")) if r not in reasons])

    strategy_notes = _safe_text(payload.get("strategy_notes"))
    if strategy_notes and strategy_notes not in reasons:
        reasons.append(strategy_notes)

    for detail in _extract_detail_reasons(payload.get("details")):
        if detail not in reasons:
            reasons.append(detail)

    if reasons:
        return reasons[:6]

    ticker = _safe_text(payload.get("ticker"), "UNKNOWN")
    action = _safe_text(payload.get("action") or payload.get("type"), "HOLD")
    entry = _safe_float(payload.get("entry") or payload.get("entry_price"), 0.0)
    tp = _safe_float(payload.get("tp"), 0.0)
    sl = _safe_float(payload.get("sl"), 0.0)
    fallback = (
        f"{ticker} {action} | entry {entry:.4f} | target {tp:.4f} | stop {sl:.4f}"
        if entry > 0 and tp > 0 and sl > 0
        else f"{ticker} {action} signal"
    )
    return [fallback]


def _compute_risk_profile(entry: float, tp: float, sl: float, action: str) -> Dict[str, Any]:
    side = _safe_text(action).upper()
    if entry <= 0 or tp <= 0 or sl <= 0:
        return {
            "entry": round(entry, 8),
            "target": round(tp, 8),
            "stop": round(sl, 8),
            "risk_reward_ratio": None,
            "stop_distance_pct": None,
            "target_distance_pct": None,
        }

    if side == "SHORT":
        risk = abs(sl - entry)
        reward = abs(entry - tp)
        stop_distance_pct = ((sl - entry) / entry) * 100.0
        target_distance_pct = ((entry - tp) / entry) * 100.0
    else:
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        stop_distance_pct = ((entry - sl) / entry) * 100.0
        target_distance_pct = ((tp - entry) / entry) * 100.0

    rr = (reward / risk) if risk > 0 else None
    return {
        "entry": round(entry, 8),
        "target": round(tp, 8),
        "stop": round(sl, 8),
        "risk_reward_ratio": round(rr, 4) if rr is not None else None,
        "stop_distance_pct": round(stop_distance_pct, 4),
        "target_distance_pct": round(target_distance_pct, 4),
    }


def _flow_evidence(signal: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Free order-flow evidence (whale / institutional proxy) for the signal.

    Evidence-only by contract: CONFIRM/NEUTRAL/CONTRADICT vs the setup's own
    direction — it never vetoes, scores or sizes anything. Absolute fail-safe:
    any failure yields an UNAVAILABLE block so the decision pipeline continues
    unchanged (owner spec §5).
    """
    unavailable = {"alignment": "UNAVAILABLE", "asset_class": None,
                   "note": "flow evidence unavailable", "summary": None}
    try:
        from core.flow_indicators import flow_evidence_for_signal
        if not isinstance(signal, dict) or not signal.get("ticker"):
            return unavailable
        return flow_evidence_for_signal(signal)
    except Exception:
        return unavailable


def build_signal_decision_context(
    signal: Optional[Dict[str, Any]],
    *,
    decision_status: str = "accepted",
    strategy_type: Optional[str] = None,
    analysis_provider: str = "signal_only",
    risk_evaluation: Optional[Dict[str, Any]] = None,
    execution_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = signal or {}
    entry = _safe_float(payload.get("entry") or payload.get("entry_price"), 0.0)
    tp = _safe_float(payload.get("tp"), 0.0)
    sl = _safe_float(payload.get("sl"), 0.0)
    action = _safe_text(payload.get("action") or payload.get("type"), "HOLD").upper()
    reasons = extract_signal_reasons(payload)
    strategy = _safe_text(
        strategy_type
        or payload.get("strategy_type")
        or payload.get("strategy_id")
        or payload.get("signal_mode")
        or payload.get("variant_mode")
        or payload.get("variant"),
        "SYSTEM",
    )
    autonomous_source = _safe_text(payload.get("autonomous_source"))
    decision_source = "autonomous" if autonomous_source else "signal"
    return {
        "decision_id": f"dec_{uuid.uuid4().hex[:12]}",
        "timestamp": _utc_now_iso(),
        "decision_status": decision_status,
        "decision_source": decision_source,
        "ticker": _safe_text(payload.get("ticker")).upper(),
        "action": action,
        "signal_id": _safe_text(payload.get("id")),
        "strategy_type": strategy,
        "strategy_variant": _safe_text(payload.get("variant") or payload.get("variant_mode")),
        "reason": reasons[0],
        "reasons": reasons,
        "scoring": {
            "signal_score": _safe_float(payload.get("signal_score"), 0.0),
            "probability_score": _safe_float(payload.get("probability_score"), 0.0),
            "probability_calibrated": _probability_is_calibrated(payload),
            "score_kind": SCORE_KIND_CALIBRATED if _probability_is_calibrated(payload) else SCORE_KIND_HEURISTIC,
            "quality_score": _safe_float(payload.get("quality_score"), 0.0),
            "signal_threshold": _safe_float(payload.get("signal_threshold"), 0.0),
            "mtf_alignment": payload.get("mtf_alignment"),
            "profile": _safe_text(payload.get("profile")),
            "mode": _safe_text(payload.get("signal_mode") or payload.get("mode")),
            "details": dict(payload.get("details") or {}) if isinstance(payload.get("details"), dict) else {},
            "warnings": list(payload.get("warnings") or []) if isinstance(payload.get("warnings"), list) else [],
        },
        "risk": _compute_risk_profile(entry, tp, sl, action),
        "sizing": {
            "invested_amount": _safe_float(payload.get("invested_amount"), 0.0),
            "base_risk_pct": _safe_float(payload.get("base_risk_pct"), 0.0),
            "risk_pct_used": _safe_float(payload.get("risk_pct_used"), 0.0),
            "position_size_pct": _safe_float(payload.get("position_size_pct"), 0.0),
            "size_factor": _safe_float(payload.get("size_factor"), 0.0),
            "usd_size": _safe_float(payload.get("usd_size"), 0.0),
            "leverage": _safe_float(payload.get("leverage"), 1.0),
        },
        "execution": {
            "analysis_provider": _safe_text(analysis_provider, "signal_only"),
            "ai_available": bool(payload.get("ai_available", False)),
            "fallback_used": _safe_text(payload.get("fallback_used"), "signal_only"),
            "source": _safe_text(payload.get("source")),
        },
        "autonomous": {
            "source": autonomous_source,
            "scanner_context": dict(payload.get("scanner_context") or {}) if isinstance(payload.get("scanner_context"), dict) else {},
            "pre_earnings_context": dict(payload.get("pre_earnings_context") or {}) if isinstance(payload.get("pre_earnings_context"), dict) else {},
        },
        "risk_evaluation": dict(risk_evaluation or {}),
        "execution_context": dict(execution_context or {}),
        "confidence": {
            "tier": _safe_text(payload.get("confidence_tier"), "C"),
            "adjusted_size_factor": _safe_float(payload.get("adjusted_size_factor"), 1.0),
            "reasons": list(payload.get("confidence_reasons") or []) if isinstance(payload.get("confidence_reasons"), list) else [],
            "strategy_source": _safe_text(payload.get("strategy_source"), "V1"),
            **_validation_fields(payload),
        },
        "strategy_evidence": dict(payload.get("strategy_evidence") or {}) if isinstance(payload.get("strategy_evidence"), dict) else {},
        "flow_evidence": _flow_evidence(payload),
        "research": {
            "research_only": bool(payload.get("research_only")),
            "capital_execution_eligible": bool(payload.get("capital_execution_eligible", not bool(payload.get("research_only")))),
            "capital_execution_block_reason": _safe_text(payload.get("capital_execution_block_reason")),
            "validation_rejection": _safe_text(payload.get("validation_rejection")),
        },
    }


def build_manual_decision_context(
    *,
    ticker: str,
    direction: str,
    entry_price: float,
    take_profit_price: float,
    stop_loss_price: float,
    usd_size: float,
    leverage: int = 1,
    reason: str = "",
    strategy_type: str = "MANUAL",
) -> Dict[str, Any]:
    reasons = _normalize_reasons(reason) or ["Manual terminal order"]
    return {
        "decision_id": f"dec_{uuid.uuid4().hex[:12]}",
        "timestamp": _utc_now_iso(),
        "decision_status": "manual",
        "decision_source": "manual",
        "ticker": _safe_text(ticker).upper(),
        "action": _safe_text(direction).upper(),
        "signal_id": "",
        "strategy_type": _safe_text(strategy_type, "MANUAL"),
        "strategy_variant": "",
        "reason": reasons[0],
        "reasons": reasons,
        "scoring": {
            "signal_score": 0.0,
            "probability_score": 0.0,
            "probability_calibrated": False,
            "score_kind": SCORE_KIND_HEURISTIC,
            "quality_score": 0.0,
            "signal_threshold": 0.0,
            "mtf_alignment": None,
            "profile": "MANUAL",
            "mode": "MANUAL",
            "details": {},
            "warnings": [],
        },
        "risk": _compute_risk_profile(entry_price, take_profit_price, stop_loss_price, direction),
        "sizing": {
            "invested_amount": round(_safe_float(usd_size, 0.0), 6),
            "base_risk_pct": 0.0,
            "risk_pct_used": 0.0,
            "position_size_pct": 0.0,
            "size_factor": 1.0,
            "usd_size": round(_safe_float(usd_size, 0.0), 6),
            "leverage": int(leverage or 1),
        },
        "execution": {
            "analysis_provider": "manual",
            "ai_available": False,
            "fallback_used": "manual",
            "source": "capital_terminal",
        },
        "risk_evaluation": {
            "accepted": True,
            "reason": "manual_order",
            "checks": [],
        },
        "execution_context": {
            "source": "capital_terminal",
        },
    }


def summarize_position_explainability(position: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Build a rich, human-readable explainability summary for a position.
    Answers:
      - Why was the trade entered?
      - What was the entry/exit?
      - Why did it exit (SL/TP/Manual/Time)?
      - Which agents approved it and what were their scores?
    """
    payload = position or {}
    decision_context = payload.get("decision_context")
    if not isinstance(decision_context, dict):
        decision_context = build_manual_decision_context(
            ticker=_safe_text(payload.get("ticker")),
            direction=_safe_text(payload.get("direction"), "LONG"),
            entry_price=_safe_float(payload.get("entry_price"), 0.0),
            take_profit_price=_safe_float(payload.get("take_profit_price"), 0.0),
            stop_loss_price=_safe_float(payload.get("stop_loss_price"), 0.0),
            usd_size=_safe_float(payload.get("usd_size"), 0.0),
            leverage=int(_safe_float(payload.get("leverage"), 1.0)),
            reason=_safe_text(payload.get("entry_reason") or payload.get("note"), "Recovered position context"),
            strategy_type=_safe_text(payload.get("strategy_type"), "RECOVERED"),
        )

    scoring = dict(decision_context.get("scoring") or {})
    risk = dict(decision_context.get("risk") or {})
    sizing = dict(decision_context.get("sizing") or {})
    execution = dict(decision_context.get("execution") or {})
    confidence = dict(decision_context.get("confidence") or {})
    autonomous = dict(decision_context.get("autonomous") or {})
    scanner_context = dict(autonomous.get("scanner_context") or {})
    pre_earnings_context = dict(autonomous.get("pre_earnings_context") or {})
    risk_evaluation = dict(decision_context.get("risk_evaluation") or {})
    ollama_review = dict(decision_context.get("ollama") or {}) if isinstance(decision_context.get("ollama"), dict) else {}
    news_evidence = _news_evidence_from_context(decision_context)
    capital_entry_gate = (
        dict(decision_context.get("capital_entry_gate") or {})
        if isinstance(decision_context.get("capital_entry_gate"), dict)
        else {}
    )
    strategy_evidence = (
        dict(decision_context.get("strategy_evidence") or {})
        if isinstance(decision_context.get("strategy_evidence"), dict)
        else dict(payload.get("strategy_evidence") or {}) if isinstance(payload.get("strategy_evidence"), dict) else {}
    )
    entry_chart_snapshot = (
        dict(decision_context.get("entry_chart_snapshot") or {})
        if isinstance(decision_context.get("entry_chart_snapshot"), dict)
        else dict(payload.get("entry_chart_snapshot") or {})
        if isinstance(payload.get("entry_chart_snapshot"), dict)
        else {}
    )
    research = dict(decision_context.get("research") or {}) if isinstance(decision_context.get("research"), dict) else {}
    reasons = list(decision_context.get("reasons") or [])

    ticker = _safe_text(payload.get("ticker") or decision_context.get("ticker"), "UNKNOWN").upper()
    raw_action = _safe_text(payload.get("direction") or decision_context.get("action"), "LONG").upper()
    direction = "LONG" if raw_action in {"LONG", "BUY"} else ("SHORT" if raw_action in {"SHORT", "SELL"} else raw_action)
    entry_price = _first_positive_float(payload.get("entry_price"), risk.get("entry"), default=0.0)
    exit_price = _safe_float(payload.get("exit_price"), 0.0)
    qty = _safe_float(payload.get("qty"), 0.0)
    status = _safe_text(payload.get("status"), "UNKNOWN")
    close_reason = _safe_text(payload.get("close_reason"), "")
    is_open = status.upper() == "OPEN"
    if is_open:
        pnl_value = _first_present_float(
            payload.get("unrealized_pnl"),
            payload.get("current_pnl"),
            payload.get("pnl"),
            payload.get("net_pnl"),
            default=0.0,
        )
        pnl_label = "Live PnL"
    else:
        pnl_value = _first_present_float(
            payload.get("net_pnl"),
            payload.get("pnl"),
            payload.get("cash_pnl"),
            payload.get("gross_pnl"),
            default=0.0,
        )
        pnl_label = "Realized PnL"

    # Human readable decision chain.
    decision_source = _safe_text(decision_context.get("decision_source"), "unknown").lower()
    signal_score = _safe_float(scoring.get("signal_score"), 0.0)
    probability_score = _safe_float(scoring.get("probability_score"), 0.0)
    probability_calibrated = _probability_is_calibrated(scoring)
    # Records written before these fields existed get them on the way out.
    scoring.setdefault("probability_calibrated", probability_calibrated)
    scoring.setdefault("score_kind", SCORE_KIND_CALIBRATED if probability_calibrated else SCORE_KIND_HEURISTIC)
    quality_score = _safe_float(scoring.get("quality_score"), 0.0)
    signal_threshold = _safe_float(scoring.get("signal_threshold"), 0.0)
    rr = _safe_float(risk.get("risk_reward_ratio"), 0.0)
    stop_distance_pct = _safe_float(risk.get("stop_distance_pct"), 0.0)
    target_distance_pct = _safe_float(risk.get("target_distance_pct"), 0.0)
    target_price = _first_positive_float(risk.get("target"), payload.get("take_profit_price"), default=0.0)
    stop_price = _first_positive_float(risk.get("stop"), payload.get("stop_loss_price"), default=0.0)
    decision_explanation = _safe_text(decision_context.get("decision_explanation"))
    if decision_source in {"signal", "autonomous"}:
        entry_story = (
            f"AI-assisted {direction} on {ticker}: score {_fmt_score(signal_score)}"
            f" vs gate {_fmt_score(signal_threshold) if signal_threshold > 0 else 'n/a'}"
        )
        if reasons:
            entry_story += f" — {'; '.join(reasons[:3])}"
        sequence = strategy_evidence.get("sequence") if isinstance(strategy_evidence.get("sequence"), list) else []
        factual_steps = [
            _safe_text(item.get("detail")) for item in sequence
            if isinstance(item, dict) and _safe_text(item.get("detail"))
        ]
        if factual_steps:
            entry_story += f" | factual trigger: {' -> '.join(factual_steps[:3])}"
    elif decision_source == "manual":
        entry_story = f"Manual {direction} on {ticker}: {reasons[0] if reasons else 'Terminal order'}"
    else:
        entry_story = f"{direction} trade on {ticker}: {reasons[0] if reasons else decision_source}"

    # Exit story
    if status.upper() == "CLOSED":
        if close_reason.lower().startswith("manual"):
            exit_story = f"Manually closed at {exit_price}"
        elif "stop" in close_reason.lower():
            exit_story = f"Hit stop-loss at {exit_price} (SL was {risk.get('stop', 'N/A')})"
        elif "target" in close_reason.lower() or "tp" in close_reason.lower():
            exit_story = f"Hit take-profit at {exit_price} (TP was {risk.get('target', 'N/A')})"
        else:
            exit_story = f"Closed at {exit_price} — {close_reason}"
    else:
        exit_story = "Position still open"

    # Sizing story
    usd_size = _first_positive_float(
        sizing.get("usd_size"),
        sizing.get("invested_amount"),
        payload.get("usd_size"),
        payload.get("notional"),
        payload.get("margin_used"),
        default=0.0,
    )
    invested_amount = _first_positive_float(
        sizing.get("invested_amount"),
        payload.get("margin_used"),
        payload.get("usd_size"),
        default=usd_size,
    )
    leverage = int(max(1.0, _first_positive_float(sizing.get("leverage"), payload.get("leverage"), default=1.0)))
    if usd_size > 0:
        sizing["usd_size"] = round(usd_size, 6)
    if invested_amount > 0:
        sizing["invested_amount"] = round(invested_amount, 6)
    sizing["leverage"] = leverage
    sizing_story = f"Notional ${usd_size:,.2f} with {leverage}x leverage" if usd_size > 0 else "Sizing unavailable"

    risk_checks = risk_evaluation.get("checks") if isinstance(risk_evaluation.get("checks"), list) else []
    passed_checks = [check for check in risk_checks if isinstance(check, dict) and bool(check.get("passed"))]
    risk_gate_value = (
        f"{len(passed_checks)}/{len(risk_checks)} passed"
        if risk_checks
        else _safe_text(risk_evaluation.get("reason"), "n/a")
    )

    ai_review = {
        "available": bool(ollama_review.get("available")),
        "provider": _safe_text(ollama_review.get("provider") or execution.get("analysis_provider"), "n/a"),
        "model": _safe_text(ollama_review.get("model"), ""),
        "recommendation": _safe_text(ollama_review.get("recommendation"), ""),
        "confidence": _safe_float(ollama_review.get("confidence"), 0.0),
        "size_factor": _safe_float(ollama_review.get("size_factor"), 0.0),
        "leverage": int(max(1.0, _first_positive_float(ollama_review.get("leverage"), leverage, default=1.0))),
        "sentiment": _safe_text(ollama_review.get("sentiment"), ""),
        "supports_direction": ollama_review.get("supports_direction"),
        "reason": _safe_text(ollama_review.get("reason"), ""),
        "risks": list(ollama_review.get("risks") or []) if isinstance(ollama_review.get("risks"), list) else [],
        "generated_at": _safe_text(ollama_review.get("generated_at"), ""),
    }

    if ai_review["available"] and ai_review["reason"]:
        ai_decision_story = ai_review["reason"]
    elif decision_source == "manual":
        ai_decision_story = (
            "AI did not originate this manual trade; it reconstructs the rationale from the "
            "operator note, risk levels, position size, and live tracking data."
        )
    else:
        ai_decision_story = (
            "AI explanation is reconstructed from the signal, risk gates, sizing context, "
            "and execution metadata because no full model narrative was stored."
        )
    factual_sequence = strategy_evidence.get("sequence") if isinstance(strategy_evidence.get("sequence"), list) else []
    factual_details = [
        _safe_text(item.get("detail"))
        for item in factual_sequence
        if isinstance(item, dict) and _safe_text(item.get("detail"))
    ]
    if factual_details:
        ai_decision_story = (
            f"{ai_decision_story} Exact technical proof: "
            + " ".join(factual_details[:3])
        )

    why_opened_parts: List[str] = []
    if signal_score > 0:
        gate_text = f" cleared the score gate {_fmt_score(signal_threshold)}" if signal_threshold > 0 else " provided a positive signal"
        why_opened_parts.append(f"score {_fmt_score(signal_score)}{gate_text}")
    if probability_score > 0:
        why_opened_parts.append(_setup_score_text(probability_score, probability_calibrated))
    if rr > 0:
        why_opened_parts.append(f"R:R {_fmt_number(rr, 2)}")
    if risk_gate_value != "n/a":
        why_opened_parts.append(f"risk gates {risk_gate_value}")
    if ai_review["available"] and ai_review["recommendation"]:
        why_opened_parts.append(
            f"AI review {ai_review['recommendation']} at {_fmt_number(ai_review['confidence'], 2)} confidence"
        )
    if scanner_context:
        why_opened_parts.append(
            f"scanner move {_fmt_number(scanner_context.get('change_pct'), 2)}%"
        )
    if pre_earnings_context:
        why_opened_parts.append(
            f"pre-earnings GapRisk {_fmt_number(pre_earnings_context.get('gap_risk'), 0)}"
        )
    why_opened = (
        f"Opened {direction} {ticker} because " + ", ".join(why_opened_parts) + "."
        if why_opened_parts
        else entry_story
    )

    decision_factors: List[Dict[str, str]] = []
    if signal_score > 0 or signal_threshold > 0:
        decision_factors.append(_decision_factor(
            "Signal edge",
            f"{_fmt_score(signal_score)} / gate {_fmt_score(signal_threshold) if signal_threshold > 0 else 'n/a'}",
            "The setup was eligible only after the signal score cleared the configured threshold.",
        ))
    if probability_score > 0 or quality_score > 0:
        if probability_calibrated:
            decision_factors.append(_decision_factor(
                "Probability and quality",
                f"P {_fmt_score(probability_score)}% / Q {_fmt_score(quality_score)}",
                "Calibrated probability of directional follow-through; quality captures setup cleanliness.",
            ))
        else:
            decision_factors.append(_decision_factor(
                "Setup score and quality",
                f"Setup {_fmt_score(probability_score, 0)}/100 / Q {_fmt_score(quality_score)}",
                "Setup score is a heuristic 0-100 rating of the setup, not a probability of winning "
                "(it is not calibrated against outcomes); quality captures setup cleanliness.",
            ))
    if (
        _safe_text(confidence.get("strategy_source")).upper().startswith("V2")
        and (
            confidence.get("validation_status") == VALIDATION_STATUS_UNAVAILABLE
            or ("validation_status" not in confidence and confidence.get("validation_score") is None)
        )
    ):
        decision_factors.append(_decision_factor(
            "Validation",
            "unavailable (not measured)",
            VALIDATION_UNAVAILABLE_NOTE,
        ))
    if rr > 0:
        decision_factors.append(_decision_factor(
            "Risk reward",
            f"R:R {_fmt_number(rr, 2)}",
            f"Initial stop distance {_fmt_number(stop_distance_pct, 2)}%, target distance {_fmt_number(target_distance_pct, 2)}%.",
        ))
    decision_factors.append(_decision_factor(
        "Risk gates",
        risk_gate_value,
        _safe_text(risk_evaluation.get("reason"), "Risk manager accepted the setup within configured limits."),
    ))
    if scanner_context:
        decision_factors.append(_decision_factor(
            "Scanner context",
            f"{_fmt_number(scanner_context.get('change_pct'), 2)}% move",
            f"Source {scanner_context.get('source', 'scanner')}; price {_fmt_money(scanner_context.get('price'))}.",
        ))
    if pre_earnings_context:
        decision_factors.append(_decision_factor(
            "Pre-earnings context",
            f"GapRisk {_fmt_number(pre_earnings_context.get('gap_risk'), 0)} / bias {_fmt_number(pre_earnings_context.get('direction_bias_score'), 1)}",
            f"Coverage {_fmt_number(pre_earnings_context.get('coverage_score'), 0)}, confidence {pre_earnings_context.get('data_confidence', 'n/a')}, event {pre_earnings_context.get('earnings_time_utc', 'n/a')}.",
        ))
    if ai_review["available"]:
        decision_factors.append(_decision_factor(
            "AI review",
            f"{ai_review['recommendation'] or 'reviewed'} / confidence {_fmt_number(ai_review['confidence'], 2)}",
            ai_review["reason"] or "Ollama reviewed the trade context and did not veto the position.",
        ))
    if news_evidence:
        news_label = "allowed" if news_evidence.get("allowed") else "blocked"
        decision_factors.append(_decision_factor(
            "News evidence",
            f"{news_label} / AI {news_evidence.get('ai_sentiment') or 'unknown'}",
            (
                f"{news_evidence.get('source_backed_count_24h', 0)} source-backed and "
                f"{news_evidence.get('ai_analyzed_count_24h', 0)} AI-analyzed article(s) in 24h; "
                f"{news_evidence.get('source_backed_ai_analyzed_count_24h', 0)} satisfy both. "
                f"{news_evidence.get('reason') or 'No additional News reason recorded.'}"
            ),
        ))
    if capital_entry_gate:
        entry_label = "validated" if capital_entry_gate.get("allowed") else "blocked"
        decision_factors.append(_decision_factor(
            "Capital evidence",
            f"{entry_label} / {_safe_text(capital_entry_gate.get('check'), 'n/a')}",
            _safe_text(
                capital_entry_gate.get("reason"),
                "No Capital-entry evidence reason was retained.",
            ),
        ))
    if bool(payload.get("paper_capital_experiment")) or bool(
        execution.get("paper_capital_experiment")
        or decision_context.get("paper_capital_experiment")
    ):
        paper_strategy = _safe_text(decision_context.get("strategy_type"), "strategy")
        decision_factors.append(_decision_factor(
            "Paper test mode",
            f"{paper_strategy} Capital experiment",
            f"This {paper_strategy} position is simulated only. Price integrity, risk, sentiment, limits, and AI review remain required; OOS and realised-edge evidence are being collected prospectively.",
        ))
    decision_factors.extend(_strategy_evidence_factors(strategy_evidence))
    if research.get("research_only"):
        decision_factors.append(_decision_factor(
            "Execution status",
            "RESEARCH ONLY",
            _safe_text(
                research.get("capital_execution_block_reason"),
                "Visible for forward research; Capital remains blocked until OOS validation passes.",
            ),
        ))
    if sizing_story:
        decision_factors.append(_decision_factor(
            "Sizing",
            f"{_fmt_money(usd_size)} x{leverage}",
            f"Risk used {_fmt_number(sizing.get('risk_pct_used'), 2)}%, size factor {_fmt_number(sizing.get('size_factor'), 2)}.",
        ))
    if confidence.get("tier"):
        decision_factors.append(_decision_factor(
            "Confidence tier",
            confidence.get("tier"),
            "; ".join([str(x) for x in (confidence.get("reasons") or [])][:3]) or "Confidence layer supplied no extra warning.",
        ))

    expectations: List[str] = []
    if entry_price > 0 and target_price > 0:
        direction_text = "fall from entry toward target" if direction == "SHORT" else "rise from entry toward target"
        expectations.append(
            f"Expected path: price should {direction_text} {_fmt_money(target_price)}; target distance is {_fmt_number(target_distance_pct, 2)}%."
        )
    if entry_price > 0 and stop_price > 0:
        invalidation = "above" if direction == "SHORT" else "below"
        expectations.append(
            f"Invalidation: thesis weakens if price trades {invalidation} stop {_fmt_money(stop_price)} ({_fmt_number(stop_distance_pct, 2)}% initial risk)."
        )
    if rr > 0:
        expectations.append(f"Payoff expectation: reward is {_fmt_number(rr, 2)}x the initial risk if TP is reached before SL.")
    if ai_review["sentiment"]:
        expectations.append(f"AI sentiment read: {ai_review['sentiment']}; direction support={ai_review['supports_direction']}.")

    trade_plan: List[str] = []
    _append_unique(trade_plan, f"Monitor current price versus entry {_fmt_money(entry_price)}, stop {_fmt_money(stop_price)}, and target {_fmt_money(target_price)}.")
    _append_unique(trade_plan, "Do not increase exposure unless a fresh signal confirms the same direction and risk gates still pass.")
    for risk_note in ai_review["risks"][:3]:
        _append_unique(trade_plan, risk_note)
    if decision_explanation and "?: ?" not in decision_explanation:
        _append_unique(trade_plan, decision_explanation[:500])

    ai_warnings: List[Dict[str, str]] = []

    def add_warning(source: str, message: Any, *, severity: str = "attention", detail: Any = "") -> None:
        item = _warning_item(source, message, severity=severity, detail=detail)
        if not item["message"]:
            return
        key = (item["source"].lower(), item["message"].lower())
        existing = {(w["source"].lower(), w["message"].lower()) for w in ai_warnings}
        if key not in existing:
            ai_warnings.append(item)

    for risk_note in ai_review["risks"][:6]:
        add_warning("AI risk", risk_note, severity="attention")

    for warning in scoring.get("warnings") or []:
        add_warning("Signal warning", warning, severity="attention")

    for check in risk_checks:
        if isinstance(check, dict) and not bool(check.get("passed")):
            check_name = _safe_text(check.get("name") or check.get("check"), "risk_check")
            add_warning(
                "Risk gate",
                f"{check_name} failed",
                severity="critical",
                detail=check.get("detail") or risk_evaluation.get("reason") or "",
            )

    recommendation = ai_review["recommendation"].lower()
    has_rec_warning = ai_review["available"] and recommendation in {"reduce", "watch", "avoid"}
    if has_rec_warning:
        severity = "critical" if recommendation == "avoid" else "attention"
        add_warning(
            "AI recommendation",
            f"AI recommendation is {recommendation}",
            severity=severity,
            detail=ai_review["reason"],
        )
    if ai_review["supports_direction"] is False:
        direction_detail = (
            ai_review.get("direction_reason")
            or ("" if has_rec_warning else ai_review["reason"])
        )
        add_warning(
            "AI direction check",
            "AI review does not support the current direction",
            severity="critical",
            detail=direction_detail,
        )
    if ai_review["available"] and 0 < ai_review["confidence"] < 0.55:
        add_warning(
            "AI confidence",
            f"AI confidence is low ({_fmt_number(ai_review['confidence'], 2)})",
            severity="attention",
        )
    if news_evidence and not news_evidence.get("allowed"):
        add_warning(
            "News gate",
            "Stored News verdict did not allow autonomous entry",
            severity="critical",
            detail=news_evidence.get("reason") or "",
        )
    if rr > 0 and rr < 1.5:
        add_warning(
            "Risk reward",
            f"Risk/reward is thin at {_fmt_number(rr, 2)}",
            severity="attention",
        )
    if research.get("research_only"):
        add_warning(
            "Validation",
            "Research-only signal cannot open Capital",
            severity="attention",
            detail=_safe_text(
                research.get("capital_execution_block_reason") or research.get("validation_rejection"),
                "Faithful V2 out-of-sample validation is still required.",
            ),
        )

    current_price = _first_positive_float(payload.get("current_price"), entry_price, default=entry_price)
    if is_open and entry_price > 0 and stop_price > 0 and current_price > 0:
        stop_progress = 0.0
        if direction == "SHORT" and stop_price > entry_price:
            stop_progress = ((current_price - entry_price) / (stop_price - entry_price)) * 100.0
        elif direction == "LONG" and stop_price < entry_price:
            stop_progress = ((entry_price - current_price) / (entry_price - stop_price)) * 100.0
        if stop_progress >= 70.0:
            add_warning(
                "Live risk",
                f"Price is {stop_progress:.0f}% of the way toward stop-loss",
                severity="critical" if stop_progress >= 90.0 else "attention",
                detail=f"Current {_fmt_money(current_price)}, stop {_fmt_money(stop_price)}.",
            )
    if is_open and pnl_value < 0:
        add_warning(
            "Live PnL",
            f"Position is currently negative ({_fmt_money(abs(pnl_value))})",
            severity="attention",
        )

    return {
        "reason": _safe_text(decision_context.get("reason")),
        "reasons": reasons,
        "strategy_type": _safe_text(decision_context.get("strategy_type")),
        "scoring": scoring,
        "risk": risk,
        "sizing": sizing,
        "timestamp": _safe_text(decision_context.get("timestamp")),
        "decision_status": _safe_text(decision_context.get("decision_status")),
        "decision_source": decision_source,
        "entry_story": entry_story,
        "exit_story": exit_story,
        "sizing_story": sizing_story,
        "summary": f"{why_opened} {exit_story}. {pnl_label}: ${pnl_value:,.2f}. {sizing_story}.",
        "why_opened": why_opened,
        "ai_decision_story": ai_decision_story,
        "decision_factors": decision_factors,
        "expectations": expectations[:8],
        "trade_plan": trade_plan[:8],
        "useful_notes": trade_plan[:8],
        "ai_warnings": ai_warnings[:10],
        "ai_review": ai_review,
        "decision_explanation": decision_explanation,
        "pnl": round(pnl_value, 6),
        "pnl_label": pnl_label,
        "confidence_tier": confidence.get("tier", "N/A"),
        "confidence_score": _safe_float(confidence.get("score"), _safe_float(confidence.get("adjusted_size_factor"), 0.0)),
        "execution_provider": execution.get("analysis_provider", "N/A"),
        "news_evidence": news_evidence,
        "strategy_evidence": strategy_evidence,
        "entry_chart_snapshot": entry_chart_snapshot,
        "research_only": bool(research.get("research_only")),
    }
