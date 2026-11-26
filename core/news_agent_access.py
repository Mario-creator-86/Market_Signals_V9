import os
import threading
import time
from typing import Any, Optional

from agents.news_tab_agent import (
    get_news_tab_agent,
    get_news_tab_agent_if_ready,
    kickoff_news_tab_agent_init,
)


_INIT_LOCK = threading.Lock()
_INIT_LAST_ATTEMPT = 0.0


def _retry_seconds() -> float:
    try:
        value = float(os.getenv("MS_NEWS_AGENT_INIT_RETRY_SECONDS", "15") or 15)
    except (TypeError, ValueError):
        value = 15.0
    return max(1.0, min(120.0, value))


def get_news_agent_cached(*, allow_blocking: bool = False) -> Optional[Any]:
    """Return the ready agent and throttle asynchronous initialization attempts."""
    agent = get_news_tab_agent_if_ready()
    if agent is not None:
        return agent
    try:
        from core.request_context import get_request_context

        ctx = get_request_context()
        if ctx is not None and bool(getattr(ctx, "cache_only", False)):
            # Cache-only UI reads must never start heavyweight agent initialization.
            return None
    except Exception:
        pass

    global _INIT_LAST_ATTEMPT
    now = time.monotonic()
    should_initialize = False
    with _INIT_LOCK:
        if now - _INIT_LAST_ATTEMPT >= _retry_seconds():
            _INIT_LAST_ATTEMPT = now
            should_initialize = True
    if should_initialize:
        try:
            kickoff_news_tab_agent_init()
        except Exception:
            pass

    if allow_blocking:
        try:
            return get_news_tab_agent()
        except Exception:
            return None
    return None


def reset_news_agent_access_state() -> None:
    """Reset the throttle for deterministic tests and explicit runtime recovery."""
    global _INIT_LAST_ATTEMPT
    with _INIT_LOCK:
        _INIT_LAST_ATTEMPT = 0.0
