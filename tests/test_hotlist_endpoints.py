import api.state_service as state_service
from core.hotlist_scoring import get_hotlist_state


def test_get_hotlist_state_uses_fast_path_and_timestamp(monkeypatch):
    calls = {}

    def fake_get_state(**kwargs):
        calls.update(kwargs)
        return {
            "hot_list": [{"ticker": "AAPL", "earnings_edge_score": 77}],
            "timestamp": "2026-02-25T12:00:00+00:00",
        }

    monkeypatch.setattr(state_service, "get_state", fake_get_state)
    payload = get_hotlist_state()

    assert calls["include_news"] is False
    assert calls["include_hotlist"] is True
    assert calls["hotlist_details"] is False
    assert calls["lean"] is False
    assert payload["last_update"] == "2026-02-25T12:00:00+00:00"
    assert payload["hotlist"][0]["ticker"] == "AAPL"
    assert payload["hot_list"][0]["ticker"] == "AAPL"


def test_toggle_hotlist_normalizes_ticker(client, monkeypatch):
    class DummyConfig:
        def __init__(self):
            self.received = None

        def toggle_hot_list(self, ticker):
            self.received = ticker
            return True

    dummy = DummyConfig()
    monkeypatch.setattr(state_service, "get_shared_config", lambda: dummy)

    response = client.post("/api/watchlist/hotlist/toggle", json={"ticker": " aapl "})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["ticker"] == "AAPL"
    assert data["is_in"] is True
    assert dummy.received == "AAPL"


def test_toggle_hotlist_rejects_empty_ticker(client):
    response = client.post("/api/watchlist/hotlist/toggle", json={"ticker": "   "})
    assert response.status_code == 400
    assert "Ticker cannot be empty" in response.text
