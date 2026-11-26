from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


BASE_URL = os.getenv("MARKET_SIGNALS_BASE_URL", "http://127.0.0.1:8003")
OUTPUT_DIR = Path(os.getenv("MARKET_SIGNALS_PLAYWRIGHT_OUTPUT", "output/playwright"))

NAV_TARGETS = {
    "dashboard": ("nav-home", "#view-home"),
    "watchlist": ("nav-watchlist", "#view-watchlist"),
    "news": ("nav-news", "#view-news"),
    "capital": ("nav-capital", "#view-capital"),
}

COLLECT_STATE_JS = r"""
async (viewName) => {
  const text = (sel) => (document.querySelector(sel)?.innerText || '').trim();
  const exists = (sel) => !!document.querySelector(sel);
  const count = (sel) => document.querySelectorAll(sel).length;
  const visible = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const active = [...document.querySelectorAll('.view-section')]
    .find(el => !el.classList.contains('hidden') && visible(el));
  const apiFetch = async (url) => {
    try {
      const res = await fetch(url, { cache: 'no-store' });
      const ct = res.headers.get('content-type') || '';
      const body = ct.includes('application/json') ? await res.json() : await res.text();
      return { ok: res.ok, status: res.status, body };
    } catch (e) {
      return { ok: false, status: 0, error: String(e && e.message || e) };
    }
  };

  const [stateRes, watchlistRes, newsRes] = await Promise.all([
    apiFetch('/api/state?lean=true&include_news=true&include_hotlist=false'),
    apiFetch('/api/watchlist'),
    apiFetch('/api/news-tab/state?limit=60')
  ]);

  const state = stateRes.body || {};
  const signalsRawOpen = state.signals_raw && state.signals_raw.open && typeof state.signals_raw.open === 'object'
    ? state.signals_raw.open : {};
  const stateSignals = state.signals && state.signals.open && typeof state.signals.open === 'object'
    ? state.signals.open : {};
  const apiSignalsOpen = Number(
    (state.stats && state.stats.open)
    || state.signals_count
    || Object.keys(signalsRawOpen).length
    || Object.keys(stateSignals).length
    || 0
  );

  const wlBody = watchlistRes.body || {};
  const wlTickers = Array.isArray(wlBody.tickers)
    ? wlBody.tickers
    : (Array.isArray(state.tickers) ? state.tickers : []);

  const newsBody = newsRes.body || {};
  const newsArticles = Array.isArray(newsBody.articles)
    ? newsBody.articles
    : (Array.isArray(newsBody.news)
      ? newsBody.news
      : (Array.isArray(newsBody.items)
        ? newsBody.items
        : (newsBody.feed && Array.isArray(newsBody.feed.items) ? newsBody.feed.items : [])));
  const newsTickerCount = newsBody.ticker_news && typeof newsBody.ticker_news === 'object'
    ? Object.values(newsBody.ticker_news).reduce((acc, arr) => acc + (Array.isArray(arr) ? arr.length : 0), 0)
    : 0;
  const newsStateArticleCount = Number(newsBody.total_articles || 0)
    || newsArticles.length
    || (Array.isArray(newsBody.market_news) ? newsBody.market_news.length : 0)
      + (Array.isArray(newsBody.breaking_news) ? newsBody.breaking_news.length : 0)
      + newsTickerCount;
  const capital = state.capital && typeof state.capital === 'object' ? state.capital : {};
  const capitalPositions = Array.isArray(capital.positions) ? capital.positions : [];
  const capitalOpenPositions = capitalPositions.filter(
    pos => String(pos && pos.status || '').toUpperCase() === 'OPEN'
  );
  const capitalOpportunities = capital.opportunities && typeof capital.opportunities === 'object'
    ? capital.opportunities
    : {};
  const capitalOpportunitySignals = Array.isArray(capitalOpportunities.signals)
    ? capitalOpportunities.signals
    : [];
  const capitalScannerWatchlist = Array.isArray(capitalOpportunities.scanner_watchlist)
    ? capitalOpportunities.scanner_watchlist
    : [];
  const capitalView = document.querySelector('#view-capital');
  const capitalStyles = capitalView ? window.getComputedStyle(capitalView) : null;
  const rootStyles = window.getComputedStyle(document.documentElement);

  const tickerLabels = [...document.querySelectorAll(
    '#detailed-watchlist-content .wl-symbol, #detailed-watchlist-content .ticker-symbol, '
    + '#detailed-watchlist-content [data-ticker], #watchlist-content .wl-symbol, '
    + '#watchlist-content .ticker-symbol'
  )]
    .map(el => (el.getAttribute('data-ticker') || el.innerText || '').trim())
    .filter(Boolean)
    .slice(0, 20);

  return {
    viewName,
    title: document.title,
    activeView: active ? active.id : null,
    bodyHasFatalText: /traceback|uncaught|cannot read properties|undefined is not/i.test(document.body.innerText || ''),
    dashboard: {
      signalRows: count('#active-signals-list .signal-row, #active-signals-list .mm-signal-card, #active-signals-list .signal-card'),
      signalCards: count('#active-signals-list .signal-card, #active-signals-list .signal-card-premium, #active-signals-list .mm-signal-card'),
      signalEmpty: exists('#active-signals-list .signals-empty-state'),
      statActiveSignals: text('#stat-active-signals'),
      signalHeaderStats: text('#signals-stats-header'),
      homeNewsCards: count('#news-feed-content .news-card-v2, #news-feed-content article, .cockpit-card [data-action="news"], .cockpit-table-row, [data-cockpit-section="major_news"] tr, [data-cockpit-section="major_news"] .cockpit-table tbody tr, .cockpit-list-item'),
      homeNewsText: text('#news-feed-content').slice(0, 300) || text('#cockpit-root').slice(0, 300),
      watchlistPreviewItems: count(
        '#watchlist-content .wl-card-mini, #watchlist-content .watchlist-item, '
        + '#watchlist-content article, #watchlist-content .trader-watchlist-item, '
        + '#watchlist-content .dash-wl-v2'
      ),
      watchlistPreviewText: text('#watchlist-content').slice(0, 300),
      narrative: text('#home-market-narrative-content').slice(0, 300)
    },
    scanner: {
      cryptoCards: count('#crypto-marquee .scanner-card, #view-scanner .scanner-card, .capital-scanner-row'),
      kucoinCards: count('#kucoin-marquee .scanner-card, #view-scanner .tape-col-list .tape-col-item'),
      stockCards: count('#stocks-marquee .scanner-card, #view-scanner .tape-item, .capital-scanner-row'),
      cryptoText: text('#crypto-marquee').slice(0, 200),
      kucoinText: text('#kucoin-marquee').slice(0, 200),
      stockText: text('#stocks-marquee').slice(0, 200)
    },
    watchlist: {
      detailedRows: count('#detailed-watchlist-content .wl-row'),
      cards: count(
        '#detailed-watchlist-content .wl-card-mini, #detailed-watchlist-content .watchlist-card, '
        + '#detailed-watchlist-content article, #detailed-watchlist-content .trader-watchlist-item'
      ),
      empty: /empty|no tickers|nu exista|nu există|loading/i.test(text('#detailed-watchlist-content')),
      searchVisible: visible(document.querySelector('#watchlist-search')),
      text: text('#detailed-watchlist-content').slice(0, 500),
      tickerLabels
    },
    news: {
      pageCards: count('#news-page-feed-content .news-card-v2, #news-page-feed-content article'),
      placeholderVisible: exists('#news-page-feed-content .news-placeholder'),
      feedCountText: text('#news-feed-count'),
      narrativeStatus: text('#news-narrative-status'),
      narrativeText: text('#news-market-narrative-content').slice(0, 300),
      agentStatus: text('#news-agent-status').slice(0, 300),
      breakingItems: count('#news-breaking-list > *'),
      tickerOptions: count('#news-follow-ticker option'),
      text: text('#news-page-feed-content').slice(0, 500)
    },
    capital: {
      balanceText: text('#capital-equity-value') || text('#capital-balance-display'),
      openPnlText: text('#capital-open-pnl'),
      positionsText: text('#capital-positions-count'),
      winRateText: text('#capital-win-rate'),
      tradeCards: count('#capital-trade-cards .capital-trade-card'),
      emptyTradeCards: exists('#capital-trade-cards .capital-detail-empty'),
      autopilotStatus: text('#capital-autopilot-status'),
      opportunityCards: count('#capital-opportunities .capital-opportunity-card'),
      scannerRows: count('#capital-opportunities .capital-scanner-row'),
      pipelineSteps: count('#capital-opportunities .capital-pipeline-step'),
      opportunitiesText: text('#capital-opportunities').slice(0, 500),
      tradeCardsText: text('#capital-trade-cards').slice(0, 500),
      palette: {
        background: capitalStyles ? capitalStyles.backgroundColor : '',
        capBg: capitalStyles ? capitalStyles.getPropertyValue('--cap-bg').trim() : '',
        capSurface: capitalStyles ? capitalStyles.getPropertyValue('--cap-surface').trim() : '',
        capAccent: capitalStyles ? capitalStyles.getPropertyValue('--cap-accent-gold').trim() : '',
        siteBg: rootStyles.getPropertyValue('--bg-color').trim(),
        siteSurface: rootStyles.getPropertyValue('--surface-color').trim(),
        siteAccent: rootStyles.getPropertyValue('--accent-color').trim(),
        siteText: rootStyles.getPropertyValue('--text-primary').trim()
      }
    },
    api: {
      stateOk: stateRes.ok,
      stateStatus: stateRes.status,
      watchlistOk: watchlistRes.ok,
      watchlistStatus: watchlistRes.status,
      watchlistTickerCount: wlTickers.length,
      newsOk: newsRes.ok,
      newsStatus: newsRes.status,
      newsArticleCount: newsStateArticleCount,
      apiSignalsOpen,
      capitalOpenPositions: capitalOpenPositions.length,
      capitalPositionsCount: capitalPositions.length,
      capitalAutonomousEnabled: Boolean(capitalOpportunities.autonomous_enabled),
      capitalOpportunitySignalCount: capitalOpportunitySignals.length,
      capitalScannerWatchlistCount: capitalScannerWatchlist.length,
      stateTickerCount: Array.isArray(state.tickers) ? state.tickers.length : 0,
      sampleTickers: wlTickers.slice(0, 12).map(t => (
        typeof t === 'string' ? t : (t.ticker || t.symbol || t.name || String(t))
      ))
    }
  };
}
"""


def wait_for_render(page: Page, ms: int) -> None:
    page.wait_for_timeout(ms)
    page.wait_for_timeout(500)


def goto_with_retry(page: Page, url: str, attempts: int = 3) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            return
        except PlaywrightTimeoutError as exc:
            last_error = exc
            if attempt == attempts:
                break
            page.wait_for_timeout(1_000 * attempt)
    if last_error:
        raise last_error


def get_api_counts(page: Page) -> dict[str, int]:
    try:
        return page.evaluate(
            """
            async () => {
              const readJson = async (url) => {
                try {
                  const res = await fetch(url, { cache: 'no-store' });
                  if (!res.ok) return {};
                  return await res.json();
                } catch (_) {
                  return {};
                }
              };
              const [state, watchlist, news] = await Promise.all([
                readJson('/api/state?lean=true&include_news=true&include_hotlist=false'),
                readJson('/api/watchlist'),
                readJson('/api/news-tab/state?limit=60')
              ]);
              const rawOpen = state.signals_raw && state.signals_raw.open && typeof state.signals_raw.open === 'object'
                ? Object.keys(state.signals_raw.open).length : 0;
              const signalsOpen = Number((state.stats && state.stats.open) || state.signals_count || rawOpen || 0);
              const watchlistCount = Array.isArray(watchlist.tickers)
                ? watchlist.tickers.length
                : (Array.isArray(state.tickers) ? state.tickers.length : 0);
              const capital = state.capital && typeof state.capital === 'object' ? state.capital : {};
              const capitalPositions = Array.isArray(capital.positions) ? capital.positions : [];
              const capitalOpen = capitalPositions.filter(
                pos => String(pos && pos.status || '').toUpperCase() === 'OPEN'
              ).length;
              const tickerNewsCount = news.ticker_news && typeof news.ticker_news === 'object'
                ? Object.values(news.ticker_news).reduce((acc, arr) => acc + (Array.isArray(arr) ? arr.length : 0), 0)
                : 0;
              const newsCount = Number(news.total_articles || 0)
                || (Array.isArray(news.market_news) ? news.market_news.length : 0)
                  + (Array.isArray(news.breaking_news) ? news.breaking_news.length : 0)
                  + tickerNewsCount;
              return { signalsOpen, watchlistCount, newsCount, capitalOpen };
            }
            """
        )
    except Exception:
        return {"signalsOpen": 0, "watchlistCount": 0, "newsCount": 0, "capitalOpen": 0}


def navigate_and_collect(page: Page, name: str, nav_id: str, view_selector: str) -> dict[str, Any]:
    page.get_by_test_id(nav_id).click(timeout=12_000)
    page.wait_for_function(
        "sel => { const el = document.querySelector(sel); return el && !el.classList.contains('hidden'); }",
        arg=view_selector,
        timeout=20_000,
    )
    wait_for_render(page, 7_000 if name in {"news", "capital"} else 5_000)

    if name == "dashboard":
        try:
            page.wait_for_function(
                """
                () => {
                  const loaded = document.querySelectorAll(
                    '#crypto-marquee .scanner-card, #kucoin-marquee .scanner-card, #stocks-marquee .scanner-card'
                  ).length;
                  const loadingText = [
                    document.querySelector('#crypto-marquee')?.innerText || '',
                    document.querySelector('#kucoin-marquee')?.innerText || '',
                    document.querySelector('#stocks-marquee')?.innerText || ''
                  ].join(' ').toLowerCase();
                  return loaded > 0 || !loadingText.includes('loading');
                }
                """,
                timeout=30_000,
            )
        except PlaywrightTimeoutError:
            pass
        counts = get_api_counts(page)
        waits = [
            (counts.get("signalsOpen", 0), "#active-signals-list .signal-row"),
            (counts.get("watchlistCount", 0), "#watchlist-content .dash-wl-v2, #watchlist-content .wl-card-mini"),
            (counts.get("newsCount", 0), "#news-feed-content .news-card-v2, #news-feed-content article"),
        ]
        for expected, selector in waits:
            if expected > 0:
                try:
                    page.wait_for_selector(selector, timeout=45_000)
                except PlaywrightTimeoutError:
                    pass

    if name == "news":
        counts = get_api_counts(page)
        if counts.get("newsCount", 0) > 0:
            try:
                page.wait_for_selector("#news-page-feed-content .news-card-v2, #news-page-feed-content article", timeout=45_000)
            except PlaywrightTimeoutError:
                pass

    if name == "watchlist":
        counts = get_api_counts(page)
        if counts.get("watchlistCount", 0) > 0:
            try:
                page.wait_for_selector("#detailed-watchlist-content .wl-row", timeout=45_000)
            except PlaywrightTimeoutError:
                pass

    if name == "capital":
        try:
            page.wait_for_selector("#capital-equity-value, #capital-balance-display", timeout=20_000)
            page.wait_for_selector("#capital-opportunities", timeout=20_000)
            page.wait_for_function(
                """
                () => {
                  const status = (document.querySelector('#capital-autopilot-status')?.innerText || '').trim().toLowerCase();
                  const opportunities = document.querySelector('#capital-opportunities')?.innerText || '';
                  return status && !status.includes('verificare') && !opportunities.toLowerCase().includes('se încarcă');
                }
                """,
                timeout=45_000,
            )
        except PlaywrightTimeoutError:
            pass
        counts = get_api_counts(page)
        if counts.get("capitalOpen", 0) > 0:
            try:
                page.wait_for_selector("#capital-trade-cards .capital-trade-card", timeout=45_000)
            except PlaywrightTimeoutError:
                pass

    screenshot = OUTPUT_DIR / f"{name}_playwright_check.png"
    page.screenshot(path=str(screenshot), full_page=True)
    data = page.evaluate(COLLECT_STATE_JS, name)
    data["screenshot"] = str(screenshot.resolve())
    return data


def run_mobile_smoke(context: Any) -> tuple[dict[str, Any], list[dict[str, str]]]:
    page = context.new_page()
    page.set_viewport_size({"width": 390, "height": 900})
    console_errors: list[dict[str, str]] = []
    page.on(
        "console",
        lambda msg: console_errors.append({"type": msg.type, "text": msg.text[:500]})
        if msg.type == "error"
        else None,
    )

    goto_with_retry(page, BASE_URL)
    page.wait_for_selector('[data-testid="nav-home"]', timeout=20_000)
    wait_for_render(page, 5_000)

    results: dict[str, Any] = {}
    for name, (nav_id, view_selector) in NAV_TARGETS.items():
        page.get_by_test_id(nav_id).click(timeout=12_000)
        page.wait_for_function(
            "sel => { const el = document.querySelector(sel); return el && !el.classList.contains('hidden'); }",
            arg=view_selector,
            timeout=20_000,
        )
        wait_for_render(page, 4_000)
        screenshot = OUTPUT_DIR / f"{name}_mobile_playwright_check.png"
        page.screenshot(path=str(screenshot), full_page=True)
        rects = page.evaluate(
            """
            (viewSel) => {
              const view = document.querySelector(viewSel);
              const active = [...document.querySelectorAll('.view-section')]
                .find(el => !el.classList.contains('hidden'));
              const overflow = [...document.querySelectorAll('body *')]
                .filter(el => {
                  const r = el.getBoundingClientRect();
                  return r.width > 0 && r.right > window.innerWidth + 8;
                })
                .slice(0, 8)
                .map(el => ({
                  tag: el.tagName,
                  id: el.id,
                  cls: String(el.className || '').slice(0, 120),
                  right: Math.round(el.getBoundingClientRect().right)
                }));
              return {
                activeView: active ? active.id : null,
                viewVisible: !!view && !view.classList.contains('hidden'),
                overflowCount: overflow.length,
                overflowSamples: overflow
              };
            }
            """,
            view_selector,
        )
        results[name] = {**rects, "screenshot": str(screenshot.resolve())}

    page.close()
    return results, console_errors


def validate(results: dict[str, Any], console_messages: list[dict[str, Any]], failed_requests: list[dict[str, Any]], bad_api_responses: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    expected_views = {
        "dashboard": "view-home",
        "watchlist": "view-watchlist",
        "news": "view-news",
        "capital": "view-capital",
    }

    for name, expected in expected_views.items():
        active = results[name].get("activeView")
        if active != expected:
            failures.append(f"{name}: activeView={active}, expected={expected}")

    api = results["dashboard"]["api"]
    if not api["stateOk"]:
        failures.append(f"state API failed: {api['stateStatus']}")
    if not api["watchlistOk"]:
        failures.append(f"watchlist API failed: {api['watchlistStatus']}")
    if not api["newsOk"]:
        failures.append(f"news-tab API failed: {api['newsStatus']}")

    if api["apiSignalsOpen"] > 0 and results["dashboard"]["dashboard"]["signalRows"] == 0:
        failures.append(
            f"dashboard: API has {api['apiSignalsOpen']} open signals but UI rendered 0 signal rows"
        )
    scanner = results["dashboard"].get("scanner") or {}
    scanner_cards = int(scanner.get("cryptoCards") or 0) + int(scanner.get("kucoinCards") or 0) + int(scanner.get("stockCards") or 0)
    if scanner_cards == 0:
        failures.append("dashboard: scanner stream rendered no mover cards")
    if api["watchlistTickerCount"] > 0:
        watchlist_rendered = (
            results["watchlist"]["watchlist"]["detailedRows"]
            + results["watchlist"]["watchlist"]["cards"]
        )
        if watchlist_rendered == 0:
            failures.append(
                f"watchlist: API has {api['watchlistTickerCount']} tickers but UI rendered no rows/cards"
            )
    if api["newsArticleCount"] > 0 and results["news"]["news"]["pageCards"] == 0:
        failures.append(
            f"news: API has {api['newsArticleCount']} articles but UI rendered no article cards"
        )
    if api["newsArticleCount"] > 0 and results["dashboard"]["dashboard"]["homeNewsCards"] == 0:
        failures.append(
            f"dashboard: API has {api['newsArticleCount']} news articles but home news widget rendered 0 cards"
        )
    capital = results["capital"]["capital"]
    capital_api = results["capital"]["api"]
    if capital_api["capitalOpenPositions"] > 0 and capital["tradeCards"] == 0:
        failures.append(
            f"capital: API has {capital_api['capitalOpenPositions']} open positions but UI rendered 0 trade cards"
        )
    if not str(capital.get("balanceText") or "").strip():
        failures.append("capital: balance display is empty")
    if not str(capital.get("autopilotStatus") or "").strip():
        failures.append("capital: autopilot status is empty")
    if "verificare" in str(capital.get("autopilotStatus") or "").lower():
        failures.append("capital: autopilot status remained in loading state")
    if capital_api["capitalOpportunitySignalCount"] > 0 and capital["opportunityCards"] == 0:
        failures.append(
            f"capital: API has {capital_api['capitalOpportunitySignalCount']} decision-ready opportunities but UI rendered 0 opportunity cards"
        )
    if capital_api["capitalScannerWatchlistCount"] > 0 and capital["scannerRows"] == 0:
        failures.append(
            f"capital: API has {capital_api['capitalScannerWatchlistCount']} scanner watchlist movers but UI rendered 0 scanner rows"
        )
    palette = capital.get("palette") or {}
    background = str(palette.get("background") or "")
    if not background or background == "rgba(0, 0, 0, 0)":
        failures.append("capital: background color is not applied")
    if "15, 23, 42" in background or "17, 24, 39" in background:
        failures.append(f"capital: background still looks like the old dark palette ({background})")
    for key in ("capBg", "capSurface", "capAccent", "siteBg", "siteSurface", "siteAccent"):
        if not str(palette.get(key) or "").strip():
            failures.append(f"capital: palette variable {key} is empty")

    if [m for m in console_messages if m["type"] == "error"]:
        failures.append("console errors detected")
    if bad_api_responses:
        failures.append("API responses >=400 detected")
    if failed_requests:
        failures.append("failed network requests detected")

    for name, expected in expected_views.items():
        mobile = results["mobile"][name]
        if mobile["activeView"] != expected or not mobile["viewVisible"]:
            failures.append(f"mobile {name}: view not visible")

    if results.get("mobile_console_errors"):
        failures.append("mobile console errors detected")

    return failures


def is_ignorable_request_failure(item: dict[str, Any]) -> bool:
    failure = str(item.get("failure") or "")
    return "ERR_ABORTED" in failure


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    console_messages: list[dict[str, Any]] = []
    failed_requests: list[dict[str, Any]] = []
    bad_api_responses: list[dict[str, Any]] = []
    results: dict[str, Any] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
        page = context.new_page()

        page.on(
            "console",
            lambda msg: console_messages.append(
                {
                    "type": msg.type,
                    "text": msg.text[:800],
                    "location": msg.location,
                }
            )
            if msg.type in ("error", "warning")
            else None,
        )
        page.on(
            "requestfailed",
            lambda req: failed_requests.append(
                {
                    "url": req.url,
                    "method": req.method,
                    "failure": req.failure,
                }
            ),
        )

        def on_response(res: Any) -> None:
            try:
                if "/api/" in res.url and res.status >= 400:
                    bad_api_responses.append({"url": res.url, "status": res.status})
            except Exception:
                return

        page.on("response", on_response)
        goto_with_retry(page, BASE_URL)
        page.wait_for_selector('[data-testid="nav-home"]', timeout=20_000)
        wait_for_render(page, 7_000)

        for name, (nav_id, view_selector) in NAV_TARGETS.items():
            results[name] = navigate_and_collect(page, name, nav_id, view_selector)

        mobile_results, mobile_console_errors = run_mobile_smoke(context)
        results["mobile"] = mobile_results
        results["mobile_console_errors"] = mobile_console_errors

        context.close()
        browser.close()

    relevant_failed_requests = [item for item in failed_requests if not is_ignorable_request_failure(item)]
    ignored_failed_requests = [item for item in failed_requests if is_ignorable_request_failure(item)]

    summary = {
        "base_url": BASE_URL,
        "results": results,
        "console_errors": [m for m in console_messages if m["type"] == "error"],
        "console_warnings": [m for m in console_messages if m["type"] == "warning"][:20],
        "failed_requests": relevant_failed_requests,
        "ignored_failed_requests": ignored_failed_requests,
        "bad_api_responses": bad_api_responses,
    }
    # Windows consoles may still default to cp1252; keep the QA report
    # printable even when UI text contains typographic Unicode punctuation.
    print(json.dumps(summary, ensure_ascii=True, indent=2))

    failures = validate(results, console_messages, relevant_failed_requests, bad_api_responses)
    if failures:
        print("\nPLAYWRIGHT_FAILURES")
        for failure in failures:
            print(f"- {failure}")
        return 2

    print("\nPLAYWRIGHT_CHECKS_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
