# Phase 11 implementation plan

1. Add execution domain types, strict state transitions and SQLite migration.
2. Add `ExecutionBroker` plus Capital-backed `PaperBroker` idempotency.
3. Add partial-reduction accounting and deterministic position execution.
4. Add reconciliation and fail-closed paper-entry readiness.
5. Route ExecutionAgent entries and automated Capital exits through PaperBroker.
6. Add API controls for mode, reduce percentage, kill switch, orders and reconciliation.
7. Add prominent owner controls to the Agents UI without preset trading values.
8. Add unit, API, golden, migration and retained regression tests.
9. Run dedicated, curated and full gates; update controls and checkpoint.
