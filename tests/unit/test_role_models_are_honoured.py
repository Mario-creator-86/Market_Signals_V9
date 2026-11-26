"""Every AI call uses the model the owner assigned to its role, not the global default.

Seen live on 2026-11-23: the owner had set every role to Claude Opus, yet the
Pre-Earnings "LLM view" still said "model deepseek-v4-pro:cloud". The
Pre-Earnings and Pre-Market agents, the Watchlist quick AI and the on-demand
news analysis took the global model and never looked at the roles.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.ai import role_resolution
from core.ai.role_resolution import assigned_model, call_timeout_for

ROOT = Path(__file__).resolve().parents[2]
CONFIG = {"ai": {"assignment": {"all": "deepseek-v4-pro:cloud", "pre_earnings_analyst": "claude-opus-5-5",
                                "market_analyst": "claude-opus-5-5", "news_analyst": None}}}


class _Manager:
    def __init__(self, config):
        self.config = config


def test_a_role_with_a_model_gets_it_and_a_role_without_one_gets_the_default():
    assert assigned_model("pre_earnings_analyst", config=CONFIG, fallback="deepseek-v4-pro:cloud") == "claude-opus-5-5"
    assert assigned_model("market_analyst", config=_Manager(CONFIG), fallback="x") == "claude-opus-5-5"
    # A role without its own model leaves the choice to the caller's fallback: the global model as the
    # client resolves it (installed models, the owner's global selector), not the raw "all" entry.
    assert assigned_model("news_analyst", config=CONFIG, fallback="the-global-model") == "the-global-model"
    tiered = {"ai": {"assignment": {"news_analyst": None}, "router": {"analysis": {"model": "tier-model"}}}}
    assert assigned_model("news_analyst", config=tiered, fallback="x") == "tier-model"
    assert assigned_model("pre_earnings_analyst", config={}, fallback="global-model") == "global-model"
    assert assigned_model("pre_earnings_analyst", config="not a config", fallback="global-model") == "global-model"


def test_without_a_config_object_the_config_file_is_read(tmp_path, monkeypatch):
    import json

    import core.config as core_config

    path = tmp_path / "config.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    monkeypatch.setattr(core_config, "CONFIG_PATH", path)
    monkeypatch.setitem(role_resolution._FILE_CACHE, "mtime_ns", None)
    assert assigned_model("market_analyst", fallback="x") == "claude-opus-5-5"
    path.write_text("{ broken", encoding="utf-8")
    monkeypatch.setitem(role_resolution._FILE_CACHE, "mtime_ns", None)
    assert assigned_model("market_analyst", fallback="x") == "x"         # unreadable config: the global model


def test_a_claude_model_gets_time_to_answer_and_an_ollama_model_keeps_its_short_budget(monkeypatch):
    import core.ai.subscription_routing as routing

    monkeypatch.setattr(routing, "subscription_provider_name_for",
                        lambda model: "claude_subscription" if str(model).startswith("claude-") else "")
    assert call_timeout_for("claude-opus-5-5", 6) == 60               # 6 s would cut every answer off
    assert call_timeout_for("claude-opus-5-5", 120) == 120
    assert call_timeout_for("deepseek-v4-pro:cloud", 6) == 6
    assert call_timeout_for("claude-opus-5-5", "junk") == 60


@pytest.mark.parametrize("module,cls,role", [
    ("agents.pre_earnings_agent", "PreEarningsAgent", "pre_earnings_analyst"),
    ("agents.premarket_scanner_agent", "PremarketScannerAgent", "market_analyst"),
])
def test_the_agents_ask_for_their_roles_model(monkeypatch, module, cls, role):
    import importlib

    mod = importlib.import_module(module)
    agent = getattr(mod, cls).__new__(getattr(mod, cls))
    agent.config_manager = _Manager(CONFIG)
    monkeypatch.setattr(mod.ollama, "get_best_model", lambda: "deepseek-v4-pro:cloud")
    assert agent._analyst_model() == "claude-opus-5-5"

    agent.config_manager = _Manager({"ai": {"assignment": {"all": None, role: None}}})
    assert agent._analyst_model() == "deepseek-v4-pro:cloud"            # no role model: the global one, as before


def test_no_ai_call_site_takes_the_global_model_behind_the_roles_back():
    """The global model may only appear as the fallback of a role lookup."""
    for relative in ("agents/pre_earnings_agent.py", "agents/premarket_scanner_agent.py", "core/watchlist_quick_ai.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        for match in re.finditer(r"^.*ollama\.get_best_model\(\).*$", source, flags=re.MULTILINE):
            assert "fallback=ollama.get_best_model()" in match.group(0), f"{relative}: {match.group(0).strip()}"
    main = (ROOT / "api/main.py").read_text(encoding="utf-8")
    assert 'assigned_model("news_analyst", config=get_shared_config(), fallback=ollama.get_best_model())' in main


def test_the_saved_pre_earnings_view_names_the_model_that_really_answered(monkeypatch):
    """The role asks for Opus; when the subscription cannot answer, the fallback model does.

    The saved "LLM view" used to carry the REQUESTED model, so a view written by
    DeepSeek would have read "claude-opus-5-5".
    """
    import json

    from agents import pre_earnings_agent as pe

    agent = pe.PreEarningsAgent()
    monkeypatch.setattr(agent, "_analyst_model", lambda: "claude-opus-5-5")
    monkeypatch.setattr(pe.ollama, "is_cloud_host", lambda: True)
    monkeypatch.setattr(pe.ollama, "get_api_key", lambda *a, **k: "test-key")
    monkeypatch.setattr(pe.ollama, "get_timeout", lambda: 30)
    answer = json.dumps({"sentiment": 0.4, "confidence": 0.6, "decision": "LONG", "decision_confidence": 0.7,
                         "decision_rationale": "Guidance raised.", "catalyst_label": "GUIDANCE",
                         "why_bullets": ["w"], "what_to_watch": ["x"]})

    def item():
        return {"symbol": "AAPL", "gap_risk": 70, "direction_bias_score": 10, "direction_label": "NEUTRAL",
                "top_news": [{"title": "AAPL raises guidance", "source": "Reuters", "url": "x"}],
                "bias_reasons": ["r"], "why": ["w"], "what_to_watch": ["x"]}

    # 1. Opus answered: the view says Opus and carries no fallback note.
    served = item()
    monkeypatch.setattr(pe.ollama, "generate", lambda *a, **k: {
        "response": answer, "_meta": {"model": "claude-opus-5-5", "provider": "claude_subscription"}})
    assert agent._apply_ollama([served], agent._get_config(), force=True) is True
    assert served["ai_decision"]["model"] == "claude-opus-5-5"
    assert "requested_model" not in served["ai_decision"] and "fallback_reason" not in served["ai_decision"]

    # 2. The subscription could not answer: the view names the model that did, and why.
    stood_in = item()
    monkeypatch.setattr(pe.ollama, "generate", lambda *a, **k: {
        "response": answer, "_meta": {"model": "deepseek-v4-pro:cloud", "requested_model": "claude-opus-5-5",
                                      "subscription_fallback_reason": "Claude subscription daily call limit reached"}})
    assert agent._apply_ollama([stood_in], agent._get_config(), force=True) is True
    ai = stood_in["ai_decision"]
    assert ai["model"] == "deepseek-v4-pro:cloud"
    assert ai["requested_model"] == "claude-opus-5-5"
    assert "daily call limit" in ai["fallback_reason"]
