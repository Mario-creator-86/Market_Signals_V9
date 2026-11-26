"""§39 signals + strategy tools (read-only wrappers; get_signal is a net-new by-id seam)."""
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


def make_get_signal_tool(*, signal_manager) -> Tool:
    def execute(**a: Any) -> ToolResult:
        sid = a.get("signal_id")
        if not sid:
            return ToolResult(ok=False, error="signal_id is required")
        sm = signal_manager
        sig = (getattr(sm, "open_signals", {}) or {}).get(sid)
        if sig is None:
            for c in getattr(sm, "closed_signals", []) or []:
                if isinstance(c, dict) and c.get("id") == sid:
                    sig = c
                    break
        if sig is None:
            return ToolResult(ok=False, error=f"signal not found: {sid}")
        return ToolResult(ok=True, data=sig if isinstance(sig, dict) else {"value": sig})
    return Tool(
        name="get_signal",
        description="Return a signal by id (open first, then closed). Net-new by-id read seam over SignalManager.",
        arguments_schema={"type": "object", "properties": {"signal_id": {"type": "string"}}, "required": ["signal_id"]},
        execute=execute,
    )


def make_get_strategy_tool(*, list_strategies: Callable) -> Tool:
    def execute(**a: Any) -> ToolResult:
        return _safe(list_strategies)
    return Tool(
        name="get_strategy",
        description="List the registered strategies (V2 specs).",
        arguments_schema={"type": "object", "properties": {}},
        execute=execute,
    )


def make_get_strategy_performance_tool(*, get_strategy_performance: Callable) -> Tool:
    def execute(**a: Any) -> ToolResult:
        return _safe(get_strategy_performance, a.get("symbol"))
    return Tool(
        name="get_strategy_performance",
        description="Return cached strategy-performance rows for a symbol.",
        arguments_schema={"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
        execute=execute,
    )


def make_compare_strategies_tool(*, compare: Callable) -> Tool:
    def execute(**a: Any) -> ToolResult:
        return _safe(compare, a.get("ticker"), days=a.get("days", 30))
    return Tool(
        name="compare_strategies",
        description="Run all strategy combinations for a ticker and return ranked win-rate output.",
        arguments_schema={"type": "object",
                          "properties": {"ticker": {"type": "string"}, "days": {"type": "integer", "default": 30}},
                          "required": ["ticker"]},
        execute=execute,
    )


def signals_strategy_tools() -> List[Tool]:
    from core.signals import get_signal_manager
    from core.strategy_v2.registry import build_default_registry
    from api.actions_service import get_strategy_performance, _get_strategy_comparison

    _v2 = build_default_registry()
    # DOCUMENTED DEVIATION: list ALL registered specs via the registry's public
    # `specs` attribute rather than get_specs_for_asset("stock") — the real
    # asset_class strings are "US_STOCKS"/"CRYPTO_FUTURES"/etc. (not "stock"),
    # and the tool takes no asset argument, so listing all strategies is the
    # useful, correct behavior.
    def _list_strategies():
        return {"strategies": [s.__dict__ for s in _v2.specs]}

    return [
        make_get_signal_tool(signal_manager=get_signal_manager()),
        make_get_strategy_tool(list_strategies=_list_strategies),
        make_get_strategy_performance_tool(get_strategy_performance=get_strategy_performance),
        make_compare_strategies_tool(compare=_get_strategy_comparison),
    ]