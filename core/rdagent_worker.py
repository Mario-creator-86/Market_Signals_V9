"""RD-Agent worker manager — Part of the V9 ML/Research pipeline.

Microsoft RD-Agent (github.com/microsoft/RD-Agent) is an autonomous research
framework for factor/model discovery. V9 integrates it as an isolated subprocess
running in a dedicated Python 3.10 venv (RD-Agent requires Python <=3.11).

Integration strategy
--------------------
1. The owner installs Python 3.10 and sets RD_AGENT_ENABLED=1 (+ optional
   RD_AGENT_PYTHON_310 pointing to the py3.10 executable).
2. On first call, `ensure_rdagent_worker` creates a dedicated venv at
   runtime/rdagent_venv and installs rdagent inside it.
3. The worker is started as a detached subprocess; its PID and HTTP endpoint
   are written to runtime/rdagent_status.json.
4. `probe_rdagent` makes a short HTTP GET to the RD-Agent health endpoint.
5. The cockpit tile reflects the honest live status: HEALTHY / UNVERIFIED /
   NOT_CONFIGURED.

Safety: the worker only reads data — it never executes trades. All order
paths go through V9's Risk + Capital + PAPER broker as usual.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from core.config import RUNTIME_DIR
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── defaults ──────────────────────────────────────────────────────────────────
DEFAULT_ENDPOINT = "http://127.0.0.1:19000"
# In the configured runtime folder (MS_RUNTIME_DIR), not relative to the working directory.
DEFAULT_VENV_PATH = RUNTIME_DIR / "rdagent_venv"
DEFAULT_WORKSPACE = RUNTIME_DIR / "rdagent_workspace"
STATUS_PATH = RUNTIME_DIR / "rdagent_status.json"

_RDAGENT_PROCESS: Optional[subprocess.Popen] = None
_STATUS_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── config helpers ─────────────────────────────────────────────────────────────

def is_enabled(config: Any = None) -> bool:
    """True when RD_AGENT_ENABLED env or config rdagent.enabled is set."""
    if os.environ.get("RD_AGENT_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    root = getattr(config, "config", config) if config is not None else {}
    section = (root or {}).get("rdagent") or {}
    return bool((section or {}).get("enabled", False))


def _endpoint(config: Any = None) -> str:
    ep = os.environ.get("RD_AGENT_ENDPOINT", "").strip()
    if ep:
        return ep
    root = getattr(config, "config", config) if config is not None else {}
    section = (root or {}).get("rdagent") or {}
    return str((section or {}).get("endpoint", DEFAULT_ENDPOINT)).strip() or DEFAULT_ENDPOINT


def _python310(config: Any = None) -> Optional[str]:
    """Path to the Python 3.10/3.11 executable (env override or auto-detect)."""
    override = os.environ.get("RD_AGENT_PYTHON_310", "").strip()
    if override and os.path.exists(override):
        return override

    venv_dir = _venv_path(config)
    venv_py = venv_dir / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    if venv_py.exists():
        return str(venv_py)

    candidates = [
        os.path.expandvars(r"%LocalAppData%\Programs\Python\Python311\python.exe"),
        os.path.expandvars(r"%LocalAppData%\Programs\Python\Python310\python.exe"),
        "py", "python3.11", "python3.10", "python3",
    ]
    for candidate in candidates:
        if os.path.isabs(candidate) and os.path.exists(candidate):
            return candidate
        found = shutil.which(candidate)
        if not found:
            continue
        if candidate == "py":
            try:
                subprocess.check_call(["py", "-3.11", "-c", "pass"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return "py"
            except Exception:
                pass
        try:
            out = subprocess.check_output(
                [found, "-c", "import sys; print(sys.version_info[:2])"],
                timeout=5, text=True, stderr=subprocess.DEVNULL,
            ).strip()
            if "(3, 10)" in out or "(3, 9)" in out or "(3, 11)" in out:
                return found
        except Exception:
            continue
    return None


def _venv_path(config: Any = None) -> Path:
    override = os.environ.get("RD_AGENT_VENV_PATH", "").strip()
    if override:
        return Path(override)
    root = getattr(config, "config", config) if config is not None else {}
    section = (root or {}).get("rdagent") or {}
    venv_str = (section or {}).get("venv_path", "")
    return Path(venv_str) if venv_str else DEFAULT_VENV_PATH


def _workspace(config: Any = None) -> Path:
    override = os.environ.get("RD_AGENT_WORKSPACE", "").strip()
    if override:
        return Path(override)
    root = getattr(config, "config", config) if config is not None else {}
    section = (root or {}).get("rdagent") or {}
    ws_str = (section or {}).get("workspace", "")
    return Path(ws_str) if ws_str else DEFAULT_WORKSPACE


# ── status persistence ─────────────────────────────────────────────────────────

def _write_status(status: str, detail: str, pid: Optional[int] = None,
                  endpoint: str = "") -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "status": status,
        "detail": detail,
        "pid": pid,
        "endpoint": endpoint,
        "updated_at": _utc_now(),
    }
    try:
        with _STATUS_LOCK:
            STATUS_PATH.write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    except Exception:
        pass


def read_status() -> Dict[str, Any]:
    try:
        with _STATUS_LOCK:
            data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"status": "UNKNOWN", "detail": "no status file", "pid": None,
            "endpoint": "", "updated_at": ""}


# ── live probe ─────────────────────────────────────────────────────────────────

def probe_rdagent(config: Any = None, *, timeout: float = 3.0) -> bool:
    """True when the RD-Agent endpoint answers /health (or root) with HTTP 2xx."""
    if not is_enabled(config):
        return False
    endpoint = _endpoint(config).rstrip("/")
    for path in ("/health", "/api/health", "/api/status", "/"):
        try:
            with urllib.request.urlopen(f"{endpoint}{path}", timeout=timeout) as resp:
                if 200 <= resp.status < 300:
                    return True
        except Exception:
            continue
    return False


# ── venv + install ─────────────────────────────────────────────────────────────

def _ensure_venv(python310: str, venv_dir: Path) -> Optional[str]:
    """Create an rdagent venv if it doesn't exist. Returns venv python path."""
    venv_python = venv_dir / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    if venv_python.exists():
        return str(venv_python)
    logger.info("Creating RD-Agent venv at %s using %s …", venv_dir, python310)
    try:
        cmd = ["py", "-3.11", "-m", "venv", str(venv_dir)] if python310 == "py" else [python310, "-m", "venv", str(venv_dir)]
        subprocess.check_call(cmd, timeout=120)
    except Exception as exc:
        logger.error("RD-Agent venv creation failed: %s", exc)
        return None
    return str(venv_python)


def _install_rdagent(venv_python: str) -> bool:
    """Install rdagent inside the venv. True on success."""
    try:
        logger.info("Installing rdagent into venv (this may take a few minutes) …")
        subprocess.check_call(
            [venv_python, "-m", "pip", "install", "--quiet", "rdagent"],
            timeout=300,
        )
        return True
    except Exception as exc:
        logger.error("rdagent pip install failed: %s", exc)
        return False


def _is_rdagent_installed(venv_python: str) -> bool:
    try:
        result = subprocess.run(
            [venv_python, "-c", "import rdagent; print('ok')"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and "ok" in result.stdout
    except Exception:
        return False


# ── subprocess start ───────────────────────────────────────────────────────────

def _spawn_rdagent(venv_python: str, workspace: Path,
                   endpoint: str) -> subprocess.Popen:
    """Spawn the RD-Agent server subprocess."""
    from core.config import BASE_DIR
    workspace.mkdir(parents=True, exist_ok=True)
    log_path = workspace / "rdagent_server.log"
    log_handle = open(log_path, "ab")

    from urllib.parse import urlparse
    parsed = urlparse(endpoint)
    port = parsed.port or 19000
    host = parsed.hostname or "127.0.0.1"

    kwargs: Dict[str, Any] = {"stdout": log_handle, "stderr": log_handle,
                               "cwd": str(BASE_DIR)}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        )

    runner_script = BASE_DIR / "core" / "rdagent_runner.py"
    return subprocess.Popen(
        [venv_python, str(runner_script),
         "--host", host, "--port", str(port)],
        **kwargs,
    )


# ── public API ─────────────────────────────────────────────────────────────────

def ensure_rdagent_worker(config: Any = None, *,
                          wait_seconds: float = 30.0) -> Dict[str, Any]:
    """Start RD-Agent worker when enabled + not already running.

    Returns a result dict with keys: enabled, started, healthy, detail.
    """
    global _RDAGENT_PROCESS
    result: Dict[str, Any] = {
        "enabled": False, "started": False, "healthy": False, "detail": ""}

    if not is_enabled(config):
        result["detail"] = "RD_AGENT_ENABLED not set — worker not started"
        _write_status("NOT_CONFIGURED", result["detail"])
        return result

    result["enabled"] = True
    endpoint = _endpoint(config)

    # Already healthy?
    if probe_rdagent(config):
        result.update(healthy=True,
                      detail="RD-Agent already running (external or previous start)")
        _write_status("HEALTHY", result["detail"],
                      pid=getattr(_RDAGENT_PROCESS, "pid", None),
                      endpoint=endpoint)
        return result

    # Need Python 3.10
    python310 = _python310(config)
    if not python310:
        result["detail"] = (
            "Python 3.10/3.11 not found on PATH. "
            "Install it and set RD_AGENT_PYTHON_310=/path/to/python3.10"
        )
        _write_status("UNAVAILABLE", result["detail"], endpoint=endpoint)
        logger.warning("RD-Agent: %s", result["detail"])
        return result

    venv_dir = _venv_path(config)
    venv_python = _ensure_venv(python310, venv_dir)
    if not venv_python:
        result["detail"] = f"Failed to create RD-Agent venv at {venv_dir}"
        _write_status("ERROR", result["detail"], endpoint=endpoint)
        return result

    if not _is_rdagent_installed(venv_python):
        ok = _install_rdagent(venv_python)
        if not ok:
            result["detail"] = "rdagent pip install failed (see venv logs)"
            _write_status("ERROR", result["detail"], endpoint=endpoint)
            return result

    workspace = _workspace(config)
    try:
        proc = _spawn_rdagent(venv_python, workspace, endpoint)
    except Exception as exc:
        result["detail"] = f"RD-Agent spawn failed: {exc}"
        _write_status("ERROR", result["detail"], endpoint=endpoint)
        logger.error("RD-Agent worker start failed: %s", exc)
        return result

    _RDAGENT_PROCESS = proc
    deadline = time.time() + max(5.0, wait_seconds)
    while time.time() < deadline:
        if probe_rdagent(config):
            result.update(started=True, healthy=True,
                          detail=f"RD-Agent started pid={proc.pid}")
            _write_status("HEALTHY", result["detail"], pid=proc.pid,
                          endpoint=endpoint)
            logger.info("RD-Agent worker started at %s", endpoint)
            return result
        if getattr(proc, "poll", lambda: None)() is not None:
            result["detail"] = "RD-Agent process exited during startup (see workspace/rdagent_server.log)"
            _write_status("ERROR", result["detail"], endpoint=endpoint)
            return result
        time.sleep(2.0)

    result["detail"] = (
        f"RD-Agent process started (pid={proc.pid}) but /health "
        f"did not answer within {wait_seconds:.0f}s — "
        f"check {workspace}/rdagent_server.log"
    )
    _write_status("UNVERIFIED", result["detail"], pid=proc.pid, endpoint=endpoint)
    return result


def stop_rdagent_worker() -> bool:
    """Terminate only a process this module spawned."""
    global _RDAGENT_PROCESS
    if _RDAGENT_PROCESS is None:
        return False
    try:
        _RDAGENT_PROCESS.terminate()
        _RDAGENT_PROCESS.wait(timeout=10)
    except Exception:
        logger.exception("RD-Agent worker stop failed")
        return False
    finally:
        _RDAGENT_PROCESS = None
    _write_status("STOPPED", "Worker terminated by application shutdown")
    logger.info("RD-Agent worker stopped")
    return True


__all__ = [
    "ensure_rdagent_worker", "stop_rdagent_worker", "probe_rdagent",
    "is_enabled", "read_status",
]
