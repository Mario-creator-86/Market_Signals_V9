"""Debug script to inspect network requests for capital state."""
import os, time, json
from playwright.sync_api import sync_playwright

BASE_URL = os.getenv("E2E_BASE_URL", f"http://127.0.0.1:{os.getenv('E2E_PORT', '8010')}")

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})

        state_responses = []
        page.on("response", lambda response: handle_response(response, state_responses))

        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)

        print("=== After page load ===")
        state = page.evaluate("""() => {
            return {
                hasCachedState: typeof cachedState !== 'undefined',
                hasCapital: typeof cachedState !== 'undefined' && cachedState && !!cachedState.capital,
                balance: typeof cachedState !== 'undefined' && cachedState && cachedState.capital ? cachedState.capital.balance : null,
            };
        }""")
        print(json.dumps(state, indent=2))
        print(f"Network responses (state): {json.dumps(state_responses, indent=2)}")

        # Click capital
        page.get_by_test_id("nav-capital").click()
        time.sleep(5)

        print("\n=== After capital nav ===")
        state2 = page.evaluate("""() => {
            return {
                hasCachedState: typeof cachedState !== 'undefined',
                hasCapital: typeof cachedState !== 'undefined' && cachedState && !!cachedState.capital,
                balance: typeof cachedState !== 'undefined' && cachedState && cachedState.capital ? cachedState.capital.balance : null,
                positionsCount: typeof cachedState !== 'undefined' && cachedState && cachedState.capital && Array.isArray(cachedState.capital.positions) ? cachedState.capital.positions.length : 0,
            };
        }""")
        print(json.dumps(state2, indent=2))
        print(f"Network responses (state): {json.dumps(state_responses, indent=2)}")

        browser.close()

def handle_response(response, state_responses):
    if "/api/state" in response.url:
        try:
            body = response.json()
            state_responses.append({
                "url": response.url,
                "has_capital": "capital" in body,
                "keys": list(body.keys())[:15],
                "balance": body.get("capital", {}).get("balance") if "capital" in body else None,
            })
        except Exception:
            pass

if __name__ == "__main__":
    main()
