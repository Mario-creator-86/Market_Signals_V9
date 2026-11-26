# AUDIT FORENSIC V2 — TAB 11: AGENTS (planul de control al agenților, `#view-agents`)

**Data:** 2026-09-05
**Metodă:** re-audit UI↔backend câmp cu câmp; probe LIVE GET-only pe `http://127.0.0.1:8003` (5 cereri: `/api/agents`, `/api/agents/control-plane`, `/api/execution/control`, `/api/agents/risk/pipeline-status`, `/api/bot/status` — toate 200); cod citit, nimic mutat; dump-uri salvate doar în temp sistem.
**Referință round 1:** `docs/audit_tabs/TAB_11_AGENTS.md` (2026-09-04) + scripturile din `218009f` (`scripts/test_agents_probe.py`, `scripts/test_agents_testclient.py`, `scripts/test_agents_mutations.py`).

---

## VERDICT

**COMPLIANT — 0 P1, 0 P2, 5 P3.** Control-plane-ul este real, fail-closed și coerent cap-coadă: fiecare field randat în UI tracează la un producător backend verificabil, iar live-payload-ul confirmă toate cheile consumate. Nu am găsit niciun câmp FAKE sau DEAD-BACKEND. Cele 3 invariante de siguranță cerute sunt prezente în cod (409 pe dezactivarea `execution`/`risk` cu botul pornit, 410 pe `PATCH /api/execution/control`, kill-switch fail-closed în readiness). Defectele rămase sunt de calitate a afișajului (P3), iar două dintre P3-urile din round 1 sunt **în încă deschise, nerezolvate**.

Statistici live (2026-09-05, ~20:52 UTC): 14/14 agenți `enabled=true`, `running=true`, `lifecycle_state=running`; `managed_by=engine` pentru exact {execution, risk, scanner, sentiment} și `api` pentru ceilalți 10 — coerent cu `ENGINE_MANAGED_AGENTS` (api/agent_runtime_service.py:17) și `LOCAL_LOOP_AGENTS` (api/agent_runtime_service.py:76-89). `liveness_source=engine_heartbeat` pe cei 4 agenți de motor; fișierul `data/agent_status_engine.json` avea 0,5s vechime la verificare. Bot: `running=true, mode=paper, circuit_breaker_active=false, min_edge_score=90`; readiness `ready=true, reasons=[]`, reconciliere `HEALTHY`, kill-switch inactiv.

---

## 1. ARHITECTURA REALĂ DE RENDARE (constatare structurală nouă vs round 1)

Tab-ul are **DOUĂ randere concure definite pe același nume**:

| Varianta | Fișier:linie | Endpoint-uri consumate | Stare |
| :--- | :--- | :--- | :--- |
| **Activă** | `UI.renderAgents` — frontend/agents.js:1028-1088; `Actions.toggleAgent` — frontend/agents.js:1174-1209 | `/api/agents/control-plane`, `/api/agents/risk/pipeline-status`, `/api/execution/control`; toggle pe `POST /api/agents/{name}/enabled` | **ACTIVĂ** |
| **Legacy (umbră)** | `UI.renderAgents` — frontend/app.js:19362-19566; `Actions.toggleAgent` — frontend/app.js:19568-19616 | `/api/config`, `/api/agents`, `/api/premarket`, `/api/pre-earnings`; toggle pe `POST /api/config` | **MOARTĂ (shadowed)** |

Ordinea de încărcare în frontend/index.html: `app.js?v=38.26` la linia 2731, `agents.js?v=2.7` la linia **2735** → `agents.js` suprascrie ambele funcții la încărcare; apelantul din router (`frontend/app.js:12966-12968`, `if (viewId === 'agents') UI.renderAgents();`) rezolvă proprietatea la runtime și ajunge la varianta din `agents.js`. Markup-ul static (`#agents-master-panel`, `#agents-kpi-grid`, `#agents-readiness-panel`, `#agents-opportunity-monitor`, `#agents-grid`) e în `frontend/index.html:555-627` și e consumat doar de varianta activă.

Consecințe (vezi P3-3): renderer-ul legacy numără doar 10 agenți (lipsesc research_factory, coding_agent, critic_agent, ui_inspector — frontend/app.js:19453-19464) și, critic, toggle-ul lui scrie prin `POST /api/config` (frontend/app.js:19601-19606), **fără** poarta de 409 din `set_agent_enabled`. Astăzi e cod mort; dacă ordinea scripturilor s-ar inversa vreodată, invarianta de siguranță ar dispărea din UI.

`GET /api/agents` (api/main.py:2216-2237) este sănătos (200, 14 agenți, același `snapshot_agent_runtimes` ca control-plane), dar **singurul consumator UI este renderer-ul mort** — în tab-ul curent e efectiv endpoint de diagnostic (consumat de `scripts/test_agents_probe.py:5`).

---

## 2. TABEL CÂMP-CU-CÂMP (UI → payload → endpoint → producător → sursă)

Legenda clasificare: **OK** = real și corect calibrat; alte clasificări doar unde s-a constatat defect.

### 2.1 Panourile de sus

| Field UI (fișier:linie frontend) | Cheie payload | Endpoint | Producător backend (fișier:linie) | Sursa finală | Clasificare |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `#agents-last-updated` (agents.js:1073-1074) | — (ceas client) | — | — | ceasul browserului | OK |
| Stare master „Bot running/stopped" (agents.js:558-560) | `bot.running` | GET /api/agents/control-plane | `build_control_plane_payload` (api/agents_service.py:1247-1307) ← `BotController.get_control_status` (core/bot_controller.py:168-191, apelat din api/main.py:2352-2355) | instanța singleton BotController + `config.bot.enabled` | OK (live: running) |
| Subtitlu mod master (agents.js:555) | `bot.mode` / `config.bot.mode` | același | același | `config.bot.mode` | OK (live: PAPER) |
| Copy master „qualified signals may pass…"/„monitoring only" (agents.js:537-541) | `bot.running` + `state.executionControl.readiness.ready` | control-plane + /api/execution/control | `get_execution_control` (api/execution_service.py:69-137) ← `evaluate_paper_entry_readiness` (core/execution/policy.py:41-95) | config autonomy/risk_constitution + reconciliere broker | OK |
| Butoane Start/Stop bot (agents.js:564-571) | acțiune `POST /api/bot/start\|stop` | mutație (audit cod; Agents actions 1101-1131) | api/main.py (api_bot_start/stop) → BotController | — | OK (blocare client dacă `!engine_running \|\| !execution \|\| !risk`, agents.js:536) |
| KPI „Market engine" (agents.js:583) | `bot.engine_running` | control-plane | `build_control_plane_payload` → `bool(engine_running)` (agents_service.py:1261) ← `is_engine_running()` (api/engine_service.py:301-304) | procesul motor (`_engine_process.poll()`) | OK (live: Running) |
| KPI „Worker fleet X active / Y enabled" (agents.js:584) | derivat din `cards[].status.group` + `cards[].enabled` | control-plane | `snapshot_agent_runtimes` (api/agent_runtime_service.py:293-345) | heartbeat engine + singletons locale | OK (live: 14/14) |
| KPI „Circuit breaker" (agents.js:585) | `bot.circuit_breaker_active` | control-plane | core/bot_controller.py:158,179 (setat la 279,289) | contor drawdown/daily-loss al controllerului | OK (live: Ready) |
| KPI „Entry threshold" (agents.js:586) | `bot.min_edge_score` | control-plane | core/bot_controller.py:165,180 ← `config.bot.min_edge_score` | config.json | OK (live: 90 / 100) |
| Readiness 7 itemi (agents.js:598-606) | `bot.engine_running`; `config.agents.execution/risk.enabled`; `config.agents.execution.autonomous_candidates_enabled`; `bot.circuit_breaker_active`; `bot.mode`; `executionControl.readiness.ready` | control-plane + /api/execution/control | vezi sus; `autonomous_candidates_enabled` confirmat în config live | config.json | OK (live: 7/7 ready) |
| Monitor oportunități — status ciclu (agents.js:653, 626-635) | `result.cycle.status` | GET /api/agents/risk/pipeline-status | `api_risk_manager_cycle_status` (api/main.py:3247-3258) ← `core.autonomous_control.get_autonomous_cycle_status` | sidecar `data/autonomous_cycle_status.json` scris de ciclul motorului | OK (live: „No eligible setup" din `no_eligible_candidates`) |
| — „Source observations" (agents.js:740, 663-666) | `sources.scanner.observation_count` (100), `watchlist.observation_count` (38), `pre_earnings.evaluated` (25) | pipeline-status | același | același | OK |
| — „Candidates X / reviewed / submitted" (agents.js:741) | `cycle.candidate_count` (8), `reviewed` (0), `submitted` (0) | pipeline-status | același | același | OK |
| — „Scan cap / bucket" (agents.js:742) | `cycle.limits.scanner_symbols_per_bucket` (50), `limits.watchlist_symbols` (100) | pipeline-status | același ← `agents.execution.autonomous_scanner_max_symbols_per_bucket` | config.json (validat live: 50/100, identic cu /api/execution/control `autonomous_cycle_limits`) | OK |
| — „Cycle gate" (agents.js:743) | `limits.candidate_reviews_per_cycle` (0→„Unlimited"), `trade_submissions_per_cycle` (1) | pipeline-status + fallback `executionControl.autonomous_cycle_limits` (agents.js:660-662) | același | config.json | OK |
| — „Last completed" + trigger (agents.js:744, 658-659) | `updated_at`, `trigger` | pipeline-status | același | sidecar | OK (live: „Just now" / „scheduled") |
| — „Allocation lane" (agents.js:745, 643-644) | `allocation.target_bucket` (pre_earnings), `allocation.normalized_pct[target_bucket]` (33.33), `actual_buckets` | pipeline-status | același | sidecar | OK |
| — Rânduri Scanner/Watchlist/Pre-Earnings (agents.js:677-693) | `sources.*.enabled/observation_count/candidate_count/status/evaluated` | pipeline-status | același | sidecar | OK |
| — Secțiune „Cross-sectional momentum" (agents.js:706-753) | `sources.cross_sectional_momentum.*` | pipeline-status | același | sidecar | **P3-4** (surse dezactivate se afișează „Awaiting cycle"/„0 eligible symbols", nu „disabled"; live: `enabled=false`) |
| Buton „Run signal cycle" (agents.js:733-736, 674-676) | acțiune `POST /api/agents/risk/run-cycle` | mutație (audit cod) | api/main.py:3216-3244 — 409 dacă bot oprit/non-paper/readiness false | — | OK (gardă triplă în cod) |

### 2.2 Cardul de agent (×14, `renderCards` agents.js:774-864; date din `buildCards` 757-772)

| Field UI | Cheie payload (control-plane `agents[key]` / `config.agents[key]`) | Producător backend | Sursa finală | Clasificare |
| :--- | :--- | :--- | :--- | :--- |
| Titlu, categorie, icon, descriere (agents.js:813-816, 848) | hardcodat în `AGENTS` (agents.js:23-116) | — | — | OK (static, 14 intrări) |
| Toggle `agent-toggle-{key}` (agents.js:840-844) | `config.agents[key].enabled` | `build_control_plane_payload` → `agents_config` (agents_service.py:1269-1271) | config.json `agents.{key}.enabled` | OK |
| Toggle blocat pe execution/risk cât timp botul rulează (agents.js:31,40 `lockedWhenRunning`; 789) | `bot.running` | BotController | — | OK; backend-ul Reinforcează cu 409 (vezi §3) |
| Status pill (agents.js:847) — stare calculată (agents.js:483-526) | `running`, `lifecycle_state`, `last_error`, `initializing`, `accepting_new_entries` | `snapshot_agent_runtimes` (agent_runtime_service.py:293-345); `accepting_new_entries`/`operating_mode` injectate pentru execution/risk în agents_service.py:1264-1275 | heartbeat `data/agent_status_engine.json` (agenți motor) / `get_runtime_status()` pe singletons / `RepairWatchdog.status()` (coding/critic) / `auto_cycle_status()` (research_factory) | OK — mașina de stări coherentă; stările produse: `disabled/running/starting/stopped/error/test_suppressed/waiting_dependency` (agent_runtime_service.py:121-135,369,451); pauses benigne filtrate (agents.js:118) |
| Metrici detaliu stânga/dreapta (agents.js:805-806 ← `configDetail` 169-207) | `config.agents.execution.mode/max_open_trades`; `risk_constitution.active.max_daily_loss_pct/max_risk_per_trade_pct`; `agents.scanner.interval_sec/min_volume_24h`; `agents.sentiment.provider/min_sentiment_score`; `config.premarket.refresh_seconds/cache_ttl_seconds`; `config.pre_earnings.refresh_seconds/lookahead_days`; `config.news.news_refresh_minutes`; `config.strategy_v2.research_agent.interval_sec` | control-plane `config` block (agents_service.py:1269-1290) | config.json | OK pentru 8 agenți (toate cheile validate în payload-ul live); **P3-1 pentru ceilalți 6** (fallback „Last run: On demand / Mode: Cache-first", agents.js:206) |
| Metrică „Runtime" (agents.js:790-796, 800) | `runtime.last_run`, `initializing`, `running` | `snapshot_agent_runtimes` ← metrics din `snapshot_agent_status` (core/observability.py:567) + runtime | observabilitate locală / singleton | OK; agenții motor arată onest „Engine managed" (fără `last_run` în API-process — confirmat live: `last_run=null` pentru execution/risk/scanner/sentiment) |
| Metrică „Next run"/„Duration" (agents.js:797-801) | `runtime.next_run_in`, `avg_duration_ms`/`last_run_duration_ms` | idem | idem | OK (live: trade_radar 300s, economic_calendar 3600s, premarket 300s, pre_earnings 600s, strategy_research 900s); **contrazis** de P3-1 pe trade_radar/economic_calendar |
| Notă eroare inline (agents.js:850) | `runtime.last_error` (≠ pauses benigne) | agent_runtime_service.py:151-152,218-220,242-244,276-278; `_loop_wrapper` trunchiază la 300 char (agents/base_agent.py — claim round 1 re-verificat structural) | erorile agentului | OK |
| Buton run UI Inspector (agents.js:820-825) | acțiune `POST /api/ui-inspector/run` | mutație | api/main.py → `UiInspectorAgent.inspect_all_tabs` | OK |
| Buton run Research Factory (agents.js:826-831) | acțiune `POST /api/research/auto-cycle/run` | mutație | api/main.py → `research_factory_service.auto_cycle_run_once` | OK |
| Buton configurare Risk (agents.js:832-838) | deschide `riskEditorHtml` (agents.js:352-475) | — | — | OK |
| — Status constituție + fingerprint (agents.js:399-405, 373-376) | `config.risk_constitution.status/manager_fingerprint` | `get_risk_constitution_settings` (apelat în agents_service.py:1290) | config.json + hash-ul regulilor confirmate | OK (live: status `active`, fingerprint `660b7371…`, 16 câmpuri schema + 27 trade policy, `runtime_drift: []`) |
| — Profiluri (agents.js:300-349) | `risk_constitution.profiles.items[].{id,label,description,values,tone}`, `profiles.status/source/error` | idem | `risk_constitution_profiles.json` | OK (live: `status: ready`) |
| — Kill switch „Stop/Resume new entries" (agents.js:446-449) | `state.executionControl.kill_switch_active` | GET /api/execution/control → `get_execution_control` (execution_service.py:105) | `config.autonomy.kill_switch_active` | OK (live: released) |
| — „Broker reconciliation" + readiness copy (agents.js:442-443) | `executionControl.readiness.ready/reasons`, `reconciliation.status` | execution_service.py:72-81,133,135 ← `evaluate_paper_entry_readiness` (core/execution/policy.py:41-95) + `broker.store.get_reconciliation()` | config + DB broker paper | OK (live: ready=true, reasons=[], HEALTHY) |
| Salvare constituție (agents.js:1384-1394) | acțiune `PATCH /api/agents/risk/constitution` | mutație | api/main.py:2433-2456 → `set_risk_constitution` | OK (audit cod) |

### 2.3 Validarea invariantelor de siguranță (audit COD — nu am executat mutații live)

| Invariantă | Dovadă în cod | Status |
| :--- | :--- | :--- |
| Kill-switch schimbă readiness | `set_kill_switch` scrie atomic `autonomy.kill_switch_active` (api/execution_service.py:140-152); `evaluate_paper_entry_readiness` adaugă motivul „STOP AUTONOMOUS TRADING is active" și `ready=false` (core/execution/policy.py:49-53, 87); UI-ul re-rotește `renderAgents({force:true})` după toggle (agents.js:1149-1151) | **CONFIRMAT în cod** (și în `scripts/test_agents_mutations.py:15-23`, rulat de round 1 cu rezultat 200/stopped→ready=false→released) |
| Dezactivarea execution/risk cu botul pornit se refuză | `REQUIRED_WHILE_RUNNING = {execution, risk}` + `RequiredAgentRunningError` (api/agents_service.py:56,1325-1327) → HTTP 409 (api/main.py:2396-2400); UI blochează suplimentar toggle-ul (agents.js:789,841) | **CONFIRMAT în cod** |
| `PATCH /api/execution/control` → 410 | api/main.py:2471-2481, mesaj „Execution controls moved to Risk Manager…" | **CONFIRMAT în cod** (round 1 l-a verificat live: 410) |
| Rollback config dacă tranziția runtime eșuează | `set_agent_enabled` reface `agents.{key}.enabled` la valoarea anterioară pe excepție (api/agents_service.py:1340-1353) | CONFIRMAT în cod |

### 2.4 Coerență live lifecycle/managed_by (14 agenți, GET /api/agents)

- `managed_by=engine` + `liveness_source=engine_heartbeat`: execution, risk, scanner, sentiment — exact `ENGINE_MANAGED_AGENTS` (agent_runtime_service.py:17). Heartbeat-urile din `data/agent_status_engine.json` erau proaspete (<1 min): execution 20:56:38, risk 20:55:42, scanner 20:56:37, sentiment 20:55:52.
- `managed_by=api`: news_tab, economic_calendar, trade_radar, premarket_scanner, pre_earnings, strategy_research, research_factory, coding_agent, critic_agent, ui_inspector — exact `LOCAL_LOOP_AGENTS` (agent_runtime_service.py:76-89). coding/critic poartă `runtime_component=repair_watchdog`, `shared_runtime=true`, `runtime_role=implementation|independent_review`, `stalled=false`, `thread_alive=true`.
- Toate `lifecycle_state=running` cu `enabled=true && running=true` — coerent cu `_base_runtime` (agent_runtime_service.py:113-136). Niciun agent în `stopped/error/starting/test_suppressed`.
- `/api/bot/status` (200): `running=true, mode=paper, circuit_breaker_active=false, min_edge_score=90, engine_running=true` — identic cu blocul `bot` din control-plane. Payload suplimentar neconsumat de acest tab (daily_pnl, drawdown_pct, positions_open, balance, equity) — nu e defect, doar neexpus aici.

---

## 3. DEFECTE (P1/P2/P3)

**P1: 0. P2: 0. P3: 5.**

### P3-1 (DESCHIS din round 1) — „Last run: On demand" / „Mode: Cache-first" false pe 6 carduri
- **Unde:** `configDetail` fallback, frontend/agents.js:206 — se aplică oricărui agent fără branch dedicat: `trade_radar`, `economic_calendar`, `research_factory`, `coding_agent`, `critic_agent`, `ui_inspector`.
- **Dovadă live:** trade_radar are `next_run_in=300`, economic_calendar `next_run_in=3600` (și config-ul său conține literal `refresh_seconds: 3600`, neconsumat) — cardul afișează simultan „Last run: **On demand**" și „Next run: 5m/1h", contradicție pe același card. „Mode: Cache-first" e fals pentru coding/critic (watchdog-driven), ui_inspector (interval 300s, prezent în config dar neafișat) și research_factory (auto-cycle).
- **Impact:** informație greșită/ccontradictorie, fără risc de execuție.

### P3-2 (DESCHIS din round 1) — un 5xx pe `/api/execution/control` aruncă tot tab-ul în eroare
- **Unde:** frontend/agents.js:1037-1058 — doar `pipeline-status` e protejat cu try/catch (1053-1058); `responseJson(controlResponse)` (1051) și `responseJson(executionResponse)` (1052) nu sunt, așa că o eroare 5xx pe oricare dintre cele două sare în catch-ul exterior (1075-1082): grid-ul e înlocuit cu bannerul de eroare, toast, iar `#agents-master-panel`/`#agents-kpi-grid`/`#agents-readiness-panel` rămân cu DOM-ul vechi (stale, fără marcaj de staleness).
- **Notă:** eșecul pe control-plane (payload-ul primar) e rezonabil să cadă tab-ul; eșecul pe execution/control (auxiliar, folosit doar de readiness + kill switch + editor) ar merita degradare grațioasă.
- **Impact:** disponibilitate UI, nu integritate date.

### P3-3 (NOU) — renderer-ul legacy shadowed din app.js (cod mort periculos)
- **Unde:** frontend/app.js:19362-19566 (`UI.renderAgents` pe `/api/config` + `/api/agents`, doar 10 agenți) și app.js:19568-19616 (`Actions.toggleAgent` pe `POST /api/config`, **fără** poarta 409).
- **Dovadă:** definit de două ori (frontend/agents.js:1028, frontend/app.js:19362); câștigă agents.js pentru că se încarcă ultimul (frontend/index.html:2731 vs 2735).
- **Impact:** ~255 linii duplicate care pot putrezi; dacă ordinea de încărcare s-ar schimba, toggle-ul ar ocoli complet invarianta „nu opri execution/risk cât timp botul rulează" (scrierea directă prin `/api/config` nu trece prin `set_agent_enabled`). Clasificare: DEAD-RENDER cu risc latent.

### P3-4 (NOU, minor) — secțiunea de research nu distinge „dezactivat" de „în așteptare"
- **Unde:** frontend/agents.js:706-753 — când `sources.cross_sectional_momentum.enabled=false` (starea live actuală), secțiunea afișează „Awaiting cycle" + „0 eligible symbols" + „Liquidity threshold unavailable", deși sursa e dezactivată, nu în așteptare (cf. rândurile Scanner/Watchlist care au starea `disabled`, agents.js:680,685).
- **Impact:** cosmetic; poate inducerea în eroare că pipeline-ul de research e blocat, nu oprit.

### P3-5 (NOU, minor) — starea `waiting_dependency` nu e afișată distinct
- **Unde:** produs de api/agent_runtime_service.py:444-454 (coding_agent activat când watchdog-ul nu poate porni pentru că critic_agent e dezactivat), dar `computeAgentState` (frontend/agents.js:483-526) nu are branch pentru `lifecycle_state='waiting_dependency'` → cade în genericul „Stopped / attention".
- **Impact:** operatorul nu vede cauza reală (dependența critic_agent) direct pe card; rămâne în payload.

---

## 4. STATUS REGRESII vs ROUND 1

- **git log:** ultimele comit-uri care ating căile agents (`frontend/agents.js`, `api/agents_service.py`, `api/agent_runtime_service.py`, `api/execution_service.py`, `core/execution/policy.py`) sunt `1a39400` (audit 2026-09-02) și mai vechi; `git diff 218009f..HEAD --stat` pe aceste căi = **gol** → **nicio regresie de cod de la probele round 1**. Comit-urile ulterioare (7f26fd2, e9eda27, 1eedcfc, 200cd2e) nu ating tab-ul (doar docs/alerts/pre-earnings).
- **Round 1 P3 „Last run: On demand":** **ÎNCĂ DESCHIS** (P3-1; perimeter corectat: 6 agenți, nu 2).
- **Round 1 P3 „execution/control drops the tab on 5xx":** **ÎNCĂ DESCHIS** (P3-2).
- **Round 1 — clasele de defecte majore:** nicio confirmare de FAKE/DEAD-BACKEND/MISCALIBRAT pe câmpurile numerice; toate valorile KPI/pipeline/readiness verificate live corespund surselor.
- **Round 1 — eroare de atribuire:** raportul spunea că `/api/agents/control-plane` e produs de „api.agents_service:build_control_plane_payload (api/agents_service.py:1017-1085)" — real: builder-ul e acum la **api/agents_service.py:1247-1307**; linia s-a mutat, funcția e aceeași. Similar, `set_agent_enabled` e la **agents_service.py:1309-1363** (nu în agent_runtime_service.py:348-479 — acolo e `set_agent_runtime`, apelat de primul).

## 5. RECOMANDĂRI

1. **P3-1:** adăugați branch-uri `configDetail` pentru trade_radar/economic_calendar/ui_inspector (config-urile au deja `refresh_seconds`/`interval_sec`), research_factory (interval auto-cycle) și coding/critic (afisați `cycle_age_seconds`/`cycles_completed` din watchdog) sau, minim, un fallback neutru „Agent gestionat automat" în loc de „On demand / Cache-first" (agents.js:206).
2. **P3-2:** mutați `responseJson(executionResponse, …)` în blocul try/catch al `pipeline-status` și randati readiness-ul cu „necunoscut" când execution/control eșuează (degradare parțială, nu colaps).
3. **P3-3:** ștergeți `UI.renderAgents`/`Actions.toggleAgent` din frontend/app.js:19360-19616 (inventarul dead-code) — elimină dublul și riscul latent de ocolire a invariantelor 409.
4. **P3-4:** adăugați starea `disabled` pentru secțiunea cross-sectional/forward V2 (oglindind `sources.*.enabled=false`).
5. **P3-5:** mapați `waiting_dependency` la un label dedicat („Waiting for Critic Agent") în `computeAgentState`.
6. Opțional: expuneți `GET /api/agents` în UI ca sursă pentru un viitor „telemetry drawer" (rămâne azi endpoint doar-diagnostic; singurul consumator UI e codul mort).

---

**Semnătură probe:** 5 cereri GET (toate 200): `/api/agents` 26ms, `/api/agents/control-plane` 400ms, `/api/execution/control` 270ms, `/api/agents/risk/pipeline-status` 15ms, `/api/bot/status` 200. Zero mutații, zero modificări de cod, zero reporniri. Dump-uri: doar în temp sistem, nu în repo.
