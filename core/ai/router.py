"""ModelRouter (spec §38) — triage / analysis / critic tiers.

`select_model(role, tier_hint)` returns the model for a role given a tier hint:
the tier's configured model wins (fast/cheap → triage, strong → analysis,
independent → critic); otherwise it falls back to the ModelAssignment for the
role. This is how V9 avoids spending expensive AI calls on thousands of
low-quality candidates (§38): triage runs on the cheap tier, only survivors
escalate to analysis/critic.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from core.ai.assignment import ModelAssignment

TIERS = ("triage", "analysis", "critic")


class ModelRouter:
    def __init__(self, router: Optional[Dict[str, Any]], assignment: ModelAssignment) -> None:
        self._router: Dict[str, Dict[str, Any]] = {t: {"model": None} for t in TIERS}
        if isinstance(router, dict):
            for t in TIERS:
                if isinstance(router.get(t), dict):
                    self._router[t] = {"model": router[t].get("model")}
        self._assignment = assignment

    def select_model(self, role: str, tier_hint: Optional[str] = None) -> Optional[str]:
        # Explicit per-role assignment chosen by owner takes highest priority
        explicit = self._assignment._assignment.get(role) if hasattr(self._assignment, "_assignment") else None
        if explicit:
            return explicit
        if tier_hint in self._router:
            tier_model = self._router[tier_hint].get("model")
            if tier_model:
                return tier_model
        return self._assignment.resolve(role)

    def to_dict(self) -> Dict[str, Any]:
        return {t: dict(self._router[t]) for t in TIERS}

    @classmethod
    def from_config(cls, config: Dict[str, Any], assignment: ModelAssignment) -> "ModelRouter":
        ai = (config or {}).get("ai") or {}
        return cls(ai.get("router") or {}, assignment)