"""Debug script to inspect Capital state in browser."""
import os, time, json
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

        # Inject script to inspect cachedState
        state = page.evaluate("""() => {
            return {
                hasCachedState: typeof cachedState !== 'undefined',
                hasCapital: typeof cachedState !== 'undefined' && cachedState && !!cachedState.capital,
                capitalKeys: typeof cachedState !== 'undefined' && cachedState && cachedState.capital ? Object.keys(cachedState.capital) : [],
                balance: typeof cachedState !== 'undefined' && cachedState && cachedState.capital ? cachedState.capital.balance : null,
                positionsCount: typeof cachedState !== 'undefined' && cachedState && cachedState.capital && Array.isArray(cachedState.capital.positions) ? cachedState.capital.positions.length : 0,
                historyCount: typeof cachedState !== 'undefined' && cachedState && cachedState.capital && Array.isArray(cachedState.capital.history) ? cachedState.capital.history.length : 0
            };
        }""")
        print("Browser cachedState.capital:")
        print(json.dumps(state, indent=2))

        # Also check API directly
        import requests
        r = requests.get(f"{BASE_URL}/api/state", timeout=10)
        if r.status_code == 200:
            data = r.json()
            c = data.get("capital", {})
            print("\nAPI /api/state capital:")
            print(f"  balance: {c.get('balance')}")
            print(f"  positions: {len(c.get('positions', []))}")
            print(f"  history: {len(c.get('history', []))}")

        browser.close()

if __name__ == "__main__":
    main()
