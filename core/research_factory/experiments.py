"""Phase 14 §77 — experiment registry with the nine spec statuses.

PROPOSED → APPROVED_FOR_RESEARCH → RUNNING → {FAILED | REJECTED | VALIDATED
→ SHADOW → {PROMOTED | ROLLED_BACK}} — a legal-transition table enforces the
lifecycle; every transition appends an immutable event. Experiments link a
§76 hypothesis to a strategy/version and carry evaluation evidence.
Promotion itself remains gated by the Phase 8 StrategyRegistry policy;
Phase 14 hands off at SHADOW.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import DATA_DIR
from core.research_factory.hypothesis import HypothesisStore

DEFAULT_EXPERIMENTS_PATH = DATA_DIR / "research_experiments.json"
SCHEMA_VERSION = 1


def default_experiments_path() -> Path:
    import os

    override = os.environ.get("MS_RESEARCH_EXPERIMENTS_PATH", "").strip()
    return Path(override) if override else DEFAULT_EXPERIMENTS_PATH


class ExperimentStatus(str, Enum):
    PROPOSED = "PROPOSED"
    APPROVED_FOR_RESEARCH = "APPROVED_FOR_RESEARCH"
    RUNNING = "RUNNING"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    VALIDATED = "VALIDATED"
    SHADOW = "SHADOW"
    PROMOTED = "PROMOTED"
    ROLLED_BACK = "ROLLED_BACK"


_LEGAL_TRANSITIONS: Dict[ExperimentStatus, frozenset] = {
    ExperimentStatus.PROPOSED: frozenset({ExperimentStatus.APPROVED_FOR_RESEARCH,
                                          ExperimentStatus.REJECTED}),
    ExperimentStatus.APPROVED_FOR_RESEARCH: frozenset({ExperimentStatus.RUNNING,
                                                       ExperimentStatus.REJECTED}),
    ExperimentStatus.RUNNING: frozenset({ExperimentStatus.VALIDATED,
                                         ExperimentStatus.REJECTED,
                                         ExperimentStatus.FAILED}),
    ExperimentStatus.VALIDATED: frozenset({ExperimentStatus.SHADOW,
                                           ExperimentStatus.REJECTED}),
    ExperimentStatus.SHADOW: frozenset({ExperimentStatus.PROMOTED,
                                        ExperimentStatus.ROLLED_BACK}),
    ExperimentStatus.PROMOTED: frozenset({ExperimentStatus.ROLLED_BACK}),
    ExperimentStatus.FAILED: frozenset(),
    ExperimentStatus.REJECTED: frozenset(),
    ExperimentStatus.ROLLED_BACK: frozenset(),
}


class ExperimentError(ValueError):
    """Invalid experiment record or illegal lifecycle transition."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ExperimentEvent:
    event_id: str
    experiment_id: str
    from_status: str
    to_status: str
    actor: str
    reason: str
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id, "experiment_id": self.experiment_id,
            "from_status": self.from_status, "to_status": self.to_status,
            "actor": self.actor, "reason": self.reason, "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ExperimentRecord:
    experiment_id: str
    hypothesis_id: str
    strategy_id: str
    version: str
    status: str
    description: str
    created_by: str = "experiment_planner"
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    evidence: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "experiment_id": self.experiment_id, "hypothesis_id": self.hypothesis_id,
            "strategy_id": self.strategy_id, "version": self.version,
            "status": self.status, "description": self.description,
            "created_by": self.created_by, "created_at": self.created_at,
            "updated_at": self.updated_at, "evidence": dict(self.evidence),
            "events": [dict(event) for event in self.events],
        }

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "ExperimentRecord":
        known = set(ExperimentRecord.__dataclass_fields__)
        payload = {k: v for k, v in (raw or {}).items() if k in known}
        return ExperimentRecord(**payload)


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def validate_transition(source: ExperimentStatus, destination: ExperimentStatus) -> None:
    if destination not in _LEGAL_TRANSITIONS.get(source, frozenset()):
        raise ExperimentError(f"illegal experiment transition {source.value} -> {destination.value}")


class ExperimentRegistry:
    """Atomic JSON experiment registry with an immutable event log."""

    def __init__(self, path: Optional[Path] = None,
                 hypothesis_store: Optional[HypothesisStore] = None):
        self.path = Path(path) if path is not None else default_experiments_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.hypothesis_store = hypothesis_store

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict) and isinstance(payload.get("experiments"), list):
                return payload
        except (OSError, ValueError):
            pass
        return {"schema_version": SCHEMA_VERSION, "experiments": []}

    def _save(self, payload: Dict[str, Any]) -> None:
        with _path_lock(self.path):
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    def propose(
        self, *, hypothesis_id: str, strategy_id: str, version: str, description: str,
        created_by: str = "experiment_planner", evidence: Optional[Dict[str, Any]] = None,
    ) -> ExperimentRecord:
        if not str(hypothesis_id or "").strip():
            raise ExperimentError("experiment requires a hypothesis_id")
        if self.hypothesis_store is not None and self.hypothesis_store.get(hypothesis_id) is None:
            raise ExperimentError(f"unknown hypothesis_id: {hypothesis_id}")
        if not str(strategy_id or "").strip() or not str(description or "").strip():
            raise ExperimentError("experiment requires strategy_id and description")
        record = ExperimentRecord(
            experiment_id=f"exp_{uuid.uuid4().hex[:12]}",
            hypothesis_id=hypothesis_id, strategy_id=str(strategy_id).strip(),
            version=str(version or "1").strip(), status=ExperimentStatus.PROPOSED.value,
            description=str(description).strip(), created_by=created_by,
            evidence=dict(evidence or {}),
            events=[ExperimentEvent(
                event_id=uuid.uuid4().hex, experiment_id="", from_status="",
                to_status=ExperimentStatus.PROPOSED.value, actor=created_by,
                reason="proposed", created_at=_utc_now(),
            ).to_dict()],
        )
        record.events[0]["experiment_id"] = record.experiment_id
        with _path_lock(self.path):
            payload = self._load()
            payload["experiments"].append(record.to_dict())
            self._save(payload)
        return record

    def get(self, experiment_id: str) -> Optional[ExperimentRecord]:
        for raw in self._load()["experiments"]:
            if raw.get("experiment_id") == experiment_id:
                return ExperimentRecord.from_dict(raw)
        return None

    def list_records(self, *, limit: int = 100, status: Optional[str] = None) -> List[ExperimentRecord]:
        records = [ExperimentRecord.from_dict(raw) for raw in self._load()["experiments"]]
        if status:
            records = [r for r in records if r.status == str(status).upper()]
        return list(reversed(records[-max(1, min(500, int(limit or 100))):]))

    def transition(
        self, experiment_id: str, destination: ExperimentStatus | str, *,
        actor: str, reason: str = "", evidence: Optional[Dict[str, Any]] = None,
    ) -> ExperimentRecord:
        target = destination if isinstance(destination, ExperimentStatus) else ExperimentStatus(str(destination).upper())
        with _path_lock(self.path):
            payload = self._load()
            for index, raw in enumerate(payload["experiments"]):
                if raw.get("experiment_id") != experiment_id:
                    continue
                record = ExperimentRecord.from_dict(raw)
                source = ExperimentStatus(record.status)
                validate_transition(source, target)
                event = ExperimentEvent(
                    event_id=uuid.uuid4().hex, experiment_id=experiment_id,
                    from_status=source.value, to_status=target.value,
                    actor=str(actor or "system"), reason=str(reason or ""), created_at=_utc_now(),
                )
                updated = ExperimentRecord(
                    **{**record.to_dict(),
                       "status": target.value,
                       "updated_at": _utc_now(),
                       "evidence": {**record.evidence, **(evidence or {})},
                       "events": record.events + [event.to_dict()]}
                )
                payload["experiments"][index] = updated.to_dict()
                self._save(payload)
                return updated
        raise ExperimentError(f"unknown experiment_id: {experiment_id}")

    def status_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for raw in self._load()["experiments"]:
            status = str(raw.get("status") or "")
            counts[status] = counts.get(status, 0) + 1
        return counts


__all__ = [
    "ExperimentStatus", "ExperimentRecord", "ExperimentRegistry", "ExperimentError",
    "validate_transition", "DEFAULT_EXPERIMENTS_PATH", "SCHEMA_VERSION",
]
