from unittest.mock import patch

from core.alpaca_client import AlpacaClient


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _stock_payload():
    return {
        "snapshots": {
            "AAPL": {
                "latestTrade": {"p": 200.0, "t": "2026-07-24T20:00:00Z"},
                "latestQuote": {"bp": 199.9, "ap": 200.1},
                "dailyBar": {"c": 199.8},
                "prevDailyBar": {"c": 190.0},
            }
        }
    }


def test_stock_snapshot_is_normalized_and_cached(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    client = AlpacaClient(
        {
            "alpaca": {
                "enabled": True,
                "stock_feed": "iex",
                "currency": "USD",
                "snapshot_ttl_seconds": 30,
            }
        }
    )

    with patch("core.alpaca_client.http_get", return_value=_Response(_stock_payload())) as request:
        first = client.get_market_data("AAPL")
        second = client.get_market_data("AAPL")

    assert first == second
    assert first["price"] == 200.0
    assert first["change_pct"] == 5.2632
    assert first["bid"] == 199.9
    assert first["ask"] == 200.1
    assert first["source"] == "alpaca_iex"
    assert request.call_count == 1
    assert request.call_args.args[0] == "https://data.alpaca.markets/v2/stocks/snapshots"
    assert request.call_args.kwargs["params"]["symbols"] == "AAPL"
    assert request.call_args.kwargs["headers"]["APCA-API-KEY-ID"] == "key"
    assert "key" not in request.call_args.args[0]
    assert "secret" not in request.call_args.args[0]


def test_crypto_symbol_mapping_and_normalization(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "secret")
    client = AlpacaClient({"alpaca": {"enabled": True, "crypto_feed": "us"}})
    payload = {
        "snapshots": {
            "BTC/USD": {
                "latestTrade": {"p": 65000.0},
                "latestQuote": {"bp": 64990.0, "ap": 65010.0},
                "prevDailyBar": {"c": 64000.0},
            }
        }
    }

    with patch("core.alpaca_client.http_get", return_value=_Response(payload)) as request:
        result = client.get_market_data("BTC-USD", crypto=True)

    assert result["alpaca_symbol"] == "BTC/USD"
    assert result["price"] == 65000.0
    assert result["source"] == "alpaca_crypto"
    assert request.call_args.kwargs["params"]["symbols"] == "BTC/USD"


def test_client_is_unavailable_without_both_credentials(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)

    client = AlpacaClient({"alpaca": {"enabled": True}})

    assert client.status()["configured"] is False
    assert client.is_available() is False
    with patch("core.alpaca_client.http_get") as request:
        assert client.get_market_data("AAPL") is None
    request.assert_not_called()
