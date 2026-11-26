import json
import math
import re
from typing import Any, Dict, Iterable, List, Optional

ALLOWED_SENTIMENT = {"bullish", "bearish", "neutral"}
ALLOWED_PLAY = {"PLAY_PRE", "PLAY_POST", "WAIT", "AVOID"}
ALLOWED_DIRECTION = {"LONG", "SHORT", "NONE"}

# Who produced the analysis. The UI must be able to tell a model's answer from
# the rule-based stand-in that replaces an unusable answer.
SOURCE_NONE = "none"
SOURCE_AI = "ai"
SOURCE_DETERMINISTIC_FALLBACK = "deterministic_fallback"
# What the `confidence` number is. It is never a probability of profit.
CONFIDENCE_BASIS_NONE = "none"
CONFIDENCE_BASIS_AI = "model_self_reported_uncalibrated"
CONFIDENCE_BASIS_RULES = "rule_vote_count_uncalibrated"


def default_structured_analysis() -> Dict[str, Any]:
    return {
        "source": SOURCE_NONE,
        "confidence_basis": CONFIDENCE_BASIS_NONE,
        "sentiment": "neutral",
        "earnings_play": "WAIT",
        "confidence": 0.0,
        "key_drivers": [],
        "risks": [],
        "news_summary": {
            "last_7d_positive": [],
            "last_7d_negative": [],
            "last_30d_theme": "unknown",
            "major_risk_events": [],
        },
        "scenario": {
            "bull_case": "unknown",
            "base_case": "unknown",
            "bear_case": "unknown",
        },
        "trade_plan": {
            "direction": "NONE",
            "entry_condition": "unknown",
            "invalidations": "unknown",
            "tp_logic": "unknown",
            "notes": "unknown",
        },
    }


def _extract_json_object(raw_text: str) -> Optional[str]:
    if not raw_text:
        return None
    start = raw_text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(raw_text[start:], start=start):
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
                return raw_text[start : i + 1]
    return None


def _as_list_str(value: Any, max_items: int) -> List[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = str(item).strip()
        if not text:
            continue
        out.append(text)
        if len(out) >= max_items:
            break
    return out


def _is_bullish_text(value: Any) -> bool:
    text = str(value or "").upper()
    return "BULL" in text


def _is_bearish_text(value: Any) -> bool:
    text = str(value or "").upper()
    return "BEAR" in text


def _safe_float(value: Any, default: float = 0.0) -> float:
    """float(value), or `default` for anything that is not a finite number
    (NaN/Infinity pass json.loads and `min(1.0, nan)` is 1.0)."""
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except Exception:
        return default
    return number if math.isfinite(number) else default


def _optional_float(value: Any) -> Optional[float]:
    """A finite number, or None when the input was not supplied / not numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def validate_structured_analysis(candidate: Any) -> Dict[str, Any]:
    out = default_structured_analysis()
    if not isinstance(candidate, dict):
        return out

    sentiment = str(candidate.get("sentiment") or "").lower()
    if sentiment in ALLOWED_SENTIMENT:
        out["sentiment"] = sentiment

    play = str(candidate.get("earnings_play") or "").upper()
    if play in ALLOWED_PLAY:
        out["earnings_play"] = play

    out["source"] = SOURCE_AI
    out["confidence_basis"] = CONFIDENCE_BASIS_AI
    out["confidence"] = max(0.0, min(1.0, _safe_float(candidate.get("confidence"), 0.0)))

    out["key_drivers"] = _as_list_str(candidate.get("key_drivers"), max_items=8)
    out["risks"] = _as_list_str(candidate.get("risks"), max_items=8)

    ns = candidate.get("news_summary")
    if isinstance(ns, dict):
        out["news_summary"]["last_7d_positive"] = _as_list_str(ns.get("last_7d_positive"), max_items=6)
        out["news_summary"]["last_7d_negative"] = _as_list_str(ns.get("last_7d_negative"), max_items=6)
        theme = str(ns.get("last_30d_theme") or "").strip()
        out["news_summary"]["last_30d_theme"] = theme if theme else "unknown"
        out["news_summary"]["major_risk_events"] = _as_list_str(ns.get("major_risk_events"), max_items=3)

    scenario = candidate.get("scenario")
    if isinstance(scenario, dict):
        out["scenario"]["bull_case"] = str(scenario.get("bull_case") or "unknown")
        out["scenario"]["base_case"] = str(scenario.get("base_case") or "unknown")
        out["scenario"]["bear_case"] = str(scenario.get("bear_case") or "unknown")

    trade = candidate.get("trade_plan")
    if isinstance(trade, dict):
        direction = str(trade.get("direction") or "").upper()
        if direction in ALLOWED_DIRECTION:
            out["trade_plan"]["direction"] = direction
        out["trade_plan"]["entry_condition"] = str(trade.get("entry_condition") or "unknown")
        out["trade_plan"]["invalidations"] = str(trade.get("invalidations") or "unknown")
        out["trade_plan"]["tp_logic"] = str(trade.get("tp_logic") or "unknown")
        out["trade_plan"]["notes"] = str(trade.get("notes") or "unknown")

    return out


def is_structured_analysis_default(candidate: Dict[str, Any]) -> bool:
    if not isinstance(candidate, dict):
        return True
    default = default_structured_analysis()
    if str(candidate.get("sentiment")) != str(default.get("sentiment")):
        return False
    if str(candidate.get("earnings_play")) != str(default.get("earnings_play")):
        return False
    if _safe_float(candidate.get("confidence"), 0.0) > 0.0:
        return False
    if isinstance(candidate.get("key_drivers"), list) and candidate.get("key_drivers"):
        return False
    if isinstance(candidate.get("risks"), list) and candidate.get("risks"):
        return False
    trade = candidate.get("trade_plan", {})
    if isinstance(trade, dict) and str(trade.get("direction") or "NONE").upper() != "NONE":
        return False
    return True


def build_deterministic_structured_analysis(context_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ctx = context_payload if isinstance(context_payload, dict) else {}
    technical = ctx.get("technical", {}) if isinstance(ctx.get("technical"), dict) else {}
    price = _safe_float(ctx.get("price"), 0.0)
    # An input that was not supplied stays None: it casts no vote and is listed
    # under `missing_inputs`. The old defaults (RSI 50.0, MACD 0.0, change 0.0)
    # were printed in the drivers as if they had been measured.
    change_opt = _optional_float(ctx.get("change_pct"))
    rsi_opt = _optional_float(technical.get("rsi"))
    macd_opt = _optional_float(technical.get("macd_hist"))
    change_pct = change_opt if change_opt is not None else 0.0
    trend_1d = str(technical.get("trend_1d") or ctx.get("trend_1d") or "UNKNOWN")
    trend_4h = str(technical.get("trend_4h") or ctx.get("trend_4h") or "UNKNOWN")
    atr = _safe_float(technical.get("atr"), 0.0)
    missing_inputs: List[str] = []
    if price <= 0:
        missing_inputs.append("price")
    if change_opt is None:
        missing_inputs.append("change_pct")
    if rsi_opt is None:
        missing_inputs.append("rsi")
    if macd_opt is None:
        missing_inputs.append("macd_hist")
    if "UNKNOWN" in trend_1d.upper() or trend_1d.upper() in {"N/A", ""}:
        missing_inputs.append("trend_1d")
    if "UNKNOWN" in trend_4h.upper() or trend_4h.upper() in {"N/A", ""}:
        missing_inputs.append("trend_4h")

    bull_votes = 0
    bear_votes = 0
    if _is_bullish_text(trend_1d):
        bull_votes += 2
    if _is_bullish_text(trend_4h):
        bull_votes += 2
    if _is_bearish_text(trend_1d):
        bear_votes += 2
    if _is_bearish_text(trend_4h):
        bear_votes += 2
    if macd_opt is not None:
        if macd_opt > 0:
            bull_votes += 1
        elif macd_opt < 0:
            bear_votes += 1
    if rsi_opt is not None:
        if rsi_opt >= 68:
            bear_votes += 1
        elif rsi_opt <= 32:
            bull_votes += 1
    if change_pct >= 1.5:
        bull_votes += 1
    elif change_pct <= -1.5:
        bear_votes += 1

    if bull_votes >= bear_votes + 2:
        sentiment = "bullish"
    elif bear_votes >= bull_votes + 2:
        sentiment = "bearish"
    else:
        sentiment = "neutral"

    confidence = min(1.0, max(0.25, abs(bull_votes - bear_votes) / 8.0 + 0.35))
    direction = "NONE"
    if confidence >= 0.55:
        if sentiment == "bullish":
            direction = "LONG"
        elif sentiment == "bearish":
            direction = "SHORT"

    key_drivers: List[str] = []
    key_drivers.append(f"Trend context 1D/4H: {trend_1d}/{trend_4h}.")
    rsi_text = f"RSI {rsi_opt:.1f}" if rsi_opt is not None else "RSI unavailable"
    macd_text = f"MACD hist {macd_opt:.4f}" if macd_opt is not None else "MACD hist unavailable"
    key_drivers.append(f"{rsi_text} with {macd_text}.")
    if price > 0:
        change_text = f"{change_pct:+.2f}%" if change_opt is not None else "unavailable"
        key_drivers.append(f"Price {price:.4f}, intraday change {change_text}.")
    if atr > 0:
        key_drivers.append(f"ATR context {atr:.4f} supports volatility-aware sizing.")

    risks: List[str] = []
    if rsi_opt is not None and rsi_opt >= 70:
        risks.append("RSI is elevated; mean-reversion risk is higher.")
    if rsi_opt is not None and rsi_opt <= 30:
        risks.append("RSI is compressed; downside continuation risk remains.")
    if missing_inputs:
        risks.append("Inputs unavailable for this summary: " + ", ".join(missing_inputs) + ".")
    if abs(change_pct) >= 4.0:
        risks.append("Large intraday move can increase whipsaw risk.")
    if "UNKNOWN" in trend_1d.upper() or "UNKNOWN" in trend_4h.upper():
        risks.append("Trend signal quality is partial; wait for cleaner alignment.")
    if not risks:
        risks.append("No dominant risk detected; keep standard stop discipline.")

    out = default_structured_analysis()
    out["source"] = SOURCE_DETERMINISTIC_FALLBACK
    out["confidence_basis"] = CONFIDENCE_BASIS_RULES
    out["fallback_reason"] = "The AI reply was empty or not valid JSON; this summary was computed by fixed rules."
    out["missing_inputs"] = missing_inputs
    out["sentiment"] = sentiment
    out["earnings_play"] = "WAIT"
    out["confidence"] = round(confidence, 4)
    out["key_drivers"] = key_drivers[:5]
    out["risks"] = risks[:5]
    out["scenario"] = {
        "bull_case": "Trend alignment persists and momentum confirms continuation.",
        "base_case": "Price consolidates while waiting for additional catalysts.",
        "bear_case": "Momentum fades and price reverts toward recent support zones.",
    }
    out["trade_plan"] = {
        "direction": direction,
        "entry_condition": "Wait for multi-timeframe confirmation near trend continuation.",
        "invalidations": "Exit if trend alignment breaks and momentum diverges.",
        "tp_logic": "ATR",
        "notes": "Deterministic fallback used because AI JSON was invalid.",
    }
    return out


def parse_structured_ai_output(raw_text: str) -> Dict[str, Any]:
    snippet = _extract_json_object(str(raw_text or ""))
    if not snippet:
        return default_structured_analysis()
    try:
        parsed = json.loads(snippet)
    except Exception:
        return default_structured_analysis()
    return validate_structured_analysis(parsed)


# ---------------------------------------------------------------------------
# §43 for the analysis text: a figure the model writes must be in its input.
# ---------------------------------------------------------------------------
# The decision path strips AI-supplied authoritative numbers (core.ai.decision).
# The per-ticker analysis is free text, so the same rule is applied here as a
# disclosure: every figure in the model's text is looked up in the data the
# model was given, and the ones that are not there are listed for the reader.

_NUMBER_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_.])([$\u20ac]?)(-?\d[\d,]*(?:\.\d+)?)(%|[KMBT]\b)?(?![A-Za-z0-9_]|\.\d)"
)
_SUFFIX_MULTIPLIER = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
_MISSING_WORDS = {"", "unknown", "n/a", "na", "none", "null", "unavailable"}
_MAX_REPORTED = 12


def _number_tokens(text: Any) -> List[Dict[str, Any]]:
    """Figures in a piece of text: value, decimals, and how it was written."""
    out: List[Dict[str, Any]] = []
    for match in _NUMBER_TOKEN.finditer(str(text or "")):
        currency, digits, suffix = match.group(1), match.group(2).rstrip(","), match.group(3) or ""
        try:
            value = float(digits.replace(",", ""))
        except ValueError:
            continue
        decimals = len(digits.split(".", 1)[1]) if "." in digits else 0
        if suffix in _SUFFIX_MULTIPLIER and not currency and decimals == 0:
            continue  # "5M" / "30M" is a chart timeframe far more often than five million
        value *= _SUFFIX_MULTIPLIER.get(suffix, 1.0)
        out.append({
            "value": value,
            "decimals": decimals,
            "marked": bool(currency or suffix),  # written as money, percent or a magnitude
            "token": f"{currency}{digits}{suffix}",
        })
    return out


def _context_numbers(payload: Any, _depth: int = 0) -> List[float]:
    """Every figure in the data handed to the model (numeric leaves and numbers in strings)."""
    if _depth > 8 or payload is None or isinstance(payload, bool):
        return []
    if isinstance(payload, (int, float)):
        return [float(payload)] if math.isfinite(float(payload)) else []
    if isinstance(payload, str):
        return [token["value"] for token in _number_tokens(payload)]
    if isinstance(payload, dict):
        values: List[float] = []
        for child in payload.values():
            values.extend(_context_numbers(child, _depth + 1))
        return values
    if isinstance(payload, (list, tuple)):
        values = []
        for child in payload:
            values.extend(_context_numbers(child, _depth + 1))
        return values
    return []


def _is_grounded(token: Dict[str, Any], known: Iterable[float]) -> bool:
    value, decimals = abs(float(token["value"])), int(token["decimals"])
    for raw in known:
        base = abs(raw)
        # the same figure, as given / rounded the way the text rounds it /
        # a 0..1 fraction quoted as a percentage
        for candidate in (base, base * 100.0):
            if abs(candidate - value) <= max(1e-9, 0.005 * candidate):
                return True
            if round(candidate, decimals) == round(value, decimals):
                return True
    return False


def _analysis_text_fields(analysis: Dict[str, Any]) -> List[str]:
    texts: List[str] = []
    for key in ("key_drivers", "risks"):
        texts.extend(str(x) for x in (analysis.get(key) or []))
    news = analysis.get("news_summary") if isinstance(analysis.get("news_summary"), dict) else {}
    for key in ("last_7d_positive", "last_7d_negative", "major_risk_events"):
        texts.extend(str(x) for x in (news.get(key) or []))
    texts.append(str(news.get("last_30d_theme") or ""))
    for block in ("scenario", "trade_plan"):
        values = analysis.get(block) if isinstance(analysis.get(block), dict) else {}
        texts.extend(str(x) for x in values.values())
    return texts


def find_ungrounded_numbers(analysis: Dict[str, Any], context_payload: Any) -> List[str]:
    """Figures in the analysis text that are not in the data the model received.

    Small plain counts (0-10) and calendar years are not treated as data.
    """
    known = _context_numbers(context_payload)
    reported: List[str] = []
    for text in _analysis_text_fields(analysis if isinstance(analysis, dict) else {}):
        for token in _number_tokens(text):
            value = abs(token["value"])
            plain_integer = token["decimals"] == 0 and not token["marked"]
            if plain_integer and (value <= 10 or 1990 <= value <= 2100):
                continue
            if _is_grounded(token, known):
                continue
            if token["token"] not in reported:
                reported.append(token["token"])
            if len(reported) >= _MAX_REPORTED:
                return reported
    return reported


def find_missing_inputs(context_payload: Any, _prefix: str = "", _depth: int = 0) -> List[str]:
    """Paths of the inputs that were unknown/empty when the model was asked."""
    if _depth > 6 or not isinstance(context_payload, dict):
        return []
    missing: List[str] = []
    for key, value in context_payload.items():
        path = f"{_prefix}{key}"
        if isinstance(value, dict):
            if value:
                missing.extend(find_missing_inputs(value, f"{path}.", _depth + 1))
            else:
                missing.append(path)
        elif value is None or (isinstance(value, str) and value.strip().lower() in _MISSING_WORDS):
            missing.append(path)
        elif isinstance(value, (list, tuple)) and not value:
            missing.append(path)
        if len(missing) >= _MAX_REPORTED:
            return missing[:_MAX_REPORTED]
    return missing


def parse_structured_ai_output_with_deterministic_fallback(
    raw_text: str,
    context_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    parsed = parse_structured_ai_output(raw_text)
    if not is_structured_analysis_default(parsed):
        if isinstance(context_payload, dict):
            parsed["numbers_checked"] = True
            parsed["ungrounded_numbers"] = find_ungrounded_numbers(parsed, context_payload)
            parsed["missing_inputs"] = find_missing_inputs(context_payload)
        else:
            parsed["numbers_checked"] = False
        return parsed
    return build_deterministic_structured_analysis(context_payload=context_payload)
