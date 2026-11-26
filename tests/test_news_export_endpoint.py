from datetime import datetime, timedelta, timezone
from unittest.mock import patch


def test_news_export_uses_flat_articles_and_filters(client):
    now = datetime.now(timezone.utc)
    payload = [
        {
            "id": "a1",
            "title": "Apple earnings beat expectations",
            "url": "https://example.com/a1",
            "source": "Reuters",
            "published_at": (now - timedelta(hours=2)).isoformat(),
            "ticker": "AAPL",
            "impact": "high",
            "action_score": 82,
        },
        {
            "id": "m1",
            "title": "Fed keeps rates unchanged",
            "url": "https://example.com/m1",
            "source": "Bloomberg",
            "published_at": (now - timedelta(hours=1)).isoformat(),
            "ticker": None,
            "impact": "critical",
            "action_score": 90,
        },
        {
            "id": "old1",
            "title": "Old Apple article",
            "url": "https://example.com/old1",
            "source": "CNBC",
            "published_at": (now - timedelta(days=20)).isoformat(),
            "ticker": "AAPL",
            "impact": "high",
            "action_score": 55,
        },
    ]

    class _FakeAgent:
        def get_all_articles_flat(self):
            return payload

    with patch("agents.news_tab_agent.get_news_tab_agent", return_value=_FakeAgent()):
        res = client.get("/api/news-tab/export?days=7&tickers=AAPL&impact=high")

    assert res.status_code == 200
    text = res.text
    assert "Apple earnings beat expectations" in text
    assert "[AAPL]" in text
    assert "Fed keeps rates unchanged" not in text
    assert "Old Apple article" not in text


def test_news_export_preserves_multi_ticker_assignments(client):
    now = datetime.now(timezone.utc)
    payload = [
        {
            "id": "shared-1",
            "title": "AAPL and MSFT rally after guidance",
            "url": "https://example.com/shared",
            "source": "Reuters",
            "published_at": (now - timedelta(hours=1)).isoformat(),
            "ticker": "AAPL",
            "impact": "high",
            "action_score": 70,
        },
        {
            "id": "shared-1",
            "title": "AAPL and MSFT rally after guidance",
            "url": "https://example.com/shared",
            "source": "Reuters",
            "published_at": (now - timedelta(hours=1)).isoformat(),
            "ticker": "MSFT",
            "impact": "high",
            "action_score": 70,
        },
    ]

    class _FakeAgent:
        def get_all_articles_flat(self):
            return payload

    with patch("agents.news_tab_agent.get_news_tab_agent", return_value=_FakeAgent()):
        res_aapl = client.get("/api/news-tab/export?days=7&tickers=AAPL")
        res_msft = client.get("/api/news-tab/export?days=7&tickers=MSFT")

    assert res_aapl.status_code == 200
    assert res_msft.status_code == 200
    assert "[AAPL]" in res_aapl.text
    assert "[MSFT]" not in res_aapl.text
    assert "[MSFT]" in res_msft.text
