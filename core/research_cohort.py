"""Strictly isolated forward-paper cohort admission rules.

Research cohort positions collect prospective, execution-price-verified outcomes
for hypotheses that are not eligible for Capital execution yet.  They are never
used by the realised-performance cache or Capital's evidence gate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from core.autonomous_trade_guard import evaluate_autonomous_news, is_autonomous_candidate


DEFAULT_RESEARCH_COHORT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "paper_only": True,
    "allowed_sources": ["scanner"],
    "allowed_strategy_sources": [],
    "max_open_positions": 1,
    "notional_pct_of_balance": 0.25,
    "min_notional_usd": 20.0,
    "max_notional_usd": 250.0,
    "max_signal_age_seconds": 180,
    "min_signal_score": 85.0,
    "min_risk_reward_ratio": 1.5,
    "max_entry_deviation_pct": 3.0,
    "ai_review_enabled": False,
    "block_ai_veto": True,
    # Research is price-first, not news-blind. A missing article must not turn
    # a valid, liquid technical observation into a permanently empty cohort,
    # but verified directional conflict still vetoes it. News remains contextual
    # for every crypto asset; this policy never affects Capital validation.
    "news_policy": {
        "allow_missing_context": False,
        "require_context_for_major_crypto": False,
        "block_directional_conflict": True,
    },
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    return str(value or "").strip()


def _direction(signal: Dict[str, Any]) -> str:
    raw = _text(signal.get("direction") or signal.get("action") or signal.get("type")).upper()
    if raw in {"BUY", "LONG"}:
        return "LONG"
    if raw in {"SELL", "SHORT"}:
        return "SHORT"
    return ""


def _utc_datetime(value: Any) -> Optional[datetime]:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def research_cohort_config(execution_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    execution = execution_config if isinstance(execution_config, dict) else {}
    configured = execution.get("research_cohort")
    cfg = dict(DEFAULT_RESEARCH_COHORT_CONFIG)
    if isinstance(configured, dict):
        cfg.update(configured)
    sources = cfg.get("allowed_sources")
    cfg["allowed_sources"] = {
        _text(value).lower() for value in (sources if isinstance(sources, list) else []) if _text(value)
    }
    strategy_sources = cfg.get("allowed_strategy_sources")
    cfg["allowed_strategy_sources"] = {
        _text(value).upper()
        for value in (strategy_sources if isinstance(strategy_sources, list) else [])
        if _text(value)
    }
    policy = cfg.get("news_policy") if isinstance(cfg.get("news_policy"), dict) else {}
    cfg["news_policy"] = {
        **dict(DEFAULT_RESEARCH_COHORT_CONFIG["news_policy"]),
        **policy,
    }
    return cfg


def _levels(signal: Dict[str, Any], direction: str) -> tuple[float, float, float, float]:
    entry = _number(signal.get("entry"), 0.0)
    stop = _number(signal.get("sl", signal.get("stop_loss")), 0.0)
    target = _number(signal.get("tp", signal.get("take_profit")), 0.0)
    if entry <= 0 or stop <= 0 or target <= 0:
        return entry, 0.0, 0.0, 0.0
    stop_pct = abs(entry - stop) / entry * 100.0
    target_pct = abs(target - entry) / entry * 100.0
    correct_sides = (direction == "LONG" and stop < entry < target) or (
        direction == "SHORT" and target < entry < stop
    )
    risk_reward = target_pct / stop_pct if correct_sides and stop_pct > 0 else 0.0
    return entry, stop_pct, target_pct, risk_reward


def _open_research_positions(positions: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    return [
        dict(position)
        for position in positions or []
        if isinstance(position, dict)
        and _text(position.get("status")).upper() == "OPEN"
        and (
            position.get("research_only") is True
            or _text(position.get("position_type")).lower() == "research_cohort"
        )
    ]


_MISSING_NEWS_STATUSES = frozenset({
    "unavailable",
    "insufficient_evidence",
    "unverifiable_sources",
    "ai_analysis_unavailable",
    "ai_analysis_unverifiable",
})


def _research_news_decision(
    payload: Dict[str, Any],
    execution_config: Optional[Dict[str, Any]],
    cfg: Dict[str, Any],
    supplied_decision: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Apply the separate forward-paper News policy without weakening Capital.

    ``evaluate_autonomous_news`` stays the authoritative source-backed AI
    analysis. This wrapper only changes *missing-context* handling for the
    isolated cohort. A verified conflicting article is never converted into
    approval, and the original decision remains attached for audit.
    """
    decision = (
        dict(supplied_decision)
        if isinstance(supplied_decision, dict)
        else evaluate_autonomous_news(payload, execution_config)
    )
    if decision.get("allowed"):
        return decision

    policy = cfg.get("news_policy") if isinstance(cfg.get("news_policy"), dict) else {}
    if not bool(policy.get("allow_missing_context", False)):
        return decision

    status = _text(decision.get("status")).lower()
    crypto_policy = decision.get("crypto_policy") if isinstance(decision.get("crypto_policy"), dict) else {}
    if bool(policy.get("require_context_for_major_crypto", False)) and bool(crypto_policy.get("is_major_crypto")):
        return decision
    if status not in _MISSING_NEWS_STATUSES:
        # In particular, direction_conflict remains a hard veto.
        return decision

    context = decision.get("context") if isinstance(decision.get("context"), dict) else {}
    return {
        **decision,
        "allowed": True,
        "status": "missing_context_price_first",
        "reason": (
            "No usable fresh News context; admitting the bounded price-first "
            "research observation. Verified conflicting News would still veto it."
        ),
        "context": context,
        "research_news_policy": {
            "mode": "price_first_news_contextual",
            "missing_context_original_status": status or "unknown",
            "requires_major_crypto_context": bool(policy.get("require_context_for_major_crypto", False)),
            "capital_policy_changed": False,
        },
    }


def evaluate_research_cohort_entry(
    signal: Dict[str, Any],
    execution_config: Optional[Dict[str, Any]],
    positions: Iterable[Dict[str, Any]],
    *,
    news_decision: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return a fail-closed admission decision for a paper research trade."""
    payload = dict(signal or {})
    cfg = research_cohort_config(execution_config)
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ticker = _text(payload.get("ticker")).upper()
    direction = _direction(payload)
    source = _text(payload.get("autonomous_source")).lower()
    source_set = cfg["allowed_sources"]
    strategy_source = _text(payload.get("strategy_source")).upper()
    strategy_source_set = cfg["allowed_strategy_sources"]
    signal_score = _number(payload.get("signal_score", payload.get("score")), 0.0)
    expected_entry, stop_pct, target_pct, risk_reward = _levels(payload, direction)
    created_at = _utc_datetime(payload.get("created_at") or payload.get("timestamp"))
    age_seconds = (now_utc - created_at).total_seconds() if created_at else None
    research_positions = _open_research_positions(positions)
    base = {
        "gate": "research_cohort",
        "evaluated_at": now_utc.isoformat(),
        "ticker": ticker,
        "direction": direction,
        "source": source,
        "research_only": True,
        "position_type": "research_cohort",
        "thresholds": {
            "max_open_positions": max(1, int(_number(cfg.get("max_open_positions"), 1))),
            "max_signal_age_seconds": max(1, int(_number(cfg.get("max_signal_age_seconds"), 180))),
            "min_signal_score": _number(cfg.get("min_signal_score"), 85.0),
            "min_risk_reward_ratio": _number(cfg.get("min_risk_reward_ratio"), 1.5),
            "max_entry_deviation_pct": max(0.0, _number(cfg.get("max_entry_deviation_pct"), 3.0)),
        },
        "metrics": {
            "signal_score": round(signal_score, 4),
            "risk_reward_ratio": round(risk_reward, 4),
            "signal_age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
            "research_open_positions": len(research_positions),
            "expected_entry_price": round(expected_entry, 8) if expected_entry > 0 else None,
            "stop_loss_pct": round(stop_pct, 6),
            "take_profit_pct": round(target_pct, 6),
        },
    }

    def deny(status: str, reason: str, **extra: Any) -> Dict[str, Any]:
        return {**base, "allowed": False, "status": status, "reason": reason, **extra}

    if not bool(cfg.get("enabled", False)):
        return deny("disabled", "Research cohort is disabled")
    if bool(cfg.get("paper_only", True)) and _text((execution_config or {}).get("mode") or "paper").lower() != "paper":
        return deny("not_paper_mode", "Research cohort is restricted to paper mode")
    if not is_autonomous_candidate(payload):
        return deny("not_autonomous", "Only autonomous candidates can enter the research cohort")
    if not ticker or not direction:
        return deny("invalid_signal", "Ticker or direction is missing")
    if source_set and source not in source_set:
        return deny("source_not_allowed", f"Autonomous source {source or 'unknown'} is not enabled for research")
    if strategy_source_set and strategy_source not in strategy_source_set:
        return deny(
            "strategy_source_not_allowed",
            f"Strategy source {strategy_source or 'unknown'} is not enabled for research",
        )
    if age_seconds is None:
        return deny("stale_signal", "Signal is missing a fresh creation timestamp")
    if age_seconds < 0:
        return deny("stale_signal", "Signal creation timestamp is in the future")
    max_signal_age = base["thresholds"]["max_signal_age_seconds"]
    if age_seconds > max_signal_age:
        return deny(
            "stale_signal",
            f"Signal age {age_seconds:.0f}s exceeds research limit {max_signal_age}s",
        )
    if signal_score < base["thresholds"]["min_signal_score"]:
        return deny("low_score", f"Signal score {signal_score:.1f} is below research threshold")
    if expected_entry <= 0 or stop_pct <= 0 or target_pct <= 0:
        return deny("invalid_levels", "Signal lacks valid entry, stop, or target levels")
    # Levels are derived from decimal market prices.  Do not reject a setup
    # that equals the configured threshold solely because of binary float noise.
    if risk_reward + 1e-9 < base["thresholds"]["min_risk_reward_ratio"]:
        return deny("low_risk_reward", f"R:R {risk_reward:.2f} is below research threshold")

    matching_open = [
        position for position in research_positions
        if _text(position.get("ticker")).upper() == ticker
        and _text(position.get("direction")).upper() == direction
    ]
    if matching_open:
        return deny("duplicate_open", "A matching research cohort position is already open")
    if len(research_positions) >= base["thresholds"]["max_open_positions"]:
        return deny("capacity_reached", "Research cohort maximum open-position count reached")

    decision = _research_news_decision(payload, execution_config, cfg, news_decision)
    if not decision.get("allowed"):
        return deny(
            "news_gate",
            _text(decision.get("reason")) or "Fresh source-backed AI News evidence is required",
            news_gate=decision,
        )
    return {
        **base,
        "allowed": True,
        "status": "eligible",
        "reason": "Eligible for isolated forward paper research; not Capital-executable",
        "news_gate": decision,
    }


def research_cohort_notional(balance: float, execution_config: Optional[Dict[str, Any]]) -> float:
    """Return a bounded paper notional; never infer a size from signal confidence."""
    cfg = research_cohort_config(execution_config)
    available = max(0.0, _number(balance, 0.0))
    candidate = available * max(0.0, _number(cfg.get("notional_pct_of_balance"), 0.25)) / 100.0
    max_notional = max(0.0, _number(cfg.get("max_notional_usd"), 250.0))
    if max_notional > 0:
        candidate = min(candidate, max_notional)
    return round(candidate, 6)
