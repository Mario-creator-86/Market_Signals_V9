from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_watchlist_requests_stale_aware_remote_refresh():
    source = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

    refresh_start = source.index("UI.refreshWatchlistLivePrices = async function")
    refresh_end = source.index("// Honest trend line", refresh_start)
    refresh_source = source[refresh_start:refresh_end]

    assert "remote: true" in refresh_source
    assert "cacheFallback: true" in refresh_source
    assert "maxAgeSeconds: 45" in refresh_source
    assert "price_freshness" in source
    assert "wl-price-freshness" in source


def test_watchlist_navigation_triggers_immediate_price_refresh():
    source = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    navigation_start = source.index("if (viewId === 'watchlist')")
    navigation_source = source[navigation_start:navigation_start + 1200]

    assert "refreshWatchlistLivePrices" in navigation_source
    assert "force: true" in navigation_source
