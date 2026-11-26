"""§109 E2E journey — Pre-Earnings Paper Auto:
  candidate -> data validation -> Pre-Earnings analysis -> strategy evidence ->
  backtest evidence -> AI Analyst -> AI Critic -> Decision -> Risk -> Capital ->
  Paper order -> position -> Decision Ledger.
Exercised end-to-end with fake services (no network). Under ANALYZE no order
fires; under PAPER_AUTO an OPEN_LONG reaches the order-bearing stage."""
from core.ai.decision import Action
from core.ai.provider import AIResponse
from core.pre_earnings.decision import (
    build_pre_earnings_decision_context, evaluate_pre_earnings_decision,
)


class _FP:
    name = "fake"

    def __init__(self, text):
        self._text = text

    def generate(self, prompt, *, model=None, system=""):
        return AIResponse(text=self._text, model="m")


def _candidate():
    return {
        "symbol": "NVDA", "price": 850.0, "direction_bias": 45, "gap_risk": 72,
        "pre_earnings_score": 58, "pre_earnings_score_label": "MODERATE",
        "data_confidence": "HIGH", "coverage_score": 72, "coverage_label": "HIGH",
        "news_only_call": False, "expected_move_pct": 8.5,
        "historical_median_move_pct": 6.2, "session": "AMC",
        "earnings_time_utc": "2026-08-20T12:00:00+00:00",
        "risk_evaluation": {"accepted": True, "reason": "within limits"},
    }


def _services():
    return {
        "primary_provider": _FP('{"action":"OPEN_LONG","ticker":"NVDA","confidence":0.82,"thesis":"strong pre-earnings setup","strategy_id":"pre_earnings_v1"}'),
        "critic_provider": _FP('{"veto":false,"categories":[],"severity":0.1,"rationale":"ok"}'),
    }


_BASE = {"ai": {"decision": {"critic_enabled": True}}}


def test_e2e_journey_stages_all_present():
    candidate = _candidate()
    # data validation + Pre-Earnings analysis already encoded in the candidate item.
    ctx = build_pre_earnings_decision_context(candidate, services=_services())
    stages = ["pre_earnings", "strategy_evidence", "signal", "backtest", "ai",
              "critic", "data_quality", "portfolio", "risk", "capital"]
    for s in stages:
        assert s in ctx  # candidate -> data validation -> Pre-Earnings -> strategy/backtest/AI/critic/...
    # Decision -> Risk -> Capital -> Paper order under PAPER_AUTO
    cfg = {**_BASE, "autonomy": {"mode": "PAPER_AUTO"}}
    result = evaluate_pre_earnings_decision(candidate, config=cfg, services=_services())
    assert result.decision.action is Action.OPEN_LONG  # reaches the order-bearing stage
    # Decision Ledger entry shape (the agent attaches pre_earnings_decision in run_cycle;
    # here we mirror that summary as the ledger record).
    ledger = {
        "ticker": candidate["symbol"],
        "action": result.decision.action.value,
        "recommendation": result.review["recommendation"],
        "confidence": result.review["confidence"],
        "critic_veto": result.review["critic_veto"],
        "validation_stage": result.review["validation_stage"],
        "data_confidence": candidate["data_confidence"],
        "pre_earnings_score": candidate["pre_earnings_score"],
    }
    assert ledger["action"] == "OPEN_LONG" and ledger["critic_veto"] is False


def test_e2e_journey_no_order_under_analyze():
    cfg = {**_BASE, "autonomy": {"mode": "ANALYZE"}}
    result = evaluate_pre_earnings_decision(_candidate(), config=cfg, services=_services())
    assert result.decision.action is Action.WATCH  # no paper order fires below PAPER_AUTO
    assert "autonomy_mode_ANALYZE" in result.review["reason"]


def test_e2e_journey_critic_veto_blocks_order():
    s = _services()
    s["critic_provider"] = _FP('{"veto":true,"categories":["concentration"],"severity":0.9,"rationale":"concentrated"}')
    cfg = {**_BASE, "autonomy": {"mode": "PAPER_AUTO"}}
    result = evaluate_pre_earnings_decision(_candidate(), config=cfg, services=s)
    assert result.decision.action is Action.NO_TRADE  # critic veto -> no order