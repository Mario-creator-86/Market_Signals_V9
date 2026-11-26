"""
Economic Calendar Agent - Specialized agent for US Market major economic events

Features:
- Track Fed interest rate decisions (dobanda)
- Monitor CPI, PPI, unemployment data
- Track earnings calendars for major stocks
- Real-time event notifications
- Market impact assessment
- Historical event data

This agent addresses the user's need to see all major US market news
like today's Fed rate decision (dobanda).
"""

import json
import logging
import math
import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from core.finnhub_client import get_finnhub_client
from core.agent_toggles import is_agent_enabled
from core.config import DATA_DIR
from core.http_utils import http_get
from core.observability import update_agent_status

logger = logging.getLogger("EconomicCalendarAgent")

# Global singleton instance
_calendar_agent_instance = None
_calendar_agent_lock = threading.Lock()

# Treat events within the last N days as still "upcoming" for display stability
UPCOMING_GRACE_DAYS = 7


def _is_upcoming(dt: Optional[datetime]) -> bool:
    if not dt:
        return False
    now = datetime.now().replace(tzinfo=None)
    return dt >= (now - timedelta(days=UPCOMING_GRACE_DAYS))


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        v = float(value)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def get_economic_calendar_agent(config: Optional[Dict] = None):
    """Factory function for singleton access"""
    global _calendar_agent_instance
    with _calendar_agent_lock:
        if _calendar_agent_instance is None:
            _calendar_agent_instance = EconomicCalendarAgent(config)
    return _calendar_agent_instance


def get_economic_calendar_agent_if_ready():
    """Return the singleton only when already initialized."""
    return _calendar_agent_instance


class EventImpact(Enum):
    """Impact level for economic events"""
    HIGH = "high"       # Fed rate, CPI, NFP - major market movers
    MEDIUM = "medium"   # PMI, housing data, etc.
    LOW = "low"         # Minor reports
    

class EventCategory(Enum):
    """Categories of economic events"""
    FED = "fed"                     # Federal Reserve decisions
    INFLATION = "inflation"         # CPI, PPI, PCE
    EMPLOYMENT = "employment"       # NFP, unemployment, jobless claims
    GDP = "gdp"                     # GDP reports
    HOUSING = "housing"             # Housing starts, sales
    CONSUMER = "consumer"           # Consumer confidence, retail sales
    MANUFACTURING = "manufacturing" # PMI, industrial production
    EARNINGS = "earnings"           # Company earnings reports
    OTHER = "other"


@dataclass
class EconomicEvent:
    """Single economic event with full metadata"""
    id: str
    title: str
    date: datetime
    category: EventCategory
    impact: EventImpact
    country: str = "US"
    actual: Optional[str] = None
    forecast: Optional[str] = None
    previous: Optional[str] = None
    description: str = ""
    source_url: str = ""
    source_name: str = ""
    time_precision: str = "exact"
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "title": self.title,
            "date": self.date.isoformat() if self.date else "",
            "date_formatted": (
                self.date.strftime("%Y-%m-%d")
                if self.date and self.time_precision == "date_only"
                else (self.date.strftime("%Y-%m-%d %H:%M") if self.date else "")
            ),
            "category": self.category.value,
            "impact": self.impact.value,
            "country": self.country,
            "actual": self.actual,
            "forecast": self.forecast,
            "previous": self.previous,
            "description": self.description,
            "source_url": self.source_url,
            "source_name": self.source_name,
            "time_precision": self.time_precision,
            "is_upcoming": _is_upcoming(self.date),
        }
    
    @property
    def impact_emoji(self) -> str:
        """Get emoji representation of impact level"""
        return {
            EventImpact.HIGH: "🔴",
            EventImpact.MEDIUM: "🟡",
            EventImpact.LOW: "🟢",
        }.get(self.impact, "⚪")


@dataclass
class EarningsEvent:
    """Earnings announcement event"""
    symbol: str
    date: datetime
    eps_estimated: Optional[float] = None
    eps_actual: Optional[float] = None
    revenue_estimated: Optional[float] = None
    revenue_actual: Optional[float] = None
    # "bmo" (before market open), "amc" (after market close) or "unknown".
    # The default is "unknown": a row built without a session used to be
    # shown as "Pre-Market" although no provider ever said so.
    when: str = "unknown"
    is_watchlist: bool = False
    source: str = "unknown"
    source_url: Optional[str] = None
    confirmed: bool = False
    verified_at: Optional[str] = None
    session_confidence: str = "unknown"
    fiscal_period: Optional[str] = None
    company_name: Optional[str] = None
    market_cap: Optional[float] = None
    eps_estimate_count: Optional[int] = None
    date_confidence: str = "unknown"
    
    def to_dict(self) -> Dict:
        return {
            "id": f"earn_{self.symbol}_{self.date.strftime('%Y%m%d')}",
            "title": f"{self.symbol} Earnings",
            "symbol": self.symbol,
            "date": self.date.isoformat(),
            "date_formatted": self.date.strftime("%Y-%m-%d"),
            "category": "earnings",
            "impact": "high",  # Earnings are generally high impact for the stock
            "eps_estimated": self.eps_estimated,
            "eps_actual": self.eps_actual,
            "revenue_estimated": self.revenue_estimated,
            "revenue_actual": self.revenue_actual,
            "when": self.when,
            "when_desc": (
                "Pre-Market" if self.when == "bmo"
                else ("After Hours" if self.when == "amc" else "Time Unconfirmed")
            ),
            "is_upcoming": _is_upcoming(self.date),
            "is_watchlist": self.is_watchlist,
            "source": self.source,
            "source_url": self.source_url,
            "confirmed": bool(self.confirmed),
            "verified_at": self.verified_at,
            "session_confidence": self.session_confidence,
            "fiscal_period": self.fiscal_period,
            "company_name": self.company_name,
            "market_cap": self.market_cap,
            "eps_estimate_count": self.eps_estimate_count,
            "date_confidence": self.date_confidence,
        }
@dataclass
class IPOEvent:
    """IPO listing event"""
    symbol: str
    company: str
    date: datetime
    price_range: Optional[str] = None  # "15-17"
    exchange: str = "NASDAQ"
    shares: Optional[int] = None
    
    def to_dict(self) -> Dict:
        return {
            "id": f"ipo_{self.symbol}_{self.date.strftime('%Y%m%d')}",
            "title": f"IPO: {self.company} ({self.symbol})",
            "symbol": self.symbol,
            "date": self.date.isoformat(),
            "date_formatted": self.date.strftime("%Y-%m-%d"),
            "category": "ipo",
            "impact": "medium",
            "company": self.company,
            "price_range": self.price_range,
            "exchange": self.exchange,
            "shares": self.shares,
            "is_upcoming": _is_upcoming(self.date),
        }

class EconomicCalendarAgent:
    """
    Economic Calendar Agent for tracking major US market events.
    
    Provides:
    - Fed rate decisions (dobanda)
    - Inflation data (CPI, PPI)
    - Employment reports (NFP, unemployment)
    - Earnings calendar for major stocks
    - Real-time event updates
    """

    # All-US-reporters bulk earnings fetch refresh interval (Faza 2). Earnings
    # dates barely move within an hour, so 1h TTL keeps the Calendar tab fast
    # while staying current.
    EARNINGS_BULK_TTL_SECONDS = 3600

    # Major recurring events with their typical schedule
    MAJOR_EVENTS = {
        "FOMC": {
            "title": "Fed Interest Rate Decision (FOMC)",
            "category": EventCategory.FED,
            "impact": EventImpact.HIGH,
            "description": "Federal Reserve interest rate decision and monetary policy statement",
            "frequency": "8 times per year",
        },
        "CPI": {
            "title": "Consumer Price Index (CPI)",
            "category": EventCategory.INFLATION,
            "impact": EventImpact.HIGH,
            "description": "Monthly inflation data measuring consumer prices",
            "frequency": "Monthly",
        },
        "PPI": {
            "title": "Producer Price Index (PPI)",
            "category": EventCategory.INFLATION,
            "impact": EventImpact.MEDIUM,
            "description": "Monthly inflation data measuring producer/wholesale prices",
            "frequency": "Monthly",
        },
        "NFP": {
            "title": "Non-Farm Payrolls",
            "category": EventCategory.EMPLOYMENT,
            "impact": EventImpact.HIGH,
            "description": "Monthly employment report showing job creation",
            "frequency": "Monthly (first Friday)",
        },
        "UNEMPLOYMENT": {
            "title": "Unemployment Rate",
            "category": EventCategory.EMPLOYMENT,
            "impact": EventImpact.HIGH,
            "description": "Monthly unemployment percentage",
            "frequency": "Monthly",
        },
        "GDP": {
            "title": "Gross Domestic Product (GDP)",
            "category": EventCategory.GDP,
            "impact": EventImpact.HIGH,
            "description": "Quarterly economic growth report",
            "frequency": "Quarterly",
        },
        "RETAIL_SALES": {
            "title": "Retail Sales",
            "category": EventCategory.CONSUMER,
            "impact": EventImpact.MEDIUM,
            "description": "Monthly retail sales data",
            "frequency": "Monthly",
        },
        "PMI": {
            "title": "ISM Manufacturing PMI",
            "category": EventCategory.MANUFACTURING,
            "impact": EventImpact.MEDIUM,
            "description": "Purchasing Managers Index for manufacturing",
            "frequency": "Monthly",
        },
        "JOBLESS_CLAIMS": {
            "title": "Initial Jobless Claims",
            "category": EventCategory.EMPLOYMENT,
            "impact": EventImpact.MEDIUM,
            "description": "Weekly unemployment insurance claims",
            "frequency": "Weekly (Thursday)",
        },
        "PCE": {
            "title": "PCE Price Index",
            "category": EventCategory.INFLATION,
            "impact": EventImpact.HIGH,
            "description": "Fed's preferred inflation measure",
            "frequency": "Monthly",
        },
    }
    
    # Official 2026 FOMC meeting dates.  We record the final meeting day with
    # date-only precision rather than inventing a decision timestamp; meeting
    # dates can change until confirmed at the preceding meeting.
    FOMC_SCHEDULE_SOURCE_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
    # 2027 rows re-read from the official page on 2026-11-15 (page "Last Update:
    # November 12, 2026"): the Fed now lists November 16–17 as the last 2027
    # meeting, no December meeting, and tags October 26–27 "Monetary Policy
    # Strategy Meeting". The earlier December 7–8, 2027 entry is no longer on
    # the page and was removed — a wrong date is worse than a missing one.
    FOMC_SCHEDULE_SOURCE_NAME = "Federal Reserve — FOMC meeting calendar (verified 2026-11-15)"
    FOMC_SCHEDULE = (
        (datetime(2026, 1, 28), "January 27–28, 2026"),
        (datetime(2026, 3, 18), "March 17–18, 2026"),
        (datetime(2026, 4, 29), "April 28–29, 2026"),
        (datetime(2026, 6, 17), "June 16–17, 2026"),
        (datetime(2026, 7, 29), "July 28–29, 2026"),
        (datetime(2026, 9, 16), "September 15–16, 2026"),
        (datetime(2026, 10, 28), "October 27–28, 2026"),
        (datetime(2026, 12, 9), "December 8–9, 2026"),
        (datetime(2027, 1, 27), "January 26–27, 2027"),
        (datetime(2027, 3, 17), "March 16–17, 2027"),
        (datetime(2027, 4, 28), "April 27–28, 2027"),
        (datetime(2027, 6, 9), "June 8–9, 2027"),
        (datetime(2027, 7, 28), "July 27–28, 2027"),
        (datetime(2027, 9, 15), "September 14–15, 2027"),
        (datetime(2027, 10, 27), "October 26–27, 2027 (listed by the Federal Reserve as \"Monetary Policy Strategy Meeting\")"),
        (datetime(2027, 11, 17), "November 16–17, 2027"),
    )
    FOMC_DATES = tuple(item[0] for item in FOMC_SCHEDULE)
    _FOMC_MEETING_LABELS = {item[0].date(): item[1] for item in FOMC_SCHEDULE}
    
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self._cache_lock = threading.Lock()
        self._cache_path = str(DATA_DIR / "calendar_events_cache.json")
        
        # Memory cache for events
        self._events_cache = {
            "economic": {}, # key: YYYY-MM-DD, value: List[Dict]
            "earnings": {}, # key: Symbol, value: List[Dict]
            "ipo": {}       # key: YYYY-MM-DD (month level maybe? or range)
        }
        
        self._last_fetch: datetime = datetime.min

        # All-US-reporters bulk fetch TTL + single-flight refresh guard (Faza 2,
        # 2026-07-07). The earnings cache had no TTL — it was only populated
        # on explicit refresh=true. Now the Calendar tab shows ALL US
        # reporters (not just the ~9 watchlist tickers): a bulk FinnHub
        # /calendar/earnings fetch (symbol omitted) fills the cache, refreshed
        # at most once per hour (earnings dates don't change faster). On stale
        # cache-only reads never schedule remote work. Explicit refresh callers
        # share a single-flight guard so concurrent tabs cannot duplicate the
        # same provider request.
        self._earnings_bulk_fetched_at: Optional[datetime] = None
        self._earnings_warm_lock = threading.Lock()
        self._earnings_warming = False
        self._earnings_warm_done = threading.Event()
        self._earnings_warm_done.set()
        self._nasdaq_last_fetch_succeeded = False
        # What the last bulk fetch really covered / whether it failed. The
        # Nasdaq fallback is bounded to ~35 days, so the Calendar must be able
        # to say "earnings listed through <date>" instead of showing the later
        # months as if nobody reports (tab audit 2026-11-15).
        self._nasdaq_last_window: Optional[tuple] = None
        self._nasdaq_last_day_counts: Optional[tuple] = None
        self._earnings_bulk_window: Optional[tuple] = None
        self._earnings_bulk_provider: Optional[str] = None
        self._earnings_bulk_last_attempt_at: Optional[datetime] = None
        self._earnings_bulk_last_error: Optional[str] = None
        # Optional API keys
        self.alpha_vantage_key = self.config.get("alpha_vantage_key", "")
        # FinnHub access goes through the shared client (core/finnhub_client.py)
        # which resolves the key from news.finnhub_api_key / env. Reading
        # self.config.get("finnhub_key") here was always "" (the agent is often
        # constructed without config) — that silently disabled FinnHub entirely.
        # is_available() re-resolves lazily, so a key added via config/env later
        # is still picked up.
        self._fh = get_finnhub_client()

        # Treasury cache
        self._treasury_cache: List[Dict] = []
        
        # Initial load from disk
        self._load_cache_from_disk()

        # Agent Fleet lifecycle.  Economic data is cache-first, but an enabled
        # agent still owns a real scheduled loop so ON has operational meaning.
        self._runtime_lock = threading.RLock()
        self._runtime_stop = threading.Event()
        self._runtime_wake = threading.Event()
        self._runtime_thread: Optional[threading.Thread] = None
        self._runtime_last_run: Optional[str] = None
        self._runtime_last_success: Optional[str] = None
        self._runtime_last_error: Optional[str] = None

    def _runtime_interval_seconds(self) -> int:
        agents = self.config.get("agents", {}) if isinstance(self.config, dict) else {}
        settings = agents.get("economic_calendar", {}) if isinstance(agents, dict) else {}
        try:
            return max(300, int((settings or {}).get("refresh_seconds", 3600) or 3600))
        except (TypeError, ValueError):
            return 3600

    def _calendar_settings(self) -> Dict[str, Any]:
        agents = self.config.get("agents", {}) if isinstance(self.config, dict) else {}
        settings = agents.get("economic_calendar", {}) if isinstance(agents, dict) else {}
        return settings if isinstance(settings, dict) else {}

    def _nasdaq_bulk_max_days(self) -> int:
        try:
            return max(7, min(62, int(self._calendar_settings().get("nasdaq_bulk_max_days", 35))))
        except (TypeError, ValueError):
            return 35

    def _earnings_warm_wait_seconds(self) -> float:
        try:
            return max(1.0, min(30.0, float(self._calendar_settings().get("earnings_warm_wait_seconds", 15))))
        except (TypeError, ValueError):
            return 15.0

    def _nasdaq_parallel_requests(self) -> int:
        try:
            return max(1, min(8, int(self._calendar_settings().get("nasdaq_parallel_requests", 6))))
        except (TypeError, ValueError):
            return 6

    @property
    def is_running(self) -> bool:
        thread = self._runtime_thread
        return bool(thread and thread.is_alive())

    def start(self) -> None:
        with self._runtime_lock:
            if self.is_running:
                return
            self._runtime_stop.clear()
            self._runtime_wake.clear()
            self._runtime_thread = threading.Thread(
                target=self._runtime_loop,
                daemon=True,
                name="EconomicCalendarAgent",
            )
            self._runtime_thread.start()
        logger.info("EconomicCalendarAgent started")

    def stop(self) -> None:
        self._runtime_stop.set()
        self._runtime_wake.set()
        thread = self._runtime_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        logger.info("EconomicCalendarAgent stopped")

    def get_runtime_status(self) -> Dict[str, Any]:
        with self._runtime_lock:
            return {
                "running": self.is_running,
                "last_run": self._runtime_last_run,
                "last_success": self._runtime_last_success,
                "last_error": self._runtime_last_error,
                "next_run_in": self._runtime_interval_seconds() if self.is_running else None,
            }

    def _runtime_loop(self) -> None:
        while not self._runtime_stop.is_set():
            started = datetime.now(timezone.utc).isoformat()
            with self._runtime_lock:
                self._runtime_last_run = started
            try:
                self.refresh_data(days_before=30, days_after=90)
                bulk_error = self._earnings_bulk_last_error
                if bulk_error:
                    # refresh_data() swallows provider failures; without this
                    # the Agents tab reported "last success" for a refresh
                    # that fetched nothing.
                    raise RuntimeError(f"earnings refresh failed: {bulk_error}")
                completed = datetime.now(timezone.utc).isoformat()
                with self._runtime_lock:
                    self._runtime_last_success = completed
                    self._runtime_last_error = None
                update_agent_status(
                    "economic_calendar",
                    enabled=True,
                    last_run=started,
                    last_success=completed,
                    last_error="",
                    next_run_in=self._runtime_interval_seconds(),
                )
            except Exception as exc:
                with self._runtime_lock:
                    self._runtime_last_error = str(exc)
                update_agent_status(
                    "economic_calendar",
                    enabled=True,
                    last_run=started,
                    last_error=str(exc),
                    next_run_in=self._runtime_interval_seconds(),
                )
                logger.error("Economic calendar scheduled refresh failed: %s", exc)

            self._runtime_wake.wait(timeout=self._runtime_interval_seconds())
            self._runtime_wake.clear()

    def get_today_events(self, allow_remote: bool = True) -> List[Dict]:
        """
        Get all economic events for today.
        
        Returns:
            List of today's economic events
        """
        if not is_agent_enabled("economic_calendar", default=True):
            allow_remote = False
        today = datetime.now().date()
        events = self._get_events_for_date_range(today, today, allow_remote=allow_remote)
        return [e.to_dict() for e in events]

    def get_state(self, days: int = 14) -> Dict[str, Any]:
        """
        Lightweight calendar state (cache-only).
        Avoids external calls to keep API responsive.
        """
        today = datetime.now().date()
        end_date = today + timedelta(days=days)

        def _parse_date(value: Any) -> Optional[datetime]:
            if not value:
                return None
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except Exception:
                try:
                    return datetime.fromisoformat(str(value)[:10])
                except Exception:
                    return None

        def _in_range(value: Any) -> bool:
            dt = _parse_date(value)
            if not dt:
                return False
            return today <= dt.date() <= end_date

        economic: List[Dict[str, Any]] = []
        earnings: List[Dict[str, Any]] = []
        ipo: List[Dict[str, Any]] = []
        cache_meta = {"economic_days": 0, "earnings_tickers": 0, "ipo_days": 0}

        with self._cache_lock:
            econ_cache = dict(self._events_cache.get("economic", {}) or {})
            earnings_cache = dict(self._events_cache.get("earnings", {}) or {})
            ipo_cache = dict(self._events_cache.get("ipo", {}) or {})

        for date_str, items in econ_cache.items():
            if _in_range(date_str):
                if isinstance(items, list):
                    economic.extend([i for i in items if isinstance(i, dict)])
        for items in earnings_cache.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and _in_range(item.get("date") or item.get("date_formatted")):
                    earnings.append(item)
        for date_str, items in ipo_cache.items():
            if _in_range(date_str):
                if isinstance(items, list):
                    ipo.extend([i for i in items if isinstance(i, dict)])

        cache_meta["economic_days"] = len(econ_cache)
        cache_meta["earnings_tickers"] = len(earnings_cache)
        cache_meta["ipo_days"] = len(ipo_cache)

        # Resilience: when the cache has no upcoming economic events for the
        # requested range (cold/stale cache), fetch fresh so the state endpoint
        # returns upcoming events instead of an empty 0-events view. One-time
        # cost; subsequent reads hit the now-warmed cache. Win-rate-neutral
        # (calendar is a context input, not a signal direction driver).
        cache_was_empty = not economic
        if cache_was_empty and is_agent_enabled("economic_calendar", default=True):
            try:
                fresh = self.get_upcoming_events(days=days, allow_remote=True) or {}
                economic = list(fresh.get("economic", []) or [])
                # get_upcoming_events also refreshes earnings/ipo from cache ranges
                # (which the remote economic fetch may have just touched). Re-read
                # earnings from the now-warmed cache so the state is consistent.
                earnings = list(fresh.get("earnings", []) or [])
                ipo = list(fresh.get("ipo", []) or [])
                with self._cache_lock:
                    cache_meta["economic_days"] = len(self._events_cache.get("economic", {}) or {})
                    cache_meta["earnings_tickers"] = len(self._events_cache.get("earnings", {}) or {})
                    cache_meta["ipo_days"] = len(self._events_cache.get("ipo", {}) or {})
            except Exception as e:
                logger.warning(f"get_state remote refresh failed: {e}")

        return {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "days": days,
            "economic": economic,
            "earnings": earnings,
            "ipo": ipo,
            "cache_meta": cache_meta,
            "cache_was_empty": bool(cache_was_empty),
        }
    
    def get_this_week_events(self) -> List[Dict]:
        """
        Get all economic events for the current week.
        
        Returns:
            List of this week's economic events
        """
        today = datetime.now().date()
        # Get start of week (Monday)
        start_of_week = today - timedelta(days=today.weekday())
        end_of_week = start_of_week + timedelta(days=6)
        
        events = self._get_events_for_date_range(start_of_week, end_of_week)
        return [e.to_dict() for e in events]
    
    def get_upcoming_events(self, days: int = 7, allow_remote: bool = True) -> List[Dict]:
        """
        Get upcoming economic events for the next N days.
        
        Args:
            days: Number of days to look ahead
            
        Returns:
            List of upcoming events
        """
        if not is_agent_enabled("economic_calendar", default=True):
            allow_remote = False
        today = datetime.now().date()
        end_date = today + timedelta(days=days)

        events = self._get_events_for_date_range(today, end_date, allow_remote=allow_remote)
        result = {
            "economic": [e.to_dict() for e in events],
            "earnings": [],
            "ipo": []
        }

        # Earnings: read ALL cached US reporters (no watchlist filter). Remote
        # work is permitted only when the caller explicitly sets allow_remote.
        if self._earnings_bulk_is_stale():
            self._populate_all_reporters(
                datetime.combine(today, datetime.min.time()),
                datetime.combine(end_date, datetime.min.time()),
                allow_remote=allow_remote,
            )
        result["earnings"] = self._get_cached_earnings_range(today, end_date)
        result["ipo"] = self._get_cached_ipo_range(today, end_date)

        return result

    def get_calendar_events_range(self, start_date: datetime, end_date: datetime, watchlist_tickers: List[str] = None, allow_remote: bool = True, include_all_reporters: bool = True) -> Dict[str, List[Dict]]:
        """
        Get all calendar events for a specific date range.
        Used for the full Calendar Page view.

        include_all_reporters=True (default): bulk-fetch ALL US earnings
        reporters via FinnHub /calendar/earnings (symbol omitted) and return
        every reporter in range — not just the watchlist. Cached for 1h.
        Cache-only requests never schedule hidden remote work. False: legacy
        watchlist-only view.
        """
        if not is_agent_enabled("economic_calendar", default=True):
            allow_remote = False

        result = {
            "economic": [],
            "earnings": [],
            "ipo": [],
            "treasury_rates": []
        }

        # 1. Economic (cache-first for responsiveness)
        eco_events = self._get_events_for_date_range(start_date, end_date, allow_remote=allow_remote)
        result["economic"] = [e.to_dict() for e in eco_events]

        # 2. Earnings
        if include_all_reporters:
            self._populate_all_reporters(start_date, end_date, allow_remote=allow_remote)
            # No watchlist filter — return every cached reporter in range.
            result["earnings"] = self._get_cached_earnings_range(start_date, end_date, watchlist_tickers=None)
        else:
            # Legacy watchlist-only path.
            if allow_remote and self._fh.is_available():
                try:
                    finnhub_earnings = self._fetch_finnhub_earnings_calendar(start_date, end_date, watchlist_tickers=watchlist_tickers)
                    if finnhub_earnings:
                        self._update_earnings_cache_from_events(finnhub_earnings, watchlist_tickers=watchlist_tickers)
                except Exception as e:
                    logger.warning(f"FinnHub earnings fetch failed: {e}")
            result["earnings"] = self._get_cached_earnings_range(start_date, end_date, watchlist_tickers=watchlist_tickers)

        # 3. IPO (cache only; FMP deprecated)
        result["ipo"] = self._get_cached_ipo_range(start_date, end_date)
        result["treasury_rates"] = list(self._treasury_cache or [])

        # Persist potentially new data
        if allow_remote:
            self._save_cache_to_disk()

        return result

    def _earnings_bulk_is_stale(self) -> bool:
        """True if the all-US-reporters bulk cache needs refreshing."""
        if self._earnings_bulk_fetched_at is None:
            return True
        try:
            age = (datetime.now() - self._earnings_bulk_fetched_at).total_seconds()
        except Exception:
            return True
        return age > self.EARNINGS_BULK_TTL_SECONDS

    def _populate_all_reporters(self, start_date, end_date, allow_remote: bool) -> None:
        """Bulk-fetch ALL US earnings reporters (FinnHub, symbol omitted) and
        merge into the earnings cache. 1h TTL. A stale cache is refreshed only
        for an explicit remote-enabled call; cache-only reads have no side
        effects. Concurrent refreshes are collapsed into one provider call.
        """
        stale = self._earnings_bulk_is_stale()
        if not stale:
            return  # fresh cache — nothing to do

        if not allow_remote:
            return

        # Explicit remote refresh: one owner performs the provider call while
        # concurrent callers wait briefly for that same result.
        with self._earnings_warm_lock:
            if self._earnings_warming:
                warm_done = self._earnings_warm_done
                is_owner = False
            else:
                self._earnings_warming = True
                self._earnings_warm_done.clear()
                warm_done = self._earnings_warm_done
                is_owner = True
        if not is_owner:
            warm_done.wait(timeout=self._earnings_warm_wait_seconds())
            return
        try:
            self._do_bulk_earnings_fetch(start_date, end_date)
        finally:
            with self._earnings_warm_lock:
                self._earnings_warming = False
                self._earnings_warm_done.set()

    def _do_bulk_earnings_fetch(self, start_date, end_date) -> None:
        """Fetch all-US reporters for the range and merge into cache."""
        self._earnings_bulk_last_attempt_at = datetime.now()
        try:
            provider = "finnhub"
            events: List[EarningsEvent] = []
            self._nasdaq_last_window = None
            self._nasdaq_last_day_counts = None
            if self._fh.is_available():
                events = self._fetch_finnhub_earnings_calendar(
                    start_date, end_date,
                    watchlist_tickers=None,
                    restrict_to_watchlist=False,
                )
            if not events:
                provider = "nasdaq"
                events = self._fetch_nasdaq_earnings_calendar(start_date, end_date)
            if events:
                self._update_earnings_cache_from_events(events, watchlist_tickers=None)
            if events or (provider == "nasdaq" and self._nasdaq_last_fetch_succeeded):
                self._earnings_bulk_fetched_at = datetime.now()
                self._earnings_bulk_provider = provider
                # The Nasdaq fallback only fetches a bounded sub-window of the
                # requested range — record (and log) what was really covered.
                window = self._nasdaq_last_window if provider == "nasdaq" else None
                if not window:
                    window = (
                        start_date.date() if isinstance(start_date, datetime) else start_date,
                        end_date.date() if isinstance(end_date, datetime) else end_date,
                    )
                self._earnings_bulk_window = (window[0].isoformat(), window[1].isoformat())
                counts = self._nasdaq_last_day_counts if provider == "nasdaq" else None
                if counts and counts[0] < counts[1]:
                    self._earnings_bulk_last_error = (
                        f"partial refresh: {counts[1] - counts[0]} of {counts[1]} days "
                        "could not be fetched from Nasdaq"
                    )
                else:
                    self._earnings_bulk_last_error = None
                with self._cache_lock:
                    self._events_cache["bulk_meta"] = {
                        "fetched_at": self._earnings_bulk_fetched_at.astimezone(timezone.utc).isoformat(),
                        "provider": provider,
                        "window_start": self._earnings_bulk_window[0],
                        "window_end": self._earnings_bulk_window[1],
                    }
                self._save_cache_to_disk(force=True)
                logger.info(
                    "Calendar: bulk-fetched %s earnings reporters via %s for %s..%s (requested %s..%s)",
                    len(events), provider, window[0], window[1], start_date.date(), end_date.date(),
                )
            else:
                self._earnings_bulk_last_error = (
                    "no earnings provider returned data (FinnHub not configured or empty, "
                    "Nasdaq calendar unreachable)"
                )
                logger.warning("Bulk earnings fetch returned nothing: %s", self._earnings_bulk_last_error)
        except Exception as e:
            self._earnings_bulk_last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"Bulk earnings fetch failed: {e}")

    def get_calendar_coverage(self) -> Dict[str, Any]:
        """What the calendar data really covers and how old it is.

        The Calendar tab uses this to state honestly: how old the earnings
        list is, through which date reporters are listed, whether the last
        refresh failed, and that no IPO feed exists — instead of rendering an
        uncovered month as "no events"."""
        now = datetime.now()
        fetched = self._earnings_bulk_fetched_at
        age_seconds: Optional[int] = None
        if fetched is not None:
            try:
                age_seconds = max(0, int((now - fetched).total_seconds()))
            except Exception:
                age_seconds = None
        stale_after = 2 * max(self.EARNINGS_BULK_TTL_SECONDS, self._runtime_interval_seconds())
        window = self._earnings_bulk_window
        attempt = self._earnings_bulk_last_attempt_at
        with self._cache_lock:
            ipo_cache = self._events_cache.get("ipo") or {}
            ipo_rows = sum(len(v) for v in ipo_cache.values() if isinstance(v, list)) if isinstance(ipo_cache, dict) else 0
        provider = self._earnings_bulk_provider
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "earnings": {
                "provider": provider,
                "dates_are_estimates": provider == "nasdaq",
                "fetched_at": fetched.astimezone(timezone.utc).isoformat() if fetched else None,
                "age_seconds": age_seconds,
                "stale": age_seconds is None or age_seconds > stale_after,
                "window_start": window[0] if window else None,
                "window_end": window[1] if window else None,
                "last_attempt_at": attempt.astimezone(timezone.utc).isoformat() if attempt else None,
                "last_error": self._earnings_bulk_last_error,
            },
            "ipo": {
                "available": ipo_rows > 0,
                "reason": "" if ipo_rows > 0 else "No IPO data provider is configured.",
            },
            "fomc": {
                "schedule_through": max(self.FOMC_DATES).date().isoformat(),
                "source_name": self.FOMC_SCHEDULE_SOURCE_NAME,
            },
        }
    def get_high_impact_events(self, days: int = 30, allow_remote: bool = True) -> List[Dict]:
        """
        Get only HIGH impact events for the next N days.
        
        Args:
            days: Number of days to look ahead
            
        Returns:
            List of high-impact events (Fed, CPI, NFP, etc.)
        """
        if not is_agent_enabled("economic_calendar", default=True):
            allow_remote = False
        today = datetime.now().date()
        end_date = today + timedelta(days=days)
        
        events = self._get_events_for_date_range(today, end_date, allow_remote=allow_remote)
        high_impact = [e for e in events if e.impact == EventImpact.HIGH]
        
        return [e.to_dict() for e in high_impact]
    
    def get_fed_schedule(self) -> List[Dict]:
        """
        Get all upcoming Fed (FOMC) meeting dates.
        
        Returns:
            List of FOMC meeting events
        """
        now = datetime.now()
        fed_events = []

        for meeting_date in self.FOMC_DATES:
            # Upcoming meetings + the single most recent past one (for "last decision"
            # context). 50 days covers at most one prior FOMC (~6-8 week cadence) and
            # stops a full year of stale past dates leaking into the calendar.
            if meeting_date.date() >= (now - timedelta(days=50)).date():
                event = self._fomc_event(meeting_date)
                fed_events.append(event.to_dict())
        
        return fed_events
    
    def get_next_fed_meeting(self) -> Optional[Dict]:
        """
        Get the next Fed meeting date.
        
        Returns:
            Next FOMC meeting details or None
        """
        now = datetime.now()
        
        for meeting_date in self.FOMC_DATES:
            if meeting_date.date() >= now.date():
                event = self._fomc_event(meeting_date)
                days_until = (meeting_date.date() - now.date()).days
                result = event.to_dict()
                result["days_until"] = days_until
                return result
        
        return None

    def _fomc_event(self, meeting_date: datetime) -> EconomicEvent:
        """Build a source-linked FOMC event without fabricating an exact time."""
        label = self._FOMC_MEETING_LABELS.get(meeting_date.date(), meeting_date.strftime("%Y-%m-%d"))
        return EconomicEvent(
            id=f"fomc_{meeting_date.strftime('%Y%m%d')}",
            title=self.MAJOR_EVENTS["FOMC"]["title"],
            date=meeting_date,
            category=EventCategory.FED,
            impact=EventImpact.HIGH,
            description=(
                f"Official FOMC meeting dates: {label}. Exact decision-release time is not "
                "inferred from the date-only schedule."
            ),
            source_url=self.FOMC_SCHEDULE_SOURCE_URL,
            source_name=self.FOMC_SCHEDULE_SOURCE_NAME,
            time_precision="date_only",
        )

    def _get_events_for_date_range(self, start_date, end_date, allow_remote: bool = True) -> List[EconomicEvent]:
        """Get events for a date range from multiple sources, using cache where possible."""
        events: List[EconomicEvent] = []
        
        # Normalize dates
        start_dt = start_date.date() if isinstance(start_date, datetime) else start_date
        end_dt = end_date.date() if isinstance(end_date, datetime) else end_date
        
        # 1. Try Memory Cache first
        missing_dates = []
        curr = start_dt
        while curr <= end_dt:
            date_str = curr.strftime("%Y-%m-%d")
            with self._cache_lock:
                day_events = self._events_cache["economic"].get(date_str)
            
            if day_events is not None:
                # Convert dict back to EconomicEvent
                for item in day_events:
                    try:
                        dt = datetime.fromisoformat(item["date"])
                        events.append(EconomicEvent(
                            id=item["id"],
                            title=item["title"],
                            date=dt,
                            category=EventCategory(item["category"]),
                            impact=EventImpact(item["impact"]),
                            country=item.get("country", "US"),
                            actual=item.get("actual"),
                            forecast=item.get("forecast"),
                            previous=item.get("previous"),
                            description=item.get("description", ""),
                            source_url=item.get("source_url", ""),
                            source_name=item.get("source_name", ""),
                            time_precision=item.get("time_precision", "exact"),
                        ))
                    except: continue
            else:
                missing_dates.append(curr)
            curr += timedelta(days=1)

        # 2. If missing significant dates or force refresh
        if missing_dates and allow_remote:
            api_events = self._fetch_from_apis(start_date, end_date)
            
            # Merge API results into memory cache
            new_cache_entries = {}
            for ev in api_events:
                ev_date_str = ev.date.strftime("%Y-%m-%d")
                if ev_date_str not in new_cache_entries:
                    new_cache_entries[ev_date_str] = []
                new_cache_entries[ev_date_str].append(ev.to_dict())
            
            with self._cache_lock:
                for d_str, ev_list in new_cache_entries.items():
                    self._events_cache["economic"][d_str] = ev_list
                # Also mark missing dates as empty if no events found to avoid re-fetch
                for d in missing_dates:
                    d_str = d.strftime("%Y-%m-%d")
                    if d_str not in self._events_cache["economic"]:
                        self._events_cache["economic"][d_str] = []

            # Return the full combined list
            # We already have some in 'events', lets Just add the ones from this fetch 
            # that were actually missing
            events.extend(api_events)
        
        # Add FOMC meetings (Built-in)
        for meeting_date in self.FOMC_DATES:
            if start_dt <= meeting_date.date() <= end_dt:
                # Check for duplicates by ID
                fomc_id = f"fomc_{meeting_date.strftime('%Y%m%d')}"
                if not any(e.id == fomc_id for e in events):
                    events.append(self._fomc_event(meeting_date))
        
        # Sort and de-duplicate by ID
        unique_events = {}
        for ev in events: unique_events[ev.id] = ev
        
        result = list(unique_events.values())
        result.sort(key=lambda e: e.date)
        return result

    def _get_cached_earnings_range(self, start_date, end_date, watchlist_tickers: Optional[List[str]] = None) -> List[Dict]:
        """Return cached earnings events for a date range (no remote fetch)."""
        s_date = start_date.date() if isinstance(start_date, datetime) else start_date
        e_date = end_date.date() if isinstance(end_date, datetime) else end_date

        allow = None
        if watchlist_tickers is not None:
            allow = {str(t).upper() for t in watchlist_tickers if t}

        results: List[Dict] = []
        deduped: Dict[tuple, Dict[str, Any]] = {}
        with self._cache_lock:
            earnings_cache = dict(self._events_cache.get("earnings", {}) or {})

        for sym, items in earnings_cache.items():
            sym_u = str(sym or "").upper()
            if allow is not None and sym_u not in allow:
                continue
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_key = self._earnings_cache_key(item)
                if not item_key:
                    continue
                dedup_key = (sym_u, item_key)
                prev = deduped.get(dedup_key)
                if prev is None:
                    deduped[dedup_key] = item
                else:
                    deduped[dedup_key] = self._select_better_earnings_row(prev, item)

        # Superseded estimates are judged per symbol across ALL its rows (the
        # replacing row may sit outside the requested range).
        by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        for (sym_u, _key), item in deduped.items():
            by_symbol.setdefault(sym_u, []).append(item)

        for sym_u, sym_rows in by_symbol.items():
            for item in self._drop_superseded_estimates(sym_rows):
                date_str = item.get("date") or item.get("date_formatted")
                if not date_str:
                    continue
                try:
                    dt = datetime.fromisoformat(str(date_str))
                except Exception:
                    try:
                        dt = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
                    except Exception:
                        continue
                d = dt.date()
                if s_date <= d <= e_date:
                    # Hand out a copy: callers annotate rows (is_watchlist) and
                    # must not write into the cache. is_upcoming was frozen at
                    # fetch time, so recompute it; rows written by other agents
                    # carry no title, which rendered as "undefined".
                    row = dict(item)
                    row["is_upcoming"] = _is_upcoming(dt.replace(tzinfo=None))
                    if not row.get("title"):
                        row["title"] = f"{sym_u} Earnings"
                    if not row.get("symbol"):
                        row["symbol"] = sym_u
                    results.append(row)

        results.sort(
            key=lambda row: self._parse_earnings_datetime(row.get("date") or row.get("date_formatted")) or datetime.min,
            reverse=True,
        )
        return results

    def _get_cached_ipo_range(self, start_date, end_date) -> List[Dict]:
        """Return cached IPO events for a date range (no remote fetch)."""
        s_date = start_date.date() if isinstance(start_date, datetime) else start_date
        e_date = end_date.date() if isinstance(end_date, datetime) else end_date

        results: List[Dict] = []
        with self._cache_lock:
            ipo_cache = dict(self._events_cache.get("ipo", {}) or {})

        curr = s_date
        while curr <= e_date:
            items = ipo_cache.get(curr.strftime("%Y-%m-%d"), []) or []
            for item in items:
                if isinstance(item, dict):
                    results.append(item)
            curr += timedelta(days=1)

        return results
    
    def _fetch_from_apis(
        self,
        start_date,
        end_date
    ) -> List[EconomicEvent]:
        """Fetch events from external APIs."""
        if not is_agent_enabled("economic_calendar", default=True):
            return []
        events = []

        # Try FinnHub if API key available
        if self._fh.is_available():
            finnhub_events = self._fetch_finnhub_calendar(start_date, end_date)
            events.extend(finnhub_events)

        # Try free investing.com alternative
        free_events = self._fetch_free_calendar(start_date, end_date)
        events.extend(free_events)

        return events
    
    def _fetch_finnhub_calendar(
        self,
        start_date,
        end_date
    ) -> List[EconomicEvent]:
        """Fetch economic calendar from FinnHub (via shared client)."""
        events = []

        if not self._fh.is_available():
            return events

        try:
            items = self._fh.get_economic_calendar(start_date, end_date)
            if not items:
                return events

            for item in items:
                try:
                    # Filter for US only
                    if item.get("country") != "US":
                        continue
                    
                    event_date = datetime.strptime(item.get("time", ""), "%Y-%m-%d %H:%M:%S")
                    impact = self._map_finnhub_impact(item.get("impact", ""))
                    category = self._categorize_event_title(item.get("event", ""))
                    
                    events.append(EconomicEvent(
                        id=f"finnhub_{item.get('id', '')}",
                        title=item.get("event", "Unknown Event"),
                        date=event_date,
                        category=category,
                        impact=impact,
                        actual=item.get("actual"),
                        forecast=item.get("estimate"),
                        previous=item.get("prev"),
                    ))
                except Exception as e:
                    logger.debug(f"Error parsing FinnHub event: {e}")
                    continue
                    
        except Exception as e:
            logger.error(f"Error fetching FinnHub calendar: {e}")

        return events

    def _fetch_finnhub_earnings_calendar(
        self,
        start_date,
        end_date,
        watchlist_tickers: Optional[List[str]] = None,
        restrict_to_watchlist: bool = True,
    ) -> List[EarningsEvent]:
        """Fetch earnings calendar from FinnHub (via shared client).

        With symbol omitted, FinnHub returns ALL US reporters in the range.
        restrict_to_watchlist=True (default, backward-compat) filters to the
        watchlist set; False returns every reporter — used by the Calendar tab's
        "show all US earnings" view (Part A, 2026-07-07).
        """
        events: List[EarningsEvent] = []
        if not self._fh.is_available():
            return events

        try:
            items = self._fh.get_earnings_calendar(start_date, end_date, symbol=None)
            if not items:
                return events

            wl_set = {t.upper() for t in watchlist_tickers} if watchlist_tickers else None
            for item in items:
                try:
                    sym = str(item.get("symbol") or "").upper().strip()
                    if not sym:
                        continue
                    # If watchlist restricted, skip non-watchlist symbols
                    if restrict_to_watchlist and wl_set is not None and sym not in wl_set:
                        continue

                    date_str = str(item.get("date") or "").strip()
                    if not date_str:
                        # A row without a date used to be stamped "today".
                        continue
                    event_date = datetime.strptime(date_str, "%Y-%m-%d")
                    when_raw = str(item.get("hour", "")).strip().lower()
                    if when_raw in ("bmo", "before", "before open", "before market open"):
                        when = "bmo"
                    elif when_raw in ("amc", "after", "after close", "after market close"):
                        when = "amc"
                    else:
                        when = "unknown"

                    events.append(EarningsEvent(
                        symbol=sym,
                        date=event_date,
                        eps_estimated=_safe_float(item.get("epsEstimate"), None),
                        eps_actual=_safe_float(item.get("epsActual"), None),
                        revenue_estimated=_safe_float(item.get("revenueEstimate"), None),
                        revenue_actual=_safe_float(item.get("revenueActual"), None),
                        when=when,
                        is_watchlist=wl_set is not None and sym in wl_set,
                        source="finnhub",
                        confirmed=bool(date_str),
                        verified_at=datetime.now(timezone.utc).isoformat(),
                        session_confidence="provider" if when != "unknown" else "unknown",
                        fiscal_period=str(item.get("quarter") or item.get("period") or "").strip() or None,
                    ))
                except Exception as e:
                    logger.debug(f"Error parsing FinnHub earnings event: {e}")
                    continue
        except Exception as e:
            logger.error(f"Error fetching FinnHub earnings calendar: {e}")

        return events

    def _fetch_nasdaq_earnings_calendar(
        self,
        start_date,
        end_date,
    ) -> List[EarningsEvent]:
        """Fetch the public Nasdaq/Zacks earnings slate one day at a time.

        This is the no-key bulk fallback when FinnHub is not configured or
        returns no rows. Nasdaq describes these dates as algorithmic estimates,
        so rows stay explicitly unconfirmed until a stronger source verifies
        them; they expand discovery without weakening the execution gates.
        """
        events: List[EarningsEvent] = []
        self._nasdaq_last_fetch_succeeded = False
        start_day = start_date.date() if isinstance(start_date, datetime) else start_date
        end_day = end_date.date() if isinstance(end_date, datetime) else end_date
        if start_day is None or end_day is None or end_day < start_day:
            return events

        max_days = self._nasdaq_bulk_max_days()
        total_days = (end_day - start_day).days + 1
        if total_days > max_days:
            today = datetime.now().date()
            if start_day <= today <= end_day:
                start_day = max(start_day, today - timedelta(days=1))
            end_day = min(end_day, start_day + timedelta(days=max_days - 1))
            logger.info(
                "Nasdaq earnings fallback bounded to %s days (%s..%s)",
                max_days, start_day, end_day,
            )

        def _nasdaq_number(value: Any) -> Optional[float]:
            text = str(value or "").strip()
            if not text or text.upper() in {"N/A", "NA", "NONE", "--"}:
                return None
            negative = text.startswith("(") and text.endswith(")")
            cleaned = text.strip("()").replace("$", "").replace(",", "").strip()
            parsed = _safe_float(cleaned, None)
            return -parsed if negative and parsed is not None else parsed

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; MarketSignals/9.0)",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/market-activity/earnings",
        }
        def _fetch_day(current_day):
            try:
                response = http_get(
                    "https://api.nasdaq.com/api/calendar/earnings",
                    provider="nasdaq_calendar",
                    params={"date": current_day.isoformat()},
                    headers=headers,
                    timeout=(3, 12),
                    max_retries=1,
                )
                if response is None or response.status_code != 200:
                    return current_day, None
                payload = response.json()
                rows = ((payload.get("data") or {}).get("rows") or []) if isinstance(payload, dict) else []
                return current_day, rows
            except Exception as exc:
                logger.warning("Nasdaq earnings day fetch failed for %s: %s", current_day, exc)
                return current_day, None

        days = []
        current_day = start_day
        while current_day <= end_day:
            days.append(current_day)
            current_day += timedelta(days=1)
        self._nasdaq_last_window = (start_day, end_day)

        successful_days = 0
        seen: set[tuple[str, str]] = set()
        with ThreadPoolExecutor(
            max_workers=self._nasdaq_parallel_requests(),
            thread_name_prefix="nasdaq-earnings",
        ) as pool:
            results = list(pool.map(_fetch_day, days))
        for current_day, rows in sorted(results, key=lambda value: value[0]):
            if rows is None:
                continue
            successful_days += 1
            for item in rows:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol") or "").upper().strip()
                if not symbol or (symbol, current_day.isoformat()) in seen:
                    continue
                seen.add((symbol, current_day.isoformat()))
                time_label = str(item.get("time") or "").lower().strip()
                when = (
                    "bmo" if time_label == "time-pre-market"
                    else ("amc" if time_label == "time-after-hours" else "unknown")
                )
                events.append(EarningsEvent(
                    symbol=symbol,
                    date=datetime.combine(current_day, datetime.min.time()),
                    eps_estimated=_nasdaq_number(item.get("epsForecast")),
                    when=when,
                    source="nasdaq_zacks_estimate",
                    source_url="https://www.nasdaq.com/market-activity/earnings",
                    confirmed=False,
                    verified_at=datetime.now(timezone.utc).isoformat(),
                    session_confidence="provider" if when != "unknown" else "unknown",
                    fiscal_period=str(item.get("fiscalQuarterEnding") or "").strip() or None,
                    company_name=str(item.get("name") or "").strip() or None,
                    market_cap=_nasdaq_number(item.get("marketCap")),
                    eps_estimate_count=_safe_int(item.get("noOfEsts"), 0) or None,
                    date_confidence="estimated",
                ))

        self._nasdaq_last_fetch_succeeded = successful_days > 0
        self._nasdaq_last_day_counts = (successful_days, len(days))
        return events

    def _fetch_finnhub_symbol_earnings(self, symbol: str) -> List[EarningsEvent]:
        """Fetch historical + upcoming earnings for a single symbol from FinnHub."""
        events: List[EarningsEvent] = []
        if not self._fh.is_available():
            return events

        try:
            data = self._fh.get_symbol_earnings(symbol)
            if not data:
                return events
            for item in data:
                try:
                    period = str(item.get("period", "")).strip()
                    if not period:
                        continue
                    event_date = datetime.strptime(period, "%Y-%m-%d")
                    eps_est = _safe_float(item.get("epsEstimate"), None)
                    eps_act = _safe_float(item.get("epsActual"), None)
                    rev_est = _safe_float(item.get("revenueEstimate"), None)
                    rev_act = _safe_float(item.get("revenueActual"), None)
                    # Infer session from whether actuals are present
                    when = "amc" if eps_act is not None else "bmo"

                    events.append(EarningsEvent(
                        symbol=symbol.upper(),
                        date=event_date,
                        eps_estimated=eps_est,
                        eps_actual=eps_act,
                        revenue_estimated=rev_est,
                        revenue_actual=rev_act,
                        when=when,
                        is_watchlist=True,
                        source="finnhub_symbol_earnings",
                        confirmed=True,
                        verified_at=datetime.now(timezone.utc).isoformat(),
                        session_confidence="inferred" if eps_act is not None else "unknown",
                        fiscal_period=period,
                    ))
                except Exception as e:
                    logger.debug(f"Error parsing FinnHub symbol earnings: {e}")
                    continue
        except Exception as e:
            logger.error(f"Error fetching FinnHub symbol earnings for {symbol}: {e}")

        return events

    def _fetch_fmp_economic_calendar(
        self,
        start_date,
        end_date
    ) -> List[EconomicEvent]:
        """Stub: FMP API is deprecated."""
        return []
    
    def _fetch_treasury_rates(self) -> List[Dict]:
        """Stub: FMP API is deprecated; returns cached rates if any."""
        return list(self._treasury_cache or [])
    
    def _detect_event_category(self, event_name: str) -> EventCategory:
        """
        Detect event category from event name using keyword matching.
        
        Args:
            event_name: The name of the economic event
            
        Returns:
            EventCategory enum value
        """
        event_lower = event_name.lower()
        
        # Fed/FOMC events
        if any(keyword in event_lower for keyword in ["fomc", "fed", "federal reserve", "interest rate decision", "powell speech"]):
            return EventCategory.FED
        
        # Inflation indicators
        if any(keyword in event_lower for keyword in ["cpi", "consumer price", "ppi", "producer price", "pce", "inflation"]):
            return EventCategory.INFLATION
        
        # Employment indicators
        if any(keyword in event_lower for keyword in ["nfp", "nonfarm", "payroll", "unemployment", "jobless", "claims", "employment", "adp"]):
            return EventCategory.EMPLOYMENT
        
        # GDP
        if any(keyword in event_lower for keyword in ["gdp", "gross domestic"]):
            return EventCategory.GDP
        
        # Housing
        if any(keyword in event_lower for keyword in ["housing", "building permits", "home sales", "mortgage"]):
            return EventCategory.HOUSING
        
        # Consumer
        if any(keyword in event_lower for keyword in ["retail sales", "consumer confidence", "consumer spending", "personal income"]):
            return EventCategory.CONSUMER
        
        # Manufacturing
        if any(keyword in event_lower for keyword in ["pmi", "ism", "manufacturing", "industrial production", "factory"]):
            return EventCategory.MANUFACTURING
        
        # Default
        return EventCategory.OTHER


    def refresh_data(self, days_before: int = 30, days_after: int = 90):
        """
        Proactively refresh all calendar data.
        Triggered on server start and watchlist changes.
        """
        if not is_agent_enabled("economic_calendar", default=True):
            logger.info("Economic calendar flow disabled; skipping proactive refresh.")
            return
        now = datetime.now()
        start_date = now - timedelta(days=days_before)
        end_date = now + timedelta(days=days_after)

        logger.info(f"🔄 Proactive calendar refresh: {start_date.date()} to {end_date.date()}")

        # Economic & IPO Range (cache-only; FMP deprecated)
        self.get_calendar_events_range(start_date, end_date)

        logger.info("✅ Calendar proactive refresh complete")


    def _load_cache_from_disk(self):
        """Unified load from disk."""
        primary_path = self._cache_path
        backup_path = f"{self._cache_path}.bak"

        def _read_cache(path: str) -> Optional[Dict[str, Any]]:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                raise ValueError("calendar cache payload is not an object")
            return payload

        data = None
        source = None

        if os.path.exists(primary_path):
            try:
                data = _read_cache(primary_path)
                source = "primary"
            except Exception as e:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                corrupt_path = f"{primary_path}.corrupt.{ts}.json"
                try:
                    shutil.move(primary_path, corrupt_path)
                    logger.warning(f"Corrupt calendar cache moved to {corrupt_path}: {e}")
                except Exception:
                    logger.error(f"Error loading calendar cache: {e}")

        if data is None and os.path.exists(backup_path):
            try:
                data = _read_cache(backup_path)
                source = "backup"
            except Exception as e:
                logger.error(f"Error loading calendar backup cache: {e}")

        if data is None:
            return

        with self._cache_lock:
            if isinstance(data.get("economic"), dict):
                self._events_cache["economic"] = data["economic"]
            if isinstance(data.get("earnings"), dict):
                self._events_cache["earnings"] = data["earnings"]
            if isinstance(data.get("ipo"), dict):
                self._events_cache["ipo"] = data["ipo"]
            if isinstance(data.get("earnings_meta"), dict):
                self._events_cache["earnings_meta"] = data["earnings_meta"]
            if isinstance(data.get("bulk_meta"), dict):
                self._events_cache["bulk_meta"] = data["bulk_meta"]

        # Restore when/what the last bulk earnings fetch covered so the
        # Calendar can show the real data age right after a restart.
        bulk_meta = data.get("bulk_meta")
        if isinstance(bulk_meta, dict):
            try:
                fetched = datetime.fromisoformat(str(bulk_meta.get("fetched_at")))
                if fetched.tzinfo is not None:
                    fetched = fetched.astimezone().replace(tzinfo=None)
                self._earnings_bulk_fetched_at = fetched
                self._earnings_bulk_provider = str(bulk_meta.get("provider") or "") or None
                if bulk_meta.get("window_start") and bulk_meta.get("window_end"):
                    self._earnings_bulk_window = (
                        str(bulk_meta["window_start"]), str(bulk_meta["window_end"]),
                    )
            except Exception:
                pass

        if source == "backup":
            logger.info("?? Calendar cache restored from backup")
        else:
            logger.info("?? Calendar cache loaded from disk")

    def _save_cache_to_disk(self, force: bool = False):
        """Unified save to disk using atomic replace to avoid partial writes.

        Throttled: the cache is >10 MB, so writing it on every API request keeps
        a long hard-kill window open (the root cause of leaked *.tmp files).
        """
        now_ts = time.time()
        if not force and (now_ts - getattr(self, "_last_disk_save_ts", 0.0)) < 60.0:
            return
        self._last_disk_save_ts = now_ts

        target_path = self._cache_path
        target_dir = os.path.dirname(os.path.abspath(target_path)) or "."
        tmp_path = None
        try:
            self._prune_events_cache()
        except Exception as e:
            logger.warning(f"Calendar cache prune failed: {e}")
        try:
            with self._cache_lock:
                snapshot = dict(self._events_cache)

            fd, tmp_path = tempfile.mkstemp(prefix="calendar_cache_", suffix=".tmp", dir=target_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                # Compact JSON: indent=2 triples the size of a 12 MB file
                json.dump(snapshot, f, separators=(",", ":"), ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())

            if os.path.exists(target_path):
                try:
                    shutil.copy2(target_path, f"{target_path}.bak")
                except Exception:
                    pass

            os.replace(tmp_path, target_path)
            tmp_path = None
            # logger.debug("?? Calendar cache saved to disk")
        except Exception as e:
            logger.error(f"Error saving calendar cache: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _prune_events_cache(self) -> None:
        """Bound the on-disk cache — it previously grew without limit (12+ MB).

        Economic/IPO day-buckets older than 90 days are dropped. Earnings rows are
        kept for ~10 quarters because Pre-Earnings needs >=6 quarters of history.
        """
        day_cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        earnings_cutoff = datetime.now() - timedelta(days=900)
        with self._cache_lock:
            for section in ("economic", "ipo"):
                cache = self._events_cache.get(section)
                if isinstance(cache, dict):
                    stale = [k for k in cache if isinstance(k, str) and k < day_cutoff]
                    for k in stale:
                        del cache[k]
            earnings = self._events_cache.get("earnings")
            if isinstance(earnings, dict):
                meta = self._events_cache.get("earnings_meta")
                for sym, rows in list(earnings.items()):
                    if not isinstance(rows, list):
                        continue
                    kept = []
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        dt = self._parse_earnings_datetime(row.get("date") or row.get("date_formatted"))
                        if dt is not None and dt.tzinfo is not None:
                            dt = dt.replace(tzinfo=None)
                        if dt is None or dt >= earnings_cutoff:
                            kept.append(row)
                    if kept:
                        earnings[sym] = kept
                    else:
                        del earnings[sym]
                        if isinstance(meta, dict):
                            meta.pop(sym, None)

    def _update_earnings_cache_from_events(self, events: List["EarningsEvent"], watchlist_tickers: Optional[List[str]] = None) -> None:
        """
        Merge earnings events into the per-ticker cache so get_earnings_for_ticker stays in sync.
        Optionally restrict updates to a watchlist ticker set.
        """
        if not events:
            return

        allow = None
        if watchlist_tickers is not None:
            allow = {str(t).upper() for t in watchlist_tickers if t}

        now_ts = datetime.now().timestamp()
        with self._cache_lock:
            if "earnings_meta" not in self._events_cache:
                self._events_cache["earnings_meta"] = {}
            if "earnings" not in self._events_cache:
                self._events_cache["earnings"] = {}

            for ev in events:
                try:
                    sym = (ev.symbol or "").upper()
                except Exception:
                    sym = ""
                if not sym:
                    continue
                if allow is not None and sym not in allow:
                    continue

                existing = self._events_cache["earnings"].get(sym, [])
                merged: Dict[str, Dict[str, Any]] = {}
                if isinstance(existing, list):
                    for item in existing:
                        if not isinstance(item, dict):
                            continue
                        item_key = self._earnings_cache_key(item)
                        if not item_key:
                            continue
                        prev = merged.get(item_key)
                        if prev is None:
                            merged[item_key] = item
                        else:
                            merged[item_key] = self._select_better_earnings_row(prev, item)

                ev_dict = ev.to_dict() if hasattr(ev, "to_dict") else None
                if isinstance(ev_dict, dict):
                    item_key = self._earnings_cache_key(ev_dict)
                    if item_key:
                        prev = merged.get(item_key)
                        if prev is None:
                            merged[item_key] = ev_dict
                        else:
                            merged[item_key] = self._select_better_earnings_row(prev, ev_dict)

                if merged:
                    merged_items = self._drop_superseded_estimates(list(merged.values()))
                    merged_items.sort(
                        key=lambda row: self._parse_earnings_datetime(row.get("date") or row.get("date_formatted")) or datetime.min,
                        reverse=True,
                    )
                    self._events_cache["earnings"][sym] = merged_items
                    self._events_cache["earnings_meta"][sym] = now_ts

    @staticmethod
    def _parse_earnings_datetime(value: Any) -> Optional[datetime]:
        if not value:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            try:
                dt = datetime.strptime(text[:10], "%Y-%m-%d")
            except Exception:
                return None
        if dt.tzinfo is not None:
            try:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                dt = dt.replace(tzinfo=None)
        return dt

    @classmethod
    def _earnings_cache_key(cls, row: Dict[str, Any]) -> str:
        if not isinstance(row, dict):
            return ""
        item_id = str(row.get("id") or "").strip()
        if item_id:
            return f"id:{item_id}"
        dt = cls._parse_earnings_datetime(row.get("date") or row.get("date_formatted"))
        if dt:
            symbol = str(row.get("symbol") or "").upper().strip()
            if symbol:
                return f"day:{symbol}:{dt.date().isoformat()}"
            return f"day:{dt.date().isoformat()}"
        return ""

    @staticmethod
    def _earnings_row_quality(row: Dict[str, Any]) -> int:
        if not isinstance(row, dict):
            return 0
        score = 0
        if row.get("eps_actual") is not None:
            score += 4
        if row.get("revenue_actual") is not None:
            score += 4
        if row.get("eps_estimated") is not None:
            score += 1
        if row.get("revenue_estimated") is not None:
            score += 1
        if row.get("is_watchlist"):
            score += 1
        source = str(row.get("source") or "").lower()
        if "investor" in source or "official" in source:
            score += 20
        elif source in {"sec", "sec_filing"}:
            score += 18
        elif "yfinance" in source:
            score += 12
        elif "finnhub" in source:
            score += 10
        elif "nasdaq" in source:
            score += 6
        if row.get("confirmed"):
            score += 5
        if str(row.get("session_confidence") or "").lower() in {"official", "provider"}:
            score += 2
        return score

    @classmethod
    def _drop_superseded_estimates(cls, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Drop unconfirmed estimate rows of ONE symbol that a newer row for
        the same quarter has replaced.

        Provider estimates move (ZS: 24 Nov on the 22 Oct fetch, 2 Dec on the
        12 Nov fetch). Rows are keyed by symbol+date, so the old estimate was
        never removed and the Calendar listed the same report twice — 393
        symbols on 2026-11-15. An estimate is superseded when another row for
        the same fiscal period (or, without a period, within 45 days) sits on
        a different date and is either confirmed/reported or was verified more
        recently. Confirmed and reported rows are never dropped."""
        if not isinstance(rows, list) or len(rows) < 2:
            return rows

        def _is_estimate(row: Dict[str, Any]) -> bool:
            return (
                not row.get("confirmed")
                and row.get("eps_actual") is None
                and row.get("revenue_actual") is None
            )

        parsed = []
        for row in rows:
            if not isinstance(row, dict):
                parsed.append((None, "", ""))
                continue
            dt = cls._parse_earnings_datetime(row.get("date") or row.get("date_formatted"))
            parsed.append((
                dt,
                str(row.get("fiscal_period") or "").strip().lower(),
                str(row.get("verified_at") or ""),
            ))

        dropped = set()
        for i, row in enumerate(rows):
            dt_i, period_i, verified_i = parsed[i]
            if dt_i is None or not isinstance(row, dict) or not _is_estimate(row):
                continue
            for j, other in enumerate(rows):
                dt_j, period_j, verified_j = parsed[j]
                if i == j or dt_j is None or dt_j.date() == dt_i.date():
                    continue
                if period_i and period_j:
                    same_quarter = period_i == period_j
                else:
                    same_quarter = abs((dt_j - dt_i).days) <= 45
                if not same_quarter:
                    continue
                if not _is_estimate(other) or verified_j > verified_i:
                    dropped.add(i)
                    break
        if not dropped:
            return rows
        return [row for index, row in enumerate(rows) if index not in dropped]

    @classmethod
    def _select_better_earnings_row(cls, left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
        left_quality = cls._earnings_row_quality(left)
        right_quality = cls._earnings_row_quality(right)
        if right_quality > left_quality:
            return right
        if right_quality < left_quality:
            return left
        left_dt = cls._parse_earnings_datetime(left.get("date") or left.get("date_formatted"))
        right_dt = cls._parse_earnings_datetime(right.get("date") or right.get("date_formatted"))
        if right_dt and (not left_dt or right_dt > left_dt):
            return right
        return left

    def invalidate_earnings_cache(self, ticker: str = None):
        """
        Invalidate cache for a specific ticker or entirely.
        """
        with self._cache_lock:
            if ticker:
                if ticker in self._events_cache["earnings"]:
                    del self._events_cache["earnings"][ticker]
            else:
                self._events_cache["earnings"] = {}
        self._save_cache_to_disk(force=True)

    def replace_future_earnings_for_ticker(
        self,
        ticker: str,
        rows: List[Dict[str, Any]],
    ) -> None:
        """Replace conflicting future dates with a targeted canonical refresh.

        Historical rows are retained because Pre-Earnings uses them for EPS beat
        and post-report move statistics. Only future rows are replaced, avoiding
        the stale-date accumulation that previously left symbols such as GCT with
        three mutually exclusive upcoming report dates.
        """
        symbol = str(ticker or "").upper().strip()
        if not symbol:
            return
        today = datetime.now().date()
        incoming = []
        for raw in rows or []:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("symbol") or symbol).upper().strip() != symbol:
                continue
            row = dict(raw)
            row["symbol"] = symbol
            dt = self._parse_earnings_datetime(row.get("date") or row.get("date_formatted"))
            if dt is None or dt.date() < today:
                continue
            incoming.append(row)
        if not incoming:
            return

        with self._cache_lock:
            earnings = self._events_cache.setdefault("earnings", {})
            existing = earnings.get(symbol, [])
            historical = []
            for row in existing if isinstance(existing, list) else []:
                if not isinstance(row, dict):
                    continue
                dt = self._parse_earnings_datetime(row.get("date") or row.get("date_formatted"))
                if dt is not None and dt.date() < today:
                    historical.append(row)

            merged: Dict[str, Dict[str, Any]] = {}
            for row in historical + incoming:
                key = self._earnings_cache_key(row)
                if not key:
                    continue
                previous = merged.get(key)
                merged[key] = row if previous is None else self._select_better_earnings_row(previous, row)
            ordered = list(merged.values())
            ordered.sort(
                key=lambda row: self._parse_earnings_datetime(
                    row.get("date") or row.get("date_formatted")
                ) or datetime.min,
                reverse=True,
            )
            earnings[symbol] = ordered
            self._events_cache.setdefault("earnings_meta", {})[symbol] = datetime.now().timestamp()
        self._save_cache_to_disk(force=True)


    def get_earnings_for_ticker(self, ticker: str) -> Optional[Dict]:
        """
        Get the next/most recent earnings event for a specific ticker from cache.
        """
        ticker = ticker.upper()
        
        # 1. Check Specific Earnings Cache first (if populated by watchlist fetch)
        with self._cache_lock:
            if ticker in self._events_cache["earnings"]:
                # Return the future-most event or the most recent one
                events = self._events_cache["earnings"][ticker]
                if not events: return None
                
                # Sort by date descending
                sorted_events = sorted(events, key=lambda x: x.get("date", ""), reverse=True)
                
                # Find first future event
                now_str = datetime.now().strftime("%Y-%m-%d")
                
                # Strategy: If we have future events, return the closest one.
                # If all are past, return the most recent past one.
                future_events = [e for e in sorted_events if e.get("date", "") >= now_str]
                if future_events:
                    # Closest future event is the last in a reversed sort (descending) -> smallest date >= now?
                    # No, sorted descending means [2026-12, 2026-06, 2026-01]. We want 2026-01.
                    # So it's the last element of future_events.
                    return future_events[-1]
                
                # If no future event, return latest past
                return sorted_events[0]

        return None

    def try_get_earnings_for_ticker(self, ticker: str) -> Optional[Dict]:
        """Return cached evidence immediately, or ``None`` while a writer owns the cache.

        Lean UI polls are cache-only and must not wait behind a large calendar
        merge. Missing this one read for a poll is safer than stalling the full
        application state; the next poll sees the committed snapshot.
        """
        acquired = self._cache_lock.acquire(blocking=False)
        if not acquired:
            return None
        try:
            symbol = ticker.upper()
            events = self._events_cache["earnings"].get(symbol, [])
            if not events:
                return None
            sorted_events = sorted(events, key=lambda item: item.get("date", ""), reverse=True)
            now_str = datetime.now().strftime("%Y-%m-%d")
            future_events = [item for item in sorted_events if item.get("date", "") >= now_str]
            return future_events[-1] if future_events else sorted_events[0]
        finally:
            self._cache_lock.release()

    def _map_finnhub_impact(self, finnhub_impact: str) -> EventImpact:
        """Map FinnHub impact to EventImpact enum."""
        impact_lower = finnhub_impact.lower() if finnhub_impact else "medium"
        
        if impact_lower in ["3", "high"]:
            return EventImpact.HIGH
        elif impact_lower in ["1", "low"]:
            return EventImpact.LOW
        else:
            return EventImpact.MEDIUM
    
    def _categorize_event_title(self, event_title: str) -> EventCategory:
        """Categorize event from title - alias for _detect_event_category."""
        return self._detect_event_category(event_title)
    
    def _fetch_free_calendar(self, start_date, end_date) -> List[EconomicEvent]:
        """Fetch from free sources - stub for now."""
        return []
    
    def _generate_estimated_events(self, start_date, end_date) -> List[EconomicEvent]:
        """Generate estimated events - stub for now."""
        return []

    def _get_watchlist_tickers(self) -> List[str]:
        """Get watchlist tickers from config or watchlist.json."""
        tickers: List[str] = []

        try:
            if isinstance(self.config, dict):
                watchlist = self.config.get("watchlist") or {}
                for key in ("yahoo", "stocks"):
                    tickers.extend(watchlist.get(key, []) or [])
                for key in ("binance", "crypto"):
                    tickers.extend(watchlist.get(key, []) or [])
        except Exception:
            pass

        if not tickers:
            try:
                from core.config import WATCHLIST_PATH, FALLBACK_STOCKS, FALLBACK_CRYPTOS
                if WATCHLIST_PATH.exists():
                    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        tickers.extend(data.get("stocks", []) or [])
                        tickers.extend(data.get("crypto", []) or [])
                if not tickers:
                    tickers.extend(FALLBACK_STOCKS + FALLBACK_CRYPTOS)
            except Exception:
                pass

        # De-duplicate while preserving order
        seen = set()
        result: List[str] = []
        for t in tickers:
            t = (t or "").strip()
            if not t or t in seen:
                continue
            seen.add(t)
            result.append(t)
        return result

    def _fetch_hybrid_earnings_calendar(self, start_date, end_date, watchlist_tickers: Optional[List[str]] = None) -> List[EarningsEvent]:
        """Stub: FMP API is deprecated."""
        return []

    def _fetch_fmp_earnings_calendar_enriched(self, start_date, end_date) -> List[EarningsEvent]:
        """Stub: FMP API is deprecated."""
        return []

    def _fetch_fmp_earnings_calendar(self, start_date, end_date, watchlist_tickers=None, use_watchlist=True) -> List[EarningsEvent]:
        """Stub: FMP API is deprecated."""
        return []

    def _fetch_watchlist_earnings_only(self, start_date: datetime, end_date: datetime, watchlist_tickers: List[str]) -> List[EarningsEvent]:
        """Stub: FMP API is deprecated."""
        return []

    def _parse_fmp_earnings_item(self, symbol: str, item: dict, s_date, e_date) -> Optional[EarningsEvent]:
        """Stub: FMP API is deprecated."""
        return None

    def _fetch_fmp_ipo_calendar(self, start_date, end_date) -> List[IPOEvent]:
        """Stub: FMP API is deprecated."""
        return []
