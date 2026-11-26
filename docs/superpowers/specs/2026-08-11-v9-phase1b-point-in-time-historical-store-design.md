# Phase 1B — Point-in-Time Historical Store (Design)

**Date:** 2026-08-11
**Phase:** 1B (second of three Phase 1 sub-plans; 1A = PASS @ commit 41bf30b)
**Spec basis:** `prompt_to_follow.md` §29 (point-in-time data), §32 (no synthetic historical facts), §33 (data quality / conflicts), §27 (canonical entities), §7 (extend → migrate → test, do not rewrite), §23 (do not replace working providers), §106 (no secrets), §115 (no test weakening), §117 (V7 read-only), §118 (no new open-source deps in Phase 1), §4 (no forbidden success language).

## 1. Problem

Phase 1A delivered the canonical data model (`core/data_model/entities.py` — 15 §27 entity types), the point-in-time contract (`pit.py` — `PointInTime`, `UNAVAILABLE`, `as_of`), and the DATA/SIGNAL quality separation (`quality.py`). The `as_of(records, T)` function selects the newest record available at T over an **in-memory list** and returns `UNAVAILABLE` when nothing qualifies — this is the §32 no-synthetic-historical-facts guard.

What 1A did **not** provide: a place where historical records **persist** and can be queried as-of T by the rest of the system (backtesting, validation, research). §32 says missing historical information "must remain `UNAVAILABLE` **unless a valid point-in-time source exists**." 1A's in-memory `as_of` is not that source — it vanishes when the process exits. For §29/§32 to be enforceable system-wide, V9 needs an **append-only point-in-time store**: each fetched fact is written with its `available_from`/`fetched_at`/`version` and never mutated; a later query at historical time T returns only what was knowable at T, else `UNAVAILABLE`.

## 2. What is and is not in scope

**In scope (spec-grounded):**
- An append-only point-in-time historical store backed by SQLite (the "valid point-in-time source" §32 references), holding canonical §27 entity records.
- An `as_of` query **over the store** that reuses the 1A `as_of` tie-break semantics (DRY — one implementation of the selection rule), returning `UNAVAILABLE` on miss.
- An `as_of_range` window query (the series of records valid in `[T_start, T_end]`) — the shape a backtester needs to replay a bar series as-of T.
- Store-level **conflict detection** (§33 `conflicts`): when two providers disagree for the same `(entity_type, symbol, T)` within a tolerance, emit a `Conflict` (the 1A `quality.py` type).
- A **minimal, opt-in ingestion seam** — a `ProviderRecordAdapter` protocol + `ingest_via` helper + **one proof adapter** (yfinance OHLCV bars → `OHLCVBar`) — to demonstrate the store ingests real V7 client output, not just hand-crafted entities.

**Explicitly OUT of scope (deferred, with phase owners):**
- Rewriting or replacing the 5 working V7 market-data clients (yfinance, FinnHub, Binance, Alpaca, KuCoin). §23/§7 forbid this. The clients stay as-is; adapters **wrap** them opt-in. `# populate in later phase` markers are NOT used here — the deferral is documented in this section.
- A mandated market-data `DataProvider` ABC. The spec mandates **no** such ABC: §34 "Multi-Provider AI Platform" governs **AI** providers (Ollama/OpenAI/Anthropic/etc), which is Phase 3. The `ProviderRecordAdapter` protocol here is a thin ingestion seam, not a provider abstraction with normalized capabilities.
- Adapter implementations for FinnHub earnings, news, fundamentals, Binance/KuCoin crypto bars, Alpaca snapshots → deferred to the phases that consume each entity (Phase 7 backtest needs bars; Phase 5/10 need earnings/news/fundamentals). 1B ships the **seam + one proof adapter** only.
- Survivorship registry (§30) and corporate-action capture/adjustment (§31) → Phase 1C.
- Schema migrations of existing V9 tables → none; 1B adds one new table to the existing DB file.

## 3. Architecture

### 3.1 File layout (all new, under `core/data_model/`)

| File | Responsibility |
|---|---|
| `core/data_model/store.py` | `HistoricalStore` — singleton SQLite append-only store; `append`, `as_of`, `as_of_range`, `query`, `detect_conflicts`. |
| `core/data_model/ingest.py` | `ProviderRecordAdapter` protocol, `ingest_via`, `df_to_bar_rows` (yfinance DataFrame → bar dicts), `YFOHLCVAdapter` (proof adapter). |
| `tests/unit/test_data_model_store.py` | Store schema, append-only enforcement, `as_of` over store (hit/miss/tie-break), `as_of_range`, conflict detection. |
| `tests/unit/test_data_model_ingest.py` | Adapter protocol, `df_to_bar_rows`, `YFOHLCVAdapter` → `OHLCVBar`, `ingest_via` end-to-end into the store. |

Modified (additive, backward-compatible):
- `core/data_model/__init__.py` — export `HistoricalStore`, `get_historical_store`, `ProviderRecordAdapter`, `ingest_via`, `YFOHLCVAdapter`, `df_to_bar_rows`.
- `docs/V9_ARCHITECTURE.md` — add "## Point-in-time historical store (Phase 1B)" section.
- `docs/V9_IMPLEMENTATION.md` — add Phase 1B record (Phase 1 row stays IN PROGRESS through 1C).

### 3.2 Storage substrate — `HistoricalStore`

Follows the existing `core/sqlite_store.py` pattern exactly (§7 extend-don't-rewrite, §118 no new deps):

- **Same DB file** as `SQLiteKVStore`: `MS_SQLITE_DB_PATH` (default `data/market_signals.db`). One connection, singleton via `get_historical_store()`.
- **Same pragmas**: `journal_mode=WAL`, `synchronous=FULL`, `busy_timeout=5000`, `check_same_thread=False`, guarded by an `RLock`.
- **New table `pit_records`** (created `IF NOT EXISTS` — no migration of existing tables):
  ```sql
  CREATE TABLE IF NOT EXISTS pit_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type   TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    provider      TEXT    NOT NULL,
    event_time    TEXT,             -- ISO-8601 UTC, nullable (some entities have no event_time)
    available_from TEXT    NOT NULL,-- ISO-8601 UTC (the as_of key; §29)
    fetched_at    TEXT    NOT NULL,-- ISO-8601 UTC (when V9 wrote it)
    version       INTEGER NOT NULL DEFAULT 1,
    payload       TEXT    NOT NULL,-- JSON serialization of the canonical entity
    quality_flags TEXT    NOT NULL DEFAULT '[]', -- JSON array of strings
    UNIQUE(entity_type, symbol, provider, available_from, version)
  );
  CREATE INDEX IF NOT EXISTS ix_pit_asof ON pit_records(entity_type, symbol, available_from, version);
  ```
- **Append-only by API contract**: the class exposes `append()` and read methods only — there is **no** `update` or `delete` method. Corrections are a new row with a higher `version` (the 1A tie-break prefers higher version on equal `available_from`). The `UNIQUE` constraint prevents accidental exact-duplicate appends; a conflicting insert raises (surfaced to the caller, not swallowed).

#### `append(record, *, entity_type=None, symbol=None, provider=None)` → `int` (row id)

Accepts either a canonical entity instance (duck-typed on `.pit: PointInTime` and `.symbol`/`.ticker`/`.id`) or a plain dict payload. Resolution:
- `entity_type` / `symbol` / `provider`: explicit kwargs override; else inferred from the entity (`type(record).__name__`; `getattr(record, "symbol", None) or getattr(record, "ticker", None)`; `record.pit` has no provider → caller must pass `provider` or it defaults to `"unknown"`).
- Timestamps from `record.pit` when present: `event_time`, `available_from` (required — raises `ValueError` if missing, since `as_of` keys on it), `version` (default 1). `fetched_at` defaults to `datetime.now(timezone.utc).isoformat()` when missing.
- `payload` = `json.dumps(record_to_dict(record))` where `record_to_dict` uses `dataclasses.asdict` for dataclass entities or the dict itself.
- `quality_flags` = `sorted(getattr(record, "quality_flags", frozenset()) or [])` serialized to JSON.
- Inserts under the `RLock`; commits; returns `cursor.lastrowid`.

#### `as_of(entity_type, symbol, T, *, prefer="available_from")` → entity | `UNAVAILABLE`

- `T` is normalized via the 1A `to_utc(T)`; `prefer` is the PointInTime field to key on (default `available_from`).
- SQL fetches candidate rows: `SELECT payload, available_from, version, event_time FROM pit_records WHERE entity_type=? AND symbol=? AND <prefer> <= ? ORDER BY <prefer> DESC, version DESC`.
- Deserializes each `payload` JSON to a dict, wraps it with a lightweight object exposing `.pit` (a `PointInTime` rebuilt from the row's `available_from`/`version`/`event_time`/`fetched_at`) so the 1A `as_of` duck-typing works, then **delegates the final selection to the 1A `as_of(candidates, T, prefer=prefer)`**. This guarantees the store's selection rule is identical to the in-memory contract (DRY — one tie-break implementation). Returns `UNAVAILABLE` when 1A `as_of` returns `UNAVAILABLE`.
- Returns the deserialized payload dict (the canonical entity as JSON-round-tripped). A future enhancement can rehydrate to the concrete dataclass; for 1B a dict with a `.pit` view is sufficient and avoids coupling the store to every entity constructor. (Parked minor, documented.)

#### `as_of_range(entity_type, symbol, T_start, T_end, *, prefer="available_from")` → `list[dict]`

Returns the ordered series of records whose `prefer` timestamp falls in `[T_start, T_end]`, ascending — the replay shape a backtester consumes. Empty list when none.

#### `query(entity_type, symbol=None, provider=None, *, limit=None)` → `list[dict]`

Generic filtered read for diagnostics/admin. Not used by `as_of`.

#### `detect_conflicts(entity_type, symbol, T, *, tolerance_pct=1.0, value_field="close")` → `list[Conflict]`

For each provider that has a record as-of T for `(entity_type, symbol)`, compare the `value_field` of the winning record across providers. When the max relative deviation exceeds `tolerance_pct`, build a `Conflict` (1A `quality.py`: `field`, `values`, `sources`, `deviation_pct`) covering all providers' values. Returns `[]` when fewer than 2 providers disagree (or agree within tolerance). Grounded in §33 `conflicts`. Initial implementation supports numeric `value_field` on `OHLCVBar`-style payloads; other entity types return `[]` (documented; conflict detection for non-numeric entities is a later-phase concern).

### 3.3 Ingestion seam — `ingest.py`

#### `ProviderRecordAdapter` (Protocol, `typing.Protocol`)

```python
class ProviderRecordAdapter(Protocol):
    entity_type: str
    provider: str
    def to_entity(self, raw: Any) -> Optional[Any]: ...
```

`to_entity` converts one raw provider record (a DataFrame row, a dict, etc.) to a canonical §27 entity (with a `.pit`) or `None` to skip. The adapter does **not** call the provider itself — it is a pure transform. Callers fetch via the existing client, then feed `raw` through the adapter. This keeps the store decoupled from network/credential concerns (§106 — adapters never see keys; the existing clients own credentials via headers/env).

#### `ingest_via(adapter, store, raw_batch, *, symbol=None)` → `int` (count appended)

For each `raw` in `raw_batch`: `entity = adapter.to_entity(raw)`; if not `None`, `store.append(entity, provider=adapter.provider)`. Returns the number appended. One network fetch → many appends.

#### `df_to_bar_rows(df)` → `list[dict]`

Normalizes a yfinance history DataFrame (`DatetimeIndex` with columns `Open/High/Low/Close/Volume`, possibly tz-stripped by the existing `_strip_tz`) into a list of plain dicts: `{"ts": <iso>, "open": ..., "high": ..., "low": ..., "close": ..., "volume": ...}`. Handles `None`/empty DataFrame → `[]`. This is the only yfinance-coupled function; it is thin and tested with a synthetic DataFrame (no network, no yfinance import required in tests).

#### `YFOHLCVAdapter` (proof adapter)

```python
class YFOHLCVAdapter:
    entity_type = "OHLCVBar"
    provider = "yfinance"
    def __init__(self, symbol: str, *, interval: str = "5m", fetched_at=None): ...
    def to_entity(self, raw: dict) -> Optional[OHLCVBar]: ...
```

`raw` is one dict from `df_to_bar_rows`. Builds an `OHLCVBar` (1A `entities.py`) with a `PointInTime` where:
- `event_time` = the bar timestamp (the bar's open time).
- `available_from` = the bar timestamp (a bar is knowable at its close; for 1B proof we use the bar timestamp as available_from — a conservative choice documented in the plan; precise bar-close-vs-open availability is a Phase 7 backtest refinement).
- `fetched_at` = `fetched_at` or now.
- `version` = 1.

This proves the seam end-to-end: `YahooFinanceClient.get_stock_data(...)` → `df_to_bar_rows(df)` → `ingest_via(YFOHLCVAdapter(sym), store, rows)` → `store.as_of("OHLCVBar", sym, T)` returns the bar knowable at T, else `UNAVAILABLE`.

### 3.4 Backward compatibility

Purely additive: two new files, new exports, one new SQLite table (created `IF NOT EXISTS`, no migration). No existing function signature changes. No existing call site changes. No existing test changes. The 4 working clients and the Binance/KuCoin REST paths are untouched. The store is opt-in; nothing routes through it yet. §118: no new open-source dependencies (`sqlite3`, `json`, `typing`, `dataclasses` — all stdlib, all already used by `sqlite_store.py`/`entities.py`).

### 3.5 Testing strategy (TDD)

- **Store**: append round-trips; `UNIQUE` constraint rejects exact duplicates; `as_of` hit / miss (`UNAVAILABLE`) / tie-break (equal `available_from`, higher `version` wins) / `prefer` field selection; `as_of_range` window boundaries; `query` filtering; `detect_conflicts` two-provider disagreement → `Conflict`, agreement within tolerance → `[]`, single provider → `[]`. Tests use a temp DB (override `MS_SQLITE_DB_PATH` to a `tmp_path` file) so they never touch the real `data/market_signals.db`.
- **Ingest**: `df_to_bar_rows` on a synthetic 3-row DataFrame → 3 dicts; empty/None → `[]`. `YFOHLCVAdapter.to_entity` on a bar dict → `OHLCVBar` with correct `PointInTime`. `ingest_via` end-to-end: append 3 bars, `as_of` at T between bar1 and bar2 returns bar1, at T after bar3 returns bar3, before bar1 returns `UNAVAILABLE`.
- **Regression**: full `pytest tests/unit tests/integration tests/contract tests/golden tests/perf` must remain green (1604 baseline + new tests, 0 failures).

## 4. Spec coverage mapping

| Spec | 1B delivery |
|---|---|
| §29 point-in-time data (7 fields, "use only data available at T") | `HistoricalStore.as_of` / `as_of_range` query over persisted records keyed on `available_from`; reuses 1A `PointInTime` fields. |
| §32 no synthetic historical facts ("UNAVAILABLE unless a valid point-in-time source exists") | The store **is** the valid point-in-time source; `as_of` returns `UNAVAILABLE` on miss. |
| §33 data quality (`conflicts`) | `detect_conflicts` emits 1A `Conflict` objects at the store level. |
| §27 canonical entities | Store holds serialized §27 entities; proof adapter produces `OHLCVBar`. |
| §7 extend → migrate → test (no rewrite) | Follows `sqlite_store.py` pattern; adapters wrap existing clients. |
| §23 do not replace working providers | Clients untouched; adapters opt-in, wrap-not-replace. |
| §106 no secrets | Adapters never see keys; payloads are entity data only. |
| §115 no test weakening | New tests only; existing 1604 unchanged. |
| §117 V7 read-only | All files in V9 `core/data_model/`; V7 untouched. |
| §118 no new deps | stdlib only (`sqlite3`, `json`, `typing`, `dataclasses`). |
| §4 no forbidden language | None used. |

## 5. Decomposition into tasks (preview for the plan)

1. **Task 1** — `HistoricalStore` skeleton + `pit_records` schema + `append()` + `query()` + tests (temp DB, round-trip, append-only via absent update API, `UNIQUE` duplicate rejection).
2. **Task 2** — `as_of()` over store delegating to 1A `as_of` (hit / `UNAVAILABLE` miss / version tie-break / `prefer` selection) + tests.
3. **Task 3** — `as_of_range()` window series + tests.
4. **Task 4** — `detect_conflicts()` (§33, two-provider disagreement → `Conflict`; agreement/single → `[]`) + tests.
5. **Task 5** — `ProviderRecordAdapter` protocol + `df_to_bar_rows` + `YFOHLCVAdapter` + `ingest_via` + tests (synthetic DataFrame, no network).
6. **Task 6** — end-to-end ingest→`as_of` proof + `__init__.py` exports + tests.
7. **Task 7** — control-doc updates (`V9_ARCHITECTURE.md`, `V9_IMPLEMENTATION.md` Phase 1B record; Phase 1 row stays IN PROGRESS).
8. **Task 8** — exit gate: full regression green, §4 sweep, §106 sweep, no-new-deps diff empty, V7 untouched, all public symbols import.

## 6. Risks and mitigations

- **Store `as_of` performance** — delegating to the 1A in-memory `as_of` over fetched candidates loads all candidate rows for `(entity_type, symbol)` with `available_from <= T` into memory. At Phase 1 scale (V9 not yet production-volume) this is correct and fine. Mitigation: the `ix_pit_asof` index scopes the SQL fetch; a SQL-side `LIMIT` subquery optimization is parked for Phase 7 when backtest volume demands it. Correctness (identical tie-break to the contract) wins over premature optimization now.
- **Append-only vs corrections** — corrections are a new higher-`version` row; old rows remain. This is intentional (point-in-time auditability) and matches §29/§32. A future admin purge for very old low-version rows is a later-phase hygiene concern, not 1B.
- **Adapter coupling to yfinance DataFrame shape** — isolated to `df_to_bar_rows` (one thin function); the adapter and store tests use plain dicts, so a yfinance column rename breaks only `df_to_bar_rows`, not the store. Acceptable for a proof seam.
- **`available_from` for bars** — using the bar timestamp as `available_from` is a conservative proof choice (a bar is strictly knowable only at its close). The precise bar-close availability rule is a Phase 7 backtest refinement; documented, not a 1B defect.

## 7. Out of scope reaffirmation (phase owners)

- Provider adapters for FinnHub/Binance/Alpaca/KuCoin/news/fundamentals → phases that consume each entity (5/7/10).
- Market-data `DataProvider` ABC → not spec-mandated; not built (§34 is AI providers, Phase 3).
- Survivorship registry (§30) + corporate-action capture/adjustment (§31) → Phase 1C.
- `exchange_calendars` or any new open-source dep → Phase 2 (§118).