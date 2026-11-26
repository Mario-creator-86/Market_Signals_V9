# Phase 13 - Decision Ledger, Model Lab and AI Replay design

## Scope

Phase 13 makes every trading decision — executed or not — a persistent,
point-in-time, queryable record, and turns the accumulated record into an
evaluation asset. It delivers spec §88 (Model Lab), §89 (AI replay),
§90 (recording no-trade decisions and measuring rejection quality) and the
persistent §97 eleven-question Decision Ledger that Phase 12 rendered from
computed audit rows with `coverage: PARTIAL_PHASE12`.

It does not: change any decision logic, relax the §42 pipeline, allow replay
to reach the execution broker, or create one global AI score (§88 forbids it).

## Persistent Decision Ledger (`core/decision_ledger/`)

### Store

`DecisionLedgerStore` follows the Phase 11 `ExecutionStore` pattern: SQLite,
WAL, per-operation connections, `BEGIN IMMEDIATE` writes, idempotent
migration to `user_version=1`, bounded reads. Tables:

- `decision_ledger_entries` — one row per `decision_id` (PK). Append-only:
  a redelivered `decision_id` returns the original row and never rewrites it.
  Columns: `decision_id`, `created_at`, `ticker`, `horizon`, `action`,
  `source` (`SIGNAL_PATH`/`PRE_EARNINGS_PATH`/`REPLAY`), `replay_of`
  (NULL except replay entries), `entry_json` (the full §97 evidence record).
- `decision_ledger_outcomes` — `(decision_id, evaluated_at)` PK, append-only;
  the latest row per decision is the current outcome (§89 "compare against
  future result afterward" — later evaluations may refine, history is kept).
- `model_lab_runs` / `model_lab_results` — §88 comparisons (below).

Path resolution mirrors Phase 11: env `MS_DECISION_LEDGER_PATH`, then
`config["decision_ledger"]["store_path"]`, then `data/decision_ledger.db`.
Runtime DB/WAL files are git-ignored; tests redirect via `conftest.py`.

### Entry record (§97 eleven questions)

Each `entry_json` answers, from persisted evidence only:

```
what_happened    final action + status + reason (event summary)
data_existed     bounded point-in-time input snapshot (scalar facts + digests)
strategy         strategy_id / strategy_type used
ai_model         provider + model + prompt_version + prompt/response digests
prompt           prompt_version, sha256 digest, capped prompt text
ai_proposal      §41 Decision (action, confidence, thesis, evidence lists)
critic           §44 verdict (veto, categories, severity, rationale digest)
risk             §42 policy/risk-gate checks + Risk Agent evaluation summary
capital_allowed  authoritative sizing / capital evidence (§43 — app-computed)
executed         order linkage: order_intent_id + final state, or "none"
afterward        outcome summary once evaluated (forward returns vs action)
```

Untrusted external text never enters the ledger verbatim: News/SEC strings
inside snapshots keep their Phase 10 `UNTRUSTED_EXTERNAL_DATA` digests.

## Prompt/model capture

`EvaluationResult` (Phase 4C) gains three defaulted fields — `prompt`,
`model`, `prompt_version` — populated by `evaluate_trade_decision()` where
both prompt and response model exist today but are discarded. Frozen-dataclass
back-compat: all existing five-positional-arg constructor calls stay valid.
`build_trade_review_prompt` gains a module constant `TRADE_REVIEW_PROMPT_VERSION`
so the ledger answers "which prompt?" with a version, not just text.

## Recorder hook

`record_decision(...)` is called from `agent_manager` immediately after
`build_signal_decision_context()` — on BOTH the accepted and the rejected
path (§90: no-trade decisions are recorded, not just trades). It is
fail-open for the trading path (ledger failure logs and never blocks an
order decision; the ledger is an audit surface, not a gate). Replay entries
share the same store and are marked `source=REPLAY` — the execution runtime
ignores them, so a replay can never become an order.

## AI Replay (§89, GOLD-002)

`build_pit_snapshot(ticker, as_of)` composes the decision inputs from the
Phase 1 point-in-time contract: `HistoricalStore.as_of()` for each entity;
anything without a record available at T renders `UNAVAILABLE` (§32) — the
replay never reconstructs missing history from future data. The snapshot
records per-entity `available_from` so a test can prove the T+1 bar is
absent from a T replay.

`run_ai_replay(decision_id, config, ...)` re-runs the decision engine on the
identical prompt rebuilt from that snapshot (same `prompt_version`) with a
caller-selected model, records the result as a `REPLAY` entry linked via
`replay_of`, and never executes.

`evaluate_decision_outcome(entry, bars_after)` computes the deterministic
afterward-facts: forward return over the horizon window, gap result, and
whether the action direction was favorable. Outcomes are appended with the
evaluation timestamp; entries keep only a latest-outcome summary.

## No-trade evaluation (§90)

`evaluate_no_trade_decisions(entries, bars, *, horizon, min_move_pct)`
partitions recorded NO_TRADE/rejected decisions into:

- `correctly_rejected` — subsequent adverse move beyond threshold;
- `incorrectly_rejected_profitable` — subsequent favorable move beyond
  threshold (the "filter too conservative" signal);
- `inconclusive` — insufficient post-decision data (honest, never guessed).

Deterministic, threshold-explicit, no probability language.

## Model Lab (§88)

`ModelLab.run(role, snapshot, models, config)` executes every configured
model against ONE frozen snapshot (identical prompt, `inputs_sha256`
recorded per run) and stores per-model: provider, model, response digest,
parse/schema validity, decision distribution, critic verdict. Comparisons
are per-role (`pre_earnings`, `market_analyst`, `news_analyst`, `quant`,
`risk_critic`, ...) — there is deliberately no cross-role aggregate score.
Metrics are counts/rates over stored runs; nothing is fabricated when a
provider is unavailable (`ai_available=false` recorded, not imputed).

## API + Cockpit

`api/ledger_service.py` owns logic; `api/main.py` adds thin routes:

- `GET /api/ledger/decisions?limit=` — newest entries, eleven-question rows;
- `GET /api/ledger/decisions/{decision_id}` — full record + outcome history;
- `GET /api/ledger/no-trade-evaluation` — §90 metrics;
- `GET /api/ledger/model-lab` — runs + per-role comparisons;
- `POST /api/ledger/replay` — bounded replay request (AI must be configured;
  replays are audit artifacts and can never place orders).

Cockpit `_decision_rows` prefers persistent ledger rows; the computed
Phase 12 audit rows remain the fallback while the ledger is empty. The
`coverage: PARTIAL_PHASE12` marker flips to `PERSISTENT_LEDGER` when
ledger entries exist. The `deferred` `decision_replay`/`model_lab` markers
are replaced by real availability sections. UI states remain
LOADING/SUCCESS/EMPTY/ERROR with no fabricated values (§99).

## Golden tests

- `test_phase13_golden.py::GOLD-002` — end-to-end PIT replay: a future bar
  provably absent from the T snapshot, replay recorded as `REPLAY` +
  `replay_of`, outcome evaluation deterministic, replay entry cannot reach
  the broker.
- GOLD-005 re-pointing — Phase 5A already froze the §50 score
  (`test_phase5a_golden.py`); the matrix row is corrected to cite it.
- §90 golden — rejection-quality partition is frozen against a fixed bar
  fixture (correctly vs incorrectly rejected vs inconclusive).
- §88 golden — identical-input digest across models and per-role isolation.

## Honest boundaries

- Outcomes need post-decision market data; entries without enough future
  bars stay `inconclusive` — never imputed (§32 spirit).
- Model Lab executes real configured providers only; with none configured
  it reports unavailable, it does not simulate.
- The §51 pre-earnings stack stubs (strategy registry/backtest evidence
  from Phases 7/8) remain honestly unavailable in the §51 context builder;
  wiring them is tracked as remaining work, not silently faked here.
