from __future__ import annotations

import dataclasses

import pytest

from core.ai.provider import (
    AIProvider,
    AIResponse,
    AIStreamChunk,
    HttpCallable,
    ProviderCapabilities,
    ToolCall,
    UNAVAILABLE,
    Usage,
)


def test_usage_is_frozen_value_type():
    u = Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    assert u.total_tokens == 15
    assert u.prompt_tokens == 10 and u.completion_tokens == 5
    with pytest.raises(dataclasses.FrozenInstanceError):
        u.total_tokens = 99  # type: ignore[misc]


def test_provider_capabilities_defaults():
    c = ProviderCapabilities()
    assert (c.text, c.structured_output, c.tool_calling, c.streaming) == (True, True, True, True)


def test_tool_call_carries_parsed_arguments():
    tc = ToolCall(id="call_1", name="get_price", arguments={"ticker": "AAPL"})
    assert tc.arguments == {"ticker": "AAPL"}
    assert tc.id == "call_1" and tc.name == "get_price"


def test_ai_response_minimal_and_with_error():
    ok = AIResponse(text="hello", tool_calls=[], usage=Usage(1, 2, 3), model="m", raw={}, error=None)
    assert ok.text == "hello" and ok.error is None
    err = AIResponse(text="", tool_calls=[], usage=Usage(0, 0, 0), model="", raw={}, error="boom")
    assert err.error == "boom" and err.text == ""


def test_ai_stream_chunk():
    ch = AIStreamChunk(text="hel", done=False)
    assert ch.text == "hel" and ch.done is False


def test_unavailable_sentinel_is_error_airesponse():
    assert isinstance(UNAVAILABLE, AIResponse)
    assert UNAVAILABLE.error == "all providers unavailable"
    assert UNAVAILABLE.text == ""


def test_ai_provider_is_abstract_and_lists_eight_capabilities():
    abstract = {name for name in dir(AIProvider) if not name.startswith("_")}
    for cap in (
        "list_models",
        "health_check",
        "capabilities",
        "generate",
        "generate_structured",
        "tool_call",
        "stream",
        "usage",
    ):
        assert cap in abstract, f"AIProvider must declare capability {cap}"
    with pytest.raises(TypeError):
        AIProvider()  # type: ignore[abstract]


def test_concrete_provider_must_implement_all():
    class Half(AIProvider):
        name = "half"

        def list_models(self):
            return []

        def health_check(self, *, force=False):
            return False

        def capabilities(self):
            return ProviderCapabilities()

        def generate(self, prompt, *, model=None, system="", tools=None, **opts):
            return AIResponse(text="", tool_calls=[], usage=Usage(0, 0, 0), model="", raw={}, error=None)

    with pytest.raises(TypeError):
        Half()  # missing generate_structured/tool_call/stream/usage