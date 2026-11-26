# Market Scanner V9 — Runtime Flow Map

## Process and storage boundaries

```text
run_server.py
  ├─ bootstrap_runtime_layout() → runtime/, data/, logs/, audit_logs/, backups/
  ├─ FastAPI/Uvicorn → static frontend + /api/*
  ├─ optional engine subprocess → engine PID/state under runtime/data
  └─ PAPER workers/schedulers → bounded provider gateways → atomic persistence

run_server_test.py
  └─ temporary isolated runtime/data/log/audit roots
     + TEST_MODE=1
     + engine/warmup/prefetch/background/remote-price work disabled
```

Mutable operator files are ignored and live under `runtime/` or `data/`. Repository defaults and `data/us_macro_calendar_2026.json` remain immutable fixtures. `core/runtime_layout.py` performs non-destructive, idempotent migration and archives conflicting generated defaults before copying owner data.

## UI → API → domain → persistence

| Feature | UI entry | API | Domain path | Persistence/result | Status |
| --- | --- | --- | --- | --- | --- |
| Main state | `frontend/app.js` | `GET /api/state` | `api/state_service.py` | cache-first aggregate | PASS |
| Cockpit | Cockpit workspaces/tabs | `GET /api/cockpit` | `api/cockpit_service.py` + agents | bounded aggregate + ledger | PASS |
| Pre-Earnings | decision matrix/details | `GET /api/pre-earnings`, refresh endpoints | `agents/pre_earnings_agent.py`, calendar/providers | atomic snapshot, AI cache, pulse history | PASS |
| Scanner/hotlist | watchlist/hotlist | `/api/scanner/state`, `/api/hotlist*` | scanner + `api/state_service.py` | cache-only reads or explicit refresh | PASS |
| Live prices/sparklines | watchlist cards | `GET /api/live-prices` | `api/main.py` + `api/market_data_service.py` | newest-timestamp cache merge; stale-only remote opt-in; LIVE/CACHED/STALE UI | PASS |
| Signals | signal/capital views | `GET/POST/DELETE /api/signals*` | signal engine/manager | signal lifecycle and decision IDs | PASS |
| Backtest | Backtest/OOS panels | `/api/backtest/*` | backtest service/engines | history, OOS and reports | PASS |
| Research | research tabs | `/api/research/*` | `api/research_factory_service.py`, research factory | hypotheses, experiments, registry, Optuna | PASS |
| Capital | Capital panel | `/api/capital/*` | capital manager + execution service | execution SQLite and portfolio state | PASS |
| Risk | Risk Manager | `/api/agents/risk/*` | risk agent/policy | draft/confirmed constitution and decisions | PASS |
| Execution | Paper controls/orders and Pre-Earnings gate thresholds | `/api/execution/*` | execution control + autonomous assessment + paper broker | atomic config, intents, orders, fills, positions | PASS |
| Ledger | Ledger/Model Lab | `/api/ledger/*` | decision ledger store | SQLite trace/replay/model metrics | PASS |
| News/sentiment | News Hub | `/api/news-tab/*`, `/api/news*` | news agent, FinBERT, provider gateway | deduped articles/evidence/cache | PASS |
| SEC | ticker evidence | `GET /api/sec/{ticker}` | SEC service/EdgarTools adapter | normalized SEC evidence/cache | local PASS; live BLOCKED_EXTERNAL |
| Health | status indicators | `/api/health`, `/api/health/diagnostics` | observability/provider budgets/SLOs | structured operational diagnosis | PASS |

## Cache and provider semantics

```text
routine UI read
  → allow_remote=false
  → memory/disk cache only
  → explicit unavailable/null when absent

owner refresh or scheduled job
  → allow_remote=true
  → single-flight gate
  → provider-specific symbol/interval normalization
  → retry/circuit breaker + external-call budget
  → atomic cache/state update
```

Dashed `*-USDT`/`*-USDC` tickers route to KuCoin first; Binance invalid symbols are negative-cached for one hour. KuCoin candle intervals use its native values such as `15min`. Hotlist detail and scanner lazy refresh paths inherit the caller's remote policy and cannot escape cache-only attribution.

## Decision/execution correlation

```text
market/earnings/news evidence
  → candidate_id / signal_id
  → ai_decision_id (structured schema + prompt/model trace)
  → critic verdict
  → risk_decision_id
  → capital_allocation_id
  → order_intent_id
  → paper_order_id / fill
  → position_id
  → monitor/exit/final P&L
  → decision ledger
  → research feedback/experiment
```

The chain is verified in deterministic PAPER acceptance and golden tests. Duplicate decision IDs are idempotent, kill switch is fail-closed, and test mode cannot touch operator persistence.

### Current autonomous admission diagnosis (2026-08-20)

```text
Scanner / Pre-Earnings candidate
  → executable quote concordance → has_data_conflict (explicit fact)
  → USD liquidity observation → average_daily_volume_usd
  → lifecycle fact → RESEARCH
  → Risk Constitution
       active requirement: APPROVED or CHAMPION
       Strategy Lab approved count: 0 / 8
  → structured strategy_evidence veto
  → AI not consulted
  → Capital not reached
  → no paper order / no position
```

The operator can inspect and edit the active policy through Cockpit → Decision Ledger → Open Risk controls. Research/Challenger status is never rewritten merely to create a trade; promotion remains evidence-gated.

## Pre-Earnings refresh, eligibility and owner controls

```text
calendar universe (488 in the observed snapshot)
  → light scan of every reporter
  → bounded deep score (max_results=200; 193 current)
  → bounded AI evidence (ollama_max_tickers=25 per full cycle)
  → news-fingerprint reanalysis when evidence changes
  → recommendation_eligible (research/recommendation layer only)
  → autonomous prefilter (owner-configured gap/bias/coverage/score)
  → regular US session → AI critic → Risk → Capital → PAPER order/fill/position
```

The full evidence cycle runs every 600 seconds. The 60-second continuous scan is targeted and does not re-run AI for every calendar reporter. Cockpit `Verify gates` uses the same `assess_pre_earnings_candidate` function as the autonomous manager, so the UI cannot invent a different threshold verdict.

Owner controls are under Agents → Autonomous Paper Trading and persist through `PATCH /api/execution/control` to:

- `agents.execution.pre_earnings_min_gap_risk`
- `agents.execution.pre_earnings_min_abs_bias`
- `agents.execution.pre_earnings_min_coverage_score`
- `agents.execution.pre_earnings_min_score`

Already-reported rows are removed before presentation on both file-backed and initialized in-memory paths. Explicit result evidence outranks session timing; BMO/AMC fallback boundaries are DST-aware in America/New_York.

## Runtime invariants

- No `LIVE_AUTO` activation exists in this audit path; PAPER is the highest execution mode exercised.
- Cache-only endpoints do not start provider threads or hidden refreshes.
- A failed/invalid AI result cannot bypass critic, risk, capital, idempotency or kill-switch gates.
- Background lifecycles are not started in `TEST_MODE`.
- State writes use the configured runtime/data roots and are isolated in E2E tests.
