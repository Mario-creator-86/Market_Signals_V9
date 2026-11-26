import json
from datetime import datetime, timezone

from core.data_model.entities import NewsArticle
from core.data_model.pit import UNAVAILABLE, as_of

UTC = timezone.utc


def _observed(article_id, ticker, observed_at, published_at, title="t", url="u", source="s"):
    return json.dumps({
        "event": "article_observed",
        "observed_at": observed_at,
        "article_id": article_id,
        "ticker": ticker,
        "title": title,
        "url": url,
        "source": source,
        "published_at": published_at,
    })


def test_read_articles_as_of_filters_by_observed_at(tmp_path, monkeypatch):
    ledger_file = tmp_path / "news_evidence_ledger.jsonl"
    with open(ledger_file, "w") as f:
        f.write(_observed("a1", "AAPL", "2026-01-01T12:00:00+00:00", "2026-01-01T11:00:00+00:00") + "\n")
        f.write(_observed("a2", "MSFT", "2026-03-01T12:00:00+00:00", "2026-03-01T11:00:00+00:00") + "\n")
    monkeypatch.setattr("core.news_evidence_ledger.NEWS_EVIDENCE_LEDGER_PATH", ledger_file)
    from core.news_evidence_ledger import read_articles_as_of
    # T before a2 was observed -> only a1 is available at T (§32 guard).
    articles = read_articles_as_of(datetime(2026, 2, 1, tzinfo=UTC))
    assert len(articles) == 1
    assert isinstance(articles[0], NewsArticle)
    assert articles[0].id == "a1"
    assert articles[0].ticker == "AAPL"
    assert articles[0].pit.available_from == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    # as_of over the returned list returns UNAVAILABLE before any available_from
    assert as_of(articles, datetime(2020, 1, 1, tzinfo=UTC)) is UNAVAILABLE

    # Reading with a T that covers both -> as_of resolves newest-available-at-T.
    both = read_articles_as_of(datetime(2026, 6, 1, tzinfo=UTC))
    assert len(both) == 2
    assert as_of(both, datetime(2026, 2, 1, tzinfo=UTC)).id == "a1"   # a2 not yet available
    assert as_of(both, datetime(2026, 6, 1, tzinfo=UTC)).id == "a2"   # newest available


def test_read_articles_as_of_skips_non_observed_events(tmp_path, monkeypatch):
    ledger_file = tmp_path / "news_evidence_ledger.jsonl"
    with open(ledger_file, "w") as f:
        f.write(json.dumps({
            "event": "ai_micro_summary_available", "analyzed_at": "2026-01-01T13:00:00+00:00",
            "article_id": "x", "ticker": "AAPL", "title": "t", "url": "u",
            "source": "s", "published_at": "2026-01-01T11:00:00+00:00",
        }) + "\n")
        f.write(_observed("a1", "AAPL", "2026-01-01T12:00:00+00:00", "2026-01-01T11:00:00+00:00") + "\n")
    monkeypatch.setattr("core.news_evidence_ledger.NEWS_EVIDENCE_LEDGER_PATH", ledger_file)
    from core.news_evidence_ledger import read_articles_as_of
    articles = read_articles_as_of(datetime(2026, 6, 1, tzinfo=UTC))
    assert len(articles) == 1
    assert articles[0].id == "a1"


def test_read_articles_as_of_empty_when_file_empty(tmp_path, monkeypatch):
    ledger_file = tmp_path / "news_evidence_ledger.jsonl"
    ledger_file.write_text("")
    monkeypatch.setattr("core.news_evidence_ledger.NEWS_EVIDENCE_LEDGER_PATH", ledger_file)
    from core.news_evidence_ledger import read_articles_as_of
    assert read_articles_as_of(datetime(2026, 6, 1, tzinfo=UTC)) == []


def test_read_articles_as_of_missing_file_returns_empty(monkeypatch):
    monkeypatch.setattr("core.news_evidence_ledger.NEWS_EVIDENCE_LEDGER_PATH",
                        "/nonexistent/dir/ledger.jsonl")
    from core.news_evidence_ledger import read_articles_as_of
    assert read_articles_as_of(datetime(2026, 6, 1, tzinfo=UTC)) == []


def test_read_articles_as_of_skips_malformed_lines(tmp_path, monkeypatch):
    ledger_file = tmp_path / "news_evidence_ledger.jsonl"
    with open(ledger_file, "w") as f:
        f.write("not json\n")
        f.write(_observed("a1", "AAPL", "2026-01-01T12:00:00+00:00", "2026-01-01T11:00:00+00:00") + "\n")
    monkeypatch.setattr("core.news_evidence_ledger.NEWS_EVIDENCE_LEDGER_PATH", ledger_file)
    from core.news_evidence_ledger import read_articles_as_of
    articles = read_articles_as_of(datetime(2026, 6, 1, tzinfo=UTC))
    assert len(articles) == 1
    assert articles[0].id == "a1"