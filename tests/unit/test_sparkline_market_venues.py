import pandas as pd


def _candles(values):
    return pd.DataFrame({"Close": values})


def test_crypto_sparkline_uses_kucoin_when_binance_has_no_pair(monkeypatch):
    from api.main import _fetch_sparkline_points
    from api.market_data_service import BinanceClient, KuCoinClient
    from core.stock_data_client import StockDataClient

    monkeypatch.setattr(BinanceClient, "fetch_klines", lambda *args, **kwargs: None)
    monkeypatch.setattr(KuCoinClient, "fetch_klines", lambda *args, **kwargs: _candles([1, 2, 3, 4, 5]))
    monkeypatch.setattr(
        StockDataClient,
        "get_historical",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("crypto must not query Yahoo")),
    )

    assert _fetch_sparkline_points("CLO-USD") == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_crypto_sparkline_returns_unavailable_when_no_supported_venue(monkeypatch):
    from api.main import _fetch_sparkline_points
    from api.market_data_service import BinanceClient, KuCoinClient
    from core.stock_data_client import StockDataClient

    monkeypatch.setattr(BinanceClient, "fetch_klines", lambda *args, **kwargs: None)
    monkeypatch.setattr(KuCoinClient, "fetch_klines", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        StockDataClient,
        "get_historical",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("crypto must not query Yahoo")),
    )

    assert _fetch_sparkline_points("UNKNOWN-USD") is None


def test_kucoin_scanner_symbol_uses_native_interval_without_binance_probe(monkeypatch):
    from api.main import _fetch_sparkline_points
    from api.market_data_service import BinanceClient, KuCoinClient

    calls = []

    def kucoin_candles(ticker, interval="1min", limit=200, ttl=30):
        calls.append((ticker, interval, limit))
        return _candles([1, 2, 3, 4, 5])

    monkeypatch.setattr(KuCoinClient, "fetch_klines", kucoin_candles)
    monkeypatch.setattr(
        BinanceClient,
        "fetch_klines",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected Binance probe")),
    )

    assert _fetch_sparkline_points("CLO-USDT") == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert calls == [("CLO-USDT", "15min", 32)]
