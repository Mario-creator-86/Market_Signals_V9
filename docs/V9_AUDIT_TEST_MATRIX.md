# Market Scanner V9 — Audit Test Matrix

All critical rows contain executed evidence. `PASS` on a deterministic safety/accounting row means the real V9 implementation ran against controlled inputs; it does not mean a live broker or unavailable credentialed provider was used.

| ID | Area | Scenario | Expected | Actual | Result | Evidence |
| -- | ---- | -------- | -------- | ------ | ------ | -------- |
| AUD-GOLD-001 | Runtime | App starts | V9 API/UI/workers start in PAPER | Full stack started; final health/diagnostics and metadata valid | PASS | live run + release smoke |
| AUD-GOLD-002 | Build | Production build | deployable static app/container contract | no frontend bundler required; compileall, production security and 8/8 container contracts pass | PASS | release report; actual Docker image is separately BLOCKED_EXTERNAL |
| AUD-GOLD-003 | Contract | UI route/API mapping | no missing/dead critical mapping | all critical tabs/routes loaded; current Risk/OOS contracts assert backend state | PASS | 17/17 E2E; frontend contract tests |
| AUD-GOLD-004 | AI | Provider/model selection | owner selection reaches configured model | Ollama `glm-5.2:cloud` returned `V9_AI_OK`; selection tests pass | PASS | live completion + AI provider config tests |
| AUD-GOLD-005 | AI | Tool call | allow-listed tool validates and executes | representative real registry dispatch succeeded; registry has exactly 28 tools | PASS | `test_ai_tool_registry_golden.py`; tool suites |
| AUD-GOLD-006 | AI | Structured decision | strict schema, trace and safe action | valid structured output accepted; invalid output freezes to no-trade | PASS | AI decision/pipeline golden tests |
| AUD-GOLD-007 | AI | Independent critic | veto changes final decision | critic runs post-pipeline; veto forces no-trade | PASS | `test_phase4c_golden.py` |
| AUD-GOLD-008 | Pre-Earnings | BMO | correct causal session alignment | known BMO fixture aligned to pre-event session | PASS | `test_phase17_golden.py::test_gold_003_earnings_bmo_alignment` |
| AUD-GOLD-009 | Pre-Earnings | AMC | correct causal session alignment | known AMC fixture aligned to post-close/next session | PASS | `test_phase17_golden.py::test_gold_004_earnings_amc_alignment` |
| AUD-GOLD-010 | Pre-Earnings | Score | known formula matches | frozen independent score and score≠probability invariant match | PASS | `test_phase5a_golden.py` |
| AUD-GOLD-011 | News | Normalize/dedup/sentiment | one causal normalized story/evidence set | fingerprint dedup, ticker assignment, impact and FinBERT paths pass | PASS | 127 news/SEC tests; real FinBERT inference |
| AUD-GOLD-012 | PIT safety | No look-ahead | future observations cannot change T | fail-closed PIT guard and HTF series tests pass | PASS | `test_phase7a_golden.py`; `test_strategy_backtester.py` |
| AUD-GOLD-013 | Backtest | Accounting | exact fees, cash, equity and P/L | known trade math and capital conservation match independent expected values | PASS | `test_phase9_golden.py` |
| AUD-GOLD-014 | AutoResearch | Experiment | persisted baseline/challenger lifecycle | deterministic experiment lifecycle/registry pass; real Optuna study persisted 5 completed trials | PASS | research suite; `test_phase14_golden.py` |
| AUD-GOLD-015 | Capital | Allocation | non-zero bounded allocation for valid candidate | known capital/risk inputs produce expected non-zero size; invalid quote rejected | PASS | capital allocation/tool/accounting suites |
| AUD-GOLD-016 | Risk | Veto | structured reason and zero execution | every mandatory constraint vetoes; critic/risk rejection creates no order | PASS | `test_phase9_golden.py`; acceptance journey |
| AUD-GOLD-017 | Paper | Decision → position | one correlated PAPER position | OPEN decision → critic/risk/capital → paper fill/position/ledger passes | PASS | `tests/acceptance/test_pre_earnings_e2e_journey.py` |
| AUD-GOLD-018 | Paper | Duplicate prevention | duplicate decision creates one order | repeated intent is idempotent | PASS | `test_phase11_golden.py::test_gold_010_duplicate_order_prevention` |
| AUD-GOLD-019 | Safety | Kill Switch | no new risk/order | blocks entry while existing position monitoring continues | PASS | phase 9/11 golden and agent-manager tests |
| AUD-GOLD-020 | Paper | Monitoring | fresh scheduled reassessment | monitor refreshes evidence, stamps learning and preserves controls | PASS | execution ticker-learning/monitor suites |
| AUD-GOLD-021 | Paper | Autonomous exit | close fill, exact realized P/L, portfolio update | stop/thesis exit closes at verified price with commission and ledger update | PASS | capital execution/integration scenarios |
| AUD-GOLD-022 | Ledger | Complete trace | decision through execution evidence persisted | correlation, decisions, replay/model-lab and no-trade evaluation persist | PASS | decision-ledger and API suites |
| AUD-GOLD-023 | Persistence | Restart | controlled state survives restart | risk settings, watchlist, pulses, execution and ledger rehydrate | PASS | persistence/restart/contracts + SQLite integrity |
| AUD-GOLD-024 | Security | Prompt injection | no policy/tool/order bypass | malicious News/SEC text remains inert; unknown tool rejected | PASS | `test_phase10_golden.py` |
| AUD-GOLD-025 | Resilience | Provider failure/fallback | bounded failure/correct fallback | primary failure selects configured fallback; guard can force unavailable/no-trade | PASS | `test_ai_fallback_golden.py` |

## Final suite evidence

| ID | Area | Command/scenario | Actual | Result | Evidence |
| --- | --- | --- | --- | --- | --- |
| AUD-FINAL-001 | Backend | `python -m pytest -q tests --ignore=tests/e2e` | 2,983 passed, 1 skipped, 1 warning in 237.12s | PASS | terminal execution 2026-08-20 after Decision Ledger/Risk evidence repair |
| AUD-FINAL-002 | Browser | `python tools/test_runner.py frontend --run-e2e 1` | 17 passed in 23.62s | PASS | fresh isolated Chromium/server run after E2E port repair |
| AUD-FINAL-003 | Release | `python tools/release_check.py` | 11/11 checks pass; zero issues/log hits | PASS | `output/release_check_report.json` |
| AUD-FINAL-004 | Smoke | release smoke selection | 5 passed, 2,308 deselected in 11.63s | PASS | release report |
| AUD-FINAL-005 | Security | production auth/traffic/operator UI | 27 passed | PASS | release report |
| AUD-FINAL-006 | Recovery | DR/API/backup CLI | 12 passed | PASS | release report |
| AUD-FINAL-007 | Containers | entrypoint/deployment/runtime boot contracts | 8 passed | PASS | release report |
| AUD-FINAL-008 | Operator audit | chain/API | 12 passed | PASS | release report |
| AUD-FINAL-009 | Monitoring | OpenMetrics/SLO | 8 passed | PASS | release report |
| AUD-FINAL-010 | Supply chain | dependency audit + SBOM | 122 dependencies, 0 known vulnerabilities | PASS | `output/supply-chain/summary.json` |
| AUD-FINAL-011 | Dependencies | `python -m pip check` | no broken requirements | PASS | terminal execution |
| AUD-FINAL-012 | SQLite | integrity/FK on 4 discovered DBs | all `ok`, zero FK violations | PASS | read-only PRAGMA execution |
| AUD-FINAL-013 | Live provider budget | complete PAPER stack | Pre-Earnings 30/30/0 blocked; News 15/20/0 blocked | PASS | health diagnostics/live logs |
| AUD-FINAL-014 | Performance | warmed live run | endpoint p95 383ms, global p95 458.85ms; live price ~45–80ms | PASS | metrics/diagnostics |
| AUD-FINAL-015 | UI console/network | full route/cockpit tour | 0 unresolved critical console errors, 0 critical failed requests, 0 dead controls | PASS | in-app browser inspection + E2E |
| AUD-FINAL-016 | DAO report sanitation | initialize `/api/pre-earnings`, then read fresh Cockpit on isolated live server | DAO absent, 12 reported-history rows, 188 active; 488 light/193 deep/74 AI evidence | PASS | port 8011 live API + initialized-agent regression |
| AUD-FINAL-017 | Entry explainability/control | expand Cockpit gates and open Agents control | 20 gate drill-downs; actual/minimum metrics and full trace visible; configured 65/20/50/78 controls rendered; 0 browser warnings/errors | PASS | in-app browser inspection |
| AUD-FINAL-018 | PAPER chain | focused Pre-Earnings/autonomy/execution suite | 36 passed in 1.23s; no live-money path | PASS | controlled PAPER fixtures |
| AUD-FINAL-019 | Watchlist freshness | stale state/cache → stale-only Yahoo/Binance refresh → UI | 2,981 backend tests + 17/17 E2E; live AAPL 305.86 → 317.15; 20/20 rows show freshness; zero browser errors | PASS | port 8011 provider/API/browser run |
| AUD-FINAL-020 | Decision Ledger/Risk | full ledger aggregation → candidate evidence → Risk → browser explanation | 859 persistent decisions observed (857 rejected, 2 replay); 7/7 post-repair live rows have verified evidence and fail only owner-required `strategy_evidence`; 21 historical closed trades disclosed | PASS | live PAPER cycles on port 8003 + browser DOM/screenshot |
| AUD-FINAL-021 | Release isolation | complete release check after a prior E2E session | 11/11 checks pass, 0 issues, 0 unhandled log hits; `pip check` clean | PASS | fresh ephemeral E2E port + `output/release_check_report.json` |

## Explicitly unavailable live verification

| Item | Impact | What passed locally | Result |
| --- | --- | --- | --- |
| Docker daemon/CLI | actual Python 3.12 image build/boot not run | Dockerfile/Compose/CI/entrypoint/security/runtime contracts 8/8 | BLOCKED_EXTERNAL |
| Python 3.12 interpreter | local suite not repeated on declared version | complete 3.14.3 suite and production 3.12 contract | BLOCKED_EXTERNAL |
| `EDGAR_IDENTITY` | compliant live SEC request not made | EdgarTools adapter/parser/schema/failure tests | BLOCKED_EXTERNAL |
| commercial AI keys | OpenAI/Anthropic/Gemini not live-called | provider/tool/schema/fallback tests; configured Ollama live completion | BLOCKED_EXTERNAL |

Critical acceptance: **25/25 PASS** for the implemented V9 acceptance contracts. External items remain excluded from live-pass claims exactly as required.
