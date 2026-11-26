"""Phase 14+ — automatic research-cycle scheduler (§111 continuous loop).

Runs `run_research_cycle` periodically in a daemon thread so the weakness →
hypothesis → experiment → SHADOW/REJECT journey happens without manual
intervention. Owner-gated by config (default OFF in the example config; the
owner's runtime config enables it). Research-only by construction: the cycle
places no orders, changes no risk, and promotes nothing (§79/§86).

Fail-open: a cycle failure is recorded in status and logged; the scheduler
keeps running. Interval is clamped to a sane floor so a misconfiguration
cannot hammer the journals.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 6 * 3600  # research cadence, not a trading loop
MIN_INTERVAL_SECONDS = 15 * 60       # clamp floor
STARTUP_DELAY_SECONDS = 60           # let the app settle before the first cycle


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResearchCycleScheduler:
    """Daemon thread executing the bounded §111 research cycle on an interval."""

    def __init__(self, *, interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
                 max_experiments: int = 3, startup_delay: int = STARTUP_DELAY_SECONDS):
        self.interval_seconds = max(MIN_INTERVAL_SECONDS, int(interval_seconds or DEFAULT_INTERVAL_SECONDS))
        self.max_experiments = max(0, min(5, int(max_experiments or 3)))
        self.startup_delay = max(0, int(startup_delay))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._status_lock = threading.Lock()
        self._status: Dict[str, Any] = {
            "running": False,
            "enabled": False,
            "interval_seconds": self.interval_seconds,
            "max_experiments": self.max_experiments,
            "cycles_completed": 0,
            "last_run": None,
            "last_result": None,
            "last_error": "",
        }

    # ------------------------------------------------------------------ control

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="research-cycle-scheduler", daemon=True,
        )
        self._thread.start()
        with self._status_lock:
            self._status["running"] = True
            self._status["enabled"] = True
        logger.info("Research-cycle scheduler started (interval=%ss, max_experiments=%s)",
                    self.interval_seconds, self.max_experiments)
        return True

    def stop(self) -> bool:
        was = self._status.get("running")
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        with self._status_lock:
            self._status["running"] = False
            self._status["enabled"] = False
        return bool(was)

    def run_once_now(self) -> Dict[str, Any]:
        """Owner-triggered immediate cycle (same bounded pipeline)."""
        return self._run_cycle()

    def status(self) -> Dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    # -------------------------------------------------------------------- loop

    def _loop(self) -> None:
        self._stop.wait(self.startup_delay)
        while not self._stop.is_set():
            try:
                self._run_cycle()
            except Exception:  # noqa: BLE001 — the scheduler must survive cycle failures
                logger.exception("research cycle crashed (scheduler continues)")
            if self._stop.wait(self.interval_seconds):
                break

    def _run_cycle(self) -> Dict[str, Any]:
        from core.research_factory.experiments import ExperimentRegistry
        from core.research_factory.hypothesis import HypothesisStore
        from core.research_factory.pipeline import run_research_cycle

        started = _utc_now_iso()
        try:
            result = run_research_cycle(
                hypothesis_store=HypothesisStore(),
                experiment_registry=ExperimentRegistry(hypothesis_store=HypothesisStore()),
                max_experiments=self.max_experiments,
            )
            # Evidence-based registry advancement (Phase 8 §62): strategies with
            # accumulated walk-forward evidence progress RESEARCH->CHALLENGER
            # and CHALLENGER->SHADOW. CHAMPION stays behind the §63/§86 gates.
            try:
                from core.research_factory.lifecycle_advance import advance_registry_lifecycles

                result["lifecycle_advancements"] = advance_registry_lifecycles()
            except Exception:
                logger.exception("lifecycle advancement step failed (cycle continues)")
            with self._status_lock:
                self._status["cycles_completed"] += 1
                self._status["last_run"] = started
                self._status["last_result"] = {
                    "weaknesses_detected": result.get("weaknesses_detected"),
                    "experiments": len(result.get("experiments") or []),
                    "status_counts": result.get("status_counts") or {},
                    "revalidations": len((result.get("drift") or {}).get("revalidations") or []),
                    "lifecycle_advancements": len(result.get("lifecycle_advancements") or []),
                }
                self._status["last_error"] = ""
            logger.info("Research cycle completed: %s", self._status["last_result"])
            return result
        except Exception as exc:  # noqa: BLE001
            with self._status_lock:
                self._status["last_run"] = started
                self._status["last_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.exception("Research cycle failed")
            raise


_SCHEDULER: Optional[ResearchCycleScheduler] = None
_SCHEDULER_LOCK = threading.Lock()


def auto_cycle_config(config: Any) -> Dict[str, Any]:
    """Read `research_factory.auto_cycle` from config (tolerant)."""
    root = getattr(config, "config", config) if config is not None else {}
    section = ((root or {}).get("research_factory") or {}).get("auto_cycle") if isinstance(root, dict) else None
    if not isinstance(section, dict):
        section = {}
    return {
        "enabled": bool(section.get("enabled", False)),
        "interval_seconds": int(section.get("interval_seconds", DEFAULT_INTERVAL_SECONDS) or DEFAULT_INTERVAL_SECONDS),
        "max_experiments": int(section.get("max_experiments", 3) or 3),
        "startup_delay_seconds": int(section.get("startup_delay_seconds", STARTUP_DELAY_SECONDS) or 0),
    }


def get_research_scheduler(config: Any = None) -> ResearchCycleScheduler:
    """Process-wide scheduler; config seeds interval/bounds on first build."""
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is None:
            cfg = auto_cycle_config(config)
            _SCHEDULER = ResearchCycleScheduler(
                interval_seconds=cfg["interval_seconds"],
                max_experiments=cfg["max_experiments"],
                startup_delay=cfg["startup_delay_seconds"],
            )
        return _SCHEDULER


def reset_research_scheduler_singleton() -> None:
    """Test seam — stops a running scheduler and drops the singleton."""
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is not None and _SCHEDULER.status().get("running"):
            _SCHEDULER.stop()
        _SCHEDULER = None


def shutdown_research_scheduler() -> bool:
    """App-shutdown hook — stop and drop the singleton if one exists."""
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is None:
            return False
        if _SCHEDULER.status().get("running"):
            _SCHEDULER.stop()
        _SCHEDULER = None
        return True


__all__ = [
    "ResearchCycleScheduler", "get_research_scheduler", "auto_cycle_config",
    "reset_research_scheduler_singleton", "shutdown_research_scheduler",
    "DEFAULT_INTERVAL_SECONDS", "MIN_INTERVAL_SECONDS",
]
