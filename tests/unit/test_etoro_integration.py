"""Tests for eToro client integration, data formatting, and pipeline/AI context flow."""
from unittest.mock import MagicMock, patch
import pytest

from core.etoro_client import EToroClient, get_etoro_client
from core.agent_manager import _attach_etoro_context_for_ai
from core.ollama_decision import _compact_signal, build_trade_review_prompt


def test_etoro_client_singleton():
    c1 = get_etoro_client()
    c2 = get_etoro_client()
    assert c1 is c2
    assert isinstance(c1, EToroClient)


def test_compact_signal_includes_etoro_context():
    signal = {
        "ticker": "AAPL",
        "action": "BUY",
        "entry": 150.0,
        "tp": 160.0,
        "sl": 145.0,
        "etoro_context": {
            "instrument_id": 1001,
            "holders": 10,
            "popularity_rank": 10,
            "buy_pct": 100.0,
            "sell_pct": 0.0,
            "pe_ratio": 37.25,
            "esg_score": 62.93,
            "bid": 324.0,
            "ask": 324.15,
            "spread": 0.15,
            "social_headlines": ["Test headline"],
        }
    }
    compact = _compact_signal(signal)
    assert "etoro_context" in compact
    assert compact["etoro_context"]["instrument_id"] == 1001
    assert compact["etoro_context"]["buy_pct"] == 100.0


def test_build_trade_review_prompt_includes_etoro_instructions_and_context():
    signal = {
        "ticker": "AAPL",
        "action": "BUY",
        "entry": 150.0,
        "etoro_context": {
            "holders": 10,
            "buy_pct": 95.0,
            "sell_pct": 5.0,
            "pe_ratio": 28.0,
            "social_headlines": ["Crowd is very bullish on Apple earnings"]
        }
    }
    prompt = build_trade_review_prompt(signal, {"accepted": True})
    assert "When etoro_context is present, weigh retail crowd positioning" in prompt
    assert "Crowd is very bullish on Apple earnings" in prompt
    assert '"buy_pct": 95.0' in prompt


def test_attach_etoro_context_skips_when_already_present():
    sig = {"ticker": "TSLA", "etoro_context": {"already": "present"}}
    res = _attach_etoro_context_for_ai(sig)
    assert res["etoro_context"] == {"already": "present"}
