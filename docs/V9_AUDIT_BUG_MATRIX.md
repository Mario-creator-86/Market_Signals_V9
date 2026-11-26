# Market Scanner V9 — Audit Bug Matrix

Severity: P0 critical/safety; P1 major; P2 important non-blocking; P3 minor. Final state: 19 repaired findings PASS, 1 environment item `BLOCKED_EXTERNAL`, zero unresolved P0/P1 implementation defects.

## AUD-001 — Mutable runtime data tracked by Git

- Severity: P1
- Component: persistence/repository hygiene
- User-visible symptom: routine operation permanently dirtied the worktree and mixed owner data with source.
- Reproduction steps: start the app, then run `git status --short` and inspect tracked `data/*.json`, SQLite and `watchlist.json`.
- Expected behavior: source/fixtures are immutable; mutable owner state lives in ignored runtime roots.
- Actual behavior: 12 tracked state files and 18 untracked runtime paths changed during operation.
- Frontend evidence: watchlist/capital/news state wrote back to source-controlled paths.
- Backend evidence: local defaults contradicted Compose's `/app/runtime` and `/app/data` volumes.
- Logs: normal workers recorded successful writes into project-root/data paths.
- Root cause: inconsistent path defaults and no migration boundary.
- Files involved: `core/runtime_layout.py`, `core/config.py`, API/agents/run server, `.gitignore`, mutable data/watchlist paths.
- Fix: dedicated roots, atomic non-destructive migration, archive-on-conflict, mutable files removed from the Git index. Owner data and root originals were preserved.
- Regression test: `tests/unit/test_runtime_layout.py` and path contract suite.
- Status: PASS

## AUD-002 — Local/production Python drift

- Severity: P2
- Component: environment
- User-visible symptom: local evidence runs on Python 3.14.3 while deployment declares 3.12.
- Reproduction steps: compare `python --version`, `py -0p`, `.python-version` and Docker base image.
- Expected behavior: verify on the declared interpreter/image.
- Actual behavior: installed interpreters are 3.14 and 3.11; Docker is unavailable.
- Frontend evidence: not applicable.
- Backend evidence: the full suite is clean on 3.14.3; container contracts retain 3.12.
- Logs: `docker` command not found; `py -0p` has no 3.12.
- Root cause: local toolchain drift, not an application defect.
- Files involved: `.python-version`, Docker/Compose and operations evidence.
- Fix: kept the 3.12 production pin and made the limitation explicit; did not weaken the contract.
- Regression test: release/container deployment contracts (8/8 PASS).
- Status: BLOCKED_EXTERNAL

## AUD-003 — E2E/release server entrypoint missing

- Severity: P1
- Component: E2E/release
- User-visible symptom: browser tests timed out before opening the page and release check failed.
- Reproduction steps: run E2E with `RUN_E2E=1` at the baseline commit.
- Expected behavior: isolated test server starts on the fixture-selected port.
- Actual behavior: both fixtures launched nonexistent `run_server_test.py` and hid child stderr until timeout.
- Frontend evidence: no browser test reached the UI.
- Backend evidence: file absent; readiness polling outlived the failed child.
- Logs: E2E setup timeout; release check 10/11.
- Root cause: uncommitted/never-created test entrypoint plus non-fail-fast fixtures.
- Files involved: `run_server_test.py`, `tests_e2e/conftest.py`, `tests/e2e/conftest.py`.
- Fix: isolated temporary runtime server, absolute paths, fail-fast child diagnostics.
- Regression test: `test_e2e_server_entrypoint.py`; canonical E2E.
- Status: PASS

## AUD-004 — Cache-only Pre-Earnings starts remote work

- Severity: P1
- Component: Economic Calendar/Pre-Earnings
- User-visible symptom: cache reads caused latency, provider calls and shutdown logging errors.
- Reproduction steps: call Pre-Earnings `get_state()` with stale calendar cache and `allow_remote=False`.
- Expected behavior: bounded local read with no singleton/provider/thread initialization.
- Actual behavior: calendar population scheduled a daemon remote warm; suite latency reached 489ms and leaked logging after capture closed.
- Frontend evidence: Cockpit and `/api/state` were slow during startup contention.
- Backend evidence: remote calendar calls occurred from a cache-only path.
- Logs: repeated calendar calls and `ValueError: I/O operation on closed file` after pytest.
- Root cause: contradictory `allow_remote=False` behavior and eager calendar singleton construction.
- Files involved: `agents/economic_calendar_agent.py`, `agents/pre_earnings_agent.py`.
- Fix: strict cache-only return, lazy singleton, remote work only on explicit single-flight refresh.
- Regression test: economic-calendar dedup and Pre-Earnings core hardening suites.
- Status: PASS

## AUD-005 — Provider request storm/invalid crypto routing

- Severity: P1
- Component: providers/scanner/live prices
- User-visible symptom: 5.5–25s watchlist refresh, DEGRADED health and repeated Binance HTTP 400.
- Reproduction steps: full-stack startup with dashed KuCoin tickers and observe prices/sparklines for two minutes.
- Expected behavior: route symbols/intervals to compatible venue, cache failures and bound calls.
- Actual behavior: `XXX-USDT` went to Binance first and KuCoin received `15m` instead of `15min`; UI forced strict remote refresh.
- Frontend evidence: watchlist timeout and stale/degraded status.
- Backend evidence: repeated invalid-symbol probes and absent reusable candle cache.
- Logs: Binance 400 storm; provider budgets depleted.
- Root cause: venue-agnostic symbol/interval ordering plus implicit remote-on-miss behavior.
- Files involved: `api/market_data_service.py`, `api/main.py`, `frontend/app.js`, scanner/state services.
- Fix: KuCoin-first dashed pairs, native intervals, one-hour Binance negative cache, explicit remote policy, cache-first UI.
- Regression test: market-data crypto fallback, sparkline venue and live-price tests.
- Status: PASS

## AUD-006 — Expired Pre-Earnings rows shown as active

- Severity: P1
- Component: Pre-Earnings/Cockpit
- User-visible symptom: on 2026-08-20, rows dated 2026-08-19 led the active matrix.
- Reproduction steps: open Cockpit after loading the baseline persisted snapshot.
- Expected behavior: active candidates are causal upcoming/current-session events; expired rows move to history/reported state.
- Actual behavior: past unresolved rows outranked valid future reporters.
- Frontend evidence: first displayed rows carried `past date unresolved`.
- Backend evidence: active state retained expired event sessions.
- Logs: no exception; semantic freshness error.
- Root cause: ranking normalized scores without a session-aware active filter.
- Files involved: `agents/pre_earnings_agent.py` and Cockpit adapter.
- Fix: filter by effective event session and merge eligible items into `reported_today` separately.
- Regression test: current-date stale-row hardening test.
- Status: PASS

## AUD-007 — V9 identifies itself as V7

- Severity: P3
- Component: metadata/observability/UI
- User-visible symptom: startup and FastAPI metadata advertised V7.
- Reproduction steps: start server and inspect banner, app title/version and frontend metadata.
- Expected behavior: consistent V9 identity.
- Actual behavior: stale V7 literals remained after migration.
- Frontend evidence: product/version labeling was inconsistent.
- Backend evidence: FastAPI title and startup log used V7.
- Logs: `Market Signals V7 - Web API Starting`.
- Root cause: hard-coded legacy metadata.
- Files involved: `run_server.py`, `api/main.py`, observability, frontend, Compose and env sample.
- Fix: consistent V9 title/version/banner and contract.
- Regression test: `tests/contract/test_v9_metadata_contract.py`.
- Status: PASS

## AUD-008 — Test lifespan starts real background jobs

- Severity: P1
- Component: test isolation/lifespan
- User-visible symptom: tests could consume provider quotas, mutate real state and become order-dependent.
- Reproduction steps: create a TestClient with `TEST_MODE=1` and trace lifespan startup.
- Expected behavior: no engine, sweep, scheduler, prefetch or remote work.
- Actual behavior: disaster/research/Pre-Earnings work could start before the test-mode guard.
- Frontend evidence: E2E state could change asynchronously under the browser.
- Backend evidence: lifespan order invoked production background setup in tests.
- Logs: nondeterministic provider and scheduler messages during test runs.
- Root cause: isolation guard was too late and incomplete.
- Files involved: `api/main.py`, `run_server_test.py`.
- Fix: guard before sweeps; disable engine, warmup, prefetch, background workers, scanner lazy refresh and live-price remote access.
- Regression test: `test_test_mode_lifespan_isolation.py`.
- Status: PASS

## AUD-009 — Test state contamination/operator-data risk

- Severity: P1
- Component: capital/ledger tests
- User-visible symptom: combined suites behaved differently and could touch operator databases.
- Reproduction steps: run capital/risk/execution and ledger tests together in varying order.
- Expected behavior: temporary per-test roots and restored environment/singletons.
- Actual behavior: tests referenced global `DATA_DIR`, reused ledger singleton and removed inherited suite variables.
- Frontend evidence: not directly visible; persisted API state could be contaminated.
- Backend evidence: combined suite exposed failures that isolated files did not.
- Logs: path/state mismatches rather than runtime exceptions.
- Root cause: incomplete fixture isolation.
- Files involved: capital live-quote, decision-ledger store and ledger API fixtures.
- Fix: temp data paths, isolated singleton/store and monkeypatch restoration.
- Regression test: 334 combined capital/risk/execution tests; focused ledger suite.
- Status: PASS

## AUD-010 — Cache-only scanner/hotlist escapes to remote work

- Severity: P1
- Component: scanner/hotlist/state service
- User-visible symptom: routine reads spawned hidden refreshes and added startup contention.
- Reproduction steps: request scanner/hotlist state with forced cache-only and patch provider/build calls.
- Expected behavior: zero lazy refresh/build/provider calls.
- Actual behavior: scanner scheduled refresh and hotlist detail builder ignored request attribution.
- Frontend evidence: slow watchlist/hotlist response under full-stack load.
- Backend evidence: remote-capable work occurred after cache-only request.
- Logs: extra provider activity without an explicit refresh action.
- Root cause: remote permission was not propagated through nested builders.
- Files involved: `api/main.py`, `api/state_service.py`.
- Fix: disable lazy refresh on forced cache and pass `allow_build=allow_remote`.
- Regression test: `tests/integration/test_ui_cache_only_guard.py`.
- Status: PASS

## AUD-011 — E2E contracts stale and async work leaks

- Severity: P2
- Component: UI/E2E
- User-visible symptom: Risk, Pre-Earnings and OOS tests failed despite current APIs, with a transient OOS empty title.
- Reproduction steps: run the canonical 17-test browser suite after enabling the missing server.
- Expected behavior: tests assert current owner-facing contracts and await bounded state.
- Actual behavior: old endpoint/schema assumptions and a real heavy async backtest leaked across assertions.
- Frontend evidence: three browser failures in first repaired-server run.
- Backend evidence: out-of-order expectation contradicted single-flight; backtest work remained running.
- Logs: transient OOS response race.
- Root cause: regression suite had not evolved with API contracts.
- Files involved: `tests_e2e/test_quality_tabs.py`, `tests_e2e/test_ui_flows.py`.
- Fix: current contracts, deterministic route stubs and explicit UI waits.
- Regression test: full E2E 17/17.
- Status: PASS

## AUD-012 — Routine live-price polling is strict remote

- Severity: P1
- Component: frontend/live-price API
- User-visible symptom: routine watchlist refresh blocked for seconds and could time out at 25s.
- Reproduction steps: load main UI with 20 watchlist symbols and inspect `/api/live-prices` query/latency.
- Expected behavior: fast cache-first read; remote refresh only on an explicit action/job.
- Actual behavior: frontend sent `remote=true&cache_fallback=false`; old bundle cache prolonged behavior.
- Frontend evidence: timeout warning and repeated network latency.
- Backend evidence: one request fanned out to many live providers.
- Logs: remote live-price latency around 5.5s during baseline.
- Root cause: strict remote defaults in a periodic UI task.
- Files involved: `frontend/app.js`, `frontend/index.html`, `api/main.py`.
- Fix: `remote=false`, cache fallback true, API honors explicit false, asset version `36.23`.
- Regression test: live-price null/cap, cache-only contract and E2E.
- Status: PASS

## AUD-013 — News refresh plans beyond external-call budget

- Severity: P2
- Component: News provider orchestration
- User-visible symptom: health degraded even though the agent had a documented 20-call budget.
- Reproduction steps: run a full news refresh with default feeds/watchlist and inspect provider gate counters.
- Expected behavior: plan only calls that fit remaining budget, preferring fresh cache.
- Actual behavior: plan attempted extra feeds/tickers; gate blocked three after dispatch planning.
- Frontend evidence: health card showed DEGRADED.
- Backend evidence: `external_fetches=20`, `blocked=3` in baseline refresh.
- Logs: provider-budget blocked events.
- Root cause: budget enforcement occurred at HTTP gate, not during work planning.
- Files involved: `agents/news_tab_agent.py`.
- Fix: remaining-budget estimators and cache-freshness checks before every market/feed/ticker/announcement step.
- Regression test: news refresh single-flight/budget tests.
- Status: PASS

## AUD-014 — Dead default Bitcoin Magazine feed

- Severity: P3
- Component: News sources
- User-visible symptom: every refresh logged a predictable source failure.
- Reproduction steps: request the configured Bitcoin Magazine RSS endpoint.
- Expected behavior: active defaults are reachable or have a valid fallback.
- Actual behavior: endpoint consistently returned HTTP 403.
- Frontend evidence: no unique article loss was exposed; alternate crypto sources remained.
- Backend evidence: one generic provider error per news cycle.
- Logs: Bitcoin Magazine RSS 403.
- Root cause: obsolete/blocked endpoint remained in the default feed set.
- Files involved: `agents/news_tab_agent.py`.
- Fix: removed the dead default while retaining other crypto RSS and announcement sources.
- Regression test: News refresh source/budget suite.
- Status: PASS

## AUD-015 — Strategy Research continues after provider budget exhaustion

- Severity: P2
- Component: Strategy Research/provider governance
- User-visible symptom: otherwise healthy live startup reported `DEGRADED` because Strategy Research accumulated predictable blocked provider calls.
- Reproduction steps: run a forced research cycle with more symbol/strategy/direction combinations than its external-call budget, then inspect `/api/health/diagnostics`.
- Expected behavior: stop scheduling remote work at the shared budget boundary and defer the remaining Cartesian product to a later cycle.
- Actual behavior: the HTTP gate rejected every later combination, producing 25 blocked calls after the 15-call allowance was consumed.
- Frontend evidence: Cockpit System Health inherited the degraded diagnostic state.
- Backend evidence: Strategy Research iterated symbol × strategy × LONG/SHORT without consulting the remaining request budget.
- Logs: `external_fetches=15`, `external_budget=15`, `blocked=25` before repair.
- Root cause: budget enforcement existed only at the provider gate, after research work had already been planned.
- Files involved: `agents/strategy_research_agent.py`, `tests/unit/test_strategy_research_provenance.py`.
- Fix: inspect the current request-context budget before each walk-forward call, stop cleanly at zero and expose explicit deferred/skipped evidence.
- Regression test: two-call boundary test proves zero blocked calls; focused Strategy Research/diagnostics suite passes.
- Status: PASS

## AUD-016 — Capital tab ARIA state and Premarket AI failure copy drift

- Severity: P3
- Component: frontend/accessibility/operational messaging
- User-visible symptom: Capital rendered correctly but remained `aria-selected=false`; a transient AI timeout was described only as "AI off or no cloud model".
- Reproduction steps: navigate to Capital in a real browser and inspect the accessible tab state; run Premarket through a bounded AI timeout and inspect the pulse message.
- Expected behavior: visual and accessible tab state agree, and transient provider failures are described honestly.
- Actual behavior: the special Capital router added only the visual class, while Premarket copy omitted timeout/unavailable cases.
- Frontend evidence: live DOM showed Capital `[active]` without `[selected]`; Premarket showed the misleading fixed sentence.
- Backend evidence: the Premarket worker failed safely and later AI health recovered, proving the condition was not necessarily disabled configuration.
- Logs: one bounded Ollama timeout; no browser console error.
- Root cause: duplicated special-route logic missed the ARIA assignment, and the empty-pulse state had no reason-specific copy.
- Files involved: `frontend/app.js`, `frontend/index.html`, `tests/test_capital_levels_ui_contract.py`.
- Fix: set `aria-selected=true`, use accurate disabled/unavailable/timeout wording, and advance the asset version to `36.24`.
- Regression test: two static UI contracts plus live browser retest; Capital is selected, corrected copy is visible and console errors/warnings are empty.
- Status: PASS

## AUD-017 — Cold versioned state polling blocks behind background refresh work

- Severity: P2
- Component: aggregate state/cache-only read performance
- User-visible symptom: an otherwise cache-only UI state poll could pause for multiple seconds while News or Economic Calendar committed a refreshed snapshot.
- Reproduction steps: run the full stack with provider workers active, let the 15-second `/api/state` endpoint cache expire, then issue the versioned lean polling request used by the UI.
- Expected behavior: cache-only reads never wait for refresh writers and an unchanged payload returns within the 500ms SLO.
- Actual behavior: cache-only aggregation used blocking News/Calendar readers and a cold HTTP response serialized the full state twice.
- Frontend evidence: the active polling client showed intermittent state refresh stalls during warm-up.
- Backend evidence: access traces recorded 2.4–4.0s cold state builds while background refreshes held agent locks.
- Logs: `/api/state?...unchanged=true&if_version=...` was the recurring slow cache-only route.
- Root cause: read helpers shared blocking writer locks; `api_get_state` encoded bytes for the endpoint cache and then returned the dictionary for a second FastAPI encoding pass.
- Files involved: `agents/news_tab_agent.py`, `agents/economic_calendar_agent.py`, `core/news_pulse.py`, `api/state_service.py`, `api/main.py` and focused tests.
- Fix: add non-blocking cache readers with bounded stale fallback and reuse the single serialized byte payload for HTTP cache misses.
- Regression test: 51 focused state/news/calendar/UI/research/diagnostics tests pass; two isolated post-expiry version polls measured 168.18ms and 189.50ms client-side, each returning a 47-byte unchanged response.
- Status: PASS

## AUD-018 — Recommendation eligibility was presented as execution approval

- Severity: P1
- Component: Pre-Earnings/Cockpit/autonomous candidates
- User-visible symptom: DAO displayed `ELIGIBLE`, but no position opened and the UI exposed only a blocker count.
- Reproduction steps: inspect the 2026-08-20 DAO row, then trace `recommendation_eligible` through `AgentManager._pre_earnings_candidates` and the PAPER execution chain.
- Expected behavior: recommendation eligibility and autonomous order admission are separate, and the first failed gate is visible with actual/required values.
- Actual behavior: the Cockpit called recommendation eligibility `Risk status`; the autonomous prefilter independently required gap-risk 65, absolute bias 20, coverage 50 and candidate score 78 without exposing the failed stage. DAO had recommendation eligibility but gap-risk 48, below 65; the captured screen was also before the US regular session.
- Frontend evidence: `ELIGIBLE` appeared beside `1 blocker(s)` with no drill-down.
- Backend evidence: DAO had no order/position correlation because it stopped before AI critic, Risk, Capital and Paper Broker.
- Logs: no DAO trade event existed in the inspected decision/order history.
- Root cause: two legitimate eligibility layers had no shared assessment/read model and were labeled as if they were one Risk verdict.
- Files involved: `core/autonomous_candidates.py`, `core/agent_manager.py`, `core/autonomous_control.py`, `api/cockpit_service.py`, `frontend/cockpit.js`, `frontend/styles_cockpit.css`.
- Fix: one authoritative structured autonomous assessment, scheduled-cycle evidence persistence, explicit `Recommendation / Paper Auto` labeling, first-blocker metrics and the complete stage trace through position creation.
- Regression test: DAO 48/65 Cockpit regression, autonomous-candidate gates, controlled PAPER acceptance and live browser gate expansion.
- Status: PASS

## AUD-019 — Reported earnings and ambiguous DAO news could remain active

- Severity: P1
- Component: Pre-Earnings/news/entity resolution/session filtering
- User-visible symptom: tickers that had already published results remained in the active list; DAO/Youdao could also inherit unrelated crypto DAO headlines.
- Reproduction steps: load DAO with the Youdao Q2 results release plus Binance/Kelp DAO governance stories, initialize the in-process Pre-Earnings reader, then request the Cockpit.
- Expected behavior: only explicit Youdao/NYSE:DAO evidence may expire DAO, and a result-confirmed event moves to `reported_today` on every read path.
- Actual behavior: bare `DAO` matched decentralized-autonomous-organization news; `_filter_reported_items` ignored explicit reported evidence until the session cutoff; the Cockpit's early in-memory return could bypass final sanitation. The BMO cutoff also used a fixed UTC time across DST.
- Frontend evidence: already-reported DAO could reappear after an automatic poll.
- Backend evidence: two decisive Youdao result stories were present while `flags.already_reported=false`; crypto governance stories were mixed into its evidence.
- Logs: no exception; this was a semantic entity/freshness defect.
- Root cause: an ambiguous acronym was treated like an ordinary ticker and report filtering was split across file/in-memory/session paths.
- Files involved: `agents/news_tab_agent.py`, `agents/pre_earnings_agent.py`, `core/market_hours.py`, `api/cockpit_service.py`.
- Fix: fail closed on bare DAO, accept `$DAO`/`NYSE:DAO`/explicit exchange forms, expire on decisive result evidence, use DST-aware 09:30 ET BMO/18:00 ET AMC boundaries and sanitize both file and initialized in-memory snapshots.
- Regression test: crypto-DAO exclusion, Youdao result-summary detection, explicit-reported filtering, DST session tests and the initialized-agent Cockpit regression.
- Status: PASS

## AUD-020 — Pre-Earnings autonomous thresholds were not owner-accessible

- Severity: P2
- Component: Agents/PAPER_AUTO control plane
- User-visible symptom: the owner could see that gap-risk 65 blocked an entry but could not change it from the application (for example to 40).
- Reproduction steps: open Agents → Autonomous Paper Trading before repair and inspect the available controls.
- Expected behavior: active deterministic thresholds are visible, owner-editable, validated and atomically persisted.
- Actual behavior: only autonomy mode and reduction fraction were exposed; entry thresholds existed only under `agents.execution` configuration.
- Frontend evidence: no gap-risk/bias/coverage/execution-score inputs existed.
- Backend evidence: `/api/execution/control` neither returned nor accepted the four Pre-Earnings threshold values.
- Logs: not applicable; missing control-plane capability.
- Root cause: the Phase 11 control contract was not extended when the Pre-Earnings autonomous prefilter was introduced.
- Files involved: `api/execution_service.py`, `api/main.py`, `frontend/agents.js`, `frontend/styles_agents.css`, `frontend/index.html`.
- Fix: expose current thresholds, validate each as a finite 0–100 number and persist all selected values in one atomic config update; the UI explains that downstream AI/Risk/Capital/session gates still apply.
- Regression test: service/API range and persistence tests, frontend contract, live browser verification of values 65/20/50/78 and editable gap-risk input.
- Status: PASS

## AUD-021 — Watchlist silently preferred stale state prices

- Severity: P1
- Component: Watchlist/live-price API/cache freshness
- User-visible symptom: prices appeared unchanged while the market was open; AAPL remained at 305.86 USD from 2026-08-03 while Yahoo returned about 317 USD.
- Reproduction steps: open Watchlist, compare displayed values/timestamps with `GET /api/live-prices?remote=true&cache_fallback=false`.
- Expected behavior: render cached data immediately, refresh only expired quotes, prefer the newest timestamp and disclose LIVE/CACHED/STALE state.
- Actual behavior: the 30-second UI loop called `remote=false`; the API accepted the first `state.json` hit even when a newer persistent quote existed.
- Frontend evidence: AAPL 305.86, NVDA 217.67 and ETH 2298.81 remained visible despite newer provider quotes.
- Backend evidence: cache-only response returned AAPL timestamp 2026-08-03; strict remote returned a current Yahoo quote immediately.
- Logs: no provider error; this was a cache precedence/polling policy defect.
- Root cause: AUD-012 removed expensive strict-remote polling but did not add a bounded stale-only refresh, and local cache layers were merged by fixed priority rather than observation time.
- Files involved: `api/main.py`, `frontend/app.js`, `frontend/styles_watchlist.css`, `frontend/main.css`, `frontend/index.html`.
- Fix: timestamp-aware quote merge, 45-second freshness metadata, stale-only remote refresh, immediate refresh on Watchlist navigation and visible LIVE/CACHED/STALE badges.
- Regression test: newest-cache precedence, stale-only provider dispatch, frontend refresh/navigation contracts, full E2E and browser/provider live verification.
- Status: PASS

## AUD-022 — Decision Ledger obscured a structurally closed execution path

- Severity: P1
- Component: Decision Ledger / autonomous candidates / Risk Constitution evidence
- User-visible symptom: every visible entry was rejected, but the expanded view resembled a code dump and did not identify the systemic policy that prevented positions.
- Reproduction steps: inspect all persistent ledger rows, aggregate failed Risk checks, compare the active Risk Constitution with Strategy Registry lifecycle state, then run one PAPER autonomous cycle.
- Expected behavior: every rejection is human-readable, exact current blockers are visible, source conflict/liquidity/lifecycle evidence reaches Risk and owner policy remains authoritative.
- Actual behavior: 847/849 pre-repair decisions were rejected; 383 failed minimum-cash plus liquidity, 460 hit evidence-adapter errors. The newest candidates lacked a transported conflict verdict; Scanner USD liquidity stayed nested in `scanner_context`; autonomous strategy lifecycle was absent. The UI recursively rendered internal keys, hashes and reason codes.
- Frontend evidence: expanded entries showed `available`, `reason`, context-key lists and SHA-256 facts as operator prose, with no aggregate answer to "why no position?".
- Backend evidence: the active constitution required APPROVED/CHAMPION while Strategy Lab had 0 approved strategies; the post-fix PAPER ACE-USD/BOME-USD decisions carried verified prices, conflict=false and real dollar volume, then failed only `strategy_evidence=RESEARCH`.
- Logs: before repair, current cycles emitted `risk_constitution_evidence_error: missing signal data-conflict evidence`; after repair, `risk_constitution_veto` contains one structured `strategy_evidence` failure.
- Root cause: candidate evidence existed in adjacent fields but was not adapted into the Risk proposal, and the Ledger had only a generic recursive formatter rather than an operator diagnosis.
- Files involved: `core/autonomous_candidates.py`, `core/agent_manager.py`, `core/decision_ledger/recorder.py`, `core/decision_ledger/store.py`, `api/cockpit_service.py`, `frontend/cockpit.js`, `frontend/styles_cockpit.css`, `frontend/index.html` and focused tests.
- Fix: propagate explicit conflict, USD-liquidity and RESEARCH lifecycle evidence; expose all-history status counts/governance context; render plain-language blockers and open the Risk Constitution editor directly. No strategy was promoted and no owner limit was relaxed.
- Regression test: candidate/integrity/adapter/store/ledger/Cockpit tests, live PAPER cycle, browser DOM/screenshot and full project/E2E gates.
- Status: PASS

## AUD-023 — E2E smoke reused rate-limited state from a previous test session

- Severity: P2
- Component: E2E harness / release gate
- User-visible symptom: the standalone 17-test E2E run passed, but the following release check reported browser-console 429 errors.
- Reproduction steps: leave an isolated `run_server_test.py` process on fixed port 8010, run the complete browser suite, then run `tools/release_check.py`.
- Expected behavior: every E2E/release session has disposable runtime state and fresh API traffic-control buckets.
- Actual behavior: both conftests and release check defaulted to port 8010 and deliberately reused any test-mode server already listening there.
- Frontend evidence: five resource requests returned 429 during tab navigation; the assertion correctly failed on console errors.
- Backend evidence: port 8010 belonged to a long-running `run_server_test.py`; all other 10 release checks passed and no unhandled log hit existed.
- Logs: `e2e_smoke failed (exit=1)` with `429 (Too Many Requests)` in `output/release_check_report.json`.
- Root cause: test-mode identity proved persistence isolation from production, but did not prove isolation between separate E2E sessions.
- Files involved: both E2E `conftest.py` files, `tools/test_runner.py`, `tools/release_check.py`, harness tests.
- Fix: allocate a fresh loopback port unless an explicit reusable base URL/port is supplied; release check always chooses a fresh port unless `E2E_BASE_URL` explicitly opts into reuse. Removed the orphan test server after verification.
- Regression test: 4/4 harness tests, two independent 17/17 E2E runs, final release check 11/11 with zero issues/log hits.
- Status: PASS
