import json
import os
import time
from pathlib import Path
from typing import Dict, Any, List

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_URL = os.getenv("UI_AUDIT_BASE", "http://127.0.0.1:8000")
OUT_DIR = Path("output/ui_audit")
OUT_DIR.mkdir(parents=True, exist_ok=True)

VIEWS = [
    {"name": "home", "hash": "home", "view": "#view-home", "ready": ".home-scanner-top"},
    {"name": "watchlist", "hash": "watchlist", "view": "#view-watchlist", "ready": "#detailed-watchlist-content"},
    {"name": "scanner", "hash": "scanner", "view": "#view-scanner", "ready": "#full-scanner-grid"},
    {"name": "news", "hash": "news", "view": "#view-news", "ready": "#news-page-feed-content"},
    {"name": "hotlist", "hash": "hotlist", "view": "#view-hotlist", "ready": "#hotlist-grid"},
    {"name": "backtest", "hash": "backtest", "view": "#view-backtest", "ready": "#backtest-form"},
    {"name": "calendar", "hash": "calendar", "view": "#view-calendar", "ready": "#calendar-months-wrapper"},
    {"name": "agents", "hash": "agents", "view": "#view-agents", "ready": ".agents-grid"},
    {"name": "alerts", "hash": "alerts", "view": "#view-alerts", "ready": "#alerts-list-container"},
    {"name": "capital", "hash": "capital", "view": "#view-capital", "ready": "#capital-history-body"},
]


def _visible(locator) -> bool:
    try:
        return locator.is_visible()
    except Exception:
        return False


def main():
    report: Dict[str, Any] = {
        "base_url": BASE_URL,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "views": [],
        "console_errors": [],
        "page_errors": [],
        "request_failures": [],
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        context.add_init_script("localStorage.clear(); sessionStorage.clear();")
        page = context.new_page()
        page.set_default_timeout(8000)
        page.set_default_navigation_timeout(15000)

        def on_console(msg):
            if msg.type == "error":
                report["console_errors"].append({"text": msg.text, "location": msg.location})

        def on_page_error(err):
            report["page_errors"].append(str(err))

        def on_request_failed(req):
            report["request_failures"].append({"url": req.url, "failure": req.failure})

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("requestfailed", on_request_failed)

        for view in VIEWS:
            item: Dict[str, Any] = {"name": view["name"], "hash": view["hash"], "ok": True, "notes": []}
            url = f"{BASE_URL}/#{view['hash']}"
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception as e:
                item["ok"] = False
                item["notes"].append(f"goto failed: {e}")

            # Wait for view section visible
            try:
                page.wait_for_selector(view["view"], state="visible", timeout=8000)
            except PWTimeout:
                item["ok"] = False
                item["notes"].append("view not visible")

            # Wait for ready selector (best-effort)
            if view.get("ready"):
                try:
                    page.wait_for_selector(view["ready"], timeout=8000)
                except PWTimeout:
                    item["notes"].append("ready selector not found")

            # Offline banner
            offline_visible = _visible(page.locator("#offline-banner"))
            if offline_visible:
                item["notes"].append("offline banner visible")

            # Placeholder texts within view
            try:
                placeholders = page.locator(f"{view['view']} .placeholder-text")
                count = placeholders.count()
                if count:
                    texts = [placeholders.nth(i).inner_text() for i in range(min(count, 6))]
                    item["placeholders"] = texts
            except Exception:
                pass

            # Screenshot
            shot_path = OUT_DIR / f"{view['name']}.png"
            try:
                page.screenshot(path=str(shot_path), full_page=True)
                item["screenshot"] = str(shot_path)
            except Exception as e:
                item["notes"].append(f"screenshot failed: {e}")

            report["views"].append(item)

        browser.close()

    report_path = OUT_DIR / "tabs_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"Report written to {report_path}")


if __name__ == "__main__":
    main()
