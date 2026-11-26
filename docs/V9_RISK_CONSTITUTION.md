# V9_RISK_CONSTITUTION  — PROTECTED

> **PROTECTED (spec §48, §82).** No autonomous agent may modify this document, the kill switch, promotion gates, critical tests, live-autonomy permissions, holdout policy, or final-holdout policy without explicit owner authorization. Owner action required for any change.

Living document. Fields from spec §47. Values marked `OWNER_MUST_SET` are introduced by V9 and require the owner to set them (Phase 9). Values marked `V7_CURRENT` reflect the existing V7 limit where one exists (to be audited and confirmed in Phase 9).

| Field | Value | Source |
|---|---|---|
| Max risk per trade | OWNER_MUST_SET | §47 |
| Max position size | OWNER_MUST_SET (V7 has max-positions cap, recently raised 10→100) | §47 |
| Max total exposure | OWNER_MUST_SET | §47 |
| Leverage | OWNER_MUST_SET (V7 has 5x path — audit flagged no liquidation guard) | §47 |
| Minimum cash | OWNER_MUST_SET | §47 |
| Max sector exposure | OWNER_MUST_SET | §47 |
| Correlation constraints | OWNER_MUST_SET | §47 |
| Daily loss | OWNER_MUST_SET | §47 |
| Drawdown | OWNER_MUST_SET | §47 |
| Stale-data limits | OWNER_MUST_SET (V7 has data_guard thresholds, audit flagged fail-open) | §47 |
| Data-conflict rules | OWNER_MUST_SET | §47 |
| Strategy evidence requirement | OWNER_MUST_SET (no strategy without validated evidence, §61) | §47 |
| Kill-switch behavior | block new automated risk immediately; monitoring stays active (§74) | §47 |
| Live-autonomy permissions | LIVE_AUTO disabled by default; owner-enabled only (§45, §46) | §47 |

## Goal-hacking protection (§83)
Agents may NOT improve their score by: deleting losing tests; removing transaction costs; changing evaluation period after seeing results; excluding losing securities; reducing sample-size requirements; weakening risk limits; editing holdout; changing target metric after results; lowering promotion threshold.

## Protected files (§82)
Autonomous agents may NOT modify without explicit owner authorization: V9_RISK_CONSTITUTION, critical acceptance policy, constitutional test definitions, production secrets, live autonomy permissions, promotion policy, final holdout policy.

## AI cannot change the Risk Constitution (§48)
No autonomous agent may weaken any of the above. Owner action required.