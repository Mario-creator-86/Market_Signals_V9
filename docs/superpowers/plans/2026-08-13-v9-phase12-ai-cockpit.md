# Phase 12 - AI Trading Cockpit implementation plan

1. Audit sections 91-100 and map each UI datum to an existing authoritative source.
2. Build a bounded, cache-first `/api/cockpit` read model with explicit async and health states.
3. Add the Cockpit command header and Overview, Strategy Lab, Decision Ledger, Research Factory and System Health workspaces.
4. Add schema and source contracts for the payload and frontend state/routing behavior.
5. Add externally configured Risk Constitution templates and a non-persistent Custom workflow.
6. Verify Python/JavaScript/JSON syntax, targeted contracts, curated regression and full suite.
7. Exercise the real local UI without changing Kill Switch or owner trading settings.
8. Update V9 control documents and create a source-only Git checkpoint.

All implementation steps are additive. Runtime data, owner configuration and the protected `docs/V9_RISK_CONSTITUTION.md` are excluded from the checkpoint.
