"""
tests/unit/test_pre_earnings_early_ai_pass.py
=============================================
Regression for the Pre-Earnings "AI takes 10+ min to appear" UX problem
(2026-08): the full run_cycle is slow (200 tickers x per-ticker yfinance
fetches), so `_apply_ollama` at the END of the cycle was unreachable for many
minutes and the user saw no `ai_decision`.

Contract for the early AI pass:
- Enriches the PERSISTED items (from the last successful cycle) at the START of
  run_cycle, before the slow rebuild, so `ai_decision` appears promptly.
- Deep-copies the persisted items, enriches the copies, then atomically swaps
  them back under the lock (no mutation of dicts a concurrent GET may read).
- Skips cleanly when items are already AI-enriched (no redundant work / cost).
- Skips when use_ollama is disabled.
- Never raises into run_cycle (non-fatal).
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


def _item(symbol="AAPL", score=10, gap=70):
    return {
        "symbol": symbol,
        "gap_risk": gap,
        "direction_bias_score": score,
        "direction_label": direction_label_from_score(score),
        "top_news": [{"title": "raises guidance", "source": "Reuters", "url": "x"}],
        "bias_reasons": ["structured reason"],
        "why": ["structured why"],
        "what_to_watch": ["gap direction"],
    }


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


@pytest.mark.unit
def test_early_pass_enriches_persisted_items_and_persists(monkeypatch):
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    persisted_items = [_item("AAPL", score=10, gap=70), _item("MSFT", score=-10, gap=60)]
    agent._state = {"items": copy.deepcopy(persisted_items), "status": {}}

    persist_calls = {"n": 0}
    def fake_persist():
        persist_calls["n"] += 1
    monkeypatch.setattr(agent, "_persist_locked", fake_persist)

    payload = {"sentiment": 0.6, "confidence": 0.7, "decision": "LONG",
               "decision_confidence": 0.8, "decision_rationale": "Bullish.",
               "catalyst_label": "GUIDANCE", "why_bullets": ["w"], "what_to_watch": ["ww"]}
    with patch.object(pe.ollama, "generate", return_value=_ollama_response(payload)):
        agent._early_ai_pass(agent._get_config())

    # Persisted (swapped into state) items now carry an ai_decision block.
    state_items = agent._state.get("items", [])
    ai_items = [it for it in state_items if isinstance(it.get("ai_decision"), dict) and it["ai_decision"].get("used")]
    assert len(ai_items) >= 1, "early pass must enrich at least the top persisted item"
    assert ai_items[0]["ai_decision"]["verdict"] == "LONG"
    assert persist_calls["n"] >= 1, "early pass must persist the enriched items"


@pytest.mark.unit
def test_early_pass_skips_when_already_enriched(monkeypatch):
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    it = _item("AAPL", score=10, gap=70)
    it["ai_decision"] = {"used": True, "verdict": "LONG", "confidence": 0.8}
    agent._state = {"items": [it], "status": {}}

    generate_calls = {"n": 0}
    def fake_gen(*a, **kw):
        generate_calls["n"] += 1
        return _ollama_response({"decision": "SHORT", "sentiment": -0.5, "confidence": 0.5,
                                  "decision_confidence": 0.5, "decision_rationale": "x"})
    monkeypatch.setattr(pe.ollama, "generate", fake_gen)
    persist_calls = {"n": 0}
    monkeypatch.setattr(agent, "_persist_locked", lambda: persist_calls.__setitem__("n", persist_calls["n"] + 1))

    agent._early_ai_pass(agent._get_config())
    assert generate_calls["n"] == 0, "must not call the LLM when items are already enriched"
    assert persist_calls["n"] == 0, "must not persist when nothing changed"


@pytest.mark.unit
def test_early_pass_skips_when_use_ollama_disabled(monkeypatch):
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    agent._state = {"items": [_item("AAPL")], "status": {}}
    cfg = agent._get_config()
    cfg["use_ollama"] = False

    generate_calls = {"n": 0}
    monkeypatch.setattr(pe.ollama, "generate", lambda *a, **kw: generate_calls.__setitem__("n", generate_calls["n"] + 1))
    agent._early_ai_pass(cfg)
    assert generate_calls["n"] == 0


@pytest.mark.unit
def test_early_pass_non_fatal_on_error(monkeypatch):
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    agent._state = {"items": [_item("AAPL")], "status": {}}

    def boom(*a, **kw):
        raise RuntimeError("simulated LLM outage")
    monkeypatch.setattr(pe.ollama, "generate", boom)
    # Must not raise — the early pass is non-fatal so run_cycle continues.
    agent._early_ai_pass(agent._get_config())