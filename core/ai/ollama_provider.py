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
    AIProvider,
    AIResponse,
    AIStreamChunk,
    ProviderCapabilities,
    ToolCall,
    Usage,
    call_timeout,
)

# Default transport seam: the governed http_utils POST (circuit breaker / rate limit /
# budget exemption keyed by provider="ollama"). Resolved lazily to avoid an import cycle.
_DefaultPost: Optional[Callable[..., Any]] = None


def _default_http_post() -> Callable[..., Any]:
    global _DefaultPost
    if _DefaultPost is None:
        from core.http_utils import http_post as _DefaultPost  # type: ignore[assignment]
    return _DefaultPost  # type: ignore[return-value]


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


def _subscription_provider(model: Any):
    """The Claude subscription provider serving `model`, or None (see core.ai.subscription_routing)."""
    try:
        from core.ai.subscription_routing import subscription_provider_for

        return subscription_provider_for(model)
    except Exception:
        return None


def _subscription_fallback(model: Any) -> str:
    try:
        from core.ai.subscription_routing import fallback_model_for

        return fallback_model_for(model)
    except Exception:
        return ""


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
            text=True,
            structured_output=True,
            tool_calling=True,
            streaming=True,
            supported_formats=("json",),
        )

    # --- typed generate (lifts the legacy dict to AIResponse) ---
    def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        **opts: Any,
    ) -> AIResponse:
        ui_call = bool(opts.get("ui_call", False))
        timeout_override = opts.get("timeout_override")
        data = self._client.generate(
            prompt,
            model=model,
            system=system,
            ui_call=ui_call,
            timeout_override=timeout_override,
        )
        if isinstance(data, dict) and data.get("error"):
            return AIResponse(text="", error=str(data["error"]))
        text = str(data.get("response", "")) if isinstance(data, dict) else ""
        meta = (data or {}).get("_meta", {}) if isinstance(data, dict) else {}
        usage = self._usage_from_ollama(data) if isinstance(data, dict) else Usage()
        with self._lock:
            self._last_usage = usage
        return AIResponse(
            text=text,
            tool_calls=[],
            usage=usage,
            model=str(meta.get("model", "")),
            raw=data if isinstance(data, dict) else {},
        )

    # --- structured (JSON mode) — direct /api/chat, does not touch OllamaClient.generate ---
    def generate_structured(
        self,
        prompt: str,
        schema: Dict[str, Any],
        *,
        model: Optional[str] = None,
        system: str = "",
        **opts: Any,
    ) -> AIResponse:
        model = model or self._client.get_best_model()
        subscription = _subscription_provider(model)
        if subscription is not None:
            resp = subscription.generate_structured(prompt, schema, model=model, system=system, **opts)
            if not resp.is_error:
                return resp
            model = _subscription_fallback(model) or model
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
        return self._post_chat(payload, parse_json=True, read_timeout=call_timeout(120, opts))

    def tool_call(
        self,
        prompt: str,
        tools: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        system: str = "",
        **opts: Any,
    ) -> AIResponse:
        model = model or self._client.get_best_model()
        if _subscription_provider(model) is not None:
            # The subscription answers with text only: tool calls run on the fallback model.
            model = _subscription_fallback(model) or model
        messages: List[Dict[str, Any]] = []
        if str(system or "").strip():
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": model, "messages": messages, "stream": False, "tools": tools}
        return self._post_chat(payload, parse_tools=True, read_timeout=call_timeout(120, opts))

    def stream(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        system: str = "",
        **opts: Any,
    ) -> Iterator[AIStreamChunk]:
        model = model or self._client.get_best_model()
        subscription = _subscription_provider(model)
        if subscription is not None:
            yield from subscription.stream(prompt, model=model, system=system, **opts)
            return
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
            timeout=(5, call_timeout(120, opts)),
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
    def _post_chat(
        self,
        payload: Dict[str, Any],
        *,
        parse_json: bool = False,
        parse_tools: bool = False,
        read_timeout: Any = 120,
    ) -> AIResponse:
        post = self._http_post or _default_http_post()
        resp = post(
            self._client._api_url("chat"),
            provider="ollama",
            json=payload,
            headers=self._client._headers(),
            timeout=(5, read_timeout),
            max_retries=0,
        )
        if resp is None:
            return AIResponse(text="", error="ollama circuit open")
        if resp.status_code != 200:
            return AIResponse(text="", error=f"ollama error: {resp.status_code}")
        data = resp.json()
        msg = data.get("message") or {}
        text = str(msg.get("content") or "")
        # Capture usage from every completed non-stream call, even if later parsing
        # fails — the provider still consumed tokens for this request.
        usage = self._usage_from_ollama(data)
        with self._lock:
            self._last_usage = usage
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
                tool_calls.append(
                    ToolCall(
                        id=str(tc.get("id", "")),
                        name=str(fn.get("name", "")),
                        arguments=args if isinstance(args, dict) else {},
                    )
                )
        if parse_json:
            parsed = _extract_json(text)
            if parsed is None:
                return AIResponse(
                    text=text,
                    error="structured output parse failed",
                    usage=usage,
                    raw={"json": None},
                    model=payload.get("model", ""),
                )
        if parse_json:
            raw: Dict[str, Any] = {"json": parsed}
        elif parse_tools:
            raw = {"tool_calls": msg.get("tool_calls")}
        else:
            raw = data
        return AIResponse(
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            model=payload.get("model", ""),
            raw=raw,
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