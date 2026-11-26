"""Adapter from edgartools to V9's canonical SEC filing model."""
from __future__ import annotations

import os
import re
import threading
from datetime import date, datetime, time, timezone
from typing import Any, Callable, Iterable, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from core.data_model.entities import SECFiling
from core.data_model.pit import PointInTime, to_utc


_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,14}$")
_FORM_RE = re.compile(r"^[A-Z0-9][A-Z0-9/ -]{0,19}$")


class SECProviderUnavailable(RuntimeError):
    pass


def _text_attr(value: Any, *names: str) -> str:
    for name in names:
        raw = getattr(value, name, None)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return ""


def _date_value(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return to_utc(parsed)


def _sec_url(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "sec.gov" or host.endswith(".sec.gov")):
        return ""
    return raw


class EdgarToolsAdapter:
    """Read-only metadata adapter; document bodies are never fetched implicitly."""

    def __init__(
        self,
        *,
        identity_env: str = "EDGAR_IDENTITY",
        company_factory: Optional[Callable[[str], Any]] = None,
        set_identity: Optional[Callable[[str], Any]] = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.identity_env = str(identity_env or "EDGAR_IDENTITY").strip()
        self._company_factory = company_factory
        self._set_identity = set_identity
        self._now = now
        self._identity_ready = False
        self._lock = threading.RLock()

    def _runtime(self) -> Tuple[Callable[[str], Any], Callable[[str], Any]]:
        if self._company_factory is not None and self._set_identity is not None:
            return self._company_factory, self._set_identity
        try:
            from edgar import Company, set_identity
        except ImportError as exc:
            raise SECProviderUnavailable("edgartools is not installed") from exc
        return self._company_factory or Company, self._set_identity or set_identity

    def _ensure_identity(self) -> Callable[[str], Any]:
        company_factory, identity_setter = self._runtime()
        with self._lock:
            if not self._identity_ready:
                identity = os.environ.get(self.identity_env, "").strip()
                if not identity:
                    raise SECProviderUnavailable(f"SEC identity is not configured in {self.identity_env}")
                identity_setter(identity)
                self._identity_ready = True
        return company_factory

    @staticmethod
    def _iter_filings(filings: Any, limit: int) -> Iterable[Any]:
        selected = filings.head(limit) if hasattr(filings, "head") else filings
        try:
            return list(selected)[:limit]
        except TypeError as exc:
            raise SECProviderUnavailable("edgartools returned a non-iterable filing collection") from exc

    def fetch_filings(
        self,
        ticker: str,
        *,
        forms: Sequence[str],
        limit: int,
        as_of: datetime,
    ) -> Tuple[SECFiling, ...]:
        normalized_ticker = str(ticker or "").strip().upper()
        if not _TICKER_RE.fullmatch(normalized_ticker):
            raise ValueError("invalid US equity ticker")
        normalized_forms = tuple(dict.fromkeys(str(form).strip().upper() for form in forms))
        if not normalized_forms or any(not _FORM_RE.fullmatch(form) for form in normalized_forms):
            raise ValueError("invalid or empty SEC form selection")
        bounded_limit = max(1, min(100, int(limit)))
        cutoff = to_utc(as_of)
        company_factory = self._ensure_identity()
        company = company_factory(normalized_ticker)
        fetched_at = to_utc(self._now())
        try:
            filings = company.get_filings(form=list(normalized_forms))
        except TypeError:
            filings = company.get_filings()
        records = []
        for filing in self._iter_filings(filings, bounded_limit * 4):
            form = _text_attr(filing, "form").upper()
            if form not in normalized_forms:
                continue
            filed_at = _date_value(
                getattr(filing, "filing_date", None)
                or getattr(filing, "filed_at", None)
                or getattr(filing, "date_of_filing", None)
            )
            accession_no = _text_attr(filing, "accession_no", "accession_number")
            url = _sec_url(_text_attr(filing, "homepage_url", "url", "filing_url"))
            if filed_at is None or filed_at > cutoff or not accession_no or not url:
                continue
            pit = PointInTime(
                event_time=filed_at,
                received_at=fetched_at,
                available_from=fetched_at,
                fetched_at=fetched_at,
                version=0,
            )
            records.append(
                SECFiling(
                    symbol=None,
                    form=form,
                    filed_at=filed_at,
                    accession_no=accession_no,
                    url=url,
                    pit=pit,
                    ticker=normalized_ticker,
                    company_name=_text_attr(company, "name", "company_name"),
                    primary_document=_text_attr(filing, "primary_document", "document"),
                    description=_text_attr(filing, "description", "filing_description"),
                )
            )
        unique = {record.accession_no: record for record in records}
        return tuple(sorted(unique.values(), key=lambda item: item.filed_at, reverse=True)[:bounded_limit])
