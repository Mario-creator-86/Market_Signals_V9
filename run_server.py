import os
import subprocess
import sys

# Ensure root directory is in python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load .env file for API keys and secrets
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass  # python-dotenv not installed; env vars must be set manually


def _maybe_reexec_with_compatible_python():
    """If the default interpreter lacks runtime deps, relaunch with a known-good local Python."""
    if os.environ.get("MS_PYTHON_FALLBACK_ACTIVE") == "1":
        return

    base_dir = os.path.dirname(os.path.abspath(__file__))
    allowed_missing = {"uvicorn", "fastapi", "starlette", "pydantic", "feedparser"}

    try:
        import uvicorn  # noqa: F401
        from api.main import app as _app  # noqa: F401
        return
    except ModuleNotFoundError as exc:
        missing_name = exc.name or ""
        if missing_name not in allowed_missing:
            raise
    except ImportError:
        # Binary/runtime import errors (e.g. stdlib extension DLL mismatch) can
        # be fixed by switching to a healthy interpreter for this project.
        pass

    current = os.path.normcase(sys.executable)
    candidates = [
        os.path.join(base_dir, ".venv314", "Scripts", "python.exe"),
        os.path.join(base_dir, ".venv", "Scripts", "python.exe"),
        os.path.expandvars(r"%LocalAppData%\Programs\Python\Python310\python.exe"),
    ]
    script_path = os.path.abspath(__file__)

    for candidate in candidates:
        if not os.path.exists(candidate):
            continue
        if os.path.normcase(candidate) == current:
            continue

        probe_env = os.environ.copy()
        probe_env["PYTHONPATH"] = base_dir + os.pathsep + probe_env.get("PYTHONPATH", "")
        probe = subprocess.run(
            [candidate, "-c", "import uvicorn; from api.main import app"],  # noqa: F401
            cwd=base_dir,
            env=probe_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if probe.returncode != 0:
            continue

        child_env = os.environ.copy()
        child_env["MS_PYTHON_FALLBACK_ACTIVE"] = "1"
        completed = subprocess.run(
            [candidate, script_path, *sys.argv[1:]],
            env=child_env,
        )
        raise SystemExit(completed.returncode)

    raise SystemExit(
        "Missing runtime dependencies in the active Python. "
        "Install requirements into this interpreter or use Python 3.12."
    )


def _validate_deployment_security(host: str) -> None:
    """Fail closed before exposing the API without an authentication boundary."""
    from core.deployment_security import (
        DeploymentSecurityError,
        validate_deployment_security,
    )

    try:
        result = validate_deployment_security(host)
    except DeploymentSecurityError as exc:
        raise SystemExit(str(exc)) from exc
    for warning in result.warnings:
        print(f"WARNING: {warning}")

if __name__ == "__main__":
    _maybe_reexec_with_compatible_python()

    import uvicorn

    print("Starting Market Signals V9 - Premium Edition...")
    # Development switch: run without the self-repair watchdog, which applies
    # code patches to this working tree while the server is up.
    if "--no-repair-watchdog" in sys.argv[1:]:
        os.environ["MS_DISABLE_REPAIR_WATCHDOG"] = "1"
        print("Self-repair watchdog disabled for this run (--no-repair-watchdog).")
    # Favor stability on Windows: run engine in subprocess and skip heavy warmups by default.
    os.environ.setdefault("MS_ENGINE_MODE", "subprocess")
    os.environ.setdefault("MS_DISABLE_WARMUP", "1")
    os.environ.setdefault("MS_DISABLE_STATE_PREFETCH", "1")
    os.environ.setdefault("MS_RATE_LIMIT_READS", "2400")
    os.environ.setdefault("MS_API_MAX_INFLIGHT_PER_CLIENT", "128")
    os.environ.setdefault("MS_API_MAX_INFLIGHT", "512")
    # Pre-Earnings behavior is owned by config.json (or an explicit operator
    # environment override). Startup must not silently replace owner choices.
    # Force no-reload for stability; reload supervisor has been causing the API to exit.
    from api.main import app
    # Use port from environment or default to 8003
    port = int(os.environ.get("MS_PORT", "8003"))

    # Default to loopback: every mutating endpoint is public when MS_API_KEY
    # is unset, so exposing 0.0.0.0 by default lets anyone on the LAN flip
    # bot.enabled, reset capital or open positions. Set MS_HOST=0.0.0.0
    # explicitly (ideally together with MS_API_KEY) to serve the LAN.
    host = os.environ.get("MS_HOST", "127.0.0.1").strip() or "127.0.0.1"
    _validate_deployment_security(host)

    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )
