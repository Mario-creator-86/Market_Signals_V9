"""Golden §40/§43/§42 invariants for the Phase 4A tool registry."""
import pytest

pytestmark = pytest.mark.golden

from core.ai import SPEC_TOOL_NAMES, build_default_registry, run_tool_loop
from core.ai.provider import AIProvider, AIResponse, ToolCall


def test_golden_registry_exposes_exactly_the_28_spec_tools():
    reg = build_default_registry()
    assert set(reg.names()) == SPEC_TOOL_NAMES
    assert len(reg.names()) == 28


def test_golden_no_registered_tool_can_spawn_a_subprocess_or_open_a_file():
    # §40 — static guard: none of the 28 tool names is a shell/fs/sql/exec surface,
    # and the broker mutating methods are excluded from the allow-list.
    forbidden = {"run_shell", "exec_python", "read_file", "write_file", "query_sql",
                 "http_get", "read_secret", "open_position", "close_position", "process_signal"}
    assert forbidden.isdisjoint(SPEC_TOOL_NAMES)


def test_golden_propose_order_and_close_are_draft_only(monkeypatch):
    # §40/§42 — monkeypatch the broker mutating methods to raise; the draft tools
    # must still return a draft and never reach them.
    reg = build_default_registry()
    import core.capital_manager as cm_mod

    def _raise(*a, **k):
        raise AssertionError("broker mutation reached from a tool (§40/§42)")
    monkeypatch.setattr(cm_mod.CapitalManager, "open_position", _raise)
    monkeypatch.setattr(cm_mod.CapitalManager, "close_position", _raise)

    r1 = reg.dispatch(ToolCall(id="1", name="propose_order",
                              arguments={"ticker": "NVDA", "direction": "LONG", "score": 70,
                                         "notional_usd": 5000, "stop_loss_fraction": 0.05,
                                         "take_profit_pct": 0.1, "stop_loss_pct": 0.05}))
    assert r1.ok and r1.data.get("draft") is True
    r2 = reg.dispatch(ToolCall(id="2", name="propose_close", arguments={"position_id": "P1", "reason": "x"}))
    # propose_close returns ok=False if P1 not found, but must NOT raise the broker assertion.
    assert (r2.ok and r2.data.get("draft") is True) or (r2.ok is False and "not found" in r2.error)


def test_golden_get_available_capital_delegates_to_authoritative_service(monkeypatch):
    # §43 — get_available_capital returns the authoritative get_deployable_balance value,
    # not an AI-computed number. Monkeypatch the authoritative source and assert equality.
    reg = build_default_registry()
    import core.capital_manager as cm_mod

    sentinel = 12345.67
    monkeypatch.setattr(cm_mod.CapitalManager, "get_deployable_balance", lambda self: sentinel)
    r = reg.dispatch(ToolCall(id="1", name="get_available_capital", arguments={}))
    assert r.ok and r.data["available_capital"] == sentinel


class _FakeProvider(AIProvider):
    name = "fake"
    def __init__(self, scripted):
        self._scripted = scripted
        self._i = 0
        self.calls = []
    def list_models(self):
        return []
    def health_check(self, *, force=False):
        return True
    def capabilities(self):
        from core.ai.provider import ProviderCapabilities
        return ProviderCapabilities()
    def generate(self, prompt, *, model=None, system="", tools=None, **opts):
        return AIResponse(text="ok")
    def generate_structured(self, prompt, schema, *, model=None, system="", **opts):
        return AIResponse(text="ok")
    def tool_call(self, prompt, tools, *, model=None, system="", **opts):
        self.calls.append(tools)
        resp = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return resp
    def stream(self, prompt, *, model=None, system="", **opts):
        from core.ai.provider import AIStreamChunk
        yield AIStreamChunk(text="ok", done=True)
    def usage(self):
        from core.ai.provider import Usage
        return Usage()


def test_golden_dispatch_loop_end_to_end():
    reg = build_default_registry()
    provider = _FakeProvider([
        AIResponse(tool_calls=[ToolCall(id="c1", name="get_available_capital", arguments={})]),
        AIResponse(text="FINAL"),
    ])
    resp = run_tool_loop(provider, reg, "what capital is available?", max_rounds=4)
    assert resp.text == "FINAL"
    # The provider received the 28-tool payload on round 1.
    names = {t["name"] for t in provider.calls[0]}
    assert "get_available_capital" in names
    assert len(names) == 28