"""Cache-first SEC service with point-in-time filtering and atomic persistence."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from core.config import DATA_DIR
from core.data_model.entities import SECFiling
from core.data_model.pit import PointInTime, to_utc
from core.sec.edgar_adapter import EdgarToolsAdapter, SECProviderUnavailable


SEC_CACHE_PATH = DATA_DIR / "sec_filings_cache.json"  # configured data folder (MS_DATA_DIR)


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return to_utc(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return to_utc(parsed)


def _filing_to_dict(filing: SECFiling) -> Dict[str, Any]:
    return {
        "ticker": filing.ticker,
        "company_name": filing.company_name,
        "form": filing.form,
        "filed_at": filing.filed_at.isoformat(),
        "accession_no": filing.accession_no,
        "url": filing.url,
        "primary_document": filing.primary_document,
        "description": filing.description,
        "available_from": filing.pit.available_from.isoformat() if filing.pit and filing.pit.available_from else None,
    }


def _filing_from_dict(value: Dict[str, Any]) -> Optional[SECFiling]:
    filed_at = _parse_dt(value.get("filed_at"))
    available = _parse_dt(value.get("available_from"))
    if filed_at is None or not value.get("accession_no") or not value.get("url"):
        return None
    pit = PointInTime(
        event_time=filed_at,
        received_at=available,
        available_from=available,
        fetched_at=available,
        version=0,
    )
    return SECFiling(
        symbol=None,
        form=str(value.get("form") or ""),
        filed_at=filed_at,
        accession_no=str(value.get("accession_no") or ""),
        url=str(value.get("url") or ""),
        pit=pit,
        ticker=str(value.get("ticker") or "") or None,
        company_name=str(value.get("company_name") or "") or None,
        primary_document=str(value.get("primary_document") or "") or None,
        description=str(value.get("description") or "") or None,
    )


class SECFilingCache:
    def __init__(self, path: Path = SEC_CACHE_PATH, *, max_records_per_key: int = 500):
        self.path = Path(path)
        self.max_records_per_key = max(1, int(max_records_per_key))
        self._lock = threading.RLock()

    def _read(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {"version": 1, "entries": {}}
        return value if isinstance(value, dict) else {"version": 1, "entries": {}}

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = (self._read().get("entries") or {}).get(key)
            return dict(entry) if isinstance(entry, dict) else None

    def put(self, key: str, *, fetched_at: datetime, filings: Sequence[SECFiling]) -> None:
        with self._lock:
            payload = self._read()
            entries = payload.setdefault("entries", {})
            previous = entries.get(key) if isinstance(entries.get(key), dict) else {}
            previous_by_accession = {
                str(row.get("accession_no")): dict(row)
                for row in (previous.get("filings") or [])
                if isinstance(row, dict) and row.get("accession_no")
            }
            merged = dict(previous_by_accession)
            for filing in filings:
                row = _filing_to_dict(filing)
                accession = str(row.get("accession_no") or "")
                old = previous_by_accession.get(accession)
                old_available = _parse_dt((old or {}).get("available_from"))
                new_available = _parse_dt(row.get("available_from"))
                if old_available and (new_available is None or old_available < new_available):
                    row["available_from"] = old_available.isoformat()
                merged[accession] = row
            retained = sorted(
                merged.values(),
                key=lambda row: _parse_dt(row.get("filed_at")) or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )[: self.max_records_per_key]
            entries[key] = {
                "fetched_at": to_utc(fetched_at).isoformat(),
                "filings": retained,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
                os.replace(temp, self.path)
            finally:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass


class SECFilingService:
    def __init__(
        self,
        provider: EdgarToolsAdapter,
        cache: SECFilingCache,
        *,
        forms: Sequence[str],
        cache_ttl_seconds: int,
        max_filings: int,
        now=lambda: datetime.now(timezone.utc),
    ):
        self.provider = provider
        self.cache = cache
        self.forms = tuple(str(form).upper() for form in forms)
        self.cache_ttl_seconds = max(1, int(cache_ttl_seconds))
        self.max_filings = max(1, min(100, int(max_filings)))
        self._now = now

    def _key(self, ticker: str) -> str:
        return f"{ticker.upper()}|{','.join(self.forms)}|{self.max_filings}"

    def _cached_filings(self, entry: Optional[Dict[str, Any]], as_of: datetime) -> Tuple[SECFiling, ...]:
        if not entry:
            return ()
        filings = []
        for raw in entry.get("filings") or []:
            filing = _filing_from_dict(raw) if isinstance(raw, dict) else None
            if filing and filing.filed_at <= as_of and (not filing.pit or not filing.pit.available_from or filing.pit.available_from <= as_of):
                filings.append(filing)
        filings.sort(key=lambda filing: filing.filed_at, reverse=True)
        return tuple(filings[: self.max_filings])

    def get_context(self, ticker: str, *, as_of: Optional[datetime] = None, refresh: bool = False) -> Dict[str, Any]:
        normalized = str(ticker or "").strip().upper()
        explicit_as_of = as_of is not None
        cutoff = to_utc(as_of or self._now())
        key = self._key(normalized)
        cached = self.cache.get(key)
        fetched_at = _parse_dt((cached or {}).get("fetched_at"))
        if explicit_as_of:
            filings = self._cached_filings(cached, cutoff)
            return self._payload(
                normalized,
                cutoff,
                fetched_at,
                filings,
                "cache" if cached else "unavailable",
                "" if cached else "point-in-time SEC cache unavailable",
            )
        age = (cutoff - fetched_at).total_seconds() if fetched_at and cutoff >= fetched_at else None
        if not refresh and cached and age is not None and age <= self.cache_ttl_seconds:
            filings = self._cached_filings(cached, cutoff)
            return self._payload(normalized, cutoff, fetched_at, filings, "cache", "")
        try:
            filings = self.provider.fetch_filings(
                normalized, forms=self.forms, limit=self.max_filings, as_of=cutoff,
            )
            fetched_at = to_utc(self._now())
            cutoff = fetched_at
            filings = tuple(
                filing for filing in filings
                if filing.filed_at <= cutoff
                and (
                    not filing.pit
                    or not filing.pit.available_from
                    or filing.pit.available_from <= cutoff
                )
            )
            self.cache.put(key, fetched_at=fetched_at, filings=filings)
            return self._payload(normalized, cutoff, fetched_at, filings, "edgartools", "")
        except Exception as exc:
            filings = self._cached_filings(cached, cutoff)
            if filings:
                return self._payload(normalized, cutoff, fetched_at, filings, "stale_cache", str(exc))
            return self._payload(normalized, cutoff, fetched_at, (), "unavailable", str(exc))

    @staticmethod
    def _payload(
        ticker: str,
        as_of: datetime,
        fetched_at: Optional[datetime],
        filings: Sequence[SECFiling],
        source: str,
        error: str,
    ) -> Dict[str, Any]:
        return {
            "ticker": ticker,
            "as_of": as_of.isoformat(),
            "fetched_at": fetched_at.isoformat() if fetched_at else None,
            "status": "available" if filings else "unavailable",
            "source": source,
            "stale": source == "stale_cache",
            "error": error,
            "filings": [_filing_to_dict(filing) for filing in filings],
            "total_filings": len(filings),
        }


_SERVICE_LOCK = threading.RLock()
_SERVICE_CACHE: Dict[str, SECFilingService] = {}


def get_sec_filing_service(config: Optional[Dict[str, Any]] = None) -> SECFilingService:
    if config is None:
        from core.config import ConfigManager

        config = dict(getattr(ConfigManager(), "_config", {}) or {})
    settings = dict((config or {}).get("sec") or {})
    key = repr(sorted((name, repr(value)) for name, value in settings.items()))
    with _SERVICE_LOCK:
        service = _SERVICE_CACHE.get(key)
        if service is None:
            service = SECFilingService(
                EdgarToolsAdapter(identity_env=str(settings.get("identity_env") or "EDGAR_IDENTITY")),
                SECFilingCache(
                    Path(settings.get("cache_path") or SEC_CACHE_PATH),
                    max_records_per_key=int(settings.get("history_limit", 500)),
                ),
                forms=settings.get("forms") or (),
                cache_ttl_seconds=int(settings.get("cache_ttl_seconds", 3_600)),
                max_filings=int(settings.get("max_filings", 20)),
            )
            _SERVICE_CACHE[key] = service
        return service
