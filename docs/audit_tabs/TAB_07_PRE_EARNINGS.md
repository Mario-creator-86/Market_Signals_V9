# Forensic Audit Report — TAB 7: PRE-EARNINGS DASHBOARD (`#view-preearnings`)

**Auditor:** Senior Quantitative Systems Engineer, Full-Stack Software Architect & UX/UI Specialist  
**System Under Test:** Market Signals V9 (Production Release)  
**Date of Audit:** 2026-09-04  
**Status:** COMPLETE & VERIFIED (41/41 Schema/Unit Tests Passed, 51/51 Scanner/Integration Tests Passed, 30/30 Frontend Contract Tests Passed)

---

## Executive Summary

The **Pre-Earnings Dashboard** (`#view-preearnings`) is one of the most sophisticated quantitative modules in Market Signals V9. It serves as an autonomous event-driven intelligence radar designed to analyze upcoming quarterly earnings candidates across US equities. Unlike simplistic calendar widgets, Tab 7 implements an uncompromising **multi-factor scoring engine**, **peer read-through network**, **strict date verification gating**, **range-proxy volatility fallback**, and an **evidence-weighted autonomous trading gate** (`ENTRY_WINDOW_OPEN`, `WAITING_FOR_ENTRY_WINDOW`, `BLOCKED_OPPOSITE_OPEN_POSITION`).

### Key Audit Findings
1. **Uncompromising Data Honesty (Strict Anti-Hallucination Policy):**  
   Where commercial options feeds (IV implied move, Put/Call ratios) or historical EPS samples are absent (e.g., recent IPOs or API gaps), the system refuses to invent synthetic data. It falls back to an explicit **Range Proxy** (median intraday high-low volatility) and marks unverified historical names as `N/A / INSUFFICIENT_DATA`, routing them into a dedicated review queue rather than polluting directional trade rankings.
2. **Critical Forensic Discovery — eToro Retail Context Disconnect:**  
   The backend service (`PreEarningsAgent._fetch_etoro_context` in [`agents/pre_earnings_agent.py`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/pre_earnings_agent.py#L5087-L5120)) actively queries `core/etoro_client.py` and enriches candidates with real-time eToro retail context (`holders`, `popularity_rank`, `buy_pct`, `sell_pct`, `pe_ratio`, `spread`, and 2 social community headlines), storing them in `data/pre_earnings.json`. However, the frontend (`frontend/app.js`) **completely omits rendering this data**, representing a missed UI opportunity.
3. **Execution Gating & Trading Blackouts:**  
   Pre-earnings entries are strictly time-gated via [`core/market_hours.py:is_pre_earnings_entry_window`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/market_hours.py#L251-L320). Positions are eligible for execution only within a 2-hour window prior to market close (14:00–16:00 ET) on earnings day for AMC (After Market Close) or on the prior trading day for BMO (Before Market Open). Cross-horizon conflicts with existing open positions block execution automatically.

---

## 1. Architecture & Reverse Data Flow Map

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                  BROWSER UI (FRONTEND)                                 │
│  [#pe-stat-total, #pe-stat-high-risk, #pe-stat-bullish, #pe-stat-avg-conf, #pe-stat-na]│
│  [#pe-reported-strip] · [Filter Command Center: Scope / Signal Quality / Output View]  │
│  [#pe-top-10-grid] · [.pe-table Matrix (4 Split Sections)] · [#preearnings-details]   │
└──────────────────────────────────────────┬─────────────────────────────────────────────┘
                                           │
                           JS Controller (frontend/app.js)
                           - UI.loadPreEarningsData() [app.js:13813]
                           - UI.renderPreEarnings() [app.js:15602]
                           - UI._filterPreEarningsItems() [app.js:14649]
                           - UI._sortPreEarningsItems() [app.js:14738]
                           - UI._renderPreEarningsTop10() [app.js:15368]
                           - UI._renderPreEarningsPulse() [app.js:15457]
                           - UI.renderPreEarningsDetails() [app.js:14845]
                           - UI.renderPreEarningsTrackRecord() [app.js:13948]
                                           │
                               HTTP GET /api/pre-earnings
                                           │
┌──────────────────────────────────────────▼─────────────────────────────────────────────┐
│                                 API LAYER (api/main.py)                                │
│  - Endpoint: GET /api/pre-earnings [api/main.py:7429]                                  │
│  - Auxiliary: GET /api/pre-earnings/track-record [api/main.py:7815]                    │
│  - Auxiliary: GET /api/pre-earnings/refresh-status [api/main.py:7660]                  │
│  - Action: POST /api/pre-earnings/refresh [api/main.py:7615]                           │
│  - Action: POST /api/pre-earnings/refresh-symbol [api/main.py:7693]                    │
│  - Action: POST /api/pre-earnings/refresh-ai [api/main.py:7726]                        │
│  - Action: POST /api/pre-earnings/coverage/warmup [api/main.py:7754]                   │
│  Cache Pipeline: In-Memory Singleton -> _endpoint_payload_get (60s) -> Disk Snapshot │
└──────────────────────────────────────────┬─────────────────────────────────────────────┘
                                           │
┌──────────────────────────────────────────▼─────────────────────────────────────────────┐
│                           CORE ENGINE & AGENT LAYER                                    │
│  - agents/pre_earnings_agent.py (PreEarningsAgent singleton, 12,903 lines)             │
│  - core/pre_earnings/score.py (compute_pre_earnings_score, compute_data_confidence)   │
│  - core/pre_earnings/decision.py (evaluate_pre_earnings_decision, _autonomy_clamp)     │
│  - core/market_hours.py (is_pre_earnings_entry_window)                                 │
│  - core/stock_data_client.py (StockDataClient: Yahoo Finance, FinnHub, Chart history) │
│  - core/etoro_client.py (eToro retail instrument search, rates, community feed)       │
│  - agents/economic_calendar_agent.py (Nasdaq scraper fallback, event deduplication)    │
│  - agents/news_tab_agent.py (Multi-source news scanner & sentiment indexer)            │
│  - core/ollama_client.py (Local LLM Pre-Earnings Pulse & single-symbol analysis)      │
└──────────────────────────────────────────┬─────────────────────────────────────────────┘
                                           │
┌──────────────────────────────────────────▼─────────────────────────────────────────────┐
│                                 PERSISTENT STORAGE                                     │
│  - data/pre_earnings.json (Current verified state & candidates snapshot)                │
│  - data/pre_earnings_feedback.json (Track record outcomes & settled hit-rates)         │
│  - data/pre_earnings_ai_cache.json (Persistent LLM analysis cache)                     │
│  - data/pre_earnings_pulse_history.json (Pulse snapshot timeline)                      │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Detailed Component Reverse Engineering

### 2.1 Top 5 KPI Summary Cards
- **DOM Container:** `<div class="pe-stats-row">` ([`frontend/index.html:1293-1314`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/index.html#L1293-L1314))
- **JS Controller:** `populateStats()` inside `UI.renderPreEarnings` ([`frontend/app.js:15744-15767`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L15744-L15767))
- **Card Breakdown:**
  1. `#pe-stat-total` (**Analyzed Candidates**): `activeUniverse.length` (candidates currently analyzed, excluding news-confirmed `ALREADY_REPORTED` events).
  2. `#pe-stat-high-risk` (**High Gap Risk**): Count of candidates where `Number(it.gap_risk || 0) >= 70`. Styled with `.text-red`.
  3. `#pe-stat-bullish` (**Bullish Bias**): Count of candidates where `UI._preEarningsDirectionLabel(it) === 'LONG'`. Styled with `.text-green`.
  4. `#pe-stat-avg-conf` (**Avg Confidence**): Arithmetic mean of `it.confidence_score` computed strictly over items with finite numeric scores (avoids penalizing the universe average with uncalculated rows).
  5. `#pe-stat-na-risk` (**N/A Risk**): Count of candidates where `UI._preEarningsGapRiskLevel(it) === 'none'` (companies without verified historical quarters, e.g., recent IPOs).

### 2.2 Reported Strip (`#pe-reported-strip`, `#pe-reported-chip`, `#pe-reported-list`)
- **DOM Location:** [`frontend/index.html:1321-1325`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/index.html#L1321-L1325)
- **JS Controllers:** `UI.togglePreEarningsReported()` ([`frontend/app.js:14796-14806`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L14796-L14806)) and rendering logic in [`frontend/app.js:15805-15857`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L15805-L15857).
- **Functionality & Data Integrity:**
  - Merges `data.reported_today` from the backend and any item carrying the badge `ALREADY_REPORTED`.
  - Driven by news sentiment and headline keyword extraction (`AlreadyReportedDetector` in [`agents/pre_earnings_agent.py`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/pre_earnings_agent.py)). When headlines confirm results have already crossed the tape (e.g., "reports Q2 EPS of $1.15 vs $1.10 est"), the ticker is immediately stripped from forward-looking LONG/SHORT setups and moved to this collapsible panel, preventing stale orders on past events.

### 2.3 Filter Command Center
- **DOM Location:** [`frontend/index.html:1327-1512`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/index.html#L1327-L1512)
- **JS Controllers:** 
  - Filtering: `UI._filterPreEarningsItems()` ([`frontend/app.js:14649-14736`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L14649-L14736))
  - Sorting: `UI._sortPreEarningsItems()` ([`frontend/app.js:14738-14757`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L14738-L14757))
  - Market Cap Presets: `UI.applyCapPreset()` ([`frontend/app.js:14763-14773`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L14763-L14773))
  - Filter Reset: `UI.resetPreEarningsFilters()` ([`frontend/app.js:14777-14794`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L14777-L14794))
  - Debouncer: `UI.schedulePreEarningsRender()` ([`frontend/app.js:14808-14812`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L14808-L14812)) (120ms debounce prevents CPU lockup during slider drags)
- **Three-Column Grid Structure:**
  1. **Scope (`.pe-filter-group--scope`):** Range (1, 3, 7, 10, 14, 21, 30 days), Session (ALL, BMO, AMC), Dynamic Sector dropdown (populated from payload), MarketCap preset (`ALL`, `MEGA` >=$10B, `MID` $2B-$10B, `SMALL` <$2B) and custom min/max $B inputs.
  2. **Signal Quality (`.pe-filter-group--signal`):** Direction (`ALL`, `LONG`, `SHORT`, `NEUTRAL`, `INSUFFICIENT_DATA`), Risk Tier (`ALL`, `HIGH`, `MED`, `LOW`, `N/A`), Min GapRisk slider (0–100), Min Confidence slider (0–100).
  3. **Output & Search (`.pe-filter-group--view`):** Ticker symbol search input, Sort mode (`GapRisk`, `Trader score`, `Bias strength`, `Confidence`, `Earnings date`), Live result set status indicator.

### 2.4 Top 10 High-Conviction Decision Board (`#preearnings-top10`)
- **DOM Container:** `<div id="preearnings-top10" class="pe-top10-card">` ([`frontend/index.html:1515`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/index.html#L1515))
- **JS Controller:** `UI._renderPreEarningsTop10()` ([`frontend/app.js:15368-15455`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L15368-L15455))
- **Backend Generator:** `PreEarningsAgent._compute_top_predictions()` ([`agents/pre_earnings_agent.py:6750-6850`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/pre_earnings_agent.py#L6750-L6850))
- **Key Visual & Quantitative Attributes:**
  - Rank badge `#1` through `#10`.
  - Conviction Bar: **Trader Score (0–100)**. Explicitly marked with the subtext: *"trader score · not probability"*.
  - Provenance & Truth Tag: `UPCOMING · high/medium date · source`.
  - Execution State Badges:
    - `.is-ready`: `<i class="fa-solid fa-circle-check"></i> Entry window open` (when current time is within 2h before market close on the appropriate entry day).
    - `.is-waiting`: `<i class="fa-regular fa-clock"></i> Waiting for entry window` (valid setup, but too early to enter).
    - `.is-conflict`: `<i class="fa-solid fa-code-compare"></i> Active SHORT/LONG · opposite research · no order` (blocks execution if an opposite open swing position exists).
  - AI State: Displays model verdict (`AI LONG · 85% conf`) or `.is-pending` if headlines changed since the last LLM review.

### 2.5 Matrix Table (`#preearnings-sections` -> `.pe-table`)
The main matrix divides the universe into four distinct, non-overlapping semantic sections ([`frontend/app.js:16099-16104`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L16099-L16104)):
1. **`HIGH GAP RISK — NO DIRECTIONAL CALL`**: Neutral setups where high volatility/gap is expected, but directional lean is below statistical edge.
2. **`LONG BIAS SIGNALS`**: High conviction bullish setups.
3. **`SHORT BIAS SIGNALS`**: High conviction bearish setups.
4. **`NEEDS COVERAGE / DATE UNAVAILABLE`**: Unscored or partial-data tickers (capped at 15 items by default with a "Show all" toggle).

#### Table Column Architecture & Data Mapping:
| Column Header | UI Element / Class | Data Source Field | Formatting & Logic |
| :--- | :--- | :--- | :--- |
| **Ticker** | `.pe-cell-ticker` | `row.symbol`, `row.session`, `row.earnings_time_utc`, `row.range_proxy_pct` | Symbol in accent color; session + UTC formatted date (`DD MMM`); expected move pill (`~X.X% exp`) from range proxy. |
| **GapRisk** | `.pe-cell-gap-risk` | `row.gap_risk`, `row.gap_risk_level` | Score 0–100 with dynamic color fill bar (Green <40, Orange 40–69, Red ≥70). Displays honest `N/A` if historical samples < 1. |
| **Final Blended Signal** | `.pe-cell-recommendation` | `row.final_direction_bias_score`, `row.direction_label` | Signed score (-100 to +100). Never displays `%` (directional score, not probability). Filled bar with contrast-safe colors. |
| **Structured vs LLM** | `.pe-dir-cell` | `row.direction_label`, `row.why`, `row.recent_reactions`, `row.ai_decision`, `row.peer_readthrough` | Direction badge (`▲ LONG` / `▼ SHORT` / `◆ NEUTRAL`), human rationale string, post-earnings reaction badges (`▲+4.2%`), AI LLM verdict chip, and peer read-through chip (`Peers +6.8%`). |
| **EPS Beat Rate** | `.pe-cell-beat-rate` | `row.beat_rate_historical`, `row.beat_rate_beats`, `row.beat_rate_samples`, `row.beat_rate_source` | Historical beat percentage (e.g. `75%`), fraction (`6/8 · Yahoo`), or `N/A · no EPS pairs` with tooltip explaining missing estimates. |
| **Analyst Consensus** | `.pe-cell-analyst` | `row.analyst_target_upside_pct`, `row.analyst_recommendation`, `row.analyst_count` | Target upside percentage (`+18.4%`), consensus rating (`Buy`, `Hold`), and analyst coverage count. |
| **Data Coverage** | `.pe-cell-quality` | `row.coverage_score`, `row.coverage_contributors` | Availability index 0–100% across 6 inputs. Highlights missing components in orange (e.g., `missing: news, range`). |

---

## 3. Forensic Analysis: Failovers, Fallback Chains & Edge Cases

### 3.1 Fallback Chain 1: Calendar Data & The Nasdaq Scraper
- **Primary Provider:** Yahoo Finance calendar & Finnhub via [`agents/economic_calendar_agent.py`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/economic_calendar_agent.py).
- **Secondary / Discovery Provider:** Public Nasdaq/Zacks earnings calendar scraper (`_fetch_nasdaq_earnings_calendar` in [`agents/economic_calendar_agent.py:1143-1254`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/economic_calendar_agent.py#L1143-L1254)).
  - **Forensic Detail:** Nasdaq provides broad date coverage based on historical algorithmic estimates. The engine marks Nasdaq/Zacks dates with priority `50` (`event_date_confidence = "low"`).
  - **Failover Behavior:** If Nasdaq blocks requests (e.g. Cloudflare / 403 / timeout) or returns empty rows, `self._nasdaq_last_fetch_succeeded = False`. The system falls back to the persisted cache in `data/economic_calendar.json` and cached events in `data/pre_earnings.json`.
  - **Date Conflict Resolution:** If two providers specify differing dates for the same fiscal quarter, [`agents/pre_earnings_agent.py:4117-4170`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/pre_earnings_agent.py#L4117-L4170) refuses to pick one. It flags `event_status = "DATE_CONFLICT"`, assigns badge `CALENDAR_MISMATCH`, and **blocks the ticker from execution eligibility**.

### 3.2 Fallback Chain 2: Options-Implied Move (IV) vs Range Proxy
- **Industry Expectation:** Professional platforms use live options chains to calculate front-month at-the-money (ATM) straddle implied volatility move ($\text{Implied Move} \approx 0.8 \times \text{ATM Straddle Price}$).
- **V9 Provider Reality:** Market Signals V9 does **not** maintain an expensive commercial options-flow API feed (e.g., ORATS, CBOE, Tradier).
- **Forensic Implementation ([`agents/pre_earnings_agent.py:5068-5085, 5809-5812`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/pre_earnings_agent.py#L5068-L5085)):
  ```python
  # No real options-flow feed (needs a paid provider) — leave unknown rather than
  # faking a neutral 1.0 that renders as a misleading per-ticker "50".
  calls_vs_puts = None
  return {
      "implied_move_pct": None,
      "iv_rank": None,
      "calls_vs_puts_ratio": None,
  }
  ```
- **Fallback Mechanism:** The system calculates a **Range Proxy** (`range_proxy_pct`) using daily intraday High-Low ranges over the past 20 trading sessions (`StockDataClient.get_historical_chart`).
- **UI Transparency:** In [`frontend/app.js:14884`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L14884), the tooltip explicitly states:
  > *"Recent intraday high-low range as a volatility proxy; this is not options-implied volatility."*
  This is a model implementation of anti-hallucination quant engineering.

### 3.3 Fallback Chain 3: Missing Historical Quarters & New IPOs
- **Primary Source:** Yahoo Finance `get_earnings_dates(limit=16)` via `StockDataClient` ([`agents/pre_earnings_agent.py:7309`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/pre_earnings_agent.py#L7309)).
- **What happens if Yahoo has no historical quarters (e.g. IPOs, micro-caps)?**
  1. `historical_samples == 0`.
  2. `compute_data_confidence()` ([`core/pre_earnings/score.py:44-73`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/pre_earnings/score.py#L44-L73)) sets:
     - `data_confidence = "NONE"`
     - `data_confidence_reason = "No historical quarters... GapRisk unavailable. DirectionBias from news."`
     - `is_insufficient = True`
  3. `compute_pre_earnings_score()` ([`core/pre_earnings/score.py:88-158`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/pre_earnings/score.py#L88-L158)) sets `score = 0`.
  4. In `PreEarningsAgent._prediction_score_details()` ([`agents/pre_earnings_agent.py:6585`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/pre_earnings_agent.py#L6585)):
     - `blockers.append("gap_risk_unavailable")`
  5. In UI: Rendered as `N/A` under GapRisk, tagged `INSUFFICIENT_DATA`, and segregated into `NEEDS COVERAGE / DATE UNAVAILABLE`.

### 3.4 Fallback Chain 4: The eToro Retail Context Disconnect
- **Backend Fetch:** [`agents/pre_earnings_agent.py:5087-5120`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/pre_earnings_agent.py#L5087-L5120) runs `_fetch_etoro_context()`:
  - Fetches instrument metadata (`holders`, `popularity_rank`, `buy_pct`, `sell_pct`, `pe_ratio`, `esg_score`).
  - Fetches live bid/ask/spread.
  - Fetches top 2 community social posts from the instrument's eToro feed.
  - Successfully writes this to `data/pre_earnings.json` under `item.etoro_context`.
- **Live Probe Verification:**
  ```json
  "etoro_context": {
    "instrument_id": 6503,
    "holders": 4136,
    "popularity_rank": 4136,
    "buy_pct": 100.0,
    "sell_pct": 0.0,
    "pe_ratio": -0.62,
    "esg_score": 58.45,
    "bid": 2.8,
    "ask": 2.81,
    "spread": 0.01,
    "social_headlines": [...]
  }
  ```
- **UI Bug / Forensic Gap:** A search across `frontend/app.js` and `frontend/index.html` shows **zero references to `etoro_context` or `buy_pct`**. The data is fetched, cached, and transmitted via HTTP, but remains completely hidden from the user.

---

## 4. UI/UX & Ergonomic Audit

### 4.1 Layout, Responsiveness & Density
- **Viewport Strategy:** Constrained to `max-width: 1600px` with 24px/40px padding. Responsive CSS in `frontend/styles_preearnings.css` utilizes CSS Grid for the 3-section filter center (`1.32fr : 1.08fr : 0.8fr`) and transitions to single-column flex under 900px.
- **Scroll Memory ([`frontend/app.js:16093-16109`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L16093-L16109)):**  
  Every section records `scroller.scrollTop` before DOM replacement and restores it immediately after re-render. A background refresh or filter adjustment does not snap the user's scroll position back to the top.
- **Visual Contrast & Color Grading:**  
  Uses CSS variables `--pe-green`, `--pe-red`, `--pe-orange`, `--pe-accent`, and `--pe-border` with dedicated contrast-safe text variants (`--pe-green-text`, `--pe-red-text`) to ensure WCAG AA compliance against dark backgrounds.

### 4.2 Cognitive Load & Terminology Evaluation
- **Strengths:**
  - **"Trader score is not probability":** Explicit disclaimer displayed prominently in the Top 10 head and table tooltips.
  - **Factor Decomposition Widget ([`frontend/app.js:15030-15061`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L15030-L15061)):** Diverging horizontal bar chart centered at neutral (green bar right = LONG, red bar left = SHORT), detailing exactly how each structured input and the LLM verdict contributed to the final score.
- **Areas for Improvement:**
  - **Session Jargon:** Acronyms `BMO` (Before Market Open) and `AMC` (After Market Close) are second nature to seasoned institutional traders, but retail traders frequently confuse AMC with the ticker symbol `AMC` (AMC Entertainment). A subtle tooltip on the session filter would eliminate ambiguity.
  - **Expected Move vs Implied Move:** While tooltips clarify that the expected move is derived from the range proxy, the column header in the table is labeled simply `Ticker` and embeds `~X.X% exp` underneath. Adding an explicit column or hover badge would increase clarity.

---

## 5. Industry Benchmark Comparison

| Evaluation Feature | Market Signals V9 (Tab 7) | Earnings Whispers | Koyfin Earnings | OptionsPlay / ORATS | TrendSpider Earnings |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Calendar Discovery** | Yahoo + Nasdaq scraper + Finnhub fallback | Direct corporate IR verification | Multi-source institutional feed | Exchange CBOE calendar | Corporate feeds + PR Newswire |
| **Event Date Verification** | `DATE_VERIFIED` vs `DATE_ESTIMATED` priority scoring | Proprietary confirmation badge | Verified date timestamps | Confirmed options expiration | Verified calendar badges |
| **Implied Move Engine** | **Range Proxy (Intraday High-Low)** (Honest fallback) | Average post-earnings move | N/A (Fundamental focus) | **Real ATM Straddle IV Implied Volatility** | Options implied move cone |
| **Post-Earnings Track Record** | **Last 3 reports move + internal directional accuracy** | Historical beat/miss reaction charts | 10-year historical beat/miss graphs | Historical straddle win-rate (over/under) | Multi-quarter price gap stats |
| **Retail Sentiment Integration** | **eToro holders & buy%** (in API, UI pending) | Proprietary Investor Sentiment Vote | Institutional 13F ownership | Retail vs Institutional options flow | Social sentiment index (Stocktwits/X) |
| **AI LLM Analysis** | **Ollama / Cloud LLM** (news + catalyst + peer synthesis) | None (Pure human / quantitative) | None (Data terminal) | None | AI automated candlestick recognition |
| **Autonomous Execution Gating** | **Strict 2h entry window + position conflict checks** | None (Information only) | None (Information only) | Options strategy generator (manual) | Automated bot trading via Webhooks |

---

## 6. SWOT & Strategic Recommendations

```
┌────────────────────────────────────────────────────────┬────────────────────────────────────────────────────────┐
│                      STRENGTHS                         │                      WEAKNESSES                        │
│ - Uncompromising quant honesty (Anti-hallucination)    │ - eToro retail context gathered but not shown in UI    │
│ - Transparent factor decomposition bar charts          │ - Lack of real options chain / ATM straddle IV pricing │
│ - Strict entry-window & position-conflict gating       │ - No historical EPS revision progression charts        │
│ - Automatic peer read-through clustering               │ - BMO/AMC abbreviations lack basic novice tooltips     │
├────────────────────────────────────────────────────────┼────────────────────────────────────────────────────────┤
│                    OPPORTUNITIES                       │                        THREATS                         │
│ - Surface eToro retail sentiment & social stream in UI │ - Yahoo Finance scraping fragility & rate limiting     │
│ - Integrate low-cost options API (e.g. Tradier/ORATS)  │ - Nasdaq calendar scraper Cloudflare 403 blocks        │
│ - Add post-earnings price gap charting visualizer      │ - Corporate earnings rescheduling late notices         │
└────────────────────────────────────────────────────────┴────────────────────────────────────────────────────────┘
```

### Strategic Action Items
1. **Surface eToro Retail Context in Details Panel (High Priority, Low Effort):**  
   Update `UI.renderPreEarningsDetails()` in [`frontend/app.js`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js) to render a dedicated `eToro Retail Sentiment` card displaying `holders`, `buy_pct / sell_pct` ratio bar, and the social headline stream already provided in `item.etoro_context`.
2. **Options Data Upgrade Path:**  
   When a paid options provider is integrated in future releases, route options-implied volatility into `components.options_implied_move`. The frontend already contains backward-compatible fallback logic ([`app.js:14915`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L14915)) that will seamlessly render real implied move once available.
3. **Novice Glossary Tooltips:**  
   Add `title="Before Market Open (morning report before 09:30 ET)"` and `title="After Market Close (afternoon report after 16:00 ET)"` to the session filter options in `frontend/index.html`.

---

## 7. Forensic Verification Evidence (Test Suite Results)

The following test suites were executed live during this forensic audit:

### Test Suite 1: Contract & Scoring Unit Tests
```powershell
pytest tests/contract/test_pre_earnings_schema_contract.py tests/unit/test_pre_earnings_score.py tests/unit/test_pre_earnings_entry_window.py -v
============================= 41 passed in 4.59s ==============================
```
- Validated complete JSON schema contract compliance (`tests/contract/schemas/pre_earnings.schema.json`).
- Verified that `compute_pre_earnings_score()` never upgrades confidence bands on thin data and treats score as signal-strength composite, not win probability.
- Verified that `is_pre_earnings_entry_window()` strictly enforces AMC same-day 2h window and BMO previous-day 2h window.

### Test Suite 2: Scanner & Cache Coherence Integration Tests
```powershell
pytest tests/test_pre_earnings_scanner.py tests/integration/test_pre_earnings_cache_coherence_api.py -v
============================= 51 passed in 0.79s ==============================
```
- Verified that `CalendarMismatchDetector` flags stale and unverified dates.
- Verified that `AlreadyReportedDetector` identifies earnings results that already occurred.
- Verified that memory caches bust correctly upon fresh agent persistence.

### Test Suite 3: Frontend Contract & Track Record Tests
```powershell
pytest tests/integration/test_pre_earnings_track_record_api.py tests/test_pre_earnings_premium_frontend.py -v
============================= 30 passed in 0.57s ==============================
```
- Verified uniqueness of all 21 DOM containers and input controls.
- Verified responsive CSS grid breakpoints and desktop command center balance.
- Verified that nullable scores (`N/A`) are never coerced to false zeros.
