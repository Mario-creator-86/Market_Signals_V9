"""§39 news + news-sentiment tools (read-only wrappers)."""
from __future__ import annotations

from typing import Any, Callable, List

from core.ai.tools.tool import Tool, ToolResult
from core.external_text import quarantine_external_payload


def _safe_external(delegate: Callable, *args, **kwargs) -> ToolResult:
    try:
        data = delegate(*args, **kwargs)
    except Exception as exc:
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    if not isinstance(data, dict):
        data = {"value": data}
    return ToolResult(ok=True, data=quarantine_external_payload(data))


def make_get_news_tool(*, get_ticker_news: Callable) -> Tool:
    def execute(**a: Any) -> ToolResult:
        return _safe_external(get_ticker_news, a.get("ticker"), a.get("max_items", 20))
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
        return _safe_external(get_sentiment_summary, a.get("ticker"))
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
