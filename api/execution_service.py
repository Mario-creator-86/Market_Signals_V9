"""Owner control and bounded status service for Phase 11 paper execution."""

from __future__ import annotations

import copy
from typing import Any, Mapping, Optional

from core.autonomy import AutonomyMode
from core.execution.policy import evaluate_paper_entry_readiness
from core.execution.runtime import get_paper_broker


SELECTABLE_PHASE11_MODES = (
    AutonomyMode.OFF.value,
    AutonomyMode.ANALYZE.value,
    AutonomyMode.RECOMMEND.value,
    AutonomyMode.PAPER_AUTO.value,
)

DEFAULT_PRE_EARNINGS_THRESHOLDS = {
    "gap_risk": 65.0,
    "abs_bias": 20.0,
    "coverage_score": 50.0,
    "candidate_score": 78.0,
}

_PRE_EARNINGS_CONFIG_KEYS = {
    "gap_risk": "pre_earnings_min_gap_risk",
    "abs_bias": "pre_earnings_min_abs_bias",
    "coverage_score": "pre_earnings_min_coverage_score",
    "candidate_score": "pre_earnings_min_score",
}


class ExecutionControlError(ValueError):
    pass


def _root(config: Any) -> dict[str, Any]:
    value = getattr(config, "config", {})
    if isinstance(config, Mapping):
        value = config
    return dict(value) if isinstance(value, Mapping) else {}


def _cycle_limit(execution: Mapping[str, Any], key: str, default: int, *, minimum: int = 0) -> int:
    """Expose execution limits without letting malformed config break the control UI."""
    try:
        value = int(execution.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _candidate_root(
    config: Any,
    *,
    mode: Optional[str] = None,
    reduce_fraction_pct: Any = ...,
) -> dict[str, Any]:
    root = copy.deepcopy(_root(config))
    if mode is not None:
        root.setdefault("autonomy", {})["mode"] = mode
    if reduce_fraction_pct is not ...:
        root.setdefault("paper_execution", {})["reduce_fraction_pct"] = reduce_fraction_pct
    return root


def _explained_reconciliation(broker: Any) -> dict[str, Any]:
    """Stored reconciliation plus an owner-readable explanation of each conflict."""
    reconciliation = dict(broker.store.get_reconciliation() or {})
    describe = getattr(broker, "describe_conflicts", None)
    details: list[dict[str, Any]] = []
    if callable(describe):
        try:
            details = list(describe(reconciliation.get("conflicts") or []))
        except Exception:
            details = []
    reconciliation["conflict_details"] = details
    summarize = getattr(broker, "summarize_conflicts", None)
    reconciliation["summary"] = str(summarize(details)) if callable(summarize) and details else ""
    return reconciliation


def get_execution_control(config: Any) -> dict[str, Any]:
    root = _root(config)
    broker = get_paper_broker(config=config)
    reconciliation = _explained_reconciliation(broker)
    current = evaluate_paper_entry_readiness(
        root,
        reconciliation_status=str(reconciliation.get("status") or "UNVERIFIED"),
    )
    selection_root = _candidate_root(config, mode=AutonomyMode.PAPER_AUTO.value)
    selection = evaluate_paper_entry_readiness(
        selection_root,
        reconciliation_status=str(reconciliation.get("status") or "UNVERIFIED"),
    )
    paper = root.get("paper_execution") if isinstance(root.get("paper_execution"), Mapping) else {}
    autonomy = root.get("autonomy") if isinstance(root.get("autonomy"), Mapping) else {}
    agents = root.get("agents") if isinstance(root.get("agents"), Mapping) else {}
    execution = agents.get("execution") if isinstance(agents.get("execution"), Mapping) else {}
    allocation = (
        dict(execution.get("autonomous_allocation"))
        if isinstance(execution.get("autonomous_allocation"), Mapping)
        else {}
    )
    allocation["buckets_pct"] = (
        dict(allocation.get("buckets_pct"))
        if isinstance(allocation.get("buckets_pct"), Mapping)
        else {}
    )
    pre_earnings_thresholds = {
        name: float(execution.get(config_key, DEFAULT_PRE_EARNINGS_THRESHOLDS[name]))
        for name, config_key in _PRE_EARNINGS_CONFIG_KEYS.items()
    }
    return {
        "policy_owner": "RISK_MANAGER",
        "write_endpoint": "/api/agents/risk/constitution",
        "mode": str(autonomy.get("mode") or AutonomyMode.ANALYZE.value).upper(),
        "selectable_modes": list(SELECTABLE_PHASE11_MODES),
        "kill_switch_active": bool(autonomy.get("kill_switch_active", False)),
        "reduce_fraction_pct": paper.get("reduce_fraction_pct"),
        "pre_earnings_thresholds": pre_earnings_thresholds,
        "pre_earnings_policy": {
            "require_canonical_ai_open": bool(
                execution.get("pre_earnings_require_canonical_decision", False)
            ),
        },
        "autonomous_allocation": allocation,
        "autonomous_sources": (
            dict(execution.get("autonomous_sources"))
            if isinstance(execution.get("autonomous_sources"), Mapping)
            else {}
        ),
        "autonomous_cycle_limits": {
            "candidate_reviews_per_cycle": _cycle_limit(
                execution, "max_autonomous_candidates_per_cycle", 0
            ),
            "trade_submissions_per_cycle": _cycle_limit(
                execution, "max_autonomous_trades_per_cycle", 1, minimum=1
            ),
            "scanner_symbols_per_bucket": _cycle_limit(
                execution, "autonomous_scanner_max_symbols_per_bucket", 25, minimum=1
            ),
            "watchlist_symbols": _cycle_limit(
                execution, "autonomous_watchlist_max_symbols", 100, minimum=1
            ),
        },
        "readiness": current.to_dict(),
        "paper_auto_selection": selection.to_dict(),
        "reconciliation": reconciliation,
        "live_execution_available": False,
    }


def set_kill_switch(*, config: Any, active: bool) -> dict[str, Any]:
    if not isinstance(active, bool):
        raise ExecutionControlError("active must be a JSON boolean")
    update_value = getattr(config, "update_value", None)
    if not callable(update_value):
        raise RuntimeError("Atomic config updates are unavailable")
    update_value(["autonomy", "kill_switch_active"], active)
    return {
        "status": "stopped" if active else "released",
        "new_automated_risk_blocked": active,
        "monitoring_active": True,
        "control": get_execution_control(config),
    }


def reconcile_execution(config: Any) -> dict[str, Any]:
    broker = get_paper_broker(config=config)
    result = broker.reconcile()
    return {"status": result["status"], "reconciliation": result}


def rollback_orphaned_fill(config: Any, *, order_intent_id: str, reason: str) -> dict[str, Any]:
    """Owner repair for a FILLED open whose Capital position never became durable.

    Appends an audit event to the order ledger and re-runs reconciliation. It
    never changes Capital cash or positions, and the broker refuses it when the
    Capital position still exists or Capital history shows the fill moved cash.
    """
    identifier = str(order_intent_id or "").strip()
    text = str(reason or "").strip()
    if not identifier:
        raise ExecutionControlError("order_intent_id is required")
    if len(text) < 10:
        raise ExecutionControlError("reason is required (at least 10 characters)")
    broker = get_paper_broker(config=config)
    try:
        result = broker.rollback_orphaned_fill(identifier, reason=text)
    except KeyError as exc:
        raise ExecutionControlError("order intent not found") from exc
    except ValueError as exc:
        raise ExecutionControlError(str(exc)) from exc
    reconciliation = dict(result.get("reconciliation") or {})
    return {
        "status": str(reconciliation.get("status") or "UNVERIFIED"),
        "resolution": result.get("resolution"),
        "reconciliation": reconciliation,
    }


def list_execution_orders(config: Any, *, limit: int = 100) -> dict[str, Any]:
    broker = get_paper_broker(config=config)
    orders = broker.store.list_orders(limit=limit)
    bounded = [
        {
            "order_intent_id": order.get("order_intent_id"),
            "decision_id": order.get("decision_id"),
            "idempotency_key": order.get("idempotency_key"),
            "ticker": order.get("ticker"),
            "action": order.get("action"),
            "state": order.get("state"),
            "position_id": (order.get("result") or {}).get("position_id"),
            "error": order.get("error"),
            "created_at": order.get("created_at"),
            "updated_at": order.get("updated_at"),
        }
        for order in orders
    ]
    return {"orders": bounded, "total": len(bounded), "limit": max(1, min(1000, int(limit or 100)))}


__all__ = [
    "ExecutionControlError",
    "SELECTABLE_PHASE11_MODES",
    "get_execution_control",
    "list_execution_orders",
    "reconcile_execution",
    "rollback_orphaned_fill",
    "set_kill_switch",
]
