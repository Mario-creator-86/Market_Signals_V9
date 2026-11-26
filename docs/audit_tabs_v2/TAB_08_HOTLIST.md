# AUDIT CRIMINALISTIC Runda 2 — TAB 8: PREMIUM HOT LIST & DEEP ANALYSIS (`#view-hotlist`)
**Market Signals V9 | Audit UI↔Backend câmp cu câmp | Data: 2026-09-05 | Server live: http://127.0.0.1:8003**
*Metodă: re-verificarea raportului Runda 1 (`docs/audit_tabs/TAB_08_HOTLIST.md`) + trasarea completă Frontend → payload → endpoint → producător backend → sursă finală. Verificare live exclusiv cu GET (7 cereri, ≥1s distanță). Cod: `frontend/app.js`, `frontend/hotlist_dossier.js`, `frontend/index.html`, `api/main.py`, `api/hotlist_service.py`, `api/state_service.py`, `core/hotlist_scoring.py`, `core/hotlist_news.py`, `core/ai/debate.py`, `core/ai/fallback.py`, `core/ai/decision_engine.py`.*

---

## 1. VERDICT GENERAL

| Criteriu | Verdict |
| :--- | :--- |
| Sănătate generală Tab 8 | **FUNCȚIONAL CU 3 DEFECTE P2 + 1 P1 NOU** — toate scorurile principale se calculează real din inputuri live (formulele re-verificate numeric pe payload live), dar panoul "Market Data (FMP)" din modalul de detaliu redă date FAKE (zero-uri în roșu) după "Load Details", lanțul de fallback AI este corect în cod dar **gol în config** (debaterea moare încă la 429 Ollama), iar momentum-ul este otrăvit 6h de un cache negativ. |
| Scoruri (heat/edge/overall/priority/risk/confidence/bull-bear) | **OK / CALCULATE REAL** — `overall` MARA 59.14 reprodus exact cu formula `0.35*edge + 0.20*momentum + 0.20*news + 0.15*technical + 0.10*regime`; `priority` MARA 32.4 și NVDA 0.4 reproduse exact; `news_score` MARA 57.2 reprodus din `counts` live. Nu am găsit装饰ări hardcodate active pe cardul principal. |
| Fix-uri Runda 1 (a) Load-Details-wipe, (b) retry deep-analysis, (c) timeout-uri 90s/45s | **TOATE VERIFICATE, FĂRĂ REGRESIE** — detalii în §4. |
| P3-uri Runda 1 | `hot_context null` MARA/BMNR — **ÎNCA DESCHIS**; `bull/bear_score negated` — **ÎnCHIS (inexistent în cod)**. |
| Regresii vs. comiturile c7edafc / 8bf97a2 / 2acbdd9 | **NU S-A DETECTAT NICIO REGRESIE** în căile auditate. |

**Numărătoare defecte: 1 × P1, 3 × P2, 8 × P3.**

---

## 2. TABELUL DE TRASABILITATE CÂMP-CU-CÂMP

Legendă verdict: OK = trasabilitate completă și valoare reală; STALE = valoare reală dar înghețată/întârziată; DEAD-RENDER = element UI fără producător; DEAD-BACKEND = producător inexistent/inert; FAKE = valoare inventată/placeholder afișată ca reală; MISCALIBRATED = calcul real dar formulă sau input deformatat.

### 2.1. Header & structură (`frontend/index.html:306-334`)

| Câmp | Frontend file:line | Endpoint | Backend file:line | Sursă | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Titlu "Premium Hot List" | index.html:312-318 | — | — | static | OK |
| Buton "Load Details" | index.html:319-321 → app.js:5813 (`Actions.loadHotListDetails`) | GET `/api/state?hotlist_details=true` | state_service.py:3133 → `_get_hot_list_details_cached(allow_build=True)` :2746 | state.json + live fetch + cache disc `runtime/hotlist_cache.json` | OK (dar vezi **P1-1** pentru conținutul FMP) |
| `#hotlist-stats` ("Filled by JS") | index.html:326-328 | — | — | — | **DEAD-RENDER (P3-2)** — zero referințe JS în `frontend/*.js` |
| `#hotlist-freshness-badge` | index.html:329 | — | — | — | **DEAD-RENDER (P3-2)** — zero referințe JS |
| `#hotlist-empty-state` | index.html:334 | — | — | — | DEAD-RENDER (minor; gridul e rescris integral de `renderHotList`) |

### 2.2. Market Heat Strip (`#hotlist-heat-strip`, index.html:330 → `frontend/hotlist_dossier.js:40-96`)

| Câmp | Frontend file:line | Endpoint | Backend file:line | Sursă | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Fetch + cache client 60s + gardă `heatInflight` | hotlist_dossier.js:36-65 (timeout **45s** :52) | GET `/api/hotlist/heat` | api/main.py:8740-8749 → `hotlist_service.build_heat_list` :86-114 (SWR TTL 15s :175-176, thread `heat-swr` :102-112) | cache memorie + `_build_heat_list_uncached` :117-172 | OK — **SWR verificat live**: primul GET a servit payload stale (as_of 17:55 UTC) în 0.18s, al doilea a primit as_of proaspăt 20:11 UTC |
| Chip: ticker | hotlist_dossier.js:78-84 | `/api/hotlist/heat` → `heat[].ticker` | hotlist_service.py:131-149 (`_touch`) ← `api/main._build_hotlist_response` :8589-8637 ← `agents/scanner_agent.py:652-750` (Yahoo screener `day_gainers/losers/most_actives` :663-671) + Binance 24h (:280-337, :611-649) | Yahoo Screener / Binance live | OK (54 rows live; NVDA prezent) |
| Chip: change % colorat | hotlist_dossier.js:73-81 | `heat[].change_pct` | `_normalize_scanner_item` api/main.py:8510-8521 | Yahoo/Binance change la scanare | OK |
| Chip: categorii (gainers/losers/trending/volume_leaders) | hotlist_dossier.js:76-77 | `heat[].categories` | hotlist_service.py:139-140 ← maparea sursă→categorie api/main.py:8490-8496 | tag-ul `source` al scanerului | OK |
| "N in your Hot List" | hotlist_dossier.js:86-88 | `heat.curated` (lungime) | hotlist_service.py:119-124 → `hotlist_scoring.get_hotlist_state` :705-738 → `state_service.get_state` → `build_hot_list_fast` :2554-2670 | config.hot_list (watchlist curat) | OK — live: 3 (MARA, BMNR, NVDA) |
| Badge "scanner warming up…" | hotlist_dossier.js:93 | `scanner_available === false` | hotlist_service.py:154 | flag din `_scanner_hot_data` :65-83 | OK |
| Click chip → dossier | hotlist_dossier.js:78 | — (UI) | — | — | OK |
| **Notă dormantă**: maparea `("mktcap","name")` din `_touch` | — | — | hotlist_service.py:141-144 | — | P3-8 (latent: dacă un rând scanner ar avea `mktcap`, ar suprascrie `name` cu o capitalizare; în prezent scanerul nu emite `mktcap`) |

### 2.3. Carduri Hot List (`#hotlist-grid`, `frontend/app.js:21793-22165`, `renderCard` 21854-22149)

| Câmp | Frontend file:line | Payload key (`/api/hotlist/state` → `hotlist[]`) | Backend file:line | Sursă | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Simbol | app.js:22062 | `ticker` | state_service.py:2643-2644 | config.hot_list | OK |
| Preț `$X.XX` | app.js:22064 | `price` | state_service.py:2596-2619: watchlist row → state.json engine → **fallback `data/live_prices_cache.json`** :2613-2619 (fix 2acbdd9) | Yahoo/eToro → cache prețuri persistat | OK — live MARA 11.255 (state.json pe disc e vechi 09-05, dar starea în-memorie a serverului e proaspătă) |
| Change % colorat | app.js:22065 | `change_pct` | state_service.py:2597-2619 | idem | OK — live MARA -2.97% |
| Badge "Best: mode/risk" | app.js:21865-21866 | `best_strategy` | state_service.py:2586-2587 | open_signals (semnale deschise) | OK (gol live → ascuns) |
| **OVERALL 0-100** | app.js:22069-22073 (tooltip cu formula :22069) | `overall_score` | `compute_hotlist_composite_metrics` hotlist_scoring.py:221-282, formula :236-242 | pondere din edge/momentum/news/technical/regime — toate calculate din inputuri live | **OK** — verificare numerică live: MARA 0.35×54+0.20×50+0.20×57.2+0.15×84+0.10×62 = **59.14** = valoarea din payload |
| **Priority score** | app.js:22072 | `priority_score` | hotlist_scoring.py:244-247, greutăți `_earnings_time_weight` :186-209 + factor BMO/AMC :212-218 | earnings_days + edge − penalty risc | **OK** — MARA: 12×0.9+0.4×54−0 = **32.4** ✓; NVDA: 8×1.0+0.4×31−20 = **0.4** ✓ |
| Countdown earnings ("in Xd" / "Last Xd ago" / TBA) | app.js:22019-22048 | `earnings_date`, `earnings_days`, `earnings_when` | state_service.py:2630-2641 → `_resolve_earnings_from_sources(allow_remote=False)` | calendar economic cache + watchlist + `last_known_earnings.json` | OK — eticheta "Last 10d ago" pentru NVDA (data în trecut) e cinstită |
| **Confidence bar %** | app.js:22078-22086 | `confidence_score` | hotlist_scoring.py:277: `int(round(overall))` | **aceeași formulă ca Overall** | OK calculat, dar **P3-4**: e literalmente Overall re-afihat ca "Confidence" — nu aduce informație nouă |
| **Badge EDGE A/B/C + scor** | app.js:22089 (tooltip :21999) | `earnings_edge_score`, `earnings_setup` | `compute_earnings_edge_score` hotlist_scoring.py:453-675 (determinist, fără rețea: earnings_days, trend bias, momentum, ADX, insider, rating, gap stats, ATR pct, regim vol, news) | inputuri live din card | **OK** — MARA 54/C, BMNR 50/C, NVDA 31/C; praguri A≥75/B≥60 :617 |
| **Badge PLAY (PLAY_PRE/POST/WAIT/AVOID/N-A)** | app.js:22090 | `earnings_play` | hotlist_scoring.py:626-643 | scor + zile până la earnings + risc | **OK** — NVDA AVOID (edge 31, risk high) |
| **Badge RISK LOW/MED/HIGH** | app.js:22091 | `risk_level` | hotlist_scoring.py:619-624 (risk_points + negative_risk + risk_events) | flags compute | **OK** |
| Badge "Partial Data" | app.js:21902-21910 | `_data_quality` | state_service.py:2305-2311 (`has_price/has_trends/has_fundamental_details/source`) | auto-diagnostic backend | OK — toate 3 cardurile au `source: 'fast'` → badge prezent (cinstit) |
| Trend 1D/4H | app.js:22096-22100 | `trend_1d`, `trend_4h` | state_service.py:2598-2599 | watchlist rows (bare 1D/4H) | OK — live BULL/BULL etc. |
| **ADX** | app.js:22101-22107 (extracție robustă :21922-21933) | `adx` | `_hotlist_adx` state_service.py:2125-2132 → `_compute_adx14` (Wilder) :2050-2088 | bare OHLC zilnice cache (încălzite de heat build :160-170) | **OK** — live MARA 18.0, BMNR 52.6; NVDA N/A (bare < 28) |
| **Momentum 1W / 1M** | app.js:22105, parse :21942-21955 | `momentum_1w`, `momentum_1m` | `_hotlist_momentum` state_service.py:2097-2122 (close-to-close din bare; TTL cache **6h**) | bare zilnice | **STALE / P2-3** — toate 3 cardurile live au `momentum_1w/1m = None` deși barele EXISTĂ în cache (dovadă: ADX MARA/BMNR calculat din aceleași bare); rezultatul negativ (None) e cache-uit 6h (:2120-2121) de la un cold-start anterior barelor → `momentum_score` cade pe 50 (neutru) și `top_risk` devine `momentum_unknown` |
| Insider sentiment | app.js:22110 | `insider.sentiment` | state_service.py:2664 (`data.get("insider", {})`) | **FMP pensionat** — fast path dă mereu `{}` | **DEAD-BACKEND dar onest (P3-5)** — afișează `unknown`, nu inventează |
| News sentiment | app.js:22111 | `news_signals.sentiment_trend` | `summarize_ticker_news_signals` core/hotlist_news.py:238+ | bucketul agentului de știri (eToro + yahoo RSS) | OK — live MARA `bullish` |
| Expected vs 8Q Move | app.js:22113-22117 | `expected_move_vs_avg.{expected_move_pct, avg_move_8q, edge_pct}` | `_build_hotlist_earnings_context` state_service.py:2167-2203 (pre_earnings.json → estimare din bare ATR; edge = diferența) | pre-earnings map + bare | OK — live MARA 7.929/N-A/N-A (avg lipsă → N/A cinstit) |
| Earnings History (8Q) U/D + avg gap | app.js:22118-22122 | `earnings_history_summary.last_8` + `sample_size` | state_service.py:2150-2159 (`_compute_historical_earnings_gap_stats`) | bare + istoric earnings | OK — live nesample-uit → afișează `—` |
| **Risk Heat bar %** | app.js:22125-22131, **formula în frontend** :21882-21884: `20 + 14*flagCount + (high?30:low?0:12)`, clamp 20-95 | `earnings_risk_flags`, `risk_level` | flags reale din `compute_earnings_edge_score` | flags + nivel risc | OK (calculat din inputuri reale, declarat în tooltip ca "visual composite, not a probability") — **P3-3**: calculul e în frontend, nu backend (fix-ul 60cb8c8 respectat ca substanță) |
| Top Risk | app.js:22133-22136 | `top_risk` | hotlist_scoring.py:661-666 (primul flag structurat; fallback risk_events) | flags | MISCALIBRATED ușor (**P3-3b**) — toate 3 cardurile afișează `momentum_unknown` ca "Top Risk", deși pentru NVDA flags mai materiale există (`volatility_compressed`, `risk_events_present`); comentariul din cod :655-660 declară intenția "most material" dar implementarea ia flags[0] |
| Buton Analyze | app.js:22139-22141 → `Actions.analyzeTicker` :22790-22830 | POST `/api/analyze-ticker` (timeout 420s) | api/main.py:3610 → `actions_service.analyze_ticker_mode` :1176 → `core/ticker_analyzer.py:1091` → fallback determinist `core/ai_analysis_schema.py:169/270` | Engine V1/V2 + LLM | **Auditat doar pe cod (POST interzis live)** — lanțul de fallback determinist există; consistent cu Runda 1 |
| Buton Remove (coș) | app.js:22053-22055 → `Actions.toggleHotList` :22647 | POST `/api/watchlist/hotlist/toggle` | api/main.py:3589-3601 | config hot_list | Auditat pe cod (POST interzis live) |
| Buton Dossier (folder) | app.js:22056-22058 → `UI.openTickerDossier` | GET dossier | vezi §2.4 | — | OK |

### 2.4. Dossier Modal (`#hl-dossier-overlay`, `frontend/hotlist_dossier.js:100-304`)

Endpoint: `GET /api/hotlist/ticker/{ticker}/dossier` (timeout client **90s**, :125) → api/main.py:8752-8761 → `hotlist_service.build_ticker_dossier` (:461-492, cache 60s :41-42,66-69). Probe live: **MARA, BMNR, NVDA** (HTTP 200, 2.3-4.7s).

| Câmp | Frontend file:line | Payload key | Backend file:line | Sursă | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Head: ticker + preț + change | hotlist_dossier.js:159-171 | `ticker`, `quote.price`, `quote.change_pct` | `_quote_section` hotlist_service.py:181-223 (3 niveluri: watchlist state → state.json → `live_prices_cache.json`) | cache local, fără rețea pe calea de citire | OK |
| Badge "Hot List" / "Traded by us" | hotlist_dossier.js:167-168 | `in_curated_hotlist`, `trades.traded` | :478, :321 | heat curated + SQLite | OK |
| **"Why it is hot"**: categorii | hotlist_dossier.js:178-188 | `hot_context.categories` | hotlist_service.py:471-472 (`build_heat_list().heat`) | scanner heat | **P3-1 (Runda 1, ÎNCĂ DESCHIS)** — live: `hot_context = null` pentru MARA și BMNR (nu apar în niciun screener acum), NVDA îl are (`most_active`, `volume_leaders`); pentru MARA/BMNR secțiunea arată doar regim/trend, deși `in_curated_hotlist: true` există în payload dar NU e folosit în această secțiune |
| Contradicția losers-vs-BULL | hotlist_dossier.js:182-185 | `hot_context.categories` + `quote.change_pct` | — | — | OK (activ doar când `losers` ∈ categorii — NVDA/MARA/BMNR nu sunt în losers acum, deci notă nu apare; logica e corectă) |
| Regim + trend 1D/4H în why-hot | hotlist_dossier.js:187 | `quote.market_regime/trend_1d/trend_4h` | :191-195 | watchlist state | OK (live: range, BULL/BULL) |
| **Deep analysis**: verdict badge (APPROVE/REDUCE/WATCH/AVOID) | hotlist_dossier.js:192-196, 203 | `deep_analysis.verdict.recommendation` | `get_deep_analysis` :497-508 (store `data/hotlist_deep_analysis.json`) ← `run_entry_debate` → `_norm_verdict` debate.py:138-159 | JSON arbitrat de manager LLM | OK — live BMNR: `avoid`, conf 0.7, bull 5 / bear 8 (coerent: bear mai mare → avoid) |
| Confidence % | hotlist_dossier.js:200-204 | `verdict.confidence` | debate.py:145-147 (clamp 0-1) | LLM manager | OK |
| **bull_score / bear_score /10** | hotlist_dossier.js:205 (afișare directă, fără negare) | `verdict.bull_score/bear_score` | debate.py:149-152 (clamp 0-10) | LLM manager | **OK — P3 Runda 1 "negated" ÎnCHIS**: nu există nicio negare pe calea `debate.py → recorder.py:197-198 → hotlist_dossier.js:205` |
| Meta: models, generated_at, vârstă, fresh, frame | hotlist_dossier.js:201-206 | `models`, `generated_at`, `age_seconds`, `fresh`, `direction_frame` | hotlist_service.py:500-507 (age/fresh); `direction_frame` :589 | store disc | OK — live BMNR "61.5h ago" calculat corect (`age/360/10` = ore) |
| Rationale + argumente Bull/Bear | hotlist_dossier.js:207-211 | `verdict.rationale`, `bull`, `bear` | debate.py:216-226, store :587-598 | LLM | OK — live BMNR texte reale coerente cu evidence |
| Starea "No deep analysis yet" + retry | hotlist_dossier.js:212-218 | `deep_analysis.available === false` | `run_deep_analysis` retry-gate :521: cere `cached.get("fresh") AND cached.get("available")` | — | **OK — Fix Runda 1 (b) VERIFICAT**: rândul eșuat MARA (available=false, error=`bull_argument_unavailable`, generat 2026-09-04T22:37) nu blochează retry; `MARA__history` e gol (rândurile eșuate nu intră în istoric :602-608) |
| Buton "Run deep analysis" | hotlist_dossier.js:217-219, 308-324 | **POST** `/deep-analysis` (timeout 120s :316) | api/main.py:8764-8774 → `run_deep_analysis` :511-618 → `run_entry_debate` | LLM | Auditat pe cod (POST interzis live) — vezi **P2-2** |
| **Lanț fallback dezbatere (fix c7edafc)** | — | — | debate.py:197-198 (captură `bull_fallback`/`mgr_fallback`, nu `_`), :208-233 (`generate_with_fallback`); config: api/main.py:8771 `get_shared_config()` → `ConfigManager.config` (core/config.py:1311-1313) → debate.py:189 `getattr(config,"config",config)` ✓ | config.json `ai.providers` + `ai.fallback.chain` | **P2-2**: calea de cod e CORECTĂ și configul este pasat corect, DAR `config.json` are `ai.fallback = {"enabled": true, "chain": []}` → lanțul este **GOL** → la 429 Ollama `generate_with_fallback` nu are unde să cadă → `UNAVAILABLE` → `bull_argument_unavailable`. Dovezi: MARA deep row eșuat doar cu `models: ollama`; `config.json` citit direct. Fix-ul funcționează structural, dar e inert în producția actuală |
| News: titlu, sursă, vârstă, sentiment, count | hotlist_dossier.js:222-230 | `news.articles[]`, `news.count` | `_news_section` hotlist_service.py:226-249 (max 6 articole, count = total bucket) | agent știri (eToro + yahoo RSS) | OK — live MARA count=10, 6 afișate; **P3-6**: sursa include postări sociale eToro ("eToro (@user)") amestecate cu știri propriu-zise |
| Our trades: totals, chips strategii, poziții deschise, istoric | hotlist_dossier.js:233-259 | `trades.totals/strategies/open_positions/closed_trades` | `_trades_section` hotlist_service.py:252-335 | SQLite `data/market_signals.db` `kv_store.capital_state` (timeout 2s) | OK — live BMNR: traded=true, 1 poziție OPEN afișată; MARA/NVDA "We never traded this ticker" |
| AI decision history + "why?" | hotlist_dossier.js:263-280 | `decisions.decisions[].reason` | `_decisions_section` hotlist_service.py:338-378 (ledger, skip `no_decision_recorded`) | Decision Ledger SQLite | OK — live BMNR: 2 decizii cu motive reale (`Fade-the-pump SHORT...`, `ai_review_not_consulted_risk_rejected`) |
| Earnings context | hotlist_dossier.js:285-294 | `pre_earnings.*` | `_pre_earnings_section` hotlist_service.py:412-458 (3 niveluri: pre_earnings.json → watchlist_state → last_known_earnings.json) + persistare `_remember_earnings` :393-409 | triplu fallback | OK cu **P3-7**: NVDA vine din `pre_earnings_universe` cu `earnings_time_utc: null`, `gap_risk: null`, `gap_risk_level: "NONE"`, `coverage_score: 30` → UI randează dată goală + "gap risk — (NONE) · bias NEUTRAL" (informativ slab, dar nu minte) |
| Snapshot as_of | hotlist_dossier.js:303 | `as_of` | :477 | ceas server | OK |
| Dossier heat context preț din heat | — | `hot_context.price/volume` | :135-144 | scanner | OK |

### 2.5. Card Drilldown Modal (`#hotlist-detail-modal`, app.js:22202-22303) & System Analysis Modal (`#analysis-modal`, app.js:22858+)

| Câmp | Frontend file:line | Sursă | Verdict |
| :--- | :--- | :--- | :--- |
| Preț/change în header | app.js:22238-22241 | `cachedState.hot_list` (aceleași date ca cardul) | OK |
| Multi-timeframe 1D/4H/1H/30M/5M | app.js:22253-22267 | `item.trend_*` — 1H/30M/5M populati doar după "Load Details" (live fetch state_service.py:2940-2955) sau watchlist `col_*` | OK |
| **"Market Data (FMP)"** | app.js:22271-22276 → `renderHotListFmpSection` :22314-22645 | `item.insider/ratings/financialScores/target/congressTrades/ownerEarnings/enterpriseValues/...` | **FAKE — P1-1**: producătorul `build_hot_list` setează `fmp_data = {}` **hardcodat** (state_service.py:2973) și marchează `_details_loaded: True` (:3015). Cache-ul detaliilor (`runtime/hotlist_cache.json`) **nu există** pe sistemul live → după "Load Details" panoul afișează garantat: **Piotroski "0/9" în roșu** (:22352-22353, :22534), tooltip "Altman Z-Score: N/A / Status: **RISK (Bankruptcy risk)**" (:22447-22456), Rating "N/A" cu stea roșie (:22364-22366, :22543), Target N/A, Congress N/A, Sector N/A, Owner Earnings/EPS/EV/EV-EBITDA/growth toate N/A (:22410-22420, :22584-22633). Butonul devine "Details loaded" și dezactivat (:21811-21815). Singurul câștig real al Load Details: completarea trendurilor 1H/30M/5M lipsă prin live fetch (:2940-2955) |
| Strategy Notes | app.js:22283-22285 | `item.strategy_notes` | OK |
| "Start AI Analysis" | app.js:22289 | POST analyze-ticker | Auditat pe cod |

---

## 3. DEFECTE (P1/P2/P3) CU DOVEZI LIVE

### [P1-1] Panoul "Market Data (FMP)" din drilldown afișează placeholder-e ca date încărcate (FAKE)
- **Unde**: `api/state_service.py:2973` (`fmp_data = {}` hardcodat în `build_hot_list`), `:3015` (`_details_loaded: True` necondiționat); `frontend/app.js:22314-22645` (randare FMP), `:21811-21815` (butonul "Details loaded").
- **Dovadă live**: `runtime/hotlist_cache.json` NU există → orice apăsare "Load Details" construiește detalii cu FMP gol; toate cele 3 carduri live au `_details_loaded: false`, `has_fundamental_details: false`.
- **Impact**: după click, utilizatorul vede "Piotroski 0/9" colorat roșu și tooltip-ul "RISK (Bankruptcy risk)" pentru ORICE ticker — o afirmație gravă derivată dintr-o valoare inexistentă — plus rating/growth toate N/A, fără niciun marker că datele lipsesc (badge-ul "Partial Data" rămâne doar pe card, nu în modal). FMP a fost pensionat (comentariu state_service.py:2050), dar UI-ul încă pretinde că îl încarcă.
- **Reparație recomandată**: (i) elimină panoul FMP sau afișează-l doar dacă există date reale; (ii) nu seta `_details_loaded: True` când `fmp_data` e gol — redenumste semantic în `_live_trends_loaded`; (iii) butonul "Load Details" să comunice real ce face (completează trendurile intraday).

### [P2-2] Lanțul de fallback al dezbaterii e corect în cod, dar GOL în config — simptomul P1 din Runda 1 persistă
- **Unde**: fix verificat în `core/ai/debate.py:193-231` (captura `fallback_chain` + `generate_with_fallback` din `core/ai/fallback.py`); config pasat corect: `api/main.py:8771` → `get_shared_config()` (state_service.py:115-122) → `ConfigManager.config` (core/config.py:1311-1313) → `debate.py:189`.
- **Dovadă**: `config.json` conține `ai.fallback = {"enabled": true, "chain": []}` — niciun provider de rezervă; `data/hotlist_deep_analysis.json` MARA: `available=false, error=bull_argument_unavailable, models={debater: ollama/glm-5.3-flash:cloud, manager: ollama/...}` (doar Ollama încercat, 2026-04T22:37Z).
- **Impact**: la epuizarea cotei Ollama Cloud dezbaterea tot eșuează, exact scenariul documentat în Runda 1; fix-ul c7edafc nu poate declanșa nimic pe un lanț vid.
- **Reparație**: populează `ai.fallback.chain` (ex. `["zai","openai","gemini"]` — providerii sunt deja definiți și enabled în config, cu `api_key_env`) sau construiește un lanț implicit din providerii cu chei prezente.

### [P2-3] Momentum otrăvit 6h de cache-ul negativ → linia ADX/Momentum + Top Risk degradate pe toate cardurile
- **Unde**: `api/state_service.py:2097-2122` — `_hotlist_momentum` cache-uiește și rezultatul `{"momentum_1w": None, "momentum_1m": None}` (TTL 6h, :2094, :2120-2121); barele negative sunt cache-uite doar 10 min (:1801).
- **Dovadă live**: `/api/hotlist/state` → MARA `adx=18.0`, BMNR `adx=52.6` (bare ≥28 PREZENTE în cache), dar `momentum_1w/1m = None` pe toate 3 cardurile; `top_risk = "momentum_unknown"` pe toate 3; `momentum_score = 50.0` (neutru) intră cu pondere 20% în Overall.
- **Impact**: după un cold-start (starea construită înainte de încălzirea barelor), momentum-ul rămâne N/A și scorul Overall este calculat cu valoarea neutră până la 6h — deși datele pentru calcul există deja.
- **Reparație**: nu cache-ui rezultatul negativ (sau TTL ≤ 10 min, sincron cu barele negative) și/sau invalidează `_MOMENTUM_CACHE` când `_SYMBOL_BARS_CACHE` primește bare.

### [P3-1] `hot_context` null pentru MARA/BMNR în dossier (Runda 1, încă deschis)
- Live: `hot_context: null` (MARA, BMNR), prezent pentru NVDA. Cauza: `build_ticker_dossier` (hotlist_service.py:472) caută doar în scaner heat; secțiunea "Why it is hot" (hotlist_dossier.js:178-188) nu folosește `in_curated_hotlist` (redat doar ca badge în header :167). Degradare onestă, dar secțiunea pierde motivul real ("in your curated list").

### [P3-2] Markup mort în headerul tab-ului
- `#hotlist-stats` (index.html:326-328) și `#hotlist-freshness-badge` (index.html:329): zero referințe în `frontend/*.js`. În plus, `last_update` din `/api/hotlist/state` e proaspăt (live: același minut) dar nu e afișat nicăieri. `#hotlist-empty-state` (index.html:334) de asemenea nefolosit.

### [P3-3] Risk Heat calculat în frontend + Top Risk ia primul flag, nu cel mai material
- Formula `20 + 14*flags + levelPts` (app.js:21882-21884) e derivată din flags reale, dar trăiește în frontend (comentariul backend hotlist_scoring.py:655-660 declare intenția "most material", implementarea ia `flags[0]`). Live: toate 3 cardurile afișează "Top Risk: momentum unknown" — pentru NVDA există flags mai relevante (`volatility_compressed`, `risk_events_present`).

### [P3-4] Confidence = Overall re-afihat
- `confidence_score = int(round(overall))` (hotlist_scoring.py:277) — bara "Confidence" (app.js:22078-22086) nu aduce nicio informație diferită de scorul Overall din colțul cardului.

### [P3-5] Linia "Insider" de pe card nu are producător activ
- `insider` vine doar din FMP pensionat (state_service.py:2664, `fmp_data={}`) → afișează permanent `unknown` (onest, dar un rând mort pe card; scoring-ul `compute_earnings_edge_score` pierde și el semnalul insider :522-528).

### [P3-6] Bucketul "News" din dossier amestecă postări sociale eToro cu știri
- Live MARA: surse `eToro (@fogi70)`, `eToro (@KrejzekEmil)`, `yahoo_rss`. Postările de utilizator nu sunt știri propriu-zise — ar merita etichetate distinct.

### [P3-7] Earnings context NVDA din universul pre-earnings randează câmpuri goale
- Live NVDA: `earnings_time_utc: null`, `gap_risk: null`, `gap_risk_level: "NONE"`, `coverage_score: 30` → UI (hotlist_dossier.js:291) afișează dată goală + "gap risk — (NONE) · bias NEUTRAL". Datele watchlist există (quote.earnings_date 2026-08-26) dar nu sunt folosite la acest nivel de fallback.

### [P3-8] Dormant: maparea `mktcap → name` în merge-ul heat
- hotlist_service.py:141-144: dacă un rând scanner ar conține `mktcap`, ar suprascrie `name`. În prezent inert (scanerul nu emite câmpul).

---

## 4. STATUS REGRESII & FIX-URI Runda 1

| Item Runda 1 | Status Runda 2 | Dovezi |
| :--- | :--- | :--- |
| (a) Load Details ștergea cardurile populate (P1) | **REZOLVAT, fără regresie** | fallback fast-build în `_get_hot_list_details_cached` când `allow_build=False` și cache gol (state_service.py:2834-2845); merge păstrează cardurile (`_merge_hot_list_details` :2673-2743); live: 3/3 carduri populate pe `/api/hotlist/state` |
| (b) Deep-analysis eșuată cache-uită 30 min (P2) | **REZOLVAT, fără regresie** | gate `cached.get("available")` (hotlist_service.py:521); live: rândul MARA eșuat (available=false) e păstrat cinstit, iar `MARA__history` nu conține rândul eșuat; retry permis |
| (c) Timeout-uri client dossier 90s / heat 45s | **REZOLVAT** | hotlist_dossier.js:125 (`90000`), :52 (`45000`); deep POST 120s (:316); live: dossier MARA/BMNR/NVDA HTTP 200 în 2.3-4.7s |
| SWR heat (8bf97a2) | **FUNCȚIONEAZĂ** | primul GET: stale servit în 0.18s + refresh în fundal; al doilea GET: `as_of` proaspăt |
| Fallback BMNR pe live-prices (2acbdd9) | **PREZENT** | state_service.py:2613-2619 |
| P1 Runda 1: fallback lanț dezbatere (c7edafc) | **REZOLVAT ÎN COD / INERT ÎN CONFIG → P2-2** | debate.py:197-231; config.json `fallback.chain: []`; MARA live eșuat doar cu Ollama |
| P3 Runda 1: `hot_context` null MARA/BMNR | **DESCHIS** | dossier live MARA/BMNR `hot_context: null` |
| P3 Runda 1: `bull/bear_score` negate | **ÎnCHIS — negarea nu există** | singura re-afisare e hotlist_dossier.js:205 (direct), singura normalizare e clamp 0-10 (debate.py:149-152) |
| Comituri de la Runda 1: c7edafc, e9eda27, 218009f, 1eedcfc | **Fără regresii în căile Tab 8** | git log + verificare cod deasupra; e9eda27/2a3ebce ating pre-earnings (Tab 7), nu Tab 8 |

## 5. VALIDAREA LIVE (rezumat)

- `GET /api/hotlist/heat` (×2): 200; SWR demonstrat; 3 curate (MARA/BMNR/NVDA) + 54 heat (Yahoo gainers/losers/active + Binance).
- `GET /api/hotlist`: toate categoriile populate (gainers 20, losers 20, most_active 19, trending 20, volume_leaders 20; premarket/after_hours marcate `no_live_source`).
- `GET /api/hotlist/state`: 3 carduri; toate scorurile re-verificate numeric (Overall/Priority/News); momentum None (P2-3); `_details_loaded: false` pe toate.
- `GET /api/hotlist/ticker/{MARA,BMNR,NVDA}/dossier`: HTTP 200; MARA deep-analysis eșuat onest; BMNR dezbatere reală 61.5h veche cu verdict coerent (avoid, bull 5/bear 8); NVDA fără deep analysis (niciun rând) — toate degradările oneste.
- 7 cereri GET totale; zero mutații (POST/PUT/PATCH/DELETE); zero fișiere scrise în repo (dumps în `$TMP`).

## 6. RECOMANDĂRI (prioritate)

1. **P1-1**: elimină/conditionează panoul FMP din `renderHotListFmpSection` și nu marca `_details_loaded: True` fără date reale (state_service.py:2973/3015).
2. **P2-2**: populează `ai.fallback.chain` în `config.json` cu providerii deja activi (zai/openai/gemini) — singurul pas lipsesc pentru ca fix-ul c7edafc să livreze 99.9% disponibilitate a dezbaterii.
3. **P2-3**: elimină cache-ul negativ de 6h din `_hotlist_momentum` (sau TTL 10 min + invalidare la sosirea barelor).
4. **P3**: folosește `in_curated_hotlist` în secțiunea "Why it is hot"; șterge `#hotlist-stats`/`#hotlist-freshness-badge` sau implementează-le (last_update e disponibil); mută calculul Risk Heat în backend; alege `top_risk` după severitate; distinge postările eToro de știri în dossier; completează fallback-ul pre-earnings cu datele watchlist pentru nivelul `pre_earnings_universe`.

## 7. CONCLUZIE

Tab 8 rămâne cel mai bine trasat tab din aplicație: scorurile sale provin din formule deterministe re-verificate numeric pe payload live, degradările sunt oneste, iar toate cele trei fix-uri din Runda 1 țin. Cele două probleme materiale rămân de natură "de conectare": un panou FMP mort care afișează zero-uri amenințătoare (P1-1) și un lanț de fallback AI corect implementat dar neconfigurat (P2-2), plus otrăvirea momentum-ului pe 6h (P2-3). Nu s-au detectat regresii introduse de comiturile de la Runda 1.
