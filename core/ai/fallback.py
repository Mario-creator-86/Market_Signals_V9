"""Fallback + autonomous-trade guard (spec §38 chain, §107 GOLD-013/ GOLD-014).

`generate_with_fallback` walks a provider chain (GOLD-013): the primary provider
is tried first with the requested model; on any error the next provider in
`fallback_chain` is tried with ITS OWN default model (§35 — no hardcoded model
cross-provider). If every provider fails, the 3A `UNAVAILABLE` sentinel is
returned (identity-stable) so callers can gate on it.

`no_new_autonomous_trade_guard` enforces GOLD-014: when
`config["ai"]["all_unavailable_no_trade"]` is true (the 3A default) and the
response is `UNAVAILABLE`, autonomous trading must NOT open a new position.
The spec default is True; a missing key is treated as True (never silently
allow a trade when the policy is unspecified).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import logging
import re
import time

from core.ai.provider import AIProvider, AIResponse, UNAVAILABLE

logger = logging.getLogger(__name__)

# Set on `AIResponse.raw` so callers can attribute the answer to the provider
# that actually produced it rather than to the primary.
SERVED_BY_KEY = "_served_by"


# Upper bound for one whole provider chain when the caller names no timeout.
DEFAULT_CHAIN_BUDGET_SECONDS = 180.0
_MIN_HOP_SECONDS = 1.0


def _positive_seconds(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds != seconds or seconds <= 0 or seconds == float("inf"):
        return None
    return seconds


def _redact(text: Any) -> str:
    return re.sub(r"(key=)[^&\s]+", r"***", str(text or ""))


def served_by(response: Any, default: str = "") -> str:
    raw = getattr(response, "raw", None)
    return str((raw.get(SERVED_BY_KEY) if isinstance(raw, dict) else "") or default)


def generate_with_fallback(
    primary: AIProvider,
    prompt: str,
    *,
    model: Optional[str] = None,
    fallback_chain: Optional[List[AIProvider]] = None,
    system: str = "",
    total_timeout: Optional[float] = None,
    **opts: Any,
) -> AIResponse:
    """Try `primary` (with `model`), then each fallback provider (its default model).

    Returns the first non-error AIResponse, or the `UNAVAILABLE` sentinel if all
    providers error. An error response (`response.is_error`) triggers the next hop.

    The whole chain shares one time budget so a run of slow or dead providers
    cannot hold the caller for (number of providers) x (their timeout):
    `total_timeout` seconds when given, else twice the per-call
    `timeout_override`, else `DEFAULT_CHAIN_BUDGET_SECONDS`. Each call gets at
    most what is left of the budget, and no further provider is tried once it
    is spent.
    """
    candidates: List[tuple] = [(primary, model)]
    for fb in fallback_chain or []:
        candidates.append((fb, None))  # fallback uses its own default_model

    hop_timeout = _positive_seconds(opts.get("timeout_override"))
    budget = _positive_seconds(total_timeout)
    if budget is None:
        budget = hop_timeout * 2.0 if hop_timeout else DEFAULT_CHAIN_BUDGET_SECONDS
    started = time.monotonic()

    empty: Optional[AIResponse] = None
    for index, (provider, mdl) in enumerate(candidates):
        name = str(getattr(provider, "name", "") or "")
        remaining = budget - (time.monotonic() - started)
        if index > 0 and remaining < _MIN_HOP_SECONDS:
            logger.warning(
                "AI fallback chain stopped before %s: %.0fs budget spent on %d provider(s)",
                name or "next provider", budget, index,
            )
            break
        hop_opts = dict(opts)
        if hop_timeout:
            hop_opts["timeout_override"] = max(_MIN_HOP_SECONDS, min(hop_timeout, remaining))
        try:
            resp = provider.generate(prompt, model=mdl, system=system, **hop_opts)
        except Exception as exc:  # a raised exception = this provider unavailable
            logger.warning("AI provider %s raised: %s", name, type(exc).__name__)
            continue
        if resp is None:
            continue
        if getattr(resp, "is_error", False):
            logger.warning("AI provider %s failed: %s", name, _redact(resp.error))
            continue
        if isinstance(getattr(resp, "raw", None), dict):
            resp.raw[SERVED_BY_KEY] = name
        if not str(getattr(resp, "text", "") or "").strip() and not getattr(resp, "tool_calls", None):
            # A 200 with no content is not an answer: try the next provider,
            # and hand the empty reply back only if nobody else answers.
            logger.warning("AI provider %s returned an empty reply", name)
            empty = empty or resp
            continue
        return resp
    if empty is not None:
        return empty
    # Every provider failed: return the identity-stable sentinel (GOLD-014).
    return UNAVAILABLE


def all_unavailable(response: AIResponse) -> bool:
    """True iff `response` is the all-providers-unavailable sentinel (GOLD-014)."""
    if response is UNAVAILABLE:
        return True
    return bool(response) and not response.text and response.error == UNAVAILABLE.error


def no_new_autonomous_trade_guard(response: AIResponse, config: Dict[str, Any]) -> bool:
    """GOLD-014 — True when autonomous trading must NOT open a new position.

    Blocks iff `config["ai"]["all_unavailable_no_trade"]` is true (default True
    when the key is missing) AND `response` is the all-unavailable sentinel.
    """
    ai = (config or {}).get("ai") or {}
    flag = ai.get("all_unavailable_no_trade")
    flag_enabled = True if flag is None else bool(flag)
    return flag_enabled and all_unavailable(response)