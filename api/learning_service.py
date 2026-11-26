"""Learning-mode progress for the API and the cockpit.

The worker runs in the engine process and writes ``data/learning_mode/status.json``
after every pass; this only reads it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

STALE_AFTER = timedelta(minutes=10)


def _root() -> Path:
    from core.config import DATA_DIR

    return Path(DATA_DIR) / "learning_mode"


def learning_state(root: Optional[Path] = None, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The latest learning-mode status: progress toward the target, today's count, a pause and its reason."""
    folder = Path(root) if root is not None else _root()
    moment = now or datetime.now(timezone.utc)
    path = folder / "status.json"
    if not path.exists():
        return {"status": "not_started", "reason": "The learning-mode worker has not written its status yet."}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "unreadable", "reason": "The learning-mode status file could not be read."}
    if not isinstance(data, dict):
        return {"status": "unreadable", "reason": "The learning-mode status file is not an object."}
    try:
        written = datetime.fromisoformat(str(data.get("updated_at")).replace("Z", "+00:00"))
        stale = moment - written > STALE_AFTER
    except (TypeError, ValueError):
        stale = True
    return {**data, "stale": bool(stale)}


__all__ = ["learning_state"]
