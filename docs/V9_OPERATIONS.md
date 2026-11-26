# V9_OPERATIONS

Living document. Structure from spec §45, §74, §98, §103, §104–§106. Implementation `NOT STARTED` at Phase 0; built across phases.

## Autonomy modes (§45)
OFF, ANALYZE, RECOMMEND, PAPER_AUTO, LIVE_ASSISTED, LIVE_AUTO. Default = OFF (NOT LIVE_AUTO). LIVE_AUTO may only be enabled explicitly by the owner.

## Paper auto before live auto (§46)
The full V9 decision loop must first prove itself in PAPER_AUTO. LIVE_AUTO stays disabled until mandatory Paper-Auto acceptance tests pass.

## Kill switch (§74)
Prominent "STOP AUTONOMOUS TRADING" control. Immediately blocks new automated risk. Monitoring remains active.

## System health (§98)
Real health checks for: backend, DB, scheduler, market provider, earnings, options, news, SEC, AI providers, ML model, Paper Broker, live broker if configured, research worker, MLflow if used, RD-Agent worker if used. No fake green status.

## Job states (§103)
QUEUED, RUNNING, PROGRESS, COMPLETE, FAILED, CANCELLED. Heavy computation (scans, backtests, AI batches, ML training, Monte Carlo, research) must not freeze the app (§102).

## Migrations (§104)
Use DB migrations. Test: REALISTIC V7 SCHEMA COPY → V9 MIGRATION → V9 START → V7 DATA PRESERVED. Do not delete the database for convenience.

## Backups and rollback (§105)
Before destructive migration: create verified backup in dev/test workflow; provide migration rollback strategy when practical.

## Secrets (§106)
Never commit API keys; never store secrets in prompts; never expose keys to frontend; never expose broker credentials to Coding AI; never log tokens.

## Crash recovery (V7 audit F5, to be addressed)
V7 has no crash recovery on Windows (engine poll() reports but does not respawn). V9 operations must add a watchdog. To be designed in Phase 18.

## Phase 10 News / FinBERT / SEC runtime

- Install the pinned `requirements.txt` in the same interpreter that runs the API/engine.
- Provision the immutable model once with `python scripts/provision_finbert.py --allow-download`. Normal runtime is `local_files_only`; it never silently changes model revision. `python scripts/provision_finbert.py` is the cache-only readiness check.
- Set `EDGAR_IDENTITY` in `.env` to the real operator name/contact required by SEC policy. Never put it in committed config. Until configured, `/api/sec/{ticker}` returns an explicit unavailable payload and News/trading remain operational.
- SEC cache TTL, forms, and result bounds live under `config.sec`. Refresh does not download filing bodies; only bounded metadata is cached.
- Real Phase 10 health means FinBERT source/revision is present and SEC is either `available`, `stale_cache`, or explicitly `unavailable`; do not turn missing identity into fake green.

## Phase 11 Paper Trading activation and emergency control

Use the Agents page as the owner control surface. First choose every Risk Constitution value and explicitly confirm/apply it. Then choose a REDUCE percentage (strictly greater than 0 and less than 100), run **Reconcile**, and select **PAPER_AUTO**. No trading value is prefilled; OFF, ANALYZE, and RECOMMEND remain usable before activation.

The backend rejects PAPER_AUTO while any prerequisite is missing: active owner Risk Constitution, owner-selected REDUCE percentage, enabled Paper Execution Agent, healthy reconciliation, and inactive Kill Switch. `EXECUTION_STATE_CONFLICT` must be investigated and reconciled before new automated risk can resume.

**STOP AUTONOMOUS TRADING** immediately blocks new automated Paper risk. It does not liquidate positions or stop monitoring; audited REDUCE/CLOSE paths remain available. Releasing the switch does not bypass the other readiness checks.

The execution ledger defaults to `data/execution.db` and may be redirected with `MS_EXECUTION_DB_PATH`. Keep its DB/WAL/SHM files together during backup/restore. Do not delete the ledger to clear a conflict; retain evidence and reconcile the underlying state.
