"""AnthropicProvider — the AIProvider adapter for the Anthropic Messages API (spec §34/§35).

Wire shape verified 2026-08-11 (§25): POST {base_url}/v1/messages, headers x-api-key +
anthropic-version 2023-06-01 (NOT Authorization), max_tokens REQUIRED, content[] blocks
(text / tool_use), usage.{input_tokens,output_tokens}. Streaming SSE events
message_start/content_block_delta(text_delta)/message_stop. Zero new deps (§118): over
core/http_utils (requests, pinned), not the unpinned `anthropic` SDK.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional

from core.ai.provider import (
    AIProvider,
    AIResponse,
    AIStreamChunk,
    ProviderCapabilities,
    ToolCall,
    Usage,
    call_timeout,
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
        self,
        *,
        name: str,
        base_url: str,
        api_key_env: str,
        default_model: str = "",
        timeout: int = 60,
        max_tokens: int = 1024,
        http_post: Optional[Callable[..., Any]] = None,
        http_get: Optional[Callable[..., Any]] = None,
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
        return ProviderCapabilities(
            text=True, structured_output=True, tool_calling=True, streaming=True, supported_formats=("json",)
        )

    def generate(self, prompt, *, model=None, system="", tools=None, **opts) -> AIResponse:
        return self._post_messages(prompt, model=model, system=system, parse_json=False,
                                   timeout=call_timeout(self.timeout, opts))

    def generate_structured(self, prompt, schema, *, model=None, system="", **opts) -> AIResponse:
        instruction = f"{prompt}\n\nRespond with a SINGLE JSON object matching this schema:\n{json.dumps(schema) if schema else ''}"
        return self._post_messages(instruction, model=model, system=system, parse_json=True,
                                   timeout=call_timeout(self.timeout, opts))

    def tool_call(self, prompt, tools, *, model=None, system="", **opts) -> AIResponse:
        return self._post_messages(prompt, model=model, system=system, parse_tools=True, tools=tools,
                                   timeout=call_timeout(self.timeout, opts))

    def stream(self, prompt, *, model=None, system="", **opts) -> Iterator[AIStreamChunk]:
        key = self._api_key()
        if not key:
            yield AIStreamChunk(text="", done=True)
            return
        post = self._http_post or _default_post()
        payload = self._payload(prompt, model=model, system=system, stream=True)
        resp = post(
            f"{self.base_url}/v1/messages",
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
            try:
                obj = json.loads(line[len("data:"):].strip())
            except (ValueError, TypeError):
                continue
            etype = obj.get("type")
            if etype == "content_block_delta":
                delta = obj.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text = str(delta.get("text") or "")
                    if text:
                        yield AIStreamChunk(text=text, done=False)
            elif etype == "message_stop":
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

    def _post_messages(
        self,
        prompt: str,
        *,
        model: Optional[str],
        system: str,
        parse_json: bool = False,
        parse_tools: bool = False,
        tools: Optional[List[Dict[str, Any]]] = None,
        timeout: Any = None,
    ) -> AIResponse:
        key = self._api_key()
        if not key:
            return AIResponse(text="", error=f"{self.name} api key not set (env {self.api_key_env})")
        post = self._http_post or _default_post()
        payload = self._payload(prompt, model=model, system=system, tools=tools)
        resp = post(
            f"{self.base_url}/v1/messages",
            provider=self.name, headers=self._headers(), json=payload,
            timeout=timeout if timeout is not None else self.timeout, max_retries=0,
        )
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
                inp = block.get("input")
                tool_calls.append(ToolCall(id=str(block.get("id", "")), name=str(block.get("name", "")), arguments=inp if isinstance(inp, dict) else {}))
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