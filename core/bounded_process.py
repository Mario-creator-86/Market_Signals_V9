"""Run a child process with UTF-8 pipes and a deadline that always holds.

`subprocess.run(text=True, input=...)` is unsafe on Windows for anything that is
not plain ASCII: text mode encodes with the ANSI code page, so one character
outside it kills the stdin writer before stdin is closed and the child waits for
EOF forever; after the timeout `run()` kills only the direct child and then
waits without limit on pipes a surviving grandchild still holds (git.exe sits
behind a launcher, for example). Here the input goes out as UTF-8 bytes, a
timeout kills the whole process tree, and the wait for leftover output is
itself bounded.
"""
from __future__ import annotations

import subprocess
import sys
from typing import Mapping, Optional, Sequence


class BoundedProcessTimeout(RuntimeError):
    """The command did not finish inside its deadline (its tree was killed)."""


def kill_process_tree(process: "subprocess.Popen") -> None:
    """Kill the child and everything it started."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=15,
            )
        except Exception:
            pass
    try:
        process.kill()
    except Exception:
        pass


def run_bounded(
    cmd: Sequence[str], *, input_text: Optional[str] = None, timeout: float = 60.0,
    cwd: Optional[str] = None, env: Optional[Mapping[str, str]] = None,
) -> "subprocess.CompletedProcess[str]":
    """Run `cmd`; stdout/stderr come back decoded as UTF-8 (undecodable bytes replaced).

    Raises BoundedProcessTimeout when the deadline passes.
    """
    data = None if input_text is None else str(input_text).encode("utf-8")
    process = subprocess.Popen(
        list(cmd), cwd=cwd, env=dict(env) if env is not None else None,
        stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        out, err = process.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(process)
        try:
            process.communicate(timeout=10)
        except Exception:
            pass  # a stray pipe holder must not turn the timeout into a hang
        raise BoundedProcessTimeout(f"{cmd[0]} timed out after {float(timeout):.0f}s") from None
    except BaseException:
        kill_process_tree(process)
        raise
    return subprocess.CompletedProcess(
        list(cmd), process.returncode,
        (out or b"").decode("utf-8", "replace"), (err or b"").decode("utf-8", "replace"),
    )


__all__ = ["BoundedProcessTimeout", "kill_process_tree", "run_bounded"]
