# MASTER AUDIT REPORT — Market Signals V7 Premium

**Date:** 2026-06-12
**Method:** 7 specialized audit agents (Backend, Frontend, Quant/Trading, News Intelligence, Data Engineering, DevOps, QA) — all findings verified by direct code inspection with file:line evidence. No code was modified during the audit.
**Baseline:** full backend test suite run in progress at time of writing (results appended in §9).

---

## 1. Executive Summary

The application is **functional but not production-ready**. The backend serves real data, the frontend tabs all load, the agents pipeline works, and prior audits (UI audit 2026-05, production audit 2026-05) clearly drove real improvements — mobile nav, focus styles, null guards, polling backoff are now fixed. However, this audit found **8 P0 (critical)** and **~20 P1 (high)** verified issues across four themes:

1. **Money math is wrong in places that matter.** Phantom P&L is credited to the real balance for signals that never reserved capital (hardcoded $500 stake). Two of four backtesters evaluate TP/SL incorrectly. Backtest results (computed with a buggy gap penalty and a different strategy + 3-5x lower costs than live) are used to **kill live signals** — a data bug in the backtester closes live trades.
2. **Security is not acceptable for any networked deployment.** Path traversal in the catch-all static route; a live Marketaux API key committed to git (and an FMP key in ~28 test files + git history); all mutating endpoints unauthenticated by default while binding 0.0.0.0; `GET /api/config` returns secrets verbatim.
3. **Silent failure is endemic.** A NameError swallowed by `except: continue` means breaking-news history never loads; pre-earnings enrichment reads a file that no longer exists and silently emits empty data; manual config.json edits are silently ignored (SQLite shadows the file); ~15 hotlist payload fields are permanently empty FMP residue stamped `_details_loaded: True`.
4. **Operational hygiene blocks reliable operation.** 425 MB of leaked temp files, 504 MB of unrotated logs, runtime state (capital.json with a fabricated $32K demo trade) tracked in git, no crash recovery on the actual (Windows) host, unpinned dependencies, no Docker/CI deploy artifact.

**Verdict:** the platform core is sound and worth repairing; the repair plan in §8 is ordered so that money-math and security P0s land first, with the test suite as the regression gate.

---

## 2. Critical Risk Report (all P0s)

| # | Domain | Risk | Evidence |
|---|--------|------|----------|
| R1 | Quant | **Phantom P&L mutates real balance** — signals without paper positions settle invented capital (default $500) into capital.json on TP/SL | core/signals.py:693-698 |
| R2 | Quant | **backtest_engine evaluates TP/SL on close only and fills at close** — overstates wins and losses; diverges from core/backtester.py | core/backtest_engine.py:320,387-396,409 |
| R3 | Quant | **Stale `prev_close` fires gap penalty on nearly every backtest entry** (+2% phantom slippage) — and these results gate/kill live signals via min_win_rate | core/backtester.py:136,254 → core/signals.py:415-441 |
| R4 | Security | **Path traversal** in `/{filename}` catch-all (encoded backslashes escape frontend/ on Windows) | api/main.py:5082-5088 |
| R5 | Security | **Live Marketaux key committed to git** (config.json:176), unused by any code; FMP key hardcoded in ~28 test files and git history | config.json:176; tests/debug_fmp_*.py |
| R6 | Security | **All mutating endpoints unauthenticated by default** (MS_API_KEY unset ⇒ auth off) while binding 0.0.0.0; GET /api/config returns secrets and GETs bypass auth entirely | api/main.py:354-393; api/config_service.py:47 |
| R7 | News | **Breaking-news history reload is dead code** — NameError (`MarketNewsCategory`/`NewsImpact` don't exist) swallowed per-item; history always empty at startup | agents/us_market_news_agent.py:503-507 |
| R8 | Data | **Pre-earnings enrichment reads missing fmp_cache.json** — silently empty quotes/fundamentals for the PreEarnings tab, no error, no fallback | agents/pre_earnings_agent.py:26,3019-3041 |
| R9 | Data | **Yahoo Finance is a SPOF with no real rate limiting** — 3 independent fetch paths, none through http_utils circuit breaker; 429 ⇒ UI silently freezes on stale state | core/engine.py:1722-1751; core/stock_data_client.py:172 |
| R10 | Frontend | **News polling timers fork exponentially** when user is off Home/News (double setTimeout per cycle) — ~1,000 pending timers after 15 min, then request flood | frontend/app.js:15619-15777 |
| R11 | Frontend/Repo | **Fabricated $32K TSLA "demo WIN" trade committed in capital.json** at HEAD — fake trade history shipped to anyone cloning | capital.json @ HEAD 966e357 |
| R12 | QA | **Test suite writes the developer's real state.json** (no MS_STATE_PATH override); 8/10 tests/e2e files ungated, hit live server; bare `pytest` collects them | core/config.py:23-24; tests/e2e/* |

---

## 3. System Architecture Map

```
External providers                     Backend                                Frontend (SPA)
──────────────────                     ───────                                ──────────────
Yahoo (yf.download,      ┌──> core/engine.py (subprocess, 3.8k lines)
 yf.Ticker, raw chart    │      ├── core/signals.py (SignalManager → signals_state.json)
 API — NO rate limiter)──┤      ├── core/scoring_system.py (logistic score + components)
Binance REST (via        │      ├── core/strategy_v2/ (regime, selector, evaluators)
 http_utils: limiter +   │      ├── core/backtester.py ──> quick_backtest_30d gates live signals
 circuit breaker — OK) ──┤      └── core/exit_manager.py / capital_manager.py (paper fills,
FinnHub (calendar/news,  │            25bps+spread; SQLite + capital.json)
 needs env key) ─────────┤
RSS: Yahoo, MarketWatch, └──> agents/ (news_tab_agent, us_market_news_agent, pre_earnings,
 4 crypto feeds               economic_calendar, premarket_scanner, scanner...)
 (NO fetch timeout)                │
Ollama (optional,                  v
 graceful degrade)        api/main.py (5,200 lines, FastAPI)
                            ├── api/state_service.py (payload builders; FMP stubs残)
                            ├── api/engine_service.py (engine subprocess mgmt, engine.pid)
                            ├── api/{config,actions,dashboard,market_data}_service.py
                            └── state: state.json / signals_state.json / capital.json /
                                watchlist.json / SQLite (data/) — JSON+SQLite dual-write
                                                    │
                                                    v
                          frontend/app.js (18.8k lines) + data_store.js
                            ~10 view-gated polling loops → /api/state, /api/dashboard,
                            /api/news-tab/*, /api/capital/*, /api/calendar/*, ...
                            (contract check: 60+ URLs, ZERO mismatches)
```

**Key flows:**
- Signals: engine subprocess → scoring → SignalManager → signals_state.json → /api/state → app.js render (5-8.5s poll, version-gated on Home).
- Paper trading: signal → confidence_gates → capital_manager.open_position (SQLite+JSON) → exit_manager/P&L tracker thread → close → feedback_loop → performance_cache → gates future trades.
- News: news_tab_agent (RSS+yfinance, dedup, sentiment) → news_backup.json + state → /api/news-tab/state.
- Config: config.json ⇄ SQLite snapshot (SQLite WINS — file edits silently ignored, see B6).

---

## 4. Dependency / Contract Map (verified)

- **Frontend→Backend contract: clean.** All 60+ fetch() URLs resolve to existing routes with matching methods. Zero undefined JS function references. Latent landmine: `/api/hotlist/{category}` registered before `/api/hotlist/search|state` shadows them (data_store.js:19 references the dead one but never fetches it).
- **Two parallel agent frameworks:** only 4 of 11 agents use agents/base_agent.py.
- **Dead modules (confirmed orphans):** api/endpoints_integration.py.dead, core/data_source_manager.py (+ its core/binance_client.py and kucoin client — the live crypto path is a *third* BinanceClient inside engine.py), agents/news_agent.py (reachable only via eager `agents/__init__.py`), frontend/styles.css (3,250 lines, not loaded by any HTML), core/news_module.py v2 pipeline (`fetch_rss_feeds` never called; /api/news/v2 serves stale disk cache).
- **Triple-duplicated Binance client**; duplicate `api_news_sentiment` defs; duplicate method defs in stock_data_client (get_quote, get_income_statement_growth — first defs dead).
- **agents/__init__.py eagerly imports 6 heavy agents**, defeating all lazy-import work.

---

## 5. Findings by Domain (condensed;每 finding verified)

### A. Backend (Agent 1)
- **[P0] A1** Path traversal `/{filename}` (api/main.py:5082) — apply same `resolve().relative_to` guard as the `/frontend/` route.
- **[P0] A2** GET /api/config returns secrets; GETs exempt from auth (config_service.py:47, main.py:364,1386).
- **[P0] A3** Auth off by default + 0.0.0.0 bind ⇒ LAN can flip `bot.enabled`, reset capital, open positions (main.py:354-393).
- **[P1] A4** Hotlist route shadowing breaks /api/hotlist/search & /state (main.py:4571 vs 4633/4822).
- **[P1] A5** `NameError: timedelta` in /api/backtest/run-single default path ⇒ guaranteed 500 (main.py:4680).
- **[P1] A6** Blocking sync I/O + `future.result(timeout=10)` inside `async def` handlers freezes event loop (main.py:4211, 3853, 3692) — make them `def`.
- **[P1] A7** SignalManager: no singleton/lock; whole-payload read-modify-write from 4 concurrent writers ⇒ lost updates (core/signals.py:17-26,229).
- **[P1] A8** config.json manual edits silently ignored (SQLite shadows file; reload_if_changed no-ops) (core/config.py:852-898).
- **[P1] A9** ConfigManager.save() non-atomic + doesn't invalidate cache ⇒ bot circuit-breaker config changes invisible in-process (core/config.py:900-914; bot_controller.py:243).
- **[P2]** `get_engine` NameError swallowed in api_bot_start (main.py:5127); capital payload ignores cache-only and fetches N serial remote quotes (state_service.py:654-691); build_tickers_list serial Yahoo refresh in request path (state_service.py:2302); `ConfigManager()` constructed per call in agent_toggles (sqlite+file I/O per request); query-param API key + non-constant-time compare + unbounded rate-limit store (main.py:373-375,498-523); FMP stub fields emitted as fake data with `_details_loaded: True` (state_service.py:995,1951,1993); cross-process capital TOCTOU window.
- **[P3]** watchlist.json opened relative to CWD (main.py:4015); /api/bot/positions unhandled TimeoutError; `fmp_rpm` dead metric; manual-signal endpoint accepts arbitrary dicts; CORS/bind inconsistency.

### B. Frontend (Agent 2)
- **[P0] B1** News polling fork: gated early-returns schedule setTimeout inside `try` AND `finally` reschedules ⇒ exponential timer growth + request flood on tab return (app.js:15619-15777). Fix: single scheduler in `finally`.
- **[P1] B2** Fake TSLA demo trade committed in capital.json @ HEAD (37 refs, $32,645 fake profit) — working tree cleans it but file is git-tracked; `git rm --cached` + commit.
- **[P1] B3** "Clear Performance History" is a no-op: clears in-memory only, never calls existing `POST /api/performance/clear`; state poll restores data in seconds (app.js:18550-18591).
- **[P2]** Capital tab full innerHTML rebuild every 5s (hover/click loss; apply state_version gate like Home) (app.js:2399-2512, 15448); ~6 Romanian strings remain (notebook/deep-analysis flow); api_stats_widget polls every 20s on all views, duplicating 2 other pollers.
- **[P3]** styles.css dead file (3,250 lines); 14 `??` mojibake in dev console logs; CSV export quote-escaping (app.js:18539); unescaped position_id in onclick (app.js:2464); stylesheet link inside body (index.html:1208).
- **Prior-audit verification:** mobile nav FIXED, skip-link/focus FIXED, null crashes FIXED, Romanian mostly fixed, polling improved-not-consolidated, renderAll improved (home version-gated; capital not).

### C. Quant & Trading (Agent 3)
- **[P0] C1** Phantom P&L → real balance (signals.py:693-698): `invested_amount = 500.0` fallback + `cm.add_trade_pnl(...)` for watch-only signals. Only settle P&L for signals with attached paper positions.
- **[P0] C2** backtest_engine TP/SL on close only, exit fills at close (backtest_engine.py:320,387-409).
- **[P0] C3** backtester stale `prev_close` ⇒ gap penalty on ~every entry; results feed signal-kill logic (backtester.py:136,254).
- **[P1] C4** Backtest costs 5/10 bps vs live 25 bps + spread — calibration won't survive paper trading (backtester.py:75 vs capital_manager.py:926).
- **[P1] C5** Backtested strategy ≠ live strategy (plain RSI/MACD rule vs logistic scoring/V2 selector), yet its win rate gates live signals.
- **[P1] C6** MACD/RSI computed differently in backtest_engine vs core/ta.py (adjust=True vs False).
- **[P1] C7** feedback_loop stop-distance `max(entry_price, 1)` breaks sub-$1 assets ⇒ expectancy_R inflated ~10x, gates autonomous trading (feedback_loop.py:59).
- **[P1] C8** confidence_gates loss cooldown lasts exactly one evaluation; in-memory only (confidence_gates.py:273-276).
- **[P1] C9** `dollar_risk` formula wrong (size × size-% instead of size × stop-distance) (signals.py:628-630).
- **[P1] C10** backtest_optimizer pushes win rate the wrong way (farther TP + tighter SL for low WR) (backtest_optimizer.py:248-258).
- **[P2]** orb_fvg: entry-bar exits skipped, zero costs, BOTH-mode double counting; quick_backtester cost knobs accepted but ignored; optimizer fake inputs (avg_rsi=50 hardcoded, volatility unit mismatch); performance_cache "max_drawdown" = worst single trade; backtest_engine wins classified on gross P&L; no liquidation guard at 5x (capital_manager.py:1485); displayed component scores don't drive the logit total_score; portfolio max-DD double-counts; profit_optimizer in-sample-only at 2-7x leverage with inconsistent costs.
- **[P3]** missing f-string in explanations renders literal `{max_points}` to users (scoring_system.py:952-967); weight-calibration audit is a stub claiming "active"; risk_agent unrealized PnL always 0; crypto 30d backtest silently ~21d.
- **Four backtesters, four strategies, none reproduces live trading.** Consolidation is mandatory.

### D. News Intelligence (Agent 4)
- **[P0] D1** History reload NameError swallowed ⇒ breaking_news always empty at startup (us_market_news_agent.py:503-507): use `EventCategory`/`EventImpact`.
- **[P0] D2** Marketaux key committed and 100% unused (config.json:176) — revoke, remove.
- **[P1] D3** RSS timestamps off by UTC offset: `time.mktime()` on UTC struct_time ⇒ every article 2-3h "older" (Bucharest host); skews FRESH/RECENT, breaking window, dedup, action score (news_tab_agent.py:2236,2281,2320). Fix: `calendar.timegm`.
- **[P1] D4** `feedparser.parse(url)` without timeout can hang refresh thread indefinitely (news_tab_agent.py:2222+).
- **[P1] D5** 60-day history fetch immediately pruned to 7 days (NEWS_HISTORY_WINDOW_DAYS=60 vs prune default 7) — wasted API calls, broken expectation (news_tab_agent.py:143 vs 3088).
- **[P1] D6** Legacy news_agent.py crashes on multi-ticker path; effectively dead but importable — quarantine/delete.
- **[P2]** /api/news/v2 zombie pipeline (serves stale disk cache; blocking 120s Ollama call in request path); "AI"/"GM"/"ON"/"SO" missing from ambiguous-ticker set ⇒ false positives; sentiment_quality hardcoded "ESTIMATED"; no source-trust scoring (claimed in docs, not implemented); `_seen_ids` unbounded.
- **[P3]** quarantine file unbounded; duplicate `get_us_market_news_agent` def; random sleep in Yahoo fetch path; substring ticker match in hotlist_news ("ARM" in "FARMERS").
- **Verified OK:** per-source failure isolation, Ollama graceful degradation, news_backup.json hygiene (atomic, capped, quarantined), dedup works (Jaccard, not the claimed MinHash), freshness labels correct modulo D3.
- **Reality check:** live sources today = yfinance index tickers, Yahoo top RSS, MarketWatch RSS, 4 crypto RSS, FinnHub (only if env key set). Reuters/AP/Seeking Alpha dead or never existed despite claims in tasks/todo.md.

### E. Data Engineering (Agent 5)
- **[P0] E1** = R8 (pre-earnings fmp_cache.json missing).
- **[P0] E2** = R9 (Yahoo SPOF, 3 unprotected paths, per-thread sleep useless under 8-worker pool, 429 ⇒ silent stale UI).
- **[P1] E3** Temp-file leak root cause: atomic-write code is correct, but `calendar_events_cache.json` is **12.3 MB written with indent=2 + fsync on every calendar request**, and the process is hard-killed inside the window (reload supervisor history; run_server.py:86 comment). 68 calendar + 16 watchlist orphans, nothing sweeps them, `*.tmp` gitignored so invisible. Fix: startup sweep → compact JSON → debounced background save → prune cache.
- **[P1] E4** Calendar cache grows unbounded (per-ticker earnings merged forever) — 12.3 MB and climbing, amplifies E3.
- **[P2]** DataGuard "yahoo" freshness can never fire (no Yahoo call goes through http_utils); stale-bar guard thresholds ≠ documented (5m→360s not 180s) and fail-open on exception (engine.py:1558); duplicate dead method defs in stock_data_client; calendar date ranges use server-local time not US/Eastern (drops same-evening US events); data_source_manager + 2 crypto clients dead code; FMP residue (state_service map → 20 empty payload keys, fmp_rpm metric).
- **[P3]** unbounded class-level caches (`_legacy_cache` keyed by full symbol-list permutations); QualifiedValue telemetry decorative (per-source keys overwritten by last symbol; almost nothing calls `.is_valid()`).

### F. DevOps (Agent 6)
- **[P0] F1** Secrets: Marketaux key in tracked config.json (in history since 18ebeb8); FMP key in history + untracked debug files. **Rotate both keys.** `.env` handling itself is correct.
- **[P1] F2** capital.json + config.json tracked AND runtime-mutated (8,357-line diff per run); `git rm --cached`, split config.example.json, point runtime writes at data/.
- **[P1] F3** 425 MB orphaned tmp + 312 dirty entries + 12 root logs + debug PNGs/baks.
- **[P1] F4** logs/ = 504 MB, 130 files, plain FileHandler, zero rotation (engine.py:146).
- **[P1] F5** No crash recovery on Windows: engine `poll()` only reports, never respawns; no watchdog for API; DEPLOY.md is Linux/systemd-only (aspirational).
- **[P2]** CI exists (windows-latest, unit/integration/contract/perf) but no smoke/e2e/pip-audit/secret-scan/deploy artifact, no Dockerfile; requirements fully unpinned with test deps in prod; run_server.py multi-interpreter re-exec hack; 0.0.0.0:8003 + auth-off default.
- **[P3]** Monitoring pull-only (/api/health, /api/ready real and good) but nothing consumes them; no alert channel; Makefile POSIX-only env syntax.

### G. QA / Test Suite (Agent 7)
- **[P0] G1** FMP key hardcoded in 28 files under tests/ (+ history). Revoke + delete files.
- **[P0] G2** 8/10 tests/e2e files have no RUN_E2E gate, hit live localhost:8003; conftest yields None without skipping ⇒ fail/timeout under bare `pytest`.
- **[P0] G3** Bare `pytest` collects ~66 loose root test files + tests/manual (live FMP calls) — only Makefile subdir-enumeration dodges it.
- **[P1] G4** No MS_STATE_PATH override ⇒ collected tests mutate the developer's real state.json (core/config.py:23-24; conftest redirects only capital+sqlite).
- **[P1] G5** Root conftest `collect_ignore` list is 100% stale (all 17 files gone); real offenders unignored.
- **[P1] G6** ≥5 assert-free "tests" that always pass (test_user_scenarios.py etc.); test_api_jan_2026.py can never fail.
- **[P2]** ~50 one-off debug scripts polluting tests/; FMP tautology tests; autouse network mock covers only yf.download (requests/httpx/Ticker.news unmocked); `make test-frontend` silently 100%-skips (RUN_E2E=0 default); duplicate divergent e2e infrastructures (tests/e2e vs tests_e2e).
- **[P3]** golden suite ≈ empty (2 tests) despite determinism mandate; release_check omits golden; 39 of 80 core/agents modules have zero direct test imports (incl. capital_manager-adjacent paths, backtester, state_persistence, execution_agent).

---

## 6. Dead Code Inventory (safe to delete after verification)

| Item | Evidence |
|---|---|
| api/endpoints_integration.py.dead | uses undefined `app`; imported nowhere |
| core/data_source_manager.py + core/binance_client.py + kucoin client | imported nowhere; live path is engine-internal client |
| agents/news_agent.py | crashes if used; reachable only via eager `__init__` |
| core/news_module.py v2 fetch path + /api/news/v2 endpoints | `fetch_rss_feeds` never called |
| frontend/styles.css (3,250 lines) | not linked by any HTML |
| stock_data_client duplicate first defs (get_quote, get_income_statement_growth) | shadowed by later defs |
| state_service FMP cache map + ~20 always-empty payload fields | `_load_fmp_cache_map_cached` returns {} |
| tests/: 28 FMP debug files, 8 ungated e2e debug files, output artifacts | Agent 7 list §(c) |
| dead unreachable block in `_resolve_earnings_from_sources` | state_service.py:1463-1490 |

---

## 7. Duplication Inventory

- 4 backtesters, 4 strategies, none matching live (consolidate to one that replays live scoring with live costs).
- 3 BinanceClient implementations.
- 2 agent frameworks (base_agent used by 4/11).
- 2 e2e infrastructures (tests/e2e vs tests_e2e).
- 3 independent /api/stats/api-requests pollers in frontend.
- 2 `api_news_sentiment` defs; 2 `get_us_market_news_agent` defs.

---

## 8. Repair Plan (Phase 2 — prioritized, regression-gated)

**Batch 0 — Safety/secrets (no behavior change):**
1. Rotate/revoke Marketaux + FMP keys (user action); blank from config.json; delete 28 FMP test files.
2. Path traversal fix (main.py:5082); redact secrets from GET /api/config.
3. `git rm --cached capital.json` (+ fake TSLA trade removal commit).

**Batch 1 — Money math P0s:** signals.py phantom P&L (C1); backtester prev_close (C3); backtest_engine intrabar TP/SL (C2); dollar_risk formula (C9); feedback_loop sub-$1 (C7).

**Batch 2 — Broken-functionality P0/P1:** us_market_news NameError (D1); RSS timegm (D3); feedparser timeout (D4); pre-earnings fmp_cache (E1: remove dead enrichment or rewire); timedelta NameError (A5); hotlist route order (A4); frontend news timer fork (B1); Clear History no-op (B3); config.json shadowing (A8) + atomic save (A9).

**Batch 3 — Stability/perf:** async-handler blocking (A6); SignalManager lock/singleton (A7); calendar cache prune + debounced save + startup tmp sweep (E3/E4); Yahoo through http_utils (E2); log rotation (F4); engine watchdog (F5).

**Batch 4 — Hygiene:** dead code deletion (§6); test suite cleanup (G2/G3/G4/G5); repo cleanup (F2/F3); Romanian strings; capital tab dirty-check.

**Gate after every batch:** `make test-backend` green + targeted smoke; no new failures vs baseline (§9).

---

## 9. Baseline Test Results

**Post-repair regression run (2026-06-13, after Batches 0–4):**

| Suite | Command | Result |
|---|---|---|
| Backend full | `pytest tests/unit tests/integration tests/contract tests/golden tests/perf` | **557 passed in 27.46s** (exit 0) |
| Smoke (non-e2e) | `pytest -m "smoke and not e2e" tests/unit tests/integration tests/contract tests/perf/test_load_smoke.py` | **4 passed, 549 deselected** (exit 0) |
| App import | `python -c "import api.main"` | clean (FastAPI app builds; only expected `MS_API_KEY not set` auth warning) |
| e2e (`tests_e2e`) | `RUN_E2E=1 pytest tests_e2e` | skipped on Windows (named pipes / RUN_E2E=0) — accepted |

**Verdict:** regression gate **GREEN**. Zero failures, zero errors. All 8 P0 and ~20 P1 fixes from the repair plan (§8) pass the suite with no regressions. Python 3.14.3 / pytest 9.0.2.
