"""A failed production apply must leave the repository exactly as it was.

Regression for 2026-11-22: the owner approved an improvement whose diff no
longer merged with the live file. `git apply --3way` wrote `<<<<<<<` markers
into `agents/strategy_research_agent.py`, left unmerged index entries and
exited with "Applied patch ... with conflicts"; the watchdog marked the case
FAILED and walked away, so the app's source no longer imported.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

BASE = b"def price():\n    return 1\n\n\ndef size():\n    return 10\n"
REL = "pkg/mod.py"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=30)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    return done.stdout.decode("utf-8", "replace")


def _repo(tmp_path: Path) -> Path:
    repo = Path(tmp_path) / "repo"
    (repo / "pkg").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "pkg" / "__init__.py").write_bytes(b"")
    (repo / REL).write_bytes(BASE)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _reviewed_diff(repo: Path, new_content: bytes) -> str:
    """The diff the review chain stores: `git diff` of the sandbox change."""
    target = repo / REL
    before = target.read_bytes()
    target.write_bytes(new_content)
    diff = _git(repo, "diff", "--", REL)
    target.write_bytes(before)
    assert diff.strip()
    return diff


def _proposed_case(tmp_path: Path, diff_text: str):
    from core.development_factory.watchdog import CaseDraft, RepairCaseStore

    store = RepairCaseStore(tmp_path / "cases.json")
    case = store.enqueue(CaseDraft(
        fingerprint="fp-restore", kind="IMPROVEMENT", source_agent="strategy_research",
        title="Tune the price helper", problem="price() should return 2",
    ))
    store.update(case["case_id"], status="PROPOSED", diff_text=diff_text,
                 _note="improvement reviewed by the chain; awaiting owner approval")
    return store, case["case_id"]


def test_conflicting_approval_leaves_file_and_index_untouched(tmp_path):
    from core.development_factory.watchdog import approve_case

    repo = _repo(tmp_path)
    diff = _reviewed_diff(repo, BASE.replace(b"return 1\n", b"return 2\n"))
    # the file moved on after the proposal was reviewed (an earlier auto-repair
    # patch, applied and staged), so the proposal no longer merges
    live = BASE.replace(b"return 1\n", b"return 3\n")
    (repo / REL).write_bytes(live)
    _git(repo, "add", "--", REL)
    index_before = _git(repo, "ls-files", "-s", "--", REL)
    store, case_id = _proposed_case(tmp_path, diff)

    updated = approve_case(case_id, repo_path=repo, store=store)

    assert updated["status"] == "FAILED"
    assert (repo / REL).read_bytes() == live                      # no conflict markers
    assert _git(repo, "ls-files", "-u") == ""                     # nothing unmerged
    assert _git(repo, "ls-files", "-s", "--", REL) == index_before
    assert "restored" in updated["error"]


def test_failed_post_check_rolls_back_the_index_as_well(tmp_path):
    from core.development_factory.watchdog import approve_case

    repo = _repo(tmp_path)
    diff = _reviewed_diff(repo, BASE.replace(b"return 1\n", b"return (\n"))   # does not compile
    store, case_id = _proposed_case(tmp_path, diff)

    updated = approve_case(case_id, repo_path=repo, store=store)

    assert updated["status"] == "ROLLED_BACK"
    assert (repo / REL).read_bytes() == BASE
    assert _git(repo, "status", "--porcelain") == ""              # nothing left staged


def test_restore_removes_a_file_the_diff_created(tmp_path):
    from core.development_factory.watchdog import (
        _restore_touched_files, _snapshot_touched_files,
    )

    repo = _repo(tmp_path)
    diff = (
        "diff --git a/pkg/extra.py b/pkg/extra.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/pkg/extra.py\n"
        "@@ -0,0 +1 @@\n"
        "+VALUE = 1\n"
    )
    snapshot = _snapshot_touched_files(repo, diff)
    (repo / "pkg" / "extra.py").write_bytes(b"VALUE = 1\n")
    _git(repo, "add", "--", "pkg/extra.py")

    assert _restore_touched_files(repo, snapshot) is True

    assert not (repo / "pkg" / "extra.py").exists()
    assert _git(repo, "status", "--porcelain") == ""
