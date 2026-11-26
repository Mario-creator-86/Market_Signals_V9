# V9 — Phase 0 Design: V7→V9 Migration, Baseline & V9 Control Documents

**Date:** 2026-08-11
**Spec basis:** `prompt_to_follow.md` (Market Scanner V7 → V9 Master Implementation Specification)
**Sections invoked:** §2 (direct migration V7→V9), §5 (Phase 0 must come first, no rewriting), §6 (establish V7 baseline), §7 (preserve V7 data & functionality), §8 (create V9 control documents immediately), §9 (V9_IMPLEMENTATION.md structure), §10 (open-source manifest), §116 (code checkpoints), §117 (forbidden actions).
**Phase scope:** Phase 0 only.
**Approach chosen:** B — spec-compliant, viable.

**Owner constraint (overrides §2's "in-place" wording):** All V9 work happens in the `Market Signals V9` directory. The `Market Signals V7 - Premium` repository is a **read-only source of record** and must NOT be modified. V9 is therefore a **migration (copy) of the V7 codebase into the V9 directory**, then evolved there. This satisfies §2/§117 ("direct, tested evolution of the existing V7 codebase"; "do NOT create a second unrelated V9") because V9 is the V7 codebase moved forward, not a new unrelated app.

---

## 1. Objective

Phase 0 must NOT rewrite code (§5). It must:

1. Initialize V9 as a git repository in `C:\Users\<user>\PyCharmMiscProject\Market Signals V9`.
2. Migrate the V7 codebase into V9 as a clean copy (code + docs + tests + preserved data; excluding audit-flagged junk and secrets), with V7 left untouched.
3. Establish the verified baseline of the migrated V9 codebase (§6) — recorded, not assumed.
4. Create all required V9 control documents (§8) as living documents with their mandated structure.
5. Seed `V9_IMPLEMENTATION.md` as the operational source of truth with all 20 phases (§9) and Phase 0 = `PASS`.
6. Seed `V9_OPEN_SOURCE_MANIFEST.md` with the 12 mandated repositories (§10–§23).
7. Declare what V7 data and functionality is preserved (§7).
8. Reach the Phase 0 exit gate and create a local checkpoint commit (§116), no remote push.

## 2. Where the work happens / what is touched

- **V9 directory** (`C:\Users\<user>\PyCharmMiscProject\Market Signals V9`): `git init`, migrated code, `docs/V9_*.md`, this design doc. All writes happen here.
- **V7 directory** (`Market Signals V7 - Premium`): **read-only**. We only read from it to perform the migration and to record the V7 source baseline. No file in V7 is created, modified, or deleted. V7's git state (`master` @ `f912a33`, 109 dirty files) is left exactly as found.

## 3. V7→V9 migration (clean copy)

### 3.1 Include (the codebase to evolve, per §7 preserve functionality)
- Python: `api/`, `core/`, `agents/`, `tools/`, `scripts/` — excluding `__pycache__/`.
- Frontend: `frontend/` (HTML, JS, CSS, assets).
- Tests: `tests/`, `tests_e2e/`, `conftest.py`, `pytest.ini`.
- Docs: `docs/` (V7 system docs are useful reference), plus this V9 design doc.
- Config/build: `requirements.txt`, `requirements-dev.txt`, `Makefile`, `run_server.py`, `Dockerfile`, `compose.production.yml`, `config.example.json`, `.dockerignore`, `.env.sample`, `.python-version`.
- CI/infra config: `.github/` (workflows: `ci.yml`, `quality-gate.yml`; `dependabot.yml`) and `deploy/` (`deploy/monitoring/market-signals-alerts.yml`, `deploy/monitoring/prometheus.example.yml`). Required by the supply-chain (`test_github_actions_are_pinned_to_full_commit_shas`), container (`test_container_context_excludes_secrets_runtime_state_and_debug_artifacts`, `test_ci_builds_and_boots_hardened_production_image`), and monitoring (`test_prometheus_alert_rules_cover_critical_operational_controls`, `test_prometheus_scrape_uses_scoped_bearer_secret_file`) contract tests. Verified secret-free: the only secret-pattern matches in `.github/workflows/*.yml` are synthetic CI test fixtures (`ci-only-operator-key-...`, `ci-only-metrics-token-...`, `ci-only-audit-hmac-key-...`), not real credentials; `.env.sample` contains empty template values only; `.python-version` is `3.12`.
- Preserved data (§7): `watchlist.json`, `capital.json`, `signals_state.json`, `state.json`, `data/market_signals.db` (SQLite), `data/pre_earnings*.json`, `data/news_backup.json`, `data/trade_decisions.json`, `data/trade_journal.json`, `data/strategy_performance_cache.json`, `data/scanner_state.json`, `data/trade_radar_state.json`, `data/us_macro_calendar_2026.json`, `data/yfinance_cache/`.

### 3.2 Exclude (audit-flagged junk / secrets / V7 history)
- `logs/` (504 MB, unrotated — audit F4).
- `*.tmp`, `data/*.tmp`, `analysis_store_*.tmp` (425 MB orphaned temp — audit F3/E3).
- All `__pycache__/`.
- V7 `.git/` (V9 starts its own git history).
- `config.json` (contains committed Marketaux key — audit R5/F1; V9 uses `config.example.json` as template, owner supplies own secrets).
- Root debug artifacts: `*.png` (`pe_*.png`, `traderadar_full.png`), `*.bak`, `nul`, `server_startup.log`, `*_srv.log`, `output.log`, `news_backup.json.bak`, `calendar_events_cache.json*`, `hotlist_cache.json`, `signals_state.bak.json`, `signals_state.summary.json`, `pe_*.json`, `watchlist.json.*.bak`.
- Debug/one-off scripts flagged by audit (tests/: 28 FMP debug files, ~50 debug scripts; tests/manual). These are not functionality (§7) and carry hardcoded FMP keys (audit G1).
- `MARKET_SCANNER_V7_PREMIUM_COMPLETED.flag`, `AUDIT_REPORT.md`, `MASTER_AUDIT_REPORT.md` are V7-specific; carried into V9 `docs/` as **read-only reference** (renamed under `docs/v7-audit/`) to preserve audit history without implying V9 completion.

The exact file-level exclude list is finalized in the implementation plan; the categories above are the design contract.

## 4. Baseline capture (§6)

### 4.1 V7 source baseline (read-only reference)
Recorded in `V9_IMPLEMENTATION.md` Phase 0 record: V7 branch `master` @ `f912a33`, 109 dirty files (left untouched), Python 3.14.3, Node v24.14.0, SQLite 44 MB + JSON dual-write, FastAPI 0.135.1, vanilla JS SPA, Ollama optional, providers (yfinance/Binance/FinnHub/RSS/FMP-stub), deps pinned, existing audit `MASTER_AUDIT_REPORT.md` (8 P0 + ~20 P1 repaired, 557 passing). V7 known failures carried from that audit (not re-audited).

### 4.2 V9 migrated baseline — verified by execution (§6: run backend, frontend, tests, build, journeys)
On the migrated copy in V9, record results of actual runs (no "should work"):
- `python -c "import api.main"` — FastAPI app builds (expected: only `MS_API_KEY not set` auth warning).
- `pytest tests/unit tests/integration tests/contract tests/golden tests/perf` — regression gate (expected ~557 passed, exit 0).
- `make test-smoke` — fast quality gate.
- Frontend: static SPA served by FastAPI (no build step); verify `frontend/index.html` + assets resolve.
- E2E (`tests_e2e`): skipped on Windows host under `RUN_E2E=0`; recorded as accepted-skip with reason, NOT as PASS.

## 5. V9 control documents (§8) — living skeletons, written in V9 `docs/`

All 10 created with mandated structure, content honestly minimal. Status vocabulary from §9 (`NOT STARTED | IN PROGRESS | BLOCKED | FAILED | PASS`); explicit "to be populated in Phase N" markers elsewhere. No forbidden success language (§4).

1. `V9_IMPLEMENTATION.md` — 20-phase tracker (§9); Phase 0 full record with `Status=PASS`; others `NOT STARTED`.
2. `V9_ARCHITECTURE.md` — §26 diagram adapted to real V7 modules + UI→client→API→service→provider map (§101).
3. `V9_TEST_MATRIX.md` — 19 GOLD-* (§107) + 28 acceptance areas (§121); all `NOT STARTED`; honest note that V7 golden suite is near-empty (audit §9).
4. `V9_CHANGELOG.md` — first entry: "Phase 0 — V7→V9 migration + baseline + control documents created."
5. `V9_OPEN_SOURCE_MANIFEST.md` — 12 repos (§11–§23) with `decision=PENDING`; evaluation is Phase 2 (§118).
6. `V9_AI_SYSTEM.md` — §34–§44 structure (AIProvider caps, providers, registry, router, tools, sandboxing, decision schema, validation, critic).
7. `V9_DATA_POLICY.md` — §27–§33 (canonical entities, calendar, point-in-time, survivorship, corporate actions, no-synthetic, data quality).
8. `V9_RISK_CONSTITUTION.md` — §47 fields with current V7 values where they exist + `OWNER_MUST_SET` placeholders; marked `PROTECTED` (§48/§82); §83 goal-hacking + §82 protected-files lists verbatim.
9. `V9_RESEARCH_FACTORY.md` — §3 loop, §75 agents, §76 hypothesis output, §77 experiment statuses, §78 memory, §79 drift.
10. `V9_OPERATIONS.md` — §45 autonomy modes (default OFF), §74 kill switch, §98 health, §103 job states, §104–§106 migrations/backups/secrets.

## 6. Preserve V7 (§7)

Phase 0 makes **zero code changes** to the migrated logic (the migration is a verbatim copy, not a rewrite). Preserved inventory recorded in `V9_IMPLEMENTATION.md` Phase 0: watchlists, paper trades, capital, settings, historical results, signal logic, strategy_v2, providers, Ollama, UI components. Preference (§7): EXTEND → MIGRATE → TEST → REFACTOR.

## 7. Phase 0 exit gate (PASS criteria)

Phase 0 = `PASS` iff all true:
1. V9 is a git repo; V7 is untouched (diff of V7 working tree identical to pre-Phase-0: 109 dirty files, HEAD `f912a33`).
2. Migration complete: V9 contains the codebase per §3.1/§3.2 (no junk, no secrets, no V7 `.git`).
3. Baseline recorded with results of actual executed runs (§4.2), no assumed values.
4. All 10 `docs/V9_*.md` exist with mandated structure.
5. `V9_IMPLEMENTATION.md` lists all 20 phases; Phase 0 = `PASS`; others `NOT STARTED`.
6. `V9_OPEN_SOURCE_MANIFEST.md` lists all 12 mandated repos with `decision=PENDING`.
7. Regression gate green (exit 0), no new failures vs V7 audit §9 baseline.
8. Local checkpoint commit(s) created in V9 (§116); no remote push.

If any criterion fails: `V9 STATUS = NOT DONE` (§4/§122) with exact failing items.

## 8. Out of scope (explicit)

- Any code/logic change to V7 or to the migrated V9 logic (deferred to Phase 1+).
- License/feature evaluation of the 12 repos (Phase 2, §118).
- Canonical data layer, point-in-time store, AIProvider, backtester consolidation, research factory (Phases 1, 3, 7, 14…).
- Filling content that later phases produce — control docs remain living skeletons.

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Accidentally modify V7 | Migration is read-only on V7; verify V7 git state unchanged as gate criterion 1. |
| Copy secrets/junk into V9 | §3.2 exclude list; `config.json` excluded (use `config.example.json`); grep V9 for known key patterns post-migration. |
| Migration brings 44 MB SQLite + caches | Acceptable (preserved data, §7); caches under `data/yfinance_cache/` may be excluded if regenerable — decided in plan. |
| Test run regresses vs §9 baseline | Record exact deltas; if new failure, Phase 0 = NOT DONE, root-cause before proceeding. |
| Skeleton docs read as "done" | Every section carries §9 status vocab or explicit "to be populated in Phase N"; no forbidden success language. |

## 10. Handoff

On PASS, invoke the `superpowers:writing-plans` skill to produce the implementation plan for executing Phase 0 (git init V9; migrate; verify; create docs; record results; commit). Per the brainstorming process, the terminal state of brainstorming is invoking writing-plans — no other implementation skill.