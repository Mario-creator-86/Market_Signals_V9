"""Deterministic Phase 10 news pipeline over V7 provider output.

Stages are explicit and independently testable: normalize, deduplicate, entity
match, financial sentiment, and catalyst classification.  The service accepts
plain mappings so existing V7 fetchers remain untouched.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.data_model.entities import NewsArticle
from core.data_model.pit import PointInTime, to_utc
from core.news_v9.finbert import FinancialSentimentService


_TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source",
    "utm_campaign", "utm_content", "utm_medium", "utm_source", "utm_term",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class NewsPipelineResult:
    articles: Tuple[NewsArticle, ...]
    observed_at: datetime
    input_count: int
    normalized_count: int
    primary_count: int
    duplicate_count: int
    rejected_count: int


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return to_utc(value)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return to_utc(parsed)


def canonicalize_url(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    query = [
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_QUERY_KEYS
    ]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", urlencode(query), ""))


def _headline_tokens(value: str) -> frozenset[str]:
    return frozenset(token for token in _TOKEN_RE.findall(str(value or "").lower()) if len(token) > 1)


def _similarity(left: str, right: str) -> float:
    a, b = _headline_tokens(left), _headline_tokens(right)
    if not a or not b:
        return 0.0
    overlap = len(a & b)
    return max(overlap / len(a | b), overlap / min(len(a), len(b)))


class NewsPipeline:
    def __init__(
        self,
        *,
        sentiment: FinancialSentimentService,
        entity_aliases: Optional[Mapping[str, Sequence[str]]] = None,
        catalyst_patterns: Optional[Mapping[str, Sequence[str]]] = None,
        duplicate_similarity: float = 0.78,
    ):
        if not 0.0 <= float(duplicate_similarity) <= 1.0:
            raise ValueError("duplicate_similarity must be in [0, 1]")
        self.sentiment = sentiment
        self.entity_aliases = {
            str(ticker).upper(): tuple(str(alias).strip() for alias in aliases if str(alias).strip())
            for ticker, aliases in (entity_aliases or {}).items()
            if str(ticker).strip()
        }
        self.catalyst_patterns = {
            str(name).lower(): tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)
            for name, patterns in (catalyst_patterns or {}).items()
        }
        self.duplicate_similarity = float(duplicate_similarity)

    def _entities(self, text: str, explicit_ticker: str) -> Tuple[str, ...]:
        found = []
        ticker = str(explicit_ticker or "").strip().upper()
        if ticker:
            found.append(ticker)
        for candidate, aliases in self.entity_aliases.items():
            terms = (candidate, *aliases)
            if any(re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text, re.IGNORECASE) for term in terms):
                found.append(candidate)
        return tuple(dict.fromkeys(found))

    def _catalyst(self, text: str, fallback: Any) -> str:
        for name, patterns in self.catalyst_patterns.items():
            if any(pattern.search(text) for pattern in patterns):
                return name
        return str(fallback or "general").strip().lower() or "general"

    def normalize(self, raw: Mapping[str, Any], observed_at: datetime) -> Optional[NewsArticle]:
        title = " ".join(str(raw.get("title") or "").split())[:1_000]
        source = " ".join(str(raw.get("source") or "").split())[:256]
        url = canonicalize_url(raw.get("url"))
        published_at = _parse_datetime(raw.get("published_at"))
        if not title or not source or not url or published_at is None:
            return None
        summary = " ".join(str(raw.get("summary") or "").split())[:6_000]
        explicit_ticker = str(raw.get("ticker") or "").strip().upper()
        entities = self._entities(f"{title} {summary}", explicit_ticker)
        sentiment = self.sentiment.analyze(f"{title}. {summary}")
        catalyst = self._catalyst(f"{title} {summary}", raw.get("category"))
        identity = str(raw.get("id") or "").strip()
        if not identity:
            identity = hashlib.sha256(f"{url}|{published_at.isoformat()}".encode("utf-8")).hexdigest()
        fingerprint = hashlib.sha256(" ".join(sorted(_headline_tokens(title))).encode("utf-8")).hexdigest()
        pit = PointInTime(
            event_time=published_at,
            received_at=observed_at,
            available_from=observed_at,
            fetched_at=observed_at,
            version=0,
        )
        label = sentiment.label
        if label == "positive":
            label = "bullish"
        elif label == "negative":
            label = "bearish"
        return NewsArticle(
            id=identity,
            title=title,
            url=url,
            source=source,
            published_at=published_at,
            ticker=entities[0] if entities else None,
            category=catalyst,
            sentiment=label,
            sentiment_score=sentiment.score,
            sentiment_confidence=sentiment.confidence,
            story_fingerprint=fingerprint,
            summary=summary,
            related_tickers=entities,
            sentiment_quality={
                "level": "MEASURED" if sentiment.source == "finbert" else "ESTIMATED",
                "source": sentiment.source,
                "model": sentiment.model,
                "revision": sentiment.revision,
                "fallback_used": sentiment.fallback_used,
                "available": sentiment.available,
                "error": sentiment.error,
            },
            pit=pit,
        )

    def run(self, rows: Iterable[Mapping[str, Any]], *, observed_at: Optional[datetime] = None) -> NewsPipelineResult:
        observed = to_utc(observed_at or datetime.now(timezone.utc))
        incoming = list(rows)
        normalized = [article for row in incoming if isinstance(row, Mapping) if (article := self.normalize(row, observed))]
        primaries = []
        output = []
        duplicate_count = 0
        for article in sorted(normalized, key=lambda item: item.published_at):
            primary_index = None
            for index, primary in enumerate(primaries):
                same_url = article.url == primary.url
                same_ticker = not article.ticker or not primary.ticker or article.ticker == primary.ticker
                if same_url or (same_ticker and _similarity(article.title, primary.title) >= self.duplicate_similarity):
                    primary_index = index
                    break
            if primary_index is None:
                primaries.append(article)
                output.append(article)
                continue
            duplicate_count += 1
            primary = primaries[primary_index]
            reporters = tuple(sorted(set(primary.also_reported_by + (article.source,)) - {primary.source}))
            updated_primary = replace(primary, also_reported_by=reporters)
            primaries[primary_index] = updated_primary
            output[output.index(primary)] = updated_primary
            output.append(replace(article, is_duplicate=True, story_fingerprint=primary.story_fingerprint))
        output.sort(key=lambda item: item.published_at, reverse=True)
        return NewsPipelineResult(
            articles=tuple(output),
            observed_at=observed,
            input_count=len(incoming),
            normalized_count=len(normalized),
            primary_count=len(normalized) - duplicate_count,
            duplicate_count=duplicate_count,
            rejected_count=len(incoming) - len(normalized),
        )
