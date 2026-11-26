"""§39 tool loop primitive — generate → dispatch tool_calls → feed results back.

This is the agent-loop primitive; wiring it into the production autonomous
cycle (behind the §45 autonomy gate + §44 critic) is Phase 4C. Here it is a
tested helper: it calls `provider.tool_call(tools=registry.list_tools())`, and
for every `ToolCall` in the response it dispatches via the registry and
appends a `tool_result` role message, then loops. The loop stops when the
provider returns no tool_calls (a final answer) or `max_rounds` is reached.
"""
from __future__ import annotations

from typing import Any, List, Optional

from core.ai.provider import AIProvider, AIResponse
from core.ai.tools.registry import ToolRegistry
from core.external_text import is_untrusted_envelope, quarantine_external_payload


_EXTERNAL_TEXT_TOOLS = frozenset({"get_news", "get_news_sentiment", "get_sec_context"})


def run_tool_loop(
    provider: AIProvider,
    registry: ToolRegistry,
    prompt: str,
    *,
    max_rounds: int = 4,
    model: Optional[str] = None,
    system: str = "",
    **opts: Any,
) -> AIResponse:
    rounds = 0
    messages_log: List[str] = [prompt]
    response: AIResponse = AIResponse()
    while rounds < max_rounds:
        rounds += 1
        joined = "\n\n".join(messages_log)
        response = provider.tool_call(
            joined, registry.list_tools(), model=model, system=system, **opts
        )
        if response is None or response.is_error:
            return response
        if not response.tool_calls:
            return response  # final answer
        for call in response.tool_calls:
            result = registry.dispatch(call)
            if call.name in _EXTERNAL_TEXT_TOOLS:
                payload = "ok" if result.ok else "error: external data unavailable"
                if result.ok and is_untrusted_envelope(result.data):
                    data = result.data
                else:
                    data = quarantine_external_payload({
                        "data": result.data,
                        "error": result.error,
                    })
            else:
                payload = "ok" if result.ok else f"error: {result.error}"
                data = result.data if result.ok else {}
            messages_log.append(
                f"tool_result name={call.name} id={call.id} {payload} data={data}"
            )
    # Exhausted rounds without a final answer: return the last response.
    return response
