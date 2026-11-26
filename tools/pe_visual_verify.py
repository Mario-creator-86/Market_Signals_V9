"""Headless visual verification of the live Pre-Earnings tab on :8003.

The running server executes the OLD backend but serves the NEW frontend from
disk — exactly what the user sees after a page reload. Captures the tab at
desktop size plus a row-click interaction, and dumps console errors.
"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://localhost:8003"
errors = []

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1680, "height": 1050})
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(BASE, wait_until="networkidle", timeout=60000)

    # Navigate to the Pre-Earnings view (tab nav like a user would).
    clicked = page.evaluate(
        """() => {
            const els = [...document.querySelectorAll('a,button,[data-view],.nav-item,.tab')];
            const t = els.find(e => /pre.?earnings/i.test(e.textContent || '') || /preearnings/i.test(e.getAttribute('data-view') || ''));
            if (t) { t.click(); return (t.textContent || t.getAttribute('data-view') || '').trim(); }
            return null;
        }"""
    )
    print("nav click:", clicked)
    page.wait_for_timeout(6000)  # let the fetch + render settle

    state = page.evaluate(
        """() => {
            const view = document.getElementById('view-preearnings');
            const rows = document.querySelectorAll('#preearnings-sections tbody tr');
            const gapTh = [...document.querySelectorAll('#preearnings-sections th')].map(t => t.textContent.trim());
            const pulse = document.getElementById('pe-pulse-card') || document.querySelector('.pe-pulse-card');
            const stats = ['pe-stat-total','pe-stat-high-risk','pe-stat-bullish','pe-stat-avg-conf','pe-stat-na-risk']
                .map(id => { const el = document.getElementById(id); return el ? el.textContent : null; });
            const meta = document.getElementById('preearnings-meta');
            return {
                viewVisible: !!(view && view.offsetParent !== null || view && view.classList.contains('active')),
                rowCount: rows.length,
                headers: gapTh.slice(0, 8),
                pulsePresent: !!pulse,
                pulseText: pulse ? pulse.textContent.replace(/\\s+/g,' ').slice(0, 160) : null,
                stats, meta: meta ? meta.textContent : null,
            };
        }"""
    )
    print("STATE:", state)

    page.screenshot(path="pe_verify_top.png")
    page.evaluate("window.scrollTo(0, 1400)")
    page.wait_for_timeout(400)
    page.screenshot(path="pe_verify_tables.png")

    # Row click → details should appear and row gets is-selected
    page.evaluate(
        """() => {
            const tr = document.querySelector('#preearnings-sections tbody tr[data-symbol]');
            if (tr) tr.click();
        }"""
    )
    page.wait_for_timeout(1200)
    detail = page.evaluate(
        """() => {
            const sel = document.querySelector('#preearnings-sections tbody tr.is-selected');
            const panel = document.getElementById('preearnings-details');
            return {
                selectedRow: sel ? sel.getAttribute('data-symbol') : null,
                panelHasContent: !!(panel && panel.textContent.trim().length > 100),
                panelHead: panel ? panel.textContent.replace(/\\s+/g,' ').slice(0, 100) : null,
            };
        }"""
    )
    print("DETAIL:", detail)
    page.screenshot(path="pe_verify_detail.png", full_page=False)
    browser.close()

print("CONSOLE ERRORS:", len(errors))
for e in errors[:10]:
    print("  -", e[:200])
