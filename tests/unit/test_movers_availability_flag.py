"""tests/unit/test_movers_availability_flag.py
=============================================
F10: ``KuCoinClient.get_top_movers`` must NOT silently return an empty-looking
list when the source is stale/unavailable. The response now carries an explicit
``available: False`` flag (+ ``stale: True`` + a human ``reason``) so the UI can
show "movers unavailable" instead of rendering an empty-but-looks-live list.

Backward compatible: a fresh success still has ``gainers``/``losers`` lists the
existing callers read; the new keys are additive.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import os
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from api.market_data_service import KuCoinClient


def _reset_movers_cache():
    with KuCoinClient._cache_lock:
        KuCoinClient._movers_cache.pop("all", None)


def _seed_stale_cache(data, age_seconds):
    with KuCoinClient._cache_lock:
        KuCoinClient._movers_cache["all"] = {
            "data": data,
            "ts": datetime.now() - timedelta(seconds=age_seconds),
        }


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def test_fresh_success_marks_available_true_and_stale_false(monkeypatch):
    """A live KuCoin response returns the movers list with available=True,
    stale=False (the happy path -- the new flags are additive)."""
    _reset_movers_cache()
    payload = {
        "code": "200000",
        "data": {"ticker": [
            {"symbol": "BTC-USDT", "last": "100000", "changeRate": "0.05", "volValue": "999999"},
            {"symbol": "ETH-USDT", "last": "3000", "changeRate": "-0.03", "volValue": "888888"},
            {"symbol": "SOL-USDT", "last": "200", "changeRate": "0.02", "volValue": "777777"},
        ]},
    }
    monkeypatch.setattr(
        KuCoinClient, "BASE_URL", "https://fake-kucoin.test/api/v1"
    )
    import api.market_data_service as mds
    monkeypatch.setattr(
        mds, "http_get",
        lambda url, **kw: _Resp(payload),
    )
    res = KuCoinClient.get_top_movers(limit=2, ttl=60)
    assert res["available"] is True
    assert res["stale"] is False
    assert len(res["gainers"]) == 2
    assert len(res["losers"]) >= 1
    _reset_movers_cache()


def test_unavailable_no_cache_returns_available_false_with_reason(monkeypatch):
    """Source unreachable, no stale cache -> empty list + available=False +
    stale=False + a human reason (NOT a silent empty-but-looks-live list)."""
    _reset_movers_cache()
    import api.market_data_service as mds
    monkeypatch.setattr(mds, "http_get", lambda url, **kw: None)
    res = KuCoinClient.get_top_movers(limit=5, ttl=60)
    assert res["available"] is False
    assert res["stale"] is False
    assert res["gainers"] == [] and res["losers"] == []
    assert "reason" in res and isinstance(res["reason"], str) and res["reason"]
    _reset_movers_cache()


def test_unavailable_with_fresh_stale_returns_stale_flagged_unavailable(monkeypatch):
    """Source unreachable but a recent stale cache exists -> return the stale
    data flagged available=False, stale=True + reason. UI can show 'movers
    unavailable, showing cached data' instead of an empty list."""
    stale_data = {"gainers": [{"symbol": "FOO-USDT", "price": 1.0,
                               "change_pct": 12.3, "volume": 100000}],
                  "losers": [{"symbol": "BAR-USDT", "price": 2.0,
                              "change_pct": -8.1, "volume": 100000}]}
    _seed_stale_cache(stale_data, age_seconds=120)  # within 10-min max-age
    import api.market_data_service as mds
    monkeypatch.setattr(mds, "http_get", lambda url, **kw: None)
    res = KuCoinClient.get_top_movers(limit=5, ttl=60)
    assert res["available"] is False
    assert res["stale"] is True
    assert res["gainers"] == stale_data["gainers"]  # stale data is returned
    assert "reason" in res and "cached" in res["reason"].lower()
    _reset_movers_cache()


def test_unavailable_with_too_old_stale_drops_cache_and_returns_empty(monkeypatch):
    """A stale cache older than _STALE_MAX_AGE_SECONDS is NOT returned (it would
    mislead the UI into rendering a stale list as live). Empty fallback with
    available=False, stale=False."""
    stale_data = {"gainers": [{"symbol": "OLD-USDT"}], "losers": []}
    _seed_stale_cache(stale_data, age_seconds=KuCoinClient._STALE_MAX_AGE_SECONDS + 60)
    import api.market_data_service as mds
    monkeypatch.setattr(mds, "http_get", lambda url, **kw: None)
    res = KuCoinClient.get_top_movers(limit=5, ttl=60)
    assert res["available"] is False
    assert res["stale"] is False
    assert res["gainers"] == []  # too-old stale dropped, not surfaced as live
    _reset_movers_cache()


def test_non200_code_returns_unavailable_with_reason(monkeypatch):
    """A non-200 code from KuCoin returns the unavailable flag (no silent
    empty list that looks live)."""
    _reset_movers_cache()
    payload = {"code": "400001", "data": {"ticker": []}}
    import api.market_data_service as mds
    monkeypatch.setattr(mds, "http_get", lambda url, **kw: _Resp(payload))
    res = KuCoinClient.get_top_movers(limit=5, ttl=60)
    assert res["available"] is False
    assert res["stale"] is False
    assert "reason" in res and res["reason"]
    _reset_movers_cache()


def test_cache_hit_marks_available_true(monkeypatch):
    """A cache hit within TTL returns available=True, stale=False (fast path
    also carries the flags for a consistent contract)."""
    _reset_movers_cache()
    payload = {"code": "200000", "data": {"ticker": [
        {"symbol": "X-USDT", "last": "1", "changeRate": "0.1", "volValue": "999999"},
    ]}}
    import api.market_data_service as mds
    monkeypatch.setattr(mds, "http_get", lambda url, **kw: _Resp(payload))
    # First call populates cache.
    first = KuCoinClient.get_top_movers(limit=1, ttl=60)
    assert first["available"] is True
    # Second call hits the cache (http_get not called again -- if it were, the
    # test would still pass, but the point is the cache path carries flags).
    second = KuCoinClient.get_top_movers(limit=1, ttl=60)
    assert second["available"] is True
    assert second["stale"] is False
    _reset_movers_cache()