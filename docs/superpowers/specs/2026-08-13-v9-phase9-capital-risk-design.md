# Phase 9 — Capital and Risk Constitution Design

## Goal

Implement sections 47/48 and 65–67 as an additive deterministic boundary over V7 capital accounting. Preserve `CapitalManager` paper execution and its existing risk gates; add the missing sizing methods, complete portfolio constraints, exact accounting contract, and structured position evidence/reassessment.

## Protected boundary

`docs/V9_RISK_CONSTITUTION.md` is PROTECTED and still contains `OWNER_MUST_SET`. This phase does not edit it, choose limits, or claim owner confirmation. The implementation accepts an explicit `risk_constitution` mapping only when `owner_confirmed` is true and every required field is valid. Missing, partial, or unconfirmed constitutions fail closed in the new engine.

The existing V7 `RiskAgent` behavior remains the compatibility path until owner confirmation exists. Once a complete confirmed mapping is present, `RiskAgent` composes the new constraint engine before its legacy checks. An autonomous actor cannot manufacture confirmation through any registered AI tool; the AI tool registry has no arbitrary config-write surface.

## Package

`core/portfolio/`:

- `types.py`: immutable finite domain types for capital sizing, proposals, positions, portfolio snapshots, constraint decisions, accounting, and position actions.
- `sizing.py`: fixed amount, fixed percentage, fixed risk, inverse volatility, and volatility targeting. Advanced HRP/NCO/CVaR/risk-budget optimizers remain absent until independently validated.
- `constraints.py`: validated `RiskConstitution` plus the complete section 66/47 gate.
- `accounting.py`: exact open/close cash, margin, gross P/L, fees, and conservation identities.
- `position.py`: required section 67 evidence persistence and deterministic HOLD/REDUCE/CLOSE reassessment.
- `adapter.py`: convert V7 positions/signals and `CapitalManager` state into the new immutable contracts.

## Capital sizing

Every method returns a transparent formula and requested notional; it does not silently override portfolio policy.

- fixed amount: explicit currency amount;
- fixed percentage: equity × configured percentage;
- fixed risk: equity × risk percentage / stop-loss fraction;
- inverse volatility: equity × risk-budget percentage / annualized asset volatility;
- volatility targeting: equity × allocation percentage × target volatility / asset volatility.

All inputs must be finite. Percentages and volatilities have explicit units. A sizing result is never executable until the constraint engine approves it.

## Constraints

The proposed order is checked against:

- constitution confirmed and kill switch inactive;
- approved strategy evidence when required;
- data freshness and conflict policy;
- max leverage and max position count;
- projected symbol position, gross exposure, sector exposure, and correlated exposure;
- minimum remaining cash;
- loss at stop / risk per trade;
- average-dollar-volume participation / liquidity;
- daily loss and peak-to-current drawdown;
- live autonomy owner permission for live autonomous proposals.

Each check is named and auditable. Missing sector, correlation, liquidity, strategy, or timestamp evidence fails closed when that constraint needs it. Decisions never rely on AI-supplied portfolio values.

## Exact accounting

The pure accounting contract mirrors the V7 paper ledger:

```text
open cash = starting cash - margin - entry fee
close cash = open cash + margin + gross P/L - exit fee
ending cash = starting cash + gross P/L - entry fee - exit fee
```

LONG and SHORT use recorded quantity and execution prices. All values are finite and no cash balance may become negative at open. Golden tests compare the pure contract with V7 `CapitalManager` behavior under injected verified prices.

## Position contract

Every newly opened paper position gains:

- `entry_thesis`;
- `supporting_evidence`;
- `opposing_evidence`;
- `strategy`;
- `signal`;
- `ai_model`;
- `invalidation_rules`;
- `risk_state`.

The adapter derives these fields from the already persisted `decision_context`, signal identity, app-authoritative levels, and risk context. Manual/fallback positions receive honest system provenance rather than fabricated AI evidence.

Reassessment is deterministic: invalidation or critical risk produces CLOSE; a degraded risk state or a non-critical breached constraint produces REDUCE; otherwise HOLD. The Kill Switch blocks new risk but does not force liquidation; reassessment and monitoring remain active. Phase 9 returns decisions and audit reasons. Execution of those actions remains with the existing position/execution surfaces and Phase 11.

## Integration and compatibility

- `CapitalManager._apply_position_explainability` composes the section 67 adapter; existing fields remain.
- `RiskAgent.evaluate_signal` invokes the new engine only when a complete owner-confirmed constitution exists. Invalid confirmed configuration rejects with `risk_constitution_invalid`; unconfirmed/missing configuration keeps the audited V7 compatibility path and keeps Phase 9 status honest.
- `DEFAULT_CONFIG` and `config.example.json` add a null-valued `risk_constitution` template with `owner_confirmed: false`. Null is intentional: Codex does not select protected owner values.
- No new dependencies, migrations, API routes, or UI.

## Acceptance

- GOLD-006 capital conservation;
- GOLD-007 exact trade accounting;
- GOLD-008 risk veto across every mandatory constraint;
- GOLD-009 kill switch blocks new risk while reassessment/monitoring remains available;
- GOLD-016 protected policy modification rejected;
- existing capital/risk/position tests remain green;
- Phase 9 cannot be marked PASS until the owner supplies and confirms the protected values.
