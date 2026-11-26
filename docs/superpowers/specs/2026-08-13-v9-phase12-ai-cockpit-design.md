# Phase 12 - AI Trading Cockpit design

## Scope

Phase 12 adds one evidence-first command surface over the existing V9 services. It does not duplicate provider ingestion, make AI prose authoritative, activate trading, seed strategy performance, or implement later Research Factory/Decision Ledger phases prematurely.

## Read boundary

`GET /api/cockpit` composes bounded, cache-first snapshots from the existing dashboard, Capital, Pre-Earnings, News, Agents, execution, strategy-performance and audit services. Every section has one explicit asynchronous state: `LOADING`, `SUCCESS`, `EMPTY`, or `ERROR`. Missing options, SEC, ML, registry, research or broker evidence remains unavailable/not configured; the UI never manufactures a green state or a numeric value.

The payload is versioned with a stable content digest and supports unchanged polling. The frontend refreshes without provider fan-out and keeps deterministic evidence visibly separate from AI review.

## UI structure

- Command header: V9/Paper identity, market status, autonomy, AI, regime, capital, exposure, UTC daily P/L, risk, positions, system health and prominent Kill Switch.
- Overview: eligible opportunities, risks, positions, exact Pre-Earnings decision matrix, sentiment, news, decisions, degradation, research and health.
- Strategy Lab: lifecycle counts, unverified/validated leaderboard evidence, promotion/rollback history and optional AI Quant commentary.
- Decision Ledger: the eleven audit questions, with missing Phase 13 evidence explicitly stated.
- Research Factory: observed activity plus labelled Phase 14-16 capability states; future actions are not fake buttons.
- System Health: real backend/database/scheduler/provider/SEC/AI/ML/broker/research/MLflow/RD-Agent checks.

## Owner-controlled Risk Constitution profiles

The Risk editor offers three Paper Trading starting templates plus `Custom`. Trading values are stored in `risk_constitution_profiles.json`, not JavaScript. `MS_RISK_CONSTITUTION_PROFILES_PATH` can point to an operator-managed file; mtime/size-keyed caching provides hot reload. Every template is validated against the same complete Risk Constitution schema and invariants as activation.

Selecting a template only fills the browser form. It performs no request, save, confirmation or activation. Any field edit switches the selection to `Custom`; switching back to `Custom` restores the owner's unsaved custom values. Only the existing explicit `Confirm and apply` submission can replace the active policy.

## Safety and acceptance

- No live broker path is introduced.
- No profile is selected automatically.
- Invalid/missing profile configuration fails visibly with an empty list; there is no compiled fallback.
- The existing active policy is preserved while a draft/template is edited.
- Browser acceptance must cover all cockpit workspaces, refresh/routing, profile selection, custom restoration, incomplete-form feedback and zero console errors without clicking Kill Switch or applying a policy.
