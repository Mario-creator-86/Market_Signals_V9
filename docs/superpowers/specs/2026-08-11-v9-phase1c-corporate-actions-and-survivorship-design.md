# Phase 1C — Corporate Actions (§31) + Survivorship Registry (§30) (Design)

**Date:** 2026-08-11
**Phase:** 1C (third and final Phase 1 sub-plan; 1A = PASS @ 41bf30b, 1B = PASS @ 10a3ef6)
**Spec basis:** `prompt_to_follow.md` §31 (corporate actions), §30 (survivorship bias), §29 (point-in-time), §32 (no synthetic historical facts), §27 (canonical entities — `CorporateAction` already typed in 1A), §7 (extend → migrate → test), §23 (do not replace working providers), §106 (no secrets), §115 (no test weakening), §117 (V7 read-only), §118 (no new open-source deps in Phase 1), §4 (no forbidden success language).

## 1. Problem

Phase 1A typed `CorporateAction` (§31: SPLIT | DIVIDEND | TICKER_CHANGE | MERGER, with `ex_date`/`ratio`/`amount`/`from_ticker`/`to_ticker`/`pit`) and the `OHLCVBar.adjusted` flag, but provided **no capture path** and **no adjusted/raw price convention** — §31 requires both. Phase 1B delivered the `HistoricalStore` (append-only, `as_of` over storage). Phase 1C closes the §31 gap by recording corporate actions in the store (point-in-time, so a backtest at T can know actions up to T) and by documenting the adjusted-vs-raw convention clearly.

§30 (survivorship): the V7/V9 backtester builds its universe from `watchlist.get("yahoo") + watchlist.get("binance")` (`core/backtester.py:305-306`) — the **current** watchlist, i.e. current survivors. Historical backtests therefore silently use only current surviving securities, the exact bias §30 forbids being left silent. §30 explicitly permits the honest path: "If unavailable: document limitation. Do not pretend it is solved." V9 has no comprehensive historical delisting / index-membership database (and §118 forbids adding one in Phase 1). So 1C delivers a **survivorship registry scaffold** — a place to record per-symbol lifecycle (listing, delisting, ticker change) with point-in-time queries — **plus a loudly documented limitation**, rather than fake delisting data.

## 2. What is and is not in scope

**In scope (spec-grounded):**
- A `SurvivorshipRegistry` (`core/data_model/survivorship.py`): records per-symbol lifecycle events (LISTED / DELISTED / TICKER_CHANGE) with point-in-time `event_time`; queries `is_listed(symbol, T) -> bool` and `universe_at(T, base_symbols) -> set[str]`. Backed by a new `symbol_lifecycle` SQLite table (same DB, same WAL pattern as `sqlite_store.py` / `HistoricalStore`). Seeded from the current watchlist as "listed as of registry creation".
- A `YFCorporateActionAdapter` (`core/data_model/ingest.py`, additive): wraps a yfinance `.actions` DataFrame (combined Dividends + Stock Splits) → `CorporateAction` entities → appends to the 1B `HistoricalStore` (entity_type `"CorporateAction"`). Wrap, not replace (§23); no credentials (§106).
- An `actions_as_of(symbol, T)` helper on `HistoricalStore` (or a thin query) returning the corporate actions knowable at T — the point-in-time feed a backtest needs (§29).
- **Adjusted/raw price convention** (§31): documented in `V9_ARCHITECTURE.md`. V9 default = **adjusted** OHLCV for signal/backtest work (consistent with yfinance `auto_adjust=True`); the `OHLCVBar.adjusted` flag records which; raw bars are available on request; the corporate-action registry is the audit trail for *why* adjusted differs from raw. The `YFOHLCVAdapter` (1B) gains an `adjusted` parameter (default `True`) and sets `OHLCVBar.adjusted` accordingly.
- A loudly documented **§30 limitation**: V9's historical universe is survivorship-biased for symbols not in the current watchlist and for periods before the registry's first record; no comprehensive delisting DB exists; the registry is the scaffold to record delistings/ticker-changes as they are learned (manually or via a future Phase 2+ data source). A code comment at `backtester.py:305` makes the bias explicit rather than silent (additive comment, no behavior change).

**Explicitly OUT of scope (deferred, with phase owners):**
- Rewiring the backtester universe to call `SurvivorshipRegistry.universe_at(T)` and `actions_as_of(T)` → **Phase 7** (backtest engine consolidation; that is where the universe/adjustment question belongs). 1C delivers the substrate + the documented limitation; it does not change backtest behavior (§5/§7 — additive, no premature rewrite). A behavior-preserving call-site comment is the only backtester touch.
- A comprehensive historical delisting / index-membership database → no such source is available without a new dependency (§118 forbids it in Phase 1); deferred to Phase 2+ when an open-source data source is evaluated.
- Re-implementing split/dividend price adjustment math → yfinance already provides adjusted prices (`auto_adjust=True`); 1C records the actions + documents the convention, it does not recompute adjustments.
- Corporate-action adapters for non-yfinance providers (FinnHub/Binance/Alpaca) → phases that consume them (5/7/10). 1C ships the yfinance proof + the store path.

## 3. Architecture

### 3.1 File layout

| File | Responsibility | New/Modified |
|---|---|---|
| `core/data_model/survivorship.py` | `SurvivorshipRegistry` — `symbol_lifecycle` table, `register_listed`/`register_delisted`/`register_ticker_change`, `is_listed(symbol, T)`, `universe_at(T, base_symbols)`, `get_survivorship_registry`. | NEW |
| `core/data_model/ingest.py` | Add `YFCorporateActionAdapter` + `actions_df_to_records` (yfinance `.actions` DataFrame → action dicts). Add `adjusted` param to `YFOHLCVAdapter` (default `True`, sets `OHLCVBar.adjusted`). | MODIFY (additive) |
| `core/data_model/store.py` | Add `actions_as_of(symbol, T) -> list[CorporateAction-dict]` (thin query over `entity_type="CorporateAction"`). | MODIFY (additive) |
| `core/data_model/__init__.py` | Export `SurvivorshipRegistry`, `get_survivorship_registry`, `YFCorporateActionAdapter`. | MODIFY (additive) |
| `tests/unit/test_data_model_survivorship.py` | Registry tests. | NEW |
| `tests/unit/test_data_model_corporate_actions.py` | Adapter + `actions_as_of` tests (synthetic `.actions` DataFrame, no network). | NEW |
| `core/backtester.py` | One additive code comment at the universe-construction site documenting the §30 survivorship limitation (no behavior change). | MODIFY (comment only) |
| `docs/V9_ARCHITECTURE.md` | "Corporate actions + survivorship (Phase 1C)" section: adjusted/raw convention + §30 limitation. | MODIFY (additive) |
| `docs/V9_IMPLEMENTATION.md` | Phase 1C record; Phase 1 row → PASS (1C is the last Phase 1 sub-plan). | MODIFY (additive) |

### 3.2 Survivorship registry — `core/data_model/survivorship.py`

Follows the `sqlite_store.py` / `HistoricalStore` singleton + WAL pattern (§7, §118 stdlib-only). New table `symbol_lifecycle` in the same `MS_SQLITE_DB_PATH` DB:

```sql
CREATE TABLE IF NOT EXISTS symbol_lifecycle (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    event       TEXT    NOT NULL,   -- LISTED | DELISTED | TICKER_CHANGE
    event_time  TEXT    NOT NULL,   -- ISO-8601 UTC (§29 point-in-time)
    to_ticker   TEXT,               -- for TICKER_CHANGE; NULL otherwise
    source      TEXT    NOT NULL DEFAULT 'registry',
    UNIQUE(symbol, event, event_time)
);
CREATE INDEX IF NOT EXISTS ix_sym_life ON symbol_lifecycle(symbol, event_time);
```

API (append-only — corrections = new row; no update/delete):

- `register_listed(symbol, *, event_time=None, source="registry")` — record that `symbol` was listed as of `event_time` (default now UTC).
- `register_delisted(symbol, *, event_time=None, source="registry")` — record delisting as of `event_time`.
- `register_ticker_change(from_symbol, to_symbol, *, event_time=None, source="registry")` — record a ticker change (LISTED for `to_symbol`, plus a `TICKER_CHANGE` row on `from_symbol` with `to_ticker`).
- `is_listed(symbol, T) -> bool` — True iff the newest event for `symbol` with `event_time <= T` is `LISTED` (or `TICKER_CHANGE` into it), False if `DELISTED` or no record. Uses the same "newest event <= T" selection rule as 1A `as_of` (delegates to a small in-memory scan over the symbol's rows, ordered by `event_time`, version-less here since lifecycle events are unique per `(symbol, event, event_time)`).
- `universe_at(T, base_symbols) -> set[str]` — for each symbol in `base_symbols` (e.g. the current watchlist), include it iff `is_listed(symbol, T)`. This is the point-in-time universe filter; today (no delistings recorded) it returns `base_symbols` unchanged, but the path is now point-in-time-aware.
- `get_survivorship_registry()` — singleton factory.

### 3.3 Corporate-action capture — `ingest.py` additions

`actions_df_to_records(df) -> list[dict]`: normalizes a yfinance `.actions` DataFrame (DatetimeIndex + `Dividends` + `Stock Splits` columns) into action dicts:
- `{"ex_date": <iso-UTC>, "type": "DIVIDEND", "amount": <div>, "ratio": None}` when `Dividends != 0`,
- `{"ex_date": <iso-UTC>, "type": "SPLIT", "ratio": <split>, "amount": None}` when `Stock Splits != 0`.
(yfinance `.actions` combines both; a row can carry one or the other — never both nonzero in practice, but the function handles each independently.) None/empty → `[]`.

`YFCorporateActionAdapter`:
```python
class YFCorporateActionAdapter:
    entity_type = "CorporateAction"
    provider = "yfinance"
    def __init__(self, symbol, *, exchange_mic="XNYS", fetched_at=None): ...
    def to_entity(self, raw: dict) -> Optional[CorporateAction]: ...
```
`to_entity` builds a `CorporateAction` (1A entity) with `ex_date = raw["ex_date"]`, `type = raw["type"]`, `ratio`/`amount` from raw, a `PointInTime(event_time=ex_date, available_from=ex_date, fetched_at=self.fetched_at, version=1)`, and `symbol = Symbol(ticker=..., exchange=get_exchange(mic), ...)`. `ingest_via` (1B) appends each to the `HistoricalStore` with `entity_type="CorporateAction"`.

### 3.4 `actions_as_of` — `store.py` addition

```python
def actions_as_of(self, symbol, T) -> list[dict]:
    """Corporate actions for `symbol` knowable at T (§29), ascending by ex_date."""
```
Queries `pit_records WHERE entity_type='CorporateAction' AND symbol=? AND available_from <= T_iso` ordered ascending by `available_from`; returns the deserialized payload dicts. Thin, reuses the store's `as_of`-style selection (here a range, since we want *all* actions up to T, not just the newest).

### 3.5 Adjusted/raw price convention (§31)

Documented in `V9_ARCHITECTURE.md`:

> **V9 price convention:** V9 uses **adjusted** OHLCV bars (split- and dividend-adjusted) as the default for signal generation and backtesting, consistent with the yfinance default `auto_adjust=True`. The `OHLCVBar.adjusted` flag (§27) records whether a given bar is adjusted (`True`) or raw (`False`). Raw bars are available on request by passing `auto_adjust=False` to the underlying fetcher. The `CorporateAction` records in the point-in-time store are the audit trail for *why* adjusted differs from raw for a symbol as of a given time. A backtest at historical time T uses `actions_as_of(symbol, T)` to know which corporate actions have occurred up to T (§29) — it does not reconstruct actions from future data (§32).

`YFOHLCVAdapter.__init__` gains `adjusted: bool = True`; `to_entity` sets `OHLCVBar(..., adjusted=self.adjusted, ...)`. The 1B default was `False`; this flips the default to `True` to match the documented convention and yfinance's actual default. **Behavior note:** the 1B `YFOHLCVAdapter` tests do not assert `adjusted == False` (they assert on `close`/`symbol`/`pit`), so flipping the default to `True` is backward-compatible for the test suite; the 1B end-to-end test still passes. This is a documented behavior change (1B's `False` default was a proof placeholder; 1C establishes the real convention).

### 3.6 Backward compatibility

Additive: one new module (`survivorship.py`), additive methods on `store.py`/`ingest.py`, new exports, one new SQLite table (`symbol_lifecycle`, `CREATE IF NOT EXISTS`), one comment in `backtester.py` (no behavior change), and the `YFOHLCVAdapter.adjusted` default flip (`False`→`True`, documented, test-suite-compatible). No existing call site routed through the registry. No new open-source deps (§118: stdlib `sqlite3`/`json`/`dataclasses`; pandas already a V7 dep for the adapter normalizer). V7 untouched (§117).

### 3.7 Testing strategy (TDD)

- **Survivorship**: `register_listed` + `is_listed(T)` (listed before T → True; delisting recorded → False after delisting, True before); `universe_at(T, base)` filters out delisted; `register_ticker_change` records both rows; temp DB via `tmp_path`. ~6 tests.
- **Corporate actions**: `actions_df_to_records` on a synthetic 3-row `.actions` DataFrame (1 dividend, 1 split, 1 no-action) → correct dicts; empty/None → `[]`. `YFCorporateActionAdapter.to_entity` → `CorporateAction` with correct `type`/`ratio`/`amount`/`pit`. `ingest_via` → store; `store.actions_as_of(symbol, T)` returns actions up to T (excludes a future-dated action). ~6 tests.
- **Adjusted flag**: `YFOHLCVAdapter(adjusted=True).to_entity(...)` → `OHLCVBar.adjusted is True`; `adjusted=False` → `False`. ~1-2 tests.
- **Regression**: full suite must remain green (1629 baseline + new, 0 failures).

## 4. Spec coverage mapping

| Spec | 1C delivery |
|---|---|
| §31 corporate actions (splits/dividends/ticker changes/mergers + adjusted/raw convention) | `CorporateAction` capture via `YFCorporateActionAdapter` → store; `actions_as_of(T)`; adjusted/raw convention documented in `V9_ARCHITECTURE.md`; `OHLCVBar.adjusted` flag set by the adapter. |
| §30 survivorship (delisted/index membership/ticker changes/mergers/bankruptcies; document limitation; do not pretend solved) | `SurvivorshipRegistry` scaffold + `is_listed(T)`/`universe_at(T)` + loudly documented limitation in `V9_ARCHITECTURE.md` + a comment at `backtester.py:305`. No fake delisting data. |
| §29 point-in-time | `actions_as_of(T)` returns only actions knowable at T; `is_listed(T)` / `universe_at(T)` are point-in-time. |
| §32 no synthetic historical facts | No reconstructed/fake delisting or action data; missing lifecycle = "not listed (unknown)" not "listed". |
| §7 / §23 (extend, wrap-not-replace) | yfinance adapter wraps; backtester untouched (comment only); registry is additive. |
| §106 / §115 / §117 / §118 / §4 | honored (verified at gate). |

## 5. Decomposition into tasks (preview for the plan)

1. **Task 1** — `SurvivorshipRegistry` + `symbol_lifecycle` schema + `register_*` + `is_listed(T)` + `universe_at(T)` + tests.
2. **Task 2** — `actions_df_to_records` + `YFCorporateActionAdapter` + tests (synthetic `.actions` DataFrame).
3. **Task 3** — `HistoricalStore.actions_as_of(symbol, T)` + ingest→`actions_as_of` test.
4. **Task 4** — `YFOHLCVAdapter.adjusted` param (default `True`) + tests; update 1B end-to-end test if needed (it isn't — no `adjusted` assertion there).
5. **Task 5** — `__init__.py` exports + `backtester.py` §30 comment + control-doc updates (V9_ARCHITECTURE convention + limitation; V9_IMPLEMENTATION Phase 1C record; **Phase 1 row → PASS**).
6. **Task 6** — exit gate: full regression, sweeps, V7 untouched, no new deps, imports; flip Phase 1C Status to PASS and Phase 1 row to PASS.

## 6. Risks and mitigations

- **`YFOHLCVAdapter.adjusted` default flip (False→True)** — documented behavior change; mitigated by verifying no 1B test asserts `adjusted == False` (they assert on `close`/`symbol`/`pit` only) and re-running the 1B ingest suite. The flip aligns V9 with the documented convention + yfinance's actual default.
- **Survivorship registry unused by backtest** — by design (1C delivers the scaffold; rewiring is Phase 7). The §30 limitation is documented loudly so the bias is not silent. No pretend solution.
- **yfinance `.actions` shape drift** — isolated to `actions_df_to_records` (one function); tests use a synthetic DataFrame, so a yfinance column rename breaks only the normalizer, not the store/registry.
- **TICKER_CHANGE semantics** — recorded as a `TICKER_CHANGE` row on the old symbol + a `LISTED` row on the new symbol; `is_listed(new, T)` returns True after the change, `is_listed(old, T)` returns False after (via the `TICKER_CHANGE` event acting as a terminal event for the old symbol). Documented in the registry docstring + a test.

## 7. Out of scope reaffirmation (phase owners)

- Backtester universe/adjustment rewiring → Phase 7 (backtest consolidation).
- Comprehensive historical delisting / index-membership DB → Phase 2+ (open-source data source evaluation).
- Corporate-action adapters for FinnHub/Binance/Alpaca → phases that consume them (5/7/10).
- `exchange_calendars` or any new open-source dep → Phase 2 (§118).