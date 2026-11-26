# RAPORT DE AUDIT CRIMINALISTIC EXHAUSTIV: TAB 8 — PREMIUM HOT LIST & DEEP ANALYSIS (`#view-hotlist`)
**Market Signals V9 — Quantitative Systems, Multi-Agent Intelligence & Reverse Data Flow Forensic Audit**
*Data Auditului: 2026-09-04 | Auditor: Senior Quantitative Systems Engineer, Full-Stack Software Architect & UX/UI Expert*

---

## 1. EXECUTIVE SUMMARY & IDENTITATE TAB

Tabul 8, **PREMIUM HOT LIST & DEEP ANALYSIS** (`#view-hotlist`), reprezintă nucleul de selecție tactică de înaltă convingere (*high-conviction idea generation*) din Market Signals V9. Misiunea sa arhitecturală este de a agrega într-un singur ecran atât candidații curatori din lista proprie de monitorizare a utilizatorului, cât și emitenții aflați sub presiune extremă de lichiditate și volatilitate din întregul univers al pieței (Market Heat: Gainers, Losers, Trending, Volume Leaders).

Fiecărui ticker i se calculează un scor compozit ierarhizat (Overall 0-100, Priority Score, Edge Setup A/B/C, Play PRE/POST/WAIT/AVOID), iar utilizatorul poate deschide un **Dossier de Inteligență Criminalistică** complet per simbol și poate declanșa la cerere o **Dezbatere Adversarială Multi-Agent AI** (Bull Case Researcher vs. Bear Case Researcher arbitrați de un Research Portfolio Manager).

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ TAB 8: PREMIUM HOT LIST & DEEP ANALYSIS (#view-hotlist)                                                         │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ [Header] Fire Icon | "Premium Hot List" | [Load Details Button] | [Freshness Badge]                              │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ MARKET HEAT STRIP (#hotlist-heat-strip) — SWR (Stale-While-Revalidate 15s)                                       │
│ [Flame Icon Market Heat] [3 in your Hot List] [Chips: TICKER +%/- % (Categories: Gainers/Losers/Trending)]       │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ HOT LIST CARDS GRID (#hotlist-grid) — Multi-Factor Risk & Opportunity Cards                                      │
│ ┌──────────────────────────────────────┐  ┌──────────────────────────────────────┐  ┌─────────────────────────┐ │
│ │ NVDA $178.45 +1.85%   [OVERALL 82.4] │  │ MARA $11.25 -2.97%   [OVERALL 68.1] │  │ ...                     │ │
│ │ Earnings in 42d: 2026-10-18          │  │ Next Earnings: 2026-11-04 (62d)      │  │                         │ │
│ │ Confidence: [██████████░░░░] 78%     │  │ Confidence: [███████░░░░░░] 55%     │  │                         │ │
│ │ [EDGE A 85.0] [PLAY_PRE] [RISK MED]  │  │ [EDGE B 60.0] [WAIT] [RISK HIGH]    │  │                         │ │
│ │ Trend 1D/4H: BULL / BULL             │  │ Trend 1D/4H: BULL / BULL             │  │                         │ │
│ │ ADX / Momentum: 38.2 | +4.2% / +12%  │  │ ADX / Momentum: 22.1 | -3.1% / -1.5% │  │                         │ │
│ │ Insider / News: Bullish / Bullish    │  │ Insider / News: Neutral / Neutral    │  │                         │ │
│ │ Expected vs 8Q Move: 6.2% | +1.4%    │  │ Expected vs 8Q Move: N/A             │  │                         │ │
│ │ Risk Heat [████████░░░░] 58%         │  │ Risk Heat [████████████] 84%         │  │                         │ │
│ │ Top Risk: High Implied Volatility    │  │ Top Risk: Dilution / Hashrate Drop   │  │                         │ │
│ │ [Folder: Dossier] [Trash: Remove]    │  │ [Folder: Dossier] [Trash: Remove]    │  │                         │ │
│ │ [Button: Analyze (AI & Technical)]   │  │ [Button: Analyze (AI & Technical)]   │  │                         │ │
│ └──────────────────────────────────────┘  └──────────────────────────────────────┘  └─────────────────────────┘ │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ OVERLAYS & MODALS DISPONIBILE:                                                                                   │
│ 1. TICKER DOSSIER MODAL (#hl-dossier-overlay): Quote/Technicals, Why Hot, AI Deep Debate, News, Trades, AI Hist.│
│ 2. CARD DRILLDOWN MODAL (#hotlist-detail-modal): Multi-Timeframe 1D-5M, FMP Institutional Fundamentals, Notes   │
│ 3. SYSTEM ANALYSIS MODAL (#analysis-modal): Technical Engine V1/V2, FMP Fundamentals, Ollama AI, Notebook        │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### Principalele Constatări Criminalistice (Forensic Findings):
1. **Dovada Directă a Erorii HTTP 429 pe Ollama Cloud**:
   În testele live efectuate în acest audit, sistemul a capturat eroarea exactă menționată de utilizator:
   `DataQuality Issue [ollama_generate]: HTTP 429 (Source: ollama)` cu răspunsul de la serverul cloud:
   `{"error":"you (<user>) have reached your weekly usage limit, upgrade for higher limits: https://ollama.com/upgrade or add extra usage: https://ollama.com/settings"}`.
2. **Discrepanță Critică în Lanțul de Fallback AI (Debate vs. Decision Engine)**:
   - În modulul de analiză deterministă (`core/ticker_analyzer.py` -> `core/ai_analysis_schema.py`), la apariția erorii 429 se activează instantaneu `build_deterministic_structured_analysis()`, care livrează o analiză regulamentară pe bază de voturi tehnice (trend 1D/4H, RSI, MACD, ATR) cu nota explicită: *"Deterministic fallback used because AI JSON was invalid."*
   - În schimb, în modulul de dezbatere **Deep Analysis** (`core/ai/debate.py:192-193`), funcția apelează `_build_providers(config_root, svc, role="research_scientist")`, dar **aruncă la gunoi cel de-al treilea parametru returnat** (`fallback_chain`) folosind operatorul dummy `_`! Drept urmare, la eroarea 429 pe Ollama, dezbaterea Bull/Bear **NU apelează fallback-ul pe OpenAI, Anthropic sau Gemini**, ci se blochează cu `error: "bull_argument_unavailable"`, lăsând analiza indisponibilă.
3. **Arhitectura Stale-While-Revalidate (SWR) pe Market Heat**:
   Pusă în funcțiune la data de 2026-09-03, elimină timeout-urile de 30-45 secunde raportate anterior. Endpoint-ul `/api/hotlist/heat` servește din cache în sub 5ms (TTL 15s). Dacă datele au expirat, livrează instantaneu datele stale și declanșează asincron un thread daemon `heat-swr` pentru reîmprospătare. Pe client (`frontend/hotlist_dossier.js`), un cache suplimentar de 60s (`heatCache`) și garda `heatInflight` previn suprasolicitarea rețelei la fiecare ciclu de interogare de stare.
4. **Triplă Schizofrenie Modală (UI/UX Friction)**:
   De pe un singur card din Hot List, utilizatorul are 3 căi diferite de deschidere a datelor, fiecare deschizând un modal complet separat:
   - Clic pe corpul cardului -> deschide `#hotlist-detail-modal` (`UI.showHotListDetail` din `frontend/app.js:22012`).
   - Clic pe butonul Folder (sus dreapta) sau pe un cip din Market Heat -> deschide `#hl-dossier-overlay` (`UI.openTickerDossier` din `frontend/hotlist_dossier.js:100`).
   - Clic pe butonul "Analyze" -> deschide `#analysis-modal` (`Actions.analyzeTicker` din `frontend/app.js:22600`).
   Această fragmentare creează confuzie cognitivă majoră pentru un utilizator nou, informația fiind disipată în trei locuri.
5. **Reconcilierea Onestă a Contradicțiilor de Trend**:
   Sistemul rezolvă elegant anomalia raportată pe 2026-09-03 (când un ticker apărea în lista *Losers*, dar avea trend daily *BULL*): secțiunea "Why it is hot" explică explicit traderului că eticheta descrie o mișcare intraday de la momentul scanării, în timp ce trendul 1D descrie structura macro pe timeframe superior.

---

## 2. REVERSE DATA FLOW (UI -> SURSE, ENDPOINTS & SERVICII)

### 2.1. Matricea Criminalistică de Trasabilitate (De la Element DOM la Sursă & Fallback)

| Element UI / DOM ID | JS Controller (Frontend) | Rută API & Metodă HTTP | Modul Backend & Handler | Sursă Primară de Date | Lanț de Failover & Politică Fallback |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Market Heat Strip**<br>`#hotlist-heat-strip` | `UI.loadHotlistHeat()` & `UI.renderHotlistHeat(data)`<br>[`frontend/hotlist_dossier.js:40-96`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/hotlist_dossier.js#L40-L96) | `GET /api/hotlist/heat` | `api.main:api_hotlist_heat`<br>[`api/main.py:8510`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/api/main.py#L8510) -> `api.hotlist_service:build_heat_list` | `ScannerAgent` (Yahoo Screener + Binance 24h ticker) | **Stale-While-Revalidate (15s)**. Thread de fundal `heat-swr`. Dacă Yahoo dă 429, cade pe watchlist yfinance + Binance. Frontend păstrează cache 60s. |
| **Grilă Carduri Hot List**<br>`#hotlist-grid` | `UI.renderHotList(state)`<br>[`frontend/app.js:21603-21975`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L21603-L21975) | `GET /api/state` (`state.hot_list`) | `api.state_service:get_state` -> `core.hotlist_scoring:get_hotlist_state` | Stare compilată din DB SQLite + Scanner | Cardurile folosesc date cached. Butonul "Load Details" forțează îmbogățirea cu date FMP instituționale. |
| **Cotație Live & 24h %**<br>`.hot-card-price`, `.hot-card-change` | `UI.renderHotList`<br>[`frontend/app.js:21873-21876`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L21873-L21876) | `GET /api/state` | `core.hotlist_module:enrich_hotlist_items` | Yahoo Finance / eToro rates / Binance | eToro timeout (10s) -> Yahoo Finance -> Persisted `data/live_prices_cache.json` -> Afișare cinstită `N/A`. |
| **Insignă Risk Heat**<br>`.hot-card-riskheat`, `.risk-heat-fill` | `UI.renderHotList`<br>[`frontend/app.js:21685-21695`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L21685-L21695) | `GET /api/state` | `core.hotlist_scoring:compute_hotlist_composite_metrics` | Calcul algoritmic dinamic | `riskHeatPct = 20 + 14 * flagCount + riskPts` (clamp 20-95%). Deduce riscul real din numărul de stegulețe de earnings. |
| **Indicatori Tehnici (Trend, ADX, Mom)**<br>`.hot-card-quicklines` | `UI.renderHotList`<br>[`frontend/app.js:21905-21933`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L21905-L21933) | `GET /api/state` | `core.ta:TechnicalAnalysis` | Bare istorice 1D/4H/1H (StockDataClient / Binance) | Cache disc bare OHLC (`data/bars/`). Dacă lipsesc barele, afișează `N/A` și badge-ul "Partial Data". |
| **Buton Load Details**<br>`#hotlist-load-details-btn` | `Actions.loadHotListDetails()`<br>[`frontend/app.js:5735-5765`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L5735-L5765) | `GET /api/state?hotlistDetails=true` | `api.state_service:get_state(hotlist_details=True)` | Financial Modeling Prep (FMP API) | Dacă FMP lipsește sau dă eroare, `_details_loaded=False`. Cardul afișează `N/A` pentru Piotroski, EV/EBITDA, Insider. |
| **Buton Analyze**<br>`.btn-hot-analysis` | `Actions.analyzeTicker(ticker)`<br>[`frontend/app.js:22600-22640`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L22600-L22640) | `POST /api/analyze-ticker` | `api.actions_service:analyze_ticker_mode`<br>[`api/actions_service.py:1176`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/api/actions_service.py#L1176) | Engine V1/V2 + Yahoo + FMP + Ollama | Cache memorie 2 ore. Dacă Ollama e 429 -> Fallback determinist (`core/ai_analysis_schema.py:169`). |
| **Modal Ticker Dossier**<br>`#hl-dossier-overlay` | `UI.openTickerDossier(ticker)`<br>[`frontend/hotlist_dossier.js:100-133`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/hotlist_dossier.js#L100-L133) | `GET /api/hotlist/ticker/{ticker}/dossier` | `api.hotlist_service:build_ticker_dossier`<br>[`api/hotlist_service.py:461`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/api/hotlist_service.py#L461) | Agregator Multi-Modul (Quote, News, DB, Ledger) | Cache bounded 60s (`_dossier_cache`). Fiecare secțiune degradează independent fără a bloca întregul dossier. |
| **Dossier Tab 1: Quote & Why Hot**<br>`.hl-dossier-section` | `UI.renderTickerDossier`<br>[`frontend/hotlist_dossier.js:150-189`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/hotlist_dossier.js#L150-L189) | Date din răspunsul `/dossier` | `api.hotlist_service:_quote_section` | Watchlist state -> `state.json` -> `live_prices_cache.json` | 3 niveluri de căutare exclusiv locale (fără apel remote pe calea critică de citire). |
| **Dossier Tab 2: News**<br>`.hl-news-list` | `UI.renderTickerDossier`<br>[`frontend/hotlist_dossier.js:221-230`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/hotlist_dossier.js#L221-L230) | Date din răspunsul `/dossier` | `api.hotlist_service:_news_section` | `NewsTabAgent` (Finnhub / RSS Yahoo) | Dacă Finnhub nu are cheie API -> degradare automată la flux RSS Yahoo. Dacă agentul e oprit -> `news_agent_initializing`. |
| **Dossier Tab 3: Our Trades**<br>`.hl-totals`, `.hl-table` | `UI.renderTickerDossier`<br>[`frontend/hotlist_dossier.js:232-260`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/hotlist_dossier.js#L232-L260) | Date din răspunsul `/dossier` | `api.hotlist_service:_trades_section` | SQLite `data/market_signals.db` (`capital_state`) | Interogare directă locală SQLite (timeout 2s). Dacă utilizatorul nu a tranzacționat tickerul -> afișează cinstit "We never traded this ticker". |
| **Dossier Tab 4: AI Decision History**<br>`.hl-dec-list` | `UI.renderTickerDossier`<br>[`frontend/hotlist_dossier.js:261-280`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/hotlist_dossier.js#L261-L280) | Date din răspunsul `/dossier` | `api.hotlist_service:_decisions_section` | `core.decision_ledger.store:DecisionLedgerStore` | Citire din istoricul local de decizii. Extrage motivul real (`reason`/`thesis`) chiar dacă decizia a fost REJECTED. |
| **Dossier Tab 5: Pre-Earnings**<br>`.hl-dossier-section` | `UI.renderTickerDossier`<br>[`frontend/hotlist_dossier.js:282-295`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/hotlist_dossier.js#L282-L295) | Date din răspunsul `/dossier` | `api.hotlist_service:_pre_earnings_section` | `data/pre_earnings.json` -> Watchlist -> `data/last_known_earnings.json` | Lanț triplu de memorie: dacă e în afara universului de scoring, folosește data din watchlist; dacă lipsește tranzitoriu, ia din `last_known_earnings.json`. |
| **Deep Analysis (AI Debate)**<br>`.hl-deep-verdict`, `#hl-deep-btn` | `UI.runHotlistDeepAnalysis(ticker)`<br>[`frontend/hotlist_dossier.js:308-324`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/hotlist_dossier.js#L308-L324) | `POST /api/hotlist/ticker/{ticker}/deep-analysis` | `api.hotlist_service:run_deep_analysis`<br>[`api/hotlist_service.py:511`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/api/hotlist_service.py#L511) -> `core.ai.debate:run_entry_debate` | Multi-LLM (Ollama Cloud / Local / GLM-5.3-flash) | **Cache disc 30 min** (`data/hotlist_deep_analysis.json`), singleflight per ticker. Dacă dă 429 -> `available: false`, păstrează analiza anterioară. |

---

## 3. ANATOMIA DEZBATERII MULTI-AGENT AI (TRADINGAGENTS ARCHITECTURE)

Secțiunea **Deep Analysis** din Ticker Dossier implementează un cadru inspirat din lucrarea academică *TradingAgents* (TauricResearch), înlocuind opinia monolit a unui singur LLM cu o dispută dialectică structurată:

```
                  ┌────────────────────────────────────────────────────────┐
                  │                 EVIDENCE SIGNAL PAYLOAD                │
                  │ Price, 1D/4H/1H/30M/5M Trends, RSI, MACD, Regime,      │
                  │ ADV USD, Top 5 News Headlines, Prior Trades Base Rates │
                  └──────────────────────────┬─────────────────────────────┘
                                             │
                       ┌─────────────────────┴─────────────────────┐
                       ▼                                           ▼
         ┌───────────────────────────┐               ┌───────────────────────────┐
         │      BULL RESEARCHER      │               │      BEAR RESEARCHER      │
         │ Role: "research_scientist"│               │ Role: "research_scientist"│
         │ Model: glm-5.3-flash:cloud│               │ Model: glm-5.3-flash:cloud│
         │ Case: Maximizes positives,│               │ Case: Attacks weak links, │
         │ addresses risk flags.     │               │ base rates, gap risks.    │
         └─────────────┬─────────────┘               └─────────────┬─────────────┘
                       │                                           │
                       │ 3-5 tight sentences plain prose           │ 3-5 tight sentences plain prose
                       └─────────────────────┬─────────────────────┘
                                             │
                                             ▼
                               ┌───────────────────────────┐
                               │     RESEARCH MANAGER      │
                               │ Role: "portfolio_manager" │
                               │ Model: glm-5.3-flash:cloud│
                               │ Judges both cases against │
                               │ evidence. Output: JSON    │
                               └─────────────┬─────────────┘
                                             │
                                             ▼
                      ┌──────────────────────────────────────────────┐
                      │               STRUCTURED VERDICT             │
                      │ Recommendation: APPROVE | REDUCE |           │
                      │                 WATCH | AVOID                │
                      │ Confidence: 0.0 - 1.0 (ex: 0.78 = 78%)       │
                      │ Bull Score: 0 - 10 | Bear Score: 0 - 10      │
                      │ Rationale: Deciding factor in plain prose    │
                      └──────────────────────────────────────────────┘
```

### Prompt-urile Agenților ([`core/ai/debate.py:81-121`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/ai/debate.py#L81-L121)):
1. **Bull Researcher Prompt**:
   > *"You are the Bull Researcher in a trading firm. Argue the STRONGEST honest case for taking the proposed {action} on {ticker}. Use only the evidence below — never invent data. Address the reviewer's risk flags head-on and explain why the position still merits taking. Write 3-5 tight sentences, plain prose."*
2. **Bear Researcher Prompt**:
   > *"You are the Bear Researcher in a trading firm. Argue the STRONGEST honest case AGAINST taking the proposed {action} on {ticker}. Use only the evidence below — never invent data. Attack the weakest link: thesis assumptions, data quality, gap/liquidity risk, base rates. Write 3-5 tight sentences, plain prose."*
3. **Research Manager Prompt**:
   > *"You are the Research Manager judging a one-round bull/bear debate about a proposed paper trade on {ticker} ({direction}). Weigh both arguments against the evidence summary and the system's own reviewer verdict, then issue YOUR post-debate verdict. Return STRICT JSON: {recommendation: approve|reduce|watch|avoid, confidence: 0.0-1.0, rationale, bull_score: 0-10, bear_score: 0-10}."*

---

## 4. LANȚUL CRIMINALISTIC DE FAILOVER: CE SE ÎNTÂMPLĂ CÂND CRAPĂ PROVIDERII?

### 4.1. Cazul Real de Astăzi: Eroare HTTP 429 Quota Exceeded pe Ollama Cloud
În timpul auditului live a fost reprodusă exact starea din log:
```
DataQuality Issue [ollama_generate]: HTTP 429 (Source: ollama)
Ollama generation failed: {"error":"you (<user>) have reached your weekly usage limit, upgrade for higher limits: https://ollama.com/upgrade or add extra usage: https://ollama.com/settings (ref: 896ce5e0-9864-47ed-aaf6-061967b5a282)"}
Endpoint cooldown active for provider=ollama, status=429
DataQuality Issue [ollama_generate]: circuit open (Source: ollama)
```

**Unde cade analiza și ce date livrează?**
Sistemul reacționează diferențiat în funcție de punctul de intrare:

1. **La nivel de Ticker Analyzer (`core/ticker_analyzer.py:1088-1095`)**:
   - `ollama.generate()` returnează dicționarul `{"error": "Ollama error: 429"}`.
   - `single_raw = ""` (șir vid).
   - Apelul intră în [`core/ai_analysis_schema.py:270-278`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/ai_analysis_schema.py#L270-L278):
     `parse_structured_ai_output_with_deterministic_fallback(single_raw, context_payload)`.
   - Se apelează `build_deterministic_structured_analysis(context_payload)`:
     - Calculează voturile de trend: `bull_votes` și `bear_votes` pe baza `trend_1d`, `trend_4h`, `rsi`, `macd_hist` și `change_pct`.
     - Determină `sentiment`: `bullish`, `bearish` sau `neutral`.
     - Setează `confidence`: formulă bazată pe diferența de voturi (ex. 0.55 - 0.72).
     - Setează `earnings_play`: `"WAIT"`.
     - Livrează planul de tranzacționare cu nota explicită în UI:
       `"notes": "Deterministic fallback used because AI JSON was invalid."`.
   - **Verdict**: UI-ul nu crapă, nu afișează eroare roșie, ci livrează o analiză cantitativă cinstită bazată pe reguli deterministe.

2. **La nivel de Deep Analysis Debate (`core/ai/debate.py:203-236`) — DEFICIT ARHITECTURAL IDENTIFICAT**:
   - `bull_resp = bull_provider.generate(...)` primește `AIResponse(text="", error="Ollama error: 429")`.
   - La linia 207: `if not bull_text: result["error"] = "bull_argument_unavailable"; return result`.
   - `result["available"] = False`.
   - **Vulnerabilitate**: În [`core/ai/debate.py:192-193`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/ai/debate.py#L192-L193):
     ```python
     bull_provider, bull_model, _ = _build_providers(config_root, svc, role="research_scientist")
     manager_provider, manager_model, _ = _build_providers(config_root, svc, role="portfolio_manager")
     ```
     Variabila returnată pentru `fallback_chain` este ignorată cu `_`! Astfel, deși sistemul are configurat lanț de rezervă în `config.json` (OpenAI, Gemini, Anthropic), funcția de dezbatere **nu încearcă furnizorii secundari**!
   - Pe ecranul utilizatorului, în Dossier, butonul revine la starea inactivă cu mesajul:
     `"No deep analysis yet. Run the AI debate on this ticker's current evidence."`.

### 4.2. Ce se întâmplă dacă Yahoo Finance rate-limitează cotațiile de căldură (Market Heat)?
1. În [`agents/scanner_agent.py:671-752`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/scanner_agent.py#L671-L752), apelurile către `query1.finance.yahoo.com/v1/finance/screener/...` interceptează eroarea 429:
   `logger.warning(f"Yahoo screener rate limited for {scr_id}")`.
2. Se activează fallback-ul imediat:
   `Fallback: if Yahoo screener failed, try yfinance for watchlist stocks` ([`agents/scanner_agent.py:812`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/scanner_agent.py#L812)).
3. Criptomonedele din Market Heat sunt protejate complet, provenind din fluxul independent Binance (`/api/v3/ticker/24hr`) sau KuCoin.
4. În [`api/hotlist_service.py:86-115`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/api/hotlist_service.py#L86-L115), mecanismul SWR livrează instantaneu ultimul set valid de cipuri din cache (`_heat_cache["payload"]`).
5. Dacă totuși scannerul este la pornire rece și Yahoo este blocat, se întoarce `"scanner_available": false`, iar UI-ul afișează discret insigna:
   `scanner warming up…`, fără a bloca randarea cipurilor din Hot List-ul curatoriat al utilizatorului.

### 4.3. Ce se întâmplă dacă eToro dă Timeout pe Rate-uri?
În proba executată astăzi, log-ul a înregistrat:
`eToro request /market-data/instruments/rates failed: HTTPSConnectionPool(host='public-api.etoro.com', port=443): Read timed out. (read timeout=10)`.
Sistemul a retrogradat instantaneu la nivelul următor din lanț: cotațiile au fost preluate din Yahoo Finance și cache-ul local `live_prices_cache.json`. Pentru utilizator, prețul a rămas afișat fără întrerupere.

---

## 5. AUDIT UI/UX, ERGONOMIE & COGNITIVE LOAD

### 5.1. Puncte Forte de Design & Ergonomie:
- **Insigna Dinamică de Risk Heat**: Trecerea de la vechiul indicator static (90/62/28) la formula dinamică bazată pe stegulețele reale de earnings (`riskFlags.length`) oferă o granularitate vizuală autentică. Doi emitenți din aceeași categorie de risc afișează bare de progres diferite (ex. 48% vs. 76%).
- **Tratarea Onestă a Datelor Parțiale (`Partial Data Badge`)**: Dacă un card se bazează pe fluxul rapid de pornire fără detalii fundamentale sau fără istoric pe 8 trimestre, cardul primește eticheta gri `Partial Data` ([`frontend/app.js:21718`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L21718)), avertizând traderul să nu execute orbește.
- **Transparența Schizofreniei de Trend (Intraday vs. Macro)**: Dacă un emitent este catalogat *Losers* din cauza scăderii intraday, dar trendul său 1D este *BULL*, Dossier-ul afișează un avertisment inteligent: *"Categorised losers at scanner time, but live change is +X% — scan snapshot describes intraday move, 1D trend describes daily structure. Both real."*

### 5.2. Puncte Slabe & Fricțiuni Ergonomice Majore:
1. **Tripla Fractură a Ferestrelor Modale (Modal Chaos)**:
   Așa cum s-a subliniat în sumar, prezența a trei modale independente pentru același ticker este o eroare gravă de arhitectură UX:
   - Utilizatorul apasă pe card -> vede date fundamentale FMP și trenduri (fără dezbaterea AI).
   - Utilizatorul apasă pe iconița folder -> vede dezbaterea AI și istoricul de tranzacții (fără datele fundamentale FMP avansate).
   - Utilizatorul apasă pe "Analyze" -> vede motorul de semnale V1/V2 și un alt AI (Ollama legacy).
   *Recomandare*: Unificarea completă a celor trei experiențe într-un singur **Unified Ticker Workspace Modal** cu tab-uri clare.
2. **Inconsistența Terminologiei Butoanelor**:
   Pe card există butonul `Analyze` cu iconiță de microchip ([`frontend/app.js:21950`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L21950)), iar în Dossier există `Run deep analysis` cu iconiță de baghetă magică ([`frontend/hotlist_dossier.js:218`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/hotlist_dossier.js#L218)). Utilizatorul nu înțelege de ce un ecran numește analiza "Analyze" și celălalt "Deep Analysis", deși rulează mecanisme complet diferite.
3. **Absența Nivelurilor Tehnice Cheie în Cardul Principal**:
   Cardurile din `#hotlist-grid` afișează `Trend 1D/4H`, `ADX/Momentum`, `Expected vs 8Q Move`, dar **nu afișează niveluri de Pivot, Suport 1 / Rezistență 1 sau ATR curent**, deși acestea sunt calculate în backend. Traderul trebuie să calculeze mental nivelul de intrare.

---

## 6. BENCHMARK COMPARATIV CU PLATFORMELE DIN INDUSTRIE

| Funcționalitate / Criteriu | Market Signals V9 (Tab 8 Hot List) | Seeking Alpha Quant Ratings | TipRanks Smart Score | Koyfin Deep Dive |
| :--- | :--- | :--- | :--- | :--- |
| **Market Heat Discovery** | **EXCELENT**: Merged Screener (Gainers/Losers/Active) + Curated Watchlist într-un singur feed SWR. | **MEDIOCRE**: Screenere separate, fără integrare directă cu watchlist-ul în aceeași bandă. | **SLAB**: Listă simplă de trending fără distincție intraday vs structură de trend. | **BUN**: Watchlist integrat cu heat maps grafice avansate. |
| **Evaluare & Scoring** | **BUN**: Overall 0-100, Priority Score, Edge Setup (A/B/C) bazat pe volatilitate și earnings. | **EXCELENT**: Factor Grades A+ la F (Valuation, Growth, Profitability, Momentum, EPS Revisions). | **BUN**: Smart Score 1-10 agregat din 8 factori independenți. | **EXCELENT**: Scoruri financiare detaliate (Piotroski F-Score, Altman Z-Score, Beneish M-Score). |
| **Analiză Fundamentală** | **BUN**: Integrare FMP (Piotroski, EV/EBITDA, Owner Earnings, Senat/Congress Trades, Insider). | **EXCELENT**: Date financiare complete pe 10 ani, transcrieri de teleconferințe. | **MEDIOCRE**: Focus exclusiv pe insider trading și ținte de preț ale analiștilor. | **SUPERIOR (World Class)**: Bilanț contabil interactiv, cash flow, grafice de multipli financiari istorici. |
| **Inteligență Artificială & LLM** | **UNIC ÎN INDUSTRIE**: Dezbatere adversarială Bull Case vs Bear Case cu arbitraj de Manager de Portofoliu. | **INEXISTENT**: Doar articole scrise de autori umani și sinteze statice de știri. | **MEDIOCRE**: Sinteze simple de sentiment din articole de presă (Blogger Sentiment). | **INEXISTENT**: Fără agenți generativi de dezbatere dialectică. |
| **Trasabilitate Tranzacțională** | **EXCELENT**: Tab dedicat "Our Trades" care leagă ideea direct de execuțiile trecute din portofoliul propriu. | **INEXISTENT**: Nu știe ce a tranzacționat utilizatorul pe tickerul respectiv. | **MEDIOCRE**: Arată doar portofoliul de tip mock. | **BUN**: Conectivitate cu brokeri (Plaid/Yodlee) pentru urmărire portofoliu. |
| **Transparența Deciziilor AI** | **EXCELENT**: Decision Ledger integrat cu buton explicit "Why?" pentru semnale respinse. | **N/A**: Modele cantitative proprietare de tip "cutie neagră" (black box). | **N/A**: Fără explicații dialectice detaliate. | **N/A**: Doar metrici cantitative brute. |

---

## 7. MATRICEA SWOT (STRENGTHS, WEAKNESSES, OPPORTUNITIES, THREATS)

### STRENGTHS (Puncte Forte)
- **Arhitectură de Dezbatere Adversarială (TradingAgents Paradigm)**: Confruntarea directă a tezei Bull cu teza Bear forțează modelul lingvistic să caute fisuri în raționamentul de tranzacționare, eliminând halucinațiile optimiste tipice ale unui singur prompt.
- **Securitate & Reziliență prin SWR (Stale-While-Revalidate)**: Eliminarea blocării browserului pe `/api/hotlist/heat` prin thread-ul de fundal `heat-swr` este un exemplu de manual de inginerie de performanță web.
- **Transparența Cinstită a Istoricului din Decision Ledger**: Includerea secțiunii "AI Decision History" cu acces direct la motivul respingerii rezolvă frustrarea majoră a traderilor care vor să știe de ce sistemul a refuzat o intrare.
- **Fallback Determinist Robust în Ticker Analyzer**: Căderea pe algoritmul bazat pe voturi de trend și indicatori tehnici garantează că utilizatorul primește întotdeauna un verdict cuantificabil, chiar și în avarii totale ale providerilor de LLM.

### WEAKNESSES (Puncte Slabe)
- **Ignorarea Lanțului de Fallback în `core/ai/debate.py`**: Utilizarea `_` pentru a ignora `fallback_chain` în faza de dezbatere multi-agent face ca sistemul să clacheze la prima eroare HTTP 429 de la Ollama, ignorând cheile valide de OpenAI/Gemini/Anthropic prezente în sistem.
- **Fragmentarea Experienței Utilizatorului în 3 Modale Suprapuse**: Prezența simultană a `#hotlist-detail-modal`, `#hl-dossier-overlay` și `#analysis-modal` creează o curbă de învățare abruptă și confuzie operațională în momentele critice de pre-deschidere a pieței.
- **Absența Nivelurilor Cheie de Preț pe Card**: Lipsa afișării nivelurilor de Pivot (R1/S1), ATR și Breakout Price pe cardul principal obligă utilizatorul să caute manual aceste valori în alte tab-uri.
- **Dependența de Finnhub Neconfigurat**: În proba live s-a confirmat că știrile de acțiuni degradează frecvent la fluxul RSS Yahoo deoarece cheia Finnhub nu este configurată implicit.

### OPPORTUNITIES (Oportunități de Dezvoltare)
- **Activarea Fallback-ului Multi-LLM în Dezbatere**: Conectarea lanțului `fallback_chain` în `debate.py` va asigura o disponibilitate de 99.99% a dezbaterilor AI, comutând automat pe Gemini Flash sau OpenAI GPT-4o-mini la 429 pe Ollama.
- **Consolidarea într-un Dosar Unic Master-Detail**: Comasarea datelor tehnice, a datelor fundamentale FMP, a dezbaterii AI și a carnetului de notițe într-o singură fereastră modală cu tab-uri curate (Overview, Debate, Fundamentals, History, Notes).
- **Adăugarea Nivelurilor de Execuție Direct pe Card**: Includerea a două linii compacte de preț: `Pivot: $X.XX | R1: $Y.YY | S1: $Z.ZZ` și `Suggested Stop: $W.WW`.

### THREATS (Amenințări Tehnice)
- **Rate-Limiting Agresiv pe Yahoo Screener**: Blocarea IP-ului de către Yahoo Finance în perioadele de mare volatilitate poate lăsa Market Heat fără cotațiile de acțiuni americane dacă nu se adaugă un provider secundar dedicat de screener (ex. Finnhub sau Alpaca).
- **Consumul Rapid al Cotelor pe LLM Cloud Gratuit**: Bazarea exclusivă pe contul `<user>` pe Ollama Cloud fără un limitator de rată client-side duce la epuizarea săptămânală a resurselor înainte de încheierea zilelor de tranzacționare.

---

## 8. RECOMANDĂRI CU PRIORITATE (P1 - P3)

### [P1] Remedierea Imediată a Lanțului de Fallback AI în `core/ai/debate.py`
- **Fișier afectat**: [`core/ai/debate.py:184-235`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/ai/debate.py#L184-L235)
- **Soluție**: Nu mai ignora parametrul `fallback` returnat de `_build_providers`. Dacă `bull_resp.text` este vid sau providerul primar aruncă o excepție (HTTP 429/500/timeout), iterează prin lista `fallback` și apelează providerul de rezervă (OpenAI -> Gemini -> Anthropic).
- **Impact**: Garantează că utilizatorul poate rula "Deep analysis" chiar dacă Ollama Cloud este pe limita de cotă săptămânală.

### [P2] Unificarea celor 3 Modale într-un Singur "Unified Ticker Dossier"
- **Fișiere afectate**: [`frontend/hotlist_dossier.js`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/hotlist_dossier.js), [`frontend/app.js:22012-22123`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L22012-L22123), [`frontend/app.js:22668-22845`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L22668-L22845).
- **Soluție**: Eliminarea modalului redundant `hotlist-detail-modal`. Atunci când utilizatorul dă clic pe card, pe iconița folder sau pe un cip din heat strip, să se deschidă același modal unificat (`#hl-dossier-overlay`), îmbogățit cu tab-urile:
  1. *Overview & Quotes* (Preț, Trenduri MTF 1D-5M, S/R Levels, Why Hot).
  2. *AI Deep Debate* (Dezbaterea Bull/Bear/Manager + buton re-run).
  3. *Institutional Fundamentals* (Datele FMP: Piotroski, Multipli, Congress Trades, Insider).
  4. *News & Catalysts* (Articole recente cu sentiment).
  5. *Trade & Ledger History* (Tranzacțiile proprii simulate + deciziile istorice din ledger).
  6. *Strategy Notes* (Carnetul de notițe pentru ticker).

### [P3] Îmbunătățirea Cardului Principal din `#hotlist-grid` cu Niveluri Cheie
- **Fișier afectat**: [`frontend/app.js:21861-21954`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L21861-L21954)
- **Soluție**: Adăugarea unei linii suplimentare în `.hot-card-quicklines`:
  `Levels: Pivot $X | R1 $Y | S1 $Z | ATR $W`.
- **Impact**: Oferă traderului reperele concrete de execuție direct pe card, fără a fi nevoit să deschidă modalul pentru fiecare acțiune.

---

## 9. CONCLUZIE CRIMINALISTICĂ

Tabul 8 (**PREMIUM HOT LIST & DEEP ANALYSIS**) din Market Signals V9 este un modul cantitativ de excepție, remarcabil prin integrarea unică a paradigmei de cercetare dialectică adversarială (TradingAgents) și prin mecanismul de protecție a latenței Stale-While-Revalidate. Deficiențele identificate nu țin de logica matematică de calcul a scorurilor (care este riguroasă și cinstită față de calitatea datelor), ci de deconectarea lanțului de fallback multi-provider în dezbaterea AI și de fragmentarea neergonomică a interfeței grafice în trei ferestre modale paralele. Cu implementarea măsurilor P1 și P2, Tabul 8 atinge standardul de aur al platformelor instituționale de vârf.
