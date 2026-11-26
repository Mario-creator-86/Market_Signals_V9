"""
state_persistence.py — V2: reads and writes route through the in-memory StateStore.

The StateStore's background writer (3-second flush) handles disk I/O so
load/save operations on hot paths never block on file I/O.
"""
import json
import logging
import os
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.config import SIGNALS_PATH, STATE_PATH, _discard_temp_file
from core.sqlite_store import get_sqlite_store

logger = logging.getLogger("StatePersistence")

SQLITE_KEY_STATE = "state"
SQLITE_KEY_SIGNALS = "signals_state"
SIGNALS_SUMMARY_VERSION = 1
SIGNALS_RECENT_CLOSED_LIMIT = 250

_signals_payload_cache_lock = threading.RLock()
_signals_payload_cache = {"marker": None, "data": None}


def _signals_summary_path(path: Optional[Path] = None) -> Path:
    """Return the small sidecar used by latency-sensitive signal readers."""
    target = path or SIGNALS_PATH
    return target.with_name(f"{target.stem}.summary.json")


def _signals_source_marker() -> tuple:
    """Cheap cross-process revision marker; never reads the full signal payload."""
    summary_path = _signals_summary_path()
    try:
        stat = summary_path.stat()
        return ("summary", stat.st_mtime_ns, stat.st_size)
    except OSError:
        pass
    try:
        stat = SIGNALS_PATH.stat()
        return ("signals", stat.st_mtime_ns, stat.st_size)
    except OSError:
        return ("missing",)


def _signal_outcome_counts(closed: Any) -> tuple[int, int]:
    wins = 0
    losses = 0
    for signal in closed if isinstance(closed, list) else []:
        if not isinstance(signal, dict):
            continue
        # A verified quote is necessary but not sufficient: V1 and legacy
        # research signals must never train or market an automated strategy.
        if (
            signal.get("outcome_data_valid") is not True
            or signal.get("research_only") is True
        ):
            continue
        outcome = str(
            signal.get("result")
            or signal.get("outcome")
            or signal.get("status")
            or ""
        ).upper().strip()
        if outcome in {"WIN", "WON", "TAKE_PROFIT", "TP", "PROFIT"}:
            wins += 1
        elif outcome in {"LOSS", "LOST", "STOP_LOSS", "SL", "STOPPED"}:
            losses += 1
    return wins, losses


def _observed_outcome_count(closed: Any) -> int:
    count = 0
    for signal in closed if isinstance(closed, list) else []:
        if not isinstance(signal, dict):
            continue
        outcome = str(
            signal.get("result")
            or signal.get("outcome")
            or signal.get("status")
            or ""
        ).upper().strip()
        if outcome in {"WIN", "WON", "TAKE_PROFIT", "TP", "PROFIT", "LOSS", "LOST", "STOP_LOSS", "SL", "STOPPED"}:
            count += 1
    return count


def _compact_closed_signal(signal: Any) -> dict:
    if not isinstance(signal, dict):
        return {}
    fields = (
        "id", "ticker", "action", "type", "entry", "entry_price", "tp", "sl",
        "exit_price", "created_at", "closed_at", "closedat", "status", "result",
        "outcome", "pnl_pct", "pnl_abs", "variant", "variant_mode", "signal_mode",
        "quality_score", "best_strategy", "signal_thesis", "timeframe",
        "expected_holding_period", "dollar_risk", "signal_age_seconds",
        "position_size_pct", "risk_pct_used",
        "outcome_data_valid", "outcome_validation", "strategy_source",
        "research_only", "capital_execution_eligible", "capital_execution_block_reason",
    )
    return {field: signal[field] for field in fields if field in signal}


def _build_signals_summary(payload: Any) -> dict:
    # Keep the cheap sidecar subject to the same V2-only execution boundary as
    # the full payload. This matters on its first rebuild, before a later read
    # has a chance to migrate legacy rows in place.
    data = _sanitize_signals_payload(payload) if isinstance(payload, dict) else {}
    open_map = data.get("open") if isinstance(data.get("open"), dict) else {}
    closed = data.get("closed") if isinstance(data.get("closed"), list) else []
    recent_closed = sorted(
        (row for row in closed if isinstance(row, dict)),
        key=lambda row: str(row.get("closed_at") or row.get("closedat") or row.get("created_at") or ""),
        reverse=True,
    )[:SIGNALS_RECENT_CLOSED_LIMIT]
    wins, losses = _signal_outcome_counts(closed)
    verified_outcomes = wins + losses
    observed_outcomes = _observed_outcome_count(closed)
    return {
        "schema_version": SIGNALS_SUMMARY_VERSION,
        "updated_at": str(data.get("updated_at") or datetime.now(timezone.utc).isoformat()),
        "open": open_map,
        "open_count": len(open_map),
        "closed_count": len(closed),
        "wins": wins,
        "losses": losses,
        "verified_outcomes": verified_outcomes,
        "unverified_outcomes": max(0, observed_outcomes - verified_outcomes),
        "performance_basis": "verified_price_provenance",
        "recent_closed": [_compact_closed_signal(row) for row in recent_closed],
        "latest_closed_at": str(
            (recent_closed[0].get("closed_at") or recent_closed[0].get("closedat") or recent_closed[0].get("created_at"))
            if recent_closed else ""
        ),
    }


def _write_signals_summary(payload: Any) -> None:
    try:
        _write_json_atomic(_signals_summary_path(), _build_signals_summary(payload), indent=0, ensure_ascii=False)
    except Exception as exc:
        logger.warning(f"Failed writing signals summary sidecar: {exc}")


def load_signals_summary(default: Any = None) -> Any:
    """Load the compact, file-backed signal summary without touching SQLite."""
    fallback = default if default is not None else {}
    summary = _load_json_file(_signals_summary_path(), fallback)
    if not isinstance(summary, dict):
        return fallback

    # The summary sidecar is intentionally read without the full SQLite payload
    # on every UI poll. Migrate the execution boundary here too, otherwise an old
    # sidecar can expose a V1 signal as executable until the next engine save.
    summary = deepcopy(summary)
    changed = False
    for field in ("open",):
        rows = summary.get(field)
        if isinstance(rows, dict):
            for signal in rows.values():
                changed = _mark_research_only_when_unvalidated(signal) or changed
    for signal in summary.get("recent_closed", []):
        changed = _mark_research_only_when_unvalidated(signal) or changed
    if changed:
        try:
            _write_json_atomic(_signals_summary_path(), summary, indent=0, ensure_ascii=False)
        except Exception as exc:
            logger.warning(f"Failed migrating signal summary execution boundaries: {exc}")
    return summary


def _mark_research_only_when_unvalidated(signal: Any) -> bool:
    """Apply the safe V2-only automated-execution boundary in persisted views."""
    if not isinstance(signal, dict):
        return False
    # A signal receives this flag only after the explicit paper-only S7
    # admission gate has approved it.  Retain the normal Capital classification
    # in saved UI state; non-paper execution remains blocked by that gate.
    if bool(signal.get("paper_capital_experiment")):
        return False
    strategy_source = str(signal.get("strategy_source") or "V1").upper().strip()
    if strategy_source == "V2":
        return False

    changed = False
    expected = {
        "research_only": True,
        "capital_execution_eligible": False,
        "capital_execution_block_reason": (
            "Automated Capital execution requires a faithful V2 OOS validation"
        ),
    }
    for key, value in expected.items():
        if key == "capital_execution_block_reason" and signal.get(key):
            continue
        if signal.get(key) != value:
            signal[key] = value
            changed = True
    return changed


def _sanitize_signals_payload(data: Any) -> Any:
    if not isinstance(data, dict):
        return data

    payload = deepcopy(data)
    open_map = payload.get("open")
    if isinstance(open_map, dict):
        cleaned_open = {}
        for key, sig in open_map.items():
            if not isinstance(sig, dict):
                continue
            ticker = str(sig.get("ticker") or "").upper().strip()
            if not ticker:
                continue
            # Drop garbage tickers from the pre-fix empty-base USDT/USDC
            # conversion (e.g. "-USD" from a bare "USDT" symbol). Not a real
            # asset — would render as a blank ticker on Home and fail every
            # price fetch. Auto-heals persisted/SQLite state on read (directive:
            # nothing invented, every value correct). Upstream converters now
            # guard empty bases, so no new garbage is produced.
            if ticker.startswith("-") or ticker == "USD-USD":
                continue
            has_payload = any(
                sig.get(field) not in (None, "", 0, 0.0)
                for field in ("id", "action", "type", "created_at", "entry", "tp", "sl")
            )
            if not has_payload:
                continue
            _mark_research_only_when_unvalidated(sig)
            cleaned_open[key] = sig
        payload["open"] = cleaned_open

    closed = payload.get("closed")
    if isinstance(closed, list):
        for signal in closed:
            _mark_research_only_when_unvalidated(signal)

    return payload


def _write_json_atomic(path: Path, data: Any, *, indent: int = 2, ensure_ascii: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
        temp_path = Path(tmp_name)
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)  # an open descriptor would keep the temp file undeletable
            raise
        with handle as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)
        import time
        for attempt in range(5):
            try:
                os.replace(str(temp_path), str(path))
                temp_path = None
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1)
    finally:
        # Reached with a temp file only when the write or the replace failed;
        # the removal is retried (a single swallowed unlink leaked the file).
        if temp_path is not None:
            _discard_temp_file(temp_path)


def _load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception as e:
        logger.error(f"Failed reading JSON file {path}: {e}")
        return default


def load_persistent_json(path: Path, key: str, default: Any) -> Any:
    store = None
    try:
        store = get_sqlite_store()
    except Exception as e:
        logger.error(f"SQLite unavailable while loading {key}: {e}")

    if store is not None:
        try:
            data = store.get_json(key, None)
            if data is not None:
                return data
        except Exception as e:
            logger.error(f"Failed loading key '{key}' from SQLite: {e}")

    data = _load_json_file(path, default)
    if data is default:
        return default

    if store is not None:
        try:
            store.set_json(key, data)
        except Exception as e:
            logger.error(f"Failed syncing key '{key}' to SQLite after file load: {e}")
    return data


def save_persistent_json(path: Path, key: str, data: Any, *, indent: int = 2, ensure_ascii: bool = False) -> None:
    store = None
    try:
        store = get_sqlite_store()
    except Exception as e:
        logger.error(f"SQLite unavailable while saving {key}: {e}")

    if store is not None:
        try:
            store.set_json(key, data)
        except Exception as e:
            logger.error(f"Failed saving key '{key}' to SQLite: {e}")

    _write_json_atomic(path, data, indent=indent, ensure_ascii=ensure_ascii)


# ---------------------------------------------------------------------------
# Public helpers — route through the in-memory StateStore first.
# The StateStore background-writer flushes to disk every 3 s.
# ---------------------------------------------------------------------------

def state_owned_by_this_process() -> bool:
    """True when this process is the writer of the market ``state`` snapshot.

    With ``MS_ENGINE_MODE=subprocess`` the engine process produces the state
    and the API process only reads it. The API must then never pin a private
    in-memory copy: after its first ``save_state_payload`` (manual signal
    delete, global-settings apply) every later ``load_state_payload`` returned
    that frozen copy forever, so prices and tickers in the UI stopped updating
    until a restart, and the stale copy was flushed over the engine's state.
    """
    role = str(os.getenv("MS_PROCESS_ROLE", "") or "").strip().lower()
    mode = str(os.getenv("MS_ENGINE_MODE", "") or "").strip().lower()
    return not (role == "api" and mode == "subprocess")


def load_state_payload(default: Any = None) -> Any:
    """Return state dict. Reads from RAM when this process owns the state."""
    fallback = default if default is not None else {}
    if state_owned_by_this_process():
        try:
            from core.state_store import get_state_store  # lazy to avoid circular imports
            snap = get_state_store().get_snapshot("state")
            if snap is not None:
                return snap
        except Exception:
            pass
    return load_persistent_json(STATE_PATH, SQLITE_KEY_STATE, fallback)


def save_state_payload(data: Any) -> None:
    """Persist the state snapshot.

    The owning process writes to the in-memory store (a background thread
    flushes it). A non-owning process (API with a subprocess engine) writes
    straight through to storage and keeps no private copy, so its next read
    sees whatever the engine saved last.
    """
    if state_owned_by_this_process():
        try:
            from core.state_store import get_state_store
            get_state_store().replace("state", data)
            return
        except Exception as exc:
            logger.warning(f"StateStore unavailable — falling back to direct write: {exc}")
    save_persistent_json(STATE_PATH, SQLITE_KEY_STATE, data, indent=2, ensure_ascii=False)


def load_signals_payload(default: Any = None, *, include_closed: bool = True) -> Any:
    """Return signal state with cache-first, cross-process-safe reads.

    ``include_closed=False`` uses the small summary sidecar and avoids loading the
    complete SQLite history. It is intended for UI polls and status endpoints;
    full-history workflows retain the existing default behaviour.
    """
    fallback = default if default is not None else {"open": {}, "closed": []}
    if not include_closed:
        summary = load_signals_summary(None)
        if isinstance(summary, dict) and summary:
            open_map = summary.get("open") if isinstance(summary.get("open"), dict) else {}
            recent_closed = summary.get("recent_closed") if isinstance(summary.get("recent_closed"), list) else []
            return {
                "open": deepcopy(open_map),
                "closed": deepcopy(recent_closed),
                "_summary": {
                    "open_count": int(summary.get("open_count") or len(open_map)),
                    "closed_count": int(summary.get("closed_count") or 0),
                    "wins": int(summary.get("wins") or 0),
                    "losses": int(summary.get("losses") or 0),
                    "verified_outcomes": int(summary.get("verified_outcomes") or 0),
                    "unverified_outcomes": int(summary.get("unverified_outcomes") or 0),
                    "performance_basis": str(summary.get("performance_basis") or "verified_price_provenance"),
                    "latest_closed_at": str(summary.get("latest_closed_at") or ""),
                    "updated_at": str(summary.get("updated_at") or ""),
                },
            }

    marker = _signals_source_marker()
    with _signals_payload_cache_lock:
        cached = _signals_payload_cache.get("data")
        if cached is not None and _signals_payload_cache.get("marker") == marker:
            return deepcopy(cached)

    try:
        payload = load_persistent_json(SIGNALS_PATH, SQLITE_KEY_SIGNALS, fallback)
        sanitized = _sanitize_signals_payload(payload)
        _write_signals_summary(sanitized)
        with _signals_payload_cache_lock:
            _signals_payload_cache["marker"] = _signals_source_marker()
            _signals_payload_cache["data"] = sanitized
        try:
            from core.state_store import get_state_store
            store = get_state_store()
            if store.get_raw("signals") is None:
                store.replace("signals", sanitized)
        except Exception:
            pass
        return deepcopy(sanitized)
    except Exception as exc:
        logger.warning(f"Persistent signals load failed; using StateStore snapshot if available: {exc}")

    try:
        from core.state_store import get_state_store
        snap = get_state_store().get_snapshot("signals")
        if snap is not None:
            return _sanitize_signals_payload(snap)
    except Exception:
        pass
    payload = load_persistent_json(SIGNALS_PATH, SQLITE_KEY_SIGNALS, fallback)
    return _sanitize_signals_payload(payload)


def save_signals_payload(data: Any) -> None:
    """Write signals to in-memory store; background thread handles disk flush."""
    sanitized = _sanitize_signals_payload(data)
    try:
        from core.state_store import get_state_store
        store = get_state_store()
        store.replace("signals", sanitized)
        store.flush("signals")
        _write_signals_summary(sanitized)
        with _signals_payload_cache_lock:
            _signals_payload_cache["marker"] = _signals_source_marker()
            _signals_payload_cache["data"] = sanitized
        return
    except Exception as exc:
        logger.warning(f"StateStore unavailable for signals — falling back: {exc}")
    save_persistent_json(SIGNALS_PATH, SQLITE_KEY_SIGNALS, sanitized, indent=2, ensure_ascii=False)
    _write_signals_summary(sanitized)
    with _signals_payload_cache_lock:
        _signals_payload_cache["marker"] = _signals_source_marker()
        _signals_payload_cache["data"] = sanitized
