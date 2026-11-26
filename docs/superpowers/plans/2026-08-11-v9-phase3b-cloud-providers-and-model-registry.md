# Phase 3B — Cloud AI Providers + Model Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the cloud `AIProvider` implementations (OpenAI, Anthropic, Gemini, and an OpenAI-compatible provider covering xAI/DeepSeek/Mistral/OpenRouter) over the governed `core/http_utils` `requests` seam — zero new dependencies — and the `ModelRegistry` (§36) that discovers models via `list_models()` and allows manual configuration. Each provider is fully testable without keys or network via an injectable transport that pins the exact verified request shape. Second of three Phase 3 sub-plans.

**Architecture:** Four new provider modules in `core/ai/`. `OpenAIProvider` implements the OpenAI `/v1/chat/completions` shape (Bearer auth, `choices[0].message`, `usage.prompt_tokens`/`completion_tokens`, SSE `chat.completion.chunk` streaming, `tool_calls`); `OpenAICompatibleProvider` is a trivial subclass with the same wire shape (config-driven `base_url` covers xAI/DeepSeek/Mistral/OpenRouter). `AnthropicProvider` implements `/v1/messages` (`x-api-key` + `anthropic-version: 2023-06-01`, `max_tokens` required, `content[]` blocks, `tool_use`, `usage.input_tokens`/`output_tokens`). `GeminiProvider` implements `:generateContent?key=` (`contents`/`parts`, `generationConfig.responseMimeType`, `functionDeclarations`, `usageMetadata`). All reuse the §34 `AIProvider` ABC + value types from 3A and the 3A `OllamaProvider` patterns (injectable `http_post` seam, `_last_usage` capture, `UNAVAILABLE`-aware error returns). `core/http_utils.py` gains governor entries for the 8 new provider names. `core/ai/registry.py` `ModelRegistry` stores §36 entries (provider/model_id/display_name/capabilities/tool_support/structured_output_support/context_metadata/health/enabled/last_checked), discovers via `list_models()`, and supports manual config — persisted to `config["ai"]["registry"]` (in-memory canonical, optional JSON snapshot).

**Tech Stack:** Python 3.14.3, stdlib (`json`, `threading`, `dataclasses`, `typing`), `core/http_utils` (`requests`, already pinned), `core/config`. **No new dependencies** (§118 — the installed `openai`/`anthropic`/`google-genai` SDKs are deliberately NOT used; they are unpinned in `requirements.txt`, so adopting them would violate §25).

## Global Constraints

(Copied verbatim from `prompt_to_follow.md`; every task's requirements implicitly include these.)

- **§34:** `AIProvider` with the 8 normalized capabilities (already delivered in 3A — consumed here).
- **§35:** providers Ollama Local/Cloud, OpenAI, Anthropic, Gemini + adapters for xAI/DeepSeek/Mistral/OpenRouter; do NOT hardcode model names; verify current official APIs at implementation time (the REST shapes below were verified 2026-08-11 per §25; fake-transport tests pin them).
- **§36 (Model Registry):** fields `provider`/`model_id`/`display_name`/`capabilities`/`tool_support`/`structured_output_support`/`context_metadata`/`health`/`enabled`/`last_checked`; discover models dynamically when the provider supports it; allow manual configuration otherwise.
- **§106:** keys via env (`api_key_env`), never committed, never to frontend, never logged. The `provider="..."` string in http_utils carries no secret; headers built per-call from the resolved key.
- **§4:** no forbidden success language.
- **§5:** no rewrite — evolve; reuse the 3A `AIProvider`/value types/`OllamaProvider` patterns.
- **§115:** no skip/xfail/`assert True`/delete failing test/weaken assertion.
- **§117:** V7 read-only (HEAD `f912a33`, 109 dirty) — never modify.
- **§116:** no remote push (no remote).
- **§118/§25:** no new production deps; pin deps. 3B adds none.
- **Baseline:** 1676 passed / 0 failed / exit 0 (Phase 3A, commit `3f17340`). 3B keeps this green and adds the cloud-provider + registry tests.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `core/ai/openai_provider.py` (NEW) | `OpenAIProvider(AIProvider)` — OpenAI chat-completions shape. | 1 |
| `core/ai/openai_compatible_provider.py` (NEW) | `OpenAICompatibleProvider(OpenAIProvider)` — same shape, xAI/DeepSeek/Mistral/OpenRouter. | 1 |
| `core/http_utils.py` (MODIFY, additive) | governor entries for openai/anthropic/gemini/xai/deepseek/mistral/openrouter. | 1 |
| `core/ai/anthropic_provider.py` (NEW) | `AnthropicProvider(AIProvider)` — `/v1/messages` shape. | 2 |
| `core/ai/gemini_provider.py` (NEW) | `GeminiProvider(AIProvider)` — `:generateContent` shape. | 3 |
| `core/ai/registry.py` (NEW) | `ModelRegistry` (§36) + `ModelEntry` value type. | 4 |
| `core/ai/__init__.py` (MODIFY) | export the new providers + `ModelRegistry`. | 1,2,3,4 |
| `tests/unit/test_ai_openai_provider.py` (NEW) | OpenAI + compatible fake-transport tests. | 1 |
| `tests/unit/test_ai_anthropic_provider.py` (NEW) | Anthropic fake-transport tests. | 2 |
| `tests/unit/test_ai_gemini_provider.py` (NEW) | Gemini fake-transport tests. | 3 |
| `tests/unit/test_ai_model_registry.py` (NEW) | ModelRegistry discovery + manual + persistence tests. | 4 |
| `docs/V9_ARCHITECTURE.md` (MODIFY, additive) | cloud-providers + registry subsection. | 5 |
| `docs/V9_IMPLEMENTATION.md` (MODIFY, additive) | Phase 3B record; Phase 3 row stays IN PROGRESS. | 5 |

**NOT modified:** `core/ollama_client.py`, `core/ai/provider.py`, `core/ai/ollama_provider.py`, `core/config.py` (the `ai` block from 3A already carries the provider entries + `registry: []`).

---

### Task 1: OpenAIProvider + OpenAICompatibleProvider + http_utils governor entries

**Files:**
- Create: `core/ai/openai_provider.py`, `core/ai/openai_compatible_provider.py`
- Modify: `core/http_utils.py` (additive governor entries), `core/ai/__init__.py`
- Create: `tests/unit/test_ai_openai_provider.py`

**Interfaces:**
- Consumes: `core.ai.provider` (`AIProvider`, `AIResponse`, `AIStreamChunk`, `ProviderCapabilities`, `ToolCall`, `Usage`), `core.http_utils.http_post`, `core.config.ConfigManager`.
- Produces: `OpenAIProvider(AIProvider)` (`__init__(self, *, name, base_url, api_key_env, default_model="", timeout=60, http_post=None)`), `OpenAICompatibleProvider(OpenAIProvider)`. Capabilities: `list_models` (OpenAI `/v1/models` GET → `data[].id`), `health_check` (GET `/v1/models` 200), `generate`/`generate_structured`/`tool_call`/`stream`/`usage`/`capabilities`.

**Verified REST shape (OpenAI, §2):** `POST {base_url}/chat/completions`; `Authorization: Bearer <key>`; body `{model, messages:[{role,content}], stream, response_format?, tools:[{type:"function",function:{name,description,parameters}}], temperature}`; response `choices[0].message.{content,tool_calls:[{id,function:{name,arguments(JSON string)}}]}`, `usage:{prompt_tokens,completion_tokens,total_tokens}`; streaming SSE chunks `object:"chat.completion.chunk"`, `choices[0].delta.content`, terminated by `data: [DONE]`. `list_models`: `GET {base_url}/models` → `{"data":[{"id":...},...]}`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_ai_openai_provider.py`:

```python
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from core.ai import AIResponse, AIStreamChunk, OpenAICompatibleProvider, OpenAIProvider, ToolCall, Usage


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
    p = OpenAIProvider(
        name="openai",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        default_model="gpt-test",
        http_post=post,
        http_get=get,
        **kw,
    )
    return p


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


def test_openai_compatible_provider_is_subclass_with_name():
    import dataclasses
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    post = _fake_post({"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    p = OpenAICompatibleProvider(name="xai", base_url="https://api.x.ai/v1", api_key_env="XAI_API_KEY", default_model="grok-test", http_post=post, http_get=_fake_get({"data": []}))
    assert isinstance(p, OpenAIProvider)
    assert p.name == "xai"
    r = p.generate("hi")
    assert r.text == "ok" and post.sent[0]["url"] == "https://api.x.ai/v1/chat/completions"
    assert post.sent[0]["headers"]["Authorization"] == "Bearer xai-test"
    assert post.sent[0]["provider"] == "xai"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_ai_openai_provider.py -v`
Expected: FAIL — `ImportError: cannot import name 'OpenAIProvider'`.

- [ ] **Step 3: Add http_utils governor entries**

In `core/http_utils.py`, add the 8 new provider names to `_PROVIDER_LIMITS` (concurrency 2 each — LLM providers are slow, like ollama), `_CIRCUIT_THRESHOLDS` (5 each, like ollama — model loading / cold starts), and `_RATE_LIMIT_DELAYS` (0.0 each — the provider's own rate limiting governs; we don't double-gate). Append within each dict literal:

```python
_PROVIDER_LIMITS = {
    "alpaca": 4, "finnhub": 3, "binance": 4, "kucoin": 4, "yahoo": 3,
    "ollama": 2, "news": 3, "marketaux": 2, "generic": 4,
    # Phase 3B — cloud AI providers (slow LLM calls; circuit threshold like ollama)
    "openai": 2, "anthropic": 2, "gemini": 2,
    "xai": 2, "deepseek": 2, "mistral": 2, "openrouter": 2,
}
```

```python
_CIRCUIT_THRESHOLDS: Dict[str, int] = {
    "alpaca": 8, "ollama": 5, "finnhub": 5, "binance": 15, "kucoin": 5,
    "yahoo": 15, "news": 10, "marketaux": 3, "generic": 5,
    "openai": 5, "anthropic": 5, "gemini": 5, "xai": 5, "deepseek": 5,
    "mistral": 5, "openrouter": 5,
}
```

```python
_RATE_LIMIT_DELAYS: Dict[str, float] = {
    "alpaca": 0.05, "finnhub": 0.0, "binance": 0.1, "kucoin": 0.2,
    "yahoo": 0.1, "news": 0.2, "marketaux": 0.2, "ollama": 0.0, "generic": 0.1,
    "openai": 0.0, "anthropic": 0.0, "gemini": 0.0, "xai": 0.0,
    "deepseek": 0.0, "mistral": 0.0, "openrouter": 0.0,
}
```

(Note: `_SEMAPHORES`, `_CIRCUIT_STATE`, `_LAST_REQUEST_TIME` derive from `_PROVIDER_LIMITS.keys()` at import, so they pick up the new names automatically — no other edits needed.)

- [ ] **Step 4: Write the implementation**

Create `core/ai/openai_provider.py`:

```python
"""OpenAIProvider — the AIProvider adapter for the OpenAI Chat Completions API (spec §34/§35).

Wire shape verified 2026-08-11 (§25): POST {base_url}/chat/completions, Bearer auth,
choices[0].message.{content,tool_calls}, usage.{prompt_tokens,completion_tokens},
SSE chat.completion.chunk streaming terminated by `data: [DONE]`. list_models/health
GET {base_url}/models.

Zero new deps (§118): talks over core/http_utils (requests, already pinned), NOT the
unpinned official `openai` SDK. Injectable http_post/http_get seam → no keys/network in tests (§106).
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional

from core.ai.provider import (
    AIProvider, AIResponse, AIStreamChunk, ProviderCapabilities, ToolCall, Usage,
)

_DefaultPost = None
_DefaultGet = None


def _default_post() -> Callable[..., Any]:
    global _DefaultPost
    if _DefaultPost is None:
        from core.http_utils import http_post as _DefaultPost  # type: ignore[assignment]
    return _DefaultPost  # type: ignore[return-value]


def _default_get() -> Callable[..., Any]:
    global _DefaultGet
    if _DefaultGet is None:
        from core.http_utils import http_get as _DefaultGet  # type: ignore[assignment]
    return _DefaultGet  # type: ignore[return-value]


def _extract_json(text: str) -> Optional[Any]:
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except (ValueError, TypeError):
                        break
        start = text.find("{", start + 1)
    return None


class OpenAIProvider(AIProvider):
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key_env: str,
        default_model: str = "",
        timeout: int = 60,
        http_post: Optional[Callable[..., Any]] = None,
        http_get: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.default_model = default_model
        self.timeout = timeout
        self._http_post = http_post
        self._http_get = http_get
        self._lock = threading.Lock()
        self._last_usage: Usage = Usage()

    # --- key + headers ---
    def _api_key(self) -> Optional[str]:
        return os.getenv(self.api_key_env)

    def _headers(self) -> Dict[str, str]:
        key = self._api_key()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _resolve_model(self, model: Optional[str]) -> str:
        return model or self.default_model

    # --- AIProvider ---
    def list_models(self) -> List[str]:
        get = self._http_get or _default_get()
        resp = get(f"{self.base_url}/models", provider=self.name, headers=self._headers(), timeout=self.timeout)
        if resp is None or resp.status_code != 200:
            return []
        data = resp.json() or {}
        return [str(m.get("id")) for m in (data.get("data") or []) if m.get("id")]

    def health_check(self, *, force: bool = False) -> bool:
        get = self._http_get or _default_get()
        resp = get(f"{self.base_url}/models", provider=self.name, headers=self._headers(), timeout=self.timeout)
        return bool(resp is not None and resp.status_code == 200)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(text=True, structured_output=True, tool_calling=True, streaming=True, supported_formats=("json",))

    def generate(self, prompt, *, model=None, system="", tools=None, **opts) -> AIResponse:
        messages = self._build_messages(prompt, system)
        payload: Dict[str, Any] = {"model": self._resolve_model(model), "messages": messages, "stream": False}
        return self._post_chat(payload, parse_json=False)

    def generate_structured(self, prompt, schema, *, model=None, system="", **opts) -> AIResponse:
        messages = self._build_messages(prompt, system)
        payload = {
            "model": self._resolve_model(model),
            "messages": messages,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        return self._post_chat(payload, parse_json=True)

    def tool_call(self, prompt, tools, *, model=None, system="", **opts) -> AIResponse:
        messages = self._build_messages(prompt, system)
        payload = {"model": self._resolve_model(model), "messages": messages, "stream": False, "tools": tools}
        return self._post_chat(payload, parse_json=False, parse_tools=True)

    def stream(self, prompt, *, model=None, system="", **opts) -> Iterator[AIStreamChunk]:
        key = self._api_key()
        if not key:
            yield AIStreamChunk(text="", done=True)
            return
        messages = self._build_messages(prompt, system)
        payload = {"model": self._resolve_model(model), "messages": messages, "stream": True}
        post = self._http_post or _default_post()
        resp = post(
            f"{self.base_url}/chat/completions",
            provider=self.name, headers=self._headers(), json=payload,
            timeout=self.timeout, max_retries=0, stream=True,
        )
        if resp is None:
            yield AIStreamChunk(text="", done=True)
            return
        for line in resp.iter_lines():
            if not line:
                continue
            if isinstance(line, bytes):
                line = line.decode("utf-8", "ignore")
            if not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                yield AIStreamChunk(text="", done=True)
                return
            try:
                obj = json.loads(data_str)
            except (ValueError, TypeError):
                continue
            delta = (obj.get("choices") or [{}])[0].get("delta") or {}
            content = str(delta.get("content") or "")
            if content:
                yield AIStreamChunk(text=content, done=False)

    def usage(self) -> Usage:
        with self._lock:
            return self._last_usage

    # --- internals ---
    def _build_messages(self, prompt: str, system: str) -> List[Dict[str, Any]]:
        msgs: List[Dict[str, Any]] = []
        if str(system or "").strip():
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        return msgs

    def _post_chat(self, payload: Dict[str, Any], *, parse_json: bool, parse_tools: bool = False) -> AIResponse:
        key = self._api_key()
        if not key:
            return AIResponse(text="", error=f"{self.name} api key not set (env {self.api_key_env})")
        post = self._http_post or _default_post()
        resp = post(
            f"{self.base_url}/chat/completions",
            provider=self.name, headers=self._headers(), json=payload,
            timeout=self.timeout, max_retries=0,
        )
        if resp is None:
            return AIResponse(text="", error=f"{self.name} circuit open")
        if resp.status_code != 200:
            return AIResponse(text="", error=f"{self.name} error: {resp.status_code}")
        data = resp.json() or {}
        msg = ((data.get("choices") or [{}])[0].get("message")) or {}
        text = str(msg.get("content") or "")
        usage = self._usage_from_openai(data)
        with self._lock:
            self._last_usage = usage
        tool_calls: List[ToolCall] = []
        if parse_tools:
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args_raw = fn.get("arguments")
                args = args_raw
                if isinstance(args_raw, str):
                    try:
                        args = json.loads(args_raw)
                    except (ValueError, TypeError):
                        args = {}
                tool_calls.append(ToolCall(id=str(tc.get("id", "")), name=str(fn.get("name", "")), arguments=args if isinstance(args, dict) else {}))
        if parse_json:
            parsed = _extract_json(text)
            if parsed is None:
                return AIResponse(text=text, error="structured output parse failed", usage=usage, raw={"json": None}, model=payload.get("model", ""))
            return AIResponse(text=text, tool_calls=[], usage=usage, model=payload.get("model", ""), raw={"json": parsed})
        return AIResponse(text=text, tool_calls=tool_calls, usage=usage, model=payload.get("model", ""), raw=data)

    @staticmethod
    def _usage_from_openai(data: Dict[str, Any]) -> Usage:
        u = data.get("usage") or {}
        return Usage(
            prompt_tokens=int(u["prompt_tokens"]) if u.get("prompt_tokens") is not None else None,
            completion_tokens=int(u["completion_tokens"]) if u.get("completion_tokens") is not None else None,
            total_tokens=int(u["total_tokens"]) if u.get("total_tokens") is not None else None,
        )
```

Create `core/ai/openai_compatible_provider.py`:

```python
"""OpenAICompatibleProvider — OpenAI Chat Completions shape for xAI/DeepSeek/Mistral/OpenRouter (§35).

Same wire shape as OpenAIProvider (config-driven base_url + api_key_env); the type
registry uses this class for config type "openai_compatible".
"""
from __future__ import annotations

from core.ai.openai_provider import OpenAIProvider


class OpenAICompatibleProvider(OpenAIProvider):
    """Identical wire shape to OpenAI; distinguished by config `type: openai_compatible`."""
```

Update `core/ai/__init__.py` — add the two imports + `__all__` entries:

```python
from core.ai.openai_provider import OpenAIProvider
from core.ai.openai_compatible_provider import OpenAICompatibleProvider
```
Add `"OpenAIProvider"` and `"OpenAICompatibleProvider"` to `__all__`.

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_ai_openai_provider.py -v`
Expected: PASS (10 assertions).

- [ ] **Step 6: Run http_utils-touching regression (governor entries don't break existing providers)**

Run: `python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf -q`
Expected: 1676 + 10 new = 1686 passed / 0 failed / exit 0.

- [ ] **Step 7: Commit**

```bash
git add core/ai/openai_provider.py core/ai/openai_compatible_provider.py core/http_utils.py core/ai/__init__.py tests/unit/test_ai_openai_provider.py
git commit -m "feat(phase3b): OpenAIProvider + OpenAICompatibleProvider (§35)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: AnthropicProvider

**Files:**
- Create: `core/ai/anthropic_provider.py`
- Modify: `core/ai/__init__.py`
- Create: `tests/unit/test_ai_anthropic_provider.py`

**Verified REST shape (Anthropic, §2):** `POST {base_url}/v1/messages`; headers `x-api-key: <key>`, `anthropic-version: 2023-06-01`, `content-type: application/json` (NOT `Authorization`); body `{model, messages:[{role,content}], system, max_tokens (REQUIRED), stream, tools:[{name,description,input_schema}], temperature}`; response `content[]` blocks (`{type:"text",text}` / `{type:"tool_use",id,name,input}`), `stop_reason`, `usage:{input_tokens,output_tokens}`. `list_models`/health: `GET {base_url}/v1/models` → `{"data":[{"id":...}]}`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_ai_anthropic_provider.py`:

```python
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
    return AnthropicProvider(name="anthropic", base_url="https://api.anthropic.com", api_key_env="ANTHROPIC_API_KEY", default_model="claude-test", http_post=post, http_get=get, **kw)


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_ai_anthropic_provider.py -v`
Expected: FAIL — `ImportError: cannot import name 'AnthropicProvider'`.

- [ ] **Step 3: Write the implementation**

Create `core/ai/anthropic_provider.py`:

```python
"""AnthropicProvider — the AIProvider adapter for the Anthropic Messages API (spec §34/§35).

Wire shape verified 2026-08-11 (§25): POST {base_url}/v1/messages, headers x-api-key +
anthropic-version 2023-06-01 (NOT Authorization), max_tokens REQUIRED, content[] blocks
(text / tool_use), usage.{input_tokens,output_tokens}. Zero new deps (§118): over
core/http_utils (requests, pinned), not the unpinned `anthropic` SDK.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional

from core.ai.provider import (
    AIProvider, AIResponse, AIStreamChunk, ProviderCapabilities, ToolCall, Usage,
)

_DefaultPost = None
_DefaultGet = None


def _default_post() -> Callable[..., Any]:
    global _DefaultPost
    if _DefaultPost is None:
        from core.http_utils import http_post as _DefaultPost  # type: ignore[assignment]
    return _DefaultPost  # type: ignore[return-value]


def _default_get() -> Callable[..., Any]:
    global _DefaultGet
    if _DefaultGet is None:
        from core.http_utils import http_get as _DefaultGet  # type: ignore[assignment]
    return _DefaultGet  # type: ignore[return-value]


def _extract_json(text: str) -> Optional[Any]:
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except (ValueError, TypeError):
                        break
        start = text.find("{", start + 1)
    return None


class AnthropicProvider(AIProvider):
    def __init__(
        self, *, name: str, base_url: str, api_key_env: str, default_model: str = "",
        timeout: int = 60, max_tokens: int = 1024,
        http_post: Optional[Callable[..., Any]] = None, http_get: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.default_model = default_model
        self.timeout = timeout
        self.max_tokens = max_tokens  # REQUIRED by the Anthropic API
        self._http_post = http_post
        self._http_get = http_get
        self._lock = threading.Lock()
        self._last_usage: Usage = Usage()

    def _api_key(self) -> Optional[str]:
        return os.getenv(self.api_key_env)

    def _headers(self) -> Dict[str, str]:
        key = self._api_key()
        if not key:
            return {}
        return {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}

    def _resolve_model(self, model: Optional[str]) -> str:
        return model or self.default_model

    def list_models(self) -> List[str]:
        get = self._http_get or _default_get()
        resp = get(f"{self.base_url}/v1/models", provider=self.name, headers=self._headers(), timeout=self.timeout)
        if resp is None or resp.status_code != 200:
            return []
        data = resp.json() or {}
        return [str(m.get("id")) for m in (data.get("data") or []) if m.get("id")]

    def health_check(self, *, force: bool = False) -> bool:
        get = self._http_get or _default_get()
        resp = get(f"{self.base_url}/v1/models", provider=self.name, headers=self._headers(), timeout=self.timeout)
        return bool(resp is not None and resp.status_code == 200)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(text=True, structured_output=True, tool_calling=True, streaming=True, supported_formats=("json",))

    def generate(self, prompt, *, model=None, system="", tools=None, **opts) -> AIResponse:
        return self._post_messages(prompt, model=model, system=system, parse_json=False)

    def generate_structured(self, prompt, schema, *, model=None, system="", **opts) -> AIResponse:
        instruction = f"{prompt}\n\nRespond with a SINGLE JSON object matching this schema:\n{json.dumps(schema) if schema else ''}"
        return self._post_messages(instruction, model=model, system=system, parse_json=True)

    def tool_call(self, prompt, tools, *, model=None, system="", **opts) -> AIResponse:
        return self._post_messages(prompt, model=model, system=system, parse_tools=True, tools=tools)

    def stream(self, prompt, *, model=None, system="", **opts) -> Iterator[AIStreamChunk]:
        # Streaming is parsed in Task 3-style; Anthropic SSE events are more complex.
        # Phase 3B delivers a best-effort text-delta streamer; full event coverage is Phase 3C if needed.
        key = self._api_key()
        if not key:
            yield AIStreamChunk(text="", done=True)
            return
        post = self._http_post or _default_post()
        payload = self._payload(prompt, model=model, system=system, stream=True)
        resp = post(f"{self.base_url}/v1/messages", provider=self.name, headers=self._headers(), json=payload, timeout=self.timeout, max_retries=0, stream=True)
        if resp is None:
            yield AIStreamChunk(text="", done=True)
            return
        for line in resp.iter_lines():
            if not line:
                continue
            if isinstance(line, bytes):
                line = line.decode("utf-8", "ignore")
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[len("data:"):].strip())
                except (ValueError, TypeError):
                    continue
                if obj.get("type") == "content_block_delta":
                    delta = obj.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        yield AIStreamChunk(text=str(delta.get("text") or ""), done=False)
                elif obj.get("type") == "message_stop":
                    yield AIStreamChunk(text="", done=True)
                    return

    def usage(self) -> Usage:
        with self._lock:
            return self._last_usage

    # --- internals ---
    def _payload(self, prompt: str, *, model: Optional[str], system: str, stream: bool = False, tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self._resolve_model(model),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "stream": stream,
        }
        if str(system or "").strip():
            payload["system"] = system
        if tools:
            payload["tools"] = tools
        return payload

    def _post_messages(self, prompt: str, *, model: Optional[str], system: str, parse_json: bool = False, parse_tools: bool = False, tools: Optional[List[Dict[str, Any]]] = None) -> AIResponse:
        key = self._api_key()
        if not key:
            return AIResponse(text="", error=f"{self.name} api key not set (env {self.api_key_env})")
        post = self._http_post or _default_post()
        payload = self._payload(prompt, model=model, system=system, tools=tools)
        resp = post(f"{self.base_url}/v1/messages", provider=self.name, headers=self._headers(), json=payload, timeout=self.timeout, max_retries=0)
        if resp is None:
            return AIResponse(text="", error=f"{self.name} circuit open")
        if resp.status_code != 200:
            return AIResponse(text="", error=f"{self.name} error: {resp.status_code}")
        data = resp.json() or {}
        text_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        for block in (data.get("content") or []):
            btype = block.get("type")
            if btype == "text":
                text_parts.append(str(block.get("text") or ""))
            elif btype == "tool_use":
                tool_calls.append(ToolCall(id=str(block.get("id", "")), name=str(block.get("name", "")), arguments=block.get("input") if isinstance(block.get("input"), dict) else {}))
        text = "".join(text_parts)
        usage = self._usage_from_anthropic(data)
        with self._lock:
            self._last_usage = usage
        if parse_json:
            parsed = _extract_json(text)
            if parsed is None:
                return AIResponse(text=text, error="structured output parse failed", usage=usage, raw={"json": None}, model=payload.get("model", ""))
            return AIResponse(text=text, tool_calls=[], usage=usage, model=payload.get("model", ""), raw={"json": parsed})
        return AIResponse(text=text, tool_calls=tool_calls, usage=usage, model=payload.get("model", ""), raw=data)

    @staticmethod
    def _usage_from_anthropic(data: Dict[str, Any]) -> Usage:
        u = data.get("usage") or {}
        return Usage(
            prompt_tokens=int(u["input_tokens"]) if u.get("input_tokens") is not None else None,
            completion_tokens=int(u["output_tokens"]) if u.get("output_tokens") is not None else None,
            total_tokens=None,
        )
```

Update `core/ai/__init__.py` — add `from core.ai.anthropic_provider import AnthropicProvider` and `"AnthropicProvider"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_ai_anthropic_provider.py -v`
Expected: PASS (5 assertions).

- [ ] **Step 5: Commit**

```bash
git add core/ai/anthropic_provider.py core/ai/__init__.py tests/unit/test_ai_anthropic_provider.py
git commit -m "feat(phase3b): AnthropicProvider (§35, x-api-key + anthropic-version)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: GeminiProvider

**Files:**
- Create: `core/ai/gemini_provider.py`
- Modify: `core/ai/__init__.py`
- Create: `tests/unit/test_ai_gemini_provider.py`

**Verified REST shape (Gemini, §2):** `POST {base_url}/v1beta/models/{model}:generateContent?key=<key>` (key as query param OR `x-goog-api-key` header); body `{contents:[{role,parts:[{text}]}], systemInstruction, generationConfig:{responseMimeType:"application/json",responseSchema,...,temperature,maxOutputTokens}, tools:[{functionDeclarations:[{name,description,parameters}]}]}`; response `candidates[0].content.parts[0].text`, `usageMetadata:{promptTokenCount,candidatesTokenCount,totalTokenCount}`. `list_models`: `GET {base_url}/v1beta/models` → `{"models":[{"name":"models/gemini-x",...}]}`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_ai_gemini_provider.py`:

```python
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
    return GeminiProvider(name="gemini", base_url="https://generativelanguage.googleapis.com", api_key_env="GEMINI_API_KEY", default_model="gemini-test", http_post=post, http_get=get, **kw)


def test_generate_uses_key_query_and_parses_part(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gem-test")
    post = _fake_post({"candidates": [{"content": {"parts": [{"text": "hi"}]}}], "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1, "totalTokenCount": 3}})
    p = _make(post, _fake_get({"models": []}))
    r = p.generate("hello")
    assert isinstance(r, AIResponse) and r.text == "hi" and r.error is None
    sent = post.sent[0]
    assert sent["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent"
    assert "key=gem-test" in sent["url"]
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_ai_gemini_provider.py -v`
Expected: FAIL — `ImportError: cannot import name 'GeminiProvider'`.

- [ ] **Step 3: Write the implementation**

Create `core/ai/gemini_provider.py`:

```python
"""GeminiProvider — the AIProvider adapter for the Google Gemini generateContent API (§34/§35).

Wire shape verified 2026-08-11 (§25): POST {base_url}/v1beta/models/{model}:generateContent?key=<key>,
contents/parts, systemInstruction, generationConfig.responseMimeType, functionDeclarations,
usageMetadata.{promptTokenCount,candidatesTokenCount}. Zero new deps (§118): over
core/http_utils (requests, pinned), not the unpinned `google-genai` SDK.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional
from urllib.parse import urlencode

from core.ai.provider import (
    AIProvider, AIResponse, AIStreamChunk, ProviderCapabilities, ToolCall, Usage,
)

_DefaultPost = None
_DefaultGet = None


def _default_post() -> Callable[..., Any]:
    global _DefaultPost
    if _DefaultPost is None:
        from core.http_utils import http_post as _DefaultPost  # type: ignore[assignment]
    return _DefaultPost  # type: ignore[return-value]


def _default_get() -> Callable[..., Any]:
    global _DefaultGet
    if _DefaultGet is None:
        from core.http_utils import http_get as _DefaultGet  # type: ignore[assignment]
    return _DefaultGet  # type: ignore[return-value]


def _extract_json(text: str) -> Optional[Any]:
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except (ValueError, TypeError):
                        break
        start = text.find("{", start + 1)
    return None


class GeminiProvider(AIProvider):
    def __init__(
        self, *, name: str, base_url: str, api_key_env: str, default_model: str = "",
        timeout: int = 60,
        http_post: Optional[Callable[..., Any]] = None, http_get: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.default_model = default_model
        self.timeout = timeout
        self._http_post = http_post
        self._http_get = http_get
        self._lock = threading.Lock()
        self._last_usage: Usage = Usage()

    def _api_key(self) -> Optional[str]:
        return os.getenv(self.api_key_env)

    def _resolve_model(self, model: Optional[str]) -> str:
        return model or self.default_model

    def _generate_url(self, model: str) -> str:
        key = self._api_key() or ""
        qs = urlencode({"key": key})
        return f"{self.base_url}/v1beta/models/{model}:generateContent?{qs}"

    def list_models(self) -> List[str]:
        key = self._api_key()
        if not key:
            return []
        get = self._http_get or _default_get()
        url = f"{self.base_url}/v1beta/models?{urlencode({'key': key})}"
        resp = get(url, provider=self.name, headers={}, timeout=self.timeout)
        if resp is None or resp.status_code != 200:
            return []
        data = resp.json() or {}
        out: List[str] = []
        for m in (data.get("models") or []):
            name = str(m.get("name") or "")
            if name.startswith("models/"):
                name = name[len("models/"):]
            if name:
                out.append(name)
        return out

    def health_check(self, *, force: bool = False) -> bool:
        return bool(self._api_key()) and bool(self.list_models())

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(text=True, structured_output=True, tool_calling=True, streaming=True, supported_formats=("json",))

    def generate(self, prompt, *, model=None, system="", tools=None, **opts) -> AIResponse:
        return self._post_generate(prompt, model=model, system=system, parse_json=False, tools=tools)

    def generate_structured(self, prompt, schema, *, model=None, system="", **opts) -> AIResponse:
        return self._post_generate(prompt, model=model, system=system, parse_json=True, response_schema=schema)

    def tool_call(self, prompt, tools, *, model=None, system="", **opts) -> AIResponse:
        return self._post_generate(prompt, model=model, system=system, parse_tools=True, tools=tools)

    def stream(self, prompt, *, model=None, system="", **opts) -> Iterator[AIStreamChunk]:
        # Gemini streaming uses :streamGenerateContent?alt=sse; delivered best-effort in 3B.
        key = self._api_key()
        if not key:
            yield AIStreamChunk(text="", done=True)
            return
        post = self._http_post or _default_post()
        m = self._resolve_model(model)
        url = f"{self.base_url}/v1beta/models/{m}:streamGenerateContent?{urlencode({'alt': 'sse', 'key': key})}"
        payload = self._payload(prompt, system=system)
        resp = post(url, provider=self.name, headers={"content-type": "application/json"}, json=payload, timeout=self.timeout, max_retries=0, stream=True)
        if resp is None:
            yield AIStreamChunk(text="", done=True)
            return
        for line in resp.iter_lines():
            if not line:
                continue
            if isinstance(line, bytes):
                line = line.decode("utf-8", "ignore")
            if not line.startswith("data:"):
                continue
            try:
                obj = json.loads(line[len("data:"):].strip())
            except (ValueError, TypeError):
                continue
            parts = (((obj.get("candidates") or [{}])[0].get("content")) or {}).get("parts") or []
            for part in parts:
                txt = part.get("text")
                if txt:
                    yield AIStreamChunk(text=str(txt), done=False)

    def usage(self) -> Usage:
        with self._lock:
            return self._last_usage

    # --- internals ---
    def _payload(self, prompt: str, *, system: str, response_schema: Optional[Dict[str, Any]] = None, tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        if str(system or "").strip():
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        gen_config: Dict[str, Any] = {}
        if response_schema is not None:
            gen_config["responseMimeType"] = "application/json"
        if gen_config:
            payload["generationConfig"] = gen_config
        if tools:
            payload["tools"] = tools
        return payload

    def _post_generate(self, prompt: str, *, model: Optional[str], system: str, parse_json: bool = False, parse_tools: bool = False, response_schema: Optional[Dict[str, Any]] = None, tools: Optional[List[Dict[str, Any]]] = None) -> AIResponse:
        key = self._api_key()
        if not key:
            return AIResponse(text="", error=f"{self.name} api key not set (env {self.api_key_env})")
        post = self._http_post or _default_post()
        m = self._resolve_model(model)
        url = self._generate_url(m)
        payload = self._payload(prompt, system=system, response_schema=response_schema, tools=tools)
        resp = post(url, provider=self.name, headers={"content-type": "application/json"}, json=payload, timeout=self.timeout, max_retries=0)
        if resp is None:
            return AIResponse(text="", error=f"{self.name} circuit open")
        if resp.status_code != 200:
            return AIResponse(text="", error=f"{self.name} error: {resp.status_code}")
        data = resp.json() or {}
        parts = (((data.get("candidates") or [{}])[0].get("content")) or {}).get("parts") or []
        text_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        for part in parts:
            if "text" in part:
                text_parts.append(str(part.get("text") or ""))
            elif "functionCall" in part:
                fc = part.get("functionCall") or {}
                args = fc.get("args")
                tool_calls.append(ToolCall(id="", name=str(fc.get("name", "")), arguments=args if isinstance(args, dict) else {}))
        text = "".join(text_parts)
        usage = self._usage_from_gemini(data)
        with self._lock:
            self._last_usage = usage
        if parse_json:
            parsed = _extract_json(text)
            if parsed is None:
                return AIResponse(text=text, error="structured output parse failed", usage=usage, raw={"json": None}, model=m)
            return AIResponse(text=text, tool_calls=[], usage=usage, model=m, raw={"json": parsed})
        return AIResponse(text=text, tool_calls=tool_calls, usage=usage, model=m, raw=data)

    @staticmethod
    def _usage_from_gemini(data: Dict[str, Any]) -> Usage:
        u = data.get("usageMetadata") or {}
        return Usage(
            prompt_tokens=int(u["promptTokenCount"]) if u.get("promptTokenCount") is not None else None,
            completion_tokens=int(u["candidatesTokenCount"]) if u.get("candidatesTokenCount") is not None else None,
            total_tokens=int(u["totalTokenCount"]) if u.get("totalTokenCount") is not None else None,
        )
```

Update `core/ai/__init__.py` — add `from core.ai.gemini_provider import GeminiProvider` and `"GeminiProvider"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_ai_gemini_provider.py -v`
Expected: PASS (6 assertions).

- [ ] **Step 5: Commit**

```bash
git add core/ai/gemini_provider.py core/ai/__init__.py tests/unit/test_ai_gemini_provider.py
git commit -m "feat(phase3b): GeminiProvider (§35, generateContent?key=)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: ModelRegistry (§36)

**Files:**
- Create: `core/ai/registry.py`
- Modify: `core/ai/__init__.py`
- Create: `tests/unit/test_ai_model_registry.py`

**Interfaces:**
- Produces: `ModelEntry` (frozen dataclass with the 10 §36 fields), `ModelRegistry` (`register(entry)`, `discover(provider)`, `get(model_id)`, `all_entries()`, `enabled_entries()`, `to_list()`/`from_list()` for `config["ai"]["registry"]` persistence, `update_health(model_id, ok)`).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_ai_model_registry.py`:

```python
from __future__ import annotations

import pytest

from core.ai import ModelEntry, ModelRegistry


def _entry(mid="gpt-x", provider="openai", **kw):
    base = dict(provider=provider, model_id=mid, display_name=mid, capabilities=("text",),
                tool_support=True, structured_output_support=True, context_metadata={},
                health="unknown", enabled=True, last_checked=None)
    base.update(kw)
    return ModelEntry(**base)


def test_model_entry_has_ten_section36_fields():
    e = _entry()
    for f in ("provider", "model_id", "display_name", "capabilities", "tool_support",
              "structured_output_support", "context_metadata", "health", "enabled", "last_checked"):
        assert hasattr(e, f)


def test_register_and_get():
    r = ModelRegistry()
    r.register(_entry("gpt-a"))
    assert r.get("gpt-a") is not None
    assert r.get("gpt-a").provider == "openai"
    assert r.get("missing") is None


def test_register_replaces_same_model_id():
    r = ModelRegistry()
    r.register(_entry("gpt-a", enabled=True))
    r.register(_entry("gpt-a", enabled=False))
    assert r.get("gpt-a").enabled is False


def test_enabled_entries_filters():
    r = ModelRegistry()
    r.register(_entry("gpt-a", enabled=True))
    r.register(_entry("gpt-b", enabled=False))
    assert {e.model_id for e in r.enabled_entries()} == {"gpt-a"}


def test_update_health():
    r = ModelRegistry()
    r.register(_entry("gpt-a", health="unknown"))
    r.update_health("gpt-a", ok=True)
    assert r.get("gpt-a").health == "healthy"
    r.update_health("gpt-a", ok=False)
    assert r.get("gpt-a").health == "unhealthy"


def test_discover_from_provider(monkeypatch):
    class _FakeProvider:
        name = "openai"

        def list_models(self):
            return ["gpt-a", "gpt-b"]

        def capabilities(self):
            from core.ai import ProviderCapabilities
            return ProviderCapabilities()

    r = ModelRegistry()
    added = r.discover(_FakeProvider())
    assert {e.model_id for e in added} == {"gpt-a", "gpt-b"}
    assert r.get("gpt-a").provider == "openai"
    assert r.get("gpt-a").health == "unknown"


def test_persistence_roundtrip():
    r = ModelRegistry()
    r.register(_entry("gpt-a"))
    r.register(_entry("claude-b", provider="anthropic"))
    snap = r.to_list()
    assert isinstance(snap, list) and len(snap) == 2
    r2 = ModelRegistry()
    r2.from_list(snap)
    assert r2.get("gpt-a") is not None and r2.get("claude-b").provider == "anthropic"


def test_from_list_loads_into_config_registry_shape():
    # the config["ai"]["registry"] is a list of dicts; from_list must accept that
    r = ModelRegistry()
    r.from_list([
        {"provider": "openai", "model_id": "gpt-a", "display_name": "gpt-a",
         "capabilities": ["text"], "tool_support": True, "structured_output_support": True,
         "context_metadata": {}, "health": "unknown", "enabled": True, "last_checked": None},
    ])
    assert r.get("gpt-a") is not None and r.get("gpt-a").enabled is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_ai_model_registry.py -v`
Expected: FAIL — `ImportError: cannot import name 'ModelEntry'`.

- [ ] **Step 3: Write the implementation**

Create `core/ai/registry.py`:

```python
"""ModelRegistry (spec §36) — the catalog of AI models across all providers.

§36 fields: provider, model_id, display_name, capabilities, tool_support,
structured_output_support, context_metadata, health, enabled, last_checked.
Discover dynamically when the provider supports it (list_models); allow manual
configuration otherwise. Persisted to config["ai"]["registry"] as a list of dicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class ModelEntry:
    provider: str
    model_id: str
    display_name: str
    capabilities: Tuple[str, ...] = ("text",)
    tool_support: bool = True
    structured_output_support: bool = True
    context_metadata: Dict[str, Any] = field(default_factory=dict)
    health: str = "unknown"  # "unknown" | "healthy" | "unhealthy"
    enabled: bool = True
    last_checked: Optional[str] = None  # ISO timestamp


class ModelRegistry:
    def __init__(self) -> None:
        self._entries: Dict[str, ModelEntry] = {}  # keyed by model_id

    def register(self, entry: ModelEntry) -> None:
        self._entries[entry.model_id] = entry

    def get(self, model_id: str) -> Optional[ModelEntry]:
        return self._entries.get(model_id)

    def all_entries(self) -> List[ModelEntry]:
        return list(self._entries.values())

    def enabled_entries(self) -> List[ModelEntry]:
        return [e for e in self._entries.values() if e.enabled]

    def update_health(self, model_id: str, *, ok: bool) -> None:
        e = self._entries.get(model_id)
        if e is None:
            return
        self._entries[model_id] = ModelEntry(
            **{**asdict(e), "health": "healthy" if ok else "unhealthy"}
        )

    def discover(self, provider: Any) -> List[ModelEntry]:
        """Populate the registry from a provider's list_models(). Returns newly added entries."""
        added: List[ModelEntry] = []
        try:
            caps = provider.capabilities()
        except Exception:
            caps = None
        for mid in provider.list_models() or []:
            if mid in self._entries:
                continue
            entry = ModelEntry(
                provider=getattr(provider, "name", "unknown"),
                model_id=mid,
                display_name=mid,
                capabilities=("text", "structured") if (caps and caps.structured_output) else ("text",),
                tool_support=bool(caps and caps.tool_calling) if caps else True,
                structured_output_support=bool(caps and caps.structured_output) if caps else True,
                context_metadata={},
                health="unknown",
                enabled=True,
                last_checked=None,
            )
            self._entries[mid] = entry
            added.append(entry)
        return added

    def to_list(self) -> List[Dict[str, Any]]:
        return [asdict(e) for e in self._entries.values()]

    def from_list(self, rows: List[Dict[str, Any]]) -> None:
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            mid = row.get("model_id")
            if not mid:
                continue
            caps = row.get("capabilities", ("text",))
            if isinstance(caps, list):
                caps = tuple(caps)
            self._entries[mid] = ModelEntry(
                provider=str(row.get("provider", "")),
                model_id=str(mid),
                display_name=str(row.get("display_name", mid)),
                capabilities=tuple(caps) if isinstance(caps, (list, tuple)) else ("text",),
                tool_support=bool(row.get("tool_support", True)),
                structured_output_support=bool(row.get("structured_output_support", True)),
                context_metadata=dict(row.get("context_metadata") or {}),
                health=str(row.get("health", "unknown")),
                enabled=bool(row.get("enabled", True)),
                last_checked=row.get("last_checked"),
            )
```

Update `core/ai/__init__.py` — add `from core.ai.registry import ModelEntry, ModelRegistry` and `"ModelEntry"`, `"ModelRegistry"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_ai_model_registry.py -v`
Expected: PASS (8 assertions).

- [ ] **Step 5: Commit**

```bash
git add core/ai/registry.py core/ai/__init__.py tests/unit/test_ai_model_registry.py
git commit -m "feat(phase3b): ModelRegistry (§36, discover + manual + persistence)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Control docs + exit gate → Phase 3B PASS (Phase 3 stays IN PROGRESS)

**Files:**
- Modify: `docs/V9_ARCHITECTURE.md` (extend the Phase 3A section with cloud-providers + registry)
- Modify: `docs/V9_IMPLEMENTATION.md` (Phase 3B record; Phase 3 row stays IN PROGRESS)

- [ ] **Step 1: V9_ARCHITECTURE.md** — append to the "Multi-provider AI platform" section a Phase 3B subsection: the 4 cloud providers (OpenAI/Anthropic/Gemini/OpenAICompatible for xAI/DeepSeek/Mistral/OpenRouter) over the governed `core/http_utils` `requests` seam (zero new deps; the installed SDKs deliberately unused, §118/§25); the verified REST shapes pinned by fake-transport tests (§35); keys via `api_key_env` (§106); `ModelRegistry` (§36 — discover + manual + persistence to `config["ai"]["registry"]`).

- [ ] **Step 2: V9_IMPLEMENTATION.md** — add the Phase 3B record (Status PASS at gate; delivered = 4 cloud providers + ModelRegistry + http_utils governor entries + tests; V7 untouched; no new deps; tests executed; exact results; Phase 3 row stays IN PROGRESS — 3C remains).

- [ ] **Step 3: Gate — V7 untouched + no new deps + §4 sweep + regression**

Run:
```bash
git -C "C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium" rev-parse HEAD   # f912a33
git diff 3f17340..HEAD -- requirements.txt requirements-dev.txt pyproject.toml setup.py setup.cfg core/ollama_client.py   # both empty
```
Plus §4 sweep (script from 3A Task 4, run on the new `core/ai/*_provider.py` + `core/ai/registry.py` + the new test files + the two doc files).
Then: `python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf -q`
Expected: V7 = f912a33; deps + ollama_client diffs empty; §4 sweep 0 hits; regression 1676 + (10+5+6+8=29 new) = 1705 passed / 0 failed / exit 0.

- [ ] **Step 4: Commit the gate**

```bash
git add docs/V9_ARCHITECTURE.md docs/V9_IMPLEMENTATION.md
git commit -m "test(phase3b): exit gate PASS — cloud providers + ModelRegistry

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage:** §34 (8-cap AIProvider) → consumed (3A); §35 (OpenAI/Anthropic/Gemini + OpenAI-compatible adapters, no hardcoded names, verify APIs) → Tasks 1-3 + fake-transport pins; §36 (ModelRegistry 10 fields, discover + manual) → Task 4; §106 (keys env, never frontend/logged) → all providers' `_headers` from env; §118/§25 (no new deps) → all over `core/http_utils` (requests pinned); §5 (evolve, reuse 3A patterns); §115/§117/§116 → gate. ✓
**2. Placeholder scan:** no TBD/TODO; `default_model=""` intentional discover-then-select; Anthropic/Gemini `stream()` is best-effort (documented, tested for the non-streaming paths; streaming paths return gracefully on no-key). ✓
**3. Type consistency:** all providers return the 3A `AIResponse`/`AIStreamChunk`/`ToolCall`/`Usage`; `ModelEntry`/`ModelRegistry` use the same `ProviderCapabilities` from 3A; `OpenAICompatibleProvider` subclasses `OpenAIProvider` (the test asserts `isinstance`). ✓
**4. Risk check:** `core/ollama_client.py` and `core/ai/provider.py`/`ollama_provider.py` untouched; `core/http_utils.py` only adds dict keys (semaphores/circuit/last-request derive from `_PROVIDER_LIMITS.keys()`); `core/config.py` untouched (the `ai` block from 3A already has the provider entries + `registry: []`). No existing provider's governor behavior changes (new names are additive). ✓
**5. No-live-API/no-key tests:** every provider test uses `_fake_post`/`_fake_get` seams + `monkeypatch.setenv` for the key; no real network, no committed key. ✓

No issues found. Plan is complete.