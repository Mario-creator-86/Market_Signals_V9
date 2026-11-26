# AUDIT FORENSIC V2 — TAB 3 "MARKET SCANNER" (`#view-scanner`)

**Data evaluării:** 2026-09-05 (audit round 2, live pe http://127.0.0.1:8003)
**Metodă:** trasare completă UI → payload → endpoint → producător backend → sursă primară, verificare live GET-only (17 cereri API local + 1 cross-check Binance public), comparație byte-level frontend servit vs. repo.
**Verdict general:** **SĂNĂTOS CU DEFECTE MINORE — 0 P1, 1 P2, 8 P3.** Fiecare valoare randată în UI se trasează la o sursă reală (Binance / KuCoin / Yahoo Finance); nu s-a găsit niciun câmp fabricat (FAKE) și nicio coloană moartă (DEAD-BACKEND). Fix-urile P2 din round 1 se confirmă live. S-a descoperit **un P2 nou dovedit live**: cache-ul KuCoin movers ignoră parametrul `limit`.

**Integritate live confirmată:** `http://127.0.0.1:8003/app.js` servit este **byte-identic** cu `frontend/app.js` din repo (md5 `58e38e2de7`, 1.321.817 bytes) — auditul codului = auditul instanței care rulează.

---

## 1. HARTĂ CÂMP-CU-CÂMP (Câmp | Frontend | Endpoint | Backend | Sursă | Verdict)

Legendă verdict: OK / DEAD-RENDER / DEAD-BACKEND / FAKE / STALE / MISCALIBRATED.

### 1.1 Scanner Stream Tape (`.tape-board`, 3 coloane) — `frontend/index.html:664-688`

| Câmp | Frontend file:line | Endpoint | Backend file:line | Sursă | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Coloană Binance `#crypto-marquee` | index.html:670; app.js:4147-4160 (`_renderTapeColumn`) | GET `/api/scanner/state` | agents/scanner_agent.py:854-905 (`_scan_crypto`) | Binance `GET /api/v3/ticker/24hr` (scanner_agent.py:857) | OK |
| Coloană KuCoin `#kucoin-marquee` | index.html:677; app.js:4054-4106 | GET `/api/crypto/movers?limit=15` | api/main.py:4782-4794 → api/market_data_service.py:435-537 (`get_top_movers`) | KuCoin `GET /market/allTickers` (market_data_service.py:471) | OK (dar vezi P2-1 cache limit) |
| Coloană Stocks `#stocks-marquee` | index.html:686; app.js:4153-4167 | GET `/api/scanner/state` | agents/scanner_agent.py:651-850 (`_scan_stocks`) | Yahoo Screener `day_gainers`/`day_losers`/`most_actives` (scanner_agent.py:663-671) + `trending/US` (756) + yfinance watchlist fallback (813-842) | OK |
| `tr-rank` (locul în coloană) | app.js:3860 | — (index local din `_renderTapeColumn`:3899) | — | client-side | OK |
| `tr-ticker` (simbol) | app.js:3861, 3850 | idem coloană | crypto: scanner_agent.py:891; stocks: 730/790; kucoin: market_data_service.py:500 | Binance `symbol` / Yahoo `symbol` / KuCoin `symbol` | OK |
| `tr-spark` (sparkline SVG 46×24) | app.js:3862, `_tapeSpark`:3812-3842, `_refreshScannerSparklines`:3935-3962, `_patchScannerSparklines`:3964-3972 | GET `/api/sparklines?symbols=...` | api/main.py:4320-4357 (`api_sparklines`, TTL 900s) → `_fetch_sparkline_points`:4271-4317 | Binance/KuCoin klines 15m (crypto) sau StockDataClient 15m (acțiuni) | OK (live: placeholder onest la primul hit, 26-30 puncte reale după ~7s; retry front-end 4×6s la app.js:3958-3959) |
| `tr-price` (preț `$`) | app.js:3863 (`formatPrice`), patch:3902-3919 | idem | crypto: scanner_agent.py:893 (`lastPrice`); stocks: 733/793; kucoin: market_data_service.py:501 (`last`) | Binance lastPrice / Yahoo regular|pre|post / KuCoin last | OK — cross-check live: `NFPUSDT` scanner `0.00181/-65.85%` == Binance public live `0.00181000/-65.849` |
| `tr-chip` (procent ±) | app.js:3864, patch:3920-3924 | idem | crypto: scanner_agent.py:892 (`priceChangePercent`); stocks: 703-712 (preferă pre/post-market: 689-712); kucoin: market_data_service.py:502 (`changeRate`×100) | idem surse | OK |
| Buton `+` watchlist | app.js:3865 → `UI.addScannerTickerToWatchlist` :4605 | POST watchlist (nu GET, ne-testat) | — | — | OK |
| Flash `flash-up`/`flash-down` la schimbarea prețului | app.js:3907-3916 (`_patchTapeRow`) | — | — | — | **DEAD-RENDER pe coloanele Binance+Stocks** — `renderScanner` golește `innerHTML` înainte de diff (app.js:4116-4117), deci calea de patch (3894-3897) nu se atingere niciodată pentru acestea; funcționează doar pe coloana KuCoin (care nu e golită). P3-4 |
| Cascadă animată `.tape-cascade` | app.js:3877-3884 (`_tapeCascade`, durată `max(16, n*2.4)s`) | — | — | — | OK (dar din cauza golirii de la 4116, animația Binance/Stocks se restartează la fiecare poll de 15s — același P3-4) |
| Tilt 3D `.tape-col` | app.js:3993-4014 (`_initTapeTilt`, rAF, `--rx/--ry`) | — | — | — | OK |
| Sortare tape după \|change\| | app.js:4061 (kucoin), 4149 (crypto), 4155 (stocks) | — | — | client-side | OK |
| Slice tape la 20 rânduri | app.js:4150, 4156 | — | — | client-side | OK (sursă oscilației contor — P3-1) |

### 1.2 Bara de sumar `#scanner-summary-strip` — index.html:651; app.js:4197-4237

| Câmp | Frontend file:line | Endpoint | Backend | Sursă | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Universe | app.js:4218 | — (derivat client din `rows`) | — | setul activ din `/api/scanner/state` sau `/api/crypto/movers` | OK (pe tab KuCoin arată 30 din cauza P2-1 în loc de ~100) |
| Breadth (pozitiv/negativ) | app.js:4201-4202, 4223 | — | — | idem | OK |
| Average Move | app.js:4203-4205, 4228 | — | — | media \|change_pct\| | OK |
| Leader + timestamp | app.js:4206, 4211-4214, 4231-4234; `as_of_utc` din `scannerState` | GET `/api/scanner/state` | scanner_agent.py:569 (`_last_success_dt`) | timestamp UTC ultim scan reușit | OK (live: age 36s la poll) |

### 1.3 Status header / freshness / alertă degradare — index.html:647-650

| Câmp | Frontend file:line | Endpoint | Backend | Sursă | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Badge stare agent (OK/ERROR/WARMING_UP) | app.js:4257 → `renderAgentStatusHeader`:7020-7086 | GET `/api/scanner/state` | scanner_agent.py:548-554 (`status.state`, `_last_error`) | stare internă agent | OK (live: `state=OK`, `last_error=null`) |
| Freshness `#scanner-freshness-badge` | app.js:4260-4263 (`warnMinutes:3, staleMinutes:10`) | idem | scanner_agent.py:569 | `as_of_utc` | OK |
| Activitate (Idle/Refreshing) + popover Status (Synced/Cadence/Age) | app.js:7043-7075, 7077-7084 | idem | scanner_agent.py:539-546 (`next_run_in`, `data_age_seconds`), 565/570 | intern | OK (live: `next_run_in` 21→0, apoi scan nou la 19:34:51 — ciclu 60s sănătos) |
| Alertă `#scanner-status-alert` "Degraded" | app.js:4265-4275 | idem | scanner_agent.py:553 (`last_error`) | excepții backend | OK (live: ascunsă — `last_error: null`) |

### 1.4 Grila principală `#full-scanner-grid` — index.html:722; app.js:4252-4563

| Câmp | Frontend file:line | Endpoint | Backend | Sursă | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Ticker | app.js:4494, 4517 | `/api/scanner/state` (crypto/stocks) | scanner_agent.py:730/791/831 | Yahoo/Binance | OK |
| Preț + "spot reference" / `vol compact` | app.js:4523, 4504-4506 (`Intl.NumberFormat` compact) | idem | volume: scanner_agent.py:738/798 (Yahoo `regularMarketVolume`), 894 (Binance `quoteVolume`) | Yahoo/Binance | OK (live: toate cele 72 de acțiuni au `volume>0` și `avg_volume` 72/72) |
| Modificare (24h) + tag colorat | app.js:4529, 4499-4501 | idem | scanner_agent.py:703-712/777-785 (pre/post-market preferred) | Yahoo | OK |
| Bară momentum `.scanner-momentum-fill` | app.js:4503, 4531-4533 (`min(100, max(8, \|chg\|*1.4))`) | — | — | derivat din change_pct | OK (scală arbitrară dar coerentă; saturează la ~71%) |
| Badge market CRYPTO/STOCK + sursă | app.js:4509-4511 | idem | `source`: scanner_agent.py:745/807/836/897 | yahoo_gainers/losers/active/trending/watchlist, binance | OK |
| Meta "Binance momentum tape" / "Equity scanner feed" | app.js:4518 | — | — | static | OK |
| **Randurile KuCoin** (tab `#tab-kucoin`) | app.js:4278-4392; fetch `limit=50` la :4288 | GET `/api/crypto/movers?limit=50` | market_data_service.py:435-537 | KuCoin allTickers, filtru >$50k vol (:498) | **MISCALIBRATED — P2-1**: live, `limit=50` a returnat **15+15 rânduri, byte-identic** cu `limit=15` (md5 `3392f12d` ambele), pentru că cache-ul `_movers_cache["all"]` (:452, :526) nu include `limit` în cheie |
| Meta "KuCoin perpetual mover" | app.js:4346 | — | — | static | **MISCALIBRATED (P3-6)**: datele sunt **spot** (`/market/allTickers`), nu perpetual futures |
| Sort "Most Traded" (`active`) | app.js:4431-4450 | — | — | client-side | **OK — FIX P2 round 1 confirmat**: fallback `sorted = filtered.slice()` la :4441 când filtrul `_active` golește tab-ul crypto + sortare pe `volume` :4445, fallback volatilitate :4448-4450; live toate cele 122 rânduri au `volume>0`, deci sortarea pe volum funcționează pe ambele tab-uri |
| Sort gainers/losers/volatility | app.js:4427-4430, 4452 | — | — | client-side; persistat `localStorage('scanner_sort_mode')` :4189/:4193 | OK |
| Empty state | app.js:4398-4408, 4465-4472 (`EmptyStateUI.scanner()`) | — | — | — | OK |
| Retry + refresh manual | app.js:4402 → `Actions.refreshScanner`:16747 → `API.refreshScanner`:834-847 | POST `/api/scanner/refresh` | api/main.py:7212-7228 → scanner_agent.py:484-491 | thread `ScannerManualRefresh` + `_endpoint_payload_clear("scanner_state")` main.py:7221 | OK (POST ne-testat conform constrângeri; cale de cod verificată) |

### 1.5 Toolbar tab-uri + contoare — index.html:692-717

| Câmp | Frontend file:line | Verdict |
| :--- | :--- | :--- |
| `#crypto-count` | scris de 2 autori cu valori diferite: `renderScanner` app.js:4176-4177 (**tape ≤20**) vs `renderFullScanner` app.js:4456/:4461 (**complet = 50 live**) | **P3-1 CONFIRMAT** (oscilație 20↔50; round 1 raporta 20↔73 — azi 20↔72, numărul variază cu rezultatul scanării Yahoo) |
| `#stocks-count` | app.js:4179-4180 (≤20) vs app.js:4457/:4462 (72 live) | **P3-1** — pe tab-ul KuCoin `renderFullScanner` iese devreme prin branch-ul KuCoin (:4278-4392) fără să actualizeze contoarele crypto/stocks, deci ele rămân la 20 |
| `#kucoin-count` | app.js:4065-4066 (gainers+losers de la `limit=15` → 30) vs app.js:4314-4315 (de la `limit=50` → 100 intenționat, **30 real** din cauza P2-1) | P3-1 + P2-1 |
| `aria-selected` pe tab-uri | index.html:695/698/702 static; `switchScannerTab` app.js:4239-4248 schimbă doar clasa `.active` | **P3-3 CONFIRMAT** (round 1 — nereparat) |
| Polling 15s | app.js:20849-20885 (`scannerLoop`; `base=15000` activ, 26000 fundal) → `renderScanner` la +80ms (`scanner:marquee-loop` :20863) și `renderFullScanner` la +120ms (`scanner:full-loop` :20867) | OK (cauza mecanică a P3-1: două randări cu valori diferite la 40ms distanță) |

### 1.6 Panoul Home "Signals Live" (citește feed-ul de semnale generat din scanner) — index.html:215-258

| Câmp | Frontend file:line | Endpoint | Backend | Sursă | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Lista semnale deschise (cap 10 rânduri) | app.js:3545-3727 (`renderSignalsFeed`), cap la :3726 (`capSignalsFeedHeight(container, 10)`, :3732-3750) | GET `/api/signals?limit=100` | api/main.py:2176-2215 (`api_signals` → `load_signals_payload`) | `signals_state.json` — semnale generate de motor din **candidați scanner** (id-uri live `auto_scanner_*`; live: 14 deschise) | OK |
| `ticker`, `side` (LONG/SHORT) | app.js:12268-12271, 12329-12330 | idem | payload `open` | scanner_context + decizie motor | OK |
| Age badge (z vechime) | app.js:12303-12317 | idem | `created_at` | live: PANW creat 2026-09-02 | OK |
| Inel "AI Confidence"/"Signal Score" | app.js:12280-12293, 12334-12340 | idem | `confidence_factor` / `signal_score` | live: `confidence_factor=null` → cade pe `signal_score=85.79` cu eticheta onestă "Signal Score" (fix round 1 intact) | OK |
| Entry / Stop / Target / R:R | app.js:12272-12279, 12341-12346 | idem | `entry/sl/tp/rr_ratio` | live: PANW 330.2448 / 317.035 / 350.059, R:R coerent | OK |
| Prețuri complete din `/api/live-prices` | app.js:3671-3718 | GET `/api/live-prices?remote=true&symbols=...` | api/main.py:4360+ | — | OK |
| Badge "research only" | app.js:12319-12322 | idem | `research_only` | — | OK |
| Empty state "Syncing signals…" coerent cu contor | app.js:3641-3661 | idem | `open_count` | — | OK |

### 1.7 Carduri `capital-scanner-row` (Secțiunea Capital, alimentată din stream-ul scanner) — app.js:3296-3313

| Câmp | Frontend file:line | Endpoint | Backend | Sursă | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Rânduri scanner (ticker, direcție, %, sursă, watchlist) | app.js:3199 (`data.scanner`), 3296-3313 (slice 6) | GET `/api/state?include=capital` | api/main.py:1897/1976 → api/state_service.py:1470 (`get_capital_state`) → `_build_capital_opportunities`:639-680 (hot_tickers[:20] → `scanner`[:12] la :831) | **același** `ScannerAgent.get_state()` (state_service.py:643) | OK — live: 12 rânduri, direcție coerentă cu semnul (VSNT +7.1% LONG, ETN -3.78% SHORT), `provider=yahoo_*` curățat la afișare (app.js:3302) |
| Chip "watchlist" | app.js:3309 | idem | state_service.py:675 (`in_watchlist`) | watchlist.json | OK |

### 1.8 Chei payload neconsumate (informațional)

| Cheie din `/api/scanner/state` | Consumatori frontend | Verdict |
| :--- | :--- | :--- |
| `liquid_crypto_tickers`, `watchlist_stock_tickers`, `watchlist_crypto_tickers` (scanner_agent.py:562-564) | **niciunul** (0 hit-uri în app.js) | DEAD-BACKEND (payload inutil — P3-7, fără efect UI) |
| `premarket_price` (scanner_agent.py:734/794) | neconsumat în scanner UI | observație: câmpul stochează prețul live extins (și post/regular), denumire înșelătoare la nivel de API; fără impact UI |

---

## 2. VERIFICARE LIVE (evidență curl, dumps în $TMP — repo neatins)

1. **`GET /api/scanner/state` (×5, inclusiv 2 la 2s distanță):** 122 `hot_tickers` = 50 `binance` + 25 `yahoo_gainers` + 25 `yahoo_losers` + 19 `yahoo_active` + 3 `yahoo_trending`; toate câmpurile consumate prezente, **0 prețuri 0/null**, `volume>0` pe toate rândurile, `avg_volume` 72/72; `status.state=OK`, `last_error=null`, `as_of_utc` proaspăt (age 36s→59s, `refresh_seconds=60`). Două poll-uri la 2s distanță → **byte-identice** (md5 `862f9040`) — cache-ul endpoint de 20s (api/main.py:7148) funcționează conform documentației.
2. **`GET /api/crypto/movers?limit=50` vs `?limit=15`:** ambele au returnat **15 gainers + 15 losers, byte-identice** (md5 `3392f12d`) — dovada live a **P2-1**. După expirarea TTL-ului de 60s, `limit=50` a returnat 50+50 (md5 `667e514c`) — confirmă că "primul apelant câștigă" pentru fereastra de cache.
3. **`GET /api/sparklines?symbols=NFPUSDT,VSNT,BULLA-USDT,EQPT`:** primul apel `{}` (warming onest), după ~7s → 30/26/30/26 puncte reale de close-uri 15m. Retry-ul frontend (app.js:3958) acoperă fereastra de warming.
4. **`GET /api/signals?limit=100`:** 14 semnale deschise, toate cu `scanner_context` populat (ex. PANW: `source=yahoo_trending`, `change_pct=-8.79`, `dollar_volume=1.788B`) — legătura semnale↔scanner confirmată live.
5. **`GET /api/state?include=capital`:** `opportunities.scanner` = 12 rânduri coerente cu `/api/scanner/state` (VSNT/BE/ETN/AXTI identice).
6. **`GET /api/traderadar/state?limit=20`:** 50 candidați, `meta.scanner_candidates=122` (= hot_tickers); **verdict-vs-move coerent 20/20** (toate change_pct>0 → LONG, <0 → SHORT, inclusiv NFPUSDT -65.85% → SHORT cu entry/tp/sl coerente). **Fix-ul P2 "verdicts coherent" din round 1 se menține.**
7. **Cross-check sursă primară (Binance public, 1 cerere):** `NFPUSDT lastPrice=0.00181000, priceChangePercent=-65.849, quoteVolume=3834192.03` — identic cu payload-ul scanner-ului → date reale, nu fabricate.

---

## 3. DEFECTE

### P1 — niciunul

Nu s-a identificat niciun defect de clasei P1: nu există câmpuri FAKE, coloane moarte sau date înșelătoare cu impact pe decizie.

### P2 — 1

**P2-1 · Cache-ul KuCoin movers ignoră parametrul `limit` — grila KuCoin e trunchiată la 30 rânduri.**
- Cod: `api/market_data_service.py:452` (`cls._movers_cache.get("all")`) și `:526` (scriere sub cheia unică `"all"`) — `limit` nu face parte din cheie, deși `get_top_movers(limit)` e parametrizat (:435).
- Evidență live: `limit=50` → 15+15 byte-identic cu `limit=15`; după TTL 60s → 50+50. În producție, tape-ul (app.js:4080, `limit=15`) re-încălzește cache-ul, deci tab-ul KuCoin (app.js:4288, `limit=50`) primește de regulă 30 rânduri în loc de 100; `#kucoin-count` afișează 30, iar "Universe" din sumar arată 30.
- Impact: tab-ul de scanare detaliată KuCoin pierde ~70% din univers; inconsistend cu badge-ul și cu sortarea "Most Traded".
- Fix propus: cheie de cache per-limit (ex. `f"all:{limit}"`) sau invalidare la schimbarea limitei.

### P3 — 8

| ID | Defect | Evidență |
| :--- | :--- | :--- |
| P3-1 | **Contoarele tab-urilor oscilează** (round 1, nereparat): `#crypto-count`/`#stocks-count` scrise de `renderScanner` cu valori tape (≤20, app.js:4176-4180) și de `renderFullScanner` cu valori complete (50/72, app.js:4456-4462), la 40ms distanță în fiecare poll de 15s (app.js:20863 vs :20867); pe tab-ul KuCoin rămân blocate la 20 (branch-ul KuCoin nu le actualizează, :4278-4392). Round 1 raporta 20↔73; azi universul stocks e 72 — mecanismul identic | cod + live 50/72 |
| P3-2 | **Header sticky ineficient** (round 1, nereparat): `.scanner-list-table th { position: sticky; top: 0 }` (styles_scanner.css:536-539) e prins într-un scroll-container creat de `.scanner-list-wrapper { overflow-x: auto }` (styles_scanner.css:517-519) care nu are înălțime limitată — `top:0` nu se activează niciodată la scroll-ul paginii | CSS |
| P3-3 | **`aria-selected` static** (round 1, nereparat): index.html:695/698/702 hardcodat; `switchScannerTab` (app.js:4239-4248) schimbă doar clasa `.active` | cod |
| P3-4 | **Feedback-ul flash de preț mort pe 2 din 3 coloane**: `renderScanner` golește `innerHTML` (app.js:4116-4117) înainte de `_renderTapeColumn`, distrugând calea de patch `_patchTapeRow` (app.js:3894-3897) pentru Binance+Stocks; flash-ul (app.js:3911-3915) și continuitatea cascadei funcționează doar pe KuCoin; animația celor 2 coloane se restartează la fiecare poll | cod |
| P3-5 | **Fanioanele `available`/`stale`/`reason` din `/api/crypto/movers` (market_data_service.py:455-537, F10) sunt ignorate de frontend** (app.js:4089 verifică doar prezența gainers/losers): datele stale (>60s, ≤600s) sunt afișate ca live, iar la pană mesajul "Server offline" (app.js:4100) e înșelător (serverul e viu; KuCoin e căzut) | cod |
| P3-6 | **Etichetă greșită "KuCoin perpetual mover"** (app.js:4346): datele provin din `market/allTickers` **spot**, nu din perpetual futures | cod |
| P3-7 | **Payload mort**: `liquid_crypto_tickers` / `watchlist_stock_tickers` / `watchlist_crypto_tickers` (scanner_agent.py:562-564) nu au niciun consumator în app.js; cod mort `renderCard` (app.js:4113) | grep 0 hits |
| P3-8 | **`premarket_price` conține prețul live extins indiferent de sesiune** (scanner_agent.py:734/794 scrie `price` și în `premarket_price`, chiar și când sursa e post-market sau regular) — denumire de API înșelătoare; fără impact pe scanner UI (câmpul nu e randat) | cod |

---

## 4. STARE REGRESII vs ROUND 1 (comite 80180a2 → HEAD)

- Comite care ating fișiere scanner după round 1: `7f26fd2` (app.js: premarket status text + îndepărtare wrapper `App.updateUI`; api/main.py: alerts — **0 linii în căile scanner**), `e9eda27` (app.js: 12 linii doar în `_preEarningsDecision`), `2a3ebce` (pre-earnings doar), `c7edafc` (premarket_scanner_agent + debate — nu `scanner_agent.py`).
- **Niciun regres** în `agents/scanner_agent.py`, `api/market_data_service.py`, randerele scanner din `frontend/app.js` sau `frontend/index.html`.
- Fix-uri round 1 re-verificate: **(a) "Most Traded" gol pe crypto — REPARAT și confirmat** (app.js:4436-4450; live volume>0 pe 122/122 rânduri); **(b) verdicte coerente în Trade Radar alimentat din stream — MENȚINUT** (20/20 coherent live).
- P3-urile round 1 (oscilație contoare, sticky, aria-selected) — **toate înca prezente** (P3-1/P3-2/P3-3).

---

## 5. RECOMANDĂRI

1. **P2-1:** cheie de cache per-limit în `KuCoinClient.get_top_movers` (market_data_service.py:452/526) — efort minim, câștig imediat pe tab-ul KuCoin.
2. **P3-1:** mută actualizarea contoarelor într-un singur loc (ex. doar `renderFullScanner`) sau pasează contorul complet și către tape; alternatively, ca `renderScanner` să nu scrie contoarele când `#full-scanner-grid` e activ.
3. **P3-4:** elimină golirea `innerHTML` de la app.js:4116-4117 (diff-ing-ul din `_renderTapeColumn` face deja treaba) — restaurează flash-ul și continuitatea cascadei.
4. **P3-5:** consumă `available/stale/reason` în `renderKuCoin`/grila KuCoin și afișează un badge "stale 3m" + motivul real în loc de "Server offline".
5. **P3-2/P3-3:** dă wrapper-ului `max-height` + `overflow-y:auto` pentru sticky real (sau mută sticky pe toolbar); sincronizează `aria-selected` în `switchScannerTab`.
6. **P3-6:** renumește meta-ul în "KuCoin spot mover" (onest, 1 linie).
7. **P3-7/P3-8:** elimină cheile neconsumate din payload sau documentează-le; renumește `premarket_price` → `extended_price` la următoarea migrare de schemă.

---

## 6. CONCLUZIE

Tab-ul MARKET SCANNER este, la round 2, **integral trasabil sursă→UI**: fiecare preț, procent, volum și sparkline provine din Binance/KuCoin/Yahoo Finance, cu cache-uri corect dimensionate (20s endpoint, 60s movers, 900s sparklines, 60s ciclu agent) și fallback-uri reale. Fix-urile P2 din round 1 se confirmă live, iar cross-check-ul extern Binance arată potrivire exactă. Singurul defect nou material este **P2-1 (cache KuCoin fără `limit` în cheie)**, împreună cu cele 3 P3-uri moștenite nereparate (contoare, sticky, aria-selected) și 5 P3-uri minore de onestitate a etichetelor/cod mort. Fără P1.
