# Phase 1C — Corporate Actions (§31) + Survivorship Registry (§30) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the Phase 1 spec gaps: §31 corporate-action capture (record splits/dividends in the point-in-time store + a documented adjusted/raw price convention) and §30 survivorship (a registry scaffold with point-in-time `is_listed`/`universe_at` queries + a loudly documented limitation — no fake delisting data). Backward-compatible; keeps the 1629-test green baseline green. Final Phase 1 sub-plan (flips Phase 1 row to PASS).

**Architecture:** A new `SurvivorshipRegistry` (SQLite `symbol_lifecycle` table, same DB/WAL pattern as `sqlite_store.py`/`HistoricalStore`) records per-symbol lifecycle events (LISTED/DELISTED/TICKER_CHANGE) with point-in-time queries. A `YFCorporateActionAdapter` (additive to `core/data_model/ingest.py`) wraps a yfinance `.actions` DataFrame → `CorporateAction` entities → the 1B `HistoricalStore`. `HistoricalStore.actions_as_of(symbol, T)` returns actions knowable at T. The adjusted/raw convention is documented; `YFOHLCVAdapter.adjusted` flips default `False`→`True` to match it. The backtester gets one §30 comment (no behavior change); universe rewiring is deferred to Phase 7.

**Tech Stack:** Python 3.14.3, stdlib `sqlite3` + `json` + `dataclasses` + `typing` (no new open-source deps — §118), pandas (already a V7 dep, used in `actions_df_to_records`), pytest.

## Global Constraints

Copied verbatim from `prompt_to_follow.md`; every task's requirements implicitly include these:

- **§117:** V7 at `C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium` (HEAD f912a33, 109 dirty) is READ-ONLY — never modify anything in V7; everything happens in V9.
- **§116:** no remote push (repo has no remote).
- **§106:** never commit API keys / store secrets / expose keys to frontend / expose broker credentials to Coding AI / log tokens. Adapters never see credentials.
- **§115:** no skip/xfail critical test / `assert True` / delete failing test / weaken assertion.
- **§4:** forbidden success language — "mostly complete", "approximately complete", "99%", "should work", "probably works", "backend finished", "frontend pending", "TODO later", "mock for now", "temporary workaround", "untested but implemented". `# populate in later phase` sanctioned stubs (with a phase owner) are NOT forbidden.
- **§5 / §7:** no rewrite — evolve V7 (additive only; existing call sites unchanged); prefer EXTEND → MIGRATE → TEST → REFACTOR.
- **§23:** do NOT replace working V7 providers; adapters wrap, never replace.
- **§30:** survivorship — account for delisted/index membership/ticker changes/mergers/bankruptcies; **if unavailable, document limitation; do not pretend it is solved.**
- **§31:** corporate actions — handle splits/dividends/ticker changes/mergers; **define adjusted/raw price conventions clearly.**
- **§118:** Phase 2 is the open-source manifest phase, so Phase 1C adds NO new open-source dependencies. stdlib only (+ pandas already in V7).
- **Phase 1B baseline:** 1629 passed / 0 failed / exit 0 (commit 10a3ef6). Phase 1C must keep this green and add new tests only.
- **Existing 1A/1B symbols this plan consumes (do not redefine):** `core.data_model.entities.CorporateAction` (frozen dataclass; fields `symbol: Symbol, ex_date: datetime, type: str, ratio: Optional[float]=None, amount: Optional[float]=None, from_ticker: Optional[str]=None, to_ticker: Optional[str]=None, pit: Optional[PointInTime]=None`); `core.data_model.entities.OHLCVBar` (frozen; has `adjusted: bool = False` field); `core.data_model.entities.Symbol`, `get_exchange`, `EXCHANGES`; `core.data_model.pit.PointInTime` (7 fields, `__post_init__` raises on tz-naive), `to_utc`, `UNAVAILABLE`; `core.data_model.store.HistoricalStore` (`append(record, *, entity_type=None, symbol=None, provider=None, available_from=None, version=None, fetched_at=None)`, `query`, `as_of`, `as_of_range`, `detect_conflicts`); `core.data_model.ingest.ProviderRecordAdapter`, `YFOHLCVAdapter` (1B; `__init__(symbol, *, interval="5m", exchange_mic="XNYS", fetched_at=None)`, `entity_type="OHLCVBar"`, `provider="yfinance"`, `to_entity(raw)->Optional[OHLCVBar]`), `df_to_bar_rows`, `ingest_via`.
- **Existing SQLite pattern to follow (§7):** `core/sqlite_store.py` / `core/data_model/store.py` — singleton, `MS_SQLITE_DB_PATH` env (default `data/market_signals.db`), `PRAGMA journal_mode=WAL; synchronous=FULL; busy_timeout=5000`, `check_same_thread=False`, `RLock`, `CREATE TABLE IF NOT EXISTS`.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `core/data_model/survivorship.py` (NEW) | `SurvivorshipRegistry` + `get_survivorship_registry`. | 1 |
| `core/data_model/ingest.py` (MODIFY, additive) | `actions_df_to_records`, `YFCorporateActionAdapter`, `adjusted` param on `YFOHLCVAdapter`. | 2, 4 |
| `core/data_model/store.py` (MODIFY, additive) | `actions_as_of(symbol, T)`. | 3 |
| `core/data_model/__init__.py` (MODIFY, additive) | Export `SurvivorshipRegistry`, `get_survivorship_registry`, `YFCorporateActionAdapter`. | 5 |
| `core/backtester.py` (MODIFY, comment only) | §30 limitation comment at universe-construction site. | 5 |
| `docs/V9_ARCHITECTURE.md` (MODIFY, additive) | Corporate-actions + survivorship section (convention + §30 limitation). | 5 |
| `docs/V9_IMPLEMENTATION.md` (MODIFY, additive) | Phase 1C record; Phase 1 row → PASS. | 5, 6 |
| `tests/unit/test_data_model_survivorship.py` (NEW) | Registry tests. | 1 |
| `tests/unit/test_data_model_corporate_actions.py` (NEW) | Adapter + `actions_as_of` + adjusted-flag tests. | 2, 3, 4 |

---

### Task 1: SurvivorshipRegistry

**Files:**
- Create: `core/data_model/survivorship.py`
- Test: `tests/unit/test_data_model_survivorship.py`

**Interfaces:**
- Consumes: `core.data_model.pit.to_utc`.
- Produces: `SurvivorshipRegistry` (`register_listed(symbol, *, event_time=None, source="registry")`, `register_delisted(...)`, `register_ticker_change(from_symbol, to_symbol, *, event_time=None, source="registry")`, `is_listed(symbol, T) -> bool`, `universe_at(T, base_symbols) -> set[str]`); `get_survivorship_registry()`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_data_model_survivorship.py`:

```python
from datetime import datetime, timezone

from core.data_model.survivorship import SurvivorshipRegistry


def _iso(s):
    return datetime.fromisoformat(s)


def test_is_listed_true_after_register_listed(tmp_path):
    reg = SurvivorshipRegistry(path=tmp_path / "t.db")
    reg.register_listed("AAPL", event_time=_iso("2020-01-01T00:00:00+00:00"))
    assert reg.is_listed("AAPL", _iso("2026-01-01T00:00:00+00:00")) is True


def test_is_listed_false_before_listing(tmp_path):
    reg = SurvivorshipRegistry(path=tmp_path / "t.db")
    reg.register_listed("AAPL", event_time=_iso("2020-01-01T00:00:00+00:00"))
    assert reg.is_listed("AAPL", _iso("2019-01-01T00:00:00+00:00")) is False


def test_is_listed_false_after_delisting(tmp_path):
    reg = SurvivorshipRegistry(path=tmp_path / "t.db")
    reg.register_listed("ENRN", event_time=_iso("1999-01-01T00:00:00+00:00"))
    reg.register_delisted("ENRN", event_time=_iso("2001-12-01T00:00:00+00:00"))
    assert reg.is_listed("ENRN", _iso("2000-06-01T00:00:00+00:00")) is True
    assert reg.is_listed("ENRN", _iso("2002-06-01T00:00:00+00:00")) is False


def test_is_listed_unknown_symbol_is_false(tmp_path):
    reg = SurvivorshipRegistry(path=tmp_path / "t.db")
    assert reg.is_listed("NOPE", _iso("2026-01-01T00:00:00+00:00")) is False  # §32: unknown -> not listed


def test_ticker_change_old_false_new_true(tmp_path):
    reg = SurvivorshipRegistry(path=tmp_path / "t.db")
    reg.register_listed("FB", event_time=_iso("2012-05-01T00:00:00+00:00"))
    reg.register_ticker_change("FB", "META", event_time=_iso("2022-06-09T00:00:00+00:00"))
    assert reg.is_listed("FB", _iso("2021-01-01T00:00:00+00:00")) is True   # before change
    assert reg.is_listed("FB", _iso("2023-01-01T00:00:00+00:00")) is False    # after change (old ticker gone)
    assert reg.is_listed("META", _iso("2023-01-01T00:00:00+00:00")) is True    # new ticker listed


def test_universe_at_filters_delisted(tmp_path):
    reg = SurvivorshipRegistry(path=tmp_path / "t.db")
    reg.register_listed("AAPL", event_time=_iso("2020-01-01T00:00:00+00:00"))
    reg.register_listed("ENRN", event_time=_iso("1999-01-01T00:00:00+00:00"))
    reg.register_delisted("ENRN", event_time=_iso("2001-12-01T00:00:00+00:00"))
    base = {"AAPL", "ENRN", "MSFT"}
    res = reg.universe_at(_iso("2026-01-01T00:00:00+00:00"), base)
    assert res == {"AAPL", "MSFT"}  # ENRN delisted, MSFT unknown -> excluded (§32)


def test_universe_at_returns_all_when_none_delisted(tmp_path):
    reg = SurvivorshipRegistry(path=tmp_path / "t.db")
    for s in ["AAPL", "MSFT", "GOOG"]:
        reg.register_listed(s, event_time=_iso("2020-01-01T00:00:00+00:00"))
    base = {"AAPL", "MSFT", "GOOG"}
    assert reg.universe_at(_iso("2026-01-01T00:00:00+00:00"), base) == base
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_data_model_survivorship.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.data_model.survivorship'`.

- [ ] **Step 3: Write minimal implementation**

Create `core/data_model/survivorship.py`:

```python
"""V9 survivorship registry (spec §30) — point-in-time symbol lifecycle.

Records per-symbol lifecycle events (LISTED / DELISTED / TICKER_CHANGE) with
point-in-time event_time, and answers is_listed(symbol, T) / universe_at(T).

§30 honest limitation: V9 has no comprehensive historical delisting / index-
membership database (§118 forbids adding one in Phase 1). This registry is a
SCAFFOLD — it captures delistings/ticker-changes AS THEY ARE LEARNED (manually
or via a future Phase 2+ data source). Historical backtests before the
registry's first record remain survivorship-biased for symbols not in the
current watchlist; this is documented, NOT pretended solved (§30). The
backtest universe rewiring (calling universe_at(T)) is Phase 7.

Follows the core/sqlite_store.py + core/data_model/store.py WAL/singleton
pattern (§7, §118 stdlib-only).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Set

from core.data_model.pit import to_utc

logger = logging.getLogger("SurvivorshipRegistry")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DB_PATH = Path(os.getenv("MS_SQLITE_DB_PATH", BASE_DIR / "data" / "market_signals.db"))


def _from_iso(s):
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


class SurvivorshipRegistry:
    """Append-only point-in-time symbol-lifecycle registry. Singleton via
    get_survivorship_registry(). No update/delete API (corrections = new row)."""

    _instance: Optional["SurvivorshipRegistry"] = None
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
            CREATE TABLE IF NOT EXISTS symbol_lifecycle (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT    NOT NULL,
                event       TEXT    NOT NULL,
                event_time  TEXT    NOT NULL,
                to_ticker   TEXT,
                source      TEXT    NOT NULL DEFAULT 'registry',
                UNIQUE(symbol, event, event_time)
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_sym_life "
            "ON symbol_lifecycle(symbol, event_time)"
        )
        self._conn.commit()

    @classmethod
    def instance(cls) -> "SurvivorshipRegistry":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = SurvivorshipRegistry()
        return cls._instance

    def _register(self, symbol: str, event: str, event_time, *, to_ticker=None, source="registry") -> None:
        ts = to_utc(event_time) if event_time is not None else datetime.now(timezone.utc)
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO symbol_lifecycle (symbol, event, event_time, to_ticker, source) "
                "VALUES (?, ?, ?, ?, ?)",
                (symbol, event, ts.isoformat(), to_ticker, source),
            )
            self._conn.commit()

    def register_listed(self, symbol: str, *, event_time=None, source="registry") -> None:
        self._register(symbol, "LISTED", event_time, source=source)

    def register_delisted(self, symbol: str, *, event_time=None, source="registry") -> None:
        self._register(symbol, "DELISTED", event_time, source=source)

    def register_ticker_change(self, from_symbol: str, to_symbol: str, *, event_time=None, source="registry") -> None:
        # old ticker marked as changed-away; new ticker becomes listed
        self._register(from_symbol, "TICKER_CHANGE", event_time, to_ticker=to_symbol, source=source)
        self._register(to_symbol, "LISTED", event_time, source=source)

    def is_listed(self, symbol: str, T) -> bool:
        """True iff the newest lifecycle event for `symbol` with event_time <= T
        is LISTED. DELISTED / TICKER_CHANGE / no-record -> False (§32: unknown
        is NOT silently treated as listed)."""
        T = to_utc(T)
        with self._lock:
            cur = self._conn.execute(
                "SELECT event, event_time FROM symbol_lifecycle WHERE symbol=? "
                "ORDER BY event_time ASC",
                (symbol,),
            )
            rows = cur.fetchall()
        latest = None
        for ev, et in rows:
            dt = _from_iso(et)
            if dt is None:
                continue
            dt = to_utc(dt)
            if dt <= T:
                latest = ev
        return latest == "LISTED"

    def universe_at(self, T, base_symbols: Iterable[str]) -> Set[str]:
        """Point-in-time universe: symbols from base_symbols listed at T."""
        return {s for s in base_symbols if self.is_listed(s, T)}


def get_survivorship_registry() -> SurvivorshipRegistry:
    return SurvivorshipRegistry.instance()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_data_model_survivorship.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add core/data_model/survivorship.py tests/unit/test_data_model_survivorship.py
git commit -m "feat(phase1c): SurvivorshipRegistry point-in-time lifecycle (§30)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: YFCorporateActionAdapter + actions_df_to_records

**Files:**
- Modify: `core/data_model/ingest.py` (add `actions_df_to_records` + `YFCorporateActionAdapter` + import `CorporateAction`)
- Test: `tests/unit/test_data_model_corporate_actions.py`

**Interfaces:**
- Consumes: `core.data_model.entities.CorporateAction`, `Symbol`, `get_exchange`; `core.data_model.pit.PointInTime`.
- Produces: `actions_df_to_records(df) -> list[dict]`; `YFCorporateActionAdapter(symbol, *, exchange_mic="XNYS", fetched_at=None)` (`entity_type="CorporateAction"`, `provider="yfinance"`, `to_entity(raw)->Optional[CorporateAction]`).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_data_model_corporate_actions.py`:

```python
from datetime import datetime, timezone

import pandas as pd

from core.data_model.entities import CorporateAction
from core.data_model.ingest import YFCorporateActionAdapter, actions_df_to_records
from core.data_model.store import HistoricalStore


def _actions_df():
    # yfinance .actions shape: DatetimeIndex + Dividends + "Stock Splits"
    idx = pd.to_datetime(["2026-01-15", "2026-03-15", "2026-05-15"])
    return pd.DataFrame(
        {"Dividends": [0.50, 0.0, 0.0], "Stock Splits": [0.0, 0.0, 2.0]},
        index=idx,
    )  # 2026-01-15 dividend; 2026-05-15 2-for-1 split; 2026-03-15 no action


def test_actions_df_to_records_dividend_and_split():
    recs = actions_df_to_records(_actions_df())
    assert len(recs) == 2  # the no-action row is skipped
    div = [r for r in recs if r["type"] == "DIVIDEND"][0]
    split = [r for r in recs if r["type"] == "SPLIT"][0]
    assert div["amount"] == 0.50 and div["ratio"] is None
    assert split["ratio"] == 2.0 and split["amount"] is None
    assert all(r["ex_date"].endswith("+00:00") for r in recs)


def test_actions_df_to_records_empty_and_none():
    assert actions_df_to_records(None) == []
    assert actions_df_to_records(pd.DataFrame()) == []


def test_yf_corporate_action_adapter_to_entity():
    adapter = YFCorporateActionAdapter("AAPL")
    recs = actions_df_to_records(_actions_df())
    ca = adapter.to_entity(recs[0])
    assert isinstance(ca, CorporateAction)
    assert ca.symbol.ticker == "AAPL"
    assert ca.type in ("DIVIDEND", "SPLIT")
    assert ca.pit is not None
    assert ca.pit.available_from is not None
    assert ca.pit.version == 1


def test_yf_corporate_action_adapter_skips_invalid():
    adapter = YFCorporateActionAdapter("AAPL")
    assert adapter.to_entity({"no_ex_date": 1}) is None
    assert adapter.to_entity("not a dict") is None


def test_yf_corporate_action_adapter_is_provider_record_adapter():
    adapter = YFCorporateActionAdapter("AAPL")
    assert adapter.entity_type == "CorporateAction"
    assert adapter.provider == "yfinance"
    assert callable(adapter.to_entity)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_data_model_corporate_actions.py -v`
Expected: FAIL with `ImportError: cannot import name 'YFCorporateActionAdapter'` (or `actions_df_to_records`).

- [ ] **Step 3: Write minimal implementation**

In `core/data_model/ingest.py`, add `CorporateAction` to the entities import line:

```python
from core.data_model.entities import CorporateAction, OHLCVBar, Symbol, get_exchange
```

Append at the end of the file (after `ingest_via`):

```python
def actions_df_to_records(df: Any) -> list:
    """Normalize a yfinance .actions DataFrame (DatetimeIndex + 'Dividends' +
    'Stock Splits' columns) into action dicts: {"ex_date": iso-UTC, "type":
    "DIVIDEND"|"SPLIT", "amount", "ratio"}. Rows with no action are skipped.
    None/empty -> []."""
    if df is None:
        return []
    try:
        if df.empty:
            return []
    except Exception:
        return []
    import pandas as pd
    df = df.copy()  # do not mutate the caller's DataFrame
    if isinstance(df.index, pd.DatetimeIndex):
        df.index = df.index.tz_localize("UTC") if df.index.tz is None else df.index.tz_convert("UTC")
    out = []
    for ts, row in df.iterrows():
        ts_iso = pd.Timestamp(ts).isoformat()
        div = float(row.get("Dividends", 0.0) or 0.0)
        split = float(row.get("Stock Splits", 0.0) or 0.0)
        if div != 0.0:
            out.append({"ex_date": ts_iso, "type": "DIVIDEND", "amount": div, "ratio": None})
        if split != 0.0:
            out.append({"ex_date": ts_iso, "type": "SPLIT", "ratio": split, "amount": None})
    return out


class YFCorporateActionAdapter:
    """Proof adapter: a yfinance action dict -> CorporateAction (§31) with an
    explicit PointInTime (event_time=available_from=ex_date). Wrap-not-replace
    (§23); never sees credentials (§106)."""
    entity_type = "CorporateAction"
    provider = "yfinance"

    def __init__(self, symbol: str, *, exchange_mic: str = "XNYS", fetched_at: Optional[datetime] = None):
        self.symbol = str(symbol or "").upper().strip()
        self.exchange_mic = exchange_mic
        self.fetched_at = fetched_at or datetime.now(timezone.utc)

    def to_entity(self, raw: Any) -> Optional[CorporateAction]:
        if not isinstance(raw, dict) or "ex_date" not in raw or "type" not in raw:
            return None
        try:
            ex_date = datetime.fromisoformat(raw["ex_date"])
        except (ValueError, TypeError):
            return None
        if ex_date.tzinfo is None:
            ex_date = ex_date.replace(tzinfo=timezone.utc)
        ex = get_exchange(self.exchange_mic)
        if ex is None:
            return None
        sym = Symbol(ticker=self.symbol, exchange=ex, asset_class=ex.asset_class, currency=ex.currency)
        pit = PointInTime(event_time=ex_date, available_from=ex_date, fetched_at=self.fetched_at, version=1)
        return CorporateAction(
            symbol=sym,
            ex_date=ex_date,
            type=raw["type"],
            ratio=raw.get("ratio"),
            amount=raw.get("amount"),
            from_ticker=raw.get("from_ticker"),
            to_ticker=raw.get("to_ticker"),
            pit=pit,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_data_model_corporate_actions.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add core/data_model/ingest.py tests/unit/test_data_model_corporate_actions.py
git commit -m "feat(phase1c): YFCorporateActionAdapter -> CorporateAction capture (§31)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: HistoricalStore.actions_as_of(symbol, T)

**Files:**
- Modify: `core/data_model/store.py` (add `actions_as_of`)
- Test: `tests/unit/test_data_model_corporate_actions.py` (append)

**Interfaces:**
- Produces: `HistoricalStore.actions_as_of(symbol, T, *, prefer="available_from") -> list[dict]` (all CorporateAction payloads with `prefer <= T`, ascending).

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_data_model_corporate_actions.py`:

```python
from core.data_model.ingest import ingest_via
from core.data_model import UNAVAILABLE


def test_actions_as_of_returns_only_past_actions(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    adapter = YFCorporateActionAdapter("AAPL")
    ingest_via(adapter, store, actions_df_to_records(_actions_df()))
    # actions: 2026-01-15 dividend, 2026-05-15 split
    T_before_split = datetime.fromisoformat("2026-03-01T00:00:00+00:00")
    res = store.actions_as_of("AAPL", T_before_split)
    assert len(res) == 1  # only the 2026-01-15 dividend; split is future
    assert res[0]["type"] == "DIVIDEND"
    T_after_split = datetime.fromisoformat("2026-06-01T00:00:00+00:00")
    res2 = store.actions_as_of("AAPL", T_after_split)
    assert len(res2) == 2
    assert [r["type"] for r in res2] == ["DIVIDEND", "SPLIT"]  # ascending by ex_date


def test_actions_as_of_empty_when_none(tmp_path):
    store = HistoricalStore(path=tmp_path / "t.db")
    assert store.actions_as_of("AAPL", datetime.fromisoformat("2026-06-01T00:00:00+00:00")) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_data_model_corporate_actions.py -v`
Expected: FAIL with `AttributeError: 'HistoricalStore' object has no attribute 'actions_as_of'`.

- [ ] **Step 3: Write minimal implementation**

Add this method to the `HistoricalStore` class in `core/data_model/store.py` (after `detect_conflicts`):

```python
    def actions_as_of(
        self,
        symbol: str,
        T: Any,
        *,
        prefer: str = "available_from",
    ) -> List[Dict[str, Any]]:
        """Corporate actions for `symbol` knowable at T (§29), ascending by
        `prefer`. Returns the deserialized CorporateAction payloads with
        pit.<prefer> <= T."""
        T = to_utc(T)
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload, available_from, version, event_time, fetched_at "
                "FROM pit_records WHERE entity_type = ? AND symbol = ? "
                "ORDER BY available_from ASC, version ASC",
                ("CorporateAction", symbol),
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
            if ts <= T:
                out.append(json.loads(payload_s))
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_data_model_corporate_actions.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add core/data_model/store.py tests/unit/test_data_model_corporate_actions.py
git commit -m "feat(phase1c): HistoricalStore.actions_as_of (§29 point-in-time actions)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: YFOHLCVAdapter.adjusted flag (§31 convention)

**Files:**
- Modify: `core/data_model/ingest.py` (`YFOHLCVAdapter.__init__` gains `adjusted: bool = True`; `to_entity` sets `OHLCVBar.adjusted`)
- Test: `tests/unit/test_data_model_corporate_actions.py` (append) — note: the adjusted-flag tests live here for cohesion, or in the ingest test file; this plan puts them in the corporate-actions file since Task 4 is bundled with the §31 convention. (Acceptable either location.)

**Interfaces:**
- Produces: `YFOHLCVAdapter(..., adjusted=True)`; `to_entity` sets `OHLCVBar.adjusted = self.adjusted`.

**Behavior note (documented):** 1B's `YFOHLCVAdapter` left `OHLCVBar.adjusted` at its dataclass default `False`. 1C flips the adapter default to `True` to match the documented V9 convention (adjusted prices for signal/backtest work, consistent with yfinance `auto_adjust=True`). The 1B ingest tests assert on `close`/`symbol`/`pit` only — they do NOT assert `adjusted == False` — so this flip is backward-compatible for the test suite.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_data_model_corporate_actions.py`:

```python
from core.data_model.ingest import YFOHLCVAdapter, df_to_bar_rows


def _sample_bars():
    idx = pd.to_datetime(["2026-01-05T14:30:00"])
    return pd.DataFrame(
        {"Open": [99], "High": [101], "Low": [98], "Close": [100], "Volume": [1000]},
        index=idx,
    )


def test_yf_ohlcv_adapter_adjusted_default_true():
    adapter = YFOHLCVAdapter("AAPL")
    bar = adapter.to_entity(df_to_bar_rows(_sample_bars())[0])
    assert bar.adjusted is True  # 1C convention: adjusted is the default


def test_yf_ohlcv_adapter_adjusted_false_when_requested():
    adapter = YFOHLCVAdapter("AAPL", adjusted=False)
    bar = adapter.to_entity(df_to_bar_rows(_sample_bars())[0])
    assert bar.adjusted is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_data_model_corporate_actions.py -v -k adjusted`
Expected: FAIL — `bar.adjusted is True` fails because the 1B adapter leaves `adjusted` at the dataclass default `False`.

- [ ] **Step 3: Write minimal implementation**

In `core/data_model/ingest.py`, change the `YFOHLCVAdapter.__init__` signature and `to_entity`:

`__init__`:
```python
    def __init__(
        self,
        symbol: str,
        *,
        interval: str = "5m",
        exchange_mic: str = "XNYS",
        fetched_at: Optional[datetime] = None,
        adjusted: bool = True,
    ):
        self.symbol = str(symbol or "").upper().strip()
        self.interval = interval
        self.exchange_mic = exchange_mic
        self.fetched_at = fetched_at or datetime.now(timezone.utc)
        self.adjusted = adjusted
```

In `to_entity`, add `adjusted=self.adjusted` to the `OHLCVBar(...)` call:
```python
        return OHLCVBar(
            symbol=sym,
            timestamp=ts,
            open=float(raw.get("open", 0.0)),
            high=float(raw.get("high", 0.0)),
            low=float(raw.get("low", 0.0)),
            close=float(raw.get("close", 0.0)),
            volume=float(raw.get("volume", 0.0)),
            resolution=self.interval,
            adjusted=self.adjusted,
            pit=pit,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_data_model_corporate_actions.py tests/unit/test_data_model_ingest.py -v`
Expected: PASS (9 corporate-action tests + 9 1B ingest tests; the 1B ingest tests still pass because they do not assert `adjusted`).

- [ ] **Step 5: Commit**

```bash
git add core/data_model/ingest.py tests/unit/test_data_model_corporate_actions.py
git commit -m "feat(phase1c): YFOHLCVAdapter.adjusted flag (§31 adjusted/raw convention)

Default True (matches yfinance auto_adjust=True + documented V9 convention);
explicitly settable to False for raw bars. 1B ingest tests unaffected.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Exports + backtester §30 comment + control docs

**Files:**
- Modify: `core/data_model/__init__.py` (export `SurvivorshipRegistry`, `get_survivorship_registry`, `YFCorporateActionAdapter`)
- Modify: `core/backtester.py` (additive §30 comment at universe-construction site, no behavior change)
- Modify: `docs/V9_ARCHITECTURE.md` (corporate-actions + survivorship section)
- Modify: `docs/V9_IMPLEMENTATION.md` (Phase 1C record; Phase 1 row stays IN PROGRESS until Task 6 gate flips it to PASS)
- Test: none (docs + comment + exports; verified by import test below).

**Interfaces:** none.

- [ ] **Step 1: Add exports**

In `core/data_model/__init__.py`, add to the imports:

```python
from core.data_model.survivorship import SurvivorshipRegistry, get_survivorship_registry
```

Add `"SurvivorshipRegistry"`, `"get_survivorship_registry"` to `__all__`. `YFCorporateActionAdapter` is already exported from `ingest` via the existing `from core.data_model.ingest import (...)` block — add `"YFCorporateActionAdapter"` to that import line and to `__all__`.

- [ ] **Step 2: Add the §30 comment in backtester.py**

Read `core/backtester.py` around the universe construction (the lines `watchlist = self.config.watchlist` / `all_tickers = watchlist.get("yahoo", []) + watchlist.get("binance", [])`). Insert an additive comment between them:

```python
        watchlist = self.config.watchlist
        # §30 survivorship limitation (documented, not pretended solved): this
        # universe is the CURRENT watchlist (current survivors). Historical
        # backtests are therefore survivorship-biased for symbols not in the
        # current watchlist and for periods before the SurvivorshipRegistry's
        # first record (core/data_model/survivorship.py). No comprehensive
        # historical delisting DB exists in V9 yet (§118). Point-in-time
        # universe filtering via SurvivorshipRegistry.universe_at(T) is wired
        # in Phase 7 (backtest consolidation). No behavior change in 1C.
        all_tickers = watchlist.get("yahoo", []) + watchlist.get("binance", [])
```

- [ ] **Step 3: Update V9_ARCHITECTURE.md**

After the "Point-in-time historical store (Phase 1B)" section, add:

```markdown
## Corporate actions + survivorship (Phase 1C)

**Adjusted/raw price convention (§31).** V9 uses **adjusted** OHLCV bars
(split- and dividend-adjusted) as the default for signal generation and
backtesting, consistent with the yfinance default `auto_adjust=True`. The
`OHLCVBar.adjusted` flag (§27) records whether a bar is adjusted (`True`,
the `YFOHLCVAdapter` default) or raw (`False`, available by passing
`auto_adjust=False` to the underlying fetcher). The `CorporateAction` records
in the point-in-time store are the audit trail for *why* adjusted differs from
raw for a symbol as of a given time. A backtest at historical time T uses
`HistoricalStore.actions_as_of(symbol, T)` to know which actions have occurred
up to T (§29) — it never reconstructs actions from future data (§32).

**Survivorship (§30).** `core/data_model/survivorship.py` —
`SurvivorshipRegistry` records per-symbol lifecycle events (LISTED /
DELISTED / TICKER_CHANGE) with point-in-time `event_time`, and answers
`is_listed(symbol, T)` and `universe_at(T, base_symbols)`. §30 honest
limitation: V9 has no comprehensive historical delisting / index-membership
database (§118 forbids adding one in Phase 1). The registry is a SCAFFOLD —
it captures delistings/ticker-changes as they are learned (manually or via a
future Phase 2+ data source). Historical backtests before the registry's first
record remain survivorship-biased for symbols not in the current watchlist;
this is documented at `core/backtester.py` (universe construction) and here,
NOT pretended solved. Wiring the backtest universe to
`SurvivorshipRegistry.universe_at(T)` + `actions_as_of(T)` is Phase 7
(backtest consolidation).
```

- [ ] **Step 4: Update V9_IMPLEMENTATION.md**

Add a `## Phase 1C — Corporate Actions (§31) + Survivorship Registry (§30)` record after the Phase 1B record, using the same field structure. Set **Status: IN PROGRESS** (flipped to PASS at the Task 6 gate). Fill the delivered/files/exact-results fields; exact results are finalized at the gate. Keep the **Phase 1 row IN PROGRESS** until Task 6.

- [ ] **Step 5: Verify imports + commit**

Run:
```bash
python -c "from core.data_model import SurvivorshipRegistry, get_survivorship_registry, YFCorporateActionAdapter, HistoricalStore; print('ok')"
python -m pytest tests/unit/test_data_model_survivorship.py tests/unit/test_data_model_corporate_actions.py tests/unit/test_data_model_store.py tests/unit/test_data_model_ingest.py -q
```
Expected: `ok`; all 1B+1C unit tests pass (25 + 7 + 9 = 41).

```bash
git add core/data_model/__init__.py core/backtester.py docs/V9_ARCHITECTURE.md docs/V9_IMPLEMENTATION.md
git commit -m "docs(phase1c): exports + backtester §30 comment + control docs

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Phase 1C exit gate + Phase 1 row → PASS

**Files:**
- Modify: `docs/V9_IMPLEMENTATION.md` (flip Phase 1C Status to PASS; flip Phase 1 row to PASS)
- Test: full regression + sweeps.

- [ ] **Step 1: Verify V7 untouched**

Run:
```bash
git -C "C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium" rev-parse HEAD
git -C "C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium" status --porcelain | wc -l
```
Expected: HEAD = `f912a33`; 109 dirty (unchanged).

- [ ] **Step 2: Verify no new deps (§118)**

Run:
```bash
git diff 10a3ef6..HEAD -- requirements.txt requirements-dev.txt pyproject.toml
```
Expected: empty diff (survivorship.py stdlib-only; ingest additions use pandas already in V7).

- [ ] **Step 3: §4 forbidden-language sweep + §106 secrets sweep**

Run (bash):
```bash
grep -rniE "mostly complete|approximately complete|backend finished|frontend pending|should work|probably works|mock for now|temporary workaround|untested but implemented|todo later|99%" core/data_model/survivorship.py core/data_model/ingest.py core/data_model/store.py core/data_model/__init__.py tests/unit/test_data_model_survivorship.py tests/unit/test_data_model_corporate_actions.py
grep -niE "api[_-]?key|secret|token|password|credential" core/data_model/survivorship.py
```
Expected: no matches (the §106 sweep on survivorship.py: 0; ingest.py docstrings already reference "credentials" as a non-secret — re-verify store.py/ingest.py unchanged on that point).

- [ ] **Step 4: All public symbols import**

Run:
```bash
python -c "from core.data_model import SurvivorshipRegistry, get_survivorship_registry, YFCorporateActionAdapter, HistoricalStore, get_historical_store, OHLCVBar, CorporateAction; print('ok')"
```
Expected: `ok`.

- [ ] **Step 5: Full regression**

Run:
```bash
python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf
```
Expected: 1629 (1B baseline) + new 1C tests (7 survivorship + 5 corporate-action-adapter + 2 actions_as_of + 2 adjusted-flag = 16) = **1645 passed / 0 failed / exit 0**. No existing test weakened/dropped/skipped (§115). If the count differs, investigate before flipping to PASS.

- [ ] **Step 6: Flip Phase 1C Status to PASS + Phase 1 row to PASS**

Edit `docs/V9_IMPLEMENTATION.md`: Phase 1C record `Status: PASS` with exact results; **Phase 1 row → PASS** (1C is the final Phase 1 sub-plan).

- [ ] **Step 7: Commit the gate**

```bash
git add docs/V9_IMPLEMENTATION.md
git commit -m "test(phase1c): exit gate PASS — Phase 1 complete (1645 passed)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage:**
- §31 (corporate actions: splits/dividends/ticker changes/mergers + adjusted/raw convention) → `YFCorporateActionAdapter` captures DIVIDEND/SPLIT into the store; `actions_as_of(T)`; `OHLCVBar.adjusted` flag set by the adapter (default `True`); adjusted/raw convention documented in `V9_ARCHITECTURE.md`. (TICKER_CHANGE/MERGER capture via the `SurvivorshipRegistry.register_ticker_change` path + the `CorporateAction` `from_ticker`/`to_ticker` fields, ready for future adapters.) ✓
- §30 (survivorship: document limitation, do not pretend solved) → `SurvivorshipRegistry` scaffold + `is_listed(T)`/`universe_at(T)` + loudly documented limitation in `V9_ARCHITECTURE.md` + backtester comment. No fake delisting data. ✓
- §29 (point-in-time) → `actions_as_of(T)` and `is_listed(T)`/`universe_at(T)` are point-in-time. ✓
- §32 (no synthetic historical facts) → unknown symbol → `is_listed` returns `False` (not silently listed); no reconstructed/fake actions. ✓
- §7/§23 (extend, wrap-not-replace) → yfinance adapter wraps; backtester comment-only; registry additive. ✓
- §106/§115/§117/§118/§4 → honored, verified at gate. ✓

**2. Placeholder scan:** No TBD/TODO. Deferrals documented with phase owners (backtester rewiring → Phase 7; delisting DB → Phase 2+; non-yfinance adapters → 5/7/10). Clean.

**3. Type consistency:**
- `CorporateAction` constructed with `symbol, ex_date, type, ratio, amount, from_ticker, to_ticker, pit` — matches the 1A entity. ✓
- `SurvivorshipRegistry` uses `to_utc` from pit, local `_from_iso` (same shape as store's). ✓
- `actions_as_of` reuses the store's `_from_iso`/`PointInTime`/`to_utc`/`_RowView`-free approach (builds PointInTime inline, like `as_of_range`). ✓
- `YFOHLCVAdapter.adjusted` passed to `OHLCVBar(adjusted=...)` — `adjusted` is a real 1A field. ✓
- Expected test counts: T1=7, T2=5, T3=2 (→7), T4=2 (→9) corporate-action tests; gate expects 1629+16=1645. ✓

**4. Ambiguity check:**
- `is_listed` for `TICKER_CHANGE` on the old symbol → False (old ticker gone); on the new symbol → True after the LISTED row. Tested. ✓
- `actions_as_of` returns a range (all actions <= T), not a single newest — the backtest needs the full action history up to T. ✓
- `adjusted` default flip False→True documented + test-suite-compatible (no 1B test asserts `adjusted`). ✓

No issues found. Plan is complete.