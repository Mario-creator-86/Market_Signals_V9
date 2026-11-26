# Phase 16 - Shadow / Promotion / Rollback design (§85/§86/§87, GOLD-017)

## Scope

Close the champion/challenger loop: identical-condition shadow comparison
(§85), evidence-gated promotion that reuses the locked Phase 8 gates (§86),
and automatic rollback of a promoted version when its recorded regression
conditions breach (§87 / GOLD-017). Risk limits are never touched by any of
these paths (§48 / GOLD-016 stay authoritative).

## Modules (`core/promotion/`)

- `records.py` — `PromotionRecord` (previous_version, new_version,
  promotion_time, promotion_evidence, rollback_conditions, status
  ACTIVE/ROLLED_BACK) in an atomic JSON store. Rollback conditions are
  validated against a fixed kind set (`expectancy_r_below`,
  `profit_factor_below`, `drawdown_above`, `sample_minimum`) with numeric
  thresholds and optional windows; a promotion without conditions is
  rejected (§87 requires them). Rollback is idempotent (at most once).
- `lifecycle.py`:
  - `compare_shadow` (§85) — pairs champion/challenger decision runs ONLY on
    identical `inputs_sha256`; computes agreement rate and majority
    outcomes on the comparable subset; honest `identical_inputs=False` when
    nothing pairs.
  - `promote_from_shadow` (§86) — only a SHADOW experiment can be promoted;
    the caller injects the Phase 8 `evaluate_promotion` decision; a rejected
    decision leaves no state change (no record, no transition). On approval
    the promotion record is appended and the experiment transitions to
    PROMOTED with the promotion id as evidence.
  - `evaluate_rollback` (§87, GOLD-017) — deterministic evaluation of the
    recorded conditions over supplied metrics; on breach: promotion record →
    ROLLED_BACK with reason, experiment → ROLLED_BACK with
    `restored_version` evidence, response carries `restored_version` and
    `risk_limits_changed: False`. Idempotent re-evaluation returns the
    original rollback.

## API

`GET /api/promotion/records`, `POST /api/promotion/records/{id}/evaluate-rollback`
(bounded metrics payload; 422 on invalid promotion). Cockpit: the
`shadow_promotion_rollback` deferred marker is removed and the capability
flips to AVAILABLE.

## Tests

Unit: shadow pairing honesty, condition validation, promotion requirements
(SHADOW-only, mandatory rollback conditions), gate-rejection leaving no
trace. Golden: GOLD-017 (healthy → no rollback; breach → rollback restores
v1, experiment ROLLED_BACK, risk untouched, idempotent) and the §86
gate-freeze.
