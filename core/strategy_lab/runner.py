"""Strategy Lab runner: the live app starts one lab run a day, as a child process.

The search is CPU work and would compete with the trading loops inside the
app's interpreter, so it runs as ``python -m core.strategy_lab.run`` and the app
only watches it. The run is due once per UTC day, after ``run_hour_utc`` (the
daily crypto bar and the previous US session are complete then), or at once
when the lab has never run.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from core.strategy_lab.lab import lab_config, load_registry

logger = logging.getLogger(__name__)

DAY_MS = 86_400_000
RETRY_AFTER_FAILURE_MS = 60 * 60_000
RUN_TIMEOUT_SECONDS = 90 * 60

Spawn = Callable[[], Dict[str, Any]]


def _spawn_process() -> Dict[str, Any]:
    from core.bounded_process import BoundedProcessTimeout, run_bounded
    from core.config import BASE_DIR

    try:
        done = run_bounded([sys.executable, "-m", "core.strategy_lab.run"], timeout=RUN_TIMEOUT_SECONDS, cwd=str(BASE_DIR))
    except BoundedProcessTimeout as exc:
        return {"ok": False, "detail": str(exc)}
    except OSError as exc:
        return {"ok": False, "detail": f"{exc.__class__.__name__}: {exc}"}
    tail = "\n".join((done.stdout or "").strip().splitlines()[-4:])
    if done.returncode == 3:
        return {"ok": True, "detail": "another lab run was already in progress"}
    if done.returncode == 2:
        return {"ok": False, "detail": tail}                 # ran, but the data did not arrive whole: retry later
    if done.returncode != 0:
        return {"ok": False, "detail": f"exit code {done.returncode}: {(done.stderr or '').strip()[-400:]}"}
    return {"ok": True, "detail": tail}


def run_is_due(registry: Dict[str, Any], cfg: Dict[str, Any], now_ms: int) -> bool:
    last = registry.get("last_run_day")
    if last is None:
        return True
    if int(last) >= now_ms // DAY_MS:
        return False
    minutes = (now_ms % DAY_MS) // 60_000
    return minutes >= int(cfg.get("run_hour_utc", 0)) * 60 + int(cfg.get("run_minute_utc", 15))


class StrategyLabRunner:
    def __init__(self, config_getter: Callable[[], Any], *, spawn: Optional[Spawn] = None,
                 now_ms: Optional[Callable[[], int]] = None, poll_seconds: float = 300.0) -> None:
        self._config_getter = config_getter
        self._spawn = spawn or _spawn_process
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_started_ms: Optional[int] = None
        self._last_finished_ms: Optional[int] = None
        self._last_result: Dict[str, Any] = {}

    def _config(self) -> Dict[str, Any]:
        try:
            raw = self._config_getter()
            return lab_config(getattr(raw, "config", raw))
        except Exception:  # noqa: BLE001
            return lab_config(None)

    def _run(self, reason: str) -> Dict[str, Any]:
        with self._lock:
            if self._running:
                return {"started": False, "reason": "a lab run is already in progress"}
            self._running = True
            self._last_started_ms = self._now_ms()
        logger.info("Strategy lab: run started (%s)", reason)
        try:
            result = self._spawn()
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "detail": f"{exc.__class__.__name__}: {exc}"}
        with self._lock:
            self._running = False
            self._last_finished_ms = self._now_ms()
            self._last_result = {**result, "reason": reason}
        logger.info("Strategy lab: run finished (%s): %s", "ok" if result.get("ok") else "failed", result.get("detail"))
        return {"started": True, **result}

    def tick(self) -> Optional[Dict[str, Any]]:
        """Start a run when one is due. Returns the outcome, or None when nothing was due."""
        cfg = self._config()
        if not bool(cfg.get("enabled", True)):
            return None
        now = self._now_ms()
        failed_recently = (
            self._last_finished_ms is not None and not self._last_result.get("ok")
            and now - self._last_finished_ms < RETRY_AFTER_FAILURE_MS
        )
        if failed_recently or not run_is_due(load_registry(), cfg, now):
            return None
        return self._run("scheduled")

    def run_now(self) -> Dict[str, Any]:
        """Start a run in the background on the owner's request."""
        with self._lock:
            if self._running:
                return {"started": False, "reason": "a lab run is already in progress"}
        threading.Thread(target=self._run, args=("requested",), name="strategy-lab-run", daemon=True).start()
        return {"started": True}

    def _loop(self) -> None:
        # Give the app a few minutes to finish starting before the first run.
        if self._stop.wait(180.0):
            return
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the scheduler must survive anything
                logger.exception("Strategy lab: scheduler tick failed")
            self._stop.wait(self._poll_seconds)

    def start(self) -> bool:
        if os.environ.get("MS_ENV") == "test":
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="strategy-lab-scheduler", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> Dict[str, Any]:
        def iso(ms: Optional[int]) -> Optional[str]:
            return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms else None

        cfg = self._config()
        with self._lock:
            return {
                "enabled": bool(cfg.get("enabled", True)),
                "scheduler_running": bool(self._thread is not None and self._thread.is_alive()),
                "run_in_progress": self._running,
                "last_started_at": iso(self._last_started_ms),
                "last_finished_at": iso(self._last_finished_ms),
                "last_result": dict(self._last_result),
                "daily_run_after_utc": f"{int(cfg.get('run_hour_utc', 0)):02d}:{int(cfg.get('run_minute_utc', 15)):02d}",
            }


_RUNNER: Optional[StrategyLabRunner] = None
_RUNNER_LOCK = threading.Lock()


def get_lab_runner(config_getter: Optional[Callable[[], Any]] = None) -> StrategyLabRunner:
    global _RUNNER
    with _RUNNER_LOCK:
        if _RUNNER is None:
            if config_getter is None:
                from api.state_service import get_shared_config as config_getter  # type: ignore[no-redef]
            _RUNNER = StrategyLabRunner(config_getter)
        return _RUNNER


def lab_startup(config_getter: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
    runner = get_lab_runner(config_getter)
    return {"started": runner.start(), **runner.status()}
