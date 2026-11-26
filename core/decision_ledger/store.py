"""SQLite Decision Ledger store — the Phase 11 ExecutionStore pattern.

Append-only entries (idempotent on decision_id), append-only outcome
evaluations (latest per decision wins on read), and §88 Model Lab runs.
Cross-process-safe: WAL, busy_timeout, per-operation connections.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from .types import LedgerEntry, LedgerOutcome, validate_outcome, validate_source

SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _decode(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


class DecisionLedgerStore:
    """Persistent §97 decision ledger + §89 outcomes + §88 model-lab runs."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migration_lock = threading.RLock()
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def migrate(self) -> int:
        with self._migration_lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS decision_ledger_entries (
                    decision_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    action TEXT NOT NULL,
                    horizon TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT '',
                    strategy TEXT NOT NULL DEFAULT '',
                    replay_of TEXT,
                    pit_as_of TEXT,
                    entry_json TEXT NOT NULL,
                    outcome_json TEXT NOT NULL DEFAULT 'null'
                );
                CREATE INDEX IF NOT EXISTS idx_ledger_entries_created
                    ON decision_ledger_entries(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_ledger_entries_ticker
                    ON decision_ledger_entries(ticker, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_ledger_replay_of
                    ON decision_ledger_entries(replay_of);
                CREATE TABLE IF NOT EXISTS decision_ledger_outcomes (
                    decision_id TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    basis TEXT NOT NULL,
                    outcome_payload TEXT NOT NULL,
                    PRIMARY KEY(decision_id, evaluated_at),
                    FOREIGN KEY(decision_id) REFERENCES decision_ledger_entries(decision_id)
                );
                CREATE TABLE IF NOT EXISTS model_lab_runs (
                    run_id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    inputs_sha256 TEXT NOT NULL,
                    prompt_version TEXT NOT NULL DEFAULT '',
                    run_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_model_lab_runs_role
                    ON model_lab_runs(role, created_at DESC);
                CREATE TABLE IF NOT EXISTS model_lab_results (
                    run_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, provider, model),
                    FOREIGN KEY(run_id) REFERENCES model_lab_runs(run_id)
                );
                """
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
        return SCHEMA_VERSION

    # ------------------------------------------------------------------ entries

    def append_entry(self, entry: LedgerEntry) -> tuple[LedgerEntry, bool]:
        """Idempotent append: a redelivered decision_id returns (original, False)."""
        validate_source(entry.source)
        payload = entry.to_dict()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT entry_json FROM decision_ledger_entries WHERE decision_id=?",
                (entry.decision_id,),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return LedgerEntry.from_dict(_decode(existing["entry_json"], {})), False
            connection.execute(
                """INSERT INTO decision_ledger_entries
                   (decision_id, created_at, ticker, action, horizon, source, status,
                    strategy, replay_of, pit_as_of, entry_json, outcome_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'null')""",
                (
                    entry.decision_id, entry.created_at, entry.ticker, entry.action,
                    entry.horizon, entry.source, entry.status, entry.strategy,
                    entry.replay_of, entry.pit_as_of, _json(payload),
                ),
            )
            connection.commit()
        return entry, True

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> LedgerEntry:
        entry = LedgerEntry.from_dict(_decode(row["entry_json"], {}))
        outcome = _decode(row["outcome_json"], None)
        if outcome is not None and entry.afterward is None:
            entry = LedgerEntry(**{**entry.to_dict(), "afterward": outcome})
        return entry

    def get_entry(self, decision_id: str) -> Optional[LedgerEntry]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entry_json, outcome_json FROM decision_ledger_entries WHERE decision_id=?",
                (str(decision_id or "").strip(),),
            ).fetchone()
            return self._row_to_entry(row) if row is not None else None

    def list_entries(
        self,
        *,
        limit: int = 50,
        ticker: Optional[str] = None,
        source: Optional[str] = None,
        replay_of: Optional[str] = None,
    ) -> List[LedgerEntry]:
        bounded = max(1, min(500, int(limit or 50)))
        clauses, params = ["1=1"], []
        if ticker:
            clauses.append("ticker=?")
            params.append(str(ticker).upper())
        if source:
            clauses.append("source=?")
            params.append(str(source))
        if replay_of:
            clauses.append("replay_of=?")
            params.append(str(replay_of))
        params.append(bounded)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT entry_json, outcome_json FROM decision_ledger_entries
                    WHERE {' AND '.join(clauses)}
                    ORDER BY created_at DESC LIMIT ?""",
                tuple(params),
            ).fetchall()
            return [self._row_to_entry(row) for row in rows]

    def count_entries(self, *, source: Optional[str] = None) -> int:
        with self._connect() as connection:
            if source:
                row = connection.execute(
                    "SELECT COUNT(*) AS n FROM decision_ledger_entries WHERE source=?",
                    (str(source),),
                ).fetchone()
            else:
                row = connection.execute("SELECT COUNT(*) AS n FROM decision_ledger_entries").fetchone()
            return int(row["n"] or 0)

    def count_entries_by_status(self) -> dict[str, int]:
        """Return persistent all-history decision counts from indexed columns."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS n FROM decision_ledger_entries GROUP BY status"
            ).fetchall()
        return {str(row["status"] or "unknown"): int(row["n"] or 0) for row in rows}

    # ----------------------------------------------------------------- outcomes

    def append_outcome(self, outcome: LedgerOutcome) -> tuple[LedgerOutcome, bool]:
        validate_outcome(outcome.outcome)
        payload = outcome.to_dict()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            entry = connection.execute(
                "SELECT 1 FROM decision_ledger_entries WHERE decision_id=?",
                (outcome.decision_id,),
            ).fetchone()
            if entry is None:
                connection.commit()
                raise KeyError(f"unknown decision_id: {outcome.decision_id}")
            existing = connection.execute(
                """SELECT outcome_payload FROM decision_ledger_outcomes
                   WHERE decision_id=? AND evaluated_at=?""",
                (outcome.decision_id, outcome.evaluated_at),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return LedgerOutcome(**_decode(existing["outcome_payload"], payload)), False
            connection.execute(
                """INSERT INTO decision_ledger_outcomes
                   (decision_id, evaluated_at, outcome, basis, outcome_payload)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    outcome.decision_id, outcome.evaluated_at, outcome.outcome,
                    outcome.basis, _json(payload),
                ),
            )
            # Latest outcome summary mirrored onto the entry row for cheap reads.
            latest = connection.execute(
                """SELECT outcome_payload FROM decision_ledger_outcomes
                   WHERE decision_id=? ORDER BY evaluated_at DESC LIMIT 1""",
                (outcome.decision_id,),
            ).fetchone()
            connection.execute(
                "UPDATE decision_ledger_entries SET outcome_json=? WHERE decision_id=?",
                (latest["outcome_payload"] if latest else "null", outcome.decision_id),
            )
            connection.commit()
        return outcome, True

    def get_outcome(self, decision_id: str) -> Optional[LedgerOutcome]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT outcome_payload FROM decision_ledger_outcomes
                   WHERE decision_id=? ORDER BY evaluated_at DESC LIMIT 1""",
                (str(decision_id or "").strip(),),
            ).fetchone()
            return LedgerOutcome(**_decode(row["outcome_payload"], {})) if row is not None else None

    def list_position_close_trades(self, *, limit: int = 5000) -> List[dict[str, Any]]:
        """Closed paper positions with realized net P&L, latest evaluation per decision."""
        bounded = max(1, min(20000, int(limit or 5000)))
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT o.decision_id, o.outcome_payload, e.source, e.strategy
                   FROM decision_ledger_outcomes o
                   LEFT JOIN decision_ledger_entries e ON e.decision_id = o.decision_id
                   WHERE o.basis = 'position_close'
                   ORDER BY o.evaluated_at DESC LIMIT ?""",
                (bounded,),
            ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            decision_id = row["decision_id"]
            if decision_id in latest:
                continue
            payload = _decode(row["outcome_payload"], {})
            payload = payload if isinstance(payload, dict) else {}
            detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
            latest[decision_id] = {
                "decision_id": decision_id,
                "source": row["source"],
                "strategy_type": detail.get("strategy_type") or row["strategy"],
                "ticker": detail.get("ticker"),
                "direction": detail.get("direction"),
                "pnl_usd": detail.get("pnl_usd"),
                "closed_at": detail.get("closed_at"),
                "close_reason": detail.get("close_reason"),
                "forward_return_pct": payload.get("forward_return_pct"),
                # A close delayed by an outage is a real paper result but not
                # strategy evidence; the reader needs the marker to leave it out.
                "evidence_excluded": bool(detail.get("evidence_excluded")),
                "evidence_exclusion_reason": detail.get("evidence_exclusion_reason"),
            }
        return list(latest.values())

    def list_outcomes(self, decision_id: str) -> List[LedgerOutcome]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT outcome_payload FROM decision_ledger_outcomes
                   WHERE decision_id=? ORDER BY evaluated_at""",
                (str(decision_id or "").strip(),),
            ).fetchall()
            return [LedgerOutcome(**_decode(row["outcome_payload"], {})) for row in rows]

    # ---------------------------------------------------------------- model lab

    def append_model_lab_run(self, run: dict[str, Any], results: List[dict[str, Any]]) -> bool:
        run_id = str(run.get("run_id") or "")
        if not run_id:
            raise ValueError("model lab run requires run_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM model_lab_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing is not None:
                connection.commit()
                return False
            connection.execute(
                """INSERT INTO model_lab_runs
                   (run_id, role, created_at, inputs_sha256, prompt_version, run_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    run_id, str(run.get("role") or ""), str(run.get("created_at") or _utc_now_iso()),
                    str(run.get("inputs_sha256") or ""), str(run.get("prompt_version") or ""),
                    _json(run),
                ),
            )
            now = _utc_now_iso()
            for result in results:
                connection.execute(
                    """INSERT OR REPLACE INTO model_lab_results
                       (run_id, provider, model, result_json, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        run_id, str(result.get("provider") or ""), str(result.get("model") or ""),
                        _json(result), now,
                    ),
                )
            connection.commit()
        return True

    def list_model_lab_runs(self, *, limit: int = 20, role: Optional[str] = None) -> List[dict[str, Any]]:
        bounded = max(1, min(100, int(limit or 20)))
        with self._connect() as connection:
            if role:
                rows = connection.execute(
                    """SELECT run_id FROM model_lab_runs WHERE role=?
                       ORDER BY created_at DESC LIMIT ?""",
                    (str(role), bounded),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT run_id FROM model_lab_runs ORDER BY created_at DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
            runs = []
            for row in rows:
                run_row = connection.execute(
                    "SELECT run_json FROM model_lab_runs WHERE run_id=?", (row["run_id"],)
                ).fetchone()
                if run_row is None:
                    continue
                run = _decode(run_row["run_json"], {})
                result_rows = connection.execute(
                    """SELECT result_json FROM model_lab_results
                       WHERE run_id=? ORDER BY provider, model""",
                    (row["run_id"],),
                ).fetchall()
                run["results"] = [_decode(r["result_json"], {}) for r in result_rows]
                runs.append(run)
            return runs


_DEFAULT_LEDGER_FILENAME = "decision_ledger.db"
_LEDGER_SINGLETON: Optional[DecisionLedgerStore] = None
_LEDGER_LOCK = threading.Lock()


def decision_ledger_store_path(config: Optional[dict[str, Any]] = None) -> Path:
    """Env `MS_DECISION_LEDGER_PATH` > config `decision_ledger.store_path` > default."""
    import os

    env_path = os.environ.get("MS_DECISION_LEDGER_PATH", "").strip()
    if env_path:
        return Path(env_path)
    cfg_path = ((config or {}).get("decision_ledger") or {}).get("store_path") if config else None
    # Never relative to the working directory: "data/..." means the configured
    # data folder (MS_DATA_DIR), so a second instance keeps its own ledger.
    from core.config import data_path, resolve_state_path

    if cfg_path:
        return resolve_state_path(cfg_path)
    return data_path(_DEFAULT_LEDGER_FILENAME)


def get_decision_ledger_store(config: Optional[dict[str, Any]] = None) -> DecisionLedgerStore:
    """Process-wide singleton (tests redirect via MS_DECISION_LEDGER_PATH)."""
    global _LEDGER_SINGLETON
    with _LEDGER_LOCK:
        if _LEDGER_SINGLETON is None:
            _LEDGER_SINGLETON = DecisionLedgerStore(decision_ledger_store_path(config))
        return _LEDGER_SINGLETON


def reset_decision_ledger_store_singleton() -> None:
    """Test seam — drop the cached singleton (does not delete data)."""
    global _LEDGER_SINGLETON
    with _LEDGER_LOCK:
        _LEDGER_SINGLETON = None


__all__ = [
    "DecisionLedgerStore", "SCHEMA_VERSION", "decision_ledger_store_path",
    "get_decision_ledger_store", "reset_decision_ledger_store_singleton",
]
