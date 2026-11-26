"""Managed MLflow tracking server — part of the system, starts with the app.

The owner made MLflow mandatory: when the app server starts (port 8003) the
tracking server must come up on its configured URI (default 127.0.0.1:5000).
`ensure_mlflow_server` probes the /health endpoint and only spawns a detached
`python -m mlflow server` process when nothing answers — an externally
managed MLflow is never duplicated. `stop_mlflow_server` terminates a process
this module spawned (an external one is left alone).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_URI = "http://127.0.0.1:5001"
_MLFLOW_PROCESS: Optional[subprocess.Popen] = None


def tracking_uri(config: Any = None) -> str:
    """The tracking URI: MLFLOW_TRACKING_URI env, else config, else default."""
    uri = str(os.environ.get("MLFLOW_TRACKING_URI", "")).strip()
    if uri:
        return uri
    root = getattr(config, "config", config) if config is not None else {}
    uri = str(((root or {}).get("ml") or {}).get("mlflow_tracking_uri", "")).strip()
    return uri or DEFAULT_URI


def autostart_enabled(config: Any = None) -> bool:
    root = getattr(config, "config", config) if config is not None else {}
    section = (root or {}).get("ml") or {}
    if not isinstance(section, dict):
        return True
    flag = section.get("mlflow_auto_start")
    return True if flag is None else bool(flag)


def _is_local(uri: str) -> bool:
    host = (urlparse(uri).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def probe(uri: str, timeout: float = 5.0) -> bool:
    """True when an MLflow answers /health at `uri`."""
    try:
        with urllib.request.urlopen(f"{uri.rstrip('/')}/health", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _spawn(uri: str) -> subprocess.Popen:
    parsed = urlparse(uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5001
    from core.config import BASE_DIR, DATA_DIR

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    store = (DATA_DIR / "mlflow.db").resolve().as_posix()
    log_handle = open(DATA_DIR / "mlflow_server.log", "ab")
    kwargs: Dict[str, Any] = {"stdout": log_handle, "stderr": log_handle}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(
        [sys.executable, "-m", "mlflow", "server",
         "--host", host, "--port", str(port),
         "--backend-store-uri", f"sqlite:///{store}"],
        cwd=str(BASE_DIR), **kwargs,
    )


def ensure_mlflow_server(config: Any = None, *, wait_seconds: float = 40.0,
                         spawner=None) -> Dict[str, Any]:
    """Start the tracking server when needed; report the honest outcome."""
    global _MLFLOW_PROCESS
    uri = tracking_uri(config)
    result = {"uri": uri, "started": False, "healthy": False, "detail": ""}
    if probe(uri):
        result.update(healthy=True, detail="already running (external or previous start)")
        return result
    if not autostart_enabled(config):
        result["detail"] = "auto-start disabled in config (ml.mlflow_auto_start)"
        return result
    if not _is_local(uri):
        result["detail"] = "tracking URI is remote — nothing to start locally"
        return result
    try:
        proc = (spawner or _spawn)(uri)
    except Exception as exc:
        result["detail"] = f"spawn failed: {exc}"
        logger.error("MLflow auto-start failed: %s", exc)
        return result
    _MLFLOW_PROCESS = proc
    import time

    deadline = time.time() + max(5.0, wait_seconds)
    while time.time() < deadline:
        if probe(uri):
            result.update(started=True, healthy=True,
                          detail=f"started pid={getattr(proc, 'pid', '?')}")
            logger.info("MLflow tracking server started at %s", uri)
            return result
        if getattr(proc, "poll", lambda: None)() is not None:
            result["detail"] = "process exited during startup (see data/mlflow_server.log)"
            return result
        time.sleep(1.0)
    result["detail"] = f"no /health answer within {wait_seconds:.0f}s after start"
    return result


def stop_mlflow_server() -> bool:
    """Terminate only a process this module spawned."""
    global _MLFLOW_PROCESS
    if _MLFLOW_PROCESS is None:
        return False
    try:
        _MLFLOW_PROCESS.terminate()
        _MLFLOW_PROCESS.wait(timeout=10)
    except Exception:
        logger.exception("MLflow server stop failed")
        return False
    finally:
        _MLFLOW_PROCESS = None
    logger.info("MLflow tracking server stopped")
    return True


__all__ = ["ensure_mlflow_server", "stop_mlflow_server", "probe",
           "tracking_uri", "autostart_enabled"]
