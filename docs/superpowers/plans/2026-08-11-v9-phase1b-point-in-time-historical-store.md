# Phase 1B — Point-in-Time Historical Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver V9's append-only point-in-time historical store — the "valid point-in-time source" that makes spec §29 ("use only data available at T") and §32 ("UNAVAILABLE unless a valid point-in-time source exists") enforceable system-wide — plus a minimal opt-in ingestion seam that proves it ingests real V7 provider output, as a backward-compatible evolution keeping the 1604-test green baseline green.

**Architecture:** A new `HistoricalStore` (SQLite, append-only, singleton — follows the existing `core/sqlite_store.py` WAL pattern) holds serialized canonical §27 entities keyed by `(entity_type, symbol, provider, available_from, version)`. `as_of`/`as_of_range` queries over the store delegate final selection to the 1A `as_of` (DRY — one tie-break implementation). A `ProviderRecordAdapter` protocol + `ingest_via` + one proof adapter (`YFOHLCVAdapter`, wrapping yfinance output → `OHLCVBar`) demonstrate the seam. No working V7 client is rewritten or replaced; adapters wrap opt-in.

**Tech Stack:** Python 3.14.3, stdlib `sqlite3` + `json` + `dataclasses` + `typing` (no new open-source deps — §118), pandas (already a V7 dep, used only in the one `df_to_bar_rows` normalizer), pytest.

## Global Constraints

Copied verbatim from `prompt_to_follow.md`; every task's requirements implicitly include these:

- **§117:** V7 at `C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium` (HEAD f912a33, 109 dirty) is READ-ONLY — never modify anything in V7; everything happens in V9.
- **§116:** no remote push (repo has no remote).
- **§106:** never commit API keys / store secrets in prompts / expose keys to frontend / expose broker credentials to Coding AI / log tokens. The store stores entity data only; adapters never see credentials.
- **§115:** no skip/xfail critical test / `assert True` / delete failing test / weaken assertion.
- **§4:** forbidden success language — "mostly complete", "approximately complete", "99%", "should work", "probably works", "backend finished", "frontend pending", "TODO later", "mock for now", "temporary workaround", "untested but implemented". `# populate in later phase` sanctioned living-doc stubs (with a phase owner) are NOT forbidden.
- **§5:** no rewrite — evolve V7 (additive only; existing call sites unchanged).
- **§7:** prefer EXTEND → MIGRATE → TEST → REFACTOR over DELETE → REWRITE → HOPE.
- **§23:** do NOT replace working V7 providers merely because an abstraction exists; adapters wrap, never replace.
- **§118:** Phase 2 is the open-source manifest phase, so Phase 1B adds NO new open-source dependencies. stdlib only (+ pandas already in V7).
- **Phase 1A baseline:** 1604 passed / 0 failed / exit 0 (commit 41bf30b). Phase 1B must keep this green and add new tests only.
- **Existing 1A symbols this plan consumes (do not redefine):** `core.data_model.pit.PointInTime` (fields: `event_time, publication_time, provider_time, received_at, available_from, fetched_at, version`; `__post_init__` raises on tz-naive fields), `core.data_model.pit.UNAVAILABLE` (singleton, `bool()==False`, identity-equal, `repr=="UNAVAILABLE"`), `core.data_model.pit.as_of(records, T, *, prefer="available_from")` (duck-types on `r.pit`/`r.pit.<prefer>`/`r.pit.version`; tie-break strict `>` on ts then `>` on version when `ts==best_ts`; returns `UNAVAILABLE` when none qualifies; calls `assert_utc(T)` → raises on tz-naive T), `core.data_model.pit.to_utc(dt)` (naive→assume UTC, aware→convert), `core.data_model.entities.OHLCVBar` (frozen dataclass; fields `symbol: Symbol, timestamp: datetime (UTC-aware, validated), open, high, low, close, volume, adjusted=False, resolution="1D", pit=None`; `__post_init__` auto-sets `pit` ONLY when `pit is None`), `core.data_model.entities.Symbol` (frozen: `ticker, exchange: Exchange, asset_class, currency, isin=None`), `core.data_model.entities.get_exchange(mic) -> Optional[Exchange]`, `core.data_model.entities.EXCHANGES` (has `"XNYS"`, `"XNAS"`, `"CRYPTO"`), `core.data_model.quality.Conflict` (frozen: `field: str, values: tuple, sources: tuple, deviation_pct: Optional[float]=None`).
- **Existing SQLite pattern to follow (§7):** `core/sqlite_store.py` — `SQLiteKVStore` singleton, `MS_SQLITE_DB_PATH` env (default `data/market_signals.db`), `PRAGMA journal_mode=WAL; synchronous=FULL; busy_timeout=5000`, `check_same_thread=False`, `RLock`, `CREATE TABLE IF NOT EXISTS`.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `core/data_model/store.py` (NEW) | `HistoricalStore`: schema, `append`, `query`, `as_of`, `as_of_range`, `detect_conflicts`; `get_historical_store` factory; `_iso`/`_from_iso`/`_RowView` helpers. | 1–4 |
| `core/data_model/ingest.py` (NEW) | `ProviderRecordAdapter` protocol, `df_to_bar_rows`, `YFOHLCVAdapter`, `ingest_via`. | 5 |
| `core/data_model/__init__.py` (MODIFY, additive) | Export store + ingest public symbols. | 6 |
| `tests/unit/test_data_model_store.py` (NEW) | Store tests (Tasks 1–4). | 1–4 |
| `tests/unit/test_data_model_ingest.py` (NEW) | Ingest seam tests (Tasks 5–6). | 5–6 |
| `docs/V9_ARCHITECTURE.md` (MODIFY, additive) | "Point-in-time historical store (Phase 1B)" section. | 7 |
| `docs/V9_IMPLEMENTATION.md` (MODIFY, additive) | Phase 1B record; Phase 1 row stays IN PROGRESS. | 7 |

---

### Task 1: HistoricalStore skeleton + pit_records schema + append() + query()

**Files:**
- Create: `core/data_model/store.py`
- Test: `tests/unit/test_data_model_store.py`

**Interfaces:**
- Consumes: `core.data_model.pit.PointInTime`, `to_utc`; `core.data_model.entities.Symbol` (for symbol extraction).
- Produces: `HistoricalStore` class (`append(record, *, entity_type=None, symbol=None, provider=None, available_from=None, version=None, fetched_at=None) -> int`; `query(entity_type, symbol=None, provider=None, *, limit=None) -> list[dict]`); `get_historical_store()`; helpers `_iso`, `_from_iso`, `_RowView`. Later tasks (2–4) add `as_of`, `as_of_range`, `detect_conflicts` to this same class.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_data_model_store.py`:

```python
import sqlite3
from datetime import datetime

import pytest

from core.data_model.entities import OHLCVBar, Symbol, get_exchange
from core.data_model.pit import PointInTime, UNAVAILABLE
from core.data_model.store import HistoricalStore


def _sym(ticker="AAPL"):
    ex = get_exchange("XNYS")
    return Symbol(ticker=ticker, exchange=ex, asset_class=ex.asset_class, currency=ex.currency)


def _bar(ticker, ts_iso, close=100.0, version=1):
    ts = datetime.fromisoformat(ts_iso)
    pit = PointInTime(event_time=ts, available_from=ts, fetched_at=ts, version=version)
    return OHLCVBar(
        symbol=_sym(ticker), timestamp=ts,
        open=close - 1, high=close, low=close - 2, close=close, volume=1000, pit=pit,
    )


def test_append_roundtrip_and_query(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    rid = store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00"), provider="yfinance")
    assert isinstance(rid, int) and rid > 0
    rows = store.query("OHLCVBar", "AAPL")
    assert len(rows) == 1
    assert rows[0]["symbol"]["ticker"] == "AAPL"
    assert rows[0]["close"] == 100.0


def test_append_only_no_update_delete_api(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    assert not hasattr(store, "update")
    assert not hasattr(store, "delete")


def test_unique_constraint_rejects_exact_duplicate(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    bar = _bar("AAPL", "2026-01-05T14:30:00+00:00")
    store.append(bar, provider="yfinance")
    with pytest.raises(sqlite3.IntegrityError):
        store.append(bar, provider="yfinance")


def test_append_requires_available_from(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    with pytest.raises(ValueError):
        store.append({"close": 100.0}, entity_type="OHLCVBar", symbol="AAPL", provider="yfinance")


def test_query_filters_by_symbol_and_provider(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00"), provider="yfinance")
    store.append(_bar("MSFT", "2026-01-05T14:30:00+00:00"), provider="yfinance")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=99), provider="finnhub")
    assert len(store.query("OHLCVBar", "AAPL")) == 2
    assert len(store.query("OHLCVBar", "AAPL", provider="yfinance")) == 1
    assert len(store.query("OHLCVBar", "MSFT")) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_data_model_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.data_model.store'` (or import error).

- [ ] **Step 3: Write minimal implementation**

Create `core/data_model/store.py`:

```python
"""V9 point-in-time historical store — append-only substrate for spec §29/§32.

Each fetched canonical entity is written once with its available_from /
fetched_at / version and never mutated (corrections = new higher-version row).
Queries select only records knowable at a historical time T (as_of) or within a
window (as_of_range), returning UNAVAILABLE when nothing qualifies (§32).

Follows the existing core/sqlite_store.py singleton + WAL pattern (§7 extend,
§118 no new deps: stdlib sqlite3 + json only).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.data_model.entities import Symbol
from core.data_model.pit import PointInTime, to_utc

logger = logging.getLogger("HistoricalStore")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DB_PATH = Path(os.getenv("MS_SQLITE_DB_PATH", BASE_DIR / "data" / "market_signals.db"))


def _iso(dt: Any) -> Optional[str]:
    """Serialize a datetime/str to an ISO-8601 UTC string. None passes through."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if isinstance(dt, datetime):
        return to_utc(dt).isoformat()
    return str(dt)


def _from_iso(s: Any) -> Optional[datetime]:
    """Parse an ISO-8601 string to an aware datetime. None/invalid -> None."""
    if s is None:
        return None
    if isinstance(s, str):
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    if isinstance(s, datetime):
        return s
    return None


def _record_to_dict(record: Any) -> Dict[str, Any]:
    if isinstance(record, dict):
        return dict(record)
    if is_dataclass(record) and not isinstance(record, type):
        return asdict(record)
    if hasattr(record, "__dict__"):
        return dict(vars(record))
    return {"value": record}


class _RowView:
    """Lightweight wrapper exposing .pit so core.data_model.pit.as_of duck-typing
    works on deserialized store rows (the payload dict is carried alongside)."""
    __slots__ = ("payload", "pit")

    def __init__(self, payload: Dict[str, Any], pit: PointInTime):
        self.payload = payload
        self.pit = pit


class HistoricalStore:
    """Append-only point-in-time store. Singleton via get_historical_store()."""

    _instance: Optional["HistoricalStore"] = None
    _instance_lock = threading.Lock()

    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pit_records (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type    TEXT    NOT NULL,
                symbol         TEXT    NOT NULL,
                provider       TEXT    NOT NULL,
                event_time     TEXT,
                available_from TEXT    NOT NULL,
                fetched_at     TEXT    NOT NULL,
                version        INTEGER NOT NULL DEFAULT 1,
                payload        TEXT    NOT NULL,
                quality_flags  TEXT    NOT NULL DEFAULT '[]',
                UNIQUE(entity_type, symbol, provider, available_from, version)
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_pit_asof "
            "ON pit_records(entity_type, symbol, available_from, version)"
        )
        self._conn.commit()

    @classmethod
    def instance(cls) -> "HistoricalStore":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = HistoricalStore()
        return cls._instance

    def append(
        self,
        record: Any,
        *,
        entity_type: Optional[str] = None,
        symbol: Optional[str] = None,
        provider: Optional[str] = None,
        available_from: Optional[Any] = None,
        version: Optional[int] = None,
        fetched_at: Optional[Any] = None,
    ) -> int:
        """Append one canonical entity (or dict) as a new point-in-time record.
        Returns the new row id. Raises ValueError if available_from cannot be
        determined (required — as_of keys on it). Append-only: no update/delete."""
        with self._lock:
            pit = getattr(record, "pit", None)
            et = entity_type or getattr(record, "entity_type", None) or type(record).__name__
            sym_raw = symbol or getattr(record, "symbol", None) or getattr(record, "ticker", None)
            if isinstance(record, dict):
                if et == type(record).__name__:
                    et = entity_type or record.get("entity_type") or type(record).__name__
                if sym_raw is None:
                    sym_raw = record.get("symbol") or record.get("ticker")
            if isinstance(sym_raw, Symbol):
                sym = sym_raw.ticker
            elif sym_raw is not None:
                sym = str(sym_raw)
            else:
                sym = None
            prov = provider or getattr(record, "provider", None) or "unknown"

            event_time_v = None
            available_from_v: Optional[str] = None
            fetched_at_v: Optional[str] = None
            version_v = 1
            if isinstance(pit, PointInTime):
                event_time_v = _iso(pit.event_time)
                available_from_v = _iso(pit.available_from)
                fetched_at_v = _iso(pit.fetched_at)
                version_v = int(pit.version or 1)
            if available_from is not None:
                available_from_v = _iso(available_from)
            if version is not None:
                version_v = int(version)
            if fetched_at is not None:
                fetched_at_v = _iso(fetched_at)
            if available_from_v is None:
                raise ValueError(
                    "append requires available_from (record.pit.available_from or explicit kwarg)"
                )
            if fetched_at_v is None:
                fetched_at_v = datetime.now(timezone.utc).isoformat()

            qflags = sorted(getattr(record, "quality_flags", frozenset()) or [])
            payload = json.dumps(_record_to_dict(record), ensure_ascii=False, default=str)
            cur = self._conn.execute(
                """INSERT INTO pit_records
                   (entity_type, symbol, provider, event_time, available_from,
                    fetched_at, version, payload, quality_flags)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (et, sym, prov, event_time_v, available_from_v, fetched_at_v,
                 version_v, payload, json.dumps(qflags)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def query(
        self,
        entity_type: str,
        symbol: Optional[str] = None,
        provider: Optional[str] = None,
        *,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Filtered read of payloads (ascending by available_from, version)."""
        sql = "SELECT payload FROM pit_records WHERE entity_type = ?"
        args: List[Any] = [entity_type]
        if symbol is not None:
            sql += " AND symbol = ?"
            args.append(symbol)
        if provider is not None:
            sql += " AND provider = ?"
            args.append(provider)
        sql += " ORDER BY available_from ASC, version ASC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self._lock:
            cur = self._conn.execute(sql, args)
            rows = cur.fetchall()
        return [json.loads(r[0]) for r in rows]


def get_historical_store() -> HistoricalStore:
    return HistoricalStore.instance()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_data_model_store.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add core/data_model/store.py tests/unit/test_data_model_store.py
git commit -m "feat(phase1b): HistoricalStore append-only schema + append/query

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: as_of() over the store (delegates to 1A as_of)

**Files:**
- Modify: `core/data_model/store.py` (add `as_of` method + import `UNAVAILABLE`, `as_of`)
- Test: `tests/unit/test_data_model_store.py` (append tests)

**Interfaces:**
- Consumes: `core.data_model.pit.as_of`, `UNAVAILABLE`, `to_utc` (already imported `to_utc`).
- Produces: `HistoricalStore.as_of(entity_type, symbol, T, *, prefer="available_from") -> dict | UNAVAILABLE`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_data_model_store.py`:

```python
def test_as_of_hit_returns_newest_available(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=100), provider="yfinance")
    store.append(_bar("AAPL", "2026-01-05T14:35:00+00:00", close=101), provider="yfinance")
    res = store.as_of("OHLCVBar", "AAPL", datetime.fromisoformat("2026-01-05T14:35:00+00:00"))
    assert res is not UNAVAILABLE
    assert res["close"] == 101


def test_as_of_miss_returns_unavailable(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00"), provider="yfinance")
    res = store.as_of("OHLCVBar", "AAPL", datetime.fromisoformat("2026-01-05T14:00:00+00:00"))
    assert res is UNAVAILABLE
    assert not res  # bool(UNAVAILABLE) is False


def test_as_of_version_tiebreak_on_equal_available_from(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=100, version=1), provider="yfinance")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=99, version=2), provider="yfinance")
    res = store.as_of("OHLCVBar", "AAPL", datetime.fromisoformat("2026-01-05T15:00:00+00:00"))
    assert res is not UNAVAILABLE
    assert res["close"] == 99  # higher version wins on equal available_from


def test_as_of_prefer_event_time(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    ts_event = datetime.fromisoformat("2026-01-05T14:30:00+00:00")
    ts_avail = datetime.fromisoformat("2026-01-05T15:00:00+00:00")
    pit = PointInTime(event_time=ts_event, available_from=ts_avail, fetched_at=ts_avail, version=1)
    bar = OHLCVBar(symbol=_sym("AAPL"), timestamp=ts_event, open=99, high=100, low=98,
                   close=100, volume=1000, pit=pit)
    store.append(bar, provider="yfinance")
    T = datetime.fromisoformat("2026-01-05T14:45:00+00:00")
    # before available_from -> UNAVAILABLE under prefer=available_from
    assert store.as_of("OHLCVBar", "AAPL", T, prefer="available_from") is UNAVAILABLE
    # after event_time -> hit under prefer=event_time
    res = store.as_of("OHLCVBar", "AAPL", T, prefer="event_time")
    assert res is not UNAVAILABLE
    assert res["close"] == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_data_model_store.py -v`
Expected: FAIL with `AttributeError: 'HistoricalStore' object has no attribute 'as_of'` (the 4 new tests fail; Task 1 tests still pass).

- [ ] **Step 3: Write minimal implementation**

In `core/data_model/store.py`, change the pit import line to include `UNAVAILABLE` and `as_of`:

```python
from core.data_model.pit import PointInTime, UNAVAILABLE, as_of, to_utc
```

Add this method to the `HistoricalStore` class (after `query`):

```python
    def as_of(
        self,
        entity_type: str,
        symbol: str,
        T: Any,
        *,
        prefer: str = "available_from",
    ) -> Any:
        """Newest record with pit.<prefer> <= T, or UNAVAILABLE (§32).

        Delegates final selection to core.data_model.pit.as_of so the store's
        tie-break rule is identical to the in-memory contract (DRY)."""
        T = to_utc(T)
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload, available_from, version, event_time, fetched_at "
                "FROM pit_records WHERE entity_type = ? AND symbol = ?",
                (entity_type, symbol),
            )
            rows = cur.fetchall()
        if not rows:
            return UNAVAILABLE
        candidates: List[_RowView] = []
        for payload_s, af, ver, et, fa in rows:
            pit = PointInTime(
                event_time=_from_iso(et),
                available_from=_from_iso(af),
                fetched_at=_from_iso(fa),
                version=int(ver),
            )
            candidates.append(_RowView(json.loads(payload_s), pit))
        best = as_of(candidates, T, prefer=prefer)
        if best is UNAVAILABLE:
            return UNAVAILABLE
        return best.payload
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_data_model_store.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add core/data_model/store.py tests/unit/test_data_model_store.py
git commit -m "feat(phase1b): HistoricalStore.as_of delegates to 1A as_of (§29/§32)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: as_of_range() window query

**Files:**
- Modify: `core/data_model/store.py` (add `as_of_range`)
- Test: `tests/unit/test_data_model_store.py` (append tests)

**Interfaces:**
- Produces: `HistoricalStore.as_of_range(entity_type, symbol, T_start, T_end, *, prefer="available_from") -> list[dict]` (ascending by `prefer`).

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_data_model_store.py`:

```python
def test_as_of_range_window_inclusive(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    for m, close in [(0, 100), (5, 101), (10, 102), (15, 103), (20, 104)]:
        store.append(_bar("AAPL", f"2026-01-05T14:{30 + m:02d}:00+00:00", close=close), provider="yfinance")
    lo = datetime.fromisoformat("2026-01-05T14:35:00+00:00")
    hi = datetime.fromisoformat("2026-01-05T14:45:00+00:00")
    rng = store.as_of_range("OHLCVBar", "AAPL", lo, hi)
    closes = [r["close"] for r in rng]
    assert closes == [101, 102, 103]  # 14:35, 14:40, 14:45 inclusive


def test_as_of_range_empty_when_none_in_window(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00"), provider="yfinance")
    lo = datetime.fromisoformat("2026-01-05T15:00:00+00:00")
    hi = datetime.fromisoformat("2026-01-05T16:00:00+00:00")
    assert store.as_of_range("OHLCVBar", "AAPL", lo, hi) == []


def test_as_of_range_ascending_order(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    # insert out of order; result must be ascending by available_from
    store.append(_bar("AAPL", "2026-01-05T14:40:00+00:00", close=102), provider="yfinance")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=100), provider="yfinance")
    lo = datetime.fromisoformat("2026-01-05T14:00:00+00:00")
    hi = datetime.fromisoformat("2026-01-05T15:00:00+00:00")
    rng = store.as_of_range("OHLCVBar", "AAPL", lo, hi)
    assert [r["close"] for r in rng] == [100, 102]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_data_model_store.py -v`
Expected: FAIL with `AttributeError: 'HistoricalStore' object has no attribute 'as_of_range'`.

- [ ] **Step 3: Write minimal implementation**

Add this method to the `HistoricalStore` class (after `as_of`):

```python
    def as_of_range(
        self,
        entity_type: str,
        symbol: str,
        T_start: Any,
        T_end: Any,
        *,
        prefer: str = "available_from",
    ) -> List[Dict[str, Any]]:
        """Records whose pit.<prefer> falls in [T_start, T_end], ascending."""
        T_start = to_utc(T_start)
        T_end = to_utc(T_end)
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload, available_from, version, event_time, fetched_at "
                "FROM pit_records WHERE entity_type = ? AND symbol = ? "
                "ORDER BY available_from ASC, version ASC",
                (entity_type, symbol),
            )
            rows = cur.fetchall()
        out: List[Dict[str, Any]] = []
        for payload_s, af, ver, et, fa in rows:
            pit = PointInTime(
                event_time=_from_iso(et),
                available_from=_from_iso(af),
                fetched_at=_from_iso(fa),
                version=int(ver),
            )
            ts = getattr(pit, prefer, None)
            if ts is None:
                continue
            ts = to_utc(ts)
            if T_start <= ts <= T_end:
                out.append(json.loads(payload_s))
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_data_model_store.py -v`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add core/data_model/store.py tests/unit/test_data_model_store.py
git commit -m "feat(phase1b): HistoricalStore.as_of_range window query

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: detect_conflicts() (§33 conflicts at the store level)

**Files:**
- Modify: `core/data_model/store.py` (add `detect_conflicts` + import `Conflict`)
- Test: `tests/unit/test_data_model_store.py` (append tests)

**Interfaces:**
- Consumes: `core.data_model.quality.Conflict` (frozen: `field, values: tuple, sources: tuple, deviation_pct: Optional[float]`).
- Produces: `HistoricalStore.detect_conflicts(entity_type, symbol, T, *, tolerance_pct=1.0, value_field="close") -> list[Conflict]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_data_model_store.py`:

```python
from core.data_model.quality import Conflict


def test_detect_conflicts_two_provider_disagreement(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=100), provider="yfinance")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=110), provider="finnhub")
    conflicts = store.detect_conflicts(
        "OHLCVBar", "AAPL",
        datetime.fromisoformat("2026-01-05T15:00:00+00:00"),
        tolerance_pct=1.0,
    )
    assert len(conflicts) == 1
    c = conflicts[0]
    assert isinstance(c, Conflict)
    assert c.field == "close"
    assert set(c.sources) == {"yfinance", "finnhub"}
    assert abs(c.deviation_pct - 10.0) < 0.01  # |110-100|/100*100 = 10%


def test_detect_conflicts_agreement_within_tolerance(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=100), provider="yfinance")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=100.5), provider="finnhub")
    assert store.detect_conflicts(
        "OHLCVBar", "AAPL",
        datetime.fromisoformat("2026-01-05T15:00:00+00:00"),
        tolerance_pct=1.0,
    ) == []


def test_detect_conflicts_single_provider_no_conflict(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    store.append(_bar("AAPL", "2026-01-05T14:30:00+00:00", close=100), provider="yfinance")
    assert store.detect_conflicts(
        "OHLCVBar", "AAPL",
        datetime.fromisoformat("2026-01-05T15:00:00+00:00"),
    ) == []


def test_detect_conflicts_empty_store(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    assert store.detect_conflicts(
        "OHLCVBar", "AAPL",
        datetime.fromisoformat("2026-01-05T15:00:00+00:00"),
    ) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_data_model_store.py -v`
Expected: FAIL with `AttributeError: 'HistoricalStore' object has no attribute 'detect_conflicts'`.

- [ ] **Step 3: Write minimal implementation**

In `core/data_model/store.py`, add the import at the top (with the other `core.data_model` imports):

```python
from core.data_model.quality import Conflict
```

Add this method to the `HistoricalStore` class (after `as_of_range`):

```python
    def detect_conflicts(
        self,
        entity_type: str,
        symbol: str,
        T: Any,
        *,
        tolerance_pct: float = 1.0,
        value_field: str = "close",
    ) -> List[Conflict]:
        """Cross-provider disagreement on `value_field` as-of T (§33 conflicts).

        For each provider, takes its winning record as-of T, then compares the
        `value_field` across providers. Emits one Conflict when the max relative
        deviation exceeds tolerance_pct. Returns [] when fewer than 2 providers
        have numeric values, or they agree within tolerance."""
        T = to_utc(T)
        with self._lock:
            cur = self._conn.execute(
                "SELECT provider, payload, available_from, version, event_time, fetched_at "
                "FROM pit_records WHERE entity_type = ? AND symbol = ?",
                (entity_type, symbol),
            )
            rows = cur.fetchall()
        if not rows:
            return []
        by_provider: Dict[str, List[_RowView]] = {}
        for prov, payload_s, af, ver, et, fa in rows:
            pit = PointInTime(
                event_time=_from_iso(et),
                available_from=_from_iso(af),
                fetched_at=_from_iso(fa),
                version=int(ver),
            )
            by_provider.setdefault(prov, []).append(_RowView(json.loads(payload_s), pit))
        winners: Dict[str, Dict[str, Any]] = {}
        for prov, cands in by_provider.items():
            w = as_of(cands, T)
            if w is not UNAVAILABLE:
                winners[prov] = w.payload
        if len(winners) < 2:
            return []
        numeric: Dict[str, float] = {}
        for prov, payload in winners.items():
            v = payload.get(value_field) if isinstance(payload, dict) else None
            if isinstance(v, (int, float)):
                numeric[prov] = float(v)
        if len(numeric) < 2:
            return []
        vals = list(numeric.values())
        lo, hi = min(vals), max(vals)
        base = lo if lo != 0 else (hi if hi != 0 else 0.0)
        deviation_pct = abs(hi - lo) / abs(base) * 100.0 if base else 0.0
        if deviation_pct <= tolerance_pct:
            return []
        return [Conflict(
            field=value_field,
            values=tuple(vals),
            sources=tuple(numeric.keys()),
            deviation_pct=round(deviation_pct, 4),
        )]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_data_model_store.py -v`
Expected: PASS (16 tests).

- [ ] **Step 5: Commit**

```bash
git add core/data_model/store.py tests/unit/test_data_model_store.py
git commit -m "feat(phase1b): HistoricalStore.detect_conflicts (§33 cross-provider)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: ProviderRecordAdapter protocol + df_to_bar_rows + YFOHLCVAdapter + ingest_via

**Files:**
- Create: `core/data_model/ingest.py`
- Test: `tests/unit/test_data_model_ingest.py`

**Interfaces:**
- Consumes: `core.data_model.entities.OHLCVBar`, `Symbol`, `get_exchange`; `core.data_model.pit.PointInTime`; `core.data_model.store.HistoricalStore`.
- Produces: `ProviderRecordAdapter` (Protocol: `entity_type: str`, `provider: str`, `to_entity(raw) -> Optional[Any]`); `df_to_bar_rows(df) -> list[dict]`; `YFOHLCVAdapter(symbol, *, interval="5m", exchange_mic="XNYS", fetched_at=None)`; `ingest_via(adapter, store, raw_batch, *, symbol=None) -> int`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_data_model_ingest.py`:

```python
from datetime import datetime, timezone

import pandas as pd

from core.data_model.ingest import (
    ProviderRecordAdapter,
    YFOHLCVAdapter,
    df_to_bar_rows,
    ingest_via,
)
from core.data_model.store import HistoricalStore


def _sample_df():
    idx = pd.to_datetime(["2026-01-05T14:30:00", "2026-01-05T14:35:00", "2026-01-05T14:40:00"])
    return pd.DataFrame(
        {"Open": [99, 100, 101], "High": [101, 102, 103], "Low": [98, 99, 100],
         "Close": [100, 101, 102], "Volume": [1000, 1100, 1200]},
        index=idx,
    )


def test_df_to_bar_rows_three_rows_utc():
    rows = df_to_bar_rows(_sample_df())
    assert len(rows) == 3
    assert rows[0]["close"] == 100.0
    assert rows[0]["ts"].endswith("+00:00")  # naive index localized to UTC
    assert rows[2]["volume"] == 1200.0


def test_df_to_bar_rows_empty_and_none():
    assert df_to_bar_rows(None) == []
    assert df_to_bar_rows(pd.DataFrame()) == []


def test_yf_adapter_to_entity_ohlcvbar():
    adapter = YFOHLCVAdapter("AAPL")
    rows = df_to_bar_rows(_sample_df())
    bar = adapter.to_entity(rows[0])
    assert bar is not None
    assert bar.symbol.ticker == "AAPL"
    assert bar.close == 100.0
    assert bar.resolution == "5m"
    assert bar.pit is not None
    assert bar.pit.available_from is not None
    assert bar.pit.version == 1


def test_yf_adapter_skips_invalid_raw():
    adapter = YFOHLCVAdapter("AAPL")
    assert adapter.to_entity({"no_ts": 1}) is None
    assert adapter.to_entity("not a dict") is None


def test_yf_adapter_is_provider_record_adapter():
    # structural typing: YFOHLCVAdapter satisfies the Protocol attributes
    adapter = YFOHLCVAdapter("AAPL")
    assert adapter.entity_type == "OHLCVBar"
    assert adapter.provider == "yfinance"
    assert callable(adapter.to_entity)


def test_ingest_via_counts_and_appends(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    adapter = YFOHLCVAdapter("AAPL")
    rows = df_to_bar_rows(_sample_df())
    n = ingest_via(adapter, store, rows)
    assert n == 3
    assert len(store.query("OHLCVBar", "AAPL")) == 3


def test_ingest_via_skips_none_entities(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    adapter = YFOHLCVAdapter("AAPL")
    mixed = [{"ts": "2026-01-05T14:30:00+00:00", "open": 1, "high": 2, "low": 0, "close": 1, "volume": 10},
             {"no_ts": 1}]
    n = ingest_via(adapter, store, mixed)
    assert n == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_data_model_ingest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.data_model.ingest'`.

- [ ] **Step 3: Write minimal implementation**

Create `core/data_model/ingest.py`:

```python
"""V9 ingestion seam — adapt existing V7 provider output to canonical §27
entities and append to the point-in-time store.

Adapters WRAP the working V7 clients (§7/§23): they never call providers and
never see credentials (§106). Callers fetch via the existing client, then feed
raw records through the adapter. The store owns persistence + point-in-time.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Protocol

from core.data_model.entities import OHLCVBar, Symbol, get_exchange
from core.data_model.pit import PointInTime
from core.data_model.store import HistoricalStore


class ProviderRecordAdapter(Protocol):
    """A pure transform from one raw provider record to one canonical §27 entity.

    Adapters carry no network/credential state — they declare their entity_type
    and provider and implement to_entity(raw) -> entity | None (None = skip)."""
    entity_type: str
    provider: str

    def to_entity(self, raw: Any) -> Optional[Any]: ...


def df_to_bar_rows(df: Any) -> list:
    """Normalize a yfinance history DataFrame (DatetimeIndex + Open/High/Low/
    Close/Volume columns, possibly tz-stripped) into a list of plain bar dicts:
    {"ts": iso-UTC, "open","high","low","close","volume": float}. None/empty -> []."""
    if df is None:
        return []
    try:
        if df.empty:
            return []
    except Exception:
        return []
    import pandas as pd
    idx = df.index
    if isinstance(idx, pd.DatetimeIndex):
        idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    rows = []
    for ts, row in df.iterrows():
        rows.append({
            "ts": pd.Timestamp(ts).isoformat(),
            "open": float(row.get("Open", row.get("open", 0.0)) or 0.0),
            "high": float(row.get("High", row.get("high", 0.0)) or 0.0),
            "low": float(row.get("Low", row.get("low", 0.0)) or 0.0),
            "close": float(row.get("Close", row.get("close", 0.0)) or 0.0),
            "volume": float(row.get("Volume", row.get("volume", 0.0)) or 0.0),
        })
    return rows


class YFOHLCVAdapter:
    """Proof adapter: yfinance bar dict -> OHLCVBar with an explicit PointInTime
    (available_from = bar timestamp; the bar-close availability refinement is a
    Phase 7 backtest concern, documented in the Phase 1B design)."""
    entity_type = "OHLCVBar"
    provider = "yfinance"

    def __init__(
        self,
        symbol: str,
        *,
        interval: str = "5m",
        exchange_mic: str = "XNYS",
        fetched_at: Optional[datetime] = None,
    ):
        self.symbol = str(symbol or "").upper().strip()
        self.interval = interval
        self.exchange_mic = exchange_mic
        self.fetched_at = fetched_at or datetime.now(timezone.utc)

    def to_entity(self, raw: Any) -> Optional[OHLCVBar]:
        if not isinstance(raw, dict) or "ts" not in raw:
            return None
        try:
            ts = datetime.fromisoformat(raw["ts"])
        except (ValueError, TypeError):
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ex = get_exchange(self.exchange_mic)
        if ex is None:
            return None
        sym = Symbol(ticker=self.symbol, exchange=ex, asset_class=ex.asset_class, currency=ex.currency)
        pit = PointInTime(event_time=ts, available_from=ts, fetched_at=self.fetched_at, version=1)
        return OHLCVBar(
            symbol=sym,
            timestamp=ts,
            open=float(raw.get("open", 0.0)),
            high=float(raw.get("high", 0.0)),
            low=float(raw.get("low", 0.0)),
            close=float(raw.get("close", 0.0)),
            volume=float(raw.get("volume", 0.0)),
            resolution=self.interval,
            pit=pit,
        )


def ingest_via(
    adapter: ProviderRecordAdapter,
    store: HistoricalStore,
    raw_batch: Iterable[Any],
    *,
    symbol: Optional[str] = None,
) -> int:
    """Run each raw record through the adapter and append the resulting entity to
    the store. Returns the number appended (skips raw records the adapter maps to None)."""
    count = 0
    for raw in raw_batch:
        entity = adapter.to_entity(raw)
        if entity is not None:
            store.append(entity, provider=adapter.provider, symbol=symbol)
            count += 1
    return count
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_data_model_ingest.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add core/data_model/ingest.py tests/unit/test_data_model_ingest.py
git commit -m "feat(phase1b): ingestion seam + YFOHLCVAdapter proof (§7 wrap, §23 no-replace)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: End-to-end ingest → as_of proof + __init__.py exports

**Files:**
- Modify: `core/data_model/__init__.py` (add store + ingest exports)
- Test: `tests/unit/test_data_model_ingest.py` (append end-to-end + export tests)

**Interfaces:**
- Produces: public exports `HistoricalStore`, `get_historical_store`, `ProviderRecordAdapter`, `ingest_via`, `YFOHLCVAdapter`, `df_to_bar_rows` from `core.data_model`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_data_model_ingest.py`:

```python
from core.data_model import (
    HistoricalStore as _HS,
    YFOHLCVAdapter as _YFA,
    df_to_bar_rows as _DTBR,
    ingest_via as _IV,
    UNAVAILABLE as _UN,
)


def test_public_exports_importable():
    import core.data_model as dm
    for name in [
        "HistoricalStore", "get_historical_store", "ProviderRecordAdapter",
        "ingest_via", "YFOHLCVAdapter", "df_to_bar_rows",
    ]:
        assert hasattr(dm, name), f"missing export: {name}"


def test_end_to_end_ingest_then_as_of(tmp_path):
    from core.data_model import HistoricalStore, YFOHLCVAdapter, df_to_bar_rows, ingest_via, UNAVAILABLE
    store = HistoricalStore(path=tmp_path / "t.db")
    adapter = YFOHLCVAdapter("AAPL")
    ingest_via(adapter, store, df_to_bar_rows(_sample_df()))
    t_before = datetime.fromisoformat("2026-01-05T14:00:00+00:00")
    t_mid = datetime.fromisoformat("2026-01-05T14:37:00+00:00")
    t_after = datetime.fromisoformat("2026-01-05T15:00:00+00:00")
    assert store.as_of("OHLCVBar", "AAPL", t_before) is UNAVAILABLE
    mid = store.as_of("OHLCVBar", "AAPL", t_mid)
    assert mid is not UNAVAILABLE
    assert mid["close"] == 101  # 14:35 bar is the newest <= 14:37
    after = store.as_of("OHLCVBar", "AAPL", t_after)
    assert after is not UNAVAILABLE
    assert after["close"] == 102  # 14:40 bar
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_data_model_ingest.py -v`
Expected: FAIL with `ImportError: cannot import name 'HistoricalStore' from 'core.data_model'`.

- [ ] **Step 3: Write minimal implementation**

In `core/data_model/__init__.py`, after the existing `quality` import block, add:

```python
from core.data_model.store import HistoricalStore, get_historical_store
from core.data_model.ingest import (
    ProviderRecordAdapter,
    YFOHLCVAdapter,
    df_to_bar_rows,
    ingest_via,
)
```

And extend the `__all__` list to include the new names. (Read the current `__all__` first and append: `"HistoricalStore"`, `"get_historical_store"`, `"ProviderRecordAdapter"`, `"YFOHLCVAdapter"`, `"df_to_bar_rows"`, `"ingest_via"`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_data_model_ingest.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add core/data_model/__init__.py tests/unit/test_data_model_ingest.py
git commit -m "feat(phase1b): public exports + end-to-end ingest->as_of proof

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Control-doc updates (V9_ARCHITECTURE.md, V9_IMPLEMENTATION.md)

**Files:**
- Modify: `docs/V9_ARCHITECTURE.md` (add Phase 1B section)
- Modify: `docs/V9_IMPLEMENTATION.md` (add Phase 1B record; Phase 1 row stays IN PROGRESS)
- Test: none (documentation; verified by reading).

**Interfaces:** none.

- [ ] **Step 1: Update V9_ARCHITECTURE.md**

Read `docs/V9_ARCHITECTURE.md`, find the "## Canonical data layer (Phase 1A)" section added in 1A, and append a new section immediately after it:

```markdown
## Point-in-time historical store (Phase 1B)

`core/data_model/store.py` — `HistoricalStore`: an append-only SQLite store (same
DB file / WAL pattern as `core/sqlite_store.py`) holding serialized canonical §27
entities keyed by `(entity_type, symbol, provider, available_from, version)`.
This is the "valid point-in-time source" spec §32 references — each fetched fact
is written once and never mutated (corrections = new higher-`version` row).

- `append(record, ...)` — append one entity/dict with its `available_from`/`version`/`fetched_at`; no update/delete API (append-only by construction).
- `as_of(entity_type, symbol, T, *, prefer="available_from")` — newest record knowable at T, or `UNAVAILABLE`; delegates tie-break to the 1A `as_of` (DRY).
- `as_of_range(entity_type, symbol, T_start, T_end, *, prefer)` — ascending window series (the backtest replay shape).
- `detect_conflicts(entity_type, symbol, T, *, tolerance_pct, value_field)` — cross-provider disagreement as-of T → 1A `Conflict` objects (§33).
- `query(...)` — filtered diagnostic read.

`core/data_model/ingest.py` — opt-in ingestion seam (§7 wrap, §23 no-replace):
`ProviderRecordAdapter` protocol + `ingest_via` + `df_to_bar_rows` + `YFOHLCVAdapter`
(proof: yfinance bars → `OHLCVBar`). Adapters never call providers or see credentials (§106);
the working V7 clients are untouched. Adapters for other providers/entities land with the
phases that consume them (5/7/10). Survivorship (§30) + corporate actions (§31) → Phase 1C.
```

- [ ] **Step 2: Update V9_IMPLEMENTATION.md**

In `docs/V9_IMPLEMENTATION.md`, add a new `## Phase 1B — Point-in-Time Historical Store` record after the existing `## Phase 1A — ...` record, using the same field structure (Goal / Status / Delivered / Files created / Files modified / Files modified in V7 / Dependencies / Migrations / API changes / UI changes / Tests executed / Exact results / Out of scope / Blockers / Phase 1 row status). Set **Status: PASS** only after the Task 8 gate verifies it by execution. **Phase 1 row stays IN PROGRESS** (1C remains). Exact results and final status are filled in Task 8 after the regression run; for now write the record with the delivered/files fields and `Status: IN PROGRESS` (to be flipped to PASS at the gate).

- [ ] **Step 3: Commit**

```bash
git add docs/V9_ARCHITECTURE.md docs/V9_IMPLEMENTATION.md
git commit -m "docs(phase1b): architecture + implementation records

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: Phase 1B exit gate

**Files:**
- Modify: `docs/V9_IMPLEMENTATION.md` (flip Phase 1B Status to PASS, fill exact results)
- Test: full regression + sweeps.

**Interfaces:** none.

- [ ] **Step 1: Verify V7 untouched**

Run:
```bash
git -C "C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium" rev-parse HEAD
git -C "C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium" status --porcelain | measure -l   # PowerShell: (count). For bash: git -C ... status --porcelain | wc -l
```
Expected: HEAD = `f912a33`; 109 dirty files (unchanged from Phase 0/1A). V7 must show no change attributable to 1B.

- [ ] **Step 2: Verify no new open-source deps (§118)**

Run:
```bash
git diff 41bf30b..HEAD -- requirements.txt requirements-dev.txt pyproject.toml
```
Expected: empty diff. (`core/data_model/store.py` uses stdlib `sqlite3`/`json`/`dataclasses`/`typing`; `ingest.py` uses pandas which is already a V7 dep.)

- [ ] **Step 3: §4 forbidden-language sweep over new/modified 1B files**

Run (bash):
```bash
grep -rniE "mostly complete|approximately complete|backend finished|frontend pending|should work|probably works|mock for now|temporary workaround|untested but implemented|todo later|99%" core/data_model/store.py core/data_model/ingest.py core/data_model/__init__.py tests/unit/test_data_model_store.py tests/unit/test_data_model_ingest.py
```
Expected: no matches (`# populate in later phase` sanctioned stubs are not forbidden; none are used in 1B code anyway).

- [ ] **Step 4: §106 secrets sweep**

Run (bash):
```bash
grep -niE "api[_-]?key|secret|token|password|credential" core/data_model/store.py core/data_model/ingest.py
```
Expected: no matches (adapters never touch credentials; the store stores entity payloads only).

- [ ] **Step 5: All public symbols import**

Run:
```bash
python -c "from core.data_model import HistoricalStore, get_historical_store, ProviderRecordAdapter, ingest_via, YFOHLCVAdapter, df_to_bar_rows, as_of, UNAVAILABLE, PointInTime, OHLCVBar; print('ok')"
```
Expected: `ok`.

- [ ] **Step 6: Full regression**

Run:
```bash
python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf
```
Expected: 1604 (1A baseline) + new 1B tests (5 store-T1 + 4 as_of-T2 + 3 as_of_range-T3 + 4 conflicts-T4 = 16 store; 7 ingest-T5 + 2 ingest-T6 = 9 ingest) = 1604 + 25 = **1629 passed / 0 failed / exit 0**. No existing test weakened, dropped, or skipped (§115).

- [ ] **Step 7: Flip Phase 1B Status to PASS + fill exact results**

Edit `docs/V9_IMPLEMENTATION.md` Phase 1B record: set `Status: PASS`, fill `Exact results: 1629 passed / 0 failed / exit 0` (use the actual number from Step 6), and confirm the Phase 1 row remains IN PROGRESS (1C remains).

- [ ] **Step 8: Commit the gate**

```bash
git add docs/V9_IMPLEMENTATION.md
git commit -m "test(phase1b): exit gate PASS — regression green, V7 untouched, no new deps

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage:**
- §29 (point-in-time, "use only data available at T") → `HistoricalStore.as_of` / `as_of_range` over persisted records keyed on `available_from`. ✓ (Tasks 2–3)
- §32 (UNAVAILABLE unless a valid point-in-time source exists) → the store IS that source; `as_of` returns `UNAVAILABLE` on miss. ✓ (Task 2)
- §33 (conflicts) → `detect_conflicts` emits 1A `Conflict` at the store level. ✓ (Task 4)
- §27 (canonical entities) → store holds serialized §27 entities; proof adapter produces `OHLCVBar`. ✓ (Tasks 1, 5)
- §7 / §23 (extend, wrap-not-replace) → follows `sqlite_store.py`; adapters wrap existing clients opt-in; no client rewritten. ✓ (all tasks)
- §106 (no secrets) → adapters never see credentials; store holds entity payloads only. ✓ (Task 8 Step 4)
- §115 (no test weakening) → new tests only; existing 1604 unchanged. ✓ (Task 8 Step 6)
- §117 (V7 read-only) → all files in V9 `core/data_model/`; verified at gate. ✓ (Task 8 Step 1)
- §118 (no new deps) → stdlib + pandas (already a V7 dep); verified empty requirements diff. ✓ (Task 8 Step 2)
- §4 (no forbidden language) → sweep at gate. ✓ (Task 8 Step 3)
- §30 / §31 (survivorship / corporate actions) → explicitly deferred to 1C (design §7). Not in 1B scope. ✓

**2. Placeholder scan:** No "TBD"/"TODO"/"implement later". The `# populate in later phase` sanctioned stub is not used in 1B code. Deferrals are documented with phase owners in the design doc and in the `V9_ARCHITECTURE.md` text (Task 7). Clean.

**3. Type consistency:**
- `_RowView` (payload, pit) defined Task 1, consumed Tasks 2, 3, 4 — same attribute names `.payload`, `.pit`. ✓
- `_iso` / `_from_iso` defined Task 1, used Tasks 1–4. ✓
- `HistoricalStore.append` signature (Task 1) — `available_from`/`version`/`fetched_at` kwargs — used by `ingest_via` (Task 5) which calls `store.append(entity, provider=adapter.provider, symbol=symbol)`. ✓
- `as_of` store method (Task 2) returns `best.payload` (a dict) or `UNAVAILABLE`; tests assert `res["close"]` and `res is UNAVAILABLE`. ✓
- `Conflict` (Task 4) constructed with `field, values, sources, deviation_pct` — matches `core.data_model.quality.Conflict`. ✓
- `YFOHLCVAdapter` (Task 5) builds `OHLCVBar(symbol=Symbol(...), timestamp=ts, ..., pit=PointInTime(...))` — matches the 1A `OHLCVBar` fields (`symbol, timestamp, open, high, low, close, volume, adjusted, resolution, pit`) and `Symbol` (`ticker, exchange, asset_class, currency, isin`). ✓
- `df_to_bar_rows` returns dicts with key `"ts"` (lowercase); `YFOHLCVAdapter.to_entity` reads `raw["ts"]`. ✓
- `__init__.py` exports (Task 6) match the names the Task 6 test imports. ✓
- Expected test counts: T1=5, T2=4 (→9), T3=3 (→12), T4=4 (→16) store tests; T5=7, T6=2 (→9) ingest tests; total new = 25; gate expects 1604+25=1629. ✓

**4. Ambiguity check:**
- `available_from` for bars = bar timestamp (conservative proof choice); documented in design §6 and in `YFOHLCVAdapter` docstring; not a 1B defect. ✓
- `provider` default `"unknown"` when neither kwarg nor `record.provider` present. ✓
- `detect_conflicts` `value_field` default `"close"`; non-numeric entities return `[]` (documented). ✓
- Store `as_of` loads all rows for `(entity_type, symbol)` then delegates to 1A `as_of` (no SQL T pre-filter) — correct-by-construction; SQL-side LIMIT parked for Phase 7 (design §6). ✓

No issues found. Plan is complete.