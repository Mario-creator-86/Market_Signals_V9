"""OpenAIProvider — the AIProvider adapter for the OpenAI Chat Completions API (spec §34/§35).

Wire shape verified 2026-08-11 (§25): POST {base_url}/chat/completions, Bearer auth,
choices[0].message.{content,tool_calls}, usage.{prompt_tokens,completion_tokens},
SSE chat.completion.chunk streaming terminated by `data: [DONE]`. list_models/health
GET {base_url}/models.

Zero new deps (§118): talks over core/http_utils (requests, already pinned), NOT the
unpinned official `openai` SDK. Injectable http_post/http_get seam → no keys/network in
tests (§106 — keys from env, never logged, only the Bearer header carries the value).
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
        return ProviderCapabilities(
            text=True, structured_output=True, tool_calling=True, streaming=True, supported_formats=("json",)
        )

    def generate(self, prompt, *, model=None, system="", tools=None, **opts) -> AIResponse:
        messages = self._build_messages(prompt, system)
        payload: Dict[str, Any] = {"model": self._resolve_model(model), "messages": messages, "stream": False}
        return self._post_chat(payload, parse_json=False, timeout=call_timeout(self.timeout, opts))

    def generate_structured(self, prompt, schema, *, model=None, system="", **opts) -> AIResponse:
        messages = self._build_messages(prompt, system)
        payload = {
            "model": self._resolve_model(model),
            "messages": messages,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        return self._post_chat(payload, parse_json=True, timeout=call_timeout(self.timeout, opts))

    def tool_call(self, prompt, tools, *, model=None, system="", **opts) -> AIResponse:
        messages = self._build_messages(prompt, system)
        payload = {"model": self._resolve_model(model), "messages": messages, "stream": False, "tools": tools}
        return self._post_chat(payload, parse_json=False, parse_tools=True,
                               timeout=call_timeout(self.timeout, opts))

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
                # OpenAI terminates the stream with `data: [DONE]` — a sentinel, not a
                # content delta. Iterator exhaustion is the done signal (no empty chunk).
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

    def _post_chat(self, payload: Dict[str, Any], *, parse_json: bool, parse_tools: bool = False,
                   timeout: Any = None) -> AIResponse:
        key = self._api_key()
        if not key:
            return AIResponse(text="", error=f"{self.name} api key not set (env {self.api_key_env})")
        post = self._http_post or _default_post()
        resp = post(
            f"{self.base_url}/chat/completions",
            provider=self.name, headers=self._headers(), json=payload,
            timeout=timeout if timeout is not None else self.timeout, max_retries=0,
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