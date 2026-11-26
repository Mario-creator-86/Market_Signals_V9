"""ModelAssignment (spec §37) — map each of the 10 AI roles to a model id.

The `all` alias, when set, applies to every role that has no explicit model.
Selection is explicit (§108 "select role models → save"); no model names are
hardcoded (§35) — model ids come from config / ModelRegistry discovery.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# The 10 §37 roles (snake_case config keys).
ROLES = (
    "market_analyst",
    "pre_earnings_analyst",
    "news_analyst",
    "quant_analyst",
    "portfolio_manager",
    "risk_critic",
    "research_scientist",
    "coding_agent",
    "code_reviewer",
    "security_reviewer",
)


class ModelAssignment:
    def __init__(self, assignment: Optional[Dict[str, Any]] = None) -> None:
        self._assignment: Dict[str, Any] = {role: None for role in ROLES}
        self._assignment["all"] = None
        if assignment:
            for k, v in assignment.items():
                if k in self._assignment:
                    self._assignment[k] = v

    def resolve(self, role: str) -> Optional[str]:
        """Return the model for `role`: explicit role model, else the `all` alias, else None.

        The `all` alias applies only to the 10 known §37 roles, not to arbitrary
        unknown role strings (which resolve to None).
        """
        if role not in ROLES:
            return None
        explicit = self._assignment.get(role)
        if explicit:
            return explicit
        return self._assignment.get("all")

    def set(self, role: str, model_id: Optional[str]) -> None:
        if role in self._assignment:
            self._assignment[role] = model_id

    def set_all(self, model_id: Optional[str]) -> None:
        self._assignment["all"] = model_id

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._assignment)

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "ModelAssignment":
        ai = (config or {}).get("ai") or {}
        assignment = dict(ai.get("assignment") or {})
        # Preserve compatibility with the existing owner-facing Ollama
        # selector: before §37 existed, its persisted ``ollama.model`` was the
        # sole application-wide selection. Treat it as the ``all`` fallback so
        # every role/router immediately follows the same global choice.
        if not assignment.get("all"):
            ollama = (config or {}).get("ollama") or {}
            fallback = ollama.get("model") or ollama.get("cloud_model")
            if fallback:
                assignment["all"] = fallback
        return cls(assignment)

    def validate(self, model_id: str, registry: Any) -> bool:
        """True iff `model_id` is registered in `registry` (§36 ModelRegistry)."""
        if model_id is None:
            return False
        entry = registry.get(model_id) if registry is not None else None
        return entry is not None
