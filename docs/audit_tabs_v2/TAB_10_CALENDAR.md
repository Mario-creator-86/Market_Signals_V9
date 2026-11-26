# AUDIT CRIMINALISTIC V2 — TAB 10: MARKET CALENDAR (`#view-calendar`)

**Data auditului:** 2026-09-05
**Metodă:** trasare câmp-cu-câmp UI → payload → endpoint → producător backend → sursă finală; verificare live GET-only pe `http://127.0.0.1:8003` (4 sonde: `/api/calendar/range`, `/api/calendar/events?days=30`, `/api/calendar/today`, `/api/calendar/state`; toate HTTP 200). Dump-uri salvate doar în temp sistem, repo neatins.
**Referință round 1:** `docs/audit_tabs/TAB_10_CALENDAR.md` (2026-09-04)

---

## VERDICT GENERAL

**TAB FUNCȚIONAL ȘI CINSTIT CU DATELE, DAR CU STRAT SEMNIFICATIV DE CÂMPURI MOARTE.** Fiecare element randat în grilă are un producător backend real și o sursă identificabilă (OMB/OIRA local, FOMC hardcodat verificat, FinnHub bulk / Nasdaq fallback, yfinance watchlist). Nu există date fabricate (FAKE = 0). Totuși: **0 P1, 4 P2, 12 P3**. Defectele P2 sunt: modalul economic afișează 4/5 casete mereu `—` (nimeni nu produce actual/forecast/previous/currency), bannerul FOMC din modalul de zi este mort (nu primește niciodată `next_fed`), coloana de oră din modalul de zi afișează ore UTC/neetichetate (FOMC = `00:00`, earnings `+00:00` = `02:00`/`03:00` în București), iar un ticker din watchlist (NVDA) lipsește complet din grilă deși `watchlist_tickers` îl conține.

Scor integritate reverse data-flow: **8.5/10** (toate căile vii trasate cap-coadă; scădere pentru lanțul de cod mort openCalendar → /events → renderCalendar și pentru câmpurile fără producător).

---

## 1. STATUS FIX-URI ȘI P3-URI DIN ROUND 1 (re-verificat)

| Item round 1 | Status | Evidence |
|---|---|---|
| Glif watchlist `? ` în loc de `★` | **REZOLVAT** | `frontend/app.js:21357` folosește `'\u2605 '`; landat în commit `7f26fd2` (git log -S "u2605") |
| "Time TBA" pentru sesiuni necunoscute | **REZOLVAT** | `frontend/app.js:19948-19952` — `bmo`→Pre-Market, `amc`→After-Hours, altfel→"Time TBA"; live: 232 rânduri `when='unknown'` + 12 `when=None` → toate TBA (47%, comentariul din cod zice 46% — corect) |
| TTL 6h pe cache-urile localStorage | **REZOLVAT** | `frontend/app.js:19647` `CALENDAR_CACHE_TTL_MS = 6*60*60*1000`; prune la 19654-19661 (aruncă și intrările legacy fără timestamp); ts-map `calendar_cache_ts_v1` la 19679-19686 |
| Evidențiere watchlist nu putea apărea (is_watchlist) | **REZOLVAT** | `api/main.py:7078-7080` anotează toate rândurile earnings; live: 13/518 rânduri cu `is_watchlist=true` (BRZE, HUT, MSFT, TSLA, TEAM, DOCS, STRL, CLSK, AAPL, MARA, SPCX, WULF, BMNR) |
| `high_impact` calculat înainte de merge (P3) | **ÎNCĂ DESCHIS** | `api/main.py:6919` calculează lista ÎNAINTE de merge-ul macro (6937); live: `high_impact` conține doar `fomc_20260916`, deși CPI/PCE/Retail (high) sunt în `upcoming.economic`. Dublu-mort: frontend NU randează niciodată `high_impact` (variabilă moartă, app.js:19773/19784/19790/19800) |
| Cod mort `openCalendar` (P3) | **ÎNCĂ DESCHIS** | Două definiții: literal la `app.js:5789` (apelează `/api/us-market-news/today` + `UI.renderCalendar` — și el mort, 5580) și override la `app.js:19712`; niciun apelant în UI (doar butonul Retry din interiorul său, 19750). `index.html` nu îl referențiază deloc |
| `/api/calendar/state` endpoint mort (P3) | **ÎNCĂ DESCHIS** | `api/main.py:7316` răspunde 200 (97 KB live), dar frontend nu-l apelează niciodată; singura referință e o intrare de TTL în `frontend/data_store.js:17` (config mort) |

Fix-urile au aterizat în commits `7f26fd2` (fix-uri audit 13 tab-uri) și `200cd2e` (audit round 2 — includes is_watchlist annotation). **Nicio regresie** introdusă de acestea: sondele live returnează payload-uri coerente, iar grila calculează corect limitele (Aug 30 2026 → Dec 5 2026 pentru Sep-Nov).

---

## 2. TABEL CÂMP-CU-CÂMP: CE RANDEAZĂ UI ← DE UNDE VINE

Legendă verdict: OK / DEAD-RENDER / DEAD-BACKEND / DEAD-CODE / FAKE / STALE / MISCALIBRATED.

| # | Câmp randat în UI | Frontend (fișier:linie) | Cheie payload | Endpoint | Producător backend | Sursă finală | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | Titlu header `September - November 2026` (`#cal-current-month`) | app.js:21188-21200 | — (aritmetică locală) | — | — | Ceasul browserului | OK |
| 2 | Butoane `cal-prev-btn` / `cal-next-btn` (±3 luni) | index.html:1642-1647 → app.js:21510-21520 | — | — | — | — | MISCALIBRATED (P3-9: `aria-label="Previous month"` dar sare 3 luni) |
| 3 | Buton `Today` | index.html:1649-1650 → app.js:21522-21525 | — | — | — | — | OK |
| 4 | Legendă Economic/Earnings/IPO (puncte colorate) | index.html:1653-1661; styles_calendar.css:188,728 | — | — | — | — | P3-7: statică, nu filtrează; dot-ul IPO e perpetuu gol (live `ipo: []`) |
| 5 | Grid 3 luni, antete Sun-Sat, `.other-month`, `.today` | app.js:21293-21374 (getGridBounds 21212-21223) | — | — | — | — | OK |
| 6 | Pastilă eveniment: label `symbol \|\| title` (trunchiat 20 ch) | app.js:21349-21354 | `symbol` / `title` | `/api/calendar/range` (app.js:21256) | earnings: `EarningsEvent.to_dict` (agent:178-208) via `_get_cached_earnings_range` (agent:938-990) ← `_do_bulk_earnings_fetch` (agent:753-777) ← FinnHub `/calendar/earnings` sau **fallback Nasdaq** `_fetch_nasdaq_earnings_calendar` (agent:1143-1255, `api.nasdaq.com/api/calendar/earnings`); macro: `_merge_macro_calendar_events` (main.py:6834-6888) | FinnHub/Nasdaq; OMB/OIRA `data/us_macro_calendar_2026.json` (census.gov/economic-indicators, PCE cross-check bea.gov) | OK |
| 7 | Steaua ★ watchlist + clasa `watchlist-highlight` | app.js:21356-21358; css 728-740 | `is_watchlist` | `/api/calendar/range` | main.py:7078-7080 ← `watchlist.json` (stocks+hot_list, main.py:6986-6993) | watchlist.json | OK |
| 8 | Clasa `high-impact` pe pastilă | app.js:21351; css:188 | `impact` | `/api/calendar/range` | macro json `impact` (real) + earnings `impact="high"` **hardcodat** (agent:197) | — | P3-11: toate 518 earnings "high" → sortarea `getPrio` (app.js:21329-21335) pune TOATE earnings (prio 5) deasupra macro-urilor medii (PPI/GDP, prio 3), contrar intenției documentate în comentariu |
| 9 | Grupare pe zile (`e.date.split('T')[0]`) | app.js:21272 | `date` | `/api/calendar/range` | agent/main.py (ISO `T00:00:00`, `T00:00:00+00:00`, `T13:30:00`) | — | OK pentru payload-ul actual (toate naivul-miezul-noapte/13:30 rămân în aceeași zi locală; comentariu corect main.py:6871-6872) |
| 10 | `+N more` → modal de zi | app.js:21361 → `openDayModal` 21752-21791 | — | — | — | — | OK |
| 11 | Titlu modal de zi (`miercuri, 16 septembrie...`) | app.js:21787-21788 | — | — | — | — | P3-8: `ro-RO` într-o aplicație en-US (round 1, încă deschis) |
| 12 | **Banner FOMC "Next Fed Meeting"** din modal | app.js:19827-19844 | `next_fed` | — | agent `get_next_fed_meeting` (agent:818-835, FOMC_SCHEDULE hardcodat 331-348) | federalreserve.gov (eticheta "verified 2026-07-14") | **DEAD-RENDER (P2-2)**: singurul apelant viu al `renderCalendar` este `openDayModal` (21777), care trimite `miniData` FĂRĂ `next_fed` → bannerul nu se randează niciodată. `next_fed` e produs doar de `/api/calendar/events` (main.py:6920) — endpoint consumat doar de codul mort `openCalendar` |
| 13 | Filtre All/Economic/Earnings/IPO în modal | app.js:19806-19821 | — | — | — | — | OK (funcționale pe miniData); fila IPO perpetuu goală (P3-7) |
| 14 | Coloana oră în modalul de zi | app.js:19912 `new Date(event.date).toLocaleTimeString('ro-RO')` | `date` | — | main.py:6876 (`T13:30:00` naiv = UTC), agent (FOMC `T00:00:00`, earnings `T00:00:00+00:00`) | — | **MISCALIBRATED (P2-3)**: ore UTC afișate ca ore locale; FOMC afișează `00:00`; earnings cu sufix `+00:00` afișează `02:00`/`03:00` (București) pe când cele naivе afișează `00:00` — mixt în aceeași zi. `date_formatted` („2026-09-11 08:30 ET" — singura etichetă corectă) NU este randată nicăieri în UI |
| 15 | Țara (`US`) | app.js:19972 `event.country \|\| 'US'` | `country` | — | main.py:6880 | — | OK (default onest pentru earnings, care nu au cheia) |
| 16 | Est/Prev/Act pentru economic (modal de zi) | app.js:19941-19943 | `forecast`/`previous`/`actual` | `/api/calendar/range` | main.py:6873-6885 (le lasă `None`) | json OMB conține doar date, nu valori | DEAD-RENDER (ascunse condiționat — onest, dar mereu absente; vezi P2-1) |
| 17 | Pre-Market / After-Hours / **Time TBA** | app.js:19948-19952 | `when` | `/api/calendar/range` | FinnHub (`when`) / Nasdaq `time-pre-market`→`bmo`, `time-after-hours`→`amc`, altfel `unknown` (agent:1232-1236); rândurile yfinance (main.py:7051-7059) nu au cheie → `None` | FinnHub/Nasdaq/yfinance | OK — FIX CONFIRMAT |
| 18 | EPS Est / Rev (modal de zi) | app.js:19956-19957 | `eps_estimated`, `revenue_estimated` | `/api/calendar/range` | FinnHub `epsForecast` / Nasdaq `epsForecast` (agent:1240); yfinance rows fără aceste chei | FinnHub/Nasdaq | Parțial OK: live `eps_estimated` pe 85/518, `revenue_estimated` pe **0/518** → "Rev: $xB" nu apare niciodată |
| 19 | Modal eveniment — EPS Estimate / EPS Actual / Revenue Est / Revenue Act | app.js:21417-21438 | `eps_estimated`, `eps_actual`, `revenue_estimated`, `revenue_actual` | `/api/calendar/range` | **Nimeni nu produce actuals** (live: 0/518 pentru toate 3 câmpurile actual/revenue) | — | DEAD-RENDER (P2-1): chiar și pentru cele **185 rânduri cu data în trecut** (Sep 1-4) actuals lipsesc; "Market Timing" `when.toUpperCase()` OK (21436, None→'N/A') |
| 20 | Modal economic — Actual / Forecast / Previous | app.js:21443-21453 | `actual`, `estimate \|\| forecast`, `previous` | `/api/calendar/range` | `EconomicEvent.to_dict` (agent:123-156) — valorile rămân None; singurul producător ar fi FinnHub economic (`_fetch_finnhub_calendar`, agent:1032), neconfigurat; FMP eliminat (agent:1303) | — | **DEAD-BACKEND (P2-1)**: 3/5 casete mereu `—` |
| 21 | Modal economic — Impact | app.js:21455-21457 | `impact` | — | macro json (`high`/`medium`) | OMB/OIRA | OK |
| 22 | Modal economic — Currency / Unit | app.js:21459-21462 | `currency` | — | **nu există în payload** (chei live confirmate: actual, category, country, date, date_formatted, description, forecast, id, impact, is_upcoming, previous, source_name, source_url, time_precision, title) | — | DEAD-RENDER (inclus în P2-1) |
| 23 | Subsol dată modal eveniment (`en-US`) | app.js:21473 | `date` | — | — | — | OK |
| 24 | Proveniență (link + sursă) | app.js:21400-21414 (guard protocol http/https) | `source_url`, `source_name`, `time_precision` | — | FOMC: federalreserve.gov (agent:329-330, 850-851); macro: main.py:6881-6882 | json OMB are `source` dar **nu are `source_url`** → macro primește `source_url=""` → text fără link (P3-10) | OK / P3-10 |
| 25 | Nota "Date only — no release time was assumed" pentru FOMC | app.js:21413 | `time_precision='date_only'` | — | agent:852 | — | OK (onest) — dar contrast: modalul de zi tot afișează `00:00` pentru FOMC (P2-3) |
| 26 | Cache localStorage `calendar_range_cache_v1` + `calendar_modal_cache_v1` | app.js:19640-19696, citire 21247-21263 | — | — | — | — | OK — TTL 6h CONFIRMAT; reziduuri: P3-5 (ts-map nu e curățat de `clearCalendarCache`, prune o dată per page-load) |
| 27 | Invalidează cache la schimbarea watchlist-ului | app.js:21231-21237, watchlistLoop 20786-20800; `getWatchlistSignature` 19623-19637 | — | `/api/watchlist` | — | — | P3-6: semnătura folosește `wl.order` (dacă există) și ignore `hot_list`, în timp ce backendul calendarului folosește stocks+hot_list (main.py:6991-6993) → o modificare doar în hot_list nu invalidează cache-ul |
| 28 | IPO grid | app.js:21283 | `ipo` | `/api/calendar/range` | `_get_cached_ipo_range` cache-only (agent:992-1009); FMP deprecated (agent:1303) | cache istoric (gol) | DEAD-BACKEND (onest: `[]` live, fără inventat) |
| 29 | `treasury_rates` în payload | — (frontend nu-l citește) | `treasury_rates` | `/api/calendar/range` | agent:701 (`_treasury_cache`, gol) | — | DEAD-BACKEND (câmp payload mort) |
| 30 | `fed_schedule` în payload | — (destrucuturat la 19773/19782/19789, niciodată randat) | `fed_schedule` | `/api/calendar/events` | agent `get_fed_schedule` (798-816) | FOMC_SCHEDULE | DEAD-RENDER (P3-4) |
| 31 | `high_impact` în payload | — (moartă în renderCalendar, 19773-19800) | `high_impact` | `/api/calendar/events` | main.py:6919 (înainte de merge!) | — | P3-3 (round 1, încă deschis; dublu-mort) |
| 32 | `openCalendar` (ambele definiții) + `UI.renderCalendar` (5580) | app.js:5789-5800, 19712-19752 | — | `/api/calendar/events?days=30` | — | — | DEAD-CODE (P3-1, round 1 încă deschis) |
| 33 | `/api/calendar/state` | — (doar TTL în data_store.js:17) | — | `api/main.py:7316` | agent `get_state` (522-607) | — | DEAD-BACKEND (P3-2, round 1 încă deschis); **`/api/calendar/today` (main.py:7101) la fel — zero consumatori UI** |
| 34 | Merge macro în `/api/calendar/events` | — | — | main.py:6925-6939 | — | — | P3-2b: comentariul spune „Calendar modal reads /events" — fals; modalul de zi își construiește datele din grilă (openDayModal), deci merge-ul deservește un endpoint pe care UI nu-l mai cheamă |

### Proba live (2026-09-05, `127.0.0.1:8003`, intervalul grilei 2026-08-30 → 2026-12-05)
- `/api/calendar/range`: **economic 21** (17 macro OMB + 3 FOMC + 1 jobs din luna trecută), **earnings 518**, **ipo 0**, watchlist_tickers **14**, `disabled:false`; 200 OK, 276 KB, 0.10s.
- `/api/calendar/events?days=30`: `upcoming{economic:7, earnings:299, ipo:0}`, `fed_schedule:12`, `high_impact:1` (doar FOMC), `next_fed` = fomc_20260916 cu `days_until`.
- `/api/calendar/today`: `{"events":[],"disabled":false}` (sâmbătă — corect, onest).
- `/api/calendar/state`: 200, 97 KB, chei `as_of/cache_meta/days/earnings/economic/ipo/disabled` — endpoint viu tehnic, mort comercial.
- Distribuție `when` (518): bmo 129 / amc 145 / unknown 232 / None 12. `source`: nasdaq_zacks_estimate 241, yfinance+calendar 256, yfinance 9, None 12. `date_confidence`: estimated 241, high 255, medium 10, None 12. Mix de formate dată: 253 naive `T00:00:00` + 265 `T00:00:00+00:00`.

---

## 3. DEFECTE — IERARHIZARE CU EVIDENȚĂ

### P1 (critic)
**Nimic.** Nu există date fabricate, nu există crăpături de tip payload-cheie-lipsă care să strice grila, nu există erori 5xx.

### P2 (semnificativ)

- **P2-1 — Modalul evenimentului economic e o carcasă de câmpuri moarte; modalul earnings afișează actuals care nu vin niciodată.**
  `app.js:21443-21462` randează Actual/Forecast/Previous/Currency pentru economic — toate `—` pentru totdeauna (producător inexistent: FinnHub economic neconfigurat, FMP eliminat; json-ul OMB nu conține valori). `app.js:21419-21433` randează EPS Actual / Revenue Est / Revenue Act — live **0/518** pentru oricare dintre `eps_actual`, `revenue_estimated`, `revenue_actual`, inclusiv pentru cele **185 rânduri datate în trecut** (1-4 Sep). Verdict: DEAD-RENDER/DEAD-BACKEND. Impact UX: utilizatorul vede 4 casete goale din 5 la fiecare deschidere de eveniment.
- **P2-2 — Bannerul FOMC din modalul de zi nu se poate randează niciodată.**
  `renderCalendar` (app.js:19827-19844) îl condiționează de `next_fed`; singurul apelător viu, `openDayModal` (app.js:21777), trimite `miniData` construit doar din `economic/earnings/ipo/watchlist_tickers` (21766-21771) — fără `next_fed`. `next_fed` există doar în `/api/calendar/events` (main.py:6920), endpoint apelat exclusiv de `openCalendar` (cod mort). Verdict: DEAD-RENDER.
- **P2-3 — Afișarea orelor din modalul de zi este miscalibrată și neetichetată.**
  `app.js:19912` convertește `event.date` la ora locală fără etichetă de fus: evenimentele macro au `T13:30:00` naiv (= UTC, comentariu main.py:6871-6872) → afișează „13:30" deși în București eliberarea e la 16:30; FOMC `T00:00:00` → afișează „00:00" deși `time_precision=date_only` (nu există oră publicată!); 265 rânduri earnings cu `+00:00` afișează „02:00"/„03:00", în timp ce cele 253 naive afișează „00:00" — mixt în aceeași listă. Singura valoare corectă („08:30 ET" din `date_formatted`, main.py:6877) nu este randată nicăieri. Verdict: MISCALIBRATED.
- **P2-4 — NVDA (watchlist) lipsește complet din grila de 3 luni.**
  Live: `watchlist_tickers` include NVDA, dar zero rânduri earnings NVDA în 2026-08-30→2026-12-05; `is_watchlist` pe 13/14. Enrichment-ul yfinance per-ticker (main.py:7039-7060) nu a produs dată pentru NVDA, iar bulk-ul FinnHub/Nasdaq nici el. Pentru un utilizator care ține NVDA în watchlist special pentru raportare, grila nu-i arată deloc data (tickers la fel de mari precum MSFT/TSLA/AAPL apar). Verdict: STALE/GAP (date-completeness).

### P3 (cosmetic / igienă)

- **P3-1** Cod mort `openCalendar` ×2 (app.js:5789-5800 versiunea care greșește endpoint-ul `/api/us-market-news/today`; 19712-19752 override) + `UI.renderCalendar` (5580) — round 1, deschis.
- **P3-2** Endpoint-uri fără consumator UI: `/api/calendar/state` (main.py:7316), `/api/calendar/today` (main.py:7101), `/api/calendar/events` (doar codul mort îl apelează; inclusiv merge-ul macro dedicat, main.py:6925-6939, cu comentariu acum incorect) + intrare de TTL moartă în data_store.js:17.
- **P3-3** `high_impact` calculat înainte de merge (main.py:6919; live: doar FOMC, CPI/PCE/Retail excluse) și neconsumat de frontend (app.js:19773-19800) — dublu-mort; round 1, deschis.
- **P3-4** `fed_schedule` în payload, niciodată randat (destrucuturat app.js:19773/19782/19789).
- **P3-5** `clearCalendarCache` (app.js:19698-19710) nu șterge `calendar_cache_ts_v1` → creștere nelegată a ts-map; plus prune-ul rulează o singură dată per page-load (o sesiune >6h servește intrări expirate).
- **P3-6** `getWatchlistSignature` (app.js:19623-19637) preferă `wl.order` și exclude `hot_list`; backendul calendarului folosește stocks+hot_list (main.py:6991-6993) → adăugări doar în hot_list nu invalidează cache-ul de grilă.
- **P3-7** Legenda IPO + fila de filtrare IPO perpetuu goale (`ipo: []` live; sursă FMP dezactivată, agent:1303, 992-1009).
- **P3-8** Inconsistență de locale: titlu modal de zi `ro-RO` (app.js:21788), ore `ro-RO` (19912), zi `en-US` în modalul eveniment (21473) — round 1, deschis.
- **P3-9** `aria-label="Previous month"` dar saltul e de 3 luni (index.html:1642, app.js:21516-21519).
- **P3-10** `us_macro_calendar_2026.json` nu are cheie `source_url` → main.py:6882 pune `""` → proveniența macro e text simplu, fără link (round 1 promitea linkuri OMB).
- **P3-11** `impact="high"` hardcodat pe toate earnings (agent:197) aplatizează sortarea pe priorități (app.js:21329-21335): macro-urile medii (PPI/GDP) pierd în fața oricărui earnings în celulele cu MAX_SHOWN=4.
- **P3-12** Fișiere reziduale `data/calendar_cache_*.tmp` (3 buc.) — igienă.

---

## 4. LANȚUL DE DATE (sinteză verificată)

```
Grilă 3 luni (renderFullCalendar, app.js:21179)
  → GET /api/calendar/range?start&end (main.py:6957)
      → watchlist.json stocks+hot_list (main.py:6986-6993)
      → EconomicCalendarAgent.get_calendar_events_range (agent:658-707)
          economic: cache memorie+disc (calendar_events_cache.json, 1448 simboluri) + FOMC hardcodat (agent:331-348, federalreserve.gov)
          earnings: FinnHub /calendar/earnings bulk (TTL 1h) → fallback Nasdaq api.nasdaq.com ThreadPool (agent:1143-1255, rows "estimated", confirmed=False)
          ipo: cache-only (FMP dezactivat) → []
      → enrich yfinance watchlist (main.py:7034-7060, api/state_service._resolve_earnings_from_yfinance)
      → merge OMB/OIRA data/us_macro_calendar_2026.json (main.py:6834-6888)
      → annotare is_watchlist (main.py:7078-7080)
  → localStorage calendar_range_cache_v1, TTL 6h (app.js:19640-19696)
Modal de zi (openDayModal → renderCalendar) ← aceleași date din grilă, fără fetch nou
Modal eveniment (openEventModal) ← Actions.calendarEventsCache (app.js:21202, 21277)
```

Deduplicarea earnings: `_select_better_earnings_row` (agent:1647-1659) cu scor de calitate (confirmed +5, session_confidence official/provider +2 etc.) pe cheie `(simbol, dată)` — verificat, corect.

---

## 5. STATUS REGRESII vs ROUND 1 (git)

- `git log --oneline -10`: `1eedcfc` (docs audit), `218009f`, `c7edafc` (fix-uri audit ne-commituite), `e9eda27`, `2a3ebce`, `e04cf46`, `7f26fd2` (fix-uri 13 tab-uri — **include TTL 6h, Time TBA, glif ★**), `80180a2`, `2acbdd9`, `8bf97a2`; pe căile calendarului: `200cd2e` (is_watchlist annotation).
- **Nicio regresie detectată**: toate sondele live 200; payload-urile coerente; fix-urile round 1 prezente în HEAD și confirmate în payload live.

---

## 6. RECOMANDĂRI (prioritizat)

1. **P2-1**: Populează actual/forecast/previous/currency pentru economic (cheie FinnHub economic sau stub onest „values not provided by schedule source") și ascunde casetele fără producător; pentru earnings, tragere actuals din cache-ul Pre-Earnings pentru rândurile din trecut.
2. **P2-3**: Randarea orelor: folosește `date_formatted` („08:30 ET") sau etichetează explicit fusul; pentru `time_precision=date_only` afișează „All day / TBA" în loc de `00:00`; normalizează `+00:00` → naiv la producer.
3. **P2-2**: Trimite `next_fed` + `fed_schedule` în `miniData` (din cache-ul range sau un apel `/api/calendar/events`) sau scoate bannerul mort.
4. **P2-4**: Diagnosticare resolver pentru NVDA (și alertă generică: orice ticker din watchlist fără rând earnings în range ar trebui logată).
5. P3: șterge `openCalendar`×2 + `UI.renderCalendar` + `/api/calendar/state` + `/api/calendar/today` (sau documentează consumatorii externi), curăță `calendar_cache_ts_v1` în `clearCalendarCache`, aliniază `getWatchlistSignature` la stocks+hot_list, adaugă `source_url` în us_macro_calendar_2026.json, unify locale la en-US, corectează `aria-label`, curăță `data/calendar_cache_*.tmp`.
6. Anual: reînnoire `us_macro_calendar_2026.json` (nota din fișier) — următoarea fereastră: decembrie 2026.
