from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from core.data_model.pit import PointInTime


class AssetClass(str, Enum):
    US_STOCKS = "US_STOCKS"
    CRYPTO_FUTURES = "CRYPTO_FUTURES"
    FOREX_MAJORS = "FOREX_MAJORS"
    COMMODITIES = "COMMODITIES"


class Currency(str, Enum):
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    JPY = "JPY"
    CHF = "CHF"
    CAD = "CAD"
    AUD = "AUD"
    NZD = "NZD"


class TradingSession(str, Enum):
    PRE_MARKET = "PRE_MARKET"
    REGULAR = "REGULAR"
    AFTER_HOURS = "AFTER_HOURS"
    BMO = "BMO"
    AMC = "AMC"
    CLOSED = "CLOSED"
    CRYPTO_CONTINUOUS = "CRYPTO_CONTINUOUS"


@dataclass(frozen=True)
class Exchange:
    mic: str
    name: str
    tz: str            # IANA timezone
    asset_class: AssetClass
    currency: Currency


# §27 exchange registry. Populated for US equities + crypto in Phase 1A;
# other MICs are stubbed with a documented "populate in later phase" marker.
EXCHANGES: dict[str, Exchange] = {
    "XNYS": Exchange("XNYS", "New York Stock Exchange", "America/New_York", AssetClass.US_STOCKS, Currency.USD),
    "XNAS": Exchange("XNAS", "Nasdaq", "America/New_York", AssetClass.US_STOCKS, Currency.USD),
    # Crypto exchanges trade 24/7; modeled with CRYPTO_CONTINUOUS session.
    "CRYPTO": Exchange("CRYPTO", "Crypto (24/7)", "UTC", AssetClass.CRYPTO_FUTURES, Currency.USD),
    # populate in later phase: LSE (Europe/London), TSE (Asia/Tokyo), EUREX, etc.
}


def get_exchange(mic: str) -> Optional[Exchange]:
    """Lookup an Exchange by MIC (case-insensitive). None if unknown/stubbed."""
    return EXCHANGES.get(str(mic or "").upper().strip())


@dataclass(frozen=True)
class Symbol:
    ticker: str
    exchange: Exchange
    asset_class: AssetClass
    currency: Currency
    isin: Optional[str] = None


# ---------------------------------------------------------------------------
# Data entities (spec §27). Each composes a PointInTime where applicable.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OHLCVBar:
    """Canonical OHLCV bar (§27). timestamp is UTC-aware bar close."""
    symbol: Symbol
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    adjusted: bool = False
    resolution: str = "1D"
    pit: Optional[PointInTime] = None

    def __post_init__(self):
        if self.timestamp.tzinfo is None:
            raise ValueError(f"OHLCVBar.timestamp must be UTC-aware: {self.timestamp!r}")
        if self.pit is None:
            object.__setattr__(self, "pit",
                PointInTime(event_time=self.timestamp, fetched_at=self.timestamp, version=0))


@dataclass(frozen=True)
class CorporateAction:
    """Corporate action (§31): SPLIT | DIVIDEND | TICKER_CHANGE | MERGER."""
    symbol: Symbol
    ex_date: datetime
    type: str
    ratio: Optional[float] = None
    amount: Optional[float] = None
    from_ticker: Optional[str] = None
    to_ticker: Optional[str] = None
    pit: Optional[PointInTime] = None


@dataclass(frozen=True)
class Earnings:
    """Earnings release (§27)."""
    symbol: Symbol
    fiscal_period: str
    report_date: datetime
    session: TradingSession
    eps_actual: Optional[float] = None
    eps_estimated: Optional[float] = None
    revenue_actual: Optional[float] = None
    pit: Optional[PointInTime] = None


@dataclass(frozen=True)
class AnalystEstimate:
    """Analyst estimate (§27)."""
    symbol: Symbol
    metric: str
    value: float
    as_of: datetime
    provider: str
    pit: Optional[PointInTime] = None


@dataclass(frozen=True)
class Fundamentals:
    """Fundamentals (§27)."""
    symbol: Symbol
    as_of: datetime
    market_cap: Optional[float] = None
    pe: Optional[float] = None
    pb: Optional[float] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    pit: Optional[PointInTime] = None


@dataclass(frozen=True)
class OptionContract:
    """Option contract (§27). Fetcher in Phase 5."""
    symbol: Symbol
    expiry: datetime
    strike: float
    type: str  # CALL | PUT
    iv: Optional[float] = None
    greeks: Optional[dict] = None
    volume: Optional[float] = None
    pit: Optional[PointInTime] = None


@dataclass(frozen=True)
class OptionsChain:
    """Options chain (§27). Fetcher in Phase 5."""
    symbol: Symbol
    as_of: datetime
    contracts: tuple = ()
    pit: Optional[PointInTime] = None


@dataclass(frozen=True)
class NewsArticle:
    """Canonical news article (§27). Superset of agents.news_tab_agent.NewsArticle."""
    id: str
    title: str
    url: str
    source: str
    published_at: datetime
    ticker: Optional[str] = None
    category: Optional[str] = None
    sentiment: Optional[str] = None
    sentiment_score: Optional[float] = None
    sentiment_confidence: Optional[float] = None
    impact: Optional[str] = None
    impact_score: Optional[float] = None
    action_score: Optional[float] = None
    article_type: Optional[str] = None
    is_duplicate: bool = False
    story_fingerprint: Optional[str] = None
    also_reported_by: tuple = ()
    ai_micro_summary: Optional[str] = None
    ai_deep_analysis: Optional[str] = None
    summary: Optional[str] = None
    related_tickers: tuple = ()
    is_breaking: bool = False
    is_market_wide: bool = False
    is_crypto: bool = False
    source_scope: Optional[str] = None
    sentiment_quality: Optional[dict] = None
    pit: Optional[PointInTime] = None


@dataclass(frozen=True)
class SECFiling:
    """Point-in-time SEC filing metadata (§27/§29)."""
    symbol: Optional[Symbol]
    form: str
    filed_at: datetime
    accession_no: str
    url: str
    pit: Optional[PointInTime] = None
    ticker: Optional[str] = None
    company_name: Optional[str] = None
    primary_document: Optional[str] = None
    description: Optional[str] = None


@dataclass
class Signal:
    """Canonical typed view over the existing core.signals signal dict (§27).

    The existing dict storage shape is NOT changed in Phase 1A; this is a
    read-only typed wrapper. Additional existing fields are carried via `meta`;
    1B migrates storage. PIT: created_at -> event_time, closed_at -> ..."""

    id: str
    ticker: str
    action: str
    entry: float
    entry_price: Optional[float]
    created_at: datetime
    closed_at: Optional[datetime] = None
    status: Optional[str] = None
    pit: Optional[PointInTime] = None
    meta: Optional[dict] = None

    def __post_init__(self):
        if self.meta is None:
            self.meta = {}
