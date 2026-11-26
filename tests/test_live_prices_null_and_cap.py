"""Tests for /api/live-prices F9 (null change_pct) and F12 (symbol cap).

F9: a ticker with NO change data must return change_pct=null instead of
    fabricating +0.00% as if real. The frontend already treats null as
    'no change data'.
F12: large watchlists must be capped to a max number of symbols processed
    per request, returning truncated/requested_count/returned_count so the
    UI knows it was capped.
"""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _empty_watchlist(monkeypatch):
    """Force an empty watchlist so /api/live-prices never fans out to remote
    Yahoo/Binance/KuCoin fetches (keeps these tests deterministic & fast)."""
    monkeypatch.setattr("api.actions_service._load_watchlist_payload",
                        lambda: {"stocks": [], "crypto": []})


@pytest.fixture(autouse=True)
def _isolated_live_price_storage(monkeypatch, tmp_path):
    """Never read operator caches or reach providers from this unit suite."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("api.main.DATA_DIR", data_dir)
    monkeypatch.setenv("MS_DISABLE_LIVE_PRICE_REMOTE", "1")


@pytest.fixture(autouse=True)
def _block_remote(monkeypatch):
    """Belt-and-suspenders: block requests.request so any remote path is a
    no-op rather than a hang."""
    import requests
    monkeypatch.setattr("requests.request",
                        lambda *a, **kw: (_ for _ in ()).throw(requests.RequestException("blocked-by-test")))
from unittest.mock import patch


def test_live_prices_change_pct_null_when_absent_from_state(client, monkeypatch, tmp_path):
    """F9: state.json row with price but no change_pct → change_pct=null, not 0.0."""
    # Build a state.json with one ticker that has price but change_pct is None.
    state_payload = {
        "tickers": {
            "FOO": {"price": 100.0, "change_pct": None, "last_update": "2026-07-03T00:00:00+00:00"},
            "BAR": {"price": 200.0, "change_pct": 1.5, "last_update": "2026-07-03T00:00:00+00:00"},
        }
    }
    monkeypatch.setattr("api.main.load_state_payload", lambda default: state_payload)
    # Redirect BASE_DIR to a tmp dir with NO live_prices_cache.json so the
    # cache-file branch is skipped naturally (no global Path.exists patch).
    import core.config as core_config
    monkeypatch.setattr(core_config, "BASE_DIR", tmp_path)
    # Empty watchlist + request the two tickers explicitly, cache_fallback=False, remote=False
    # so we only exercise the local state.json path.
    r = client.get("/api/live-prices", params={"symbols": "FOO,BAR"})
    assert r.status_code == 200
    data = r.json()
    prices = data["prices"]
    assert "FOO" in prices
    # F9: change_pct must be null, not 0.0
    assert prices["FOO"]["change_pct"] is None
    # Sanity: a ticker WITH change data still returns the value
    assert prices["BAR"]["change_pct"] == 1.5


def test_live_prices_change_pct_null_when_absent_from_cache_file(client, monkeypatch, tmp_path):
    """F9: live_prices_cache.json row with price but change_pct=None → null."""
    # state.json empty so we fall through to the cache file path.
    monkeypatch.setattr("api.main.load_state_payload", lambda default: {"tickers": {}})

    cache_payload = {
        "ts": time.time(),
        "prices": {
            "BAZ": {"price": 50.0, "change_pct": None, "source": "yahoo", "timestamp": ""},
            "QUX": {"price": 60.0, "change_pct": -0.7, "source": "yahoo", "timestamp": ""},
        },
    }

    # The autouse storage fixture points the endpoint at this isolated data dir.
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "live_prices_cache.json").write_text(json.dumps(cache_payload), encoding="utf-8")

    import core.config as core_config
    monkeypatch.setattr(core_config, "BASE_DIR", tmp_path)

    r = client.get("/api/live-prices", params={"symbols": "BAZ,QUX"})
    assert r.status_code == 200
    prices = r.json()["prices"]
    assert "BAZ" in prices
    assert prices["BAZ"]["change_pct"] is None
    assert prices["QUX"]["change_pct"] == -0.7


def test_live_prices_prefers_newer_persistent_quote_over_stale_state(client, monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        "api.main.load_state_payload",
        lambda default: {
            "tickers": {
                "AAPL": {
                    "price": 100.0,
                    "change_pct": -1.0,
                    "last_update": (now - timedelta(days=2)).isoformat(),
                }
            }
        },
    )
    cache_path = tmp_path / "data" / "live_prices_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "ts": time.time(),
                "prices": {
                    "AAPL": {
                        "price": 125.5,
                        "change_pct": 2.25,
                        "source": "yahoo",
                        "timestamp": now.isoformat(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    response = client.get(
        "/api/live-prices",
        params={"symbols": "AAPL", "remote": "false", "max_age_seconds": 45},
    )

    assert response.status_code == 200
    quote = response.json()["prices"]["AAPL"]
    assert quote["price"] == 125.5
    assert quote["source"] == "yahoo"
    assert quote["freshness"] == "CACHED"
    assert quote["age_seconds"] < 5


def test_live_prices_remote_refreshes_only_stale_quotes(client, monkeypatch):
    from api.market_data_service import YahooClient

    now = datetime.now(timezone.utc)
    monkeypatch.setenv("MS_DISABLE_LIVE_PRICE_REMOTE", "0")
    monkeypatch.setattr("core.live_price_cache.get_live_prices", lambda symbols: {})
    monkeypatch.setattr(
        "api.main.load_state_payload",
        lambda default: {
            "tickers": {
                "AAPL": {
                    "price": 100.0,
                    "change_pct": -1.0,
                    "last_update": (now - timedelta(minutes=10)).isoformat(),
                },
                "MSFT": {
                    "price": 200.0,
                    "change_pct": 0.5,
                    "last_update": now.isoformat(),
                },
            }
        },
    )
    requested = []

    class DummyAlpaca:
        def get_market_data(self, ticker, crypto=False, ttl=5):
            requested.append(("alpaca", ticker))
            if ticker == "MSFT":
                raise AssertionError("fresh MSFT quote must not be refreshed")
            return None

    monkeypatch.setattr("core.alpaca_client.get_alpaca_client", lambda: DummyAlpaca())

    def _yahoo_quote(cls, ticker, ttl=2):
        requested.append(("yahoo", ticker))
        if ticker != "AAPL":
            raise AssertionError(f"unexpected Yahoo refresh for {ticker}")
        return {"price": 101.25, "change_pct": 1.25}

    monkeypatch.setattr(YahooClient, "get_stock_data", classmethod(_yahoo_quote))

    response = client.get(
        "/api/live-prices",
        params={
            "symbols": "AAPL,MSFT",
            "remote": "true",
            "cache_fallback": "true",
            "max_age_seconds": 45,
        },
    )

    assert response.status_code == 200
    prices = response.json()["prices"]
    assert prices["AAPL"]["price"] == 101.25
    assert prices["AAPL"]["freshness"] == "LIVE"
    assert prices["MSFT"]["price"] == 200.0
    assert prices["MSFT"]["freshness"] == "CACHED"
    assert requested == [("alpaca", "AAPL"), ("yahoo", "AAPL")]


def test_live_prices_symbol_cap_truncates(client, monkeypatch, tmp_path):
    """F12: requesting more symbols than the cap returns truncated=true + counts."""
    # Build a state.json with many tickers.
    tickers = {f"T{i:03d}": {"price": 10.0 + i, "change_pct": 0.1 * i} for i in range(80)}
    monkeypatch.setattr("api.main.load_state_payload", lambda default: {"tickers": tickers})
    import core.config as core_config
    monkeypatch.setattr(core_config, "BASE_DIR", tmp_path)

    # Force the cap low for this test via env var
    monkeypatch.setenv("MS_LIVE_PRICES_MAX_SYMBOLS", "10")
    # Request all 80 symbols explicitly
    symbols = ",".join(f"T{i:03d}" for i in range(80))
    r = client.get("/api/live-prices", params={"symbols": symbols})
    assert r.status_code == 200
    data = r.json()
    assert data["truncated"] is True
    assert data["requested_count"] == 80
    assert data["returned_count"] == 10
    assert data["max_symbols"] == 10
    assert len(data["prices"]) == 10


def test_live_prices_symbol_cap_not_truncated_when_under_limit(client, monkeypatch, tmp_path):
    """F12: requesting fewer than the cap returns truncated=false."""
    tickers = {f"T{i:03d}": {"price": 10.0 + i, "change_pct": 0.1 * i} for i in range(5)}
    monkeypatch.setattr("api.main.load_state_payload", lambda default: {"tickers": tickers})
    import core.config as core_config
    monkeypatch.setattr(core_config, "BASE_DIR", tmp_path)

    symbols = ",".join(f"T{i:03d}" for i in range(5))
    r = client.get("/api/live-prices", params={"symbols": symbols})
    assert r.status_code == 200
    data = r.json()
    assert data["truncated"] is False
    assert data["requested_count"] == 5
    assert data["returned_count"] == 5
    assert len(data["prices"]) == 5


def test_live_prices_response_shape_includes_truncation_fields(client, monkeypatch, tmp_path):
    """F12: response always includes truncated/requested_count/returned_count/max_symbols."""
    monkeypatch.setattr("api.main.load_state_payload", lambda default: {"tickers": {}})
    import core.config as core_config
    monkeypatch.setattr(core_config, "BASE_DIR", tmp_path)
    r = client.get("/api/live-prices")
    assert r.status_code == 200
    data = r.json()
    for key in ("truncated", "requested_count", "returned_count", "max_symbols"):
        assert key in data, f"missing {key}"


def test_live_prices_explicit_symbols_do_not_expand_to_watchlist(client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "api.actions_service._load_watchlist_payload",
        lambda: {"stocks": ["AAPL", "MSFT"], "crypto": ["BTC-USD"]},
    )
    monkeypatch.setattr(
        "api.main.load_state_payload",
        lambda default: {
            "tickers": {
                "AAPL": {"price": 200.0},
                "MSFT": {"price": 500.0},
                "BTC-USD": {"price": 60000.0},
                "ONLY": {"price": 12.5},
            }
        },
    )
    import core.config as core_config
    monkeypatch.setattr(core_config, "BASE_DIR", tmp_path)

    response = client.get(
        "/api/live-prices",
        params={"symbols": "ONLY", "remote": "false"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["requested_count"] == 1
    assert payload["returned_count"] == 1
    assert list(payload["prices"]) == ["ONLY"]


def test_live_prices_routes_dashed_usdt_to_kucoin_first(client, monkeypatch):
    from api.market_data_service import BinanceClient, KuCoinClient

    monkeypatch.setenv("MS_DISABLE_LIVE_PRICE_REMOTE", "0")
    monkeypatch.setattr("api.main.load_state_payload", lambda default: {"tickers": {}})
    monkeypatch.setattr(
        BinanceClient,
        "get_last_price",
        classmethod(lambda cls, ticker, ttl=2: (_ for _ in ()).throw(AssertionError("unexpected Binance probe"))),
    )
    monkeypatch.setattr(
        BinanceClient,
        "get_24h_change",
        classmethod(lambda cls, ticker, ttl=30: (_ for _ in ()).throw(AssertionError("unexpected Binance probe"))),
    )
    monkeypatch.setattr(KuCoinClient, "get_last_price", classmethod(lambda cls, ticker, ttl=2: 4.25))
    monkeypatch.setattr(KuCoinClient, "get_24h_change", classmethod(lambda cls, ticker, ttl=30: 3.5))

    response = client.get(
        "/api/live-prices",
        params={"symbols": "CLO-USDT", "remote": "true", "cache_fallback": "false"},
    )

    assert response.status_code == 200
    quote = response.json()["prices"]["CLO-USDT"]
    assert quote["price"] == 4.25
    assert quote["change_pct"] == 3.5
    assert quote["live"] is True
    assert quote["source"] == "kucoin"
    assert quote["freshness"] == "LIVE"
    assert quote["age_seconds"] < 5


def test_live_prices_remote_false_never_fetches_missing_explicit_symbol(client, monkeypatch):
    from api.market_data_service import BinanceClient, KuCoinClient, YahooClient

    monkeypatch.setenv("MS_DISABLE_LIVE_PRICE_REMOTE", "0")
    monkeypatch.setattr("api.main.load_state_payload", lambda default: {"tickers": {}})
    unexpected = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected provider call"))
    monkeypatch.setattr(BinanceClient, "get_last_price", classmethod(unexpected))
    monkeypatch.setattr(KuCoinClient, "get_last_price", classmethod(unexpected))
    monkeypatch.setattr(YahooClient, "get_stock_data", classmethod(unexpected))

    response = client.get(
        "/api/live-prices",
        params={"symbols": "MISSING", "remote": "false"},
    )

    assert response.status_code == 200
    assert response.json()["prices"] == {}
    assert response.json()["source"] == "state_cache"
