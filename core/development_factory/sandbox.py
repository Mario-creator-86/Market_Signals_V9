"""Phase 15 §80 — isolated git worktrees for autonomous code changes.

Never direct production modification: every experiment gets its own
`ai/experiment/EXP-*` branch inside a git worktree rooted OUTSIDE the
production tree. All helper writes are prefix-checked against the worktree
path so a patch cannot escape the sandbox.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional, Sequence

from core.bounded_process import BoundedProcessTimeout, run_bounded as _run_bounded

BRANCH_PREFIX = "ai/experiment/"


class SandboxError(RuntimeError):
    """Worktree creation/verification failure (fail-closed)."""


def run_bounded(
    cmd: Sequence[str], *, input_text: Optional[str] = None, timeout: float = 60.0,
    cwd: Optional[str] = None,
) -> "subprocess.CompletedProcess[str]":
    """`core.bounded_process.run_bounded` for the sandbox: UTF-8 pipes and a
    deadline that always holds; a timeout is a SandboxError (fail-closed)."""
    try:
        return _run_bounded(cmd, input_text=input_text, timeout=timeout, cwd=cwd)
    except BoundedProcessTimeout as exc:
        raise SandboxError(str(exc)) from None


def _git(repo: Path, *args: str) -> str:
    completed = run_bounded(["git", "-C", str(repo), *args], timeout=60)
    if completed.returncode != 0:
        raise SandboxError(f"git {' '.join(args[:2])} failed: {completed.stderr.strip()[:500]}")
    return completed.stdout.strip()


def experiment_branch(experiment_id: str) -> str:
    identifier = str(experiment_id or "").strip()
    if not identifier or ".." in identifier or "/" in identifier:
        raise SandboxError("invalid experiment id for branch name")
    return f"{BRANCH_PREFIX}{identifier}"


def create_experiment_worktree(
    repo_path: Path, experiment_id: str, *, base_ref: str = "HEAD",
    parent_dir: Optional[Path] = None,
) -> dict:
    """Create `<parent>/wt-<experiment>` on branch `ai/experiment/<id>`."""
    repo = Path(repo_path).resolve()
    if not (repo / ".git").exists():
        raise SandboxError(f"not a git repository: {repo}")
    branch = experiment_branch(experiment_id)
    target = (Path(parent_dir) if parent_dir else repo.parent) / f"wt-{experiment_id}"
    if target.exists():
        # An abandoned prior attempt with this exact id (crash mid-chain, or a
        # post-apply cleanup that lost a Windows file lock). The id is ours by
        # naming scheme, so recover instead of burning the attempt: unregister
        # the worktree if git still knows it, drop any leftover directory.
        try:
            _git(repo, "worktree", "remove", "--force", str(target))
        except SandboxError:
            pass
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if target.exists():
            raise SandboxError(f"worktree path already exists: {target}")
    try:
        _git(repo, "worktree", "add", "-b", branch, str(target), base_ref)
    except SandboxError:
        # A crashed earlier run can leave its experiment branch behind after the
        # worktree was removed; the branch is ephemeral by design — drop it and
        # retry once instead of burning an attempt on the collision.
        _git(repo, "branch", "-D", branch)
        _git(repo, "worktree", "add", "-b", branch, str(target), base_ref)
    # Mirror uncommitted tracked changes into the sandbox so the repo the LLM
    # saw excerpts from (the live working tree) is the repo the diff applies
    # to. Without this, a locally-modified target file makes every anchored
    # diff fail "patch does not apply" against the HEAD-based worktree.
    base_tree = None
    try:
        dirty = _git(repo, "diff", "--name-only", "HEAD")
        synced = False
        for rel in [line.strip().replace("\\", "/") for line in dirty.splitlines() if line.strip()]:
            src = repo / rel
            dst = target / rel
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                synced = True
        if synced:
            # Stage the mirrored files: `git apply --3way` implies `--index`,
            # which refuses to touch a file whose worktree content differs
            # from the index ("does not match index"). The staged snapshot
            # also becomes the diff baseline so the owner's uncommitted hunks
            # are NOT included in the reviewed diff later.
            _git(target, "add", "-A", ".")
            base_tree = _git(target, "write-tree").strip() or None
    except SandboxError:
        pass  # dirty-sync is best-effort; the HEAD base remains valid
    return {
        "experiment_id": experiment_id,
        "branch": branch,
        "path": str(target),
        "base_tree": base_tree,
        "base_ref": base_ref,
        "isolation": "worktree outside the production tree (§80)",
    }


def worktree_status(repo_path: Path, experiment_id: str) -> dict:
    repo = Path(repo_path).resolve()
    branch = experiment_branch(experiment_id)
    listing = _git(repo, "worktree", "list", "--porcelain")
    current_path = ""
    for line in listing.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree "):].strip()
        if line.startswith("branch ") and line[len("branch "):].strip().endswith("/" + branch):
            return {"experiment_id": experiment_id, "branch": branch,
                    "path": current_path, "exists": True}
    return {"experiment_id": experiment_id, "branch": branch, "exists": False}


def remove_worktree(repo_path: Path, experiment_id: str) -> bool:
    repo = Path(repo_path).resolve()
    status = worktree_status(repo, experiment_id)
    if not status.get("exists"):
        return False
    _git(repo, "worktree", "remove", "--force", status["path"])
    try:
        _git(repo, "branch", "-D", experiment_branch(experiment_id))
    except SandboxError:
        pass  # branch may be checked out elsewhere; worktree removal is the contract
    return True


def ensure_inside_worktree(worktree_path: Path, candidate: Path) -> Path:
    """Prefix-check: candidate file writes must stay inside the worktree."""
    root = Path(worktree_path).resolve()
    resolved = (root / candidate).resolve() if not candidate.is_absolute() else Path(candidate).resolve()
    if root != resolved and root not in resolved.parents:
        raise SandboxError(f"§80 violation: write outside worktree rejected: {resolved}")
    return resolved


def apply_unified_diff(worktree_path: Path, diff_text: str, *, three_way: bool = False) -> dict:
    """Apply a policy-cleaned unified diff inside the sandbox worktree.

    `--recount` recomputes hunk line counts from the actual content — LLM-authored
    diffs frequently miscount `@@` headers, which is a format error, not intent.
    `--ignore-whitespace` tolerates indentation drift in the LLM's copied
    context lines (cloud models reliably match content but not exact spaces);
    the semantic lines still have to match for git to place the hunk.
    `three_way=True` (production apply) resolves against index blobs, so hunks
    that are already present in the working tree merge as no-ops instead of
    failing "patch does not apply". Because `--3way` implies `--index`, it
    refuses files with unstaged owner edits ("does not match index"); in that
    case we retry plain apply — safe here since the diff's preimages were
    generated from the live (dirty) content, so plain context matching is
    exact.
    """
    worktree = Path(worktree_path).resolve()
    if not (worktree / ".git").exists():
        raise SandboxError(f"not a worktree: {worktree}")

    def _run(check: bool, use_3way: bool, context: int):
        return run_bounded(
            ["git", "-C", str(worktree), "apply"] + (["--check"] if check else []) +
            ["--recount", f"-C{context}", "--ignore-whitespace"] +
            (["--3way"] if use_3way else []) + ["--whitespace=nowarn", "-"],
            input_text=str(diff_text or ""), timeout=60,
        )

    # -C1 first; LLM diffs occasionally paraphrase CONTEXT lines while the
    # changed lines stay verbatim — -C0 drops the context requirement and
    # applies those. Context loosening only ever widens what applies, never
    # misplaces a hunk git would have anchored itself.
    def _first_pass(use_3way: bool):
        for context in (1, 0):
            completed = _run(check=True, use_3way=use_3way, context=context)
            if completed.returncode == 0:
                return context, completed
        return None, completed

    use_3way = three_way
    context_used, completed = 1, None
    if three_way:
        context_used, completed = _first_pass(True)
        if context_used is None:
            err = (completed.stderr or "").strip() if completed is not None else ""
            if "does not match index" in err:
                context_used, completed = _first_pass(False)
                if context_used is not None:
                    use_3way = False
    else:
        context_used, completed = _first_pass(False)
    if context_used is None:
        err = (completed.stderr or "").strip() if completed is not None else "unknown"
        raise SandboxError(f"diff does not apply cleanly: {err[:500]}")

    applied = _run(check=False, use_3way=use_3way, context=context_used)
    if applied.returncode != 0:
        raise SandboxError(f"diff application failed: {applied.stderr.strip()[:500]}")
    return {"applied": True, "worktree": str(worktree),
            "mode": ("3way" if use_3way else "plain") + f"/C{context_used}"}


__all__ = [
    "SandboxError", "create_experiment_worktree", "worktree_status",
    "remove_worktree", "apply_unified_diff", "ensure_inside_worktree",
    "experiment_branch", "BRANCH_PREFIX", "run_bounded",
]
