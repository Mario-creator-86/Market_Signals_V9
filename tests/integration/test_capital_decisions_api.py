import core.capital_manager as capital_mod
from core.capital_manager import CapitalManager


def test_capital_decisions_api_returns_records(client, tmp_path, monkeypatch):
    capital_path = tmp_path / "capital_api_decisions.json"
    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", capital_path)
    monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: None)
    CapitalManager._instance = None
    monkeypatch.setattr("api.main._get_cached_entry_price", lambda ticker: 100.0)

    open_response = client.post(
        "/api/capital/open-position",
        json={
            "ticker": "AAPL",
            "direction": "long",
            "leverage": 5,
            "usd_size": 1000,
            "reason": "decision api test",
        },
    )
    assert open_response.status_code == 200

    response = client.get("/api/capital/decisions?limit=5")
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload.get("records"), list)
    assert payload.get("count", 0) >= 1
    assert any(str(item.get("event_type")) == "position_opened" for item in payload.get("records", []))

    CapitalManager._instance = None
