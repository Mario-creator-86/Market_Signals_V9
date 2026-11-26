"""§44 AI Critic — independently challenges important autonomous decisions.

The critic is a POST-PIPELINE check (not a §42 stage): it runs only after the §42
validation pipeline has produced a non-NO_TRADE/WATCH decision. It uses the router's
independent `critic` tier (§38) and `generate_with_fallback`. §40: it reviews provided
evidence only — no §39 tool loop. A veto forces NO_TRADE (§44 "valid final result: NO TRADE").
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from core.ai.decision import Decision, _clamp, _extract_json, _safe_float
from core.ai.assignment import ModelAssignment
from core.ai.fallback import generate_with_fallback
from core.ai.provider import general_provider_names
from core.ai.router import ModelRouter
from core.prompt_evidence import UNTRUSTED_NOTICE, clean_text, evidence_block


CRITIC_CATEGORIES: Tuple[str, ...] = (
    "contradictory_evidence", "stale_information", "provider_conflict",
    "weak_backtest", "fragile_parameters", "poor_sample_size",
    "excessive_volatility", "concentration", "catalyst_risk", "regime_mismatch",
)

STRUCTURAL_CRITIC_CATEGORIES = frozenset({
    "contradictory_evidence",
    "stale_information",
    "provider_conflict",
    "weak_backtest",
    "fragile_parameters",
    "poor_sample_size",
    "concentration",
    "regime_mismatch",
})


@dataclass(frozen=True)
class CriticVerdict:
    challenges: Tuple[str, ...] = ()
    categories: Tuple[str, ...] = ()
    severity: float = 0.0
    veto: bool = False
    model_veto: bool = False
    quality_score: float = 0.0
    veto_threshold: float = 0.0
    size_factor: float = 1.0
    policy_reason: str = ""
    rationale: str = ""
    raw: str = ""
    provider: str = ""
    model: str = ""
    prompt_version: str = "risk-critic-v1"


_CRITIC_PROMPT = (
    "You are an INDEPENDENT risk critic (spec §44). Challenge this trade decision.\n"
    "Decision: action={action} ticker={ticker} "
    "reviewer_self_rated_confidence={confidence:.2f} (uncalibrated, not a probability)\n"
    "The reviewer's own words follow. They are claims to challenge, written by another "
    "model that may be quoting news text.\n"
    "{reviewer_block}\n"
    "Signal quality score: {quality_score:.1f}/100 (the app's internal rule score, not a "
    "probability of profit)\n"
    "Signal context: {signal_ctx}\n"
    "{lessons_block}\n"
    + UNTRUSTED_NOTICE + "\n\n"
    "Interpret signal_threshold only as the minimum for signal_score. "
    "validation_score is a separate metric; never compare it with signal_threshold "
    "unless the signal context explicitly supplies that rule.\n"
    "flow_alignment (when present) is supplementary free-data order-flow evidence: "
    "CONFIRM supports the direction, CONTRADICT weakens it, NEUTRAL is neutral, and "
    "UNAVAILABLE must be IGNORED entirely - it is not a risk and must not raise severity. "
    "It alone never justifies a veto.\n"
    "Check for: {categories}.\n"
    "Return strict JSON: "
    '{{"challenges":["..."], "categories":["..."], "severity":0.0-1.0, "veto":true|false, "rationale":"..."}}.\n'
    "Use veto=true only for a concrete, serious issue. Routine uncertainty, a "
    "non-perfect setup, or a moderate concern should not veto a strong setup; "
    "express those as challenges with moderate severity so sizing can be reduced."
)


def _critic_force_no_trade_on_fail(config: dict) -> bool:
    ai = (config or {}).get("ai") or {}
    return bool((ai.get("decision") or {}).get("critic_force_no_trade_on_fail", True))


def _decision_config(config: Dict[str, Any]) -> Dict[str, Any]:
    ai = (config or {}).get("ai") or {}
    decision = ai.get("decision") or {}
    return decision if isinstance(decision, dict) else {}


def _configured_float(config: Dict[str, Any], key: str, default: float) -> float:
    try:
        return float(_decision_config(config).get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _configured_int(config: Dict[str, Any], key: str, default: int) -> int:
    try:
        return int(_decision_config(config).get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _normalized_quality_score(signal: Dict[str, Any], decision: Decision) -> float:
    """Return the app's mixed 0..1/0..100 quality inputs on a 0..100 scale."""
    values = (
        (signal or {}).get("signal_score"),
        (signal or {}).get("score"),
        (signal or {}).get("candidate_score"),
        (signal or {}).get("pre_earnings_score"),
        getattr(decision, "confidence", 0.0),
    )
    for raw in values:
        score = _safe_float(raw, 0.0)
        if score <= 0:
            continue
        if score <= 1.0:
            score *= 100.0
        return _clamp(score, 0.0, 100.0)
    return 0.0


# Signal fields that hold a MEASURED setup score (strategy backtest /
# validation run). `signal_score`, `score`, `candidate_score` and
# `pre_earnings_score` are rule or move-size scores: for a scanner candidate
# `signal_score` is essentially the size of the 24 h move, which says nothing
# about the quality of the setup.
VALIDATED_SCORE_FIELDS: Tuple[str, ...] = ("validation_score",)


def _veto_override_requires_validated_score(config: Dict[str, Any]) -> bool:
    """`ai.decision.veto_override_requires_validated_score` (default True)."""
    return bool(_decision_config(config).get("veto_override_requires_validated_score", True))


def _validated_quality_score(signal: Dict[str, Any]) -> Optional[float]:
    """The setup's measured validation score on 0..100, or None when unmeasured.

    None for a missing / non-numeric / non-positive score and whenever the
    signal itself declares `validation_score_status: unavailable`.
    """
    payload = signal if isinstance(signal, dict) else {}
    declared = str(
        payload.get("validation_score_status") or payload.get("validation_status") or ""
    ).strip().lower()
    if declared == "unavailable":
        return None
    for key in VALIDATED_SCORE_FIELDS:
        raw = payload.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            score = float(raw)
        except (TypeError, ValueError):
            continue
        if score != score or score in (float("inf"), float("-inf")) or score <= 0:
            continue
        if score <= 1.0:
            score *= 100.0
        return _clamp(score, 0.0, 100.0)
    return None


def _paper_mode(config: Dict[str, Any]) -> bool:
    root = config or {}
    bot_mode = str(((root.get("bot") or {}).get("mode") or "")).strip().lower()
    execution_mode = str(
        ((((root.get("agents") or {}).get("execution") or {}).get("mode")) or "")
    ).strip().lower()
    autonomy_mode = str(((root.get("autonomy") or {}).get("mode") or "")).strip().upper()
    return bot_mode == "paper" or execution_mode == "paper" or autonomy_mode == "PAPER_AUTO"


def _failure_verdict(
    reason: str,
    *,
    signal: Dict[str, Any],
    decision: Decision,
    config: Dict[str, Any],
    raw: str = "",
    provider: str = "",
    model: str = "",
) -> CriticVerdict:
    """Verdict when the critic itself is unavailable or unreadable.

    Fail-closed by default. With `critic_fail_open_good_paper_setup` enabled, a
    high-quality paper setup is let through at reduced size instead, so a
    formatting/provider failure does not block every paper entry.
    """
    quality_score = _normalized_quality_score(signal, decision)
    poor_score = _clamp(_configured_float(config, "critic_poor_setup_score", 68.0), 0.0, 100.0)
    good_score = _clamp(_configured_float(config, "critic_good_setup_score", 78.0), poor_score, 100.0)
    base_threshold = _clamp(_configured_float(config, "critic_veto_min_severity", 0.80), 0.0, 1.0)
    good_threshold = _clamp(
        _configured_float(config, "critic_good_setup_veto_severity", 0.90),
        base_threshold,
        1.0,
    )
    min_size_factor = _clamp(_configured_float(config, "critic_min_size_factor", 0.50), 0.1, 1.0)
    low_confidence_threshold = _clamp(
        _configured_float(config, "critic_low_confidence_threshold", 0.50),
        0.0,
        1.0,
    )
    decision_confidence = _clamp(_safe_float(decision.confidence, 0.0), 0.0, 1.0)
    # Off by default: a critic that could not answer has not approved anything.
    # Passing a failed review at reduced size is an explicit opt-in.
    # The fail-open shortcut is a score-based pass as well: with the validated
    # score rule on, only a measured score can earn it.
    fail_open_score = quality_score
    if _veto_override_requires_validated_score(config):
        measured = _validated_quality_score(signal)
        fail_open_score = measured if measured is not None else 0.0
    good_paper_setup = (
        bool(_decision_config(config).get("critic_fail_open_good_paper_setup", False))
        and _paper_mode(config)
        and fail_open_score >= good_score
        and decision_confidence >= low_confidence_threshold
        and not decision.requires_more_data
    )
    veto = bool(_critic_force_no_trade_on_fail(config) and not good_paper_setup)
    return CriticVerdict(
        veto=veto,
        quality_score=quality_score,
        veto_threshold=good_threshold if quality_score >= good_score else base_threshold,
        size_factor=1.0 if veto else min_size_factor,
        policy_reason=(
            f"critic_failure_veto:quality={quality_score:.1f}"
            if veto
            else (
                "critic_failure_good_paper_setup_reduced:"
                f"quality={quality_score:.1f} confidence={decision_confidence:.2f}"
            )
        ),
        rationale=reason,
        raw=raw,
        provider=provider,
        model=model,
    )


def _apply_veto_policy(
    parsed: Dict[str, Any],
    *,
    signal: Dict[str, Any],
    decision: Decision,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Calibrate the model's challenge into an app-authoritative veto.

    Strong setups tolerate ordinary criticism, while weak setups are rejected
    at a lower severity. A genuinely critical finding still blocks any score.
    This keeps the critic useful without turning it into an always-NO_TRADE
    model, and makes its final veto deterministic and auditable.
    """
    severity = _clamp(_safe_float(parsed.get("severity"), 0.0), 0.0, 1.0)
    quality_score = _normalized_quality_score(signal, decision)
    model_veto = bool(_explicit_bool(parsed.get("veto")))
    categories = [c for c in _as_list(parsed.get("categories")) if isinstance(c, str) and c in CRITIC_CATEGORIES]
    challenges = [str(c).strip()[:600] for c in _as_list(parsed.get("challenges")) if str(c).strip()][:12]
    rationale = str(parsed.get("rationale") or "").strip()[:2000]
    has_concrete_issue = bool(categories or challenges or rationale)

    poor_score = _clamp(_configured_float(config, "critic_poor_setup_score", 68.0), 0.0, 100.0)
    good_score = _clamp(_configured_float(config, "critic_good_setup_score", 78.0), poor_score, 100.0)
    poor_threshold = _clamp(_configured_float(config, "critic_poor_setup_veto_severity", 0.65), 0.0, 1.0)
    base_threshold = _clamp(_configured_float(config, "critic_veto_min_severity", 0.80), poor_threshold, 1.0)
    good_threshold = _clamp(_configured_float(config, "critic_good_setup_veto_severity", 0.90), base_threshold, 1.0)
    critical_threshold = _clamp(_configured_float(config, "critic_critical_veto_severity", 0.95), good_threshold, 1.0)
    reduce_threshold = _clamp(_configured_float(config, "critic_reduce_min_severity", 0.50), 0.0, base_threshold)
    min_size_factor = _clamp(_configured_float(config, "critic_min_size_factor", 0.50), 0.1, 1.0)
    low_confidence_threshold = _clamp(
        _configured_float(config, "critic_low_confidence_threshold", 0.50),
        0.0,
        1.0,
    )
    low_confidence_veto_severity = _clamp(
        _configured_float(config, "critic_low_confidence_veto_severity", 0.75),
        poor_threshold,
        1.0,
    )
    structural_veto_severity = _clamp(
        _configured_float(config, "critic_structural_veto_severity", 0.80),
        poor_threshold,
        1.0,
    )
    structural_min_categories = max(
        1,
        _configured_int(config, "critic_structural_min_categories", 2),
    )

    if quality_score >= good_score:
        threshold = good_threshold
        score_band = "good"
    elif quality_score < poor_score:
        threshold = poor_threshold
        score_band = "poor"
    else:
        threshold = base_threshold
        score_band = "standard"

    decision_confidence = _clamp(_safe_float(decision.confidence, 0.0), 0.0, 1.0)
    structural_categories = sorted(set(categories).intersection(STRUCTURAL_CRITIC_CATEGORIES))
    low_confidence_finding = (
        model_veto
        and has_concrete_issue
        and decision_confidence < low_confidence_threshold
    )
    structural_finding = (
        model_veto
        and len(structural_categories) >= structural_min_categories
    )
    if low_confidence_finding:
        threshold = min(threshold, low_confidence_veto_severity)
    if structural_finding:
        threshold = min(threshold, structural_veto_severity)

    critical_veto = model_veto and has_concrete_issue and severity >= critical_threshold
    weak_setup_veto = (
        quality_score < poor_score
        and has_concrete_issue
        and severity >= poor_threshold
    )
    low_confidence_veto = low_confidence_finding and severity >= low_confidence_veto_severity
    structural_veto = structural_finding and severity >= structural_veto_severity
    calibrated_model_veto = model_veto and has_concrete_issue and severity >= threshold
    veto = bool(
        critical_veto
        or weak_setup_veto
        or low_confidence_veto
        or structural_veto
        or calibrated_model_veto
    )

    # A model veto that survives to this point is about to be overridden by the
    # score band. Only a MEASURED score (strategy backtest / validation) may do
    # that: a move-size or rule score is not evidence that the setup is good.
    # (Live: SOXLB-USD - model veto downgraded on a score that was the size of
    # the 24 h move, entry accepted at AI confidence 0.35.)
    validated_score = _validated_quality_score(signal)
    unvalidated_override_refused = False
    validated_band_veto = False
    validated_threshold = threshold
    if model_veto and not veto and _veto_override_requires_validated_score(config):
        if validated_score is None:
            unvalidated_override_refused = True
            veto = True
        else:
            if validated_score >= good_score:
                validated_threshold = good_threshold
            elif validated_score < poor_score:
                validated_threshold = poor_threshold
            else:
                validated_threshold = base_threshold
            # Never looser than the band the rule score already earned.
            validated_threshold = min(validated_threshold, threshold)
            if severity >= validated_threshold:
                validated_band_veto = True
                veto = True

    size_factor = 1.0
    if not veto and severity >= reduce_threshold:
        size_factor = max(min_size_factor, round(1.0 - (severity * 0.5), 4))

    if critical_veto:
        reason = f"veto:critical severity={severity:.2f} threshold={critical_threshold:.2f}"
    elif weak_setup_veto:
        reason = f"veto:{score_band}_setup severity={severity:.2f} threshold={threshold:.2f}"
    elif low_confidence_veto:
        reason = (
            "veto:low_confidence "
            f"confidence={decision_confidence:.2f} severity={severity:.2f} threshold={threshold:.2f}"
        )
    elif structural_veto:
        reason = (
            "veto:structural_conflict "
            f"categories={len(structural_categories)} severity={severity:.2f} threshold={threshold:.2f}"
        )
    elif calibrated_model_veto:
        reason = f"veto:{score_band}_setup severity={severity:.2f} threshold={threshold:.2f}"
    elif unvalidated_override_refused:
        reason = (
            "veto:model_veto_upheld_no_validated_score "
            f"severity={severity:.2f} rule_score={quality_score:.1f} (not a measured validation score)"
        )
    elif validated_band_veto:
        reason = (
            "veto:model_veto_upheld_validated_score "
            f"severity={severity:.2f} threshold={validated_threshold:.2f} validated_score={validated_score:.1f}"
        )
    elif model_veto:
        reason = f"model_veto_downgraded:{score_band}_setup severity={severity:.2f} threshold={threshold:.2f}"
        if validated_score is not None:
            reason += f" validated_score={validated_score:.1f}"
    elif size_factor < 1.0:
        reason = f"concern_reduces_size:severity={severity:.2f}"
    else:
        reason = "critic_pass"

    return {
        "severity": severity,
        "quality_score": quality_score,
        "model_veto": model_veto,
        "veto": veto,
        "veto_threshold": validated_threshold if validated_band_veto else threshold,
        "size_factor": size_factor,
        "policy_reason": reason,
        "categories": categories,
        "challenges": challenges,
        "rationale": rationale,
    }


def _explicit_bool(value: Any) -> Optional[bool]:
    """True/False for a real boolean or the words true/false; None otherwise.

    `bool("false")` is True, so a model that answered `"veto": "false"` used to
    be read as a veto, and `"veto": null` as "no veto".
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        word = value.strip().lower()
        if word in {"true", "yes"}:
            return True
        if word in {"false", "no"}:
            return False
    return None


def _as_list(value: Any) -> list:
    """A list stays a list; a lone string becomes one item (never its letters)."""
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _critic_timeout(config: Dict[str, Any]) -> float:
    """Seconds one critic call may take: the configured review timeout."""
    try:
        seconds = float(((config or {}).get("ollama") or {}).get("decision_timeout_seconds") or 0.0)
    except (TypeError, ValueError):
        seconds = 0.0
    return seconds if seconds > 0 and seconds == seconds else 0.0


def _compact_signal(signal: Dict[str, Any]) -> str:
    if not isinstance(signal, dict):
        return ""
    labels = (
        ("ticker", "ticker"),
        ("strategy_type", "strategy_type"),
        ("signal_score", "signal_score"),
        ("signal_threshold", "signal_threshold_for_signal_score"),
        ("validation_score", "validation_score_separate_metric"),
        ("entry", "entry"),
        ("tp", "tp"),
        ("sl", "sl"),
        ("data_quality", "data_quality"),
        ("autonomous_source", "autonomous_source"),
        ("signal_mode", "signal_mode"),
        ("gap_risk", "gap_risk"),
        ("pre_earnings_window_open", "pre_earnings_window_open"),
        ("session", "session"),
        ("session_status", "session_status"),
    )
    parts = [
        f"{label}={signal.get(key)}"
        for key, label in labels
        if key in signal and not (key == "validation_score" and signal.get(key) is None)
    ]
    if "validation_score" not in signal or signal.get("validation_score") is None:
        if signal.get("validation_score_status"):
            parts.append(f"validation_score={signal.get('validation_score_status')} (not measured)")
    flow = signal.get("flow_evidence")
    if isinstance(flow, dict) and flow.get("alignment"):
        # evidence-only context for the critic: never a standalone veto reason
        parts.append(f"flow_alignment={flow.get('alignment')} ({str(flow.get('note') or '')[:90]})")
    base_rates = signal.get("strategy_base_rates")
    if isinstance(base_rates, dict):
        exact = base_rates.get("exact") if isinstance(base_rates.get("exact"), dict) else None
        family = base_rates.get("family") if isinstance(base_rates.get("family"), dict) else None
        bucket = exact or family
        if isinstance(bucket, dict) and bucket.get("n") is not None:
            parts.append(
                f"strategy_base_rates(n={bucket.get('n')}, win={bucket.get('win_rate_pct')}%, "
                f"avg={bucket.get('avg_pnl_pct')}%, small_sample={bucket.get('small_sample')})"
            )
    lessons = signal.get("strategy_lessons")
    if isinstance(lessons, list) and lessons:
        # The lesson text itself goes into the marked evidence block.
        parts.append(f"lessons_available={len(lessons)}")
    day = signal.get("market_context")
    if isinstance(day, dict):
        # How broad today's move is among the scanner's movers: context only.
        stocks = day.get("stocks") if isinstance(day.get("stocks"), dict) else {}
        coins = day.get("crypto") if isinstance(day.get("crypto"), dict) else {}
        bits = []
        if stocks:
            bits.append(f"stock movers {stocks.get('reading')} (share up {stocks.get('share_up')})")
        if coins:
            bits.append(f"crypto {coins.get('reading')} (share up {coins.get('share_up_24h')}, BTC 24h {coins.get('btc_change_24h_pct')}%)")
        if bits:
            parts.append("market_context(" + "; ".join(bits) + "; context only, not a signal)")
    record = signal.get("ai_track_record")
    if isinstance(record, dict):
        # The app's replay of past reviews of this strategy: evidence only.
        taken = record.get("taken") if isinstance(record.get("taken"), dict) else {}
        rejected = record.get("rejected_by_ai") if isinstance(record.get("rejected_by_ai"), dict) else {}
        if taken.get("independent_n") or rejected.get("independent_n"):
            parts.append(
                f"ai_track_record(approved n={taken.get('independent_n')} avg={taken.get('mean_net_pct')}%, "
                f"rejected n={rejected.get('independent_n')} avg={rejected.get('mean_net_pct')}%; "
                "replayed under one exit rule, net of costs)"
            )
    return ", ".join(parts)


def _lessons_block(signal: Dict[str, Any]) -> str:
    lessons = signal.get("strategy_lessons") if isinstance(signal, dict) else None
    if not isinstance(lessons, list) or not lessons:
        return ""
    return evidence_block("strategy_lessons", lessons[:3], limit=3, max_chars=240)


def _reviewer_block(decision: Decision) -> str:
    lines = [f"thesis: {clean_text(decision.thesis, max_chars=1000)}"]
    for label, items in (
        ("supporting", decision.supporting_evidence),
        ("opposing", decision.opposing_evidence),
        ("risk_flag", decision.risk_flags),
    ):
        lines.extend(f"{label}: {item}" for item in list(items or [])[:6])
    return evidence_block("reviewer_output", lines, limit=20, max_chars=1100)


def _parse_critic_json(raw_text: str) -> Optional[dict]:
    js = _extract_json(raw_text or "")
    if js is None:
        return None
    try:
        return json.loads(js)
    except Exception:
        return None


def _resolve_critic_provider(config: dict, services: dict) -> Tuple[Any, Optional[str]]:
    """Build a critic provider + model from config. Returns (None, model) if none available."""
    model = None
    try:
        assignment = ModelAssignment.from_config(config)
        model = ModelRouter.from_config(config, assignment).select_model(
            "risk_critic", tier_hint="critic"
        )
    except Exception:
        model = None
    # Lazy import to avoid a core->api cycle at import time.
    from api.ai_config_service import build_provider
    ai_cfg = (config or {}).get("ai") or {}
    # The assigned model belongs to the primary provider, so try it first
    # instead of whichever provider happens to be listed first.
    primary = str(ai_cfg.get("primary") or "")
    general = general_provider_names(config)
    names = ([primary] if primary in general else []) + [n for n in general if n != primary]
    for name in names:
        try:
            prov = build_provider(config, name)
        except Exception:
            prov = None
        if prov is not None:
            return prov, model
    return None, model


def run_critic(decision: Decision, signal: Dict[str, Any], *,
               config: Dict[str, Any], services: Optional[Dict[str, Any]] = None) -> CriticVerdict:
    """§44 — independently challenge `decision`. Injectable via `services` (no network in tests)."""
    svc = services or {}
    provider = svc.get("critic_provider")
    model = svc.get("critic_model")
    # Only attempt config resolution when the caller did NOT explicitly inject a critic_provider key.
    if provider is None and "critic_provider" not in svc:
        provider, model = _resolve_critic_provider(config, svc)
    if provider is None:
        return _failure_verdict(
            "critic provider unavailable",
            signal=signal,
            decision=decision,
            config=config,
            model=str(model or ""),
        )

    provider_name = str(getattr(provider, "name", "") or "")

    quality_score = _normalized_quality_score(signal, decision)
    prompt = _CRITIC_PROMPT.format(
        action=decision.action.value, ticker=clean_text(decision.ticker, max_chars=32),
        confidence=decision.confidence,
        reviewer_block=_reviewer_block(decision),
        lessons_block=_lessons_block(signal),
        quality_score=quality_score,
        signal_ctx=_compact_signal(signal),
        categories=", ".join(CRITIC_CATEGORIES),
    )
    # The critic runs inside the trading cycle: it gets the same per-call
    # timeout as the review it challenges instead of each provider's default.
    call_opts: Dict[str, Any] = {}
    critic_timeout = _critic_timeout(config)
    if critic_timeout > 0:
        call_opts["timeout_override"] = critic_timeout
    try:
        resp = generate_with_fallback(provider, prompt, model=model,
                                      system="You are an independent risk critic.", **call_opts)
    except Exception:
        return _failure_verdict(
            "critic call failed",
            signal=signal,
            decision=decision,
            config=config,
            provider=provider_name,
            model=str(model or ""),
        )

    raw_text = getattr(resp, "text", "") or ""
    parsed = _parse_critic_json(raw_text)
    # A reply without an explicit verdict (e.g. `{}`) is not a pass: it is a
    # critic that did not answer.
    if not isinstance(parsed, dict) or _explicit_bool(parsed.get("veto")) is None:
        return _failure_verdict(
            "critic output unparseable",
            signal=signal,
            decision=decision,
            config=config,
            raw=raw_text,
            provider=provider_name,
            model=str(getattr(resp, "model", "") or model or ""),
        )
    policy = _apply_veto_policy(
        parsed,
        signal=signal,
        decision=decision,
        config=config,
    )
    return CriticVerdict(
        challenges=tuple(policy["challenges"]),
        categories=tuple(policy["categories"]),
        severity=policy["severity"],
        veto=policy["veto"],
        model_veto=policy["model_veto"],
        quality_score=policy["quality_score"],
        veto_threshold=policy["veto_threshold"],
        size_factor=policy["size_factor"],
        policy_reason=policy["policy_reason"],
        rationale=policy["rationale"],
        raw=raw_text,
        provider=provider_name,
        model=str(getattr(resp, "model", "") or model or ""),
    )
