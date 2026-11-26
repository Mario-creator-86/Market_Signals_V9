from core import live_price_cache


class _MemoryStore:
    def __init__(self):
        self.values = {}

    def set_json(self, key, value):
        self.values[key] = value

    def get_json(self, key, default=None):
        return self.values.get(key, default)


def test_cross_process_live_price_cache_round_trip(monkeypatch):
    store = _MemoryStore()
    monkeypatch.setattr(live_price_cache, "get_sqlite_store", lambda: store)

    assert live_price_cache.store_live_price(
        " btc-usd ",
        61234.5,
        source="Binance_orderbook_mid",
        timestamp="2026-07-31T12:00:00+00:00",
    )

    prices = live_price_cache.get_live_prices(["BTC-USD", "btc-usd"])
    assert list(prices) == ["BTC-USD"]
    assert prices["BTC-USD"]["price"] == 61234.5
    assert prices["BTC-USD"]["source"] == "Binance_orderbook_mid"
    assert prices["BTC-USD"]["timestamp"] == "2026-07-31T12:00:00+00:00"


def test_cross_process_live_price_cache_rejects_invalid_quotes(monkeypatch):
    store = _MemoryStore()
    monkeypatch.setattr(live_price_cache, "get_sqlite_store", lambda: store)

    assert live_price_cache.store_live_price("", 10, source="test") is False
    assert live_price_cache.store_live_price("AAPL", 0, source="test") is False
    assert live_price_cache.store_live_price("AAPL", "bad", source="test") is False
    assert store.values == {}
