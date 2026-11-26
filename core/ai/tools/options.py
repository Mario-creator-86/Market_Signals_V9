"""§39 get_options_analysis — honest Phase-5 stub (§4: no forbidden language).

`core/data_model/entities.py:162 OptionsChain` is a "Fetcher in Phase 5" stub.
This tool returns an explicit not-available result until the Phase 5 fetcher
lands; the seam is wired so Phase 5 plugs the fetcher into this same factory.
"""
from __future__ import annotations

from typing import Any, List

from core.ai.tools.tool import Tool, ToolResult

_NOT_AVAILABLE = "not available: options fetcher lands in Phase 5"


def make_get_options_analysis_tool() -> Tool:
    def execute(**a: Any) -> ToolResult:
        return ToolResult(ok=False, error=_NOT_AVAILABLE)
    return Tool(
        name="get_options_analysis",
        description="Return the options chain/implied-vol analysis for a ticker (Phase 5 fetcher pending).",
        arguments_schema={"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]},
        execute=execute,
    )


def options_tools() -> List[Tool]:
    return [make_get_options_analysis_tool()]