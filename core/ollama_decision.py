import importlib
import json
import logging
import math
import time
from typing import Any, Dict, List, Optional

from core.prompt_evidence import UNTRUSTED_FIELDS_NOTICE, evidence_field_marker, quarantine_fields
from core.flow_indicators.evidence import (
    ALIGNMENT_MIN_ABS_SCORE,
    ALIGNMENT_MIN_CONFIDENCE,
)

logger = logging.getLogger("OllamaDecision")

# Cache for Ollama availability checks (30s TTL)
_availability_cache: Dict[str, Any] = {"available": False, "timestamp": 0.0}
_AVAILABILITY_CACHE_TTL = 30.0

ALLOWED_RECOMMENDATIONS = {"approve", "reduce", "avoid", "watch"}
ALLOWED_SENTIMENT = {"bullish", "bearish", "neutral"}
ALLOWED_EXIT_ACTIONS = {"hold", "close", "tighten_stop"}
ALLOWED_NEWS_SEVERITIES = {"none", "low", "moderate", "high", "critical", "unknown"}

# What a review's `confidence` is: the model's own rating of its answer. It has
# never been calibrated against outcomes and is not a probability of profit.
CONFIDENCE_BASIS = "model_self_reported_uncalibrated"


def _safe_float(value: Any, default: float = 0.0) -> float:
    """float(value), or `default` for anything that is not a finite number.

    json.loads accepts NaN/Infinity, and `max(lo, min(hi, nan))` returns `hi`:
    without this check a model that answered `"confidence": NaN` was read as
    confidence 1.0 and `"size_factor": NaN` as full size. `true`/`false` is a
    wrong type, not 1.0/0.0.
    """
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except Exception:
        return default
    return number if math.isfinite(number) else default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _extract_json_object(raw_text: str) -> Optional[str]:
    text = str(raw_text or "")
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for idx, ch in enumerate(text[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def _list_str(value: Any, limit: int = 6) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text[:300])
        if len(out) >= limit:
            break
    return out


def default_trade_review(*, available: bool = False, reason: str = "") -> Dict[str, Any]:
    return {
        "available": available,
        "provider": "ollama",
        "recommendation": "watch",
        "confidence": 0.0,
        "size_factor": 1.0,
        "leverage": 1,
        "sentiment": "neutral",
        "supports_direction": None,
        "reason": reason or "Ollama review unavailable",
        "risks": [],
        "raw_response": "",
        "fallback_used": "unavailable" if not available else "",
    }


def parse_trade_review(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        parsed = raw
    else:
        snippet = _extract_json_object(str(raw or ""))
        if not snippet:
            review = default_trade_review(reason="Ollama response did not contain JSON")
            review["raw_response"] = str(raw or "")[:2000]
            review["fallback_used"] = "invalid_json"
            return review
        try:
            parsed = json.loads(snippet)
        except Exception:
            review = default_trade_review(reason="Ollama response JSON could not be parsed")
            review["raw_response"] = str(raw or "")[:2000]
            review["fallback_used"] = "invalid_json"
            return review
    if not isinstance(parsed, dict):
        review = default_trade_review(reason="Ollama response JSON was not an object")
        review["raw_response"] = str(raw or "")[:2000]
        review["fallback_used"] = "invalid_json"
        return review

    review = default_trade_review(available=True, reason="")
    recommendation = str(parsed.get("recommendation") or "").lower().strip()
    if recommendation in ALLOWED_RECOMMENDATIONS:
        review["recommendation"] = recommendation

    sentiment = str(parsed.get("sentiment") or "").lower().strip()
    if sentiment in ALLOWED_SENTIMENT:
        review["sentiment"] = sentiment

    review["confidence"] = round(_clamp(_safe_float(parsed.get("confidence"), 0.0), 0.0, 1.0), 4)
    review["confidence_basis"] = CONFIDENCE_BASIS
    review["size_factor"] = round(_clamp(_safe_float(parsed.get("size_factor"), 1.0), 0.0, 1.0), 4)
    review["leverage"] = int(round(_clamp(_safe_float(parsed.get("leverage"), 1.0), 1.0, 5.0)))
    if isinstance(parsed.get("supports_direction"), bool):
        review["supports_direction"] = parsed.get("supports_direction")
    review["reason"] = str(parsed.get("reason") or "The AI reviewer gave no reason.").strip()[:700]
    review["risks"] = _list_str(parsed.get("risks"), limit=6)
    review["raw_response"] = str(raw or "")[:2000]
    review["fallback_used"] = ""
    return review


def _compact_signal(signal: Dict[str, Any]) -> Dict[str, Any]:
    keys = [
        "id",
        "ticker",
        "action",
        "type",
        "entry",
        "tp",
        "sl",
        "signal_score",
        "probability_score",
        "quality_score",
        "risk_reward_ratio",
        "rr_ratio",  # engine V1/V2 signals use rr_ratio (not risk_reward_ratio);
                     # autonomous candidates use risk_reward_ratio. Whitelist both
                     # so R:R reaches the AI reviewer regardless of producer.
        "strategy_type",
        "strategy_id",
        "signal_mode",
        "variant",
        "risk_mode",
        "source",
        "autonomous_source",
        "sentiment_score",
        "sentiment_label",
        "gap_risk",
        "pre_earnings_gap_risk",
        "gap_risk_level",
        "direction_bias_score",
        "direction_label",
        "coverage_score",
        "expected_move_pct",
        "trade_radar_opportunity",
        "composite_score",
        "agent_reasons",
        "confidence_tier",
        "confidence_reasons",
        # Decision-relevant context the engine computes but pre-fix never reached
        # the AI reviewer (directive: "toate informațiile valabile"). Regime/MTF
        # context, 30-day performance history, and V2 sub-scores let the model
        # weigh trend alignment and recent realized edge, not just news+basics.
        "market_regime",
        "regime_detected",
        "regime_strength",
        "regime_details",
        "mtf_alignment",
        "mtf_score",
        "mtf_trends",
        "adx_5m",
        "bb_width_5m",
        "atr_percentile_5m",
        "win_rate_30d",
        "expectancy_R_30d",
        "profit_factor_30d",
        "trades_30d",
        "quick_backtest_30d",
        "catalyst_score",
        "setup_score",
        "risk_score",
        "validation_score",
        # "unavailable" when the producer has no validation for the setup, so
        # the reviewer is told it was not measured instead of guessing.
        "validation_score_status",
        # Round-3 (AI-3): additional decision-relevant fields the engine emits
        # but pre-fix were dropped before the AI reviewer saw them (directive:
        # "toate informațiile valabile"). Volatility (atr/atr_value/avg_atr_20d),
        # momentum/overbought-oversold (rsi/macd/macd_hist), extension/exhaustion
        # (dist_vwap_atr/dist_ema_atr), liquidity (spread_estimate), absolute
        # move magnitude (tp_pct/sl_pct alongside the relative rr_ratio), the
        # engine's stated thesis + threshold (signal_thesis/signal_threshold let
        # the model judge quality, not just the score), and the MTF disagreement
        # flag (strict_disagreement) the prompt asks the model to weigh.
        "atr",
        "atr_value",
        "avg_atr_20d",
        "rsi",
        "macd",
        "macd_hist",
        "dist_vwap_atr",
        "dist_ema_atr",
        "spread_estimate",
        "tp_pct",
        "sl_pct",
        "signal_thesis",
        "signal_threshold",
        "strict_disagreement",
        # Structured, causal evidence attached by strategy evaluators.  This
        # lets the model explain the actual trigger sequence without inventing
        # market structure, liquidity, or pattern facts.
        "strategy_name",
        "strategy_reasons",
        "strategy_evidence",
        "research_only",
        "capital_execution_eligible",
        "capital_execution_block_reason",
        "validation_rejection",
        "news_score",
        "news_sentiment",
        "news_headlines",
        "top_headlines",
        "latest_news",
        "news_context",
        "scanner_context",
        "pre_earnings_context",
        # Entry regime of a scanner candidate, the time stop it was tested
        # with, and the backtest of that rule (not live results).
        "entry_strategy",
        "time_stop_minutes",
        "regime_evidence",
        # Realized strategy base rates (P2): closed-trade win rate / avg P&L
        # for this exact strategy — calibration evidence for the reviewer.
        "strategy_base_rates",
        # Reflection lessons (TradingAgents adoption): terse post-trade lessons
        # distilled from closed positions of this strategy family.
        "strategy_lessons",
        # Free whale/institutional flow evidence (§41 prompt and §44 critic
        # both review it; evidence-only, UNAVAILABLE never decides).
        "flow_evidence",
        # The app's measured record of its own past reviews of this strategy
        # (approved vs rejected candidates under one exit rule, and results by
        # stated confidence): calibration evidence, never a veto.
        "ai_track_record",
        # Binance futures positioning for a crypto candidate (open interest,
        # long/short account ratios, taker flow): context only.
        "positioning_evidence",
        # How broad today's move is among the scanner's movers, with the
        # measured result of trading with or against such a move: context only.
        "market_context",
        # eToro community sentiment, crowd positioning & live data
        "etoro_context",
    ]
    return {key: signal.get(key) for key in keys if key in signal}


TRADE_REVIEW_PROMPT_VERSION = "trade_review_v1"

# Signal fields whose strings were written by someone outside the app (news
# publishers, social posts, stored lessons). They stay where the reviewer
# expects them, cleaned, and are named in the `third_party_evidence` marker.
THIRD_PARTY_SIGNAL_KEYS = (
    "news_headlines",
    "top_headlines",
    "latest_news",
    "news_context",
    "strategy_lessons",
    "etoro_context",
    # the pre-earnings context is app data, but its `top_news` are headlines
    "pre_earnings_context.top_news",
)
THIRD_PARTY_POSITION_KEYS = ("news_headlines", "top_headlines")
# Article summaries are longer than headlines; still bounded.
_THIRD_PARTY_MAX_CHARS = 600


def _review_context_payload(name: str, compact: Dict[str, Any], third_party_keys) -> Dict[str, Any]:
    """`{name: context}` with the third-party fields cleaned in place and named
    in a `third_party_evidence` marker (omitted when there are none)."""
    cleaned, fields = quarantine_fields(compact, third_party_keys, max_chars=_THIRD_PARTY_MAX_CHARS)
    out: Dict[str, Any] = {name: cleaned}
    if fields:
        out["third_party_evidence"] = evidence_field_marker(fields, prefix=f"{name}.")
    return out


def build_trade_review_prompt(signal: Dict[str, Any], risk_evaluation: Optional[Dict[str, Any]]) -> str:
    context = _review_context_payload("signal", _compact_signal(signal or {}), THIRD_PARTY_SIGNAL_KEYS)
    payload = {
        "signal": context.pop("signal"),
        "risk_evaluation": risk_evaluation or {},
    }
    payload.update(context)
    return (
        "Review this proposed paper-trading position as a risk-aware trading co-pilot.\n"
        "Do not invent market data. Use only the JSON context below.\n"
        "Return strict JSON with keys: recommendation, confidence, size_factor, "
        "leverage, sentiment, supports_direction, reason, risks, ticker, direction.\n"
        "ticker must repeat signal.ticker exactly and direction must be LONG or SHORT exactly as "
        "proposed by signal.action or signal.type (BUY=LONG, SELL=SHORT): you review this one proposal, you do "
        "not pick another instrument or the opposite side. If you think the opposite side is "
        "right, answer avoid.\n"
        "recommendation must be one of approve, reduce, avoid, watch.\n"
        "confidence is your own 0..1 rating of this review; it is not a probability of profit.\n"
        "signal_score, probability_score, quality_score, composite_score and validation_score are "
        "the app's internal rule scores (0..100 or 0..1). None of them is a calibrated probability "
        "of profit; the only measured outcome data is strategy_base_rates. A score that is absent "
        "or marked unavailable was not measured: say so, do not assume a value.\n"
        "size_factor must be 0..1 and should reduce size for weak or conflicting setups.\n"
        "leverage must be an integer 1..5; prefer 1 unless signal quality, liquidity, news, "
        "and risk/reward are all strong and aligned.\n"
        "Use news_context/top_headlines/news_sentiment as part of the decision when present. "
        "Treat publisher title, summary, URL, timestamp, category, and any attached AI briefing as evidence; "
        "explain whether the news is a catalyst, a risk, unrelated, or insufficient. Do not invent missing article facts.\n"
        "When strategy_evidence is present, base the rationale on its factual trigger chain and levels; do not invent extra confirmations.\n"
        "If research_only is true, state that it is observational and cannot be treated as a Capital-approved entry.\n"
        "If autonomous_source is scanner, judge whether the price move is actionable momentum or exhaustion, "
        "and explicitly reconcile it with the freshly hydrated ticker news.\n"
        "If autonomous_source is pre_earnings, weigh event gap risk, directional bias, coverage, and headline risk.\n"
        "When etoro_context is present, weigh retail crowd positioning and social evidence: "
        "holders/popularity_rank shows retail breadth; buy_pct/sell_pct reveals crowd one-sidedness "
        "(extreme crowding can warn of exhaustion or confirm broad consensus); "
        "use any social_headlines as sentiment context.\n"
        "When strategy_base_rates is present, it is the system's own realized trade history for this strategy "
        "(n, win_rate, avg P&L): calibrate your confidence against it and say so in the reason; treat small "
        "samples (small_sample=true) with explicit caution and never treat it as a veto by itself.\n"
        "When regime_evidence is present, it is a backtest of this exact entry rule with costs "
        "(kind=backtest, not live results): weigh it as measured evidence for the rule together with "
        "the caveats in its note, and do not treat missing news as a defect when the note says the "
        "rule expects no catalyst. Check that the signal still matches the rule it describes.\n"
        "When ai_track_record is present, it is the measured result of your own past reviews of this "
        "strategy: every past candidate, approved or rejected, replayed under one exit rule and net of costs, "
        "plus results by the confidence you stated. Read its `reading` lines and act on them: if your "
        "approvals did not beat your rejections, approve only when this candidate differs in a specific, "
        "stated way from the usual one; if higher confidence did not win more, do not state high confidence "
        "without new evidence. Say in the reason how the record changed your answer. It is evidence, never a "
        "veto by itself.\n"
        "When positioning_evidence is present, it is Binance futures positioning for this coin (open "
        "interest change, long/short account ratios with their 30-day percentile, taker buy/sell flow). No "
        "rule built on it has been validated in this system: use it as context and name it when it "
        "contradicts the trade, but do not treat it as a signal on its own; if it is marked stale, say so.\n"
        "When market_context is present, it says how broad today's move is among the scanner's movers "
        "(a list of gainers and losers, not the whole market) and carries replay_evidence: the measured "
        "result, costs included, of trading a stock that is already 8-40% away from its previous close. "
        "Read those numbers before leaning on the day's colour: a green market was not by itself a reason "
        "to buy a stock that had already run, and a red market was not a reason to buy one that had already "
        "fallen. Use the context to say whether this candidate moves with the market or on its own, and "
        "name it in the reason when it changes your answer. It is context, never a signal by itself.\n"
        "When strategy_lessons is present, those are terse lessons distilled from closed trades of this "
        "strategy family: cite the relevant one in your reason when it applies to this setup.\n"
        "Only use avoid when the setup is clearly unsafe or contradicts the direction.\n"
        f"{UNTRUSTED_FIELDS_NOTICE}\n\n"
        f"Context:\n{json.dumps(payload, ensure_ascii=True, default=str)}"
    )


def _check_availability_cached(ollama_instance) -> bool:
    """Check Ollama availability with a 30-second TTL cache."""
    global _availability_cache
    now = time.time()
    if (now - _availability_cache["timestamp"]) < _AVAILABILITY_CACHE_TTL:
        return _availability_cache["available"]
    available = bool(ollama_instance.check_availability())
    _availability_cache = {"available": available, "timestamp": now}
    return available


def evaluate_trade_decision_with_ollama(
    signal: Dict[str, Any],
    risk_evaluation: Optional[Dict[str, Any]],
    *,
    config: Optional[Dict[str, Any]] = None,
    services: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Phase 4C — delegate to the multi-provider decision engine (§39/§42/§44).

    Evolve-not-rewrite (§5/§7): the legacy entry point now composes the 3C/4A/4B
    platform (generate_with_fallback -> §42 pipeline -> §44 critic) and returns the
    backward-compatible `review` dict that agent_manager already consumes. The
    `services` param lets tests inject providers without a network (§115 — tests
    do not weaken); production calls omit it so the engine resolves providers from
    config via build_provider. §43: leverage is no longer AI-authoritative — the
    engine always returns leverage=1 and agent_manager caps it via config.
    """
    cfg = config if isinstance(config, dict) else {}
    ollama_cfg = cfg.get("ollama", {}) if isinstance(cfg.get("ollama"), dict) else {}
    if not bool(ollama_cfg.get("decision_enabled", True)):
        return default_trade_review(available=False, reason="Ollama decision review disabled")

    # Package first, then the submodule: importing `core.ai.decision_engine`
    # directly while another thread is inside `core.ai/__init__` takes the two
    # import locks in the opposite order (KeyError('core.ai') / _DeadlockError).
    importlib.import_module("core.ai")
    from core.ai.decision_engine import evaluate_trade_decision

    result = evaluate_trade_decision(signal, risk_evaluation, config=cfg, services=services)
    return result.review


def default_exit_review(*, available: bool = False, reason: str = "") -> Dict[str, Any]:
    return {
        "available": available,
        "provider": "ollama",
        "action": "hold",
        "confidence": 0.0,
        "new_stop": None,
        "thesis_invalidated": False,
        "news_severity_assessment": "unknown",
        "reason": reason or "Ollama exit review unavailable",
        "risks": [],
        "raw_response": "",
        "fallback_used": "unavailable" if not available else "",
    }


def parse_exit_review(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        parsed = raw
    else:
        snippet = _extract_json_object(str(raw or ""))
        if not snippet:
            review = default_exit_review(reason="Ollama response did not contain JSON")
            review["raw_response"] = str(raw or "")[:2000]
            review["fallback_used"] = "invalid_json"
            return review
        try:
            parsed = json.loads(snippet)
        except Exception:
            review = default_exit_review(reason="Ollama response JSON could not be parsed")
            review["raw_response"] = str(raw or "")[:2000]
            review["fallback_used"] = "invalid_json"
            return review
    if not isinstance(parsed, dict):
        review = default_exit_review(reason="Ollama response JSON was not an object")
        review["raw_response"] = str(raw or "")[:2000]
        review["fallback_used"] = "invalid_json"
        return review

    action = str(parsed.get("action") or "").lower().strip()
    if action not in ALLOWED_EXIT_ACTIONS:
        # No recognisable verdict: this is not a "hold" opinion, it is no answer.
        review = default_exit_review(reason="Ollama response had no valid exit action")
        review["raw_response"] = str(raw or "")[:2000]
        review["fallback_used"] = "invalid_action"
        return review
    review = default_exit_review(available=True, reason="")
    review["action"] = action
    review["confidence"] = round(_clamp(_safe_float(parsed.get("confidence"), 0.0), 0.0, 1.0), 4)
    review["confidence_basis"] = CONFIDENCE_BASIS
    new_stop = _safe_float(parsed.get("new_stop"), 0.0)
    review["new_stop"] = new_stop if new_stop > 0 else None
    if isinstance(parsed.get("thesis_invalidated"), bool):
        review["thesis_invalidated"] = parsed["thesis_invalidated"]
    severity = str(parsed.get("news_severity_assessment") or "unknown").lower().strip()
    if severity in ALLOWED_NEWS_SEVERITIES:
        review["news_severity_assessment"] = severity
    review["reason"] = str(parsed.get("reason") or "The AI reviewer gave no reason.").strip()[:700]
    review["risks"] = _list_str(parsed.get("risks"), limit=6)
    review["raw_response"] = str(raw or "")[:2000]
    review["fallback_used"] = ""
    return review


def _compact_position(position: Dict[str, Any], current_price: float) -> Dict[str, Any]:
    keys = [
        "position_id",
        "ticker",
        "direction",
        "entry_price",
        "qty",
        "usd_size",
        "margin_used",
        "leverage",
        "stop_loss_price",
        "take_profit_price",
        "trailing_sl",
        "opened_at",
        "current_pnl",
        "current_pnl_pct",
        "unrealized_pnl",
        "unrealized_pnl_pct",
        "entry_reason",
        "strategy_type",
        "source_signal_id",
        "source_signal_ids",
        "last_ollama_exit_review",
        # Round-3 (AI-1): fresh news pulse context so the exit co-pilot can judge
        # whether the catalyst that motivated the trade has flipped since entry.
        # Pre-fix the exit review was news-blind: a position opened on bullish
        # news 3 days ago was reviewed for exit with zero current news, so a
        # counter-headline published since entry could not influence close/
        # tighten_stop (contra "integrează AI prin analiza news"). These keys
        # are injected by evaluate_position_exit_with_ollama from a live
        # build_news_pulse fetch (allow_blocking=False, cache-first) before the
        # prompt is built.
        "news_score",
        "news_sentiment",
        "news_sentiment_trend",
        "news_headlines",
        "top_headlines",
        "news_exit_context",
        # Post-event review context and cache-first volume/flow evidence. These
        # fields distinguish "earnings were published" from an invalid thesis.
        "post_earnings_review",
        "flow_evidence",
    ]
    compact = {key: position.get(key) for key in keys if key in position}
    compact["current_price"] = current_price
    return compact


def _exit_news_context(pulse: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize live News evidence for an exit without turning sentiment into a blind exit rule."""
    payload = pulse if isinstance(pulse, dict) else {}
    window = payload.get("24h") if isinstance(payload.get("24h"), dict) else {}
    article_count = max(0, int(_safe_float(window.get("article_count"), 0.0)))
    source_backed = max(0, int(_safe_float(window.get("source_backed_count"), 0.0)))
    source_backed_ai = max(0, int(_safe_float(window.get("source_backed_ai_analyzed_count"), 0.0)))
    average_impact = _clamp(_safe_float(window.get("avg_impact_score"), 0.0), 0.0, 100.0)
    breaking_count = max(0, int(_safe_float(window.get("breaking_count"), 0.0)))
    evidence_quality = "verified" if source_backed_ai > 0 else ("source_backed" if source_backed > 0 else "unverified")
    if article_count == 0:
        severity = "none"
    elif evidence_quality == "verified" and (average_impact >= 75.0 or breaking_count >= 2):
        severity = "high"
    elif average_impact >= 45.0 or breaking_count >= 1:
        severity = "moderate"
    else:
        severity = "low"
    return {
        "severity": severity,
        "evidence_quality": evidence_quality,
        "article_count_24h": article_count,
        "source_backed_count_24h": source_backed,
        "source_backed_ai_analyzed_count_24h": source_backed_ai,
        "avg_impact_score_24h": round(average_impact, 2),
        "breaking_count_24h": breaking_count,
        "news_score": round(_clamp(_safe_float(payload.get("score"), 0.0), 0.0, 100.0), 2),
        "sentiment": str(payload.get("ai_sentiment_trend") or payload.get("sentiment_trend") or "unknown").lower().strip(),
    }


def _adverse_risk_fraction(position: Dict[str, Any], current_price: float) -> float:
    """Return the fraction of initial stop risk consumed by an adverse move."""
    pos = position if isinstance(position, dict) else {}
    entry = _safe_float(pos.get("entry_price"), 0.0)
    stop = _safe_float(pos.get("trailing_sl") or pos.get("stop_loss_price"), 0.0)
    current = _safe_float(current_price, 0.0)
    direction = str(pos.get("direction") or "").upper()
    if entry <= 0 or stop <= 0 or current <= 0:
        return 0.0
    if direction == "LONG" and stop < entry:
        return _clamp((entry - current) / (entry - stop), 0.0, 10.0)
    if direction == "SHORT" and stop > entry:
        return _clamp((current - entry) / (stop - entry), 0.0, 10.0)
    return 0.0


def evaluate_exit_close_guard(
    review: Dict[str, Any],
    position: Dict[str, Any],
    current_price: float,
    *,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Require material, verified evidence before an AI News review can close early.

    The hard stop remains independent.  This guard specifically prevents a mildly
    bearish or poorly sourced headline from causing a discretionary close.
    """
    cfg = config if isinstance(config, dict) else {}
    ollama_cfg = cfg.get("ollama", {}) if isinstance(cfg.get("ollama"), dict) else {}
    risk_fraction = _adverse_risk_fraction(position, current_price)
    news = review.get("news_exit_context") if isinstance(review.get("news_exit_context"), dict) else {}
    severity = str(news.get("severity") or "unknown").lower()
    evidence_quality = str(news.get("evidence_quality") or "unverified").lower()
    sentiment = str(news.get("sentiment") or "unknown").lower()
    direction = str((position or {}).get("direction") or "").upper()
    post_event = review.get("post_earnings_review")
    if not isinstance(post_event, dict):
        post_event = (position or {}).get("post_earnings_review")
    post_event = post_event if isinstance(post_event, dict) else {}
    flow = review.get("flow_evidence")
    if not isinstance(flow, dict):
        flow = (position or {}).get("flow_evidence")
    flow = flow if isinstance(flow, dict) else {}
    flow_summary = flow.get("summary") if isinstance(flow.get("summary"), dict) else {}
    flow_alignment = str(flow.get("alignment") or "UNAVAILABLE").upper()
    flow_score = abs(_safe_float(flow_summary.get("score"), 0.0))
    flow_confidence = _clamp(_safe_float(flow_summary.get("confidence"), 0.0), 0.0, 1.0)
    relative_volume = max(0.0, _safe_float(flow_summary.get("relative_volume"), 0.0))
    # Defaults are the same thresholds evidence.py uses to even label a flow
    # summary CONTRADICT — configuring the guard below them would silently
    # never fire, so drift between the two modules is impossible by design.
    min_flow_score = max(0.0, _safe_float(
        ollama_cfg.get("exit_close_post_earnings_flow_min_abs_score"),
        ALIGNMENT_MIN_ABS_SCORE,
    ))
    min_flow_confidence = _clamp(_safe_float(
        ollama_cfg.get("exit_close_post_earnings_flow_min_confidence"),
        ALIGNMENT_MIN_CONFIDENCE,
    ), 0.0, 1.0)
    min_relative_volume = max(0.0, _safe_float(
        ollama_cfg.get("exit_close_post_earnings_min_relative_volume"), 1.5
    ))
    adverse_sentiment = (
        (direction == "LONG" and sentiment in {"bearish", "negative"})
        or (direction == "SHORT" and sentiment in {"bullish", "positive"})
    )
    technical_fraction = _clamp(
        _safe_float(ollama_cfg.get("exit_close_technical_risk_fraction"), 0.75), 0.25, 1.0
    )
    if str(review.get("action") or "").lower() != "close":
        return {"allowed": False, "reason": "AI action is not close", "risk_fraction": round(risk_fraction, 4)}
    if risk_fraction >= technical_fraction:
        return {
            "allowed": True,
            "reason": "Technical invalidation consumed the configured fraction of stop risk",
            "risk_fraction": round(risk_fraction, 4),
            "news_severity": severity,
            "news_evidence_quality": evidence_quality,
        }
    if bool(review.get("thesis_invalidated")) and severity in {"high", "critical"} and evidence_quality == "verified":
        return {
            "allowed": True,
            "reason": "High-severity, source-backed AI News invalidated the thesis",
            "risk_fraction": round(risk_fraction, 4),
            "news_severity": severity,
            "news_evidence_quality": evidence_quality,
        }
    if post_event and bool(review.get("thesis_invalidated")):
        strong_contradictory_flow = (
            flow_alignment == "CONTRADICT"
            and flow_score >= min_flow_score
            and flow_confidence >= min_flow_confidence
        )
        volume_confirms = relative_volume >= min_relative_volume
        verified_adverse_news = (
            adverse_sentiment
            and severity in {"moderate", "high", "critical"}
            and evidence_quality == "verified"
        )
        if strong_contradictory_flow and (volume_confirms or verified_adverse_news):
            return {
                "allowed": True,
                "reason": (
                    "Post-earnings thesis invalidation is confirmed by adverse flow "
                    "and material volume/news evidence"
                ),
                "risk_fraction": round(risk_fraction, 4),
                "news_severity": severity,
                "news_evidence_quality": evidence_quality,
                "flow_alignment": flow_alignment,
                "flow_score": round(flow_score, 2),
                "flow_confidence": round(flow_confidence, 4),
                "relative_volume": round(relative_volume, 4),
            }
    return {
        "allowed": False,
        "reason": (
            "Early close rejected: price, verified adverse News, and volume/flow do not "
            "materially invalidate the setup"
        ),
        "risk_fraction": round(risk_fraction, 4),
        "news_severity": severity,
        "news_evidence_quality": evidence_quality,
        "flow_alignment": flow_alignment,
        "flow_score": round(flow_score, 2),
        "flow_confidence": round(flow_confidence, 4),
        "relative_volume": round(relative_volume, 4),
    }


def build_exit_review_prompt(position: Dict[str, Any], current_price: float) -> str:
    context = _review_context_payload(
        "position", _compact_position(position or {}, current_price), THIRD_PARTY_POSITION_KEYS
    )
    payload = {
        "position": context.pop("position"),
        "decision_context": (position or {}).get("decision_context") or {},
    }
    payload.update(context)
    return (
        "Review this open paper-trading position as a conservative exit co-pilot.\n"
        "Do not invent market data. Use only the JSON context below.\n"
        "Hard stop-loss and take-profit rules are handled by the system before this review.\n"
        "Return strict JSON with keys: action, confidence, new_stop, thesis_invalidated, "
        "news_severity_assessment, reason, risks.\n"
        "action must be one of hold, close, tighten_stop.\n"
        "Use close only when the available context clearly says the trade thesis is invalid "
        "or risk has become asymmetric. Use tighten_stop for profit protection or risk reduction.\n"
        "Use news_headlines/news_sentiment/news_exit_context as part of the decision when present: a sentiment "
        "that has flipped against the trade direction must be evaluated against evidence severity. "
        "A mildly bearish, low-impact, poorly sourced, or isolated headline must not by itself close "
        "a position: prefer hold or tighten_stop. Close only for high-severity verified adverse News "
        "that invalidates the thesis, or material price-based technical invalidation.\n"
        "When post_earnings_review is present, the event publication itself is never a reason to close. "
        "Compare current price/PnL with entry, stop and target; then reconcile current News with "
        "flow_evidence (alignment, score, confidence, relative_volume, buy_pressure and sell_pressure). "
        "If price and flow still confirm the trade direction, use hold or tighten_stop. Use close only "
        "when adverse price thresholds or source-backed News plus material contradictory volume/flow "
        "invalidate the thesis.\n"
        "new_stop may be null unless action is tighten_stop.\n"
        "confidence is your own 0..1 rating of this review; it is not a probability.\n"
        f"{UNTRUSTED_FIELDS_NOTICE}\n\n"
        f"Context:\n{json.dumps(payload, ensure_ascii=True, default=str)}"
    )


def evaluate_position_exit_with_ollama(
    position: Dict[str, Any],
    current_price: float,
    *,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = config if isinstance(config, dict) else {}
    ollama_cfg = cfg.get("ollama", {}) if isinstance(cfg.get("ollama"), dict) else {}
    if not bool(ollama_cfg.get("exit_decision_enabled", False)):
        return default_exit_review(available=False, reason="Ollama exit review disabled")

    try:
        from core.ollama_client import ollama

        if not _check_availability_cached(ollama):
            return default_exit_review(available=False, reason="Ollama unavailable")

        exit_timeout = int(_clamp(_safe_float(ollama_cfg.get("exit_decision_timeout_seconds"), 30.0), 10.0, 180.0))
        if isinstance((position or {}).get("post_earnings_review"), dict):
            exit_timeout = int(_clamp(
                _safe_float(
                    ollama_cfg.get("post_earnings_exit_decision_timeout_seconds"),
                    max(90.0, float(exit_timeout)),
                ),
                float(exit_timeout),
                180.0,
            ))
        # Round-3 (AI-1): attach FRESH news pulse so the exit co-pilot can judge
        # whether the catalyst that motivated the trade has flipped since entry.
        # Pre-fix the exit review carried zero news context (the entry-side
        # signal's news was not persisted onto the position), so a counter-
        # headline published after entry could not influence close/tighten_stop.
        # Cache-first (allow_blocking=False) — never blocks the exit path on a
        # slow/missing news provider; on failure the review proceeds news-blind
        # (same as pre-fix) rather than failing the whole exit review.
        enriched_position = dict(position or {})
        ticker = str(enriched_position.get("ticker") or "").strip()
        if ticker:
            try:
                from core.news_pulse import build_news_pulse as _bnp
                pulse = _bnp(ticker, allow_blocking=False) or {}
                if isinstance(pulse, dict) and pulse:
                    enriched_position["news_score"] = _safe_float(pulse.get("score"), 0.0)
                    enriched_position["news_exit_context"] = _exit_news_context(pulse)
                    _sent = str(pulse.get("sentiment_trend") or "").lower().strip()
                    if _sent:
                        enriched_position["news_sentiment"] = _sent
                        enriched_position["news_sentiment_trend"] = _sent
                    _headlines = pulse.get("top_headlines") or []
                    if isinstance(_headlines, list):
                        titles = []
                        for h in _headlines:
                            if isinstance(h, dict):
                                t = str(h.get("title") or h.get("headline") or "").strip()
                                if t:
                                    titles.append(t[:220])
                            elif isinstance(h, str) and h.strip():
                                titles.append(h.strip()[:220])
                            if len(titles) >= 5:
                                break
                        if titles:
                            enriched_position["news_headlines"] = titles
                            enriched_position["top_headlines"] = titles
            except Exception as exc:
                logger.debug(f"exit-review news pulse skipped for {ticker}: {exc}")
            try:
                from core.flow_indicators import flow_evidence_for_signal

                flow_evidence = flow_evidence_for_signal({
                    "ticker": ticker,
                    "direction": str(enriched_position.get("direction") or ""),
                })
                if isinstance(flow_evidence, dict):
                    enriched_position["flow_evidence"] = flow_evidence
            except Exception as exc:
                logger.debug(f"exit-review flow evidence skipped for {ticker}: {exc}")
        result = ollama.generate(
            build_exit_review_prompt(enriched_position, current_price),
            model=None,
            system="You are a strict trading exit risk reviewer. Output JSON only.",
            ui_call=False,
            timeout_override=exit_timeout,
        )
        if not isinstance(result, dict) or result.get("error"):
            review = default_exit_review(available=False, reason=str((result or {}).get("error") or "Ollama error"))
            review["fallback_used"] = "ollama_error"
            for key in ("news_exit_context", "flow_evidence", "post_earnings_review"):
                if isinstance(enriched_position.get(key), dict):
                    review[key] = enriched_position[key]
            return review

        review = parse_exit_review(result.get("response", ""))
        # parse_exit_review already says whether the reply held a usable verdict;
        # forcing available=True here presented "no JSON in the reply" as a
        # considered "hold" from the AI.
        meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
        review["model"] = meta.get("model") or result.get("model") or ollama.get_preferred_model()
        review["provider"] = "ollama_cloud" if (ollama.is_cloud_host() or "cloud" in str(review["model"]).lower()) else "ollama_local"
        review["generated_at"] = meta.get("generated_at")
        for key in ("news_exit_context", "flow_evidence", "post_earnings_review"):
            if isinstance(enriched_position.get(key), dict):
                review[key] = enriched_position[key]
        return review
    except Exception as exc:
        logger.error(f"Ollama exit review failed: {exc}")
        review = default_exit_review(available=False, reason=str(exc))
        review["fallback_used"] = "exception"
        return review


def should_veto_trade(review: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> bool:
    cfg = config if isinstance(config, dict) else {}
    ollama_cfg = cfg.get("ollama", {}) if isinstance(cfg.get("ollama"), dict) else {}
    if not bool(ollama_cfg.get("decision_veto_enabled", True)):
        return False
    min_confidence = _safe_float(ollama_cfg.get("decision_min_confidence"), 0.65)
    return (
        bool((review or {}).get("available"))
        and str((review or {}).get("recommendation") or "").lower() == "avoid"
        and _safe_float((review or {}).get("confidence"), 0.0) >= min_confidence
    )
