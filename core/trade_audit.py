import json
import logging
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import DATA_DIR
from core.sqlite_store import get_sqlite_store


logger = logging.getLogger("TradeAudit")

TRADE_AUDIT_PATH = Path(
    os.getenv("MS_TRADE_AUDIT_PATH") or (DATA_DIR / "trade_decisions.json")
)
TRADE_AUDIT_KEY = "trade_decision_log"
TRADE_AUDIT_LIMIT = 500
# RLock (not Lock): record_trade_event wraps its whole load->append->save RMW in
# this lock, and _load_payload/_save_payload re-acquire it internally for short
# cache sections. A non-reentrant Lock would self-deadlock on that re-entry.
_AUDIT_CACHE_LOCK = threading.RLock()
_AUDIT_CACHE = {"ts": 0.0, "payload": None}
_AUDIT_CACHE_TTL_SECONDS = 30.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_payload() -> Dict[str, Any]:
    now_ts = time.time()
    with _AUDIT_CACHE_LOCK:
        cached = _AUDIT_CACHE.get("payload")
        if isinstance(cached, dict) and (now_ts - float(_AUDIT_CACHE.get("ts") or 0.0)) <= _AUDIT_CACHE_TTL_SECONDS:
            return cached

    store = None
    try:
        store = get_sqlite_store()
    except Exception as exc:
        logger.error(f"Trade audit SQLite unavailable: {exc}")

    if store is not None:
        try:
            payload = store.get_json(TRADE_AUDIT_KEY, None)
            if isinstance(payload, dict):
                with _AUDIT_CACHE_LOCK:
                    _AUDIT_CACHE["payload"] = payload
                    _AUDIT_CACHE["ts"] = time.time()
                return payload
        except Exception as exc:
            logger.error(f"Trade audit SQLite read failed: {exc}")

    if TRADE_AUDIT_PATH.exists():
        try:
            payload = json.loads(TRADE_AUDIT_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                with _AUDIT_CACHE_LOCK:
                    _AUDIT_CACHE["payload"] = payload
                    _AUDIT_CACHE["ts"] = time.time()
            return payload
        except Exception as exc:
            logger.error(f"Trade audit file read failed: {exc}")

    payload = {"records": [], "updated_at": None}
    with _AUDIT_CACHE_LOCK:
        _AUDIT_CACHE["payload"] = payload
        _AUDIT_CACHE["ts"] = time.time()
    return payload


def _save_payload(payload: Dict[str, Any]) -> None:
    data = payload if isinstance(payload, dict) else {"records": [], "updated_at": None}
    store = None
    try:
        store = get_sqlite_store()
    except Exception as exc:
        logger.error(f"Trade audit SQLite unavailable on save: {exc}")

    if store is not None:
        try:
            store.set_json(TRADE_AUDIT_KEY, data)
        except Exception as exc:
            logger.error(f"Trade audit SQLite write failed: {exc}")

    TRADE_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[Path] = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix="trade_decisions.", suffix=".tmp", dir=str(TRADE_AUDIT_PATH.parent))
        temp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        Path(tmp_name).replace(TRADE_AUDIT_PATH)
    except Exception as exc:
        logger.error(f"Trade audit file write failed: {exc}")
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
    finally:
        with _AUDIT_CACHE_LOCK:
            _AUDIT_CACHE["payload"] = data
            _AUDIT_CACHE["ts"] = time.time()


def record_trade_event(
    *,
    event_type: str,
    status: str,
    ticker: str = "",
    signal_id: str = "",
    position_id: str = "",
    strategy_type: str = "",
    summary: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    record = {
        "event_id": f"audit_{uuid.uuid4().hex[:12]}",
        "timestamp": _utc_now_iso(),
        "event_type": str(event_type or "").strip(),
        "status": str(status or "").strip(),
        "ticker": str(ticker or "").strip().upper(),
        "signal_id": str(signal_id or "").strip(),
        "position_id": str(position_id or "").strip(),
        "strategy_type": str(strategy_type or "").strip(),
        "summary": str(summary or "").strip(),
        "payload": dict(payload or {}),
    }
    # Atomic load->append->save: hold the audit lock across the whole RMW so two
    # concurrent record_trade_event callers cannot both load the same cached
    # snapshot, each append their record to their own copy, and have the second
    # save overwrite the first (silently losing a compliance audit record).
    # _load_payload/_save_payload re-acquire this RLock internally (reentrant).
    with _AUDIT_CACHE_LOCK:
        data = _load_payload()
        records = data.get("records", [])
        if not isinstance(records, list):
            records = []
        records.append(record)
        data["records"] = records[-TRADE_AUDIT_LIMIT:]
        data["updated_at"] = _utc_now_iso()
        _save_payload(data)
    return record


def get_trade_audit(limit: int = 100) -> List[Dict[str, Any]]:
    data = _load_payload()
    records = data.get("records", [])
    if not isinstance(records, list):
        return []
    try:
        max_items = max(1, min(500, int(limit or 100)))
    except Exception:
        max_items = 100
    return list(records[-max_items:])[::-1]
