# Phase 14 - Research Factory design (§75/§76/§77/§78/§79/§111, §64 Optuna)

## Scope

A dedicated research subsystem, separate from production execution (§75), that
closes the loop: observed weakness → structured hypothesis → governed
experiment → deterministic evaluation (Phase 7 OOS gate / backtest) →
VALIDATED/REJECTED → SHADOW handoff. Promotion/rollback mechanics complete in
Phase 16; Phase 14 hands off at SHADOW.

## Modules (`core/research_factory/`)

- `hypothesis.py` (§76) — frozen `HypothesisRecord` with every mandated field
  (hypothesis_id, problem, observation, hypothesis, expected_mechanism,
  baseline, challenger, universe, horizon, dataset, metrics,
  promotion_criteria, failure_criteria, risks). Validation rejects vague
  records: problem/observation/hypothesis/expected_mechanism must be
  non-empty; promotion_criteria and failure_criteria must be explicit
  (non-empty lists); metrics non-empty. Atomic JSON store
  (`data/research_hypotheses.json`), append + immutable updates by event log,
  same pattern as `core/strategy_registry/store.py`.
- `experiments.py` (§77) — frozen `ExperimentRecord` with the 9 spec statuses
  (PROPOSED → APPROVED_FOR_RESEARCH → RUNNING → {FAILED | REJECTED |
  VALIDATED → SHADOW → {PROMOTED | ROLLED_BACK}}), a legal-transition table,
  hypothesis_id linkage, strategy_id/version linkage, evidence payload,
  and an append-only event log. `ExperimentRegistry` atomic JSON store
  (`data/research_experiments.json`).
- `weakness.py` (§111) — deterministic weakness detection over
  `StrategyPerformanceCache` + `data/trade_journal.json`: per
  strategy/direction, rolling-window expectancy, win rate, drawdown and
  sample size vs explicit thresholds → structured `WeaknessObservation`
  records (strategy, metric, value, threshold, sample, window) — never a
  vague "maybe change RSI". Observations are the only sanctioned input for
  hypothesis drafting.
- `drift.py` (§79) — `DriftMonitor` computes rolling-window drift metrics
  (win rate, expectancy, trade frequency, average R) comparing the recent
  window to the reference window from the trade journal, per strategy.
  Verdict: STABLE / DRIFT_SUSPECTED / INSUFFICIENT_SAMPLE. Drift emits a
  REVALIDATION event into the linked experiment/registry (candidate
  DEGRADED trigger through the existing StrategyRegistry transition) — it
  NEVER changes risk, sizing or autonomy (§79).
- `memory.py` (§78) — `ResearchMemory` builds structured relationships
  (experiment ↔ hypothesis ↔ strategy ↔ decision-ledger entries ↔ outcomes)
  from the existing stores (experiment registry, hypothesis store,
  strategy registry events, Phase 13 decision ledger, performance cache).
  Numeric authoritative facts stay in their structured stores; memory is an
  index over them, queryable by strategy/experiment/ticker. No vector store
  is added (discovery is by structured query; §78 permits but does not
  require vector search).
- `pipeline.py` (§111) — `run_research_cycle(config)`: weakness → auto-draft
  hypothesis (from a structured observation; owner/AI may submit their own)
  → create PROPOSED experiment → APPROVED_FOR_RESEARCH → RUNNING →
  deterministic evaluation (injected evaluator; default uses the Phase 8
  `OosGateRunner` verdict seam over caller-supplied baseline/challenger
  evidence) → REJECTED or VALIDATED → SHADOW handoff (experiment status +
  optional StrategyRegistry SHADOW transition). Research-only: the pipeline
  can never place orders, change risk, or promote (promotion gates stay in
  Phase 8/16).
- `optuna_runner.py` (§64) — persisted Optuna execution behind the Phase 8
  `validate_optuna_study` guardrail: sqlite study storage under `data/`,
  bounded trials/timeout, research-only objective supplied by the caller,
  best trial recorded into the experiment evidence but NEVER auto-promoted.
  Optuna import is lazy; absence records an honest OPTUNA_UNAVAILABLE
  status instead of failing.
- `tracking.py` — `ResearchTracking` adapter seam (start_run / log_params /
  log_metrics / finish). Default `JsonTracking` writes to the experiment
  evidence; `MlflowTracking` is a lazy-import optional adapter activated by
  `MLFLOW_TRACKING_URI` — V9 domain logic never imports MLflow directly and
  remains fully functional without it (spec §17 adapter boundary; evaluated
  in the Phase 2 manifest).

## Agents (§75)

The nine §75 roles are represented as the pipeline stages plus the Phase 13
Model Lab roles (`research`, `quant`, `coding`, `review`) — each stage's
output is a structured record with an author field (`research_director`,
`hypothesis_agent`, `experiment_planner`, `quant_validator`). Fully
autonomous coding/review agents are Phase 15; Phase 14 persists their stage
authorship so Phase 15 can attach implementations without schema changes.

## API + Cockpit

`api/research_factory_service.py` + routes: `GET /api/research/weaknesses`,
`GET /api/research/hypotheses`, `GET /api/research/experiments`,
`POST /api/research/experiments/{id}/advance` (bounded, deterministic,
research-only), `GET /api/research/drift`, `GET /api/research/memory`.
Cockpit `research_factory` section flips from the PHASE_14 deferred marker
to real hypotheses/experiments/drift counts.

## Honest boundaries

- No RD-Agent runtime dependency: the internal architecture implements the
  §3/§75 loop; RD-Agent remains an optional external worker (Phase 2
  decision, existing health probe). Production trading never blocks on it.
- Evaluation evidence comes from caller-supplied deterministic backtest/OOS
  artifacts; the pipeline never invents numbers.
- Promotion stays gated by Phase 8 policy; Phase 14 stops at SHADOW.
