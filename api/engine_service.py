# api/engine_service.py - Background engine service for signal generation
# Runs the V5 MarketEngine in background to generate signals

import logging
import threading
import time
import os
import subprocess
from typing import Optional
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from core.state_persistence import load_signals_summary
from core.config import DATA_DIR

logger = logging.getLogger("EngineService")

# Engine mode:
# - "subprocess" (default): run engine in separate process to avoid API thread starvation
# - "thread": run engine in-process (legacy behavior)
_ENGINE_MODE = (os.getenv("MS_ENGINE_MODE", "subprocess") or "subprocess").strip().lower()

# Global engine instance and thread (thread mode)
_engine: Optional["MarketEngine"] = None
_engine_thread: Optional[threading.Thread] = None

# Subprocess handle (subprocess mode)
_engine_process: Optional[subprocess.Popen] = None
_engine_process_out = None
_engine_process_err = None
_ENGINE_PID_PATH = DATA_DIR / "engine.pid"

_engine_running = False
_engine_lock = threading.Lock()


def _read_engine_pid() -> Optional[int]:
    try:
        raw = _ENGINE_PID_PATH.read_text(encoding="utf-8").strip()
        pid = int(raw)
        return pid if pid > 0 else None
    except Exception:
        return None


def _write_engine_pid(pid: int) -> None:
    try:
        _ENGINE_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ENGINE_PID_PATH.write_text(str(int(pid)), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"Failed to write engine pid file: {exc}")


def _remove_engine_pid() -> None:
    try:
        _ENGINE_PID_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def _pid_is_python(pid: int) -> bool:
    if os.name != "nt":
        try:
            return "python" in Path(f"/proc/{int(pid)}/comm").read_text(encoding="utf-8").lower()
        except Exception:
            return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
        return "python" in out.lower()
    except Exception:
        return False


def _terminate_process_tree(pid: Optional[int], timeout_seconds: float = 5.0) -> None:
    if not pid:
        return
    try:
        pid_int = int(pid)
    except Exception:
        return
    if pid_int <= 0 or pid_int == os.getpid():
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid_int), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(1.0, float(timeout_seconds)),
                check=False,
            )
            return
        except Exception:
            pass
    try:
        os.kill(pid_int, 15)
    except Exception:
        pass


def _engine_instance_tag() -> str:
    """Command-line marker of THIS instance's engine (one per data folder).

    Two instances started from the same code folder (the owner's server and a
    test instance with its own ``MS_DATA_DIR``) have identical engine command
    lines. The orphan sweep matched on the code folder alone, so every engine
    start force-killed the other instance's engine and the two watchdogs
    respawned in turn until the hourly restart limit left one of them stopped.
    The engine ignores its arguments; the tag only makes the process findable.
    """
    import hashlib

    try:
        folder = str(Path(DATA_DIR).resolve())
    except Exception:
        folder = str(DATA_DIR)
    digest = hashlib.sha1(os.path.normcase(folder).encode("utf-8")).hexdigest()[:12]
    return f"--ms-instance={digest}"


def _orphan_sweep_script(base_dir: Path, instance_tag: str) -> str:
    """PowerShell that stops engine processes of this code folder AND this instance."""
    base = str(base_dir).replace("'", "''")
    tag = str(instance_tag).replace("'", "''")
    return (
        f"$base = '{base}'; $tag = '{tag}'; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -like \"python*\" -and $_.CommandLine -and $_.CommandLine -like \"*$base*\" "
        "-and $_.CommandLine -like \"*-m core.engine*\" -and $_.CommandLine -like \"*$tag*\" } | "
        "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }"
    )


def _cleanup_orphan_engine_processes(base_dir: Path) -> None:
    """Kill stale core.engine processes from previous API restarts of THIS instance.

    The pid file (in the instance's data folder) covers the normal case; the
    command-line sweep covers a lost pid file and is limited to engines that
    carry this instance's tag, so it can never stop another instance's engine.
    """
    if os.getenv("MS_ENGINE_CLEAN_ORPHANS", "1") == "0":
        return
    try:
        stale_pid = _read_engine_pid()
        if stale_pid:
            # The pid file survives reboots, and Windows reuses pids: only
            # kill it if it is still a Python process.
            if _pid_is_python(stale_pid):
                _terminate_process_tree(stale_pid)
            _remove_engine_pid()
    except Exception as exc:
        logger.warning(f"Engine pid cleanup failed: {exc}")

    if os.name != "nt":
        return
    try:
        script = _orphan_sweep_script(base_dir, _engine_instance_tag())
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        logger.warning(f"Engine orphan process cleanup failed: {exc}")


def _close_engine_streams() -> None:
    """Close subprocess log streams if open."""
    global _engine_process_out, _engine_process_err
    for stream in (_engine_process_out, _engine_process_err):
        if stream is None:
            continue
        try:
            stream.close()
        except Exception:
            pass
    _engine_process_out = None
    _engine_process_err = None


# --- Engine watchdog (subprocess mode) ---
# poll() previously only *reported* a dead engine; nothing respawned it, so a
# crash left the app silently signal-less until a manual restart.
_watchdog_thread: Optional[threading.Thread] = None
_watchdog_started = False
_restart_times: list = []
_WATCHDOG_INTERVAL_SEC = 30.0
_MAX_RESTARTS_PER_HOUR = 5
# Why the engine is not running, for the status payload (never silent).
_engine_last_exit_code: Optional[int] = None
_engine_last_exit_at: Optional[float] = None
_engine_gave_up = False


def _watchdog_loop():
    global _engine_running, _engine_last_exit_code, _engine_last_exit_at, _engine_gave_up
    while True:
        time.sleep(_WATCHDOG_INTERVAL_SEC)
        try:
            with _engine_lock:
                crashed = (
                    _ENGINE_MODE != "thread"
                    and _engine_running
                    and _engine_process is not None
                    and _engine_process.poll() is not None
                )
                exit_code = _engine_process.poll() if crashed else None
            if not crashed:
                continue

            now = time.time()
            _engine_last_exit_code = exit_code
            _engine_last_exit_at = now
            while _restart_times and now - _restart_times[0] > 3600:
                _restart_times.pop(0)
            if len(_restart_times) >= _MAX_RESTARTS_PER_HOUR:
                _engine_gave_up = True
                logger.error(
                    f"Engine subprocess died (exit={exit_code}) but restart limit "
                    f"({_MAX_RESTARTS_PER_HOUR}/h) reached — leaving it stopped."
                )
                with _engine_lock:
                    _engine_running = False
                continue

            logger.warning(f"Engine subprocess died (exit={exit_code}) — respawning")
            _restart_times.append(now)
            start_engine()
        except Exception as exc:
            logger.error(f"Engine watchdog error: {exc}")


def _ensure_watchdog():
    global _watchdog_thread, _watchdog_started
    if _watchdog_started or _ENGINE_MODE == "thread":
        return
    _watchdog_started = True
    _watchdog_thread = threading.Thread(
        target=_watchdog_loop, daemon=True, name="engine-watchdog"
    )
    _watchdog_thread.start()


def get_engine():
    """Get the engine instance, creating it if needed."""
    global _engine
    if _ENGINE_MODE != "thread":
        return None
    if _engine is None:
        try:
            from core.engine import MarketEngine
            _engine = MarketEngine()
            logger.info("MarketEngine initialized")
        except Exception as e:
            logger.error(f"Failed to create MarketEngine: {e}")
            _engine = None
    return _engine


def start_engine():
    """Start the engine in a background thread."""
    global _engine_thread, _engine_running, _engine_process, _engine_process_out, _engine_process_err

    with _engine_lock:
        if _engine_running and (
            (_ENGINE_MODE == "thread" and _engine_thread is not None)
            or (_ENGINE_MODE != "thread" and _engine_process is not None and _engine_process.poll() is None)
        ):
            logger.info("Engine already running")
            return True

        if _ENGINE_MODE != "thread":
            try:
                _close_engine_streams()
                base_dir = Path(__file__).parent.parent
                engine_path = base_dir / "core" / "engine.py"
                if not engine_path.exists():
                    logger.error(f"Engine entry not found: {engine_path}")
                    return False

                # Same folder as every other log (honours MS_LOG_DIR), so a
                # second instance does not write into the first one's logs.
                from core.config import LOG_DIR as logs_dir
                logs_dir.mkdir(parents=True, exist_ok=True)
                _cleanup_orphan_engine_processes(base_dir)
                _engine_process_out = open(logs_dir / "engine_subprocess.out", "a", encoding="utf-8")
                _engine_process_err = open(logs_dir / "engine_subprocess.err", "a", encoding="utf-8")
                # The engine's stderr carries no timestamps; without a start
                # marker a traceback in this file cannot be dated.
                try:
                    _engine_process_err.write(
                        f"\n=== engine subprocess start {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
                    )
                    _engine_process_err.flush()
                except Exception:
                    pass

                creationflags = 0
                startupinfo = None
                if os.name == "nt":
                    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

                env = os.environ.copy()
                env["PYTHONPATH"] = str(base_dir) + os.pathsep + env.get("PYTHONPATH", "")
                env["MS_PROCESS_ROLE"] = "engine"

                _engine_process = subprocess.Popen(
                    [sys.executable, "-m", "core.engine", _engine_instance_tag()],
                    cwd=str(base_dir),
                    env=env,
                    stdout=_engine_process_out,
                    stderr=_engine_process_err,
                    stdin=subprocess.DEVNULL,
                    creationflags=creationflags,
                    startupinfo=startupinfo,
                )
                _engine_running = True
                globals()["_engine_gave_up"] = False
                _write_engine_pid(_engine_process.pid)
                logger.info(f"Engine subprocess started (pid={_engine_process.pid})")
                _ensure_watchdog()
                return True
            except Exception as e:
                logger.error(f"Failed to start engine subprocess: {e}")
                _engine_process = None
                _close_engine_streams()
                return False

        engine = get_engine()
        if engine is None:
            logger.error("Cannot start engine - failed to initialize")
            return False

        _engine_running = True
        _engine_thread = threading.Thread(target=_engine_loop, daemon=True)
        _engine_thread.start()
        logger.info("Engine background thread started")
        return True


def stop_engine():
    """Stop the engine background thread."""
    global _engine_running, _engine_process

    with _engine_lock:
        _engine_running = False
        if _ENGINE_MODE != "thread":
            if _engine_process is not None and _engine_process.poll() is None:
                try:
                    _terminate_process_tree(_engine_process.pid)
                    _engine_process.wait(timeout=5)
                except Exception:
                    try:
                        _engine_process.kill()
                    except Exception:
                        pass
            _engine_process = None
            _remove_engine_pid()
            _close_engine_streams()
        logger.info("Engine stop requested")


def is_engine_running() -> bool:
    """Check if engine is running."""
    if _ENGINE_MODE != "thread":
        return _engine_process is not None and _engine_process.poll() is None
    return _engine_running


def _engine_loop():
    """Main engine loop running in background thread."""
    global _engine_running

    logger.info("Engine loop started")
    try:
        watch_engine = get_engine()
        if watch_engine is not None and hasattr(watch_engine, "start_exit_watch"):
            watch_engine.start_exit_watch()
    except Exception as exc:
        logger.error(f"Exit watch failed to start: {exc}")

    while _engine_running:
        try:
            engine = get_engine()
            if engine:
                engine.run_scan()

            # Get scan interval from config
            scan_interval = 5  # default 5 seconds
            if engine and hasattr(engine, 'config'):
                scan_interval = engine.config.scan.get("scan_interval_seconds", 5)

            # Sleep in small increments to allow quick shutdown
            for _ in range(scan_interval * 2):
                if not _engine_running:
                    break
                time.sleep(0.5)

        except Exception as e:
            error_msg = f"Engine loop error: {e}"
            logger.error(error_msg)
            # Also add to engine's log buffer so it shows in UI
            if engine and hasattr(engine, 'log_buffer'):
                from datetime import datetime
                import pytz
                tz = pytz.timezone("Europe/Bucharest")
                now = datetime.now(tz)
                engine.log_buffer.append(f"[{now.strftime('%H:%M:%S')}] ❌ ERROR: {e}")
            time.sleep(5)  # Wait before retry

    logger.info("Engine loop stopped")


def run_single_scan():
    """Run a single scan (useful for testing)."""
    if _ENGINE_MODE != "thread":
        return False
    engine = get_engine()
    if engine:
        engine.run_scan()
        return True
    return False


def get_engine_status() -> dict:
    """Get current engine status."""
    signals_count = 0
    if _ENGINE_MODE != "thread":
        try:
            data = load_signals_summary({})
            if isinstance(data, dict):
                signals_count = int(data.get("open_count") or len((data.get("open") or {}).keys()))
        except Exception:
            signals_count = 0
        now = time.time()
        return {
            "running": is_engine_running(),
            "initialized": _engine_process is not None,
            "signals_count": signals_count,
            "mode": "subprocess",
            "restarts_last_hour": sum(1 for ts in _restart_times if now - ts <= 3600),
            "restart_limit_per_hour": _MAX_RESTARTS_PER_HOUR,
            "restart_limit_reached": bool(_engine_gave_up),
            "last_exit_code": _engine_last_exit_code,
            "last_exit_seconds_ago": (
                round(now - _engine_last_exit_at, 1) if _engine_last_exit_at else None
            ),
        }

    engine = get_engine()
    if engine and hasattr(engine, "signals"):
        signals_count = len(engine.signals.open_signals)
    return {
        "running": _engine_running,
        "initialized": engine is not None,
        "signals_count": signals_count,
    }
