
import pytest
import re
from playwright.sync_api import Page, expect, TimeoutError as PlaywrightTimeoutError
import time


def _goto_home_with_retry(page: Page, base_url: str, attempts: int = 3) -> None:
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=45000)
            return
        except PlaywrightTimeoutError as exc:
            last_error = exc
            page.wait_for_timeout(500 * attempt)
    if last_error is not None:
        raise last_error


def test_navigation(page: Page, base_url: str):
    """
    Test navigation between main views using the menu.
    """
    _goto_home_with_retry(page, base_url)
    
    # Go to Watchlist
    page.get_by_test_id("nav-watchlist").click()
    expect(page.locator("#view-watchlist")).to_be_visible()
    expect(page.locator("#view-home")).to_be_hidden() # Verify home is hidden or logic works
    
    # Go to Backtest
    page.get_by_test_id("nav-backtest").click()
    expect(page.locator("#view-backtest")).to_be_visible()
    
    # Go to Agents
    page.get_by_test_id("nav-agents").click()
    expect(page.locator("#view-agents")).to_be_visible()

    # Go to Premarket
    page.get_by_test_id("nav-premarket").click()
    expect(page.locator("#view-premarket")).to_be_visible()
    
    # Return Home
    page.get_by_test_id("nav-home").click()
    expect(page.locator("#view-home")).to_be_visible()

def test_backtest_flow(page: Page, base_url: str):
    """
    Critical Flow: configure a backtest and run it.
    """
    requests = []

    def backtest_response(route):
        payload = route.request.post_data_json
        requests.append(payload)
        route.fulfill(json={
            "mode": "comparison",
            "ticker": payload["tickers"][0],
            "days": payload["days"],
            "results": [{
                "risk_mode": "MEDIUM", "trade_mode": "PULLBACK",
                "wins": 0, "losses": 0, "win_rate": 0.0,
                "total_signals": 0, "expectancy_r": 0.0, "trades": [],
            }],
        })

    # This browser test validates the UI contract. Real backtest accounting,
    # OOS and provider paths have dedicated backend suites; launching three
    # remote-data backtests here leaked work into the following E2E test.
    page.route("**/api/backtest/run", backtest_response)
    _goto_home_with_retry(page, base_url)
    
    # Navigate to Backtest
    page.get_by_test_id("nav-backtest").click()
    
    # Input Ticker
    ticker_input = page.get_by_test_id("bt-ticker-input")
    ticker_input.fill("BTC-USD")
    
    # Select Risk (Optional, default is ok)
    # page.select_option("#bt-risk", "MEDIUM")
    
    # Click Run
    page.get_by_test_id("bt-run-btn").click()
    
    # Verify we see loading state or result
    # Assuming the app shows a loader or changes the results area
    # We wait for the 'placeholder-text' to disappear or specific result to appear
    # Using a timeout because backtest might take a few seconds
    
    # The current implementation might just show an alert or update the div. 
    # Let's check if the results container gets content.
    
    results_area = page.get_by_test_id("backtest-results-area")
    
    expect(results_area).to_contain_text("Top Strategy", timeout=15000)
    assert requests == [{
        "tickers": ["BTC-USD"], "days": 30, "compare_all": True,
        "variant_mode": "STRICT", "custom_tp_pct": None, "custom_sl_pct": None,
    }]


def test_strict_oos_audit_view_loads_all_backtest_tabs(page: Page, base_url: str):
    """The customer-facing OOS audit must work with or without an artifact."""
    _goto_home_with_retry(page, base_url)
    page.get_by_test_id("nav-backtest").click()
    page.get_by_test_id("bt-oos-audit-btn").click()

    overview = page.get_by_test_id("backtest-results-area")
    expect(overview).to_contain_text(
        re.compile(r"Strict out-of-sample audit|No reproducible OOS report", re.I),
        timeout=15000,
    )

    page.locator("#bt-tab-btn-strategies").click()
    expect(page.locator("#bt-tab-strategies")).to_be_visible()
    page.locator("#bt-tab-btn-trades").click()
    expect(page.locator("#bt-tab-trades")).to_be_visible()

    page.get_by_test_id("bt-research-ledger-btn").click()
    expect(overview).to_contain_text(
        re.compile(r"Pre-registered research ledger|No reproducible pre-registered crypto research report", re.I),
        timeout=15000,
    )

def test_agents_view_loads(page: Page, base_url: str):
    """
    Verify agents view loads and shows cards (or loader).
    """
    _goto_home_with_retry(page, base_url)
    page.get_by_test_id("nav-agents").click()
    
    # Expect at least the loader or cards
    expect(page.locator(".agents-grid")).to_be_visible()
