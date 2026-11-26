# Phase 4 — AI Tool & Decision Engine (Design)

**Date:** 2026-08-12
**Phase:** 4 (AI Tool & Decision Engine). Phase 3 (Multi-Provider AI Platform) = PASS as of `7209da0` (1756 tests green; 3A + 3B + 3C all PASS). Phase 2 = PASS (`07f638f`); Phase 1 = PASS (`f1d65d4`).
**Spec basis:** `prompt_to_follow.md` §39 (AI tool architecture — ~28 registered application tools the AI reasons through), §40 (AI must NOT have unrestricted shell/Python/SQL/filesystem/HTTP/broker-creds/secrets — allow-listed domain tools only), §41 (AI output — structured decision schema; actions WATCH/NO_TRADE/OPEN_LONG/OPEN_SHORT/HOLD/REDUCE/CLOSE), §42 (AI validation — PARSE→SCHEMA→SEMANTIC→AUTHORITATIVE DATA CHECK→POLICY→RISK GATE; AI prose alone must never trigger an order), §43 (AI numbers are not authoritative — price/P&L/IV/EPS/portfolio value/allowed capital/position size are app-calculated), §44 (AI critic — independent challenge on 10 dimensions; valid final result can be NO TRADE), §45 (autonomy modes OFF/ANALYZE/RECOMMEND/PAPER_AUTO/LIVE_ASSISTED/LIVE_AUTO; default NOT LIVE_AUTO; owner-enabled only), §46 (Paper Auto before Live Auto — LIVE_AUTO disabled until mandatory Paper-Auto acceptance tests pass), §47 (Risk Constitution — protected doc, Phase 0), §48 (AI cannot change Risk Constitution/Kill Switch/promotion gates/critical tests/live permissions/holdout policy — owner action required), §49 (Pre-Earnings as a decision system), §7/§5 (evolve, preserve, no rewrite), §4 (no forbidden success language), §74 (kill switch), §82/§83 (protected files, goal-hacking protection), §106 (secrets), §115 (no test weakening), §117 (V7 read-only), §118/§25 (no new deps, pin deps).

## 1. Audit findings (the starting point — "audit first, preserve what works")

Two read-only audits of V9's current decision/critic/autonomy/risk surface and data/market/portfolio service surface establish the starting point. Nothing in §39–§48 exists as implemented code today except the pieces catalogued below.

### 1a. Decision / critic / autonomy / risk surface

- **`core/ollama_decision.py` — the current AI decision engine (evolve, not rewrite).** Allow-sets (plain strings, no enum): `ALLOWED_RECOMMENDATIONS={approve,reduce,avoid,watch}` (`:12`), `ALLOWED_SENTIMENT={bullish,bearish,neutral}` (`:13`), `ALLOWED_EXIT_ACTIONS={hold,close,tighten_stop}` (`:14`). `default_trade_review` (`:72`) / `parse_trade_review` (`:89` — brace-match `_extract_json_object` + `json.loads`, then per-field coercion/clamp/truncate = today's PARSE+SCHEMA) / `_compact_signal` (`:128`) / `build_trade_review_prompt` (`:232`) / `evaluate_trade_decision_with_ollama` (`:267` — entry point; calls legacy `ollama.generate`; on error returns `default_trade_review(available=False)`). Exit side: `default_exit_review` (`:316`) / `parse_exit_review` (`:332`) / `evaluate_exit_close_guard` (`:460` — the existing semantic+authoritative exit check: close only when AI `close` AND (`risk_fraction ≥ exit_close_technical_risk_fraction` OR (`thesis_invalidated` AND severity ∈ {high,critical} AND evidence_quality=="verified"))). **Veto:** `should_veto_trade(review, config)` (`:614`) — only `avoid`+confidence≥`decision_min_confidence` (default 0.65) triggers veto; called at `core/agent_manager.py:1433` (research cohort, behind `block_ai_veto` default True) and `:1981` (main path, unconditional). **Crucially, `ollama_decision.py` still talks to the legacy `core/ollama_client.py` singleton (`ollama.generate`), NOT the Phase 3 `core/ai/*` provider abstraction or `generate_with_fallback`.**
- **§41 actions do not exist in code.** `grep` for `OPEN_LONG|OPEN_SHORT|NO_TRADE|WATCH` across `core/**/*.py` returns no matches. Today's vocabulary is the two `ollama_decision` allow-sets plus the pre-earnings `core/ai_analysis_schema.py:5` `ALLOWED_PLAY={PLAY_PRE,PLAY_POST,WAIT,AVOID}` / `:6` `ALLOWED_DIRECTION={LONG,SHORT,NONE}` and `core/hotlist_scoring.py:10-12`. All are plain string sets, no Enum/class, enforced only by membership checks inside the `parse_*` functions. `agents/risk_agent.py:121` reads `action`/`type` as free-form "BUY"/"SHORT". **The unified §41 action enum is Phase 4 net-new.**
- **§42 RISK GATE is split across functions, no single pipeline.** (a) AI veto — `ollama_decision.should_veto_trade`. (b) Portfolio/signal risk — `agents/risk_agent.py:86 RiskAgent.evaluate_signal(signal) -> {accepted, reason, reasons[], checks[{check,passed,detail}], metrics}` (this `checks` list is the RISK GATE structure). (c) Aggregation — `core/trade_decision.py:643-647` builds `risk_gate_value="N/M passed"`. (d) Limit math — `core/risk_limits.py:32 validate_risk_limits`, `:55 configured_daily_loss_pct`, `:64 configured_risk_per_trade_pct`, `:84 calculate_trade_risk_pct(*, notional_usd, stop_loss_fraction, equity)`. (e) V2 levels — `core/strategy_v2/risk_manager.py:15 RiskManagerV2` (computes SL/TP/RR, no veto). **The §42 named pipeline as one composed object is Phase 4 net-new; the stages compose these existing functions.**
- **§45 autonomy modes exist only in docs, not code.** `core/autonomous_control.py` is solely a request/result file hand-off between the API process and engine subprocess (`request_autonomous_cycle`/`consume_autonomous_cycle_request`/`record_autonomous_cycle_result`/`get_autonomous_cycle_status` — two JSON files, atomic writes). **No `AutonomyMode` enum, no LIVE_AUTO guard in code.** `core/config.py` has no `autonomy_mode` key (only `variant_mode_default:"STRICT"` at `:148`). The six modes and "default NOT LIVE_AUTO / owner-enabled only" live only in `prompt_to_follow.md:1491-1504`, `docs/V9_OPERATIONS.md:6-9`, `docs/V9_RISK_CONSTITUTION.md:22`. **Phase 4 builds the mode enum + default-OFF + owner-only + Paper-Auto enforcement from scratch.** The Risk Constitution (`docs/V9_RISK_CONSTITUTION.md`) and Operations doc already define the protected values (Phase 0); Phase 4 builds the code that *enforces* them.
- **§44 critic does not exist.** No independent second-pass challenge on the 10 §44 dimensions exists. The §38 router `critic` tier (Phase 3C) can *select* an independent model, but no critic *workflow* calls it. Phase 4 net-new.
- **Autonomous candidate / evidence gates (evolve).** `core/autonomous_trade_guard.py:32 is_autonomous_candidate`; `:165 evaluate_autonomous_performance` (realized-edge gate: verified execution provenance, min_sample/profit_factor/expectancy/drawdown); `:256 evaluate_autonomous_news` (source-backed news, directional-conflict rejection); `:451 record_autonomous_news_evidence` (audit record via `core.execution_archive.append_execution`). `core/capital_entry_guard.py:279 evaluate_capital_entry` — ordered 4-check evidence bundle: research_only short-circuit → `evaluate_entry_data` (direction/entry/tp/sl, price_integrity, R/R≥1.2, signal age≤300s) → `evaluate_backtest_validation` (V2, faithful_v2_walk_forward, sample≥30, profit_factor≥1.2, expectancy_r>0, drawdown≤25%) → realized_performance → news. **This ordered bundle is today's de-facto AUTHORITATIVE DATA CHECK + POLICY stage; the §42 pipeline composes it, not replaces it.**
- **Tests pinning the contract (must stay green):** `tests/test_ollama_decision.py` (19, incl. `:44 test_should_veto_trade_requires_avoid_and_confidence_threshold`), `tests/unit/test_autonomous_control.py` (1), `tests/unit/test_autonomous_trade_guard.py` (13), `tests/unit/test_autonomous_candidates.py` (16), `tests/unit/test_autonomous_ticker_lockout.py` (11), `tests/unit/test_capital_entry_guard.py` (12), `tests/unit/test_risk_settings.py` (17), `tests/test_risk_agent.py` (12). **No dedicated `*veto*.py` file; the veto contract is pinned inside `tests/test_ollama_decision.py`.**

### 1b. §39 tool-to-service surface (what each tool wraps)

The 28 §39 tools (spec `:1339-1368`) map to existing read-only service functions, with 7 net-new seams:

| Domain | Tool | Delegate (evolve) or net-new |
|---|---|---|
| Market data | `get_market_snapshot` | `api/state_service.py:2724 get_state(...)` (read-only aggregation) |
| | `get_ohlcv` | `api/market_data_service.py:680 YahooClient.fetch_history` / `:129 BinanceClient.fetch_klines` / `:297 KuCoinClient.fetch_klines`; canonical PIT `core/data_model/store.py:78 HistoricalStore` (`pit_records`, entity `OHLCVBar`) |
| | `get_technical_features` | `core/strategy_v2/context_builder.py:367 build_context(...)` (read-only) |
| | `get_market_regime` | `core/strategy_v2/regime_engine.py:29 RegimeEngine.detect(context)` (computed) |
| | `get_sector_regime` | **NET-NEW** read seam over `core/sectors.py:145 get_sector` + `RegimeEngine.detect` rollup (no per-sector regime table) |
| Earnings | `get_earnings_event` | `agents/economic_calendar_agent.py:1587 get_earnings_for_ticker(ticker)` |
| | `get_historical_earnings` | `core/finnhub_client.py:170 get_symbol_earnings(symbol)` |
| | `get_earnings_surprises` | **NET-NEW** read seam over finnhub `epsActual`/`epsEstimate` (or `HistoricalStore` `Earnings` rows) |
| Options | `get_options_analysis` | **NET-NEW** — `core/data_model/entities.py:162 OptionsChain` is a "Fetcher in Phase 5" stub; tool returns honest `ok=False` "fetcher lands in Phase 5" |
| Pre-earnings | `get_pre_earnings_analysis` | `agents/pre_earnings_agent.py:2303 get_state()` (read-only) |
| News | `get_news` | `agents/news_tab_agent.py:1401 get_ticker_news(ticker, max_items)` / `:836 get_dashboard_news_items(...)` |
| | `get_news_sentiment` | `agents/news_tab_agent.py:1594 get_sentiment_summary(ticker)`; engine `core/sentiment.py:16 SentimentEngine.analyze` |
| SEC | `get_sec_context` | **NET-NEW** — `core/data_model/entities.py:203 SECFiling` is a "Fetcher in Phase 10" stub; tool returns honest `ok=False` "fetcher lands in Phase 10" |
| Signals & strategy | `get_signal` | **NET-NEW** thin read seam over `core/signals.py:18 SignalManager.open_signals`/`closed_signals` (no by-id getter) |
| | `get_strategy` | `core/strategy_v2/registry.py:47 build_default_registry()` / `core/strategies_v2.py:244 get_strategy(name, config)` |
| | `get_strategy_performance` | `api/actions_service.py:1225 get_strategy_performance(symbol)` ← `core/strategy_v2/performance_cache.py:87` |
| | `compare_strategies` | `api/actions_service.py:1262 _get_strategy_comparison(...)` via `core/quick_backtester.py:30 QuickBacktester` |
| Backtest | `run_backtest` | `api/actions_service.py:1735 run_backtest(...)` (writes a history entry; does NOT place trades) |
| | `get_backtest_result` | `api/actions_service.py:276 get_backtest_history()` (read-only) |
| Portfolio & risk | `get_portfolio` | `core/capital_manager.py:833 get_positions()` / `:815 get_balance()` / `:838 get_equity_curve()`; aggregated `api/state_service.py:1380 get_capital_state` |
| | `get_position` | `core/capital_manager.py:957 get_position(position_id)` |
| | `get_portfolio_risk` | `agents/risk_agent.py:86 RiskAgent.evaluate_signal(signal)`; math `core/risk_limits.py:84 calculate_trade_risk_pct` |
| | `get_available_capital` | `core/capital_manager.py:825 get_deployable_balance()` / `:26 resolve_deployable_balance(cm)` (**§43 authoritative**) |
| | `calculate_allowed_position_size` | `core/position_sizing.py:19 compute_size_factor(...)` + cap via `risk_agent`/`risk_limits` (**§43 authoritative**) |
| History | `get_decision_history` | `core/capital_manager.py:948 get_decisions(limit)` → `core/trade_audit.py:152 get_trade_audit(limit)` |
| | `get_trade_history` | `core/execution_archive.py:300 read_executions(...)` / `:336 read_recent_executions(limit)` |
| Order proposal | `propose_order` | **NET-NEW draft seam** — must NOT reach `core/capital_manager.py:1740 open_position` or `agents/execution_agent.py:86 process_signal`; returns a draft dict from `resolve_deployable_balance` + `compute_size_factor` + `calculate_trade_risk_pct` + read-only `evaluate_capital_entry` |
| | `propose_close` | **NET-NEW draft seam** — must NOT reach `core/capital_manager.py:2184 close_position`; drafts a close from `get_position` + live price + exit reason |

**Cross-cutting facts:**
1. **No JSON-schema-for-tool-arguments convention exists.** The Phase 3A/3B providers already accept `tools: List[Dict]` and parse vendor `tool_calls` into `core/ai/provider.py:43 ToolCall(id, name, arguments)` carried in `AIResponse.tool_calls`. The `core/ai/provider.py:117 tool_call(...)` docstring explicitly states "the §39 tool registry is Phase 4." The plumbing exists; the registry/schema/dispatcher does not.
2. **Broker / order-execution surface (propose_order must NOT call):** `core/capital_manager.py:1740 open_position` (mutates balance/positions, persists, appends ledger), `:2184 close_position`, `agents/execution_agent.py:86 process_signal` (calls `evaluate_capital_entry` then `open_position` at `:486`). **No live broker** (`execution_agent.py:120` "Live trading not yet connected"). `propose_order`/`propose_close` return drafts only — guaranteed by construction (registry dispatches only to read-only/draft seams; the broker surface is excluded from the allow-list).
3. **§43 authoritative-number sources:** available capital `core/capital_manager.py:825 get_deployable_balance` / `:26 resolve_deployable_balance`; portfolio value `:47 resolve_capital_equity`; capital accounting `:820 get_capital_accounting`; allowed size `core/position_sizing.py:19 compute_size_factor` + cap `agents/risk_agent.py:190 max_position_size_pct`; loss-at-stop `core/risk_limits.py:84 calculate_trade_risk_pct` capped by `:64 configured_risk_per_trade_pct`. **The tools delegate to these; the pipeline's AUTHORITATIVE stage recalculates from these; AI-supplied numbers are stripped/rejected.**
4. **No existing tool registry / dispatcher / action-allow-list for §39.** Confirmed by the `provider.py:127` docstring. The only allowlist patterns in the codebase are unrelated domain guards (`core/content_fetcher.py:97` outbound-HTTP domain allowlist, `core/hotlist_scoring.py:7` field allowlist). **The §39 registry is net-new; it sits between the AI agent and the ~28 existing read-only service functions.**

**Implication:** Phase 4 is evolutionary. The §42 pipeline composes the existing `parse_trade_review`/`should_veto_trade`/`evaluate_capital_entry`/`evaluate_autonomous_performance`/`evaluate_autonomous_news`/`RiskAgent.evaluate_signal`/`risk_limits` pieces as stages — it does not replace them. `should_veto_trade` keeps its exact signature (the 19 pinned tests stay green). The 28 tools wrap existing service functions. The net-new pieces are: the `Tool`/`ToolRegistry` layer, the §41 `Action` enum + `Decision` schema, the §42 pipeline as one composed object, the §44 critic workflow, the `AutonomyMode` enum + enforcement, and the `agent_manager` rewire to call `generate_with_fallback` + `no_new_autonomous_trade_guard` + the pipeline + the critic behind the autonomy gate.

## 2. Architecture

```text
                         ┌───────────────────────────────────────────────────────┐
   agent_manager         │  Autonomy gate (§45): OFF/ANALYZE/RECOMMEND/         │
   (rewired, 4C)   ────▶ │  PAPER_AUTO/LIVE_ASSISTED/LIVE_AUTO — default OFF;    │
                         │  LIVE_AUTO owner-only behind §46 Paper-Auto gate;    │
                         │  §48 reads V9_RISK_CONSTITUTION, never writes it      │
                         └───────────────────────────┬───────────────────────────┘
                                                     ▼
                         ┌───────────────────────────────────────────────────────┐
                         │  generate_with_fallback (GOLD-013, 3C) → AIResponse   │
                         │  + no_new_autonomous_trade_guard (GOLD-014, 3C)        │
                         │  ModelRouter.select_model(role, tier_hint) (3C)       │
                         └───────────────────────────┬───────────────────────────┘
                                                     ▼
                         ┌───────────────────────────────────────────────────────┐
                         │  Tool loop (4A): provider.tool_call(tools=registry.   │
                         │  list_tools()) → AIResponse.tool_calls → registry.     │
                         │  dispatch(ToolCall) → ToolResult → feed back → repeat  │
                         │  (max_rounds). §40: registry is the ONLY AI surface.   │
                         └───────────────────────────┬───────────────────────────┘
                                                     ▼
                         ┌───────────────────────────────────────────────────────┐
                         │  §42 Validation pipeline (4B):                        │
                         │   PARSE → SCHEMA → SEMANTIC → AUTHORITATIVE DATA      │
                         │   CHECK → POLICY → RISK GATE                          │
                         │  PARSE/SCHEMA evolve ollama_decision.parse_trade_review │
                         │  AUTHORITATIVE = capital_entry_guard + autonomous_    │
                         │    trade_guard + §43 recalc (strip AI numbers)        │
                         │  POLICY/RISK GATE = RiskAgent.evaluate_signal +       │
                         │    risk_limits + should_veto_trade (unchanged)         │
                         │  → validated Decision (§41) or NO_TRADE              │
                         └───────────────────────────┬───────────────────────────┘
                                                     ▼
                         ┌───────────────────────────────────────────────────────┐
                         │  §44 Independent critic (4C): second                  │
                         │  generate_with_fallback on router `critic` tier,      │
                         │  challenges on 10 dimensions; can force NO_TRADE      │
                         └───────────────────────────┬───────────────────────────┘
                                                     ▼
                         ┌───────────────────────────────────────────────────────┐
                         │  Outcome: validated Decision (or NO_TRADE).          │
                         │  AI prose alone never triggers an order (§42).       │
                         │  PAPER_AUTO → paper broker; LIVE_ASSISTED → human;    │
                         │  LIVE_AUTO → gated by §46 + §74 kill switch.          │
                         └───────────────────────────────────────────────────────┘
```

The 28 §39 tools sit inside the Tool loop, each a thin wrapper over the existing service function catalogued in §1b. The `AIProvider`/`ModelRouter`/`generate_with_fallback`/`no_new_autonomous_trade_guard` layer is the Phase 3 platform (delivered, green). Phase 4 builds everything above it.

## 3. Key autonomous decisions

1. **Evolve, not rewrite (§5/§7).** The §42 pipeline composes the existing functions as stages — `parse_trade_review` (PARSE+SCHEMA), `evaluate_capital_entry` + `evaluate_autonomous_performance` + `evaluate_autonomous_news` (AUTHORITATIVE DATA CHECK), `RiskAgent.evaluate_signal` + `risk_limits.calculate_trade_risk_pct` + `should_veto_trade` (POLICY + RISK GATE). `should_veto_trade` keeps its exact `(review, config) -> bool` signature; the 19 `test_ollama_decision.py` tests + 12 `test_risk_agent.py` + 13 `test_autonomous_trade_guard.py` + 12 `test_capital_entry_guard.py` stay green unchanged. The 28 tools wrap existing service functions; the 21 with delegates are read-only pass-throughs. `ollama_decision.evaluate_trade_decision_with_ollama` stays as a legacy fallback path; the new pipeline runs alongside it, gated by config (new path on by default for the rewired call sites; legacy path available when disabled). **No existing file's behavior changes in 4A/4B; 4C's agent_manager rewire is additive (new path coexists with the legacy path).**
2. **§40 enforced by construction (the registry IS the allow-list).** The `ToolRegistry` exposes exactly 28 `Tool` instances — the §39 names, no more, no less. The AI can call only what is registered. No `subprocess`/`os.system`/`eval`/`exec`/`open()`/`sqlite3.connect`/arbitrary-HTTP/secret-reading tool is ever registered. Guard tests assert: (a) `set(registry.names()) == SPEC_TOOL_NAMES` (the 28); (b) no registered tool's execute path can spawn a subprocess or open a file (static check over the tool delegates — each delegate is a known read-only service function); (c) `propose_order`/`propose_close` are draft-only — monkeypatch `CapitalManager.open_position`/`close_position` and `ExecutionAgent.process_signal` to raise, and the tools still return a draft (no exception, no state mutation). This is the security boundary, auditable as one explicit set.
3. **§43 AI numbers are not authoritative — stripped at SCHEMA, recalculated at AUTHORITATIVE.** The `Decision` schema accepts the §41 fields (action/ticker/horizon/confidence/strategy_id/thesis/supporting_evidence/opposing_evidence/risk_flags/data_quality/requires_more_data) and **rejects/strips** any AI-supplied `price`/`pnl`/`iv`/`eps`/`portfolio_value`/`allowed_capital`/`position_size`. The pipeline's AUTHORITATIVE DATA CHECK stage recalculates allowed capital (`resolve_deployable_balance`), position size (`compute_size_factor` + `calculate_trade_risk_pct`), and portfolio risk (`RiskAgent.evaluate_signal`) from the app services. Tests assert the validated decision's numbers equal the app-service outputs exactly (the AI's numbers, if any, are discarded).
4. **§42 AI prose alone never triggers an order.** The pipeline returns a validated `Decision` (or NO_TRADE). The `propose_order`/`propose_close` tools return **drafts** (dicts), never execute. The agent_manager rewire (4C) routes the validated Decision through the autonomy gate: OFF/ANALYZE/RECOMMEND → no execution (analysis/recommendation only); PAPER_AUTO → paper broker (`CapitalManager.open_position`, already the existing path); LIVE_ASSISTED → human approval required; LIVE_AUTO → gated by §46 + §74. **The existing direct `agent_manager → evaluate_trade_decision_with_ollama → open_position` path is preserved as the legacy fallback; the new pipeline + autonomy gate is the production path.**
5. **§44 independent critic (separate model, can force NO_TRADE).** After the pipeline produces a validated Decision, a second `generate_with_fallback` call runs on the `ModelRouter` `critic` tier (a different model from the analyst — §38 ensures independence) with the Decision + supporting evidence, challenging on the 10 §44 dimensions (contradictory evidence, stale information, provider conflict, weak backtest, fragile parameters, poor sample size, excessive volatility, concentration, catalyst risk, regime mismatch). The critic returns a verdict; on a failing verdict the Decision is forced to NO_TRADE. The critic is **never** bypassed in LIVE_AUTO (config can disable it in lower modes for speed, but LIVE_AUTO requires it).
6. **§45/§46/§48 autonomy enforcement from scratch.** New `AutonomyMode` enum (OFF/ANALYZE/RECOMMEND/PAPER_AUTO/LIVE_ASSISTED/LIVE_AUTO), default OFF. `config["autonomy"]["mode"]` is the only entry point; LIVE_AUTO requires both `live_auto_owner_enabled: true` (owner-set) AND `paper_auto_acceptance_passed: true` (the §46 gate — a dedicated acceptance test suite that must pass before LIVE_AUTO is selectable). §48: the autonomy module **reads** `docs/V9_RISK_CONSTITUTION.md` and live-permission flags but **never writes** them; a test asserts the file is byte-identical after a full cycle. AI cannot change the kill switch (§74), promotion gates, critical tests, live permissions, or holdout policy — these are owner-only, file-protected (§82).
7. **Net-new tools return honest "fetcher lands in Phase N" (no §4 forbidden language).** `get_options_analysis` (Phase 5) and `get_sec_context` (Phase 10) return `ToolResult(ok=False, error="not available: options fetcher lands in Phase 5")` — explicit, not "should work" or "TODO later". The tool is wired so the Phase 5/10 fetcher plugs into the same seam. `get_sector_regime`/`get_earnings_surprises`/`get_signal` are net-new read seams over existing data sources (implemented now, not stubbed).
8. **Injectability / testability, no live network or broker in tests.** Each `Tool` takes its service delegate as an injectable callable (default: the real function), so unit tests inject fakes asserting the delegate was called with the right args and returning canned data — no live market/broker/AI calls. The dispatch loop takes an injectable provider (fake `AIProvider.tool_call` returning canned `ToolCall`s). The pipeline stages take injectable stage functions. This is how §40/§42/§43 become executable tests.

## 4. Config schema (additive — alongside preserved `config["ollama"]` and `config["ai"]`)

```jsonc
"autonomy": {                       // §45/§46/§48 — Phase 4 net-new
  "mode": "OFF",                    // OFF|ANALYZE|RECOMMEND|PAPER_AUTO|LIVE_ASSISTED|LIVE_AUTO; default OFF (NOT LIVE_AUTO)
  "live_auto_owner_enabled": false, // owner-set; LIVE_AUTO requires this AND paper_auto_acceptance_passed
  "paper_auto_acceptance_passed": false, // §46 gate; set true only by the Paper-Auto acceptance suite (a test/owner action)
  "critic_required_for_live_auto": true   // §44 critic cannot be bypassed in LIVE_AUTO
},
"ai": {
  // ...existing Phase 3 fields preserved...
  "tools": {                        // §39 — Phase 4A net-new
    "enabled": true,
    "max_rounds": 4                  // tool-loop round cap
  },
  "decision": {                      // §41/§42/§43 — Phase 4B net-new
    "strict_schema": true,           // reject (don't silently strip) AI authoritative-number fields when true
    "critic_enabled": true,          // §44 — can be disabled in low modes; forced on in LIVE_AUTO
    "critic_force_no_trade_on_fail": true
  }
}
```

`config.example.json` mirrors this; `DEFAULT_CONFIG["autonomy"]`/`["ai"]["tools"]`/`["ai"]["decision"]` in `core/config.py` provide defaults. Existing `config["ollama"]` and `config["ai"]` (providers/registry/assignment/router/fallback) are untouched.

## 5. Decomposition into sub-plans (mirrors Phase 1's 1A/1B/1C and Phase 3's 3A/3B/3C)

- **4A — Tool registry + 28 allow-listed domain tools + §40 guard + dispatch loop.** New `core/ai/tools/` package: `tool.py` (`Tool` frozen dataclass: name/description/arguments_schema/execute; `ToolResult` frozen: ok/data/error), `registry.py` (`ToolRegistry` register/get/list_tools→vendor payload/dispatch(ToolCall)→ToolResult; the 28-name explicit allow-list = the §40 boundary), `loop.py` (`run_tool_loop(provider, registry, prompt, *, max_rounds)` — generate→dispatch→feed-back), domain modules (`market_data.py`/`earnings.py`/`options.py`/`news.py`/`sec.py`/`signals_strategy.py`/`backtest.py`/`portfolio_risk.py`/`history.py`/`order_proposal.py`) each exporting its `Tool` instances wrapping the §1b delegates. `core/ai/__init__.py` re-exports. §40 guard tests (allow-list set == spec; propose_order/propose_close draft-only via monkeypatch-to-raise; no subprocess/file escape). §43 authoritative-delegation tests for `get_available_capital`/`calculate_allowed_position_size`/`get_portfolio_risk`. Honest-stub tests for `get_options_analysis`/`get_sec_context`. End-to-end dispatch-loop test with a fake provider. Baseline 1756 stays green (additive only; no existing file's behavior changes).
- **4B — §41 decision schema + §42 validation pipeline + §43 non-authoritative numbers.** New `core/ai/decision.py` (`Action` enum: WATCH/NO_TRADE/OPEN_LONG/OPEN_SHORT/HOLD/REDUCE/CLOSE; `Decision` frozen dataclass with the §41 fields; `parse_decision(raw) -> Decision` evolving `parse_trade_review`; SCHEMA strip/reject of AI authoritative-number fields per §43). New `core/ai/pipeline.py` (`ValidationResult` frozen: ok/decision/stage/reason; the 6 stage functions PARSE/SCHEMA/SEMANTIC/AUTHORITATIVE_DATA_CHECK/POLICY/RISK_GATE composing the existing `parse_trade_review`/`evaluate_capital_entry`/`evaluate_autonomous_performance`/`evaluate_autonomous_news`/`RiskAgent.evaluate_signal`/`risk_limits.calculate_trade_risk_pct`/`should_veto_trade`; `run_pipeline(raw_decision, signal, positions, *, config) -> ValidationResult`). Bridge: `ollama_decision.evaluate_trade_decision_with_ollama` may call `generate_with_fallback` (Phase 3C) instead of legacy `ollama.generate` behind a config flag (legacy path preserved). `should_veto_trade` unchanged. Tests pinning the §42 ordering, §43 strip, and the no-order-on-AI-prose guarantee.
- **4C — §44 critic + §45/§46/§48 autonomy + agent_manager rewire.** New `core/ai/critic.py` (`run_critic(decision, evidence, *, provider, router, config) -> CriticVerdict`; the 10 §44 dimensions; force NO_TRADE on fail; `critic` tier model via `ModelRouter.select_model("risk_critic", "critic")`). New `core/ai/autonomy.py` (`AutonomyMode` enum; `resolve_mode(config)` — default OFF, LIVE_AUTO requires `live_auto_owner_enabled` AND `paper_auto_acceptance_passed`; `is_execution_allowed(mode)` — OFF/ANALYZE/RECOMMEND never, PAPER_AUTO paper-only, LIVE_ASSISTED human-approval, LIVE_AUTO gated; §48 read-only constitution access). New Paper-Auto acceptance test suite (the §46 gate — a golden/acceptance test file that must pass before `paper_auto_acceptance_passed` can be true). Rewire `agent_manager` (call sites `:1433`, `:1981`) to call `generate_with_fallback` + `no_new_autonomous_trade_guard` + `run_tool_loop` + `run_pipeline` + `run_critic` behind the autonomy gate; legacy `evaluate_trade_decision_with_ollama` path preserved as fallback. §48 test asserting `docs/V9_RISK_CONSTITUTION.md` byte-identical after a cycle. Gate → Phase 4 PASS.

## 6. Risks and mitigations

- **Breaking pinned decision/risk/autonomy tests (19+12+13+12+16+11+17)** — mitigated by evolve-not-rewrite: the pipeline *calls* the existing functions; `should_veto_trade`/`evaluate_capital_entry`/`evaluate_autonomous_performance`/`evaluate_autonomous_news`/`RiskAgent.evaluate_signal` signatures are unchanged; the full regression is the gate every sub-plan.
- **Rewiring `agent_manager` (2000+ lines; call sites `:1433`/`:1981`)** — mitigated by an additive new path (`run_decision_pipeline`) coexisting with the legacy `evaluate_trade_decision_with_ollama` path; a config flag selects; the legacy path is the fallback. The rewire touches only the two call sites + the path-selection, not a rewrite of agent_manager.
- **§40 escape (AI reaching shell/SQL/broker)** — mitigated by construction (the registry is the only AI surface; explicit 28-name allow-list; broker functions excluded) + guard tests (allow-list set equality; no subprocess/file escape; `propose_order`/`propose_close` draft-only via monkeypatch-to-raise proving no reach to `open_position`/`close_position`/`process_signal`).
- **§43 AI authoritative numbers leaking into orders** — mitigated by SCHEMA strip/reject + AUTHORITATIVE recalculation + tests asserting the validated Decision's numbers equal the app-service outputs (AI numbers discarded).
- **LIVE_AUTO safety** — mitigated by default OFF + owner-only `live_auto_owner_enabled` + §46 `paper_auto_acceptance_passed` gate (a dedicated acceptance suite) + §48 read-only constitution + §74 kill switch (already in `V9_OPERATIONS.md`) + §44 critic forced on in LIVE_AUTO.
- **Net-new stubs mistaken for working features** — mitigated by honest `ok=False` "fetcher lands in Phase N" returns (no §4 forbidden language) + tests asserting that exact message.
- **Scope creep (Phase 5/10 fetchers, MLflow, §37 UI, full §49 pre-earnings rewire)** — mitigated by the explicit out-of-scope list (§7) and the 4A/4B/4C boundary.
- **Python 3.14.3 / no new deps** — Phase 4 adds no deps (§118); all tools/pipeline/autonomy are stdlib + existing V7 code.

## 7. Out of scope (deferred, with phase owners)

- Options fetcher (`OptionsChain`) → Phase 5; `get_options_analysis` returns an honest "fetcher lands in Phase 5" draft until then.
- SEC filings fetcher (`SECFiling`) → Phase 10; `get_sec_context` returns an honest "fetcher lands in Phase 10" draft until then.
- §37 model-assignment **UI** (the §108 frontend) → Phase 12 (Phase 3 shipped backend + API; Phase 4 ships the decision engine backend).
- MLflow full experiment-tracking integration → Phase 14 (manifest ADAPTER; Phase 3 recorded the AI-Gateway evaluation).
- Full §49 Pre-Earnings-as-a-decision-system rewire → partially served in 4A via `get_pre_earnings_analysis`; the full §49 rewire is a later phase.
- Autonomy **UI** (mode selector, kill-switch button) → Phase 12/18 (Phase 4 ships the backend enforcement + the §46 acceptance suite; the UI comes with the cockpit phases).

## Self-review

- **Spec coverage:** §39 (28 tools) → 4A; §40 (no unrestricted access) → 4A guard (decision 2); §41 (decision schema + actions) → 4B `Action`/`Decision`; §42 (pipeline; no order on AI prose) → 4B pipeline + decision 4; §43 (non-authoritative numbers) → 4B SCHEMA strip + AUTHORITATIVE recalc + decision 3; §44 (independent critic, NO TRADE) → 4C `run_critic` + decision 5; §45 (autonomy modes, default not LIVE_AUTO, owner-only) → 4C `AutonomyMode` + decision 6; §46 (Paper Auto before Live Auto) → 4C acceptance suite + decision 6; §47 (Risk Constitution, Phase 0 doc) → preserved, referenced; §48 (AI cannot change constitution/kill-switch/gates/critical-tests/live-perms/holdout) → 4C read-only access + §48 test + decision 6; §49 (Pre-Earnings) → partial 4A `get_pre_earnings_analysis`, full rewire deferred (§7); §7/§5 (evolve) → §1 + decisions 1/4; §4 (no forbidden language) → honest stubs (decision 7) + §4 sweep each sub-plan; §74 (kill switch) → autonomy gate + decision 6; §82/§83 (protected files, goal-hacking) → §48 read-only + decision 6; §106/§115/§117/§118/§25 honored throughout. ✓
- **Placeholder scan:** no TBD/TODO in the design; net-new stubs return explicit "fetcher lands in Phase N" (not placeholders — honest, wired seams). Config defaults are concrete. ✓
- **Ambiguity check:** "evolve not rewrite" = pipeline composes existing functions as stages, `should_veto_trade` signature unchanged, legacy path preserved (concrete decisions 1/4); "§40 by construction" = registry is the only AI surface, 28-name explicit allow-list (concrete decision 2); "AI numbers not authoritative" = SCHEMA strip + AUTHORITATIVE recalc, tests assert equality with app-service values (concrete decision 3); "LIVE_AUTO owner-only" = requires `live_auto_owner_enabled` AND `paper_auto_acceptance_passed` (concrete decision 6). ✓
- **Scope check:** focused on §39–§48 + §49 partial; decomposed into 4A/4B/4C mirroring Phase 1/3; each sub-plan produces working, testable software on its own. ✓

No issues found. Design is complete.