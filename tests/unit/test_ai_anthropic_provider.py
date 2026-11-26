from __future__ import annotations

import pytest

from core.ai import AIResponse, AnthropicProvider, ToolCall


def _fake_post(return_payload, *, status=200, stream_lines=None):
    sent = []

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
    sent = []

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


def _make(post, get, **kw):
    return AnthropicProvider(
        name="anthropic",
        base_url="https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",
        default_model="claude-test",
        http_post=post,
        http_get=get,
        **kw,
    )


def test_generate_uses_xapikey_and_max_tokens(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-test")
    post = _fake_post({"content": [{"type": "text", "text": "hi"}], "usage": {"input_tokens": 3, "output_tokens": 2}})
    p = _make(post, _fake_get({"data": []}))
    r = p.generate("hello")
    assert isinstance(r, AIResponse) and r.text == "hi" and r.error is None
    sent = post.sent[0]
    assert sent["url"] == "https://api.anthropic.com/v1/messages"
    assert sent["headers"]["x-api-key"] == "ant-test"
    assert sent["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in sent["headers"]
    assert sent["json"]["max_tokens"]  # REQUIRED
    assert sent["json"]["messages"] == [{"role": "user", "content": "hello"}]
    assert sent["json"]["model"] == "claude-test"
    assert r.usage.prompt_tokens == 3 and r.usage.completion_tokens == 2


def test_generate_with_system_prompt(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-test")
    post = _fake_post({"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 1, "output_tokens": 1}})
    p = _make(post, _fake_get({}))
    p.generate("hi", system="be brief")
    assert post.sent[0]["json"]["system"] == "be brief"


def test_tool_call_parses_tool_use_blocks(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-test")
    post = _fake_post({"content": [{"type": "tool_use", "id": "tu_1", "name": "get_price", "input": {"ticker": "AAPL"}}], "usage": {"input_tokens": 1, "output_tokens": 1}})
    p = _make(post, _fake_get({}))
    r = p.tool_call("price?", [{"name": "get_price", "description": "d", "input_schema": {}}])
    assert r.error is None and len(r.tool_calls) == 1
    assert isinstance(r.tool_calls[0], ToolCall)
    assert r.tool_calls[0].name == "get_price" and r.tool_calls[0].arguments == {"ticker": "AAPL"}
    assert post.sent[0]["json"]["tools"] == [{"name": "get_price", "description": "d", "input_schema": {}}]


def test_no_key_returns_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p = _make(_fake_post({}), _fake_get({}))
    r = p.generate("hi")
    assert r.is_error and "key" in r.error.lower()


def test_list_models_and_health(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-test")
    get = _fake_get({"data": [{"id": "claude-a"}, {"id": "claude-b"}]})
    p = _make(_fake_post({}), get)
    assert p.list_models() == ["claude-a", "claude-b"]
    assert p.health_check() is True