"""§39 decision-history + trade-history tools (read-only wrappers)."""
from __future__ import annotations

from typing import Any, Callable, List

from core.ai.tools.tool import Tool, ToolResult


def _safe(delegate: Callable, *args, **kwargs) -> ToolResult:
    try:
        data = delegate(*args, **kwargs)
    except Exception as exc:
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    if not isinstance(data, dict):
        data = {"value": data}
    return ToolResult(ok=True, data=data)


def make_get_decision_history_tool(*, get_decisions: Callable) -> Tool:
    def execute(**a: Any) -> ToolResult:
        # get_decisions(limit=...) — capital_manager.get_decisions is positional-or-keyword.
        return _safe(get_decisions, limit=a.get("limit", 50))
    return Tool(
        name="get_decision_history",
        description="Return recent decision-log entries (authoritative: capital_manager.get_decisions → trade_audit).",
        arguments_schema={"type": "object", "properties": {"limit": {"type": "integer", "default": 50}}},
        execute=execute,
    )


def make_get_trade_history_tool(*, read_recent: Callable) -> Tool:
    def execute(**a: Any) -> ToolResult:
        # read_recent_executions(limit=...) is keyword-only; always pass keyword.
        return _safe(read_recent, limit=a.get("limit", 50))
    return Tool(
        name="get_trade_history",
        description="Return recent executed trades (authoritative: execution_archive.read_recent_executions).",
        arguments_schema={"type": "object", "properties": {"limit": {"type": "integer", "default": 50}}},
        execute=execute,
    )


def history_tools() -> List[Tool]:
    from core.capital_manager import get_capital_manager
    from core.execution_archive import read_recent_executions
    cm = get_capital_manager()
    return [
        make_get_decision_history_tool(get_decisions=cm.get_decisions),
        make_get_trade_history_tool(read_recent=read_recent_executions),
    ]