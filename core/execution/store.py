"""SQLite order ledger with an idempotent Phase 11 schema migration."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

from .types import OrderIntent, OrderState, utc_now_iso, validate_transition


SCHEMA_VERSION = 1
ORPHANED_FILL_ROLLED_BACK = "ORPHANED_FILL_ROLLED_BACK"
CAPITAL_RESET_POSITION_CLEARED = "CAPITAL_RESET_POSITION_CLEARED"
_SUPPORTED_RECONCILIATION_RESOLUTIONS = {
    ORPHANED_FILL_ROLLED_BACK,
    CAPITAL_RESET_POSITION_CLEARED,
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _decode(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


class ExecutionStore:
    """Cross-process-safe order/idempotency store backed by SQLite."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migration_lock = threading.RLock()
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def migrate(self) -> int:
        with self._migration_lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_orders (
                    order_intent_id TEXT PRIMARY KEY,
                    decision_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    ticker TEXT NOT NULL,
                    action TEXT NOT NULL,
                    state TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_order_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_intent_id TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(order_intent_id) REFERENCES execution_orders(order_intent_id)
                );
                CREATE INDEX IF NOT EXISTS idx_execution_events_order
                    ON execution_order_events(order_intent_id, event_id);
                CREATE TABLE IF NOT EXISTS execution_reconciliation (
                    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
                    status TEXT NOT NULL,
                    conflicts_json TEXT NOT NULL DEFAULT '[]',
                    checked_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """INSERT OR IGNORE INTO execution_reconciliation
                   (singleton_id, status, conflicts_json, checked_at)
                   VALUES (1, 'UNVERIFIED', '[]', ?)""",
                (utc_now_iso(),),
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
        return SCHEMA_VERSION

    @staticmethod
    def _row_to_order(row: sqlite3.Row, events: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        return {
            "order_intent_id": row["order_intent_id"],
            "decision_id": row["decision_id"],
            "idempotency_key": row["idempotency_key"],
            "ticker": row["ticker"],
            "action": row["action"],
            "state": row["state"],
            "intent": _decode(row["intent_json"], {}),
            "result": _decode(row["result_json"], {}),
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "events": events or [],
        }

    def _events(self, connection: sqlite3.Connection, order_intent_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT from_state, to_state, detail_json, created_at
               FROM execution_order_events WHERE order_intent_id=? ORDER BY event_id""",
            (order_intent_id,),
        ).fetchall()
        return [
            {
                "from_state": row["from_state"],
                "to_state": row["to_state"],
                "detail": _decode(row["detail_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_order(self, order_intent_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM execution_orders WHERE order_intent_id=?",
                (str(order_intent_id or "").strip(),),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_order(row, self._events(connection, row["order_intent_id"]))

    def get_by_idempotency_key(self, key: str) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM execution_orders WHERE idempotency_key=?",
                (str(key or "").strip(),),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_order(row, self._events(connection, row["order_intent_id"]))

    def create_order(self, intent: OrderIntent) -> tuple[dict[str, Any], bool]:
        payload = intent.to_dict()
        now = intent.created_at or utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM execution_orders WHERE idempotency_key=?",
                (intent.idempotency_key,),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._row_to_order(
                    existing, self._events(connection, existing["order_intent_id"])
                ), False
            connection.execute(
                """INSERT INTO execution_orders
                   (order_intent_id, decision_id, idempotency_key, ticker, action, state,
                    intent_json, result_json, error, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, '{}', '', ?, ?)""",
                (
                    intent.order_intent_id,
                    intent.decision_id,
                    intent.idempotency_key,
                    intent.ticker,
                    intent.action.value,
                    OrderState.PROPOSED.value,
                    _json(payload),
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO execution_order_events
                   (order_intent_id, from_state, to_state, detail_json, created_at)
                   VALUES (?, NULL, ?, '{}', ?)""",
                (intent.order_intent_id, OrderState.PROPOSED.value, now),
            )
            connection.commit()
        return self.get_order(intent.order_intent_id) or {}, True

    def transition(
        self,
        order_intent_id: str,
        target: OrderState | str,
        *,
        detail: Optional[dict[str, Any]] = None,
        result: Optional[dict[str, Any]] = None,
        error: str = "",
    ) -> dict[str, Any]:
        destination = target if isinstance(target, OrderState) else OrderState(str(target))
        identifier = str(order_intent_id or "").strip()
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM execution_orders WHERE order_intent_id=?", (identifier,)
            ).fetchone()
            if row is None:
                raise KeyError("order intent not found")
            source = OrderState(row["state"])
            validate_transition(source, destination)
            next_result = _decode(row["result_json"], {})
            if result is not None:
                next_result = dict(result)
            connection.execute(
                """UPDATE execution_orders
                   SET state=?, result_json=?, error=?, updated_at=?
                   WHERE order_intent_id=?""",
                (destination.value, _json(next_result), str(error or ""), now, identifier),
            )
            connection.execute(
                """INSERT INTO execution_order_events
                   (order_intent_id, from_state, to_state, detail_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (identifier, source.value, destination.value, _json(detail or {}), now),
            )
            connection.commit()
        return self.get_order(identifier) or {}

    def list_orders(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(1000, int(limit or 100)))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM execution_orders ORDER BY created_at DESC LIMIT ?", (bounded,)
            ).fetchall()
            return [self._row_to_order(row) for row in rows]

    def set_reconciliation(self, status: str, conflicts: Iterable[dict[str, Any]]) -> dict[str, Any]:
        normalized = str(status or "").upper().strip()
        if normalized not in {"HEALTHY", "EXECUTION_STATE_CONFLICT", "UNVERIFIED"}:
            raise ValueError("invalid reconciliation status")
        conflict_list = [dict(item) for item in conflicts]
        checked_at = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                """UPDATE execution_reconciliation
                   SET status=?, conflicts_json=?, checked_at=? WHERE singleton_id=1""",
                (normalized, _json(conflict_list), checked_at),
            )
            connection.commit()
        return {"status": normalized, "conflicts": conflict_list, "checked_at": checked_at}

    def get_reconciliation(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, conflicts_json, checked_at FROM execution_reconciliation WHERE singleton_id=1"
            ).fetchone()
            if row is None:
                return {"status": "UNVERIFIED", "conflicts": [], "checked_at": None}
            return {
                "status": row["status"],
                "conflicts": _decode(row["conflicts_json"], []),
                "checked_at": row["checked_at"],
            }

    def record_reconciliation_resolution(
        self,
        order_intent_id: str,
        *,
        resolution: str,
        reason: str,
    ) -> dict[str, Any]:
        """Append an auditable resolution without rewriting terminal history."""
        identifier = str(order_intent_id or "").strip()
        normalized = str(resolution or "").upper().strip()
        if normalized not in _SUPPORTED_RECONCILIATION_RESOLUTIONS:
            raise ValueError("unsupported reconciliation resolution")
        if not str(reason or "").strip():
            raise ValueError("reconciliation resolution reason is required")
        now = utc_now_iso()
        detail = {
            "reconciliation_resolution": normalized,
            "reason": str(reason).strip(),
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, action FROM execution_orders WHERE order_intent_id=?",
                (identifier,),
            ).fetchone()
            if row is None:
                raise KeyError("order intent not found")
            if row["state"] != OrderState.FILLED.value or not str(row["action"]).startswith("OPEN_"):
                raise ValueError("only a filled open order can receive an orphan rollback resolution")
            connection.execute(
                """INSERT INTO execution_order_events
                   (order_intent_id, from_state, to_state, detail_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    identifier,
                    OrderState.FILLED.value,
                    OrderState.FILLED.value,
                    _json(detail),
                    now,
                ),
            )
            connection.commit()
        return {"order_intent_id": identifier, **detail, "created_at": now}

    def reconciliation_resolutions(self) -> dict[str, dict[str, Any]]:
        """Return the latest explicit terminal-resolution event per order."""
        resolved: dict[str, dict[str, Any]] = {}
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT order_intent_id, detail_json, created_at
                   FROM execution_order_events
                   WHERE from_state=? AND to_state=?
                   ORDER BY event_id""",
                (OrderState.FILLED.value, OrderState.FILLED.value),
            ).fetchall()
        for row in rows:
            detail = _decode(row["detail_json"], {})
            if str(detail.get("reconciliation_resolution") or "").upper() in _SUPPORTED_RECONCILIATION_RESOLUTIONS:
                resolved[str(row["order_intent_id"])] = {
                    **detail,
                    "created_at": row["created_at"],
                }
        return resolved


__all__ = [
    "ExecutionStore",
    "ORPHANED_FILL_ROLLED_BACK",
    "CAPITAL_RESET_POSITION_CLEARED",
    "SCHEMA_VERSION",
]
