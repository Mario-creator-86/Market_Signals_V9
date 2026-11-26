# V9 Baseline Results (Phase 0)

**Date:** 2026-08-11
**Source:** migrated copy of V7 @ f912a33, run in V9 directory (`C:\Users\<user>\PyCharmMiscProject\Market Signals V9`).
**Python:** 3.14.3
**pytest:** version in system env (same env V7 used).

All numbers below are ACTUAL values captured from real runs on 2026-08-11. No assumed, rounded, "should", "approximately", or "99%" values.

## Step 1 — App import

```
$ python -c "import api.main; print('APP_IMPORT_OK')" 2>&1 | tail -5
17:49:02 [INFO] API key authentication DISABLED (MS_API_KEY not set). All endpoints are public.
APP_IMPORT_OK
```

Result: **APP_IMPORT_OK**. Only the expected `MS_API_KEY not set` auth warning. FastAPI app builds.

## Step 2 — Regression suite

### Initial run (HEAD 104f480, before migration completion)

```
$ python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf -q 2>&1 | tail -25
5 failed, 1554 passed in 165.76s (0:02:45)
```

Exit code: 1. Five failures, all traced to a migration-completeness gap (three categories of files present in V7 but absent from V9):

1. `tests/unit/test_supply_chain_policy.py::test_github_actions_are_pinned_to_full_commit_shas` — globs `.github/workflows/*.yml`, asserts non-empty. V9 had no `.github/workflows/`.
2. `tests/contract/test_container_deployment_contract.py::test_container_context_excludes_secrets_runtime_state_and_debug_artifacts` — reads `.dockerignore`, asserts it excludes `.env`, `config.json`, `state.json`, `signals_state.json`, `data/**`, `tests`. V9 had no `.dockerignore`.
3. `tests/contract/test_container_deployment_contract.py::test_ci_builds_and_boots_hardened_production_image` — reads `.github/workflows/quality-gate.yml`. V9 had no `.github/workflows/`.
4. `tests/contract/test_monitoring_contract.py::test_prometheus_alert_rules_cover_critical_operational_controls` — reads `deploy/monitoring/market-signals-alerts.yml`. V9 had no `deploy/`.
5. `tests/contract/test_monitoring_contract.py::test_prometheus_scrape_uses_scoped_bearer_secret_file` — reads `deploy/monitoring/prometheus.example.yml`. V9 had no `deploy/`.

Root cause: the Task 2/3 migration script (`scripts/migrate_v7_to_v9.py`) did not include `.github/`, `deploy/`, or `.dockerignore`. The migrated application code (api/, core/, tests/, etc.) was byte-identical and 1554 of those tests passed; only tests depending on the three missing infra/config categories failed.

### Corrected run (after migration completion)

Migration fix applied (see "Migration fix" subsection below): added `.github`, `deploy` to `INCLUDE_DIRS` and `.dockerignore`, `.env.sample`, `.python-version` to `INCLUDE_FILES` in `scripts/migrate_v7_to_v9.py`, then re-ran the migration. Eight files now present in V9:

- `.github/workflows/ci.yml`
- `.github/workflows/quality-gate.yml`
- `.github/dependabot.yml`
- `deploy/monitoring/market-signals-alerts.yml`
- `deploy/monitoring/prometheus.example.yml`
- `.dockerignore`
- `.env.sample`
- `.python-version`

Re-run command and verbatim final summary:

```
$ python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf -q 2>&1 | tail -25
1559 passed in 162.46s (0:02:42)
```

Exit code: 0. **0 failed, 1559 passed.** No tests were skipped, xfailed, deleted, edited, or weakened to reach this result (spec §115, §4).

### Migration fix (script diff)

`scripts/migrate_v7_to_v9.py`:

```diff
-INCLUDE_DIRS = ["api", "core", "agents", "tools", "scripts", "frontend", "tests", "tests_e2e"]
+INCLUDE_DIRS = ["api", "core", "agents", "tools", "scripts", "frontend", "tests", "tests_e2e", ".github", "deploy"]
 INCLUDE_FILES = [
     "requirements.txt", "requirements-dev.txt", "Makefile", "pytest.ini",
     "conftest.py", "run_server.py", "Dockerfile", "compose.production.yml",
-    "config.example.json", "watchlist.json",
+    "config.example.json", "watchlist.json",
+    ".dockerignore", ".env.sample", ".python-version",
 ]
```

Rationale: `.github/workflows/` and `deploy/monitoring/` are required by the supply-chain, container, and monitoring contract tests; `.dockerignore` is required by the container-context contract test; `.env.sample` is the env-var template referenced by `.dockerignore` (via `!.env.sample`) and needed by V9 operations docs; `.python-version` is build config. All five additions were verified secret-free (the only secret-pattern matches in `.github/workflows/*.yml` are synthetic CI test fixtures like `ci-only-operator-key-...`, not real credentials; `.env.sample` contains empty template values only; `.python-version` contains `3.12`).

## Step 3 — Smoke gate

### Initial run (HEAD 104f480)

```
$ python -m pytest -m "smoke and not e2e" tests/unit tests/integration tests/contract tests/perf -q 2>&1 | tail -10
.....                                                                    [100%]
5 passed, 1549 deselected in 12.95s
```

### Corrected run (after migration completion)

```
$ python -m pytest -m "smoke and not e2e" tests/unit tests/integration tests/contract tests/perf -q 2>&1 | tail -10
.....                                                                    [100%]
5 passed, 1549 deselected in 13.07s
```

Result: **5 passed, 1549 deselected** (exit 0). Smoke gate green on both runs (the migration gap did not affect smoke-marked tests).

## Checks table

| Check | Command | Result |
|---|---|---|
| App import | `python -c "import api.main"` | APP_IMPORT_OK (only `MS_API_KEY not set` auth warning) |
| Regression suite (initial) | `pytest tests/unit tests/integration tests/contract tests/golden tests/perf` | 5 failed, 1554 passed in 165.76s (exit 1) — migration gap |
| Regression suite (corrected) | `pytest tests/unit tests/integration tests/contract tests/golden tests/perf` | **1559 passed in 162.46s** (exit 0) |
| Smoke | `pytest -m "smoke and not e2e" tests/unit tests/integration tests/contract tests/perf` | **5 passed, 1549 deselected in 13.07s** (exit 0) |
| Frontend | static SPA served by FastAPI (no build step) | not exercised in Phase 0 baseline; served by FastAPI per app import |
| E2E (tests_e2e) | Playwright, Windows host | accepted-skip (RUN_E2E=0) — NOT recorded as PASS |

## Comparison vs V7 audit §9 baseline

V7 §9 (post-repair run, 2026-06-13):
- Backend full: **557 passed in 27.46s** (exit 0)
- Smoke (non-e2e): 4 passed, 549 deselected (exit 0)
- App import: clean

V9 corrected run (2026-08-11):
- Backend full: **1559 passed in 162.46s** (exit 0)
- Smoke (non-e2e): 5 passed, 1549 deselected (exit 0)
- App import: APP_IMPORT_OK

### Delta analysis

- **Pass count delta:** +1002 passed (1559 vs 557). The migrated V9 codebase contains ~1002 more tests total than the V7 codebase audited on 2026-06-13 (1559 vs 557). The V7 source evolved between the 2026-06-13 audit and the migration snapshot; this is growth, not a regression. Observation only.
- **Smoke delta:** +1 passed (5 vs 4), +1000 deselected (1549 vs 549) — consistent with the test-count growth. Smoke gate remains green.
- **New failures:** **0 new failures vs V7 §9 in the corrected run.** The initial run's 5 failures were caused by a migration-completeness gap (three categories of files not copied); they were resolved by completing the migration of `.github/`, `deploy/`, and `.dockerignore`. They were NOT regressions of any V7-repaired issue and are now gone. Phase 0 gate criterion 7 (0 new failures) is met.

### Verdict

- App import: OK.
- Smoke gate: OK (green).
- Regression suite (corrected): **GREEN — 1559 passed, 0 failed, exit 0.**
- Phase 0 status: **DONE.** Gate criterion 7 met (0 new failures vs V7 §9). The initial-run failures were a migration-completeness gap, now resolved; no tests were weakened, skipped, xfailed, or edited to produce the green result.

## V7 known failures (carried from docs/v7-audit/MASTER_AUDIT_REPORT.md, repaired)

All 8 P0 + ~20 P1 from the V7 master audit were repaired; gate green per audit §9. Not re-audited in Phase 0. The V9 Phase 0 initial-run failures were a migration-completeness gap (missing `.github/`, `deploy/`, `.dockerignore`), distinct from the V7 logic defects that the audit repaired, and are now resolved.

## Reproducibility

Commands run from `C:\Users\<user>\PyCharmMiscProject\Market Signals V9` on Windows 11, Python 3.14.3 (system env, same as V7 used). The re-run migration reset V9's `data/` files to V7's pristine copies (desirable for a clean baseline). Tests then mutated V9's own `data/market_signals.db`, `calendar_events_cache.json`, `data/news_evidence_ledger.jsonl`, `data/premarket.json` — this is V9's own state and is acceptable per the task brief. V7 was not modified and no tests were run against V7.