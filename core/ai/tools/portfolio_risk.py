"""§39 portfolio + risk tools — §43 authoritative delegation.

These tools NEVER compute capital/size/risk independently. They delegate to the
authoritative app services (capital_manager / position_sizing / risk_limits /
risk_agent). AI-supplied numbers are not authoritative (§43).
"""
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


def make_get_portfolio_tool(*, capital_manager) -> Tool:
    def execute(**a: Any) -> ToolResult:
        try:
            data = {"positions": capital_manager.get_positions() or []}
        except Exception as exc:
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        return ToolResult(ok=True, data=data)
    return Tool(
        name="get_portfolio",
        description="Return the open positions (authoritative: capital_manager.get_positions).",
        arguments_schema={"type": "object", "properties": {}},
        execute=execute,
    )


def make_get_position_tool(*, capital_manager) -> Tool:
    def execute(**a: Any) -> ToolResult:
        pid = a.get("position_id")
        if not pid:
            return ToolResult(ok=False, error="position_id is required")
        try:
            pos = capital_manager.get_position(pid)
        except Exception as exc:
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        if not pos:
            return ToolResult(ok=False, error=f"position not found: {pid}")
        return ToolResult(ok=True, data=pos if isinstance(pos, dict) else {"value": pos})
    return Tool(
        name="get_position",
        description="Return a single position by id (authoritative: capital_manager.get_position).",
        arguments_schema={"type": "object", "properties": {"position_id": {"type": "string"}}, "required": ["position_id"]},
        execute=execute,
    )


def make_get_portfolio_risk_tool(*, evaluate_signal: Callable) -> Tool:
    def execute(**a: Any) -> ToolResult:
        return _safe(evaluate_signal, a.get("signal") or {})
    return Tool(
        name="get_portfolio_risk",
        description="Evaluate portfolio/signal risk (authoritative: RiskAgent.evaluate_signal + risk_limits).",
        arguments_schema={"type": "object", "properties": {"signal": {"type": "object"}}},
        execute=execute,
    )


def make_get_available_capital_tool(*, capital_manager) -> Tool:
    def execute(**a: Any) -> ToolResult:
        try:
            cap = float(capital_manager.get_deployable_balance())
        except Exception as exc:
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        return ToolResult(ok=True, data={"available_capital": cap})
    return Tool(
        name="get_available_capital",
        description="Return available (deployable) capital. §43 — authoritative: capital_manager.get_deployable_balance; AI must not compute this.",
        arguments_schema={"type": "object", "properties": {}},
        execute=execute,
    )


def make_calculate_allowed_position_size_tool(*, compute_size_factor: Callable,
                                              calculate_trade_risk_pct: Callable,
                                              capital_manager) -> Tool:
    def execute(**a: Any) -> ToolResult:
        try:
            score = float(a.get("score", 0))
            notional = float(a.get("notional_usd", 0))
            stop_frac = float(a.get("stop_loss_fraction", 0))
            equity = float(capital_manager.get_deployable_balance())
        except Exception as exc:
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        size = compute_size_factor(score)
        if not isinstance(size, dict):
            size = {"size_factor": size}
        loss_pct = calculate_trade_risk_pct(notional_usd=notional, stop_loss_fraction=stop_frac, equity=equity)
        out = dict(size)
        out["loss_at_stop_pct"] = loss_pct
        out["equity_used"] = equity
        return ToolResult(ok=True, data=out)
    return Tool(
        name="calculate_allowed_position_size",
        description="Compute the allowed position size + loss-at-stop %. §43 — authoritative: position_sizing.compute_size_factor + risk_limits.calculate_trade_risk_pct; AI must not compute these.",
        arguments_schema={"type": "object",
                          "properties": {"score": {"type": "number"}, "notional_usd": {"type": "number"},
                                         "stop_loss_fraction": {"type": "number"}},
                          "required": ["score", "notional_usd", "stop_loss_fraction"]},
        execute=execute,
    )


def portfolio_risk_tools() -> List[Tool]:
    from core.capital_manager import get_capital_manager
    from core.position_sizing import compute_size_factor
    from core.risk_limits import calculate_trade_risk_pct
    from agents.risk_agent import RiskAgent
    from core.config import ConfigManager
    cm = get_capital_manager()
    # RiskAgent requires a config_manager (BaseAgent.__init__ does NOT start a
    # thread — only .start() does — so constructing it here is side-effect-free).
    _risk = RiskAgent(ConfigManager())
    return [
        make_get_portfolio_tool(capital_manager=cm),
        make_get_position_tool(capital_manager=cm),
        make_get_portfolio_risk_tool(evaluate_signal=_risk.evaluate_signal),
        make_get_available_capital_tool(capital_manager=cm),
        make_calculate_allowed_position_size_tool(
            compute_size_factor=compute_size_factor,
            calculate_trade_risk_pct=calculate_trade_risk_pct,
            capital_manager=cm),
    ]