"""Golden tests for the Phase 3 multi-provider fallback + autonomous-trade guard.

GOLD-013 — AI provider fallback: the primary provider failing does NOT abort the
request; the fallback chain serves it, and only when every provider is down is
the UNAVAILABLE sentinel returned.

GOLD-014 — All AI unavailable → no new autonomous trade: when the sentinel is
returned and config["ai"]["all_unavailable_no_trade"] is true (the default), the
guard blocks autonomous trading from opening a new position.
"""
import pytest

from core.ai.assignment import ModelAssignment
from core.ai.fallback import (
    all_unavailable,
    generate_with_fallback,
    no_new_autonomous_trade_guard,
)
from core.ai.provider import AIResponse, UNAVAILABLE
from core.ai.router import ModelRouter

pytestmark = pytest.mark.golden


class _FakeProvider:
    def __init__(self, name, *, fail=False, text="ok", model="m"):
        self.name = name
        self.default_model = model
        self._fail = fail
        self._text = text

    def generate(self, prompt, *, model=None, system="", **opts):
        if self._fail:
            return AIResponse(text="", error=f"{self.name} down")
        return AIResponse(text=self._text, model=model or self.default_model)


# --- GOLD-013: AI provider fallback ---

def test_gold013_primary_healthy_no_fallback_needed():
    primary = _FakeProvider("openai", text="primary")
    resp = generate_with_fallback(primary, "p", model="gpt-4o", fallback_chain=[_FakeProvider("anthropic", text="fb")])
    assert resp.text == "primary"
    assert not resp.is_error


def test_gold013_fallback_chain_serves_when_primary_down():
    primary = _FakeProvider("openai", fail=True)
    fb1 = _FakeProvider("anthropic", fail=True)
    fb2 = _FakeProvider("gemini", text="gemini-served")
    resp = generate_with_fallback(primary, "p", model="gpt-4o", fallback_chain=[fb1, fb2])
    assert resp.text == "gemini-served"
    assert not resp.is_error


def test_gold013_all_down_returns_unavailable_sentinel():
    primary = _FakeProvider("openai", fail=True)
    fb = _FakeProvider("anthropic", fail=True)
    resp = generate_with_fallback(primary, "p", model="gpt-4o", fallback_chain=[fb])
    assert resp is UNAVAILABLE
    assert all_unavailable(resp) is True


# --- GOLD-014: all AI unavailable → no new autonomous trade ---

def test_gold014_guard_blocks_new_autonomous_trade_when_all_down():
    config = {"ai": {"all_unavailable_no_trade": True}}
    assert no_new_autonomous_trade_guard(UNAVAILABLE, config) is True


def test_gold014_guard_allows_trade_when_ai_available():
    config = {"ai": {"all_unavailable_no_trade": True}}
    assert no_new_autonomous_trade_guard(AIResponse(text="ok"), config) is False


def test_gold014_guard_blocks_via_full_fallback_path():
    # End-to-end: all providers down → generate_with_fallback → guard blocks.
    config = {"ai": {"all_unavailable_no_trade": True, "fallback": {"enabled": True, "chain": []}}}
    primary = _FakeProvider("openai", fail=True)
    resp = generate_with_fallback(primary, "p", model="gpt-4o", fallback_chain=[_FakeProvider("anthropic", fail=True)])
    assert no_new_autonomous_trade_guard(resp, config) is True


# --- §37/§38 wiring: router + assignment resolve consistently with fallback ---

def test_router_assignment_resolve_consistently_for_critic_tier():
    assignment = ModelAssignment({"all": "cheap-mini", "risk_critic": "independent-1"})
    router = ModelRouter({"triage": {"model": "fast-mini"}, "analysis": {"model": None}, "critic": {"model": None}}, assignment)
    # triage uses the cheap tier model
    assert router.select_model("market_analyst", tier_hint="triage") == "fast-mini"
    # critic role with no critic-tier model falls back to the role assignment (independent)
    assert router.select_model("risk_critic", tier_hint="critic") == "independent-1"
    # a role with no explicit assignment uses `all`
    assert router.select_model("news_analyst") == "cheap-mini"