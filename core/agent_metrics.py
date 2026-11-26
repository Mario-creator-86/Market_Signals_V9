import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional

from core.observability import update_agent_status
from core.request_context import (
    ExternalCallBudget,
    RequestContext,
    clear_request_context,
    get_request_context,
    set_request_context,
)

logger = logging.getLogger("AgentMetrics")

_DEFAULT_EXTERNAL_BUDGETS = {
    "scanner": 20,
    "premarket_scanner": 24,
    "pre_earnings": 30,
    "trade_radar": 15,
    # A research cycle expands max_symbols x strategies x 2 directions; with
    # the old 15 the cycle deferred at the budget boundary every run and the
    # health banner read "budget exhausted" perpetually. 30 covers one full
    # default cycle (8 symbols) while the deferral guard stays as a backstop.
    "strategy_research": 30,
}


def _resolve_external_budget(agent_name: str, explicit: Optional[int]) -> int:
    if explicit is not None:
        return max(0, int(explicit))
    normalized = str(agent_name or "unknown").strip().lower()
    env_key = f"MS_AGENT_EXTERNAL_BUDGET_{normalized.upper()}"
    raw = os.getenv(env_key)
    if raw is None:
        raw = os.getenv("MS_AGENT_EXTERNAL_BUDGET_DEFAULT")
    if raw is not None and str(raw).strip():
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            pass
    return int(_DEFAULT_EXTERNAL_BUDGETS.get(normalized, 20))


# ── last-run outcome ────────────────────────────────────────────────────────
# A failed run used to leave no trace in the agent status: the exception text
# went to the log only, so the Agents tab could not say "last run failed: ...".
MAX_RUN_ERROR_CHARS = 300
_SECRET_QUERY_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|key|secret|"
    r"signature|sig|password|passwd|pwd)=([^&\s\"'<>]+)"
)
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")
_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_consecutive_failures: Dict[str, int] = {}
_consecutive_failures_lock = threading.Lock()


def redact_error_text(error: Any, max_chars: int = MAX_RUN_ERROR_CHARS) -> str:
    """Error text safe to store in the agent status and show in the UI.

    ``requests``/``httpx`` exceptions embed the full request URL, and several
    providers carry the credential in it (``?apikey=...``, ``/bot<token>/``).
    Secrets are masked, whitespace collapsed and the text bounded.
    """
    text = " ".join(str(error or "").split())
    if not text:
        return ""
    text = _SECRET_QUERY_RE.sub(lambda m: f"{m.group(1)}=***", text)
    text = _URL_USERINFO_RE.sub(lambda m: f"{m.group(1)}***@", text)
    text = _BOT_TOKEN_RE.sub("bot***", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} ***", text)
    limit = max(16, int(max_chars))
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def _run_outcome_fields(agent_name: str, error: Optional[BaseException]) -> Dict[str, Any]:
    """Status fields describing how the run that just ended went.

    Empty strings / zero (never ``None``) on success: ``update_agent_status``
    skips ``None`` values, so only an explicit value clears an earlier error.
    """
    finished_at = datetime.now(timezone.utc).isoformat()
    with _consecutive_failures_lock:
        if error is None:
            _consecutive_failures[agent_name] = 0
            failures = 0
        else:
            failures = int(_consecutive_failures.get(agent_name, 0)) + 1
            _consecutive_failures[agent_name] = failures
    if error is None:
        return {
            "last_run_ok": True,
            "last_run_finished_at": finished_at,
            "last_run_error": "",
            "last_run_error_type": "",
            "last_run_error_at": "",
            "consecutive_failures": 0,
        }
    message = redact_error_text(error) or type(error).__name__
    return {
        "last_run_ok": False,
        "last_run_finished_at": finished_at,
        "last_run_error": message,
        "last_run_error_type": type(error).__name__,
        "last_run_error_at": finished_at,
        "consecutive_failures": failures,
    }


@contextmanager
def track_agent_run(
    agent_name: str,
    *,
    external_call_budget: Optional[int] = None,
) -> Iterator[None]:
    normalized_name = str(agent_name or "unknown")
    ctx = get_request_context()
    owned_token = None
    if ctx is None:
        ctx = RequestContext(
            reason="AGENT_REFRESH",
            path=f"/agent/{normalized_name}",
            cache_only=False,
            agent_name=normalized_name,
        )
        owned_token = set_request_context(ctx)

    previous_agent_name = ctx.agent_name
    previous_budget = ctx.external_budget
    run_budget = ExternalCallBudget(
        _resolve_external_budget(normalized_name, external_call_budget)
    )
    ctx.agent_name = normalized_name
    ctx.external_budget = run_budget
    started = time.perf_counter()
    error_text = None
    run_error: Optional[BaseException] = None
    try:
        yield
    except Exception as e:
        run_error = e
        error_text = redact_error_text(e) or type(e).__name__
        raise
    finally:
        duration_ms = (time.perf_counter() - started) * 1000.0
        budget_snapshot = run_budget.snapshot()
        ext_delta = int(budget_snapshot["used"])
        hit_delta = int(budget_snapshot["cache_hits"])
        miss_delta = int(budget_snapshot["cache_misses"])
        total_delta = hit_delta + miss_delta
        cache_hit_rate = round((hit_delta / total_delta) * 100.0, 1) if total_delta > 0 else None

        try:
            update_agent_status(
                normalized_name,
                last_run_duration_ms=round(duration_ms, 2),
                last_run_external_fetches=int(ext_delta),
                last_run_external_budget=int(budget_snapshot["limit"]),
                last_run_external_blocked=int(budget_snapshot["blocked"]),
                last_run_external_by_provider=dict(budget_snapshot["by_provider"]),
                last_run_external_blocked_by_provider=dict(budget_snapshot["blocked_by_provider"]),
                last_run_cache_hits=int(hit_delta),
                last_run_cache_misses=int(miss_delta),
                last_run_cache_hit_rate=cache_hit_rate,
                **_run_outcome_fields(normalized_name, run_error),
            )
            logger.info(
                json.dumps(
                    {
                        "kind": "agent_run_metrics",
                        "agent": normalized_name,
                        "duration_ms": round(duration_ms, 2),
                        "external_fetches": int(ext_delta),
                        "external_budget": int(budget_snapshot["limit"]),
                        "external_blocked": int(budget_snapshot["blocked"]),
                        "external_by_provider": dict(budget_snapshot["by_provider"]),
                        "cache_hits": int(hit_delta),
                        "cache_misses": int(miss_delta),
                        "cache_hit_rate": cache_hit_rate,
                        "error": error_text,
                    },
                    ensure_ascii=True,
                )
            )
        except Exception:
            logger.debug("Failed to publish agent run metrics", exc_info=True)
        finally:
            ctx.external_budget = previous_budget
            ctx.agent_name = previous_agent_name
            if owned_token is not None:
                clear_request_context(owned_token)
