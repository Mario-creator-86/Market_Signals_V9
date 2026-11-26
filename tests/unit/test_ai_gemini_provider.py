from __future__ import annotations

import pytest

from core.ai import AIResponse, GeminiProvider, ToolCall


def _fake_post(return_payload, *, status=200):
    sent = []

    class _Resp:
        def __init__(self):
            self.status_code = status
            self._payload = return_payload

        def json(self):
            return self._payload

    def _post(url, *, provider=None, headers=None, json=None, timeout=None, max_retries=0, stream=False, **kw):
        sent.append({"url": url, "provider": provider, "headers": dict(headers or {}), "json": json})
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
    return GeminiProvider(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com",
        api_key_env="GEMINI_API_KEY",
        default_model="gemini-test",
        http_post=post,
        http_get=get,
        **kw,
    )


def test_generate_uses_key_query_and_parses_part(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gem-test")
    post = _fake_post({"candidates": [{"content": {"parts": [{"text": "hi"}]}}], "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1, "totalTokenCount": 3}})
    p = _make(post, _fake_get({"models": []}))
    r = p.generate("hello")
    assert isinstance(r, AIResponse) and r.text == "hi" and r.error is None
    sent = post.sent[0]
    assert sent["url"].startswith("https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent")
    assert "gem-test" not in sent["url"]
    assert sent["headers"]["x-goog-api-key"] == "gem-test"
    assert sent["json"]["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]
    assert r.usage.prompt_tokens == 2 and r.usage.completion_tokens == 1


def test_generate_with_system_instruction(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gem-test")
    post = _fake_post({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
    p = _make(post, _fake_get({}))
    p.generate("hi", system="be brief")
    assert post.sent[0]["json"]["systemInstruction"] == {"parts": [{"text": "be brief"}]}


def test_generate_structured_sets_response_mime(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gem-test")
    post = _fake_post({"candidates": [{"content": {"parts": [{"text": '{"a": 1}'}]}}]})
    p = _make(post, _fake_get({}))
    r = p.generate_structured("q", {"type": "object", "properties": {"a": {}}})
    assert r.error is None and r.raw["json"] == {"a": 1}
    assert post.sent[0]["json"]["generationConfig"]["responseMimeType"] == "application/json"


def test_tool_call_parses_function_call(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gem-test")
    post = _fake_post({"candidates": [{"content": {"parts": [{"functionCall": {"name": "get_price", "args": {"ticker": "AAPL"}}}]}}]})
    p = _make(post, _fake_get({}))
    r = p.tool_call("price?", [{"functionDeclarations": [{"name": "get_price", "parameters": {}}]}])
    assert r.error is None and len(r.tool_calls) == 1
    assert isinstance(r.tool_calls[0], ToolCall)
    assert r.tool_calls[0].name == "get_price" and r.tool_calls[0].arguments == {"ticker": "AAPL"}


def test_no_key_returns_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    p = _make(_fake_post({}), _fake_get({}))
    r = p.generate("hi")
    assert r.is_error and "key" in r.error.lower()


def test_list_models_and_health(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gem-test")
    get = _fake_get({"models": [{"name": "models/gemini-a"}, {"name": "models/gemini-b"}]})
    p = _make(_fake_post({}), get)
    assert p.list_models() == ["gemini-a", "gemini-b"]  # name stripped of "models/" prefix
    assert p.health_check() is True