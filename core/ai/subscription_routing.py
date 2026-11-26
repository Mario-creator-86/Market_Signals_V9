"""Route Claude model ids to the owner's Claude subscription (Claude Code CLI).

Nearly every AI call in the app goes through the legacy Ollama client
(`core.ollama_client`), with the model taken from the global selection or a
role assignment. This module is the one seam that lets a Claude model id be
used anywhere such a model name is accepted: `subscription_provider_for(model)`
says whether the model belongs to an enabled subscription provider, and
`generate_via_subscription(...)` runs the call and returns the same dict shape
the Ollama client returns.

The subscription is a shared, limited resource (the same plan the owner uses
interactively), so two guards live here: a per-day call limit and, in the
provider, a cap on concurrent CLI processes. When the subscription cannot
answer, the caller falls back to the provider's `fallback_model` on Ollama.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.ai.claude_code_provider import PROVIDER_TYPE, ClaudeCodeProvider

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CONFIG_CACHE: Dict[str, Any] = {"mtime_ns": None, "providers": {}}
_INSTANCES: Dict[str, Dict[str, Any]] = {}      # name -> {"fingerprint", "provider"}
_CALLS: Dict[str, Dict[str, Any]] = {}          # name -> {"day", "count"}
_CALLS_FILE: Optional[Path] = None              # where the counts outlive the process (None = memory only)
_runner_override: Optional[Callable[..., Any]] = None   # tests inject a fake CLI


def reset_for_tests() -> None:
    global _CALLS_FILE
    with _LOCK:
        _CONFIG_CACHE.update(mtime_ns=None, providers={})
        _INSTANCES.clear()
        _CALLS.clear()
        _CALLS_FILE = None


def _providers_config() -> Dict[str, Any]:
    """`ai.providers` from the runtime config, re-read when the config file changes."""
    try:
        from core.config import CONFIG_PATH, ConfigManager

        try:
            mtime_ns = CONFIG_PATH.stat().st_mtime_ns
        except OSError:
            mtime_ns = -1
        with _LOCK:
            if _CONFIG_CACHE["mtime_ns"] == mtime_ns:
                return _CONFIG_CACHE["providers"]
        data = (ConfigManager().config.get("ai") or {}).get("providers") or {}
        data = deepcopy(data) if isinstance(data, dict) else {}
        with _LOCK:
            _CONFIG_CACHE.update(mtime_ns=mtime_ns, providers=data)
        return data
    except Exception:
        return {}


def _entries() -> Dict[str, Dict[str, Any]]:
    return {
        name: entry for name, entry in (_providers_config() or {}).items()
        if isinstance(entry, dict) and entry.get("type") == PROVIDER_TYPE and entry.get("enabled")
    }


def _provider(name: str, entry: Dict[str, Any]) -> ClaudeCodeProvider:
    fingerprint = repr(sorted((k, repr(v)) for k, v in entry.items()))
    with _LOCK:
        cached = _INSTANCES.get(name)
        if cached and cached["fingerprint"] == fingerprint and cached.get("runner") is _runner_override:
            return cached["provider"]
        provider = ClaudeCodeProvider(
            name=name, executable=entry.get("executable", ""), default_model=entry.get("default_model", ""),
            models=entry.get("models") or None, effort=entry.get("effort", ""),
            timeout=int(entry.get("timeout", 300) or 300),
            max_concurrent=int(entry.get("max_concurrent", 2) or 2),
            runner=_runner_override,
        )
        _INSTANCES[name] = {"fingerprint": fingerprint, "provider": provider, "runner": _runner_override}
        return provider


def _served(provider: ClaudeCodeProvider, entry: Dict[str, Any], model: str) -> bool:
    if not provider.serves_model(model):
        return False
    listed = [str(m).strip() for m in (entry.get("models") or []) if str(m).strip()]
    return not listed or model in listed or model == str(entry.get("default_model") or "")


def subscription_provider_for(model: Any) -> Optional[ClaudeCodeProvider]:
    """The enabled subscription provider that serves `model`, or None."""
    name = _provider_name_for(model)
    return _provider(name, _entries()[name]) if name else None


def subscription_provider_name_for(model: Any) -> str:
    """Config name of the enabled subscription provider that serves `model` ("" = none)."""
    return _provider_name_for(model)


def _provider_name_for(model: Any) -> str:
    value = str(model or "").strip()
    if not value:
        return ""
    for name, entry in _entries().items():
        provider = _provider(name, entry)
        if _served(provider, entry, value) and provider.health_check():
            return name
    return ""


def subscription_models() -> List[str]:
    """Model ids the settings page may offer (enabled providers with the CLI present)."""
    out: List[str] = []
    for name, entry in _entries().items():
        provider = _provider(name, entry)
        if not provider.health_check():
            continue
        for model in provider.list_models():
            if model not in out:
                out.append(model)
    return out


def fallback_model_for(model: Any) -> str:
    """Ollama model to use when the subscription cannot serve `model` ("" = none)."""
    name = _provider_name_for(model)
    if not name:
        return ""
    configured = str((_entries().get(name) or {}).get("fallback_model") or "").strip()
    if configured:
        return configured
    # No explicit fallback (a role was pointed at a Claude model while the
    # global model stayed on Ollama): the global Ollama model is the fallback.
    global_model = _global_ollama_model()
    return global_model if global_model and not _provider_name_for(global_model) else ""


def _global_ollama_model() -> str:
    try:
        from core.ollama_client import _get_ollama_config

        cfg = _get_ollama_config()
        return str(cfg.get("model") or cfg.get("cloud_model") or "").strip()
    except Exception:
        return ""


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def remember_calls_in(path: Any) -> None:
    """Keep the daily call counts in ``path`` and take over what it holds for today.

    Without it the count lives in memory only, and a restart handed out a
    fresh daily allowance: on 2026-11-24 the app stood at 2860 of 3000 calls,
    was restarted, and read 0 of 3000. Calls already made by this process
    before the count was restored are added to it.
    """
    global _CALLS_FILE
    store = Path(path) if path else None
    with _LOCK:
        if store == _CALLS_FILE:
            return
        _CALLS_FILE = store
        if store is None:
            return
        try:
            saved = json.loads(store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        for name, row in (saved.items() if isinstance(saved, dict) else []):
            try:
                day, count = str(row.get("day") or ""), max(0, int(row.get("count") or 0))
            except (AttributeError, TypeError, ValueError):
                continue
            if day != _today():
                continue
            mine = _CALLS.get(str(name)) or {}
            already = int(mine.get("count") or 0) if mine.get("day") == day else 0
            _CALLS[str(name)] = {"day": day, "count": count + already}


def _save_calls_locked() -> None:
    """Write the counts next to the config; a failed write costs only the memory across a restart."""
    if _CALLS_FILE is None:
        return
    try:
        _CALLS_FILE.parent.mkdir(parents=True, exist_ok=True)
        scratch = _CALLS_FILE.with_name(_CALLS_FILE.name + ".tmp")
        scratch.write_text(json.dumps(_CALLS), encoding="utf-8")
        os.replace(scratch, _CALLS_FILE)
    except OSError as exc:
        logger.debug("Subscription call count not saved: %s", exc)


def _take_daily_slot(name: str, limit: int) -> bool:
    with _LOCK:
        row = _CALLS.setdefault(name, {"day": _today(), "count": 0})
        if row["day"] != _today():
            row.update(day=_today(), count=0)
        if limit > 0 and row["count"] >= limit:
            return False
        row["count"] += 1
        _save_calls_locked()
        return True


def subscription_usage() -> Dict[str, Dict[str, Any]]:
    """Calls made today per subscription provider, with the configured limit."""
    out: Dict[str, Dict[str, Any]] = {}
    for name, entry in _entries().items():
        with _LOCK:
            row = _CALLS.get(name) or {"day": _today(), "count": 0}
            count = row["count"] if row["day"] == _today() else 0
        out[name] = {"calls_today": count, "daily_call_limit": int(entry.get("daily_call_limit", 0) or 0),
                     "fallback_model": str(entry.get("fallback_model") or "")}
    return out


def generate_via_subscription(
    prompt: str, *, model: str, system: str = "", timeout_override: Any = None,
) -> Optional[Dict[str, Any]]:
    """Run one call on the subscription. None when `model` is not routed there;
    otherwise an Ollama-shaped dict (`response` + `_meta`) or `{"error": ...}`."""
    name = _provider_name_for(model)
    if not name:
        return None
    entry = _entries()[name]
    limit = int(entry.get("daily_call_limit", 0) or 0)
    if not _take_daily_slot(name, limit):
        return {"error": f"Claude subscription daily call limit reached ({limit} calls)", "_subscription": name}
    provider = _provider(name, entry)
    opts = {"timeout_override": timeout_override} if timeout_override else {}
    resp = provider.generate(prompt, model=model, system=system, **opts)
    if resp.is_error:
        return {"error": str(resp.error), "_subscription": name}
    return {
        "response": resp.text,
        "model": resp.model or model,
        "done": True,
        "prompt_eval_count": resp.usage.prompt_tokens,
        "eval_count": resp.usage.completion_tokens,
        "_meta": {
            "model": resp.model or model,
            "provider": name,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }


__all__ = [
    "fallback_model_for", "generate_via_subscription", "remember_calls_in", "reset_for_tests",
    "subscription_models", "subscription_provider_for", "subscription_provider_name_for",
    "subscription_usage",
]
