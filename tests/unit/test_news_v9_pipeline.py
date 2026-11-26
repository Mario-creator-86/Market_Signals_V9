from datetime import datetime, timezone

from core.news_v9.finbert import FinancialSentimentResult
from core.news_v9.pipeline import NewsPipeline, canonicalize_url


class _Sentiment:
    def analyze(self, text):
        return FinancialSentimentResult(
            label="positive", score=0.88, confidence=0.88, source="finbert",
            model="ProsusAI/finbert", revision="immutable",
        )


def _pipeline():
    return NewsPipeline(
        sentiment=_Sentiment(),
        entity_aliases={"AAPL": ["Apple Inc", "Apple"]},
        catalyst_patterns={"earnings": [r"\bearnings\b", r"\bguidance\b"]},
    )


def test_url_normalization_removes_tracking_but_keeps_evidence_url():
    url = canonicalize_url("HTTPS://Example.com/a?utm_source=x&id=7#fragment")
    assert url == "https://example.com/a?id=7"
    assert canonicalize_url("file:///etc/passwd") == ""


def test_pipeline_normalizes_matches_entity_classifies_and_records_pit():
    observed = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    result = _pipeline().run([{
        "title": "Apple raises earnings guidance",
        "summary": "Demand remains strong.",
        "url": "https://wire.example/story?utm_source=feed",
        "source": "Wire A",
        "published_at": "2026-08-13T11:00:00Z",
    }], observed_at=observed)
    article = result.articles[0]
    assert article.ticker == "AAPL"
    assert article.related_tickers == ("AAPL",)
    assert article.category == "earnings"
    assert article.sentiment == "bullish"
    assert article.sentiment_quality["source"] == "finbert"
    assert article.pit.available_from == observed


def test_pipeline_rejects_incomplete_articles_without_fabricating_fields():
    result = _pipeline().run([
        {"title": "No source or URL", "published_at": "2026-08-13T11:00:00Z"},
        {"title": "Good", "source": "Wire", "url": "javascript:bad", "published_at": "2026-08-13T11:00:00Z"},
    ])
    assert result.articles == ()
    assert result.rejected_count == 2


def test_syndicated_copies_are_marked_and_not_counted_as_independent_primaries():
    rows = [
        {"id": "1", "title": "Apple raises earnings guidance after strong demand", "url": "https://a.example/1", "source": "Wire A", "published_at": "2026-08-13T11:00:00Z"},
        {"id": "2", "title": "Strong demand as Apple raises earnings guidance", "url": "https://b.example/2", "source": "Wire B", "published_at": "2026-08-13T11:01:00Z"},
    ]
    result = _pipeline().run(rows)
    assert result.primary_count == 1
    assert result.duplicate_count == 1
    assert sum(not article.is_duplicate for article in result.articles) == 1
    primary = next(article for article in result.articles if not article.is_duplicate)
    assert primary.also_reported_by == ("Wire B",)
