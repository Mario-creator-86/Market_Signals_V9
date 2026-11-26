"""GeminiProvider — the AIProvider adapter for the Google Gemini generateContent API (§34/§35).

Wire shape verified 2026-08-11 (§25): POST {base_url}/v1beta/models/{model}:generateContent?key=<key>,
contents/parts, systemInstruction, generationConfig.responseMimeType, functionDeclarations,
usageMetadata.{promptTokenCount,candidatesTokenCount}. list_models GET {base_url}/v1beta/models
(models[].name with "models/" prefix). Zero new deps (§118): over core/http_utils (requests,
pinned), not the unpinned `google-genai` SDK.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional
from urllib.parse import urlencode

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


class GeminiProvider(AIProvider):
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

    def _api_key(self) -> Optional[str]:
        return os.getenv(self.api_key_env)

    def _resolve_model(self, model: Optional[str]) -> str:
        return model or self.default_model

    def _generate_url(self, model: str) -> str:
        # The key travels in the x-goog-api-key header, never in the URL:
        # request errors and traces carry the URL.
        return f"{self.base_url}/v1beta/models/{model}:generateContent"

    def list_models(self) -> List[str]:
        key = self._api_key()
        if not key:
            return []
        get = self._http_get or _default_get()
        url = f"{self.base_url}/v1beta/models"
        resp = get(url, provider=self.name, headers={"x-goog-api-key": key}, timeout=self.timeout)
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
        return ProviderCapabilities(
            text=True, structured_output=True, tool_calling=True, streaming=True, supported_formats=("json",)
        )

    def generate(self, prompt, *, model=None, system="", tools=None, **opts) -> AIResponse:
        return self._post_generate(prompt, model=model, system=system, parse_json=False, tools=tools,
                                   timeout=call_timeout(self.timeout, opts))

    def generate_structured(self, prompt, schema, *, model=None, system="", **opts) -> AIResponse:
        return self._post_generate(prompt, model=model, system=system, parse_json=True, response_schema=schema,
                                   timeout=call_timeout(self.timeout, opts))

    def tool_call(self, prompt, tools, *, model=None, system="", **opts) -> AIResponse:
        return self._post_generate(prompt, model=model, system=system, parse_tools=True, tools=tools,
                                   timeout=call_timeout(self.timeout, opts))

    def stream(self, prompt, *, model=None, system="", **opts) -> Iterator[AIStreamChunk]:
        key = self._api_key()
        if not key:
            yield AIStreamChunk(text="", done=True)
            return
        post = self._http_post or _default_post()
        m = self._resolve_model(model)
        url = f"{self.base_url}/v1beta/models/{m}:streamGenerateContent?{urlencode({'alt': 'sse'})}"
        payload = self._payload(prompt, system=system)
        resp = post(
            url, provider=self.name, headers={"content-type": "application/json", "x-goog-api-key": key},
            json=payload, timeout=self.timeout, max_retries=0, stream=True,
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
            parts = (((obj.get("candidates") or [{}])[0].get("content")) or {}).get("parts") or []
            for part in parts:
                txt = part.get("text")
                if txt:
                    yield AIStreamChunk(text=str(txt), done=False)

    def usage(self) -> Usage:
        with self._lock:
            return self._last_usage

    # --- internals ---
    def _payload(
        self, prompt: str, *, system: str, response_schema: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
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

    def _post_generate(
        self, prompt: str, *, model: Optional[str], system: str, parse_json: bool = False,
        parse_tools: bool = False, response_schema: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        timeout: Any = None,
    ) -> AIResponse:
        key = self._api_key()
        if not key:
            return AIResponse(text="", error=f"{self.name} api key not set (env {self.api_key_env})")
        post = self._http_post or _default_post()
        m = self._resolve_model(model)
        url = self._generate_url(m)
        payload = self._payload(prompt, system=system, response_schema=response_schema, tools=tools)
        resp = post(
            url, provider=self.name, headers={"content-type": "application/json", "x-goog-api-key": key},
            json=payload, timeout=timeout if timeout is not None else self.timeout, max_retries=0,
        )
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