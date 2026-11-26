import threading
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agents.news_tab_agent import (
    NewsArticle,
    NewsImpact,
    NewsTabAgent,
    _is_finnhub_news_placeholder_url,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "url",
    [
        "https://finnhub.io/api/news?id=abc123",
        "https://www.finnhub.io/api/news/?id=abc123",
        "HTTP://FINNHUB.IO/API/NEWS?id=abc123",
    ],
)
def test_finnhub_news_api_wrapper_is_not_an_article_body(url):
    assert _is_finnhub_news_placeholder_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://finnhub.io/docs/api/company-news",
        "https://example.com/api/news?id=abc123",
        "https://marketbeat.com/stocks/NASDAQ/AAPL/news/",
        "",
    ],
)
def test_article_urls_are_not_misclassified_as_finnhub_placeholders(url):
    assert _is_finnhub_news_placeholder_url(url) is False


def _article(article_id: str, url: str) -> NewsArticle:
    return NewsArticle(
        id=article_id,
        title=f"Headline {article_id}",
        url=url,
        source="Provider",
        published_at=datetime.now(timezone.utc),
        impact=NewsImpact.HIGH,
        impact_score=80,
    )


def test_auto_summary_candidates_exclude_finnhub_api_wrappers():
    agent = object.__new__(NewsTabAgent)
    agent._cache_lock = threading.RLock()
    agent.auto_summarize_top_n = 10
    wrapper = _article("wrapper", "https://finnhub.io/api/news?id=abc123")
    article = _article("article", "https://www.marketbeat.com/stocks/NASDAQ/AAPL/news/")
    agent._iter_all_articles = lambda: iter([wrapper, article])

    candidates = agent._select_auto_summary_candidates()

    assert [candidate.id for candidate in candidates] == ["article"]


def test_micro_summary_worker_never_fetches_finnhub_api_wrapper():
    agent = object.__new__(NewsTabAgent)
    agent._micro_summary_semaphore = threading.Semaphore(1)
    agent._micro_summary_lock = threading.RLock()
    agent._micro_summary_inflight = {"wrapper"}

    with patch("core.content_fetcher.fetch_article_content") as fetch:
        agent._generate_micro_summary_worker(
            "wrapper",
            "https://finnhub.io/api/news?id=abc123",
            "Provider headline",
            "Finnhub",
            "AAPL",
        )

    fetch.assert_not_called()
    assert "wrapper" not in agent._micro_summary_inflight


def test_micro_summary_worker_negative_caches_disallowed_domain():
    agent = object.__new__(NewsTabAgent)
    agent.config = {}
    agent._micro_summary_semaphore = threading.Semaphore(1)
    agent._micro_summary_lock = threading.RLock()
    agent._micro_summary_inflight = {"blocked"}
    agent._normalize_micro_summary = NewsTabAgent._normalize_micro_summary.__get__(agent)
    agent._record_micro_summary_evidence = lambda *_args: None

    with (
        patch("core.analysis_store.analysis_store.get_article_analysis", return_value=None),
        patch("core.analysis_store.analysis_store.save_article_analysis") as save,
        patch("core.ollama_client.ollama.check_availability", return_value=True),
        patch("core.ollama_client.ollama.get_best_model", return_value="test-model"),
        patch("core.ai.role_resolution.resolve_role_model", return_value="test-model"),
        patch(
            "core.content_fetcher.fetch_article_content",
            side_effect=ValueError("Blocked URL domain not in allowlist: example.invalid"),
        ),
    ):
        agent._generate_micro_summary_worker(
            "blocked",
            "https://example.invalid/story",
            "Blocked headline",
            "Provider",
            "AAPL",
        )

    save.assert_called_once()
    assert save.call_args.kwargs["analysis_type"] == "micro"
    assert save.call_args.args[1]["tldr"] == "unknown"
    assert "blocked" not in agent._micro_summary_inflight
