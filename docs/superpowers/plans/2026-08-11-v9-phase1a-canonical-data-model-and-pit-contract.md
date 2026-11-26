# Phase 1A — Canonical Data Model & Point-in-Time Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish V9's canonical data layer (the 15 §27 entities), the point-in-time contract (`as_of`/`UNAVAILABLE` = the §32 no-synthetic-historical-facts guard), the DATA-quality vs SIGNAL-quality separation (§33), and a real market-calendar/session model (§28) — as a backward-compatible evolution of the migrated V7 code that keeps the 1559-test green baseline green.

**Architecture:** A new `core/data_model/` package (`entities.py`, `pit.py`, `quality.py`) is the single source of truth for data entity types and contracts. `core/market_hours.py` is evolved in place into a session/calendar module. `core/data_quality.py` gains additive optional fields. No provider is rewired and no storage is migrated in 1A (that is 1B); 1A delivers the contract + a typed reader for the one append-only store that already exists (`news_evidence_ledger`).

**Tech Stack:** Python 3.14.3, `dataclasses`/`zoneinfo`/`enum` (stdlib), `pytz` (installed), `pytest` (existing). **No new packages** (§118 — open-source deps are Phase 2).

## Global Constraints

- **V7 read-only (§117):** V7 at `C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium` (HEAD f912a33, 109 dirty) is never modified.
- **Evolve, no rewrite (§5):** existing public signatures stay; changes are additive only.
- **No test weakening (§115):** existing `test_market_hours_guard.py`, `test_data_quality_module.py`, `test_data_quality_invariants.py`, `test_premarket_volume_and_earnings_stale.py`, and the premarket agent tests must stay green unchanged. The 1559-test baseline must stay green at 1A completion.
- **No new open-source deps (§118):** market calendar stays hand-coded; `exchange_calendars` etc. deferred to Phase 2.
- **No remote push (§116):** local commits only.
- **No secrets in V9 (§106).**
- **No forbidden success language (§4):** "mostly complete", "approximately complete", "99%", "should work", "probably works", "backend finished", "frontend pending", "TODO later", "mock for now", "temporary workaround", "untested but implemented" — must NOT appear. `# populate in later phase` markers are the sanctioned living-doc mechanism (intentional stubs with a phase owner), NOT forbidden placeholders.
- **Working dir:** `C:\Users\<user>\PyCharmMiscProject\Market Signals V9`.
- **Commit identity:** Mario (already configured). Commit message prefix `feat(v9-phase1a):` / `refactor(v9-phase1a):` / `docs(v9-phase1a):` / `chore(v9-phase1a):` as appropriate.
- **TDD:** write failing test → run (fail) → implement minimal → run (pass) → commit. Frequent small commits.

---

## File Structure

**New — `core/data_model/` package:**
- `core/data_model/__init__.py` — public exports.
- `core/data_model/entities.py` — `AssetClass`, `Currency`, `TradingSession` enums; `Exchange`, `Symbol` value types; `EXCHANGES` registry; the 15 §27 entity dataclasses (`OHLCVBar`, `CorporateAction`, `Earnings`, `AnalystEstimate`, `Fundamentals`, `OptionContract`, `OptionsChain`, `NewsArticle`, `SECFiling`, `Signal`).
- `core/data_model/pit.py` — `PointInTime`, `UNAVAILABLE` sentinel, `to_utc()`, `assert_utc()`, `as_of()`.
- `core/data_model/quality.py` — `Conflict`, `DataQuality`, `SignalQuality`, `QualityEnvelope`.

**Evolved in place (additive only):**
- `core/market_hours.py` — `MarketHoursStatus` gains `session`/`is_half_day`/`exchange`; new `_classify_session()`; new `earnings_session_reported()`; `EXCHANGES` wiring (imports from `entities`); US-equity holiday algorithm unchanged.
- `core/data_quality.py` — `QualifiedValue` gains optional `freshness`/`coverage`/`conflicts`/`quality_flags` (defaults preserve all existing call sites); `to_dict()` includes them when set.
- `core/news_evidence_ledger.py` — new `read_articles_as_of(T)` typed reader (JSONL format unchanged).
- `agents/premarket_scanner_agent.py` — `_is_us_market_open` delegates to `market_hours`; `_earnings_already_reported` same-day branch delegates to `market_hours.earnings_session_reported`.

**New tests (flat in `tests/unit/`, matching codebase convention):**
- `tests/unit/test_data_model_entities.py`
- `tests/unit/test_data_model_pit.py`
- `tests/unit/test_data_model_quality.py`
- `tests/unit/test_market_calendar_sessions.py`
- `tests/unit/test_news_evidence_ledger_pit.py`

---

### Task 1: `core/data_model/` package scaffold + value types & exchange registry + Phase 1 IN PROGRESS

**Files:**
- Create: `core/data_model/__init__.py`, `core/data_model/entities.py`
- Create: `tests/unit/test_data_model_entities.py`
- Modify: `docs/V9_IMPLEMENTATION.md` (Phase 1 row `NOT STARTED` → `IN PROGRESS`)

**Interfaces:**
- Produces: `AssetClass`, `Currency`, `TradingSession` enums; `Exchange`, `Symbol` dataclasses; `EXCHANGES` dict; `get_exchange()`.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_data_model_entities.py`:
```python
from datetime import datetime, timezone
from core.data_model.entities import (
    AssetClass, Currency, TradingSession, Exchange, Symbol, EXCHANGES, get_exchange,
)


def test_asset_class_values():
    assert AssetClass.US_STOCKS.value == "US_STOCKS"
    assert AssetClass.CRYPTO_FUTURES.value == "CRYPTO_FUTURES"
    assert AssetClass.FOREX_MAJORS.value == "FOREX_MAJORS"
    assert AssetClass.COMMODITIES.value == "COMMODITIES"


def test_trading_sessions_present():
    for s in ("PRE_MARKET", "REGULAR", "AFTER_HOURS", "BMO", "AMC", "CLOSED", "CRYPTO_CONTINUOUS"):
        assert TradingSession[s] is not None


def test_exchanges_registry_populated():
    assert EXCHANGES["XNYS"].tz == "America/New_York"
    assert EXCHANGES["XNYS"].currency is Currency.USD
    assert EXCHANGES["XNAS"].asset_class is AssetClass.US_STOCKS
    assert EXCHANGES["CRYPTO"].tz == "UTC"


def test_get_exchange_case_insensitive():
    assert get_exchange("xnys") is EXCHANGES["XNYS"]
    assert get_exchange("") is None
    assert get_exchange("XLON") is None  # stubbed for later phase


def test_symbol_frozen():
    sym = Symbol("AAPL", EXCHANGES["XNAS"], AssetClass.US_STOCKS, Currency.USD)
    assert sym.ticker == "AAPL"
    assert sym.exchange.mic == "XNAS"
    assert sym.isin is None
    import dataclasses
    try:
        sym.ticker = "MSFT"  # frozen -> must raise
        assert False, "Symbol must be frozen"
    except dataclasses.FrozenInstanceError:
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_data_model_entities.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.data_model'`

- [ ] **Step 3: Write minimal implementation**

`core/data_model/entities.py`:
```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


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
```

`core/data_model/__init__.py`:
```python
"""V9 canonical data model — single source of truth for data entities (spec §27)."""
from core.data_model.entities import (
    AssetClass, Currency, TradingSession, Exchange, Symbol, EXCHANGES, get_exchange,
)

__all__ = [
    "AssetClass", "Currency", "TradingSession", "Exchange", "Symbol",
    "EXCHANGES", "get_exchange",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_data_model_entities.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Flip Phase 1 to IN PROGRESS in `docs/V9_IMPLEMENTATION.md`**

Using `Edit`, change the Phase 1 table row:
- old: `| 1 | Data & Architecture Foundation (canonical data, point-in-time, data quality) | NOT STARTED |`
- new: `| 1 | Data & Architecture Foundation (canonical data, point-in-time, data quality) | IN PROGRESS |`

- [ ] **Step 6: Commit**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add core/data_model/__init__.py core/data_model/entities.py tests/unit/test_data_model_entities.py docs/V9_IMPLEMENTATION.md
git commit -m "feat(v9-phase1a): core/data_model package — enums, Exchange, Symbol, EXCHANGES registry; Phase 1 IN PROGRESS"
```

---

### Task 2: `core/data_model/pit.py` — PointInTime, UNAVAILABLE, as_of (the §32 guard)

**Files:**
- Create: `core/data_model/pit.py`
- Create: `tests/unit/test_data_model_pit.py`
- Modify: `core/data_model/__init__.py` (export PIT symbols)

**Interfaces:**
- Produces: `PointInTime`, `UNAVAILABLE`, `to_utc()`, `assert_utc()`, `as_of()`.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_data_model_pit.py`:
```python
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from core.data_model.pit import (
    PointInTime, UNAVAILABLE, to_utc, assert_utc, as_of,
)

UTC = timezone.utc


def test_to_utc_naive_assumed_utc():
    dt = datetime(2026, 8, 11, 12, 0, 0)  # naive
    assert to_utc(dt).tzinfo is UTC
    assert to_utc(dt).hour == 12


def test_to_utc_aware_converted():
    from zoneinfo import ZoneInfo
    dt = datetime(2026, 8, 11, 9, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    u = to_utc(dt)
    assert u.tzinfo is UTC
    assert u.hour == 13  # 09:00 ET -> 13:00 UTC (EDT)


def test_assert_utc_rejects_naive():
    try:
        assert_utc(datetime(2026, 8, 11, 12, 0, 0))
        assert False, "naive datetime must raise"
    except ValueError:
        pass


def test_point_in_time_rejects_naive_fields():
    try:
        PointInTime(event_time=datetime(2026, 8, 11, 12, 0, 0))  # naive
        assert False, "naive event_time must raise"
    except ValueError:
        pass


def test_point_in_time_defaults():
    p = PointInTime(fetched_at=datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC))
    assert p.event_time is None
    assert p.version == 0


@dataclass(frozen=True)
class _Rec:
    name: str
    pit: PointInTime


def _rec(name, available_from, version=0):
    return _Rec(name, PointInTime(available_from=available_from, fetched_at=available_from, version=version))


def test_as_of_returns_newest_qualifying():
    a = _rec("a", datetime(2026, 1, 1, tzinfo=UTC))
    b = _rec("b", datetime(2026, 3, 1, tzinfo=UTC))
    c = _rec("c", datetime(2026, 2, 1, tzinfo=UTC))
    got = as_of([a, b, c], datetime(2026, 2, 15, tzinfo=UTC))
    assert got is c  # newest with available_from <= Feb 15


def test_as_of_returns_unavailable_when_none_qualify():
    a = _rec("a", datetime(2026, 3, 1, tzinfo=UTC))
    got = as_of([a], datetime(2026, 1, 1, tzinfo=UTC))
    assert got is UNAVAILABLE
    assert not bool(UNAVAILABLE)


def test_as_of_tiebreak_by_version():
    a = _rec("a", datetime(2026, 1, 1, tzinfo=UTC), version=1)
    b = _rec("b", datetime(2026, 1, 1, tzinfo=UTC), version=3)
    got = as_of([a, b], datetime(2026, 6, 1, tzinfo=UTC))
    assert got is b  # higher version wins on tie


def test_as_of_skips_records_without_pit_or_available_from():
    @dataclass(frozen=True)
    class _NoPit:
        name: str
    a = _rec("a", datetime(2026, 1, 1, tzinfo=UTC))
    got = as_of([_NoPit("x"), a], datetime(2026, 6, 1, tzinfo=UTC))
    assert got is a
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_data_model_pit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.data_model.pit'`

- [ ] **Step 3: Write minimal implementation**

`core/data_model/pit.py`:
```python
"""Point-in-time contract (spec §29) and the no-synthetic-historical-facts guard (§32).

At historical timestamp T the system may use only data available at T.
`as_of(records, T)` returns the newest record whose `available_from <= T`, or
the `UNAVAILABLE` sentinel when none qualifies — through this API it is
impossible to read a record that was not available at T (§32).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

UTC = timezone.utc


def to_utc(dt: datetime) -> datetime:
    """Normalize a datetime to UTC-aware. Naive datetimes are assumed UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def assert_utc(dt: datetime) -> datetime:
    """Raise ValueError if dt is tz-naive; return dt normalized to UTC."""
    if dt.tzinfo is None:
        raise ValueError(f"tz-naive datetime not allowed: {dt!r}")
    return dt.astimezone(UTC)


class _Unavailable:
    """Sentinel for §32: missing historical information must remain UNAVAILABLE."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNAVAILABLE"

    def __bool__(self) -> bool:
        return False

    def __eq__(self, other) -> bool:
        return other is self

    def __hash__(self) -> int:
        return hash("UNAVAILABLE")


UNAVAILABLE = _Unavailable()


@dataclass(frozen=True)
class PointInTime:
    event_time: datetime | None = None        # when the event happened
    publication_time: datetime | None = None   # when the info was published
    provider_time: datetime | None = None      # provider's timestamp on the record
    received_at: datetime | None = None        # when V9 received it
    available_from: datetime | None = None     # when it became usable (>= publication_time)
    fetched_at: datetime | None = None         # when V9 fetched it (UTC-aware)
    version: int = 0                           # monotonic per (symbol, entity)

    def __post_init__(self):
        for f in (self.event_time, self.publication_time, self.provider_time,
                  self.received_at, self.available_from, self.fetched_at):
            if f is not None and f.tzinfo is None:
                raise ValueError(f"PointInTime field tz-naive: {f!r}")


def as_of(records: Iterable[Any], T: datetime, *, prefer: str = "available_from") -> Any:
    """Return the newest record with `pit.<prefer> <= T`, or UNAVAILABLE (§32).

    Records without a `pit` attribute, or whose `pit.<prefer>` is None, are
    treated as not-available-at-T. Ties on `prefer` are broken by `pit.version`.
    """
    T = assert_utc(T)
    best: Any = None
    best_ts: datetime | None = None
    for r in records:
        pit = getattr(r, "pit", None)
        if pit is None:
            continue
        ts = getattr(pit, prefer, None)
        if ts is None:
            continue
        ts = to_utc(ts)
        if ts > T:
            continue
        if best is None or ts > best_ts or (ts == best_ts and pit.version > best.pit.version):
            best = r
            best_ts = ts
    return best if best is not None else UNAVAILABLE
```

Update `core/data_model/__init__.py` to add:
```python
from core.data_model.pit import PointInTime, UNAVAILABLE, to_utc, assert_utc, as_of
```
and append `"PointInTime", "UNAVAILABLE", "to_utc", "assert_utc", "as_of"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_data_model_pit.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add core/data_model/pit.py core/data_model/__init__.py tests/unit/test_data_model_pit.py
git commit -m "feat(v9-phase1a): core/data_model.pit — PointInTime, UNAVAILABLE, as_of (§32 guard)"
```

---

### Task 3: `core/data_model/entities.py` — the 15 §27 entity dataclasses

**Files:**
- Modify: `core/data_model/entities.py` (append the data entities)
- Modify: `tests/unit/test_data_model_entities.py` (add entity tests)
- Modify: `core/data_model/__init__.py` (export entity types)

**Interfaces:**
- Produces: `OHLCVBar`, `CorporateAction`, `Earnings`, `AnalystEstimate`, `Fundamentals`, `OptionContract`, `OptionsChain`, `NewsArticle`, `SECFiling`, `Signal`.
- Consumes: `core.data_model.pit.PointInTime` (Task 2).

- [ ] **Step 1: Write the failing test (append to `tests/unit/test_data_model_entities.py`)**

```python
from core.data_model.entities import (
    OHLCVBar, CorporateAction, Earnings, AnalystEstimate, Fundamentals,
    OptionContract, OptionsChain, NewsArticle, SECFiling, Signal,
)
from core.data_model.pit import PointInTime, UNAVAILABLE, as_of

_NYSE = EXCHANGES["XNYS"]


def _sym(t="AAPL"):
    return Symbol(t, EXCHANGES["XNAS"], AssetClass.US_STOCKS, Currency.USD)


def test_ohlcvbar_auto_pit_and_utc_enforced():
    ts = datetime(2026, 8, 11, 20, 0, 0, tzinfo=timezone.utc)
    bar = OHLCVBar(_sym(), ts, 100.0, 101.0, 99.0, 100.5, 1000.0)
    assert bar.pit is not None
    assert bar.pit.event_time == ts
    try:
        OHLCVBar(_sym(), datetime(2026, 8, 11), 1, 1, 1, 1, 1)  # naive ts
        assert False, "naive timestamp must raise"
    except ValueError:
        pass


def test_earnings_session_field():
    e = Earnings(_sym(), "Q2 2026", datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc), TradingSession.BMO)
    assert e.session is TradingSession.BMO
    assert e.eps_actual is None


def test_news_article_pit_as_of():
    a = NewsArticle(id="1", title="t", url="u", source="s",
                    published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    pit=PointInTime(available_from=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
                                    fetched_at=datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc)))
    b = NewsArticle(id="2", title="t2", url="u2", source="s",
                    published_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
                    pit=PointInTime(available_from=datetime(2026, 3, 1, tzinfo=timezone.utc),
                                    fetched_at=datetime(2026, 3, 1, tzinfo=timezone.utc)))
    got = as_of([a, b], datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert got is a
    got2 = as_of([a, b], datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert got2 is UNAVAILABLE


def test_signal_is_mutable_view():
    s = Signal(id="s1", ticker="AAPL", action="BUY", entry=100.0, entry_price=100.0,
              created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    s.meta["variant"] = "v2"  # mutable
    assert s.meta["variant"] == "v2"


def test_all_15_entity_types_importable():
    # The 15 §27 entities: value types (Symbol/Exchange/Currency/Timezone-via-Exchange.tz/
    # TradingSession) + data entities. Confirm all are present.
    for t in (OHLCVBar, CorporateAction, Earnings, AnalystEstimate, Fundamentals,
              OptionContract, OptionsChain, NewsArticle, SECFiling, Signal):
        assert t is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_data_model_entities.py -v`
Expected: FAIL — `ImportError: cannot import name 'OHLCVBar'`

- [ ] **Step 3: Append the data entities to `core/data_model/entities.py`**

Add imports at top of `entities.py` (after existing imports):
```python
from datetime import datetime
from typing import Any
from core.data_model.pit import PointInTime
```

Append the data entities:
```python
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
    """SEC filing (§27). Fetcher in Phase 10."""
    symbol: Optional[Symbol]
    form: str
    filed_at: datetime
    accession_no: str
    url: str
    pit: Optional[PointInTime] = None


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
    meta: dict = None

    def __post_init__(self):
        if self.meta is None:
            object.__setattr__(self, "meta", {}) if False else None
            self.meta = {}
```

Note: `Signal` is intentionally mutable (the existing signals dict is mutated); `meta` defaults handled in `__post_init__` because mutable defaults can't be inline on a non-frozen dataclass field safely — use `None` sentinel then set `{}`.

Update `core/data_model/__init__.py` exports to add the 10 entity types to the import and `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_data_model_entities.py -v`
Expected: PASS (all, incl. the 5 new ones)

- [ ] **Step 5: Commit**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add core/data_model/entities.py core/data_model/__init__.py tests/unit/test_data_model_entities.py
git commit -m "feat(v9-phase1a): canonical entity types — OHLCV/Earnings/News/Options/SEC/Signal + 5 more (§27)"
```

---

### Task 4: `core/data_model/quality.py` — DATA vs SIGNAL quality contract (§33)

**Files:**
- Create: `core/data_model/quality.py`
- Create: `tests/unit/test_data_model_quality.py`
- Modify: `core/data_model/__init__.py` (export quality symbols)

**Interfaces:**
- Produces: `Conflict`, `DataQuality`, `SignalQuality`, `QualityEnvelope`.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_data_model_quality.py`:
```python
from datetime import datetime, timezone, timedelta
from core.data_model.quality import Conflict, DataQuality, SignalQuality, QualityEnvelope

UTC = timezone.utc


def test_conflict_immutable():
    c = Conflict(field="price", values=[100.0, 101.5], sources=["yahoo", "alpaca"], deviation_pct=1.5)
    assert c.field == "price"
    assert c.deviation_pct == 1.5


def test_data_quality_fields():
    q = DataQuality(source="yahoo", timestamp=datetime(2026, 8, 11, tzinfo=UTC),
                    freshness=timedelta(seconds=30), coverage=0.95, stale=False,
                    conflicts=(Conflict("price", [100.0, 101.5], ["yahoo", "alpaca"], 1.5),),
                    quality_flags=frozenset({"CROSS_SOURCE"}))
    assert q.coverage == 0.95
    assert "CROSS_SOURCE" in q.quality_flags
    assert q.conflicts[0].field == "price"


def test_signal_quality_metrics():
    sq = SignalQuality(win_rate=0.6, profit_factor=1.8, avg_R=0.4, expectancy_R=0.3,
                       max_drawdown=0.12, sample_size=50)
    assert sq.sample_size == 50
    assert sq.alerts == ()


def test_quality_envelope_carries_both():
    q = DataQuality(source="yahoo", timestamp=datetime(2026, 8, 11, tzinfo=UTC))
    sq = SignalQuality(win_rate=0.5, profit_factor=1.5, avg_R=0.3, expectancy_R=0.2,
                       max_drawdown=0.1, sample_size=20)
    env = QualityEnvelope(data=q, signal=sq)
    assert env.data.source == "yahoo"
    assert env.signal.sample_size == 20
    # DATA and SIGNAL quality are explicitly separate (§33)
    assert env.data is not env.signal
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_data_model_quality.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.data_model.quality'`

- [ ] **Step 3: Write minimal implementation**

`core/data_model/quality.py`:
```python
"""DATA-quality vs SIGNAL/ANALYSIS-quality contract (spec §33).

Every important analysis exposes: source, timestamp, freshness, coverage,
stale, conflicts, quality_flags. DATA quality is separate from SIGNAL/ANALYSIS
quality — a record may carry both side-by-side via QualityEnvelope.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, tuple


@dataclass(frozen=True)
class Conflict:
    """A cross-source disagreement on a single field (folds core.price_integrity output)."""
    field: str
    values: tuple
    sources: tuple
    deviation_pct: Optional[float] = None


@dataclass(frozen=True)
class DataQuality:
    """DATA quality (§33). Distinct from SIGNAL/ANALYSIS quality."""
    source: str
    timestamp: datetime
    freshness: Optional[timedelta] = None
    coverage: Optional[float] = None      # 0..1 fraction of requested universe served
    stale: bool = False
    conflicts: tuple = ()                 # tuple[Conflict, ...]
    quality_flags: frozenset = frozenset()


@dataclass(frozen=True)
class SignalQuality:
    """SIGNAL/ANALYSIS quality (§33). Mirrors core.signal_quality metrics."""
    win_rate: float
    profit_factor: float
    avg_R: float
    expectancy_R: float
    max_drawdown: float
    sample_size: int
    alerts: tuple = ()


@dataclass(frozen=True)
class QualityEnvelope:
    """Carries DATA quality and SIGNAL/ANALYSIS quality side-by-side (§33 separation)."""
    data: Optional[DataQuality] = None
    signal: Optional[SignalQuality] = None
```

Update `core/data_model/__init__.py` to import `Conflict, DataQuality, SignalQuality, QualityEnvelope` and add to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_data_model_quality.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add core/data_model/quality.py core/data_model/__init__.py tests/unit/test_data_model_quality.py
git commit -m "feat(v9-phase1a): core/data_model.quality — DataQuality vs SignalQuality (§33)"
```

---

### Task 5: `core/data_quality.py` — QualifiedValue additive fields (backward-compatible)

**Files:**
- Modify: `core/data_quality.py` (`QualifiedValue` gains optional fields; `to_dict()` extended)
- Modify: `tests/unit/test_data_quality_module.py` (add new-field tests; do NOT change existing assertions)

**Interfaces:**
- Evolves: `QualifiedValue` (adds `freshness`, `coverage`, `conflicts`, `quality_flags` with defaults).
- Consumes: `core.data_model.quality.Conflict` (Task 4).

**Backward-compat:** all existing `QualifiedValue(...)` and factory calls pass positional `value, level, source, timestamp[, message]`; new fields have defaults so every existing call site is unaffected. Existing `test_data_quality_module.py` assertions must stay green.

- [ ] **Step 1: Write the failing test (append to `tests/unit/test_data_quality_module.py`)**

First READ `tests/unit/test_data_quality_module.py` to confirm its existing assertions; append new tests without editing existing ones:
```python
from datetime import timedelta
from core.data_model.quality import Conflict


def test_qualified_value_new_fields_default_none():
    qv = QualifiedValue.verified(100.0, "yahoo")
    assert qv.freshness is None
    assert qv.coverage is None
    assert qv.conflicts is None
    assert qv.quality_flags == frozenset()


def test_qualified_value_optional_fields_set():
    qv = QualifiedValue(100.0, QualityLevel.VERIFIED, "yahoo",
                        datetime.now(timezone.utc),
                        freshness=timedelta(seconds=15),
                        coverage=0.9,
                        conflicts=(Conflict("price", (100.0, 101.5), ("yahoo", "alpaca"), 1.5),),
                        quality_flags=frozenset({"CROSS_SOURCE"}))
    d = qv.to_dict()
    assert d["freshness"] == 15.0
    assert d["coverage"] == 0.9
    assert "CROSS_SOURCE" in d["quality_flags"]
    assert d["conflicts"][0]["field"] == "price"


def test_qualified_value_existing_factory_unchanged():
    # Backward-compat: factories still take the same args.
    qv = QualifiedValue.stale(100.0, "yahoo", 120)
    assert qv.level is QualityLevel.STALE
    assert qv.freshness is None
    assert qv.is_valid() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_data_quality_module.py -v`
Expected: FAIL — `TypeError: QualifiedValue() got an unexpected keyword argument 'freshness'`

- [ ] **Step 3: Evolve `QualifiedValue` in `core/data_quality.py` (additive only)**

Add import near top:
```python
from datetime import timedelta
```
Edit the `QualifiedValue` dataclass to add optional fields after `message`:
```python
@dataclass
class QualifiedValue:
    """A wrapper for values that guarantees traceability of data quality and origin."""
    value: Any
    level: QualityLevel
    source: str
    timestamp: datetime
    message: Optional[str] = None
    freshness: Optional[timedelta] = None
    coverage: Optional[float] = None
    conflicts: Optional[list] = None
    quality_flags: frozenset = frozenset()
```
Edit `to_dict()` to include the new fields when set:
```python
    def to_dict(self) -> dict:
        d = {
            "value": self.value,
            "level": self.level.value,
            "source": self.source,
            "timestamp": self.timestamp.isoformat(),
            "message": self.message,
            "is_valid": self.is_valid(),
        }
        if self.freshness is not None:
            d["freshness"] = self.freshness.total_seconds()
        if self.coverage is not None:
            d["coverage"] = self.coverage
        if self.conflicts:
            d["conflicts"] = [
                {"field": c.field, "values": list(c.values), "sources": list(c.sources),
                 "deviation_pct": c.deviation_pct}
                for c in self.conflicts
            ]
        if self.quality_flags:
            d["quality_flags"] = sorted(self.quality_flags)
        return d
```
Do NOT change the factory classmethods (`verified/estimated/stale/unavailable`) — they keep their signatures; new fields default. Do NOT change `DataHealthDashboard`.

- [ ] **Step 4: Run tests to verify pass + backward-compat**

Run: `python -m pytest tests/unit/test_data_quality_module.py tests/unit/test_data_quality_invariants.py -v`
Expected: PASS (new tests pass; all existing assertions still pass — backward-compat proof).

- [ ] **Step 5: Commit**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add core/data_quality.py tests/unit/test_data_quality_module.py
git commit -m "feat(v9-phase1a): QualifiedValue additive §33 fields (freshness/coverage/conflicts/quality_flags), backward-compatible"
```

---

### Task 6: `core/market_hours.py` evolution — session model + earnings_session_reported + fold duplicates (§28)

**Files:**
- Modify: `core/market_hours.py` (extend `MarketHoursStatus`; add `_classify_session`, `earnings_session_reported`; wire `EXCHANGES`)
- Modify: `agents/premarket_scanner_agent.py` (fold `_is_us_market_open` + `_earnings_already_reported` to delegate)
- Create: `tests/unit/test_market_calendar_sessions.py`

**Interfaces:**
- Evolves: `MarketHoursStatus` (additive: `session`, `is_half_day`, `exchange`), `us_equity_market_status` (returns new fields; existing fields unchanged), new `earnings_session_reported(session_hint, now_utc) -> bool`.
- Consumes: `core.data_model.entities.TradingSession`, `EXCHANGES` (Task 1).

**Backward-compat:** existing `MarketHoursStatus.is_open/reason/now_et/open_time_et/close_time_et` values are identical for every input. `test_market_hours_guard.py` and the premarket agent tests must stay green.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_market_calendar_sessions.py`:
```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from core.market_hours import (
    us_equity_market_status, earnings_session_reported, MarketHoursStatus,
)
from core.data_model.entities import TradingSession

UTC = timezone.utc
ET = ZoneInfo("America/New_York")


def _et(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


def test_regular_session():
    # Wednesday 2026-08-12 11:00 ET (not a holiday)
    s = us_equity_market_status(_et(2026, 8, 12, 11, 0))
    assert s.is_open is True
    assert s.session is TradingSession.REGULAR
    assert s.is_half_day is False
    assert s.exchange == "XNYS"


def test_premarket_session():
    s = us_equity_market_status(_et(2026, 8, 12, 8, 0))
    assert s.is_open is False
    assert s.session is TradingSession.PRE_MARKET


def test_after_hours_session():
    s = us_equity_market_status(_et(2026, 8, 12, 17, 0))
    assert s.is_open is False
    assert s.session is TradingSession.AFTER_HOURS


def test_weekend_closed():
    s = us_equity_market_status(_et(2026, 8, 15, 11, 0))  # Saturday
    assert s.is_open is False
    assert s.session is TradingSession.CLOSED
    assert s.reason == "weekend"


def test_holiday_closed():
    # 2026-01-01 is Thursday; observed New Year -> holiday
    s = us_equity_market_status(_et(2026, 1, 1, 11, 0))
    assert s.is_open is False
    assert s.session is TradingSession.CLOSED


def test_half_day_early_close():
    # 2026-07-03 is Friday; July 4 is Saturday -> early close on Jul 3
    s = us_equity_market_status(_et(2026, 7, 3, 12, 30))
    assert s.is_open is True
    assert s.is_half_day is True
    assert s.session is TradingSession.REGULAR


def test_earnings_session_reported_bmo():
    # BMO reported once past 14:30 UTC
    assert earnings_session_reported("bmo", datetime(2026, 8, 12, 14, 29, tzinfo=UTC)) is False
    assert earnings_session_reported("bmo", datetime(2026, 8, 12, 14, 30, tzinfo=UTC)) is True


def test_earnings_session_reported_amc():
    assert earnings_session_reported("amc", datetime(2026, 8, 12, 21, 59, tzinfo=UTC)) is False
    assert earnings_session_reported("amc", datetime(2026, 8, 12, 22, 0, tzinfo=UTC)) is True


def test_earnings_session_reported_unknown_kept():
    assert earnings_session_reported("nonsense", datetime(2026, 8, 12, 23, 0, tzinfo=UTC)) is False


def test_status_is_a_market_hours_status():
    s = us_equity_market_status(_et(2026, 8, 12, 11, 0))
    assert isinstance(s, MarketHoursStatus)
    # existing fields preserved
    assert hasattr(s, "now_et") and hasattr(s, "open_time_et") and hasattr(s, "close_time_et")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_market_calendar_sessions.py -v`
Expected: FAIL — `AttributeError: 'MarketHoursStatus' object has no attribute 'session'` (or `ImportError` for `earnings_session_reported`)

- [ ] **Step 3: Evolve `core/market_hours.py` (additive)**

Add imports at top:
```python
from core.data_model.entities import TradingSession, EXCHANGES
```
Change `MarketHoursStatus` to add the three new fields (after existing fields):
```python
@dataclass(frozen=True)
class MarketHoursStatus:
    is_open: bool
    reason: str
    now_et: datetime
    open_time_et: datetime
    close_time_et: datetime
    session: TradingSession = TradingSession.CLOSED
    is_half_day: bool = False
    exchange: str = "XNYS"
```
Add pre/after-hours session constants near the existing ones:
```python
US_EQUITY_PRE_OPEN = time(4, 0)
US_EQUITY_AFTER_CLOSE = time(20, 0)
```
Add a session classifier and rewrite `us_equity_market_status` to set `session`/`is_half_day`/`exchange` while keeping the existing `is_open`/`reason`/`*_et` values identical:
```python
def _classify_session(now_et: datetime, open_dt: datetime, close_dt: datetime, half_day: bool) -> TradingSession:
    if now_et < open_dt:
        if now_et.time() >= US_EQUITY_PRE_OPEN:
            return TradingSession.PRE_MARKET
        return TradingSession.CLOSED
    if now_et <= close_dt:
        return TradingSession.REGULAR
    if now_et.time() <= US_EQUITY_AFTER_CLOSE:
        return TradingSession.AFTER_HOURS
    return TradingSession.CLOSED


def us_equity_market_status(now: Optional[datetime] = None) -> MarketHoursStatus:
    raw_now = now or datetime.now(EASTERN_TZ)
    now_et = raw_now.astimezone(EASTERN_TZ) if raw_now.tzinfo else raw_now.replace(tzinfo=EASTERN_TZ)
    day = now_et.date()
    open_dt = datetime.combine(day, US_EQUITY_OPEN, EASTERN_TZ)
    early = _us_equity_early_close_date(day)
    close_t = US_EQUITY_EARLY_CLOSE if early else US_EQUITY_CLOSE
    close_dt = datetime.combine(day, close_t, EASTERN_TZ)

    override = _override_status()
    if override is not None:
        return MarketHoursStatus(override, "override", now_et, open_dt, close_dt,
                                 TradingSession.REGULAR if override else TradingSession.CLOSED,
                                 early, "XNYS")
    if now_et.weekday() >= 5:
        return MarketHoursStatus(False, "weekend", now_et, open_dt, close_dt,
                                 TradingSession.CLOSED, early, "XNYS")
    if day in _us_equity_holidays(day.year):
        return MarketHoursStatus(False, "us_equity_holiday", now_et, open_dt, close_dt,
                                 TradingSession.CLOSED, early, "XNYS")
    session = _classify_session(now_et, open_dt, close_dt, early)
    is_open = session is TradingSession.REGULAR
    if now_et < open_dt:
        reason = "before_regular_session"
    elif now_et > close_dt:
        reason = "after_regular_session"
    else:
        reason = "regular_session"
    return MarketHoursStatus(is_open, reason, now_et, open_dt, close_dt, session, early, "XNYS")
```
Add the earnings-session policy helper (canonical home for the BMO/AMC thresholds):
```python
EARNINGS_BMO_REPORTED_UTC_MINUTES = 14 * 60 + 30   # 14:30 UTC = latest US market open (DST-safe)
EARNINGS_AMC_REPORTED_UTC_MINUTES = 22 * 60       # 22:00 UTC = after the bulk AMC print


def earnings_session_reported(session_hint: str, now_utc: datetime) -> bool:
    """Same-day earnings reported check by BMO/AMC session hint (§28).

    BMO is reported once past the latest US market open (14:30 UTC); AMC once
    past the after-hours bulk print (22:00 UTC). Unknown session -> not
    reported (keep — do not silently drop an ambiguous catalyst)."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    s = str(session_hint or "").lower().strip()
    mins = now_utc.hour * 60 + now_utc.minute
    if s in ("bmo", "pre-market", "premarket"):
        return mins >= EARNINGS_BMO_REPORTED_UTC_MINUTES
    if s in ("amc", "after_hours", "afterhours", "aft"):
        return mins >= EARNINGS_AMC_REPORTED_UTC_MINUTES
    return False
```
(Add `from datetime import timezone` to the imports if not already present.)

- [ ] **Step 4: Fold the duplicates in `agents/premarket_scanner_agent.py`**

Find the `_is_us_market_open` static method (around line 1848) and replace its body to delegate to `market_hours`:
```python
    @staticmethod
    def _is_us_market_open(now_utc: Optional[datetime] = None) -> bool:
        from core.market_hours import us_equity_market_status
        return us_equity_market_status(now_utc or _utc_now()).is_open
```
Find `_earnings_already_reported` (around line 1340) and replace ONLY the same-day session branch (the final `return False` after the `session = ...` block) to delegate:
```python
        # Same calendar day -> resolve by session hint (delegated to market_hours, §28).
        return earnings_session_reported(session, now_utc)
```
Add the import near the top of the file with the other market_hours imports (or at the top of the method if cleaner): `from core.market_hours import earnings_session_reported`. Keep the date comparison (`ev_date > today` / `ev_date < today`) and `_parse_iso` exactly as-is — only the same-day branch delegates. Behavior is identical.

- [ ] **Step 5: Run tests to verify pass + backward-compat**

Run: `python -m pytest tests/unit/test_market_calendar_sessions.py tests/unit/test_market_hours_guard.py tests/unit/test_premarket_volume_and_earnings_stale.py -v`
Expected: PASS (new session tests pass; existing market-hours-guard + premarket-stale tests still green — backward-compat proof).

Then run the broader premarket agent suite to confirm the fold didn't regress:
Run: `python -m pytest tests/unit/test_premarket_earnings_elimination.py tests/unit/test_premarket_pulse_ai.py -q`
Expected: PASS (unchanged).

- [ ] **Step 6: Commit**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add core/market_hours.py agents/premarket_scanner_agent.py tests/unit/test_market_calendar_sessions.py
git commit -m "refactor(v9-phase1a): market_hours session model (§28) + earnings_session_reported; fold premarket duplicates"
```

---

### Task 7: `core/news_evidence_ledger.py` typed `as_of` reader (§29/§32)

**Files:**
- Modify: `core/news_evidence_ledger.py` (add `read_articles_as_of(T)`)
- Create: `tests/unit/test_news_evidence_ledger_pit.py`

**Interfaces:**
- Produces: `read_articles_as_of(T) -> list[NewsArticle]` (canonical typed reader).
- Consumes: `core.data_model.entities.NewsArticle`, `core.data_model.pit.PointInTime` (Tasks 2–3).

**Note:** READ `core/news_evidence_ledger.py` first to confirm the JSONL event shape (`observed_at`, `analyzed_at`, article fields) before implementing the reader. The JSONL format is NOT changed; only a typed reader is added.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_news_evidence_ledger_pit.py`:
```python
import json, tempfile, os
from datetime import datetime, timezone
from core.data_model.entities import NewsArticle
from core.data_model.pit import UNAVAILABLE

UTC = timezone.utc


def test_read_articles_as_of_filters_by_available_from(tmp_path, monkeypatch):
    # Point the ledger at a temp file with two article_observed events.
    ledger_file = tmp_path / "news_evidence_ledger.jsonl"
    with open(ledger_file, "w") as f:
        f.write(json.dumps({"event": "article_observed", "observed_at": "2026-01-01T12:00:00+00:00",
                            "analyzed_at": "2026-01-01T12:00:05+00:00",
                            "article": {"id": "a1", "title": "t1", "url": "u1", "source": "s",
                                        "published_at": "2026-01-01T11:00:00+00:00"}}) + "\n")
        f.write(json.dumps({"event": "article_observed", "observed_at": "2026-03-01T12:00:00+00:00",
                            "analyzed_at": "2026-03-01T12:00:05+00:00",
                            "article": {"id": "a2", "title": "t2", "url": "u2", "source": "s",
                                        "published_at": "2026-03-01T11:00:00+00:00"}}) + "\n")
    monkeypatch.setattr("core.news_evidence_ledger.LEDGER_PATH", str(ledger_file))
    from core.news_evidence_ledger import read_articles_as_of
    articles = read_articles_as_of(datetime(2026, 2, 1, tzinfo=UTC))
    assert len(articles) == 1
    assert isinstance(articles[0], NewsArticle)
    assert articles[0].id == "a1"
    # as_of over the returned list must return UNAVAILABLE before any available_from
    from core.data_model.pit import as_of
    assert as_of(articles, datetime(2020, 1, 1, tzinfo=UTC)) is UNAVAILABLE


def test_read_articles_as_of_empty_when_none(tmp_path, monkeypatch):
    ledger_file = tmp_path / "news_evidence_ledger.jsonl"
    ledger_file.write_text("")
    monkeypatch.setattr("core.news_evidence_ledger.LEDGER_PATH", str(ledger_file))
    from core.news_evidence_ledger import read_articles_as_of
    assert read_articles_as_of(datetime(2026, 6, 1, tzinfo=UTC)) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_news_evidence_ledger_pit.py -v`
Expected: FAIL — `ImportError: cannot import name 'read_articles_as_of'`

- [ ] **Step 3: Implement the reader**

READ `core/news_evidence_ledger.py` to confirm the module's existing structure (the `LEDGER_PATH` constant and the JSONL event shape). Then add:
```python
def read_articles_as_of(T):
    """Typed point-in-time reader (§29/§32). Returns canonical NewsArticle records
    whose `available_from` (analyzed_at) <= T. The JSONL format is unchanged."""
    from core.data_model.entities import NewsArticle
    from core.data_model.pit import PointInTime, to_utc
    out = []
    try:
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("event") != "article_observed":
                    continue
                art = rec.get("article") or {}
                observed = _parse_iso_ts(rec.get("observed_at"))
                analyzed = _parse_iso_ts(rec.get("analyzed_at"))
                if analyzed is None or to_utc(analyzed) > to_utc(T):
                    continue
                pit = PointInTime(
                    event_time=_parse_iso_ts(art.get("published_at")),
                    received_at=observed,
                    available_from=analyzed,
                    fetched_at=observed,
                    version=0,
                )
                out.append(NewsArticle(
                    id=str(art.get("id")),
                    title=str(art.get("title", "")),
                    url=str(art.get("url", "")),
                    source=str(art.get("source", "")),
                    published_at=_parse_iso_ts(art.get("published_at")) or observed,
                    pit=pit,
                ))
    except FileNotFoundError:
        return []
    return out
```
Add a small `_parse_iso_ts` helper if one does not already exist in the module:
```python
def _parse_iso_ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
```
(Reuse the module's existing ISO parser if one already exists — do not duplicate.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_news_evidence_ledger_pit.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add core/news_evidence_ledger.py tests/unit/test_news_evidence_ledger_pit.py
git commit -m "feat(v9-phase1a): news_evidence_ledger typed read_articles_as_of reader (§29/§32)"
```

---

### Task 8: Phase 1A exit gate + control-doc updates

**Files:**
- Modify: `docs/V9_IMPLEMENTATION.md` (Phase 1A record/note; Phase 1 stays IN PROGRESS)
- Modify: `docs/V9_ARCHITECTURE.md` (canonical data layer note)
- Verify only: V7 untouched, regression green, no new deps, no push.

- [ ] **Step 1: Verify V7 untouched (§117)**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium"
echo "head=$(git rev-parse --short HEAD) dirty=$(git status --short | wc -l)"
```
Expected: `head=f912a33 dirty=109` (unchanged). If not, STOP — 1A = NOT DONE.

- [ ] **Step 2: Verify no new open-source deps added (§118)**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git diff 664ce0d HEAD -- requirements.txt requirements-dev.txt
```
Expected: empty (no dep changes). Confirm `core/data_model/` imports only stdlib + existing modules (`core.config`, `core.market_hours` existing). If any new dep appears, STOP.

- [ ] **Step 3: Run the full regression suite (gate 7, §115)**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf -q 2>&1 | tail -5
```
Expected: all passed, 0 failed, exit 0 (the 1559 baseline + the new 1A tests; 0 new failures, no test weakened). Record the exact count. If new failures exist, STOP — 1A = NOT DONE.

- [ ] **Step 4: Update `docs/V9_ARCHITECTURE.md` — canonical data layer note**

Add a short section (after the existing architecture diagram/§26 area) noting the new canonical data layer. Keep it factual and status-vocab (§9):
```markdown
## Canonical data layer (Phase 1A)

`core/data_model/` is V9's single source of truth for data entity types (spec §27): `entities.py` (AssetClass/Currency/TradingSession enums, Exchange/Symbol, EXCHANGES registry, and the 15 entity dataclasses OHLCV/CorporateAction/Earnings/AnalystEstimate/Fundamentals/OptionContract/OptionsChain/NewsArticle/SECFiling/Signal), `pit.py` (PointInTime + `as_of`/`UNAVAILABLE` — the §32 no-synthetic-historical-facts guard), `quality.py` (DataQuality vs SignalQuality, §33). `core/market_hours.py` evolved with a session model (pre/regular/after-hours/BMO/AMC/closed, §28). Provider rewiring (1B) and append-only historical storage (1B) are NOT STARTED. Options/SEC/Fundamentals fetchers are later phases (types defined here).
```

- [ ] **Step 5: Update `docs/V9_IMPLEMENTATION.md` — Phase 1A record**

Under the Phase 1 row (IN PROGRESS), add a Phase 1A sub-record (do NOT change Phase 1 row status — it stays IN PROGRESS through 1B/1C):
```markdown
## Phase 1A — Canonical Data Model & Point-in-Time Contract
- **Status:** PASS — 1A gate verified (V7 untouched, no new deps, regression green, 8 tasks complete).
- **Delivered:** `core/data_model/` (entities + pit + quality); `market_hours` session model + `earnings_session_reported`; `QualifiedValue` additive §33 fields (backward-compatible); `news_evidence_ledger.read_articles_as_of` typed reader.
- **Out of scope (deferred):** provider `DataProvider` ABC + append-only storage → 1B; corporate actions + survivorship registry → 1C; Options/SEC/Fundamentals fetchers → Phase 5/10; `exchange_calendars` dep → Phase 2.
```
(If any gate failed, set status `FAILED` and list exact failing items — spec §122. Do not claim PASS you did not earn.)

- [ ] **Step 6: Commit (no remote push, §116)**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add docs/V9_IMPLEMENTATION.md docs/V9_ARCHITECTURE.md
git commit -m "docs(v9-phase1a): Phase 1A = PASS — exit gate verified; architecture + implementation records"
git log --oneline | head -12
```

- [ ] **Step 7: Report the 1A result plainly (§4)**

State: "Phase 1A = PASS. Canonical data layer + PIT contract + market-calendar session model delivered; 1559+ regression green; V7 untouched; no new deps. Next: Phase 1B — Provider Normalization & Historical Storage." (Adjust if any gate failed.)

---

## Self-Review

**Spec coverage:** §27 (15 entities) → Task 1+3. §28 (calendar/sessions/BMO/AMC) → Task 6. §29 (PIT timestamps) → Task 2. §30 (survivorship) → explicitly deferred to 1C (documented in design + out-of-scope). §31 (corporate actions) → entity type in Task 3; capture/adjustment deferred to 1C. §32 (no synthetic facts) → Task 2 `as_of`/`UNAVAILABLE`. §33 (data quality + separation) → Tasks 4+5. §5/§115/§116/§117/§118 → Global Constraints + gate (Task 8). Covered; deferrals explicit.

**Placeholder scan:** `# populate in later phase` markers are sanctioned living-doc stubs with a phase owner (not forbidden placeholders). No "TBD"/"TODO" without an owner. Code blocks contain complete implementations.

**Type consistency:** `PointInTime` (Task 2) consumed by entities (Task 3) and the news reader (Task 7) — same field names (`available_from`, `version`). `TradingSession` (Task 1) consumed by `market_hours` (Task 6) and `Earnings` (Task 3) — same enum. `Conflict` (Task 4) consumed by `QualifiedValue.conflicts` (Task 5) — same type. `as_of` operates duck-typed on `.pit` across entities — consistent. No naming drift.

**Scope check:** 8 tasks, each a coherent independently-testable unit. Provider rewiring (1B) and storage migration (1B) and corporate-actions/survivorship (1C) are cleanly excluded. Not too large for one plan.

**Ambiguity check:** "additive only" is enforced by the backward-compat tests (existing assertions unchanged). `UNAVAILABLE` is a singleton sentinel with `__bool__=False`. `earnings_session_reported` thresholds match the existing `_earnings_already_reported` constants (14:30/22:00 UTC). The fold is behavior-identical (existing premarket tests gate it).