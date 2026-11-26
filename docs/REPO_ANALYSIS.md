# Repository Analysis

## Scope

This report summarizes the local audit run against the playbook in `.claude/worktrees/nice-liskov/.claude/debug_API_TAB.md`.

The audit covered:
- repository structure and integration points
- API surface and frontend API consumers
- local runtime checks against the app on `http://127.0.0.1:8000`
- static exception-handling heuristics across Python code

The audit did not fully complete:
- live Financial Modeling Prep documentation cross-checks
- Financial Modeling Prep MCP validation

Reason:
- no MCP resources or MCP templates were configured in the current session
- no outbound documentation validation was performed in this pass

## Repository Shape

Observed path counts from a local scan:
- total paths: `1416`
- Python files: `525`
- JavaScript files: `7`
- Markdown files: `50`

Notable directories:
- `api/`
- `agents/`
- `core/`
- `frontend/`
- `tests/`
- `tests_e2e/`
- `data/`
- `output/`
- `docs/`

## API Surface

Static scan of [api/main.py](C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium\api\main.py):
- `@app.get`: `57`
- `@app.post`: `40`
- `@app.put`: `2`
- `@app.delete`: `4`
- total `"/api/"` string occurrences: `118`

Static scan of [frontend/app.js](C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium\frontend\app.js):
- total `"/api/"` string occurrences: `43`

Primary backend integration indicators:
- [core/fmp_client.py](C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium\core\fmp_client.py) contains direct Financial Modeling Prep client logic.
- Frontend is a large single-app style file with multiple polling consumers and cached API layers in [frontend/app.js](C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium\frontend\app.js).

## MCP Availability

Current Codex session checks:
- `list_mcp_resources`: empty
- `list_mcp_resource_templates`: empty

Interpretation:
- there is no active MCP resource available to validate the playbook's MCP-specific phase
- any MCP-dependent requirement in the playbook remains unverified in this run

## Live Runtime Snapshot

Local timed checks against `127.0.0.1:8000`:
- `/api/state?include_news=false&include_hotlist=false&lean=true&unchanged=true`: timed out at about `20s`
- `/api/dashboard?unchanged=true`: timed out at about `20s`
- `/api/watchlist`: `200 OK` at about `416ms`
- `/api/scanner/state`: `200 OK` at about `267ms`
- `/api/news-tab/breaking`: `200 OK` at about `170ms`

Primary conclusion:
- the system is partially responsive
- lightweight endpoints are healthy
- state aggregation endpoints remain the dominant runtime bottleneck

## Existing Relevant Fixes In Tree

This workspace already contains recent fixes from prior debugging passes:
- crypto ticker alias canonicalization (`BTCUSDT`, `BTCUSD`, `BTC-USD` unified)
- state cache separation by request key in frontend
- in-flight state request deduplication in frontend

Those changes improved browser-driven behavior, but they do not fully eliminate the direct backend timeout observed on `/api/state` and `/api/dashboard`.
