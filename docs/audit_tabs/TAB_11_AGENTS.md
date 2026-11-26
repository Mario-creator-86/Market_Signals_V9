# FORENSIC AUDIT REPORT — TAB 11: AGENTS & AUTOMATION FLEET (`#view-agents`)

**Auditor:** Senior Quantitative Systems Engineer, Full-Stack Software Architect & UX/UI Expert  
**Date:** September 4, 2026  
**System:** Market Signals V9 Autonomous Trading & Automation Platform  
**Target View:** `#view-agents` (DOM section: `div#view-agents.view-section.agents-page-shell`)  
**Deliverable File:** `docs/audit_tabs/TAB_11_AGENTS.md`

---

## 1. EXECUTIVE SUMMARY & SYSTEM OVERVIEW

Tab 11 (**Agents & Automation Fleet**) represents the mission-critical operational cockpit and central nervous system of Market Signals V9. It centralizes control, lifecycle monitoring, safety governance, and health diagnostics for all **14 autonomous background agents** and the primary execution loop.

In Market Signals V9, this tab underwent a comprehensive architectural transformation:
1. **Separation of Concerns:** De-coupled from the monolithic legacy bundle (`frontend/app.js`, 24,393 lines) into an isolated, bounded controller (`frontend/agents.js`, 1,469 lines) and dedicated stylesheet (`frontend/styles_agents.css`, 1,353 lines).
2. **Single Policy Authority:** Subsumed fragmented execution settings into a unified **Risk Manager Constitution & Trade Policy** (`PATCH /api/agents/risk/constitution`), strictly retiring legacy duplicate endpoints (`PATCH /api/execution/control` returns `HTTP 410 Gone`).
3. **Fail-Closed Dual Heartbeat IPC:** Established cross-process heartbeat synchronization (`data/agent_status_engine.json`) between the API server and the background `MarketEngine` subprocess with staleness detection (120s max age, 600s fresh threshold), paired with an autonomous watchdog (`RepairWatchdog`) detecting stalled execution threads.
4. **Ironclad Safety Invariants:** Guaranteed at the API and domain layers that safety-critical agents (`execution`, `risk`) cannot be disabled while the bot is running (`HTTP 409 Conflict`), and automated paper risk cannot be opened without an owner-confirmed Risk Constitution, active `PAPER_AUTO` mode, and verified broker reconciliation.

---

## 2. REVERSE DATA FLOW ARCHITECTURE (UI ➔ SOURCE & FAILOVER)

The reverse data flow traces every visual element, button, badge, and input from its DOM representation to the JavaScript controller, the REST API routes, the backend services, the primary source of truth, and its failover/fallback hierarchy.

```
+---------------------------------------------------------------------------------------------------------+
|                                              BROWSER DOM                                                |
|  [#agents-master-panel]    [#agents-kpi-grid]    [#agents-readiness-panel]    [#agents-opportunity-monitor] |
|                                   [#agents-grid (14 Agent Cards)]                                       |
+---------------------------------------------------------------------------------------------------------+
                                                     |
                                                     v
+---------------------------------------------------------------------------------------------------------+
|                                    FRONTEND CONTROLLER (agents.js)                                      |
|  UI.renderAgents() ── Promise.all() Concurrent Dispatch:                                                 |
|    1. GET /api/agents/control-plane                                                                     |
|    2. GET /api/agents/risk/pipeline-status                                                              |
|    3. GET /api/execution/control                                                                       |
+---------------------------------------------------------------------------------------------------------+
                                                     |
                                                     v
+---------------------------------------------------------------------------------------------------------+
|                                        FASTAPI ROUTE HANDLERS                                           |
|  api.main:api_agents_control_plane() ──────> api.agents_service:build_control_plane_payload()            |
|  api.main:api_risk_manager_cycle_status() ─> core.autonomous_control:get_autonomous_cycle_status()     |
|  api.main:api_execution_control() ─────────> api.execution_service:get_execution_control()               |
+---------------------------------------------------------------------------------------------------------+
                                                     |
                                                     v
+---------------------------------------------------------------------------------------------------------+
|                                          BACKEND SERVICES & IPC                                         |
|  * core.bot_controller.BotController.get_control_status()                                               |
|  * api.agent_runtime_service.snapshot_agent_runtimes()                                                  |
|  * core.observability._engine_agent_heartbeats() <── data/agent_status_engine.json (Engine Subprocess)   |
|  * core.development_factory.watchdog.get_repair_watchdog().status()                                     |
|  * core.execution.policy.evaluate_paper_entry_readiness()                                                |
|  * core.execution.runtime.get_paper_broker().store.get_reconciliation()                                 |
+---------------------------------------------------------------------------------------------------------+
                                                     |
                                                     v
+---------------------------------------------------------------------------------------------------------+
|                                          PRIMARY SOURCES OF TRUTH                                       |
|  - Engine Heartbeat: data/agent_status_engine.json                                                       |
|  - Opportunity Pipeline: data/autonomous_cycle_status.json                                              |
|  - Shared Config & Risk Constitution: config.json via ConfigManager                                     |
|  - Paper Broker State: data/paper_broker_orders.db & CapitalManager                                     |
|  - Subprocess Daemons: BaseAgent threads, RepairWatchdog daemon, ResearchFactory thread                 |
+---------------------------------------------------------------------------------------------------------+
```

### 2.1 Detailed Element-to-Source Mapping Table

| UI Element / DOM Target | JS Controller / Action | HTTP Route & Method | Backend Handler & File Location | Primary Source of Truth | Failover / Fallback Behavior |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Hero Header Sync**<br>`#agents-last-updated` | `UI.renderAgents()`<br>([frontend/agents.js:1073-1074](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/agents.js#L1073-L1074)) | `GET /api/agents/control-plane` | `api.main:api_agents_control_plane`<br>([api/main.py:2116-2140](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/api/main.py#L2116-L2140)) | Client clock timestamp on HTTP 200 payload receipt | Displays `"Not synchronized"` initially; on fetch failure displays error toast. |
| **Refresh Button**<br>`#agents-refresh-btn` | `UI.renderAgents({ force: true })`<br>([frontend/agents.js:1028-1088](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/agents.js#L1028-L1088)) | `GET /api/agents/control-plane`<br>`GET /api/agents/risk/pipeline-status`<br>`GET /api/execution/control` | Concurrent endpoints via `Promise.all()` with `bypassCooldown: true` | Real-time backend runtime states | Network timeout set to 10,000ms. If unreachable, retains previous snapshot and renders alert banner. |
| **Master Control Panel**<br>`#agents-master-panel` | `renderMaster(bot, config)`<br>([frontend/agents.js:528-574](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/agents.js#L528-L574)) | `GET /api/agents/control-plane` | `api.agents_service:build_control_plane_payload`<br>([api/agents_service.py:1017-1085](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/api/agents_service.py#L1017-L1085)) | `core.bot_controller:BotController.get_control_status()` | If bot controller is uninitialized, defaults to stopped (`running: false`, `mode: "paper"`). |
| **Master Start/Stop Bot**<br>`[data-testid="agents-bot-start"]`<br>`[data-testid="agents-bot-stop"]` | `Actions.setBotRunning(bool)`<br>([frontend/agents.js:1101-1131](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/agents.js#L1101-L1131)) | `POST /api/bot/start`<br>`POST /api/bot/stop` | `api.main:api_bot_start`, `api_bot_stop`<br>([api/main.py:9087-9122](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/api/main.py#L9087-L9122)) | `core.bot_controller.BotController` instance + `config.bot.enabled` disk flag | Starting blocked if Market Engine is offline or `execution`/`risk` agents disabled (`HTTP 409 Conflict`). |
| **Master Kill Switch**<br>`[data-testid="risk-manager-kill-switch"]` | `Actions.setPaperKillSwitch(bool)`<br>([frontend/agents.js:1133-1152](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/agents.js#L1133-L1152)) | `POST /api/execution/kill-switch` | `api.execution_service:set_kill_switch`<br>([api/execution_service.py:140-153](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/api/execution_service.py#L140-L153)) | Persisted flag `autonomy.kill_switch_active` in `config.json` | Fails closed: if config manager lock fails, raises `500`; runtime defaults to blocked entries. |
| **Autonomy Mode Selector**<br>`select#risk-trade-execution-mode` | `Actions.saveRiskSettings()`<br>([frontend/agents.js:1362-1440](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/agents.js#L1362-L1440)) | `PATCH /api/agents/risk/constitution` | `api.agents_service:set_risk_constitution`<br>([api/agents_service.py:1115-1260](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/api/agents_service.py#L1115-L1260)) | Config key `autonomy.mode` (`OFF`, `ANALYZE`, `RECOMMEND`, `PAPER_AUTO`) | Evaluated by `core.execution.policy`. Non-PAPER_AUTO blocks entry execution. Fails safe to `ANALYZE`. |
| **Risk Constitution Status**<br>`.agents-risk-policy-status` | `renderCards()` & `riskEditorHtml()`<br>([frontend/agents.js:352-475](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/agents.js#L352-L475)) | `GET /api/agents/control-plane` | `core.portfolio.constraints:resolve_confirmed_risk_constitution` | Hash fingerprint of confirmed rules (`manager_fingerprint`) | If unconfirmed or invalid, status displays `"Not configured"` or `"Needs attention"`; orders vetoed. |
| **KPI Grid**<br>`#agents-kpi-grid` | `renderKpis(bot, cards)`<br>([frontend/agents.js:576-593](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/agents.js#L576-L593)) | `GET /api/agents/control-plane` | `api.agents_service:build_control_plane_payload` + `cards` array | Live computed runtime status + `BotController.get_control_status()` | Engine offline marked danger (`.is-danger`); circuit breaker triggered displays alert. |
| **Readiness Panel**<br>`#agents-readiness-panel` | `renderReadiness(bot, config, exec)`<br>([frontend/agents.js:595-624](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/agents.js#L595-L624)) | `GET /api/execution/control` | `core.execution.policy:evaluate_paper_entry_readiness`<br>([core/execution/policy.py:41-95](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/execution/policy.py#L41-L95)) | 7 atomic gates: engine, execution agent, risk agent, candidate worker, circuit breaker, paper mode, active policy | Fail-closed: returns `ready: false` with explicit array of blocking reasons (`reasons`). |
| **Opportunity Monitor**<br>`#agents-opportunity-monitor` | `renderOpportunityMonitor()`<br>([frontend/agents.js:637-755](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/agents.js#L637-L755)) | `GET /api/agents/risk/pipeline-status` | `core.autonomous_control:get_autonomous_cycle_status`<br>([core/autonomous_control.py:96-98](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/autonomous_control.py#L96-L98)) | Sidecar file `data/autonomous_cycle_status.json` written by subprocess cycle | If file is missing or unreadable, safely catches exception (`agents.js:1056`) and renders `"Awaiting cycle"`. |
| **Signal Cycle Trigger**<br>`[data-testid="agents-run-v2-cycle"]` | `Actions.runRiskManagerCycle()`<br>([frontend/agents.js:1442-1461](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/agents.js#L1442-L1461)) | `POST /api/agents/risk/run-cycle` | `core.autonomous_control:request_autonomous_cycle`<br>([core/autonomous_control.py:54-63](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/autonomous_control.py#L54-L63)) | Writes atomic task file `data/autonomous_cycle_request.json` consumed by engine | Refuses execution if bot is stopped or not in paper mode (`HTTP 409 Conflict`). |
| **Agent Fleet Catalog**<br>`#agents-grid` | `renderCards(cards, bot)`<br>([frontend/agents.js:774-864](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/agents.js#L774-L864)) | `GET /api/agents/control-plane` | `api.agent_runtime_service:snapshot_agent_runtimes`<br>([api/agent_runtime_service.py:293-346](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/api/agent_runtime_service.py#L293-L346)) | Merged dictionary of 14 agents from engine heartbeat, local thread singletons, and RepairWatchdog | If an agent is uninitialized or missing runtime status, safely maps to `lifecycle_state: "stopped"` or `"error"`. |
| **Agent Toggle Switch**<br>`[data-testid="agent-toggle-{key}"]` | `Actions.toggleAgent(key, bool)`<br>([frontend/agents.js:1174-1209](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/agents.js#L1174-L1209)) | `POST /api/agents/{agent_name}/enabled` | `api.agent_runtime_service:set_agent_runtime`<br>([api/agent_runtime_service.py:348-479](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/api/agent_runtime_service.py#L348-L479)) | Thread lifecycle management (`start()` / `stop()`) + atomic update to `config.agents[name].enabled` | Client polls via `waitForAgentRuntime()` for up to 15s. Reverts toggle and alerts toast if start fails. |
| **UI Inspector Manual Trigger**<br>`[data-testid="ui-inspector-run"]` | `Actions.runUiInspector(btn)`<br>([frontend/agents.js:1211-1234](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/agents.js#L1211-L1234)) | `POST /api/ui-inspector/run` | `api.main:api_run_ui_inspector`<br>([api/main.py:2841-2853](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/api/main.py#L2841-L2853)) | `agents.ui_inspector_agent:UiInspectorAgent.inspect_all_tabs()` | Evaluates DOM and API freshness across all 11 tabs; escalates anomalies directly to Coding Agent. |
| **Research Factory Manual Run**<br>`[data-testid="research-factory-run"]` | `Actions.runResearchCycle(btn)`<br>([frontend/agents.js:1236-1262](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/frontend/agents.js#L1236-L1262)) | `POST /api/research/auto-cycle/run` | `api.research_factory_service:auto_cycle_run_once`<br>([api/main.py:2543-2558](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/api/main.py#L2543-L2558)) | Generates hypothesis, evaluates backtest walk-forward OOS, updates strategy status | 120s timeout on API call. Returns weakness count and experimental promotion results. |

---

## 3. LIVE FORENSIC INVESTIGATION & VERIFICATION EVIDENCE

A live automated verification probe was constructed and executed against the Market Signals V9 test harness using the internal FastAPI test runner (`scripts/test_agents_testclient.py` and `scripts/test_agents_mutations.py`).

### 3.1 Live Telemetry & Endpoint Probe Results

```
Test Execution Timestamp: 2026-09-04 23:14:16 UTC
Test Environment: MS_PROCESS_ROLE=api, IS_TEST_MODE=true

[1] GET /api/agents (200 OK - 98.82ms)
    - Total Monitored Agents: 14 / 14
    - Engine-Managed: risk, execution, scanner, sentiment
    - Local Loop Singletons: news_tab, economic_calendar, trade_radar, premarket_scanner, 
                            pre_earnings, strategy_research, ui_inspector, research_factory
    - Shared Watchdog: coding_agent, critic_agent

[2] GET /api/agents/control-plane (200 OK - 21.73ms)
    - Bot Status: {'running': True, 'mode': 'paper', 'circuit_breaker_active': False, 'min_edge_score': 90}
    - Control Plane Payload Agents Count: 14
    - Config Schema Invariants: ['agents', 'bot', 'news', 'premarket', 'pre_earnings', 'strategy_v2', 'risk_constitution']

[3] GET /api/execution/control (200 OK - 185.54ms)
    - Mode: PAPER_AUTO
    - Kill Switch Active: False
    - Readiness: ready=True, reasons=[]
    - Allocation Lane: 33% Scanner / 33% Watchlist / 34% Pre-Earnings rotation

[4] GET /api/agents/risk/pipeline-status (200 OK - 11.91ms)
    - Pipeline Keys: ['updated_at', 'request_id', 'requested_at', 'trigger', 'result']
```

### 3.2 Safety Invariant & Mutation Verification

```
[TEST A] Master Kill Switch Engagement
    - POST /api/execution/kill-switch {"active": True} -> HTTP 200 OK (status="stopped")
    - Immediate GET /api/execution/control:
      * ready: False
      * reasons: ["STOP AUTONOMOUS TRADING is active"]
    - POST /api/execution/kill-switch {"active": False} -> HTTP 200 OK (status="released")
    - Immediate GET /api/execution/control:
      * ready: True (Zero blockers)

[TEST B] Protection of Safety-Critical Agents Mid-Run
    - POST /api/agents/execution/enabled {"enabled": False} (while bot running)
      * Result: HTTP 409 Conflict
      * Body: {"detail": "Stop the bot before disabling a required safety agent"}
    - POST /api/agents/strategy_research/enabled {"enabled": False} (non-safety agent)
      * Result: HTTP 200 OK (status="updated")

[TEST C] Deprecated Legacy Execution Control Retirement
    - PATCH /api/execution/control {}
      * Result: HTTP 410 Gone
      * Body: {"detail": "Execution controls moved to Risk Manager. Use PATCH /api/agents/risk/constitution with trade_policy."}
```

---

## 4. MONITORING, CONTROL & WATCHDOG ARCHITECTURAL DEEP-DIVE

### 4.1 How Does the Server Know if an Agent is Alive or Blocked?

Market Signals V9 implements a **three-tier liveness architecture** tailored to process separation:

1. **Engine-Managed Subprocess Agents (`execution`, `risk`, `scanner`, `sentiment`):**
   - The engine runs as an independent OS process (`MS_PROCESS_ROLE=engine`).
   - Every cycle of `BaseAgent` calls `self._beat()` ([agents/base_agent.py:47-58](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/base_agent.py#L47-L58)), updating `_AGENT_STATUS` in `core.observability`.
   - The engine dumps this table to `data/agent_status_engine.json` (debounced every 2.0s via `_maybe_dump_engine_agent_status()` at [core/observability.py:322-342](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/observability.py#L322-L342)).
   - The API server reads this file via `_engine_agent_heartbeats()`.
   - **Staleness Rules:**
     - If the file modification time (`st_mtime`) is older than **120 seconds**, the heartbeat is deemed silent, and no records are trusted (`return {}`).
     - If an agent's individual `last_heartbeat` timestamp in the JSON is older than **600 seconds**, `_engine_observed_running()` returns `None`, and the agent is flagged as stopped or stale.

2. **Local Loop In-Process Agents (`trade_radar`, `premarket_scanner`, etc.):**
   - Managed via lazy singletons (`get_*_agent_if_ready()`).
   - `api.agent_runtime_service:_instance_status()` directly probes `instance.is_running` or calls `instance.get_runtime_status()`.
   - If an agent thread has died or never started, `lifecycle_state` resolves to `"stopped"`.

3. **Autonomous Development & Self-Repair Agents (`coding_agent`, `critic_agent`):**
   - Managed by `RepairWatchdog` ([core/development_factory/watchdog.py:1367-1480](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/core/development_factory/watchdog.py#L1367-L1480)).
   - Inspects `self._thread.is_alive()` AND monitors `cycle_started_at`.
   - **Stall Detection:** If `cycle_age_seconds > max(600s, interval * 2)` (typically 1800s), it checks the activity heartbeat in `data/auto_repair_activity.json`. If that is also silent, it marks `stalled = True`, sets `running = False`, and injects:
     `last_error = "watchdog cycle stalled for Xs"`.

### 4.2 How Does the Forced Kill Switch Work?

Market Signals V9 features **two levels of Kill Switch**:
1. **The Master Trading Bot Switch (`Actions.setBotRunning(false)`):**
   - Dispatched via `POST /api/bot/stop`.
   - Calls `BotController.stop(reason="manual")`, which sets `self._stop_event`, flags `self._running = False`, persists `config.bot.enabled = False`, and signals `AgentManager.set_bot_mode(False)`.
2. **The New Risk Kill Switch (`Actions.setPaperKillSwitch(true)`):**
   - Dispatched via `POST /api/execution/kill-switch` with `{"active": true}`.
   - Atomically updates `autonomy.kill_switch_active = true` in `config.json`.
   - **Fail-Closed Execution Gate:** Every candidate evaluation cycle inside `core.execution.policy:evaluate_paper_entry_readiness()` evaluates `kill_switch_active(root)`. If `True`, new orders are unconditionally blocked, while telemetry, data collection, and position exit management remain 100% active.

### 4.3 Handling Unknown Exceptions in Background Loops

Every agent deriving from `BaseAgent` implements an uncompromising exception-trapping boundary in `_loop_wrapper()` ([agents/base_agent.py:60-78](file:///c:/Users/<user>/PyCharmMiscProject/Market%20Signals%20V9/agents/base_agent.py#L60-L78)):
```python
def _loop_wrapper(self):
    try:
        update_agent_status(self.name, running=True, initializing=False, last_heartbeat=_iso_now())
        self.loop()
    except Exception as e:
        logger.error(f"Agent {self.name} crashed: {e}")
        try:
            update_agent_status(
                self.name, running=False, last_error=str(e)[:300], last_heartbeat=_iso_now()
            )
        except Exception:
            pass
        self.running = False
    finally:
        try:
            update_agent_status(self.name, running=False, last_heartbeat=_iso_now())
        except Exception:
            pass
```
- **Crash Containment:** An unhandled exception in an agent loop logs the traceback, truncates the error string to 300 characters to prevent buffer bloat, updates the observability registry, and gracefully transitions `self.running = False`.
- **UI Reflection:** The frontend controller detects `runtime.last_error`, suppresses benign pause messages (e.g. `market_closed`), and renders a high-visibility warning box on the card (`.agents-error-note`) while shifting the status badge to `ERROR` (`.agents-status-error`).

---

## 5. COMPLETE AGENT FLEET CATALOG (14 MONITORED AGENTS)

| Agent Name (`key`) | Category | Architectural Role & Responsibilities | Bound Engine / Master | Runtime Strategy | Manual Trigger Available |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Execution Agent** (`execution`) | Trading | Validates confidence scores, applies risk limits, checks spread & liquidity, and dispatches paper orders to PaperBroker. | Yes (Engine & Master Bound) | Thread loop in engine subprocess (`BaseAgent`) | Evaluated via signal cycle |
| **Risk Manager** (`risk`) | Safety | Enforces portfolio limits, drawdown caps, sector exposure, correlation clustering, and maximum daily loss vetoes. | Yes (Engine & Master Bound) | Core execution filter + background analysis | `Actions.runRiskManagerCycle()` |
| **Market Scanner** (`scanner`) | Discovery | Continuously monitors configured markets, momentum breakouts, RVOL surges, and volume thresholds. | Yes (Engine Bound) | Thread loop in engine subprocess (`BaseAgent`) | Controlled via Scanner tab |
| **Sentiment Analysis** (`sentiment`) | Intelligence | Ingests news streams and social feeds, computing rolling sentiment scores to enrich candidate trades. | Yes (Engine Bound) | Periodic cycle in engine subprocess | Periodic execution |
| **Trade Radar** (`trade_radar`) | Discovery | Analyzes momentum setups, multi-timeframe trend alignment, and scoring matrices on a scheduled cycle. | Local Loop | In-process daemon thread singleton | Periodic execution |
| **Premarket Scanner** (`premarket_scanner`) | Discovery | Builds cache-first candidate lists (Long / Short / Watch) prior to regular market trading hours. | Local Loop | Scheduled cache-refresh daemon | Controlled via Premarket tab |
| **Pre-Earnings** (`pre_earnings`) | Intelligence | Identifies upcoming corporate earnings announcements, evaluates historical gap risk, and calculates directional bias. | Local Loop | In-process cache-first singleton | Controlled via Pre-Earnings tab |
| **News Flow** (`news_tab`) | Data | Coordinates multi-source RSS/API news collection, article deduplication, and cache maintenance. | Local Loop | In-process asynchronous worker | Controlled via News tab |
| **Economic Calendar** (`economic_calendar`) | Data | Tracks macroeconomic releases (CPI, FOMC, NFP), earnings calendar events, and dividend dates. | Local Loop | Cache-first background agent | Controlled via Calendar tab |
| **Strategy Research** (`strategy_research`) | Research | Continuously tests strategy hypotheses; operates strictly in read-only evidence mode (cannot authorize Capital). | Local Loop | Dedicated research loop | Periodic execution |
| **Research Factory** (`research_factory`) | Research | Autonomous quantitative lab: walk-forward validation, out-of-sample stress tests, and strategy lifecycle promotions. | Local Loop | Autonomous cycle manager (`auto_cycle`) | Yes (`[data-testid="research-factory-run"]`) |
| **Coding Agent** (`coding_agent`) | Development | Autonomous software engineer: ingests runtime diagnostics, generates minimal AST patches to fix defects. | Local Loop (Watchdog) | Controlled via `RepairWatchdog` daemon | Integrated with watchdog |
| **Critic & Watchdog** (`critic_agent`) | Development | Autonomous code reviewer: enforces AST sanity, guards protected configuration paths, runs pytest test suites. | Local Loop (Watchdog) | Controlled via `RepairWatchdog` daemon | Integrated with watchdog |
| **UI Health Inspector** (`ui_inspector`) | Maintenance | Automatically navigates all 11 UI tabs, tests data freshness, detects stalled loaders, and escalates to Coding Agent. | Local Loop | Background maintenance inspector | Yes (`[data-testid="ui-inspector-run"]`) |

---

## 6. UI / UX, ERGONOMICS & COGNITIVE AUDIT

### 6.1 Visual Layout & Visual Hierarchy
- **Container Structure:** Rendered inside a generous container (`max-width: 1440px`, padding: 24px 32px 56px). The design maintains high contrast and visual clarity using CSS design tokens (`--card-bg`, `--border-color`, `--text-primary`, `--accent-color`).
- **Two-Column Master Split:**
  - **Left (65% width):** Hero identity, Master Bot Switch, and real-time state pill (`.agents-state-running` vs `.agents-state-stopped`).
  - **Right (35% width):** 2x2 KPI Grid (Market Engine, Worker Fleet, Circuit Breaker, Entry Threshold) followed immediately by the 7-item Automation Readiness checklist.
- **Mid-Screen Pipeline Strip:** The Opportunity Monitor (`#agents-opportunity-monitor`) sits prominently between the Master Control and Fleet Catalog, providing complete visibility into trade candidates in transit.
- **Card Grid:** Responsive CSS Grid (`repeat(auto-fill, minmax(340px, 1fr))`), adapting seamlessly down to 2 columns on tablets and 1 column on mobile devices.

### 6.2 Cognitive Load & Safety Experience for Traders
- **High Operational Safety:** The distinction between **Bot Running** and **New Entries Blocked** is crystal clear. Even if the master bot is running, if the Risk Constitution is unconfirmed or the Kill Switch is engaged, large amber/red callouts inform the operator: *"The bot is monitoring and evaluating candidates, but new paper risk remains blocked by the owner control plane."*
- **Safety Interlocks:** Safety-critical agent toggles (`execution`, `risk`) are physically disabled when the bot is active, preventing accidental operator clicks with clear tooltips.
- **Honest Telemetry:** If an agent encounters an error, it is not disguised as "Online". It displays an explicit red `Error` pill and an inline callout containing the exact exception snippet.

### 6.3 Technical Terminology & Clarity
- Terms such as *Circuit Breaker*, *Edge Score Threshold*, *Paper Auto*, *Risk Constitution*, and *Cross-Sectional Momentum* are clearly presented.
- **Ergonomic Deficiency:** While the terms are clear, there is an absence of interactive tooltip hover triggers (e.g. `?` icons) explaining specific quant terminology like *Cross-Sectional Momentum* or *Normalized Allocation Lane* to novice operators.

---

## 7. INDUSTRY BENCHMARK COMPARISON

| Feature / Capability | Market Signals V9 (Tab 11) | QuantConnect Alpha Streams | Kryll.io Bot Manager | Hummingbot Gateway | Bloomberg EMSX |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Fleet Monitoring Scope** | 14 Specialized Agents (Trading, Discovery, Research, Self-Repair) | Hundreds of Alpha Models & Strategies | Multi-pair Grid & DCA Trading Bots | Arbitrage & Market Making Gateways | Global Execution Management System |
| **Kill Switch Granularity** | Master Bot Stop + Atomic Risk Kill Switch + Circuit Breaker | Per-algorithm stop + Global account liquidation | Emergency stop per bot | Global `stop --kill-all` + Cancel on disconnect | Instant venue kill + Basket cancel |
| **Risk Constitution Governance** | Inviolable owner-confirmed mathematical limits (Hard code veto) | Algorithm margin limits & equity stop rules | Fixed percentage stop-loss per strategy | Dynamic inventory skew & min/max spread limits | Strict institutional compliance rules & venue wheels |
| **Autonomous Self-Repair** | **Industry First:** Dedicated Coding & Critic Agents + Watchdog | None (Requires developer manual git commit) | None | None | None |
| **Hardware Resource Profiling** | Background psutil/tracemalloc (Not exposed in UI KPI grid) | **Live CPU %, Heap MB, and Thread pool metrics** | Server load percentage bar | Process memory & CPU usage metrics | Centralized terminal server diagnostics |
| **Live Log / Event Stream** | Truncated `last_error` string only | **Live sliding terminal log per strategy** | Node execution event history | Real-time console log stream | Full FIX protocol message audit log |
| **Pipeline In-Flight Telemetry** | Opportunity Monitor with source-by-source candidate breakdown | Order book queue position & fill latency | Visual flowchart active block indicator | Order book depth & gateway REST/WS latency | Real-time blotter with slicing & parent/child orders |

---

## 8. SWOT & PRODUCT EVALUATION

```
+---------------------------------------------------+---------------------------------------------------+
|                   STRENGTHS (S)                   |                  WEAKNESSES (W)                   |
| - Inviolable Risk Constitution fail-closed gates  | - Hardware resource consumption missing from UI   |
| - Safety invariant blocks disabling active risk   | - No live terminal log stream per agent card      |
| - Unified 5-source Opportunity Monitor pipeline   | - Absence of cycle latency trend sparklines       |
| - Dual-heartbeat IPC with stale detection         | - Toggle switches lack temporary pause option     |
| - Self-healing autonomous RepairWatchdog          | - No manual restart button on standard agents     |
+---------------------------------------------------+---------------------------------------------------+
|                 OPPORTUNITIES (O)                 |                    THREATS (T)                    |
| - Real-time WebSocket live log drawer             | - Subprocess heartbeat desynchronization under IO |
| - Dynamic execution interval sliders in UI        | - High CPU during concurrent LLM research cycles  |
| - Visual multi-agent workflow dependency DAG      | - Operator confusion over Paper vs Live gates     |
| - Gateway rate-limit & latency gauge (Alpaca/CCXT)|                                                   |
+---------------------------------------------------+---------------------------------------------------+
```

### 8.1 What is EXCELLENT in Market Signals V9
1. **Single Policy Authority Paradigm:** Centralizing all trade execution modes, partial sizes, and exposure vetoes into `PATCH /api/agents/risk/constitution` eliminated race conditions and split-brain configurations.
2. **Fail-Closed Readiness Battery:** The 7-point readiness check prevents automated paper entries under any anomalous condition (engine offline, unconfirmed constitution, active kill switch, or broker reconciliation conflict).
3. **Cross-Process Resilience:** The IPC heartbeat sidecar file (`agent_status_engine.json`) with time-decay heuristics prevents the UI from falsely reporting crashed subprocess threads as healthy.
4. **Self-Healing Capabilities:** The integration of `RepairWatchdog`, `CodingAgent`, and `CriticAgent` into the fleet catalog provides unprecedented automated observability and repair.

### 8.2 What is MEDIOCRE or WEAK
1. **Resource Consumption Omission:** While `core.observability` tracks `rss_bytes`, `vms_bytes`, and `tracemalloc` memory, the `#agents-kpi-grid` does not display CPU or Memory usage metrics.
2. **Diagnostic Log Truncation:** Operators cannot view real-time log outputs or execution traces on agent cards; they must inspect server console output or raw log files.
3. **Coarse Control Toggles:** Cards only support a binary On/Off switch; there is no "Pause for 1 cycle" or "Run Once On Demand" action for standard agents.

### 8.3 Recommended Roadmap to Reach World-Class Status
1. **Agent Telemetry Drawer:** Implement a slide-out drawer displaying the last 50 log lines and recent cycle execution durations for any clicked agent card.
2. **Resource Monitor Widget:** Add a mini-gauge to `#agents-kpi-grid` showing live process CPU%, Thread count, and Memory consumption.
3. **Interactive Agent Workflow DAG:** Add an interactive visualization toggle illustrating candidate flow: `Discovery (Scanner/News) -> Intelligence (Sentiment/PreEarnings) -> Gating (Critic/Risk) -> Execution (Capital/Broker)`.

---

## 9. CONCLUSION & AUDIT SIGN-OFF

Tab 11 (**Agents & Automation Fleet**) represents an exceptionally well-engineered, robust, and safe automation control plane. Its reverse data flow is deterministic, fail-closed, and strictly validated. All safety invariants—most notably the prevention of safety agent deactivation during active runs and atomic risk policy enforcement—passed every live forensic probe with zero failures.

**Audit Status:** `COMPLIANT`  
**Safety Grade:** `A+ (Institutional Grade Fail-Closed)`  
**Ergonomics Grade:** `A- (Polished UI; Recommended Telemetry Drawer)`
