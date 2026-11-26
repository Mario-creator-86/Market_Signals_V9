import pytest


@pytest.mark.unit
def test_ollama_status_merges_runtime_and_live_cloud_catalog(client, monkeypatch):
    from core.ollama_client import ollama

    seen = {"availability_force": None, "models_force": None, "cloud_force": None}

    def available(*, force=False):
        seen["availability_force"] = force
        return True

    def local_models(*, force=False):
        seen["models_force"] = force
        return ["gemma4:26b", "gpt-oss:120b-cloud"]

    def cloud_models(*, force=False):
        seen["cloud_force"] = force
        return ["gpt-oss:120b-cloud", "glm-5.3-flash:cloud"]

    monkeypatch.setattr(ollama, "check_availability", available)
    monkeypatch.setattr(ollama, "get_models", local_models)
    monkeypatch.setattr(ollama, "get_selectable_cloud_models", cloud_models)
    monkeypatch.setattr(ollama, "get_preferred_model", lambda: "gemma4:26b")
    monkeypatch.setattr(ollama, "get_best_model", lambda: "gemma4:26b")
    monkeypatch.setattr(ollama, "get_timeout", lambda: 120)

    response = client.get("/api/ollama/status?refresh=true")

    assert response.status_code == 200
    body = response.json()
    assert body["models"] == [
        "gemma4:26b",
        "gpt-oss:120b-cloud",
        "glm-5.3-flash:cloud",
    ]
    assert body["local_models"] == ["gemma4:26b", "gpt-oss:120b-cloud"]
    assert body["cloud_models"] == [
        "gpt-oss:120b-cloud",
        "glm-5.3-flash:cloud",
    ]
    assert body["cloud_catalog_available"] is True
    assert seen == {
        "availability_force": True,
        "models_force": True,
        "cloud_force": True,
    }
