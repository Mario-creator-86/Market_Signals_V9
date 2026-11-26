"""
Simplified Capital E2E tests using direct API calls + page reload.
This bypasses frontend state caching issues.
"""

import os
import time
import requests

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

if str(os.getenv("RUN_E2E", "0")).strip().lower() not in {"1", "true", "yes", "on"}:
    pytestmark = [pytest.mark.e2e, pytest.mark.skip(reason="E2E disabled (set RUN_E2E=1).")]

# Keep browser tests on the isolated proxy supplied by tests/e2e/conftest.py.
# Pointing the default to the live application port could reset a user's local
# Capital state while an E2E run is intended to use TEST_MODE storage.
BASE_URL = os.getenv("E2E_BASE_URL", f"http://127.0.0.1:{os.getenv('E2E_PORT', '8010')}")


def _reset_capital_api(amount: float = 10000.0):
    try:
        requests.post(
            f"{BASE_URL}/api/capital/reset",
            json={"amount": amount, "clear_positions": True},
            timeout=10,
        )
    except Exception:
        pass


def _open_position_api(ticker: str, direction: str = "long", leverage: int = 1, usd_size: float = 500):
    try:
        r = requests.post(
            f"{BASE_URL}/api/capital/open-position",
            json={
                "ticker": ticker,
                "direction": direction,
                "leverage": leverage,
                "usd_size": usd_size,
                "stop_loss_pct": 0.02,
                "take_profit_pct": 0.04,
                "reason": "E2E test",
                "strategy_type": "MANUAL",
            },
            timeout=15,
        )
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print(f"Open position error: {e}")
        return None


# ---------------------------------------------------------------------------
# Test: UI Theme Consistency
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_capital_ui_theme_colors(page: Page):
    """Verify Capital tab uses light theme."""
    _reset_capital_api(10000.0)
    page.goto(f"{BASE_URL}/#capital", wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)

    capital_view = page.locator("#view-capital")
    expect(capital_view).to_be_visible(timeout=10000)

    bg = capital_view.evaluate("el => getComputedStyle(el).backgroundColor")
    assert "rgb(" in bg, f"Expected rgb background, got {bg}"
    rgb_vals = bg.replace("rgba(", "").replace("rgb(", "").replace(")", "").split(",")
    r, g, b = [int(float(x.strip())) for x in rgb_vals[:3]]
    assert r >= 240 and g >= 240, f"Capital background too dark: {bg}"

    # Header text should be dark
    header = page.locator(".capital-page-header-bar").first
    if header.is_visible():
        color = header.evaluate("el => getComputedStyle(el).color")
        rgb_vals = color.replace("rgba(", "").replace("rgb(", "").replace(")", "").split(",")
        tr, tg, tb = [int(float(x.strip())) for x in rgb_vals[:3]]
        assert tr < 80 and tg < 80 and tb < 80, f"Header text not dark enough: {color}"


# ---------------------------------------------------------------------------
# Test: Open Position UI (pre-seeded via API)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_capital_open_position_riot(page: Page):
    """Open RIOT position via API, verify card in UI."""
    _reset_capital_api(10000.0)
    result = _open_position_api("RIOT", "long", 2, 500)
    assert result is not None, "Failed to open RIOT position via API"

    page.goto(f"{BASE_URL}/#capital", wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)

    # Card should appear
    card = page.locator(".capital-trade-card").filter(has_text="RIOT").first
    expect(card).to_be_visible(timeout=10000)

    # Verify direction badge
    expect(card.locator(".side-badge")).to_contain_text("LONG")

    # Verify leverage
    expect(card).to_contain_text("x2")

    # Verify SL / TP shown
    expect(card.locator(".cap-pos-grid")).to_contain_text("Stop")
    expect(card.locator(".cap-pos-grid")).to_contain_text("Target")

    # Portfolio equity should remain visible after margin is reserved.
    equity = page.locator("#capital-equity-value").text_content()
    assert equity != "$0.00", f"Portfolio equity should not be $0.00, got: {equity}"


@pytest.mark.smoke
def test_capital_open_position_btc_usd(page: Page):
    """Open BTC-USD position via API, verify card in UI."""
    _reset_capital_api(10000.0)
    result = _open_position_api("BTC-USD", "long", 1, 1000)
    assert result is not None, "Failed to open BTC-USD position via API"

    page.goto(f"{BASE_URL}/#capital", wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)

    card = page.locator(".capital-trade-card").filter(has_text="BTC-USD").first
    expect(card).to_be_visible(timeout=10000)
    expect(card.locator(".side-badge")).to_contain_text("LONG")


@pytest.mark.smoke
def test_capital_open_position_eth_usd_short(page: Page):
    """Open ETH-USD SHORT position via API, verify card in UI."""
    _reset_capital_api(10000.0)
    result = _open_position_api("ETH-USD", "short", 2, 800)
    assert result is not None, "Failed to open ETH-USD position via API"

    page.goto(f"{BASE_URL}/#capital", wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)

    card = page.locator(".capital-trade-card").filter(has_text="ETH-USD").first
    expect(card).to_be_visible(timeout=10000)
    expect(card.locator(".side-badge")).to_contain_text("SHORT")


# ---------------------------------------------------------------------------
# Test: Stats Reflect Positions
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_capital_stats_reflect_positions(page: Page):
    """Open 2 positions via API, verify count stat."""
    _reset_capital_api(10000.0)
    _open_position_api("RIOT", "long", 1, 300)
    _open_position_api("BTC-USD", "long", 1, 400)

    page.goto(f"{BASE_URL}/#capital", wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)

    expect(page.locator("#capital-positions-count")).to_have_text("2", timeout=10000)


# ---------------------------------------------------------------------------
# Test: Close Position via UI
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_capital_close_position(page: Page):
    """Open position via API, close via UI button."""
    _reset_capital_api(10000.0)
    result = _open_position_api("RIOT", "long", 1, 300)
    assert result is not None

    page.goto(f"{BASE_URL}/#capital", wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)

    card = page.locator(".capital-trade-card").filter(has_text="RIOT").first
    expect(card).to_be_visible(timeout=10000)

    # Click Close button
    card.locator(".btn-close-pos").click()

    # Wait for card to disappear
    expect(card).not_to_be_visible(timeout=10000)

    # History should show CLOSE_POSITION
    history = page.locator("#capital-history-body")
    expect(history).to_contain_text("CLOSE_POSITION", timeout=5000)


# ---------------------------------------------------------------------------
# Test: History Persistence
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_capital_history_persistence(page: Page):
    """Open + close via API, verify history table in UI."""
    _reset_capital_api(10000.0)
    _open_position_api("ETH-USD", "long", 1, 250)
    # Close the position via API
    try:
        r = requests.get(f"{BASE_URL}/api/state?lean=true", timeout=10)
        if r.status_code == 200:
            positions = r.json().get("capital", {}).get("positions", [])
            for pos in positions:
                if pos.get("ticker") == "ETH-USD" and pos.get("status") == "OPEN":
                    requests.post(
                        f"{BASE_URL}/api/capital/close-position",
                        json={"position_id": pos["position_id"]},
                        timeout=10,
                    )
                    break
    except Exception:
        pass

    page.goto(f"{BASE_URL}/#capital", wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)

    history = page.locator("#capital-history-body")
    expect(history).to_contain_text("OPEN_POSITION", timeout=5000)
    expect(history).to_contain_text("CLOSE_POSITION", timeout=5000)
    expect(history).to_contain_text("ETH-USD", timeout=5000)


# ---------------------------------------------------------------------------
# Test: Reset via UI
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_capital_reset(page: Page):
    """Reset capital via UI modal."""
    _reset_capital_api(5000.0)
    _open_position_api("RIOT", "long", 1, 200)

    page.goto(f"{BASE_URL}/#capital", wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)

    # Open reset modal
    page.get_by_test_id("capital-reset-button").click()
    expect(page.locator("#capital-reset-modal")).to_be_visible(timeout=5000)

    page.locator("#capital-reset-amount").fill("15000")
    page.get_by_test_id("capital-reset-confirm").click()

    # Balance should update
    expect(page.locator("#capital-equity-value")).to_contain_text("$15000.00", timeout=10000)

    # Positions should be cleared
    expect(page.locator(".capital-trade-cards")).to_contain_text("No open positions.", timeout=5000)


# ---------------------------------------------------------------------------
# Test: Validation Errors
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_capital_validation_errors(page: Page):
    """Try to open position without ticker, verify error toast."""
    _reset_capital_api(10000.0)
    page.goto(f"{BASE_URL}/#capital", wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)

    page.locator("#capital-open-ticker").fill("")
    page.get_by_test_id("capital-open-submit").click()

    toast = page.locator("#scanner-toast")
    expect(toast).to_contain_text("Ticker is required", timeout=5000)
