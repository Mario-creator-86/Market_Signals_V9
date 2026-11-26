"""Small file-backed control plane for the subprocess autonomous monitor.

The API and MarketEngine intentionally run in separate processes in production.
This module provides a bounded, auditable request/result hand-off without
sharing process memory or weakening any execution gate.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


from core.config import DATA_DIR

BASE_DIR = Path(__file__).resolve().parents[1]
# In the configured data folder, never a hard-coded <repo>/data: with two
# instances a shared request file let one instance's "run cycle" request be
# executed by the other instance's engine.
REQUEST_PATH = DATA_DIR / "autonomous_cycle_request.json"
STATUS_PATH = DATA_DIR / "autonomous_cycle_status.json"
ALLOCATION_STATE_PATH = DATA_DIR / "autonomous_allocation_state.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return dict(payload) if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[Path] = None
    try:
        fd, raw_path = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
        temp_path = Path(raw_path)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(str(temp_path), str(path))
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def request_autonomous_cycle(*, request_path: Path = REQUEST_PATH) -> Dict[str, Any]:
    """Queue one paper-mode review request for the engine subprocess."""
    request = {
        "request_id": f"autonomous_{uuid.uuid4().hex}",
        "requested_at": _utc_now_iso(),
        "request_type": "run_once",
    }
    _write_json_atomic(request_path, request)
    return request


def consume_autonomous_cycle_request(*, request_path: Path = REQUEST_PATH) -> Dict[str, Any]:
    """Read and acknowledge one queued request. Invalid files fail closed."""
    payload = _read_json(request_path)
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id or str(payload.get("request_type") or "") != "run_once":
        return {}
    try:
        request_path.unlink(missing_ok=True)
    except OSError:
        return {}
    return payload


def record_autonomous_cycle_result(
    result: Dict[str, Any],
    *,
    request: Optional[Dict[str, Any]] = None,
    status_path: Path = STATUS_PATH,
) -> Dict[str, Any]:
    """Persist the last autonomous cycle outcome for API/UI observability."""
    payload = {
        "updated_at": _utc_now_iso(),
        "request_id": str((request or {}).get("request_id") or ""),
        "requested_at": (request or {}).get("requested_at"),
        "trigger": str((request or {}).get("request_type") or "manual"),
        "result": dict(result or {}),
    }
    _write_json_atomic(status_path, payload)
    return payload


def get_autonomous_cycle_status(*, status_path: Path = STATUS_PATH) -> Dict[str, Any]:
    return _read_json(status_path)


def get_autonomous_allocation_state(
    *,
    state_path: Path = ALLOCATION_STATE_PATH,
) -> Dict[str, Any]:
    """Return the durable weighted-round-robin accumulator."""
    return _read_json(state_path)


def record_autonomous_allocation_state(
    *,
    weights_pct: Dict[str, Any],
    current: Dict[str, Any],
    last_target_bucket: str,
    state_path: Path = ALLOCATION_STATE_PATH,
) -> Dict[str, Any]:
    """Persist scheduler state so service restarts cannot bias one allocation lane."""
    payload = {
        "updated_at": _utc_now_iso(),
        "weights_pct": dict(weights_pct or {}),
        "current": dict(current or {}),
        "last_target_bucket": str(last_target_bucket or ""),
    }
    _write_json_atomic(state_path, payload)
    return payload
