# AUDIT CRIMINALISTIC V2 — TAB 6: PREMARKET SCANNER (`#view-premarket`)
**Market Signals V9 | Runda 2 (verificare live post-fix) | Data: 2026-09-05, ~20:30-20:40 UTC (piață US ÎNCHISĂ)**

---

## VERDICT GENERAL: **DATĂ SĂNĂTOASE, DAR FIX-UL c7edafc ESTE INERT ÎN PRODUCȚIE (P1)**

Payload-ul live `GET /api/premarket` este coerent cap-coadă: 30 candidați (L0/S2/W28), toate cheile consumate de UI prezente, `as_of_utc` sincer (timpul ultimului ciclu), `flags` fără `frozen_snapshot` (corect — piața e închisă), grupurile reconstruite consisten cu `candidates.direction`, niciun câmp FAKE. **Însă** fix-ul din commit `c7edafc` (distribuția snapshot-ului înghețat în sub-liste + `flags.frozen_snapshot` la deschiderea pieței) NU AJUNGE NICIODATĂ la UI în producție: traseul real de servire este cache endpoint (30s) → fișier disk `data/premarket.json`, iar `get_state()` — singurul loc unde trăiește fix-ul — nu este apelat deloc pe calea GET. Ecranul gol de la 09:30 ET (P1 din Runda 1) rămâne deci nefixat în producție.

- **P1: 1 | P2: 1 | P3: 6**
-Probe live: 4 × GET (doar citire), dumps salvate exclusiv în `$TMP`. Nicio modificare de cod, niciun POST.

---

## 1. TRASEUL REAL DE SERVIRE (descoperire-cheie a rundei 2)

Middleware-ul marchează **orice** GET `/api/*` ca UI-read (`is_ui_read = GET + path`, `api/main.py:768`) și, întrucât `/api/premarket` figurează în `_DEFAULT_UI_CACHE_ONLY_ENDPOINTS` (`api/main.py:178`) și în `_CACHE_GUARD_ENDPOINTS` (`api/main.py:171`), `ctx.cache_only = True` **pentru 100% din GET-uri** (browser, curl, orice client — nu există header/parametru de ocolire; `api/main.py:779-786`).

Consecință în `api_premarket_state` (`api/main.py:7521-7624`):
1. Cache endpoint 30s: HIT → bytes servesc direct (`7525-7538`).
2. MISS → `forced_cache=True` (`7541`) → ramura `if forced_cache and not IS_TEST_MODE` (`7542`) → **fișier disk** `_PREMARKET_DISK_STATE_PATH = DATA_DIR/"premarket.json"` (`196`, încărcat la `7543`), `disabled` suprascris (`7545`), re-cache + serve (`7546-7557`).
3. Blocul de construire în memorie `7566-7621` — `agent.get_state()`, timeout 10s, WARMING_UP, **self-heal** (`agent.start()` + `request_refresh()`, `7587-7607`) — este **cod mort în producție** (rulează doar în `IS_TEST_MODE`).

**Dovadă live că disk-path-ul servește (nu `get_state`):** în 4 probe pe parcursul a ~7 minute și 2 cicluri de agent (20:30:56 → 20:35:52), `status.next_run_in` a rămas **înghețat la 300** deși `last_run_time` îmbătrânise 0-450s. `get_state` ar fi recomputat countdown-ul (`agents/premarket_scanner_agent.py:435-438`); valoarea 300 este exact valoarea de **sfârșit de ciclu** persistedă în fișier (`agent:623` → `_persist_locked` `2057-2064` → același `data/premarket.json`). Valorile din dump-uri (as_of 20:35:52, next_run_in 300, counts 30/40) coincid 1:1 cu fișierul disk citit local. Latency-signature: 0.006s (HIT cache), 0.046s (re-citire disk).

---

## 2. MATRICEA FIELD-BY-FIELD (UI → payload → producător → sursă)

Legenda verdict: **OK** = lanț complet și valorile live consistente; **DEAD-BACKEND** = produs dar netratat de UI; **MISCALIBRATED** = randat dar cu valoare greșită/statică; **n/a onest** = null servit și afișat ca "n/a/—".

### 2.1 Bara de stare `#premarket-meta` (app.js:13574-13597)
| Câmp UI | Payload key | Producător backend | Sursă ultimă | Verdict |
|---|---|---|---|---|
| "As of <ts>" (13564,13586) | `as_of_utc` | `_build_payload` agent:1263 / restaurat din disk | timpul ciclului agent (Yahoo scrape) | **OK** (onest — vârsta reală a datelor; live 102-139s) |
| "<N> candidates" (13587) | `candidates` / `long+short+watch` (13511-13517) | `_build_payload` agent:1275-1278 + `_sanitize_state_payload` agent:2082-2085 | disk `premarket.json` | **OK** (live: 30 = 0+2+28) |
| "next run <N>s" (13565,13588) | `status.next_run_in` | valoare persistată la sfârșit de ciclu (agent:623); recomputarea live `get_state:435-438` nu se apelează | disk | **MISCALIBRATED (P3-1)** — permanent "300s" |
| "<N> reported today" (13589) | `reported_today` | agent:1116-1125 (eliminate_reported) | EconomicCalendarAgent (Finnhub/Yahoo) | **OK** (live 0, secțiune ascunsă onest 13675-13678) |
| "AGENT OFF" (13526,13581,13590) | `disabled` + `status.reason` | disk-path suprascrie `disabled` (api:7545); ciclu agent:559 | config agent | **OK** |
| "PAUSED (market open)" (13525,13582,13591) | `flags.market_open` / `status.reason=market_open_paused` | agent:537-540, persistat | ceas ET (`_is_us_market_open` agent:1948) | **OK** (live: absent, piață închisă) |
| Diag: News/Earnings "yes/no (age)" (13577-13578) | `flags.news_available/earnings_available` + `inputs_used.*_cache_age_seconds` | agent:1266-1269, 1256-1258 | cache news/earnings agents | **OK** (live: yes/1s, yes/61s, movers 27s) |
| Diag: Movers cache age (13579) | `inputs_used.movers_cache_age_seconds` | agent:1259 ← `_load_movers` agent:696-718 | ScannerAgent.cached_hot_list / `HOTLIST_CACHE_PATH` / `run_scan()` | **OK** |
| Diag: Ollama yes/no (13580) | `flags.ollama_used` | agent:604-605 | Ollama cloud/local | **OK** (live: no) |
| Diag: Pulse yes/no (13576) | `flags.pulse_used` | agent:616-617 | LLM | **OK** (live: absent → banner) |

### 2.2 Alertă de sesiune `#premarket-status-alert` (app.js:13529-13553)
| Câmp UI | Payload key | Producător | Verdict |
|---|---|---|---|
| Badge "FROZEN PREMARKET SNAPSHOT (Session Open)" (13531-13540) | `flags.frozen_snapshot` | **doar** în `get_state` (agent:474) — nu este niciodată persistat și disk-path nu apelează `get_state` | **DEAD-RENDER (P1)** — cod frontend corect, flag-ul nu sosește niciodată în producție |
| Text "scanner runs only when US market is CLOSED" (13542) | `status.state=PAUSED` + `flags.market_open`, fără frozen | agent:537, persistat | **OK** (darexact acest text va fi tot ce vede traderul la 09:30) |

### 2.3 Pre-Market Pulse `#premarket-pulse` (app.js:13600-13653)
| Câmp UI | Payload key | Producător | Verdict |
|---|---|---|---|
| model/sentiment/summary (13625-13628) | `pulse.model/.sentiment/.summary` | `_compute_premarket_pulse` agent:1667-1675 (LLM, anti-halucinație 1682-1684) | **OK** |
| Top Long/Short picks (13629-13642) | `pulse.top_long[].{symbol,reason}` / `top_short[]` | agent:1670-1671, `_normalize_pulse_picks` 1687-1701 | **OK** |
| Risks (13643) | `pulse.risks` | agent:1672 | **OK** |
| Banner "not generated" (13646-13648) | `flags.pulse_used === false` | agent:617 | **OK, onest** (live: `pulse=null`, `pulse_used=false` → banner afișat) |

### 2.4 Reported today `#premarket-reported-today` (app.js:13656-13679)
| Câmp UI | Payload key | Producător | Verdict |
|---|---|---|---|
| symbol/session/EPS/Rev surprises/watchlist (13665-13672) | `reported_today[].{symbol,session,eps_surprise_pct,revenue_surprise_pct,is_watchlist}` | agent:1117-1124 ← `_earnings_already_reported` (agent:1412-1439, session-aware BMO≥14:30 UTC / AMC≥22:00 UTC) | **OK** (live 0 itemi; structura validată în cod) |

### 2.5 Sumar `#premarket-summary` (app.js:13682-13722) — calcul client-side
| Câmp | Sursă | Verdict |
|---|---|---|
| "Candidates X of Y" (13697) | `filtered`/`candidates` (agregat din grupuri 13515-13517) | **OK** |
| Long·Short·Watch (13700-13706) | distribuție pe `direction` | **OK** (live 0/2/28) |
| Avg |Gap|% (13707-13713) | media `abs(gap_pct)` | **OK** |
| Reported Today "excluded" (13714-13720) | `reported_today.length` | **OK** |

### 2.6 Secțiuni LONG/SHORT/WATCH `#premarket-sections` (app.js:13748-13821) + Stream (13831-13869)
| Câmp pe card | Payload key | Producător | Sursă ultimă | Verdict |
|---|---|---|---|---|
| Simbol/direcție (13800-13801) | `symbol`,`direction` | agent:1194-1195; direcție `_derive_premarket_direction` agent:270-285 (praguri 58/8) | Yahoo screener | **OK** |
| Gap% pill (13769-13771,13803) | `gap_pct` | agent:1099, recalcul 1150-1155 | Yahoo `preMarketPrice/postMarketPrice` vs `regularMarketPreviousClose` (scanner_agent:689-701) | **OK** |
| Score pill + meter (13805-13807) | `premarket_score_long/_short` (fallback legacy `score`) | agent:1212-1213 (formule 1172-1179) | compozit gap+catalyst+vol+liq | **OK** (live SUPX: SHORT 61) |
| Premkt / PrevClose (13810-13811) | `premarket_price`,`prev_close` | agent:1197-1198 | Yahoo quote | **OK** |
| **"Updated"** (13812) | `data.as_of_utc` (GLOBAL, nu per-ticker) | agent:1263 | — | **MISCALIBRATED (P3-2, restant Runda 1)** — același timestamp pe toate cardurile |
| Catalysts max 2 (13783-13786) | `catalysts[]` filtrate placeholder | agent:1146 `_derive_catalysts` (1441-1460) + Ollama 1524-1529 | regex pe știri + earnings | **OK** (live: [] → rând omis onest) |
| Top news (13787,13815) | `news[0].title/summary` | agent:1104 (max 5) ← `_build_news_index` 1285+ | news agent cache | **OK** (live: 0 știri → omis) |
| Stream: Sym/Dir/Score/Gap/Premkt/PrevClose/Note (13857-13864) | aceleași chei | idem | idem | **OK** |

### 2.7 Panou detali `#premarket-details` (app.js:13205-13363)
| Câmp | Payload key | Producător | Verdict |
|---|---|---|---|
| Symbol/direcție/Gap pill (13288-13293) | `symbol`,`direction`,`gap_pct` | agent:1194-1196 | **OK** |
| **"Score"** (13296-13299, calc 13266) | `item.score` (legacy int) fallback `score_long` | agent:1214-1215 | **MISCALIBRATED (P3-3, restant Runda 1)** — live SUPX: panou "-2" vs card "61" (scări incompatibile) |
| Premarket/PrevClose (13300-13307) | `premarket_price`,`prev_close` | agent:1197-1198 | **OK** |
| Earnings Date (13308-13311) | `earnings.date_formatted || earnings.date` | passthrough `_normalize_earnings_payload` (agent:1375-1409 NU produce `date_formatted`; doar `date` de la sursă) | **OK** (live: tot null → "n/a" onest) |
| EPS/Rev Surprise (13312-13319) | `earnings.eps_surprise_pct`,`revenue_surprise_pct` | agent:1401-1402 (calcul din est vs act) | **OK** (live: null → n/a) |
| News Count (13320-13323) | `news.length` | agent:1104 | **OK** |
| Vol Rel (13324-13327) | `premarket_vol_rel` | agent:1157-1161 (Yahoo `preMarketVolume` frecvent 0) | **OK/n-a onest** (live: null → "n/a"; Rec 3 rămâne) |
| Catalyst / Liq (13328-13331) | `catalyst_score`,`liquidity_score` | agent:1210-1211 | **OK** (live 46/42) |
| Long / Short (13332-13335) | `premarket_score_long/_short` | agent:1212-1213 | **OK** (live 25/61) |
| Freshness (13336-13339) | `freshness` | agent:1063-1067 | **OK** (live "fresh") |
| Inputs age (13341-13343) | `inputs_age.{news,earnings,movers}_cache_age_seconds` | agent:1056-1060 | **OK** |
| Catalysts max 8 (13344-13347) | `catalysts[]` | agent:1146+1524 | **OK** |
| Why (Deterministic) (13348) | `why` | agent:1186-1191 | **OK** (live: "Gap -9.59% \| catalyst 46 \| liq 42") |
| Why Moving + Catalyst/Confidence (13349-13353) | `ollama.{summary,catalyst,confidence}` | `_apply_ollama`/`_ollama_enrich_candidate` agent:1462+ | **OK** (live null → "Rule-based summary active" onest) |
| Top News max 5 (13354-13361) | `news[0..4].{title,source}` | agent:1104 | **OK** |

### 2.8 Markup static `frontend/index.html:1294-1336`
Header + `#premarket-refresh-btn` (1309), `#premarket-agent-status` (1314), `#premarket-status-alert` (1315), `#premarket-meta` (1317), `#premarket-summary` (1320), `#premarket-pulse` (1322), `#premarket-reported-today` (1324), workspace main/aside cu toolbar/sections/details (1326-1336) — toate populată de `renderPremarketScanner`; `Actions.refreshPremarket` → POST `/api/premarket/refresh` (app.js:1137-1151, 16772) — ne-testat live (POST interzis), cod verificat; butonul este dezactivat corect când `disabled` (13555).

---

## 3. DEFECTE (evidenție + fișier:linie)

### P1-1 — Fix-ul frozen snapshot din c7edafc nu funcționează în producție (ecranul de la 09:30 rămâne gol)
- **Lanțul defect:** la deschiderea pieței, ciclul agentului golește listele și **persistă** starea golită pe disk (agent:545-561, `_persist_locked` 561). UI-ul este servit EXCLUSIV din cache-endpoint + fișierul disk (api:7541-7557, cf. §1). Restaura+redistribuirea și `flags.frozen_snapshot` există doar în `get_state()` (agent:456-475), care nu este apelată pe traseul GET (doar în `IS_TEST_MODE`, api:7542). Fișierul disk NU conține niciodată `frozen_snapshot` (sanitize-ul de persistență agent:2073-2090 nu-l adaugă).
- **Efect:** la 09:30 ET UI-ul va afișa secțiuni "No results" + "PAUSED (market open)" + textul "Premarket scanner runs only when US market is CLOSED" (app.js:13542) — exact fail-mode-ul din Runda 1. Badge-ul "FROZEN PREMARKET SNAPSHOT" (app.js:13531-13540) este cod mort pe traseul real.
- **De ce a trecut verificarea hermetică:** `scripts/verify_fixes_2026_09_05.py` a testat `get_state()` direct, nu traseul HTTP de servire (mesajul de commit recunoaște parțial: "the user-visible delta is the frozen_snapshot flag" — dar nici acel delta nu ajunge la UI).
- **Fix corect:** să se persiste distribuția + `flags.frozen_snapshot=True` direct în `self._state` în ramura market-open (agent:529-561, înainte de `_persist_locked`), sau ca disk-path-ul din api:7543-7557 să reconstruiască grupurile din `snapshot_candidates` + flag când listele sunt goale și `flags.market_open=true`.

### P2-1 — Calea de servire în memorie (self-heal, timeout 10s, WARMING_UP) este cod mort pe GET
- api:7566-7621 (inclusiv self-heal `agent.start()`+`request_refresh()` la 7587-7607 și mock-ul WARMING_UP 7581) este inaccesibil: orice GET are `cache_only=True` (§1). Consecințe: (a) dacă thread-ul agentului moare, endpoint-ul servește la nesfârșit ultimul fișier disk, fără rebuild și fără semnal de warming; (b) descrierea din Runda 1 ("timeout 10s -> mock WARMING_UP", failover în memorie) nu corespunde producției; (c) `next_run_in` nu se recompută niciodată pe traseu (P3-1).

### P3-1 — "next run" înghețat la "300s" (MISCALIBRATED)
- Served din disk: valoarea de ciclu (agent:623). Recomputarea `get_state:435-438` nu se apelează. Dovadă live: 4 probe / ~7 min / 2 cicluri → mereu 300. UI (app.js:13565,13588) afișează static "next run 300s".

### P3-2 — "Updated" per card = `as_of_utc` global (restant Runda 1, NErezolvat)
- app.js:13812 folosește `updatedLabel` (13566) = `data.as_of_utc` pentru fiecare card; sugerează prospețime per-ticker inexistență.

### P3-3 — "Score" legacy în panoul de detaliu (restant Runda 1, NErezolvat)
- app.js:13266 afișează `item.score` (int −9..+9, agent:1214) în timp ce cardurile afișează `premarket_score_*` 0-100. Live: SUPX → panou "-2", card "61". Inconsistență de scară pentru același candidat.

### P3-4 — Câmpuri produse de backend, netratate de frontend (DEAD-BACKEND, display-only)
- `flags.news_stale` (agent:1087,1268) și `flags.earnings_stale` (agent:1075,1270) — niciun consumator în app.js (chips-urile diag citesc doar `*_available`, 13577-13578).
- `flags.reported_today_count` (agent:1273) — UI recompută din `reported_today.length`.
- `avg_volume_unavailable` / `premarket_volume_unavailable` / `premarket_vol_rel_unavailable` (agent:1207-1209) — necitite; UI afișează doar "n/a".
- `pulse.generated_at` (agent:1674) — neafișat; `score_short` (agent:1216) — fără consumator (doar `score_long` la 13266).

### P3-5 — `snapshot_candidates` (40) > `candidates` (30) — cap inconsistență
- `_sanitize_state_payload` plafonează doar `candidates` la `max_results` (agent:2080-2081), nu și snapshot-ul; la restaurare (în ipoteza reparării P1-1), 10 itemi din snapshot ar fi tăiau silențios de sanitizarea finală (agent:478 → 2081). Live: snap=40 vs cand=30.

### P3-6 — Stări restante din Runda 1, neschimbate
- RVOL absent de pe carduri și din Stream (doar "Vol Rel: n/a" în panou, 13324-13327); dark-mode inexistent în `frontend/styles_premarket.css` (culori hardcodate deschis); clustering WATCH (live 28/30) cauzat de pragurile 58/8 (agent:276-284) + volum null → scor neutru 45 (`_score_volume_activity`).

**Fals dispus:** nu am găsit niciun câmp FAKE; toate valorile "gol" sunt afișate onest ca "n/a/—/no results". Nu există BACKEND mort în sensul "rute moarte" — doar flag-urile display-only de la P3-4.

---

## 4. STARE vs RUNDA 1 (re-verificare item cu item)

| Item Runda 1 | Stare în Runda 2 |
|---|---|
| P1: ecran gol la 09:30 ET; snapshot netratat | **ÎNCĂ DESCHIS ÎN PRODUCȚIE** — c7edafc a adăugat restaura în `get_state` + flag + consumator frontend (13511-13517, 13527-13538), dar traseul real de servire (cache 30s → disk) ocolește `get_state` (§1, P1-1). Verificat hermetic da, live nu. |
| Sursa exclusivă Yahoo Finance (fără Alpaca/eToro) | **CONFIRMAT** — `agents/scanner_agent.py:664-671,689-722,756,771-799`; PremarketScannerAgent nu folosește AlpacaClient. |
| RVOL null pe majoritatea candidaților | **CONFIRMAT LIVE** — SUPX: `premarket_volume=null`, `premarket_vol_rel=null`, `premarket_vol_rel_unavailable=true`. |
| eliminate_reported mută AMC/BMO în dropdown | **CONFIRMAT** (agent:1116-1125); nuanță: logica e session-aware (BMO considerat raportat doar ≥14:30 UTC, AMC ≥22:00 UTC, date viitoare/fără dată păstrate — agent:1412-1439), deci mai prudentă decât descria Runda 1; AMC de ieri (dată trecută) rămâne eliminat. |
| Failover "30s cache → disk → timeout 10s → WARMING_UP" | **CORECTAT**: traseul real este 30s cache → disk; blocul de memorie (timeout/self-heal/WARMING_UP) e cod mort pe GET (P2-1). |
| Pre-04:00 ET afișează after-hours de ieri (postMarketPrice) | **COD NESCHIMBAT** (scanner_agent:689-701) — comportamentul rămâne; netestabil live acum (20:30 UTC = 16:30 ET, post-close). |
| 29/30 candidați aglomerați în WATCH | **CONFIRMAT LIVE**: 28/30 WATCH (praguri neschimbate agent:276-284). |
| P3: legacy "Score" în detaliu | **ÎNCĂ DESCHIS** (P3-3). |
| P3: "Updated" = as_of global | **ÎNCĂ DESCHIS** (P3-2). |
| lipsă pivots/PMH/PML/PDH/PDL | **NESHIMBAT** — nu există în cod; rămâne Rec 4 (Runda 1). |

### Verificarea commit-ului c7edafc (git)
- `git show c7edafc --stat`: 5 fișiere; `agents/premarket_scanner_agent.py` +16 linii — exact blocul 456-475 (distribuție + flag). `git log --oneline -10`: HEAD `1eedcfc`, working tree curat (serverul rulează codul comisat).
- Comportament: distribuția e corectă și idempotentă, DAR redundantă cu sanitizarea finală din `get_state` (agent:478 → 2082-2085, care reconstruiește oricum grupurile din `candidates` restaurat — recunoscut și în mesajul de commit). Delta real pretins = `flags.frozen_snapshot`. **Ambele efecte sunt însă inerte pe traseul de servire live (P1-1).** Nicio regresie introdusă de commit pe traseul testabil (piață închisă): payload-ul e sănătos, `frozen_snapshot` absent corect.

---

## 5. RECOMANDĂRI (reevaluare)

1. **Recomandarea 1 (înghețare după 09:30) — ÎNCA VALIDĂ, DAR RELOCALIZATĂ.** Fix-ul la nivel de `get_state` nu e suficient: trebuie mutat în **stratul de persistență** (ramura market-open agent:529-561 să scrie în `self._state` listele distribuite + `flags.frozen_snapshot=True` înainte de `_persist_locked`) SAU în disk-path-ul API (api:7543-7557: reconstrucție din `snapshot_candidates` + flag când market_open și listele goale). Alternativ minim-invaziv: ca disk-path-ul să delege spre `get_state` în loc de citire brută. Prioritate: P1.
2. **Recomandarea 2 (logică AMC/BMO) — VALIDĂ, cu nuanță.** Eliminarea rămâne by default (`eliminate_reported=True`, agent:312); propunerea din Runda 1 (comutator UI + secțiune "Earnings Gap Movers") rămâne relevantă; logica session-aware actuală (agent:1412-1439) e deja mai sigură decât evaluat inițial.
3. **Recomandarea 3 (volum premarket Alpaca IEX) — VALIDĂ.** Volumul live e null pe candidați; scorul de volum folosește neutru 45; integrarea Alpaca IEX pentru top-N candidați ar debloca RVOL real și arreduce clustering-ul WATCH.
4. **NOU — Reparația traseului de servire (P2-1):** să se elimine discrepanța dintre middleware (`cache_only` pentru toate GET-urile) și intenția codului din `api_premarket_state`; minim: în disk-path, să se aplice aceeași normalizare ca `get_state` (recomputare `next_run_in`, restaurare frozen, rollback la build+self-heal când disk-ul e mai vechi de N cicluri).
5. **NOU — curățenie P3:** consumarea `news_stale`/`earnings_stale` în chips-urile de diagnostic; scalar "Score" din panou la `premarket_score_*`; eticheta "Updated" per card redenumită "As of" sau alimentată per-ticker; cap pentru `snapshot_candidates`.

---
*Metodă: citire cod (grep, fără lectură liniară) + 4 probe GET live (`$TMP` dumps) + `git show c7edafc` / `git log`. Fără POST, fără browser, fără URL-uri externe. Cod nemodificat.*
