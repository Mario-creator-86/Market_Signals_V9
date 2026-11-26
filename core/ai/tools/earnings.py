"""§39 earnings + pre-earnings tools (read-only wrappers; surprises is a net-new seam)."""
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


def make_get_earnings_event_tool(*, get_earnings_for_ticker: Callable) -> Tool:
    def execute(**a: Any) -> ToolResult:
        return _safe(get_earnings_for_ticker, a.get("ticker"))
    return Tool(
        name="get_earnings_event",
        description="Return the next/most-recent earnings event for a ticker.",
        arguments_schema={"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]},
        execute=execute,
    )


def make_get_historical_earnings_tool(*, get_symbol_earnings: Callable) -> Tool:
    def execute(**a: Any) -> ToolResult:
        return _safe(get_symbol_earnings, a.get("symbol"))
    return Tool(
        name="get_historical_earnings",
        description="Return historical earnings rows for a symbol.",
        arguments_schema={"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
        execute=execute,
    )


def make_get_earnings_surprises_tool(*, get_symbol_earnings: Callable) -> Tool:
    def execute(**a: Any) -> ToolResult:
        symbol = a.get("symbol")
        try:
            rows = get_symbol_earnings(symbol) or []
        except Exception as exc:
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        surprises = []
        for row in rows:
            try:
                actual = float(row.get("epsActual"))
                est = float(row.get("epsEstimate"))
            except (TypeError, ValueError):
                continue
            surprises.append({"quarter": row.get("quarter"), "actual": actual,
                              "estimate": est, "surprise": round(actual - est, 4)})
        return ToolResult(ok=True, data={"symbol": symbol, "surprises": surprises})
    return Tool(
        name="get_earnings_surprises",
        description="Compute EPS surprises (actual − estimate) from earnings rows (net-new read seam).",
        arguments_schema={"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
        execute=execute,
    )


def make_get_pre_earnings_analysis_tool(*, get_state: Callable) -> Tool:
    def execute(**a: Any) -> ToolResult:
        return _safe(get_state)
    return Tool(
        name="get_pre_earnings_analysis",
        description="Return the pre-earnings analysis state (per-ticker expected-move proxy, scores).",
        arguments_schema={"type": "object", "properties": {}},
        execute=execute,
    )


def earnings_tools() -> List[Tool]:
    from agents.economic_calendar_agent import EconomicCalendarAgent
    from core.finnhub_client import FinnhubClient
    from agents.pre_earnings_agent import PreEarningsAgent

    _cal = EconomicCalendarAgent()
    _finn = FinnhubClient()
    _pre = PreEarningsAgent()
    return [
        make_get_earnings_event_tool(get_earnings_for_ticker=_cal.get_earnings_for_ticker),
        make_get_historical_earnings_tool(get_symbol_earnings=_finn.get_symbol_earnings),
        make_get_earnings_surprises_tool(get_symbol_earnings=_finn.get_symbol_earnings),
        make_get_pre_earnings_analysis_tool(get_state=_pre.get_state),
    ]