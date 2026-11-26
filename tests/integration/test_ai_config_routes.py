from unittest.mock import MagicMock


def test_get_ai_providers_route(client, monkeypatch):
    # ollama is enabled by default; ensure no key value leaks (§106)
    resp = client.get("/api/ai/providers")
    assert resp.status_code == 200
    rows = resp.json()
    names = {r["name"]: r for r in rows}
    assert "ollama" in names
    assert "has_key" in names["ollama"]
    assert "api_key" not in names["ollama"]


def test_test_ai_provider_route_disabled(client):
    resp = client.post("/api/ai/providers/nope/test")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_get_assignment_route(client):
    resp = client.get("/api/ai/assignment")
    assert resp.status_code == 200
    body = resp.json()
    assert "all" in body
    assert "market_analyst" in body


def test_get_router_route(client):
    resp = client.get("/api/ai/router")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"triage", "analysis", "critic"}


def test_validated_request_route_provider_disabled(client):
    resp = client.post("/api/ai/request", json={"provider": "nope", "prompt": "hi"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_test_ai_provider_route_with_fake(client, monkeypatch):
    fake = MagicMock()
    fake.health_check.return_value = True
    fake.list_models.return_value = ["m1"]
    import api.ai_config_service as svc
    monkeypatch.setattr(svc, "build_provider", lambda c, n: fake)
    resp = client.post("/api/ai/providers/openai/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["models"] == ["m1"]


def test_discover_route_with_fake(client, monkeypatch):
    fake = MagicMock()
    fake.name = "openai"
    fake.list_models.return_value = ["m1", "m2"]
    fake.capabilities.return_value = MagicMock(structured_output=True, tool_calling=True)
    import api.ai_config_service as svc
    monkeypatch.setattr(svc, "build_provider", lambda c, n: fake)
    resp = client.post("/api/ai/providers/openai/discover")
    assert resp.status_code == 200
    ids = {r["model_id"] for r in resp.json()}
    assert {"m1", "m2"} == ids