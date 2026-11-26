from core.config import (
    _dedupe_keep_order,
    _normalize_watchlist_ticker,
    is_crypto_ticker,
    normalize_ticker,
)


def test_normalize_ticker_provider_mapping():
    assert normalize_ticker("gold", provider="yahoo") == "GC=F"
    assert normalize_ticker("EUR/USD", provider="yahoo") == "EURUSD=X"
    assert normalize_ticker("btcusd", provider="yahoo") == "BTC-USD"


def test_watchlist_normalize_and_dedup_case_insensitive():
    raw = ["aapl", "AAPL", "btcusd", "BTC-USD", "msft", "MSFT"]
    normalized = [
        _normalize_watchlist_ticker(item, is_crypto=is_crypto_ticker(item))
        for item in raw
    ]
    deduped = _dedupe_keep_order(normalized)
    assert deduped == ["AAPL", "BTC-USD", "MSFT"]
