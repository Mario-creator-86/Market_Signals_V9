# Phase 9 — Capital and Risk Constitution Implementation Plan

## Constraints

- Do not edit `docs/V9_RISK_CONSTITUTION.md` or select owner limits.
- Compose `CapitalManager`, `RiskAgent`, existing risk limits, and autonomy/kill-switch code.
- Preserve V7 paper accounting and all current call signatures.
- All new arithmetic is finite, deterministic, and independently testable.
- No advanced allocator is implemented without validation.

## Tasks

- [x] Add immutable capital/portfolio/risk/position types.
- [x] Add the five deterministic sizing methods and formula tests.
- [x] Add exact accounting and conservation tests.
- [x] Add explicit owner-confirmed constitution parsing and all mandatory constraints.
- [x] Add section 67 position evidence and deterministic reassessment.
- [x] Compose position evidence into `CapitalManager`.
- [x] Compose confirmed-constitution veto into `RiskAgent` without weakening legacy limits.
- [x] Add null-valued config template and preservation tests.
- [x] Add GOLD-006/007/008/009/016.
- [x] Run dedicated, curated, full, static, dependency, and V7-isolation gates.
- [x] Record Phase 9 as awaiting owner values unless explicit confirmation is provided.

Exit status: implementation and verification complete. Phase 9 remains IN PROGRESS because the protected production Risk Constitution still contains `OWNER_MUST_SET`; no autonomous value or confirmation was manufactured.
