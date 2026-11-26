# AUDIT FORENSIC V2: MODALUL CHART WORKSPACE (`#chart-workspace-modal`) + modulul Deep Analysis (`#analysis-modal`)

**Data audit:** 2026-09-05 (runda 2, re-verificare față de `docs/audit_tabs/MODAL_CHART_WORKSPACE.md` din 2026-09-04)
**Server live testat:** `http://127.0.0.1:8003` (doar GET, 8 cereri, dump-uri în `$TMP` — nimic în repo)
**Metodă:** trasare field-by-field UI → payload → endpoint → producător backend → sursă de date; live-curl; git log; pytest local.

---

## VERDICT GENERAL

**FUNCȚIONAL, FĂRĂ REGRESII FAȚĂ DE RUNDA 1 — dar cu 1 defect P1 nou (preț învechit din cache-ul persistent afișat ca „Market price") și 2 P2 moștenite nerezolvate.**

- Modalul se deschide din tape/scanner prin `UI.openTickerChart` (`frontend/app.js:4567-4570`) care deleagă pe `ChartWorkspace.open` (`frontend/chart_workspace.js:578`). Markup: `frontend/index.html:2618-2725`.
- **Chart engine**: datele OHLCV și motorul cantitativ V9 (`/api/chart/{symbol}`) sunt **sănătoase live** — BTC-USD 5m: 240 bare, sursă `binance`; AAPL 5m: 240 bare, `yahoo_chart`; GC=F 1d: 240 bare, `yahoo_chart`. Toate payload-urile conțin schema completă `bars/state/annotations/indicators/replay_states`.
- **Dar**: graful randat în modal este un **iframe TradingView** (`chart_workspace.js:156-212`, `index.html:2664`) care NU consumă deloc `/api/chart` — backend-ul V9 calculează pivots/BOS/CHoCH/liquidity/patterns/supertrend la fiecare request, iar modalul nu le randează niciodată (confirmat din nou — era concluzia centrală a rundei 1).
- **Test suite**: `pytest tests/test_chart_terminal_ui_contract.py tests/test_chart_api.py` → **17 passed în 0.42s** (identic cu runda 1).
- **Pipeline-ul analyze-ticker reparat în d96cda2 funcționează** (dovezi în raport, secțiunea 4).

Clasificare folosită: OK / DEAD-RENDER (randat dar fără producător activ) / DEAD-BACKEND / FAKE (inventat) / STALE (învechit) / MISCALIBRATED (calcul/text greșit).

---

## 1. TABEL FIELD-BY-FIELD — CHART WORKSPACE MODAL (`#chart-workspace-modal`)

| # | Câmp UI (selector) | Frontend (fișier:linie) | Endpoint / payload | Producător backend | Sursa finală de date | Verdict |
|---|---|---|---|---|---|---|
| 1 | Titlu `#chart-workspace-title` | index.html:2625; chart_workspace.js:588,479 | — (stare client) | — | simbol introdus/canonicalizat | OK |
| 2 | Timeframe switcher `#chart-timeframes` (1m…1d) | index.html:2629-2633; chart_workspace.js:486-493,8-11 | re-montează iframe TV (`TV_INTERVALS`); același set ca `_CHART_TIMEFRAMES` (api/main.py:3960) | — (doar widget extern) | client state + localStorage v2 | OK |
| 3 | Graful `#tv-chart-container` | index.html:2664; chart_workspace.js:156-212 | `https://www.tradingview.com/widgetembed/?…` | CDN TradingView (terț) | feed TradingView | OK (graf), dar **DEAD-RENDER pentru inteligența V9** — anotațiile din `/api/chart` nu ajung niciodată pe ecran |
| 4 | Legenda `#chart-workspace-legend` | chart_workspace.js:192-199 | — | — | text static la `iframe.onload`: „TradingView Live" | FAKE (cosmetic P3) — nu reflectă starea reală a feed-ului |
| 5 | Status bar `#chart-workspace-status` | chart_workspace.js:262-267,211 | — | — | text static `{sym} · {tf} · TradingView live feed` | FAKE (cosmetic P3) |
| 6 | Căutare simbol `#chart-symbol-input` + Load | index.html:2643-2644; chart_workspace.js:473-484,71-81 | — | `canonicalSymbol()` (aliasuri GOLD/OIL, BTCUSDT→BTC-USD) | input utilizator | OK |
| 7 | Presete piețe (BTC/ETH/AAPL/NVDA/GOLD/OIL) | index.html:2646-2653; chart_workspace.js:354-355 | — | — | client | OK |
| 8 | `Market price` `#chart-order-price` | index.html:2678; chart_workspace.js:228-243,332 | `GET /api/chart/{sym}?timeframe=5m` (poll 15s, chart_workspace.js:219-226) | `api_chart` (api/main.py:4213) → `_build_chart_payload` (4148) → `_fetch_chart_frame` (4114) | Binance→KuCoin (crypto); **doar Yahoo** (stocks/futures); cache sqlite persistent (`_load_persisted_chart_payload` 3998) | **MISCALIBRATED / STALE (P1)** — vezi Defect D1: la primul hit după restart servește payload persistent vechi (BTC: `as_of 2026-08-30`, ~6 zile) cu `cache:"stale"`; frontend-ul ia orbește `bars[-1].close` |
| 9 | `Free Capital` `#chart-order-balance` | index.html:2679; chart_workspace.js:278-300,333 | `GET /api/state?include=capital` (app.js:23998/24494) | `api_get_state` → CapitalManager | `capital.balance` — **live: 7 722.87 USD, 14 poziții** | OK |
| 10 | `#chart-order-asset` (CRYPTO/STOCK/COMMODITY) | chart_workspace.js:95-99,334 | — | — | derivat client din sufixul simbolului | OK |
| 11 | Direcție BUY/SELL `[data-paper-direction]` | index.html:2681-2684; chart_workspace.js:429-433 | — | — | client | OK |
| 12 | Mărime `#chart-order-size` + presete $100/$250/$500/10% capital | index.html:2685-2691; chart_workspace.js:541-554 | — | — | client (`10% capital` din `capital.balance`) | OK |
| 13 | Leverage `#chart-order-leverage` (1x–5x) | index.html:2693-2698; chart_workspace.js:534-538 | — | — | client | OK |
| 14 | SL/TP % + presete Scalp/Intraday/Swing | index.html:2700-2708; chart_workspace.js:557-565 | — | — | client | OK |
| 15 | Margin / Risk / Reward / R:R (`#chart-order-margin/risk/reward/rr`) | chart_workspace.js:325-343 | — | — | matematică client pe `draft.price` | OK matematic, dar **moștenește staleness-ul lui #8** (SL/TP indicativ calculate pe preț vechi) |
| 16 | `Indicative SL/TP` `#chart-order-levels` | index.html:2715; chart_workspace.js:340-343 | — | — | client (preț din #8) | OK / STALE moștenit |
| 17 | Submit `#chart-order-submit` | index.html:2717; chart_workspace.js:437-469 → `Actions.submitCapitalPaperOrder` (app.js:24023) → `API.openCapitalPosition` (app.js:1551) | `POST /api/capital/open-position` | api/main.py: guard preț live 1491-1497: `cm._get_execution_price` → **409 dacă prețul live verificat nu e disponibil**; snapshot graf `core/entry_chart_snapshot.py` | execuție paper verificată | OK (verificat la nivel de cod; POST n-a fost executat live — constrângere GET-only) |
| 18 | Poziții pe simbol `#chart-symbol-positions` | index.html:2719-2722; chart_workspace.js:372-425 | `GET /api/state` → `capital.positions[]` filtrate pe simbol | CapitalManager | chei consumate `position_id/ticker/status/direction/leverage/usd_size/entry_price/margin_used` — **toate prezente live** | OK |
| 19 | Close poziție `[data-close-paper-position]` | chart_workspace.js:409-424 → `Actions.closeCapitalPosition` (app.js:24482) | `POST /api/capital/close-position` | api/main.py: guard 409 la 1584-1593 | preț live verificat | OK (cod) |
| 20 | Status ordin `#chart-order-status` | index.html:2718; chart_workspace.js:379-385 | — | — | mesaje client | OK |
| 21 | `quoteUpdate` postMessage listener | chart_workspace.js:246-258 | — | — | nu se declanșează pe widgetembed gratuit (comentariu în cod la 215-217 recunoaște asta) | DEAD-BACKEND (inofensiv, P3) |
| 22 | Entry-chart link (din detaliile poziției) | app.js:9742-9762 | `GET /api/entry-chart/{snapshot_id}` | api/main.py:4252 → `core/entry_chart_snapshot.py` (`data/entry_charts`) | endpoint live (404 gestionat pentru id inexistent) | OK, dar **neexercitat**: `data/entry_charts/` e gol (0 fișiere) și nicio poziție nu are id de snapshot |
| 23 | Buton Refresh `#chart-refresh` + Focus mode + extern TV | index.html:2634-2637; chart_workspace.js:519-527 | — | — | client | OK |

**Trasee de deschidere ale modalului** (verificat): tape rows (`app.js:3859`), scanner rows (`app.js:4342/4374/4514/4543`) → `UI.openTickerChart` → `ChartWorkspace.open`. **Watchlist-ul (tabelul principal) NU are punct de intrare spre chart modal** — rândurile nu au onclick de deschidere (doar headerele au sortare, `app.js:5158+`). Ruta de fallback a lui `openTickerChart` (fără ChartWorkspace) deschide TradingView extern în tab nou (app.js:4571-4602) — code path mort în practică, deoarece `chart_workspace.js` e mereu încărcat (index.html:2734).

**Anotațiile calculate dar nerandate (DEAD-RENDER, confirmat live):** payload BTC-USD conține `pivots: 40, structure_events: 19, liquidity_pools: 5, chart_patterns: 19, volume_events: 77, supertrend_events: 7, indicators.supertrend: 231 puncte, replay_states: 240` — nimic din asta nu apare în modal (iframe cross-origin). Detaliere în runda 1, secțiunea „Iframe Silo" — valabilă integral.

---

## 2. TABEL FIELD-BY-FIELD — MODALUL DEEP ANALYSIS (`#analysis-modal`, deschis cu „Analyze")

Modal creat dinamic la `Actions.showAnalysisModal` (`frontend/app.js:22856-22890`), apelat din cardurile Hot List (`app.js:22139`, `22289`). Endpoint: `POST /api/analyze-ticker` (`app.js:22790-22836`, timeout 420s, `mode` din localStorage, default V1) → `api/main.py:3610` → `analyze_ticker_mode` (`api/actions_service.py:1176`) → V1: `analyze_ticker` (`api/actions_service.py:662`, cache adaptiv TTL 235-247) → `core/ticker_analyzer.analyze_ticker` (`core/ticker_analyzer.py:341`, yfinance + StockDataClient + Ollama).

| Câmp randat | Cheie payload | Producător | Verdict |
|---|---|---|---|
| Current Price + change% | `a.price`, `a.change_pct` | ticker_analyzer (bare yfinance; fallback direct yf.download la 446-505) | OK |
| RSI(14), MACD Hist, ATR, VWAP, EMA20, distanțe % | `a.rsi/macd_hist/atr/vwap/ema20/distance_*` | ticker_analyzer (indicatori pe barele MTF) | OK |
| Multi-Timeframe Trends (1D/4H/1H/30M/5M) | `a.trend_1d/4h/1h/30m/5m` | `core/ticker_analyzer.py:638-641` (get_trend pe fiecare DF) | OK |
| Best Signal (direcție/mod/profil/score) | `a.best_signal{direction,mode,profile,score}` | normalizat în wrapper-ul `api/actions_service.py:725-736` | OK |
| Strategy Scoring Matrix + Score Details | `a.mode_scores{mode}{profile}{best_score,best_direction,details}` | `_extract_current_scores` (actions_service) | OK |
| Mode decision | `a.mode_decision` | `analyze_ticker_mode` (1176-1253) | OK |
| V2 Validation Snapshot (Win Rate/PF/Expectancy/Sample + reverse check) | `a.v2.best_candidate.perf_snapshot`, `a.v2.reverse_consideration` | `analyze_ticker_v2` (actions_service:943) | OK (typo-ul `best_candidatee` reparat în d96cda2) |
| Tab Fundamentals: Market Cap/Volume/AvgVolume/P/E/EPS/52W | `a.quote`, `a.quote_batch` | `StockDataClient.get_quote` (core/stock_data_client.py:746, @staticmethod, Yahoo + enrich yfinance .info) — apelat în ticker_analyzer.py:924 | OK date; **etichetă greșită**: „Market Snapshot (FMP Quote)" — sursa e yfinance de la migrarea FMP→yfinance (P3, MISLABELED) |
| Price Performance / statements / estimates | `a.fundamentals.*` (dict-uri post-d96cda2: `analyst_estimates`, `price_change`, `enterprise_values`) | ticker_analyzer + conversie DataFrame→listă-camelCase din d96cda2 | OK |
| Tab AI Ollama: model + text + news_summary | `a.ai_analysis` (obiect sau string), `a.model`, `aiObject.news_summary.last_7d_positive/last_7d_negative/major_risk_events/last_30d_theme` | Ollama local (log: „V2 Ollama single-pass AI analysis for BMNR finished in 9.32s") | OK |
| Badge CACHED + Refresh | `a.cached` | wrapper cache `_get_cached_analysis` | OK |
| Text „Data is cached for the next 2 hours" | — | TTL real: **1h** (30 min lângă earnings) — `api/actions_service.py:235-247` | MISCALIBRATED (text, P3) |

---

## 3. DEFECTE (P1/P2/P3) CU DOVEZI

### P1

**D1 — „Market price" din order ticket poate fi învechit grav (zile), iar SL/TP/margin se calculează pe el.**
- La primul `GET /api/chart/BTC-USD?timeframe=5m` după restart, `api_chart` (api/main.py:4226-4240) încarcă payload-ul persistent din sqlite (`_load_persisted_chart_payload`, 3998-4016) și îl servește cu `cache:"stale"` + refresh în fundal.
- **Dovadă live:** răspunsul a fost `as_of: 2026-08-30T14:28` (≈6 zile vechime la momentul testului, 2026-09-05T21:09), `source: binance`, `cache: "stale"`. Cu `refresh=true` același simbol a returnat imediat `as_of: 2026-09-05T21:10`, `cache:"miss"` — providerul e sănătos, doar cache-ul served e vechi.
- Frontend-ul (`chart_workspace.js:228-243`) ia necondiționat `bars[-1].close` și îl afișează ca „Market price" (`index.html:2678`); `orderDraft()` (306-323) validează ordinul și `renderOrderPreview()` (325-343) calculează SL/TP indicativ pe acest preț. Frontend-ul ignoră complet `cache`/`as_of`/`refreshing` din payload.
- Guard salvator: la submit backend-ul refuză execuția fără preț live verificat (409, api/main.py:1491-1497) — deci nu se pot deschide poziții pe preț fals; riscul e de decizie UX (user văde un preț din urmă cu zile și nivele SL/TP irelevante).
- Notă: recomandarea P0 din runda 1 (poll `/api/live-prices`) **nu este implementată** (`chart_workspace.js:230` încă cheamă `/api/chart/…`), iar testul live pe `/api/live-prices?symbols=BTC-USD` arată că nici sursa alternativă nu e mai bună: `freshness:"STALE"`, `age_seconds: 89815` (~25h), `live:false`, `source:"Binance_orderbook_mid"`. Oreala reală este ca frontend-ul să verifice `as_of`/`cache` din payload-ul `/api/chart` și să marcheze prețul ca stală (sau să forceze `refresh=true` la deschiderea modalului).

### P2

**D2 — Stocks/commodities: lanț de fallback cu un singur furnizor (moștenit din runda 1, nerezolvat).**
- `_fetch_chart_frame` (api/main.py:4114-4146): crypto are Binance→KuCoin; pentru stocks/futures doar `_fetch_yahoo_chart_frame` → la 429/blocareYahoo rezultatul e direct `("unavailable")`. `core/stock_data_client.py` are eToro candles, dar nu e chemat aici. Runda 1, P0 #2 — neimplementat.

**D3 — Toată inteligența cantitativă a payload-ului `/api/chart` e DEAD-RENDER în modal (moștenit, structural).**
- Backend-ul rulează `analyze_market_structure + analyze_liquidity + analyze_chart_patterns + analyze_volume_structure + analyze_supertrend` la fiecare cache-miss (api/main.py:4173-4186) — inclusiv pentru poll-ul de preț al ticketului la fiecare ~2 poll-uri (TTL 5m = 20s vs poll 15s, api/main.py:3960). Modalul nu le afișează niciodată (iframe TradingView). Confirmat live: anotații prezente în payload (40 pivots etc.), zero în UI.

### P3

**D4 — Legenda și statusul „TradingView Live / live feed" sunt text static**, nelegate de starea reală a feed-ului (chart_workspace.js:192-199, 211).
**D5 — Etichetă „Market Snapshot (FMP Quote)" în tabul Fundamentals** (app.js, blocul `fundamentalHtml`) — datele vin din yfinance post-migrare; label FMP e relicvă. Risc de inducere în eroare privind sursa.
**D6 — Textul „Data is cached for the next 2 hours"** din loader-ul analizei (app.js:22933) vs TTL real 1h/30min (api/actions_service.py:235-247).
**D7 — `data/entry_charts/` gol** — funcția „imutrabil entry evidence" e cod-OK dar fără artefacte pe serverul curent; orice link spre snapshot ar da 404 (gestionat UI la app.js:9742-9762).
**D8 — Watchlist-ul nu are intrare spre chart modal** (doar tape/scanner deschid `#chart-workspace-modal`) — inconsistență de navigație raportată la scopul declarat „modal deschis din signals/watchlist".

---

## 4. STATUSUL PIPELINE-ULUI analyze-ticker (reparat în d96cda2) — RE-VERIFICAT

Constrângerea GET-only a interzis un POST live; verificarea s-a făcut prin cod + jurnale + artefacte:
- **Cod**: lanțul `POST /api/analyze-ticker` (api/main.py:3610-3639) → `analyze_ticker_mode` (api/actions_service.py:1176-1253, suportă V1/V2/BOTH_AUTO_BEST) → wrapper `analyze_ticker` (662-940: cache, fallback dict complet dacă analyzerul întoarce None, normalizare best_signal, strategy_comparison, quote+quote_batch) → `core/ticker_analyzer.analyze_ticker` (341, semnături reparate + guards per-apel din d96cda2). Toate cheile consumate de frontend există în shape-ul produs (tabelul din secțiunea 2).
- **Dovadă de execuție reală post-fix** (`logs/server_restart_newsmeta.err.log:1266,1290`): `API analyze-ticker request: BMNR (include_heavy=False, disable_ollama=False, force_refresh=False)` → `POST /api/analyze-ticker 200 în 20 895 ms` + `V2 Ollama single-pass AI analysis for BMNR finished in 9.32s`. Pipeline-ul **funcționează**.
- Ultimele comit-uri pe aceste fișiere: `200cd2e` (audit round 2) și `c7edafc` (fix audit-leak) — ating `core/ticker_analyzer.py`/`api/actions_service.py`, fără regresii vizibile pe acest lanț (17/17 teste chart trec; lanțul analyze nu e acoperit de aceste teste — risc rezidual menționat).

---

## 5. REGRESII vs RUNDA 1 (git log + re-test)

| Item runda 1 | Stare actuală |
|---|---|
| Chart payload + anotații pe schema comună | OK — confirmat live pe 3 clase de active (crypto/stock/futures) |
| Failover crypto Binance→KuCoin | Cod neschimbat (api/main.py:4119-4128); Binance activ live |
| Yahoo singurul furnizor stocks (vulnerabilitate 2.3) | **Nerezolvat** (D2) |
| Iframe-ul TV nu suprapune anotațiile V9 | **Nerezolvat** (D3) |
| P0 #1: mută polling-ul preț pe `/api/live-prices` | **Neimplementat** — și nici n-ar fi fost suficient singur (live-prices BTC e STALE 25h) |
| Teste `test_chart_terminal_ui_contract.py` + `test_chart_api.py` | **17 passed** (identic) |
| Regresii introduse de comiturile după 2026-09-04 (7f26fd2, c7edafc, e9eda27…) | Nu s-au identificat pe cările modalului; `chart_workspace.js` neatinse de acestea (ultima modificare relevantă: e9eda27 pe index.html, cosmetic pre-earnings) |
| **NOU (P1)** | D1 — prețul din ticket servit din cache persistent vechi la primul hit (nu fusese prins în runda 1 pentru că testul prin apel direct Python ocolea cache-ul HTTP/sqlite) |

---

## 6. RECOMANDĂRI PRIORITIZATE

1. **P1 (D1)** — în `fetchLivePrice` (chart_workspace.js:228): la deschiderea modalului folosiți `refresh=true` pe primul fetch și respingeți barele cu `cache:"stale"` mai vechi de N secunde (comparare `as_of` vs `Date.now()`), afișând „—" sau un badge de avertizare în `#chart-order-price` + dezactivând submit-ul până vine preț curent. (Complementar: gorila de la submit 409 rămâne ultimate guard.)
2. **P2 (D2)** — fallback eToro/StockDataClient în `_fetch_chart_frame` pentru stocks/futures când Yahoo eșuează sau < 20 bare.
3. **P2 (D3)** — overlay nativ (Lightweight Charts) sau minim un panou „V9 Signals" (pivots/BOS/liquidity/supertrend) în sidebar-ul modalului, ca `/api/chart` să nu mai fie calculat degeaba; alternativ, limitați calculul anotațiilor când requestul e doar de preț (param `?price_only=true`).
4. **P3** — corectați eticheta „FMP Quote" → „yfinance", textul „2 hours" → „1 hour", legenda „TradingView Live" → stare reală; adăugați buton chart pe rândurile watchlist-ului.
5. **P3 (D7)** — generați entry-chart snapshot la deschiderea pozițiilor noi sau eliminațireferințele moarte din UI dacă funcția e abandonată.

---

## 7. JURN DE VERIFICARE LIVE (GET-only, 8 cereri)

| # | Cerere | Rezultat-cheie |
|---|---|---|
| 1 | `GET /api/live-prices?symbols=BTC-USD` | 200; BTC-USD `freshness:"STALE"`, `age_seconds:89815`, `live:false`, `source:"Binance_orderbook_mid"` |
| 2 | `GET /api/chart/BTC-USD?timeframe=5m` | 200; 240 bare, `source:binance`, `cache:"stale"`, **`as_of:2026-08-30T14:28`** (≈6 zile) |
| 3 | `GET /api/chart/AAPL?timeframe=5m` | 200; 240 bare, `source:yahoo_chart`, `cache:"stale"`, `as_of:2026-09-05T17:37`; ultima bară flat (O=H=L=C, volum 0) |
| 4 | `GET /api/state?include=capital…` | 200; `capital.balance:7722.87`, 14 poziții, toate cheile consumate de ticket prezente |
| 5 | `GET /api/chart/BTC-USD?timeframe=5m&refresh=true` | 200; `cache:"miss"`, `as_of:2026-09-05T21:10` (acum) — provider sănătos |
| 6 | `GET /api/chart/BTC-USD?timeframe=5m` (re) | 200; `cache:"stale"`, `refreshing:true`, `as_of:21:10` — SWR funcțional (TTL 5m=20s) |
| 7 | `GET /api/chart/GC%3DF?timeframe=1d` | 200; 240 bare, `source:yahoo_chart`, `cache:"miss"`, `as_of` = acum |
| 8 | `pytest tests/test_chart_terminal_ui_contract.py tests/test_chart_api.py` | **17 passed în 0.42s** |

Dump-urile raw: `$TMP/mv9_chart_btc.json`, `mv9_chart_aapl.json`, `mv9_chart_gold.json`, `mv9_state.json`, `mv9_lp.json` (doar în temp, nu în repo).
