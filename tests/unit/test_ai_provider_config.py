"""§108/§106 tests — provider API-key configuration and global provider selection."""
import json

import pytest

from api.ai_config_service import (
    list_ai_providers, set_global_ai_provider, set_provider_api_key,
)

CONFIG = {
    "ai": {
        "providers": {
            "ollama": {"enabled": True, "type": "ollama"},
            "openai": {"enabled": True, "type": "openai", "base_url": "",
                       "api_key_env": "OPENAI_API_KEY", "default_model": ""},
            "kimi": {"enabled": True, "type": "openai_compatible",
                     "base_url": "https://api.moonshot.ai/v1",
                     "api_key_env": "KIMI_API_KEY", "default_model": ""},
            "zai": {"enabled": True, "type": "openai_compatible",
                    "base_url": "https://api.z.ai/api/paas/v4",
                    "api_key_env": "ZAI_API_KEY", "default_model": ""},
            "disabled_one": {"enabled": False, "type": "openai", "api_key_env": "X_KEY"},
        }
    }
}


def test_kimi_and_zai_are_configured_openai_compatible():
    rows = {row["name"]: row for row in list_ai_providers(CONFIG)}
    assert rows["kimi"]["type"] == "openai_compatible"
    assert rows["kimi"]["base_url"].startswith("https://api.moonshot.ai")
    assert rows["zai"]["base_url"].startswith("https://api.z.ai")
    # §35 — no hardcoded model ids; discovery fills them.
    assert rows["kimi"]["default_model"] == "" and rows["zai"]["default_model"] == ""


def test_set_provider_api_key_is_write_only_and_persists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    result = set_provider_api_key(CONFIG, "kimi", "sk-test-kimi-123")
    assert result == {"status": "saved", "provider": "kimi", "has_key": True}
    # The key value NEVER appears in the response (§106).
    assert "sk-test-kimi-123" not in json.dumps(result)
    import os
    assert os.environ["KIMI_API_KEY"] == "sk-test-kimi-123"
    persisted = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "KIMI_API_KEY=sk-test-kimi-123" in persisted
    # list_ai_providers exposes only has_key.
    rows = {r["name"]: r for r in list_ai_providers(CONFIG)}
    assert rows["kimi"]["has_key"] is True and "key" not in rows["kimi"]

    # Clearing removes the env var and the .env line.
    cleared = set_provider_api_key(CONFIG, "kimi", "")
    assert cleared == {"status": "cleared", "provider": "kimi", "has_key": False}
    assert "KIMI_API_KEY" not in os.environ
    assert "KIMI_API_KEY" not in (tmp_path / ".env").read_text(encoding="utf-8")


def test_set_provider_api_key_rejects_unknown_and_keyless_providers():
    with pytest.raises(ValueError):
        set_provider_api_key(CONFIG, "nope", "k")
    with pytest.raises(ValueError):
        set_provider_api_key(CONFIG, "disabled_one", "k")
    with pytest.raises(ValueError):
        set_provider_api_key(CONFIG, "ollama", "k")  # local Ollama needs no key


def test_set_global_ai_provider_persists_primary_and_assignment(tmp_path, monkeypatch):
    saved = {}
    import api.config_service as config_service

    monkeypatch.setattr(config_service, "save_config", lambda merged: saved.update(merged) or True)
    result = set_global_ai_provider(CONFIG, "zai", "glm-test-model")
    assert result["status"] == "ok" and result["model"] == "glm-test-model"
    merged = saved["ai"]
    assert merged["primary"] == "zai"
    assert merged["providers"]["zai"]["default_model"] == "glm-test-model"
    assert merged["assignment"]["all"] == "glm-test-model"
    assert all(merged["assignment"][role] is None for role in merged["assignment"] if role != "all")

    # Ollama selection keeps the legacy selectors in sync.
    saved.clear()
    set_global_ai_provider(CONFIG, "ollama", "gemma4:26b")
    assert saved["ollama"] == {"model": "gemma4:26b", "cloud_model": "gemma4:26b"}

    with pytest.raises(ValueError):
        set_global_ai_provider(CONFIG, "nope", "m")


def test_decision_engine_prefers_owner_selected_primary():
    from core.ai.decision_engine import _build_providers

    primary, model, fallback = _build_providers(CONFIG | {"ai": {**CONFIG["ai"], "primary": "zai"}}, {})
    assert primary is not None and primary.name == "zai"
    primary, _, _ = _build_providers(CONFIG, {})
    assert primary.name == "ollama"  # default ordering without a selection


def test_key_routes_validate_and_never_echo(client):
    missing = client.post("/api/ai/providers/does_not_exist/key", json={"api_key": "x"})
    assert missing.status_code == 422
    bad_body = client.post("/api/ai/providers/openai/key", json={})
    assert bad_body.status_code == 422
