import contextvars
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class ExternalCallBudget:
    """Thread-safe per-run meter and hard cap for provider calls."""

    def __init__(self, limit: int = 0):
        self.limit = max(0, int(limit or 0))
        self._lock = threading.Lock()
        self._used = 0
        self._blocked = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._by_provider: Dict[str, int] = {}
        self._blocked_by_provider: Dict[str, int] = {}

    def reserve(self, provider: str) -> bool:
        provider_name = str(provider or "unknown")
        with self._lock:
            if self.limit > 0 and self._used >= self.limit:
                self._blocked += 1
                self._blocked_by_provider[provider_name] = (
                    self._blocked_by_provider.get(provider_name, 0) + 1
                )
                return False
            self._used += 1
            self._by_provider[provider_name] = self._by_provider.get(provider_name, 0) + 1
            return True

    def record_cache_decision(self, decision: str) -> None:
        normalized = str(decision or "").upper()
        with self._lock:
            if normalized in {"HIT", "CACHE_HIT", "CACHED"}:
                self._cache_hits += 1
            elif normalized in {"MISS", "STALE", "CACHE_MISS"}:
                self._cache_misses += 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "limit": self.limit,
                "used": self._used,
                "remaining": max(0, self.limit - self._used) if self.limit > 0 else None,
                "blocked": self._blocked,
                "by_provider": dict(self._by_provider),
                "blocked_by_provider": dict(self._blocked_by_provider),
                "cache_hits": self._cache_hits,
                "cache_misses": self._cache_misses,
            }


@dataclass
class RequestContext:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    reason: str = "UNKNOWN"
    path: str = ""
    cache_only: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    agent_name: Optional[str] = None
    external_budget: Optional[ExternalCallBudget] = None


_REQUEST_CONTEXT: contextvars.ContextVar[Optional[RequestContext]] = contextvars.ContextVar(
    "ms_request_context",
    default=None,
)


def set_request_context(ctx: RequestContext) -> contextvars.Token:
    return _REQUEST_CONTEXT.set(ctx)


def get_request_context() -> Optional[RequestContext]:
    return _REQUEST_CONTEXT.get()


def clear_request_context(token: contextvars.Token) -> None:
    _REQUEST_CONTEXT.reset(token)


@contextmanager
def request_context(
    *,
    reason: str,
    path: str = "",
    cache_only: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
    agent_name: Optional[str] = None,
    external_budget: Optional[ExternalCallBudget] = None,
):
    ctx = RequestContext(
        reason=reason,
        path=path,
        cache_only=bool(cache_only),
        metadata=dict(metadata or {}),
        agent_name=str(agent_name or "").strip() or None,
        external_budget=external_budget,
    )
    token = set_request_context(ctx)
    try:
        yield ctx
    finally:
        clear_request_context(token)
