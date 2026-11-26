import copy
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# "N/A" is a valid play/setup for crypto tickers, which have no earnings edge
# to score (F8: _enrich_hotlist_item skips earnings scoring for crypto and sets
# neutral N/A fields). Without it in the allow-list, compute_hotlist_composite
# _metrics reset crypto's "N/A" back to "WAIT"/"C", inventing an earnings
# verdict for assets that have none (directive: nothing invented).
# "INSUFFICIENT_DATA" (tab audit 2026-11-15): a card whose scoring inputs are
# missing must say so instead of printing a confident AVOID/WAIT built from
# neutral placeholders (see apply_hotlist_data_guard).
ALLOWED_PLAYS = {"PLAY_PRE", "PLAY_POST", "WAIT", "AVOID", "N/A", "INSUFFICIENT_DATA"}
ALLOWED_SETUPS = {"A", "B", "C", "N/A"}
ALLOWED_RISK_LEVELS = {"low", "med", "high"}

# Flags that describe a MISSING INPUT, not a market risk. They must not be
# counted as risk factors (the card's Risk Heat was 95% from three "unknown"
# flags) nor be shown as the "Top Risk".
DATA_GAP_FLAGS = frozenset({
    "momentum_unknown",
    "adx_unknown",
    "earnings_history_unknown",
    "earnings_date_unknown",
    "news_context_unknown",
    "technicals_stale",
    "price_stale",
    "price_missing",
})

# A quote may lag by a delayed-feed interval and still be "current".
HOTLIST_QUOTE_MAX_AGE_SECONDS = 20 * 60
# Engine trend/RSI rows older than one full session are not "current".
HOTLIST_TECHNICALS_MAX_AGE_SECONDS = 24 * 3600

logger = logging.getLogger("HotlistScoring")


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def _trend_bias(value: Any) -> int:
    txt = str(value or "").upper()
    if "BULL" in txt:
        return 1
    if "BEAR" in txt:
        return -1
    return 0


def _extract_adx(market_data: Dict[str, Any]) -> Optional[float]:
    adx_data = market_data.get("adx")
    if isinstance(adx_data, dict):
        return _to_float(adx_data.get("adx"), None)
    return _to_float(adx_data, None)


def _extract_insider_sentiment(market_data: Dict[str, Any]) -> str:
    insider = market_data.get("insider")
    if not isinstance(insider, dict):
        return "unknown"
    sentiment = str(insider.get("sentiment") or "").lower()
    if sentiment in {"bullish", "bearish", "neutral"}:
        return sentiment

    net_buying = _to_float(insider.get("netBuying"), 0.0) or 0.0
    net_selling = _to_float(insider.get("netSelling"), 0.0) or 0.0
    if net_buying > net_selling * 1.2:
        return "bullish"
    if net_selling > net_buying * 1.2:
        return "bearish"
    return "neutral"


def _extract_rating_score(market_data: Dict[str, Any]) -> Optional[float]:
    ratings = market_data.get("ratings")
    if not isinstance(ratings, dict):
        return None
    return _to_float(ratings.get("overallScore"), None)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _normalize_pct_input(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.replace("%", "").strip()
        if not text:
            return None
        try:
            return float(text)
        except Exception:
            return None
    return _to_float(value, None)


def _extract_adx_value(item: Dict[str, Any], market_data: Dict[str, Any]) -> Optional[float]:
    adx = _extract_adx(market_data)
    if adx is not None:
        return adx
    raw = item.get("adx")
    if isinstance(raw, dict):
        return _to_float(raw.get("adx"), None)
    return _to_float(raw, None)


def _momentum_score(item: Dict[str, Any]) -> float:
    m1w = _normalize_pct_input(item.get("momentum_1w"))
    m1m = _normalize_pct_input(item.get("momentum_1m"))
    if m1w is None and m1m is None:
        return 50.0
    if m1w is None:
        m1w = m1m
    if m1m is None:
        m1m = m1w
    # Keep score stable in 0..100 for common ranges.
    raw = (50.0 + (m1w * 3.0)) * 0.45 + (50.0 + (m1m * 2.2)) * 0.55
    return _clamp(raw, 0.0, 100.0)


def compute_news_score(news_signals: Optional[Dict[str, Any]]) -> float:
    signals = news_signals or default_news_signals()
    counts = signals.get("counts", {}) if isinstance(signals, dict) else {}
    pos_count = _to_int(counts.get("positive"), 0)
    neg_count = _to_int(counts.get("negative"), 0)
    risk_events = _to_int(counts.get("risk_events"), 0)
    catalyst = _to_int(signals.get("news_catalyst_score"), 0)
    negative_risk = _to_int(signals.get("negative_risk_score"), 0)
    trend = str(signals.get("sentiment_trend") or "unknown").lower()

    score = 50.0
    score += catalyst * 2.6
    score += max(-12.0, min(12.0, (pos_count - neg_count) * 1.8))
    score -= negative_risk * 1.7
    score -= min(14.0, risk_events * 4.0)
    if trend == "bullish":
        score += 5.0
    elif trend == "bearish":
        score -= 7.0
    return _clamp(score, 0.0, 100.0)


def _technical_alignment_score(item: Dict[str, Any], market_data: Dict[str, Any]) -> float:
    trend_total = (
        _trend_bias(item.get("trend_1d"))
        + _trend_bias(item.get("trend_4h"))
        + _trend_bias(item.get("trend_1h"))
    )
    adx_value = _extract_adx_value(item, market_data)
    rsi = _to_float(item.get("rsi"), None)

    score = 50.0 + (trend_total * 10.0)
    if adx_value is not None:
        if adx_value >= 35:
            score += 16.0
        elif adx_value >= 25:
            score += 10.0
        elif adx_value < 18:
            score -= 8.0
    if rsi is not None:
        if 45.0 <= rsi <= 65.0:
            score += 4.0
        elif rsi >= 75.0 or rsi <= 25.0:
            score -= 6.0
    return _clamp(score, 0.0, 100.0)


def _regime_alignment_score(item: Dict[str, Any], market_data: Dict[str, Any]) -> float:
    regime = str(item.get("volatility_regime") or "normal").strip().lower()
    base_map = {
        "compression": 35.0,
        "low": 45.0,
        "normal": 62.0,
        "high": 76.0,
        "elevated": 76.0,
        "extreme": 60.0,
    }
    score = base_map.get(regime, 60.0)
    adx_value = _extract_adx_value(item, market_data)
    if adx_value is not None:
        if adx_value >= 25:
            score += 8.0
        elif adx_value < 18:
            score -= 8.0
    if str(item.get("risk_level") or "med").lower() == "high":
        score -= 12.0
    return _clamp(score, 0.0, 100.0)


def _earnings_date_unknown(item: Dict[str, Any], earnings_days: int) -> bool:
    """-1 is BOTH the "no date" sentinel and "reported yesterday".

    The resolver returns (date - today).days, so a report dated yesterday also
    arrives as -1 and was scored as "earnings date unknown" (and could never
    get PLAY_POST on the first post-earnings day). When the item carries a
    real earnings_date, -1 means yesterday; without one it stays "unknown".
    """
    if earnings_days != -1:
        return False
    date_text = str((item or {}).get("earnings_date") or "").strip().upper()
    return date_text in {"", "N/A"}


def _earnings_time_weight(earnings_days: int) -> float:
    if earnings_days == -1:
        return 12.0
    if earnings_days < -3:
        return 8.0
    if earnings_days < 0:
        return 32.0
    if earnings_days == 0:
        return 100.0
    if earnings_days == 1:
        return 90.0
    if earnings_days == 2:
        return 82.0
    if earnings_days == 3:
        return 74.0
    if earnings_days == 4:
        return 66.0
    if earnings_days == 5:
        return 58.0
    if earnings_days <= 10:
        return 40.0
    if earnings_days <= 20:
        return 22.0
    return 12.0


def _earnings_time_factor(earnings_when: Any) -> float:
    when = str(earnings_when or "").upper().strip()
    if when == "BMO":
        return 1.05
    if when == "AMC":
        return 1.0
    return 0.9


def compute_hotlist_composite_metrics(
    item: Dict[str, Any],
    news_signals: Optional[Dict[str, Any]] = None,
    market_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    data = item or {}
    market_data = market_data or {}
    edge = _to_float(data.get("earnings_edge_score"), 0.0) or 0.0
    risk_level = str(data.get("risk_level") or "med").lower()
    earnings_days = _to_int(data.get("earnings_days"), -1)

    momentum = _momentum_score(data)
    news = compute_news_score(news_signals)
    technical = _technical_alignment_score(data, market_data)
    regime = _regime_alignment_score(data, market_data)
    overall = (
        (0.35 * edge)
        + (0.20 * momentum)
        + (0.20 * news)
        + (0.15 * technical)
        + (0.10 * regime)
    )

    time_weight = _earnings_time_weight(earnings_days)
    if earnings_days == -1 and not _earnings_date_unknown(data, earnings_days):
        time_weight = 32.0  # reported yesterday: same weight as the other post-earnings days
    earnings_time_factor = _earnings_time_factor(data.get("earnings_when"))
    risk_penalty = 20.0 if risk_level == "high" else 0.0
    # Priority is a rank on a 0+ scale. A post-earnings / far-out ticker with a
    # low edge and the high-risk penalty came out NEGATIVE (MARA "Priority
    # -1.2", tab audit 2026-11-15) — floor it at 0 = lowest priority.
    priority = max(0.0, (time_weight * earnings_time_factor) + (0.4 * edge) - risk_penalty)

    # Build transparent factor breakdown for UI explainability
    explanation = {
        "factor_breakdown": {
            "earnings_edge": {"raw": round(edge, 2), "weight": 0.35, "contribution": round(0.35 * edge, 2)},
            "momentum": {"raw": round(momentum, 2), "weight": 0.20, "contribution": round(0.20 * momentum, 2)},
            "news": {"raw": round(news, 2), "weight": 0.20, "contribution": round(0.20 * news, 2)},
            "technical_alignment": {"raw": round(technical, 2), "weight": 0.15, "contribution": round(0.15 * technical, 2)},
            "regime_alignment": {"raw": round(regime, 2), "weight": 0.10, "contribution": round(0.10 * regime, 2)},
        },
        "time_weight": round(time_weight, 2),
        "earnings_time_factor": round(earnings_time_factor, 3),
        "risk_penalty": int(risk_penalty),
        "priority_formula": "max(0, (time_weight * earnings_time_factor) + (0.4 * edge) - risk_penalty)",
        "priority_components": {
            "time_component": round(time_weight * earnings_time_factor, 2),
            "edge_component": round(0.4 * edge, 2),
            "risk_component": -int(risk_penalty),
        },
        "overall_formula": "0.35*edge + 0.20*momentum + 0.20*news + 0.15*technical + 0.10*regime",
    }

    return {
        "momentum_score": round(_clamp(momentum, 0.0, 100.0), 2),
        "news_score": round(_clamp(news, 0.0, 100.0), 2),
        "technical_alignment": round(_clamp(technical, 0.0, 100.0), 2),
        "regime_alignment": round(_clamp(regime, 0.0, 100.0), 2),
        "overall_score": round(_clamp(overall, 0.0, 100.0), 2),
        "priority_score": round(priority, 2),
        "confidence_score": int(round(_clamp(overall, 0.0, 100.0))),
        "risk_penalty": int(risk_penalty),
        "time_weight": round(time_weight, 2),
        "earnings_time_factor": round(earnings_time_factor, 3),
        "explanation": explanation,
    }


def default_news_signals() -> Dict[str, Any]:
    return {
        "counts": {
            "7d": 0,
            "30d": 0,
            "90d": 0,
            "positive": 0,
            "negative": 0,
            "risk_events": 0,
        },
        "top_headlines": {"7d": [], "30d": []},
        "catalysts_positive": [],
        "catalysts_negative": [],
        "risk_events": [],
        "sentiment_trend": "unknown",
        "theme_30d": "unknown",
        "news_catalyst_score": 0,
        "negative_risk_score": 0,
    }


def apply_hotlist_defaults(item: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(item or {})
    out.setdefault("earnings_edge_score", 50)
    out.setdefault("earnings_play", "WAIT")
    out.setdefault("earnings_setup", "C")
    out.setdefault("earnings_risk_flags", [])
    out.setdefault("risk_level", "med")
    out.setdefault("news_signals", default_news_signals())
    out.setdefault("momentum_1w", None)
    out.setdefault("momentum_1m", None)
    out.setdefault("top_risk", "unknown")
    out.setdefault("priority_score", 0.0)
    out.setdefault("overall_score", 0.0)
    out.setdefault("momentum_score", 50.0)
    out.setdefault("news_score", 50.0)
    out.setdefault("technical_alignment", 50.0)
    out.setdefault("regime_alignment", 50.0)
    out.setdefault("confidence_score", 50)
    out.setdefault("risk_penalty", 0)
    out.setdefault("time_weight", 0.0)
    out.setdefault("earnings_time_factor", 1.0)
    out.setdefault("historical_gap_avg", None)
    out.setdefault("historical_gap_std", None)
    out.setdefault("avg_earnings_move_8q", None)
    out.setdefault("atr_percentile", None)
    out.setdefault("ATR_percentile", None)
    out.setdefault("volatility_regime", "normal")
    out.setdefault(
        "earnings_history_summary",
        {
            "last_8": {
                "up_count": 0,
                "down_count": 0,
                "avg_gap_pct": 0.0,
            }
        },
    )
    out.setdefault(
        "expected_move_vs_avg",
        {
            "expected_move_pct": None,
            "avg_move_8q": None,
            "edge_pct": None,
        },
    )

    out["earnings_edge_score"] = max(0, min(100, _to_int(out.get("earnings_edge_score"), 50)))
    if str(out.get("earnings_play") or "").upper() not in ALLOWED_PLAYS:
        out["earnings_play"] = "WAIT"
    else:
        out["earnings_play"] = str(out.get("earnings_play")).upper()

    if str(out.get("earnings_setup") or "").upper() not in ALLOWED_SETUPS:
        out["earnings_setup"] = "C"
    else:
        out["earnings_setup"] = str(out.get("earnings_setup")).upper()

    risk_level = str(out.get("risk_level") or "med").lower()
    if risk_level not in ALLOWED_RISK_LEVELS:
        risk_level = "med"
    out["risk_level"] = risk_level

    flags = out.get("earnings_risk_flags")
    if not isinstance(flags, list):
        flags = []
    out["earnings_risk_flags"] = [str(x) for x in flags if str(x).strip()]

    news = out.get("news_signals")
    if not isinstance(news, dict):
        news = default_news_signals()
    merged_news = default_news_signals()
    merged_news.update(news)
    if not isinstance(merged_news.get("counts"), dict):
        merged_news["counts"] = default_news_signals()["counts"]
    else:
        counts = default_news_signals()["counts"]
        counts.update(merged_news["counts"])
        merged_news["counts"] = counts
    if not isinstance(merged_news.get("top_headlines"), dict):
        merged_news["top_headlines"] = {"7d": [], "30d": []}
    out["news_signals"] = merged_news

    if not out.get("top_risk"):
        out["top_risk"] = out["earnings_risk_flags"][0] if out["earnings_risk_flags"] else "unknown"

    # Normalize V8 extras.
    out["priority_score"] = round(_to_float(out.get("priority_score"), 0.0) or 0.0, 2)
    out["overall_score"] = round(_to_float(out.get("overall_score"), 0.0) or 0.0, 2)
    out["momentum_score"] = round(_clamp(_to_float(out.get("momentum_score"), 50.0) or 50.0, 0.0, 100.0), 2)
    out["news_score"] = round(_clamp(_to_float(out.get("news_score"), 50.0) or 50.0, 0.0, 100.0), 2)
    out["technical_alignment"] = round(
        _clamp(_to_float(out.get("technical_alignment"), 50.0) or 50.0, 0.0, 100.0), 2
    )
    out["regime_alignment"] = round(_clamp(_to_float(out.get("regime_alignment"), 50.0) or 50.0, 0.0, 100.0), 2)
    out["confidence_score"] = max(0, min(100, _to_int(out.get("confidence_score"), int(round(out["overall_score"])))))
    out["risk_penalty"] = max(0, _to_int(out.get("risk_penalty"), 0))
    out["time_weight"] = round(max(0.0, _to_float(out.get("time_weight"), 0.0) or 0.0), 2)
    out["earnings_time_factor"] = round(max(0.0, _to_float(out.get("earnings_time_factor"), 1.0) or 1.0), 3)

    atr_pct = _to_float(out.get("ATR_percentile"), None)
    if atr_pct is None:
        atr_pct = _to_float(out.get("atr_percentile"), None)
    if atr_pct is None:
        out["atr_percentile"] = None
        out["ATR_percentile"] = None
    else:
        atr_pct = _clamp(atr_pct, 0.0, 100.0)
        out["atr_percentile"] = round(atr_pct, 2)
        out["ATR_percentile"] = round(atr_pct, 2)

    vr = str(out.get("volatility_regime") or "normal").strip().lower()
    if vr not in {"compression", "low", "normal", "high", "elevated", "extreme"}:
        vr = "normal"
    out["volatility_regime"] = vr

    for k in ("historical_gap_avg", "historical_gap_std", "avg_earnings_move_8q"):
        val = _to_float(out.get(k), None)
        out[k] = round(val, 4) if val is not None else None

    hist = out.get("earnings_history_summary")
    if not isinstance(hist, dict):
        hist = {}
    last_8 = hist.get("last_8")
    if not isinstance(last_8, dict):
        last_8 = {}
    up_count = max(0, _to_int(last_8.get("up_count"), 0))
    down_count = max(0, _to_int(last_8.get("down_count"), 0))
    # sample_size was dropped here, so the card labelled a single sampled
    # quarter as "Earnings History (8Q)". Keep it (derive it from the counts
    # when the producer did not send it) so the UI can show the real n.
    sample_size = max(0, _to_int(hist.get("sample_size"), up_count + down_count))
    out["earnings_history_summary"] = {
        "last_8": {
            "up_count": up_count,
            "down_count": down_count,
            "avg_gap_pct": round(_to_float(last_8.get("avg_gap_pct"), 0.0) or 0.0, 4),
        },
        "sample_size": sample_size,
    }

    emv = out.get("expected_move_vs_avg")
    if not isinstance(emv, dict):
        emv = {}
    expected_move_pct = _to_float(emv.get("expected_move_pct"), None)
    avg_move_8q = _to_float(emv.get("avg_move_8q"), None)
    edge_pct = _to_float(emv.get("edge_pct"), None)
    out["expected_move_vs_avg"] = {
        "expected_move_pct": round(expected_move_pct, 4) if expected_move_pct is not None else None,
        "avg_move_8q": round(avg_move_8q, 4) if avg_move_8q is not None else None,
        "edge_pct": round(edge_pct, 4) if edge_pct is not None else None,
    }
    return out


def compute_earnings_edge_score(
    item: Dict[str, Any],
    news_signals: Optional[Dict[str, Any]] = None,
    market_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Deterministic scoring engine.
    No network calls are allowed in this function.
    """
    item = item or {}
    news_signals = news_signals or default_news_signals()
    market_data = market_data or {}

    score = 45
    risk_points = 0
    flags: List[str] = []

    earnings_days = _to_int(item.get("earnings_days"), -1)
    date_unknown = _earnings_date_unknown(item, earnings_days)
    if date_unknown:
        score -= 6
        risk_points += 1
        flags.append("earnings_date_unknown")
    elif earnings_days < -2:
        score -= 3
    elif earnings_days < 0:
        score += 1
    elif earnings_days <= 1:
        score += 12
    elif earnings_days <= 3:
        score += 10
    elif earnings_days <= 5:
        score += 7
    elif earnings_days <= 10:
        score += 3
    else:
        score -= 1

    trend_bias = _trend_bias(item.get("trend_1d")) + _trend_bias(item.get("trend_4h"))
    score += trend_bias * 6
    if trend_bias <= -1 and 0 <= earnings_days <= 5:
        risk_points += 2
        flags.append("trend_conflict")

    momentum_1w = _to_float(item.get("momentum_1w"), None)
    momentum_1m = _to_float(item.get("momentum_1m"), None)
    if momentum_1w is not None and momentum_1m is not None:
        if momentum_1w > 0 and momentum_1m > 0:
            score += 8
        elif momentum_1w < 0 and momentum_1m < 0:
            score -= 10
            risk_points += 2
            flags.append("negative_momentum")
        else:
            score -= 2
    else:
        flags.append("momentum_unknown")

    adx_value = _extract_adx(market_data)
    if adx_value is None:
        flags.append("adx_unknown")
    elif adx_value >= 35:
        score += 8
    elif adx_value >= 25:
        score += 5
    elif adx_value < 18:
        score -= 6
        risk_points += 1
        flags.append("weak_trend_adx")

    insider_sentiment = _extract_insider_sentiment(market_data)
    if insider_sentiment == "bullish":
        score += 6
    elif insider_sentiment == "bearish":
        score -= 7
        risk_points += 1
        flags.append("bearish_insider_flow")

    rating_score = _extract_rating_score(market_data)
    if rating_score is not None:
        if rating_score >= 4:
            score += 5
        elif rating_score <= 2:
            score -= 4
            risk_points += 1

    # Earnings volatility context (V8).
    avg_gap = _to_float(item.get("historical_gap_avg"), None)
    gap_std = _to_float(item.get("historical_gap_std"), None)
    avg_move_8q = _to_float(item.get("avg_earnings_move_8q"), None)
    atr_percentile = _to_float(item.get("ATR_percentile"), None)
    if atr_percentile is None:
        atr_percentile = _to_float(item.get("atr_percentile"), None)
    vol_regime = str(item.get("volatility_regime") or "").lower()

    if avg_move_8q is not None:
        if avg_move_8q >= 8.0:
            score += 8
        elif avg_move_8q >= 5.0:
            score += 5
        elif avg_move_8q <= 1.5:
            score -= 4
            risk_points += 1
            flags.append("earnings_move_low")
    else:
        flags.append("earnings_history_unknown")

    if avg_gap is not None:
        # Larger historical gaps increase opportunity but also uncertainty.
        score += _clamp(abs(avg_gap) * 0.7, -4.0, 6.0)

    if gap_std is not None:
        if gap_std >= 6.0:
            score -= 7
            risk_points += 2
            flags.append("earnings_gap_dispersion_high")
        elif gap_std >= 4.0:
            score -= 3
            risk_points += 1
            flags.append("earnings_gap_dispersion_med")

    if atr_percentile is not None:
        if atr_percentile >= 85:
            score += 5
            risk_points += 1
            flags.append("volatility_extreme")
        elif atr_percentile >= 65:
            score += 4
        elif atr_percentile <= 20:
            score -= 4
            flags.append("volatility_compressed")

    if vol_regime in {"high", "elevated"}:
        score += 4
    elif vol_regime == "extreme":
        score += 2
        risk_points += 1
    elif vol_regime in {"low", "compression"}:
        score -= 3

    counts = news_signals.get("counts", {}) if isinstance(news_signals, dict) else {}
    pos_count = _to_int(counts.get("positive"), 0)
    neg_count = _to_int(counts.get("negative"), 0)
    risk_events_count = _to_int(counts.get("risk_events"), 0)
    trend = str(news_signals.get("sentiment_trend") or "unknown").lower()

    news_catalyst_score = _to_int(news_signals.get("news_catalyst_score"), 0)
    negative_risk_score = _to_int(news_signals.get("negative_risk_score"), 0)

    score += max(-10, min(10, news_catalyst_score))
    score -= max(0, min(20, negative_risk_score))

    if pos_count == 0 and neg_count == 0:
        risk_points += 1
        flags.append("news_context_unknown")
    if risk_events_count > 0:
        flags.append("risk_events_present")
        risk_points += min(3, risk_events_count)
    if trend == "bearish":
        score -= 3
        risk_points += 1
    elif trend == "bullish":
        score += 2

    score = max(0, min(100, int(round(score))))
    setup = "A" if score >= 75 else ("B" if score >= 60 else "C")

    if risk_points >= 4 or negative_risk_score >= 10 or risk_events_count >= 2:
        risk_level = "high"
    elif risk_points >= 2 or flags:
        risk_level = "med"
    else:
        risk_level = "low"

    if date_unknown:
        play = "WAIT" if score >= 45 else "AVOID"
    elif earnings_days < 0:
        if abs(earnings_days) <= 2 and score >= 65 and risk_level != "high":
            play = "PLAY_POST"
        elif score < 45 or risk_level == "high":
            play = "AVOID"
        else:
            play = "WAIT"
    elif earnings_days <= 5:
        if score >= 68 and risk_level != "high":
            play = "PLAY_PRE"
        elif score < 45 or (risk_level == "high" and score < 62):
            play = "AVOID"
        else:
            play = "WAIT"
    else:
        play = "WAIT" if score >= 35 else "AVOID"

    # Keep unique flags, stable order.
    dedup_flags: List[str] = []
    seen = set()
    for f in flags:
        if f in seen:
            continue
        seen.add(f)
        dedup_flags.append(f)

    risk_events = news_signals.get("risk_events", []) if isinstance(news_signals, dict) else []
    # top_risk should surface the most material STRUCTURED risk flag (e.g.
    # GAP_RISK_HIGH / LOW_COVERAGE / EARNINGS_IMMINENT), not a raw news headline.
    # Pre-fix this picked risk_events[0] — a news headline string — so the Hot
    # List "Top Risk" column showed an arbitrary headline (e.g. an AAPL story on
    # an unrelated ticker) instead of the actual earnings risk. Prefer flags;
    # fall back to a news risk_event headline only when no structured flag exists.
    if dedup_flags:
        top_risk = dedup_flags[0]
    elif isinstance(risk_events, list) and risk_events:
        top_risk = str(risk_events[0])
    else:
        top_risk = "unknown"

    return {
        "earnings_edge_score": score,
        "earnings_setup": setup,
        "earnings_play": play,
        "earnings_risk_flags": dedup_flags,
        "risk_level": risk_level,
        "top_risk": top_risk,
    }


# --------------------------------------------------------------------------
# Quote freshness + data sufficiency (tab audit 2026-11-15)
#
# Live evidence: the Hot List showed NVDA $225.25 while the Watchlist showed
# $161.01 at the same moment. The hot list read the engine snapshot row
# (last_update 2026-09-08, 68 days old) and presented it as current, and every
# card printed AVOID / RISK HIGH / Risk Heat 95% while momentum, ADX and the
# earnings history were all missing. These helpers are pure (no I/O).
# --------------------------------------------------------------------------

def parse_hotlist_ts(value: Any) -> Optional[datetime]:
    """ISO timestamp (or epoch seconds) -> aware UTC datetime, else None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def last_us_equity_close(now: Optional[datetime] = None) -> Optional[datetime]:
    """UTC time of the most recent regular-session close at or before `now`."""
    try:
        from core.market_hours import (
            EASTERN_TZ,
            US_EQUITY_CLOSE,
            US_EQUITY_EARLY_CLOSE,
            _us_equity_early_close_date,
            is_us_equity_trading_day,
        )
    except Exception:
        return None
    now_utc = parse_hotlist_ts(now) or datetime.now(timezone.utc)
    now_et = now_utc.astimezone(EASTERN_TZ)
    day = now_et.date()
    for _ in range(12):
        if is_us_equity_trading_day(day):
            close_time = US_EQUITY_EARLY_CLOSE if _us_equity_early_close_date(day) else US_EQUITY_CLOSE
            close_dt = datetime.combine(day, close_time, tzinfo=EASTERN_TZ)
            if close_dt <= now_et:
                return close_dt.astimezone(timezone.utc)
        day = day - timedelta(days=1)
    return None


def _is_us_equity_regular_session(now_utc: datetime) -> bool:
    try:
        from core.market_hours import is_us_equity_market_open

        return bool(is_us_equity_market_open(now_utc))
    except Exception:
        return False


def is_hotlist_quote_stale(
    as_of: Any,
    *,
    now: Optional[datetime] = None,
    always_open: bool = False,
) -> bool:
    """True when a quote observed at `as_of` no longer describes the market.

    - 24/7 instruments (crypto) and the regular US session: older than the
      delayed-feed allowance.
    - US market closed: the quote must have been taken at/after the last
      regular close (minus the same allowance) — an older one predates trading
      that has since happened.
    - Unknown observation time: stale (age cannot be shown as current).
    """
    observed = parse_hotlist_ts(as_of)
    if observed is None:
        return True
    now_utc = parse_hotlist_ts(now) or datetime.now(timezone.utc)
    age = (now_utc - observed).total_seconds()
    if always_open or _is_us_equity_regular_session(now_utc):
        return age > HOTLIST_QUOTE_MAX_AGE_SECONDS
    last_close = last_us_equity_close(now_utc)
    if last_close is None:
        return age > 4 * 86400
    return observed < (last_close - timedelta(seconds=HOTLIST_QUOTE_MAX_AGE_SECONDS))


def is_hotlist_technicals_stale(
    as_of: Any,
    *,
    now: Optional[datetime] = None,
    always_open: bool = False,
) -> bool:
    """True when engine trend/RSI data missed at least one full session."""
    observed = parse_hotlist_ts(as_of)
    if observed is None:
        return True
    now_utc = parse_hotlist_ts(now) or datetime.now(timezone.utc)
    if always_open:
        return (now_utc - observed).total_seconds() > HOTLIST_TECHNICALS_MAX_AGE_SECONDS
    last_close = last_us_equity_close(now_utc)
    if last_close is None:
        return (now_utc - observed).total_seconds() > 4 * 86400
    return observed < (last_close - timedelta(seconds=HOTLIST_TECHNICALS_MAX_AGE_SECONDS))


def _positive_price(value: Any) -> Optional[float]:
    price = _to_float(value, None)
    if price is None or price != price or price <= 0:
        return None
    return price


def _nullable_pct(value: Any) -> Optional[float]:
    pct = _normalize_pct_input(value)
    if pct is None or pct != pct or pct in (float("inf"), float("-inf")):
        return None
    return pct


def resolve_hotlist_quote(
    engine_row: Optional[Dict[str, Any]],
    live_row: Optional[Dict[str, Any]],
    watchlist_row: Optional[Dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
    always_open: bool = False,
) -> Dict[str, Any]:
    """Pick the freshest known quote for a hot-list ticker and report its age.

    Candidates: the engine snapshot row (`last_update`), the persisted live
    quote (`timestamp`, the same store the Watchlist's /api/live-prices fills)
    and the already-built watchlist row. The newest observation wins; the
    result always carries `price_as_of` / `price_age_seconds` / `price_stale`
    so an old price can never be rendered as a current one.
    """
    engine_row = engine_row if isinstance(engine_row, dict) else {}
    live_row = live_row if isinstance(live_row, dict) else {}
    watchlist_row = watchlist_row if isinstance(watchlist_row, dict) else {}
    now_utc = parse_hotlist_ts(now) or datetime.now(timezone.utc)

    candidates: List[Dict[str, Any]] = []
    engine_price = _positive_price(engine_row.get("price"))
    engine_ts = parse_hotlist_ts(engine_row.get("last_update"))
    if engine_price is not None:
        candidates.append({
            "price": engine_price,
            "change_pct": _nullable_pct(engine_row.get("change_pct")),
            "as_of": engine_ts,
            "source": str(engine_row.get("data_source") or "engine"),
            "origin": "engine_snapshot",
        })
    live_price = _positive_price(live_row.get("price"))
    live_ts = parse_hotlist_ts(live_row.get("timestamp") or live_row.get("last_update"))
    if live_price is not None:
        candidates.append({
            "price": live_price,
            "change_pct": _nullable_pct(live_row.get("change_pct")),
            "as_of": live_ts,
            "source": str(live_row.get("source") or "live_prices_cache"),
            "origin": "live_quote",
        })
    wl_price = _positive_price(watchlist_row.get("price"))
    if wl_price is not None:
        wl_ts = parse_hotlist_ts(
            watchlist_row.get("price_timestamp")
            or watchlist_row.get("price_as_of")
            or watchlist_row.get("last_update")
        )
        same_as_engine = engine_price is not None and abs(wl_price - engine_price) <= 1e-9
        same_as_live = live_price is not None and abs(wl_price - live_price) <= 1e-9
        if wl_ts is not None or not (same_as_engine or same_as_live):
            # A watchlist price that differs from both stored quotes was
            # refreshed while this response was being built.
            candidates.append({
                "price": wl_price,
                "change_pct": _nullable_pct(watchlist_row.get("change_pct")),
                "as_of": wl_ts or now_utc,
                "source": str(
                    watchlist_row.get("price_source") or watchlist_row.get("data_source") or "watchlist_refresh"
                ),
                "origin": "watchlist_row",
            })

    if not candidates:
        return {
            "price": None,
            "change_pct": None,
            "price_as_of": None,
            "price_age_seconds": None,
            "price_source": "",
            "price_origin": "",
            "price_stale": True,
        }

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    best = max(candidates, key=lambda c: c["as_of"] or epoch)
    as_of = best["as_of"]
    age = max(0.0, (now_utc - as_of).total_seconds()) if as_of is not None else None
    return {
        "price": best["price"],
        "change_pct": best["change_pct"],
        "price_as_of": as_of.isoformat() if as_of is not None else None,
        "price_age_seconds": round(age, 1) if age is not None else None,
        "price_source": best["source"],
        "price_origin": best["origin"],
        "price_stale": is_hotlist_quote_stale(as_of, now=now_utc, always_open=always_open),
    }


def _known_trend(value: Any) -> bool:
    return _trend_bias(value) != 0 or "NEUTRAL" in str(value or "").upper() or "RANGE" in str(value or "").upper()


def assess_hotlist_inputs(item: Dict[str, Any], *, has_earnings_cycle: bool = True) -> Dict[str, Any]:
    """Which scoring inputs does this card actually have?

    Returns the list of missing inputs, the coverage ratio and whether the
    card has enough to carry a verdict: a current price plus at least one
    daily-bar derived strength measure (momentum or ADX).
    `has_earnings_cycle=False` (crypto) leaves the earnings inputs out.
    """
    data = item or {}
    not_applicable_earnings = not has_earnings_cycle

    has_price = _positive_price(data.get("price")) is not None and not bool(data.get("price_stale"))
    has_trend = (_known_trend(data.get("trend_1d")) or _known_trend(data.get("trend_4h"))) and not bool(
        data.get("technicals_stale")
    )
    has_momentum = (
        _normalize_pct_input(data.get("momentum_1w")) is not None
        or _normalize_pct_input(data.get("momentum_1m")) is not None
    )
    adx_value = _extract_adx_value(data, {})
    has_adx = adx_value is not None and adx_value > 0
    news = data.get("news_signals") if isinstance(data.get("news_signals"), dict) else {}
    counts = news.get("counts") if isinstance(news.get("counts"), dict) else {}
    has_news = any(_to_int(counts.get(k), 0) > 0 for k in ("7d", "30d", "positive", "negative"))

    inputs: List[tuple] = [
        ("price", has_price),
        ("trend", has_trend),
        ("momentum", has_momentum),
        ("adx", has_adx),
        ("news", has_news),
    ]
    if not not_applicable_earnings:
        earnings_days = _to_int(data.get("earnings_days"), -1)
        has_date = not _earnings_date_unknown(data, earnings_days)
        inputs.append(("earnings_date", has_date))
        inputs.append(("earnings_history", _to_float(data.get("avg_earnings_move_8q"), None) is not None))

    missing = [name for name, ok in inputs if not ok]
    present = len(inputs) - len(missing)
    coverage = present / float(len(inputs)) if inputs else 0.0
    return {
        "inputs_total": len(inputs),
        "inputs_present": present,
        "missing_inputs": missing,
        "coverage": round(coverage, 4),
        "sufficient": bool(has_price and (has_momentum or has_adx)),
    }


def apply_hotlist_data_guard(item: Dict[str, Any], *, has_earnings_cycle: bool = True) -> Dict[str, Any]:
    """Make the verdict honest about the data behind it (mutates + returns item).

    - missing core inputs -> play INSUFFICIENT_DATA / setup N/A instead of a
      confident AVOID/WAIT built on neutral placeholders;
    - a stock whose last earnings are behind it (or unknown) has no earnings
      setup to play -> play N/A, verdict_status "no_upcoming_earnings";
    - confidence = overall score scaled by input coverage (it used to be a
      plain copy of the overall score under a different label);
    - "Top Risk" is a market risk, never a missing-input flag.
    """
    data = item if isinstance(item, dict) else {}
    assessment = assess_hotlist_inputs(data, has_earnings_cycle=has_earnings_cycle)
    coverage = float(assessment["coverage"])
    data["data_coverage_pct"] = int(round(coverage * 100))
    data["missing_inputs"] = list(assessment["missing_inputs"])

    flags = [str(f) for f in (data.get("earnings_risk_flags") or []) if str(f).strip()]
    if data.get("price_stale") and "price_stale" not in flags:
        flags.append("price_stale")
    if data.get("technicals_stale") and "technicals_stale" not in flags:
        flags.append("technicals_stale")
    data["earnings_risk_flags"] = flags
    real_flags = [f for f in flags if f not in DATA_GAP_FLAGS]
    data["risk_factor_flags"] = real_flags
    data["data_gap_flags"] = [f for f in flags if f in DATA_GAP_FLAGS]

    is_crypto_card = not has_earnings_cycle
    earnings_days = _to_int(data.get("earnings_days"), -1)

    status = "ok"
    if not assessment["sufficient"]:
        status = "insufficient_data"
        if not is_crypto_card:
            data["earnings_play"] = "INSUFFICIENT_DATA"
            data["earnings_setup"] = "N/A"
    elif not is_crypto_card and (_earnings_date_unknown(data, earnings_days) or earnings_days < -2):
        # Past the PLAY_POST window (2 sessions) or no date at all: there is no
        # earnings event left to play; AVOID/WAIT would be a verdict about nothing.
        status = "no_upcoming_earnings"
        data["earnings_play"] = "N/A"
        data["earnings_setup"] = "N/A"
    data["verdict_status"] = status

    overall = _clamp(_to_float(data.get("overall_score"), 0.0) or 0.0, 0.0, 100.0)
    data["confidence_score"] = int(round(overall * coverage))

    if not is_crypto_card:
        news = data.get("news_signals") if isinstance(data.get("news_signals"), dict) else {}
        risk_events = news.get("risk_events") if isinstance(news.get("risk_events"), list) else []
        if real_flags:
            data["top_risk"] = real_flags[0]
        elif risk_events:
            data["top_risk"] = str(risk_events[0])
        elif assessment["missing_inputs"]:
            data["top_risk"] = "not assessable - missing " + ", ".join(assessment["missing_inputs"])
        else:
            data["top_risk"] = "none identified"
    return data


def hotlist_sort_key(item: Dict[str, Any]) -> tuple:
    item = item or {}
    prio = _to_float(item.get("priority_score"), None)
    if prio is not None:
        overall = _to_float(item.get("overall_score"), 0.0) or 0.0
        edge = _to_int(item.get("earnings_edge_score"), 0)
        ticker = str(item.get("ticker") or "")
        return (-float(prio), -overall, -edge, ticker)

    # Backward-compatible fallback for older payloads.
    days = _to_int(item.get("earnings_days"), 9999)
    if days < 0:
        days = 9999
    edge = _to_int(item.get("earnings_edge_score"), 0)
    has_flags = 1 if (item.get("earnings_risk_flags") or []) else 0
    ticker = str(item.get("ticker") or "")
    return (days, -edge, has_flags, ticker)


def sort_hotlist_by_priority(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        return []
    out = [apply_hotlist_defaults(i) for i in items if isinstance(i, dict)]
    out.sort(key=hotlist_sort_key)
    return out


def warm_curated_daily_bars(tickers: List[str], *, chunk: int = 12, max_tickers: int = 36) -> int:
    """Start the background daily-bar warm-up for the curated hot list.

    Momentum, ADX, the expected move, the ATR percentile and the 8Q earnings
    history are all computed from daily bars that live in an in-memory cache.
    Nothing filled it for the curated list after a restart (only the first two
    curated tickers were warmed, and only once the heat strip had been opened),
    so every card stayed "N/A / unknown". This runs off the request thread and
    is a no-op for symbols that are already cached. Returns the number of
    symbols handed to the warmer (0 under pytest: no network from unit tests).
    """
    import os

    symbols = list(dict.fromkeys(str(t or "").upper().strip() for t in (tickers or []) if str(t or "").strip()))
    if not symbols:
        return 0
    if os.getenv("PYTEST_CURRENT_TEST") or str(os.getenv("TEST_MODE") or os.getenv("MS_TEST_MODE") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        return 0
    try:
        from api.state_service import _warm_symbol_bars
    except Exception as exc:  # pragma: no cover - import guard
        logger.debug("hot list bar warm-up unavailable: %s", exc)
        return 0
    symbols = symbols[: max(1, int(max_tickers))]
    size = max(1, int(chunk))
    for start in range(0, len(symbols), size):
        try:
            _warm_symbol_bars(symbols[start:start + size])
        except Exception as exc:
            logger.warning("hot list bar warm-up failed to start: %s", exc)
            break
    return len(symbols)


def get_hotlist_state() -> Dict[str, Any]:
    """
    Return the hotlist payload used by the web endpoint.
    Reads via state_service so scoring/enrichment logic stays centralized.
    """
    try:
        from api.state_service import get_state

        # live_prices=False: this builder runs server-side without a request
        # context, so the route-level cache-only default never applied and a
        # cache miss triggered a full sequential Yahoo watchlist refresh
        # (measured ~13s). Hot list prices come from engine/cache state.
        state = get_state(
            include_news=False,
            include_hotlist=True,
            hotlist_details=False,
            lean=False,
            live_prices=False,
        ) or {}

        raw_hotlist = state.get("hot_list")
        if not isinstance(raw_hotlist, list):
            raw_hotlist = []

        hotlist = sort_hotlist_by_priority(raw_hotlist)
        warm_curated_daily_bars([str(i.get("ticker") or "") for i in hotlist])
        last_update = state.get("last_update") or state.get("timestamp")
        return {
            "hotlist": hotlist,
            "hot_list": hotlist,
            "last_update": last_update,
        }
    except Exception as e:
        logger.error(f"Failed to build hotlist state: {e}")
        # "error" distinguishes a failed build from a genuinely empty hot list.
        return {"hotlist": [], "hot_list": [], "last_update": None, "error": "hotlist_state_unavailable"}
