"""Debug script with longer waits and state inspection."""
import os, time, json
from playwright.sync_api import sync_playwright

BASE_URL = os.getenv("E2E_BASE_URL", f"http://127.0.0.1:{os.getenv('E2E_PORT', '8010')}")

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        print(f"After load, title: {page.title()}")

        # Check initial state
        state = page.evaluate("""() => {
            return {
                hasCachedState: typeof cachedState !== 'undefined',
                hasCapital: typeof cachedState !== 'undefined' && cachedState && !!cachedState.capital,
                balance: typeof cachedState !== 'undefined' && cachedState && cachedState.capital ? cachedState.capital.balance : null,
            };
        }""")
        print(f"Initial state: {json.dumps(state)}")

        page.get_by_test_id("nav-capital").click()
        time.sleep(4)

        # Check state after nav
        state2 = page.evaluate("""() => {
            return {
                hasCachedState: typeof cachedState !== 'undefined',
                hasCapital: typeof cachedState !== 'undefined' && cachedState && !!cachedState.capital,
                balance: typeof cachedState !== 'undefined' && cachedState && cachedState.capital ? cachedState.capital.balance : null,
                positionsCount: typeof cachedState !== 'undefined' && cachedState && cachedState.capital && Array.isArray(cachedState.capital.positions) ? cachedState.capital.positions.length : 0,
            };
        }""")
        print(f"After nav state: {json.dumps(state2)}")

        # Check balance display
        balance_el = page.locator("#capital-balance-display")
        print(f"Balance display: {balance_el.text_content()}")

        # Screenshot
        page.screenshot(path="capital_debug2.png", full_page=False)
        print("Screenshot saved to capital_debug2.png")

        browser.close()

if __name__ == "__main__":
    main()
