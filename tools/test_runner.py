"""Cross-platform test entry point for local development and CI.

Usage:
    python tools/test_runner.py smoke --run-e2e 1
    python tools/test_runner.py all --run-e2e 1
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parent.parent
# Include root-level and acceptance tests as part of the backend gate. The
# legacy tests/e2e directory contains manual/debug browser probes; the
# deterministic browser suite lives in tests_e2e and is run separately.
BACKEND_SUITES = ("tests", "--ignore=tests/e2e")
SMOKE_SUITES = ("tests/unit", "tests/integration", "tests/contract", "tests/perf/test_load_smoke.py")
E2E_SUITES = ("tests_e2e",)


def _fresh_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _base_env(*, run_e2e: bool) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("TEST_MODE", "1")
    env.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")
    env.setdefault("MS_DISABLE_WARMUP", "1")
    env.setdefault("MS_DISABLE_STATE_PREFETCH", "1")
    env.setdefault("MS_DISABLE_BG_WORKER", "1")
    env["RUN_E2E"] = "1" if run_e2e else "0"
    if run_e2e and not env.get("E2E_BASE_URL") and not env.get("E2E_PORT"):
        env["E2E_PORT"] = str(_fresh_loopback_port())
    return env


def _pytest(args: Sequence[str], *, run_e2e: bool) -> int:
    command = [sys.executable, "-m", "pytest", "-q", *args]
    result = subprocess.run(command, cwd=ROOT, env=_base_env(run_e2e=run_e2e), check=False)
    return int(result.returncode)


def _run_e2e(*, run_e2e: bool) -> int:
    if not run_e2e:
        print("E2E skipped; pass --run-e2e 1 after installing Playwright Chromium.")
        return 0
    return _pytest(E2E_SUITES, run_e2e=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Market Signals quality suites cross-platform.")
    parser.add_argument("suite", choices=("backend", "contract", "frontend", "smoke", "all"))
    parser.add_argument("--run-e2e", choices=("0", "1"), default=os.getenv("RUN_E2E", "0"))
    args = parser.parse_args()
    run_e2e = args.run_e2e == "1"

    if args.suite == "backend":
        return _pytest(BACKEND_SUITES, run_e2e=run_e2e)
    if args.suite == "contract":
        return _pytest(("tests/contract",), run_e2e=run_e2e)
    if args.suite == "frontend":
        return _run_e2e(run_e2e=run_e2e)
    if args.suite == "smoke":
        code = _pytest(("-m", "smoke and not e2e", *SMOKE_SUITES), run_e2e=run_e2e)
        return code or _run_e2e(run_e2e=run_e2e)

    code = _pytest(BACKEND_SUITES, run_e2e=run_e2e)
    return code or _run_e2e(run_e2e=run_e2e)


if __name__ == "__main__":
    raise SystemExit(main())
