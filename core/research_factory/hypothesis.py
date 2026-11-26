"""Phase 14 §76 — structured, testable hypothesis records.

"No vague 'Maybe RSI should be changed.'" — validation enforces every
mandated field, explicit promotion/failure criteria and metrics before a
hypothesis can anchor an experiment. Atomic JSON store follows the Phase 8
`strategy_registry` pattern (per-path lock, temp-file write, os.replace).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import DATA_DIR

DEFAULT_HYPOTHESES_PATH = DATA_DIR / "research_hypotheses.json"
SCHEMA_VERSION = 1


def default_hypotheses_path() -> Path:
    import os

    override = os.environ.get("MS_RESEARCH_HYPOTHESES_PATH", "").strip()
    return Path(override) if override else DEFAULT_HYPOTHESES_PATH

_HYPOTHESIS_FIELDS = (
    "problem", "observation", "hypothesis", "expected_mechanism", "baseline",
    "challenger", "universe", "horizon", "dataset",
)


class HypothesisError(ValueError):
    """A hypothesis record violates the §76 contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class HypothesisRecord:
    hypothesis_id: str
    problem: str
    observation: str
    hypothesis: str
    expected_mechanism: str
    baseline: str
    challenger: str
    universe: str
    horizon: str
    dataset: str
    metrics: List[str]
    promotion_criteria: List[str]
    failure_criteria: List[str]
    risks: List[str]
    created_by: str = "research_director"
    created_at: str = field(default_factory=_utc_now)
    source_observation: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "problem": self.problem,
            "observation": self.observation,
            "hypothesis": self.hypothesis,
            "expected_mechanism": self.expected_mechanism,
            "baseline": self.baseline,
            "challenger": self.challenger,
            "universe": self.universe,
            "horizon": self.horizon,
            "dataset": self.dataset,
            "metrics": list(self.metrics),
            "promotion_criteria": list(self.promotion_criteria),
            "failure_criteria": list(self.failure_criteria),
            "risks": list(self.risks),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "source_observation": dict(self.source_observation) if self.source_observation else None,
        }

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "HypothesisRecord":
        known = set(HypothesisRecord.__dataclass_fields__)
        payload = {k: v for k, v in (raw or {}).items() if k in known}
        return HypothesisRecord(**payload)


def draft_hypothesis(
    *,
    problem: str,
    observation: str,
    hypothesis: str,
    expected_mechanism: str,
    baseline: str,
    challenger: str,
    universe: str,
    horizon: str,
    dataset: str,
    metrics: List[str],
    promotion_criteria: List[str],
    failure_criteria: List[str],
    risks: Optional[List[str]] = None,
    created_by: str = "research_director",
    source_observation: Optional[Dict[str, Any]] = None,
) -> HypothesisRecord:
    """Validate §76 completeness, then mint an immutable record."""
    for name in _HYPOTHESIS_FIELDS:
        if not str(locals().get(name, "") or "").strip():
            raise HypothesisError(f"hypothesis field '{name}' must be a non-empty string")
    if not horizon or str(horizon).upper() not in {"SHORT_TERM", "SWING", "MEDIUM_TERM"}:
        raise HypothesisError("horizon must be SHORT_TERM, SWING or MEDIUM_TERM")
    if not metrics:
        raise HypothesisError("at least one metric is required")
    if not promotion_criteria:
        raise HypothesisError("explicit promotion criteria are required (§76)")
    if not failure_criteria:
        raise HypothesisError("explicit failure criteria are required (§76)")
    return HypothesisRecord(
        hypothesis_id=f"hyp_{uuid.uuid4().hex[:12]}",
        problem=str(problem).strip(), observation=str(observation).strip(),
        hypothesis=str(hypothesis).strip(), expected_mechanism=str(expected_mechanism).strip(),
        baseline=str(baseline).strip(), challenger=str(challenger).strip(),
        universe=str(universe).strip(), horizon=str(horizon).upper(),
        dataset=str(dataset).strip(),
        metrics=[str(m).strip() for m in metrics if str(m).strip()],
        promotion_criteria=[str(c).strip() for c in promotion_criteria if str(c).strip()],
        failure_criteria=[str(c).strip() for c in failure_criteria if str(c).strip()],
        risks=[str(r).strip() for r in (risks or []) if str(r).strip()],
        created_by=created_by,
        source_observation=source_observation,
    )


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


class HypothesisStore:
    """Append-only hypothesis records + immutable updates via replace+event."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else default_hypotheses_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict) and isinstance(payload.get("hypotheses"), list):
                return payload
        except (OSError, ValueError):
            pass
        return {"schema_version": SCHEMA_VERSION, "hypotheses": []}

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

    def append(self, record: HypothesisRecord) -> HypothesisRecord:
        with _path_lock(self.path):
            payload = self._load()
            if any(h.get("hypothesis_id") == record.hypothesis_id for h in payload["hypotheses"]):
                return record  # idempotent
            payload["hypotheses"].append(record.to_dict())
            self._save(payload)
        return record

    def get(self, hypothesis_id: str) -> Optional[HypothesisRecord]:
        for raw in self._load()["hypotheses"]:
            if raw.get("hypothesis_id") == hypothesis_id:
                return HypothesisRecord.from_dict(raw)
        return None

    def list_records(self, *, limit: int = 100) -> List[HypothesisRecord]:
        records = [HypothesisRecord.from_dict(raw) for raw in self._load()["hypotheses"]]
        return records[-max(1, min(500, int(limit or 100))):]

    def count(self) -> int:
        return len(self._load()["hypotheses"])


__all__ = [
    "HypothesisRecord", "HypothesisStore", "HypothesisError", "draft_hypothesis",
    "DEFAULT_HYPOTHESES_PATH", "SCHEMA_VERSION",
]
