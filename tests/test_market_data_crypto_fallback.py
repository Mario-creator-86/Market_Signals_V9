import pandas as pd
from types import SimpleNamespace

from api.market_data_service import (
    BinanceClient,
    KuCoinClient,
    YahooClient,
    _get_crypto_data,
    calculate_rsi,
    clear_cache,
)


def _set_all_exchange_prices(monkeypatch, price: float = 0.0, change: float = 0.0):
    monkeypatch.setattr(BinanceClient, "get_last_price", classmethod(lambda cls, ticker, ttl=2: price))
    monkeypatch.setattr(BinanceClient, "get_24h_change", classmethod(lambda cls, ticker, ttl=10: change))
    monkeypatch.setattr(KuCoinClient, "get_last_price", classmethod(lambda cls, ticker, ttl=2: price))
    monkeypatch.setattr(KuCoinClient, "get_24h_change", classmethod(lambda cls, ticker, ttl=10: change))
    monkeypatch.setattr(YahooClient, "get_stock_data", classmethod(lambda cls, ticker, ttl=30: {"price": price, "change_pct": change, "sector": "Crypto"}))


def test_crypto_falls_back_to_yahoo_when_exchanges_fail(monkeypatch):
    clear_cache()
    _set_all_exchange_prices(monkeypatch, price=0.0, change=0.0)
    monkeypatch.setattr(
        YahooClient,
        "get_stock_data",
        classmethod(lambda cls, ticker, ttl=30: {"price": 0.1234, "change_pct": 7.25, "sector": "Crypto"}),
    )

    out = _get_crypto_data("BOSONUSDT", lightweight=True)

    assert out["price"] == 0.1234
    assert out["change_pct"] == 7.25
    assert out["asset_type"] == "crypto"


def test_crypto_uses_last_good_snapshot_on_transient_failure(monkeypatch):
    clear_cache()
    # Seed last_good via Yahoo
    monkeypatch.setattr(BinanceClient, "get_last_price", classmethod(lambda cls, ticker, ttl=2: 0.0))
    monkeypatch.setattr(BinanceClient, "get_24h_change", classmethod(lambda cls, ticker, ttl=10: 0.0))
    monkeypatch.setattr(KuCoinClient, "get_last_price", classmethod(lambda cls, ticker, ttl=2: 0.0))
    monkeypatch.setattr(KuCoinClient, "get_24h_change", classmethod(lambda cls, ticker, ttl=10: 0.0))
    monkeypatch.setattr(
        YahooClient,
        "get_stock_data",
        classmethod(lambda cls, ticker, ttl=30: {"price": 1.5, "change_pct": -2.0, "sector": "Crypto"}),
    )
    first = _get_crypto_data("SIRENUSDT", lightweight=True)
    assert first["price"] == 1.5

    monkeypatch.setattr(
        YahooClient,
        "get_stock_data",
        classmethod(lambda cls, ticker, ttl=30: {"price": 0.0, "change_pct": 0.0, "sector": "Crypto"}),
    )
    second = _get_crypto_data("SIRENUSDT", lightweight=True)
    assert second["price"] == 1.5
    assert second["change_pct"] == -2.0


def test_calculate_rsi_returns_none_when_history_is_missing_or_short():
    assert calculate_rsi(None) is None
    assert calculate_rsi(pd.DataFrame()) is None
    assert calculate_rsi(pd.DataFrame({"Close": [100.0] * 10}), period=14) is None


def test_dashed_usdt_symbol_uses_kucoin_without_binance_probe(monkeypatch):
    clear_cache()
    monkeypatch.setattr(
        BinanceClient,
        "get_last_price",
        classmethod(lambda cls, ticker, ttl=2: (_ for _ in ()).throw(AssertionError("unexpected Binance probe"))),
    )
    monkeypatch.setattr(
        BinanceClient,
        "get_24h_change",
        classmethod(lambda cls, ticker, ttl=10: (_ for _ in ()).throw(AssertionError("unexpected Binance probe"))),
    )
    monkeypatch.setattr(KuCoinClient, "get_last_price", classmethod(lambda cls, ticker, ttl=2: 2.5))
    monkeypatch.setattr(KuCoinClient, "get_24h_change", classmethod(lambda cls, ticker, ttl=10: 7.0))

    out = _get_crypto_data("CLO-USDT", lightweight=True)

    assert out["price"] == 2.5
    assert out["change_pct"] == 7.0
    assert out["data_source"] == "kucoin"


def test_binance_invalid_symbol_is_negative_cached_across_methods(monkeypatch):
    clear_cache()
    calls = []

    def invalid_symbol(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(
            status_code=400,
            json=lambda: {"code": -1121, "msg": "Invalid symbol."},
        )

    monkeypatch.setattr("api.market_data_service.http_get", invalid_symbol)

    assert BinanceClient.get_last_price("NO_SUCH_ASSET-USDT") is None
    assert BinanceClient.get_24h_change("NO_SUCH_ASSET-USDT") == 0.0
    assert BinanceClient.fetch_klines("NO_SUCH_ASSET-USDT", "15m", 32) is None
    assert len(calls) == 1
    clear_cache()
