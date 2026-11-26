"""§39 ToolRegistry framework + §40 allow-list guard + dispatch loop."""
import pytest

from core.ai.provider import AIResponse, ToolCall
from core.ai.tools import SPEC_TOOL_NAMES, Tool, ToolRegistry, ToolResult, run_tool_loop


def test_spec_tool_names_is_the_28_name_allow_list():
    # §39 / §40 — the registry may expose exactly these 28 names, no more, no less.
    assert SPEC_TOOL_NAMES == frozenset({
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
    assert len(SPEC_TOOL_NAMES) == 28


def test_registry_rejects_name_outside_allow_list():
    reg = ToolRegistry()
    rogue = Tool(name="run_shell", description="x", arguments_schema={},
                 execute=lambda **a: ToolResult(ok=True, data={}))
    with pytest.raises(ValueError):
        reg.register(rogue)


def test_registry_rejects_duplicate_name():
    reg = ToolRegistry()
    t = Tool(name="get_signal", description="x", arguments_schema={},
             execute=lambda **a: ToolResult(ok=True, data={}))
    reg.register(t)
    with pytest.raises(ValueError):
        reg.register(t)


def test_registry_dispatch_calls_execute_with_arguments():
    reg = ToolRegistry()
    captured = {}

    def execute(**arguments):
        captured.update(arguments)
        return ToolResult(ok=True, data={"echo": arguments.get("ticker")})

    reg.register(Tool(name="get_signal", description="x",
                      arguments_schema={"type": "object", "properties": {"ticker": {"type": "string"}}},
                      execute=execute))
    result = reg.dispatch(ToolCall(id="1", name="get_signal", arguments={"ticker": "NVDA"}))
    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.data == {"echo": "NVDA"}
    assert captured == {"ticker": "NVDA"}


def test_registry_dispatch_unknown_tool_returns_error_result():
    reg = ToolRegistry()
    result = reg.dispatch(ToolCall(id="1", name="get_signal", arguments={}))
    assert result.ok is False
    assert "get_signal" in result.error


def test_registry_dispatch_execute_raises_returns_error_result():
    reg = ToolRegistry()

    def boom(**arguments):
        raise RuntimeError("boom")

    reg.register(Tool(name="get_signal", description="x", arguments_schema={}, execute=boom))
    result = reg.dispatch(ToolCall(id="1", name="get_signal", arguments={}))
    assert result.ok is False
    assert "boom" in result.error


def test_list_tools_emits_vendor_payload_with_name_description_schema():
    reg = ToolRegistry()
    reg.register(Tool(name="get_signal", description="Get a signal by id.",
                      arguments_schema={"type": "object", "properties": {"ticker": {"type": "string"}}},
                      execute=lambda **a: ToolResult(ok=True, data={})))
    payload = reg.list_tools()
    assert isinstance(payload, list) and len(payload) == 1
    entry = payload[0]
    # OpenAI/Anthropic/Gemini all carry name + description + parameters/input_schema;
    # the providers (3A/3B) pass `tools` straight through, so a flat dict with these
    # three keys is the portable shape. Provider adapters normalize as needed.
    assert entry["name"] == "get_signal"
    assert entry["description"] == "Get a signal by id."
    assert entry["parameters"]["type"] == "object"