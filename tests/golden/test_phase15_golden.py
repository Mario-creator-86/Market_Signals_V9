"""Phase 15 golden tests — §80 sandbox isolation + §112 journey freeze."""
import subprocess
from pathlib import Path

import pytest

from core.development_factory.pipeline import (
    DevelopmentPipelineError, submit_change, submission_summary,
)
from core.development_factory.review import ReviewLedger
from core.development_factory.sandbox import (
    SandboxError, create_experiment_worktree, ensure_inside_worktree,
    remove_worktree, worktree_status,
)

pytestmark = pytest.mark.golden

CLEAN_DIFF = """--- a/core/new_feature_note.py
+++ b/core/new_feature_note.py
@@ -0,0 +1,1 @@
+NOTE = "phase15 sandbox proof"
"""


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, text=True, timeout=30)
    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "core").mkdir()
    (repo / "core" / "x.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    return repo


def test_gold080_isolated_worktree_journey(tmp_path):
    repo = _init_repo(tmp_path)
    worktree = create_experiment_worktree(repo, "EXP-002183", base_ref="HEAD",
                                          parent_dir=tmp_path)
    # §80: worktree lives OUTSIDE the production tree, on its own branch.
    assert Path(worktree["path"]) != repo
    assert worktree["branch"] == "ai/experiment/EXP-002183"
    assert worktree_status(repo, "EXP-002183")["exists"] is True
    # Writes must stay inside the sandbox (prefix-checked).
    inside = ensure_inside_worktree(Path(worktree["path"]), Path("core/new.py"))
    assert str(worktree["path"]) in str(inside)
    with pytest.raises(SandboxError):
        ensure_inside_worktree(Path(worktree["path"]), Path("../outside.py"))
    assert remove_worktree(repo, "EXP-002183") is True
    assert worktree_status(repo, "EXP-002183")["exists"] is False


def test_gold112_sandboxed_pipeline_with_independent_review(tmp_path):
    repo = _init_repo(tmp_path)
    ledger = ReviewLedger(tmp_path / "subs.json")
    result = submit_change(
        repo_path=repo, experiment_id="EXP-002184", author="coding_agent",
        diff_text=CLEAN_DIFF, ledger=ledger, worktree_parent=tmp_path,
    )
    submission_id = result["submission"]["submission_id"]
    assert result["submission"]["status"] == "OPEN"
    # The change exists only inside the worktree; production file unchanged.
    assert not (repo / "core" / "new_feature_note.py").exists()
    assert (Path(result["worktree"]["path"]) / "core" / "new_feature_note.py").exists()

    # §81: author self-review is refused; independent chain to SHADOW_READY.
    from core.development_factory.review import ReviewError

    with pytest.raises(ReviewError):
        ledger.record_review(submission_id, stage="CODE_REVIEW",
                             actor="coding_agent", verdict="PASSED")
    stages = [
        ("CODE_REVIEW", "code_review_agent"),
        ("SECURITY_REVIEW", "security_review_agent"),
        ("TESTS", "test_agent"),
        ("QUANT_VALIDATION", "quant_validator"),
        ("E2E", "e2e_agent"),
    ]
    for stage, actor in stages:
        ledger.record_review(submission_id, stage=stage, actor=actor,
                             verdict="PASSED", evidence={"run": "sandbox"})
    final = ledger.get(submission_id)
    assert final.status == "SHADOW_READY"
    summary = submission_summary(final)
    assert summary["actors"] == sorted(actor for _, actor in stages)
    assert summary["stages"][stage] == "PASSED"  # last stage recorded
    # Journey ends at SHADOW_READY — never promotion (Phase 16 owns that).
    remove_worktree(repo, "EXP-002184")


def test_gold112_protected_diff_never_enters_sandbox(tmp_path):
    repo = _init_repo(tmp_path)
    protected = """--- a/docs/V9_RISK_CONSTITUTION.md
+++ b/docs/V9_RISK_CONSTITUTION.md
@@ -1,1 +1,1 @@
-max risk per trade 2%
+max risk per trade 20%
"""
    ledger = ReviewLedger(tmp_path / "subs.json")
    with pytest.raises(DevelopmentPipelineError) as excinfo:
        submit_change(repo_path=repo, experiment_id="EXP-002185",
                      author="coding_agent", diff_text=protected,
                      ledger=ledger, worktree_parent=tmp_path)
    assert "PROTECTED_FILE" in str(excinfo.value)
    # No worktree was created for the rejected diff.
    assert worktree_status(repo, "EXP-002185")["exists"] is False
    assert ledger.list_submissions() == []
