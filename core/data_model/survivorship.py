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
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Set

from core.data_model.pit import to_utc

logger = logging.getLogger("SurvivorshipRegistry")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
# Same database as the key-value store: one rule (MS_SQLITE_DB_PATH, else the
# MS_DATA_DIR folder) lives in core.sqlite_store.
from core.sqlite_store import DB_PATH as _SQLITE_DB_PATH  # noqa: E402

DB_PATH = Path(_SQLITE_DB_PATH)


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
        """Point-in-time universe (§30): base_symbols minus those with a TERMINAL
        lifecycle event (DELISTED / TICKER_CHANGE) at or before T.

        `base_symbols` is the CURRENT universe (e.g. today's watchlist) — its
        members are asserted-listed NOW. A base symbol with no lifecycle record
        is therefore KEPT (no positive delisting evidence -> still listed at T),
        so with no delistings registered this returns base_symbols unchanged.
        This intentionally differs from is_listed(), which answers the §32
        conservative query for an ARBITRARY symbol (unknown -> False); universe_at
        answers the survivorship-filter question on an asserted-current set."""
        T = to_utc(T)
        base = set(base_symbols)
        if not base:
            return set()
        placeholders = ",".join("?" for _ in base)
        with self._lock:
            cur = self._conn.execute(
                f"SELECT symbol, event, event_time FROM symbol_lifecycle "
                f"WHERE event IN ('DELISTED', 'TICKER_CHANGE') AND symbol IN ({placeholders})",
                tuple(base),
            )
            rows = cur.fetchall()
        excluded: Set[str] = set()
        for sym, _ev, et in rows:
            dt = _from_iso(et)
            if dt is None:
                continue
            if to_utc(dt) <= T:
                excluded.add(sym)
        return base - excluded


def get_survivorship_registry() -> SurvivorshipRegistry:
    return SurvivorshipRegistry.instance()