# MARKET SCANNER V9 — FINAL AUDIT REPORT

## Status

PASS for every implementation and critical acceptance path verifiable in the current environment.

No P0/P1 implementation defect remains. Checks needing unavailable Docker/Python 3.12 or owner credentials are explicitly `BLOCKED_EXTERNAL` and are not claimed as live passes.

## Starting State

- Branch: `master`
- Commit: `8ac7d1897b354157fe9ae12868dfd002ad4f297f`
- Baseline backend: 2,939 passed, 1 failed, 36 skipped, 1 warning
- Baseline release gate: FAIL (10/11; missing E2E server)
- Baseline runtime: provider storm, 25s UI timeout, stale Pre-Earnings rows, V7 metadata and DEGRADED health
- Baseline repository: mutable owner/runtime state tracked and mixed with source

## Final State

- Commit: no commit was created; the audited working tree remains based on `8ac7d18` so the owner can review the complete repair set.
- Backend: 2,977 passed, 1 intentional manual skip, 1 third-party deprecation warning
- Browser E2E: 17/17 passed
- Release check: PASS, 11/11 checks, zero reported issues or unhandled log hits
- Golden acceptance: 25/25 passed
- Supply chain: 122 dependencies, zero known vulnerabilities, CycloneDX SBOM generated
- Persistence: all 4 discovered SQLite databases pass integrity; zero FK violations
- Execution safety: PAPER only; no real-money order enabled or sent

## Executive Summary

The 118-section `audit_to_follow.md` was read completely and followed through phases 0–20. Runtime evidence showed that the features existed but several did not work cleanly together: cache-only reads could start remote work, provider routing generated invalid requests, tests could touch live state, E2E could not boot, stale earnings remained active, periodic UI prices forced remote fanout, and news planning exceeded its own budget.

Those failure chains are repaired. The final application starts and identifies as V9, uses an isolated mutable runtime boundary, honors cache-only semantics end to end, routes crypto requests to compatible venues, respects provider budgets, keeps tests isolated, and completes the decision → critic → risk → capital → PAPER fill → position → monitoring → exit → P/L → ledger/research path under deterministic acceptance coverage.

The final targeted audit also repaired the user-visible Pre-Earnings ambiguity: `ELIGIBLE` now explicitly means recommendation-eligible, not an order or Risk approval. Every candidate exposes the actual autonomous thresholds and the first failed stage; already-reported events are filtered from both persisted and initialized in-memory states; ambiguous bare `DAO` crypto news cannot be treated as NYSE:DAO evidence; and the owner can edit the four autonomous Pre-Earnings thresholds from the Agents control plane.

## Critical Problems Found

- Tracked mutable databases/JSON/watchlist mixed operator data with source.
- Official E2E referenced a missing server and could not execute.
- Cache-only Economic Calendar/Pre-Earnings/scanner/hotlist paths leaked into provider/background work.
- Dashed KuCoin pairs and candle intervals were sent to incompatible Binance/KuCoin formats.
- Periodic watchlist polling forced strict remote requests and timed out.
- Expired Pre-Earnings events appeared as active candidates.
- Test lifespan/fixtures could start jobs or reuse operator persistence.
- News planned beyond its 20-call provider budget and kept a known-dead RSS source.
- Browser tests asserted obsolete Risk/Pre-Earnings/OOS contracts.
- Runtime metadata still called the V9 application V7.
- Pre-Earnings recommendation eligibility was mislabeled as a Risk result and did not explain why no PAPER position opened.
- Already-reported DAO could remain or reappear active, while unrelated crypto DAO headlines contaminated Youdao evidence.
- Autonomous gap/bias/coverage/score thresholds were not accessible to the owner from the UI.

## Root Causes

- Runtime path defaults and Git ignore/index policy had diverged from the container design.
- Remote permission was not propagated through nested agents/builders.
- Provider routing treated crypto symbols and intervals as venue-neutral.
- Periodic UI tasks used refresh semantics instead of read semantics.
- Test-mode guards were placed after production lifespan work and fixture isolation was incomplete.
- Budget checks happened at the HTTP gate instead of during news work planning.
- E2E and product metadata were not maintained with API/application evolution.

## Repairs

- Added `core/runtime_layout.py` with atomic, non-destructive, idempotent migration; preserved owner data and archived conflicting defaults.
- Removed mutable runtime files from the Git index and ignored generated state while retaining immutable fixtures.
- Added isolated `run_server_test.py`; made browser fixtures absolute and fail-fast.
- Made cache-only reads strict: no provider construction, remote warm, hidden scanner refresh or hotlist detail build.
- Added KuCoin-first routing for dashed pairs, native `15min` mapping and Binance invalid-symbol negative caching.
- Changed routine UI live prices to cache-first and enforced explicit `remote=false` on the API.
- Added session-aware Pre-Earnings active filtering and corrected the external-fetch budget to 30.
- Added news cache/budget-aware planning and removed the persistently failing Bitcoin Magazine default feed.
- Stopped Strategy Research before the shared external-call budget can reject planned work; remaining combinations are explicitly deferred.
- Moved the test-mode guard before all schedulers/sweeps and isolated capital/ledger persistence.
- Updated E2E contracts/waits and corrected V9 metadata across API, runtime, UI and deployment files.
- Synchronized Capital's visual/ARIA tab state and made Premarket's missing-pulse message cover bounded AI timeouts honestly.
- Made cache-only News and Economic Calendar reads non-blocking and reused the already serialized `/api/state` payload, eliminating refresh-lock waits and duplicate cold-response encoding.
- Unified Cockpit and autonomous-manager admission logic in one structured evaluator and exposed actual/minimum values plus the complete recommendation → prefilter → session → AI critic → Risk → Capital → PAPER position trace.
- Added strict NYSE:DAO entity matching, evidence-first report expiry, DST-aware BMO/AMC boundaries and final sanitation for both file-backed and initialized in-memory snapshots.
- Added owner-editable, finite 0–100 Pre-Earnings gap-risk, absolute-bias, coverage and execution-score controls with atomic persistence under `agents.execution`.

Full per-file evidence is in `V9_FIX_LOG.md`; reproduction/root-cause/status fields are in `V9_AUDIT_BUG_MATRIX.md`.

## AI

- Provider: configured Ollama path live-verified; commercial providers adapter-verified only
- Model: `glm-5.2:cloud` returned `V9_AI_OK`
- Prompts: role-specific, bounded normalized evidence, prompt version/hash persisted
- Tools: exactly 28 allow-listed tools; representative real dispatch passed; unknown tools rejected
- Decision Engine: strict structured schema; invalid output fails closed
- Critic: separate post-decision stage; veto forces no-trade
- Fallback: bounded provider-specific fallback and no-trade path pass

FinBERT loaded locally and ran real inference. No AI output can directly execute; risk, capital, PAPER mode, idempotency and kill switch remain authoritative.

## Cockpit

The full route and Cockpit tab tour rendered the critical workspaces and corresponding backend state. Final browser evidence contains zero unresolved critical console errors, zero unexplained critical failed requests and zero dead critical controls. The canonical isolated E2E suite is 17/17 PASS.

## Pre-Earnings

BMO/AMC session alignment, frozen scoring, causal freshness, AI schema, coverage, refresh single-flight and budget behavior pass. Expired/result-confirmed rows are no longer active, including after an API-local agent has initialized. In live PAPER verification the refresh consumed exactly 30/30 allowed external fetches with zero blocked calls.

The observed refresh policy is explicit: all 488 calendar reporters receive a light scan; at most 200 are deep-scored (193 in the final targeted snapshot); AI work is bounded to 25 tickers per full 600-second cycle, with targeted 60-second scans and reanalysis when the news fingerprint changes. Therefore, every ticker is refreshed through the light filter, but not every ticker is deep-scored or sent to AI every minute.

DAO's captured `ELIGIBLE` state was recommendation eligibility only. Its captured gap-risk was 48 against the autonomous minimum 65, so it never reached AI critic, Risk, Capital or Paper Broker; the screenshot was also before the US regular session. Result evidence subsequently moved DAO to reported history. MRVL is not stale for the current event: Marvell's official IR calendar schedules fiscal Q2 2027 results for 2026-08-27 after market close.

## News / Sentiment

Fingerprinting, cross-source dedup, source/ticker assignment, evidence freshness, impact scoring and sentiment paths pass. Real local FinBERT inference returned positive confidence `0.9275`. News planning now stops before budget exhaustion; the live final refresh used 15/20 calls with zero blocked. Live SEC remains credential-blocked.

## Signals

Signal normalization, score semantics, lifecycle, dedup/supersession, strategy evidence and position correlation pass. Cache-only state aggregation does not trigger hidden provider work.

## Backtest

No-look-ahead, point-in-time guards, score parity, fee/cash/equity/P&L accounting, OOS and walk-forward contracts pass. E2E uses deterministic request state and no longer leaks a heavy asynchronous run into later assertions.

## AutoResearch

Weakness/hypothesis/experiment/registry/drift/memory and scheduled-cycle tests pass. A real Optuna study named `v9_audit_20260820` persisted five completed trials. No fake experiment was generated when the live cycle correctly found no qualifying weakness, and automatic promotion remains gated.

## Capital / Risk

Known-value allocation and capital-conservation tests pass. Risk constraints, leverage, daily limits, critic veto, constitution persistence and kill switch are structured and explainable. Invalid/unverified prices and excessive-risk candidates produce zero execution.

## Paper Trading

PAPER_AUTO entry, order intent, idempotent order creation, fill, position creation and accounting pass. The audit never enabled a live broker/real-money path.

## Position Management

Fresh monitoring, thesis/stop recheck, verified close price, commission, realized P/L, portfolio update and ledger feedback pass. Kill switch blocks new risk without disabling monitoring of existing PAPER positions.

## Decision Ledger

Structured decisions, no-trade evaluations, correlations, execution evidence, replay and Model Lab data persist in isolated SQLite storage. Restart/rehydration and database integrity checks pass.

## Open-Source Integrations

Actually integrated and executed: FinBERT, Optuna and the local EdgarTools adapter path. Qlib, RD-Agent, skfolio, MLflow, RiskLabAI, vectorbt and River are honestly classified as scaffold/reference/not connected rather than advertised as runtime integrations. The complete classification and license evidence is in `V9_OPEN_SOURCE_AUDIT.md`.

## UI / Console

The UI bundle was advanced to `app.js?v=36.24` so the cache-first price and live accessibility repairs are loaded. Routine live-price latency dropped from roughly 5.5–25 seconds to about 20–80ms. The final clean browser session visited all 13 main modules, the Cockpit System Health workspace, Capital and Premarket; every route rendered live content, Capital exposed `aria-selected=true`, and the browser console contained zero warnings/errors.

## Live Revalidation — 2026-08-20

- Full stack started from `run_server.py` on port 8003 in owner-selected `PAPER_AUTO`; `live_execution_available=false` remained enforced.
- Twelve read-only API probes returned HTTP 200. Representative timings: state 199.3ms, cockpit 116.5ms, Pre-Earnings 17.6ms, news 32.6ms, scanner 8.4ms, Trade Radar 19.8ms, Premarket 41.0ms and four-symbol cache-first prices 20.3ms.
- Five-minute SLO window: 500 requests, availability 1.0, zero 4xx/5xx, global p95 365.54ms against a 500ms target, compliant.
- After closing the agent-owned browser tabs and completing provider/model warm-up, 20 consecutive lean `/api/state` cache reads measured p50 15.21ms, p95 35.13ms and max 35.66ms. A separate user-owned polling session was left untouched. The fresh-start SLO remained compliant at p95 462.35ms while startup work was still inside the five-minute window.
- The remaining cold version-poll stall was repaired live: cache-only state no longer waits on News/Calendar refresh writers and a cache miss reuses one serialized payload. Two isolated post-expiry polls returned the 47-byte unchanged response in 168.18ms and 189.50ms. Final diagnostics were `ok`, with no degraded reasons, global p95 416.43ms and `/api/state` p95 347.12ms against the 500ms target.
- Diagnostics ended `ok` with no degraded reasons. Strategy Research, News, Pre-Earnings, scanner, Trade Radar and Premarket all reported zero provider-budget blocks.
- Pre-Earnings returned 200 active candidates with 63 confirmed dates and zero dates before the current UTC day.
- Paper reconciliation was `HEALTHY`; active Capital positions remained zero. The four ledger orders exposed by the read API all predated this live test (latest 2026-08-19), so the test created no order.
- Yahoo connectivity was live-verified. Ollama had one bounded transient timeout during startup, recovered to healthy provider evidence, and had no errors in the final one-minute window.

### Targeted DAO/gate-control revalidation

- The owner process on port 8003 was left untouched. A separate `TEST_MODE=1` server loaded the patched application on port 8011.
- `/api/pre-earnings` was deliberately called first to initialize the API-local agent; a subsequent fresh Cockpit read still excluded DAO and placed it in reported history. The snapshot exposed 188 active candidates, 12 reported-history records, 488 light-scanned reporters, 193 deep-scored candidates and 74 rows carrying current AI evidence.
- The live Cockpit rendered 20 `Verify gates` controls. Expanding one showed actual/minimum metrics, structured blocker evidence and the stage trace; DAO text was absent. Browser console warnings/errors: 0.
- Agents → Autonomous Paper Trading rendered the active threshold values gap-risk 65, absolute bias 20, coverage 50 and execution score 78. The fields are editable (for example gap-risk 40) and explain that AI critic, Risk, Capital, session and Paper Broker remain downstream gates.
- No threshold or trading value was changed in the owner's configuration during validation, and no order was created.

## Tests

- Backend: 2,977 passed, 1 manual skip, 1 third-party warning in 236.60s
- Frontend: static contracts included in backend; full live browser route audit completed
- Integration: included in the 2,977-test suite; focused capital/risk/execution combined run 334 passed
- E2E: 17 passed in 24.07s
- Targeted PAPER decision/autonomy/execution: 36 passed in 1.23s
- Targeted threshold/Cockpit/frontend contracts: 54 passed in 1.23s; final focused DAO/session set 69 passed in 1.39s
- Live repair regression: 51 focused tests passed; `frontend/app.js` also passed `node --check`.
- Golden: AUD-GOLD-001…025, 25/25 PASS
- Build: release check PASS; syntax compilation and 8/8 production container contracts PASS; actual Docker image build `BLOCKED_EXTERNAL`

The warning is a Transformers/WordPiece deprecation inside an installed dependency, not a V9 failure. The skipped test is explicitly a manual news-availability check and is covered by deterministic/runtime news evidence elsewhere.

## Critical Acceptance

25 / 25 PASS.

## Remaining Problems

No unresolved critical implementation problem was found after repair. Remaining items are environment/live-verification limits:

- Python 3.12 is not installed locally; all local tests used 3.14.3.
- Docker CLI/daemon is unavailable, so an actual production image was not built/booted here.
- SEC and non-Ollama AI live calls require owner credentials/identity.

## Blocked External Verification

- `BLOCKED_EXTERNAL` — Docker/Python 3.12 image build: unavailable toolchain; container source/contracts pass locally.
- `BLOCKED_EXTERNAL` — SEC/Edgar live request: missing `EDGAR_IDENTITY`; adapter/parser/failure paths pass.
- `BLOCKED_EXTERNAL` — OpenAI/Anthropic/Gemini live calls: keys absent; schemas/tool adapters/fallback pass.
- `BLOCKED_EXTERNAL` — optional MLflow/Qlib/skfolio integrations are not installed and are not claimed.

## Recommended Next Actions

1. In CI or a host with Docker, run the production Python 3.12 image build/boot and archive its health/readiness evidence.
2. Configure `EDGAR_IDENTITY` and desired commercial AI keys through secrets management, then run only their documented live probes.
3. Review the working-tree diff and commit the repair/audit set as one controlled change.
4. Keep `LIVE_AUTO` disabled; continue PAPER soak monitoring before any separate, explicitly authorized live-trading design review.

Final conclusion: the V9 features work together across the complete deterministic PAPER chain. External credential/container checks remain honestly unverified and do not weaken or bypass the safety gates.
