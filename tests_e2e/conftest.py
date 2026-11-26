import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

# Allocate a fresh loopback port by default. A fixed 8010 test server could be
# reused across separate pytest/release-check sessions, preserving rate-limit
# buckets and mutable PAPER state between supposedly isolated runs.
def _fresh_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


E2E_PORT = int(os.getenv("E2E_PORT") or _fresh_loopback_port())
os.environ.setdefault("E2E_PORT", str(E2E_PORT))
BASE_URL = os.getenv("E2E_BASE_URL", f"http://127.0.0.1:{E2E_PORT}")
ROOT = Path(__file__).resolve().parents[1]
SERVER_SCRIPT = ROOT / "run_server_test.py"
RUN_E2E = str(os.getenv("RUN_E2E", "0")).strip().lower() in {"1", "true", "yes", "on"}


def pytest_collection_modifyitems(config, items):
    if RUN_E2E:
        return
    skip_marker = pytest.mark.skip(reason="E2E disabled (set RUN_E2E=1 to enable browser tests).")
    for item in items:
        path = str(item.fspath).replace("\\", "/")
        if "/tests_e2e/" in path or item.get_closest_marker("e2e") is not None:
            item.add_marker(skip_marker)


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


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture(scope="session", autouse=True)
def run_server():
    # Capital scenarios mutate their backing store; only an explicitly test-mode
    # server may be reused.
    try:
        existing = requests.get(f"{BASE_URL}/docs", timeout=2)
        if existing.status_code == 200:
            health = requests.get(f"{BASE_URL}/api/health/diagnostics", timeout=2)
            if health.status_code == 200 and bool(health.json().get("test_mode")):
                print(f"Explicit isolated E2E server already running at {BASE_URL} - reusing")
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
    for _ in range(30):
        exit_code = process.poll()
        if exit_code is not None:
            pytest.fail(f"Isolated E2E server exited during startup (exit={exit_code})")
        try:
            response = requests.get(f"{BASE_URL}/docs", timeout=0.75)
            if response.status_code == 200:
                break
        except requests.exceptions.RequestException:
            pass
        time.sleep(0.5)
    else:
        process.terminate()
        process.communicate()
        pytest.fail(f"E2E server failed to start at {BASE_URL}")

    yield process

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
