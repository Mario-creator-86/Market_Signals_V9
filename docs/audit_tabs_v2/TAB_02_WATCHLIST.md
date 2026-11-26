# Audit Forensic V2 — TAB 2: Watchlist & MTF Dashboard (`#view-watchlist`)
**Market Signals V9 | Data audit:** 2026-09-05 | **Metoda:** trasare UI → payload → endpoint → producător backend → sursă finală, cu verificare live (GET-only, 6 cereri către `http://127.0.0.1:8003`, dump-uri salvate doar în `$TMP`).

---

## 1. Verdict

**VERDICT: FUNCȚIONAL-CORECT (PASS cu rezerve) — 0 defecte P1 noi, 2 defecte P2, 6 P3.**

- Ambele fixuri din runda 1 verificate **țin în cod ȘI live**:
  - **(a) Chunking live-prices (8 simboluri/cerere)** — `frontend/app.js:1385` (`CHUNK = ... || 8`), introdus în commit `7f26fd2`. Live: cerere `remote=true` cu 8 simboluri = **2,93 s** (8/8 quote-uri `LIVE`); cerere cache-only pe toată lista (39) = **60 ms**. Față de 47–53 s înainte de fix (audit 2026-09-05, P1), latenta este sană.
  - **(b) Escapare XSS pentru termenul de căutare** — `esc(searchTerm)` la `frontend/app.js:18709` (mesaj „No ticker found for”) și `:18837` (card summary). Confirmat introdus în `7f26fd2` (`git log -S "esc(searchTerm)"`). ȚINE.
- Fluxurile add / remove / settings / hot-toggle sunt coerente în cod; cheia `alerts` se persistă corect (fix `c7edafc` prezent).
- Fără regresii pe căile watchlist între `7f26fd2..HEAD` (diff-ul frontend = doar funcția pre-earnings + bump versiuni cache `?v=38.26`).
- Defectele P2: **titluri de știri ne-escapate în acordeon** (XSS/robustețe) și **bug-ul CSS mobile din runda 1, NE-corectat** (Signal + Trend ascunse sub 600px).

---

## 2. Tabel câmp-cu-câmp (rand V2 compact + strip sumar + acordeon)

Legendă clasificare: OK / DEAD-RENDER / DEAD-BACKEND / FAKE / STALE / MISCALIBRATED.

| # | Câmp UI (fișier:linie) | Cheie payload | Endpoint | Producător backend (fișier:linie) | Sursa finală | Clasificare |
|---|---|---|---|---|---|---|
| 1 | Simbol ticker — `app.js:19193` | `tickers[].ticker` | `GET /api/state` | `api/state_service.py:3751` (display_ticker_map) | `runtime/watchlist.json` (ConfigManager) | OK |
| 2 | Badge HOT / buton „+” — `app.js:19195-19197` | `is_hot` | `GET /api/state` | `state_service.py:3770` (`ticker in hot_tickers`) | `watchlist.json → hot_list` | OK — live: MARA/BMNR/NVDA, identic cu `hot_list` |
| 3 | Badge NEW — `app.js:19194` | — (client) | — | `UI.isTickerNew` (localStorage) | client | OK (cosmetic, derivat local) |
| 4 | Preț — `app.js:19200` | `price` | `GET /api/live-prices` (`api/main.py:4360`) | merge `UI.applyLivePricesToRows` (`app.js:12495`) ← `UI.mergeLivePricesIntoState` (`app.js:12452`) | Lanț: `state.json` (`main.py:4521`) → SQLite `live_price_cache` (`:4534`) → `data/live_prices_cache.json` (`:4560`) → fan-out remote (`:4604-4689`: KuCoin/Binance/Alpaca crypto; Alpaca→Yahoo→eToro stocks) | OK — live: AAPL 320.01 (yahoo), BMNR 24.905 (`eToro_bid_ask_mid`) |
| 5 | Badge prospețime LIVE/CACHED/STALE — `app.js:19145-19157, 19201` | `freshness`, `age_seconds` | `GET /api/live-prices` | `main.py:4442-4452` (`_decorate_freshness`, limită 45 s) | timestamp quote | OK (onest) — 38/39 STALE la 68,5 s în modul cache; 8/8 LIVE după `remote=true` |
| 6 | Change % — `app.js:18953, 19203` | `change_pct` | `/api/state` + `/api/live-prices` | `main.py:4392` (`_nullable_change_pct`, F9: null când lipsește) + `state_service.py:3766` | exchange 24h ticker | MISCALIBRATED (P3) — frontend coalescează `null → 0` (`app.js:12506` `?? 0`, `:18953` `toNum(...,0)`) și afișează verde „+0,00%”, contrazicând intenția F9 |
| 7 | Badge semnal OPEN/PENDING/· + direcție — `app.js:18940-18942, 19204` | `signal_status`, `signal_direction`, `signal_score` | `GET /api/state` | `state_service.py:3689-3700, 3778-3781` (signal_map din open_signals) | motorul de semnale (state.json `signals`) | OK — live: 38 `none`, 1 `open` |
| 8 | Bară trend 1D/4H/1H — `app.js:19205-19207` | `trend_alignment` (+`chg`, `signal_score` pentru lățime) | `GET /api/state` | `state_service.py:3606-3617, 3713, 3784` (`_trend_alignment_label` din `trend_1d/4h/1h`) | indicatori OHLCV motor (yfinance) | OK eticheta; **MISCALIBRATED lățimea** — `trendStrengthV2` (`app.js:19168`) = `|chg|*10 + bonus score`, metrică client inventată, nu puterea reală de aliniere (P3, vizual) |
| 9 | Earnings „Xd”/„·” — `app.js:19171-19178, 19209` | `earnings_days`, `earnings_when` | `GET /api/state` | `state_service.py:3527-3560` (earnings_snapshot) → `:3708-3711, 3788` | `agents/economic_calendar_agent.py` (Finnhub/Yahoo calendar, cache) | OK — live: 12 rânduri `-1` → afișate onest „·”; NVDA are `-10` în hot_list dar `-1` în rând (inconsistență cosmetică P3) |
| 10 | Checkbox selecție rând / select-all — `app.js:19187-19190, 18894` | — (client) | — | `UI.watchlistSelectedTickers` | client | OK |
| 11 | buton coș (ștergere) — `app.js:19211-19213` | — | `DELETE /api/tickers` | `api/actions_service.py:464` | `runtime/watchlist.json` | OK (cod) + tombstone client `app.js:18610, 5973-5978` |
| 12 | Acordeon Top 3 știri — `app.js:19222` | `news_pulse.top_headlines` | `GET /api/state` | `state_service.py:3702, 3791` → `core/news_pulse.py` (`build_news_pulse_bulk`, `state_service.py:3585`) | feed agregat NewsTab (terți) | **P2: `h.title` injectat raw în innerHTML — fără `esc()`** |
| 13 | Acordeon Quick AI — `app.js:18945-18947, 19226-19230` | `quick_ai` | `POST /api/watchlist/quick-ai` (`main.py:3677`) | `core/watchlist_quick_ai.py` (Ollama); cache client `watchlist_quick_ai_cache_v1` (`app.js:4852`) | LLM local | OK — stare „AI analysis not yet run” onest când lipsă; `quick_ai` vine mereu `null` din state (greutate moartă în payload, P3) |
| 14 | Acordeon Strategie + note — `app.js:19234-19257` | `best_strategy`, `allowed_strategies`, `strategy_notes` | `GET /api/state` + `POST /api/watchlist/settings` (`main.py:3467`) | `state_service.py:3518-3525, 3690, 3764, 3771, 3773` | settings per-ticker din `watchlist.json` (ConfigManager) | OK |
| 15 | Strip sumar (Coverage / Open Setups / Event Risk / Momentum Breadth) — `app.js:18832-18854` | derivate client din `tickers[]` | `GET /api/state` | `state_service.py:3748-3804` | cf. rânduri | OK — contoare recalculate client; `searchTerm` ecou escapat (`:18837`) |
| 16 | Căutare (`#watchlist-search`, `index.html:285`) — filtrare `app.js:18669` | — (client pe `cachedState`) | — | — | client | OK — **XSS hardening confirmat** (`:18709`, `:18837`) |
| 17 | RSI / MACD (calculate la `app.js:19004-19027`) | `rsi`, `macd_hist` | `GET /api/state` | `state_service.py:3767-3768` | motor OHLCV | **DEAD-RENDER (P3, moștenit)** — `rsiDisplay`, `macdDisplay`, `momentumTooltip` (`:19026-19027, 19127`) nu sunt referențiate nicăieri |
| 18 | Day range / Volume / Sector pe rând | `day_range`/`volume` nu există în payload; `sector` (`state_service.py:3750`) | — | — | — | NE-RENDAT (by design): nu există coloane pentru acestea în V2; `sector` e folosit doar la Group-by (`app.js:18913`) și sortare (`:18757`) |
| 19 | Indicator de alerte pe rând | inexistent la nivel de rând | — | `state_service.py:3209/3247` expune doar `alerts_triggered` la nivel de state (ultimele 10) | motor alerte | ABSENT din Tab 2 (by design) — alertele se consumă ca toast (`app.js:20699, 24619`) și în Tab 12; nu există legătură per-rând |

### Numărarea rândurilor STALE (live)

- `GET /api/live-prices?remote=false` (39 simboluri): **38 STALE / 1 LIVE** — vechime uniformă ~68,5 s > limita de 45 s; ultima scriere în `data/live_prices_cache.json` fusese cu ~68 s înainte. BMNR = LIVE (`eToro_bid_ask_mid`, refresh separat de tracker-ul Capital).
- `GET /api/live-prices?remote=true&symbols=<8>`: **8/8 LIVE** (age 2,8 s).
- Explicație: UI reîmprospătează remote la maxim 30 s (`app.js:12523` `minInterval=30000`) cu `maxAgeSeconds:45`; badge-ul basculează onest LIVE(≤45 s)→STALE între refresh-uri. **Nuanță de weekend (azi sâmbătă)**: quote-urile Yahoo sunt close-ul de vineri; „LIVE” înseamnă „proaspăt preluat”, nu „piață deschisă” — etichetă semantică potential misleading (P3).

---

## 3. Defecte

### P1
- **(moștenit din runda 1, NE-corectat) CSS mobile <600px ascunde Signal + Trend** — `frontend/styles_watchlist.css:997-1002`: `.wl-row-v2 > :nth-child(5)/(6) { display:none }` vizează copilul 5 = badge Signal (`app.js:19204`) și 6 = Trend (`:19205`), nu Trend+Earnings cum intenționează comentariul. În cardul mobil dispare exact semnalul cantitativ. Fix propus în runda 1 rămâne valabil (țintiți `.wl-col-trend` / `.wl-col-earnings` pe clasă).

### P2
1. **Titluri de știri ne-escapate în acordeon (vector XSS / stricciune de layout)** — `frontend/app.js:19222`: `(h.title || 'unknown')` este interpolat direct în `innerHTML`, deși `esc()` există în scope (`:18587`). Conținutul vine din feed-uri de știri terțe (`core/news_pulse.py` → `state_service.py:3585`); un titlu cu `<img onerror=...>` sau ghilimele se injectează netratat. Fix: `esc(h.title)` + `esc(h.time)`.
2. **Regresie cosmetică pe change % null vs. F9** *(gradat P3-secondary în tabel, menționat aici pentru trasare)* — backend-ul (`api/main.py:4392-4404`) returnează deliberat `change_pct: null` când datele lipsesc, cu comentariul „The frontend already treats null as 'no change data'”, dar frontend-ul coalescează null→snapshot→0 și pictează verde „+0.00%” (`app.js:12506`, `:18953`, `:19203`). Live: BMNR vine cu `change_pct: null` din `eToro_bid_ask_mid`; valoarea afișată provine din snapshot-ul state (risc de +0,00% fals doar când și snapshot-ul e gol). Impact redus în practică, dar contractul F9 nu e respectat de randul V2.

### P3
1. **DEAD-RENDER RSI/MACD** — `app.js:19004-19027, 19127`: calcul per rând per refresh, zero utilizare în DOM (moștenit din runda 1, nefixat).
2. **Lățimea barei de trend e o metrică client fabricată** — `app.js:19168` (`|chg|*10 + 20 dacă score>50`, cap 100), nu corespunde niciunui câmp backend; tooltip-ul (`trendReason`) e onest, bara nu.
3. **`quick_ai: null` trimis pentru toate cele 39 rânduri** (`state_service.py:3797`; verificat live: 0 non-null) — greutate moartă în payload-ul `/api/state` (~3,2 MB).
4. **Inconsistență earnings NVDA** — rând: `-1` („·”), hot_list details: `-10` (2026-08-26, trecut) — două căi de rezoluție diferite pentru același ticker.
5. **Eticheta LIVE pe piață închisă** — sâmbătă, close-ul de vineri preluat proaspăt e etichetat „LIVE” (`main.py:4444-4449`); onest la nivel de cache, ambiguu la nivel de piață.
6. **`d.ticker` ne-escapat în contexte text/onclick** — `app.js:19193` (`<span class="wl-symbol-v2">${d.ticker}</span>`), `:19196-19197`, `:19260-19261`. Risc redus: randurile sunt filtrate de `allowedSymbols` (`:18608-18612`, derivat din `watchlist.json`), iar adăugarea trece prin `normalize_market_ticker` (`main.py:3595-3598`), dar ≠ apărare în profunzime față de `esc()` folosit deja pe atribute (`:19184`, `:19190`, `:19211-19213`).

---

## 4. Starea fixurilor & regresii față de runda 1

| Element runda 1 | Stare acum | Dovezi |
|---|---|---|
| P1: latență live-prices pe watchlist mare (47–53 s) | **REZOLVAT, ȚINE** | `app.js:1381-1407` (chunk 8, merge, timeout 25 s); live 2,93 s/8 simboluri; cap backend `MS_LIVE_PRICES_MAX_SYMBOLS=50` (`main.py:4454-4462`) |
| P2: dead code RSI/MACD | **NEFIXAT** | `app.js:19004-19027, 19127` |
| P1: CSS mobile ascunde Signal/Trend | **NEFIXAT** | `styles_watchlist.css:997-1002` |
| P3: add-ticker nu re-randa Tab 2 | **REZOLVAT** | `app.js:5918-5920` (`watchlist:add-refresh` → `renderDetailedWatchlist`) + placeholder „Initializing” (`:5887`) |
| XSS search term (fix din 7f26fd2) | **ȚINE** | `app.js:18709, 18837`; `git log -S` → 7f26fd2 |
| Cheia `alerts` (fix c7edafc) | **COERENTĂ** | `core/config.py:1653-1661` (load) și `:1944-1967` (save) fac round-trip; live: `state.watchlist.alerts=[]`, `alerts_triggered` expus ultimele 10 (`state_service.py:3209/3247`), consumator toast activ (`app.js:24619`) |

**Regresii `7f26fd2..HEAD` pe căile watchlist:** niciuna. Diff-ul complet pe `frontend/app.js` = doar `UI._preEarningsDecision` (eToro retail positioning, commit `e9eda27`); `frontend/index.html` = bump `?v=38.26` / `styles_preearnings.css?v=1.18`. Chunking-ul a fost introdus în `7f26fd2` și neatins de atunci.

---

## 5. Recomandări

1. **P1 (CSS mobile)** — înlocuiți `:nth-child(5)/(6)` cu clase explicite `.wl-col-trend`/`.wl-col-earnings` în media query-ul de 600 px (fix propus deja în runda 1).
2. **P2 (XSS știri)** — `esc()` pentru `h.title`/`h.time` la `app.js:19222`; din principiu, orice interpolare de conținut terț în `innerHTML`.
3. **P2 (contract F9)** — în randul V2, tratați `change_pct === null` explicit (afișați „·” sau „n/a” în loc de „+0,00%” verde).
4. **P3** — ștergeți blocul RSI/MACD mort sau injecțați-l în acordeon; alimentați `trendStrengthV2` dintr-un câmp backend real (ex. număr de TF-uri aliniate); omiteți `quick_ai: null` din payload; unificați rezoluția earnings NVDA; documentați semantic badge-ul LIVE pe weekend; escapați `d.ticker` în contexte text pentru apărare în profunzime.

---
*Audit compilat fără modificări de cod; 6 cereri GET live; niciun dump salvat în repo.*
