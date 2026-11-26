import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


BASE_DIR = Path(__file__).resolve().parent.parent
DB_FILENAME = "market_signals.db"


def resolve_db_path() -> Tuple[Path, str]:
    """Where the key-value database lives, and which setting decided it.

    ``MS_SQLITE_DB_PATH`` wins. Without it the database sits in the configured
    data folder (``MS_DATA_DIR``); only when neither is set is it
    ``<repo>/data``. The path used to ignore ``MS_DATA_DIR``: a second instance
    started with its own data folder opened the first instance's database and
    served (and could overwrite) its watchlist, config and capital state.

    core.config imports this module, so the data-folder rule is repeated here
    instead of imported; tests assert that both agree.
    """
    explicit = str(os.getenv("MS_SQLITE_DB_PATH") or "").strip()
    if explicit:
        return Path(explicit), "MS_SQLITE_DB_PATH"
    data_dir = str(os.getenv("MS_DATA_DIR") or "").strip()
    if data_dir:
        return Path(data_dir) / DB_FILENAME, "MS_DATA_DIR"
    return BASE_DIR / "data" / DB_FILENAME, "default"


DB_PATH, DB_PATH_SOURCE = resolve_db_path()

logger = logging.getLogger("SQLiteStore")


def describe_db_path() -> str:
    """One line naming the database this process uses and why."""
    try:
        resolved = Path(DB_PATH).resolve()
    except OSError:
        resolved = Path(DB_PATH)
    return f"{resolved} (from {DB_PATH_SOURCE})"


class SQLiteStoreWriteError(RuntimeError):
    """A value could not be persisted (serialization or database failure)."""


class SQLiteKVStore:
    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Writers and readers use separate connections and separate locks.
        # The store holds multi-MB JSON blobs (capital state ~12 MB, state ~3 MB);
        # with one connection behind one lock every reader in the process
        # waited for each of those writes (synchronous=FULL) to finish. WAL
        # lets a second connection read the last committed snapshot while a
        # write is in flight, so readers only queue behind other readers.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        journal_mode = ""
        try:
            row = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
            journal_mode = str(row[0] if row else "").lower()
        except sqlite3.Error:
            journal_mode = ""
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        self._conn.commit()

        self._read_lock = self._lock
        self._read_conn = self._conn
        if journal_mode == "wal":
            try:
                reader = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
                reader.execute("PRAGMA busy_timeout=5000")
                reader.execute("PRAGMA query_only=ON")
                reader.execute("SELECT 1 FROM kv_store LIMIT 1").fetchone()
                self._read_conn = reader
                self._read_lock = threading.RLock()
            except sqlite3.Error as exc:
                logger.warning("SQLite read connection unavailable; sharing the writer: %s", exc)

        self._health_lock = threading.Lock()
        self._write_failures = 0
        self._last_write_error: Optional[str] = None
        self._last_write_error_key: Optional[str] = None
        self._last_write_error_at: Optional[str] = None
        self._last_write_error_monotonic: Optional[float] = None
        self._last_write_ok_at: Optional[str] = None

    @classmethod
    def instance(cls) -> "SQLiteKVStore":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = SQLiteKVStore()
                # Once per process: which database file this instance really
                # opened, so a shared or unexpected database is never silent.
                logger.info(
                    "SQLite store opened: %s (path from %s)",
                    cls._instance.path, DB_PATH_SOURCE,
                )
        return cls._instance

    # ── health ────────────────────────────────────────────────────────────

    def _record_write_failure(self, key: str, exc: BaseException) -> None:
        with self._health_lock:
            self._write_failures += 1
            self._last_write_error = f"{type(exc).__name__}: {exc}"[:300]
            self._last_write_error_key = str(key)[:120]
            self._last_write_error_at = datetime.now(timezone.utc).isoformat()
            self._last_write_error_monotonic = time.monotonic()

    def _record_write_ok(self) -> None:
        with self._health_lock:
            self._last_write_ok_at = datetime.now(timezone.utc).isoformat()

    def write_health(self) -> Dict[str, Any]:
        """Write-path health for diagnostics; a lost write is never silent."""
        with self._health_lock:
            age = (
                None
                if self._last_write_error_monotonic is None
                else max(0.0, time.monotonic() - self._last_write_error_monotonic)
            )
            return {
                "write_failures": int(self._write_failures),
                "last_write_error": self._last_write_error,
                "last_write_error_key": self._last_write_error_key,
                "last_write_error_at": self._last_write_error_at,
                "last_write_error_age_seconds": None if age is None else round(age, 1),
                "last_write_ok_at": self._last_write_ok_at,
                "separate_read_connection": self._read_conn is not self._conn,
            }

    # ── read ──────────────────────────────────────────────────────────────

    def get_text(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._read_lock:
            cur = self._read_conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,))
            row = cur.fetchone()
            if not row:
                return default
            return row[0]

    def get_json(self, key: str, default: Any = None) -> Any:
        raw = self.get_text(key, None)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except Exception as e:
            logger.error(f"Invalid JSON in SQLite key '{key}': {e}")
            return default

    # ── write ─────────────────────────────────────────────────────────────

    def set_text(self, key: str, value: str) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO kv_store (key, value, updated_at)
                    VALUES (?, ?, datetime('now'))
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = datetime('now')
                    """,
                    (key, value),
                )
                self._conn.commit()
            except Exception as exc:
                # Leave no half-open transaction behind for the next writer.
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                self._record_write_failure(key, exc)
                logger.error(f"SQLite write failed for key '{key}': {exc}")
                raise
        self._record_write_ok()

    def set_json(self, key: str, value: Any) -> None:
        """Persist ``value`` as JSON. Raises when the value cannot be stored.

        A value that cannot be serialized used to be logged and dropped while
        the caller carried on as if it had been saved; the next read then
        returned the previous (stale) row. The failure is now recorded for
        diagnostics and raised so the caller decides what to do.
        """
        try:
            payload = json.dumps(value, ensure_ascii=False)
        except Exception as e:
            self._record_write_failure(key, e)
            logger.error(f"Failed to serialize JSON for SQLite key '{key}': {e}")
            raise SQLiteStoreWriteError(
                f"Value for SQLite key '{key}' is not JSON-serializable: {e}"
            ) from e
        self.set_text(key, payload)


def get_sqlite_store() -> SQLiteKVStore:
    return SQLiteKVStore.instance()
