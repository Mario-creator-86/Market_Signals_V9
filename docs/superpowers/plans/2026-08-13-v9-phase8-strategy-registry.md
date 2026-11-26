# Phase 8 — Strategy Registry / Champion-Challenger Implementation Plan

## Constraints

- Compose the V2 executable registry and Phase 7 backtest evidence; do not rewrite either.
- No direct CHAMPION registration and no promotion from one backtest.
- No final-holdout optimization, automatic Optuna promotion, dependency additions, or live execution changes.
- Use atomic persistence and deterministic tests with temporary paths.

## Tasks

- [x] Add immutable lifecycle, evidence, threshold, decision, and audit types with input validation.
- [x] Add deterministic multi-factor promotion policy and bad-challenger rejection tests.
- [x] Add atomic persistent registry, legal transitions, champion uniqueness, and promotion audit.
- [x] Add V2 `StrategySpec` bridge and seed tests; all migrated specs start RESEARCH.
- [x] Add §64 Optuna study policy and final-holdout/objective/trial/auto-promotion guards.
- [x] Add optional selector lifecycle enforcement, default off, with regression tests.
- [x] Add GOLD-062/063/064 tests.
- [x] Run dedicated, curated, and full gates; update V9 control documents; create local checkpoints.
