"""
core/state_store.py — in-memory single source of truth (Stage 2 - State Safety).

Problem solved: previously state.json, signals_state.json, hotlist_cache.json
were read/written concurrently by multiple threads (Engine scan, News agent,
API layer), causing potential corruption and high I/O on request paths.

Solution:
- StateStore holds namespaced data entirely in RAM.
- Fine-grained RLock per namespace: reads and updates never block each other
  across namespaces.
- Persistence is ASYNC / batched: a background writer thread flushes only dirty
  namespaces to disk every N seconds. Writes never happen inside a request.
- get_snapshot() is zero-copy (returns a deep-copied dict).
- Existing save_state_payload / save_signals_payload functions are preserved
  and now delegate to this store.

Usage:
    from core.state_store import get_state_store
    store = get_state_store()

    # Read
    snap = store.get_snapshot("state")       # dict or None

    # Partial update (merges new keys into existing dict)
    store.update("state", {"tickers": {...}, "stats": {...}})

    # Full replace
    store.replace("state", full_dict)

    # Force immediate flush to disk
    store.flush("state")
"""

import copy
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set

logger = logging.getLogger("StateStore")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# How often the background writer checks for dirty namespaces (seconds).
_FLUSH_INTERVAL_SEC: float = float(os.getenv("MS_STATE_FLUSH_SEC", "3"))
_INSTANCE_LOCK = threading.Lock()
_INSTANCE: Optional["StateStore"] = None


# ---------------------------------------------------------------------------
# Namespace definitions  (namespace -> disk path, optional SQLite key)
# ---------------------------------------------------------------------------
def _build_namespaces() -> Dict[str, Dict[str, Any]]:
    """Lazy import of config paths to avoid circular imports at module level."""
    try:
        from core.config import STATE_PATH, SIGNALS_PATH  # type: ignore
    except Exception:
        # Config unavailable: still the runtime folder (MS_RUNTIME_DIR), never
        # files relative to whatever the working directory happens to be.
        runtime_dir = Path(
            os.getenv("MS_RUNTIME_DIR")
            or (Path(__file__).resolve().parent.parent / "runtime")
        )
        STATE_PATH = Path(os.getenv("MS_STATE_PATH") or (runtime_dir / "state.json"))
        SIGNALS_PATH = Path(os.getenv("MS_SIGNALS_PATH") or (runtime_dir / "signals_state.json"))

    return {
        "state": {
            "path": STATE_PATH,
            "sqlite_key": "state",
            "indent": 2,
            "ensure_ascii": False,
        },
        "signals": {
            "path": SIGNALS_PATH,
            "sqlite_key": "signals_state",
            "indent": 2,
            "ensure_ascii": False,
        },
        # news, hotlist, calendar are managed by agents but we track them in
        # memory so the API can read them without touching disk.
        "news": {
            "path": None,    # agents manage their own persistence
            "sqlite_key": None,
            "indent": None,
            "ensure_ascii": False,
        },
        "hotlist": {
            "path": None,
            "sqlite_key": None,
            "indent": None,
            "ensure_ascii": False,
        },
        "premarket": {
            "path": None,
            "sqlite_key": None,
            "indent": None,
            "ensure_ascii": False,
        },
        "preearnings": {
            "path": None,
            "sqlite_key": None,
            "indent": None,
            "ensure_ascii": False,
        },
        "traderadar": {
            "path": None,
            "sqlite_key": None,
            "indent": None,
            "ensure_ascii": False,
        },
        "scanner": {
            "path": None,
            "sqlite_key": None,
            "indent": None,
            "ensure_ascii": False,
        },
    }


# ---------------------------------------------------------------------------
# Atomic JSON write helper (kept local to avoid circular import with engine.py)
# ---------------------------------------------------------------------------
def _discard_temp_file(temp_path: Optional[Path]) -> bool:
    """Remove an atomic-write temp file; True when it is gone.

    A just-written file can stay locked for a moment on Windows (virus
    scanner, indexer). One failed ``unlink`` used to be swallowed and the
    multi-MB ``<name>.json.XXXXXXXX.tmp`` stayed in the runtime folder for
    good, so the removal is retried briefly.
    """
    if temp_path is None:
        return True
    for attempt in range(5):
        try:
            temp_path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(0.05 * (attempt + 1))
    return not temp_path.exists()


def _open_temp_for_write(fd: int):
    try:
        return os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)  # an open descriptor would keep the temp file undeletable
        raise


def _atomic_write(path: Path, data: Any, indent: int = 2, ensure_ascii: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        temp_path = Path(tmp_name)
        with _open_temp_for_write(fd) as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii, default=str)
        for attempt in range(5):
            try:
                os.replace(str(temp_path), str(path))
                temp_path = None
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except Exception as exc:
        logger.error(f"StateStore atomic write failed for {path}: {exc}")
        raise
    finally:
        # Reached with a temp file only when the write or the replace failed.
        if temp_path is not None:
            _discard_temp_file(temp_path)


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace ``path`` with already-serialized JSON text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        temp_path = Path(tmp_name)
        with _open_temp_for_write(fd) as f:
            f.write(text)
        for attempt in range(5):
            try:
                os.replace(str(temp_path), str(path))
                temp_path = None
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except Exception as exc:
        logger.error(f"StateStore atomic write failed for {path}: {exc}")
        raise
    finally:
        # Reached with a temp file only when the write or the replace failed.
        if temp_path is not None:
            _discard_temp_file(temp_path)


# Pretty-printing uses the pure-Python JSON encoder. For the multi-MB market
# state flushed every few seconds that cost a large share of one core; only
# small payloads keep the human-readable indentation.
_PRETTY_MIRROR_MAX_BYTES = 256 * 1024


# ---------------------------------------------------------------------------
# StateStore
# ---------------------------------------------------------------------------
class StateStore:
    """
    Thread-safe in-memory state store with per-namespace locking and async
    persistence.

    All public methods are safe to call from any thread.
    """

    def __init__(self) -> None:
        self._namespaces: Dict[str, Dict[str, Any]] = _build_namespaces()
        # Per-namespace lock + data
        self._data: Dict[str, Any] = {}
        self._locks: Dict[str, threading.RLock] = {}
        self._dirty: Set[str] = set()
        self._dirty_lock = threading.Lock()
        self._last_flush: Dict[str, float] = {}
        self._version: Dict[str, int] = {}
        self._flush_failures: Dict[str, int] = {}
        self._last_flush_error: Dict[str, str] = {}

        for ns in self._namespaces:
            self._locks[ns] = threading.RLock()
            self._data[ns] = None  # Populated lazily
            self._version[ns] = 0
            self._last_flush[ns] = 0.0

        # Background writer
        self._stop_event = threading.Event()
        self._writer_thread = threading.Thread(
            target=self._background_writer,
            daemon=True,
            name="StateStore-Writer",
        )
        self._writer_thread.start()
        logger.info(
            f"StateStore started (flush_interval={_FLUSH_INTERVAL_SEC}s, "
            f"namespaces={list(self._namespaces)})"
        )

    # ── read ──────────────────────────────────────────────────────────────

    def get_snapshot(self, namespace: str) -> Optional[Dict]:
        """Return a deep copy of the current in-memory state for a namespace."""
        lock = self._locks.get(namespace)
        if lock is None:
            logger.warning(f"StateStore: unknown namespace '{namespace}'")
            return None
        with lock:
            data = self._data.get(namespace)
            if data is None:
                return None
            return copy.deepcopy(data) if isinstance(data, (dict, list)) else data

    def get_raw(self, namespace: str) -> Any:
        """Return the live reference (no copy). Caller must NOT mutate it."""
        lock = self._locks.get(namespace)
        if lock is None:
            return None
        with lock:
            return self._data.get(namespace)

    def get_version(self, namespace: str) -> int:
        """Integer counter incremented on every write — useful for ETag checks."""
        with self._locks.get(namespace, threading.RLock()):
            return self._version.get(namespace, 0)

    # ── write ─────────────────────────────────────────────────────────────

    def update(self, namespace: str, partial: Dict) -> None:
        """Merge `partial` into the existing namespace dict (shallow merge of top-level keys)."""
        lock = self._locks.get(namespace)
        if lock is None:
            logger.warning(f"StateStore.update: unknown namespace '{namespace}'")
            return
        with lock:
            existing = self._data.get(namespace)
            if not isinstance(existing, dict):
                existing = {}
            existing.update(partial)
            self._data[namespace] = existing
            self._version[namespace] = self._version.get(namespace, 0) + 1
            self._mark_dirty(namespace)

    def replace(self, namespace: str, data: Any) -> None:
        """Replace the entire namespace payload."""
        lock = self._locks.get(namespace)
        if lock is None:
            logger.warning(f"StateStore.replace: unknown namespace '{namespace}'")
            return
        with lock:
            self._data[namespace] = data
            self._version[namespace] = self._version.get(namespace, 0) + 1
            self._mark_dirty(namespace)

    def set_if_empty(self, namespace: str, data: Any) -> bool:
        """Set data only if namespace is currently None. Returns True if set."""
        lock = self._locks.get(namespace)
        if lock is None:
            return False
        with lock:
            if self._data.get(namespace) is None:
                self._data[namespace] = data
                self._version[namespace] = self._version.get(namespace, 0) + 1
                self._mark_dirty(namespace)
                return True
            return False

    # ── persistence ───────────────────────────────────────────────────────

    def _mark_dirty(self, namespace: str) -> None:
        with self._dirty_lock:
            self._dirty.add(namespace)

    def flush(self, namespace: Optional[str] = None) -> None:
        """Force immediate flush to disk. If namespace is None, flush all dirty."""
        if namespace:
            targets = [namespace]
        else:
            with self._dirty_lock:
                targets = list(self._dirty)
        for ns in targets:
            self._flush_namespace(ns)

    def _record_flush_failure(self, namespace: str, exc: BaseException) -> None:
        self._flush_failures[namespace] = self._flush_failures.get(namespace, 0) + 1
        self._last_flush_error[namespace] = f"{type(exc).__name__}: {exc}"[:300]

    def _flush_namespace(self, namespace: str) -> bool:
        """Persist one namespace. Returns False (and stays dirty) on failure."""
        ns_cfg = self._namespaces.get(namespace)
        if ns_cfg is None:
            return True
        disk_path = ns_cfg.get("path")
        sqlite_key = ns_cfg.get("sqlite_key")
        indent = ns_cfg.get("indent", 2) or 2
        ensure_ascii = bool(ns_cfg.get("ensure_ascii", False))

        # Nothing to persist for agent-only namespaces
        if disk_path is None and sqlite_key is None:
            with self._dirty_lock:
                self._dirty.discard(namespace)
            return True

        compact_text: Optional[str] = None
        serialize_error: Optional[BaseException] = None
        with self._locks[namespace]:
            data = self._data.get(namespace)
            # Clear the flag while holding the namespace lock. Every writer
            # marks the namespace dirty under this same lock, so a write that
            # lands after this point re-marks it and the next cycle flushes
            # it. Clearing the flag AFTER the slow disk write (the previous
            # order) wiped the mark left by a write that arrived meanwhile,
            # and that write was never persisted.
            with self._dirty_lock:
                self._dirty.discard(namespace)
            if data is None:
                return True
            # Serialize once, under the lock, so a concurrent update() cannot
            # resize the dict mid-encode. A caller that keeps mutating a dict
            # it handed to replace() can still race; retry a few times.
            for _attempt in range(3):
                try:
                    compact_text = json.dumps(data, ensure_ascii=False, default=str)
                    serialize_error = None
                    break
                except RuntimeError as exc:
                    serialize_error = exc
                except Exception as exc:
                    serialize_error = exc
                    break

        if compact_text is None:
            logger.error(
                f"StateStore serialization failed for '{namespace}': {serialize_error}"
            )
            self._record_flush_failure(namespace, serialize_error or RuntimeError("serialization failed"))
            self._mark_dirty(namespace)
            return False

        ok = True
        # SQLite first. The same text feeds SQLite and the JSON mirror: with
        # two independent encodes a value only one of them accepted left the
        # two copies permanently different.
        if sqlite_key:
            try:
                from core.sqlite_store import get_sqlite_store  # type: ignore
                get_sqlite_store().set_text(sqlite_key, compact_text)
            except Exception as exc:
                ok = False
                self._record_flush_failure(namespace, exc)
                logger.error(f"StateStore SQLite flush failed for '{namespace}': {exc}")

        # JSON file mirror
        if disk_path:
            try:
                mirror_text = compact_text
                if len(compact_text) <= _PRETTY_MIRROR_MAX_BYTES:
                    mirror_text = json.dumps(
                        json.loads(compact_text), indent=indent, ensure_ascii=ensure_ascii
                    )
                _atomic_write_text(Path(disk_path), mirror_text)
            except Exception as exc:
                ok = False
                self._record_flush_failure(namespace, exc)
                logger.error(f"StateStore disk flush failed for '{namespace}': {exc}")

        if not ok:
            # Keep the namespace dirty: the background writer retries instead
            # of silently dropping the snapshot.
            self._mark_dirty(namespace)
            return False
        self._last_flush[namespace] = time.time()
        return True

    def _background_writer(self) -> None:
        """Background daemon that flushes dirty namespaces every _FLUSH_INTERVAL_SEC."""
        while not self._stop_event.is_set():
            try:
                self._stop_event.wait(timeout=_FLUSH_INTERVAL_SEC)
                if self._stop_event.is_set():
                    break
                with self._dirty_lock:
                    dirty_now = set(self._dirty)
                for ns in dirty_now:
                    try:
                        self._flush_namespace(ns)
                    except Exception as exc:
                        logger.error(f"StateStore background flush error for '{ns}': {exc}")
            except Exception as exc:
                logger.error(f"StateStore background writer error: {exc}")
                time.sleep(1.0)
        # Final flush on shutdown
        with self._dirty_lock:
            remaining = set(self._dirty)
        for ns in remaining:
            try:
                self._flush_namespace(ns)
            except Exception:
                pass

    def stop(self) -> None:
        """Graceful shutdown — flush all dirty and stop background thread."""
        self._stop_event.set()
        self._writer_thread.join(timeout=10)

    def diagnostics(self) -> Dict[str, Any]:
        """Return internal health info."""
        with self._dirty_lock:
            dirty = list(self._dirty)
        result = {}
        for ns in self._namespaces:
            with self._locks[ns]:
                data = self._data.get(ns)
            size = 0
            if isinstance(data, dict):
                size = len(data)
            elif isinstance(data, list):
                size = len(data)
            result[ns] = {
                "loaded": data is not None,
                "size": size,
                "version": self._version.get(ns, 0),
                "dirty": ns in dirty,
                "last_flush": self._last_flush.get(ns, 0.0),
                "flush_failures": self._flush_failures.get(ns, 0),
                "last_flush_error": self._last_flush_error.get(ns),
            }
        return {
            "namespaces": result,
            "dirty_count": len(dirty),
            "flush_interval_sec": _FLUSH_INTERVAL_SEC,
        }


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

def get_state_store() -> StateStore:
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = StateStore()
    return _INSTANCE
