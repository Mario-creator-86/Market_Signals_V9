"""Debug script to inspect ALL network traffic."""
import os, time, json
from playwright.sync_api import sync_playwright

BASE_URL = os.getenv("E2E_BASE_URL", f"http://127.0.0.1:{os.getenv('E2E_PORT', '8010')}")

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})

        requests_log = []
        responses_log = []
        console_log = []

        page.on("request", lambda req: requests_log.append(req.url) if "/api/" in req.url else None)
        page.on("response", lambda resp: responses_log.append({"url": resp.url, "status": resp.status}) if "/api/" in resp.url else None)
        page.on("console", lambda msg: console_log.append(f"[{msg.type}] {msg.text}"))

        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(8)

        print("=== Requests ===")
        for url in requests_log:
            print(url)
        print("\n=== Responses ===")
        for r in responses_log:
            print(f"{r['status']} {r['url'][:120]}")
        print("\n=== Console ===")
        for c in console_log[:20]:
            print(c)

        print("\n=== State ===")
        state = page.evaluate("""() => {
            return {
                hasCachedState: typeof cachedState !== 'undefined',
                hasCapital: typeof cachedState !== 'undefined' && cachedState && !!cachedState.capital,
                balance: typeof cachedState !== 'undefined' && cachedState && cachedState.capital ? cachedState.capital.balance : null,
            };
        }""")
        print(json.dumps(state, indent=2))

        browser.close()

if __name__ == "__main__":
    main()
