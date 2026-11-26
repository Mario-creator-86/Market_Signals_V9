def test_sec_api_returns_service_payload(client, monkeypatch):
    monkeypatch.setattr("api.main.get_sec_context", lambda ticker, refresh=False: {
        "ticker": ticker,
        "status": "available",
        "source": "cache",
        "filings": [{"form": "10-K"}],
        "total_filings": 1,
    })
    response = client.get("/api/sec/AAPL")
    assert response.status_code == 200
    assert response.json()["filings"][0]["form"] == "10-K"


def test_sec_api_rejects_invalid_ticker(client):
    response = client.get("/api/sec/%2E%2E%2FAAPL")
    assert response.status_code in {404, 422}
