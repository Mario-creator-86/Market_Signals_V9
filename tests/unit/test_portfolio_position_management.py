from datetime import datetime, timezone

from core.portfolio import PositionAction
from core.portfolio.position import (
    normalize_position_evidence,
    reassess_position,
    reassess_positions,
)


NOW = "2026-08-13T11:00:00+00:00"


def _position(**changes):
    values = {
        "position_id": "pos-1",
        "ticker": "AAPL",
        "direction": "LONG",
        "entry_price": 100.0,
        "stop_loss_price": 95.0,
        "take_profit_price": 110.0,
        "strategy_type": "S2_BREAKOUT_RETEST",
        "signal_id": "sig-1",
        "source_signal_id": "sig-1",
        "status": "OPEN",
    }
    values.update(changes)
    return values


def _context():
    return {
        "reason": "breakout held above resistance",
        "reasons": ["volume confirmed", "retest held"],
        "strategy_type": "S2_BREAKOUT_RETEST",
        "ai_decision": {
            "thesis": "confirmed breakout continuation",
            "supporting_evidence": ["volume", "market structure"],
            "opposing_evidence": ["earnings in ten days"],
        },
        "ai_critic": {"categories": ["catalyst_risk"]},
        "execution": {"analysis_provider": "openai/gpt-test"},
        "risk_evaluation": {"accepted": True, "reason": "passed"},
    }


def test_normalizer_persists_every_required_position_evidence_field():
    evidence = normalize_position_evidence(_position(), decision_context=_context())

    assert set(evidence) == {
        "entry_thesis", "supporting_evidence", "opposing_evidence", "strategy",
        "signal", "ai_model", "invalidation_rules", "risk_state",
    }
    assert evidence["entry_thesis"] == "confirmed breakout continuation"
    assert evidence["supporting_evidence"] == ["volume", "market structure", "volume confirmed", "retest held"]
    assert "earnings in ten days" in evidence["opposing_evidence"]
    assert evidence["strategy"] == "S2_BREAKOUT_RETEST"
    assert evidence["signal"]["signal_id"] == "sig-1"
    assert evidence["ai_model"] == "openai/gpt-test"
    assert evidence["invalidation_rules"] == ["close at or below 95.00000000"]
    assert evidence["risk_state"]["status"] == "NORMAL"


def test_manual_or_recovered_position_uses_honest_system_provenance():
    evidence = normalize_position_evidence(
        _position(strategy_type="MANUAL", signal_id="", source_signal_id=""),
        decision_context={"reason": "manual owner entry", "risk": {}},
    )

    assert evidence["entry_thesis"] == "manual owner entry"
    assert evidence["ai_model"] is None
    assert evidence["supporting_evidence"] == []
    assert evidence["opposing_evidence"] == []
    assert evidence["signal"]["signal_id"] == ""


def test_manual_execution_provider_is_not_recorded_as_an_ai_model():
    evidence = normalize_position_evidence(
        _position(strategy_type="MANUAL"),
        decision_context={"reason": "manual owner entry", "execution": {"analysis_provider": "manual"}},
    )

    assert evidence["ai_model"] is None


def test_invalidation_or_critical_risk_closes_position():
    invalidated = reassess_position(
        _position(), {"invalidation_triggered": True}, assessed_at=NOW
    )
    critical = reassess_position(
        _position(), {"risk_status": "CRITICAL"}, assessed_at=NOW
    )

    assert invalidated.action is PositionAction.CLOSE
    assert "invalidation_triggered" in invalidated.triggered_rules
    assert critical.action is PositionAction.CLOSE
    assert "critical_risk_state" in critical.triggered_rules


def test_app_authoritative_stop_crossing_closes_long_or_short():
    long_result = reassess_position(
        _position(), {"current_price": 94.9}, assessed_at=NOW
    )
    short_result = reassess_position(
        _position(direction="SHORT", stop_loss_price=105.0),
        {"current_price": 105.1},
        assessed_at=NOW,
    )

    assert long_result.action is PositionAction.CLOSE
    assert short_result.action is PositionAction.CLOSE
    assert "stop_loss_reached" in long_result.triggered_rules


def test_degraded_or_noncritical_constraint_breach_reduces_position():
    degraded = reassess_position(
        _position(), {"risk_status": "DEGRADED"}, assessed_at=NOW
    )
    exposure = reassess_position(
        _position(), {"reduce_required": True, "breached_constraints": ["sector"]}, assessed_at=NOW
    )

    assert degraded.action is PositionAction.REDUCE
    assert exposure.action is PositionAction.REDUCE
    assert "sector" in exposure.triggered_rules


def test_kill_switch_keeps_monitoring_active_without_forced_liquidation():
    result = reassess_position(
        _position(), {"kill_switch_active": True, "current_price": 100.0}, assessed_at=NOW
    )

    assert result.action is PositionAction.HOLD
    assert "kill_switch_monitoring_active" in result.reasons


def test_batch_reassessment_skips_closed_positions_and_keys_by_position_id():
    results = reassess_positions(
        [_position(), _position(position_id="closed", status="CLOSED")],
        {"pos-1": {"current_price": 100.0}},
        assessed_at=NOW,
    )

    assert set(results) == {"pos-1"}
    assert results["pos-1"].action is PositionAction.HOLD
