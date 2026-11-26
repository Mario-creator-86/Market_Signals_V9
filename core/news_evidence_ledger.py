"""Append-only, source-backed evidence captured for future news-aware research.

The ledger records when an article became known to the application and when an
AI summary became available.  Those timestamps are essential: a future
walk-forward replay may only use evidence observed/analyzed no later than the
bar it evaluates.  This prevents a late backfill or a later AI run from leaking
information into a historical decision.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("NewsEvidenceLedger")
from typing import Any, Dict, Optional

from core.config import DATA_DIR


NEWS_EVIDENCE_LEDGER_PATH = DATA_DIR / "news_evidence_ledger.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, limit: int = 6000) -> str:
    return str(value or "").strip()[:limit]


class NewsEvidenceLedger:
    """Thread-safe append-only ledger with a deliberately small public schema."""

    def __init__(self, path: Path = NEWS_EVIDENCE_LEDGER_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _append(self, record: Dict[str, Any]) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            with self._lock, self.path.open("a", encoding="utf-8") as handle:
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._rotate_if_bloated()
            return True
        except OSError:
            # Evidence capture must never block the live news flow.  The caller
            # remains fail-closed for Capital if it lacks current AI evidence.
            return False

    def _rotate_if_bloated(self, max_bytes: int = 25_000_000) -> None:
        """2026-09-03 audit P2: the jsonl grew unbounded with no production
        reader wired yet. Keep the newest evidence under a hard cap by keeping
        the last half of the records once the file exceeds max_bytes."""
        try:
            if not self.path.exists() or self.path.stat().st_size < max_bytes:
                return
            with self._lock:
                if not self.path.exists() or self.path.stat().st_size < max_bytes:
                    return
                rows = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
                keep = rows[len(rows) // 2:]
                tmp = self.path.with_suffix(".jsonl.tmp")
                tmp.write_text("".join(keep), encoding="utf-8")
                os.replace(tmp, self.path)
                logger.info(
                    "news_evidence_ledger rotated: kept %s of %s records",
                    len(keep), len(rows),
                )
        except OSError:
            pass  # rotation is best-effort; evidence capture stays fail-open

    @staticmethod
    def _article_fields(article: Dict[str, Any]) -> Optional[Dict[str, str]]:
        if not isinstance(article, dict):
            return None
        fields = {
            "article_id": _text(article.get("id"), 256),
            "ticker": _text(article.get("ticker") or "MARKET", 64).upper(),
            "title": _text(article.get("title"), 1000),
            "url": _text(article.get("url"), 3000),
            "source": _text(article.get("source"), 256),
            "published_at": _text(article.get("published_at"), 64),
        }
        if not all(fields[key] for key in ("article_id", "title", "url", "source", "published_at")):
            return None
        return fields

    def record_observed(self, article: Dict[str, Any], *, observed_at: Optional[str] = None) -> bool:
        fields = self._article_fields(article)
        if fields is None:
            return False
        return self._append(
            {
                "event": "article_observed",
                "observed_at": observed_at or _now_iso(),
                **fields,
            }
        )

    def record_ai_micro_summary(
        self,
        article: Dict[str, Any],
        micro_summary: Dict[str, Any],
        *,
        analyzed_at: Optional[str] = None,
    ) -> bool:
        fields = self._article_fields(article)
        if fields is None or not isinstance(micro_summary, dict):
            return False
        # An unavailable/placeholder briefing is not evidence that the AI
        # analysed the article; recording it claimed an analysis that never ran.
        if str(micro_summary.get("ai_status") or "ok").strip().lower() != "ok":
            return False
        if (
            str(micro_summary.get("tldr") or "").strip().lower() == "unknown"
            and str(micro_summary.get("article_explainer") or "unknown").strip().lower() == "unknown"
        ):
            return False
        # Persist only the normalized decision-relevant fields.The original
        # source remains the URL/title above; article text itself does not need
        # to be duplicated in a growing operational ledger.
        analysis = {
            "overall_sentiment": _text(micro_summary.get("overall_sentiment"), 32).lower(),
            "confidence": micro_summary.get("confidence"),
            "time_horizon": _text(micro_summary.get("time_horizon"), 64),
            "actionability": micro_summary.get("actionability"),
            "tldr": _text(micro_summary.get("tldr"), 500),
        }
        return self._append(
            {
                "event": "ai_micro_summary_available",
                "analyzed_at": analyzed_at or _now_iso(),
                **fields,
                "analysis": analysis,
            }
        )


news_evidence_ledger = NewsEvidenceLedger()


def _parse_iso_ts(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp; naive values are assumed UTC. None on failure."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def read_articles_as_of(T: datetime) -> list:
    """Typed point-in-time reader (spec §29/§32).

    Returns canonical `core.data_model.entities.NewsArticle` records that were
    OBSERVED no later than T. The point-in-time mapping is:
      observed_at  -> received_at / fetched_at / available_from  (when V9 saw it)
      published_at -> event_time                                  (when it was published)
    Through this reader it is impossible to surface an article V9 had not yet
    observed at T (§32). The JSONL format is unchanged; this only adds a typed
    reader over it. A missing or unreadable ledger yields [].
    """
    from core.data_model.entities import NewsArticle
    from core.data_model.pit import PointInTime, to_utc

    cutoff = to_utc(T)
    out: list = []
    try:
        path = Path(NEWS_EVIDENCE_LEDGER_PATH)
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict) or rec.get("event") != "article_observed":
                    continue
                observed = _parse_iso_ts(rec.get("observed_at"))
                if observed is None or to_utc(observed) > cutoff:
                    continue
                published = _parse_iso_ts(rec.get("published_at"))
                pit = PointInTime(
                    event_time=published,
                    received_at=observed,
                    available_from=observed,
                    fetched_at=observed,
                    version=0,
                )
                ticker = str(rec.get("ticker") or "").strip() or None
                out.append(NewsArticle(
                    id=str(rec.get("article_id") or ""),
                    title=str(rec.get("title") or ""),
                    url=str(rec.get("url") or ""),
                    source=str(rec.get("source") or ""),
                    published_at=published or observed,
                    ticker=ticker,
                    pit=pit,
                ))
    except FileNotFoundError:
        return []
    return out
