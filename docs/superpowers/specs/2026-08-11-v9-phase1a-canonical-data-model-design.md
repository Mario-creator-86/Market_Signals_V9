# Phase 1A — Canonical Data Model & Point-in-Time Contract (Design)

> **Status:** design (autonomous). Spec basis: `prompt_to_follow.md` §5 (no rewrite — evolve V7), §27–§33 (data foundation), §115 (no test weakening), §116 (no push), §117 (V7 read-only), §118 (phase order — Phase 2 = open-source deps, so Phase 1A adds **no new open-source dependencies**).
> **Parent:** Phase 1 — Data & Architecture Foundation. This is the first of three Phase 1 sub-plans (1A canonical model + PIT contract + calendar; 1B provider ABC + historical storage; 1C corporate actions + survivorship registry).

## Goal

Establish the canonical data layer as V9's single source of truth for data entities, the point-in-time contract that makes "no synthetic historical facts" (§32) an executable guard rather than a convention, the DATA-quality vs SIGNAL-quality separation (§33), and a real market-calendar/session model (§28) — all as a **backward-compatible evolution** of the migrated V7 code, keeping the 1559-test green baseline green.

## Architecture

A new `core/data_model/` package holds the canonical types and contracts (focused modules, one concern each). `core/market_hours.py` is evolved in place into a real session/calendar module. `core/data_quality.py` gains additive optional fields. No provider is rewired in 1A (that is 1B); no storage is migrated to append-only in 1A (that is 1B). 1A delivers the **contract** that 1B/1C and all later phases build on, plus a typed reader for the one append-only store that already exists (`news_evidence_ledger`).

**Hard constraints**
- §5: evolve V7, no rewrite — existing public signatures stay; changes are additive.
- §115: no test weakening — existing `test_market_hours_guard.py`, `test_data_quality_module.py`, `test_data_quality_invariants.py` and the 1559 baseline must stay green unchanged.
- §118: no new open-source deps in Phase 1 — market calendar stays hand-coded (algorithmic US-equity holidays + extensible exchange table); `exchange_calendars` adoption is deferred to Phase 2.
- §117: V7 read-only.
- §116: no remote push.
- §4: no forbidden success language.

## Tech Stack

Python 3.14.3, `dataclasses` (stdlib), `zoneinfo` (stdlib, already used), `pytz` (already installed, used in `config.py`), `pytest` (existing). No new packages.

---

## File structure (what 1A creates/evolves)

**New package `core/data_model/`:**
- `__init__.py` — public exports.
- `entities.py` — enums + the 15 §27 entity dataclasses.
- `pit.py` — `PointInTime`, `UNAVAILABLE`, `as_of()`, `to_utc()`, `assert_utc()`.
- `quality.py` — `DataQuality`, `SignalQuality`, `QualityEnvelope`.

**Evolved in place:**
- `core/market_hours.py` — add `session`/`is_half_day`/`exchange` to `MarketHoursStatus`; add pre/after-hours + BMO/AMC session model; add `EXCHANGES` table (populated: US equities NYSE/NASDAQ → `America/New_York`/USD; crypto → 24/7); keep algorithmic US-equity holidays (no new dep); add `earnings_session(report_time_utc) -> TradingSession`.
- `core/data_quality.py` — add optional `freshness`, `coverage`, `conflicts`, `quality_flags` to `QualifiedValue`; add `DataQuality` dataclass (re-exported from `core/data_model/quality.py` to avoid a hard cycle; `core/data_model/quality.py` is canonical, `data_quality.py` keeps its existing class and composes the new fields).

**Folded duplicate:**
- `agents/premarket_scanner_agent.py` `_is_us_market_open` → replaced with a call to `core/market_hours.us_equity_market_status()` (behavior identical); `_earnings_already_reported` BMO/AMC logic → replaced with `core/market_hours.earnings_session()`.

**Typed reader (no storage change):**
- `core/news_evidence_ledger.py` — add a `read_articles_as_of(T) -> list[canonical NewsArticle+PIT]` reader that maps `observed_at`→`received_at`, `analyzed_at`→`available_from`. The JSONL format is unchanged; only a typed reader is added.

**Tests (new, TDD):**
- `tests/unit/data_model/test_entities.py` — construction, frozen, asset-class enum, exchange registry lookups.
- `tests/unit/data_model/test_pit.py` — `as_of` returns newest qualifying record; returns `UNAVAILABLE` when no record ≤ T; `to_utc`/`assert_utc` on naive/aware/local; `PointInTime` field defaults.
- `tests/unit/data_model/test_quality.py` — `DataQuality`/`SignalQuality` envelopes; `QualifiedValue` new optional fields default to None/empty (backward compat).
- `tests/unit/test_market_calendar_sessions.py` — pre/regular/after-hours boundaries, BMO/AMC mapping, half-day early-close, weekend, holiday, crypto 24/7, unknown-exchange stub.
- Existing `test_market_hours_guard.py`, `test_data_quality_module.py`, `test_data_quality_invariants.py` unchanged and must pass (backward-compat proof).

---

## Section 1 — Canonical entity model (`core/data_model/entities.py`)

**Enums / value types**
- `AssetClass` — `US_STOCKS`, `CRYPTO_FUTURES`, `FOREX_MAJORS`, `COMMODITIES` (values match the existing `strategy_v2/types.py` string constants; `strategy_v2/types.py` is **not** changed in 1A — `entities.AssetClass` is the canonical going forward, existing constants remain valid).
- `Currency` — ISO 4217 (USD, EUR, GBP, JPY, …).
- `TradingSession` — `PRE_MARKET`, `REGULAR`, `AFTER_HOURS`, `BMO`, `AMC`, `CLOSED`, `CRYPTO_CONTINUOUS`.
- `Exchange` dataclass — `mic` (MIC, e.g. `XNYS`), `name`, `tz` (IANA), `asset_class`, `currency`. `EXCHANGES` registry dict populated for NYSE/NASDAQ (US equities) and crypto exchanges (24/7); other MICs stubbed with a documented `# populate in later phase` marker.
- `Symbol` dataclass — `ticker`, `exchange: Exchange`, `asset_class`, `currency`, `isin: str | None`. `core/config.py:normalize_ticker` reused for string normalization.

**Data entities (dataclasses; frozen where immutable)**
- `OHLCVBar` (frozen) — `symbol`, `timestamp` (UTC aware), `open/high/low/close/volume: float`, `adjusted: bool`, `resolution: str` (e.g. `1D`, `5m`). Raw-vs-adjusted is explicit per bar.
- `CorporateAction` (frozen) — `symbol`, `ex_date`, `type` (`SPLIT`/`DIVIDEND`/`TICKER_CHANGE`/`MERGER`), `ratio: float | None`, `amount: float | None`, `from_ticker: str | None`, `to_ticker: str | None`, + PIT.
- `Earnings` (frozen) — `symbol`, `fiscal_period`, `eps_actual: float | None`, `eps_estimated: float | None`, `revenue_actual: float | None`, `report_date`, `session: TradingSession`, + PIT.
- `AnalystEstimate` (frozen) — `symbol`, `metric` (`target_mean`/`target_median`/`recommendation_mean`/`eps_rev`), `value: float`, `as_of` (UTC), `provider`, + PIT.
- `Fundamentals` (frozen) — `symbol`, `as_of`, `market_cap/pe/pb/sector/industry: float | str | None`, + PIT.
- `OptionContract` (frozen) — `symbol`, `expiry`, `strike: float`, `type` (`CALL`/`PUT`), `iv: float | None`, `greeks: dict | None`, `volume: float | None`, + PIT. (Type complete; fetcher in Phase 5.)
- `OptionsChain` (frozen) — `symbol`, `as_of`, `contracts: list[OptionContract]`, + PIT.
- `NewsArticle` (canonical, frozen) — superset of `agents/news_tab_agent.NewsArticle` fields (`id, title, url, source, published_at, ticker, category, sentiment, sentiment_score, sentiment_confidence, impact, impact_score, action_score, article_type, is_duplicate, story_fingerprint, also_reported_by, ai_micro_summary, ai_deep_analysis, summary, related_tickers, is_breaking, is_market_wide, is_crypto, source_scope, sentiment_quality`) + PIT. The existing `agents/news_tab_agent.NewsArticle` is **not** removed in 1A; 1A defines the canonical type and the ledger reader. Migration of the agent to the canonical type is a 1B call-site task (not 1A).
- `SECFiling` (frozen) — `symbol: str | None`, `form` (`10-K`/`10-Q`/`8-K`/…), `filed_at`, `accession_no`, `url`, + PIT. (Type complete; fetcher in Phase 10.)
- `Signal` (canonical, mutable) — typed view over the existing signal dict fields + PIT (`created_at`→PIT.event_time, `closed_at`→PIT when closed). The existing `core/signals.py` dict shape is **not** changed in 1A; `Signal` is a typed wrapper/reader.

`PointInTime` (Section 2) is composed in via a frozen base dataclass; entities declare `pit: PointInTime` where applicable. For entities where PIT is always required (OHLCVBar's `timestamp` already is the PIT `event_time`), a helper `pit_from_bar(bar)` builds the `PointInTime`.

## Section 2 — Point-in-time contract (`core/data_model/pit.py`)

```python
@dataclass(frozen=True)
class PointInTime:
    event_time: datetime | None        # when the event happened (earnings report, filing, bar close)
    publication_time: datetime | None   # when the info was published
    provider_time: datetime | None       # provider's timestamp on the record
    received_at: datetime | None        # when V9 received it
    available_from: datetime | None     # when it became usable (>= publication_time)
    fetched_at: datetime                 # when V9 fetched it (always set, UTC-aware)
    version: int                         # monotonic per (symbol, entity)
```

All datetimes UTC-aware. `to_utc(dt)` = `dt.astimezone(utc)` if aware, else `dt.replace(tzinfo=utc)` (consolidates the per-module tz dances). `assert_utc(dt)` raises on naive. `UNAVAILABLE` is a module-level sentinel singleton — the §32 "missing historical information must remain UNAVAILABLE" value.

**`as_of(records, T, *, prefer="available_from") -> record | UNAVAILABLE`** — returns the record with the greatest `available_from <= T` (ties broken by `version`), or `UNAVAILABLE` when no record qualifies. This is the §32 no-synthetic-historical-facts guard as executable code: through this API it is **impossible** to read a record that wasn't available at T. Callers that bypass `as_of` and fetch "latest" for a historical query are the violation — flagged in 1B when providers route through it.

**1A application:** the canonical entity types carry `PointInTime`; `news_evidence_ledger` gains a typed `read_articles_as_of(T)` reader (maps `observed_at`→`received_at`, `analyzed_at`→`available_from`, returns canonical `NewsArticle`). No storage migration in 1A — `as_of` operates on in-memory record lists and the news ledger.

## Section 3 — DATA vs SIGNAL quality (`core/data_model/quality.py` + `core/data_quality.py`)

§33 requires every important analysis to expose `source, timestamp, freshness, coverage, stale, conflicts, quality_flags`, and to separate DATA quality from SIGNAL/ANALYSIS quality.

**`DataQuality` (frozen dataclass):** `source`, `timestamp` (UTC), `freshness: timedelta | None`, `coverage: float | None` (0..1 fraction of requested universe served, or count), `stale: bool`, `conflicts: list[Conflict] | None`, `quality_flags: frozenset[str]`. `Conflict` = `{field, values: list, sources: list, deviation_pct: float | None}` (folds `core/price_integrity.py` output).

**`SignalQuality` (frozen dataclass):** the existing `core/signal_quality.py` metrics — `win_rate, profit_factor, avg_R, expectancy_R, max_drawdown, sample_size, alerts`. Re-exported/canonical here; `signal_quality.py` keeps its pure functions.

**`QualityEnvelope` (frozen dataclass):** `data: DataQuality | None`, `signal: SignalQuality | None` — the side-by-side carrier that records can hold, making the separation explicit (today only implicit).

**`QualifiedValue` evolution (`core/data_quality.py`, backward-compatible):** add optional fields `freshness: timedelta | None = None`, `coverage: float | None = None`, `conflicts: list[Conflict] | None = None`, `quality_flags: frozenset[str] = frozenset()`. Existing factories (`verified/estimated/stale/unavailable`) keep their signatures (new fields default to None/empty) → all existing call sites and the 1559 baseline are unaffected. `DataHealthDashboard.register` unchanged.

## Section 4 — Market calendar evolution (`core/market_hours.py`)

**`MarketHoursStatus` extended (additive):** add `session: TradingSession`, `is_half_day: bool`, `exchange: str` (MIC). Existing fields (`is_open, reason, now_et, open_time_et, close_time_et`) unchanged.

**Session model (US equities, `America/New_York`):**
- PRE_MARKET 04:00–09:30 ET
- REGULAR 09:30–16:00 ET (unchanged; existing `is_open` maps to `session == REGULAR`)
- AFTER_HOURS 16:00–20:00 ET
- CLOSED otherwise (weekend, holiday, outside 04:00–20:00)
- Half-day early-close 13:00 ET on the existing early-close date patterns → `is_half_day=True`, `session=REGULAR` until 13:00 then `AFTER_HOURS`.
- Existing holiday algorithm retained (no new dep).

**Crypto:** `CRYPTO_CONTINUOUS` session, 24/7, `is_open=True` (crypto already exempt in existing code).

**`earnings_session(report_time_utc) -> TradingSession`:** BMO if reported before 14:30 UTC, AMC if after 22:00 UTC, else REGULAR — consolidates `premarket_scanner_agent._earnings_already_reported`. The agent imports this (duplicate removed).

**`EXCHANGES` table:** from `entities.py`. `us_equity_market_status(now)` now reads the NYSE entry; behavior identical to today (the existing hardcoded `EASTERN_TZ` + holiday algorithm move behind the table entry for NYSE, same outputs). Unknown MIC → `session=CLOSED`, `reason="unknown_exchange"` with a `# populate in later phase` marker.

**Duplicate fold:** `premarket_scanner_agent._is_us_market_open` removed, replaced by `market_hours.us_equity_market_status().is_open`. Existing agent tests must stay green.

## Section 5 — Backward-compat & testing

**Additive-only rule:** new files in `core/data_model/`; `MarketHoursStatus`/`QualifiedValue` gain optional fields with defaults; existing public method signatures unchanged; `strategy_v2/types.py` and `core/signals.py` untouched in 1A.

**TDD order (per entity/contract):** write failing test → run (fail) → implement minimal → run (pass) → commit. Frequent small commits.

**Regression gates (must stay green, unchanged):**
- `tests/unit/test_market_hours_guard.py` — proves `market_hours` evolution didn't change regular-session guard behavior.
- `tests/unit/test_data_quality_module.py`, `tests/unit/test_data_quality_invariants.py` — proves `QualifiedValue` evolution is backward-compatible.
- `tests/unit/test_premarket_volume_and_earnings_stale.py` and the premarket agent tests — prove the duplicate fold is behavior-identical.
- Full 1559-test baseline green at 1A completion (gate criterion, like Phase 0's gate 7).

**New tests:** `test_entities.py`, `test_pit.py`, `test_quality.py`, `test_market_calendar_sessions.py` (details above).

**No golden tests added in 1A** (the 19 GOLD-* tests belong to later phases); existing golden tests must stay green.

## Phase 1A exit gate (criteria)

1. `core/data_model/` package exists with entities/pit/quality modules; all 15 §27 entity types defined.
2. `as_of` + `UNAVAILABLE` implement the §32 guard; tested for both hit and miss.
3. `DataQuality`/`SignalQuality`/`QualityEnvelope` + `QualifiedValue` additive fields; DATA vs SIGNAL separation explicit.
4. `market_hours.py` session model (pre/regular/after/BMO/AMC/closed/half-day/crypto); duplicate folded.
5. `news_evidence_ledger` typed `read_articles_as_of` reader.
6. V7 untouched (§117).
7. Full regression green (1559+ passed, 0 new failures) — no test weakened (§115).
8. No new open-source deps added (§118). No push (§116).
9. `docs/V9_IMPLEMENTATION.md` Phase 1 row updated to IN PROGRESS (1A is the first Phase 1 work); `docs/V9_ARCHITECTURE.md` gets a canonical-data-layer note.

## Out of scope (deferred)
- Provider rewiring behind `DataProvider` ABC → 1B.
- Append-only historical storage + `as_of` over storage → 1B.
- Corporate-action capture/split-adjustment + survivorship registry → 1C.
- Options/SEC/Fundamentals **fetchers** → Phase 5/10 (types defined here).
- `exchange_calendars` / any new open-source dep → Phase 2.
- Migrating `agents/news_tab_agent.NewsArticle` call sites to the canonical type → 1B (canonical type defined here; agent untouched in 1A).

## Self-review (inline)
- **Spec coverage:** §27 (15 entities) → Section 1. §28 (calendar/sessions/BMO/AMC) → Section 4. §29 (PIT timestamps) → Section 2. §30 (survivorship) → explicitly deferred to 1C (documented). §31 (corporate actions) → entity type defined here; capture/adjustment deferred to 1C. §32 (no synthetic facts) → Section 2 `as_of`/`UNAVAILABLE`. §33 (data quality + separation) → Section 3. §5/§115/§116/§117/§118 → constraints. Covered, with deferrals explicit.
- **Placeholder scan:** `# populate in later phase` markers are the sanctioned living-doc mechanism (not forbidden placeholders); they mark intentional stubs for Phase 2+ / 1C. No "TBD"/"TODO" without a phase owner.
- **Internal consistency:** entities reference `PointInTime` (Section 2) and `DataQuality` (Section 3); calendar's `TradingSession` is the same enum entities use. No naming drift.
- **Scope check:** 1A is one coherent, independently-testable unit (types + contracts + calendar). Provider/storage/corporate-actions are cleanly split to 1B/1C. Not too large for one plan.
- **Ambiguity check:** "full 15 entity types now" is explicit; "no new deps" is explicit; "additive only" is explicit. `UNAVAILABLE` as a sentinel singleton is unambiguous.