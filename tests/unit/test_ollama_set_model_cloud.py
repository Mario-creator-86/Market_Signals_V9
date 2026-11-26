"""
tests/unit/test_ollama_set_model_cloud.py
==========================================
Regression for the AI-model selector bug surfaced by the live Pre-Earnings
test (2026-08): `POST /api/ollama/set-model` wrote only `ollama.model`, but on
a CLOUD host `OllamaClient.get_preferred_model()` returns
`cloud_model or model`, so `cloud_model` ("gpt-oss:120b") won and the user's
selection ("deepseek-v4-pro") was IGNORED — the selector was cosmetic and the
active AI never changed.

Contract: on a cloud host, set-model must switch the ACTIVE model (the one the
engine actually calls), not just the unused `model` key. On a local host it
must keep writing `model`.
"""

import pytest


@pytest.mark.unit
def test_set_model_on_cloud_host_switches_active_model(client, monkeypatch):
    from core import ollama_client as oc
    from core import config as cfgmod

    shared = {
        "ollama": {
            "cloud_model": "gpt-oss:120b",
            "model": "gpt-oss:120b",
            "cloud_base_url": "https://ollama.com",
        }
    }

    class FakeCM:
        def __init__(self):
            self.config = shared

        def save(self):
            return True

    monkeypatch.setattr(cfgmod, "ConfigManager", FakeCM)

    # Force _get_ollama_config() to re-read from the patched ConfigManager
    # instead of its mtime cache.
    class _FakeStat:
        st_mtime_ns = 1

    class _FakePath:
        def stat(self):
            return _FakeStat()

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", _FakePath())
    monkeypatch.setattr(oc, "_CONFIG_CACHE", {"mtime_ns": None, "data": {}})

    monkeypatch.setattr(oc.ollama, "check_availability", lambda *a, **kw: True)
    monkeypatch.setattr(oc.ollama, "get_models", lambda *a, **kw: ["deepseek-v4-pro", "gpt-oss:120b"])
    monkeypatch.setattr(oc.ollama, "get_selectable_cloud_models", lambda *a, **kw: ["deepseek-v4-pro", "gpt-oss:120b"])
    monkeypatch.setattr(oc.ollama, "is_cloud_host", lambda *a, **kw: True)
    monkeypatch.setattr(oc.ollama, "refresh_config", lambda *a, **kw: None)
    # get_best_model() must NOT be stubbed — we assert it reflects the persisted
    # choice, which is the whole point of the regression.

    r = client.post("/api/ollama/set-model", json={"model": "deepseek-v4-pro"})
    assert r.status_code == 200, r.text
    body = r.json()

    # The chosen model must become the ACTIVE model on a cloud host.
    assert body["active_model"] == "deepseek-v4-pro", (
        "set-model on a cloud host must switch the active (cloud) model — "
        "writing only `model` leaves `cloud_model` in charge and the user's "
        "selection is ignored."
    )
    # And it must persist to the key the cloud host actually reads.
    assert shared["ollama"]["cloud_model"] == "deepseek-v4-pro"
    assert shared["ai"]["assignment"]["all"] == "deepseek-v4-pro"
    # The endpoint writes to ai.providers.ollama.default_model (not ai.router which is a legacy key).
    assert shared["ai"]["providers"]["ollama"]["default_model"] == "deepseek-v4-pro"


@pytest.mark.unit
def test_set_model_on_local_host_writes_model_key(client, monkeypatch):
    from core import ollama_client as oc
    from core import config as cfgmod

    shared = {"ollama": {"model": "gpt-oss:120b", "base_url": "http://localhost:11434"}}

    class FakeCM:
        def __init__(self):
            self.config = shared

        def save(self):
            return True

    monkeypatch.setattr(cfgmod, "ConfigManager", FakeCM)

    class _FakeStat:
        st_mtime_ns = 1

    class _FakePath:
        def stat(self):
            return _FakeStat()

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", _FakePath())
    monkeypatch.setattr(oc, "_CONFIG_CACHE", {"mtime_ns": None, "data": {}})

    monkeypatch.setattr(oc.ollama, "check_availability", lambda *a, **kw: True)
    monkeypatch.setattr(oc.ollama, "get_models", lambda *a, **kw: ["deepseek-v4-pro", "gpt-oss:120b"])
    monkeypatch.setattr(oc.ollama, "get_selectable_cloud_models", lambda *a, **kw: [])
    monkeypatch.setattr(oc.ollama, "is_cloud_host", lambda *a, **kw: False)
    monkeypatch.setattr(oc.ollama, "refresh_config", lambda *a, **kw: None)

    r = client.post("/api/ollama/set-model", json={"model": "deepseek-v4-pro"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["active_model"] == "deepseek-v4-pro"
    assert shared["ollama"]["model"] == "deepseek-v4-pro"
    assert body["scope"] == "global"
    assert shared["ai"]["assignment"]["all"] == "deepseek-v4-pro"


@pytest.mark.unit
def test_set_model_activates_unlinked_cloud_catalog_model(client, monkeypatch):
    from core import ollama_client as oc
    from core import config as cfgmod

    shared = {"ollama": {"model": "gemma4:26b", "base_url": "http://localhost:11434"}}

    class FakeCM:
        def __init__(self):
            self.config = shared

        def save(self):
            return True

    monkeypatch.setattr(cfgmod, "ConfigManager", FakeCM)
    monkeypatch.setattr(oc.ollama, "check_availability", lambda *a, **kw: True)
    installed = ["gemma4:26b"]
    monkeypatch.setattr(oc.ollama, "get_models", lambda *a, **kw: list(installed))
    monkeypatch.setattr(
        oc.ollama,
        "get_selectable_cloud_models",
        lambda *a, **kw: ["glm-5.3-flash:cloud"],
    )
    monkeypatch.setattr(oc.ollama, "is_cloud_host", lambda *a, **kw: False)
    monkeypatch.setattr(oc.ollama, "refresh_config", lambda *a, **kw: None)

    def pull(model, **_kwargs):
        installed.append(model)
        return {"ok": True, "model": model}

    monkeypatch.setattr(oc.ollama, "pull_model", pull)
    monkeypatch.setattr(oc.ollama, "get_best_model", lambda: shared["ollama"]["model"])

    response = client.post(
        "/api/ollama/set-model", json={"model": "glm-5.3-flash:cloud"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["active_model"] == "glm-5.3-flash:cloud"
    assert body["activation"] == "cloud_manifest_pulled"
    assert shared["ollama"]["model"] == "glm-5.3-flash:cloud"
