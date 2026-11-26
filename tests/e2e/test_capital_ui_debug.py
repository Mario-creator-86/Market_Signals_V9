"""Debug script to open a position via UI and capture screenshot + console."""
import os, time
from playwright.sync_api import sync_playwright

BASE_URL = os.getenv("E2E_BASE_URL", f"http://127.0.0.1:{os.getenv('E2E_PORT', '8010')}")

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        page.get_by_test_id("nav-capital").click()
        time.sleep(2)

        # Fill form
        page.locator("#capital-open-ticker").fill("RIOT")
        page.locator("#capital-open-direction").select_option("long")
        page.locator("#capital-open-leverage").fill("2")
        page.locator("#capital-open-size").fill("500")
        page.locator("#capital-open-stop-pct").fill("3.0")
        page.locator("#capital-open-target-pct").fill("6.0")
        page.locator("#capital-open-reason").fill("Debug test")

        # Click open
        page.locator(".capital-terminal button:has-text('Execută')").click()
        time.sleep(3)

        # Check for toast
        toast = page.locator("#scanner-toast")
        print(f"Toast visible: {toast.is_visible()}")
        if toast.is_visible():
            print(f"Toast text: {toast.text_content()}")

        # Check cards
        cards = page.locator(".capital-trade-card")
        print(f"Card count: {cards.count()}")
        if cards.count() > 0:
            print(f"First card text: {cards.first.text_content()[:200]}")

        # Check balance
        balance = page.locator("#capital-balance-display").text_content()
        print(f"Balance: {balance}")

        # Screenshot
        page.screenshot(path="capital_debug.png", full_page=False)
        print("Screenshot saved to capital_debug.png")

        # Console logs
        logs = page.evaluate("() => { return window._consoleLogs || []; }")
        print(f"Console logs: {logs}")

        browser.close()

if __name__ == "__main__":
    main()
