import pytest


pytestmark = pytest.mark.integration


def test_core_tab_pipeline_contract(client):
    dashboard = client.get("/api/dashboard")
    assert dashboard.status_code == 200

    news = client.get("/api/news")
    assert news.status_code == 200

    earnings = client.get("/api/earnings")
    assert earnings.status_code == 200

    premarket = client.get("/api/premarket")
    assert premarket.status_code == 200

    preearn = client.get("/api/pre-earnings")
    assert preearn.status_code == 200

    agents = client.get("/api/agents")
    assert agents.status_code == 200

    # Basic dependency consistency: payloads should include stable routing keys.
    assert "signals_count" in dashboard.json()
    assert "items" in news.json()
    assert "earnings" in earnings.json()
    assert "candidates" in premarket.json()
    assert "items" in preearn.json()
    assert "agents" in agents.json()
