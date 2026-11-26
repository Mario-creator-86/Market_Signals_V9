from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from api.main import app
from api.market_data_service import _get_stock_data


def test_stock_lightweight_prefers_alpaca_over_yahoo():
    alpaca = Mock()
    alpaca.status.return_value = {"prefer_for_stocks": True}
    alpaca.get_market_data.return_value = {
        "price": 201.25,
        "change_pct": 1.75,
        "source": "alpaca_iex",
    }

    with patch("core.alpaca_client.get_alpaca_client", return_value=alpaca), patch(
        "api.market_data_service.YahooClient.get_stock_data"
    ) as yahoo:
        result = _get_stock_data("AAPL", lightweight=True)

    assert result["price"] == 201.25
    assert result["change_pct"] == 1.75
    assert result["data_source"] == "alpaca_iex"
    yahoo.assert_not_called()


def test_alpaca_status_never_exposes_credentials():
    alpaca = Mock()
    alpaca.status.return_value = {
        "enabled": True,
        "configured": True,
        "available": True,
        "stock_feed": "iex",
        "crypto_feed": "us",
    }

    with patch("core.alpaca_client.get_alpaca_client", return_value=alpaca):
        response = TestClient(app).get("/api/alpaca/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "alpaca"
    assert "api_key" not in payload
    assert "secret" not in payload


def test_alpaca_snapshots_reports_missing_runtime_credentials():
    alpaca = Mock()
    alpaca.is_available.return_value = False

    with patch("core.alpaca_client.get_alpaca_client", return_value=alpaca):
        response = TestClient(app).get("/api/alpaca/snapshots?symbols=AAPL")

    assert response.status_code == 503
    assert "ALPACA_API_KEY" in response.json()["detail"]
