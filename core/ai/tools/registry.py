"""§39 ToolRegistry — the AI's allow-listed surface (§40 enforced by construction).

`SPEC_TOOL_NAMES` is the 28-name §39 allow-list. The registry refuses any tool
whose name is not in that set, so the AI can never reach a name the spec did
not list. `build_default_registry()` assembles the 28 tools from the domain
modules (imported lazily to avoid import cycles through the service layer).
"""
from __future__ import annotations

from typing import Any, Dict, List

from core.ai.provider import ToolCall
from core.ai.tools.tool import Tool, ToolResult


SPEC_TOOL_NAMES = frozenset({
    "get_market_snapshot", "get_ohlcv", "get_technical_features",
    "get_market_regime", "get_sector_regime", "get_earnings_event",
    "get_historical_earnings", "get_earnings_surprises", "get_options_analysis",
    "get_pre_earnings_analysis", "get_news", "get_news_sentiment",
    "get_sec_context", "get_signal", "get_strategy", "get_strategy_performance",
    "compare_strategies", "run_backtest", "get_backtest_result", "get_portfolio",
    "get_position", "get_portfolio_risk", "get_available_capital",
    "calculate_allowed_position_size", "get_decision_history",
    "get_trade_history", "propose_order", "propose_close",
})


class ToolRegistry:
    """Allow-listed registry of §39 tools — the only AI-callable surface."""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name not in SPEC_TOOL_NAMES:
            raise ValueError(f"tool name not in §39 allow-list: {tool.name!r}")
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name!r}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def names(self) -> List[str]:
        return sorted(self._tools.keys())

    def list_tools(self) -> List[Dict[str, Any]]:
        # Portable vendor payload: name + description + parameters. The Phase
        # 3A/3B providers pass `tools` straight through to the vendor body, so a
        # flat dict with these three keys is the shape every provider accepts.
        return [
            {"name": t.name, "description": t.description, "parameters": t.arguments_schema}
            for t in self._tools.values()
        ]

    def dispatch(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(ok=False, error=f"unknown tool: {call.name}")
        try:
            return tool.execute(**dict(call.arguments or {}))
        except Exception as exc:  # a tool failure must NOT crash the agent loop
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


def build_default_registry() -> ToolRegistry:
    """Assemble the registry with all 28 §39 tools (their default delegates)."""
    # Imported here (not at module top) so the framework stays import-safe and
    # the domain modules can import service-layer code without cycles.
    from core.ai.tools.market_data import market_data_tools
    from core.ai.tools.earnings import earnings_tools
    from core.ai.tools.options import options_tools
    from core.ai.tools.news import news_tools
    from core.ai.tools.sec import sec_tools
    from core.ai.tools.signals_strategy import signals_strategy_tools
    from core.ai.tools.backtest import backtest_tools
    from core.ai.tools.portfolio_risk import portfolio_risk_tools
    from core.ai.tools.history import history_tools
    from core.ai.tools.order_proposal import order_proposal_tools

    reg = ToolRegistry()
    for builder in (
        market_data_tools, earnings_tools, options_tools, news_tools, sec_tools,
        signals_strategy_tools, backtest_tools, portfolio_risk_tools,
        history_tools, order_proposal_tools,
    ):
        for tool in builder():
            reg.register(tool)
    return reg