# Phase 13 - Decision Ledger, Model Lab and AI Replay implementation plan

1. Capture `prompt`, `model`, `prompt_version` in `core/ai/decision_engine.py`
   (`EvaluationResult` additive fields + `TRADE_REVIEW_PROMPT_VERSION` in
   `core/ollama_decision.py`) — back-compat verified against every existing
   constructor call.
2. Build `core/decision_ledger/`: `types.py` (frozen entry/outcome/run
   contracts), `store.py` (SQLite schema v1, idempotent migration, append-only
   entries/outcomes, bounded reads, env/config path resolution), `recorder.py`
   (decision_context → ledger entry, idempotent, fail-open),
   `replay.py` (PIT snapshot + AI replay + deterministic outcome),
   `no_trade_eval.py` (§90 partition), `model_lab.py` (§88 per-role runs).
3. Hook `record_decision` into `agent_manager` after decision-context build on
   accepted AND rejected paths; add `MS_DECISION_LEDGER_PATH` redirect in
   `tests/conftest.py` and git-ignore runtime ledger files.
4. Add `api/ledger_service.py` + `GET /api/ledger/decisions`,
   `GET /api/ledger/decisions/{decision_id}`, `GET /api/ledger/no-trade-evaluation`,
   `GET /api/ledger/model-lab`, `POST /api/ledger/replay` in `api/main.py`.
5. Integrate the cockpit: persistent-ledger-first `_decision_rows`, coverage
   flip off `PARTIAL_PHASE12`, real `decision_replay`/`model_lab` availability
   replacing `deferred` stubs; extend frontend cockpit ledger rendering with
   outcome/model-lab evidence under the existing state contract.
6. Unit tests: store migration/idempotency, recorder paths (accepted,
   rejected, ai-unavailable, redelivery), replay PIT exclusion, §90 metrics,
   §88 identical-input isolation, API contract.
7. Golden tests: `tests/golden/test_phase13_golden.py` — GOLD-002 PIT AI
   replay, §90 partition freeze, §88 input-digest freeze. Update
   `docs/V9_TEST_MATRIX.md` honestly (GOLD-002 PASS; GOLD-005 re-pointed to
   the existing Phase 5A freeze; GOLD-003/004/011 remain explicitly open).
8. Gates: focused Phase 13 selection; curated unit/integration/contract/
   golden/perf; full `tests tests_e2e`; Python compilation; JSON validation;
   `git diff --check`; forbidden-marker scan; dependency diff (no new deps).
9. Update `V9_IMPLEMENTATION.md` (Phase 13 section), `V9_ARCHITECTURE.md`,
   `V9_AI_SYSTEM.md`, `V9_CHANGELOG.md`; source-only checkpoint commit.

Runtime data, owner configuration and `docs/V9_RISK_CONSTITUTION.md` remain
untouched. No new third-party dependencies.
