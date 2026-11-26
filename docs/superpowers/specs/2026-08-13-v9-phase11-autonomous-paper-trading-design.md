# Phase 11 — Autonomous Paper Trading design

## Existing surface retained

- `agents/execution_agent.py` already performs the retained data, AI, capital,
  sizing and risk gates before a paper entry.
- `core/capital_manager.py` already owns verified paper fills, commissions,
  margin accounting, TP/SL monitoring and position persistence.
- `core/portfolio/position.py` already produces deterministic HOLD/REDUCE/CLOSE
  assessments from the eight Section 67 evidence fields.
- `core/autonomy.py` already defines the owner-facing autonomy modes and the
  kill-switch flag, but neither is currently enforced by the V7 execution path.

## Decisions

1. Add an `ExecutionBroker` protocol and a mandatory `PaperBroker`; no live
   broker is introduced or inferred.
2. Persist order intents and every lifecycle transition in a dedicated SQLite
   execution ledger. The idempotent schema migration is part of GOLD-018.
3. Every automated intent carries `decision_id`, `order_intent_id` and a unique
   `idempotency_key`. A replay returns the original order and cannot open a
   second position.
4. The exact state machine is PROPOSED → VALIDATED → SUBMITTED → ACKNOWLEDGED →
   PARTIALLY_FILLED/FILLED, with CANCELLED/REJECTED/FAILED terminal paths.
5. `PaperBroker` composes `CapitalManager`; it does not duplicate pricing,
   commission, position or P/L logic. Automated open, reduce and close
   mutations cross the broker boundary.
6. New paper risk is fail-closed unless the owner selected PAPER_AUTO in the
   UI, the kill switch is off, a complete owner-confirmed Risk Constitution is
   active, a reduce percentage is selected, and reconciliation is healthy.
7. STOP AUTONOMOUS TRADING blocks new entries immediately. Monitoring and
   risk-reducing REDUCE/CLOSE actions remain active.
8. Reconciliation compares broker orders with local Capital positions. A
   material mismatch latches `EXECUTION_STATE_CONFLICT` and blocks new risk
   until a clean reconciliation succeeds.
9. REDUCE is a real partial paper fill with proportional margin, quantity,
   entry-fee allocation and realized P/L accounting. The percentage is chosen
   by the owner in UI; no trading value is invented in code.
10. The existing Agents control plane receives autonomy-mode, reduction and
    kill-switch controls. LIVE_ASSISTED/LIVE_AUTO remain outside Phase 11.

## Acceptance

- GOLD-009 kill switch blocks new risk while monitoring/close remains usable.
- GOLD-010 duplicate scheduler delivery creates one order and one position.
- GOLD-018 execution-ledger migration is repeatable and preserves records.
- GOLD-019 reconciliation conflict blocks new paper risk.
- Existing Capital accounting, Risk, News, API and V7 regression suites remain
  green; `docs/V9_RISK_CONSTITUTION.md` and V7 remain byte-untouched.
