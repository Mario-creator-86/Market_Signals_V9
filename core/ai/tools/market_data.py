"""§39 market-data tools — thin read-only wrappers over existing services.

Each factory takes an injectable delegate (default: the real service function)
so unit tests inject fakes (no live market calls). The delegate signatures are
verified at the import sites against the existing functions named below; any
mismatch is recorded as a documented deviation at execution time.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

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
        ticker = arguments.get("ticker")
        try:
            df = fetch_history(
                ticker,
                period=arguments.get("period", "1mo"),
                interval=arguments.get("interval", "1d"),
            )
        except Exception as exc:
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        # Normalize: DataFrame -> records (avoid feeding a giant repr into the
        # agent loop); dict -> passthrough; None -> empty bars.
        import pandas as pd
        if df is None:
            return ToolResult(ok=True, data={"ticker": ticker, "bars": []})
        if isinstance(df, pd.DataFrame):
            return ToolResult(ok=True, data={"ticker": ticker, "bars": df.to_dict("records")})
        if isinstance(df, dict):
            return ToolResult(ok=True, data=df)
        return ToolResult(ok=True, data={"value": df})
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
    # Delegate contract: build_context(ticker, bars=None) -> dict. The real
    # adapter (market_data_tools) maps this onto the existing
    # core.strategy_v2.context_builder.build_context(symbol, asset_class,
    # watch_row, ohlc_df=...) — see the documented deviation in market_data_tools.
    def execute(**arguments: Any) -> ToolResult:
        return _safe(build_context, arguments.get("ticker"), bars=arguments.get("bars"))
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


def make_get_market_regime_tool(*, detect: Callable, market_engine: Optional[Any] = None) -> Tool:
    def execute(**arguments: Any) -> ToolResult:
        # Additive (Phase 6): when a universe of per-ticker regimes is supplied,
        # delegate to the market-wide breadth engine. The per-ticker single-context
        # path stays the default (back-compat). The market engine is lazy-imported so
        # the tool stays importable in test contexts that inject fakes.
        universe = arguments.get("universe_regimes")
        if universe is not None:
            engine = market_engine
            if engine is None:
                from core.signal_engine import MarketRegimeEngine
                engine = MarketRegimeEngine()
            return _safe(engine.detect_market, universe)
        return _safe(detect, arguments.get("context") or {})
    return Tool(
        name="get_market_regime",
        description="Detect the market regime (trend/range/high-vol/event/session-breakout) for a context; "
                    "or a market-wide breadth regime when universe_regimes (a list of per-ticker regime dicts) is supplied (Phase 6).",
        arguments_schema={
            "type": "object",
            "properties": {
                "context": {"type": "object"},
                "universe_regimes": {"type": "array", "items": {"type": "object"}},
            },
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
    from core.strategy_v2.context_builder import build_context, infer_asset_class
    from core.strategy_v2.regime_engine import RegimeEngine
    from core.sectors import get_tickers_by_sector

    _regime = RegimeEngine()

    # DOCUMENTED DEVIATION: build_context's real signature is
    # build_context(symbol, asset_class, watch_row, ohlc_df=None, ...) — not
    # (ticker, bars). This adapter maps the tool's (ticker, bars) contract onto
    # the real function: bars (list of OHLCV dicts) -> DataFrame -> ohlc_df.
    def _build_features(ticker, bars=None, **_):
        import pandas as pd
        ohlc_df = None
        if bars:
            try:
                ohlc_df = pd.DataFrame(bars)
            except Exception:
                ohlc_df = None
        return build_context(ticker, infer_asset_class(ticker), {}, ohlc_df=ohlc_df)

    return [
        make_get_market_snapshot_tool(get_state=get_state),
        make_get_ohlcv_tool(fetch_history=YahooClient().fetch_history),
        make_get_technical_features_tool(build_context=_build_features),
        make_get_market_regime_tool(detect=_regime.detect),
        make_get_sector_regime_tool(detect=_regime.detect, sector_tickers=get_tickers_by_sector),
    ]