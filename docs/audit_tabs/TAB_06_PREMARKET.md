# RAPORT DE AUDIT CRIMINALISTIC EXHAUSTIV: TAB 6 — PREMARKET SCANNER (`#view-premarket`)
**Market Signals V9 — Quantitative Systems & Ergonomic Architecture Audit**
*Data Auditului: 2026-09-04 | Auditor: Senior Quantitative Systems Engineer, Full-Stack Software Architect & UX/UI Expert*

---

## 1. EXECUTIVE SUMMARY & IDENTITATE TAB

Tabul 6, **PREMARKET SCANNER** (`#view-premarket`), este conceput ca un radar de volatilitate pre-market ("Session gap radar") dedicat traderilor de acțiuni US care urmăresc mișcările matinale de tip *Gap & Go*, *Gap Fade* sau *Reversal*. Misiunea sa arhitecturală este de a agrega emitenții cu variații mari de preț din sesiunea extinsă (overnight / pre-market), de a filtra zgomotul prin prisma catalizatorilor fundamentali (știri de impact și raportări financiare de tip earnings), de a sintetiza un scor compozit direcțional (LONG vs SHORT vs WATCH) și de a oferi o sinteză executivă generată de inteligență artificială (**Pre-Market Pulse**).

```
┌─────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ TAB 6: PREMARKET SCANNER (#view-premarket)                                                              │
├─────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ [Header] Sun Icon | "PREMARKET SCANNER" | [Refresh Button (Actions.refreshPremarket)]                   │
│ [Statusbar] Clock | Candidates Count | Next Run Countdown | Reported Count | [Diagnostics Dropdown]     │
│ [Pre-Market Pulse Card (#premarket-pulse)] AI Sentiment Badge | LLM Summary | Top Long/Short | Risks    │
│ [Reported Today Strip (#premarket-reported-today)] Collapsible list of spent earnings catalysts         │
│ [Summary Strip (#premarket-summary)] Total Filtered | Long/Short/Watch Distribution | Avg |Gap|% | Rep. │
│ ─────────────────────────────────────────────────────────────────────────────────────────────────────── │
│ [Workspace Main Card]                                   │ [Workspace Aside Card]                        │
│  ├─ Toolbar: Search | Direction | Sort | MinScore | View│  └─ Details Panel (#premarket-details)        │
│  └─ Sections Grid (#premarket-sections):                │      ├─ Symbol, Direction & Gap% Pill         │
│       ├─ LONG Candidates (Gap Up + Bullish Catalyst)    │      ├─ Score, Premkt Price, PrevClose        │
│       ├─ SHORT Candidates (Gap Down + Bearish Catalyst) │      ├─ Earnings Date, EPS/Rev Surprise       │
│       └─ WATCH ONLY Candidates                          │      ├─ Vol Rel, Catalyst/Liq, Inputs Age     │
│          (Or 1-line tape in "Stream" view mode)         │      ├─ Deterministic "Why" & AI "Why Moving" │
│                                                         │      └─ Top News Headlines (max 5)            │
└─────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### Principalele Constatări Criminalistice:
1. **Originea Datelor de Cotație Premarket**: Scannerul **nu** interoghează brokeri precum Alpaca sau eToro pentru Tabul 6. Sursa exclusivă de descoperire a emitenților și a cotațiilor din sesiunea extinsă este API-ul Yahoo Finance (`query1.finance.yahoo.com/v1/finance/screener/predefined/saved` cu `day_gainers`, `day_losers`, `most_actives` și `trending/US`).
2. **Deficitul Structural de Volum Premarket (RVOL)**: Yahoo Finance screener returnează frecvent câmpul `preMarketVolume` ca `0` sau `null`. Fără un fallback către un provider de Level 1 real-time (ex. Alpaca IEX data feed, Polygon sau Databento), câmpurile `premarket_volume` și `premarket_vol_rel` sunt `null` pentru marea majoritate a candidaților. Pe cardurile principale din UI, **RVOL nici măcar nu este randat**, fiind vizibil exclusiv în panoul lateral ca `Vol Rel: n/a`.
3. **Anomalia Eliminării Raportărilor de Câștiguri ("Spent Catalysts")**: Printr-o decizie inginerească (`eliminate_reported = True`), emitenții care au raportat câștiguri în seara precedentă (AMC) sau în dimineața curentă (BMO după 09:30) sunt **eliminați** din listele active `LONG`, `SHORT` și `WATCH` și mutați într-un dropdown pliat (`#premarket-reported-today`). În practica profesională de tranzacționare, acțiunile care au raportat AMC ieri sunt exact principalii candidați de gap trading ai dimineții!
4. **Discrepanța Nivelurilor Cheie (Pivot / Suport / Rezistență / Volum 15m)**: Deși indispensabile pentru tranzacționarea de gap-uri, nivelurile Pivot (Floor/Camarilla), Suport/Rezistență și metrica "Volum ultimele 15 minute" **nu există în codul sursă** al Tabului 6 (nici în backend, nici în frontend).
5. **Comportamentul la Deschiderea Pieței (09:30 ET)**: La deschiderea sesiunii regulate, backend-ul intră în starea `PAUSED` (`market_open_paused`) și golește lista `candidates = []`. Deși salvează `snapshot_candidates`, frontend-ul nu folosește acest snapshot, astfel că la 09:30 ecranul devine complet gol ("No results"), tocmai în primele 30-60 de minute când traderul are nevoie critică de referințele premarket.

---

## 2. REVERSE DATA FLOW (UI -> SURSE & LANȚURI DE FAILOVER)

### 2.1. Matricea Criminalistică de Trasabilitate (UI -> Cod Sursă)

| Element UI / DOM ID | JS Controller (Frontend) | Rute API & Backend Endpoint | Modul Backend & Clasă | Sursă Primară de Date | Lanț de Failover / Stale Fallback |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Premarket View Container**<br>`#view-premarket` | `ViewRouter.navigate('premarket')`<br>[`frontend/app.js:12938-12941`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L12938-L12941) | `GET /api/premarket` | `api.main:api_premarket_state` | `PremarketScannerAgent` | In-flight cache (30s), disc fallback `data/premarket.json`, timeout 10s -> mock WARMING_UP |
| **Bara de Stare & Diag.**<br>`#premarket-meta` | `UI.renderPremarketScanner`<br>[`frontend/app.js:13487-13499`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L13487-L13499) | `GET /api/premarket` (`payload.inputs_used`) | `PremarketScannerAgent._build_payload` | `time.time()` vs timestamps cache | Calcul vârste: `news_cache_age`, `earnings_cache_age`, `movers_cache_age`. Formatat ca `19s` sau `n/a` |
| **Pre-Market Pulse**<br>`#premarket-pulse` | `UI.renderPremarketScanner`<br>[`frontend/app.js:13502-13555`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L13502-L13555) | `GET /api/premarket` (`payload.pulse`) | `PremarketScannerAgent.get_premarket_pulse` / `_compute_premarket_pulse` | Ollama Cloud / Local LLM (`ollama.generate` cu system prompt JSON) | Fallback pe `None`. UI afișează banner pending: "Pre-Market Pulse was not generated... warming up" |
| **Reported Today Strip**<br>`#premarket-reported-today` | `UI.renderPremarketScanner` & `UI.togglePremarketReported`<br>[`frontend/app.js:13558-13581`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L13558-L13581) | `GET /api/premarket` (`payload.reported_today`) | `PremarketScannerAgent._earnings_already_reported` | `EconomicCalendarAgent` (Finnhub / Yahoo Finance earnings) | Dacă `earnings_cache_age > 86400s`, `earnings_index={}`, niciun ticker nu e eliminat eronat |
| **Summary Strip (KPIs)**<br>`#premarket-summary` | `UI.renderPremarketScanner`<br>[`frontend/app.js:13584-13624`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L13584-L13624) | `GET /api/premarket` (calcul agregat client-side) | `UI._filterPremarketCandidates` | Vector candidați primit | Calcul dinamic: număr total, distribuție L/S/W și media `abs(gap_pct)` |
| **Secțiuni Candidați**<br>`#premarket-sections` | `UI.renderPremarketScanner`<br>[`frontend/app.js:13650-13729`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L13650-L13729) | `GET /api/premarket` (`long_candidates`, `short_candidates`, `watch_only`) | `_derive_premarket_direction`, `_build_direction_groups` | Yahoo Finance Screener (`day_gainers`, `day_losers`, `most_actives`) | `ScannerAgent.cached_hot_list` -> `HOTLIST_CACHE_PATH` -> `scanner.run_scan()` -> `Watchlist` state |
| **Panou Detalii**<br>`#premarket-details` | `UI.renderPremarketDetails`<br>[`frontend/app.js:13124-13282`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L13124-L13282) | `GET /api/premarket` (`candidates[sym]`) | `_build_payload`, `_ollama_enrich_candidate` | Agregat Movers + News + Earnings + Ollama | Text explicativ determinist "Why" dacă Ollama lipsește sau intră în timeout |
| **Buton Refresh Manual**<br>`#premarket-refresh-btn` | `Actions.refreshPremarket()` -> `API.refreshPremarket()`<br>[`frontend/app.js:1137-1148`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L1137-L1148) | `POST /api/premarket/refresh` | `api.main:api_premarket_refresh` | `PremarketScannerAgent.request_refresh()` | Respins cu HTTP 409 dacă agentul e oprit sau dacă piața regulară este DESCHISĂ |

---

### 2.2. De Unde Vin Cotațiile de Sesiune Extinsă? (Alpaca vs eToro vs Yahoo Finance)

În arhitectura Market Signals V9:
- **eToro API**: Nu furnizează date de sesiune extinsă. eToro este mapat doar ca export/sync în modulul de execuție/portofoliu.
- **Alpaca API**: Este configurat în [`core/alpaca_client.py`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/alpaca_client.py) și oferă suport pentru feed IEX (`alpaca_iex`) în [`api/market_data_service.py`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/api/market_data_service.py). Cu toate acestea, **`PremarketScannerAgent` NU folosește `AlpacaClient`**!
- **Yahoo Finance**: Reprezintă **singura sursă activă** pentru cotațiile premarket din Tab 6. 
  Extragerea se realizează în [`agents/scanner_agent.py:688-746`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/scanner_agent.py#L688-L746):
  ```python
  raw_post_price = q.get('postMarketPrice', 0)
  raw_pre_price = q.get('preMarketPrice', 0)
  raw_reg_price = q.get('regularMarketPrice', 0)
  raw_prev_close = q.get('regularMarketPreviousClose', 0) or q.get('previousClose', 0)
  
  # Ierarhie de preferință a prețului extins:
  if post_price > 0:
      price = post_price
      ref_close = reg_price if reg_price > 0 else (prev_close if prev_close > 0 else post_price)
      change_pct = ((price - ref_close) / ref_close * 100.0) if ref_close > 0 else 0.0
  elif pre_price > 0:
      price = pre_price
      ref_close = reg_price if reg_price > 0 else (prev_close if prev_close > 0 else pre_price)
      change_pct = ((price - ref_close) / ref_close * 100.0) if ref_close > 0 else 0.0
  else:
      price = reg_price
      ref_close = prev_close if prev_close > 0 else reg_price
      change_pct = float(q.get('regularMarketChangePercent', 0) or 0.0)
  ```

---

### 2.3. Ce Se Întâmplă Înainte de Ora 04:00 AM ET?

Sesiunea oficială de US Pre-Market începe la ora 04:00 AM Eastern Time.
Conform modulului [`core/market_hours.py:137-165`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/market_hours.py#L137-L165):
```python
def us_equity_market_status(now: Optional[datetime] = None) -> MarketHoursStatus:
    # ...
    is_open = session is TradingSession.REGULAR  # REGULAR este doar 09:30 - 16:00 ET
    return MarketHoursStatus(is_open, ...)
```
Agentul verifică doar `if self._is_us_market_open():` ([`agents/premarket_scanner_agent.py:513`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/premarket_scanner_agent.py#L513)).
1. La ora 02:00 AM ET (sau în weekend), `_is_us_market_open()` returnează `False`.
2. Prin urmare, **`PremarketScannerAgent` NU este oprit**, ci continuă să ruleze ciclul la fiecare 300 de secunde!
3. La ora 02:00 AM ET, Yahoo Finance nu are încă tranzacții de pre-market (`preMarketPrice` este `0.0` sau absent). Totuși, câmpul `postMarketPrice` din sesiunea de after-hours a serii precedente (16:00 - 20:00 ET) este încă stocat în cache-ul Yahoo.
4. **Consecință Criminalistică**: Înainte de 04:00 AM ET, scannerul afișează de fapt **mișcările din sesiunea After-Hours a zilei anterioare**, iar dacă acestea lipsesc, afișează prețurile ultimei sesiuni regulate, calculând un gap artificial sau zero.

---

### 2.4. Cum Se Calculează RVOL Când Lipsește Volumul?

Formula matematică din [`agents/premarket_scanner_agent.py:1143-1146`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/premarket_scanner_agent.py#L1143-L1146) este:
$$\text{RVOL} = \frac{\max(0.0, \text{premarket\_volume})}{\max(1.0, \text{avg\_volume})}$$

Dacă `premarket_volume` este `None` sau `0.0`:
1. `premarket_vol_rel` devine strict `None` (înregistrat cu flag-ul `"premarket_vol_rel_unavailable": true`).
2. Pentru calculul scorului compozit de volum ([`agents/premarket_scanner_agent.py:247-252`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/premarket_scanner_agent.py#L247-L252)):
   ```python
   def _score_volume_activity(premarket_vol_rel: Optional[float]) -> float:
       rel = _safe_float(premarket_vol_rel, None)
       if rel is None:
           return 45.0  # Scor neutru de fallback
       return float(_clamp(25.0 + (_clamp(rel, 0.0, 4.0) * 18.0), 0.0, 100.0))
   ```
3. În UI:
   - Pe **cardul candidatului**: Metrica RVOL **nu apare deloc**.
   - În **Stream view**: Metrica RVOL **nu apare deloc**.
   - În **panoul lateral (#premarket-details)**: Se randează strict `Vol Rel: n/a` ([`frontend/app.js:13245`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/app.js#L13245)).

---

### 2.5. Dovada Testului Live (Python TestClient Probe)

Am executat o probă live a rutei `/api/premarket` folosind FastAPI TestClient (`os.environ['TEST_MODE']='1'`):
```bash
python -c "import os; os.environ['TEST_MODE']='1'; from fastapi.testclient import TestClient; from api.main import app; client = TestClient(app); res = client.get('/api/premarket'); print('Status:', res.status_code); data = res.json(); print('Candidates:', len(data.get('candidates', []))); print('Long:', len(data.get('long_candidates', []))); print('Short:', len(data.get('short_candidates', []))); print('Watch:', len(data.get('watch_only', []))); print('Reported today:', len(data.get('reported_today', []))); print('Flags:', data.get('flags'))"
```
**Rezultat Live**:
```json
{
  "Status": 200,
  "Candidates": 30,
  "Long": 0,
  "Short": 1,
  "Watch": 29,
  "Reported_today": 0,
  "Flags": {
    "news_available": true,
    "news_stale": false,
    "earnings_available": true,
    "earnings_stale": false,
    "ollama_used": false,
    "refreshing": false,
    "reported_today_count": 0,
    "market_open": false,
    "scope": "US_ONLY",
    "pulse_used": false
  }
}
```
**Analiză**:
- 29 din 30 de candidați sunt plasați în `WATCH`! Secțiunea `LONG` are 0 candidați, iar `SHORT` are 1 singur candidat.
- Această aglomerare masivă în `WATCH` este cauzată de pragul dur de direcție ([`agents/premarket_scanner_agent.py:276-285`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/premarket_scanner_agent.py#L276-L285)): necesită `premarket_score_long >= 58.0` și un ecart `diff >= 8.0`. Deoarece volumul este `null` (contribuind cu scor neutru 45) și catalizatorii fără știri dedicate primesc scor neutru 40, scorul compozit nu atinge pragul de 58.

---

## 3. AUDIT UI/UX & ERGONOMIE

### 3.1. Ierarhie Vizuală și Layout
- **Design System Premium**: Tabul adoptă identitatea vizuală cream/gold cu accente sobre (`#c9a227`, fundaluri degrade fine `linear-gradient(165deg, rgba(255, 255, 255, 0.97), rgba(252, 248, 239, 0.92))`), aliniat cu Trade Radar și Pre-Earnings.
- **Sistem de Vizualizare Triplu (Split / Cards / Stream)**:
  - **Split View** (default): Grilă de carduri pe stânga (2 coloane responsive) + panou de detalii sticky (`360px`) pe dreapta.
  - **Cards View**: Secțiuni extinse pe toată lățimea ecranului, ascunzând panoul lateral.
  - **Stream View**: Bandă compactă de o singură linie per candidat, excelentă pentru traderii care scanează rapid cotațiile ("tape reading"). Starea este memorată în `localStorage.getItem('premarket_view_mode')`.
- **Regula de Scroll Capping**: Pentru a preveni listele nesfârșite care deformează pagina, funcția `UI.capScrollList` plafonează afișarea la maximum 5 rânduri de carduri per secțiune, restul devenind scrollabile cu bară de derulare personalizată în nuanțe aurii.

### 3.2. Încărcare Cognitivă & Diagrame de Diagnostic
- **Bara de Stare Suplă (Slim Statusbar)**: În versiunile anterioare, pagina stivuia 5-6 benzi de informare deasupra candidaților. Soluția actuală condensează datele esențiale (oră, număr candidați, număr raportări) într-o singură linie și mută diagnosticele detaliate de cache (vârstă news, vârstă earnings, stare Ollama) într-un meniu derulant nativ `<details class="premarket-diagnostics">`.
- **Claritatea Termenilor Tehnici**: Toate etichetele esențiale (`Score`, `Gap%`, `Premarket`, `Prev Close`, `EPS Surprise`, `Catalyst`) beneficiază de atribute `title` cu explicații în limbaj natural pentru traderii începători (ex: `scoreHelp`, `gapHelp`).

### 3.3. Deficiențe UX Identificate
1. **Omiterea Volumului de pe Card**: Un card de premarket fără volum sau RVOL vizibil pe copertă obligă utilizatorul să facă clic pe fiecare card individual pentru a inspecta lichiditatea în panoul lateral.
2. **Lipsa Suportului pentru Dark Theme**: Fișierul [`frontend/styles_premarket.css`](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/styles_premarket.css) are culori hardcodate pentru fundal deschis (`#ffffff`, `#fffefb`, text `#0f172a`, `#334155`). Dacă utilizatorul activează modul întunecat în aplicație, acest tab rămâne pe fundal alb, provocând oboseală oculară severă.
3. **Discrepanța Nivelurilor Cheie**: Utilizatorul experimentat caută punctele de inflexiune tehnică: Pivots (P, R1, R2, S1, S2), High-ul și Low-ul din premarket (PMH, PML), Maximul și Minimul din ziua precedentă (PDH, PDL). Acestea lipsesc cu desăvârșire.
4. **Blocajul din Timpul Sesiunii Deschise**: Faptul că la 09:30 ET ecranul este curățat complet și afișează "No results" creează confuzie și frustrare, traderul pierzând contextul pregătit dimineața.

---

## 4. BENCHMARK COMPARATIV CU INDUSTRIA

| Criteriu de Evaluare | Market Signals V9 (Tab 6) | Trade-Ideas Premarket | Benzinga Pro Movers | TradingView Screener |
| :--- | :--- | :--- | :--- | :--- |
| **Sursă Cotații Premarket** | Yahoo Finance (scrape/API) | Direct Feed NASDAQ/NYSE (SIP) | Direct Exchange Feed + Cboe | Direct Exchange Feed (BATS/SIP) |
| **Latență Date** | 30s - 300s (cache-first polling) | Real-time ultra-low latency (<50ms) | Real-time (sub-secundar) | Real-time (cu plan Pro) / 15m delay |
| **Calcul RVOL** | Premarket Vol / Avg Daily Vol (frecvent `null`) | RVOL 5m, 15m, 1h, Session vs Historical time-of-day | Relative Volume intraday & premarket | RVOL vs 30-day average volume |
| **Identificare Catalizator** | **FinBERT (Sentiment) + Ollama AI** | Filtru algoritmic pe știri | **Audio Squawk desk + Human-verified News** | Link către feed-uri terțe (Dow Jones/PR) |
| **Niveluri Cheie (Pivots, PMH/PML)** | **Absente** | Automated High/Low alerts, Pivots, VWAP | Premarket High/Low, Support/Resistance lines | Standard Pivot Points, VWAP, Daily High/Low |
| **Disponibilitate după 09:30 ET** | **Wiped out (afișează "No results")** | **Persistă toată ziua** pentru Gap & Go | **Persistă toată ziua** în istoricul de sesiuni | **Persistă** cu filtru Pre-market activat |
| **Sinteză Executivă AI** | **Excelentă (Pre-Market Pulse Card)** | Absentă (doar alerte brute de scanare) | Absentă (doar sumar text de la moderatori) | Absentă (doar grafice și tabele) |
| **Layout & Ergonomie** | **Foarte Bună (Split / Cards / Stream)** | Complexă, interfață Java/Windows densă | Panouri modulare personalizabile | Excelentă, grafic interactiv sincronizat |

---

## 5. SWOT & EVALUARE DE PRODUS

```
┌──────────────────────────────────────────────┬──────────────────────────────────────────────┐
│ PUNCTE FORTE (STRENGTHS)                     │ SLĂBICIUNI (WEAKNESSES)                      │
├──────────────────────────────────────────────┼──────────────────────────────────────────────┤
│ • Pre-Market Pulse inovator (sinteză AI a    │ • Lipsa volumului premarket (RVOL e null     │
│   tonului sesiunii, sentimentului și riscurilor)│   pe 90%+ din tickere din cauza Yahoo).      │
│ • Triplă vizualizare flexibilă (Split/Cards/Stream)│ • Eliminarea greșită a raportărilor AMC de   │
│   cu persistență în localStorage.            │   ieri ("spent catalysts"), care sunt gapping azi│
│ • Punctare compozită multidimensională:      │ • Candidații sunt șterși complet la 09:30 ET │
│   Gap + Catalyst + Liquidity + FinBERT News. │   în loc să fie înghețați pentru trading.    │
│ • Protecție granulară de cache (Singleflight,│ • Absența nivelurilor tehnice cheie: Pivots, │
│   stale memory fallback, 10s async timeout). │   PMH, PML, PDH, PDL.                        │
│ • Diagrame transparente de vârstă a datelor. │ • Hardcodare culori deschise (fără dark mode).│
├──────────────────────────────────────────────┼──────────────────────────────────────────────┤
│ OPORTUNITĂȚI (OPPORTUNITIES)                 │ AMENINȚĂRI (THREATS)                         │
├──────────────────────────────────────────────┼──────────────────────────────────────────────┤
│ • Integrarea feed-ului Alpaca IEX pentru     │ • Rate-limiting agresiv de la Yahoo Finance  │
│   volume reale de sesiune extinsă.           │   (HTTP 429) care poate bloca tot scannerul. │
│ • Adăugarea modului "Frozen Premarket Tape"  │ • Frustrarea utilizatorului care găsește     │
│   activ între 09:30 și 11:00 ET.             │   un ecran complet alb și gol după ora 09:30.│
│ • Calcularea automată a punctelor Camarilla  │ • Halucinații ale modelului LLM dacă știrile │
│   și Classic Floor Pivots în panoul lateral. │   sunt vechi sau incomplete.                 │
│ • Afișarea badge-ului RVOL direct pe card.   │ • Tranzacționarea unor gap-uri fără volum    │
│                                              │   care se dovedesc a fi capcane de lichiditate│
└──────────────────────────────────────────────┴──────────────────────────────────────────────┘
```

---

## 6. FOAIE DE PARCURS PENTRU REMEDIERE ARHITECTURALĂ

### Recomandarea 1: Congelarea Candidaților Premarket după Ora 09:30 ET
*Problema*: La 09:30 ET, `candidates` devine `[]`, afișând un ecran inutilizabil.
*Soluție*: Frontend-ul trebuie să consume `snapshot_candidates` salvat deja de backend și să afișeze o insignă vizuală: `FROZEN PREMARKET SNAPSHOT (Session Open)`. Traderul va putea urmări cum reacționează prețul curent la nivelurile identificate în premarket.

### Recomandarea 2: Corectarea Logicii de Eliminare a Raportărilor Financiare
*Problema*: Raportările AMC de ieri sunt marcate drept "spent catalysts" și ascunse în dropdown.
*Soluție*: Modificarea `_earnings_already_reported` astfel încât raportările din ultimele 24 de ore să rămână în secțiunea dedicată de top gap-uri ("Earnings Gap Movers"), oferind utilizatorului un comutator vizual (filtru) pentru a le include sau exclude.

### Recomandarea 3: Alimentarea cu Volum Premarket Real prin Alpaca IEX
*Problema*: Yahoo Finance omite frecvent `preMarketVolume`.
*Soluție*: În `agents/premarket_scanner_agent.py`, pentru top 30 de candidați descoperiți, să se interogheze endpoint-ul de snapshot Alpaca (`core.alpaca_client.AlpacaClient.get_market_data(ticker)`), extrăgând volumul acumulat din sesiunea extinsă și permițând calcularea unui RVOL precis.

### Recomandarea 4: Calcularea Nivelurilor Cheie (Pivots & Premarket High/Low)
*Problema*: Lipsa nivelurilor esențiale pentru execuție.
*Soluție*: În `_build_payload`, să se calculeze:
- $\text{Pivot} = (H_{prev} + L_{prev} + C_{prev}) / 3$
- $R_1 = 2 \times \text{Pivot} - L_{prev}, \quad S_1 = 2 \times \text{Pivot} - H_{prev}$
- $\text{PMH}$ (Premarket High) și $\text{PML}$ (Premarket Low)
și să fie randate compact în panoul `#premarket-details`.

### Recomandarea 5: Afișarea RVOL pe Card și Suport Dark Mode
*Problema*: Utilizatorul nu știe volumul fără clic; tema întunecată este compromisă.
*Soluție*: Adăugarea unui pill de volum pe card: `<span class="premarket-vol-pill">RVOL: 2.4x</span>` și introducerea variabilelor CSS cu suport pentru `body.theme-dark` în `styles_premarket.css`.
