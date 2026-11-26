"""Unit tests for core.ai.decision — §41 Action enum + Decision schema (Phase 4B)."""
import pytest

from core.ai.decision import (
    ALLOWED_ACTIONS,
    AUTHORITATIVE_FIELDS,
    Action,
    Decision,
)


def test_action_enum_has_exactly_the_seven_spec_actions():
    # §41 allowed list, verbatim
    assert {a.value for a in Action} == {
        "WATCH", "NO_TRADE", "OPEN_LONG", "OPEN_SHORT",
        "HOLD", "REDUCE", "CLOSE",
    }


def test_action_values_match_allow_set():
    assert ALLOWED_ACTIONS == {a.value for a in Action}


def test_authoritative_fields_cover_spec_43_list():
    # §43: price, P/L, IV, EPS, portfolio value, allowed capital, position size
    assert AUTHORITATIVE_FIELDS == frozenset({
        "price", "pnl", "iv", "eps",
        "portfolio_value", "allowed_capital", "position_size",
    })


def test_decision_defaults_and_frozen():
    d = Decision(action=Action.WATCH)
    assert d.action == Action.WATCH
    assert d.ticker == ""
    assert d.confidence == 0.0
    assert d.supporting_evidence == []
    assert d.requires_more_data is False
    assert d.rejected_fields == []
    # frozen
    with pytest.raises(Exception):
        d.ticker = "NVDA"
    # default factories are per-instance (not shared)
    a = Decision(action=Action.WATCH)
    b = Decision(action=Action.WATCH)
    a.supporting_evidence.append("x")
    assert b.supporting_evidence == []


def test_decision_field_names_match_spec_41_schema():
    # §41 example schema fields, verbatim
    expected = {
        "action", "ticker", "horizon", "confidence", "strategy_id",
        "thesis", "supporting_evidence", "opposing_evidence", "risk_flags",
        "data_quality", "requires_more_data",
    }
    assert expected.issubset({f.name for f in __import__("dataclasses").fields(Decision)})


from core.ai.decision import parse_decision, DecisionSchemaError


def test_parse_decision_valid_json_coerces_fields():
    raw = """
    {
      "action": "OPEN_LONG",
      "ticker": "NVDA",
      "horizon": "SWING",
      "confidence": 1.7,
      "strategy_id": "momentum_v3",
      "thesis": "Momentum continuation.",
      "supporting_evidence": ["rsi>70", "volume up"],
      "opposing_evidence": ["overbought"],
      "risk_flags": ["thin liquidity"],
      "data_quality": 1.5,
      "requires_more_data": false
    }
    """
    d = parse_decision(raw)
    assert d.action == Action.OPEN_LONG
    assert d.ticker == "NVDA"
    assert d.horizon == "SWING"
    assert d.confidence == 1.0          # clamped to [0,1]
    assert d.data_quality == 1.0         # clamped to [0,1]
    assert d.requires_more_data is False
    assert d.supporting_evidence == ["rsi>70", "volume up"]


def test_parse_decision_accepts_dict_passthrough():
    d = parse_decision({"action": "WATCH", "ticker": "AAPL"})
    assert d.action == Action.WATCH
    assert d.ticker == "AAPL"


def test_parse_decision_invalid_action_falls_back_to_no_trade():
    d = parse_decision({"action": "BUY", "ticker": "AAPL"})
    assert d.action == Action.NO_TRADE
    assert d.ticker == "AAPL"


def test_parse_decision_invalid_json_falls_back_to_no_trade():
    d = parse_decision("not json at all")
    assert d.action == Action.NO_TRADE
    assert d.raw.get("parse_error") == "invalid_json"


def test_parse_decision_strips_authoritative_fields_and_records_them():
    raw = {
        "action": "OPEN_LONG",
        "ticker": "NVDA",
        "confidence": 0.8,
        "price": 850.0,            # §43 — not authoritative from AI
        "position_size": 12000,    # §43
        "allowed_capital": 50000,  # §43
        "portfolio_value": 100000,# §43
    }
    d = parse_decision(raw)
    # §41 schema has no such fields
    assert not hasattr(d, "price")
    assert "price" in d.rejected_fields
    assert "position_size" in d.rejected_fields
    assert "allowed_capital" in d.rejected_fields
    assert "portfolio_value" in d.rejected_fields
    # kept fields survive
    assert d.ticker == "NVDA"
    assert d.confidence == 0.8


def test_parse_decision_strict_schema_rejects_authoritative_fields():
    raw = {"action": "OPEN_LONG", "ticker": "NVDA", "price": 850.0}
    with pytest.raises(DecisionSchemaError) as exc:
        parse_decision(raw, strict_schema=True)
    assert "price" in str(exc.value)


def test_parse_decision_brace_matches_embedded_json():
    # prose around the JSON, like the legacy ollama response shape
    raw = 'Here is my decision: {"action":"HOLD","ticker":"TSLA","confidence":0.5} ok?'
    d = parse_decision(raw)
    assert d.action == Action.HOLD
    assert d.ticker == "TSLA"
    assert d.confidence == 0.5


def test_parse_decision_truncates_long_thesis_and_lists():
    raw = {"action": "NO_TRADE", "thesis": "x" * 2000,
           "risk_flags": ["a", "b", "c", "d", "e", "f", "g", "h"]}
    d = parse_decision(raw)
    assert len(d.thesis) <= 1000
    assert len(d.risk_flags) <= 8