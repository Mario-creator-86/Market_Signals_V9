"""run_tool_loop generate→dispatch→feed-back with a fake provider."""
from core.ai.provider import AIProvider, AIResponse, ToolCall
from core.ai.tools import Tool, ToolRegistry, ToolResult, run_tool_loop


class _FakeProvider(AIProvider):
    name = "fake"

    def __init__(self, scripted):
        self._scripted = scripted  # list of AIResponse, one per round
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
        self.calls.append((prompt, tools))
        resp = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return resp

    def stream(self, prompt, *, model=None, system="", **opts):
        from core.ai.provider import AIStreamChunk
        yield AIStreamChunk(text="ok", done=True)

    def usage(self):
        from core.ai.provider import Usage
        return Usage()


def test_loop_dispatches_and_feeds_back_then_returns_final_answer():
    reg = ToolRegistry()
    reg.register(Tool(name="get_signal", description="x", arguments_schema={},
                      execute=lambda **a: ToolResult(ok=True, data={"price": 100})))
    provider = _FakeProvider([
        AIResponse(tool_calls=[ToolCall(id="c1", name="get_signal", arguments={"ticker": "NVDA"})]),
        AIResponse(text="FINAL: NVDA signal price 100"),
    ])
    resp = run_tool_loop(provider, reg, "analyze NVDA", max_rounds=4)
    assert resp.text == "FINAL: NVDA signal price 100"
    # round 1 returned a tool_call; round 2 received the fed-back tool_result
    assert "tool_result" in provider.calls[1][0]
    assert "get_signal" in provider.calls[1][0]


def test_loop_stops_on_error_response():
    reg = ToolRegistry()
    provider = _FakeProvider([AIResponse(error="provider down")])
    resp = run_tool_loop(provider, reg, "x", max_rounds=4)
    assert resp.is_error


def test_loop_respects_max_rounds():
    reg = ToolRegistry()
    reg.register(Tool(name="get_signal", description="x", arguments_schema={},
                      execute=lambda **a: ToolResult(ok=True, data={})))
    # Always returns a tool_call — never a final answer; must cap at max_rounds.
    provider = _FakeProvider([
        AIResponse(tool_calls=[ToolCall(id="c", name="get_signal", arguments={})]),
    ])
    resp = run_tool_loop(provider, reg, "x", max_rounds=2)
    assert len(provider.calls) == 2  # capped