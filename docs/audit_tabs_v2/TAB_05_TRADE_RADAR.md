# AUDIT CRIMINALISTIC V2: TAB 5 — TRADE RADAR (`#view-traderadar`)
**Data:** 2026-09-05
**Server live auditat:** `http://127.0.0.1:8003` (doar GET, 4 interogări)
**Metodologie:** Re-audit complet câmp-cu-câmp: `frontend/app.js` (renderer 16320–16666) → `GET /api/traderadar/state` (`api/main.py:7331`) → `TradeRadarAgent` (`agents/trade_radar_agent.py`) → scanner stream (`/api/scanner/state`) → probe live pe 50 de rânduri reale + re-verificarea fix-urilor Round-1 (P2 sub-$1, P3 red-flag slugs, P3 disabled flag).

---

## 1. VERDICT OPERAȚIONAL

**VERIFICAT REAL, COERENT DIRECȚIONAL ȘI FĂRĂ REGRESII.** Toate câmpurile randate în UI se trasăm până la un producător backend real și o sursă de date reală. Pe cele 50 de rânduri live:
- **0 contradicții verdict-vs-mișcare** (nicio linie LONG cu mișcare ≤ −5%, niciuna SHORT cu mișcare ≥ +5%);
- **0 rânduri BLOCKED cu confidence ≥ 60**; 18 rânduri au Evidence Cap activ la exact 59 (comportament NORMAL, documentat — ciclurile auto sunt cache-only);
- **0 roșii de formatare preț**: 16 rânduri sub-$1 se randează cu 4 zecimale (fix P2 confirmat, ex. NFPUSDT `$0.0018`);
- **coerență cross-tab perfectă**: `meta.scanner_candidates=122` == `hot_tickers` din `/api/scanner/state` (122).

**Defecte rămase: 0×P1, 0×P2, 2×P3** (ambele pur cosmetice/informaționale, deja identificate în Round-1 dar încă ne-reparate: red flags afișate ca slug-uri brute; flag-ul `disabled` ignorat de renderer).

---

## 2. STATUS FIX-URI ROUND-1 (re-verificate)

| Item Round-1 | Status | Dovezie |
|---|---|---|
| **P2 — prețuri sub-$1 randate `$0.00`** | **REZOLVAT** | Fix landat în `7f26fd2` (diff: `price.toFixed(price < 1 ? 4 : 2)`), prezent în working tree la `frontend/app.js:16580` + `fmtLevel` 4 zecimale la `app.js:16557-16561`. Live: 16 rânduri sub-$1, niciun `$0.00` (preț minim 0.00181 → `$0.0018`; entry/tp/sl 0.0018/0.0017/0.0019 distincte). |
| **P3 — red flags randate ca slug-uri brute (fără label map)** | **ÎNCĂ DESCHIS** | `frontend/app.js:16545-16547` randează `esc(f)` direct. `BLOCKER_LABELS` (16473-16485) acoperă doar blocker-ele din secțiunea Readiness, nu și red flags. Live: 12 slug-uri distincte afișate raw (vezi §4). |
| **P3 — flag-ul `disabled` ignorat de renderer** | **ÎNCĂ DESCHIS** | `renderTradeRadar` (16320–16647) nu citește nicăieri `data.disabled` (unicul consumator în app.js e toast-ul la refresh eșuat, `app.js:16712`). Când agentul e dezactivat, endpoint-ul întoarce `candidates: []` + `disabled: true` (`api/main.py:7348-7359`) și UI-ul afișează „No Trade Radar candidates right now." cu CTA „Force refresh" — inducător în eroare. |

---

## 3. TABEL CÂMP-CU-CÂMP: PIXEL → ENDPOINT → PRODUCĂTOR → SURSĂ

Toate rutele: `GET /api/traderadar/state?limit=80` (`api/main.py:7331-7395`; lanț: cache endpoint 20s → `agent.get_state()` (`trade_radar_agent.py:284`) → fallback disk `get_traderadar_state()` (`api/state_service.py:1569`)). Frontend: `UI.loadTradeRadarData` (`app.js:16281`, cere limit=80) → `UI.renderTradeRadar` (`app.js:16320`).

### 3.1 Bara de metadate și KPI strip
| Câmp | Frontend file:line | Endpoint | Backend file:line | Sursă | Verdict |
|---|---|---|---|---|---|
| `As of [timestamp]` | app.js:16357-16358, 16390 | traderadar/state | trade_radar_agent.py:536 (`run_cycle` → `as_of`), served la 312 | Ceas server la ultimul ciclu | OK |
| Chip `STALE · Xh/Xd old` | app.js:16365-16376, 16391 | traderadar/state | trade_radar_agent.py:1132-1138 (`data_freshness.scanner_ts`), 450 (`last_seen`) | Scanner re-sight per simbol; prag 12h; fallback `as_of` | OK (live: cel mai proaspăt rând 0.1h → chip absent, corect) |
| `Last cycle: N enriched · M queued` | app.js:16386-16387, 16392 | traderadar/state | trade_radar_agent.py:540-546 (`meta.processed`, `meta.queued` = due_before_limit − processed) | Buget ciclu (auto_max_per_cycle=4) vs. backlog | OK (live: 4/46) |
| `Showing X of Y` (filtru) | app.js:16393 | — | total_candidates: agent.py:318 | Lista filtrată client-side | OK |
| KPI `Candidates tracked` | app.js:16403 | traderadar/state | agent.py:319 (`ordered[:lim]`), cap `max_candidates=50` (agent.py:523) | Candidate map post-expiry 24h | OK (50) |
| KPI `Long · Short` | app.js:16378-16379, 16410 | traderadar/state | agent.py:1090 (`analysis.verdict`) | `_derive_signal_direction` (1322-1350) pe mișcarea scanner | OK (live 41L · 9S) |
| KPI `Avg Confidence` | app.js:16380-16382, 16417 | traderadar/state | agent.py:1091 (`analysis.confidence` = `final_score` capped) | `_compute_confidence` (1786-1852) + Evidence Cap (1053-1059) | OK (live avg 55, min 42, max 59) |
| KPI `Ready Now` | app.js:16377, 16424 | traderadar/state | agent.py:1996-1998 (`readiness.status=READY`) | `_build_readiness` (1967-2031) | OK (live 0 — normal în cache-only) |
| Dropdown simboluri | app.js:16343-16352 | traderadar/state | agent.py:744 (`symbol`) | Intake scanner (agent.py:422-451) | OK |

### 3.2 Card de candidat (per rând)
| Câmp | Frontend file:line | Endpoint | Backend file:line | Sursă | Verdict |
|---|---|---|---|---|---|
| **Simbol** | app.js:16445, 16579 | traderadar/state | agent.py:431/447 | Scanner `cached_hot_list` (`_collect_scanner_candidates`, agent.py:691-710) — Yahoo gainers/losers + Binance | OK |
| **Age chip** (`2h old` / `1d old`) | app.js:16569-16573, 16579 | traderadar/state | agent.py:450 (`last_seen=now_iso` la re-sight) | Momentul ultimei văzări de scanner peste pragul adaptiv | OK (live: 34 rânduri cu chip; 25 la 23.9h pe cale să expire prin fereastra fantomă de 24h, agent.py:395-412) |
| **Price** (4 zecimale < $1 — fix P2) | app.js:16447, 16580 | traderadar/state | agent.py:448, re-afirmat 899-902 | Scanner (preț spot la intake) | OK (live: 16 sub-$1, 0×`$0.00`) |
| **Change %** (badge + culoare) | app.js:16446, 16581 | traderadar/state | agent.py:447, 900-903 (`round(chg,4)`) | Scanner `change_pct` | OK |
| **Verdict pill** (▲LONG/▼SHORT/■NEUTRAL) | app.js:16451, 16464, 16584 | traderadar/state | agent.py:977 → `_derive_signal_direction` (1322-1350); override mișcare ≥5% în stub (1262-1277) | Semnul mișcării + trend hint 1h din watchlist | OK — **0 contradicții live** |
| **Trade mode** (PULLBACK/BREAKOUT/CONTRA/ORB_FVG) | app.js:16585 | traderadar/state | agent.py:1093 → stub 1251-1257 | `best_strategy.trade_mode` din watchlist sau carried | OK |
| **Time horizon** (intraday / 1-2 days) | app.js:16586 | traderadar/state | agent.py:1062 → `_derive_time_horizon` (1854-1858) | |chg|≥15% ȘI ADX≥25 | OK |
| **Confidence pill + meter** (bara cu marker la 60) | app.js:16452, 16465, 16590, 16593-16597 | traderadar/state | agent.py:1040-1059 + 1091; Evidence Cap 59 (1053-1059) | `_compute_confidence`: bază tehnică + news FinBERT − calendar + backtest | OK — live max 59; 18 rânduri capped (formula poartă sufixul „(capped: key indicators missing)", agent.py:1111-1112) |
| **Readiness pill** (READY/NEAR/WATCH/BLOCKED) | app.js:16463, 16466-16468, 16591 | traderadar/state | agent.py:1115-1122 → `_build_readiness` (1967-2031) | blocker-e hard + conf ≥60 + nivele >0 | OK (live 50 BLOCKED — normal: indicatori absenți în cache-only; NU se flag-uitește ca bug) |
| **Entry** | app.js:16454, 16600 | traderadar/state | agent.py:1094 → `_derive_trade_levels` (1352-1380); stub 1290, 1296 | Preț spot; ATR relativ (sintetic 2% din preț, fără floor absolut) | OK |
| **Target (TP)** + % | app.js:16455, 16562, 16601 | traderadar/state | agent.py:1095; LONG: entry+2·ATR, SHORT: entry−2·ATR (1297-1303) | ATR real din OHLC local sau sintetic 2% | OK — 0 violări de direcție live (LONG: tp>entry>sl; SHORT: tp<entry<sl) |
| **Stop (SL)** + % | app.js:16456, 16563, 16602 | traderadar/state | agent.py:1096; ±1.5·ATR (1298-1299, 1302-1303) | idem | OK |
| **Chip-uri motive compacte** (Tech 70/100, News 3↑ 0↓, RSI · ADX, BT 30d) | app.js:16511, 16517-16544, 16609 | traderadar/state | agent.py:1064-1076 → `_build_reasons` (1860-1925) | Regex-urile frontend corespund formatelor text backend (verificat câmp cu câmp: „moved X%" tras; „strength X/100" → Tech; „News flow:" → ↑↓; „Indicators: RSI…ADX…" → RSI·ADX; „Backtest-lite…WR…PF" → BT) | OK |
| **Buton Suppress** + contor | app.js:16565, 16606 | traderadar/suppress (POST) | agent.py:`suppress_symbols` + `_is_suppressed` (2061-2066) | Map de supresie cu expirare (3600s) | OK |
| Buton Details (expand) | app.js:16312-16318, 16605 | — | — | State client (`UI.tradeRadarExpanded`) | OK |

### 3.3 Sertar Details
| Câmp | Frontend file:line | Endpoint | Backend file:line | Sursă | Verdict |
|---|---|---|---|---|---|
| Breakdown Base / News / Calendar / Backtest / Final | app.js:16486-16509, 16612-16613 | traderadar/state | agent.py:1104-1113 (`confidence_breakdown`); `_compute_confidence` 1793-1845 | Base=strength (0-100); News bonus 0..+10 (FinBERT diff×3); Calendar penalty 0..15 (earnings 24h=10, macro high=5/med=2); Backtest 0/+10 (PF/DD, halvat sub 5 trades — 1838-1843) | OK — convențiile de semn corecte (`neg1` randează cu „-" penalitatea stocată ca magnitudine pozitivă, 1818; `signed1` pentru bonusuri) |
| Formula + `Distance to open` | app.js:16615 | traderadar/state | agent.py:1111-1112 (`formula`) + 2016, 2021 (`distance_to_open = 60 − conf`) | Calcul deterministic | OK (live: 0 inconsistențe pe 50 rânduri) |
| Readiness note | app.js:16622 | traderadar/state | agent.py:1996-2014, 2030 | Texte statice pe status | OK |
| **Blocker chips** (etichete umane) | app.js:16469-16485, 16623 | traderadar/state | agent.py:1978-1990, 2029 (`blockers` = intersecția red_flags cu hard_blocker_set) | Set de 10 slug-uri hard | OK — toate blocker-ele live (`backtest_missing_ohlc`, `missing_indicators`, `missing_ohlc`) au etichetă în `BLOCKER_LABELS` |
| Reasons (listă completă, top 5) | app.js:16511, 16629 | traderadar/state | agent.py:1064-1076, 1129 (cap 5) | Texte generate | OK |
| **Red Flags chips** | app.js:16460, 16545-16547, 16633-16634 | traderadar/state | agent.py:888-896, 928-975, 1024, 1060-1061, 1077-1086, 1087 (cap 10), `_build_red_flags` 1927-1965; flag serve-time `analysis_older_than_move` (get_state 300-310) | OHLC warnings normalizate (2033-2059), news/calendar/backtest availability, contradictorii, volatilitate ≥20% | **P3 DESCHIS** — slug-uri brute (vezi §4) |
| Footnote: BT trades / signals / variantă / fallback | app.js:16636-16637 | traderadar/state | agent.py:776-788, 1000-1038 (`backtest_pack`) | QuickBacktester pe OHLC local 5-min | OK |
| Freshness scanner/news ts | app.js:16638 | traderadar/state | agent.py:1132-1138 | Timestamp-uri per pilon | OK |

### 3.4 Coerență cross-tab (scanner stream)
| Verificare | Rezultat live |
|---|---|
| `meta.scanner_candidates` (agent.py:546) vs `/api/scanner/state` → `hot_tickers` | **122 == 122** (coerent) |
| Rânduri radar prezente în hot listul curent | 43/50; lipsesc 7 (CDTG, GPRO, SNXXBUSDT, BROCCOLI714USDT, NEARUSDT, GIGGLEUSDT, FORMUSDT) — toate mișcări de acum ~23.9h păstrate intenționat de fereastra fantomă de 24h (agent.py:395-412), cu age chip randat |
| Drift change_pct rând vs scanner curent | ≤1pct pe majoritate; drifturi mari doar pe rândurile vechi (ex. FICO −16.77 radar vs +0.29 acum) — exact cazul acoperit de age chip + flag-ul `analysis_older_than_move` (13 rânduri flag-uite) |
| Prag de intake adaptiv | crypto ≥15% (max(base,15), agent.py:828-832), stocks ≥5% (min(base,5), 833-836); live: 10 moveri scanner peste prag |

---

## 4. DEFECTE (cu dovezi live)

### P1 — nu există
Zero defecte critice. Nu s-a găsit niciun câmp randat din date moarte, fabricate sau contradictorii.

### P2 — nu există
Fix-ul P2 Round-1 (sub-$1) este prezent, funcțional și acoperit live (16 rânduri).

### P3-1 (DESCHIS, moștenit Round-1) — Red flags randate ca slug-uri brute
- **Frontend:** `frontend/app.js:16545-16547` — `redFlags.map(f => \`<span class="traderadar-flag-chip">${esc(f)}</span>\`)`. Nu există niciun `FLAG_LABELS` în app.js (grep: 0 rezultate). `BLOCKER_LABELS` (16473-16485) acoperă doar blocker-ele din Readiness, nu și lista de Red Flags.
- **Backend:** `agents/trade_radar_agent.py:1087, 1130` + `_build_red_flags` (1927-1965) emit slug-uri mașinice.
- **Dovadă live (dump `GET /api/traderadar/state?limit=80`, 2026-09-05 19:46 UTC):** 12 slug-uri distincte pe 50 rânduri — `calendar_unavailable` (50), `missing_indicators` (50), `ohlc_local_cache_miss` (50), `news_unavailable` (28), `backtest_missing_ohlc` (26), `backtest_unavailable` (26), `missing_ohlc` (26), `ohlc_warning` (26), `analysis_older_than_move` (13), `extreme_volatility` (11), `analysis_strength_estimated` (4), `news_contradicts_short` (1). Cea mai specială—`news_contradicts_short`—apare ca text raw, deși e semnal semantic important.
- **Impact:** cosmetic-informațional; buyer-ul vădește „ohlc_local_cache_miss" în loc de „Cache OHLC local inexistent".

### P3-2 (DESCHIS, moștenit Round-1) — Flag-ul `disabled` ignorat de renderer
- **Backend:** `api/main.py:7348-7359` întoarce `{"candidates": [], "disabled": true}` când `_agent_enabled("trade_radar")` e false; `agents/trade_radar_agent.py:1392, 1587` propagate și la nivel de piloni.
- **Frontend:** `renderTradeRadar` (16320-16647) și `loadTradeRadarData` (16281-16310) nu citesc niciodată `data.disabled` (grep `disabled` în app.js → doar toast-ul `app.js:16712` la eșecul POST refresh).
- **Impact:** cu agentul oprit, tab-ul arată „No Trade Radar candidates right now." + buton „Force refresh" care dană doar un toast — nu un banner explicit „Trade Radar dezactivat din setări".

### Observații minore (non-defect, pentru evidență)
1. **Comentariu imprecis** `app.js:16359-16361` pretinde că „`data.as_of` e rescris la fiecare apel API" — fals: `as_of` avansează doar la cicluri (`agent.py:536`), iar cache-ul endpoint de 20s întoarce payload identic (`api/main.py:7337-7347`). Inofensiv: chip-ul STALE folosește corect proaspățenia candidatului, nu `as_of`.
2. **Age chip la limită**: la 23.9h randează „24h old" (`Math.round(23.9)=24 < 24` → ramura hours) în loc de „1d old" — se corectează singur la ≥24h; pur cosmetic.
3. **Divergență fallback disk**: `get_traderadar_state` (`api/state_service.py:1569-1600`) NU adaugă flag-ul serve-time `analysis_older_than_move` pe care îl adaugă `agent.get_state` (300-310) — doar pe calea de fallback (agent inactiv).
4. **Zgomot known-limitation**: `calendar_unavailable` pe 100% din rânduri (calendarul nu are acoperire crypto — slăbicitate #2 din Round-1, nemodificată) și `news_unavailable` pe 56%.
5. **Cap red_flags**: `agent.py:1087` trunchiază la 10, iar append-ul serve-time din `get_state` poate produce un al 11-lea element — inofensiv (doar payload).

---

## 5. STATUS REGRESII vs ROUND-1

| Verificare | Rezultat |
|---|---|
| `git diff 7f26fd2..HEAD -- frontend/app.js` | **0 hunks** în zona radar — rendererul NU a fost modificat după fix-urile Round-1 |
| Ultimele commit-uri pe `agents/trade_radar_agent.py` | `200cd2e` (audit round-2, anterioare fix-urilor) — motorul backend neschimbat de atunci |
| `80180a2` (perf news/cockpit) | Nu atinge radarul; pilonul de știri rămâne funcțional (news packs populate pe 22/50 rânduri) |
| `c7edafc` (alerts/debate/fallback/backtest/premarket) | Nu atinge nicio cale radar (`agents/premarket_scanner_agent.py`, `core/ai/*`, `core/config.py`) |
| Fix P2 din `7f26fd2` prezent în working tree | DA (`app.js:16580` + `fmtLevel` 16557-16561) |
| Teste unitare radar | **41/41 PASSED** (`test_trade_radar_audit_fixes.py`, `test_trade_radar_signal_audit.py`, `test_trade_radar_ghost_expiry.py`, `test_trade_radar_adaptive_threshold.py` — 0.89s) |
| Stabilitate server între 2 fetch-uri (19:46 → 19:56 UTC) | Ciclu rulat între timp: `as_of` avansat, payload consistent, latență 0.03–0.22s |

---

## 6. RECOMANDĂRI

1. **P3-1 (red flags):** introdus un `FLAG_LABELS` lângă `BLOCKER_LABELS` (`app.js:~16473`) și folosit la `app.js:16546`: `calendar_unavailable → "No calendar coverage"`, `ohlc_local_cache_miss → "No local OHLC"`, `ohlc_warning → "OHLC data warning"`, `news_unavailable → "No news feed"`, `backtest_unavailable → "Backtest unavailable"`, `analysis_older_than_move → "Verdict older than move"`, `extreme_volatility → "Extreme volatility (+/-20%)"`, `news_contradicts_short/long → "News contradicts SHORT/LONG"`, etc. Efort: ~20 min, display-only.
2. **P3-2 (disabled):** în `renderTradeRadar`, după linia 16342: `if (data.disabled) { listEl.innerHTML = banner „Trade Radar is disabled in Settings"; return; }` — și ascunde CTA-ul „Force refresh" inutil.
3. **Corectare comentariu** `app.js:16359-16361` (as_of nu e rescris per apel).
4. **Cosmetic age chip:** pragul de zile la `>= 23.5h` în loc de `>= 24h` (`app.js:16571-16572`) ca 23.9h să randeze „1d old".
5. **Reducerea zgomotului crypto:** suprimarea flag-ului `calendar_unavailable` pentru tickere crypto (nu există earnings) — scutește 100% din rânduri de un flag care nu poartă informație.
6. **Opțional:** mutarea flag-ului serve-time `analysis_older_than_move` și în `get_traderadar_state` pentru paritate între cele două căi de servire.

---

## 7. DOVEZI LIVE (rezumat numeric)

```
GET /api/traderadar/state?limit=80  → 200, 131KB, 0.22s (fetch 1, 19:46 UTC); 200, 131KB, 0.03s (fetch 2, 19:56 UTC)
GET /api/scanner/state              → 200, 57KB, 0.10s — hot_tickers=122
Rânduri: 50 | Verdict: 41 LONG / 9 SHORT / 0 NEUTRAL | Readiness: 50 BLOCKED
Contradicții verdict-vs-mișcare: 0 | BLOCKED cu conf>=60: 0 | conf!=final: 0
Evidence Cap activ: 18 rânduri la 59.0 (formula cu „(capped: key indicators missing)")
Conf: min 42 / max 59 / avg 55.4 | distance_to_open: 0 inconsistențe
Nivele: 0 violări de direcție | entry<=0: 0 | preț sub-$0.0001 (randare $0.0000): 0
Sub-$1: 16 rânduri → toate cu 4 zecimale (ex. NFPUSDT 0.00181 → $0.0018)
Red flags: 12 slug-uri distincte (randate raw — P3-1) | Blockers live: toate cu etichetă umană
Age: cel mai proaspăt 0.1h; 25 rânduri la 23.9h (fereastra fantomă 24h le expiră la ciclul următor)
Pytest: 41/41 passed (0.89s)
```

*Raport V2 finalizat. Sursă dump-uri: doar temporarul de sistem (`%TEMP%/tr_state.json`, `tr_state2.json`, `scanner_state.json`) — repo neatins.*
