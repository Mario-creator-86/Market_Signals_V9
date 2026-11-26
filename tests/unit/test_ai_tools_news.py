"""News + news-sentiment tools (read-only wrappers)."""
from core.ai.tools.news import make_get_news_tool, make_get_news_sentiment_tool, news_tools


def test_get_news_delegates():
    def fake(ticker, max_items=20, **_):
        return {"ticker": ticker, "items": [{"title": "x"}]}
    t = make_get_news_tool(get_ticker_news=fake)
    r = t.execute(ticker="NVDA", max_items=5)
    assert r.ok and r.data["trust"] == "UNTRUSTED_EXTERNAL_DATA"
    assert r.data["payload"]["ticker"] == "NVDA"


def test_get_news_sentiment_delegates():
    def fake(ticker=None, **_):
        return {"sentiment": "bullish", "score": 0.7}
    t = make_get_news_sentiment_tool(get_sentiment_summary=fake)
    r = t.execute(ticker="AAPL")
    assert r.ok and r.data["payload"]["sentiment"] == "bullish"


def test_news_tools_returns_two():
    assert [t.name for t in news_tools()] == ["get_news", "get_news_sentiment"]
