import json

from core.news_evidence_ledger import NewsEvidenceLedger


def _article():
    return {
        "id": "article-1",
        "ticker": "BTC-USD",
        "title": "BTC market update",
        "url": "https://example.test/btc",
        "source": "Example",
        "published_at": "2026-07-15T10:00:00+00:00",
    }


def test_ledger_records_observation_and_ai_availability_with_separate_timestamps(tmp_path):
    path = tmp_path / "news_evidence.jsonl"
    ledger = NewsEvidenceLedger(path)

    assert ledger.record_observed(_article(), observed_at="2026-07-15T10:01:00+00:00")
    assert ledger.record_ai_micro_summary(
        _article(),
        {"overall_sentiment": "bullish", "confidence": 82, "time_horizon": "intraday", "actionability": 70, "tldr": "Momentum improved"},
        analyzed_at="2026-07-15T10:02:00+00:00",
    )

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in rows] == ["article_observed", "ai_micro_summary_available"]
    assert rows[0]["observed_at"] == "2026-07-15T10:01:00+00:00"
    assert rows[1]["analyzed_at"] == "2026-07-15T10:02:00+00:00"
    assert rows[1]["analysis"]["overall_sentiment"] == "bullish"


def test_ledger_rejects_source_records_missing_required_provenance(tmp_path):
    ledger = NewsEvidenceLedger(tmp_path / "news_evidence.jsonl")
    incomplete = _article()
    incomplete["url"] = ""

    assert ledger.record_observed(incomplete) is False
    assert not ledger.path.exists()
