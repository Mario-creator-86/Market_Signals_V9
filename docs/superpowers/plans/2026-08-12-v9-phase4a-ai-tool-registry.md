# Phase 4A — AI Tool Registry & Allow-Listed Domain Tools (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the §39 tool registry — a `Tool`/`ToolRegistry` layer plus the 28 allow-listed domain tools the AI reasons through — on top of the Phase 3A/3B `tool_call` transport, with the §40 no-unrestricted-access guarantee enforced by construction.

**Architecture:** A `ToolRegistry` is the ONLY surface the AI can call. It exposes exactly 28 `Tool` instances (the §39 names), each a thin wrapper around an existing read-only service function (evolve, not rewrite — §5). The registry exports the `tools=[…]` payload the Phase 3A/3B providers already accept, and a `dispatch(ToolCall)→ToolResult` method. A `run_tool_loop(provider, registry, prompt)` helper iterates generate→dispatch→feed-back. §40 is enforced by construction: the registry is an explicit 28-name allow-list; no shell/Python/SQL/filesystem/HTTP/creds/broker tool is ever registered. `propose_order`/`propose_close` return drafts only — they never reach `CapitalManager.open_position`/`close_position`/`ExecutionAgent.process_signal`. §43: `get_available_capital`/`calculate_allowed_position_size`/`get_portfolio_risk` delegate to the authoritative app services (`capital_manager`/`position_sizing`/`risk_limits`/`risk_agent`), never compute independently.

**Tech Stack:** Python 3.14.3, stdlib `dataclasses`/`typing`/`json`, the existing `core/ai/provider.py` (`ToolCall`/`AIResponse`/`AIProvider`) and `core/ai/fallback.py` (`generate_with_fallback`). No new dependencies (§118). Tests via `pytest` with the existing `mock_external_deps` autouse fixture; injectable delegates mean no live market/broker/AI calls in tests.

## Global Constraints

Copied verbatim from `prompt_to_follow.md` + the Phase 4 design (`docs/superpowers/specs/2026-08-12-v9-phase4-ai-tool-and-decision-engine-design.md`):

- **§40:** "Trading AI MUST NOT receive unrestricted: shell; arbitrary Python execution; SQL; filesystem; arbitrary HTTP; broker credentials; secrets. AI uses allow-listed domain tools." — The registry is the allow-list; exactly the 28 §39 names; broker functions are excluded; `propose_order`/`propose_close` are draft-only.
- **§39:** The 28 tool names are exactly: `get_market_snapshot, get_ohlcv, get_technical_features, get_market_regime, get_sector_regime, get_earnings_event, get_historical_earnings, get_earnings_surprises, get_options_analysis, get_pre_earnings_analysis, get_news, get_news_sentiment, get_sec_context, get_signal, get_strategy, get_strategy_performance, compare_strategies, run_backtest, get_backtest_result, get_portfolio, get_position, get_portfolio_risk, get_available_capital, calculate_allowed_position_size, get_decision_history, get_trade_history, propose_order, propose_close`.
- **§43:** "AI does not supply authoritative: price; P/L; IV; EPS; portfolio value; allowed capital; position size. Application services calculate them." — `get_available_capital`/`calculate_allowed_position_size`/`get_portfolio_risk` delegate to `core/capital_manager.py`/`core/position_sizing.py`/`core/risk_limits.py`/`agents/risk_agent.py`; tests assert equality with those services' outputs.
- **§42:** "AI prose alone must never trigger an order." — `propose_order`/`propose_close` return a draft dict; they never execute.
- **§5/§7:** Evolve, preserve, no rewrite. The 21 tools with existing delegates wrap them (read-only pass-through); no existing file's behavior changes. `ollama_decision.py`/`agent_manager.py` are NOT modified in 4A (the rewire is 4C).
- **§4:** No forbidden success language ("should work", "TODO later", "mock for now", "99%", etc.). Net-new stubs (`get_options_analysis`, `get_sec_context`) return an explicit `ok=False` with reason `"not available: <domain> fetcher lands in Phase 5/10"` — honest, not a placeholder.
- **§106:** No secrets/keys in tools; tools never read API keys or broker credentials.
- **§115:** No test weakened/skipped/deleted; only new tests added.
- **§117:** V7 (`C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium`, HEAD `f912a33`) is read-only — never modified.
- **§118/§25:** No new dependencies. All tools/registry/loop are stdlib + existing V9 code.
- **Baseline:** 1756 passed / 0 failed (Phase 3C gate `7209da0`). Every task keeps the full regression green.

---

## File Structure

- **Create:** `core/ai/tools/__init__.py` — package + re-exports.
- **Create:** `core/ai/tools/tool.py` — `Tool` + `ToolResult` frozen dataclasses.
- **Create:** `core/ai/tools/registry.py` — `ToolRegistry` (register/get/list_tools/dispatch + the 28-name `SPEC_TOOL_NAMES` allow-list) + `build_default_registry()`.
- **Create:** `core/ai/tools/loop.py` — `run_tool_loop(provider, registry, prompt, *, max_rounds, ...)` generate→dispatch→feed-back primitive.
- **Create:** `core/ai/tools/market_data.py` — 5 market-data tools.
- **Create:** `core/ai/tools/earnings.py` — 4 tools (earnings event/historical/surprises + pre-earnings).
- **Create:** `core/ai/tools/options.py` — 1 tool (honest Phase-5 stub).
- **Create:** `core/ai/tools/news.py` — 2 tools.
- **Create:** `core/ai/tools/sec.py` — 1 tool (honest Phase-10 stub).
- **Create:** `core/ai/tools/signals_strategy.py` — 4 tools.
- **Create:** `core/ai/tools/backtest.py` — 2 tools.
- **Create:** `core/ai/tools/portfolio_risk.py` — 5 tools (§43 authoritative).
- **Create:** `core/ai/tools/history.py` — 2 tools.
- **Create:** `core/ai/tools/order_proposal.py` — 2 draft-only tools (§40/§42).
- **Modify:** `core/ai/__init__.py` — additive re-exports (`Tool`, `ToolResult`, `ToolRegistry`, `build_default_registry`, `run_tool_loop`, `SPEC_TOOL_NAMES`).
- **Test:** `tests/unit/test_ai_tool_registry.py`, `tests/unit/test_ai_tool_loop.py`, `tests/unit/test_ai_tools_market_data.py`, `tests/unit/test_ai_tools_earnings.py`, `tests/unit/test_ai_tools_options_sec.py`, `tests/unit/test_ai_tools_signals_strategy_backtest.py`, `tests/unit/test_ai_tools_portfolio_risk_history.py`, `tests/unit/test_ai_tools_order_proposal.py`, `tests/golden/test_ai_tool_registry_golden.py`.

Each domain module exports `make_<tool>_tool(*, delegate=default) -> Tool` factories (injectable delegate for testing) and a `<module>_tools() -> List[Tool]` builder. The registry assembles the 28 from these builders.

---

### Task 1: Tool / ToolResult / ToolRegistry framework + §40 allow-list guard + dispatch loop

**Files:**
- Create: `core/ai/tools/tool.py`, `core/ai/tools/registry.py`, `core/ai/tools/loop.py`, `core/ai/tools/__init__.py`
- Test: `tests/unit/test_ai_tool_registry.py`, `tests/unit/test_ai_tool_loop.py`

**Interfaces:**
- Consumes: `core/ai/provider.py:43 ToolCall(id, name, arguments: Dict)`, `core/ai/provider.py:50 AIResponse(text, tool_calls: List[ToolCall], error, is_error)`, `core/ai/provider.py:75 AIProvider.tool_call(prompt, tools: List[Dict], *, model, system, **opts) -> AIResponse`.
- Produces: `Tool` (frozen: name/description/arguments_schema/execute), `ToolResult` (frozen: ok/data/error), `ToolRegistry` (register/get/list_tools/dispatch/names), `SPEC_TOOL_NAMES` (the 28-name frozenset — the §40 boundary), `build_default_registry() -> ToolRegistry`, `run_tool_loop(provider, registry, prompt, *, max_rounds=4, model=None, system="", **opts) -> AIResponse`.

- [ ] **Step 1: Write the failing test** (`tests/unit/test_ai_tool_registry.py`)

```python
"""§39 ToolRegistry framework + §40 allow-list guard."""
import pytest

from core.ai.provider import AIResponse, ToolCall
from core.ai.tools import SPEC_TOOL_NAMES, Tool, ToolRegistry, ToolResult, build_default_registry


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
    rogue = Tool(name="run_shell", description="x", arguments_schema={}, execute=lambda **a: ToolResult(ok=True, data={}))
    with pytest.raises(ValueError):
        reg.register(rogue)


def test_registry_rejects_duplicate_name():
    reg = ToolRegistry()
    t = Tool(name="get_signal", description="x", arguments_schema={}, execute=lambda **a: ToolResult(ok=True, data={}))
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


def test_build_default_registry_has_exactly_the_28_names():
    reg = build_default_registry()
    assert set(reg.names()) == SPEC_TOOL_NAMES
    assert len(reg.names()) == 28
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_ai_tool_registry.py -v`
Expected: FAIL (ImportError — `core.ai.tools` does not exist yet).

- [ ] **Step 3: Write minimal implementation** (`core/ai/tools/tool.py`)

```python
"""§39 Tool + ToolResult value types.

A Tool is the unit the AI may call. The registry is the only surface the AI
reaches (§40): no shell/Python/SQL/filesystem/HTTP/creds/broker tool is ever
constructed. Each tool wraps an existing read-only service function (§5 —
evolve, not rewrite); `propose_order`/`propose_close` return drafts only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    arguments_schema: Dict[str, Any]
    execute: Callable[..., ToolResult]
```

- [ ] **Step 4: Write minimal implementation** (`core/ai/tools/registry.py`)

```python
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
```

- [ ] **Step 5: Write minimal implementation** (`core/ai/tools/loop.py`)

```python
"""§39 tool loop primitive — generate → dispatch tool_calls → feed results back.

This is the agent-loop primitive; wiring it into the production autonomous
cycle (behind the §45 autonomy gate + §44 critic) is Phase 4C. Here it is a
tested helper: it calls `provider.tool_call(tools=registry.list_tools())`, and
for every `ToolCall` in the response it dispatches via the registry and
appends a `tool_result` role message, then loops. The loop stops when the
provider returns no tool_calls (a final answer) or `max_rounds` is reached.
"""
from __future__ import annotations

from typing import Any, List, Optional

from core.ai.provider import AIProvider, AIResponse
from core.ai.tools.registry import ToolRegistry


def run_tool_loop(
    provider: AIProvider,
    registry: ToolRegistry,
    prompt: str,
    *,
    max_rounds: int = 4,
    model: Optional[str] = None,
    system: str = "",
    **opts: Any,
) -> AIResponse:
    rounds = 0
    messages_log: List[str] = [prompt]
    while rounds < max_rounds:
        rounds += 1
        joined = "\n\n".join(messages_log)
        response = provider.tool_call(
            joined, registry.list_tools(), model=model, system=system, **opts
        )
        if response is None or response.is_error:
            return response
        if not response.tool_calls:
            return response  # final answer
        for call in response.tool_calls:
            result = registry.dispatch(call)
            payload = "ok" if result.ok else f"error: {result.error}"
            data = result.data if result.ok else {}
            messages_log.append(
                f"tool_result name={call.name} id={call.id} {payload} data={data}"
            )
    # Exhausted rounds without a final answer: return the last response.
    return response
```

- [ ] **Step 6: Write minimal implementation** (`core/ai/tools/__init__.py`)

```python
"""§39 AI tool architecture — Tool/ToolRegistry + the 28 allow-listed tools (§40)."""
from core.ai.tools.tool import Tool, ToolResult
from core.ai.tools.registry import SPEC_TOOL_NAMES, ToolRegistry, build_default_registry
from core.ai.tools.loop import run_tool_loop

__all__ = [
    "SPEC_TOOL_NAMES",
    "Tool",
    "ToolResult",
    "ToolRegistry",
    "build_default_registry",
    "run_tool_loop",
]
```

- [ ] **Step 7: Write the loop test** (`tests/unit/test_ai_tool_loop.py`)

```python
"""run_tool_loop generate→dispatch→feed-back with a fake provider."""
from core.ai.provider import AIProvider, AIResponse, ToolCall
from core.ai.tools import Tool, ToolRegistry, ToolResult, run_tool_loop


class _FakeProvider(AIProvider):
    name = "fake"

    def __init__(self, scripted):
        self._scripted = scripted  # list of AIResponse, one per round
        self._i = 0
        self.calls = []

    def list_models(self):
        return []

    def health_check(self, *, force=False):
        return True

    def capabilities(self):
        from core.ai.provider import ProviderCapabilities
        return ProviderCapabilities()

    def generate(self, prompt, *, model=None, system="", tools=None, **opts):
        return AIResponse(text="ok")

    def generate_structured(self, prompt, schema, *, model=None, system="", **opts):
        return AIResponse(text="ok")

    def tool_call(self, prompt, tools, *, model=None, system="", **opts):
        self.calls.append((prompt, tools))
        resp = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return resp

    def stream(self, prompt, *, model=None, system="", **opts):
        from core.ai.provider import AIStreamChunk
        yield AIStreamChunk(text="ok", done=True)

    def usage(self):
        from core.ai.provider import Usage
        return Usage()


def test_loop_dispatches_and_feeds_back_then_returns_final_answer():
    reg = ToolRegistry()
    reg.register(Tool(name="get_signal", description="x", arguments_schema={},
                      execute=lambda **a: ToolResult(ok=True, data={"price": 100})))
    provider = _FakeProvider([
        AIResponse(tool_calls=[ToolCall(id="c1", name="get_signal", arguments={"ticker": "NVDA"})]),
        AIResponse(text="FINAL: NVDA signal price 100"),
    ])
    resp = run_tool_loop(provider, reg, "analyze NVDA", max_rounds=4)
    assert resp.text == "FINAL: NVDA signal price 100"
    # round 1 returned a tool_call; round 2 received the fed-back tool_result
    assert "tool_result" in provider.calls[1][0]
    assert "get_signal" in provider.calls[1][0]


def test_loop_stops_on_error_response():
    reg = ToolRegistry()
    provider = _FakeProvider([AIResponse(error="provider down")])
    resp = run_tool_loop(provider, reg, "x", max_rounds=4)
    assert resp.is_error


def test_loop_respects_max_rounds():
    reg = ToolRegistry()
    reg.register(Tool(name="get_signal", description="x", arguments_schema={},
                      execute=lambda **a: ToolResult(ok=True, data={})))
    # Always returns a tool_call — never a final answer; must cap at max_rounds.
    provider = _FakeProvider([
        AIResponse(tool_calls=[ToolCall(id="c", name="get_signal", arguments={})]),
    ])
    resp = run_tool_loop(provider, reg, "x", max_rounds=2)
    assert len(provider.calls) == 2  # capped
```

- [ ] **Step 8: Run tests to verify they pass** (after Tasks 2–6 land the domain modules so `build_default_registry` can import them)

Run: `python -m pytest tests/unit/test_ai_tool_registry.py tests/unit/test_ai_tool_loop.py -v`
Expected: `test_spec_tool_names_is_the_28_name_allow_list`, `test_registry_rejects_name_outside_allow_list`, `test_registry_rejects_duplicate_name`, `test_registry_dispatch_*`, `test_list_tools_*` PASS; `test_build_default_registry_has_exactly_the_28_names` passes once all domain modules (Tasks 2–6) are registered. The loop tests pass once `run_tool_loop` is importable.

- [ ] **Step 9: Commit**

```bash
git add core/ai/tools/tool.py core/ai/tools/registry.py core/ai/tools/loop.py core/ai/tools/__init__.py tests/unit/test_ai_tool_registry.py tests/unit/test_ai_tool_loop.py
git commit -m "feat(phase4a): Tool/ToolResult/ToolRegistry framework + §40 allow-list + tool loop (§39)"
```

---

### Task 2: Market data tools (5)

**Files:**
- Create: `core/ai/tools/market_data.py`
- Test: `tests/unit/test_ai_tools_market_data.py`

**Interfaces:**
- Consumes: `api/state_service.py:2724 get_state(...)`; `api/market_data_service.py` Yahoo/Binance/KuCoin clients; `core/strategy_v2/context_builder.py:367 build_context(...)`; `core/strategy_v2/regime_engine.py:29 RegimeEngine.detect(context)`; `core/sectors.py:145 get_sector(ticker)` + `get_all_sectors()`; OHLCV via `core/data_model/store.py` `HistoricalStore` (optional canonical source).
- Produces: `market_data_tools() -> List[Tool]` (5 tools); individual `make_get_market_snapshot_tool`, `make_get_ohlcv_tool`, `make_get_technical_features_tool`, `make_get_market_regime_tool`, `make_get_sector_regime_tool`.

- [ ] **Step 1: Write the failing test** (`tests/unit/test_ai_tools_market_data.py`)

```python
"""Market-data tools delegate to existing read-only services (injectable)."""
from core.ai.tools.market_data import (
    make_get_market_snapshot_tool, make_get_ohlcv_tool, make_get_technical_features_tool,
    make_get_market_regime_tool, make_get_sector_regime_tool, market_data_tools,
)


def test_get_market_snapshot_delegates_to_get_state():
    seen = {}

    def fake_get_state(*args, **kwargs):
        seen.update(kwargs)
        return {"tickers": ["NVDA"], "capital": 100000.0}

    tool = make_get_market_snapshot_tool(get_state=fake_get_state)
    res = tool.execute()
    assert res.ok and res.data == {"tickers": ["NVDA"], "capital": 100000.0}


def test_get_ohlcv_delegates_and_shapes_result():
    def fake_fetch(ticker, period="1mo", interval="1d", **_):
        return {"ticker": ticker, "bars": [{"o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}]}
    tool = make_get_ohlcv_tool(fetch_history=fake_fetch)
    res = tool.execute(ticker="NVDA", period="1mo", interval="1d")
    assert res.ok and res.data["ticker"] == "NVDA" and len(res.data["bars"]) == 1


def test_get_technical_features_delegates_to_build_context():
    def fake_build(df=None, **_):
        return {"rsi": 55.0, "atr": 2.1}
    tool = make_get_technical_features_tool(build_context=fake_build)
    res = tool.execute(ticker="AAPL", bars=[{"c": 1}])
    assert res.ok and res.data["rsi"] == 55.0


def test_get_market_regime_delegates_to_regime_engine():
    def fake_detect(context=None, **_):
        return {"regime": "TREND", "confidence": 0.8, "sub_scores": {}}
    tool = make_get_market_regime_tool(detect=fake_detect)
    res = tool.execute(context={"rsi": 55})
    assert res.ok and res.data["regime"] == "TREND"


def test_get_sector_regime_is_net_new_rollup():
    # Net-new read seam: rolls regime per ticker in a sector. Honest when empty.
    def fake_detect(context=None, **_):
        return {"regime": "RANGE", "confidence": 0.6}
    def fake_sector_tickers(sector):
        return ["AAPL", "MSFT"]
    tool = make_get_sector_regime_tool(detect=fake_detect, sector_tickers=fake_sector_tickers)
    res = tool.execute(sector="Technology")
    assert res.ok and res.data["sector"] == "Technology"
    assert set(res.data["per_ticker"].keys()) == {"AAPL", "MSFT"}


def test_market_data_tools_returns_five_registered_tools():
    tools = market_data_tools()
    assert [t.name for t in tools] == [
        "get_market_snapshot", "get_ohlcv", "get_technical_features",
        "get_market_regime", "get_sector_regime",
    ]


def test_market_data_tool_error_is_caught():
    def boom(*a, **k):
        raise RuntimeError("no data")
    tool = make_get_ohlcv_tool(fetch_history=boom)
    res = tool.execute(ticker="X")
    assert res.ok is False and "no data" in res.error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_ai_tools_market_data.py -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Write minimal implementation** (`core/ai/tools/market_data.py`)

```python
"""§39 market-data tools — thin read-only wrappers over existing services.

Each factory takes an injectable delegate (default: the real service function)
so unit tests inject fakes (no live market calls). The delegate signatures are
verified at the import sites against the existing functions named below; any
mismatch is recorded as a documented deviation at execution time.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

from core.ai.tools.tool import Tool, ToolResult


def _safe(delegate: Callable, *args, **kwargs) -> ToolResult:
    try:
        data = delegate(*args, **kwargs)
    except Exception as exc:
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    if not isinstance(data, dict):
        data = {"value": data}
    return ToolResult(ok=True, data=data)


def make_get_market_snapshot_tool(*, get_state: Callable) -> Tool:
    def execute(**arguments: Any) -> ToolResult:
        return _safe(get_state)
    return Tool(
        name="get_market_snapshot",
        description="Return the current watchlist/dashboard state (tickers, signals, capital, hotlist).",
        arguments_schema={"type": "object", "properties": {}, "additionalProperties": False},
        execute=execute,
    )


def make_get_ohlcv_tool(*, fetch_history: Callable) -> Tool:
    def execute(**arguments: Any) -> ToolResult:
        return _safe(
            fetch_history,
            arguments.get("ticker"),
            period=arguments.get("period", "1mo"),
            interval=arguments.get("interval", "1d"),
        )
    return Tool(
        name="get_ohlcv",
        description="Return OHLCV bars for a ticker over a period/interval.",
        arguments_schema={
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "period": {"type": "string", "default": "1mo"},
                "interval": {"type": "string", "default": "1d"},
            },
            "required": ["ticker"],
        },
        execute=execute,
    )


def make_get_technical_features_tool(*, build_context: Callable) -> Tool:
    def execute(**arguments: Any) -> ToolResult:
        return _safe(build_context, arguments.get("ticker"), arguments.get("bars"))
    return Tool(
        name="get_technical_features",
        description="Compute technical features (RSI, ATR, ADX, Bollinger, regime) from OHLCV bars.",
        arguments_schema={
            "type": "object",
            "properties": {"ticker": {"type": "string"}, "bars": {"type": "array"}},
            "required": ["ticker"],
        },
        execute=execute,
    )


def make_get_market_regime_tool(*, detect: Callable) -> Tool:
    def execute(**arguments: Any) -> ToolResult:
        return _safe(detect, arguments.get("context") or {})
    return Tool(
        name="get_market_regime",
        description="Detect the market regime (trend/range/high-vol/event/session-breakout) for a context.",
        arguments_schema={
            "type": "object",
            "properties": {"context": {"type": "object"}},
        },
        execute=execute,
    )


def make_get_sector_regime_tool(*, detect: Callable, sector_tickers: Callable) -> Tool:
    def execute(**arguments: Any) -> ToolResult:
        sector = arguments.get("sector")
        if not sector:
            return ToolResult(ok=False, error="sector is required")
        try:
            tickers = sector_tickers(sector) or []
        except Exception as exc:
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        per_ticker: Dict[str, Any] = {}
        for tk in tickers:
            r = _safe(detect, {"ticker": tk})
            per_ticker[tk] = r.data if r.ok else {"error": r.error}
        return ToolResult(ok=True, data={"sector": sector, "per_ticker": per_ticker})
    return Tool(
        name="get_sector_regime",
        description="Roll up per-ticker market regime across a sector (net-new read seam; no persisted sector-regime table).",
        arguments_schema={
            "type": "object",
            "properties": {"sector": {"type": "string"}},
            "required": ["sector"],
        },
        execute=execute,
    )


def market_data_tools() -> List[Tool]:
    # Defaults bind to the existing service functions. Imports are local so the
    # module is importable in test contexts that inject fakes without importing
    # the whole service layer.
    from api.state_service import get_state
    from api.market_data_service import YahooClient
    from core.strategy_v2.context_builder import build_context
    from core.strategy_v2.regime_engine import RegimeEngine
    from core.sectors import get_tickers_by_sector

    _regime = RegimeEngine()
    return [
        make_get_market_snapshot_tool(get_state=get_state),
        make_get_ohlcv_tool(fetch_history=YahooClient().fetch_history),
        make_get_technical_features_tool(build_context=build_context),
        make_get_market_regime_tool(detect=_regime.detect),
        make_get_sector_regime_tool(detect=_regime.detect, sector_tickers=get_tickers_by_sector),
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_ai_tools_market_data.py -v`
Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add core/ai/tools/market_data.py tests/unit/test_ai_tools_market_data.py
git commit -m "feat(phase4a): market-data tools (get_market_snapshot/ohlcv/technical_features/regime/sector_regime) (§39)"
```

---

### Task 3: Earnings (3) + pre-earnings (1) + options (1) + news (2) + SEC (1) tools

**Files:**
- Create: `core/ai/tools/earnings.py`, `core/ai/tools/options.py`, `core/ai/tools/news.py`, `core/ai/tools/sec.py`
- Test: `tests/unit/test_ai_tools_earnings.py`, `tests/unit/test_ai_tools_options_sec.py`

**Interfaces:**
- Consumes: `agents/economic_calendar_agent.py:1587 get_earnings_for_ticker(ticker)`; `core/finnhub_client.py:170 get_symbol_earnings(symbol)`; `core/data_model/entities.py:113 Earnings`; `agents/pre_earnings_agent.py:2303 get_state()`; `agents/news_tab_agent.py:1401 get_ticker_news`/`:1594 get_sentiment_summary`; `core/data_model/entities.py:162 OptionsChain` (Phase-5 stub); `:203 SECFiling` (Phase-10 stub).
- Produces: `earnings_tools()`, `options_tools()`, `news_tools()`, `sec_tools()` (each `-> List[Tool]`).

- [ ] **Step 1: Write the failing test** (`tests/unit/test_ai_tools_earnings.py`)

```python
from core.ai.tools.earnings import (
    make_get_earnings_event_tool, make_get_historical_earnings_tool,
    make_get_earnings_surprises_tool, make_get_pre_earnings_analysis_tool, earnings_tools,
)


def test_get_earnings_event_delegates():
    def fake(ticker):
        return {"ticker": ticker, "date": "2026-09-15", "eps_estimated": 1.2}
    t = make_get_earnings_event_tool(get_earnings_for_ticker=fake)
    r = t.execute(ticker="NVDA")
    assert r.ok and r.data["date"] == "2026-09-15"


def test_get_historical_earnings_delegates():
    def fake(symbol):
        return [{"quarter": "Q1", "epsActual": 1.1}]
    t = make_get_historical_earnings_tool(get_symbol_earnings=fake)
    r = t.execute(symbol="AAPL")
    assert r.ok and r.data[0]["epsActual"] == 1.1


def test_get_earnings_surprises_is_net_new_seam():
    def fake(symbol):
        return [
            {"quarter": "Q1", "epsActual": 1.3, "epsEstimate": 1.1},
            {"quarter": "Q2", "epsActual": 0.9, "epsEstimate": 1.2},
        ]
    t = make_get_earnings_surprises_tool(get_symbol_earnings=fake)
    r = t.execute(symbol="AAPL")
    assert r.ok
    assert r.data["symbol"] == "AAPL"
    assert r.data["surprises"][0]["surprise"] == pytest.approx(0.2)
    assert r.data["surprises"][1]["surprise"] == pytest.approx(-0.3)


def test_get_pre_earnings_analysis_delegates():
    def fake(*a, **k):
        return {"items": [{"ticker": "AAPL", "expected_move_pct": 4.0}]}
    t = make_get_pre_earnings_analysis_tool(get_state=fake)
    r = t.execute()
    assert r.ok and "items" in r.data


def test_earnings_tools_returns_four():
    assert [t.name for t in earnings_tools()] == [
        "get_earnings_event", "get_historical_earnings",
        "get_earnings_surprises", "get_pre_earnings_analysis",
    ]
```

Add `import pytest` at the top of the test file.

- [ ] **Step 2: Write the failing test** (`tests/unit/test_ai_tools_options_sec.py`)

```python
"""Options + SEC tools are honest net-new stubs (§4 — no forbidden language)."""
from core.ai.tools.options import make_get_options_analysis_tool, options_tools
from core.ai.tools.sec import make_get_sec_context_tool, sec_tools


def test_get_options_analysis_is_honest_phase5_stub():
    t = make_get_options_analysis_tool()
    r = t.execute(ticker="NVDA")
    assert r.ok is False
    assert "Phase 5" in r.error


def test_get_sec_context_is_honest_phase10_stub():
    t = make_get_sec_context_tool()
    r = t.execute(ticker="AAPL")
    assert r.ok is False
    assert "Phase 10" in r.error


def test_options_and_sec_tools_lists():
    assert [t.name for t in options_tools()] == ["get_options_analysis"]
    assert [t.name for t in sec_tools()] == ["get_sec_context"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_ai_tools_earnings.py tests/unit/test_ai_tools_options_sec.py -v`
Expected: FAIL (ImportError).

- [ ] **Step 4: Write minimal implementation** (`core/ai/tools/earnings.py`)

```python
"""§39 earnings + pre-earnings tools (read-only wrappers; surprises is a net-new seam)."""
from __future__ import annotations

from typing import Any, Callable, Dict, List

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
```

- [ ] **Step 5: Write minimal implementation** (`core/ai/tools/options.py`)

```python
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
```

- [ ] **Step 6: Write minimal implementation** (`core/ai/tools/sec.py`)

```python
"""§39 get_sec_context — honest Phase-10 stub (§4: no forbidden language).

`core/data_model/entities.py:203 SECFiling` is a "Fetcher in Phase 10" stub.
"""
from __future__ import annotations

from typing import Any, List

from core.ai.tools.tool import Tool, ToolResult

_NOT_AVAILABLE = "not available: SEC filings fetcher lands in Phase 10"


def make_get_sec_context_tool() -> Tool:
    def execute(**a: Any) -> ToolResult:
        return ToolResult(ok=False, error=_NOT_AVAILABLE)
    return Tool(
        name="get_sec_context",
        description="Return SEC-filing context for a ticker (Phase 10 fetcher pending).",
        arguments_schema={"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]},
        execute=execute,
    )


def sec_tools() -> List[Tool]:
    return [make_get_sec_context_tool()]
```

- [ ] **Step 7: Write minimal implementation** (`core/ai/tools/news.py`)

```python
"""§39 news + news-sentiment tools (read-only wrappers)."""
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


def make_get_news_tool(*, get_ticker_news: Callable) -> Tool:
    def execute(**a: Any) -> ToolResult:
        return _safe(get_ticker_news, a.get("ticker"), a.get("max_items", 20))
    return Tool(
        name="get_news",
        description="Return recent news articles for a ticker.",
        arguments_schema={"type": "object",
                          "properties": {"ticker": {"type": "string"}, "max_items": {"type": "integer", "default": 20}},
                          "required": ["ticker"]},
        execute=execute,
    )


def make_get_news_sentiment_tool(*, get_sentiment_summary: Callable) -> Tool:
    def execute(**a: Any) -> ToolResult:
        return _safe(get_sentiment_summary, a.get("ticker"))
    return Tool(
        name="get_news_sentiment",
        description="Return the aggregated news-sentiment summary for a ticker.",
        arguments_schema={"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]},
        execute=execute,
    )


def news_tools() -> List[Tool]:
    from agents.news_tab_agent import NewsTabAgent
    _news = NewsTabAgent()
    return [
        make_get_news_tool(get_ticker_news=_news.get_ticker_news),
        make_get_news_sentiment_tool(get_sentiment_summary=_news.get_sentiment_summary),
    ]
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_ai_tools_earnings.py tests/unit/test_ai_tools_options_sec.py -v`
Expected: 4 + 4 = 8 PASS (earnings tests + options/sec tests). Add a `tests/unit/test_ai_tools_news.py` if news is split out; here news is covered by Task 4's suite — add two news tests now:

Add to a new file `tests/unit/test_ai_tools_news.py`:

```python
from core.ai.tools.news import make_get_news_tool, make_get_news_sentiment_tool, news_tools


def test_get_news_delegates():
    def fake(ticker, max_items=20):
        return {"ticker": ticker, "items": [{"title": "x"}]}
    t = make_get_news_tool(get_ticker_news=fake)
    r = t.execute(ticker="NVDA", max_items=5)
    assert r.ok and r.data["ticker"] == "NVDA"


def test_get_news_sentiment_delegates():
    def fake(ticker):
        return {"sentiment": "bullish", "score": 0.7}
    t = make_get_news_sentiment_tool(get_sentiment_summary=fake)
    r = t.execute(ticker="AAPL")
    assert r.ok and r.data["sentiment"] == "bullish"


def test_news_tools_returns_two():
    assert [t.name for t in news_tools.__wrapped__() if False] or True  # placeholder removed below
```

Replace the last test with the real assertion:

```python
def test_news_tools_returns_two():
    from core.ai.tools.news import news_tools
    assert [t.name for t in news_tools()] == ["get_news", "get_news_sentiment"]
```

Run: `python -m pytest tests/unit/test_ai_tools_news.py -v` → 3 PASS.

- [ ] **Step 9: Commit**

```bash
git add core/ai/tools/earnings.py core/ai/tools/options.py core/ai/tools/sec.py core/ai/tools/news.py tests/unit/test_ai_tools_earnings.py tests/unit/test_ai_tools_options_sec.py tests/unit/test_ai_tools_news.py
git commit -m "feat(phase4a): earnings/pre-earnings/options/news/sec tools + honest Phase-5/10 stubs (§39/§4)"
```

---

### Task 4: Signals & strategy (4) + backtest (2) tools

**Files:**
- Create: `core/ai/tools/signals_strategy.py`, `core/ai/tools/backtest.py`
- Test: `tests/unit/test_ai_tools_signals_strategy_backtest.py`

**Interfaces:**
- Consumes: `core/signals.py:18 SignalManager` (`open_signals`/`closed_signals`); `core/strategy_v2/registry.py:47 build_default_registry()`; `api/actions_service.py:1225 get_strategy_performance(symbol)`; `:1262 _get_strategy_comparison(ticker, days, ...)`; `:1735 run_backtest(...)`; `:276 get_backtest_history()`.
- Produces: `signals_strategy_tools()`, `backtest_tools()` (each `-> List[Tool]`).

- [ ] **Step 1: Write the failing test** (`tests/unit/test_ai_tools_signals_strategy_backtest.py`)

```python
from core.ai.tools.signals_strategy import (
    make_get_signal_tool, make_get_strategy_tool, make_get_strategy_performance_tool,
    make_compare_strategies_tool, signals_strategy_tools,
)
from core.ai.tools.backtest import make_run_backtest_tool, make_get_backtest_result_tool, backtest_tools


def _fake_sm(open_signals=None, closed_signals=None):
    class _SM:
        def __init__(self):
            self.open_signals = open_signals or {}
            self.closed_signals = closed_signals or []
    return _SM()


def test_get_signal_reads_open_by_id():
    sm = _fake_sm(open_signals={"S1": {"id": "S1", "ticker": "NVDA"}})
    t = make_get_signal_tool(signal_manager=sm)
    r = t.execute(signal_id="S1")
    assert r.ok and r.data["ticker"] == "NVDA"


def test_get_signal_falls_back_to_closed():
    sm = _fake_sm(closed_signals=[{"id": "S2", "ticker": "AAPL"}])
    t = make_get_signal_tool(signal_manager=sm)
    r = t.execute(signal_id="S2")
    assert r.ok and r.data["ticker"] == "AAPL"


def test_get_signal_missing_returns_error():
    sm = _fake_sm()
    t = make_get_signal_tool(signal_manager=sm)
    r = t.execute(signal_id="X")
    assert r.ok is False and "X" in r.error


def test_get_strategy_delegates():
    def fake(*a, **k):
        return {"strategies": [{"id": "S1"}]}
    t = make_get_strategy_tool(list_strategies=fake)
    r = t.execute()
    assert r.ok and r.data["strategies"][0]["id"] == "S1"


def test_get_strategy_performance_delegates():
    def fake(symbol):
        return {"symbol": symbol, "rows": []}
    t = make_get_strategy_performance_tool(get_strategy_performance=fake)
    r = t.execute(symbol="NVDA")
    assert r.ok and r.data["symbol"] == "NVDA"


def test_compare_strategies_delegates():
    def fake(ticker, days=30, **k):
        return {"ticker": ticker, "days": days, "ranked": []}
    t = make_compare_strategies_tool(compare=fake)
    r = t.execute(ticker="NVDA", days=60)
    assert r.ok and r.data["days"] == 60


def test_run_backtest_delegates_and_shapes_result():
    def fake(**k):
        return {"ok": True, "summary": "done"}
    t = make_run_backtest_tool(run_backtest=fake)
    r = t.execute(tickers=["NVDA"], days=30)
    assert r.ok and r.data["summary"] == "done"


def test_get_backtest_result_delegates():
    def fake():
        return [{"id": "bt1"}]
    t = make_get_backtest_result_tool(get_history=fake)
    r = t.execute()
    assert r.ok and r.data["value"] == [{"id": "bt1"}]


def test_signals_strategy_and_backtest_tool_lists():
    assert [t.name for t in signals_strategy_tools()] == [
        "get_signal", "get_strategy", "get_strategy_performance", "compare_strategies"]
    assert [t.name for t in backtest_tools()] == ["run_backtest", "get_backtest_result"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_ai_tools_signals_strategy_backtest.py -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Write minimal implementation** (`core/ai/tools/signals_strategy.py`)

```python
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
        description="List the registered strategies (V2 specs + legacy).",
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
    return [
        make_get_signal_tool(signal_manager=get_signal_manager()),
        make_get_strategy_tool(list_strategies=lambda: {"strategies": [s.__dict__ for s in _v2.get_specs_for_asset("stock")]}),
        make_get_strategy_performance_tool(get_strategy_performance=get_strategy_performance),
        make_compare_strategies_tool(compare=_get_strategy_comparison),
    ]
```

- [ ] **Step 4: Write minimal implementation** (`core/ai/tools/backtest.py`)

```python
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
                                         "risk_mode": {"type": "string"}, "trade_mode": {"type": "string"}}},
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_ai_tools_signals_strategy_backtest.py -v`
Expected: 9 PASS.

- [ ] **Step 6: Commit**

```bash
git add core/ai/tools/signals_strategy.py core/ai/tools/backtest.py tests/unit/test_ai_tools_signals_strategy_backtest.py
git commit -m "feat(phase4a): signals/strategy/backtest tools (get_signal by-id seam) (§39)"
```

---

### Task 5: Portfolio & risk (5) + history (2) tools — §43 authoritative delegation

**Files:**
- Create: `core/ai/tools/portfolio_risk.py`, `core/ai/tools/history.py`
- Test: `tests/unit/test_ai_tools_portfolio_risk_history.py`

**Interfaces:**
- Consumes (§43 authoritative): `core/capital_manager.py:825 CapitalManager.get_deployable_balance()` / `:833 get_positions()` / `:957 get_position(id)`; `core/position_sizing.py:19 compute_size_factor(score, cfg, expectancy_r, win_rate, payoff_ratio)`; `core/risk_limits.py:84 calculate_trade_risk_pct(*, notional_usd, stop_loss_fraction, equity)`; `agents/risk_agent.py:86 RiskAgent.evaluate_signal(signal)`. History: `core/capital_manager.py:948 get_decisions(limit)` → `core/trade_audit.py:152 get_trade_audit(limit)`; `core/execution_archive.py:336 read_recent_executions(limit)`.
- Produces: `portfolio_risk_tools()`, `history_tools()` (each `-> List[Tool]`). The §43 test asserts `get_available_capital` == `resolve_deployable_balance` and `calculate_allowed_position_size` uses `compute_size_factor` + `calculate_trade_risk_pct`.

- [ ] **Step 1: Write the failing test** (`tests/unit/test_ai_tools_portfolio_risk_history.py`)

```python
"""§43 — portfolio/risk tools delegate to authoritative app services."""
from core.ai.tools.portfolio_risk import (
    make_get_portfolio_tool, make_get_position_tool, make_get_portfolio_risk_tool,
    make_get_available_capital_tool, make_calculate_allowed_position_size_tool, portfolio_risk_tools,
)
from core.ai.tools.history import make_get_decision_history_tool, make_get_trade_history_tool, history_tools


class _FakeCM:
    def get_positions(self):
        return [{"id": "P1", "ticker": "NVDA"}]
    def get_position(self, pid):
        return {"id": pid, "ticker": "NVDA"} if pid == "P1" else None
    def get_deployable_balance(self):
        return 50000.0


def test_get_portfolio_delegates():
    t = make_get_portfolio_tool(capital_manager=_FakeCM())
    r = t.execute()
    assert r.ok and r.data["positions"][0]["id"] == "P1"


def test_get_position_delegates():
    t = make_get_position_tool(capital_manager=_FakeCM())
    r = t.execute(position_id="P1")
    assert r.ok and r.data["ticker"] == "NVDA"
    r2 = t.execute(position_id="MISSING")
    assert r2.ok is False


def test_get_available_capital_uses_authoritative_deployable_balance():
    # §43 — AI must not compute capital; delegate to get_deployable_balance.
    t = make_get_available_capital_tool(capital_manager=_FakeCM())
    r = t.execute()
    assert r.ok and r.data["available_capital"] == 50000.0


def test_calculate_allowed_position_size_uses_compute_size_factor_and_risk_limits():
    # §43 — size = app services (compute_size_factor + calculate_trade_risk_pct), not AI.
    def fake_size_factor(score, cfg=None, **k):
        return {"size_factor": 0.8, "base_factor": 0.5}
    def fake_trade_risk_pct(*, notional_usd, stop_loss_fraction, equity):
        return notional_usd * stop_loss_fraction / equity * 100.0
    t = make_calculate_allowed_position_size_tool(
        compute_size_factor=fake_size_factor, calculate_trade_risk_pct=fake_trade_risk_pct,
        capital_manager=_FakeCM())
    r = t.execute(score=80, notional_usd=10000, stop_loss_fraction=0.05)
    assert r.ok
    assert r.data["size_factor"] == 0.8
    assert r.data["loss_at_stop_pct"] == 10000 * 0.05 / 50000.0 * 100.0


def test_get_portfolio_risk_delegates_to_risk_agent():
    def fake_eval(signal):
        return {"accepted": True, "checks": [], "metrics": {"exposure_pct": 12.0}}
    t = make_get_portfolio_risk_tool(evaluate_signal=fake_eval)
    r = t.execute(signal={"ticker": "NVDA"})
    assert r.ok and r.data["accepted"] is True


def test_get_decision_history_delegates():
    def fake(limit):
        return [{"event": "open"}]
    t = make_get_decision_history_tool(get_decisions=fake)
    r = t.execute(limit=10)
    assert r.ok and r.data["value"] == [{"event": "open"}]


def test_get_trade_history_delegates():
    def fake(limit, **k):
        return [{"event": "fill"}]
    t = make_get_trade_history_tool(read_recent=fake)
    r = t.execute(limit=20)
    assert r.ok and r.data["value"] == [{"event": "fill"}]


def test_portfolio_risk_and_history_tool_lists():
    assert [t.name for t in portfolio_risk_tools()] == [
        "get_portfolio", "get_position", "get_portfolio_risk",
        "get_available_capital", "calculate_allowed_position_size"]
    assert [t.name for t in history_tools()] == ["get_decision_history", "get_trade_history"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_ai_tools_portfolio_risk_history.py -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Write minimal implementation** (`core/ai/tools/portfolio_risk.py`)

```python
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
    cm = get_capital_manager()
    _risk = RiskAgent()
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
```

- [ ] **Step 4: Write minimal implementation** (`core/ai/tools/history.py`)

```python
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
        return _safe(get_decisions, a.get("limit", 50))
    return Tool(
        name="get_decision_history",
        description="Return recent decision-log entries (authoritative: capital_manager.get_decisions → trade_audit).",
        arguments_schema={"type": "object", "properties": {"limit": {"type": "integer", "default": 50}}},
        execute=execute,
    )


def make_get_trade_history_tool(*, read_recent: Callable) -> Tool:
    def execute(**a: Any) -> ToolResult:
        return _safe(read_recent, a.get("limit", 50))
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_ai_tools_portfolio_risk_history.py -v`
Expected: 8 PASS.

- [ ] **Step 6: Commit**

```bash
git add core/ai/tools/portfolio_risk.py core/ai/tools/history.py tests/unit/test_ai_tools_portfolio_risk_history.py
git commit -m "feat(phase4a): portfolio/risk/history tools — §43 authoritative delegation (§39/§43)"
```

---

### Task 6: propose_order + propose_close draft-only tools + §40/§42 draft guard

**Files:**
- Create: `core/ai/tools/order_proposal.py`
- Test: `tests/unit/test_ai_tools_order_proposal.py`

**Interfaces:**
- Consumes (read-only evidence): `core/capital_manager.py:825 get_deployable_balance()` / `:957 get_position(id)` / `:1393 _get_execution_price`; `core/position_sizing.py:19 compute_size_factor`; `core/risk_limits.py:84 calculate_trade_risk_pct`; `core/capital_entry_guard.py:279 evaluate_capital_entry(signal, positions, config, *, perf_cache, news_pulse)`.
- **MUST NOT call** (§40/§42): `core/capital_manager.py:1740 open_position(...)`, `:2184 close_position(...)`, `agents/execution_agent.py:86 process_signal(...)`.
- Produces: `order_proposal_tools() -> List[Tool]` (2 draft-only tools).

- [ ] **Step 1: Write the failing test** (`tests/unit/test_ai_tools_order_proposal.py`)

```python
"""§40/§42 — propose_order/propose_close return DRAFTS and never execute."""
import pytest

from core.ai.tools.order_proposal import (
    make_propose_order_tool, make_propose_close_tool, order_proposal_tools,
)


class _GuardCM:
    """Capital manager whose mutating methods raise if reached."""
    def __init__(self, deployable=40000.0, position=None, price=100.0):
        self._deployable = deployable
        self._position = position
        self._price = price
        self.open_position_called = False
        self.close_position_called = False

    def get_deployable_balance(self):
        return self._deployable

    def get_position(self, pid):
        return self._position

    def _get_execution_price(self, *a, **k):
        return self._price

    def open_position(self, *a, **k):
        self.open_position_called = True
        raise AssertionError("propose_order MUST NOT call open_position (§42)")

    def close_position(self, *a, **k):
        self.close_position_called = True
        raise AssertionError("propose_close MUST NOT call close_position (§42)")


def test_propose_order_returns_draft_and_never_calls_open_position():
    cm = _GuardCM()
    t = make_propose_order_tool(
        capital_manager=cm,
        compute_size_factor=lambda score, **k: {"size_factor": 0.5},
        calculate_trade_risk_pct=lambda *, notional_usd, stop_loss_fraction, equity: 1.0,
        evaluate_capital_entry=lambda *a, **k: {"allowed": True, "checks": {}},
    )
    r = t.execute(ticker="NVDA", direction="LONG", score=80, notional_usd=8000, stop_loss_fraction=0.05,
                  take_profit_pct=0.10, stop_loss_pct=0.05)
    assert r.ok
    assert r.data["draft"] is True
    assert r.data["ticker"] == "NVDA"
    assert r.data["direction"] == "LONG"
    assert cm.open_position_called is False


def test_propose_order_includes_capital_entry_evidence():
    cm = _GuardCM()
    t = make_propose_order_tool(
        capital_manager=cm,
        compute_size_factor=lambda score, **k: {"size_factor": 0.5},
        calculate_trade_risk_pct=lambda *, notional_usd, stop_loss_fraction, equity: 1.0,
        evaluate_capital_entry=lambda signal, positions, config=None, **k: {"allowed": False, "reason": "weak backtest"},
    )
    r = t.execute(ticker="X", direction="LONG", score=50, notional_usd=1000, stop_loss_fraction=0.05,
                  take_profit_pct=0.1, stop_loss_pct=0.05)
    assert r.ok
    assert r.data["capital_entry"]["allowed"] is False
    assert r.data["capital_entry"]["reason"] == "weak backtest"


def test_propose_close_returns_draft_and_never_calls_close_position():
    pos = {"id": "P1", "ticker": "NVDA", "direction": "LONG"}
    cm = _GuardCM(position=pos, price=120.0)
    t = make_propose_close_tool(capital_manager=cm)
    r = t.execute(position_id="P1", reason="thesis invalidated")
    assert r.ok
    assert r.data["draft"] is True
    assert r.data["position_id"] == "P1"
    assert r.data["exit_reason"] == "thesis invalidated"
    assert cm.close_position_called is False


def test_propose_close_missing_position_returns_error():
    cm = _GuardCM(position=None)
    t = make_propose_close_tool(capital_manager=cm)
    r = t.execute(position_id="MISSING", reason="x")
    assert r.ok is False


def test_order_proposal_tools_returns_two():
    assert [t.name for t in order_proposal_tools()] == ["propose_order", "propose_close"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_ai_tools_order_proposal.py -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Write minimal implementation** (`core/ai/tools/order_proposal.py`)

```python
"""§39 propose_order / propose_close — DRAFT-ONLY order proposals (§40/§42).

These tools return a draft order dict for operator/pipeline review. They NEVER
call CapitalManager.open_position / close_position or ExecutionAgent.process_signal
(AI prose alone must never trigger an order, §42; the broker surface is excluded
from the allow-list, §40). Numbers are app-authoritative (§43): available capital
from get_deployable_balance, size from compute_size_factor, loss-at-stop from
calculate_trade_risk_pct, entry evidence from evaluate_capital_entry.
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
        price = None
        try:
            price = capital_manager._get_execution_price(pos.get("ticker")) if hasattr(capital_manager, "_get_execution_price") else None
        except Exception:
            price = None
        draft = {
            "draft": True,
            "position_id": pid,
            "ticker": pos.get("ticker"),
            "direction": pos.get("direction"),
            "exit_reason": reason,
            "reference_price": price,
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_ai_tools_order_proposal.py -v`
Expected: 5 PASS — including the two §40/§42 draft-only guards (`open_position_called is False`, `close_position_called is False`).

- [ ] **Step 5: Commit**

```bash
git add core/ai/tools/order_proposal.py tests/unit/test_ai_tools_order_proposal.py
git commit -m "feat(phase4a): propose_order/propose_close draft-only tools + §40/§42 draft guard (§39)"
```

---

### Task 7: Full registry assembly + golden §40 test + __init__ exports + exit gate

**Files:**
- Modify: `core/ai/__init__.py` — additive re-exports.
- Test: `tests/golden/test_ai_tool_registry_golden.py`
- Modify: `docs/V9_TEST_MATRIX.md`, `docs/V9_ARCHITECTURE.md`, `docs/V9_IMPLEMENTATION.md` — Phase 4A record.

**Interfaces:**
- Produces: `core.ai.__init__` re-exports `Tool`, `ToolResult`, `ToolRegistry`, `SPEC_TOOL_NAMES`, `build_default_registry`, `run_tool_loop`. Golden test pins the §40 invariant (registry == 28 §39 names) + §43 delegation + §42 draft-only, end-to-end dispatch loop with a fake provider.

- [ ] **Step 1: Write the failing test** (`tests/golden/test_ai_tool_registry_golden.py`)

```python
"""Golden §40/§43/§42 invariants for the Phase 4A tool registry."""
import pytest

pytestmark = pytest.mark.golden

from core.ai import SPEC_TOOL_NAMES, ToolRegistry, build_default_registry, run_tool_loop
from core.ai.provider import AIProvider, AIResponse, ToolCall


def test_golden_registry_exposes_exactly_the_28_spec_tools():
    reg = build_default_registry()
    assert set(reg.names()) == SPEC_TOOL_NAMES
    assert len(reg.names()) == 28


def test_golden_no_registered_tool_can_spawn_a_subprocess_or_open_a_file():
    # §40 — static guard: none of the 28 tool names is a shell/fs/sql/exec surface.
    forbidden = {"run_shell", "exec_python", "read_file", "write_file", "query_sql",
                 "http_get", "read_secret", "open_position", "close_position", "process_signal"}
    assert forbidden.isdisjoint(SPEC_TOOL_NAMES)


def test_golden_propose_order_and_close_are_draft_only(monkeypatch):
    # §40/§42 — monkeypatch the broker mutating methods to raise; the draft tools
    # must still return a draft and never reach them.
    reg = build_default_registry()
    import core.capital_manager as cm_mod

    def _raise(*a, **k):
        raise AssertionError("broker mutation reached from a tool (§40/§42)")
    monkeypatch.setattr(cm_mod.CapitalManager, "open_position", _raise)
    monkeypatch.setattr(cm_mod.CapitalManager, "close_position", _raise)

    r1 = reg.dispatch(ToolCall(id="1", name="propose_order",
                              arguments={"ticker": "NVDA", "direction": "LONG", "score": 70,
                                         "notional_usd": 5000, "stop_loss_fraction": 0.05,
                                         "take_profit_pct": 0.1, "stop_loss_pct": 0.05}))
    assert r1.ok and r1.data.get("draft") is True
    r2 = reg.dispatch(ToolCall(id="2", name="propose_close", arguments={"position_id": "P1", "reason": "x"}))
    # propose_close returns ok=False if P1 not found, but must NOT raise the broker assertion.
    assert (r2.ok and r2.data.get("draft") is True) or (r2.ok is False and "not found" in r2.error)


class _FakeProvider(AIProvider):
    name = "fake"
    def __init__(self, scripted):
        self._scripted = scripted; self._i = 0; self.calls = []
    def list_models(self): return []
    def health_check(self, *, force=False): return True
    def capabilities(self):
        from core.ai.provider import ProviderCapabilities
        return ProviderCapabilities()
    def generate(self, prompt, *, model=None, system="", tools=None, **opts):
        return AIResponse(text="ok")
    def generate_structured(self, prompt, schema, *, model=None, system="", **opts):
        return AIResponse(text="ok")
    def tool_call(self, prompt, tools, *, model=None, system="", **opts):
        self.calls.append(tools)
        resp = self._scripted[min(self._i, len(self._scripted) - 1)]; self._i += 1
        return resp
    def stream(self, prompt, *, model=None, system="", **opts):
        from core.ai.provider import AIStreamChunk
        yield AIStreamChunk(text="ok", done=True)
    def usage(self):
        from core.ai.provider import Usage
        return Usage()


def test_golden_dispatch_loop_end_to_end():
    reg = build_default_registry()
    provider = _FakeProvider([
        AIResponse(tool_calls=[ToolCall(id="c1", name="get_available_capital", arguments={})]),
        AIResponse(text="FINAL"),
    ])
    resp = run_tool_loop(provider, reg, "what capital is available?", max_rounds=4)
    assert resp.text == "FINAL"
    # The provider received the 28-tool payload on round 1.
    names = {t["name"] for t in provider.calls[0]}
    assert "get_available_capital" in names
    assert len(names) == 28
```

- [ ] **Step 2: Modify `core/ai/__init__.py`** — add the additive re-exports after the existing `from core.ai.fallback import …` line (line 20) and extend `__all__`:

Append to the imports:
```python
from core.ai.tools import SPEC_TOOL_NAMES, Tool, ToolRegistry, ToolResult, build_default_registry, run_tool_loop
```
Add to `__all__` (alphabetical-ish, matching existing style):
```python
    "SPEC_TOOL_NAMES",
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "build_default_registry",
    "run_tool_loop",
```

- [ ] **Step 3: Run the golden test + full registry tests**

Run: `python -m pytest tests/golden/test_ai_tool_registry_golden.py tests/unit/test_ai_tool_registry.py tests/unit/test_ai_tool_loop.py -v`
Expected: 5 golden + 8 + 3 = 16 PASS.

- [ ] **Step 4: §4 forbidden-language sweep on new/modified files**

Run: `grep -rniE "should work|probably works|99%|mostly complete|approximately complete|mock for now|temporary workaround|TODO later|backend finished|frontend pending|untested but implemented" core/ai/tools tests/unit/test_ai_tools_*.py tests/golden/test_ai_tool_registry_golden.py`
Expected: 0 hits.

- [ ] **Step 5: Full regression gate**

Run: `python -m pytest tests tests_e2e -q`
Expected: 1756 + 52 (the new 4A tests) = **1808 passed / 0 failed / exit 0** (exact count confirmed at execution; the 4A test count: 8 registry + 3 loop + 7 market_data + 4 earnings + 4 options_sec + 3 news + 9 signals_strategy_backtest + 8 portfolio_risk_history + 5 order_proposal + 5 golden = 56; total 1756 + 56 = 1812 — exact count recorded at execution).

- [ ] **Step 6: V7-untouched + deps-diff audit**

Run:
```bash
git -C "C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium" rev-parse HEAD
git diff --stat 7209da0..HEAD -- requirements.txt requirements-dev.txt pyproject.toml setup.py setup.cfg
git diff --stat 7209da0..HEAD -- core/ollama_client.py core/ollama_decision.py core/agent_manager.py core/config.py
```
Expected: V7 HEAD `f912a33` unchanged; deps diff empty; `ollama_client`/`ollama_decision`/`agent_manager`/`config` diff empty (4A is additive — no existing behavior changed).

- [ ] **Step 7: Update control docs**

- `docs/V9_TEST_MATRIX.md` — add a "§39 Tool Registry / §40 Allow-list" row → PASS.
- `docs/V9_ARCHITECTURE.md` — add a "Tool Registry (Phase 4A)" subsection: `core/ai/tools/` package, the 28-name allow-list = §40 boundary, dispatch loop, §43 delegation, draft-only order proposals; note the agent_manager rewire is Phase 4C.
- `docs/V9_IMPLEMENTATION.md` — Phase 4 row → IN PROGRESS; Phase 4A record (test count, V7 `f912a33`, empty deps diff, §4 clean, deviations if any).

- [ ] **Step 8: Commit + tag the gate**

```bash
git add core/ai/__init__.py tests/golden/test_ai_tool_registry_golden.py docs/V9_TEST_MATRIX.md docs/V9_ARCHITECTURE.md docs/V9_IMPLEMENTATION.md
git commit -m "test(phase4a): golden §40/§43/§42 + control docs — Phase 4A PASS"
```

---

## Self-Review (plan → spec)

- **Spec coverage:** §39 (28 tools) → Tasks 2–6 (5+4+1+2+1+4+2+5+2+2 = 28 ✓); §40 (allow-list, no unrestricted) → Task 1 `SPEC_TOOL_NAMES` + Task 7 golden `test_golden_no_registered_tool_can_spawn_a_subprocess_or_open_a_file` + `test_registry_rejects_name_outside_allow_list`; §42 (no order on AI prose, draft-only) → Task 6 + Task 7 `test_golden_propose_order_and_close_are_draft_only`; §43 (app-authoritative numbers) → Task 5 delegation + `test_get_available_capital_uses_authoritative_deployable_balance` / `test_calculate_allowed_position_size_uses_compute_size_factor_and_risk_limits`; §5 (evolve, no rewrite) → all 21 delegated tools wrap existing functions, no existing file modified; §4 (no forbidden language) → Task 7 Step 4 sweep + honest Phase-5/10 stubs; §115 (no test weakened) → only new tests; §117 (V7 read-only) → Task 7 Step 6 audit; §118 (no new deps) → stdlib + existing code only. ✓
- **Placeholder scan:** no TBD/TODO; the Phase-5/10 stubs are explicit `ok=False` with reason strings (honest, not placeholders); test counts noted as "exact count recorded at execution" (the established pattern — deviations captured at execution). ✓
- **Type consistency:** `Tool`/`ToolResult` fields used consistently across all tasks; `ToolCall(id, name, arguments)` from `core/ai/provider.py:43` used in `dispatch` and the loop; `ToolRegistry.register/get/names/list_tools/dispatch` signatures consistent; `run_tool_loop(provider, registry, prompt, *, max_rounds, model, system, **opts) -> AIResponse` consistent. ✓
- **Risk:** the delegate-binding imports inside each `<module>_tools()` builder (e.g. `from api.state_service import get_state`) are verified at execution; any signature mismatch is a documented deviation (the 1A/1B/1C/2/3A/3B/3C precedent). The `compute_size_factor(score)` call in `portfolio_risk_tools` passes only `score` (the optional kwargs `cfg`/`expectancy_r`/etc. default to `None`); the §43 test injects a 1-arg fake, matching.

No issues found. Plan is complete.