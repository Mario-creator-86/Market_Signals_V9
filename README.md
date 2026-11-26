# Market Signals V9

Market Signals V9 is a self-hosted market research dashboard for US equities and crypto. A FastAPI backend runs a set of bounded agents (scanner, news, pre-earnings, premarket, trade radar, risk, backtest, paper execution) that pull data from public providers, score it, and persist the results; a vanilla JavaScript single-page frontend shows the output as a tabbed cockpit. Everything runs locally, state lives in SQLite plus JSON files, and all external API keys are optional.

The project evolved from an earlier V7 codebase and is documented phase by phase under `docs/`. Autonomous trading is paper-only and off by default; there is no live broker execution path shipped.

## Features

The UI is organised as tabs, each backed by its own API surface and agent:

- **AI cockpit** - aggregated view of signals, decisions and agent output, with a provider-agnostic AI layer (local Ollama or configured cloud providers).
- **Watchlist** - stocks and crypto with live/cached price cards, sparklines and per-ticker settings.
- **Scanner** - technical scan over the watchlist and a crypto universe (RSI/MACD/ATR based signal variants, regime gating).
- **News** - deduplicated market and ticker news with FinBERT sentiment and an evidence ledger; optional SEC EDGAR filing metadata.
- **Trade radar** - candidate tracking with decision IDs that chain through risk, capital and paper execution.
- **Premarket** - US premarket scanner driven by the market-hours model.
- **Pre-earnings** - earnings calendar, pulse history and a decision matrix for upcoming reports.
- **Hotlist** - ranked symbols with lazy, cache-first detail refresh.
- **Calendar** - economic calendar with a committed US macro fixture.
- **Agents** - agent health, toggles, Risk Constitution editor, autonomy mode and the kill switch.
- **Alerts** - signal and news alerts with sound cues.
- **Capital** - paper portfolio, positions, execution ledger and reconciliation.
- **Backtest** - backtest history, walk-forward/OOS validation and reports.

Operational features: cache-first reads with explicit remote-refresh paths, per-agent external-call budgets, provider circuit breakers, structured health diagnostics, an append-only operator audit chain, scheduled backups, and a point-in-time data store for research.

## Architecture

```
run_server.py          -> bootstraps runtime/, data/, logs/; starts Uvicorn
api/                   -> FastAPI app (api/main.py) and per-domain service modules
core/                  -> domain logic: engine, scanner, signals, backtest, capital,
                          data model, news_v9, ai/, execution, decision_ledger, ml, ...
agents/                -> bounded background agents (scanner, news, pre-earnings,
                          premarket, trade radar, risk, execution, backtest, ...)
frontend/              -> static SPA served at / (index.html, app.js, per-view CSS/JS)
scripts/               -> one-off operator and audit scripts (e.g. provision_finbert.py)
tools/                 -> test runner, release/supply-chain checks, container entrypoint
tests/, tests_e2e/     -> unit/integration/contract/golden/perf suites and Playwright E2E
docs/                  -> architecture, implementation log, operations, testing, audits
deploy/                -> Prometheus/alerting examples for monitoring
```

The backend serves the frontend and the `/api/*` routes from the same process. Heavy work runs in agents or an optional engine subprocess so the UI stays responsive.

## Requirements

- Python **3.12** (see `.python-version`; CI and the Docker image also use 3.12).
- Windows, Linux or macOS. The project was developed on Windows; `start_market_signals.bat` is a Windows convenience launcher, everything else is cross-platform.
- Optional: [Ollama](https://ollama.com) for local AI analysis, Docker for the container build, Node/Playwright only for browser E2E tests.
- The pinned dependencies include `torch` and `transformers` for FinBERT sentiment, so the first install is several hundred MB.

## Quick start

```bash
git clone <your-fork-url> market-signals-v9
cd market-signals-v9

python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt

# local config and secrets (both are gitignored)
copy .env.sample .env                   # Linux/macOS: cp .env.sample .env
copy config.example.json config.json    # Linux/macOS: cp config.example.json config.json

python run_server.py
```

Open <http://localhost:8003/>. The API health endpoint is `GET /api/health`.

`run_server.py` loads `.env`, binds to `127.0.0.1:8003` by default (`MS_HOST` / `MS_PORT` override it), runs the engine as a subprocess and skips heavy warm-ups for stability. Pass `--no-repair-watchdog` to run without the self-repair watchdog during development.

On Windows you can also double-click `start_market_signals.bat`: it expects the virtual environment at `.venv\`, starts `run_server.py`, waits for `/api/health` and opens the browser.

### Optional API keys

All keys in `.env.sample` are optional; the app starts without any of them and the affected tabs report data as unavailable instead of failing. Fill in only what you use:

| Variable | Purpose |
| --- | --- |
| `FINNHUB_API_KEY` | Finnhub market data and extra news coverage |
| `EDGAR_IDENTITY` | Identity string the SEC requires for EDGAR access; enables the SEC tab |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Alpaca stock/crypto snapshots |
| `OLLAMA_EXE`, `OLLAMA_BASE_URL` | Local Ollama binary and endpoint for AI analysis |
| `MLFLOW_TRACKING_URI` | Enables the embedded MLflow tracking server |
| `RD_AGENT_*` | Optional RD-Agent research worker (needs a Python 3.10/3.11 interpreter) |
| `MS_API_KEY`, `MS_AUDIT_HMAC_KEY`, `MS_METRICS_BEARER_TOKEN` | Authentication and audit keys; required only when exposing the server beyond loopback or running the production compose file |

The remaining `MS_*` variables tune rate limits, call budgets, paths and backups; the comments in `.env.sample` explain each one. Cloud AI providers are configured from the Agents/AI settings in the UI, not from `.env`.

## Configuration

- **`.env`** holds secrets and process-level settings (keys, host/port, security boundary, traffic limits, path overrides). It is loaded by `run_server.py` and never read by the frontend.
- **`config.json`** holds application behaviour: signal thresholds, scan intervals, news settings, pre-earnings and premarket options, risk constitution, autonomy, paper execution, AI provider selection and more. `config.example.json` documents every section with defaults. Most of it is also editable from the UI, which writes back atomically.
- On first start the app copies a root-level `config.json` (and any legacy `watchlist.json`, `state.json`, `capital.json`) into `runtime/`, which becomes the live copy. Set `MS_CONFIG_PATH` to point elsewhere.
- **Local state is gitignored**: `runtime/`, `data/` (except the committed macro-calendar fixture), `logs/`, `audit_logs/`, `backups/`, `config.json` and `.env`. Deleting `runtime/` and `data/` resets the installation.

Security defaults: with no `MS_API_KEY` set every mutating endpoint is open, so the server refuses to bind to a non-loopback host unless an API key is configured. See `docs/V9_OPERATIONS.md` and `docs/API_TRAFFIC_PROTECTION.md` before exposing it on a network.

## Testing

```bash
pip install -r requirements-dev.txt

make test-smoke      # fast production-readiness gate
make test-backend    # unit + integration + contract + golden + perf
make test-contract
make test-frontend
make test-all        # everything; RUN_E2E=1 make test-all adds Playwright E2E
```

Without `make`, call the runner directly:

```bash
python tools/test_runner.py all --run-e2e 0
python tools/test_runner.py backend
```

Or plain pytest, as CI does (`.github/workflows/ci.yml` runs on Windows with Python 3.12):

```bash
TEST_MODE=1 pytest -q tests/unit tests/integration tests/contract tests/golden tests/perf
```

`TEST_MODE=1` redirects all state to temporary directories and disables engine autostart, warm-ups and external calls, so tests never touch your real `runtime/` or `data/`. Browser E2E tests under `tests_e2e/` need `playwright install` and a running server; they are skipped unless `RUN_E2E=1`. Supporting checks: `make supply-chain` (pip-audit + SBOM) and `make release-check`.

See `docs/testing.md` for the suite layout and the rules for adding endpoints and tabs.

## Docker

The `Dockerfile` builds a non-root `python:3.12-slim` image, exposes port 8003 and starts through `tools/container_entrypoint.py`, which prepares the persistent directories before launching the API. `compose.production.yml` wires that image to named volumes for runtime, data, backups, logs, output, reports and audit logs, runs it read-only with all capabilities dropped, and publishes the port on `127.0.0.1` only (put a TLS-terminating proxy in front).

```bash
docker compose -f compose.production.yml up --build -d
```

The compose file fails fast unless `.env` defines `MS_API_KEY`, `MS_AUDIT_HMAC_KEY`, `MS_METRICS_BEARER_TOKEN`, `MS_PUBLIC_HOST` and `MS_PUBLIC_ORIGIN`. Prometheus scrape and alert rule examples live in `deploy/monitoring/`.

## Documentation

| Document | Contents |
| --- | --- |
| `docs/V9_ARCHITECTURE.md` | Layered architecture, data flow, execution boundary |
| `docs/V9_IMPLEMENTATION.md` | Phase-by-phase implementation log and status table |
| `docs/V9_OPERATIONS.md` | Autonomy modes, kill switch, health, backups, paper-trading activation |
| `docs/V9_RUNTIME_FLOW_MAP.md` | UI -> API -> domain -> persistence map and cache semantics |
| `docs/testing.md` | Test suites, quality gate, how to add endpoints/tabs |
| `docs/OPERATIONS_RUNBOOK.md` | Day-to-day operator runbook |
| `docs/API_TRAFFIC_PROTECTION.md` | Rate limiting and request budgets |
| `docs/V9_AI_SYSTEM.md` | Multi-provider AI layer and decision engine |
| `docs/V9_RISK_CONSTITUTION.md` | Risk rules that gate paper execution |
| `docs/V9_OPEN_SOURCE_MANIFEST.md` | Evaluation and licence record for every external project considered |
| `docs/SUPPLY_CHAIN_SECURITY.md` | Dependency pinning and audit policy |

Feature deep-dives: `docs/WATCHLIST_SYSTEM.md`, `docs/NEWS_TAB_SYSTEM.md`, `docs/PRE_EARNINGS_SYSTEM.md`, `docs/HOTLIST_SYSTEM.md`, `docs/TRADING_STRATEGIES.md`, `docs/BACKTEST_SCORING_GUIDE.md`.

## Disclaimer

This is a research and educational tool. Nothing it produces is financial advice, and no signal, score, backtest or AI output is a recommendation to buy or sell anything. Market data comes from third-party providers and may be delayed, incomplete or wrong. The only execution path shipped is a paper broker; use the software at your own risk and verify everything independently before acting on it.

## License

MIT License, see `LICENSE`.

## Third-party notices

Licences and sources of the bundled dependencies and integrated open-source projects are listed in `THIRD_PARTY_NOTICES.md`.
