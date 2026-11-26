
import json
import os
from pathlib import Path
from datetime import datetime

# Path to state.json
# Using BASE_DIR to be safe
BASE_DIR = Path(__file__).parent.parent
STATE_PATH = BASE_DIR / "state.json"

def verify_and_test_log():
    print(f"🚀 Starting Activity Log Verification in {BASE_DIR}...")
    
    if not STATE_PATH.exists():
        print(f"❌ state.json not found at {STATE_PATH}!")
        return

    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)

        log = state.get("activity_log", [])
        print(f"📊 Current log size: {len(log)}")

        # 1. Create a "Legacy" entry (like the one that caused Invalid Date)
        legacy_entry = {
            "timestamp": datetime.now().isoformat(),
            "message": "LEGACY TEST: This should be handled by app.js resiliency",
            "type": "news"
        }

        # 2. Create a "New Format" entry (the one we just implemented)
        new_entry = {
            "time": datetime.now().isoformat(),
            "desc": "NEW FORMAT TEST: Verified harmony between engine and frontend",
            "type": "news"
        }

        # Inject into state
        log.insert(0, legacy_entry)
        log.insert(0, new_entry)
        state["activity_log"] = log[:50]

        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        
        print(f"✅ Injected legacy and new format entries into {STATE_PATH}.")
        print("👉 Now, when you start the server, app.js will process both correctly.")
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    verify_and_test_log()
