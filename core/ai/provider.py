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
from typing import Any, Dict, Iterator, List, Optional, Protocol


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
    # human-readable list of supported structured-output formats, e.g. ("json",)
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


def call_timeout(default: Any, opts: Optional[Dict[str, Any]]) -> Any:
    """Timeout for one request: the caller's `timeout_override` (seconds) when it
    is a positive number, else the provider default. Every provider resolves its
    HTTP timeout through this, so a caller's deadline reaches the HTTP client no
    matter which provider serves the request."""
    override = (opts or {}).get("timeout_override")
    if isinstance(override, bool):
        return default
    try:
        seconds = float(override)
    except (TypeError, ValueError):
        return default
    if seconds != seconds or seconds <= 0 or seconds == float("inf"):
        return default
    return max(1.0, seconds)


# Provider types that serve only their own models (the Claude subscription).
# They are chosen for a role whose model is one of theirs and are never a
# generic primary or an automatic fallback: a trading call must not spill
# onto the owner's subscription because another provider had an outage.
MODEL_BOUND_PROVIDER_TYPES = frozenset({"claude_code"})


def is_model_bound_provider(entry: Any) -> bool:
    return isinstance(entry, dict) and entry.get("type") in MODEL_BOUND_PROVIDER_TYPES


def general_provider_names(config: Any) -> List[str]:
    """Configured provider names, in config order, without model-bound ones."""
    root = getattr(config, "config", config)
    providers = ((root or {}).get("ai") or {}).get("providers") if isinstance(root, dict) else None
    if not isinstance(providers, dict):
        return []
    return [name for name, entry in providers.items() if not is_model_bound_provider(entry)]


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
        """Free-text generation -> typed AIResponse."""

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
        """Structured (JSON-conforming to `schema`) generation -> typed AIResponse."""

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