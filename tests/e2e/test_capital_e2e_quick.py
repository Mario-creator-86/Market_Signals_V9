"""Quick standalone Playwright check for Capital tab."""
import os, time
from playwright.sync_api import sync_playwright

BASE_URL = os.getenv("E2E_BASE_URL", f"http://127.0.0.1:{os.getenv('E2E_PORT', '8010')}")

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        print("Navigating...")
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        print(f"Loaded: {page.title()}")
        time.sleep(2)
        page.get_by_test_id("nav-capital").click()
        time.sleep(1)
        capital = page.locator("#view-capital")
        print(f"Capital visible: {capital.is_visible()}")
        print(f"Balance text: {page.locator('#capital-balance-display').text_content()}")
        print(f"Positions count: {page.locator('#capital-positions-count').text_content()}")
        browser.close()
        print("Done")

if __name__ == "__main__":
    main()
