# V9_TEST_MATRIX

Living document. Rows are critical acceptance items; status uses §9 vocabulary. All `NOT STARTED` at Phase 0.

## Golden tests (spec §107)

| ID | Name | Status | Owner | Notes |
|---|---|---|---|---|
| GOLD-001 | No look-ahead | PASS | Phase 7 | `core.backtest` pins next-bar-open/no-look-ahead, rejects same-bar and pre-signal fills, and blocks legacy V1 trade output; `tests/golden/test_phase7a_golden.py` |
| GOLD-002 | Point-in-time AI replay | PASS | Phase 13 | Replays at T provably exclude post-T records (poisoned-future fixture), are stored as `REPLAY` entries with no order linkage, and outcomes are deterministic arithmetic; `tests/golden/test_phase13_golden.py`. |
| GOLD-003 | Earnings BMO alignment | PASS | Phase 17 | `earnings_session_reported` pinned: BMO reportable only from 14:30 UTC, ambiguous hints never reportable; `tests/golden/test_phase17_golden.py`. |
| GOLD-004 | Earnings AMC alignment | PASS | Phase 17 | AMC reportable only from 22:00 UTC (after-hours bulk print); unknown session stays unreported; same file. |
| GOLD-005 | Pre-Earnings score | PASS | Phase 5A | The §50 score formula, its not-a-probability invariant and confidence-band boundaries are frozen in `tests/golden/test_phase5a_golden.py` (its internal label GOLD-050 refers to spec §50). |
| GOLD-006 | Capital conservation | PASS | Phase 9 | LONG/SHORT win/loss identities pinned in `tests/golden/test_phase9_golden.py`. |
| GOLD-007 | Exact trade accounting | PASS | Phase 9 | Pure accounting matches isolated V7 `CapitalManager` open/close cash, margin, fees, and P/L. |
| GOLD-008 | Risk veto | PASS | Phase 9 | All mandatory named constraints are independently tested; the golden aggregate veto pins the full decision. |
| GOLD-009 | Kill Switch | PASS | Phase 11 | Phase 9 policy plus the Phase 11 owner control immediately block new Paper risk while monitoring and safe REDUCE/CLOSE remain available; `tests/golden/test_phase11_golden.py`. |
| GOLD-010 | Duplicate order prevention | PASS | Phase 11 | Deterministic `decision_id`, `order_intent_id`, and unique `idempotency_key` make repeated scheduler delivery return the original order without a second broker call. |
| GOLD-011 | Stale data rejection | PASS | Phase 17 | The engine Data Freshness Guard pinned: per-interval thresholds reject stale bars before signal generation; empty data rejected outright; same file. |
| GOLD-012 | Prompt injection | PASS | Phase 10 | Phase 4 allow-list/downstream validation plus Phase 10 `UNTRUSTED_EXTERNAL_DATA` quarantine. Instruction-shaped News/SEC strings are replaced by SHA-256 markers before AI composition; `tests/golden/test_phase10_golden.py`. |
| GOLD-013 | AI provider fallback | PASS | Phase 3C | `generate_with_fallback` chain → `UNAVAILABLE` sentinel; `tests/golden/test_ai_fallback_golden.py` |
| GOLD-014 | All AI unavailable → no new autonomous trade | PASS | Phase 3C | `no_new_autonomous_trade_guard(UNAVAILABLE)` blocks; `tests/golden/test_ai_fallback_golden.py` |
| GOLD-015 | Bad challenger rejected | PASS | Phase 8 | Multi-factor OOS/risk/stability/cost/regime/recent/shadow gates reject weak evidence without mutating the incumbent; `tests/golden/test_phase8_golden.py` |
| GOLD-016 | Protected policy modification rejected | PASS | Phase 9 | `assert_not_weakening` rejects autonomous Risk Constitution weakening; owner authorization remains explicit. |
| GOLD-017 | Rollback | PASS | Phase 16 | Breach of recorded conditions rolls the promotion back (restored_version evidence, experiment ROLLED_BACK, risk limits untouched, idempotent); `tests/golden/test_phase16_golden.py`. |
| GOLD-018 | DB migration | PASS | Phase 11 | The execution ledger upgrades an empty/legacy SQLite file idempotently to schema v1 without deleting owner data. |
| GOLD-019 | Broker reconciliation | PASS | Phase 11 | Startup/manual reconciliation repairs provable in-flight Paper orders and emits `EXECUTION_STATE_CONFLICT` for unresolved material disagreement, blocking new autonomous risk. |

## Acceptance areas (spec §121)

| Area | Status | Owner | Notes |
|---|---|---|---|
| SYSTEM | PASS | Phase 0/19 | Full app reverified live on port 8003 in PAPER_AUTO: diagnostics `ok`, healthy reconciliation, 500-request availability 1.0 and global p95 365.54ms (target 500ms). |
| DATA | PASS | Phase 1 | Canonical model + PIT store + corporate actions/survivorship (1A/1B/1C all PASS). |
| AI PROVIDERS | PASS | Phase 3/19 | Multi-provider platform (3A/3B/3C) + today's live verification: ollama provider verified HEALTHY (gemma4:26b) in System Health. |
| AI DECISION ENGINE | PASS | Phase 4 | §39 tools + §40 guard (4A), §41/§42/§43 schema+pipeline (4B), §44 critic + §45/§46/§48 autonomy + agent_manager rewire (4C) — all PASS. |
| PRE-EARNINGS | PASS | Phase 5/19 | §49/§50/§51/§93 delivered (5A/5B/5C); live today: 200 analyzed candidates rendered with deterministic/AI evidence visibly separate. |
| SIGNALS | PASS | Phase 6 | Signal catalog, horizons, ML engine seam, calibration contract, meta-labeling, market regime (6A/6B/6C). |
| ML | IN PROGRESS | Phase 6 | Engine/calibration/OosGate tested, but no persisted trained model exists yet (registry reports 0 records honestly); requires a real training run on live data. |
| BACKTEST | PASS | Phase 7 | Stable engine Protocol/adapters; complete metrics; TRAIN/VALIDATION/OOS/FINAL_HOLDOUT; walk-forward; overfitting battery; no-forced-strategy. Curated gate 2026 passed; full gate 2553 passed, 36 pre-existing skips. |
| STRATEGIES | PASS | Phase 8 | Exact strategy registry, atomic audited lifecycle, champion/challenger promotion, V2 RESEARCH bridge, optional approved-only selector gate, and Optuna safety. Curated gate 2102 passed; full gate 2629 passed, 36 unchanged skips. |
| CAPITAL | PASS | Phase 9 | Five deterministic sizing methods, exact conservation accounting, V7 parity, and persisted position evidence are green. |
| RISK | PASS | Phase 9/12 | Complete fail-closed engine, RiskAgent composition, owner draft/confirm UI and three external validated Paper templates plus Custom are green. Runtime currently reports an owner-confirmed active Custom constitution. |
| NEWS | PASS | Phase 10 | Existing ingestion/UI retained; deterministic normalization/dedup/entity/catalyst contract; real pinned FinBERT positive/negative/neutral inference; labelled fallback; PIT evidence; external-text quarantine. Curated 2227 passed; full 2759 passed, 36 unchanged skips. |
| SEC | IN PROGRESS | Phase 10 | edgartools adapter, PIT/cache, strict SEC-host metadata, API and AI tool pass under fakes/contracts. Live SEC acceptance awaits owner-supplied `EDGAR_IDENTITY`; no identity is invented. |
| PAPER EXECUTION | PASS | Phase 11/12 | Persistent `PaperBroker`, exact order states, idempotency, reconciliation, API and owner UI are green. Live acceptance reports owner-selected PAPER_AUTO, active Custom constitution, healthy reconciliation and owner-selected REDUCE; no value auto-applies. Latest curated: 2269 passed; full: 2808 passed, 36 explicit skips. |
| POSITION MANAGEMENT | PASS | Phase 11 | Section 67 evidence plus deterministic HOLD/REDUCE/CLOSE reassessment and real proportional REDUCE/CLOSE execution are implemented and audited. |
| DECISION LEDGER | IN PROGRESS | Phase 13 | Persistent append-only SQLite ledger records every decision (accepted, rejected, NO TRADE) with the §97 eleven questions, prompt/model/version capture, and idempotent redelivery; Cockpit reads ledger-first. Outcome evaluation requires post-decision bars, so `afterward` stays honestly INCONCLUSIVE until data exists. |
| MODEL LAB | IN PROGRESS | Phase 13 | Per-role comparison runs over one frozen point-in-time snapshot (`inputs_sha256` frozen, identical prompts proven); no global score by construction. Runs persist; a live multi-model acceptance with real configured providers remains before PASS. |
| RESEARCH FACTORY | IN PROGRESS | Phase 14 | §76 validated hypotheses, §77 nine-status governed experiments (golden-frozen), §111 deterministic pipeline to SHADOW/REJECT, §79 drift→REVALIDATION, §78 memory index, §64 persisted bounded Optuna (pinned 4.9.0). PASS awaits a live cycle on real journal data. |
| AUTONOMOUS DEVELOPMENT | IN PROGRESS | Phase 15 | §80 isolated worktrees, §82/§83/§84 fail-closed diff guard, §81 five-stage independent review ledger golden-frozen; journey ends at SHADOW_READY. 2026-08-23: the §112 middle now exists — LLM coding_agent generates diffs with a bounded self-repair loop (auto_repair.py), independent LLM reviews + deterministic TESTS stage; live-verified with glm-5.2:cloud (one honest REJECTED on a real IndentationError caught by TESTS, then a full OPEN submission with CODE_REVIEW/SECURITY_REVIEW/TESTS PASSED). Remaining for PASS: QUANT_VALIDATION/E2E actors (Phase 16/17) and promotion. |
| SHADOW | IN PROGRESS | Phase 16 | §85 identical-inputs shadow comparison implemented; PASS awaits live shadow observations on real data. |
| ROLLBACK | IN PROGRESS | Phase 16 | GOLD-017 automatic rollback implemented and golden-passed; PASS awaits a live promotion+rollback observation. |
| UI | PASS | Phase 12/19 | Re-verified live 2026-08-20: all 13 main modules and Cockpit System Health rendered real state; Capital ARIA selection and Premarket timeout copy pass; clean-session console has zero warnings/errors. |
| SECURITY | PASS | Phase 18 | Executed 2026-08-16: zero secret patterns across 878 committed files (§106); config.json/.env git-ignored with empty example keys; §40 tool allow-list and GOLD-012 injection quarantine golden-pinned. |
| PERFORMANCE | PASS | Phase 18 | Perf budgets remain green; live five-minute SLO was compliant at p95 365.54ms with zero server/client errors across 500 requests. Post-warm 20-sample lean-state p95 was 35.13ms (max 35.66ms). After repairing refresh-lock waits and duplicate cold serialization, isolated post-expiry version polls were 168.18ms/189.50ms with a 47-byte unchanged body; final live diagnostics reported global p95 416.43ms and state p95 347.12ms against the 500ms target. |
| DATABASE MIGRATION | PASS | Phase 11 | Additive, idempotent execution-ledger schema v1 migration is covered by unit and GOLD-018 tests. The broader final V7-to-V9 migration drill remains a production-build acceptance concern. |
| PRODUCTION BUILD | PASS | Phase 19 | Static frontend + FastAPI verified live 2026-08-20 on port 8003; 12/12 critical read probes returned HTTP 200 (Docker image boot remains separately blocked by the missing local Docker toolchain). |
| E2E | PASS | Phase 19 | Canonical `tests_e2e` remains 17/17; additional live browser acceptance toured every main module and System Health with zero console warnings/errors. |
| GOLDEN TESTS | PASS | Phase 17 | All 19 §107 rows PASS (GOLD-001..019); golden suite `95 passed`. |

## Starting point (honest)
V7 golden suite is near-empty (2 tests, audit §9). Phase 0 introduces no new tests (no code changes). Golden tests are built in their respective phases (GOLD-001 in Phase 7; GOLD-002 in Phase 13; GOLD-005 in Phase 5A; GOLD-006/007 in Phase 9; GOLD-008/009/016 in Phase 9; GOLD-010/018 in Phase 11; GOLD-012 in Phase 4; GOLD-013/014 in Phase 3; GOLD-015 in Phase 8; GOLD-017/019 in Phase 16). Final acceptance requires all critical rows PASS (spec §122).
