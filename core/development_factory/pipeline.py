"""Phase 15 §112 — the autonomous-development pipeline (sandbox only).

submit_change: policy-guard the diff (§82/§83/§84) → apply inside an
isolated worktree (§80) → open the staged review ledger (§81). Promotion is
Phase 16; nothing here can merge, deploy, or alter production state.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Optional

from core.development_factory.policy import DiffRejected, enforce_diff
from core.development_factory.review import ReviewLedger, ReviewError, Submission
from core.development_factory.sandbox import (
    SandboxError, apply_unified_diff, create_experiment_worktree,
    experiment_branch, remove_worktree,
)


class DevelopmentPipelineError(ValueError):
    """Bounded pipeline error (policy or sandbox rejection)."""


def diff_sha256(diff_text: str) -> str:
    return hashlib.sha256(str(diff_text or "").encode("utf-8", errors="replace")).hexdigest()


def submit_change(
    *,
    repo_path: Path,
    experiment_id: str,
    author: str,
    diff_text: str,
    ledger: Optional[ReviewLedger] = None,
    base_ref: str = "HEAD",
    worktree_parent: Optional[Path] = None,
    apply: bool = True,
) -> Dict[str, Any]:
    """Guard + sandbox + open reviews. Returns the submission record.

    `apply=False` is the dry-run contract used by tests and by callers that
    manage worktrees themselves; policy enforcement still runs.
    """
    try:
        enforce_diff(diff_text)
    except DiffRejected as exc:
        raise DevelopmentPipelineError(
            "diff rejected: " + "; ".join(f"{f.rule}:{f.path}" for f in exc.findings)
        ) from exc

    worktree_info: Dict[str, Any] = {"applied": False}
    if apply:
        try:
            worktree_info = create_experiment_worktree(
                repo_path, experiment_id, base_ref=base_ref, parent_dir=worktree_parent,
            )
            apply_unified_diff(Path(worktree_info["path"]), diff_text)
        except SandboxError as exc:
            raise DevelopmentPipelineError(f"sandbox failure: {exc}") from exc

    store = ledger or ReviewLedger()
    try:
        submission = store.open_submission(
            experiment_id=experiment_id, author=str(author or "coding_agent"),
            branch=worktree_info.get("branch") or experiment_branch(experiment_id),
            worktree=str(worktree_info.get("path") or ""),
            diff_sha256=diff_sha256(diff_text),
        )
    except ReviewError as exc:
        if apply and worktree_info.get("path"):
            remove_worktree(Path(repo_path), experiment_id)  # rollback the sandbox
        raise DevelopmentPipelineError(str(exc)) from exc
    return {
        "submission": submission.to_dict(),
        "worktree": worktree_info,
        "next": "record CODE_REVIEW → SECURITY_REVIEW → TESTS → QUANT_VALIDATION → E2E (distinct actors)",
    }


def submission_summary(submission: Submission) -> Dict[str, Any]:
    stages = {review["stage"]: review["verdict"] for review in submission.reviews}
    return {
        "submission_id": submission.submission_id,
        "experiment_id": submission.experiment_id,
        "author": submission.author,
        "status": submission.status,
        "stages": stages,
        "actors": sorted({review["actor"] for review in submission.reviews}),
        "created_at": submission.created_at,
    }


__all__ = [
    "submit_change", "submission_summary", "diff_sha256", "DevelopmentPipelineError",
]
