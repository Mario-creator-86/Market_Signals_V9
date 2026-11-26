"""§39 point-in-time SEC context tool over the Phase 10 service boundary."""
from __future__ import annotations

from typing import Any, Callable, List, Optional

from core.ai.tools.tool import Tool, ToolResult
from core.external_text import quarantine_external_payload


def make_get_sec_context_tool(*, get_context: Optional[Callable] = None) -> Tool:
    if get_context is None:
        from core.sec.service import get_sec_filing_service

        get_context = get_sec_filing_service().get_context

    def execute(**a: Any) -> ToolResult:
        ticker = str(a.get("ticker") or "").strip().upper()
        if not ticker:
            return ToolResult(ok=False, error="ticker is required")
        try:
            context = get_context(ticker)
        except Exception as exc:
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        if not isinstance(context, dict):
            return ToolResult(ok=False, error="SEC service returned an invalid payload")
        envelope = quarantine_external_payload(context)
        if context.get("status") == "unavailable":
            return ToolResult(ok=False, data=envelope, error=str(context.get("error") or "SEC context unavailable"))
        return ToolResult(ok=True, data=envelope)
    return Tool(
        name="get_sec_context",
        description="Return point-in-time, cache-backed SEC filing metadata for a US equity ticker.",
        arguments_schema={"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]},
        execute=execute,
    )


def sec_tools() -> List[Tool]:
    return [make_get_sec_context_tool()]
