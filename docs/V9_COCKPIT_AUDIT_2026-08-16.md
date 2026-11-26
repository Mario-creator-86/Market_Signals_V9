# AI Cockpit — Full Audit & Remediation Report (2026-08-16)

Owner-requested complete audit of the AI Trading Cockpit (spec §91-§100).
Method: full code review (`api/cockpit_service.py`, `/api/cockpit` route,
`frontend/cockpit.js`, cockpit markup) + live payload/browser verification on
the running port-8003 deployment. 21 findings (1 critical, 3 high, 8 medium,
9 low). Remediations implemented the same day, each with a regression test
where automatable.

## CRITICAL

### F1 — Health tiles read a process-local registry (market provider, AI providers)
**Where:** `api/cockpit_service.py` `_system_health` (market tile ~831, AI tile ~874).
**Problem:** `MS_ENGINE_MODE=subprocess` (default, `run_server.py`) — the
engine subprocess performs market/AI calls, so their quality streams
(`yf_prices`, `ollama_availability`) never register in the API process. The
tiles then showed UNAVAILABLE/UNVERIFIED as permanent falsehoods and tipped
the aggregate to DEGRADED — exactly what §98 forbids.
**Fix:** cross-process evidence, honestly labeled:
- Market tile derives evidence from the SHARED Pre-Earnings snapshot freshness
  (calendar/news ages ≤ 15 min → `DERIVED_VERIFIED` HEALTHY; snapshot ERROR →
  DEGRADED). Primary stream still wins when present.
- AI tile runs a REAL short-timeout (3s) availability probe of the primary
  LOCAL provider (`GET /api/tags`), TTL-cached 60s; cloud primaries stay
  honestly UNVERIFIED (Test in AI Settings is the real check).
**Test:** `test_audit_F1_market_provider_derives_from_shared_freshness`.

## HIGH

### F2 — Kill-switch confirm could flip the wrong way after a mid-arm poll
**Where:** `frontend/cockpit.js` `toggleKillSwitch` (~518).
**Problem:** the confirm click computed `!currentlyActive` from a snapshot up
to 30s stale; a poll landing inside the 5s arm window could re-render the
button to "Confirm release", and a muscle-memory double-click would POST
`active:false` — RELEASING an auto-tripped kill switch.
**Fix:** (1) the confirm click first GETs `/api/execution/control` and derives
the target from LIVE state (abort + disarm on failure); (2) any fresh server
payload disarms a pending confirmation; (3) kill-switch calls use a private
AbortController (see F18) so a poll can never abort an owner action.
**Tests:** updated frontend contract pins the live-state pre-read + explicit
`active: targetActive`.

### F3 — "Rollback history" structurally dead (§95)
**Where:** `api/cockpit_service.py` `_strategy_lab` (~682); `frontend/cockpit.js` renderStrategy.
**Problem:** rollback detection scanned registry events for the substring
"ROLLBACK" — an action the registry never emits; the frontend never rendered
the field anyway.
**Fix:** rollback history is sourced from the Phase 16 promotion records
(`PromotionRecordStore`, status ROLLED_BACK, with restored-version + reason)
plus registry DEMOTE events; the Strategy Lab now renders separate
"Promotion history" and "Rollback history" cards.
**Test:** `test_audit_F3_rollback_history_from_promotion_records`.

### F4 — Phase-12 Decision Ledger fallback could not answer 8 of 11 §97 questions
**Where:** `api/cockpit_service.py` `_ai_decisions_section`.
**Problem:** the fallback consumed the compacted capital decisions list
(`_compact_trade_audit` keeps header fields only — no `payload`), so on fresh
installs every evidence question rendered "Not captured" although
`core.trade_audit` holds the evidence.
**Fix:** the fallback now reads the FULL audit records (`get_trade_audit`)
when the persistent ledger is empty.

## MEDIUM

### F5 — `unchanged` polling optimization was effectively dead
**Where:** `/api/cockpit` route versioning.
**Problem:** every rebuild regenerated `checked_at` stamps, `now_et` and
`age_seconds`, so the content version changed on every build → full payload
re-transfer every 30s poll.
**Fix:** `_cockpit_stable_version` (api/main.py) — drops `generated_at`,
`now_et`, `checked_at` and bucketizes `*_age_seconds` to whole minutes;
endpoint cache TTL 5s→15s. Identical-content rebuilds now return `unchanged`;
real content changes (incl. minute-bucket rollovers) still bump the version.
**Test:** `test_audit_F5_cockpit_version_stable_across_volatile_rebuilds`.

### F6 — Heavy synchronous build + duplicate store reads
**Where:** `get_cockpit_payload`.
**Problem:** Research Factory stores constructed twice per build; ledger store
queried by three builders; full capital/pre-earnings/de/dashboard reads inline.
**Fix (partial):** 10s TTL evidence cache for ledger + research-factory
summaries; endpoint TTL raise (F5) cuts rebuild frequency ~3x. Remaining
heavy sources (capital state, pre-earnings deepcopy) stay inline — documented
as accepted for this deployment scale.

### F7 — Whole-endpoint 500 on one non-JSON passthrough
**Where:** `/api/cockpit` route serialization.
**Fix:** `json.dumps(..., default=str)` (NaN/Infinity still rejected loudly);
per-section error isolation unchanged.

### F8 — Strategy Lab: OOS/robustness/last-validated/drift computed but never rendered; drift was a circular stub
**Where:** `_strategy_lab` + renderStrategy.
**Fix:** new "Lifecycle · OOS · robustness · drift" table renders the registry
records with `oos_verdict`, `robustness`, `last_validated`, `drift`; drift is
now MEASURED by the Phase 16 drift monitor (§79: REVALIDATION semantics, risk
untouched), not re-derived from registry status.
**Note:** `oos` renamed `oos_verdict` (clearer contract).

### F11 — Overview health-preview fabricated SUCCESS
**Where:** renderOverview.
**Fix:** preview state derives from the REAL `system_health.overall`
(UNHEALTHY→ERROR, missing→EMPTY).

### F12 — "0 readiness blockers" on execution-source failure
**Where:** header autonomy + KPI.
**Fix:** header carries `autonomy.evaluated`; on failure the KPI says
"Readiness unknown (execution source error)" — never a fake pass.
**Test:** `test_audit_F12_autonomy_marks_unevaluated_on_execution_failure`.

## LOW (fixed)

- **F13** falsy-zero scores (`score=0` treated as missing) — `or`-chains replaced
  with explicit presence checks (`test_audit_F13_score_zero_is_not_treated_as_missing`).
- **F14** dead `WARN`/`MUTED` tone tokens — replaced with real `warn`/`muted`.
- **F15** missing P/L rendered "good" — now `muted` (unknown).
- **F16** "1 positions" — pluralized.
- **F17** open ledger `<details>` collapsed by the 30s re-render — open
  disclosures are preserved across renders (stable `data-ledger-id`).
- **F18** shared AbortController — owner actions (kill-switch GET/POST) use
  private controllers (`mutable: true`); polls keep the shared one.
- **F21** lexical "latest run" join — parsed-timestamp comparison (`_EPOCH` floor).

## LOW (accepted / documented, not changed)

- **F9** ML-model/MLflow/RD-Agent/live-broker tiles are configuration-state
  checks, not runtime probes — messages already say so; real probes are
  Phase 14/16 runtime items.
- **F10** §92 "Sentiment shifts" has no historical snapshot to diff against —
  card shows current tone + honest UNAVAILABLE shift pill.
- **F19** calendar-reporter denominator falls back to the analyzed count when
  both reporter fields are absent — live payload shows the real reporter count.
- **F20** Daily P/L buckets by UTC while the market card is ET — the basis is
  labeled in the KPI detail; changing the bucket is an owner product decision.

## Verified non-defects

XSS escaping consistent (`esc()` everywhere incl. new renderers); all route
buttons target valid views; single delegated event listener (no rebinding);
§91 header complete; §92 cards complete; §93 matrix complete with drilldown;
§97 eleven questions match the ledger schema; the cockpit's only mutations
remain the kill-switch POST and the Pre-Earnings refresh (no trading values,
no autonomy enable).

## Gate

Full `tests tests_e2e` after remediation: see `docs/V9_CHANGELOG.md` entry
(2026-08-16 cockpit audit). All cockpit suites green including the five new
regression tests (F1/F3/F5/F12/F13).
