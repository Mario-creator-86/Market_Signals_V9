"""Tests for the live scanner-backed hotlist endpoints (F1).

These cover the 4 previously-dead endpoints:
  - GET /api/hotlist
  - GET /api/hotlist/{category}
  - GET /api/hotlist/search
  - GET /api/hotlist/trending

The legacy HotlistModule._load_from_disk read `change_pct`/`volume` but the
on-disk cache stores `gap_pct` (different schema) → KeyError → cache stayed
empty → endpoints returned {"count":0,"data":[]}. The fix rewires them to
the live scanner agent. These tests monkeypatch the scanner agent so they are
deterministic (no network).
"""
import time
from unittest.mock import patch


def _scanner_state(hot_tickers, refreshing=False):
    return {
        "hot_tickers": hot_tickers,
        "refreshing": refreshing,
        "status": {"state": "OK"},
        "flags": {"refreshing": refreshing, "data_available": bool(hot_tickers)},
        "data_age_seconds": 5,
        "as_of_utc": "2026-07-03T00:00:00+00:00",
        "refresh_seconds": 300,
    }


HOT_TICKERS = [
    {"symbol": "AAPL", "change_pct": 1.5, "price": 220.0, "name": "Apple Inc.", "volume": 1_000_000, "source": "yahoo_gainers"},
    {"symbol": "NVDA", "change_pct": 3.2, "price": 100.0, "name": "NVIDIA", "volume": 2_000_000, "source": "yahoo_gainers"},
    {"symbol": "TSLA", "change_pct": -2.1, "price": 250.0, "name": "Tesla", "volume": 1_500_000, "source": "yahoo_losers"},
    {"symbol": "MSFT", "change_pct": -0.5, "price": 400.0, "name": "Microsoft", "volume": 800_000, "source": "yahoo_losers"},
    {"symbol": "AMZN", "change_pct": 0.8, "price": 180.0, "name": "Amazon", "volume": 3_000_000, "source": "yahoo_active"},
    {"symbol": "META", "change_pct": 1.1, "price": 500.0, "name": "Meta Platforms", "volume": 900_000, "source": "yahoo_active"},
    {"symbol": "BTC-USD", "change_pct": 4.5, "price": 60000.0, "volume": 5_000_000, "source": "binance"},
]


def _patch_scanner(hot_tickers=HOT_TICKERS, refreshing=False):
    return patch("agents.scanner_agent.get_scanner_agent",
                 lambda *a, **kw: type("A", (), {"get_state": lambda self: _scanner_state(hot_tickers, refreshing)})())


def test_hotlist_categories_returns_live_data(client):
    with _patch_scanner():
        r = client.get("/api/hotlist")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "success"
    assert data["available"] is True
    assert data["source"] == "scanner"
    cats = data["data"]
    # Supported categories present
    for cat in ("gainers", "losers", "most_active", "trending", "volume_leaders"):
        assert cat in cats, f"missing category {cat}"
    # Gainers filtered to yahoo_gainers source only (AAPL, NVDA)
    gainers = cats["gainers"]
    tickers = [g["ticker"] for g in gainers]
    assert "AAPL" in tickers
    assert "NVDA" in tickers
    assert "TSLA" not in tickers  # TSLA is a loser
    # Each item has the HotlistItem.to_dict() shape
    item = gainers[0]
    for key in ("ticker", "name", "price", "change_pct", "volume", "category", "score", "timestamp", "updated", "source"):
        assert key in item, f"missing key {key}"


def test_hotlist_category_gainers(client):
    with _patch_scanner():
        r = client.get("/api/hotlist/gainers")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "success"
    assert data["category"] == "gainers"
    assert data["available"] is True
    assert data["count"] == 2
    tickers = [d["ticker"] for d in data["data"]]
    assert set(tickers) == {"AAPL", "NVDA"}


def test_hotlist_category_losers_sorted_by_score(client):
    with _patch_scanner():
        r = client.get("/api/hotlist/losers")
    assert r.status_code == 200
    data = r.json()
    # losers: TSLA (-2.1, score 2.1) before MSFT (-0.5, score 0.5)
    assert [d["ticker"] for d in data["data"]] == ["TSLA", "MSFT"]


def test_hotlist_category_volume_leaders_sorted_by_volume(client):
    with _patch_scanner():
        r = client.get("/api/hotlist/volume_leaders")
    assert r.status_code == 200
    data = r.json()
    vols = [d["volume"] for d in data["data"]]
    assert vols == sorted(vols, reverse=True)


def test_hotlist_category_trending_sorted_by_abs_change(client):
    with _patch_scanner():
        r = client.get("/api/hotlist/trending")
    assert r.status_code == 200
    data = r.json()
    changes = [abs(d["change_pct"]) for d in data["data"]]
    assert changes == sorted(changes, reverse=True)


def test_hotlist_category_invalid_returns_400(client):
    with _patch_scanner():
        r = client.get("/api/hotlist/bogus")
    assert r.status_code == 400


def test_hotlist_category_unsupported_marks_unavailable(client):
    with _patch_scanner():
        r = client.get("/api/hotlist/premarket")
    assert r.status_code == 200
    data = r.json()
    assert data["available"] is False
    assert data["reason"] == "no_live_source"
    assert data["count"] == 0
    assert data["data"] == []


def test_hotlist_search_filters_case_insensitive(client):
    with _patch_scanner():
        r = client.get("/api/hotlist/search", params={"query": "tsla"})
    assert r.status_code == 200
    data = r.json()
    assert data["query"] == "tsla"
    tickers = [d["ticker"] for d in data["data"]]
    assert tickers == ["TSLA"]


def test_hotlist_search_matches_name(client):
    with _patch_scanner():
        r = client.get("/api/hotlist/search", params={"query": "apple"})
    assert r.status_code == 200
    data = r.json()
    tickers = [d["ticker"] for d in data["data"]]
    assert "AAPL" in tickers


def test_hotlist_search_empty_query_returns_empty(client):
    with _patch_scanner():
        r = client.get("/api/hotlist/search", params={"query": "   "})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 0
    assert data["data"] == []


def test_hotlist_warming_up_when_scanner_empty(client):
    with _patch_scanner(hot_tickers=[]):
        r = client.get("/api/hotlist/gainers")
    assert r.status_code == 200
    data = r.json()
    assert data["available"] is False
    assert data["reason"] == "scanner_warming_up"
    assert data["count"] == 0


def test_hotlist_categories_warming_up_when_scanner_empty(client):
    with _patch_scanner(hot_tickers=[]):
        r = client.get("/api/hotlist")
    assert r.status_code == 200
    data = r.json()
    assert data["available"] is False
    assert data["reason"] == "scanner_warming_up"
    # All category lists present but empty
    assert all(v == [] for v in data["data"].values())


def test_hotlist_category_min_volume_filter(client):
    with _patch_scanner():
        r = client.get("/api/hotlist/most_active", params={"min_volume": 2_000_000})
    assert r.status_code == 200
    data = r.json()
    # most_active yahoo_active source: AMZN (3M), META (900k) — only AMZN passes
    tickers = [d["ticker"] for d in data["data"]]
    assert tickers == ["AMZN"]


def test_hotlist_category_limit_caps_results(client):
    with _patch_scanner():
        r = client.get("/api/hotlist/trending", params={"limit": 2})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 2