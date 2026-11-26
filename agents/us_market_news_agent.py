"""
US Market News Agent - Economic Calendar and Market News

This agent provides:
- US Economic Calendar events (Fed meetings, NFP, CPI, etc.)
- Market status (open/closed/pre-market/after-hours)
- Breaking market news
- Event impact assessment

Author: Market Signals V6 Team
"""

import logging
import threading
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum

import requests
from core.finnhub_client import get_finnhub_client
from core.news_v9.text import clean_news_text

logger = logging.getLogger("USMarketNewsAgent")


# =============================================================================
# ENUMS AND DATA CLASSES
# =============================================================================

class EventCategory(Enum):
    """Economic event categories"""
    FED = "fed"
    INFLATION = "inflation"
    EMPLOYMENT = "employment"
    GDP = "gdp"
    HOUSING = "housing"
    CONSUMER = "consumer"
    MANUFACTURING = "manufacturing"
    SPEECH = "speech"
    OTHER = "other"


class EventImpact(Enum):
    """Event impact level"""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class EconomicEvent:
    """Economic calendar event"""
    id: str
    title: str
    event_type: str
    date: datetime
    category: EventCategory = EventCategory.OTHER
    impact: EventImpact = EventImpact.MEDIUM
    actual: Optional[str] = None
    forecast: Optional[str] = None
    previous: Optional[str] = None
    description: Optional[str] = None
    market_effect: Optional[str] = None
    # True when the date comes from the typical release pattern (e.g. "jobless
    # claims every Thursday 08:30 ET"), not from a published calendar.
    estimated: bool = False


@dataclass
class MarketNewsItem:
    """Market news item"""
    id: str
    title: str
    source: str
    published_at: datetime
    url: str = ""
    category: EventCategory = EventCategory.OTHER
    impact: EventImpact = EventImpact.MEDIUM
    summary: Optional[str] = None


@dataclass
class USMarketState:
    """Current state of US market news agent"""
    market_status: str = "UNKNOWN"
    today_events: List[EconomicEvent] = field(default_factory=list)
    this_week_events: List[EconomicEvent] = field(default_factory=list)
    upcoming_events: List[EconomicEvent] = field(default_factory=list)
    breaking_news: List[MarketNewsItem] = field(default_factory=list)
    last_update: Optional[datetime] = None


# =============================================================================
# MAJOR EVENTS INFO
# =============================================================================

MAJOR_EVENTS_INFO = {
    "NFP": {
        "title": "Non-Farm Payrolls",
        "category": EventCategory.EMPLOYMENT,
        "impact": EventImpact.HIGH,
        "description": "Monthly employment change excluding farm workers",
        "market_effect": "Higher than expected = USD bullish, stocks volatile",
    },
    "CPI": {
        "title": "Consumer Price Index",
        "category": EventCategory.INFLATION,
        "impact": EventImpact.HIGH,
        "description": "Measures inflation at consumer level",
        "market_effect": "Higher = Fed hawkish, bonds down, stocks volatile",
    },
    "FOMC": {
        "title": "FOMC Rate Decision",
        "category": EventCategory.FED,
        "impact": EventImpact.HIGH,
        "description": "Federal Reserve interest rate decision",
        "market_effect": "Rate hike = USD up, stocks down initially",
    },
    "JOBLESS_CLAIMS": {
        "title": "Initial Jobless Claims",
        "category": EventCategory.EMPLOYMENT,
        "impact": EventImpact.MEDIUM,
        "description": "Weekly unemployment insurance claims",
        "market_effect": "Higher claims = economic weakness signal",
    },
    "UNEMPLOYMENT": {
        "title": "Unemployment Rate",
        "category": EventCategory.EMPLOYMENT,
        "impact": EventImpact.HIGH,
        "description": "Percentage of labor force unemployed",
        "market_effect": "Higher = Fed dovish, mixed stock reaction",
    },
    "ISM_MFG": {
        "title": "ISM Manufacturing PMI",
        "category": EventCategory.MANUFACTURING,
        "impact": EventImpact.MEDIUM,
        "description": "Manufacturing sector health indicator",
        "market_effect": "Above 50 = expansion, below 50 = contraction",
    },
    "GDP": {
        "title": "GDP Growth Rate",
        "category": EventCategory.GDP,
        "impact": EventImpact.HIGH,
        "description": "Quarterly economic growth",
        "market_effect": "Higher growth = stocks bullish",
    },
}


# =============================================================================
# US MARKET NEWS AGENT
# =============================================================================

class USMarketNewsAgent:
    """Agent for US market economic calendar and news"""
    
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self._state = USMarketState()
        self._cache_lock = threading.Lock()
        self._last_fetch = None
        # Default to 5 minutes (300s) as requested
        self._cache_ttl = self.config.get("cache_ttl_seconds", 300)
        
        # State for rotating news fetch
        self._ticker_rotation_index = 0

        # FinnHub access is via the shared client (core/finnhub_client.py)
        # which resolves news.finnhub_api_key / env. No local key field needed.

        # History Persistence
        from core.config import data_path  # not relative to the working directory
        self._history_file = Path(data_path("news_history.jsonl"))
        self._history_seen_ids = set()
        self._load_history_ids()
        
        # Load recent news into memory state immediately
        self._state.breaking_news = self._load_recent_history_items()
            
    def _get_watchlist_tickers(self) -> List[str]:
        """Read watchlist tickers from disk."""
        try:
            from core.config import WATCHLIST_PATH
            import json
            if WATCHLIST_PATH.exists():
                with open(WATCHLIST_PATH, "r") as f:
                    data = json.load(f)
                    stocks = data.get("stocks", [])
                    crypto = data.get("crypto", [])
                    return stocks + crypto
        except Exception as e:
            logger.error(f"Error reading watchlist: {e}")
        return ["SPY", "QQQ"]
    
    def get_state(self) -> Dict[str, Any]:
        """Get current state as dictionary"""
        self._ensure_fresh_data()
        
        with self._cache_lock:
            return {
                "market_status": self._state.market_status,
                "today_events": [self._event_to_dict(e) for e in self._state.today_events],
                "this_week_events": [self._event_to_dict(e) for e in self._state.this_week_events],
                "upcoming_events": [self._event_to_dict(e) for e in self._state.upcoming_events],
                "breaking_news": [self._news_to_dict(n) for n in self._state.breaking_news],
                "last_update": self._state.last_update.isoformat() if self._state.last_update else None,
            }
    
    def _event_to_dict(self, event: EconomicEvent) -> Dict:
        """Convert event to dictionary"""
        return {
            "id": event.id,
            "title": event.title,
            "event_type": event.event_type,
            "date": event.date.isoformat(),
            "category": event.category.value,
            "impact": event.impact.value,
            "actual": event.actual,
            "forecast": event.forecast,
            "previous": event.previous,
            "description": event.description,
            "market_effect": event.market_effect,
            # Without a calendar API key the dates are estimates from the usual
            # release pattern; the payload now says so instead of presenting
            # them as a confirmed schedule.
            "estimated": bool(getattr(event, "estimated", False)),
            "date_source": "typical_release_schedule" if getattr(event, "estimated", False) else "economic_calendar",
        }
    
    def _news_to_dict(self, news: MarketNewsItem) -> Dict:
        """Convert news to dictionary"""
        return {
            "id": news.id,
            "title": news.title,
            "source": news.source,
            "published_at": news.published_at.isoformat(),
            "url": news.url,
            "category": news.category.value,
            "impact": news.impact.value,
            "summary": news.summary,
        }
    
    def _ensure_fresh_data(self):
        """Refresh data if cache expired"""
        now = datetime.now()
        
        if self._last_fetch and (now - self._last_fetch).total_seconds() < self._cache_ttl:
            return
        
        try:
            self._refresh_all_data()
            self._last_fetch = now
        except Exception as e:
            logger.error(f"Error refreshing US market data: {e}")
    
    def _refresh_all_data(self):
        """Refresh all data from sources"""
        with self._cache_lock:
            self._state.market_status = self._get_market_status()
            
            # Get economic events
            events = self._fetch_economic_events()
            
            now = datetime.now()
            today_end = now.replace(hour=23, minute=59, second=59)
            week_end = now + timedelta(days=7)
            
            self._state.today_events = [e for e in events if e.date <= today_end]
            self._state.this_week_events = [e for e in events if today_end < e.date <= week_end]
            self._state.upcoming_events = [e for e in events if e.date > week_end][:20]
            
            # Breaking news - Merge for rotation support
            new_news = self._fetch_breaking_news()
            # Lock already held from outer 'with self._cache_lock' (line 242)
            current_news = self._state.breaking_news or []
            # Map news by ID for deduplication
            news_map = {n.id: n for n in current_news}
            for n in new_news:
                news_map[n.id] = n
            
            # Persist new news to history
            self._append_to_history(new_news)
            
            # Filter old news (older than 24h)
            cutoff = datetime.now() - timedelta(hours=24)
            all_news = [n for n in news_map.values() if n.published_at > cutoff]
            
            # Sort and cap
            all_news.sort(key=lambda x: x.published_at, reverse=True)
            self._state.breaking_news = all_news[:50]
            
            self._state.last_update = now
    
    def _fetch_economic_events(self) -> List[EconomicEvent]:
        """Fetch economic events from available sources"""
        events = []
        
        # Try FinnHub if key available
        if get_finnhub_client().is_available():
            events.extend(self._fetch_finnhub_calendar())
        
        # If no API events, generate estimates for recurring events
        if not events:
            events = self._generate_estimated_events()
        
        return events

    def _fetch_finnhub_calendar(self) -> List[EconomicEvent]:
        """Fetch economic calendar from FinnHub (via shared client)."""
        events = []

        try:
            today = datetime.now().date()
            end_date = today + timedelta(days=14)

            items = get_finnhub_client().get_economic_calendar(today, end_date)
            if not items:
                return []

            for item in items:
                # Only US events
                if item.get("country") != "US":
                    continue
                
                try:
                    event_time = item.get("time", "")
                    if event_time:
                        event_date = datetime.strptime(event_time, "%Y-%m-%d %H:%M:%S")
                    else:
                        event_date = datetime.strptime(item.get("date", ""), "%Y-%m-%d")
                    
                    # Map to our categories
                    event_name = item.get("event", "")
                    category = self._categorize_event(event_name)
                    impact = self._map_impact(item.get("impact", ""))
                    
                    events.append(EconomicEvent(
                        id=f"fh_{item.get('id', event_date.strftime('%Y%m%d%H%M'))}",
                        title=event_name,
                        event_type="API",
                        date=event_date,
                        category=category,
                        impact=impact,
                        actual=item.get("actual"),
                        forecast=item.get("estimate"),
                        previous=item.get("prev"),
                    ))
                except Exception as e:
                    logger.debug(f"Error parsing FinnHub event: {e}")
                    
        except Exception as e:
            logger.error(f"FinnHub calendar fetch error: {e}")
        
        return events
    
    def _generate_estimated_events(self) -> List[EconomicEvent]:
        """Generate estimated events for recurring releases."""
        events = []
        now = datetime.now()
        
        # Generate events for next 30 days based on typical schedules
        for i in range(30):
            check_date = now + timedelta(days=i)
            weekday = check_date.weekday()  # Monday = 0
            day_of_month = check_date.day
            
            # Weekly Jobless Claims (every Thursday)
            if weekday == 3:  # Thursday
                event_time = check_date.replace(hour=8, minute=30, second=0, microsecond=0)
                if event_time > now:
                    info = MAJOR_EVENTS_INFO["JOBLESS_CLAIMS"]
                    events.append(EconomicEvent(
                        id=f"jobless_{check_date.strftime('%Y%m%d')}",
                        title=info["title"],
                        event_type="JOBLESS_CLAIMS",
                        date=event_time,
                        category=info["category"],
                        impact=info["impact"],
                        description=info["description"],
                        market_effect=info["market_effect"],
                    ))
            
            # NFP (first Friday)
            if weekday == 4 and day_of_month <= 7:  # First Friday
                event_time = check_date.replace(hour=8, minute=30, second=0, microsecond=0)
                if event_time > now:
                    info = MAJOR_EVENTS_INFO["NFP"]
                    events.append(EconomicEvent(
                        id=f"nfp_{check_date.strftime('%Y%m%d')}",
                        title=info["title"],
                        event_type="NFP",
                        date=event_time,
                        category=info["category"],
                        impact=info["impact"],
                        description=info["description"],
                        market_effect=info["market_effect"],
                    ))
                    
                    # Also add unemployment rate
                    info_ue = MAJOR_EVENTS_INFO["UNEMPLOYMENT"]
                    events.append(EconomicEvent(
                        id=f"ue_{check_date.strftime('%Y%m%d')}",
                        title=info_ue["title"],
                        event_type="UNEMPLOYMENT",
                        date=event_time,
                        category=info_ue["category"],
                        impact=info_ue["impact"],
                        description=info_ue["description"],
                        market_effect=info_ue["market_effect"],
                    ))
            
            # CPI (around 10th-15th)
            if 10 <= day_of_month <= 15 and weekday < 5:  # Weekday between 10-15
                if day_of_month == 10 or (day_of_month > 10 and check_date.replace(day=day_of_month-1).weekday() >= 5):
                    event_time = check_date.replace(hour=8, minute=30, second=0, microsecond=0)
                    if event_time > now:
                        info = MAJOR_EVENTS_INFO["CPI"]
                        events.append(EconomicEvent(
                            id=f"cpi_{check_date.strftime('%Y%m')}",
                            title=info["title"],
                            event_type="CPI",
                            date=event_time,
                            category=info["category"],
                            impact=info["impact"],
                            description=info["description"],
                            market_effect=info["market_effect"],
                        ))
            
            # ISM Manufacturing (first business day)
            if weekday < 5 and day_of_month <= 3:
                first_day = check_date.replace(day=1)
                if first_day.weekday() >= 5:
                    first_biz_day = first_day + timedelta(days=(7 - first_day.weekday()))
                else:
                    first_biz_day = first_day
                
                if check_date.date() == first_biz_day.date():
                    event_time = check_date.replace(hour=10, minute=0, second=0, microsecond=0)
                    if event_time > now:
                        info = MAJOR_EVENTS_INFO["ISM_MFG"]
                        events.append(EconomicEvent(
                            id=f"ism_mfg_{check_date.strftime('%Y%m')}",
                            title=info["title"],
                            event_type="ISM_MFG",
                            date=event_time,
                            category=info["category"],
                            impact=info["impact"],
                            description=info["description"],
                            market_effect=info["market_effect"],
                        ))

        for event in events:
            event.estimated = True
        return events
    
    def _load_history_ids(self):
        """Load existing news IDs from history to prevent duplicates."""
        if not self._history_file.exists():
            return
            
        try:
            with open(self._history_file, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        data = json.loads(line.strip())
                        if 'id' in data:
                            self._history_seen_ids.add(data['id'])
                    except:
                        continue
            logger.info(f"Loaded {len(self._history_seen_ids)} historical news items.")
        except Exception as e:
            logger.error(f"Error loading news history: {e}")

    def _load_recent_history_items(self, hours: int = 24) -> List[MarketNewsItem]:
        """Load full news objects from history within the last window."""
        items = []
        if not self._history_file.exists():
            return items
            
        cutoff = datetime.now() - timedelta(hours=hours)
        
        try:
            with open(self._history_file, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        data = json.loads(line.strip())
                        # Check fetched_at or published_at
                        ts_str = data.get('fetched_at') or data.get('published_at')
                        if ts_str:
                            ts = datetime.fromisoformat(ts_str)
                            if ts > cutoff:
                                # Reconstruct MarketNewsItem
                                try:
                                    category = EventCategory(data.get('category'))
                                except ValueError:
                                    category = EventCategory.OTHER
                                try:
                                    impact = EventImpact(data.get('impact'))
                                except ValueError:
                                    impact = EventImpact.MEDIUM
                                items.append(MarketNewsItem(
                                    id=data['id'],
                                    title=data['title'],
                                    source=data['source'],
                                    published_at=datetime.fromisoformat(data['published_at']),
                                    url=data.get('url', ''),
                                    category=category,
                                    impact=impact,
                                    summary=data.get('summary', '')
                                ))
                    except Exception:
                        continue
        except Exception as e:
            logger.error(f"Error loading recent history items: {e}")
            
        # Sort newest first
        items.sort(key=lambda x: x.published_at, reverse=True)
        return items

    def _append_to_history(self, news_items: List[MarketNewsItem]):
        """Append new news items to persistent history."""
        if not news_items:
            return
            
        # Ensure directory exists
        self._history_file.parent.mkdir(parents=True, exist_ok=True)
            
        try:
            with open(self._history_file, 'a', encoding='utf-8') as f:
                count = 0
                for item in news_items:
                    if item.id not in self._history_seen_ids:
                        # Convert to dict and serialize
                        data = self._news_to_dict(item)
                        # Add timestamp for when we fetched it (retention valid)
                        data['fetched_at'] = datetime.now().isoformat()
                        
                        f.write(json.dumps(data) + '\n')
                        self._history_seen_ids.add(item.id)
                        count += 1
                
                if count > 0:
                    logger.info(f"Persisted {count} new news items to history.")
        except Exception as e:
            logger.error(f"Error ensuring news history: {e}")

    def _fetch_breaking_news(self, max_age_hours: int = 24) -> List[MarketNewsItem]:
        """Fetch breaking market news."""
        news = []
        
        # 1. Fetch for Watchlist Tickers (FinnHub Priority)
        try:
            all_tickers = self._get_watchlist_tickers()
            
            # ROTATION LOGIC: Pick next batch of tickers
            BATCH_SIZE = 15
            if not all_tickers:
                tickers = []
            else:
                total_tickers = len(all_tickers)
                # Ensure we don't divide by zero
                start_idx = self._ticker_rotation_index % total_tickers
                
                # Select batch with wrap-around support
                tickers = []
                for i in range(BATCH_SIZE):
                    idx = (start_idx + i) % total_tickers
                    tickers.append(all_tickers[idx])
                
                # Update rotation index for next run
                self._ticker_rotation_index = (start_idx + BATCH_SIZE) % total_tickers
                logger.info(f"News Rotation: Fetching items {start_idx} to {(start_idx + BATCH_SIZE - 1) % total_tickers} (Batch: {tickers})")
            
            fh = get_finnhub_client()
            if not fh.is_available():
                logger.warning("No FinnHub API key found in configuration. Skipping breaking news fetch.")
            else:
                # Log current fetching action
                logger.info(f"Fetching breaking news for tickers: {tickers}")
                today = datetime.now()
                week_ago = today - timedelta(days=3)
                for ticker in tickers:
                    try:
                        # FinnHub Company News (last 3 days) — token stays in
                        # params inside the client (never in the URL string, so
                        # it can't leak via http_get's logged endpoint).
                        data = fh.get_company_news(ticker, week_ago, today)
                        if not isinstance(data, list):
                            continue
                        for item in data[:3]:
                            pub_ts = item.get("datetime", 0)
                            published = datetime.fromtimestamp(pub_ts)
                            # Provider text can carry markup and undecoded
                            # entities ("&amp;", "&#39;"): clean once here.
                            headline = clean_news_text(item.get("headline", ""))

                            news.append(MarketNewsItem(
                                id=f"fh_{item.get('id', pub_ts)}",
                                title=f"[{ticker}] {headline}",
                                source=item.get("source", "FinnHub"),
                                published_at=published,
                                url=item.get("url", ""),
                                category=self._categorize_news(headline),
                                impact=self._assess_news_impact(headline),
                                summary=clean_news_text(item.get("summary", ""))
                            ))
                    except Exception as e:
                        logger.warning(f"FinnHub news fetch error for {ticker}: {e}")
        except Exception as e:
             logger.debug(f"Watchlist news fetch error: {e}")

        # 2. Yahoo Fallback Removed (Redundant with NewsTabAgent and causes Rate Limits)
        # The user requested news to come from FMP. NewsTabAgent handles this.
        # This agent now strictly focuses on Economic Calendar and FinnHub specific hits.
        
        unique_news = news
        unique_news.sort(key=lambda x: x.published_at, reverse=True)
        
        return unique_news[:20]
    
    def _get_market_status(self) -> str:
        """Get current market status."""
        now = datetime.now()
        weekday = now.weekday()
        hour = now.hour
        minute = now.minute
        
        if weekday >= 5:
            return "CLOSED"
        
        current_time = hour * 60 + minute
        
        pre_market_start = 4 * 60
        market_open = 9 * 60 + 30
        market_close = 16 * 60
        after_hours_end = 20 * 60
        
        if current_time < pre_market_start:
            return "CLOSED"
        elif current_time < market_open:
            return "PRE_MARKET"
        elif current_time < market_close:
            return "OPEN"
        elif current_time < after_hours_end:
            return "AFTER_HOURS"
        else:
            return "CLOSED"
    
    def _categorize_event(self, event_name: str) -> EventCategory:
        """Categorize event by name."""
        name_lower = event_name.lower()
        
        if any(kw in name_lower for kw in ["fed", "fomc", "rate", "powell"]):
            return EventCategory.FED
        if any(kw in name_lower for kw in ["cpi", "ppi", "pce", "inflation", "price index"]):
            return EventCategory.INFLATION
        if any(kw in name_lower for kw in ["payroll", "nfp", "unemployment", "jobless", "employment"]):
            return EventCategory.EMPLOYMENT
        if any(kw in name_lower for kw in ["gdp", "growth"]):
            return EventCategory.GDP
        if any(kw in name_lower for kw in ["housing", "home", "construction"]):
            return EventCategory.HOUSING
        if any(kw in name_lower for kw in ["retail", "consumer", "spending", "confidence"]):
            return EventCategory.CONSUMER
        if any(kw in name_lower for kw in ["pmi", "manufacturing", "industrial", "ism"]):
            return EventCategory.MANUFACTURING
        if any(kw in name_lower for kw in ["speak", "speech", "testimony"]):
            return EventCategory.SPEECH
        
        return EventCategory.OTHER
    
    def _categorize_news(self, title: str) -> EventCategory:
        """Categorize news by title."""
        return self._categorize_event(title)
    
    def _map_impact(self, impact_str: str) -> EventImpact:
        """Map API impact string to our enum."""
        impact_lower = (impact_str or "").lower()
        
        if impact_lower in ["high", "3"]:
            return EventImpact.HIGH
        if impact_lower in ["medium", "2"]:
            return EventImpact.MEDIUM
        if impact_lower in ["low", "1"]:
            return EventImpact.LOW
        
        return EventImpact.MEDIUM
    
    def _assess_news_impact(self, title: str) -> EventImpact:
        """Assess news impact from title."""
        title_lower = title.lower()
        
        if any(kw in title_lower for kw in ["crash", "surge", "plunge", "fed", "rate", "emergency"]):
            return EventImpact.HIGH
        
        if any(kw in title_lower for kw in ["earnings", "beat", "miss", "guidance", "layoff", "acquisition"]):
            return EventImpact.HIGH
        
        if any(kw in title_lower for kw in ["analyst", "upgrade", "downgrade", "price target"]):
            return EventImpact.MEDIUM
        
        return EventImpact.LOW
    
    def _get_all_events_until(self, cutoff: datetime) -> List[EconomicEvent]:
        """Get all events until cutoff date."""
        self._ensure_fresh_data()
        
        all_events = []
        with self._cache_lock:
            all_events.extend(self._state.today_events)
            all_events.extend(self._state.this_week_events)
            all_events.extend(self._state.upcoming_events)
        
        seen_ids = set()
        unique = []
        for e in all_events:
            if e.id not in seen_ids and e.date <= cutoff:
                seen_ids.add(e.id)
                unique.append(e)
        
        return unique


    def get_today_events(self) -> List[Dict]:
        """Get today's economic events."""
        self._ensure_fresh_data()
        with self._cache_lock:
            return [self._event_to_dict(e) for e in self._state.today_events]

    def get_this_week_events(self) -> List[Dict]:
        """Get this week's economic events."""
        self._ensure_fresh_data()
        with self._cache_lock:
            return [self._event_to_dict(e) for e in self._state.this_week_events]

    def get_high_impact_events(self, days: int = 30) -> List[Dict]:
        """Get high impact events for next N days."""
        self._ensure_fresh_data()
        
        cutoff = datetime.now() + timedelta(days=days)
        events = self._get_all_events_until(cutoff)
        
        high_impact = [e for e in events if e.impact == EventImpact.HIGH]
        return [self._event_to_dict(e) for e in high_impact]

    def get_fed_schedule(self) -> List[Dict]:
        """Get upcoming FED/FOMC events."""
        self._ensure_fresh_data()
        
        cutoff = datetime.now() + timedelta(days=90)
        events = self._get_all_events_until(cutoff)
        
        fed_events = [e for e in events if e.category == EventCategory.FED]
        return [self._event_to_dict(e) for e in fed_events]

    def get_next_fed_meeting(self) -> Optional[Dict]:
        """Get next FED meeting."""
        schedule = self.get_fed_schedule()
        return schedule[0] if schedule else None

    def get_breaking_news(self, max_age_hours: int = 24) -> List[Dict]:
        """Get breaking news filtered by age."""
        self._ensure_fresh_data()
        
        with self._cache_lock:
            news = self._state.breaking_news
            
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        valid_news = [n for n in news if n.published_at >= cutoff]
        
        return [self._news_to_dict(n) for n in valid_news]
    
    def refresh(self) -> Dict:
        """Force refresh data."""
        try:
            self._refresh_all_data()
            return {"status": "ok", "message": "Data refreshed"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

# =============================================================================
# SINGLETON
# =============================================================================

_us_market_news_agent: Optional[USMarketNewsAgent] = None


def get_us_market_news_agent(config: Optional[Dict] = None) -> USMarketNewsAgent:
    """Get or create USMarketNewsAgent singleton."""
    global _us_market_news_agent
    if _us_market_news_agent is None:
        _us_market_news_agent = USMarketNewsAgent(config)
    return _us_market_news_agent

# Singleton Instance
_us_market_news_agent_instance = None

def get_us_market_news_agent(config=None):
    global _us_market_news_agent_instance
    if _us_market_news_agent_instance is None:
        if config is None:
            from core.config import ConfigManager
            config = ConfigManager()._config
        _us_market_news_agent_instance = USMarketNewsAgent(config)
    return _us_market_news_agent_instance
