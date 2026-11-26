from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


BASE_URL = os.getenv("MARKET_SIGNALS_BASE_URL", "http://127.0.0.1:8003").rstrip("/")
OUTPUT_DIR = Path(os.getenv("MARKET_SIGNALS_PLAYWRIGHT_OUTPUT", "output/playwright"))
CAPITAL_PATH = Path(os.getenv("MARKET_SIGNALS_CAPITAL_PATH", "capital.json"))


def api_json(path: str, payload: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
    url = f"{BASE_URL}{path}"
    data = None
    headers = {"Accept": "application/json"}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = res.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} failed HTTP {exc.code}: {body}") from exc


def get_capital_state() -> dict[str, Any]:
    state = api_json("/api/state?lean=true&include_news=false&include_hotlist=false", timeout=45.0)
    capital = state.get("capital") if isinstance(state, dict) else {}
    return capital if isinstance(capital, dict) else {}


def format_price(value: Any) -> str:
    num = float(value or 0.0)
    return f"{num:.4f}" if abs(num) < 1 else f"{num:.2f}"


def visible_text(page: Page, selector: str) -> str:
    return (page.locator(selector).first.inner_text(timeout=10_000) or "").strip()


def wait_for_capital(page: Page) -> None:
    page.get_by_test_id("nav-capital").click(timeout=12_000)
    page.wait_for_function(
        """
        () => {
          const el = document.querySelector('#view-capital');
          return el && !el.classList.contains('hidden');
        }
        """,
        timeout=20_000,
    )
    page.wait_for_selector("#capital-balance-display", timeout=20_000)
    page.wait_for_function(
        """
        () => window.Actions
          && typeof window.Actions.openCapitalPosition === 'function'
        """,
        timeout=45_000,
    )


def goto_app(page: Page) -> None:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=120_000)
            return
        except PlaywrightTimeoutError as exc:
            last_error = exc
            try:
                page.goto("about:blank", wait_until="commit", timeout=10_000)
            except Exception:
                pass
            page.wait_for_timeout(1_000 * attempt)
    try:
        page.goto(BASE_URL, wait_until="commit", timeout=120_000)
        return
    except PlaywrightTimeoutError as exc:
        last_error = exc
    if last_error:
        raise last_error


def fill_order_terminal(page: Page) -> None:
    page.locator("#capital-open-ticker").fill("AAPL", timeout=10_000)
    page.locator("#capital-open-direction").select_option("long", timeout=10_000)
    page.locator("#capital-open-leverage").fill("1", timeout=10_000)
    page.locator("#capital-open-size").fill("100", timeout=10_000)
    page.locator("#capital-open-stop-pct").fill("2", timeout=10_000)
    page.locator("#capital-open-target-pct").fill("4", timeout=10_000)
    page.locator("#capital-open-reason").fill("Playwright capital roundtrip", timeout=10_000)


def restore_capital_file(original_bytes: bytes | None) -> None:
    target = CAPITAL_PATH.resolve()
    if original_bytes is None:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        return
    tmp = target.with_suffix(target.suffix + ".roundtrip_restore.tmp")
    tmp.write_bytes(original_bytes)
    os.replace(tmp, target)


def run() -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    capital_file = CAPITAL_PATH.resolve()
    original_bytes = capital_file.read_bytes() if capital_file.exists() else None
    backup_path = OUTPUT_DIR / "capital_roundtrip_backup.json"
    if original_bytes is not None:
        backup_path.write_bytes(original_bytes)

    summary: dict[str, Any] = {
        "base_url": BASE_URL,
        "capital_file": str(capital_file),
        "backup": str(backup_path.resolve()) if original_bytes is not None else None,
        "screenshots": {},
    }

    try:
        initial_state = get_capital_state()
        summary["initial"] = {
            "balance": initial_state.get("balance"),
            "open_positions": len([
                pos for pos in initial_state.get("positions", [])
                if str(pos.get("status", "")).upper() == "OPEN"
            ]),
        }

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
            page = context.new_page()
            console_errors: list[str] = []
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            goto_app(page)
            page.wait_for_selector('[data-testid="nav-capital"]', timeout=20_000)
            wait_for_capital(page)
            try:
                page.wait_for_function(
                    """
                    () => {
                      const txt = (document.querySelector('#capital-balance-display')?.innerText || '').trim();
                      return txt && txt !== '$0.00';
                    }
                    """,
                    timeout=60_000,
                )
            except PlaywrightTimeoutError:
                pass

            fill_order_terminal(page)
            open_response_body = ""
            try:
                with page.expect_response(lambda res: "/api/capital/open-position" in res.url, timeout=30_000) as open_res_info:
                    page.locator(".capital-terminal .btn-terminal").click(timeout=12_000)
                open_response = open_res_info.value
                open_response_body = open_response.text()
                if open_response.status != 200:
                    raise AssertionError(
                        f"open-position returned HTTP {open_response.status}: {open_response_body}"
                    )
            except PlaywrightTimeoutError as exc:
                values = page.evaluate(
                    """
                    () => ({
                      ticker: document.querySelector('#capital-open-ticker')?.value || '',
                      direction: document.querySelector('#capital-open-direction')?.value || '',
                      leverage: document.querySelector('#capital-open-leverage')?.value || '',
                      size: document.querySelector('#capital-open-size')?.value || '',
                      stop: document.querySelector('#capital-open-stop-pct')?.value || '',
                      target: document.querySelector('#capital-open-target-pct')?.value || ''
                    })
                    """
                )
                raise AssertionError(f"UI did not send open-position request; terminal values={values}") from exc
            card = page.locator(".capital-trade-card").filter(has_text="AAPL").first
            try:
                card.wait_for(state="visible", timeout=30_000)
            except PlaywrightTimeoutError as exc:
                toast_text = ""
                try:
                    toast_text = page.locator("#scanner-toast, .toast, .notification").first.inner_text(timeout=2_000)
                except Exception:
                    pass
                capital_text = ""
                try:
                    capital_text = page.locator("#view-capital").inner_text(timeout=5_000)[:1500]
                except Exception:
                    pass
                raise AssertionError(
                    f"AAPL card did not render after UI open. open_response={open_response_body!r}; "
                    f"toast={toast_text!r}; capital_text={capital_text!r}"
                ) from exc

            open_state = get_capital_state()
            open_positions = [
                pos for pos in open_state.get("positions", [])
                if str(pos.get("ticker", "")).upper() == "AAPL" and str(pos.get("status", "")).upper() == "OPEN"
            ]
            if len(open_positions) != 1:
                raise AssertionError(f"expected one open AAPL position, got {len(open_positions)}")
            opened = open_positions[0]
            entry = float(opened.get("entry_price") or 0.0)
            stop = float(opened.get("stop_loss_price") or 0.0)
            target = float(opened.get("take_profit_price") or 0.0)
            if entry <= 0 or stop <= 0 or target <= 0:
                raise AssertionError(f"invalid opened prices: entry={entry}, stop={stop}, target={target}")
            if not (stop < entry < target):
                raise AssertionError(f"long risk levels invalid: stop={stop}, entry={entry}, target={target}")

            card_text = card.inner_text(timeout=10_000)
            for expected in (f"Entry ${format_price(entry)}", f"SL ${format_price(stop)}", f"TP ${format_price(target)}", "$100.00", "x1"):
                if expected not in card_text:
                    raise AssertionError(f"open card missing {expected!r}: {card_text}")

            open_shot = OUTPUT_DIR / "capital_roundtrip_open.png"
            page.screenshot(path=str(open_shot), full_page=True)
            summary["screenshots"]["open"] = str(open_shot.resolve())
            summary["opened"] = {
                "position_id": opened.get("position_id"),
                "ticker": opened.get("ticker"),
                "direction": opened.get("direction"),
                "entry_price": entry,
                "stop_loss_price": stop,
                "take_profit_price": target,
                "balance_after_open": open_state.get("balance"),
            }

            with page.expect_response(lambda res: "/api/capital/close-position" in res.url, timeout=30_000) as close_res_info:
                card.locator(".btn-close-pos").click(timeout=12_000)
            close_response = close_res_info.value
            close_response_body = close_response.text()
            if close_response.status != 200:
                raise AssertionError(
                    f"close-position returned HTTP {close_response.status}: {close_response_body}"
                )
            try:
                card.wait_for(state="detached", timeout=45_000)
            except PlaywrightTimeoutError:
                card.wait_for(state="hidden", timeout=15_000)

            closed_state = get_capital_state()
            closed_positions = [
                pos for pos in closed_state.get("positions", [])
                if pos.get("position_id") == opened.get("position_id")
            ]
            if len(closed_positions) != 1:
                raise AssertionError("closed position is missing from capital state")
            closed = closed_positions[0]
            if str(closed.get("status", "")).upper() != "CLOSED":
                raise AssertionError(f"position did not close: {closed}")
            exit_price = float(closed.get("exit_price") or 0.0)
            if exit_price <= 0:
                raise AssertionError(f"invalid exit price: {exit_price}")
            history_text = visible_text(page, "#capital-history-body")
            if "CLOSE_POSITION" not in history_text or "AAPL" not in history_text:
                raise AssertionError(f"history missing close event: {history_text}")

            closed_shot = OUTPUT_DIR / "capital_roundtrip_closed.png"
            page.screenshot(path=str(closed_shot), full_page=True)
            summary["screenshots"]["closed"] = str(closed_shot.resolve())
            summary["closed"] = {
                "position_id": closed.get("position_id"),
                "exit_price": exit_price,
                "gross_pnl": closed.get("gross_pnl"),
                "net_pnl": closed.get("net_pnl"),
                "balance_after_close": closed_state.get("balance"),
            }
            summary["console_errors"] = console_errors
            if console_errors:
                raise AssertionError(f"console errors detected: {console_errors[:3]}")

            context.close()
            browser.close()
    finally:
        restore_capital_file(original_bytes)
        # Force the long-running API process to re-read the restored mirror on next state access.
        try:
            time.sleep(0.2)
            get_capital_state()
        except Exception:
            pass

    return summary


def main() -> int:
    try:
        summary = run()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print("\nCAPITAL_ROUNDTRIP_OK")
        return 0
    except Exception as exc:
        print(f"CAPITAL_ROUNDTRIP_FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
