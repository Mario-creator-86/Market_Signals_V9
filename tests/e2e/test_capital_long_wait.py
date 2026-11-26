"""Debug script with long wait for coreLoop."""
import os, time, json
from playwright.sync_api import sync_playwright

BASE_URL = os.getenv("E2E_BASE_URL", f"http://127.0.0.1:{os.getenv('E2E_PORT', '8010')}")

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})

        requests_log = []
        page.on("request", lambda req: requests_log.append(req.url) if "/api/" in req.url else None)

        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(15)

        print("=== Requests after 15s ===")
        for url in requests_log:
            print(url)

        print("\n=== State ===")
        state = page.evaluate("""() => {
            return {
                hasCapital: typeof cachedState !== 'undefined' && cachedState && !!cachedState.capital,
                balance: typeof cachedState !== 'undefined' && cachedState && cachedState.capital ? cachedState.capital.balance : null,
                positionsCount: typeof cachedState !== 'undefined' && cachedState && cachedState.capital && Array.isArray(cachedState.capital.positions) ? cachedState.capital.positions.length : 0,
            };
        }""")
        print(json.dumps(state, indent=2))

        browser.close()

if __name__ == "__main__":
    main()
