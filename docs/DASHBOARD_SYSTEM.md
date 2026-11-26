# Market Signals V7 - Dashboard Cockpit

This document describes the current Home Dashboard ("Trader Cockpit") architecture and behavior.

## Scope

- Frontend SPA Home layout and renderers
- Backend lean dashboard payload in `/api/state`
- Refresh loops, caching, offline fallback, and localStorage persistence
- Activity log integrations for dashboard risk events

## Discovery Map (Key Files and Functions)

### Frontend structure
- `frontend/index.html`
  - `#view-home`
  - `#card-news`
  - `#card-performance`
  - `#active-signals-list`
  - scanner marquees (`#crypto-marquee`, `#kucoin-marquee`, `#stocks-marquee`)
  - modals: `#home-news-insight-modal`, `#home-signal-modal`

### Frontend behavior
- `frontend/app.js`
  - Routing: `ViewRouter.navigate(...)`
  - Loops: `startAutoRefresh()` -> `coreLoop`, `watchlistLoop`, `scannerLoop`
  - News loop: `UI.startNewsAutoRefresh()`
  - Rendering:
    - `UI.renderAll(...)`
    - `UI.renderSignalsFeed(...)`
    - `UI.renderNews(...)`
    - `UI.renderHomeMarketNarrative(...)`
    - `UI.renderDashboardWatchlistPreview(...)`
    - `UI.renderDashboardRiskFlags(...)`
    - `UI.renderDashboardEarnings(...)`
    - `UI.renderPerformance(...)`
  - Activity log and dedup: `UI.updateActivityLog(...)`, `UI.renderTimeline(...)`
  - Offline mode: `UI.setOfflineMode(...)`
  - Persistence: `UI.saveState(...)`, `UI.loadActivityLog(...)`

### Frontend styles
- `frontend/styles_base.css`
  - Cockpit grid and new Home widget classes (`.home-cockpit-grid`, `.home-news-*`, `.home-watchlist-*`, `.home-compact-list`, etc.)

### Backend payload builders
- `api/state_service.py`
  - `_build_dashboard_payload(...)`
  - `_build_dashboard_earnings_7d(...)`
  - `_build_dashboard_watchlist_priority(...)`
  - `_build_dashboard_risk_flags(...)`
  - `get_state(...)` injects dashboard fields

### News/Narrative backend sources
- `agents/news_tab_agent.py`
  - `get_dashboard_news_items(...)`
  - `get_market_narrative(...)`
  - narrative normalization and fallback

## Home Cockpit Layout

Home is organized into a 3-zone cockpit:

1. Top stream row:
   - Compact scanner marquees (Binance, KuCoin, Stocks)
2. Main grid:
   - Left main: Signals + Performance
   - Right main: News Action Feed + Today Market Narrative
   - Side column: Watchlist Preview + Risk Flags + Upcoming Earnings (7d)

## Dashboard API Payload

`GET /api/state` now includes lean dashboard keys:

- `dashboard_news_items` (max 12)
- `dashboard_market_narrative`
- `dashboard_earnings_7d` (max 10)
- `dashboard_watchlist_priority` (max 12)
- `dashboard_risk_flags` (max 10)
- `dashboard_generated_at`

This payload is intentionally lean for Home rendering and offline persistence.

## Market Narrative

Server-side narrative output schema:

```json
{
  "tone": "risk-on|risk-off|mixed",
  "top_catalysts": ["...", "...", "..."],
  "what_matters_today": ["...", "...", "...", "...", "..."],
  "watchlist_implications": ["...", "..."],
  "generated_at": "ISO-8601"
}
```

Compatibility aliases still supported in frontend rendering:
- `risk_regime`
- `bullets`

## News Widget (Home)

`UI.renderNews(...)` provides:
- quick filters: Breaking / High+ / Watchlist
- per-row badges: impact, category, sentiment, breaking, action score
- TLDR line for high/critical items (when available)
- "Also reported by" display for deduped stories
- item click -> Home News Insight modal

Force refresh:
- primary endpoint: `POST /api/news-tab/refresh`
- fallback endpoint: `POST /api/news/force-refresh`
- spinner + toast feedback

## Watchlist Preview (Home)

Two modes:
- `priority` (default): uses backend `dashboard_watchlist_priority`
- `order`: uses watchlist order/pinned sequence

Card details:
- ticker, price, change
- badges: trend, earnings days, news pulse, open signal
- click opens Watchlist view with ticker search prefilled

## Signals Widget Enhancements

`UI.renderSignalsFeed(...)` adds:
- compact TP progress / SL pressure bar
- time-in-trade marker
- row click opens Signal Action modal

Signal modal actions:
- close manual
- suppress ticker signals
- open ticker analyze flow in Watchlist view

## Activity Log Extensions

New Home-related event types:
- `NEWS_CRITICAL`
- `EARNINGS_SOON`
- `RISK_FLAG`

Dedup logic:
- news key uses `type + ticker + normalized_title + hour_bucket`
- risk flag key uses `type + ticker + title + hour_bucket`

## Refresh Loops and Performance Behavior

`startAutoRefresh()` keeps 3 independent loops:
- `coreLoop` (state)
- `watchlistLoop`
- `scannerLoop`

Optimizations:
- jitter (+/-10%)
- per-loop exponential backoff on failures
- slower cadence when user is not on relevant view
- stronger backoff while offline
- avoid unnecessary full Home re-render outside Home view

`UI.startNewsAutoRefresh()` also uses jitter/backoff and only updates Home news widget when Home is active.

## LocalStorage and Offline Fallback

`UI.saveState(...)` trims heavy fields and persists lean dashboard snapshots:
- `last_market_narrative`
- `last_news_widget_snapshot` (max 12)
- `last_watchlist_priority_snapshot` (max 12)

Existing keys remain in use:
- `appState`
- `appWatchlist`
- `appScanner`
- `marketMuse_activityLog`

If offline or backend unavailable, Home renderers can fallback to saved snapshots.

## Test Coverage

Automated:
- `tests/test_dashboard_cockpit_payload.py`
  - earnings 7d selection/sort
  - watchlist priority ranking
  - risk flag generation types

Manual smoke:
- `tests/manual/dashboard_cockpit_smoke.py`
  - validates dashboard payload presence from `/api/state`
  - validates narrative endpoint
  - checks watchlist/scanner endpoint sanity

