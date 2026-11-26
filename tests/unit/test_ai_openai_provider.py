from __future__ import annotations

from typing import Any, Dict, List

import pytest

from core.ai import AIResponse, AIStreamChunk, OpenAICompatibleProvider, OpenAIProvider, ToolCall


def _fake_post(return_payload, *, status=200, stream_lines=None):
    sent: List[Dict[str, Any]] = []

    class _Resp:
        def __init__(self):
            self.status_code = status
            self._payload = return_payload
            self._stream = stream_lines

        def json(self):
            return self._payload

        def iter_lines(self):
            for line in (self._stream or []):
                yield line

    def _post(url, *, provider=None, headers=None, json=None, timeout=None, max_retries=0, stream=False, **kw):
        sent.append({"url": url, "provider": provider, "headers": dict(headers or {}), "json": json, "stream": stream})
        return _Resp()

    _post.sent = sent  # type: ignore[attr-defined]
    return _post


def _fake_get(return_payload, *, status=200):
    sent: List[Dict[str, Any]] = []

    class _Resp:
        def __init__(self):
            self.status_code = status
            self._payload = return_payload

        def json(self):
            return self._payload

    def _get(url, *, provider=None, headers=None, timeout=None, **kw):
        sent.append({"url": url, "provider": provider, "headers": dict(headers or {})})
        return _Resp()

    _get.sent = sent  # type: ignore[attr-defined]
    return _get


def _make_provider(post, get, **kw):
    return OpenAIProvider(
        name="openai",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        default_model="gpt-test",
        http_post=post,
        http_get=get,
        **kw,
    )


def test_generate_posts_bearer_and_parses_choice(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    post = _fake_post({"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}})
    get = _fake_get({"data": [{"id": "gpt-test"}]})
    p = _make_provider(post, get)
    r = p.generate("hello")
    assert isinstance(r, AIResponse) and r.text == "hi" and r.error is None
    sent = post.sent[0]
    assert sent["url"] == "https://api.openai.com/v1/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer sk-test"
    assert sent["json"]["model"] == "gpt-test"
    assert sent["json"]["messages"] == [{"role": "user", "content": "hello"}]
    assert sent["json"]["stream"] is False
    assert sent["provider"] == "openai"
    assert r.usage.prompt_tokens == 2 and r.usage.completion_tokens == 1


def test_generate_no_key_returns_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    p = _make_provider(_fake_post({}), _fake_get({}))
    r = p.generate("hi")
    assert r.is_error and "key" in r.error.lower()


def test_generate_propagates_http_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    post = _fake_post({"error": {"message": "bad"}}, status=401)
    p = _make_provider(post, _fake_get({}))
    r = p.generate("hi")
    assert r.is_error and "401" in r.error


def test_generate_structured_uses_json_object_format(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    post = _fake_post({"choices": [{"message": {"content": '{"a": 1}'}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    p = _make_provider(post, _fake_get({}))
    r = p.generate_structured("q", {"type": "object", "properties": {"a": {}}})
    assert r.error is None and r.raw["json"] == {"a": 1}
    assert post.sent[0]["json"]["response_format"] == {"type": "json_object"}


def test_tool_call_parses_openai_tool_calls(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    post = _fake_post({"choices": [{"message": {"tool_calls": [
        {"id": "call_1", "function": {"name": "get_price", "arguments": '{"ticker": "AAPL"}'}}]}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    p = _make_provider(post, _fake_get({}))
    r = p.tool_call("price?", [{"type": "function", "function": {"name": "get_price", "parameters": {}}}])
    assert r.error is None and len(r.tool_calls) == 1
    assert isinstance(r.tool_calls[0], ToolCall)
    assert r.tool_calls[0].name == "get_price" and r.tool_calls[0].arguments == {"ticker": "AAPL"}
    assert post.sent[0]["json"]["tools"] == [{"type": "function", "function": {"name": "get_price", "parameters": {}}}]


def test_stream_parses_openai_sse(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    lines = [
        b'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"hel"}}]}',
        b'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"lo"}}]}',
        b'data: [DONE]',
    ]
    post = _fake_post(None, stream_lines=lines)
    p = _make_provider(post, _fake_get({}))
    chunks = list(p.stream("hi"))
    assert [c.text for c in chunks] == ["hel", "lo"]
    assert all(isinstance(c, AIStreamChunk) for c in chunks)
    assert post.sent[0]["json"]["stream"] is True


def test_list_models_and_health(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    get = _fake_get({"data": [{"id": "gpt-a"}, {"id": "gpt-b"}]})
    post = _fake_post({})
    p = _make_provider(post, get)
    assert p.list_models() == ["gpt-a", "gpt-b"]
    assert p.health_check() is True
    assert get.sent[0]["url"] == "https://api.openai.com/v1/models"


def test_capabilities():
    p = OpenAIProvider(name="openai", base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY")
    c = p.capabilities()
    assert (c.text, c.structured_output, c.tool_calling, c.streaming) == (True, True, True, True)


def test_openai_compatible_provider_is_subclass_with_name(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    post = _fake_post({"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    p = OpenAICompatibleProvider(name="xai", base_url="https://api.x.ai/v1", api_key_env="XAI_API_KEY", default_model="grok-test", http_post=post, http_get=_fake_get({"data": []}))
    assert isinstance(p, OpenAIProvider)
    assert p.name == "xai"
    r = p.generate("hi")
    assert r.text == "ok" and post.sent[0]["url"] == "https://api.x.ai/v1/chat/completions"
    assert post.sent[0]["headers"]["Authorization"] == "Bearer xai-test"
    assert post.sent[0]["provider"] == "xai"