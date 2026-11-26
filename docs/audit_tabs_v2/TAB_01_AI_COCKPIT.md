# Audit forensics UI↔Backend — TAB 1: AI Cockpit (`#view-home` / `#ai-trading-cockpit`) — Runda 2

**Data:** 2026-09-05 | **Metodă:** re-verificare completă câmp-câmp a raportului runda 1 (`docs/audit_tabs/TAB_01_AI_COCKPIT.md` + `docs/AUDIT_2026-09-05.md`), cod sursă (`frontend/cockpit.js` 1383 linii, `api/cockpit_service.py` 2048 linii, `api/main.py` SWR) + payload live `GET http://127.0.0.1:8003/api/cockpit` (480 KB, doar GET, ~12 cereri, ≥1s distanță, dump-uri doar în `$TMP`).
**Commit-uri verificate de la runda 1:** `80180a2` (SWR cockpit/state), `7f26fd2` (Telegram + fix-uri audit, doar `api/main.py`), `c7edafc` (fallback debate — nu atinge cockpit-ul), `e9eda27` (componenta c6 eToro retail positioning — doar `agents/pre_earnings_agent.py`, nu atinge renderer-ele cockpit).

---

## Verdict: **SĂNĂTOS CU DEFECTE** — 0×P1 | **2×P2** | 8×P3

Tabul este funcțional cap-coadă: toate cele ~85 de câmpuri randate au producător backend real, nicio valoare falsă hardcodată, stările EMPTY/ERROR sunt oneste, iar SWR-ul din `80180a2` funcționează live (short-circuit `unchanged:true` 47 bytes verificat; gzip activ; `/api/state` la 9.7 ms). **Nu s-a găsit nicio regresie** introdusă de commit-urile de la runda 1.

Cele 2×P2 sunt însă reale și vizibile în UI:

1. **P2-1 — Coloana „Robustness" afișează `[Object Object]`** în tabelul Lifecycle (Strategy Lab): backend-ul trimite un **obiect** walk-forward, frontend-ul îl trece prin `statusPill()`.
2. **P2-2 — KPI „AI status: HEALTHY" contrazice propriul Decision Ledger din același tab**: toate cele 30 de decizii recente au fost respinse cu `ai_model.available=false, response_fallback_used="all_unavailable_blocked"` (AI-ul nu a putut genera niciun răspuns), în timp ce KPI-ul e verde — proba de health e doar un `GET /api/tags` (reachability, nu generare).

---

## 1. Tabel câmp-câmp (UI → payload → endpoint → producător → sursă → verdict)

Legendă verdict: **OK** = cablat corect și live sănătos; **OK\*** = OK cu rezervă minoră; **MISCAL** = miscalibrat; **GOL** = producător nu furnizează date în deployment-ul curent (onest, dar coloană permanent vidă).

### 1.1 Command bar + KPI-uri (`renderKpis`, cockpit.js:149-187)

| Câmp | Frontend file:line | Endpoint | Backend file:line | Sursă | Verdict |
|---|---|---|---|---|---|
| `#cockpit-updated-at` „Snapshot …" | cockpit.js:957-958 | GET /api/cockpit | api/main.py:2058-2096 → `generated_at` (cockpit_service.py:1919) | momentul rebuild-ului | OK |
| `#cockpit-status` (erori surse) | cockpit.js:959-965 | GET /api/cockpit | cockpit_service.py:1965 `source_errors` (`_safe_source`:160-169) | erori sub-producători | OK (0 erori live) |
| `#cockpit-refresh` | cockpit.js:1090-1107 | POST /api/pre-earnings/refresh → GET ?fresh=true | api/main.py (worker pre-earnings) | worker Pre-Earnings | OK (ne-testat activ: POST interzis; cod corect) |
| `#cockpit-kill-switch` stare | cockpit.js:970-985 | GET /api/cockpit | cockpit_service.py:1950 (`execution.kill_switch_active`) | `GET /api/execution/control` (main.py:2458) — **confirmat live: false** | OK |
| Kill-switch dublă armare §74 | cockpit.js:1109-1165 (GET control la 1128 înainte de POST 1141) | GET /api/execution/control + POST kill-switch | api/execution_service.py | config în-memorie | OK (P2 runda 1 — ține) |
| KPI Market status | cockpit.js:158, 169 | GET /api/cockpit | cockpit_service.py:1715-1725 (`us_equity_market_status`, core/market_hours.py) | calendar NYSE/NASDAQ + ET | OK (live: `CLOSED · weekend`) |
| KPI Autonomy mode | cockpit.js:164-167, 170 | GET /api/cockpit | cockpit_service.py:1746, 1923-1930 | `get_execution_control` (api/execution_service.py) | OK (live: `PAPER_AUTO · Paper entry ready`, evaluated=true) |
| KPI AI status | cockpit.js:171 | GET /api/cockpit | cockpit_service.py:1733-1745, 1931-1936 | stream `ollama_availability` (core/ollama_client.py:146 `check_availability`) | **MISCAL (P2-2)** — vezi defecte |
| KPI Market regime | cockpit.js:172 | GET /api/cockpit | cockpit_service.py:1726-1731, 1937-1943 | `get_dashboard_payload` (api/dashboard_service.py) — ADX/BB + sentiment | OK (live: `Mixed · neutral · 93%`) |
| KPI Deployable capital | cockpit.js:173 | GET /api/cockpit | cockpit_service.py:414-463 (`_capital_summary`) | `get_capital_state` (api/state_service.py) → SQLite paper ledger | OK (live: $7,722.87; equity $10,018.12) |
| KPI Portfolio exposure | cockpit.js:174 | GET /api/cockpit | cockpit_service.py:430-434, 453-454 | nu=Σnotional/equity×100 — **formula verificată aritmetic live** (2281.47/10018.12=22.77%) | OK |
| KPI Daily P/L | cockpit.js:175 | GET /api/cockpit | cockpit_service.py:394-411 (`_daily_realized_pnl`) | evenimente `CLOSE_POSITION` din `capital.history` (SQLite), bază UTC | OK (live: −$22.36, 6 tranzacții, bază afișată) |
| KPI Risk status | cockpit.js:176 | GET /api/cockpit | cockpit_service.py:1733-1734, 1945-1949 | constituția de risc activă (agents payload) | OK (live: `Active`) |
| KPI Active positions | cockpit.js:177 | GET /api/cockpit | cockpit_service.py:415-431, 459 | SQLite poziții OPEN | OK (live: 14, unrealized +$13.78) |
| KPI System health | cockpit.js:178 | GET /api/cockpit | cockpit_service.py:1841-1850, 1963 (`_system_health`:1410) | 15 verificări reale | OK (live: `HEALTHY · 15 real checks`) |

### 1.2 Panoul Overview (`renderOverview`, cockpit.js:304-335)

| Câmp | Frontend file:line | Endpoint | Backend file:line | Sursă | Verdict |
|---|---|---|---|---|---|
| Best opportunities (12 rânduri) | cockpit.js:189-197, 320 | GET /api/cockpit | `_opportunity_rows` cockpit_service.py:466-494 | `capital.opportunities.signals` (pipeline scanner) + pre-earnings eligible | OK (live: ENA-USD SHORT 91.8 …) |
| `ai_critic` (cheie din payload) | — (frontend NU o citește) | GET /api/cockpit | cockpit_service.py:478 | evidence critic | OK (greutate moartă inofensivă) |
| Current risks (10 rânduri) | cockpit.js:199-207, 321 | GET /api/cockpit | `_risk_rows` cockpit_service.py:497-543 | kill-switch/reconciliere/readiness + `dashboard_risk_flags` | OK\* (live: 10/10 NEWS_*; vezi P3-4) |
| Active positions card | cockpit.js:289-302, 322 | GET /api/cockpit | `_compact_position` cockpit_service.py:370-391 | SQLite + mark-to-market | OK (pnl/pnl% coerente la cent) |
| Pre-Earnings matrix card | cockpit.js:209-263, 323-327 | GET /api/cockpit | `upcoming_earnings` cockpit_service.py:1863-1892 | `data/pre_earnings.json` + pre-filtrare `_read_pre_earnings_cache`:172-242 | OK (283 candidați, 20 afișați) |
| Sentiment shifts card | cockpit.js:265-272, 328 | GET /api/cockpit | `_sentiment_section` cockpit_service.py:569-583 | `dashboard_market_narrative` | OK\* (`shift_state` hardcodat — P3-3) |
| Major news card | cockpit.js:274-281, 329 | GET /api/cockpit | `_major_news` cockpit_service.py:546-566 | `dashboard_news_items` (agent știri: eToro/Yahoo RSS) | OK\* (calibrare impact — P3-4) |
| AI decisions card (6 rânduri) | cockpit.js:283-287, 330 | GET /api/cockpit | `_ai_decisions_section` cockpit_service.py:899-947 | SQLite `decision_ledger` (core/decision_ledger/store.py) | OK (1975 intrări) |
| Strategy degradation card | cockpit.js:316-317, 331 | GET /api/cockpit | cockpit_service.py:1163, 1907-1911 | strategy registry | OK (EMPTY onest — 0 degradate) |
| Research activity card | cockpit.js:314-315, 332 | GET /api/cockpit | `_research_section` cockpit_service.py:1211-1241 | ExperimentRegistry / fallback perf rows | OK (8 observații) |
| Provider/system health preview | cockpit.js:306-313, 333 | GET /api/cockpit | `_system_health` cockpit_service.py:1410+ | 15 servicii reale | OK (primele 5) |

### 1.3 Matricea Pre-Earnings (`renderEarnings`, cockpit.js:209-263) — 283 candidați, 20 afișați

| Câmp | Frontend file:line | Backend file:line | Sursă | Verdict |
|---|---|---|---|---|
| Ticker | cockpit.js:228 | `_pre_earnings_row`:326 | pre_earnings.json `symbol` | OK |
| Earnings date + schedule_status | cockpit.js:229 | :327 + status calculat la 1696-1705 (DATE_VERIFIED/ESTIMATED/UNAVAILABLE) | `earnings_time_utc` + `event_verified` | OK (live: mix VERIFIED/ESTIMATED, niciodată confirmat fals) |
| BMO/AMC session | cockpit.js:230 | :328 | snapshot | OK |
| Score + score_kind | cockpit.js:231 | :290-294, 334-335 | `trader_score` / `pre_earnings_score` | OK (live: `TRADER_SCORE_NOT_PROBABILITY` — etichetat onest) |
| Data confidence + coverage | cockpit.js:232 | :336-337 | snapshot | OK (live: `NONE` + coverage 56%) |
| Expected move | cockpit.js:233 | :295-297, 338 | `expected_move_pct` / `range_proxy_pct` | OK |
| Historical move + n | cockpit.js:234 | :298-300, 340-341 | `historical_median_move_pct` | OK (2 null-uri → „N/A" onest) |
| IV/RV | cockpit.js:235 | :302-305, 342-343 | `options_snapshot.iv_rv_ratio` | **GOL** (0/20 — Options UNAVAILABLE, coverage gap declarat; onest) |
| Bias + bias_score | cockpit.js:236 | :287-289, 308, 344-345 | `direction_label` + `final_direction_bias_score` (incl. componenta c6 eToro din `e9eda27`) | OK (live: SHORT, −29) |
| Strategy | cockpit.js:237 | :309, 346 | derivat doar pt. eligibili | OK (19 null → „No eligible strategy" onest) |
| AI decision + confidence + model + vârstă | cockpit.js:238 | :285, 347-350 | `ai_decision` din snapshot | OK (1 rând cu glm-5.3-flash:cloud; restul „Not reviewed") |
| Recommendation / risk status | cockpit.js:239 | :307-315, 351-354 | blockers/eligible | OK (toate BLOCKED — onest) |
| Paper-auto trace status | cockpit.js:239 (`trace.status`) | cockpit_service.py:1755-1824 | prefiltru `assess_pre_earnings_candidate` (core/autonomous_candidates.py:246) + blocker-uri runtime (mod/sesiune/kill/readiness) | OK |
| Gate panel: why_no_position, policies | cockpit.js:243 | :1801-1804, 243 | blocker #1 | OK |
| Gate metrics/thresholds (4 perechi) | cockpit.js:245-248 | prefilter `metrics`/`thresholds` | pre_earnings + config execuție | OK (toate cheile prezente live) |
| Gate blockers + stages (8 etape) | cockpit.js:223-226, 250 | :1812-1823 | stare compusă | OK |
| Event source + verified_at + URL | cockpit.js:251 | :331-333 | `event_source_url` (Yahoo) — URL validat `safeHttpUrl`:76-83 | OK |
| Note schedule_counts (7 contoare) | cockpit.js:256 | :1870-1882 | snapshot + ranking | OK (live: 260 light / 253 deep / 173 AI / 7 reported) |
| Notă refresh_policy | cockpit.js:257 | :1883-1890 | config pre_earnings | OK (600s full + 60s scan + max 25 AI) |
| Notă source_freshness | cockpit.js:258 | :1869 → `_pre_earnings_freshness`:97-115 | `status.last_success_time` + vârste input | OK (live: FRESH, 58s) |

### 1.4 Strategy Lab (`renderStrategy`, cockpit.js:337-379)

| Câmp | Frontend file:line | Backend file:line | Sursă | Verdict |
|---|---|---|---|---|
| Summary boxes Champion/Challenger/Shadow/Degraded | cockpit.js:339-344, 371 | cockpit_service.py:1160-1163 | registry lifecycle | OK (toate 0 — onest) |
| Leaderboard (30 rânduri × 10 coloane) | cockpit.js:345-350 | `_strategy_lab`:1098-1118 | `get_strategy_performance` (api/actions_service.py:1267) → StrategyPerformanceCache | OK\* (n=1 cu 11.46R pe locul 1 — zgomotos dar etichetat `unverified`) |
| Leaderboard: Profit factor | cockpit.js:348 | :1107 | cache entries fără `profit_factor` | **GOL** (0/30 → „—"; cablaj OK, date lipsă — P3-1) |
| Lifecycle: Robustness | cockpit.js:362 | :1091 → `_mapping(perf.get("walk_forward"))` | cache walk_forward | **MISCAL (P2-1)** — obiect randat ca pill `[Object Object]` |
| Lifecycle: OOS verdict | cockpit.js:361 | :1090 | `validation_status` | OK (live: `research_only`) |
| Lifecycle: Drift | cockpit.js:364 | :1070-1093 | `detect_drift` (core/research_factory/drift.py) | OK (`NOT_MEASURED` onest) |
| Promotion history | cockpit.js:351-353 | :1119-1122, 1165 | registry events PROMOTE/DEMOTE | OK (0 — mesaj onest) |
| Rollback history | cockpit.js:354-357 | :1126-1151 | registry DEMOTE + PromotionRecordStore ROLLED_BACK | OK (0 — mesaj onest) |
| AI Quant commentary | cockpit.js:369, 377 | :1167-1171 | — (nicio funcție de generare) | OK (EMPTY onest, mesaj explicit) |

### 1.5 Decision Ledger (13 întrebări, `renderLedger`, cockpit.js:498-648) — 30 intrări din 1975

| Câmp | Frontend file:line | Backend file:line | Sursă | Verdict |
|---|---|---|---|---|
| Summary: persistente / rejects / rejected recent / closed trades | cockpit.js:635-640 | cockpit_service.py:910-921 (`_ledger_evidence`:597-620) + `capital.closed_trades` | SQLite ledger + capital | OK (1975 / 1535 / 30·30 / 226) |
| „Why positions are not opening" (rankare blocker-e) | cockpit.js:505-518, 641 | client-side din `risk.checks[].passed=false` | ledger | OK\* (vezi P3-7: 0 blocker-i de risc, cauza reală e în Q5) |
| Avertisment guvernanță (0 aprobate) | cockpit.js:515-521, 642 | `ledger.governance` cockpit_service.py:1833-1840 | registry + constituție | OK (nu se afișează: require_approved_strategy=false) |
| Q1 What happened | cockpit.js:525, 611 | `what_happened` (reconciliere :808-813) | ledger + poziții | OK |
| Q2 Why no position / Position status | cockpit.js:612 | `risk.checks` + `executed` (reconciliere :782-845) | ledger + capital | OK („No order was sent…" pentru rejected) |
| Q3 What data existed (9 fapte) | cockpit.js:444-456, 613 | `data_existed` (payload `data_evidence`) | punct-in-time snapshot | OK (30/30 populate) |
| Q4 Which strategy | cockpit.js:614 | `strategy` + `data_existed.strategy_status` | ledger | OK |
| Q5 Which AI model | cockpit.js:457-463, 615 | `ai_model` | ledger | OK (text onest pt. available=false) |
| Q6 Which prompt | cockpit.js:616 | `prompt` {version, chars, sha256, preview} | ledger | OK (trade_review_v1, 20273 chars) |
| Q7 What did AI propose | cockpit.js:526-543, 617 | `ai_proposal` | ledger | OK\* (fallback NO_TRADE/0.0 randat ca propunere — P3-8) |
| Q8 What did critic say | cockpit.js:544-576, 618 | `critic` | ledger | OK (branch-uri complete; live: critic_not_run → text corect) |
| Q9 Did the AI debate itself | cockpit.js:577-601, 619 | `debate` | ledger (doar pre-earnings) | OK (0/30 → mesaj explicativ onest) |
| Q10 What did risk say (18 reguli mapate) | cockpit.js:381-443, 620 | `risk.checks` (750 intrări verificate: check/passed/actual/limit/detail) | ledger | OK |
| Q11 What did capital allow | cockpit.js:464-478, 621 | `capital_allowed` | ledger + reconciliere sizing | OK |
| Q12 What was executed | cockpit.js:479-496, 622 | `executed` (reconciliere cu fills ulterioare `_reconcile_ledger_rows`:664-845) | ledger + poziții paper | OK |
| Q13 What happened afterward | cockpit.js:602-609, 623 | `afterward` + `reflection` | ledger | OK (0/30 → „No afterward evaluation…" onest) |
| Point-in-time replays | cockpit.js:630-631, 646 | TOP-LEVEL `decision_replay` (cockpit_service.py:1959 — fix 2026-09-03 care ÎNCĂ ȚINE) | store source=REPLAY | OK (2 intrări live) |
| Model Lab (per role) | cockpit.js:632-633, 647 | TOP-LEVEL `model_lab` (:1960) → `get_model_lab` (api/ledger_service.py) | ledger MODEL_LAB | OK (EMPTY onest) |

### 1.6 Research Factory (`renderResearch`, cockpit.js:651-680)

| Câmp | Frontend file:line | Backend file:line | Sursă | Verdict |
|---|---|---|---|---|
| Worker Running/Disabled + last success | cockpit.js:670-673 | `_research_section`:1212, 1295-1303 | `agents.agents.strategy_research` runtime | OK (live: Running, 19:15:47) |
| Observations count + tabel activitate (6 coloane) | cockpit.js:654-656, 672 | :1214-1241 | ExperimentRegistry → fallback perf rows | OK (8 rânduri) |
| Auto-cycle ON/OFF + last run note | cockpit.js:657, 666-668, 674 | :1254-1268 | core/research_factory/scheduler.py | OK (live: ON, ultimul run 17:16, 3 slăbiciuni/3 experimente) |
| Autoresearch KEEP/DISCARD + current best | cockpit.js:659-662 | :1269-1290 | ExperimentRegistry status_counts | OK (live: 0 KEEP / 188 DISCARD / 1 open, fără best) |
| Capability map (5 capabilități) | cockpit.js:678 | :1242-1248 | hardcodat structural (faze, nu valori de trading) | OK (decorativ-declarativ, nu date de piață) |
| Toggle auto-cycle | cockpit.js:1217-1220 | POST /api/research/auto-cycle/{start,stop} | api/main.py | OK (ne-testat: POST) |

### 1.7 System Health (`renderHealth`, cockpit.js:870-887)

| Câmp | Frontend file:line | Backend file:line | Sursă | Verdict |
|---|---|---|---|---|
| Overall pill | cockpit.js:877 | cockpit_service.py:1963 | agregare `_system_health`:1410-1650 | OK (HEALTHY; componente inactive NU contează) |
| Coverage gaps note | cockpit.js:873-875 | `system_health.coverage_gaps` | options lipsă | OK (live: `Options`) |
| 15 tile-uri (label/status/message/evidence/checked_at) | cockpit.js:879-886 | `_service`:137-157; servicii la 1424-1650 | backend SQLite, scheduler, market_provider (fallback cross-proces :1445-1456), earnings, options, news, sec, ai_providers (probe :1370-1408), ml_model, mlflow, rd_agent, paper_broker, live_broker, research_worker | OK (live: 12 HEALTHY, options UNAVAILABLE, sec+live_broker NOT_CONFIGURED) |
| Buton Train/Retrain ML (doar tile ml_model) | cockpit.js:881-884 → `trainMlModel`:1308-1351 | POST /api/ml/train | api/main.py | OK (ne-testat: POST) |

### 1.8 Coding Agent (`loadRepairAgent`:687-700 + `renderRepairAgent`:702-868, endpoint dedicat)

| Câmp | Frontend file:line | Endpoint | Backend file:line | Sursă | Verdict |
|---|---|---|---|---|---|
| Agent state / Working-Idle / Applied / No-defect / Failed / Proposals / Owner actions (7 boxes) | cockpit.js:848-856 | GET /api/development/repair-agent (api/main.py:3099) | core/development_factory/watchdog.py (RepairCaseStore) | SQLite repair cases | OK (live: Idle 43m; 27 applied; 49 failed; 0 propuneri; diag onest „generation unavailable (all providers failed)") |
| Card „Right now" + watchdog note | cockpit.js:858-860 | idem | `watchdog` {running, interval 900s, 10 cicluri, max 3} | watchdog runtime | OK |
| Owner actions needed | cockpit.js:861 | idem | `owner_actions` (6 în listă) | detectoare configurare | OK |
| Proposals + Approve | cockpit.js:799-809, 1239-1243 | idem + POST approve | watchdog | — | OK (0 propuneri → mesaj onest) |
| Latest case journey (4 pași) | cockpit.js:810-824 | idem | `cases[0]` {evidence, attempts, review_chain, outcome} | SQLite | OK |
| History tabel 20 cazuri + detalii expandabile (5 secțiuni) | cockpit.js:825-842, 763-798 | idem | `cases` cu diagnostic/istoric/încercări/review-uri/post_check | SQLite | OK (toate cheile consumate există în payload) |
| Watchdog toggle + Run/Retry caz | cockpit.js:845-846, 1244-1284 | POST repair-watchdog/{start,stop,run}, POST repair-cases/{id}/run | api/main.py | — | OK (ne-testat: POST) |

### 1.9 Chei din payload NEconsumate de frontend (informatic, nu defect)

| Cheie | Producător | Observație |
|---|---|---|
| `continuous_scan` | cockpit_service.py:879-896, :1961 | UI nu o citește (folosită de Pre-Earnings tab/scanner) |
| `sections.autonomous_repair` | :998-1024, :1913 | panoul repair folosește endpoint dedicat; secțiunea rămâne greutate moartă (~KB) |
| `header.reduce_fraction_pct` | :1951 | nerandat în cockpit |
| `opportunities[].ai_critic` | :478 | nerandat |

---

## 2. Defecte (cu dovezi live)

### P1 — nu există

Nu s-a identificat niciun câmp fals randat, niciun blocare de tab, nicio scurgere de date între surse. Stările degradate sunt toate oneste.

### P2-1 — Coloana „Robustness" (Lifecycle) afișează `[Object Object]`
- **UI:** `frontend/cockpit.js:362` — `<td>${statusPill(record.robustness || 'UNAVAILABLE')}</td>`; `statusPill` (cockpit.js:91-94) face `String(obj)` → `[OBJECT OBJECT]` → pill cu textul „[Object Object]" și clasă CSS invalidă `is-[object object]`.
- **Backend:** `api/cockpit_service.py:1091` — `"robustness": _mapping(perf.get("walk_forward")) or None` — trimite obiectul `{avg_train_expectancy_R, avg_test_expectancy_R, overfit_penalty, …}`.
- **Dovadă live:** toate cele 8 records au `robustness` = obiect (ex. `{"avg_train_expectancy_R": 0.0, …, "overfit_penalty": 80.0, …}`). În runda 1 coloana arăta „UNAVAILABLE" doar pentru că perf era gol; acum că walk-forward există în cache, garbage-ul devine vizibil. Defect latent din commit-ul inițial `b356d85`, declanșat de apariția datelor — nu e regresie din commit-urile recente, dar e vizibil ACUM.
- **Reparație minimală:** fie backend trimite un șir de status (ex. `ROBUST`/`WEAK` derivat din `overfit_penalty`), fie frontend randează `robustness` ca text-cheie:valoare (ex. `fmtNumber(record.robustness?.avg_test_expectancy_R, 3)`).

### P2-2 — KPI „AI status: HEALTHY" contradictă ledger-ul din același tab (proba de health = reachability-only)
- **UI:** cockpit.js:171 — pill `HEALTHY` verde, „Global · gpt-oss:120b-cloud".
- **Backend:** cockpit_service.py:1735-1745, 1931-1936 — `ai_verified` vine din stream-ul `ollama_availability` (nivel VERIFIED); căderea în备用 `_probe_primary_ai_provider` (:1370-1408) face doar `GET {base}/api/tags` (listare modele), cu comentariu explicit în cod: „an honest UNVERIFIED is better than burning a cloud call".
- **Dovadă live (aceeași cerere /api/cockpit):** `header.ai.status="HEALTHY"`, dar în `decision_ledger.items` **30/30** decizii recente au `ai_model.available=false`, `response_fallback_used="all_unavailable_blocked"`, `ai_proposal={action:"NO_TRADE", confidence:0.0, thesis:""}` (ultima decizie 19:13:41 UTC). Consistent, Coding Agent raportează `last_outcome=FAILED`, motiv „coding agent generation unavailable (all providers failed)" (18:49 UTC).
- **Impact:** operatorul vede verde la KPI, dar pipeline-ul autonom nu consultă AI de deloc — blocajul real e vizibil doar în Ledger/Coding Agent. Sonda ar trebui să distingă „server reachable" (UNVERIFIED/DEGRADED) de „generation verified" (HEALTHY, ex. ping de generare 1-token la TTL lung), sau tile-ul să includă ultimul `response_fallback_used` din ledger.

### P3 (ordine după impact)

1. **P3-1 Profit factor mereu „—"** — leaderboard 0/30 null (`get_strategy_performance` propagă None; intrările din StrategyPerformanceCache nu conțin cheia). Cablaj corect (actions_service.py:1281 → cockpit_service.py:1107 → cockpit.js:348); gap de date. Colț de coloană moartă atâta timp cât scriitorul cache-ului nu salvează profit_factor.
2. **P3-2 IV/RV mereu „N/A · unavailable"** — 0/20 rânduri cu opțiuni; coerent cu tile `options` UNAVAILABLE + coverage gap „Options" (onest, dar o coloană întreagă permanent vidă pe acest deployment).
3. **P3-3 Cardul „Sentiment shifts" nu arată niciodată un shift** — `shift_state: "UNAVAILABLE"` hardcodat în cockpit_service.py:581 (împreună cu `prior_tone/shift: None`); cardul e de fapt „tone current", numele promite ceva ce nu există.
4. **P3-4 Calibrare impact știri + riscuri 100% știri** — Major news: **12/12 CRITICAL** (inclusiv comentarii eToro de tip „Trump asked on Truth Social…"), iar cardul Current risks are 10/10 intrări `NEWS_CRITICAL/NEWS_*` — un tweet macro ajunge „CRITICAL risk" pe NVDA/MSFT/AAPL. Calibrarea e în amonte (news agent → `dashboard_news_items`), dar cockpit-ul o amplifică în 2 carduri.
5. **P3-5 `aria-busy="true"` hardcodat și niciodată curățat (runda 1 — ÎNCĂ DESCHIS)** — `frontend/index.html:257` `#signals-feed` (în `#view-home`, sub cockpit): `app.js` nu conține niciun apel `aria-busy` (grep = 0); la fel `#capital-trade-cards` (:982) și `#news-page-feed-content` (:1207). Doar agents.js își curăță al său (:1033/:1085). Cockpit-ul propriu-zis nu folosește aria-busy (folosește role=status).
6. **P3-6 Fereastră de stale nelimitată în SWR** — `api/main.py:1955-1963` `_endpoint_payload_get_stale` întoarce bytes fără limită de vârstă; dacă rebuild-ul eșuează repetat (ex. lock news), endpoint-ul servește veșnic snapshot-ul vechi. Singurul indicator e textul „Snapshot <data>" (cockpit.js:957) — nu există badge de stale și nici max-age. (Live: rebuild-urile funcționează — generated_at 19:24:26 → 19:29:15 între două fetch-uri — deci riscul e doar la defecțiuni repetate.)
7. **P3-7 Diagnosticul „Why positions are not opening" nu explică cauza reală** — blockerSummary (cockpit.js:516-518) afișează „No failed Risk checks appear in the visible window" deși 30/30 decizii recente au fost respinse; cauza reală (AI indisponibil) apare doar în răspunsul Q5. În plus, `risk.accepted=true` + status `rejected` (AI-ul blochează după acceptul riscului) face ca Q10 să spună „Risk accepted the proposal" — corect factual, dar confuz fără agregarea motivului de respingere.
8. **P3-8 Zgomot leaderboard + fallback NO_TRADE randat ca propunere AI** — (a) rând n=1 cu expectancy 11.459R pe rank 1 (sortare `cockpit_service.py:1111-1118` pune verificat-le înainte, dar toate sunt `unverified`); (b) când AI e indisponibil, `ai_proposal`-ul de fallback (NO_TRADE, confidence 0.0) e randat la Q7 ca „No Trade" ca și cum AI ar fi propus — atenuează confuzia doar faptul că `conf>0` ascunde 0%.

*Observație de performanță (nu defect):* manual Refresh (force `fresh=true`) construiește sincron (main.py:2082-2087) — la un rebuild rece poate bloca până la timeout-ul clientului de 60s (cockpit.js:1058). SWR-ul acoperă doar polling-ul normal.

---

## 3. Status vs runda 1 (recheck P2/P3 din `docs/AUDIT_2026-09-05.md`)

| Areviz runda 1 | Status runda 2 | Dovezi |
|---|---|---|
| P2 „cache-bust stale" | **REZOLVAT** | SWR `80180a2`: TTL 60s + single-flight `_kick_cockpit_rebuild` (main.py:2027-2055); live: fetch 2 în 0.19s, `unchanged:true` 47 bytes; gzip activ (GZipMiddleware main.py:933) — 480KB→compresat |
| P3 „aria-busy niciodată curățat" | **ÎNCĂ DESCHIS** | index.html:257 hardcodat `aria-busy="true"`; app.js = 0 referințe (vezi P3-5) |
| P3 „/api/state default lent" | **REZOLVAT** | main.py:1847-1853 (default `live_prices=false`) + SWR cu TTL 45s/10s (main.py:1856-1892) + warmup la startup (main.py:321-350); live: **9.7 ms** la primul și al doilea poll |
| P3 „cockpit poll rarely short-circuits" | **REZOLVAT (mecanic)** | `_cockpit_stable_version` (main.py:5299-5324) elimină generated_at/now_et/checked_at și cuantifică `*_age_seconds` pe minute; live: `{"unchanged": true, "version": "cb6411c7236923e0"}`. Rezerva:MTM crypto schimbă conținutul la fiecare rebuild → poll-urile peste TTL tot aduc payload complet (~1/min la cadența 30s) |
| P2 fix kill-switch (dublă armare + GET control înainte de POST) | **ȚINE** | cockpit.js:1109-1165 identic; `GET /api/execution/control` live: kill_switch_active=false, mode PAPER_AUTO, readiness ready |
| P2 fix decision_replay/model_lab la nivel top | **ȚINE** | cockpit_service.py:1956-1960; live: `decision_replay.state=SUCCESS` (2 itemi), `model_lab.state=EMPTY` |
| Întrebarea misiunii: „servește vreo secțiune date permanent stale?" | **Risc, nu realitate** | SWR servește stale nelimitat DOAR dacă rebuild-ul eșuează repetat (P3-6); în prezent rebuild-urile reușesc și toate timestamp-urile sunt proaspete (max: narațiunea 19:16 vs snapshot 19:24) |

**Regresii din commit-uri de la runda 1:** NEIDENTIFICATE. `80180a2`/`7f26fd2` ating doar stratul de cache din `api/main.py` (comportament verificat live); `c7edafc` nu atinge cockpit; `e9eda27` schimbă doar producătorul pre-earnings (câmpurile `bias`/`bias_score` din matrice rămân populate corect). Din `80180a2` până la HEAD, niciun commit nu atinge `frontend/cockpit.js` sau `api/cockpit_service.py`.

---

## 4. Recomandări (priority order)

1. **P2-1:** randează `robustness` ca scară de status — fie backend (cockpit_service.py:1091) adaugă `robustness_status` derivat din `overfit_penalty`/`avg_test_expectancy_R`, fie frontend (cockpit.js:362) afișează `record.robustness?.avg_test_expectancy_R` numeric, cu fallback la pill doar pentru șiruri.
2. **P2-2:** adaugă în `_system_health`/`header.ai` un verdict de **generare** (ex. sondă de generare 1-token la TTL 5-15 min sau citirea ultimului `response_fallback_used` din ledger); KPI-ul să treacă în DEGRADED când ultimele N decizii au `available=false`, chiar dacă `/api/tags` răspunde.
3. **P3-6:** impune `max_stale_seconds` (ex. 10× TTL) în `api_cockpit`: peste prag, întoarce payload cu `state:"STALE"` + badge în UI în loc de bytes vechi la nesfârșit.
4. **P3-7:** extinde blockerSummary (cockpit.js:505-518) să agregheze și `ai_model.available===false` ca „blocker" („AI generation unavailable — N/30 recent"), nu doar `risk.checks` eșuate.
5. **P3-3:** redenumește cardul „Sentiment shifts" în „Market tone" până când `shift_state` are producător real, sau implementează comparația cu snapshot-ul anterior.
6. **P3-4:** recalibrare impact în news agent (prag CRITICAL pe surse sociale) — rezolvă simultan cardurile Major news și Current risks.
7. **P3-1:** asigură că scriitorul StrategyPerformanceCache salvează `profit_factor` (coloana leaderboard devine funcțională automat).
8. **P3-5:** curăță `aria-busy` în `renderSignalsFeed`/capital/news din app.js (sau elimină atributul hardcodat).
9. **P3-8:** în leaderboard, despărțește rank-ul pe `sample_size>=N` sau afișează explicit „n=1 — zgomot statistic"; la Q7, când `ai_model.available===false`, randează „No usable AI response — pipeline defaulted to NO_TRADE" în locul propunerii de fallback.

---

## 5. Anexă — probe live (doar GET, 2026-09-05 19:24-19:31 UTC)

- `GET /api/cockpit` → 200, 480 KB, 0.17-0.30s, `state=SUCCESS`, `source_errors={}`, `generated_at` 19:24:26 → 19:29:15 (rebuild SWR funcțional); gzip confirmat la a doua cerere.
- `GET /api/cockpit?unchanged=true&if_version=cb6411c7236923e0` → `{"unchanged": true, "version": "cb6411c7236923e0"}` (47 bytes, 0.19s).
- `GET /api/state` (default, x2) → 200, 9.7 ms / 9.4 ms (cache SWR + warmup).
- `GET /api/execution/control` → 200, 0.36s: `kill_switch_active=false, mode=PAPER_AUTO, readiness.ready=true` — coerent cu header-ul cockpit.
- `GET /api/development/repair-agent` → 200, 48.5 KB, 0.07s: toate cheile consumate de `renderRepairAgent` prezente (activity/watchdog/summary/cases[20]/proposals[0]/owner_actions[6]/policies).
- Validare câmp: script Python pe payload — 100% din cheile consumate de cockpit.js prezente; anomalii: `profit_factor` 0/30, `iv_rv_ratio` 0/20, `robustness` obiect 8/8, ledger 30/30 `ai_model.available=false`.
