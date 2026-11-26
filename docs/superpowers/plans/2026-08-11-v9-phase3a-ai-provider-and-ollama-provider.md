# Phase 3A — AIProvider Interface + OllamaProvider (Evolve) + Config Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce V9's `AIProvider` abstraction (spec §34 — the 8 normalized capabilities `list_models`/`health_check`/`capabilities`/`generate`/`generate_structured`/`tool_call`/`stream`/`usage`) and the first concrete provider — `OllamaProvider`, which **wraps the existing `OllamaClient` singleton without modifying it** (§7 "preserve what works") — plus the `config["ai"]` schema (§35/§36/§37/§38 forward config). All 13 existing Ollama callers and 8 pinned Ollama tests stay green unchanged. Keep the 1651-test baseline green. First of three Phase 3 sub-plans.

**Architecture:** `core/ai/` is a new package. `provider.py` defines the `AIProvider` ABC + frozen value types (`Usage`, `ProviderCapabilities`, `ToolCall`, `AIResponse`, `AIStreamChunk`) + an `UNAVAILABLE` sentinel. `ollama_provider.py` defines `OllamaProvider(AIProvider)` that **wraps** an `OllamaClient` instance (default the existing `ollama` singleton — shared caches/state, no second client). It delegates `list_models`/`health_check`/`generate`-dict to the wrapped client and implements the new typed capabilities (`generate`→`AIResponse`, `generate_structured` with Ollama JSON mode, `stream` parsing Ollama NDJSON, `tool_call` with Ollama `tools`, `usage`, `capabilities`) on top. `core/ollama_client.py` is **NOT modified** — the `ollama` singleton stays exactly as `OllamaClient()`. `core/config.py` + `config.example.json` gain `DEFAULT_CONFIG["ai"]` (providers/registry/assignment/router/fallback/all_unavailable_no_trade). New providers (OpenAI/Anthropic/Gemini/compatible) land in 3B; assignment/router/fallback/API routes land in 3C.

**Tech Stack:** Python 3.14.3, stdlib (`abc`, `dataclasses`, `json`, `typing`), `core/http_utils` (requests, already pinned — OllamaProvider reuses the wrapped client which already uses it). **No new dependencies** (§118 — `core/ai/` imports stdlib + `core.ollama_client` + `core.http_utils` only; `requirements*.txt` unchanged).

## Global Constraints

(Copied verbatim from `prompt_to_follow.md`; every task's requirements implicitly include these.)

- **§34:** `AIProvider` with normalized capabilities `list_models()` / `health_check()` / `capabilities()` / `generate()` / `generate_structured()` / `tool_call()` / `stream()` / `usage()`.
- **§7/§34:** "Audit existing Ollama/Ollama Cloud integration first. Preserve what works. Refactor toward AIProvider." — `core/ollama_client.py` is NOT modified in 3A; `OllamaProvider` wraps the existing `ollama` singleton.
- **§35:** do NOT hardcode current model names; verify current official APIs at implementation time. (3A touches only Ollama, whose API V7 already verified; new-provider defaults are `""`.)
- **§106:** no committed keys; keys via env (`api_key_env`) / gitignored `config.json`; never to frontend; never logged. (OllamaProvider reuses the existing key-omitting `_cache_identity`.)
- **§4:** no forbidden success language ("mostly complete", "should work", "99%", "mock for now", etc.).
- **§5:** no rewrite — evolve V7/V9. `OllamaProvider` adapts, does not duplicate, `OllamaClient`.
- **§115:** no skip/xfail/`assert True`/delete failing test/weaken assertion.
- **§117:** V7 at `C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium` (HEAD `f912a33`, 109 dirty) is READ-ONLY — never modify.
- **§116:** no remote push (no remote).
- **§118/§25:** no new production deps; pin deps. 3A adds none.
- **Phase 2 baseline:** 1651 passed / 0 failed / exit 0 (commit `07f638f`). 3A keeps this green and adds the `core/ai/` tests only.
- **Pinned Ollama tests (must stay green, untouched):** `tests/unit/test_ollama_chat_endpoint.py`, `tests/unit/test_ollama_cloud_host.py`, `tests/unit/test_ollama_set_model_cloud.py`, `tests/unit/test_ollama_budget_exempt.py`, `tests/unit/test_premarket_ollama_chat_endpoint.py`, `tests/test_ollama_decision.py`, `tests/unit/test_pre_earnings_pulse.py`, `tests/conftest.py` autouse `mock_external_deps` (patches `OllamaClient.check_availability → False`).

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `core/ai/__init__.py` (NEW) | Package exports. | 1, 2, 3 |
| `core/ai/provider.py` (NEW) | `AIProvider` ABC + frozen value types + `UNAVAILABLE` sentinel + `HttpCallable` seam. | 1 |
| `core/ai/ollama_provider.py` (NEW) | `OllamaProvider(AIProvider)` wrapping `OllamaClient`. | 2 |
| `core/config.py` (MODIFY, additive) | `DEFAULT_CONFIG["ai"]` schema. | 3 |
| `config.example.json` (MODIFY, additive) | `ai` block mirror. | 3 |
| `tests/unit/test_ai_provider_contract.py` (NEW) | Value types + ABC contract tests. | 1 |
| `tests/unit/test_ai_ollama_provider.py` (NEW) | `OllamaProvider` behavior (fake client). | 2 |
| `tests/unit/test_ai_config_schema.py` (NEW) | `DEFAULT_CONFIG["ai"]` present + shape + Ollama provider reads existing `ollama` block. | 3 |
| `docs/V9_ARCHITECTURE.md` (MODIFY, additive) | "Multi-provider AI platform (Phase 3A)" section. | 4 |
| `docs/V9_IMPLEMENTATION.md` (MODIFY, additive) | Phase 3A record; Phase 3 row stays IN PROGRESS. | 4 |

**NOT modified:** `core/ollama_client.py`, `core/ollama_decision.py`, any of the 13 call sites, `requirements*.txt`.

---

### Task 1: `AIProvider` ABC + value types + UNAVAILABLE sentinel

**Files:**
- Create: `core/ai/__init__.py`, `core/ai/provider.py`
- Create: `tests/unit/test_ai_provider_contract.py`

**Interfaces:**
- Produces: `AIProvider` (ABC), `Usage`, `ProviderCapabilities`, `ToolCall`, `AIResponse`, `AIStreamChunk`, `UNAVAILABLE`, `HttpCallable`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_ai_provider_contract.py`:

```python
from __future__ import annotations

import dataclasses
from typing import Iterator

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
        "list_models", "health_check", "capabilities", "generate",
        "generate_structured", "tool_call", "stream", "usage",
    ):
        assert cap in abstract, f"AIProvider must declare capability {cap}"
    with pytest.raises(TypeError):
        AIProvider()  # type: ignore[abstract]


def test_concrete_provider_must_implement_all():
    class Half(AIProvider):
        name = "half"
        def list_models(self): return []
        def health_check(self, *, force=False): return False
        def capabilities(self): return ProviderCapabilities()
        def generate(self, prompt, *, model=None, system="", tools=None, **opts):
            return AIResponse(text="", tool_calls=[], usage=Usage(0,0,0), model="", raw={}, error=None)
    with pytest.raises(TypeError):
        Half()  # missing generate_structured/tool_call/stream/usage
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_ai_provider_contract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.ai.provider'`.

- [ ] **Step 3: Write the implementation**

Create `core/ai/provider.py`:

```python
"""V9 Multi-Provider AI Platform — the AIProvider abstraction (spec §34).

Domain logic talks to `AIProvider`, never to a concrete vendor client, so V9 stays
vendor-independent (§17). A provider exposes the 8 normalized capabilities:
list_models / health_check / capabilities / generate / generate_structured /
tool_call / stream / usage.

No model names are hardcoded (§35): models come from config + `list_models()`.
API keys are read from env vars named by config (`api_key_env`), never committed,
never sent to the frontend, never logged (§106).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Protocol


class HttpCallable(Protocol):
    """Minimal seam for an injectable HTTP transport (testability without keys/network)."""

    def __call__(self, url: str, *, provider: Optional[str] = None, **kwargs: Any) -> Optional[Any]:
        ...


@dataclass(frozen=True)
class Usage:
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


@dataclass(frozen=True)
class ProviderCapabilities:
    text: bool = True
    structured_output: bool = True
    tool_calling: bool = True
    streaming: bool = True
    # human-readable list of supported structured-output formats, e.g. ["json"]
    supported_formats: tuple = ("json",)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AIResponse:
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=lambda: Usage())
    model: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def is_error(self) -> bool:
        return self.error is not None


@dataclass(frozen=True)
class AIStreamChunk:
    text: str = ""
    done: bool = False


# GOLD-014 sentinel: every provider in the chain was unavailable. Callers that gate
# autonomous trading on AI must treat this as "no new autonomous trade".
UNAVAILABLE = AIResponse(text="", error="all providers unavailable")


class AIProvider(ABC):
    """Abstract multi-provider AI interface (spec §34)."""

    name: str = ""

    @abstractmethod
    def list_models(self) -> List[str]:
        """Discover model ids the provider exposes (dynamic; no hardcoded names, §35)."""

    @abstractmethod
    def health_check(self, *, force: bool = False) -> bool:
        """Cheap reachability probe; True iff the provider can serve a request now."""

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """What this provider supports (text/structured/tool/stream)."""

    @abstractmethod
    def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        **opts: Any,
    ) -> AIResponse:
        """Free-text generation → typed AIResponse."""

    @abstractmethod
    def generate_structured(
        self,
        prompt: str,
        schema: Dict[str, Any],
        *,
        model: Optional[str] = None,
        system: str = "",
        **opts: Any,
    ) -> AIResponse:
        """Structured (JSON-conforming to `schema`) generation → typed AIResponse."""

    @abstractmethod
    def tool_call(
        self,
        prompt: str,
        tools: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        system: str = "",
        **opts: Any,
    ) -> AIResponse:
        """Generation that may return tool_calls (the provider round-trip only;
        the §39 tool registry is Phase 4)."""

    @abstractmethod
    def stream(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        system: str = "",
        **opts: Any,
    ) -> Iterator[AIStreamChunk]:
        """Streaming generation, yielding incremental AIStreamChunks."""

    @abstractmethod
    def usage(self) -> Usage:
        """Token usage of the most recent non-stream call (best-effort)."""
```

Create `core/ai/__init__.py`:

```python
"""V9 Multi-Provider AI Platform (spec §34/§35)."""
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

__all__ = [
    "AIProvider",
    "AIResponse",
    "AIStreamChunk",
    "HttpCallable",
    "ProviderCapabilities",
    "ToolCall",
    "UNAVAILABLE",
    "Usage",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_ai_provider_contract.py -v`
Expected: PASS (8 assertions).

- [ ] **Step 5: Commit**

```bash
git add core/ai/__init__.py core/ai/provider.py tests/unit/test_ai_provider_contract.py
git commit -m "feat(phase3a): AIProvider abstraction + value types (§34)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: `OllamaProvider` wrapping the existing `OllamaClient`

**Files:**
- Create: `core/ai/ollama_provider.py`
- Modify: `core/ai/__init__.py` (export `OllamaProvider`, `get_ollama_provider`)
- Create: `tests/unit/test_ai_ollama_provider.py`

**Interfaces:**
- Consumes: `core.ollama_client.OllamaClient` (the existing singleton `ollama`), `core.http_utils.http_post`, `core.ai.provider.*`.
- Produces: `OllamaProvider(AIProvider)` with `__init__(self, client=None, *, http_post=None)`, `name="ollama"`; methods `list_models`/`health_check`/`capabilities`/`generate`/`generate_structured`/`tool_call`/`stream`/`usage`; factory `get_ollama_provider()` returning a provider wrapping the `ollama` singleton.

**Design notes:**
- `OllamaProvider` wraps an `OllamaClient` (default `core.ollama_client.ollama` — the shared singleton, so caches/state are shared and `core/ollama_client.py` is untouched).
- `generate(prompt, *, model, system, tools, **opts)` calls the wrapped client's `generate(prompt, model, system, ui_call, timeout_override)` (which returns the legacy dict `{response, _meta}` or `{error}`) and lifts it to `AIResponse`. The legacy dict's `response` → `AIResponse.text`; `_meta.model` → `AIResponse.model`; `error` → `AIResponse.error`.
- `generate_structured(prompt, schema, *, model, system, **opts)` POSTs `/api/chat` directly (via the injectable `http_post` seam, default `core.http_utils.http_post`) with `format: "json"` + the in-prompt JSON instruction + the schema summary, `stream: False`, then parses `message.content` as JSON (brace-match fallback) into `AIResponse.raw["json"]` and `text`. This does NOT touch `OllamaClient.generate` (the pinned `/api/chat`-only path stays in-prompt-only for the 13 callers).
- `stream(prompt, *, model, system, **opts)` POSTs `/api/chat` with `stream: True` and parses Ollama's **NDJSON** (one JSON object per line; each has `message.content`; the last has `done: true`) into `AIStreamChunk(text=delta, done=done)`.
- `tool_call(prompt, tools, *, model, system, **opts)` POSTs `/api/chat` with `tools` + `stream: False`, parses `message.tool_calls` (`[{function:{name,arguments(JSON string)}}]`) into `AIResponse.tool_calls`.
- `usage()` returns the `Usage` captured from the last non-stream call's Ollama response (`prompt_eval_count`/`eval_count`/or `eval_count` total).
- `capabilities()` returns `ProviderCapabilities(text=True, structured_output=True, tool_calling=True, streaming=True, supported_formats=("json",))`.
- `list_models()` → `client.get_models()`; `health_check()` → `client.check_availability(force=force)`.
- A small `_extract_json_object(text)` helper (brace-matching) — consistent with the existing one in `ollama_decision.py` (not imported, to keep `core/ai` decoupled from the decision layer; this is a local copy for the provider, justified by the §5 no-duplicate rule being about *functionality*, not a 10-line JSON extractor — but to avoid duplication, **import** it from `core.ollama_decision` instead. Use `from core.ollama_decision import _extract_json_object`. Wait — that's a private name. Decision: copy a minimal local `_extract_json` to keep `core/ai` self-contained and avoid importing a private symbol across module boundaries. The plan uses a local copy.)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_ai_ollama_provider.py`:

```python
from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from core.ai import AIResponse, AIStreamChunk, OllamaProvider, ToolCall, Usage, get_ollama_provider


class _FakeClient:
    """Stand-in for core.ollama_client.ollama — records calls, returns canned dicts."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._models = ["gemma3:12b", "gpt-oss:120b"]
        self._available = True
        self._generate_result: Dict[str, Any] = {
            "response": "ok text",
            "_meta": {"model": "gemma3:12b", "generated_at": "2026-08-11T00:00:00+00:00",
                      "ui_call": False, "timeout_sec": 120},
        }

    def get_models(self, *, force: bool = False) -> List[str]:
        return list(self._models)

    def check_availability(self, *, force: bool = False) -> bool:
        return self._available

    def generate(self, prompt, model=None, system="", ui_call=False, timeout_override=None):
        self.calls.append({"prompt": prompt, "model": model, "system": system,
                            "ui_call": ui_call, "timeout_override": timeout_override})
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
            # NDJSON streaming: two content deltas + a final done object
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
    # the request used format=json (Ollama JSON mode)
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
    payload = {"message": {"tool_calls": [
        {"function": {"name": "get_price", "arguments": '{"ticker": "AAPL"}'}},
    ]}}
    post = _fake_http_post(payload)
    r = OllamaProvider(client=_FakeClient(), http_post=post).tool_call(
        "price?", [{"type": "function", "function": {"name": "get_price"}}])
    assert r.error is None
    assert len(r.tool_calls) == 1
    assert isinstance(r.tool_calls[0], ToolCall)
    assert r.tool_calls[0].name == "get_price"
    assert r.tool_calls[0].arguments == {"ticker": "AAPL"}
    assert post.sent[0]["kwargs"]["json"]["tools"] == [
        {"type": "function", "function": {"name": "get_price"}}]


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_ai_ollama_provider.py -v`
Expected: FAIL — `ImportError: cannot import name 'OllamaProvider'`.

- [ ] **Step 3: Write the implementation**

Create `core/ai/ollama_provider.py`:

```python
"""OllamaProvider — the AIProvider adapter for Ollama Local + Ollama Cloud (spec §34/§35).

This wraps the existing `core.ollama_client.OllamaClient` singleton (`ollama`) WITHOUT
modifying it (§7 "preserve what works"). The legacy `ollama.generate()` dict contract
(`{response, _meta}` / `{error}`) stays intact for the 13 existing callers; this provider
adds the typed §34 capabilities on top.

Local-vs-cloud routing, Bearer auth, TTL caches, and the `/api/chat`-only endpoint are all
owned by the wrapped `OllamaClient` — reused, not duplicated (§5).
"""
from __future__ import annotations

import json
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional

from core.ai.provider import (
    AIProvider, AIResponse, AIStreamChunk, ProviderCapabilities, ToolCall, Usage,
)

# Default transport seam: the governed http_utils POST (circuit breaker / rate limit /
# budget exemption keyed by provider="ollama"). Injectable for tests.
_DefaultPost = None  # resolved lazily to avoid an import cycle at module import


def _default_http_post():
    global _DefaultPost
    if _DefaultPost is None:
        from core.http_utils import http_post as _DefaultPost  # type: ignore[assignment]
    return _DefaultPost


def _extract_json(text: str) -> Optional[Any]:
    """Brace-matching JSON object extractor (robust to surrounding prose)."""
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


class OllamaProvider(AIProvider):
    name = "ollama"

    def __init__(
        self,
        client: Optional[Any] = None,
        *,
        http_post: Optional[Callable[..., Any]] = None,
    ) -> None:
        if client is None:
            from core.ollama_client import ollama as client  # type: ignore[assignment]
        self._client = client
        self._http_post = http_post
        self._lock = threading.Lock()
        self._last_usage: Usage = Usage()

    # --- delegated (preserve existing behavior) ---
    def list_models(self) -> List[str]:
        return list(self._client.get_models())

    def health_check(self, *, force: bool = False) -> bool:
        return bool(self._client.check_availability(force=force))

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            text=True, structured_output=True, tool_calling=True, streaming=True,
            supported_formats=("json",),
        )

    # --- typed generate (lifts the legacy dict to AIResponse) ---
    def generate(
        self, prompt: str, *, model: Optional[str] = None, system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None, **opts: Any,
    ) -> AIResponse:
        ui_call = bool(opts.get("ui_call", False))
        timeout_override = opts.get("timeout_override")
        data = self._client.generate(
            prompt, model=model, system=system, ui_call=ui_call, timeout_override=timeout_override)
        if isinstance(data, dict) and data.get("error"):
            return AIResponse(text="", error=str(data["error"]))
        text = str(data.get("response", "")) if isinstance(data, dict) else ""
        meta = (data or {}).get("_meta", {}) if isinstance(data, dict) else {}
        usage = self._usage_from_ollama(data) if isinstance(data, dict) else Usage()
        with self._lock:
            self._last_usage = usage
        return AIResponse(text=text, tool_calls=[], usage=usage,
                          model=str(meta.get("model", "")), raw=data if isinstance(data, dict) else {})

    # --- structured (JSON mode) — direct /api/chat, does not touch OllamaClient.generate ---
    def generate_structured(
        self, prompt: str, schema: Dict[str, Any], *, model: Optional[str] = None,
        system: str = "", **opts: Any,
    ) -> AIResponse:
        model = model or self._client.get_best_model()
        schema_hint = json.dumps(schema) if schema else ""
        instruction = (
            f"{prompt}\n\nRespond with a SINGLE JSON object matching this schema "
            f"(extra keys allowed, no prose):\n{schema_hint}"
        )
        messages: List[Dict[str, Any]] = []
        if str(system or "").strip():
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": instruction})
        payload = {"model": model, "messages": messages, "stream": False, "format": "json"}
        return self._post_chat(payload, parse_json=True)

    def tool_call(
        self, prompt: str, tools: List[Dict[str, Any]], *, model: Optional[str] = None,
        system: str = "", **opts: Any,
    ) -> AIResponse:
        model = model or self._client.get_best_model()
        messages: List[Dict[str, Any]] = []
        if str(system or "").strip():
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": model, "messages": messages, "stream": False, "tools": tools}
        return self._post_chat(payload, parse_tools=True)

    def stream(
        self, prompt: str, *, model: Optional[str] = None, system: str = "", **opts: Any,
    ) -> Iterator[AIStreamChunk]:
        model = model or self._client.get_best_model()
        messages: List[Dict[str, Any]] = []
        if str(system or "").strip():
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": model, "messages": messages, "stream": True}
        post = self._http_post or _default_http_post()
        resp = post(
            self._client._api_url("chat"),
            provider="ollama",
            json=payload,
            headers=self._client._headers(),
            timeout=(5, opts.get("timeout_override") or 120),
            max_retries=0,
            stream=True,
        )
        if resp is None:
            yield AIStreamChunk(text="", done=True)
            return
        for line in resp.iter_lines():
            if not line:
                continue
            if isinstance(line, bytes):
                line = line.decode("utf-8", "ignore")
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                continue
            content = str((obj.get("message") or {}).get("content") or "")
            done = bool(obj.get("done"))
            yield AIStreamChunk(text=content, done=done)
            if done:
                return

    def usage(self) -> Usage:
        with self._lock:
            return self._last_usage

    # --- internals ---
    def _post_chat(self, payload: Dict[str, Any], *, parse_json: bool = False,
                   parse_tools: bool = False) -> AIResponse:
        post = self._http_post or _default_http_post()
        resp = post(
            self._client._api_url("chat"),
            provider="ollama",
            json=payload,
            headers=self._client._headers(),
            timeout=(5, 120),
            max_retries=0,
        )
        if resp is None:
            return AIResponse(text="", error="ollama circuit open")
        if resp.status_code != 200:
            return AIResponse(text="", error=f"ollama error: {resp.status_code}")
        data = resp.json()
        msg = data.get("message") or {}
        text = str(msg.get("content") or "")
        tool_calls: List[ToolCall] = []
        parsed: Optional[Any] = None
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
                tool_calls.append(ToolCall(id=str(tc.get("id", "")), name=str(fn.get("name", "")),
                                            arguments=args if isinstance(args, dict) else {}))
        if parse_json:
            parsed = _extract_json(text)
            if parsed is None:
                return AIResponse(text=text, error="structured output parse failed",
                                  raw={"json": None}, model=payload.get("model", ""))
        usage = self._usage_from_ollama(data)
        with self._lock:
            self._last_usage = usage
        return AIResponse(
            text=text, tool_calls=tool_calls, usage=usage,
            model=payload.get("model", ""),
            raw={"json": parsed} if parse_json else ({"tool_calls": msg.get("tool_calls")} if parse_tools else data),
        )

    @staticmethod
    def _usage_from_ollama(data: Dict[str, Any]) -> Usage:
        prompt = data.get("prompt_eval_count") or data.get("prompt_eval")
        completion = data.get("eval_count")
        total = data.get("eval_count")
        if prompt is None and data.get("count") is not None:
            total = data.get("count")
        return Usage(
            prompt_tokens=int(prompt) if prompt is not None else None,
            completion_tokens=int(completion) if completion is not None else None,
            total_tokens=int(total) if total is not None else None,
        )


_OLLAMA_PROVIDER: Optional[OllamaProvider] = None
_PROVIDER_LOCK = threading.Lock()


def get_ollama_provider() -> OllamaProvider:
    """Return a cached OllamaProvider wrapping the `ollama` singleton (§7 — shared state)."""
    global _OLLAMA_PROVIDER
    if _OLLAMA_PROVIDER is None:
        with _PROVIDER_LOCK:
            if _OLLAMA_PROVIDER is None:
                _OLLAMA_PROVIDER = OllamaProvider()
    return _OLLAMA_PROVIDER
```

Extend `core/ai/__init__.py` exports — replace the `__init__.py` content with:

```python
"""V9 Multi-Provider AI Platform (spec §34/§35)."""
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
from core.ai.ollama_provider import OllamaProvider, get_ollama_provider

__all__ = [
    "AIProvider",
    "AIResponse",
    "AIStreamChunk",
    "HttpCallable",
    "OllamaProvider",
    "ProviderCapabilities",
    "ToolCall",
    "UNAVAILABLE",
    "Usage",
    "get_ollama_provider",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_ai_ollama_provider.py -v`
Expected: PASS (10 assertions).

- [ ] **Step 5: Run the pinned Ollama tests (must stay green, untouched)**

Run: `python -m pytest tests/unit/test_ollama_chat_endpoint.py tests/unit/test_ollama_cloud_host.py tests/unit/test_ollama_set_model_cloud.py tests/unit/test_ollama_budget_exempt.py tests/unit/test_premarket_ollama_chat_endpoint.py tests/test_ollama_decision.py tests/unit/test_pre_earnings_pulse.py -q`
Expected: PASS (all pre-existing; `core/ollama_client.py` unchanged).

- [ ] **Step 6: Commit**

```bash
git add core/ai/ollama_provider.py core/ai/__init__.py tests/unit/test_ai_ollama_provider.py
git commit -m "feat(phase3a): OllamaProvider wraps OllamaClient (§34/§7, no rewrite)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: `config["ai"]` schema (§35/§36/§37/§38 forward config)

**Files:**
- Modify: `core/config.py` (add `DEFAULT_CONFIG["ai"]`)
- Modify: `config.example.json` (add `ai` block)
- Create: `tests/unit/test_ai_config_schema.py`

**Interfaces:**
- Produces: `DEFAULT_CONFIG["ai"]` with `providers` (8 entries: ollama/openai/anthropic/gemini/xai/deepseek/mistral/openrouter, each `{enabled,type,base_url,api_key_env,default_model,timeout}`), `registry: []`, `assignment` (10 roles + `all`), `router` (triage/analysis/critic), `fallback` (`enabled`, `chain`), `all_unavailable_no_trade: true`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_ai_config_schema.py`:

```python
from core.config import DEFAULT_CONFIG


def test_default_config_has_ai_block():
    assert isinstance(DEFAULT_CONFIG.get("ai"), dict)


def test_ai_providers_block_has_eight_providers():
    providers = DEFAULT_CONFIG["ai"]["providers"]
    expected = {"ollama", "openai", "anthropic", "gemini", "xai", "deepseek", "mistral", "openrouter"}
    assert expected <= set(providers.keys())


def test_ai_provider_entries_have_required_shape():
    for name, entry in DEFAULT_CONFIG["ai"]["providers"].items():
        assert "enabled" in entry and isinstance(entry["enabled"], bool)
        assert "type" in entry
        if name != "ollama":
            assert "api_key_env" in entry and entry["api_key_env"]
            assert "default_model" in entry  # "" means discover-then-select (§35)


def test_ai_assignment_has_ten_roles_plus_all():
    a = DEFAULT_CONFIG["ai"]["assignment"]
    roles = {"market_analyst", "pre_earnings_analyst", "news_analyst", "quant_analyst",
             "portfolio_manager", "risk_critic", "research_scientist", "coding_agent",
             "code_reviewer", "security_reviewer"}
    assert roles <= set(a.keys())
    assert "all" in a


def test_ai_router_has_three_tiers():
    r = DEFAULT_CONFIG["ai"]["router"]
    assert {"triage", "analysis", "critic"} <= set(r.keys())


def test_ai_fallback_and_no_trade_flags():
    ai = DEFAULT_CONFIG["ai"]
    assert "fallback" in ai and "enabled" in ai["fallback"] and "chain" in ai["fallback"]
    assert ai.get("all_unavailable_no_trade") is True  # GOLD-014


def test_config_example_json_has_ai_block():
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / "config.example.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert isinstance(data.get("ai"), dict)
    assert "providers" in data["ai"] and "ollama" in data["ai"]["providers"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_ai_config_schema.py -v`
Expected: FAIL — `DEFAULT_CONFIG` has no `"ai"` key.

- [ ] **Step 3: Write the implementation**

In `core/config.py`, locate `DEFAULT_CONFIG` (the dict literal; the audit found `DEFAULT_CONFIG["ollama"]` at `core/config.py:773-797`). Add the `"ai"` key alongside `"ollama"` (additive — do not alter the existing `"ollama"` block). The value:

```python
    "ai": {
        "providers": {
            "ollama":     {"enabled": True,  "type": "ollama"},
            "openai":     {"enabled": False, "type": "openai", "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "default_model": "", "timeout": 60},
            "anthropic":  {"enabled": False, "type": "anthropic", "base_url": "https://api.anthropic.com", "api_key_env": "ANTHROPIC_API_KEY", "default_model": "", "timeout": 60},
            "gemini":     {"enabled": False, "type": "gemini", "base_url": "https://generativelanguage.googleapis.com", "api_key_env": "GEMINI_API_KEY", "default_model": "", "timeout": 60},
            "xai":        {"enabled": False, "type": "openai_compatible", "base_url": "https://api.x.ai/v1", "api_key_env": "XAI_API_KEY", "default_model": "", "timeout": 60},
            "deepseek":   {"enabled": False, "type": "openai_compatible", "base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY", "default_model": "", "timeout": 60},
            "mistral":    {"enabled": False, "type": "openai_compatible", "base_url": "https://api.mistral.ai/v1", "api_key_env": "MISTRAL_API_KEY", "default_model": "", "timeout": 60},
            "openrouter": {"enabled": False, "type": "openai_compatible", "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY", "default_model": "", "timeout": 60},
        },
        "registry": [],
        "assignment": {
            "all": None,
            "market_analyst": None, "pre_earnings_analyst": None, "news_analyst": None,
            "quant_analyst": None, "portfolio_manager": None, "risk_critic": None,
            "research_scientist": None, "coding_agent": None, "code_reviewer": None,
            "security_reviewer": None,
        },
        "router": {
            "triage":   {"model": None},
            "analysis": {"model": None},
            "critic":   {"model": None},
        },
        "fallback": {"enabled": True, "chain": []},
        "all_unavailable_no_trade": True,
    },
```

In `config.example.json`, add the same `"ai"` block (mirror) alongside the existing `"ollama"` block. (Keys identical to `DEFAULT_CONFIG["ai"]`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_ai_config_schema.py -v`
Expected: PASS (7 assertions).

- [ ] **Step 5: Run the full regression to confirm no config breakage**

Run: `python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf -q`
Expected: 1651 + new (8 + 10 + 7 = 25 new) passed / 0 failed / exit 0.

- [ ] **Step 6: Commit**

```bash
git add core/config.py config.example.json tests/unit/test_ai_config_schema.py
git commit -m "feat(phase3a): config['ai'] schema (§35/§36/§37/§38, GOLD-014)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Control docs + exit gate → Phase 3A PASS (Phase 3 stays IN PROGRESS)

**Files:**
- Modify: `docs/V9_ARCHITECTURE.md` (additive "Multi-provider AI platform (Phase 3A)" section)
- Modify: `docs/V9_IMPLEMENTATION.md` (Phase 3A record; Phase 3 row → IN PROGRESS)

- [ ] **Step 1: V9_ARCHITECTURE.md** — add a "Multi-provider AI platform (Phase 3A)" section after the open-source boundary section: V9 domain code talks to `AIProvider` (§34, 8 capabilities), never a vendor client (§17 vendor-independence). `OllamaProvider` wraps the existing `OllamaClient` singleton (§7 — `core/ollama_client.py` unchanged, 13 callers + 8 pinned tests preserved). `config["ai"]` is the forward schema (providers/registry/assignment/router/fallback). New cloud providers (3B), assignment/router/fallback/API (3C). No hardcoded model names (§35); keys via env (§106). Zero new deps.

- [ ] **Step 2: V9_IMPLEMENTATION.md** — add the Phase 3A record (Status PASS at gate; delivered = `core/ai/` package: `provider.py` ABC + value types + `UNAVAILABLE`; `ollama_provider.py` `OllamaProvider` wrapping `OllamaClient`; `config["ai"]` schema; tests; files; V7 untouched; no new deps; tests executed; exact results; Phase 3 row stays IN PROGRESS — 3B/3C remain).

- [ ] **Step 3: Gate — V7 untouched + no new deps + sweeps + regression**

Run:
```bash
git -C "C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium" rev-parse HEAD   # f912a33
git diff 07f638f..HEAD -- requirements.txt requirements-dev.txt pyproject.toml setup.py setup.cfg   # empty
python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf -q   # 1651 + 25 new, 0 failed
```
Expected: V7 = f912a33; deps diff empty; regression 1676 passed / 0 failed / exit 0.

- [ ] **Step 4: Commit the gate**

```bash
git add docs/V9_ARCHITECTURE.md docs/V9_IMPLEMENTATION.md
git commit -m "test(phase3a): exit gate PASS — AIProvider + OllamaProvider

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage:** §34 (8-cap AIProvider) → Task 1; §7/§34 (preserve Ollama, refactor toward AIProvider) → Task 2 (wrap, no modify); §35 (no hardcoded names) → config defaults `""`; §36/§37/§38 forward config → Task 3; §106 (keys env) → config `api_key_env`; §4/§5/§115/§117/§118/§25 → gate. ✓
**2. Placeholder scan:** no TBD/TODO; `default_model: ""` is intentional discover-then-select (documented), not a placeholder. ✓
**3. Type consistency:** `AIResponse`/`Usage`/`ToolCall`/`AIStreamChunk`/`ProviderCapabilities` defined in Task 1, used identically in Task 2 + tests. `OllamaProvider.__init__(client, *, http_post)` matches the test's `_FakeClient`/`_fake_http_post`. `get_ollama_provider()` cached singleton matches `test_get_ollama_provider_wraps_singleton`. ✓
**4. Risk check:** `core/ollama_client.py` is NOT modified (Task 2 wraps, never rewrites) — the 8 pinned Ollama tests run unchanged at Task 2 Step 5. The `stream`/`generate_structured`/`tool_call` direct `/api/chat` paths are NEW provider methods, not changes to the legacy `generate`. `config["ollama"]` is untouched; `config["ai"]` is additive. ✓
**5. Deviation from design (strict improvement):** the design said "the `ollama` singleton becomes a shim delegating to OllamaProvider"; the plan instead **wraps** the singleton (`OllamaProvider(client=ollama)`) and leaves `core/ollama_client.py` fully untouched — lower risk (zero changes to the 13 callers' import target + the 8 pinned tests' patch targets), same §7/§34 outcome. Recorded here.

No issues found. Plan is complete.