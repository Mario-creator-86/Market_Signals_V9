import copy
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.data_quality import get_health_dashboard, QualifiedValue
from core.news_agent_access import get_news_agent_cached

logger = logging.getLogger("NewsPulse")

_CACHE_LOCK = threading.RLock()
_CACHE: Dict[str, Dict[str, Any]] = {}

# Equity symbols that are also ordinary English words must never match a
# breaking headline merely because the word appears in prose (for example,
# NOW matching "Bitcoin's most important level right now").
_AMBIGUOUS_WORD_SYMBOLS = {
    "ODD", "APP", "IT", "ALL", "NOW", "HAS", "BE", "FOR", "SEE", "DO", "ARE",
    "AI", "GM", "ON", "SO", "DAO",
}


def _breaking_article_matches_symbol(article: Dict[str, Any], symbol: str) -> bool:
    """Use article entity metadata first and strict lexical fallback second."""
    target = str(symbol or "").upper().strip()
    if not target:
        return False

    tagged_symbols = {
        str(article.get("ticker") or "").upper().strip(),
        *(
            str(value or "").upper().strip()
            for value in (
                article.get("related_tickers")
                if isinstance(article.get("related_tickers"), (list, tuple, set))
                else []
            )
        ),
    }
    tagged_symbols.discard("")
    if tagged_symbols:
        return target in tagged_symbols

    title = str(article.get("title") or "")
    if not title:
        return False
    escaped = re.escape(target)
    explicit_patterns = (
        rf"\${escaped}(?![A-Za-z0-9])",
        rf"\((?:NYSE|NASDAQ|AMEX|NYSEARCA|NYSEMKT)\s*:\s*{escaped}\)",
        rf"\b(?:NYSE|NASDAQ|AMEX|NYSEARCA|NYSEMKT)\s*:\s*{escaped}(?![A-Za-z0-9])",
        rf"\({escaped}\)",
    )
    if target in _AMBIGUOUS_WORD_SYMBOLS:
        return any(re.search(pattern, title, flags=re.IGNORECASE) for pattern in explicit_patterns)
    return re.search(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", title, flags=re.IGNORECASE) is not None


def reset_news_pulse_cache() -> None:
    invalidate_news_pulse_cache()


def invalidate_news_pulse_cache(ticker: Optional[str] = None) -> None:
    """Invalidate one ticker pulse after News Agent persisted fresher articles."""
    with _CACHE_LOCK:
        symbol = str(ticker or "").upper().strip()
        if symbol:
            _CACHE.pop(symbol, None)
        else:
            _CACHE.clear()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_sentiment_bucket(value: Any) -> str:
    text = str(value or "").lower()
    if "bull" in text:
        return "bullish"
    if "bear" in text:
        return "bearish"
    return "neutral"


def _to_impact_score(article: Dict[str, Any]) -> int:
    raw = article.get("impact_score")
    if raw is None:
        impact = str(article.get("impact") or "").lower()
        if impact == "critical":
            return 95
        if impact == "high":
            return 80
        if impact == "medium":
            return 55
        return 25
    try:
        score = int(float(raw))
    except Exception:
        score = 0
    return max(0, min(100, score))


def _is_source_backed(article: Dict[str, Any]) -> bool:
    """A source-backed article must retain the exact item the AI reviewed."""
    return all(
        str(article.get(field) or "").strip()
        for field in ("title", "published_at", "url", "source")
    )


def _usable_ai_analysis(article: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a normalized AI result only when it has an explicit verdict."""
    for field in ("ai_micro_summary", "ai_deep_analysis"):
        analysis = article.get(field)
        if not isinstance(analysis, dict):
            continue
        # A placeholder written when the model produced nothing usable (or the
        # article could not be fetched) is not an AI verdict. It used to count
        # as "AI analysed, neutral" in the pulse and in the evidence gate.
        if str(analysis.get("ai_status") or "ok").strip().lower() != "ok":
            continue
        if (
            str(analysis.get("tldr") or "").strip().lower() == "unknown"
            and str(analysis.get("article_explainer") or "unknown").strip().lower() == "unknown"
        ):
            continue
        sentiment = str(
            analysis.get("overall_sentiment") or analysis.get("sentiment") or ""
        ).strip().lower()
        if sentiment in {"bullish", "bearish", "neutral"}:
            return analysis
    return None


def _headline_payload(article: Dict[str, Any]) -> Dict[str, Any]:
    # Preserve the source identifiers used for this pulse.  The gate audit must
    # point to the exact published item instead of retaining only a generated
    # sentiment label.
    micro = article.get("ai_micro_summary")
    deep = article.get("ai_deep_analysis")
    return {
        "article_id": str(article.get("id") or ""),
        "title": str(article.get("title") or "unknown"),
        "time": str(article.get("published_at") or ""),
        "url": str(article.get("url") or ""),
        "source": str(article.get("source") or ""),
        "summary": str(article.get("summary") or "").strip()[:600],
        "ticker": str(article.get("ticker") or "").upper().strip(),
        "related_tickers": [
            str(value or "").upper().strip()
            for value in (
                article.get("related_tickers")
                if isinstance(article.get("related_tickers"), (list, tuple, set))
                else []
            )
            if str(value or "").strip()
        ][:6],
        "category": str(article.get("category") or "general").lower(),
        "impact": str(article.get("impact") or "low").lower(),
        "impact_score": _to_impact_score(article),
        "sentiment": _to_sentiment_bucket(article.get("sentiment")),
        "sentiment_score": article.get("sentiment_score"),
        "sentiment_confidence": article.get("sentiment_confidence"),
        # These are retained only when an existing analysis is attached to the
        # article.  This module never synthesizes an AI result.
        "ai_micro_summary": copy.deepcopy(micro) if isinstance(micro, dict) else None,
        "ai_deep_analysis": copy.deepcopy(deep) if isinstance(deep, dict) else None,
    }


def _empty_window_payload() -> Dict[str, Any]:
    return {
        "article_count": 0,
        "avg_impact_score": 0,
        "avg_actionability": 0,
        "sentiment_distribution": {"bullish": 0, "neutral": 0, "bearish": 0},
        "top_categories": [],
        "top_headlines": [],
        "breaking_count": 0,
        "source_backed_count": 0,
        "ai_analyzed_count": 0,
        "source_backed_ai_analyzed_count": 0,
        "ai_sentiment_distribution": {"bullish": 0, "neutral": 0, "bearish": 0},
    }


def _get_news_agent(allow_blocking: bool) -> Optional[Any]:
    return get_news_agent_cached(allow_blocking=allow_blocking)


def _summarize_window(articles: List[Dict[str, Any]], top_headlines_limit: int = 3) -> Dict[str, Any]:
    if not articles:
        return _empty_window_payload()

    sentiments = {"bullish": 0, "neutral": 0, "bearish": 0}
    categories: Dict[str, int] = {}
    impact_values: List[int] = []
    action_values: List[int] = []
    headlines = []
    seen_titles = set()
    breaking_count = 0
    source_backed_count = 0
    ai_analyzed_count = 0
    source_backed_ai_analyzed_count = 0
    ai_sentiments = {"bullish": 0, "neutral": 0, "bearish": 0}

    for article in articles:
        sent = _to_sentiment_bucket(article.get("sentiment"))
        sentiments[sent] += 1

        category = str(article.get("category") or "general").lower()
        categories[category] = categories.get(category, 0) + 1

        impact_values.append(_to_impact_score(article))
        try:
            action_val = int(float(article.get("action_score", 0)))
        except Exception:
            action_val = 0
        action_values.append(max(0, min(100, action_val)))

        if article.get("is_breaking"):
            breaking_count += 1

        source_backed = _is_source_backed(article)
        if source_backed:
            source_backed_count += 1

        ai_analysis = _usable_ai_analysis(article)
        if ai_analysis is not None:
            ai_analyzed_count += 1
            ai_sentiments[_to_sentiment_bucket(
                ai_analysis.get("overall_sentiment") or ai_analysis.get("sentiment")
            )] += 1
            if source_backed:
                source_backed_ai_analyzed_count += 1

        title_key = str(article.get("title") or "").strip().lower()
        if title_key and title_key not in seen_titles and len(headlines) < top_headlines_limit:
            seen_titles.add(title_key)
            headlines.append(_headline_payload(article))

    sorted_categories = sorted(categories.items(), key=lambda kv: kv[1], reverse=True)
    top_categories = [name for name, _ in sorted_categories[:3]]

    avg_impact = int(round(sum(impact_values) / max(1, len(impact_values))))
    avg_action = int(round(sum(action_values) / max(1, len(action_values))))

    return {
        "article_count": len(articles),
        "avg_impact_score": avg_impact,
        "avg_actionability": avg_action,
        "sentiment_distribution": sentiments,
        "top_categories": top_categories,
        "top_headlines": headlines,
        "breaking_count": breaking_count,
        "source_backed_count": source_backed_count,
        "ai_analyzed_count": ai_analyzed_count,
        "source_backed_ai_analyzed_count": source_backed_ai_analyzed_count,
        "ai_sentiment_distribution": ai_sentiments,
    }


def _score_news_pulse(window_24h: Dict[str, Any], window_7d: Dict[str, Any], duplicate_penalty: int) -> int:
    impact_component = min(45, int(window_24h.get("avg_impact_score", 0) * 0.45 + window_7d.get("avg_impact_score", 0) * 0.2))
    action_component = min(20, int(window_24h.get("avg_actionability", 0) * 0.2))
    breaking_component = min(20, int(window_24h.get("breaking_count", 0)) * 6)

    sentiment_dist = window_7d.get("sentiment_distribution", {}) or {}
    bull = int(sentiment_dist.get("bullish", 0))
    bear = int(sentiment_dist.get("bearish", 0))
    sentiment_component = 0
    if bull > bear:
        sentiment_component = min(10, bull - bear)
    elif bear > bull:
        sentiment_component = min(10, bear - bull)

    score = impact_component + action_component + breaking_component + sentiment_component - duplicate_penalty
    return max(0, min(100, int(score)))


def build_news_pulse(ticker: str, ttl_seconds: int = 45, max_items: int = 180, allow_blocking: bool = False) -> Dict[str, Any]:
    symbol = str(ticker or "").upper().strip()
    if not symbol:
        return {
            "24h": _empty_window_payload(),
            "7d": _empty_window_payload(),
            "score": 0,
            "duplicate_count": 0,
            "top_headlines": [],
            "sentiment_trend": "unknown",
        }

    now_ts = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(symbol)
        cache_ttl = cached.get("ttl", max(5, int(ttl_seconds or 45))) if cached else 0
        if cached and (now_ts - cached.get("ts", 0.0)) < cache_ttl:
            return copy.deepcopy(cached.get("data", {}))

    agent = _get_news_agent(allow_blocking)
    if agent is None:
        get_health_dashboard().register("news_pulse", QualifiedValue.unavailable("news_pulse_agent", "Agent not available"))
        payload = {
            "24h": _empty_window_payload(),
            "7d": _empty_window_payload(),
            "score": 0,
            "duplicate_count": 0,
            "top_headlines": [],
            "sentiment_trend": "unknown",
        }
        with _CACHE_LOCK:
            _CACHE[symbol] = {"ts": now_ts, "ttl": 5, "data": payload}
        return copy.deepcopy(payload)

    def deferred_snapshot(reason: str) -> Dict[str, Any]:
        if cached and isinstance(cached.get("data"), dict):
            payload = copy.deepcopy(cached["data"])
            payload["stale"] = True
            payload["stale_reason"] = reason
            return payload
        payload = {
            "24h": _empty_window_payload(),
            "7d": _empty_window_payload(),
            "score": 0,
            "duplicate_count": 0,
            "top_headlines": [],
            "sentiment_trend": "unknown",
            "stale": True,
            "stale_reason": reason,
        }
        with _CACHE_LOCK:
            _CACHE[symbol] = {"ts": now_ts, "ttl": 2, "data": payload}
        return copy.deepcopy(payload)

    try:
        nonblocking_reader = getattr(agent, "try_get_ticker_news", None)
        ticker_news = (
            nonblocking_reader(symbol, max_items=max(40, int(max_items)))
            if not allow_blocking and callable(nonblocking_reader)
            else agent.get_ticker_news(symbol, max_items=max(40, int(max_items)))
        )
        if ticker_news is None:
            return deferred_snapshot("news_refresh_commit_in_progress")
        get_health_dashboard().register("news_pulse", QualifiedValue.verified(len(ticker_news), "news_pulse_agent"))
    except Exception as e:
        logger.warning(f"Ticker news unavailable for {symbol}: {e}")
        get_health_dashboard().register("news_pulse", QualifiedValue.unavailable("news_pulse_agent", str(e)))
        ticker_news = []

    try:
        nonblocking_breaking_reader = getattr(agent, "try_get_breaking_news", None)
        breaking_news = (
            nonblocking_breaking_reader(max_age_minutes=7 * 24 * 60)
            if not allow_blocking and callable(nonblocking_breaking_reader)
            else agent.get_breaking_news(max_age_minutes=7 * 24 * 60)
        )
        if breaking_news is None:
            return deferred_snapshot("news_refresh_commit_in_progress")
    except Exception:
        breaking_news = []

    cutoff_24h = _now_utc() - timedelta(hours=24)
    cutoff_7d = _now_utc() - timedelta(days=7)

    merged: List[Dict[str, Any]] = []
    for article in ticker_news or []:
        dt = _parse_dt(article.get("published_at"))
        if not dt:
            continue
        payload = dict(article)
        payload["published_at"] = dt.isoformat()
        payload["is_breaking"] = bool(payload.get("is_breaking", False))
        merged.append(payload)

    for article in breaking_news or []:
        if not _breaking_article_matches_symbol(article, symbol):
            continue
        dt = _parse_dt(article.get("published_at"))
        if not dt:
            continue
        payload = dict(article)
        payload["published_at"] = dt.isoformat()
        payload["is_breaking"] = True
        merged.append(payload)

    merged.sort(key=lambda a: str(a.get("published_at") or ""), reverse=True)

    # De-dup by title/url for noise reduction.
    unique_articles: List[Dict[str, Any]] = []
    seen = set()
    duplicate_count = 0
    for article in merged:
        key = (str(article.get("url") or "").strip(), str(article.get("title") or "").strip().lower())
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        unique_articles.append(article)

    window_24h = [a for a in unique_articles if (_parse_dt(a.get("published_at")) or _now_utc()) >= cutoff_24h]
    window_7d = [a for a in unique_articles if (_parse_dt(a.get("published_at")) or _now_utc()) >= cutoff_7d]

    summary_24h = _summarize_window(window_24h, top_headlines_limit=3)
    summary_7d = _summarize_window(window_7d, top_headlines_limit=3)

    sentiment_trend = "unknown"
    dist_7d = summary_7d.get("sentiment_distribution", {}) or {}
    bull = int(dist_7d.get("bullish", 0))
    bear = int(dist_7d.get("bearish", 0))
    neutral = int(dist_7d.get("neutral", 0))
    if (bull + bear + neutral) >= 3:
        if bull > bear * 1.2:
            sentiment_trend = "bullish"
        elif bear > bull * 1.2:
            sentiment_trend = "bearish"
        else:
            sentiment_trend = "neutral"

    ai_sentiment_trend = "unknown"
    ai_dist_24h = summary_24h.get("ai_sentiment_distribution", {}) or {}
    ai_bull = int(ai_dist_24h.get("bullish", 0))
    ai_bear = int(ai_dist_24h.get("bearish", 0))
    ai_neutral = int(ai_dist_24h.get("neutral", 0))
    if (ai_bull + ai_bear + ai_neutral) >= 1:
        if ai_bull > ai_bear:
            ai_sentiment_trend = "bullish"
        elif ai_bear > ai_bull:
            ai_sentiment_trend = "bearish"
        else:
            ai_sentiment_trend = "neutral"

    duplicate_penalty = min(25, duplicate_count * 4)
    score = _score_news_pulse(summary_24h, summary_7d, duplicate_penalty=duplicate_penalty)

    payload = {
        "24h": summary_24h,
        "7d": summary_7d,
        "score": score,
        "duplicate_count": duplicate_count,
        "top_headlines": (summary_24h.get("top_headlines", []) or [])[:3],
        "sentiment_trend": sentiment_trend,
        "ai_sentiment_trend": ai_sentiment_trend,
        "generated_at": _now_utc().isoformat(),
    }

    with _CACHE_LOCK:
        _CACHE[symbol] = {"ts": now_ts, "ttl": max(5, int(ttl_seconds or 45)), "data": payload}
    return copy.deepcopy(payload)


def hydrate_news_pulse(
    ticker: str,
    *,
    ttl_seconds: int = 45,
    max_items: int = 180,
    allow_remote: bool = True,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Fetch-and-persist ticker news before building the AI decision snapshot.

    ``build_news_pulse`` is deliberately a cache reader. Scanner entry review is
    different: a newly discovered symbol must first ask the News Agent to fill
    its persistent ticker bucket.  This coordinator keeps that side effect
    explicit, invalidates only the affected pulse, and returns an auditable
    hydration receipt alongside the source-backed articles.
    """
    symbol = str(ticker or "").upper().strip()
    requested_at = _now_utc().isoformat()
    hydration: Dict[str, Any] = {
        "requested": True,
        "requested_at": requested_at,
        "ticker": symbol,
        "allow_remote": bool(allow_remote),
        "force_refresh": bool(force_refresh),
        "status": "unavailable",
        "reason": "news_agent_unavailable",
        "fetched": False,
        "persisted": False,
        "count": 0,
        "new_items": 0,
    }
    if not symbol:
        hydration["reason"] = "invalid_ticker"
        return {**build_news_pulse(symbol, ttl_seconds=ttl_seconds), "hydration": hydration}

    agent = _get_news_agent(allow_blocking=True)
    if agent is not None:
        ensure_cached = getattr(agent, "ensure_ticker_news_cached", None)
        if callable(ensure_cached):
            try:
                result = ensure_cached(
                    symbol,
                    force_refresh=bool(force_refresh),
                    allow_remote=bool(allow_remote),
                )
                result = result if isinstance(result, dict) else {}
                hydration.update({
                    "status": str(result.get("status") or "unknown"),
                    "reason": str(result.get("reason") or ""),
                    "fetched": bool(result.get("fetched", False)),
                    "count": int(result.get("count", 0) or 0),
                    "new_items": int(result.get("new_items", 0) or 0),
                })
                hydration["persisted"] = bool(
                    hydration["count"] > 0
                    and hydration["status"] in {"hit", "refreshed"}
                )
            except Exception as exc:
                hydration.update({
                    "status": "error",
                    "reason": str(exc),
                    "error": str(exc),
                })
                logger.warning("Ticker news hydration failed for %s: %s", symbol, exc)
        else:
            hydration["reason"] = "ensure_ticker_news_cached_unsupported"

    # A prior empty pulse can otherwise hide the freshly persisted bucket until
    # its normal TTL expires.
    invalidate_news_pulse_cache(symbol)
    pulse = build_news_pulse(
        symbol,
        ttl_seconds=ttl_seconds,
        max_items=max_items,
        allow_blocking=True,
    )
    pulse = pulse if isinstance(pulse, dict) else {}
    pulse["hydration"] = hydration
    return copy.deepcopy(pulse)


def build_news_pulse_bulk(tickers: List[str], ttl_seconds: int = 45, allow_blocking: bool = False) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for ticker in tickers or []:
        symbol = str(ticker or "").upper().strip()
        if not symbol:
            continue
        result[symbol] = build_news_pulse(symbol, ttl_seconds=ttl_seconds, allow_blocking=allow_blocking)
    return result
