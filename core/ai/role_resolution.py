"""Runtime resolution of the owner-selected model for one AI role."""
from __future__ import annotations

import json
import threading
from typing import Any, Dict, Optional

from core.ai.assignment import ModelAssignment
from core.ai.router import ModelRouter

# A Claude subscription model answers through the Claude Code CLI: a few seconds for a short prompt,
# more for a long one. The call sites were budgeted for a hosted Ollama model (6-12 s).
SUBSCRIPTION_MIN_TIMEOUT_SECONDS = 60

_FILE_CACHE: Dict[str, Any] = {"mtime_ns": None, "data": {}}
_FILE_CACHE_LOCK = threading.Lock()


def resolve_role_model(
    config: Any,
    role: str,
    *,
    tier_hint: Optional[str] = None,
) -> Optional[str]:
    root = getattr(config, "config", config)
    if not isinstance(root, dict):
        return None
    assignment = ModelAssignment.from_config(root)
    return ModelRouter.from_config(root, assignment).select_model(role, tier_hint=tier_hint)


def _config_from_file() -> Dict[str, Any]:
    """The app's config file, read once per revision (for callers that hold no config object)."""
    try:
        from core.config import CONFIG_PATH

        mtime_ns = CONFIG_PATH.stat().st_mtime_ns
        with _FILE_CACHE_LOCK:
            if _FILE_CACHE["mtime_ns"] != mtime_ns:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                _FILE_CACHE.update({"mtime_ns": mtime_ns, "data": data if isinstance(data, dict) else {}})
            return _FILE_CACHE["data"]
    except (OSError, ValueError):
        return {}


def assigned_model(role: str, *, config: Any = None, fallback: Optional[str] = None,
                   tier_hint: Optional[str] = "analysis") -> Optional[str]:
    """The model the owner assigned to ``role``; ``fallback`` (the global model) when the role has none.

    An AI call site that takes the global model directly ignores the owner's
    per-role choice: the Pre-Earnings view kept answering with the global
    DeepSeek model after its analyst role had been set to Claude Opus.
    """
    try:
        root = getattr(config, "config", config) if config is not None else _config_from_file()
        if not isinstance(root, dict):
            return fallback
        # Only what the owner chose for this role (or for its tier). The "all" default is left to the
        # caller's fallback, which is the global model as the client resolves it (it knows which
        # models are installed and which one the owner picked in the global selector).
        chosen = ModelAssignment.from_config(root).to_dict().get(role)
        if not chosen and tier_hint:
            chosen = (((root.get("ai") or {}).get("router") or {}).get(tier_hint) or {}).get("model")
    except Exception:  # noqa: BLE001 - a broken assignment must not take the AI call down
        chosen = None
    return str(chosen).strip() if chosen and str(chosen).strip() else fallback


def call_timeout_for(model: Any, seconds: Any, *, floor: int = SUBSCRIPTION_MIN_TIMEOUT_SECONDS) -> int:
    """Time budget of one call to ``model``.

    A Claude subscription model gets at least ``floor`` seconds: cutting it
    off after a budget meant for a hosted Ollama model would throw the answer
    away and fall back to the global model.
    """
    try:
        budget = int(seconds)
    except (TypeError, ValueError):
        budget = int(floor)
    try:
        from core.ai.subscription_routing import subscription_provider_name_for

        if subscription_provider_name_for(model):
            return max(budget, int(floor))
    except Exception:  # noqa: BLE001
        pass
    return budget


__all__ = ["SUBSCRIPTION_MIN_TIMEOUT_SECONDS", "assigned_model", "call_timeout_for", "resolve_role_model"]
