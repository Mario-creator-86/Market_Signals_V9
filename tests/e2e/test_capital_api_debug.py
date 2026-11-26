"""Debug script to inspect Capital API and state response."""
import os, time, json, requests

BASE_URL = os.getenv("E2E_BASE_URL", f"http://127.0.0.1:{os.getenv('E2E_PORT', '8010')}")

def main():
    # Test /api/state with lean=true (default for frontend)
    print("=== /api/state?lean=true ===")
    r = requests.get(f"{BASE_URL}/api/state?lean=true&unchanged=true", timeout=30)
    print(f"Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"Keys: {list(data.keys())}")
        print(f"Has capital: {'capital' in data}")
        if 'capital' in data:
            c = data['capital']
            print(f"  balance: {c.get('balance')}")
            print(f"  positions: {len(c.get('positions', []))}")
            print(f"  history: {len(c.get('history', []))}")

    # Test /api/state with lean=false (capital tab uses this)
    print("\n=== /api/state?lean=false ===")
    r2 = requests.get(f"{BASE_URL}/api/state?lean=false&unchanged=true", timeout=30)
    print(f"Status: {r2.status_code}")
    if r2.status_code == 200:
        data2 = r2.json()
        print(f"Keys: {list(data2.keys())}")
        print(f"Has capital: {'capital' in data2}")
        if 'capital' in data2:
            c = data2['capital']
            print(f"  balance: {c.get('balance')}")
            print(f"  positions: {len(c.get('positions', []))}")
            print(f"  history: {len(c.get('history', []))}")

    # Reset capital via API
    print("\n=== Reset capital ===")
    r3 = requests.post(f"{BASE_URL}/api/capital/reset", json={"amount": 10000, "clear_positions": True}, timeout=10)
    print(f"Status: {r3.status_code}")
    if r3.status_code == 200:
        print(f"Response: {r3.json()}")

    # Open position via API
    print("\n=== Open position RIOT ===")
    r4 = requests.post(f"{BASE_URL}/api/capital/open-position", json={
        "ticker": "RIOT",
        "direction": "long",
        "leverage": 2,
        "usd_size": 500,
        "stop_loss_pct": 0.03,
        "take_profit_pct": 0.06,
        "reason": "E2E test",
        "strategy_type": "MANUAL"
    }, timeout=10)
    print(f"Status: {r4.status_code}")
    if r4.status_code == 200:
        print(f"Response keys: {list(r4.json().keys())}")

    # Check state again
    print("\n=== State after open ===")
    r5 = requests.get(f"{BASE_URL}/api/state?lean=false&unchanged=true", timeout=30)
    if r5.status_code == 200:
        data5 = r5.json()
        c = data5.get('capital', {})
        print(f"  balance: {c.get('balance')}")
        print(f"  positions: {len(c.get('positions', []))}")
        for p in c.get('positions', []):
            print(f"    - {p.get('ticker')} {p.get('direction')} {p.get('status')}")

if __name__ == "__main__":
    main()
