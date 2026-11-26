"""Fail-closed owner and safety policy for autonomous paper entries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from core.autonomy import AutonomyMode, kill_switch_active, resolve_mode
from core.portfolio.constraints import resolve_confirmed_risk_constitution


def _root(config: Any) -> dict[str, Any]:
    if isinstance(config, Mapping):
        return dict(config)
    value = getattr(config, "config", {})
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class ExecutionReadiness:
    ready: bool
    mode: str
    reasons: tuple[str, ...]
    kill_switch_active: bool
    risk_constitution_active: bool
    reduce_fraction_pct: Optional[float]
    reconciliation_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "mode": self.mode,
            "reasons": list(self.reasons),
            "kill_switch_active": self.kill_switch_active,
            "risk_constitution_active": self.risk_constitution_active,
            "reduce_fraction_pct": self.reduce_fraction_pct,
            "reconciliation_status": self.reconciliation_status,
        }


def evaluate_paper_entry_readiness(
    config: Any,
    *,
    reconciliation_status: str = "UNVERIFIED",
) -> ExecutionReadiness:
    root = _root(config)
    reasons: list[str] = []
    mode = resolve_mode(root)
    stopped = kill_switch_active(root)
    if mode is not AutonomyMode.PAPER_AUTO:
        reasons.append("owner must select PAPER_AUTO")
    if stopped:
        reasons.append("STOP AUTONOMOUS TRADING is active")

    risk_active = False
    try:
        risk_active = resolve_confirmed_risk_constitution(root) is not None
    except Exception as exc:
        reasons.append(f"Risk Constitution is invalid: {exc}")
    if not risk_active and not any(reason.startswith("Risk Constitution is invalid") for reason in reasons):
        reasons.append("owner-confirmed Risk Constitution is required")

    paper = root.get("paper_execution") if isinstance(root.get("paper_execution"), Mapping) else {}
    raw_fraction = paper.get("reduce_fraction_pct")
    reduce_fraction: Optional[float] = None
    if raw_fraction is not None and not isinstance(raw_fraction, bool):
        try:
            reduce_fraction = float(raw_fraction)
        except (TypeError, ValueError):
            reduce_fraction = None
    if reduce_fraction is None or not (0.0 < reduce_fraction < 100.0):
        reasons.append("owner must select a position reduction percentage between 0 and 100")

    execution = ((root.get("agents") or {}).get("execution") or {})
    if not isinstance(execution, Mapping) or execution.get("enabled") is not True:
        reasons.append("Execution Agent is disabled")
    if str(execution.get("mode") or "paper").lower() != "paper":
        reasons.append("Execution Agent must be in paper mode")

    reconciliation = str(reconciliation_status or "UNVERIFIED").upper().strip()
    if reconciliation == "EXECUTION_STATE_CONFLICT":
        reasons.append("EXECUTION_STATE_CONFLICT must be resolved")
    elif reconciliation != "HEALTHY":
        reasons.append("paper broker reconciliation has not passed")

    return ExecutionReadiness(
        ready=not reasons,
        mode=mode.value,
        reasons=tuple(reasons),
        kill_switch_active=stopped,
        risk_constitution_active=risk_active,
        reduce_fraction_pct=reduce_fraction,
        reconciliation_status=reconciliation,
    )


__all__ = ["ExecutionReadiness", "evaluate_paper_entry_readiness"]
