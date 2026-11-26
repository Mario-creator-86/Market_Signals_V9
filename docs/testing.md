# Testing & Quality Gate

## One-command runs

- Smoke gate (< 60s target):
  - `make test-smoke`
- Full quality suite (< 10 min target on CI runner):
  - `make test-all`
- Backend-only suites:
  - `pytest -q tests/unit tests/integration tests/contract tests/golden tests/perf`
- Frontend/E2E suite:
  - `RUN_E2E=1 npm test`
  - implicit skip if `RUN_E2E` is not set.

## Test harness structure

- `tests/unit/`
  - Deterministic unit checks (cache manager, invariants, parsing/normalization rules).
- `tests/integration/`
  - API integration with FastAPI `TestClient`, diagnostics and request budgets.
- `tests/contract/`
  - JSON schema contracts for UI-consumed endpoints.
- `tests/perf/`
  - Guardrails (p95 budgets), low-concurrency load smoke (30s).
- `tests/golden/`
  - Fixture/golden snapshots for premarket and pre-earnings outputs.
- `tests_e2e/`
  - Playwright tab navigation and UI smoke checks.
- `tests/e2e/`
  - Mirror E2E smoke location for quality-gate structure compatibility.

## Cache-first enforcement

- External calls go through shared HTTP wrapper `core/http_utils.py` with tracer/counters.
- Central cache decision logging:
  - `core/cache_manager.py`
- Request context for call reason:
  - `UI_READ`, `UI_WRITE`, `AGENT_REFRESH`
- Diagnostics endpoint:
  - `GET /api/health/diagnostics`

## Adding a new TAB without breaking contracts

1. Add/update endpoint in `api/main.py`.
2. Add schema in `tests/contract/schemas/<tab>.schema.json`.
3. Extend `tests/contract/test_api_contracts.py` mapping.
4. Add one E2E tab check in `tests_e2e/test_quality_tabs.py`.
5. Run `make test-smoke`.

## Adding a new endpoint

1. Define stable response shape (required fields, types, timestamps).
2. Add/extend JSON schema file under `tests/contract/schemas/`.
3. Add integration test for cache-first behavior if endpoint is polled.
4. If endpoint can trigger external data, verify:
   - no calls in warm-cache scenario
   - `x-cache` reflects cache decision
5. Run:
   - `pytest -q tests/contract`
   - `pytest -q tests/integration`

## Diagnostics and debugging

- Health diagnostics:
  - `GET /api/health/diagnostics`
- Request metrics:
  - `GET /api/stats/api-requests`
- Trace sources:
  - external call tracer: `core/http_utils.py` + `core/observability.py`
  - cache decisions: `core/cache_manager.py`
- Release report:
  - `python tools/release_check.py`
  - output: `output/release_check_report.json`
