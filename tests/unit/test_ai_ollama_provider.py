from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from core.ai import AIResponse, AIStreamChunk, OllamaProvider, ToolCall, get_ollama_provider


class _FakeClient:
    """Stand-in for core.ollama_client.ollama — records calls, returns canned dicts."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._models = ["gemma3:12b", "gpt-oss:120b"]
        self._available = True
        self._generate_result: Dict[str, Any] = {
            "response": "ok text",
            "_meta": {
                "model": "gemma3:12b",
                "generated_at": "2026-08-11T00:00:00+00:00",
                "ui_call": False,
                "timeout_sec": 120,
            },
        }

    def get_models(self, *, force: bool = False) -> List[str]:
        return list(self._models)

    def check_availability(self, *, force: bool = False) -> bool:
        return self._available

    def generate(self, prompt, model=None, system="", ui_call=False, timeout_override=None):
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "system": system,
                "ui_call": ui_call,
                "timeout_override": timeout_override,
            }
        )
        return dict(self._generate_result)

    def is_cloud_host(self) -> bool:
        return False

    def get_preferred_model(self) -> str:
        return "gemma3:12b"

    def get_best_model(self) -> str:
        return "gemma3:12b"

    def _headers(self) -> Dict[str, str]:
        return {}

    def _api_url(self, path: str) -> str:
        return f"http://localhost:11434/api/{path}"


def _fake_http_post(return_payload):
    """Return an http_post seam that records the request and returns a fake response."""
    sent: List[Dict[str, Any]] = []

    class _Resp:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload

        def json(self):
            return self._payload

        def iter_lines(self):
            # NDJSON streaming: emit one JSON object per payload element
            for obj in return_payload:
                yield json.dumps(obj)

    def _post(url, *, provider=None, **kwargs):
        sent.append({"url": url, "provider": provider, "kwargs": kwargs})
        return _Resp(return_payload)

    _post.sent = sent  # type: ignore[attr-defined]
    return _post


def test_generate_lifts_legacy_dict_to_airesponse():
    client = _FakeClient()
    p = OllamaProvider(client=client)
    r = p.generate("hi", model="gemma3:12b", system="be brief")
    assert isinstance(r, AIResponse)
    assert r.text == "ok text"
    assert r.model == "gemma3:12b"
    assert r.error is None
    assert client.calls[0]["system"] == "be brief"


def test_generate_propagates_error():
    client = _FakeClient()
    client._generate_result = {"error": "Ollama circuit open"}
    r = OllamaProvider(client=client).generate("hi")
    assert r.is_error and r.error == "Ollama circuit open"
    assert r.text == ""


def test_list_models_and_health_delegate():
    client = _FakeClient()
    p = OllamaProvider(client=client)
    assert p.list_models() == ["gemma3:12b", "gpt-oss:120b"]
    assert p.health_check() is True
    client._available = False
    assert p.health_check() is False


def test_capabilities():
    c = OllamaProvider(client=_FakeClient()).capabilities()
    assert (c.text, c.structured_output, c.tool_calling, c.streaming) == (True, True, True, True)
    assert "json" in c.supported_formats


def test_generate_structured_posts_json_mode_and_parses():
    payload = {"message": {"content": '{"answer": 42}'}}
    post = _fake_http_post(payload)
    p = OllamaProvider(client=_FakeClient(), http_post=post)
    r = p.generate_structured("what is the answer?", {"type": "object", "properties": {"answer": {}}})
    assert r.error is None
    assert r.raw["json"] == {"answer": 42}
    sent = post.sent[0]
    assert sent["kwargs"]["json"]["format"] == "json"
    assert sent["kwargs"]["json"]["stream"] is False
    assert sent["provider"] == "ollama"


def test_generate_structured_handles_bad_json():
    payload = {"message": {"content": "not json at all"}}
    post = _fake_http_post(payload)
    r = OllamaProvider(client=_FakeClient(), http_post=post).generate_structured("x", {})
    assert r.is_error  # could not parse structured output
    assert r.raw.get("json") is None


def test_stream_parses_ollama_ndjson():
    payload = [
        {"message": {"content": "hel"}, "done": False},
        {"message": {"content": "lo"}, "done": False},
        {"message": {"content": ""}, "done": True, "eval_count": 5},
    ]
    post = _fake_http_post(payload)
    p = OllamaProvider(client=_FakeClient(), http_post=post)
    chunks = list(p.stream("hi"))
    assert [c.text for c in chunks] == ["hel", "lo", ""]
    assert chunks[-1].done is True
    assert chunks[0].done is False
    assert post.sent[0]["kwargs"]["json"]["stream"] is True


def test_tool_call_parses_ollama_tool_calls():
    payload = {
        "message": {
            "tool_calls": [
                {"function": {"name": "get_price", "arguments": '{"ticker": "AAPL"}'}},
            ]
        }
    }
    post = _fake_http_post(payload)
    r = OllamaProvider(client=_FakeClient(), http_post=post).tool_call(
        "price?", [{"type": "function", "function": {"name": "get_price"}}]
    )
    assert r.error is None
    assert len(r.tool_calls) == 1
    assert isinstance(r.tool_calls[0], ToolCall)
    assert r.tool_calls[0].name == "get_price"
    assert r.tool_calls[0].arguments == {"ticker": "AAPL"}
    assert post.sent[0]["kwargs"]["json"]["tools"] == [
        {"type": "function", "function": {"name": "get_price"}}
    ]


def test_usage_captured_from_last_call():
    payload = {"message": {"content": "ok"}, "prompt_eval_count": 7, "eval_count": 3}
    post = _fake_http_post(payload)
    p = OllamaProvider(client=_FakeClient(), http_post=post)
    p.generate_structured("x", {})
    u = p.usage()
    assert u.prompt_tokens == 7 and u.completion_tokens == 3


def test_get_ollama_provider_wraps_singleton():
    p = get_ollama_provider()
    assert p.name == "ollama"
    assert p is get_ollama_provider()  # cached singleton