"""finnhub_client.py — thin shared adapter for the FinnHub API.

Why this exists (Runda-8 / 2026-07-07): FinnHub was called from 4 independent
sites (economic_calendar_agent, news_tab_agent, us_market_news_agent, engine),
each constructing its own URL f-string with `token=<key>` embedded inline. Two
bugs came from that sprawl:
  1. economic_calendar_agent read `self.config.get("finnhub_key")` (wrong key
     name) — silently disabled FinnHub entirely.
  2. us_market_news_agent:588 embedded the token in the URL passed to http_get,
     which logs the URL → API key leaked into logs.

This client centralizes URL construction + key resolution + the `token` param
(always in `params=`, never in the URL string → no log leak). All calls go
through `http_get(provider="finnhub")`, so the global 60/min sliding-window
budget in core/http_utils.py governs every call site from one place.

Scope guard (CLAUDE.md §8 — controlled elegance): this is a URL/key/params
adapter ONLY. It does NO caching, NO retry policy, NO rate-limit logic — those
stay in http_utils.py and the callers. Callers own their own caches.

Key resolution order (mirrors us_market_news_agent.py:168-170 + env fallback):
  1. config["news"]["finnhub_api_key"]
  2. config["finnhub_api_key"]
  3. env FINNHUB_API_KEY
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Union

from core.http_utils import http_get

logger = logging.getLogger("FinnhubClient")

_BASE = "https://finnhub.io/api/v1"
_DateLike = Union[str, date, datetime]


def _fmt(d: _DateLike) -> str:
    if isinstance(d, str):
        return d
    if isinstance(d, (datetime, date)):
        return d.strftime("%Y-%m-%d")
    return str(d)


class FinnhubClient:
    """Thin FinnHub adapter. Singleton via get_finnhub_client()."""

    def __init__(self, config: Optional[Dict] = None):
        self._explicit_config = config
        self._cached_key: Optional[str] = None
        self._key_resolved = False
        self._lock = threading.Lock()

    def set_config(self, config: Optional[Dict]) -> None:
        """Inject a config dict (agents that already hold one). Re-resolves key."""
        with self._lock:
            self._explicit_config = config
            self._cached_key = None
            self._key_resolved = False

    def _resolve_key(self) -> str:
        # Fast path: already resolved and non-empty.
        if self._key_resolved and self._cached_key:
            return self._cached_key
        cfg: Dict[str, Any] = {}
        if self._explicit_config:
            cfg = self._explicit_config
        else:
            try:
                from core.config import ConfigManager
                cfg = ConfigManager().config or {}
            except Exception as e:
                logger.debug(f"ConfigManager unavailable for finnhub key: {e}")
                cfg = {}
        key = (
            (cfg.get("news") or {}).get("finnhub_api_key", "")
            or cfg.get("finnhub_api_key", "")
            or os.getenv("FINNHUB_API_KEY", "")
        )
        key = (key or "").strip()
        with self._lock:
            self._cached_key = key
            self._key_resolved = True
        if key:
            logger.debug("FinnHub API key resolved")
        return key

    def is_available(self) -> bool:
        return bool(self._resolve_key())

    def key(self) -> str:
        """Resolved API key (empty string if none)."""
        return self._resolve_key()

    def _params(self, extra: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(extra)
        p["token"] = self._resolve_key()
        return p

    def _get(self, path: str, params: Dict[str, Any], *, timeout=(2, 6), max_retries: int = 1) -> Optional[Any]:
        if not self.is_available():
            return None
        url = f"{_BASE}{path}"
        resp = http_get(url, provider="finnhub", params=self._params(params), timeout=timeout, max_retries=max_retries)
        if resp is None:
            return None
        if resp.status_code != 200:
            logger.debug(f"FinnHub {path} -> HTTP {resp.status_code}")
            return None
        try:
            return resp.json()
        except Exception as e:
            logger.debug(f"FinnHub {path} JSON decode error: {e}")
            return None

    # ---- quote ----
    def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Real-time quote: {c, d, dp, h, l, o, pc, t}. None on failure/no key."""
        return self._get("/quote", {"symbol": symbol.upper()})

    # ---- news ----
    def get_company_news(self, symbol: str, from_date: _DateLike, to_date: _DateLike) -> Optional[List[Dict]]:
        """Per-symbol company news. Returns raw list of items."""
        return self._get(
            "/company-news",
            {"symbol": symbol.upper(), "from": _fmt(from_date), "to": _fmt(to_date)},
            timeout=(2, 8),
        )

    def get_market_news(self, category: str = "general") -> Optional[List[Dict]]:
        """Market-wide news (NOT used before Runda-8). category: general|crypto|forex."""
        return self._get("/news", {"category": category})

    # ---- calendar ----
    def get_economic_calendar(self, from_date: _DateLike, to_date: _DateLike) -> Optional[List[Dict]]:
        """Economic calendar. Returns the economicCalendar list."""
        data = self._get(
            "/calendar/economic",
            {"from": _fmt(from_date), "to": _fmt(to_date)},
            timeout=(2, 8),
        )
        if not isinstance(data, dict):
            return None
        return data.get("economicCalendar", [])

    def get_earnings_calendar(
        self,
        from_date: _DateLike,
        to_date: _DateLike,
        symbol: Optional[str] = None,
    ) -> Optional[List[Dict]]:
        """Earnings calendar. With symbol=None returns ALL US reporters in range.

        Returns the earningsCalendar list (raw items: date, symbol, epsActual,
        epsEstimate, revenueActual, revenueEstimate, hour). NO market cap.
        """
        params: Dict[str, Any] = {"from": _fmt(from_date), "to": _fmt(to_date)}
        if symbol:
            params["symbol"] = symbol.upper()
        data = self._get("/calendar/earnings", params, timeout=(3, 8))
        if not isinstance(data, dict):
            return None
        return data.get("earningsCalendar", [])

    def get_symbol_earnings(self, symbol: str) -> Optional[List[Dict]]:
        """Historical + upcoming earnings for one symbol (/stock/earnings)."""
        return self._get("/stock/earnings", {"symbol": symbol.upper()}, timeout=(3, 8))

    # ---- profile (market cap) ----
    def get_profile2(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Company profile2 — includes marketCapitalization (in millions)."""
        return self._get("/stock/profile2", {"symbol": symbol.upper()}, timeout=(2, 8))


# ---- singleton ----
_client: Optional[FinnhubClient] = None
_client_lock = threading.Lock()


def get_finnhub_client() -> FinnhubClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = FinnhubClient()
    return _client