"""Phase 14 API service — Research Factory read models + bounded cycle run.

Research-only: nothing here can place orders, change risk, or promote a
strategy (promotion gates stay in Phase 8 policy / Phase 16 mechanics).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from core.research_factory.drift import detect_drift, revalidation_recommendations
from core.research_factory.experiments import (
    ExperimentError, ExperimentRegistry, ExperimentStatus,
)
from core.research_factory.hypothesis import HypothesisStore
from core.research_factory.pipeline import run_research_cycle
from core.research_factory.weakness import (
    detect_weaknesses, load_closed_trades,
)

logger = logging.getLogger(__name__)


class ResearchFactoryError(ValueError):
    """Bounded, caller-facing error (422-class)."""


def _stores() -> tuple[HypothesisStore, ExperimentRegistry]:
    return HypothesisStore(), ExperimentRegistry(hypothesis_store=HypothesisStore())


def get_weaknesses() -> Dict[str, Any]:
    trades = load_closed_trades()
    observations = detect_weaknesses(trades)
    return {
        "observations": [o.to_dict() for o in observations],
        "weaknesses": [o.to_dict() for o in observations if o.verdict == "WEAKNESS"],
        "trades_loaded": len(trades),
    }


def get_hypotheses(limit: int = 50) -> Dict[str, Any]:
    store = HypothesisStore()
    records = store.list_records(limit=limit)
    return {
        "hypotheses": [record.to_dict() for record in records],
        "count": len(records),
    }


def get_experiments(limit: int = 50, status: Optional[str] = None) -> Dict[str, Any]:
    registry = ExperimentRegistry(hypothesis_store=HypothesisStore())
    records = registry.list_records(limit=limit, status=status)
    return {
        "experiments": [
            {
                "experiment_id": r.experiment_id, "hypothesis_id": r.hypothesis_id,
                "strategy_id": r.strategy_id, "version": r.version, "status": r.status,
                "description": r.description, "created_at": r.created_at,
                "updated_at": r.updated_at, "events": len(r.events),
            }
            for r in records
        ],
        "status_counts": registry.status_counts(),
        "count": len(records),
    }


def get_drift() -> Dict[str, Any]:
    trades = load_closed_trades()
    reports = detect_drift(trades)
    return {
        "reports": [report.to_dict() for report in reports],
        "revalidations": revalidation_recommendations(reports),
        "note": "drift triggers REVALIDATION only — never automatic risk change (§79)",
    }




def run_cycle(max_experiments: int = 3) -> Dict[str, Any]:
    """One bounded §111 research cycle (research-only; no orders/risk/promote)."""
    bounded = max(0, min(5, int(max_experiments or 3)))
    hypothesis_store, registry = _stores()
    return run_research_cycle(
        hypothesis_store=hypothesis_store, experiment_registry=registry,
        max_experiments=bounded,
    )


# ------------------------------------------------------------- auto-cycle (§111)

def _scheduler():
    from core.research_factory.scheduler import get_research_scheduler

    return get_research_scheduler(_runtime_config())


def _runtime_config() -> Any:
    try:
        from api.state_service import get_shared_config

        return get_shared_config()
    except Exception:
        return None


def auto_cycle_status() -> Dict[str, Any]:
    status = _scheduler().status()
    status["note"] = (
        "research-only: no orders, no risk changes, no promotion (§79/§86/§111)"
    )
    # Karpathy autoresearch adoption: KEEP/DISCARD ledger summary + current best.
    # KEEP maps to an applied (promoted) experiment; DISCARD to failed/rejected.
    try:
        from core.research_factory.experiments import DEFAULT_EXPERIMENTS_PATH, ExperimentRegistry

        registry = ExperimentRegistry(DEFAULT_EXPERIMENTS_PATH)
        counts = registry.status_counts()
        applied = registry.list_records(status="APPLIED", limit=500)
        latest_applied = applied[-1] if applied else None
        status["autoresearch"] = {
            "keep": int(counts.get("APPLIED", 0)),
            "discard": int(counts.get("FAILED", 0)) + int(counts.get("REJECTED", 0)),
            "open": int(counts.get("PROPOSED", 0)) + int(counts.get("IN_PROGRESS", 0)),
            "current_best": (
                {
                    "experiment_id": latest_applied.experiment_id,
                    "strategy_id": latest_applied.strategy_id,
                    "description": str(latest_applied.description or "")[:160],
                    "updated_at": latest_applied.updated_at,
                }
                if latest_applied is not None else None
            ),
        }
    except Exception:
        status.setdefault("autoresearch", {"keep": 0, "discard": 0, "open": 0, "current_best": None})
    return status


def _research_factory_agent_intent(config: Any) -> Optional[bool]:
    """Return the Agent Fleet toggle when it is explicitly configured.

    ``agents.research_factory.enabled`` is the operator-facing source of truth.
    Older configurations only had ``research_factory.auto_cycle.enabled``;
    returning ``None`` preserves that backwards-compatible fallback.
    """
    root = getattr(config, "config", config) if config is not None else {}
    if not isinstance(root, dict):
        return None
    agents = root.get("agents")
    if not isinstance(agents, dict) or "research_factory" not in agents:
        return None
    value = agents.get("research_factory")
    if isinstance(value, dict):
        return bool(value.get("enabled", False))
    if isinstance(value, bool):
        return value
    return None


def _persist_research_factory_enabled(enabled: bool) -> None:
    """Keep the Agent Fleet toggle and scheduler intent atomically aligned."""
    config = _runtime_config()
    update_values = getattr(config, "update_values", None)
    if callable(update_values):
        update_values([
            (["agents", "research_factory", "enabled"], bool(enabled)),
            (["research_factory", "auto_cycle", "enabled"], bool(enabled)),
        ])
        return
    update_value = getattr(config, "update_value", None)
    if callable(update_value):
        update_value(["agents", "research_factory", "enabled"], bool(enabled))
        update_value(["research_factory", "auto_cycle", "enabled"], bool(enabled))


def auto_cycle_start() -> Dict[str, Any]:
    scheduler = _scheduler()
    started = scheduler.start()
    _persist_research_factory_enabled(True)
    return {"started": started, **scheduler.status()}


def auto_cycle_stop() -> Dict[str, Any]:
    scheduler = _scheduler()
    was_running = scheduler.stop()
    _persist_research_factory_enabled(False)
    return {"stopped": was_running, **scheduler.status()}


def auto_cycle_run_now() -> Dict[str, Any]:
    """Immediate bounded cycle through the scheduler's status bookkeeping."""
    scheduler = _scheduler()
    result = scheduler.run_once_now()
    return {
        "result": {
            "weaknesses_detected": result.get("weaknesses_detected"),
            "experiments": len(result.get("experiments") or []),
            "status_counts": result.get("status_counts") or {},
            "revalidations": len((result.get("drift") or {}).get("revalidations") or []),
        },
        **scheduler.status(),
    }


def auto_cycle_startup() -> Dict[str, Any]:
    """Reconcile persisted Agent Fleet intent with the scheduler at startup."""
    from core.research_factory.scheduler import auto_cycle_config

    config = _runtime_config()
    cfg = auto_cycle_config(config)
    agent_intent = _research_factory_agent_intent(config)
    enabled = cfg["enabled"] if agent_intent is None else agent_intent
    if agent_intent is not None and agent_intent != cfg["enabled"]:
        # Repair legacy split-brain configuration before exposing runtime state.
        _persist_research_factory_enabled(enabled)
    if not enabled:
        return {"started": False, "enabled": False}
    scheduler = _scheduler()
    started = scheduler.start()
    return {"started": bool(started or scheduler.status().get("running")), "enabled": True}


def auto_cycle_shutdown() -> Dict[str, Any]:
    """Lifespan hook: stop the scheduler if a singleton exists (idempotent)."""
    from core.research_factory.scheduler import shutdown_research_scheduler

    return {"stopped": shutdown_research_scheduler()}


__all__ = [
    "ResearchFactoryError", "get_weaknesses", "get_hypotheses",
    "get_experiments", "get_drift", "run_cycle",
    "auto_cycle_status", "auto_cycle_start", "auto_cycle_stop",
    "auto_cycle_run_now", "auto_cycle_startup", "auto_cycle_shutdown",
    "pe_scan_status", "pe_scan_start", "pe_scan_stop", "pe_scan_once",
    "pe_scan_startup", "pe_scan_shutdown",
]


# ------------------------------------------------- continuous Pre-Earnings scan

def _pe_runtime_config() -> Any:
    try:
        from api.state_service import get_shared_config

        return get_shared_config()
    except Exception:
        return None


def pe_scan_status() -> Dict[str, Any]:
    from core.pre_earnings.continuous import get_continuous_scanner

    status = get_continuous_scanner(_pe_runtime_config()).status()
    status["note"] = (
        "1 active 30-day candidate per interval, nearest earnings first: wait for "
        "fresh data + persisted rescore, then run AI; restart-safe progress; no trading capability"
    )
    return status


def pe_scan_start() -> Dict[str, Any]:
    from core.pre_earnings.continuous import get_continuous_scanner

    scanner = get_continuous_scanner(_pe_runtime_config())
    started = scanner.start()
    _persist_pe_scan_enabled(True)
    return {"started": started, **scanner.status()}


def pe_scan_stop() -> Dict[str, Any]:
    from core.pre_earnings.continuous import get_continuous_scanner

    scanner = get_continuous_scanner(_pe_runtime_config())
    was_running = scanner.stop()
    _persist_pe_scan_enabled(False)
    return {"stopped": was_running, **scanner.status()}


def pe_scan_once() -> Dict[str, Any]:
    from core.pre_earnings.continuous import get_continuous_scanner

    scanner = get_continuous_scanner(_pe_runtime_config())
    outcome = scanner.scan_next()
    return {"outcome": outcome, **scanner.status()}


def pe_scan_startup() -> Dict[str, Any]:
    from core.pre_earnings.continuous import continuous_scan_config, get_continuous_scanner

    cfg = continuous_scan_config(_pe_runtime_config())
    if not cfg["enabled"]:
        return {"started": False, "enabled": False}
    get_continuous_scanner(_pe_runtime_config()).start()
    return {"started": True, "enabled": True}


def pe_scan_shutdown() -> Dict[str, Any]:
    from core.pre_earnings.continuous import shutdown_continuous_scanner

    return {"stopped": shutdown_continuous_scanner()}


def _persist_pe_scan_enabled(enabled: bool) -> None:
    config = _pe_runtime_config()
    update_value = getattr(config, "update_value", None)
    if callable(update_value):
        update_value(["pre_earnings", "continuous_scan", "enabled"], enabled)
