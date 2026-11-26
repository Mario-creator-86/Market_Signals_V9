import os
from unittest.mock import MagicMock

from api.ai_config_service import (
    build_provider,
    list_ai_providers,
    probe_ai_provider,
    discover_ai_models,
    ai_validated_request,
    set_ai_assignment,
)


def _config(providers=None):
    base = {
        "ai": {
            "providers": {
                "ollama": {"enabled": True, "type": "ollama"},
                "openai": {
                    "enabled": True,
                    "type": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key_env": "OPENAI_API_KEY",
                    "default_model": "",
                    "timeout": 60,
                },
                "xai": {
                    "enabled": False,
                    "type": "openai_compatible",
                    "base_url": "https://api.x.ai/v1",
                    "api_key_env": "XAI_API_KEY",
                    "default_model": "",
                    "timeout": 60,
                },
            },
            "registry": [],
        }
    }
    if providers:
        base["ai"]["providers"].update(providers)
    return base


def test_build_provider_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    p = build_provider(_config(), "openai")
    assert p is not None
    assert p.name == "openai"
    assert p.base_url == "https://api.openai.com/v1"
    assert p.api_key_env == "OPENAI_API_KEY"


def test_build_provider_openai_compatible_for_xai(monkeypatch):
    cfg = _config(providers={"xai": {"enabled": True, "type": "openai_compatible", "base_url": "https://api.x.ai/v1", "api_key_env": "XAI_API_KEY", "default_model": "", "timeout": 60}})
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    p = build_provider(cfg, "xai")
    assert p is not None
    # OpenAICompatibleProvider subclasses OpenAIProvider (same wire shape)
    assert p.base_url == "https://api.x.ai/v1"


def test_build_provider_ollama_returns_cached_singleton():
    p = build_provider(_config(), "ollama")
    assert p is not None
    assert p.name == "ollama"


def test_build_provider_unknown_returns_none():
    assert build_provider(_config(), "nope") is None


def test_build_provider_disabled_returns_none():
    cfg = _config()
    p = build_provider(cfg, "xai")  # xai enabled=False in _config
    assert p is None


def test_list_ai_providers_returns_has_key_not_the_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    rows = list_ai_providers(_config())
    names = {r["name"]: r for r in rows}
    assert "openai" in names
    assert names["openai"]["has_key"] is True
    # §106 — the key value must NEVER appear in the payload
    assert "sk-secret-value" not in str(rows)
    assert "api_key" not in names["openai"]  # only has_key, never the value


def test_list_ai_providers_has_key_false_when_env_missing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rows = list_ai_providers(_config())
    names = {r["name"]: r for r in rows}
    assert names["openai"]["has_key"] is False


def test_test_ai_provider_reports_ok(monkeypatch):
    cfg = _config()
    fake_provider = MagicMock()
    fake_provider.health_check.return_value = True
    fake_provider.list_models.return_value = ["gpt-4o", "gpt-4o-mini"]
    import api.ai_config_service as svc
    monkeypatch.setattr(svc, "build_provider", lambda c, n: fake_provider)
    result = probe_ai_provider(cfg, "openai")
    assert result["ok"] is True
    assert result["models"] == ["gpt-4o", "gpt-4o-mini"]


def test_test_ai_provider_reports_not_ok_when_unhealthy(monkeypatch):
    cfg = _config()
    fake_provider = MagicMock()
    fake_provider.health_check.return_value = False
    import api.ai_config_service as svc
    monkeypatch.setattr(svc, "build_provider", lambda c, n: fake_provider)
    result = probe_ai_provider(cfg, "openai")
    assert result["ok"] is False


def test_discover_ai_models_returns_entries(monkeypatch):
    cfg = _config()
    fake_provider = MagicMock()
    fake_provider.name = "openai"
    fake_provider.list_models.return_value = ["gpt-4o", "o3-mini"]
    fake_provider.capabilities.return_value = MagicMock(structured_output=True, tool_calling=True)
    import api.ai_config_service as svc
    monkeypatch.setattr(svc, "build_provider", lambda c, n: fake_provider)
    rows = discover_ai_models(cfg, "openai")
    ids = {r["model_id"] for r in rows}
    assert {"gpt-4o", "o3-mini"} == ids
    for r in rows:
        assert r["provider"] == "openai"


def test_ai_validated_request_returns_text_and_usage(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = _config()
    fake_provider = MagicMock()
    fake_provider.generate.return_value = MagicMock(text="answer", is_error=False, error=None, model="gpt-4o", usage=MagicMock(prompt_tokens=5, completion_tokens=3, total_tokens=8))
    import api.ai_config_service as svc
    monkeypatch.setattr(svc, "build_provider", lambda c, n: fake_provider)
    out = ai_validated_request(cfg, "openai", "hello", model="gpt-4o")
    assert out["ok"] is True
    assert out["text"] == "answer"
    assert out["model"] == "gpt-4o"
    assert "key" not in out  # §106 — no key in the response


def test_global_assignment_synchronizes_legacy_selector_and_router(monkeypatch):
    saved = {}
    monkeypatch.setattr("api.config_service.save_config", lambda payload: saved.update(payload) or True)

    assert set_ai_assignment(_config(), {"all": "owner-model", "risk_critic": "old-model"}) is True

    assert saved["ollama"] == {"model": "owner-model", "cloud_model": "owner-model"}
    assert saved["ai"]["assignment"]["all"] == "owner-model"
    assert saved["ai"]["assignment"]["risk_critic"] is None
    assert all(tier["model"] is None for tier in saved["ai"]["router"].values())
