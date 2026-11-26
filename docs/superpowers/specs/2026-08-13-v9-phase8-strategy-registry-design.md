# Phase 8 — Strategy Registry / Champion-Challenger Design

## Goal

Implement §62–§64 as a persistent governance layer without replacing the V2 executable strategy catalog or its performance cache.

## Existing surfaces retained

- `core.strategy_v2.registry`: executable `StrategySpec` definitions and evaluators.
- `core.strategy_v2.performance_cache`: per-symbol/direction rolling and OOS metrics consumed by the selector.
- `core.backtest`: Phase 7 authoritative OOS, holdout, and overfitting evidence.

The new layer records strategy identity, version, lifecycle, promotion evidence, and champion ownership. It does not duplicate evaluator code or store live rolling metrics.

## Package

`core/strategy_registry/`:

- `types.py`: immutable `StrategyRecord`, `StrategyStatus`, `PromotionEvidence`, `PromotionThresholds`, `PromotionDecision`, and audit events.
- `policy.py`: deterministic promotion evaluation. A single superior backtest is insufficient: OOS, sample size, drawdown, risk-adjusted evidence, stability, costs, regime coverage, recent evidence, shadow evidence, and untouched-final-holdout attestations are required.
- `store.py`: atomic JSON persistence, schema versioning, corruption fail-closed behavior, legal lifecycle transitions, one champion per scope, and append-only event history.
- `bridge.py`: maps the eight existing V2 `StrategySpec` definitions into versioned RESEARCH records without inventing validation status.
- `optuna_policy.py`: §64 research-only study guard; final holdout cannot be part of search, raw profit cannot be the sole objective, trial budgets are bounded, and the best trial never auto-promotes.

## Lifecycle

```text
RESEARCH -> CHALLENGER -> SHADOW -> APPROVED -> CHAMPION
    |             |          |          |
    +-----------> RETIRED <---+----------+

CHAMPION -> DEGRADED -> APPROVED / RETIRED
CHAMPION -> APPROVED when a validated replacement is promoted
```

`selection_scope = horizon + sorted universe`. At most one CHAMPION exists per scope. Promotion is atomic: the prior champion is moved to APPROVED and an audit event records both versions plus a SHA-256 fingerprint of the evidence and policy.

## Promotion gates

Named thresholds require:

- OOS verdict ROBUST or MODERATE;
- minimum OOS sample, positive expectancy, minimum profit factor;
- bounded OOS drawdown and minimum Sharpe;
- stable parameters and costs included;
- minimum regime coverage;
- positive recent evidence with minimum sample;
- positive shadow evidence with minimum sample and bounded drawdown;
- final holdout untouched by optimization;
- evaluation period and promotion criteria locked before results;
- if a champion exists, challenger OOS and shadow expectancy must beat it by the predefined margin.

No AI-provided prose can promote a record. No `register()` call may create a CHAMPION directly.

## Selector integration

`StrategySelector` receives an optional lifecycle registry. `strategy_v2.lifecycle_enforcement_enabled` defaults to false to preserve V7 behavior while existing definitions are seeded as RESEARCH. When explicitly enabled, only APPROVED or CHAMPION versions may enter executable candidate selection; all other statuses are rejected with an auditable reason. This switch cannot promote records or weaken promotion gates.

## Persistence and recovery

The JSON file uses atomic `os.replace`, a process lock, a schema version, deterministic serialization, and no delete API. Malformed files raise `StrategyRegistryCorruptionError`; they are never treated as an empty registry.

## Scope boundary

Phase 8 provides registry, lifecycle, deterministic promotion, audit history, selector enforcement seam, and §64 Optuna policy. Live shadow data production, automatic rollback, and UI are Phase 16/12. The Phase 8 registry can store SHADOW status/evidence but does not manufacture shadow observations.
