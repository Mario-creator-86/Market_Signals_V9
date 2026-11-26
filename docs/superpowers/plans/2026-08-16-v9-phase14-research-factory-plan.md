# Phase 14 - Research Factory implementation plan

1. `core/research_factory/hypothesis.py` — §76 frozen records + validation + atomic store.
2. `core/research_factory/experiments.py` — §77 9-status lifecycle + legal transitions + atomic store + event log.
3. `core/research_factory/weakness.py` — deterministic weakness observations from performance cache + trade journal.
4. `core/research_factory/drift.py` — §79 rolling-window drift verdicts + REVALIDATION events (never risk changes).
5. `core/research_factory/memory.py` — §78 relationship index over existing structured stores (incl. Phase 13 ledger).
6. `core/research_factory/tracking.py` — ResearchTracking seam (JSON default, optional lazy MLflow).
7. `core/research_factory/optuna_runner.py` — §64 persisted bounded studies behind `validate_optuna_study`; honest unavailability.
8. `core/research_factory/pipeline.py` — §111 cycle: weakness → hypothesis → experiment → evaluation (OosGateRunner seam) → REJECTED/VALIDATED → SHADOW handoff. Add `optuna` to requirements (Phase 2 pin).
9. API routes + `api/research_factory_service.py`; cockpit research section flips off the PHASE_14 deferred marker; runtime JSON artifacts git-ignored; `conftest.py` redirects.
10. Tests: unit per module; golden §77 status machine + §111 deterministic cycle (weakness→…→SHADOW, no order, no promotion); curated + full gates; control docs; checkpoint commit.
