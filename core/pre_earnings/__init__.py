"""Phase 5 — Pre-Earnings V9 decision system (§49/§50/§51).

§50 triple (score/coverage/confidence) in score.py; §51 decision context +
engine bridge in decision.py. Evolves the V7 pre_earnings_agent scoring surface
(evolve-not-rewrite, §5/§7); composes the Phase 4C decision engine + autonomy.
Stdlib-only, no I/O in score.py.
"""
from core.pre_earnings.decision import (
    build_pre_earnings_decision_context, evaluate_pre_earnings_decision,
)
from core.pre_earnings.score import (
    PreEarningsScoreResult, compute_data_confidence, compute_pre_earnings_score,
)

__all__ = [
    "PreEarningsScoreResult", "build_pre_earnings_decision_context",
    "compute_data_confidence", "compute_pre_earnings_score",
    "evaluate_pre_earnings_decision",
]