"""
tests/unit/test_pre_earnings_force_ai.py
========================================
Tests for PreEarningsAgent.force_ai_for_symbol — the per-ticker "Refresh AI"
button backend. Forces a fresh LLM verdict on ONE ticker on demand, bypassing
the "already enriched" skip, reusing _apply_ollama on a 1-item deep-copied list
(LLM call outside the lock; atomic swap-back + persist).
"""

import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents import pre_earnings_agent as pe
from agents.pre_earnings_agent import PreEarningsAgent, direction_label_from_score


def _item(symbol="AAPL", score=10, gap=70, ai=None):
    it = {
        "symbol": symbol,
        "gap_risk": gap,
        "direction_bias_score": score,
        "direction_label": direction_label_from_score(score),
        "top_news": [{"title": "raises guidance", "source": "Reuters", "url": "x"}],
        "bias_reasons": ["structured reason"],
        "why": ["structured why"],
        "what_to_watch": ["gap direction"],
    }
    if ai is not None:
        it["ai_decision"] = ai
    return it


def _ollama_response(payload: dict):
    return {
        "response": json.dumps(payload),
        "model": "gpt-oss:120b",
        "done": True,
        "_meta": {"model": "gpt-oss:120b"},
    }


def _patch_cloud(monkeypatch):
    monkeypatch.setattr(pe.ollama, "is_cloud_host", lambda: True)
    monkeypatch.setattr(pe.ollama, "get_best_model", lambda: "gpt-oss:120b")


def _ok_payload(verdict="LONG", conf=0.8, sentiment=0.6):
    return {"sentiment": sentiment, "confidence": 0.7, "decision": verdict,
            "decision_confidence": conf, "decision_rationale": "Bullish guidance.",
            "catalyst_label": "GUIDANCE", "why_bullets": ["w"], "what_to_watch": ["ww"]}


@pytest.mark.unit
def test_force_ai_enriches_target_symbol_and_persists(monkeypatch):
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    agent._state = {"items": [_item("AAPL", score=10, gap=70),
                              _item("MSFT", score=-10, gap=60)], "status": {}}
    persist_calls = {"n": 0}
    monkeypatch.setattr(agent, "_persist_locked",
                        lambda: persist_calls.__setitem__("n", persist_calls["n"] + 1))

    with patch.object(pe.ollama, "generate", return_value=_ollama_response(_ok_payload("LONG"))):
        result = agent.force_ai_for_symbol("aapl")

    assert result["status"] == "ok"
    assert result["symbol"] == "AAPL"
    assert result["ai_decision"]["verdict"] == "LONG"
    assert result["ai_decision"]["used"] is True
    # The target item in state was swapped to the enriched copy.
    aapl = next(it for it in agent._state["items"] if it["symbol"] == "AAPL")
    assert aapl["ai_decision"]["used"] is True
    assert aapl["ai_decision"]["verdict"] == "LONG"
    assert persist_calls["n"] == 1, "must persist exactly once on success"


@pytest.mark.unit
def test_force_ai_unknown_symbol_returns_not_found(monkeypatch):
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    agent._state = {"items": [_item("AAPL")], "status": {}}
    persist_calls = {"n": 0}
    monkeypatch.setattr(agent, "_persist_locked",
                        lambda: persist_calls.__setitem__("n", persist_calls["n"] + 1))
    generate_calls = {"n": 0}
    monkeypatch.setattr(pe.ollama, "generate",
                        lambda *a, **kw: generate_calls.__setitem__("n", generate_calls["n"] + 1))

    result = agent.force_ai_for_symbol("NOPE")

    assert result["status"] == "not_found"
    assert generate_calls["n"] == 0, "must not call the LLM for an unknown symbol"
    assert persist_calls["n"] == 0, "must not persist when symbol not found"


@pytest.mark.unit
def test_force_ai_use_ollama_disabled_returns_ai_unavailable(monkeypatch):
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    agent._state = {"items": [_item("AAPL")], "status": {}}
    # _get_config() returns a fresh dict each call, so mutating a throwaway copy
    # has no effect on force_ai_for_symbol's internal fetch. Monkeypatch the
    # method so the agent actually sees use_ollama=False.
    monkeypatch.setattr(agent, "_get_config", lambda: {"use_ollama": False})
    generate_calls = {"n": 0}
    monkeypatch.setattr(pe.ollama, "generate",
                        lambda *a, **kw: generate_calls.__setitem__("n", generate_calls["n"] + 1))

    result = agent.force_ai_for_symbol("AAPL")

    assert result["status"] == "ai_unavailable"
    assert generate_calls["n"] == 0


@pytest.mark.unit
def test_force_ai_manual_refresh_uses_selected_local_model(monkeypatch):
    monkeypatch.setattr(pe.ollama, "is_cloud_host", lambda: False)
    monkeypatch.setattr(pe.ollama, "get_best_model", lambda: "local-owner-model")
    monkeypatch.setattr(pe.ollama, "get_timeout", lambda: 30)
    agent = PreEarningsAgent()
    agent._state = {"items": [_item("AAPL")], "status": {}}
    monkeypatch.setattr(agent, "_persist_locked", lambda: None)

    with patch.object(
        pe.ollama,
        "generate",
        return_value=_ollama_response(_ok_payload("LONG")),
    ) as generate:
        result = agent.force_ai_for_symbol("AAPL")

    assert result["status"] == "ok"
    assert result["ai_decision"]["model"] == "local-owner-model"
    assert generate.call_count == 1
    assert generate.call_args.kwargs["manual_request"] is True


@pytest.mark.unit
def test_force_ai_reruns_even_when_already_enriched(monkeypatch):
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    old = {"used": True, "verdict": "LONG", "confidence": 0.5,
           "sentiment": 0.3, "rationale": "old", "catalyst_label": "OTHER", "model": "old"}
    agent._state = {"items": [_item("AAPL", ai=old)], "status": {}}
    monkeypatch.setattr(agent, "_persist_locked", lambda: None)

    # Mock returns a DIFFERENT verdict (SHORT) — force must overwrite the old LONG.
    with patch.object(pe.ollama, "generate", return_value=_ollama_response(_ok_payload("SHORT", conf=0.9, sentiment=-0.6))):
        result = agent.force_ai_for_symbol("AAPL")

    assert result["status"] == "ok"
    assert result["ai_decision"]["verdict"] == "SHORT", "force must overwrite an existing verdict"
    aapl = next(it for it in agent._state["items"] if it["symbol"] == "AAPL")
    assert aapl["ai_decision"]["verdict"] == "SHORT"


@pytest.mark.unit
def test_force_ai_leaves_non_target_items_untouched(monkeypatch):
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    msft_ai = {"used": True, "verdict": "SHORT", "confidence": 0.7,
               "sentiment": -0.5, "rationale": "ms", "catalyst_label": "OTHER", "model": "old"}
    aapl_before = _item("AAPL", score=10, gap=70)
    msft_before = _item("MSFT", score=-10, gap=60, ai=msft_ai)
    agent._state = {"items": [aapl_before, msft_before], "status": {}}
    monkeypatch.setattr(agent, "_persist_locked", lambda: None)

    with patch.object(pe.ollama, "generate", return_value=_ollama_response(_ok_payload("LONG"))):
        agent.force_ai_for_symbol("AAPL")

    msft_after = next(it for it in agent._state["items"] if it["symbol"] == "MSFT")
    assert msft_after["ai_decision"] == msft_ai, "non-target item's ai_decision must be untouched"
    assert msft_after["direction_bias_score"] == -10, "non-target bias score must be untouched"


@pytest.mark.unit
def test_force_ai_llm_failure_returns_ai_failed_no_mutation(monkeypatch):
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    agent._state = {"items": [_item("AAPL", score=10, gap=70)], "status": {}}
    persist_calls = {"n": 0}
    monkeypatch.setattr(agent, "_persist_locked",
                        lambda: persist_calls.__setitem__("n", persist_calls["n"] + 1))

    def boom(*a, **kw):
        raise RuntimeError("simulated LLM outage")
    monkeypatch.setattr(pe.ollama, "generate", boom)

    result = agent.force_ai_for_symbol("AAPL")

    assert result["status"] == "ai_failed"
    assert persist_calls["n"] == 0, "must not persist on LLM failure"
    aapl = next(it for it in agent._state["items"] if it["symbol"] == "AAPL")
    assert "ai_decision" not in aapl or not aapl["ai_decision"].get("used"), \
        "must not write a used ai_decision on failure"
