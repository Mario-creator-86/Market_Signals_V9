import copy
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from agents.news_tab_agent import get_news_tab_agent_if_ready
from core.data_quality import get_health_dashboard, QualifiedValue
from core.news_agent_access import get_news_agent_cached

logger = logging.getLogger("HotListNews")

_NEWS_CACHE: Dict[tuple, Dict[str, Any]] = {}
_NEWS_CACHE_LOCK = threading.RLock()
_NEWS_CACHE_TTL_SECONDS = 180

_SUMMARY_CACHE: Dict[str, Dict[str, Any]] = {}
_SUMMARY_CACHE_LOCK = threading.RLock()
_SUMMARY_CACHE_TTL_SECONDS = 180


def reset_hotlist_news_caches() -> None:
    with _NEWS_CACHE_LOCK:
        _NEWS_CACHE.clear()
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE.clear()


_POSITIVE_KEYWORDS = (
    "upgrade",
    "beats",
    "beat",
    "guidance raise",
    "guidance raised",
    "buyback",
    "partnership",
    "contract",
    "approval",
    "launch",
    "record revenue",
    "margin expansion",
)

_NEGATIVE_KEYWORDS = (
    "downgrade",
    "miss",
    "guidance cut",
    "guidance lowered",
    "profit warning",
    "weak demand",
    "delay",
    "dilution",
    "layoff",
    "default",
    "bankruptcy",
)

_RISK_EVENT_KEYWORDS = (
    "investigation",
    "lawsuit",
    "litigation",
    "recall",
    "sec probe",
    "fraud",
    "accounting",
    "cfo resign",
    "ceo resign",
    "ceo steps down",
    "class action",
    "guidance cut",
    "downgrade",
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_published_at(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _get_news_agent(allow_blocking: bool) -> Optional[Any]:
    return get_news_agent_cached(allow_blocking=allow_blocking)


def _normalize_article(article: Dict[str, Any]) -> Dict[str, Any]:
    dt = _parse_published_at(article.get("published_at"))
    return {
        "title": str(article.get("title") or "unknown"),
        "url": str(article.get("url") or ""),
        "source": str(article.get("source") or "unknown"),
        "published_at": dt.isoformat() if dt else "",
        "sentiment": str(article.get("sentiment") or "neutral").lower(),
        "impact": str(article.get("impact") or "low").lower(),
        "category": str(article.get("category") or "general").lower(),
        "summary": str(article.get("summary") or ""),
        "is_breaking": bool(article.get("is_breaking", False)),
        # "social" = a user-written post (eToro feed), "news" = a publisher article.
        "content_type": str(article.get("content_type") or "news").lower(),
    }


def _article_text(article: Dict[str, Any]) -> str:
    return f"{article.get('title', '')} {article.get('summary', '')}".lower()


def _has_any_keyword(text: str, keywords: tuple) -> bool:
    return any(k in text for k in keywords)


def _sentiment_trend(articles_30d: List[Dict[str, Any]]) -> str:
    if len(articles_30d) < 3:
        return "unknown"
    bull = 0
    bear = 0
    for a in articles_30d:
        sent = str(a.get("sentiment") or "neutral").lower()
        if "bull" in sent:
            bull += 1
        elif "bear" in sent:
            bear += 1
    if bull == 0 and bear == 0:
        return "neutral"
    if bull >= bear * 1.25:
        return "bullish"
    if bear >= bull * 1.25:
        return "bearish"
    return "neutral"


def _theme_30d(articles_30d: List[Dict[str, Any]]) -> str:
    if not articles_30d:
        return "unknown"

    counters = {"analyst": 0, "guidance": 0, "product": 0, "legal": 0, "macro": 0}
    for a in articles_30d:
        text = _article_text(a)
        if "upgrade" in text or "downgrade" in text or "rating" in text:
            counters["analyst"] += 1
        if "guidance" in text:
            counters["guidance"] += 1
        if "launch" in text or "product" in text or "release" in text:
            counters["product"] += 1
        if _has_any_keyword(text, ("lawsuit", "litigation", "investigation", "recall")):
            counters["legal"] += 1
        if _has_any_keyword(text, ("inflation", "rates", "fed", "macro", "yield")):
            counters["macro"] += 1

    best = max(counters.items(), key=lambda x: x[1])
    if best[1] == 0:
        return "mixed/unknown"
    return best[0]


def get_ticker_news(ticker: str, days: int = 7, max_items: int = 60, allow_blocking: bool = False) -> List[Dict[str, Any]]:
    """
    Return ticker news from NewsTabAgent cache, filtered by time window.
    """
    ticker = str(ticker or "").upper().strip()
    if not ticker:
        return []

    days = max(1, min(int(days or 7), 365))
    max_items = max(1, min(int(max_items or 60), 500))
    cache_key = (ticker, days, max_items)
    now_ts = time.time()

    with _NEWS_CACHE_LOCK:
        cached = _NEWS_CACHE.get(cache_key)
        cache_ttl = cached.get("ttl", _NEWS_CACHE_TTL_SECONDS) if cached else 0
        if cached and (now_ts - cached.get("ts", 0.0)) < cache_ttl:
            return copy.deepcopy(cached.get("data", []))

    articles: List[Dict[str, Any]] = []
    try:
        agent = _get_news_agent(allow_blocking)
        if agent is None:
            get_health_dashboard().register("hotlist_news", QualifiedValue.unavailable("news_agent", "Agent not available"))
            with _NEWS_CACHE_LOCK:
                _NEWS_CACHE[cache_key] = {"ts": now_ts, "ttl": 5, "data": []}
            return []
        raw = agent.get_ticker_news(ticker, max_items=max(120, max_items * 3))
        cutoff = _now_utc() - timedelta(days=days)
        for item in raw or []:
            dt = _parse_published_at(item.get("published_at"))
            if not dt or dt < cutoff:
                continue
            articles.append(_normalize_article(item))

        # Include recent breaking items that directly mention the ticker.
        max_age = min(days * 24 * 60, 90 * 24 * 60)
        for item in agent.get_breaking_news(max_age_minutes=max_age) or []:
            title = str(item.get("title") or "")
            hit = str(item.get("ticker") or "").upper() == ticker or ticker in title.upper()
            if not hit:
                continue
            dt = _parse_published_at(item.get("published_at"))
            if not dt or dt < cutoff:
                continue
            norm = _normalize_article(item)
            norm["is_breaking"] = True
            articles.append(norm)

        # De-duplicate by URL+title.
        seen = set()
        unique = []
        for a in articles:
            key = (a.get("url"), a.get("title"))
            if key in seen:
                continue
            seen.add(key)
            unique.append(a)

        unique.sort(key=lambda x: x.get("published_at", ""), reverse=True)
        result = unique[:max_items]
        get_health_dashboard().register("hotlist_news", QualifiedValue.verified(len(result), "news_agent"))
    except Exception as e:
        logger.warning(f"get_ticker_news failed for {ticker}: {e}")
        get_health_dashboard().register("hotlist_news", QualifiedValue.unavailable("news_agent", str(e)))
        result = []

    with _NEWS_CACHE_LOCK:
        _NEWS_CACHE[cache_key] = {"ts": now_ts, "ttl": _NEWS_CACHE_TTL_SECONDS, "data": result}
    return copy.deepcopy(result)


def summarize_ticker_news_signals(
    ticker: str,
    news_7d: Optional[List[Dict[str, Any]]] = None,
    news_30d: Optional[List[Dict[str, Any]]] = None,
    news_90d: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Build compact, prompt-safe multi-window news signals.
    """
    ticker = str(ticker or "").upper().strip()
    if not ticker:
        return {
            "counts": {},
            "top_headlines": {"7d": [], "30d": []},
            "catalysts_positive": [],
            "catalysts_negative": [],
            "risk_events": [],
            "sentiment_trend": "unknown",
            "news_catalyst_score": 0,
            "negative_risk_score": 0,
            "theme_30d": "unknown",
        }

    cache_key = ticker
    now_ts = time.time()
    if news_7d is None and news_30d is None and news_90d is None:
        with _SUMMARY_CACHE_LOCK:
            cached = _SUMMARY_CACHE.get(cache_key)
            if cached and (now_ts - cached.get("ts", 0.0)) < _SUMMARY_CACHE_TTL_SECONDS:
                return copy.deepcopy(cached.get("data", {}))

    n7 = news_7d if news_7d is not None else get_ticker_news(ticker, days=7, max_items=80)
    n30 = news_30d if news_30d is not None else get_ticker_news(ticker, days=30, max_items=120)
    n90 = news_90d if news_90d is not None else get_ticker_news(ticker, days=90, max_items=180)

    catalysts_positive: List[str] = []
    catalysts_negative: List[str] = []
    risk_events: List[str] = []

    for a in n30:
        # A user's social post is opinion, not a company catalyst or a risk
        # event: "Weekly update from <user>" was listed as a positive catalyst
        # because the post body contained a keyword.
        if str(a.get("content_type") or "news").lower() == "social":
            continue
        text = _article_text(a)
        title = a.get("title", "unknown")
        is_pos = _has_any_keyword(text, _POSITIVE_KEYWORDS) or "bull" in str(a.get("sentiment", ""))
        is_neg = _has_any_keyword(text, _NEGATIVE_KEYWORDS) or "bear" in str(a.get("sentiment", ""))
        is_risk = _has_any_keyword(text, _RISK_EVENT_KEYWORDS)
        if is_pos and title not in catalysts_positive:
            catalysts_positive.append(title)
        if is_neg and title not in catalysts_negative:
            catalysts_negative.append(title)
        if is_risk and title not in risk_events:
            risk_events.append(title)

    trend = _sentiment_trend(n30)
    theme_30d = _theme_30d(n30)

    # Deterministic scores for downstream earnings edge scoring.
    catalyst_score = min(10, (len(catalysts_positive[:6]) * 2))
    catalyst_score -= min(10, len(catalysts_negative[:6]) * 2)
    if trend == "bullish":
        catalyst_score += 2
    elif trend == "bearish":
        catalyst_score -= 2

    negative_risk_score = min(20, len(catalysts_negative[:6]) * 2 + len(risk_events[:3]) * 3)
    if trend == "bearish":
        negative_risk_score = min(20, negative_risk_score + 2)

    result = {
        "counts": {
            "7d": len(n7),
            "30d": len(n30),
            "90d": len(n90),
            "positive": len(catalysts_positive),
            "negative": len(catalysts_negative),
            "risk_events": len(risk_events),
            "social_30d": sum(
                1 for a in n30 if str(a.get("content_type") or "news").lower() == "social"
            ),
        },
        "top_headlines": {
            "7d": [a.get("title", "unknown") for a in n7[:6]],
            "30d": [a.get("title", "unknown") for a in n30[:6]],
        },
        "catalysts_positive": catalysts_positive[:6],
        "catalysts_negative": catalysts_negative[:6],
        "risk_events": risk_events[:3],
        "sentiment_trend": trend,
        "theme_30d": theme_30d,
        "news_catalyst_score": int(max(-10, min(10, catalyst_score))),
        "negative_risk_score": int(max(0, min(20, negative_risk_score))),
    }

    if news_7d is None and news_30d is None and news_90d is None:
        # Only cache if we actually had a ready agent, otherwise we cache empty results during startup
        if get_news_tab_agent_if_ready() is not None:
            with _SUMMARY_CACHE_LOCK:
                _SUMMARY_CACHE[cache_key] = {"ts": now_ts, "data": result}
    return copy.deepcopy(result)
