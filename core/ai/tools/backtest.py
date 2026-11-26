"""§39 backtest tools (read-only / history-writing wrappers; never place trades)."""
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


def make_run_backtest_tool(*, run_backtest: Callable) -> Tool:
    def execute(**a: Any) -> ToolResult:
        return _safe(run_backtest, days=a.get("days", 30), tickers=a.get("tickers") or [],
                    risk_mode=a.get("risk_mode"), trade_mode=a.get("trade_mode"))
    return Tool(
        name="run_backtest",
        description="Run a historical backtest and save a result entry (does NOT place trades).",
        arguments_schema={"type": "object",
                          "properties": {"tickers": {"type": "array", "items": {"type": "string"}},
                                         "days": {"type": "integer", "default": 30},
                                         "risk_mode": {"type": "string"}, "trade_mode": {"type": "string"}},
                          },
        execute=execute,
    )


def make_get_backtest_result_tool(*, get_history: Callable) -> Tool:
    def execute(**a: Any) -> ToolResult:
        return _safe(get_history)
    return Tool(
        name="get_backtest_result",
        description="Return saved backtest-result history (read-only).",
        arguments_schema={"type": "object", "properties": {}},
        execute=execute,
    )


def backtest_tools() -> List[Tool]:
    from api.actions_service import run_backtest, get_backtest_history
    return [
        make_run_backtest_tool(run_backtest=run_backtest),
        make_get_backtest_result_tool(get_history=get_backtest_history),
    ]