import requests
import time
from playwright.sync_api import Page, expect


def _get_hotlist_tickers(base_url: str) -> list[str]:
    try:
        response = requests.get(f"{base_url}/api/watchlist", timeout=8)
        response.raise_for_status()
        data = response.json() or {}
        hot = data.get("hot_list") or data.get("hotlist") or []
        return [str(t or "").upper() for t in hot if str(t or "").strip()]
    except Exception:
        response = requests.get(
            f"{base_url}/api/state?include_news=false&include_hotlist=false&hotlist_details=false&lean=true",
            timeout=20,
        )
        response.raise_for_status()
        data = response.json() or {}
        watchlist = data.get("watchlist") or {}
        return [str(t or "").upper() for t in (watchlist.get("hot_list") or []) if str(t or "").strip()]


def _toggle_hotlist(base_url: str, ticker: str) -> bool:
    response = requests.post(
        f"{base_url}/api/watchlist/hotlist/toggle",
        json={"ticker": ticker},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json() or {}
    return bool(payload.get("is_in"))


def _set_hotlist_membership(base_url: str, ticker: str, should_be_in: bool, retries: int = 6) -> None:
    target = bool(should_be_in)
    for _ in range(retries):
        current = ticker in _get_hotlist_tickers(base_url)
        if current == target:
            return
        _toggle_hotlist(base_url, ticker)
        time.sleep(0.35)
    final_state = ticker in _get_hotlist_tickers(base_url)
    assert final_state == target, f"Failed to set hotlist membership for {ticker} -> {target}"


def test_hotlist_flow_smoke(page: Page, base_url: str):
    # Ensure at least one known ticker is present so the grid has a card.
    baseline_hot = _get_hotlist_tickers(base_url)
    if "AAPL" not in baseline_hot:
        _set_hotlist_membership(base_url, "AAPL", True)

    page.goto(base_url, wait_until="domcontentloaded")
    page.get_by_test_id("nav-hotlist").click()
    expect(page.locator("#view-hotlist")).to_be_visible()
    expect(page.locator("#hotlist-grid")).to_be_visible()

    page.wait_for_selector("#hotlist-grid .hotlist-card", timeout=20000)

    first_card = page.locator("#hotlist-grid .hotlist-card").first
    ticker = (first_card.locator(".hot-card-symbol").first.text_content() or "").strip().upper()
    assert ticker

    # Details load action should be clickable and not break the page.
    details_btn = page.locator("#hotlist-load-details-btn")
    if details_btn.count() > 0:
        try:
            details_btn.click(timeout=3000)
            page.wait_for_timeout(2000)
        except Exception:
            # Accept if already disabled/loaded; the goal is no runtime crash.
            pass

    # Open/close detail modal.
    first_card.click()
    expect(page.locator("#hotlist-detail-modal")).to_be_visible(timeout=10000)
    page.locator("#hotlist-detail-modal .btn-modal-close").first.click()
    expect(page.locator("#hotlist-detail-modal")).to_be_hidden(timeout=10000)

    # Toggle from card remove button, then restore the same ticker.
    first_card.locator(".btn-hot-remove.top-right").first.click()
    page.wait_for_timeout(1500)

    _set_hotlist_membership(base_url, ticker, True)
