# AUDIT FORENSIC V2 — TAB 4 „NEWS" (MARKET NEWSROOM, `#view-news`)
**Data:** 2026-09-05
**Metodă:** re-audit campo-cu-câmp (UI → payload → endpoint → producător backend → sursă externă), cod + probe HTTP GET live pe `http://127.0.0.1:8003` (doar GET, dumps în `$TMP`), diff git față de runda 1 (8c77edd / 80180a2 / 7f26fd2 / c7edafc).
**Raport precednt:** `docs/audit_tabs/TAB_04_NEWS.md` (2026-09-04).

---

## 1. VERDICT GLOBAL

**SISTEM FUNCȚIONAL ȘI ONEST — 0 defecte P1. Toate fixurile din runda 1 se confirmă live. 3 defecte P2 (calibrare impact/breaking dominată de postări sociale eToro, pipeline-ul de briefings AI efectiv inoperant din cauza allowlist-ului SSRF, payload de 2,7 MB cu 31% duplicate) + 6 P3.**

Rezultatele-cheie ale verificării live:
- `GET /api/news-tab/state` → 200, **2.692.174 bytes (~2,69 MB)**, rebuild complet în **0,34–0,36 s** (runda 1 raporta 9,9 s — rezolvat prin memoizarea `_canonical_headline_key`, commit 80180a2), HIT în cache la **0,008 s** (TTL 10 s, single-flight `_build_news_tab_state_cached`, `api/main.py:5451-5576`).
- Payload: 1.005 intrări / **691 id-uri unice** (314 intrări duplicate cross-bucket, deduplicate corect în frontend), 38 bucket-uri ticker, `market_news=200`, `breaking_news=50` (limite din `config.json`: `state_limit_market=200`, `state_limit_breaking=50`).
- **Vârste corecte:** drift `now − published_at` vs `age_minutes` = **0,01–1,01 min** pe toate cele 1.005 articole (metadatele de vârstă din fixul 8c77edd sunt corecte și consumabile).
- **Dedup by id — REZOLVAT și confirmat live:** `frontend/app.js:10276-10286` deduplichează după `id` singur; 1.005 → 691 carduri.
- Sentiment: **100% FinBERT** (`sentiment_quality.source = "finbert"` pe toate cele 691 unice) — MEASURED, nu lexicon fallback.
- FinnHub: cheie absentă din `config.json` (`finnhub_api_key` lipsă) → sursă dezactivată onest, fallback Yahoo RSS; payload conține **0 articole finnhub** — conform avertismentului de la startup.
- Telegram hook (`agents/news_tab_agent.py:1495-1509`): cap 3/refresh, `try/except` total, `notify_telegram` fire-and-forget pe thread daemon (`core/telegram_service.py:194-231`), rate-floor intern; **nu poate bloca pipeline-ul de refresh**. Live: `telegram` neconfigurat în config → no-op tăcut.

---

## 2. TABEL CÂMP-CU-CÂMP (UI → sursă de adevăr)

Legendă verdict: **OK** = lanț complet și real; **STALE** = real dar învechit/mislabeled; **MISCALIBRATED** = produs, dar distorsionat; **DEAD-BACKEND** = produs de backend, neconsumat de UI; **FALLBACK-ONEST** = degradează declarat, nu minte.

### 2.1 Feed principal `#news-page-feed-content` (carduri `.news-card-v2`, `UI.renderNewsCard`, app.js:10926-11002)

| Câmp în UI | Frontend file:line | Endpoint | Backend file:line | Sursă finală | Verdict |
|---|---|---|---|---|---|
| Ticker pill (AAPL/MARKET) | app.js:10961-10964 | /api/news-tab/state | news_tab_agent.py:816 (`payload["ticker"]=t`), to_dict:411 | atribuire la ingest (Yahoo/eToro/crypto) | OK |
| Sentiment pill (▲ Bullish…) | app.js:10929-10930, 10970 | /api/news-tab/state | to_dict:413 + `_analyze_article`→news_tab_agent.py:3556-3570 | **FinBERT local** (core/news_v9/finbert.py) | OK (live: 691/691 finbert) |
| Impact pill (CRT/HIGH + scor) | app.js:10966-10968 | /api/news-tab/state | `_assess_impact` news_tab_agent.py:3617-3624; `score_to_severity`:337-346 | reguli V2 pe titlu + is_breaking | MISCALIBRATED (P2-1) |
| Sursă (`nc2-meta-source`) | app.js:10987 + `formatNewsSourceName`:10292-10314 | /api/news-tab/state | to_dict:408 | numele feed-ului/contului la ingest | OK (dar KPI Sources umflat, P3-4) |
| Vârstă (`nc2-time`, „12m ago") | app.js:10932 | /api/news-tab/state | `published_at_formatted` = `_format_time()` news_tab_agent.py:508-532 (TZ București), fallback `UI.formatTimeAgo(published_at)` | `published_at` de la provider (ISO/epoch — parser 8c77edd acoperă epoch s/ms) | OK (drift ≤1 min live; formatar ea îngheață max ~10 s cât e valabil blob-ul cache-uit — acceptabil) |
| Titlu (`nc2-headline`) | **app.js:10995 — inserat RAW, fără escaping** | /api/news-tab/state | to_dict:406 | RSS/API provider, nesanitizezat nici în agent | P3-1 (XSS latent; live 0/691 titluri cu HTML) |
| „Takeaway: …" (WIIM) | app.js:10953-10959, 10972-10974 | /api/news-tab/state | `ai_micro_summary.tldr` ← `_generate_micro_summary_worker` news_tab_agent.py:2237-2343 || fallback `article.summary` (provider) | P2-2 (AI acoperire 1/691; fallback-onest spre sumarul providerului) |
| Categorie chip | app.js:10931, 10998 | /api/news-tab/state | `_detect_category` news_tab_agent.py:3609-3614 + CATEGORY_PATTERNS:196-213 | regex pe titlu+sumar | OK de la reorder (FED ultima); P3-5 (43% „earnings" prin pattern larg) |
| Tag BREAKING | app.js:10939, 10999 | /api/news-tab/state | to_dict:438 + `_is_breaking` news_tab_agent.py:4122-4162 | substring keywords + macro tier | MISCALIBRATED (P2-1; fereastră badge 72 h — news_tab_agent.py:243) |
| Stare citit/necitit | app.js:10934-10940 | — (localStorage `read_news`) | — | client | OK |
| Salvare (bookmark) | app.js:10934, 10990-10992 | — (localStorage `saved_news`) | — | client | OK |
| Selectare card → insight | app.js:11004-11027 | — + POST /api/news/analyze la cerere | api/main.py:6488 | Ollama + fallback determinist | OK (ne-testat live — POST interzis; cod verificat) |
| Deduplicarea feed-ului | app.js:10266-10290 (`_flattenNewsArticles`) | /api/news-tab/state | `_organize_state` (același articol în mai multe bucket-uri) | — | **OK — fix runda 1 confirmat live (1005→691)** |

### 2.2 Panoul breaking „Market-moving now" (`#news-breaking-section`, app.js:10813-10854)

| Câmp | Frontend | Endpoint | Backend | Sursă | Verdict |
|---|---|---|---|---|---|
| Lista urgentă (≤6) | app.js:10815-10849, titluri **escapate** (`escT` 10831) | /api/news-tab/state | idem impact/is_breaking | idem | OK (XSS tratat aici, spre deosebire de card) |
| Contor `#news-breaking-count` | app.js:10825-10829 | /api/news-tab/state | — (numărare client) | — | OK |
| Poll dedicat + audio/toast | app.js:21069-21125 (buclă 45 s, backoff, dedup `_alertedNewsKeys`) | GET /api/news-tab/breaking (api/main.py:5810) | agent breaking slice; to_dict:438 | idem | OK (Alerte audio din frontend/Critical_News.wav) |

### 2.3 KPI overview (`UI.renderNewsOverview`, app.js:10340-10379)

| Câmp | Frontend | Sursă | Verdict |
|---|---|---|---|
| `#news-metric-total` + range | app.js:10365-10366 | numărare client pe fereastră (24h…30d) din payload | OK |
| `#news-metric-moving` + notă | app.js:10367-10368 | is_breaking ∨ impact∈{critical,high} ∨ action_score≥prag (settings.actionability_threshold=60, backend news_tab_agent.py:866-874) | MISCALIBRATED (moștenește P2-1; live 109 unice cu action_score ≥90) |
| `#news-metric-watchlist` + notă | app.js:10369-10370 | intersecție cu `NewsUIState.watchlistSet` (config watchlist) | OK |
| `#news-metric-sources` + „Most active" | app.js:10371-10372 | număr de `source` unice | MISCALIBRATED (P3-4: 217 „surse", din care 206 sunt handle-uri eToro @user) |

### 2.4 Bandă narativă & mood (`UI.renderMarketNarrative` 10624-10647, `UI._renderNewsMoodBar` 10705-10729)

| Câmp | Frontend | Endpoint | Backend | Sursă | Verdict |
|---|---|---|---|---|---|
| Regim `#news-narrative-status` | app.js:10630-10637 | /api/news-tab/state | `market_narrative.risk_regime` ← `get_market_narrative` news_tab_agent.py:2453-2468 | Ollama (worker 2378-2420) sau `_build_market_narrative_fallback`:1948-1960 | OK — live: „mixed", conținut real generat (nu mai e hardcoded-ul din runda a 2-a); tipuri corecte list (verificat live) |
| Text pulse `#news-market-narrative-content` | app.js:10638-10644 | idem | `what_matters_today[0] ‖ top_catalysts[0]` | idem | OK (folosește `textContent` — fără risc XSS) |
| Timestamp `#news-narrative-updated` | **app.js:10645-10646** | idem | `generated_at` există în payload, dar UI afișează `formatTimeAgo(safeData.last_update)` | — | **P3-2 — STALE/mislabeled**: etichetează timpul de sincronizare a feed-ului ca timp al narativului; `generated_at` (producător real) e ignorat |
| Mood bar (▲x% ▼y% + tilt) | app.js:10705-10729 | /api/news-tab/state | agregare client pe `sentiment` | FinBERT | OK |

### 2.5 Panou AI Insight (`UI.renderInsightPanel`, app.js:11445-11700)

| Câmp | Frontend | Backend | Sursă | Verdict |
|---|---|---|---|---|
| WHAT — `article_explainer` | app.js:11500-11501 | `ai_deep_analysis`/`ai_micro_summary.briefing_version≥3` (news_tab_agent.py:2143-2157) | Ollama → fallback sumar provider | FALLBACK-ONEST — live 0 deep, 1 micro valid → aproape mereu meta-fallback |
| MARKET — `market_implication` + badge direcție | app.js:11502-11504, 11490-11492 | idem; fallback = bucket sentiment | idem | FALLBACK-ONEST |
| TICKER — direcție/încredere/„Why" | app.js:11506-11526 | `ticker_implication` (direction/confidence/relationship/rationale) | idem | FALLBACK-ONEST |
| Evidence scope label | app.js:11493-11499 | `evidence_scope` (full-article/headline-only/…) | worker 2276-2279 | OK — eticheta „Provider metadata" e onestă când AI lipsește |
| Footer acțiune/orizont | app.js:11487 + secțiunea footere | `action_score` (news_tab_agent.py:3732+) | reguli + orizont AI | OK |
| Buton „REFRESH FULL ANALYSIS" | app.js:11851 (`POST /api/news/analyze`) | api/main.py:6488-6380 | Ollama + fallback determinist | OK (cod; POST netestat conform constrângeri) |

### 2.6 Bară de stare, contoare, filtre

| Câmp | Frontend | Backend | Verdict |
|---|---|---|---|
| `#news-last-updated` (Synced Xm ago · HH:MM, .is-stale) | app.js:10221-10228 | `last_update` = commit-ul `refresh_all_news` (news_tab_agent.py:1528) | OK |
| `#news-agent-status` (OK/DEGRADED/PAUSED/INITIALIZING) | app.js:10184-10219 | `flags` (news_tab_agent.py:886-916: stuck_tickers, stuck_feed_count) + disabled/loading/error | OK — chei confirmate live (toate goale = sănătos) |
| Chips ticker `#news-ticker-chips` + contoare | app.js:10678-10703 | numărare client; `ALL` = total | OK |
| Contor feed `#news-feed-count` | app.js:10907-10914 | număr client după filtre | OK |
| Selector sursă `#news-source-select` | app.js:10316-10338 | sursele prezente în payload | OK |
| Panou export `#news-export-panel` → GET /api/news-tab/export | app.js:10596-10612 | api/main.py:6017 (parametri days/tickers/impact/include_* — se potrivesc 1:1) | OK — probă live: 200, 3.497 bytes, 15 articole AAPL/1z, format text corect |
| Supresii `#news-suppression-summary` | app.js (`updateSuppressionSummary`) + GET /api/news-tab/suppression (api/main.py:5866; live: reguli goale) | persistă în data/news_suppression.json | OK, dar panoul rămâne în blocul ascuns `#news-legacy-elements` (index.html:1236-1237) — P3-6 |

---

## 3. STATUS REGRESII vs RUNDA 1

| Element runda 1 | Status | Dovezii |
|---|---|---|
| Dedup feed by id (fix) | **ȚINUT** | app.js:10276-10286; live 1.005 intrări → 691 id unice, 0 duplicate randate |
| Metadate vârstă + hydration skip (fix 8c77edd) | **ȚINUT** | `age_minutes`/`freshness` în to_dict:443-444; drift live ≤1,01 min; parser epoch la news_tab_agent.py:110-120 |
| Cold rebuild 9,9 s (P3, candidat SWR) | **REZOLVAT FĂRĂ SWR** | rebuild live 0,337-0,359 s după memoizarea `_canonical_headline_key` (80180a2, news_tab_agent.py:3980-4010). SWR NU a fost implementat pentru acest endpoint: helper `_get_news_tab_state_stale_payload` (api/main.py:4883-4889) este **cod mort — niciun apelant** (doar /api/cockpit a primit SWR). Risc rezidual: rebuild-urile concurente sunt coalescate single-flight, deci OK |
| Calibrare impact 70% high/critical (P3) | **PARȚIAL — RĂMÂNE DESCHIS (P2-1)** | live: 46,3% high+critical din unice (54,7% din intrări) — mai bine, dar **88% (49/56) din articolele CRITICAL sunt postări sociale eToro** (ex. „ETORO BUILDERS ASKED ME WHAT I AM BUILDING…" = CRT 100, action 100) |
| XSS latent în titluri (P3) | **PARȚIAL — DESCHIS (P3-1)** | lista breaking escapată (app.js:10831 escT) și insight panel (esc 11473-11474), dar cardul principal inserază `article.title` raw la app.js:10995; live 0/691 titluri cu `<`/tags — latent |
| FED misclassification (runda 1, anomalia #1) | **REZOLVAT** | reorder CATEGORY_PATTERNS (80180a2): EARNINGS primul, FED ultimul (news_tab_agent.py:196-213); live: doar 10 articole FED vs 436 earnings |
| `#news-impact-bars` orfan (runda 1, #2) | **DOWNGRADE — cod mort** | `UI.renderNewsHeatmap` (app.js:20412-20413) nu mai are niciun apelant (grep: doar definiția); nu mai rulează „în gol", doar e moartă |
| Supresia ascunsă (runda 1, #3) | **DESCHIS (P3-6)** | index.html:1236-1237 — `#news-suppression-summary` încă în `#news-legacy-elements.hidden` |
| Narativ hardcoded fake (fix 200cd2e) | **ȚINUT** | live: narativ real generat (list types, generated_at 19:46, tone=mixed) |

---

## 4. DEFECTE (cu dovezi live)

### P2-1 — Calibrarea impact/breaking este dominată de zgomotul social eToro
- **Producător:** `_is_breaking` (news_tab_agent.py:4122-4162) folosește potriviri **substring** (`"hot"` prinde „hotels", `"fed"` prinde „federal/fedex") și `_detect_macro_tier` pe titlu; `_calculate_impact_score` (3626-3690) adună +40 base ticker +25 breaking +40 macro tier-1 → orice postare eToro care menționează „Fed/jobs" ajunge **CRT 100, action_score 100**.
- **Live:** 105/691 unice = breaking (15,2%); 56 critical, din care 49 (88%) eToro social; exemplu: `etoro_8fc85bb0…` „ETORO BUILDERS ASKED ME WHAT I AM BUILDING FOR MY COMMU…" — CRT 100, action 100. Fereastra badge-ului BREAKING este **72 h** (news_tab_agent.py:243, 4130) — o postare de acum 2 zile încă afișează „BREAKING" și ocupă inbox-ul urgent (`#news-breaking-section` ia critical/high <24 h, deci atenuat acolo, dar tag-ul de card persistă).
- **Impact UI:** KPI „Market-moving", focus rail „Top"/„Breaking", mood-ul și sunetele de alertă (bucla 45 s) sunt antrenate de aceste etichete → alarme false frecvente.

### P2-2 — „Takeaway" AI / Briefing 3 puncte: acoperire reală 1/691 (0,14%)
- **Lanț:** card → `ai_micro_summary.tldr` ← `_generate_micro_summary_worker` (news_tab_agent.py:2237-2343) → `fetch_article_content` (core/content_fetcher.py:74-127) → **allowlist SSRF la core/content_fetcher.py:17-37 care NU conține `etoro.com`**.
- **Live:** 47% din articolele unice sunt postări eToro (327/691) — toate eșuează la fetch cu „Blocked URL domain not in allowlist" și primesc placeholder `{"tldr":"unknown"}` (news_tab_agent.py:2330-2340). Magazia `data/news_analysis.json`: 142 intrări recente (≥09-03), doar **10 valide**; payload live: 18 micro non-null, **1 tldr valid**, 0 deep. Ollama este UP (gpt-oss:20b-cloud răspunde pe :11434) — cauza nu e LLM-ul.
- **Efect:** cardurile afișează de fapt sumarul providerului sub eticheta „Takeaway:" (fallback app.js:10953-10959 — onest, dar feature-ul flagship e inoperant); panoul Insight rămâne pe „Provider metadata".

### P2-3 — Payload de 2,69 MB cu 31% duplicate + SWR mort
- `/api/news-tab/state` servește 1.005 intrări pentru 691 articole (314 duplicate cross-bucket, news_tab_agent.py:816 păstrează fiecare articol în toate bucket-urile relevante). Frontendul poll-uiește la ~8 s (client TTL, app.js:925-960) + buclă breaking la 45 s → ~19 MB/min descărcat în viziune activă.
- SWR nu există pentru acest endpoint: `_get_news_tab_state_stale_payload` (api/main.py:4883-4889) — definit, **niciun apelant** (doar cockpit-ul are SWR după 80180a2). Mitigat de rebuild-ul de 0,34 s, dar parametrul rămâne un cârlig abandonat.

### P3 (sinteză)
1. **XSS latent** — `article.title` inserat raw la app.js:10995 (și `article.id` în `onclick='…'` la 10979/10963 — id-urile actuale sunt hex/UUID, deci sigure). Live 0/691 titluri cu HTML; un feed rău-intenționat ar putea injecta markup.
2. **Timestamp narativ mislabeled** — `#news-narrative-updated` afișează `last_update` (sync feed), nu `market_narrative.generated_at` (producătorul real, prezent în payload) — app.js:10645-10646.
3. **Câmpuri backend moarte** — `age_minutes`, `freshness`, `sentiment_error`, `also_reported_by` (4/691), `source_provider` (gol pe 158) sunt produși de `to_dict` dar neconsumați în frontend-ul news (grep zero referințe).
4. **KPI „Sources" umflat** — 217 „surse" unice, dintre care 206 sunt handle-uri eToro `@user` (app.js:10354-10372 numără string-ul `source` ca publicație); efectiv ~6 outlet-uri reale.
5. **Dezechilibru categorii** — 43% „EARNINGS" (regex prea larg: `revenue|profit|results|beat|miss|quarter`, news_tab_agent.py:197) — inversul problemei FED din runda 1.
6. **Supresii fără UI vizibil** — `#news-suppression-summary` în `#news-legacy-elements.hidden` (index.html:1236-1237); regulile funcționează (endpoint live OK) dar utilizatorul nu le poate gestiona vizual.

---

## 5. RECOMANDĂRI

1. **(P2-1)** În `_is_breaking`, înlocuiește substring-urile cu regex word-boundary (amplasează `\bhot\b`, `\bfed\b`); coboară `BREAKING_WINDOW_MINUTES` de la 72 h la 2-6 h; pentru articolele `source_provider=etoro` aplică un plafon de impact (ex. max MEDIUM cu excepția symbol-urilor watchlist) sau scade bonusul breaking pentru conținutul social.
2. **(P2-2)** Adaugă `etoro.com` (+ subdomenii) în `_ALLOWED_DOMAINS` (core/content_fetcher.py:17) sau scurtează calea: pentru postările eToro generează micro-summary direct din `summary`-ul deja ingest-uit (evidence_scope „headline-summary") în locul fetch-ului blocat; monitorizează rata tldr-valid/total.
3. **(P2-3)** Trimite în payload doar prima apariție a fiecărui id (sau un flag `dup_of`), reducând ~30% din bytes; conectează sau șterge `_get_news_tab_state_stale_payload`.
4. **(P3-1)** Aplică `escT`/`esc` și pe `nc2-headline` (app.js:10995) și pe celelate interpolări de titlu (ticker panel 11162, renderNewsTickerGroup 11200).
5. **(P3-2)** Folosește `market_narrative.generated_at` pentru `#news-narrative-updated`.
6. **(P3-4)** Agregă sursele eToro într-o singură publicație „eToro Community" pentru KPI-ul Sources și selectorul de surse.
7. Curăță codul mort: `UI.renderNewsHeatmap` (app.js:20412), câmpurile neconsumate din `to_dict` (după decizie explicită), și scoate panoul de supresii din blocul legacy.

---

## 6. ANEXĂ — Probe live (curl GET, 2026-09-05 ~19:51-19:58 UTC)

| Cerere | Rezultat |
|---|---|
| GET /api/news-tab/state (×3, la ~1,2 s) | 200; 0,359 s / 0,008 s / 0,008 s; 2.692.174 B identic (cache HIT) |
| GET /api/news-tab/state după pauză 12 s | 200; 0,337 s (rebuild după TTL 10 s) |
| GET /api/news-tab/state?active_tickers=AAPL,MSFT,TSLA,BTC-USD | 200; 0,149 s; 1.067.002 B; bucket-uri AAPL/MSFT/TSLA (BTC-USD fără știri relevante → absent, corect) |
| GET /api/news-tab/ticker/AAPL?max_items=5 | 200; 0,022 s; cache_status=HIT, fetch_reason=cached; 5 articole, schemă completă |
| GET /api/news-tab/breaking | 200; 0,020 s; `{breaking:[50×critical], disabled:false}` |
| GET /api/news-tab/suppression | 200; reguli goale (0 sources/0 keywords/0 ticker_topics) |
| GET /api/news-tab/export?days=1&tickers=AAPL&include_tldr=true | 200; 3.497 B text; 15 articole, format corect |
| GET http://127.0.0.1:11434/api/tags (verificare Ollama) | 200; gpt-oss:20b-cloud disponibil |

Verificări pe payload: chei top-level complete (13/13 consumate de frontend); chei articol (31) toate prezente; 0 id-uri lipsă; 0 titluri goale; 0 URL-uri invalide; distribuție vârste 13 min … 59,5 zile (fereastră istoric 60 zile — consistent cu NEWS_HISTORY_WINDOW_DAYS).
