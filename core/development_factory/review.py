"""Phase 15 §81 — staged review ledger: implementer ≠ reviewer.

A submission walks CODE_REVIEW → SECURITY_REVIEW → TESTS → QUANT_VALIDATION
→ E2E → SHADOW_READY. Every stage records its own reviewer actor; the author
can never record a stage (§81); verdicts are append-only; SHADOW_READY
requires every stage PASSED with at least two distinct non-author actors.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import DATA_DIR

DEFAULT_SUBMISSIONS_PATH = DATA_DIR / "development_submissions.json"
SCHEMA_VERSION = 1

STAGE_ORDER = (
    "CODE_REVIEW", "SECURITY_REVIEW", "TESTS", "QUANT_VALIDATION", "E2E",
)

STAGE_ACTORS = {
    "CODE_REVIEW": "code_review_agent",
    "SECURITY_REVIEW": "security_review_agent",
    "TESTS": "test_agent",
    "QUANT_VALIDATION": "quant_validator",
    "E2E": "e2e_agent",
}


class ReviewError(ValueError):
    """Invalid submission or illegal review advance."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_submissions_path() -> Path:
    override = os.environ.get("MS_DEVELOPMENT_SUBMISSIONS_PATH", "").strip()
    return Path(override) if override else DEFAULT_SUBMISSIONS_PATH


@dataclass(frozen=True)
class ReviewVerdict:
    stage: str
    actor: str
    verdict: str          # PASSED | FAILED | NOT_PERFORMED
    evidence: Dict[str, Any]
    reviewed_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage, "actor": self.actor, "verdict": self.verdict,
            "evidence": dict(self.evidence), "reviewed_at": self.reviewed_at,
        }


@dataclass(frozen=True)
class Submission:
    submission_id: str
    experiment_id: str
    author: str
    branch: str
    worktree: str
    diff_sha256: str
    status: str                       # OPEN | REJECTED | SHADOW_READY
    created_at: str = field(default_factory=_utc_now)
    reviews: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "submission_id": self.submission_id, "experiment_id": self.experiment_id,
            "author": self.author, "branch": self.branch, "worktree": self.worktree,
            "diff_sha256": self.diff_sha256, "status": self.status,
            "created_at": self.created_at, "reviews": [dict(r) for r in self.reviews],
        }

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "Submission":
        known = set(Submission.__dataclass_fields__)
        return Submission(**{k: v for k, v in (raw or {}).items() if k in known})


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


class ReviewLedger:
    """Atomic JSON store of submissions and their append-only review stages."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else default_submissions_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict) and isinstance(payload.get("submissions"), list):
                return payload
        except (OSError, ValueError):
            pass
        return {"schema_version": SCHEMA_VERSION, "submissions": []}

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

    def open_submission(
        self, *, experiment_id: str, author: str, branch: str, worktree: str,
        diff_sha256: str,
    ) -> Submission:
        if not all(str(x or "").strip() for x in (experiment_id, author, branch, worktree, diff_sha256)):
            raise ReviewError("submission requires experiment_id/author/branch/worktree/diff_sha256")
        submission = Submission(
            submission_id=f"sub_{uuid.uuid4().hex[:12]}",
            experiment_id=str(experiment_id), author=str(author),
            branch=str(branch), worktree=str(worktree), diff_sha256=str(diff_sha256),
            status="OPEN",
        )
        with _path_lock(self.path):
            payload = self._load()
            payload["submissions"].append(submission.to_dict())
            self._save(payload)
        return submission

    def get(self, submission_id: str) -> Optional[Submission]:
        for raw in self._load()["submissions"]:
            if raw.get("submission_id") == submission_id:
                return Submission.from_dict(raw)
        return None

    def list_submissions(self, *, limit: int = 50, experiment_id: Optional[str] = None) -> List[Submission]:
        records = [Submission.from_dict(raw) for raw in self._load()["submissions"]]
        if experiment_id:
            records = [r for r in records if r.experiment_id == experiment_id]
        return list(reversed(records[-max(1, min(500, int(limit or 50))):]))

    def record_review(
        self, submission_id: str, *, stage: str, actor: str,
        verdict: str, evidence: Optional[Dict[str, Any]] = None,
    ) -> Submission:
        normalized_stage = str(stage or "").upper().strip()
        if normalized_stage not in STAGE_ORDER:
            raise ReviewError(f"unknown review stage: {stage}")
        if verdict not in {"PASSED", "FAILED", "NOT_PERFORMED"}:
            raise ReviewError("verdict must be PASSED, FAILED or NOT_PERFORMED")
        with _path_lock(self.path):
            payload = self._load()
            for index, raw in enumerate(payload["submissions"]):
                if raw.get("submission_id") != submission_id:
                    continue
                submission = Submission.from_dict(raw)
                if submission.status != "OPEN":
                    raise ReviewError(f"submission is {submission.status}; reviews are closed")
                if str(actor or "").strip() == submission.author:
                    raise ReviewError("§81: the implementer cannot record any review stage")
                expected = STAGE_ACTORS[normalized_stage]
                if str(actor or "").strip() != expected:
                    raise ReviewError(f"stage {normalized_stage} must be recorded by {expected}")
                recorded = {review["stage"] for review in submission.reviews}
                if normalized_stage in recorded:
                    raise ReviewError(f"stage {normalized_stage} already recorded (append-only)")
                entry = ReviewVerdict(
                    stage=normalized_stage, actor=str(actor).strip(), verdict=str(verdict),
                    evidence=dict(evidence or {}), reviewed_at=_utc_now(),
                )
                reviews = submission.reviews + [entry.to_dict()]
                status = submission.status
                if entry.verdict == "FAILED":
                    status = "REJECTED"
                elif normalized_stage == STAGE_ORDER[-1] and entry.verdict == "PASSED":
                    actors = {review["actor"] for review in reviews}
                    if all(
                        any(review["stage"] == stage and review["verdict"] == "PASSED"
                            for review in reviews)
                        for stage in STAGE_ORDER
                    ) and len(actors) >= 2:
                        status = "SHADOW_READY"
                updated = Submission(**{**submission.to_dict(), "reviews": reviews, "status": status})
                payload["submissions"][index] = updated.to_dict()
                self._save(payload)
                return updated
        raise ReviewError(f"unknown submission_id: {submission_id}")


__all__ = [
    "ReviewLedger", "ReviewError", "Submission", "ReviewVerdict",
    "STAGE_ORDER", "STAGE_ACTORS", "default_submissions_path",
]
