"""§39 propose_order / propose_close — DRAFT-ONLY order proposals (§40/§42).

These tools return a draft order dict for operator/pipeline review. They NEVER
call CapitalManager.open_position / close_position or ExecutionAgent.process_signal
(AI prose alone must never trigger an order, §42; the broker surface is excluded
from the allow-list, §40). Numbers are app-authoritative (§43): available capital
from get_deployable_balance, size from compute_size_factor, loss-at-stop from
calculate_trade_risk_pct, entry evidence from evaluate_capital_entry. A draft does
NOT embed a reference price (§43 — price is authoritative and computed at
execution time, not at proposal time).
"""
from __future__ import annotations

from typing import Any, Callable, List

from core.ai.tools.tool import Tool, ToolResult


def make_propose_order_tool(*, capital_manager, compute_size_factor: Callable,
                            calculate_trade_risk_pct: Callable,
                            evaluate_capital_entry: Callable) -> Tool:
    def execute(**a: Any) -> ToolResult:
        try:
            ticker = a["ticker"]
            direction = a["direction"]
            score = float(a.get("score", 0))
            notional = float(a.get("notional_usd", 0))
            stop_frac = float(a.get("stop_loss_fraction", a.get("stop_loss_pct", 0)))
            deployable = float(capital_manager.get_deployable_balance())
        except Exception as exc:
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        size = compute_size_factor(score)
        if not isinstance(size, dict):
            size = {"size_factor": size}
        loss_pct = calculate_trade_risk_pct(notional_usd=notional, stop_loss_fraction=stop_frac, equity=deployable)
        signal = {"ticker": ticker, "direction": direction, "score": score}
        try:
            entry = evaluate_capital_entry(signal, capital_manager.get_positions() or [], None) or {}
        except Exception as exc:
            entry = {"allowed": False, "reason": f"entry-eval error: {exc}"}
        draft = {
            "draft": True,
            "ticker": ticker,
            "direction": direction,
            "notional_usd": notional,
            "stop_loss_pct": a.get("stop_loss_pct"),
            "take_profit_pct": a.get("take_profit_pct"),
            "available_capital": deployable,
            "size": size,
            "loss_at_stop_pct": loss_pct,
            "capital_entry": entry,
        }
        return ToolResult(ok=True, data=draft)
    return Tool(
        name="propose_order",
        description="Propose a DRAFT order (never executes; §42 AI prose alone never triggers an order). Returns size + loss-at-stop + capital-entry evidence.",
        arguments_schema={"type": "object",
                          "properties": {"ticker": {"type": "string"}, "direction": {"type": "string", "enum": ["LONG", "SHORT"]},
                                         "score": {"type": "number"}, "notional_usd": {"type": "number"},
                                         "stop_loss_pct": {"type": "number"}, "take_profit_pct": {"type": "number"},
                                         "stop_loss_fraction": {"type": "number"}},
                          "required": ["ticker", "direction"]},
        execute=execute,
    )


def make_propose_close_tool(*, capital_manager) -> Tool:
    def execute(**a: Any) -> ToolResult:
        pid = a.get("position_id")
        reason = a.get("reason", "")
        if not pid:
            return ToolResult(ok=False, error="position_id is required")
        try:
            pos = capital_manager.get_position(pid)
        except Exception as exc:
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        if not pos:
            return ToolResult(ok=False, error=f"position not found: {pid}")
        # No reference price: §43 — price is app-authoritative and computed at
        # execution time, not embedded in a draft proposal.
        draft = {
            "draft": True,
            "position_id": pid,
            "ticker": pos.get("ticker"),
            "direction": pos.get("direction"),
            "exit_reason": reason,
        }
        return ToolResult(ok=True, data=draft)
    return Tool(
        name="propose_close",
        description="Propose a DRAFT close for an open position (never executes; §42).",
        arguments_schema={"type": "object",
                          "properties": {"position_id": {"type": "string"}, "reason": {"type": "string"}},
                          "required": ["position_id"]},
        execute=execute,
    )


def order_proposal_tools() -> List[Tool]:
    from core.capital_manager import get_capital_manager
    from core.position_sizing import compute_size_factor
    from core.risk_limits import calculate_trade_risk_pct
    from core.capital_entry_guard import evaluate_capital_entry
    cm = get_capital_manager()
    return [
        make_propose_order_tool(
            capital_manager=cm,
            compute_size_factor=compute_size_factor,
            calculate_trade_risk_pct=calculate_trade_risk_pct,
            evaluate_capital_entry=evaluate_capital_entry),
        make_propose_close_tool(capital_manager=cm),
    ]