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
import sqlite3
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.data_model.entities import Symbol
from core.data_model.pit import PointInTime, UNAVAILABLE, as_of, to_utc
from core.data_model.quality import Conflict

logger = logging.getLogger("HistoricalStore")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
# Same database as the key-value store: one rule (MS_SQLITE_DB_PATH, else the
# MS_DATA_DIR folder) lives in core.sqlite_store.
from core.sqlite_store import DB_PATH as _SQLITE_DB_PATH  # noqa: E402

DB_PATH = Path(_SQLITE_DB_PATH)


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

    def actions_as_of(
        self,
        symbol: str,
        T: Any,
        *,
        prefer: str = "available_from",
    ) -> List[Dict[str, Any]]:
        """Corporate actions for `symbol` knowable at T (§29), ascending by
        `prefer`. Returns the deserialized CorporateAction payloads with
        pit.<prefer> <= T (a range — a backtest needs all actions up to T,
        not just the newest)."""
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


def get_historical_store() -> HistoricalStore:
    return HistoricalStore.instance()