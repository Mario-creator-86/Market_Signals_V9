"""Phase 13 unit tests — recorder: decision_context → §97 ledger entry."""
import json

import pytest

from core.ai.provider import AIResponse
from core.decision_ledger.recorder import build_ledger_entry, record_decision
from core.decision_ledger.store import DecisionLedgerStore


class StubProvider:
    name = "stub"

    def __init__(self, text=None, model="stub-mini"):
        self.text = text or json.dumps({
            "recommendation": "approve", "confidence": 0.72, "size_factor": 0.5,
            "leverage": 1, "sentiment": "neutral", "supports_direction": True,
            "reason": "aligned momentum", "risks": [],
        })
        self.model = model

    def generate(self, prompt, **kwargs):
        return AIResponse(text=self.text, model=self.model)


def _context(decision_id="dec_rec_1", status="accepted", risk_accepted=True):
    return {
        "decision_id": decision_id,
        "timestamp": "2026-08-16T14:30:00+00:00",
        "ticker": "AAPL",
        "decision_status": status,
        "strategy_type": "MOMENTUM",
        "risk_evaluation": {"accepted": risk_accepted, "reason": "" if risk_accepted else "veto"},
        "sizing": {"usd_size": 750.0, "size_factor": 0.5},
        "risk": {"entry": 100.0, "take_profit": 110.0, "stop_loss": 95.0},
    }


def _signal():
    return {
        "ticker": "AAPL", "id": "sig_9", "entry": 100.0, "tp": 110.0, "sl": 95.0,
        "signal_score": 0.82, "horizon": "SWING", "strategy_type": "MOMENTUM",
        "risk_flags": ["gap_risk"], "strategy_evidence": {"confluence": 2},
    }


def test_entry_answers_eleven_questions():
    from core.ai.decision_engine import evaluate_trade_decision
    from core.pre_earnings.decision import _pe_default_services

    signal = _signal()
    result = evaluate_trade_decision(
        signal, None, config={},
        services={"primary_provider": StubProvider(), "primary_model": "stub-mini",
                  **_pe_default_services()},
    )
    assert result.prompt and result.prompt_version == "trade_review_v1" and result.model == "stub-mini"

    entry = build_ledger_entry(signal, _context(), eval_result=result, review=result.review)
    questions = entry.eleven_questions()
    expected = {
        "what_happened", "data_existed", "strategy", "ai_model", "prompt", "ai_proposal",
        "validation", "critic", "risk", "capital_allowed", "executed", "afterward",
    }
    assert expected <= set(questions.keys())
    assert questions["ai_model"]["model"] == "stub-mini"
    assert questions["ai_model"]["prompt_version"] == "trade_review_v1"
    assert questions["ai_model"]["prompt_sha256"]
    assert questions["strategy"] == "MOMENTUM"
    assert questions["data_existed"]["signal_id"] == "sig_9"
    assert questions["validation"]["ok"] is True
    assert entry.source == "SIGNAL_PATH"


def test_rejected_decision_is_recorded_with_no_trade_action():
    entry = build_ledger_entry(_signal(), _context(status="rejected", risk_accepted=False), review={})
    assert entry.status == "rejected"
    assert entry.action == "NO_TRADE"
    assert entry.risk["accepted"] is False


def test_pre_earnings_source_detected():
    signal = {**_signal(), "autonomous_source": "pre_earnings"}
    entry = build_ledger_entry(signal, _context())
    assert entry.source == "PRE_EARNINGS_PATH"


def test_record_decision_redelivery_returns_original(tmp_path):
    store = DecisionLedgerStore(tmp_path / "ledger.db")
    first, created = record_decision(store, _signal(), _context("dec_redeliver"))
    second, created_again = record_decision(store, _signal(), _context("dec_redeliver"))
    assert created and not created_again
    assert second.decision_id == first.decision_id


def test_missing_decision_id_raises(tmp_path):
    store = DecisionLedgerStore(tmp_path / "ledger.db")
    with pytest.raises(ValueError):
        record_decision(store, _signal(), {"decision_id": "", "ticker": "AAPL"})


def test_prompt_text_is_capped():
    from core.decision_ledger.types import PROMPT_TEXT_CAP, bounded_text

    long_prompt = "x" * (PROMPT_TEXT_CAP + 500)
    assert len(bounded_text(long_prompt)) < len(long_prompt)
    assert bounded_text(long_prompt).endswith("chars]")


def test_unavailable_ai_review_still_records():
    entry = build_ledger_entry(
        _signal(), _context(),
        review={"available": False, "fallback_used": "all_unavailable_blocked", "provider": "ollama"},
    )
    assert entry.ai["available"] is False
    assert entry.ai["response_fallback_used"] == "all_unavailable_blocked"
    assert entry.action in ("NO_TRADE", "WATCH", "HOLD")


def test_critic_is_recorded_as_skipped_when_ai_validation_already_vetoed():
    from core.ai.decision import Action, Decision
    from core.ai.decision_engine import EvaluationResult
    from core.ai.pipeline import POLICY, ValidationResult

    proposal = Decision(action=Action.OPEN_LONG, ticker="BNTX", confidence=0.5)
    result = EvaluationResult(
        decision=proposal,
        validation=ValidationResult(
            ok=False,
            decision=proposal,
            stage=POLICY,
            reason="POLICY: risk_constitution_veto",
        ),
        critic=None,
        review={"available": True, "recommendation": "avoid"},
        ai_available=True,
    )

    entry = build_ledger_entry(_signal(), _context(status="rejected"), eval_result=result, review=result.review)

    assert entry.critic == {
        "available": False,
        "skipped": True,
        "reason": "skipped_after_ai_validation_veto",
        "upstream_stage": "POLICY",
        "upstream_reason": "POLICY: risk_constitution_veto",
    }


def test_risk_rejected_decision_records_not_conslected_explicitly():
    """§90/§97 — a signal rejected BEFORE the AI review must record that the
    AI was deliberately not consulted, not an empty AI-failure shape."""
    entry = build_ledger_entry(_signal(), _context(status="rejected", risk_accepted=False), review=None)
    assert entry.ai == {
        "consulted": False,
        "reason": "ai_review_not_consulted_risk_rejected",
        "available": False,
    }
    questions = entry.eleven_questions()
    assert questions["ai_model"]["consulted"] is False
    assert "not consulted" in questions["prompt"]["note"]


def test_eleven_questions_prompt_is_summary_not_full_text():
    from core.ai.decision_engine import evaluate_trade_decision
    from core.pre_earnings.decision import _pe_default_services

    result = evaluate_trade_decision(
        _signal(), None, config={},
        services={"primary_provider": StubProvider(), "primary_model": "stub-mini",
                  **_pe_default_services()},
    )
    entry = build_ledger_entry(_signal(), _context(), eval_result=result, review=result.review)
    questions = entry.eleven_questions()
    # The row summary carries version/digest — never the full prompt blob.
    assert questions["prompt"]["version"] == "trade_review_v1"
    assert len(questions["prompt"]["sha256"]) == 64
    assert "prompt_text" not in questions["ai_model"]
    assert questions["ai_model"]["consulted"] is True
