"""Repair sandbox: git pipes are UTF-8 and every child process has a hard deadline.

Regression for the 2026-11-22 stall: the watchdog thread sat for hours inside
`apply_unified_diff`. The diff touched a CSS file whose header holds box-drawing
characters; `subprocess.run(text=True, input=...)` encoded it with the Windows
ANSI code page, the stdin writer died before closing stdin, git waited for EOF
forever, and after the 60 s timeout `run()` waited without limit on the pipes
the surviving git.exe still held.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from core.development_factory.sandbox import SandboxError, apply_unified_diff

ORIGINAL = (
    "/* ═══ Premarket — layout: chips → strip ═══ */\n"
    ".pm-table { width: 100%; }\n"
)
PATCHED = (
    "/* ═══ Premarket — layout: chips → strip ═══ */\n"
    ".pm-table { width: 100%; }\n"
    ".pm-table thead { overflow: hidden; } /* clip → card */\n"
)
DIFF = (
    "diff --git a/frontend/pm.css b/frontend/pm.css\n"
    "--- a/frontend/pm.css\n"
    "+++ b/frontend/pm.css\n"
    "@@ -1,2 +1,3 @@\n"
    " /* ═══ Premarket — layout: chips → strip ═══ */\n"
    " .pm-table { width: 100%; }\n"
    "+.pm-table thead { overflow: hidden; } /* clip → card */\n"
)


def _init_repo(tmp_path: Path) -> Path:
    repo = Path(tmp_path) / "repo"
    (repo / "frontend").mkdir(parents=True)

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, timeout=30)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "core.autocrlf", "false")
    (repo / "frontend" / "pm.css").write_bytes(ORIGINAL.encode("utf-8"))
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    return repo


def _within(seconds: float, fn, *args):
    """Run fn in a thread; fail the test instead of hanging the suite."""
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001 - reported to the test below
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), f"{getattr(fn, '__name__', fn)} still running after {seconds}s"
    if "error" in box:
        raise box["error"]
    return box.get("value")


def test_apply_unified_diff_with_non_ascii_context_applies(tmp_path):
    repo = _init_repo(tmp_path)

    result = _within(25, apply_unified_diff, repo, DIFF)

    assert result["applied"] is True
    assert (repo / "frontend" / "pm.css").read_bytes() == PATCHED.encode("utf-8")


def test_reverse_apply_with_non_ascii_context_restores_the_file(tmp_path):
    from core.development_factory.watchdog import _reverse_apply

    repo = _init_repo(tmp_path)
    (repo / "frontend" / "pm.css").write_bytes(PATCHED.encode("utf-8"))

    assert _within(25, _reverse_apply, repo, DIFF) is True
    assert (repo / "frontend" / "pm.css").read_bytes() == ORIGINAL.encode("utf-8")


def test_run_bounded_returns_utf8_output(tmp_path):
    from core.development_factory.sandbox import run_bounded

    script = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
    done = _within(25, lambda: run_bounded([sys.executable, "-c", script],
                                           input_text="a → b ═", timeout=20))

    assert done.returncode == 0
    assert done.stdout == "a → b ═"


def test_run_bounded_deadline_holds_when_a_grandchild_keeps_the_pipes(tmp_path):
    """The child exits on kill but its own child inherited stdout/stderr."""
    from core.development_factory.sandbox import run_bounded

    marker = tmp_path / "grandchild.pid"
    grandchild = (
        "import os, time, pathlib; "
        f"pathlib.Path(r'{marker}').write_text(str(os.getpid())); time.sleep(120)"
    )
    child = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); time.sleep(120)"
    )
    started = time.monotonic()

    with pytest.raises(SandboxError, match="timed out"):
        _within(40, lambda: run_bounded([sys.executable, "-c", child], timeout=3))

    assert time.monotonic() - started < 30
    pid = int(marker.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.2)
    assert not _pid_alive(pid), "the grandchild survived the timeout"


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        listed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                capture_output=True, timeout=20)
        return str(pid).encode() in listed.stdout
    try:
        import os
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# --- cases interrupted by a restart -------------------------------------------

def _case_in_progress(tmp_path):
    from core.development_factory.watchdog import CaseDraft, RepairCaseStore

    store = RepairCaseStore(tmp_path / "cases.json")
    case = store.enqueue(CaseDraft(
        fingerprint="fp-interrupted", kind="RUNTIME", source_agent="ui_inspector",
        title="Text escapes its container in premarket", problem="thead overflows its card",
    ))
    store.update(case["case_id"], status="IN_PROGRESS", _note="repair chain started")
    return store, case["case_id"]


def test_case_left_in_progress_by_a_restart_is_queued_again(tmp_path):
    from core.development_factory.watchdog import (
        ACTIVITY_IDLE, ACTIVITY_WORKING, activity_status, record_activity,
        requeue_interrupted_cases,
    )

    store, case_id = _case_in_progress(tmp_path)
    activity = tmp_path / "activity.json"
    record_activity(ACTIVITY_WORKING, case_id=case_id, stage="coding",
                    detail="attempt 1: generating a diff", path=activity)

    requeued = requeue_interrupted_cases(store, activity_path=activity)

    assert requeued == [case_id]
    case = store.get(case_id)
    assert case["status"] == "DETECTED"
    assert "restart" in case["history"][-1]["note"]
    assert activity_status(activity)["state"] == ACTIVITY_IDLE


def test_case_interrupted_repeatedly_is_closed_as_failed(tmp_path):
    from core.development_factory.watchdog import requeue_interrupted_cases

    store, case_id = _case_in_progress(tmp_path)
    activity = tmp_path / "activity.json"
    for _ in range(2):
        assert requeue_interrupted_cases(store, activity_path=activity) == [case_id]
        store.update(case_id, status="IN_PROGRESS", _note="repair chain started")

    assert requeue_interrupted_cases(store, activity_path=activity) == []
    case = store.get(case_id)
    assert case["status"] == "FAILED"
    assert "interrupted" in case["error"]
