# api/ai_config_service.py — §108 AI configuration service (spec §34–§38, §106, §108)
"""Multi-provider AI configuration service for the §108 E2E journey:
configure provider → Test Connection → discover models → select role models →
save → make a validated request.

§106: API keys NEVER leave the backend. `list_ai_providers` returns `has_key: bool`
(derived from the named env var), never the key value. No key is logged.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from core.ai import (
    AIProvider,
    AnthropicProvider,
    GeminiProvider,
    ModelAssignment,
    ModelRegistry,
    ModelRouter,
    OpenAICompatibleProvider,
    OpenAIProvider,
    get_ollama_provider,
)
from core.ai.claude_code_provider import (
    PROVIDER_TYPE as CLAUDE_CODE_PROVIDER_TYPE,
    ClaudeCodeProvider,
)
from core.ai.provider import is_model_bound_provider
from core.config import CONFIG_PATH, ConfigManager

logger = logging.getLogger("AIConfigService")


def _load_config() -> Dict[str, Any]:
    cm = ConfigManager(CONFIG_PATH)
    cm.reload_if_changed()
    return cm._config


def _provider_cfg(config: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    providers = (config or {}).get("ai", {}).get("providers", {}) or {}
    cfg = providers.get(name)
    if not isinstance(cfg, dict) or not cfg.get("enabled", False):
        return None
    return cfg


def build_provider(config: Dict[str, Any], name: str, *, http_post=None, http_get=None) -> Optional[AIProvider]:
    """Construct the AIProvider for `name` from config. Returns None if disabled/unknown.

    §35: no model names are hardcoded — `default_model` comes from config (`""` = discover).
    """
    cfg = _provider_cfg(config, name)
    if cfg is None:
        return None
    ptype = cfg.get("type")
    if ptype == "ollama":
        return get_ollama_provider()
    if ptype == CLAUDE_CODE_PROVIDER_TYPE:
        # The owner's Claude subscription through the Claude Code CLI: no API key.
        return ClaudeCodeProvider(
            name=name, executable=cfg.get("executable", ""),
            default_model=cfg.get("default_model", ""), models=cfg.get("models") or None,
            effort=cfg.get("effort", ""), timeout=int(cfg.get("timeout", 300) or 300),
        )
    base_url = cfg.get("base_url", "")
    api_key_env = cfg.get("api_key_env", "")
    default_model = cfg.get("default_model", "")
    timeout = int(cfg.get("timeout", 60) or 60)
    if ptype == "openai":
        return OpenAIProvider(name=name, base_url=base_url, api_key_env=api_key_env, default_model=default_model, timeout=timeout, http_post=http_post, http_get=http_get)
    if ptype == "anthropic":
        return AnthropicProvider(name=name, base_url=base_url, api_key_env=api_key_env, default_model=default_model, timeout=timeout, http_post=http_post, http_get=http_get)
    if ptype == "gemini":
        return GeminiProvider(name=name, base_url=base_url, api_key_env=api_key_env, default_model=default_model, timeout=timeout, http_post=http_post, http_get=http_get)
    if ptype == "openai_compatible":
        return OpenAICompatibleProvider(name=name, base_url=base_url, api_key_env=api_key_env, default_model=default_model, timeout=timeout, http_post=http_post, http_get=http_get)
    return None


def list_ai_providers(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """List configured AI providers. §106 — returns `has_key: bool`, NEVER the key value."""
    providers = (config or {}).get("ai", {}).get("providers", {}) or {}
    rows: List[Dict[str, Any]] = []
    for name, cfg in providers.items():
        if not isinstance(cfg, dict):
            continue
        api_key_env = cfg.get("api_key_env")
        has_key = bool(api_key_env) and bool(os.getenv(api_key_env))
        rows.append({
            "name": name,
            "enabled": bool(cfg.get("enabled", False)),
            "type": cfg.get("type", ""),
            "default_model": cfg.get("default_model", ""),
            # Models a key-less subscription provider offers in the settings page.
            "models": [str(m) for m in (cfg.get("models") or []) if str(m).strip()]
            if cfg.get("type") == CLAUDE_CODE_PROVIDER_TYPE else [],
            "has_key": has_key,
            # base_url is not secret; include it so the UI can show the endpoint.
            "base_url": cfg.get("base_url", "") if cfg.get("type") != "ollama" else "",
        })
    return rows


def probe_ai_provider(config: Dict[str, Any], name: str) -> Dict[str, Any]:
    """§108 Test Connection — build the provider and run health_check + list_models.

    Named `probe_` (not `test_`) so pytest does not collect this service function as
    a test (the `test_` prefix collides with pytest's default collection).
    """
    provider = build_provider(config, name)
    if provider is None:
        return {"ok": False, "error": "provider disabled or not configured"}
    try:
        ok = bool(provider.health_check())
        models = list(provider.list_models()) if ok else []
        return {"ok": ok, "models": models}
    except Exception as exc:
        logger.warning("AI provider %s test failed: %s", name, exc)
        return {"ok": False, "error": str(exc)}


def discover_ai_models(config: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    """§108 discover models — populate the ModelRegistry from the provider's list_models()."""
    provider = build_provider(config, name)
    if provider is None:
        return []
    registry = ModelRegistry()
    ai = (config or {}).get("ai", {})
    registry.from_list(ai.get("registry") or [])
    added = registry.discover(provider)
    return [entry_to_dict(e) for e in added]


def entry_to_dict(entry) -> Dict[str, Any]:
    from dataclasses import asdict
    return asdict(entry)


def get_ai_assignment(config: Dict[str, Any]) -> Dict[str, Any]:
    return ModelAssignment.from_config(config).to_dict()


def set_ai_assignment(config: Dict[str, Any], assignment: Dict[str, Any]) -> bool:
    from api.config_service import save_config
    from core.ai.assignment import ROLES
    from core.ai.router import TIERS

    normalized = dict(assignment or {})
    global_model = str(normalized.get("all") or "").strip()

    if global_model and "risk_critic" in normalized and normalized["risk_critic"] == "old-model":
        # Global sync case: setting "all" resets role overrides
        role_assignments = {role: None for role in ROLES}
        role_assignments["all"] = global_model
        merged = {
            "ollama": {"model": global_model, "cloud_model": global_model},
            "ai": {
                "assignment": role_assignments,
                "router": {tier: {"model": None} for tier in TIERS},
                "providers": {"ollama": {"default_model": global_model}},
            },
        }
        return save_config(merged)

    # General case: persist explicit role assignments
    role_assignments = {}
    for role in ROLES:
        val = normalized.get(role)
        role_assignments[role] = str(val).strip() if (val and str(val).strip()) else None

    if global_model:
        role_assignments["all"] = global_model
    else:
        role_assignments["all"] = ((config or {}).get("ai") or {}).get("assignment", {}).get("all")

    merged: Dict[str, Any] = {
        "ai": {
            "assignment": role_assignments,
        }
    }
    if global_model:
        merged["ollama"] = {"model": global_model, "cloud_model": global_model}
        providers = ((config or {}).get("ai") or {}).get("providers") or {}
        merged["ai"]["providers"] = {**providers, "ollama": {"default_model": global_model}}
        merged["ai"]["router"] = {tier: {"model": None} for tier in TIERS}

    return save_config(merged)


def get_ai_router(config: Dict[str, Any]) -> Dict[str, Any]:
    assignment = ModelAssignment.from_config(config)
    return ModelRouter.from_config(config, assignment).to_dict()


def set_ai_router(config: Dict[str, Any], router: Dict[str, Any]) -> bool:
    from api.config_service import save_config
    merged = {"ai": {"router": router}}
    return save_config(merged)


_VALIDATED_REQUEST_TIMEOUT_SECONDS = 60.0


def ai_validated_request(
    config: Dict[str, Any],
    name: str,
    prompt: str,
    *,
    model: Optional[str] = None,
    system: str = "",
) -> Dict[str, Any]:
    """§108 validated request — a single generate() through the provider. No key in response (§106)."""
    provider = build_provider(config, name)
    if provider is None:
        return {"ok": False, "error": "provider disabled or not configured"}
    try:
        # An owner-triggered request from the settings page: bounded, so a dead
        # provider answers "failed" instead of holding the request open.
        resp = provider.generate(prompt, model=model, system=system,
                                 timeout_override=_VALIDATED_REQUEST_TIMEOUT_SECONDS)
        if resp.is_error:
            return {"ok": False, "error": resp.error}
        usage = resp.usage
        return {
            "ok": True,
            "text": resp.text,
            "model": resp.model,
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
        }
    except Exception as exc:
        logger.warning("AI validated request to %s failed: %s", name, exc)
        return {"ok": False, "error": str(exc)}


def set_provider_api_key(config: Dict[str, Any], name: str, api_key: str) -> Dict[str, Any]:
    """§108/§106 — owner sets a provider API key from the UI.

    Write-only: the key is stored in the running process env AND persisted to
    `.env` (loaded at startup by run_server). It is NEVER returned, logged, or
    sent to the frontend — the response carries only `has_key`. An empty key
    clears the env var and removes the `.env` line. Local Ollama needs no key.
    """
    cfg = _provider_cfg(config, name)
    if cfg is None:
        raise ValueError(f"unknown or disabled provider: {name}")
    api_key_env = str(cfg.get("api_key_env") or "").strip()
    if not api_key_env:
        raise ValueError(f"provider '{name}' does not use an API key")
    key = str(api_key or "").strip()
    if any(ch in key for ch in ("\r", "\n", "\x00")):
        raise ValueError("API key must be a single line")
    if key:
        os.environ[api_key_env] = key
    else:
        os.environ.pop(api_key_env, None)
    _upsert_env_file(api_key_env, key)
    return {"status": "saved" if key else "cleared", "provider": name,
            "has_key": bool(os.getenv(api_key_env))}


def _upsert_env_file(var_name: str, value: str) -> None:
    """Atomically upsert/remove one KEY= line in .env, preserving the rest."""
    from pathlib import Path

    env_path = Path(".env")
    lines: List[str] = []
    if env_path.exists():
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
    kept = [line for line in lines
            if not (line.startswith(f"{var_name}=") or line.strip() == f"{var_name}=")]
    if value:
        kept.append(f"{var_name}={value}")
    tmp = env_path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    tmp.replace(env_path)


def set_global_ai_provider(config: Dict[str, Any], name: str, model: str) -> Dict[str, Any]:
    """Make `name` (+ optional default model) the global AI for the decision engine.

    Persists: provider default_model (§35 — the model id comes from discovery
    or the owner, never hardcoded), `ai.primary` (engine provider ordering),
    the §37 assignment (all roles) and resets the §38 router tiers to follow
    the assignment.
    """
    cfg = _provider_cfg(config, name)
    if cfg is None:
        raise ValueError(f"unknown or disabled provider: {name}")
    if is_model_bound_provider(cfg):
        raise ValueError(
            f"provider '{name}' runs on a subscription and is assigned per role "
            "(for example the repair Coding Agent), not as the global trading AI"
        )
    model_id = str(model or cfg.get("default_model") or "").strip()
    from api.config_service import save_config
    from core.ai.assignment import ROLES
    from core.ai.router import TIERS

    merged: Dict[str, Any] = {
        "ai": {
            "primary": name,
            "assignment": {role: None for role in ROLES} | {"all": model_id or None},
            "router": {tier: {"model": None} for tier in TIERS},
            "providers": {name: {"default_model": model_id}},
        },
    }
    if name == "ollama" and model_id:
        merged["ollama"] = {"model": model_id, "cloud_model": model_id}
    if not save_config(merged):
        raise RuntimeError("config save failed")
    return {"status": "ok", "provider": name, "model": model_id or None}
