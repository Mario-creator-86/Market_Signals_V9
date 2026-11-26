# Phase 15 - Autonomous Development Factory design (§80/§81/§82/§83/§84/§112)

## Scope

The controlled pipeline that lets AI-generated code changes exist ONLY in
isolation, be reviewed by agents distinct from the implementer, and be
blocked from touching protected surfaces. Promotion of reviewed changes is
Phase 16; Phase 15 ends at a recorded, fully-reviewed, sandbox-verified
submission.

## Modules (`core/development_factory/`)

- `sandbox.py` (§80) — `create_experiment_worktree(repo, experiment_id,
  base_ref)` creates a git worktree + branch `ai/experiment/EXP-*` from the
  base ref; `worktree_status`, `remove_worktree`. Production files are never
  touched: every write path is inside the worktree (verified by prefix check
  against the repository root).
- `policy.py` (§82/§83/§84) — the goal-hacking guard over a unified diff:
  * PROTECTED_PATHS (§82): `docs/V9_RISK_CONSTITUTION.md`,
    `core/autonomy.py` protected-target files, promotion policy files,
    holdout configuration, live-permission config — any diff hunk touching
    them is rejected with `PROTECTED_FILE`.
  * CONSTITUTIONAL_TESTS (§84): `tests/golden/**` may not be deleted or
    modified (`GOLDEN_TEST_MODIFIED`), and no diff may add `pytest.skip`,
    `pytest.mark.skip`, `xfail`, or weaken an assertion line
    (`TEST_WEAKENED` heuristics: skip/xfail markers only — assertion
    semantics stay under golden immutability).
  * Cost/evaluation transparency (§83): diffs removing `slippage`/`fees`/
    `cost` parameters from backtest modules are flagged
    (`EVALUATION_WEAKENED`).
- `review.py` (§81) — the staged review ledger. A `Submission` (experiment
  worktree + unified diff + author) walks: CODE_REVIEW → SECURITY_REVIEW →
  TESTS → QUANT_VALIDATION → E2E → SHADOW_READY. Each stage records its own
  reviewer actor and verdict. Hard rules: a reviewer actor may not equal the
  submission author (§81); a stage may only be recorded by its designated
  actor class; verdicts are append-only. A submission is SHADOW_READY only
  when every stage passed with distinct actors.
- `pipeline.py` (§112) — `submit_change(...)` runs: guard the diff
  (policy) → apply to the isolated worktree (sandbox) → open the staged
  review ledger. `record_review(...)` advances stages. Actual coding agents
  plug in as authors/reviewers (Phase 13 Model Lab roles `coding`/`review`);
  with none configured the pipeline records honest NOT_PERFORMED reviews
  rather than faking passes. E2E/test execution results are supplied as
  evidence payloads by the caller (deterministic runner in Phase 17).
- Stores: `data/development_submissions.json` (atomic JSON, event-logged,
  same pattern as Phase 14). Runtime file git-ignored; test redirect env
  `MS_DEVELOPMENT_SUBMISSIONS_PATH`.

## API

`GET /api/development/submissions`, `POST /api/development/submissions`
(guard + sandbox + open reviews), `POST /api/development/submissions/{id}/review`
(record one stage). Bounded 422 errors. Cockpit capability markers flip
Coding/Review Agent from PHASE_15 NOT_STARTED to AVAILABLE.

## Safety

The factory never: writes outside a worktree, promotes (Phase 16), edits
protected files, weakens golden tests, or lets the implementer self-approve.
GOLD-016's `assert_not_weakening` remains the runtime guard; §83 checks are
pre-merge diff gates on top of it.
