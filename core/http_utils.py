import logging
import os
import threading
import time
from collections import deque
from contextlib import nullcontext
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

import requests
from core.observability import record_external_call
from core.request_context import get_request_context

logger = logging.getLogger("HttpUtils")

# Default timeouts: (connect, read)
DEFAULT_TIMEOUT = (2, 6)

# Provider concurrency limits
_PROVIDER_LIMITS = {
    "alpaca": 4,
    "finnhub": 3,
    "binance": 4,
    "kucoin": 4,
    "yahoo": 3,
    "marketaux": 3,
    "ollama": 2,
    "news": 3,
    "generic": 4,
    # Phase 3B — cloud AI providers (slow LLM calls; concurrency like ollama)
    "openai": 2,
    "anthropic": 2,
    "gemini": 2,
    "xai": 2,
    "deepseek": 2,
    "mistral": 2,
    "openrouter": 2,
    "kimi": 2,
    "zai": 2,
}

_SEMAPHORES: Dict[str, threading.BoundedSemaphore] = {
    name: threading.BoundedSemaphore(value=limit)
    for name, limit in _PROVIDER_LIMITS.items()
}

_CIRCUIT_LOCK = threading.Lock()
_CIRCUIT_STATE: Dict[str, Dict[str, float]] = {
    name: {"fail_count": 0.0, "open_until": 0.0, "last_fail": 0.0}
    for name in _PROVIDER_LIMITS.keys()
}

# Circuit breaker failure thresholds per provider
# Ollama gets a higher threshold — model loading causes slow responses that look like failures
_CIRCUIT_THRESHOLDS: Dict[str, int] = {
    "alpaca": 8,
    "ollama": 5,
    "finnhub": 5,
    "binance": 15,
    "kucoin": 5,
    "yahoo": 15,
    "marketaux": 3,
    "news": 10,
    "generic": 5,
    "openai": 5,
    "anthropic": 5,
    "gemini": 5,
    "xai": 5,
    "deepseek": 5,
    "mistral": 5,
    "openrouter": 5,
    "kimi": 5,
    "zai": 5,
}

# Rate limiting: min seconds between requests per provider.
# For providers in _RATE_LIMIT_MAX_PER_MIN the min-delay is 0.0 — a stricter
# sliding-window cap (below) governs them instead, so we don't double-gate.
_RATE_LIMIT_DELAYS: Dict[str, float] = {
    "alpaca": 0.05,
    "finnhub": 0.0,
    "binance": 0.1,
    "kucoin": 0.2,
    "yahoo": 0.1,
    "marketaux": 0.2,
    "news": 0.2,
    "ollama": 0.0,
    "generic": 0.1,
    "openai": 0.0,
    "anthropic": 0.0,
    "gemini": 0.0,
    "xai": 0.0,
    "deepseek": 0.0,
    "mistral": 0.0,
    "openrouter": 0.0,
    "kimi": 0.0,
    "zai": 0.0,
}

# Sliding-window rate caps: at most N requests per rolling 60s window.
# FinnHub free tier is 60/min — a sliding window (vs token bucket) prevents
# the 60-call burst that the API rejects with 429. This is the SINGLE global
# gate: every finnhub call routes through http_request → _rate_limit_wait, so
# all 4 call sites share one budget.
_RATE_LIMIT_MAX_PER_MIN: Dict[str, int] = {
    "finnhub": 60,
}

# Per-provider deque of request timestamps (only initialized for capped providers)
_RATE_LIMIT_WINDOWS: Dict[str, deque] = {
    name: deque() for name in _RATE_LIMIT_MAX_PER_MIN.keys()
}
_RATE_WINDOW_LOCK = threading.Lock()

# Last request timestamp per provider
_LAST_REQUEST_TIME: Dict[str, float] = {
    name: 0.0 for name in _PROVIDER_LIMITS.keys()
}

_PERMANENT_STATUS_CODES = frozenset({401, 402, 403})
_ENDPOINT_COOLDOWN_LOCK = threading.Lock()
_ENDPOINT_COOLDOWNS: Dict[str, Dict[str, Any]] = {}
_ENDPOINT_COOLDOWN_MAX_ENTRIES = 500
_POLICY_BLOCK_LOCK = threading.Lock()
_POLICY_BLOCK_COUNTS: Dict[str, int] = {}


class CircuitOpen(RuntimeError):
    pass


def _now() -> float:
    return time.time()


def _get_provider(name: Optional[str], url: str = "") -> str:
    normalized = str(name or "").strip().lower()
    if normalized in _SEMAPHORES:
        return normalized
    host = (urlparse(str(url or "")).hostname or "").lower()
    if "marketaux.com" in host:
        return "marketaux"
    if "finance.yahoo.com" in host:
        return "yahoo"
    if "binance.com" in host:
        return "binance"
    if "kucoin.com" in host:
        return "kucoin"
    if "finnhub.io" in host:
        return "finnhub"
    # Cloud AI hosts get their own bucket so unrelated "generic" 401s/cooldowns
    # never collateral-damage AI provider calls.
    if "api.openai.com" in host:
        return "openai"
    if "api.anthropic.com" in host:
        return "anthropic"
    if "generativelanguage.googleapis.com" in host:
        return "gemini"
    if "api.x.ai" in host or "x.ai" in host:
        return "xai"
    if "api.deepseek.com" in host:
        return "deepseek"
    if "api.mistral.ai" in host:
        return "mistral"
    if "openrouter.ai" in host:
        return "openrouter"
    if "api.moonshot.ai" in host:
        return "kimi"
    if "api.z.ai" in host or host.endswith("z.ai") or "bigmodel.cn" in host:
        return "zai"
    return "generic"


def _endpoint_key(provider: str, url: str) -> str:
    parsed = urlparse(str(url or ""))
    return f"{provider}|{(parsed.hostname or '').lower()}|{parsed.path or '/'}"


def _permanent_failure_cooldown_seconds(status_code: int) -> float:
    if int(status_code) == 429:
        env_name = "MS_HTTP_RATE_LIMIT_COOLDOWN_SECONDS"
        default = 60.0
    elif int(status_code) == 402:
        env_name = "MS_HTTP_BILLING_COOLDOWN_SECONDS"
        default = 1800.0
    else:
        env_name = "MS_HTTP_AUTH_COOLDOWN_SECONDS"
        default = 300.0
    try:
        return max(15.0, min(float(os.getenv(env_name, str(default)) or default), 86400.0))
    except (TypeError, ValueError):
        return default


def _set_endpoint_cooldown(provider: str, url: str, status_code: int) -> None:
    now_ts = _now()
    key = _endpoint_key(provider, url)
    with _ENDPOINT_COOLDOWN_LOCK:
        for stale_key, stale_entry in list(_ENDPOINT_COOLDOWNS.items()):
            if float(stale_entry.get("open_until", 0.0)) <= now_ts:
                _ENDPOINT_COOLDOWNS.pop(stale_key, None)
        previous = _ENDPOINT_COOLDOWNS.get(key) or {}
        _ENDPOINT_COOLDOWNS[key] = {
            "provider": provider,
            "host": (urlparse(str(url or "")).hostname or "").lower(),
            "endpoint": urlparse(str(url or "")).path or "/",
            "status": int(status_code),
            "opened_at": now_ts,
            "open_until": now_ts + _permanent_failure_cooldown_seconds(status_code),
            "blocked_count": int(previous.get("blocked_count", 0)),
        }
        if len(_ENDPOINT_COOLDOWNS) > _ENDPOINT_COOLDOWN_MAX_ENTRIES:
            oldest = min(
                _ENDPOINT_COOLDOWNS,
                key=lambda item_key: float(
                    (_ENDPOINT_COOLDOWNS.get(item_key) or {}).get("opened_at", 0.0)
                ),
            )
            _ENDPOINT_COOLDOWNS.pop(oldest, None)


def _active_endpoint_cooldown(provider: str, url: str) -> Optional[Dict[str, Any]]:
    now_ts = _now()
    key = _endpoint_key(provider, url)
    with _ENDPOINT_COOLDOWN_LOCK:
        entry = _ENDPOINT_COOLDOWNS.get(key)
        if not entry:
            return None
        if float(entry.get("open_until", 0.0)) <= now_ts:
            _ENDPOINT_COOLDOWNS.pop(key, None)
            return None
        entry["blocked_count"] = int(entry.get("blocked_count", 0)) + 1
        return dict(entry)


def _record_policy_block(reason: str) -> None:
    with _POLICY_BLOCK_LOCK:
        _POLICY_BLOCK_COUNTS[reason] = int(_POLICY_BLOCK_COUNTS.get(reason, 0)) + 1


def snapshot_http_governor() -> Dict[str, Any]:
    """Return redacted provider-governance state for diagnostics."""
    now_ts = _now()
    with _CIRCUIT_LOCK:
        circuits = {
            name: {
                "fail_count": int(state.get("fail_count", 0)),
                "open": bool(float(state.get("open_until", 0.0)) > now_ts),
                "retry_in_seconds": round(max(0.0, float(state.get("open_until", 0.0)) - now_ts), 3),
            }
            for name, state in _CIRCUIT_STATE.items()
        }
    with _ENDPOINT_COOLDOWN_LOCK:
        for stale_key, stale_entry in list(_ENDPOINT_COOLDOWNS.items()):
            if float(stale_entry.get("open_until", 0.0)) <= now_ts:
                _ENDPOINT_COOLDOWNS.pop(stale_key, None)
        cooldowns = [
            {
                "provider": entry.get("provider"),
                "host": entry.get("host"),
                "endpoint": entry.get("endpoint"),
                "status": entry.get("status"),
                "retry_in_seconds": round(max(0.0, float(entry.get("open_until", 0.0)) - now_ts), 3),
                "blocked_count": int(entry.get("blocked_count", 0)),
            }
            for entry in _ENDPOINT_COOLDOWNS.values()
            if float(entry.get("open_until", 0.0)) > now_ts
        ]
    with _POLICY_BLOCK_LOCK:
        policy_blocks = dict(_POLICY_BLOCK_COUNTS)
    return {
        "circuits": circuits,
        "endpoint_cooldowns": sorted(
            cooldowns,
            key=lambda item: (str(item.get("provider")), str(item.get("endpoint"))),
        ),
        "policy_blocks": policy_blocks,
        "rate_limits_per_minute": dict(_RATE_LIMIT_MAX_PER_MIN),
    }


def reset_http_governor_state() -> None:
    """Reset mutable governor state for deterministic tests."""
    with _CIRCUIT_LOCK:
        for state in _CIRCUIT_STATE.values():
            state.update({"fail_count": 0.0, "open_until": 0.0, "last_fail": 0.0})
    with _ENDPOINT_COOLDOWN_LOCK:
        _ENDPOINT_COOLDOWNS.clear()
    with _POLICY_BLOCK_LOCK:
        _POLICY_BLOCK_COUNTS.clear()
    with _RATE_WINDOW_LOCK:
        for window in _RATE_LIMIT_WINDOWS.values():
            window.clear()


def _is_circuit_open(provider: str) -> bool:
    state = _CIRCUIT_STATE.get(provider, {})
    return bool(state.get("open_until", 0.0) > _now())


def _record_success(provider: str) -> None:
    with _CIRCUIT_LOCK:
        st = _CIRCUIT_STATE.setdefault(provider, {"fail_count": 0.0, "open_until": 0.0, "last_fail": 0.0})
        st["fail_count"] = 0.0
        st["open_until"] = 0.0
        st["last_fail"] = 0.0


def _record_failure(provider: str) -> None:
    with _CIRCUIT_LOCK:
        st = _CIRCUIT_STATE.setdefault(provider, {"fail_count": 0.0, "open_until": 0.0, "last_fail": 0.0})
        now = _now()
        last_fail = float(st.get("last_fail", 0.0))
        if now - last_fail > 120:
            st["fail_count"] = 0.0
        st["last_fail"] = now
        st["fail_count"] = float(st.get("fail_count", 0.0)) + 1.0
        threshold = _CIRCUIT_THRESHOLDS.get(provider, 3)
        if st["fail_count"] >= threshold:
            st["open_until"] = now + 60.0


def _rate_limit_wait(provider: str) -> None:
    """Wait if needed to respect rate limits.

    Two mechanisms, mutually exclusive per provider:
    - Sliding 60s window (providers in _RATE_LIMIT_MAX_PER_MIN): guarantees
      ≤ N requests in any rolling 60s window. Used for finnhub (60/min free).
    - Min-delay spacing (other providers): at least _RATE_LIMIT_DELAYS[p] s
      between consecutive requests.
    """
    window_max = _RATE_LIMIT_MAX_PER_MIN.get(provider)
    if window_max:
        win = _RATE_LIMIT_WINDOWS[provider]
        while True:
            with _RATE_WINDOW_LOCK:
                now = _now()
                # Drop timestamps older than 60s
                cutoff = now - 60.0
                while win and win[0] <= cutoff:
                    win.popleft()
                if len(win) < window_max:
                    win.append(now)
                    return
                # Window full: sleep until the oldest request ages out (+epsilon)
                sleep_for = (win[0] + 60.0) - now + 0.05
            if sleep_for > 0:
                time.sleep(sleep_for)
        # unreachable

    delay = _RATE_LIMIT_DELAYS.get(provider, 0.1)
    if delay <= 0:
        return

    now = _now()
    last = _LAST_REQUEST_TIME.get(provider, 0.0)
    elapsed = now - last

    if elapsed < delay:
        sleep_time = delay - elapsed
        time.sleep(sleep_time)

    _LAST_REQUEST_TIME[provider] = _now()


def http_request(
    method: str,
    url: str,
    *,
    provider: Optional[str] = None,
    timeout: Optional[Any] = None,
    max_retries: int = 2,  # Increased default retries
    backoff_base: float = 1.0,  # Exponential backoff base
    priority: bool = False,
    bypass_circuit: bool = False,
    **kwargs,
) -> requests.Response:
    """
    Wrapper with timeout defaults, semaphore concurrency, retry (max 2),
    circuit-breaker behavior, and rate limiting.
    """
    prov = _get_provider(provider, url)
    ctx = get_request_context()
    if timeout is None:
        timeout = DEFAULT_TIMEOUT

    if ctx and ctx.cache_only:
        _record_policy_block("cache_only")
        if isinstance(ctx.metadata, dict):
            ctx.metadata["cache_reason"] = "FORCED_CACHE_ONLY"
        raise CircuitOpen(f"Cache-only context active for provider={prov}")

    if not bypass_circuit and _is_circuit_open(prov):
        _record_policy_block(f"circuit_open:{prov}")
        raise CircuitOpen(f"Circuit open for provider={prov}")

    cooldown = _active_endpoint_cooldown(prov, url)
    if cooldown:
        _record_policy_block(f"endpoint_cooldown:{prov}")
        raise CircuitOpen(
            f"Endpoint cooldown active for provider={prov}, "
            f"status={cooldown.get('status')}"
        )

    attempts = max(1, int(max_retries) + 1)
    last_exc: Optional[Exception] = None

    for attempt in range(attempts):
        # Legacy safety net: block FMP domain even if no provider uses it anymore
        if "financialmodelingprep.com" in (url or ""):
            _record_policy_block("forbidden_domain:financialmodelingprep.com")
            raise CircuitOpen(f"Domain financialmodelingprep.com is strictly forbidden.")

        if not bypass_circuit and _is_circuit_open(prov):
            _record_policy_block(f"circuit_open:{prov}")
            raise CircuitOpen(f"Circuit open for provider={prov}")

        budget = getattr(ctx, "external_budget", None) if ctx is not None else None
        # Ollama AI calls are an intentional, separately-capped premium feature
        # (the agent's `ollama_max_tickers` already bounds the call count). They
        # MUST NOT compete with data-ingestion calls for the per-run external
        # budget, otherwise data fetches starve the AI and no `ai_decision` is
        # ever produced (regression surfaced live on the Pre-Earnings tab).
        if budget is not None and prov != "ollama" and not budget.reserve(prov):
            agent_name = str(getattr(ctx, "agent_name", None) or "unknown")
            _record_policy_block(f"agent_budget:{agent_name}")
            if isinstance(ctx.metadata, dict):
                ctx.metadata["external_budget_exhausted"] = True
            raise CircuitOpen(
                f"External-call budget exhausted for agent={agent_name}"
            )
        
        # Rate limiting
        _rate_limit_wait(prov)
        
        sem = _SEMAPHORES.get(prov) or _SEMAPHORES["generic"]
        semaphore_context = nullcontext() if priority else sem
        with semaphore_context:
            started = time.perf_counter()
            try:
                resp = requests.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                duration_ms = (time.perf_counter() - started) * 1000.0
                last_exc = exc
                record_external_call(
                    service=prov,
                    endpoint=url,
                    duration_ms=duration_ms,
                    status=None,
                    cache_reason="NETWORK_ERROR",
                    error=str(exc),
                )
                _record_failure(prov)
                if attempt < attempts - 1:
                    time.sleep(0.25 * (attempt + 1))
                    continue
                raise

        duration_ms = (time.perf_counter() - started) * 1000.0
        record_external_call(
            service=prov,
            endpoint=url,
            duration_ms=duration_ms,
            status=getattr(resp, "status_code", None),
            cache_reason=(
                (ctx.metadata.get("cache_reason") if ctx and isinstance(ctx.metadata, dict) else None)
                or (None if resp.status_code < 500 else "UPSTREAM_5XX")
            ),
        )
        permanent_failure = resp.status_code in _PERMANENT_STATUS_CODES
        if permanent_failure:
            _set_endpoint_cooldown(prov, url, resp.status_code)
        if permanent_failure:
            return resp
        if resp.status_code == 429:
            # Immediate retries amplify an upstream rate limit and, for generic
            # RSS sources, used to poison the shared generic-provider circuit.
            _set_endpoint_cooldown(prov, url, resp.status_code)
            return resp
        if resp.status_code >= 500:
            _record_failure(prov)
            if attempt < attempts - 1:
                # Exponential backoff: 1s, 2s, 4s
                sleep_time = backoff_base * (2 ** attempt)
                logger.warning(f"Rate limit/error for {prov}, retrying in {sleep_time}s (attempt {attempt + 1}/{attempts})")
                time.sleep(sleep_time)
                continue
        else:
            _record_success(prov)
            try:
                from core.data_guard import get_data_guard
                get_data_guard().update_timestamp(prov)
            except Exception as e:
                logger.error(f"DataGuard hook error: {e}")
        return resp

    if last_exc is not None:
        raise last_exc
    raise requests.RequestException("http_request failed without exception")


def _log_circuit_open(exc: CircuitOpen) -> None:
    message = str(exc)
    if message.startswith("External-call budget exhausted"):
        ctx = get_request_context()
        metadata = ctx.metadata if ctx is not None and isinstance(ctx.metadata, dict) else None
        if metadata is not None:
            if metadata.get("external_budget_warning_logged"):
                logger.debug(message)
                return
            metadata["external_budget_warning_logged"] = True
    logger.warning(message)


def http_get(url: str, *, provider: Optional[str] = None, **kwargs) -> Optional[requests.Response]:
    try:
        return http_request("GET", url, provider=provider, **kwargs)
    except CircuitOpen as e:
        _log_circuit_open(e)
        return None


def http_post(url: str, *, provider: Optional[str] = None, **kwargs) -> Optional[requests.Response]:
    try:
        return http_request("POST", url, provider=provider, **kwargs)
    except CircuitOpen as e:
        _log_circuit_open(e)
        return None
