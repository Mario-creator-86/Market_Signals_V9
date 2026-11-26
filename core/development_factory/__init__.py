"""Phase 15 — Autonomous Development Factory (§80-§84, §112).

Sandbox-only code changes with independent review. See
docs/superpowers/specs/2026-08-16-v9-phase15-autonomous-development-factory-design.md.
"""
from core.development_factory.pipeline import (
    DevelopmentPipelineError, diff_sha256, submit_change, submission_summary,
)
from core.development_factory.policy import (
    COST_SENSITIVE_MODULES, GOLDEN_TEST_GLOBS, PROTECTED_PATHS, DiffRejected,
    PolicyFinding, enforce_diff, review_diff,
)
from core.development_factory.review import (
    STAGE_ACTORS, STAGE_ORDER, ReviewError, ReviewLedger, Submission,
)
from core.development_factory.sandbox import (
    BRANCH_PREFIX, SandboxError, apply_unified_diff, create_experiment_worktree,
    ensure_inside_worktree, experiment_branch, remove_worktree, worktree_status,
)

__all__ = [
    "PROTECTED_PATHS", "GOLDEN_TEST_GLOBS", "COST_SENSITIVE_MODULES",
    "PolicyFinding", "DiffRejected", "review_diff", "enforce_diff",
    "SandboxError", "create_experiment_worktree", "worktree_status",
    "remove_worktree", "apply_unified_diff", "ensure_inside_worktree",
    "experiment_branch", "BRANCH_PREFIX", "ReviewLedger", "ReviewError",
    "Submission", "STAGE_ORDER", "STAGE_ACTORS", "submit_change",
    "submission_summary", "diff_sha256", "DevelopmentPipelineError",
]
