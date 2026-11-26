# MARKET SIGNALS V7 — COMPLETE UI AUDIT REPORT

**Date:** 2026-05-14  
**Auditors:** 7 specialized agents (Design, QA, Integration, Trading Logic, Responsive/A11y, Performance, Copywriting)  
**Methodology:** Full source code audit of frontend (index.html, app.js, 10+ CSS files) and backend (api/, core/)

---

## 1. Executive Summary

### Overall Assessment
The application has a **solid foundation** with a well-designed gold/cream color system, thoughtful card components, and comprehensive trading features. However, it suffers from significant issues in **consistency** (two design systems, hardcoded colors, fragmented typography), **accessibility** (no skip nav, no focus styles, ARIA violations), **performance** (9 concurrent polling loops, full DOM rebuilds every 5s), and **product clarity** (signals lack explanations, Romanian text remains, scores are opaque).

### Top 5 Critical Problems

| # | Problem | Impact |
|---|---------|--------|
| 1 | **30+ Romanian text strings remain in production UI** | Makes the app look unprofessional and confusing for non-Romanian users |
| 2 | **9 concurrent polling loops with 15-25 req/min** | Kills mobile battery, overloads server, makes app sluggish |
| 3 | **Navigation hidden on mobile (display:none at 900px)** | App is completely unusable on phones |
| 4 | **Signals lack "Why" explanation** | Traders can't evaluate or trust signals — just numbers without context |
| 5 | **`renderAll()` rebuilds 12+ DOM sections every 5-8.5s** | Main performance bottleneck — entire page reconstructed 7-12x/min |

### Top 5 Recommended Improvements

| # | Improvement | Expected Impact |
|---|-------------|-----------------|
| 1 | **Consolidate to single polling orchestrator** | Reduce requests by 60%, improve battery life |
| 2 | **Add signal thesis text (from backend `details` dict)** | Traders understand WHY a signal exists — massive trust increase |
| 3 | **Implement dirty-checking before DOM rebuilds** | Eliminate unnecessary renders, smooth UX |
| 4 | **Add mobile hamburger nav + responsive sidebar** | Mobile users can actually use the app |
| 5 | **Replace all remaining Romanian text** | Professional, international-ready product |

### Scores

| Dimension | Score | Notes |
|-----------|-------|-------|
| **UI/UX Design** | 6.5/10 | Good design system vars, but bypassed by hardcoding; no dark mode |
| **Functional** | 7.0/10 | Most features work; dead buttons, MEDIUMM typo, encoding bugs |
| **Trading Product** | 5.5/10 | Powerful backend, but signals opaque to traders; no timeframe/expiry |
| **Responsive/A11y** | 4.0/10 | Mobile unusable; no skip nav; no focus styles; ARIA violations |
| **Performance** | 5.0/10 | 15-25 req/min; full DOM rebuilds; unbounded caches; duplicate polling |
| **Copywriting** | 5.5/10 | 72+ issues; 30+ Romanian strings; 11+ corrupted `??`; jargon unexplained |
| **Overall** | **5.5/10** | Solid backend with significant frontend gaps |

---

## 2. Critical Issues

| ID | Page | Problem | Severity | Impact | Recommendation | File |
|----|------|---------|----------|--------|----------------|------|
| C1 | Global | 30+ Romanian text strings in app.js and index.html | Critical | App appears unprofessional and confusing | Translate all RO→EN | app.js (35+), index.html (6+) |
| C2 | Global | 11+ `??`/`???` corrupted unicode in dropdowns and toasts | Critical | UI looks broken with question marks | Replace with proper characters or remove | index.html:513-718, app.js:4250+ |
| C3 | Global | Navigation hidden on mobile (display:none at 900px) | Critical | App is completely unusable on phones | Add hamburger menu or scrollable tabs | styles.css:1326 |
| C4 | Home | 9 concurrent polling loops (15-25 req/min) | Critical | Battery drain, server overload, sluggish UX | Single orchestrator, poll only active tab | app.js:14932-15368 |
| C5 | Home | `renderAll()` rebuilds 12+ DOM sections every 5-8.5s | Critical | Layout thrashing, scroll position loss, jank | Dirty-checking + incremental DOM updates | app.js:14720-14762 |
| C6 | Signals | No "Why This Signal?" explanation | Critical | Traders can't evaluate or trust signals | Surface `details` dict as natural language thesis | app.js:3540-3600 |
| C7 | Signals | No timeframe/holding period shown | Critical | Traders don't know if signal is 15min or multi-day | Add `timeframe` and `expected_holding_period` fields | core/scoring_system.py, app.js |
| C8 | Capital | `cachedState.capital.positions` null crash | Critical | TypeError when positions array is undefined | Add null-safe guard | app.js:2659 |
| C9 | Home | No skip-to-content link | Critical | WCAG 2.4.1 violation; keyboard users tab through 12+ nav items | Add skip link as first focusable element | index.html |
| C10 | Global | `outline:none` on 9 focusable element types without replacement | Critical | Keyboard users can't see focus; WCAG 2.4.7 violation | Add `:focus-visible` styles | Multiple CSS files |
| C11 | Global | No dark mode despite trading app 24/7 usage | Critical | Eye strain for extended use | Implement data-theme="dark" with CSS variable overrides | styles_variables.css |

---

## 3. Page-by-Page Audit

### Home / Dashboard
- ✅ **Works well:** KPI header, live signal feed, news integration, watchlist preview
- ❌ **Broken:** `Actions.clearPerformanceHistory()` not defined — "Clear" button is dead
- ❌ **Visual:** Information overload — 12+ data points in single viewport, no clear focal point
- ❌ **Performance:** `renderAll()` rebuilds entire page every 5-8.5s
- ❌ **Data:** Dashboard can crash on cold start if `cachedDashboard` is null
- 📋 **Recommend:** Establish visual hierarchy; make signal feed dominant; reduce side cards to 3

### Watchlist
- ✅ **Works well:** Multi-timeframe data, detailed view, reordering
- ❌ **Broken:** 13-column grid with `!important` overrides; columns squeeze to ~40px on 1400px screens
- ❌ **Visual:** Dark navy headers (#0f172a) clash with cream/gold theme
- ❌ **Responsive:** Table breaks completely on mobile
- ❌ **Data:** `col_1h` field missing in dashboard context but used by frontend
- 📋 **Recommend:** Reduce to 8-9 columns; add expandable detail rows; harmonize header theme

### Scanner
- ✅ **Works well:** Real-time filtering, sort by multiple criteria
- ❌ **Broken:** `??` encoding in sort dropdown options
- ❌ **Visual:** Dark header clashes with theme (same as watchlist)
- ❌ **Responsive:** `min-width: 860px` forces horizontal scroll on mobile
- ❌ **Empty State:** No clear "no results" or "loading failed" state
- 📋 **Recommend:** Add empty/error states; fix encoding; responsive card layout

### Pre-Market / Pre-Earnings
- ✅ **Works well:** Gap risk scoring, confidence indicators, catalyst detection
- ❌ **Data:** Pre-Earnings `GapRisk` label inconsistent (sometimes "Gap Risk")
- ❌ **Copywriting:** Romanian tooltips (`Decizie:`, `Motiv:`, `Catalysts detectate:`)
- ❌ **Jargon:** "EDGE" and "PLAY" tooltips not explained to new users
- ❌ **Responsive:** Tables with `min-width` overflow on small screens
- 📋 **Recommend:** Standardize "Gap Risk" spelling; translate all Romanian; add jargon glossary

### News
- ✅ **Works well:** Impact scoring, sentiment indicators, export functionality
- ❌ **Broken:** News can get stuck in perpetual "Loading..." state on fetch failure
- ❌ **Visual:** Dual news card systems (legacy + V2) — inconsistent appearance
- ❌ **Copywriting:** `action_score`, `time_horizon` underscores in user-visible labels
- ❌ **Responsive:** Two-column layout breaks at 1120px
- 📋 **Recommend:** Standardize on V2 card design; add error state with retry; fix labels

### Hot List
- ✅ **Works well:** Priority scoring, fire badge, sentiment aggregation
- ❌ **Visual:** 620px tooltip width overflows on mobile
- ❌ **Responsive:** Cards don't reflow well on narrow screens
- 📋 **Recommend:** Reduce tooltip max-width; add responsive card grid

### Capital / Paper Trading
- ✅ **Works well:** Position tracking, explainability panel, trade journal
- ❌ **Broken:** `positions` null crash when capital data not loaded
- ❌ **Data:** RSI shows `0` or `NaN` for stocks with insufficient 5-min data
- ❌ **Copywriting:** 15+ Romanian strings in explainability panel
- ❌ **Product:** No drawdown tracking, no Sharpe ratio, no win/loss streak display
- 📋 **Recommend:** Add risk metrics dashboard; translate all Romanian; add dollar-at-risk display

### Backtest
- ✅ **Works well:** Multiple risk modes, walk-forward, export
- ❌ **Broken:** `MEDIUMM` vs `MEDIUM` risk mode mismatch — dropdown value doesn't match code
- ❌ **Broken:** `Allal` instead of `Total` in results headers
- ❌ **Product:** Backtest uses V1 simple logic, NOT V7 scoring — results are misleading
- ❌ **Visual:** 5 full-width action buttons with no visual hierarchy
- ❌ **Copywriting:** `??` encoding in dropdown options; `Def` placeholder instead of `Default`
- 📋 **Recommend:** Fix MEDIUMM typo; add backtest disclaimer; consolidate action buttons

### Calendar
- ✅ **Works well:** Economic events display, earnings dates
- ❌ **Empty State:** No "No events" message when date range is empty
- 📋 **Recommend:** Add empty state with guidance text

### Settings / Global Settings
- ❌ **Copywriting:** `Strategii` tab label (Romanian); `Configurare Ticker` modal title; `tickerele` in warning
- ❌ **Jargon:** `Min RR` not explained; `Event Block (h)` ambiguous
- 📋 **Recommend:** Translate labels; add tooltips for all jargon terms

---

## 4. UI/UX Design Findings

### Layout
- Home cockpit tries to show 12+ data points in one viewport — cognitive overload
- Sidebars (260px each) are position:fixed with no responsive collapse
- Navigation has 13 items in horizontal bar — overflows on screens under 900px

### Colors
- CSS variable system is well-designed but extensively bypassed by hardcoded hex values (#0f172a, #1e293b, #334155 used 30+ times)
- Watchlist/Scanner use dark navy headers (#0f172a) that clash with cream/gold theme
- News ticker groups use dark theme (#0f172a) inconsistent with light app

### Fonts
- 30+ distinct font sizes from 0.58rem to 3.5rem with no clear type scale
- Font sizes differ by 1-2px (0.66rem, 0.67rem, 0.68rem, 0.7rem) — no hierarchy

### Spacing
- Inconsistent padding/margin patterns across components
- Inconsistent border-radius (6px, 8px, 10px, 12px, 14px, 16px, 18px, 20px, 22px, 24px)

### Tables
- Watchlist 13-column grid squeezes columns to ~40px
- Scanner `min-width: 860px` forces horizontal scroll
- Pre-market `min-width: 1020px` — unusable on tablets

### Navigation
- 13 tabs overflow on screens under ~1100px
- `display: none` at 900px breakpoint makes app completely unusable on mobile
- No hamburger menu fallback

### Cards
- Three different news card designs (legacy, V2, home-news-item)
- Inconsistent shadow values (15+ different hardcoded shadows)

### Consistency
- Capital tab defines parallel CSS variables (`--cap-*`) that map to same colors as global vars
- Inline styles throughout HTML bypass design system
- Duplicate animation keyframe definitions across CSS files

---

## 5. Functional QA Findings

### Dead Buttons
| Button | Location | Problem |
|--------|----------|---------|
| Clear Performance History | Home Performance card | `Actions.clearPerformanceHistory()` not defined |

### Encoding Bugs
| Element | Location | Current | Expected |
|---------|----------|---------|----------|
| Risk mode dropdown | Backtest | `?? ALL (Compare 4 Risk Modes)` | `ALL (Compare 4 Risk Modes)` |
| Risk mode dropdown | Backtest | `?? OPENING RANGE (FVG)` | `OPENING RANGE (FVG)` |
| Scanner sort | Scanner | `?? Volatility (Abs)` | `Volatility (Absolute)` |
| Add Ticker button | Watchlist | `>Add< Ticker` | `Add Ticker` |
| Backtest column | Backtest | `Allal` | `Total` |

### Data Mismatches
| Issue | Frontend Expects | Backend Provides | Impact |
|-------|-------------------|-------------------|--------|
| MTF column | `col_1h` | `trend_1h` in dashboard | Empty MTF cells in dashboard |
| RSI for stocks | Number | `None` (defaults to 50) | Misleading RSI=50 when no data |
| Risk mode | `MEDIUM` (HTML) | `MEDIUMM` (JS) | MEDIUM risk mode may not validate |
| Watchlist settings | Normalized ticker | Raw ticker from URL | Settings may not load/save |

### Missing Error/Loading States
| Component | Current Behavior | Expected |
|-----------|-----------------|----------|
| Scanner empty | Shows stale data or "Loading..." forever | Error state with retry button |
| News failed | Perpetual "Loading..." | Error state with retry |
| Dashboard cold start | Crash if null data | Null-safe fallback |
| Calendar empty | Empty grid, no message | "No events for this date range" |
| Capital positions null | TypeError crash | Empty positions array |

---

## 6. Trading Product Findings

### What's Missing for Traders

| Missing Element | Impact | Priority |
|-----------------|--------|----------|
| Signal thesis (WHY this signal) | Traders can't evaluate signal quality | Critical |
| Timeframe/holding period | Timeframe mismatch causes losses | Critical |
| Dollar risk per trade | Poor risk management decisions | High |
| Confidence tier explanation (A/B/C) | Can't size positions properly | High |
| Signal expiration/freshness indicator | Acting on stale signals | High |
| Drawdown tracking in Capital | Trading blind on risk | High |
| Backtest disclaimer (V1 vs V7) | Misleading performance expectations | High |
| Signal filtering/sorting | Can't identify best opportunity quickly | Medium |
| Catalyst-signal relevance | False confidence from coincidental news | Medium |
| Trade plan / exit guidance | No trade management framework | Medium |
| R:R after commissions | Distrust in risk/reward metric | Medium |
| Win rate sample size context | Overconfidence from small samples | Medium |

### Fields to Add

| Field | Where | Description |
|-------|-------|-------------|
| `signal_thesis` | Signal card | 1-2 sentence natural language explanation |
| `timeframe` | Signal card | "1m-15m scalps" / "1-4h swing" / "intraday" |
| `expected_holding_period` | Signal card | "~15-60 min for PULLBACK" |
| `dollar_risk` | Signal card | "$25.00 (2% of $1,250)" |
| `confidence_tier` | Signal card | "Tier A (1.0x size)" with reasons |
| `signal_age_badge` | Signal card | Green (<5m), Yellow (5-15m), Red (>15m) |
| `sample_size` | Backtest badge | "WR 58% (42 trades, 30d)" |
| `max_drawdown` | Capital dashboard | Peak-to-trough percentage |

---

## 7. Responsive & Mobile Findings

### Desktop (1200px+)
- Sidebars take 540px, content squeezed to ~860px — tight but functional
- 13 navigation tabs overflow at ~1400px viewport

### Laptop (900-1199px)
- Sidebars should collapse but don't (no responsive rule)
- 6-column KPI grid squeezes to 3 columns at 1199px

### Tablet (600-899px)
- **Navigation completely hidden** (`display: none`)
- Tables overflow horizontally
- Capital opportunities grid squeezes
- Backtest parameters panel overlaps content

### Mobile (<600px)
- **App is entirely unusable** — no navigation, no sidebars, tables overflow
- Touch targets as small as 26x26px (WCAG minimum: 44x44px)
- Font sizes down to 0.58rem (9.3px) — below readability minimum
- Footer takes disproportionate screen space

### Accessibility
- No skip-to-content link (WCAG 2.4.1 violation)
- No `:focus-visible` styles (WCAG 2.4.7 violation)
- `role="tab"` on nav links without `role="tablist"` parent (ARIA violation)
- No focus traps on modals
- No `prefers-reduced-motion` support (except skeleton loaders)
- Color contrast issues: `#94a3b8` on white = 2.8:1 (needs 4.5:1)

---

## 8. Performance Findings

### Request Load
| Metric | Value | Target |
|--------|-------|--------|
| Requests/min on home view | 15-25 | 5-8 |
| API calls per render cycle | 3-5 | 1-2 |
| DOM rebuilds per minute | 7-12 | 2-4 |
| localStorage writes per minute | 7-12 | 1-2 |

### Key Bottlenecks

| Bottleneck | Impact | Fix |
|------------|--------|-----|
| 9 independent polling loops | 15-25 req/min | Single orchestrator |
| `renderAll()` full DOM rebuild | Layout thrashing | Dirty-checking + diffing |
| `saveState()` every 5-8.5s | Main thread blocking | Debounce to 30s |
| Duplicate API stats polling | 12-15 wasted req/min | Remove one poller |
| `/api/live-prices` sync file reads | Thread blocking | Memory cache |
| Unbounded `_ENDPOINT_PAYLOAD_CACHE` | Memory growth | LRU eviction (max 200) |
| `seenNews`/`seenSignals` Sets grow unbounded | Memory leak | Cap at 1000 entries |

### Optimization Recommendations

1. **Single polling orchestrator** — only poll active tab's endpoints; pause on hidden tab
2. **Dirty-checking before DOM updates** — compare data hashes; only re-render changed sections
3. **Debounce `saveState()`** — max once per 30s, only if state changed
4. **Remove duplicate API stats poller** — keep `api_stats_widget.js` only
5. **Cache `/api/live-prices` watchlist in memory** — avoid file reads on every request
6. **Add LRU eviction to endpoint cache** — max 200 entries
7. **Cap `seenNews`/`seenSignals` Sets** — max 1000 entries, prune old
8. **Pause non-essential polling on hidden tab** — reduce to 0, not just 2.5x multiplier

---

## 9. Copywriting Improvements

### Top Priority Translations (RO → EN)

| Current (Romanian) | Location | Proposed (English) |
|---------------------|----------|---------------------|
| `Configurare Ticker` | index.html:1368 | `Ticker Settings` |
| `Adauga in Jurnal` | index.html:1611 | `Add to Journal` |
| `Strategii` | index.html:1675 | `Strategies` |
| `tickerele` | index.html:1659 | `tickers` |
| `Nicio simulare saved` | index.html:581 | `No saved simulations` |
| `Introdu un Ticker.` | app.js:12686 | `Please enter a ticker symbol.` |
| `Nu exista suficiente decizii evaluate` | app.js:8069 | `Not enough evaluated decisions` |
| `Selecteaza o pozitie...` | app.js:8511 | `Select a position or event...` |
| `Catalysts detectate:` | app.js:11358 | `Detected catalysts:` |
| `Decizie: context favorabil...` | app.js:16073 | `Decision: favorable context...` |

### Jargon → Clear Labels

| Current | Problem | Proposed |
|---------|---------|----------|
| `Min RR` | Abbreviation unclear | `Min Risk/Reward` + tooltip |
| `Event Block (h)` | Ambiguous | `Event Block (hours)` + tooltip |
| `Cust TP%` / `Cust SL%` | "Cust" is jargon | `Custom TP%` / `Custom SL%` |
| `Def` placeholder | Cryptic | `Default` |
| `Conf 0.50` | Unexplained | `Tier B (0.5x size)` + tooltip |
| `GapRisk` | Inconsistent spacing | `Gap Risk` |
| `PnL` | Abbreviation | `PnL` with tooltip: "Profit and Loss" |
| `Regime: Mixed (0%)` | Jargon | `Regime: Mixed (0%)` + tooltip explaining what Regime means |
| `Autonomous Capital Flow` | Vague | `AI Trade Opportunities` |

---

## 10. Prioritized Fix Plan

### Phase 1 — Critical Fixes (1-2 weeks)

| # | Fix | Files | Effort |
|---|-----|-------|--------|
| P1-1 | Translate all 30+ Romanian strings to English | app.js, index.html | 4h |
| P1-2 | Fix all `??`/`???` encoding corruptions | index.html, app.js | 2h |
| P1-3 | Fix `MEDIUMM` → `MEDIUM` typo everywhere | index.html, app.js | 1h |
| P1-4 | Fix `Allal` → `Total` in backtest headers | app.js | 30min |
| P1-5 | Fix `>Add<` button rendering bug | index.html | 15min |
| P1-6 | Implement `Actions.clearPerformanceHistory()` | app.js | 2h |
| P1-7 | Add null-safe guard for `capital.positions` | app.js | 30min |
| P1-8 | Add skip-to-content link + `:focus-visible` styles | index.html, styles_base.css | 3h |
| P1-9 | Fix mobile navigation (add hamburger menu) | index.html, app.js, CSS | 6h |

### Phase 2 — UX & Visual Cleanup (2-3 weeks)

| # | Fix | Files | Effort |
|---|-----|-------|--------|
| P2-1 | Consolidate font sizes to 8-10 CSS variables | styles_variables.css, all CSS | 6h |
| P2-2 | Replace hardcoded colors with CSS variables | all CSS files | 8h |
| P2-3 | Standardize border-radius to 3 levels | all CSS files | 4h |
| P2-4 | Unify news card system (remove legacy `.news-card`) | styles_news.css, app.js | 4h |
| P2-5 | Harmonize watchlist/scanner dark headers with light theme | styles_watchlist.css, styles_scanner.css | 3h |
| P2-6 | Remove duplicate API stats poller | app.js or api_stats_widget.js | 1h |
| P2-7 | Add empty/error states for all data-loading sections | app.js | 4h |
| P2-8 | Fix all ARIA violations (remove role="tab" from nav, add tablist) | index.html | 2h |
| P2-9 | Add signal freshness indicator (green/yellow/red age badge) | app.js | 3h |
| P2-10 | Add tooltips for all jargon terms (Regime, Confidence, Gap Risk, etc.) | app.js, index.html | 4h |

### Phase 3 — Trading Intelligence Improvements (3-4 weeks)

| # | Fix | Files | Effort |
|---|-----|-------|--------|
| P3-1 | Surface signal thesis from `details` dict in expanded card | app.js, core/scoring_system.py | 6h |
| P3-2 | Add timeframe + expected holding period to signals | core/scoring_system.py, app.js | 4h |
| P3-3 | Add dollar risk display ("Risk $25 (2% of $1,250)") | app.js | 3h |
| P3-4 | Add confidence tier label + reasons ("Tier B, 0.5x size") | app.js | 3h |
| P3-5 | Add backtest disclaimer (V1 vs V7 logic mismatch) | index.html, app.js | 2h |
| P3-6 | Add signal filtering/sorting (score, R:R, freshness, strategy) | app.js | 6h |
| P3-7 | Add risk dashboard in Capital (drawdown, win streak, Sharpe) | app.js, core/capital_manager.py | 8h |
| P3-8 | Fix RSI null handling (don't default to 50 for stocks) | api/state_service.py, app.js | 2h |
| P3-9 | Add win rate sample size context ("WR 58% (42 trades, 30d)") | app.js | 2h |
| P3-10 | Add signal expiration/staleness warning | app.js | 3h |

### Phase 4 — Performance & Polish (2-3 weeks)

| # | Fix | Files | Effort |
|---|-----|-------|--------|
| P4-1 | Consolidate 9 polling loops into single orchestrator | app.js | 8h |
| P4-2 | Implement dirty-checking before DOM rebuilds | app.js | 6h |
| P4-3 | Debounce `saveState()` to max once per 30s | app.js | 1h |
| P4-4 | Cache `/api/live-prices` watchlist in memory | api/main.py | 2h |
| P4-5 | Add LRU eviction to `_ENDPOINT_PAYLOAD_CACHE` | api/main.py | 2h |
| P4-6 | Cap `seenNews`/`seenSignals` Sets at 1000 entries | app.js | 1h |
| P4-7 | Implement dark mode via CSS variable overrides | styles_dark.css (new) | 8h |
| P4-8 | Make tables responsive (card layout on mobile) | all CSS | 6h |
| P4-9 | Add `prefers-reduced-motion` media query for all animations | all CSS | 2h |
| P4-10 | Fix sidebar responsive collapse (hide below 1200px) | styles_base.css | 3h |

---

## 11. Exact Fix Instructions for Coding Agent

### Files to Verify/Modify

| Priority | File | What to Check/Change |
|----------|------|---------------------|
| P1 | `frontend/app.js` | Search for all Romanian strings; translate to English. Search for `MEDIUMM`; replace with `MEDIUM`. Search for `Allal`; replace with `Total`. Search for `??` in toast messages; replace with proper text. |
| P1 | `frontend/index.html` | Fix `??` encoding in dropdown options (lines 513-718). Fix `>Add<` rendering (lines 394, 2031). Translate Romanian: `Configurare`, `Adauga`, `Strategii`, `tickerele`, `Nicio`. Add skip-nav link. |
| P1 | `frontend/styles_base.css` | Add `:focus-visible` global style. Remove `outline: none` without replacement. Add responsive nav at 900px (hamburger or scrollable). |
| P1 | `frontend/styles.css` | Remove `display: none` for `.main-nav` at mobile breakpoint. |
| P2 | `frontend/styles_variables.css` | Add type scale (`--text-xs` through `--text-3xl`). |
| P2 | `frontend/styles_watchlist.css` | Replace dark header (`#0f172a`, `#1e293b`) with light theme colors. |
| P2 | `frontend/styles_scanner.css` | Same dark header fix. |
| P2 | `frontend/styles_news.css` | Remove legacy `.news-card` styles. |
| P2 | `frontend/styles_skeleton.css` | Add `prefers-reduced-motion` override for all animations. |
| P3 | `core/scoring_system.py` | Add `signal_thesis` field to ScoreResult. Add `timeframe` and `expected_holding_period`. |
| P3 | `core/signals.py` | Add `dollar_risk` and `signal_age` fields. Don't default RSI to 50 for stocks. |
| P3 | `core/capital_manager.py` | Add drawdown tracking, win/loss streak, Sharpe calculation. |
| P3 | `api/state_service.py` | Return `col_1h` consistently; don't default RSI to 50. |
| P3 | `core/backtester.py` | Add comment about V1 vs V7 logic mismatch. |
| P4 | `api/main.py` | Cache live-prices watchlist in memory. Add LRU to endpoint cache. Remove duplicate file reads. |

### How to Test Each Fix

1. **Romanian translations**: Search `app.js` for diacritics (`ă`, `ț`, `ș`, `î`, `â`, `Ă`, `Ț`, `Ș`, `Î`, `Â`) and common RO words (`Nu`, `este`, `pentru`, `care`, `acest`)
2. **Encoding fixes**: Search `index.html` and `app.js` for `??` and `???`
3. **MEDIUMM typo**: Search all files for `MEDIUMM`
4. **Null safety**: Open Capital tab before server responds — should show loading state, not crash
5. **Mobile nav**: Resize browser to 800px — should show hamburger or scrollable tabs, not hidden nav
6. **Focus styles**: Tab through page with keyboard — every interactive element should show visible focus ring
7. **Signal thesis**: Generate a signal, expand card — should see 1-2 sentence natural language explanation
8. **Performance**: Open DevTools Network tab on Home — should see <8 requests/min when idle
9. **Dark mode**: Toggle system dark mode — skeleton loaders should not flash differently from app
10. **Responsive**: View on 375px width — all content should be accessible without horizontal scroll