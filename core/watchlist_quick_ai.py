import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.ollama_client import ollama

logger = logging.getLogger("WatchlistQuickAI")

_CACHE_LOCK = threading.RLock()
_CACHE: Dict[str, Dict[str, Any]] = {}

_ALLOWED_BIAS = {"bullish", "bearish", "neutral"}
_ALLOWED_HORIZON = {"intraday", "1-7d", "1-4w", "unknown"}
_ALLOWED_ACTION = {"BUY", "SELL", "WAIT", "AVOID"}
_FAILURE_TTL_SECONDS = 60

# The quick analysis is a UI action: it must not hold the request for the
# client's general 120 s default. `ollama.quick_ai_timeout_seconds` overrides.
DEFAULT_QUICK_AI_TIMEOUT_SECONDS = 45
_MIN_QUICK_AI_TIMEOUT_SECONDS = 5
_MAX_QUICK_AI_TIMEOUT_SECONDS = 120


def quick_ai_timeout_seconds() -> int:
    """Bounded read timeout for one quick-AI call (`ollama.quick_ai_timeout_seconds`)."""
    raw: Any = None
    try:
        from core.ollama_client import _get_ollama_config

        raw = (_get_ollama_config() or {}).get("quick_ai_timeout_seconds")
    except Exception:
        raw = None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        seconds = float(DEFAULT_QUICK_AI_TIMEOUT_SECONDS)
    if seconds != seconds or seconds <= 0 or seconds == float("inf"):
        seconds = float(DEFAULT_QUICK_AI_TIMEOUT_SECONDS)
    return int(max(_MIN_QUICK_AI_TIMEOUT_SECONDS, min(_MAX_QUICK_AI_TIMEOUT_SECONDS, round(seconds))))


def _is_timeout_error(message: Any) -> bool:
    text = str(message or "").lower()
    return "timed out" in text or "timeout" in text


def default_quick_ai_payload() -> Dict[str, Any]:
    return {
        "bias": "neutral",
        "confidence": 0,
        "time_horizon": "unknown",
        "top_reasons": [],
        "top_risks": [],
        "action": "WAIT",
        "notes": "unknown",
    }


def _extract_json_object(raw: str) -> Optional[str]:
    text = str(raw or "")
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escaped = False
    for idx, ch in enumerate(text[start:], start=start):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return None


def _to_list(value: Any, max_items: int) -> list:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        txt = str(item or "").strip()
        if not txt:
            continue
        out.append(txt)
        if len(out) >= max_items:
            break
    return out


def parse_quick_ai_output(raw: str) -> Dict[str, Any]:
    return parse_quick_ai_output_checked(raw)[0]


def parse_quick_ai_output_checked(raw: str) -> tuple:
    """Return ``(payload, ok)``.

    ``ok`` is False when the model produced nothing usable (no JSON object,
    invalid JSON, or neither a valid bias nor a valid action). The payload is
    then only the neutral placeholder and must not be shown as an analysis.
    """
    payload = default_quick_ai_payload()
    snippet = _extract_json_object(raw)
    if not snippet:
        return payload, False
    try:
        parsed = json.loads(snippet)
    except Exception:
        return payload, False
    if not isinstance(parsed, dict):
        return payload, False

    answered = False
    bias = str(parsed.get("bias") or "").lower()
    if bias in _ALLOWED_BIAS:
        payload["bias"] = bias
        answered = True

    try:
        conf_raw = float(parsed.get("confidence", 0))
        if conf_raw != conf_raw:  # NaN
            conf_raw = 0.0
    except Exception:
        conf_raw = 0.0
    # The scale is 0-100. Models often answer on 0-1 ("0.8"); rounding that to
    # 1 would show an 80%-confident call as "confidence 1".
    if 0.0 < conf_raw <= 1.0:
        conf_raw *= 100.0
    conf = int(round(conf_raw))
    payload["confidence"] = max(0, min(100, conf))

    horizon = str(parsed.get("time_horizon") or "").lower()
    if horizon in _ALLOWED_HORIZON:
        payload["time_horizon"] = horizon

    action = str(parsed.get("action") or "").upper()
    if action in _ALLOWED_ACTION:
        payload["action"] = action
        answered = True

    payload["top_reasons"] = _to_list(parsed.get("top_reasons"), 3)
    payload["top_risks"] = _to_list(parsed.get("top_risks"), 3)
    notes = str(parsed.get("notes") or "").strip()
    payload["notes"] = notes if notes else "unknown"
    return payload, answered


def _cache_key(ticker: str, context: Dict[str, Any]) -> str:
    return (
        f"{ticker}|e={context.get('earnings_days')}|"
        f"ns={context.get('news_score')}|sg={context.get('signal_score')}|"
        f"t={context.get('trend_alignment')}|"
        f"oeps={context.get('owner_earnings_per_share')}|"
        f"ev_ebitda={context.get('ev_to_ebitda')}|"
        f"revg={context.get('revenue_growth_yoy')}|"
        f"fcfg={context.get('free_cash_flow_growth_yoy')}"
    )


def _ttl_seconds(earnings_days: Any, cfg: Dict[str, Any]) -> int:
    try:
        days = int(earnings_days)
    except Exception:
        days = -1
    near = int(cfg.get("ttl_near_earnings", 600) or 600)
    normal = int(cfg.get("ttl_normal", 1800) or 1800)
    if 0 <= days <= 5:
        return max(60, near)
    return max(120, normal)


_STALE_ANALYSIS_SECONDS = 4.5 * 86400


def _trend_line_for_prompt(context: Dict[str, Any]) -> str:
    """Trend alignment as the model may use it.

    The engine's trend can be months old (it is only refreshed while the
    market is open). An old or missing analysis is handed to the model as
    'unknown' with the reason, never as a current reading.
    """
    trend = str(context.get("trend_alignment") or "unknown")
    if context.get("analysis_available") is False:
        return "unknown (no technical analysis available for this ticker)"
    try:
        age = float(context.get("analysis_age_seconds"))
    except (TypeError, ValueError):
        return trend
    if age > _STALE_ANALYSIS_SECONDS:
        return f"unknown (last technical analysis is {int(age // 86400)} days old - too old to use)"
    return trend


def _price_line_for_prompt(context: Dict[str, Any]) -> str:
    """The price, or 'unknown' with the reason it was withheld."""
    price = context.get("price")
    if price is None or price == "":
        reason = str(context.get("price_omitted_reason") or "").strip()
        return f"unknown ({reason})" if reason else "unknown"
    return str(price)


def _prompt_from_context(ticker: str, context: Dict[str, Any]) -> str:
    return (
        "You are a strict trading assistant.\n"
        "Use only provided data. If missing, write 'unknown'. Never invent numbers.\n"
        "confidence is an integer from 0 to 100.\n"
        "Return ONLY valid JSON with this exact schema:\n"
        "{"
        "\"bias\":\"bullish|bearish|neutral\","
        "\"confidence\":0,"
        "\"time_horizon\":\"intraday|1-7d|1-4w|unknown\","
        "\"top_reasons\":[\"...\",\"...\",\"...\"],"
        "\"top_risks\":[\"...\",\"...\"],"
        "\"action\":\"BUY|SELL|WAIT|AVOID\","
        "\"notes\":\"...\""
        "}\n\n"
        f"Ticker: {ticker}\n"
        f"Price: {_price_line_for_prompt(context)}\n"
        # What the price is: the date of the bar it comes from and the session
        # ("closed" = last regular close, not a live quote).
        f"Price as of: {context.get('price_as_of') or 'unknown'}\n"
        f"Session: {context.get('session') or 'unknown'}\n"
        f"ChangePct: {context.get('change_pct', 'unknown')}\n"
        f"TrendAlignment(1D/4H/1H): {_trend_line_for_prompt(context)}\n"
        f"SignalStatus: {context.get('signal_status', 'unknown')}\n"
        f"SignalScore: {context.get('signal_score', 'unknown')}\n"
        f"EarningsDays: {context.get('earnings_days', 'unknown')}\n"
        f"EarningsWhen: {context.get('earnings_when', 'unknown')}\n"
        f"News24h count/impact/score: {context.get('news_24h_count', 0)}/{context.get('news_24h_impact', 0)}/{context.get('news_score', 0)}\n"
        f"News7d count/impact/sentiment: {context.get('news_7d_count', 0)}/{context.get('news_7d_impact', 0)}/{context.get('news_sentiment', 'unknown')}\n"
        f"TopHeadlines: {context.get('top_headlines', [])}\n"
        f"OwnerEarningsPerShare: {context.get('owner_earnings_per_share', 'unknown')}\n"
        f"EVtoEBITDA: {context.get('ev_to_ebitda', 'unknown')}\n"
        f"RevenueGrowthYoY: {context.get('revenue_growth_yoy', 'unknown')}\n"
        f"FreeCashFlowGrowthYoY: {context.get('free_cash_flow_growth_yoy', 'unknown')}\n"
    )


def run_quick_ai_for_ticker(
    ticker: str,
    context: Dict[str, Any],
    quick_ai_cfg: Optional[Dict[str, Any]] = None,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    cfg = dict(quick_ai_cfg or {})
    if not bool(cfg.get("enabled", True)):
        return {
            "enabled": False,
            "available": False,
            "cached": False,
            "analysis": default_quick_ai_payload(),
            "model": "disabled",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": "quick AI is disabled in settings",
        }

    symbol = str(ticker or "").upper().strip()
    if not symbol:
        return {
            "enabled": True,
            "available": False,
            "cached": False,
            "analysis": default_quick_ai_payload(),
            "model": "unknown",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": "invalid ticker",
        }

    ttl = _ttl_seconds(context.get("earnings_days", -1), cfg)
    key = _cache_key(symbol, context)
    now_ts = time.time()

    if not force_refresh:
        with _CACHE_LOCK:
            entry = _CACHE.get(key)
            entry_payload = (entry or {}).get("payload", {}) or {}
            # A failed run (AI offline / unusable answer) is only remembered
            # briefly so the next click retries instead of replaying the
            # failure for the whole TTL.
            entry_ttl = ttl if entry_payload.get("available", True) else min(ttl, _FAILURE_TTL_SECONDS)
            if entry and (now_ts - entry.get("ts", 0.0)) < entry_ttl:
                out = copy_payload(entry.get("payload", {}))
                out["cached"] = True
                return out

    if not ollama.check_availability():
        fallback = {
            "enabled": True,
            "available": False,
            "cached": False,
            "analysis": default_quick_ai_payload(),
            "model": "offline",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": "ollama unavailable",
        }
        with _CACHE_LOCK:
            _CACHE[key] = {"ts": now_ts, "payload": fallback}
        return fallback

    from core.ai.role_resolution import assigned_model, call_timeout_for

    # The owner's model for market analysis; the global model when the role has none.
    model = assigned_model("market_analyst", fallback=ollama.get_best_model())
    if not model:
        fallback = {
            "enabled": True,
            "available": False,
            "cached": False,
            "analysis": default_quick_ai_payload(),
            "model": "unavailable",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": "no compatible model",
        }
        with _CACHE_LOCK:
            _CACHE[key] = {"ts": now_ts, "payload": fallback}
        return fallback

    prompt = _prompt_from_context(symbol, context)
    timeout_seconds = call_timeout_for(model, quick_ai_timeout_seconds())
    call_error = ""
    try:
        raw = ollama.generate(
            prompt=prompt,
            model=model,
            system="Output strict JSON only. Unknown values must be 'unknown'.",
            timeout_override=timeout_seconds,
        )
    except Exception as exc:  # the client normally returns {"error": ...}
        raw = {"error": str(exc)}
    text = ""
    if isinstance(raw, dict):
        text = str(raw.get("response") or "")
        call_error = str(raw.get("error") or "").strip()
    if call_error and not text.strip():
        # The call itself failed: this is "AI unavailable", not an analysis
        # that came back neutral.
        timed_out = _is_timeout_error(call_error)
        failure = {
            "enabled": True,
            "available": False,
            "cached": False,
            "analysis": default_quick_ai_payload(),
            "model": model,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "unavailable_reason": "timeout" if timed_out else "call_failed",
            "timeout_seconds": timeout_seconds,
            "error": (
                f"the AI model did not answer within {timeout_seconds} s (timeout)"
                if timed_out
                else f"the AI model call failed: {call_error[:200]}"
            ),
        }
        with _CACHE_LOCK:
            _CACHE[key] = {"ts": now_ts, "payload": failure}
        return failure
    analysis, answered = parse_quick_ai_output_checked(text)

    payload = {
        "enabled": True,
        "available": bool(answered),
        "cached": False,
        "analysis": analysis,
        "model": model,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if not answered:
        # The neutral/WAIT placeholder is NOT a verdict: say the model failed.
        payload["error"] = (
            "the AI model returned no answer" if not text.strip()
            else "the AI model returned an answer that could not be read"
        )
    with _CACHE_LOCK:
        _CACHE[key] = {"ts": now_ts, "payload": payload}
    return payload


def copy_payload(value: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(value or {}))
