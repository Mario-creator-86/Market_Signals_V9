"""Validate chart navigation, Long/Short measurement and persisted drawings."""

import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    artifact_dir = Path("output/playwright")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto("http://127.0.0.1:8003", wait_until="domcontentloaded", timeout=60_000)
        page.evaluate(
            """() => {
                for (const key of Object.keys(localStorage)) {
                    if (
                        key.startsWith('market-signals.chart-workspace.drawings.v1:BTC-USD:5m')
                        || key.startsWith('market-signals.chart-workspace.view.v1:BTC-USD:5m')
                    ) localStorage.removeItem(key);
                }
            }"""
        )
        page.evaluate("window.ChartWorkspace.open('BTCUSDT')")
        page.locator("#chart-workspace-modal").wait_for(state="visible", timeout=30_000)
        page.wait_for_function(
            "() => window.ChartWorkspace.getDrawingSnapshot().chartTotal > 0",
            timeout=60_000,
        )
        canvas = page.locator("#chart-workspace-canvas")
        box = canvas.bounding_box() or {}
        if not canvas.is_visible() or box.get("width", 0) < 800:
            raise RuntimeError("Chart canvas did not render at a usable width")
        legend = page.locator("#chart-workspace-legend").inner_text().strip()
        if not legend:
            raise RuntimeError("Chart legend is empty")

        # Cursor must remain useful in the blank future area to the right.
        page.mouse.move(box["x"] + box["width"] * 0.91, box["y"] + box["height"] * 0.45)
        page.locator("#chart-crosshair-tooltip").wait_for(state="visible", timeout=5_000)
        tooltip = page.locator("#chart-crosshair-tooltip").inner_text()
        if "Future chart space" not in tooltip:
            raise RuntimeError(f"Right-side future cursor space is unavailable: {tooltip!r}")

        # Draw a profitable Long measurement from a real candle into future space.
        page.locator('[data-chart-tool="long-position"]').click()
        page.mouse.move(box["x"] + box["width"] * 0.66, box["y"] + box["height"] * 0.58)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] * 0.90, box["y"] + box["height"] * 0.34, steps=12)
        page.mouse.up()
        snapshot = page.evaluate("window.ChartWorkspace.getDrawingSnapshot()")
        if len(snapshot["drawings"]) != 1 or snapshot["drawings"][0]["type"] != "long-position":
            raise RuntimeError(f"Long position drawing was not created: {snapshot!r}")
        result = snapshot["drawings"][0].get("result") or {}
        if float(result.get("pnl") or 0) <= 0 or float(result.get("returnPct") or 0) <= 0:
            raise RuntimeError(f"Long position profit calculation is invalid: {result!r}")
        if not snapshot.get("selectedDrawingId"):
            raise RuntimeError("New drawing selection was not retained")

        # Short uses the inverse return direction and must also report profit.
        page.locator('[data-chart-tool="short-position"]').click()
        page.mouse.move(box["x"] + box["width"] * 0.64, box["y"] + box["height"] * 0.34)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] * 0.88, box["y"] + box["height"] * 0.58, steps=12)
        page.mouse.up()
        snapshot = page.evaluate("window.ChartWorkspace.getDrawingSnapshot()")
        if len(snapshot["drawings"]) != 2 or snapshot["drawings"][1]["type"] != "short-position":
            raise RuntimeError(f"Short position drawing was not created: {snapshot!r}")
        short_result = snapshot["drawings"][1].get("result") or {}
        if float(short_result.get("pnl") or 0) <= 0 or float(short_result.get("returnPct") or 0) <= 0:
            raise RuntimeError(f"Short position profit calculation is invalid: {short_result!r}")

        # Reopen after a full reload: drawing, selection and viewport must survive.
        saved = snapshot
        page.reload(wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_function("() => Boolean(window.ChartWorkspace)", timeout=30_000)
        page.evaluate("window.ChartWorkspace.open('BTC-USD')")
        page.locator("#chart-workspace-modal").wait_for(state="visible", timeout=30_000)
        page.wait_for_function(
            "() => window.ChartWorkspace.getDrawingSnapshot().chartTotal > 0",
            timeout=60_000,
        )
        restored = page.evaluate("window.ChartWorkspace.getDrawingSnapshot()")
        if len(restored["drawings"]) != 2:
            raise RuntimeError(f"Drawing did not persist after reload: {restored!r}")
        if restored.get("selectedDrawingId") != saved.get("selectedDrawingId"):
            raise RuntimeError("Selected drawing did not persist after reload")
        if restored.get("viewStart") != saved.get("viewStart") or restored.get("viewCount") != saved.get("viewCount"):
            raise RuntimeError(f"Viewport did not persist: before={saved!r}, after={restored!r}")
        if page_errors:
            raise RuntimeError(f"Chart page errors: {page_errors!r}")

        page.screenshot(path=str(artifact_dir / "chart-workspace-btcusdt.png"), full_page=False)
        report = {
            "status": page.locator("#chart-workspace-status").inner_text(),
            "legend": legend,
            "right_cursor": tooltip,
            "long_position_result": result,
            "short_position_result": short_result,
            "persisted": {
                "drawings": len(restored["drawings"]),
                "selectedDrawingId": restored.get("selectedDrawingId"),
                "viewStart": restored.get("viewStart"),
                "viewCount": restored.get("viewCount"),
            },
            "page_errors": page_errors,
        }
        (artifact_dir / "chart-workspace-qa.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False))
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
