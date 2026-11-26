"""
tests/unit/test_ollama_chat_endpoint.py
========================================
Regression for the Pre-Earnings "AI does not respond" failure (2026-08):

The ollama.com CLOUD API does NOT expose `/api/generate` (HTTP 404), only
`/api/chat` (HTTP 401 → exists, needs auth). The OllamaClient called
`/api/generate`, so on a cloud host every AI call 404'd and the AI never
produced a verdict/analysis — even after the budget-exemption fix let the call
through.

Contract:
- `OllamaClient.generate` MUST call `/api/chat` with a chat-format payload
  (`messages`), supported by both local and cloud Ollama.
- The returned dict MUST expose the model text under the `response` key (the
  field every existing caller reads), populated from the chat response's
  `message.content`.
"""

import pytest

import core.ollama_client as oc


class _FakeResp:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


@pytest.mark.unit
def test_generate_uses_chat_endpoint_and_normalizes_response(monkeypatch):
    captured = {}

    def fake_post(url, *, provider=None, json=None, **kwargs):
        captured["url"] = url
        captured["payload"] = json
        return _FakeResp({"message": {"role": "assistant", "content": '{"verdict":"LONG"}'},
                          "done": True})

    monkeypatch.setattr(oc, "http_post", fake_post)
    monkeypatch.setattr(oc.ollama, "refresh_config", lambda: None)
    monkeypatch.setattr(oc.ollama, "get_best_model", lambda: "glm-5.2")

    result = oc.ollama.generate("analyze this", model="glm-5.2", system="be strict")

    # Must call the chat endpoint, not generate.
    assert "/api/chat" in captured["url"], f"expected /api/chat, got {captured['url']}"
    assert "/api/generate" not in captured["url"]

    # Must send a chat-format payload (messages), carrying the prompt + system.
    payload = captured["payload"]
    assert "messages" in payload
    roles = {m["role"]: m["content"] for m in payload["messages"]}
    assert roles.get("user") == "analyze this"
    assert roles.get("system") == "be strict"
    assert payload.get("stream") is False

    # The text must be exposed under `response` (what every caller reads).
    assert result["response"] == '{"verdict":"LONG"}'
    # Model metadata still attached.
    assert result["_meta"]["model"] == "glm-5.2"


@pytest.mark.unit
def test_generate_without_system_omits_system_message(monkeypatch):
    captured = {}

    def fake_post(url, *, provider=None, json=None, **kwargs):
        captured["payload"] = json
        return _FakeResp({"message": {"content": "ok"}, "done": True})

    monkeypatch.setattr(oc, "http_post", fake_post)
    monkeypatch.setattr(oc.ollama, "refresh_config", lambda: None)
    monkeypatch.setattr(oc.ollama, "get_best_model", lambda: "glm-5.2")

    result = oc.ollama.generate("hello", model="glm-5.2")
    roles = [m["role"] for m in captured["payload"]["messages"]]
    assert roles == ["user"]
    assert result["response"] == "ok"