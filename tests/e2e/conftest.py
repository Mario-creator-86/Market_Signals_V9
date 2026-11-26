import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

def _fresh_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


E2E_PORT = int(os.getenv("E2E_PORT") or _fresh_loopback_port())
os.environ.setdefault("E2E_PORT", str(E2E_PORT))
BASE_URL = os.getenv("E2E_BASE_URL", f"http://127.0.0.1:{E2E_PORT}")
ROOT = Path(__file__).resolve().parents[2]
SERVER_SCRIPT = ROOT / "run_server_test.py"

_E2E_ENABLED = str(os.getenv("RUN_E2E", "0")).strip().lower() in {"1", "true", "yes", "on"}


def pytest_collection_modifyitems(config, items):
    """Skip e2e tests (browser/subprocess-backed) unless RUN_E2E is enabled.

    Scope the skip to items that are ACTUALLY e2e tests — those located under an
    e2e directory (tests/e2e, tests_e2e) or carrying the `e2e` marker. Without
    this guard, a bare `pytest` run hits whatever happens to listen on
    localhost:8000 (or times out waiting for it).

    NOTE: the previous version added the skip marker to EVERY item in the session.
    Because conftest hooks are registered session-wide, that nuked the ENTIRE
    suite (unit, integration, contract, golden, root) whenever tests/e2e was on
    the collection path (e.g. `pytest tests`). That forced the Makefile to pass an
    explicit subdir list excluding tests/e2e — which silently excluded ~52
    root-level tests/test_*.py (they sit at tests/ root, outside the listed
    subdirs), so `make test-all` never ran the most important tests (capital
    engine, strategy_v2 selector, scanners, position sizing). Scoping the skip
    restores them.
    """
    if _E2E_ENABLED:
        return
    skip = pytest.mark.skip(reason="e2e disabled (set RUN_E2E=1 to enable)")
    for item in items:
        path = str(item.fspath).replace("\\", "/")
        is_e2e = (
            "/e2e/" in path
            or "/tests_e2e/" in path
            or item.get_closest_marker("e2e") is not None
        )
        if is_e2e:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


def _windows_playwright_pipe_ready() -> tuple[bool, str]:
    if os.name != "nt":
        return True, ""
    try:
        import _winapi
        import asyncio.windows_utils as windows_utils

        h1, h2 = windows_utils.pipe(overlapped=(False, True), duplex=True)
        _winapi.CloseHandle(h1)
        _winapi.CloseHandle(h2)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def pytest_runtest_setup(item):
    if "page" not in getattr(item, "fixturenames", ()):
        return
    ready, reason = _windows_playwright_pipe_ready()
    if not ready:
        pytest.skip(
            "Playwright unavailable in this environment: "
            f"{reason}. Grant AV/EDR permission for duplex named pipes."
        )


@pytest.fixture(scope="session", autouse=True)
def run_server():
    if str(os.getenv("RUN_E2E", "0")).strip().lower() not in {"1", "true", "yes", "on"}:
        yield None
        return

    try:
        existing = requests.get(f"{BASE_URL}/docs", timeout=2)
        if existing.status_code == 200:
            health = requests.get(f"{BASE_URL}/api/health/diagnostics", timeout=2)
            if health.status_code == 200 and bool(health.json().get("test_mode")):
                yield None
                return
            pytest.fail(f"Refusing to run mutating E2E tests against non-test server {BASE_URL}")
    except requests.exceptions.RequestException:
        pass

    if not SERVER_SCRIPT.is_file():
        pytest.fail(f"Missing isolated E2E server entrypoint: {SERVER_SCRIPT}")

    process = subprocess.Popen(
        [sys.executable, str(SERVER_SCRIPT)],
        cwd=str(ROOT),
        env={**os.environ, "E2E_PORT": str(E2E_PORT)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    ready = False
    for _ in range(30):
        exit_code = process.poll()
        if exit_code is not None:
            pytest.fail(f"Isolated E2E server exited during startup (exit={exit_code})")
        try:
            resp = requests.get(f"{BASE_URL}/docs", timeout=0.75)
            if resp.status_code == 200:
                ready = True
                break
        except requests.exceptions.RequestException:
            pass
        time.sleep(0.5)

    if not ready:
        process.terminate()
        process.communicate()
        pytest.fail(f"E2E server failed to start at {BASE_URL}")

    yield process

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
