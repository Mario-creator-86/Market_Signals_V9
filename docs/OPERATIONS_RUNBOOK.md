# Operations Runbook

This is the operational source of truth for Market Signals V7 Premium. It covers the supported runtime, the automation master switch, and the bounded data APIs used by the web application.

## Supported runtime

- Python: `3.12` (tracked in `.python-version`). Python 3.10 is not supported because the pinned `pandas==3.0.1` requires Python 3.11 or newer.
- Server: `python run_server.py`, listening on `MS_PORT` or `8003` by default.
- Production package: `Dockerfile` + `compose.production.yml`; the container is
  non-root, read-only, capability-free, health-checked, and uses persistent
  volumes for every mutable path.
- Engine: subprocess mode (`MS_ENGINE_MODE=subprocess`) is the production default. It isolates signal scanning from API request handling.
- Persistence: SQLite is authoritative, with JSON mirrors for recovery and compatibility. Do not manually edit runtime files while the service is running.

## Local bootstrap

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -r requirements-dev.txt
python run_server.py
```

Linux/macOS:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -r requirements-dev.txt
python run_server.py
```

Open `http://127.0.0.1:8003`. For external deployment, bind behind a TLS reverse proxy rather than exposing the development listener directly.

Before deployment, audit the resolved dependency graph and generate the release
SBOM:

```powershell
python tools/supply_chain_check.py
```

The command must report `PASS` with zero known vulnerabilities. Preserve
`output/supply-chain/` with the release evidence. The update policy is in
`docs/SUPPLY_CHAIN_SECURITY.md`.

## API saturation and abuse

Traffic admission is mandatory in production. The defaults allow 240 reads, 20
mutations, and 5 login attempts per client/minute, a 1 MiB request body, 128
in-flight requests per process, and 16 per client. The complete environment
contract is documented in `docs/API_TRAFFIC_PROTECTION.md`.

Investigate these metrics before increasing a limit:

- `market_signals_api_admission_rejections_total`
- `market_signals_api_inflight_requests`
- `market_signals_api_inflight_limit`
- `market_signals_api_rate_limit_buckets`

Repeated `global_concurrency` rejections indicate capacity exhaustion, not a
client retry problem. Reduce expensive work, add cache coverage, or scale behind
a shared ingress boundary before raising the process limit.

## Production container bootstrap

Copy `.env.sample` to `.env`, set independent strong values for `MS_API_KEY`,
`MS_AUDIT_HMAC_KEY`, and the read-only `MS_METRICS_BEARER_TOKEN`, the real
`MS_PUBLIC_HOST`, and the HTTPS `MS_PUBLIC_ORIGIN`. Then:

```bash
docker compose -f compose.production.yml config
docker compose -f compose.production.yml build --pull
docker compose -f compose.production.yml up -d
docker compose -f compose.production.yml ps
```

The host listener is loopback-only. A TLS reverse proxy is required for public
traffic. Never remove `read_only`, `cap_drop: ALL`, or
`no-new-privileges:true` to work around a persistence error; correct the
corresponding volume/path mapping instead.

Before every image upgrade:

```bash
docker compose -f compose.production.yml exec app \
  python tools/backup_manager.py create --label before-upgrade
docker compose -f compose.production.yml exec app \
  python tools/backup_manager.py list --verify
```

Use immutable `MS_IMAGE_TAG` values so rollback selects the prior image without
rewriting runtime volumes.

## Operator audit evidence

Every mutating API request is appended to `data/operator_audit.jsonl` with a
monotonic sequence and HMAC chain. Bodies, credentials, cookies, query strings,
and raw IP addresses are never stored. Production refuses to start or accept a
new mutation if the chain is corrupt or unavailable.

```bash
python tools/operator_audit.py status
python tools/operator_audit.py verify
```

During a signing-key rotation, put the former key in
`MS_AUDIT_HMAC_PREVIOUS_KEYS` and the new key in `MS_AUDIT_HMAC_KEY`, restart,
then verify the full chain. Audit keys are secrets and are intentionally absent
from backup archives; keep them in the deployment secret manager.

## Metrics, SLOs, and alerts

Prometheus scrapes `GET /api/metrics` with
`Authorization: Bearer <MS_METRICS_BEARER_TOKEN>`. This scoped token cannot
access any other protected endpoint. The exporter uses bounded route templates
and emits no request bodies, credentials, raw addresses, cache keys, or error
text.

```bash
curl -H "Authorization: Bearer ${MS_METRICS_BEARER_TOKEN}" \
  http://127.0.0.1:8003/api/metrics
curl -H "X-API-Key: ${MS_API_KEY}" \
  http://127.0.0.1:8003/api/ops/slo
```

Defaults are 99.9% availability and 500 ms p95 over a five-minute rolling
window, evaluated after 20 requests. Override them only through the documented
`MS_SLO_*` environment variables and keep Prometheus alert thresholds tied to
the exported target metrics.

Use `deploy/monitoring/prometheus.example.yml` and
`deploy/monitoring/market-signals-alerts.yml`. Every alert links back to this
runbook. Treat audit-unavailable, API-down, availability-breach, and stale or
missing recovery-point alerts as critical.

## Automation control plane

`bot.enabled` is the persisted automation master switch. It is the single source of truth for Capital and Agents: the engine may scan while the bot remains stopped, but no automatic execution is allowed when this value is false.

Use the **Agents** tab for normal operation. Its API equivalents are:

```bash
# Read the lightweight live control plane
curl http://127.0.0.1:8003/api/agents/control-plane

# Start or stop the master switch
curl -X POST http://127.0.0.1:8003/api/bot/start
curl -X POST http://127.0.0.1:8003/api/bot/stop

# Enable or disable a supported worker
curl -X POST http://127.0.0.1:8003/api/agents/news_tab/enabled \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```

When `MS_API_KEY` is configured, send it in `X-API-Key` on every mutating request. The API refuses to disable the execution or risk worker while the bot is running; stop the bot first.

After a restart, check these two endpoints before declaring the system operational:

```bash
curl http://127.0.0.1:8003/api/engine/status
curl http://127.0.0.1:8003/api/agents/control-plane
```

Expected safe idle state: `engine.running=true`, `bot.running=false`, and `bot.configured_enabled=false`.

## Capital execution evidence

An **Open** item in Signals is a research candidate, not an order. Before the execution worker can open an automated Capital position, `agents.execution.capital_entry_gate` requires all of the following for the same ticker, direction, and V2 strategy:

1. Fresh, coherent entry / target / stop levels with at least the configured risk-reward ratio.
2. A fresh `faithful_v2_walk_forward` validation with a disclosed cost model, at least 30 observations, profit factor at least `1.2`, positive expectancy, and bounded drawdown.
3. At least 30 matching closed Capital positions with positive net expectancy and profit factor at least `1.2`.
4. Fresh, source-backed News containing AI analysis with no directional conflict, plus a successful AI trade review. Signal-only AI fallback is not eligible.

If any record is missing, stale, research-only, or negative, the candidate is retained for diagnosis but the execution event is recorded as skipped (`data_gate`, `backtest_gate`, `profitability_gate`, or `news_gate`). Do not lower these thresholds to make a current candidate executable. Refresh and inspect the OOS evidence first:

```bash
python tools/run_oos_validation.py BTC-USD --days 90 --train-days 20 --test-days 10 --max-windows 6
curl http://127.0.0.1:8003/api/backtest/oos-validation/latest
```

Price-only OOS reports are evidence for research, not an execution approval by themselves: historical News cannot be replayed, so the live source-backed News and realised Capital gates remain mandatory.

For a parity check of the actual V2 selector (rather than the legacy
QuickBacktester proxy), run:

```bash
python tools/run_faithful_v2_technical_validation.py BTC-USD --days 30 --train-days 7 --test-days 3 --max-windows 3
```

This report is intentionally labelled `research_only`: each OOS window seeds an
in-memory selector cache only from its own preceding training bars and never
writes the live Capital cache.  It becomes eligible for promotion only after a
timestamped, source-backed historical AI-News ledger has enough coverage to
replay News without look-ahead.  New live articles and completed AI micro
summaries are now captured in `data/news_evidence_ledger.jsonl` with distinct
observation and analysis timestamps for that purpose.

## Bounded state and signal history

The UI routes are deliberately bounded and safe to poll:

- `GET /api/state?lean=true` returns open signals plus a compact recent-closed window (default 30 items).
- Non-lean `GET /api/state` still returns a compact maximum of 250 recent closed signals; it never sends the full persistence history.
- `GET /api/dashboard` and `GET /api/engine/status` use compact cached data rather than loading the full signal history.
- `GET /api/signals?limit=100&offset=0` returns a bounded page. `closed_count` is the total count. If `history_complete` is false after the compact window, an administrative/export caller may request `history=true` explicitly and page that result. Do not use `history=true` for UI polling.

This separation prevents the historical signal store from blocking the interactive application path.

## Quality gates

Use the portable Python runner; it is the supported command on Windows and in environments without GNU Make.

```bash
# Backend unit, integration, contract, golden, and performance suites
python tools/test_runner.py backend

# Fast smoke suite
python tools/test_runner.py smoke

# Full suite after Playwright Chromium is installed
python -m playwright install chromium
python tools/test_runner.py all --run-e2e 1

# Release-readiness report
python tools/release_check.py
```

GitHub Actions runs the backend gate on Windows with Python 3.12 and a Playwright browser smoke gate on Ubuntu. Both install `requirements-dev.txt`, so `pytest-timeout` is required rather than silently ignored.

## Incident response

1. Stop automated execution in **Agents** if a risk or data-quality incident is suspected.
2. Preserve `logs/`, `data/market_signals.db`, `data/operator_audit.jsonl`, `signals_state.json`, and the active `config.json` before remediation.
3. Run `python tools/operator_audit.py verify`, then inspect `/api/health/diagnostics`, `/api/ops/slo`, `/api/agents/control-plane`, and `/api/metrics`.
4. Correct the root cause, run the targeted tests plus `python tools/test_runner.py smoke`, then restart the API.
5. Re-enable the bot only after the control plane reports healthy prerequisites and paper-trading validation is complete.

The platform reports market analysis and paper-trading results; it cannot guarantee trading profitability. Treat live execution as a separately approved, risk-managed deployment.
