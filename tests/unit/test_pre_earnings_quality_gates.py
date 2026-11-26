from datetime import datetime, timedelta, timezone

import pytest

from agents import pre_earnings_agent as pe
from agents.pre_earnings_agent import PreEarningsAgent, detect_already_reported_symbols


def _agent() -> PreEarningsAgent:
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._get_config = lambda: dict(PreEarningsAgent.DEFAULT_CONFIG)
    return agent


@pytest.mark.unit
def test_conflicting_provider_dates_are_explicitly_non_actionable():
    agent = _agent()
    now = datetime.now(timezone.utc)
    rows = [
        {
            "symbol": "TEST",
            "date": pe._iso(now + timedelta(days=2)),
            "session": "AMC",
            "source": "finnhub",
            "confirmed": True,
        },
        {
            "symbol": "TEST",
            "date": pe._iso(now + timedelta(days=4)),
            "session": "AMC",
            "source": "finnhub",
            "confirmed": True,
        },
    ]

    index = agent._build_earnings_index(rows)
    assert {row["event_status"] for row in index["TEST"]} == {"DATE_CONFLICT"}
    item = {
        "symbol": "TEST",
        "earnings_time_utc": rows[0]["date"],
        "session": "AMC",
        "event_status": "DATE_CONFLICT",
        "event_date_confidence": "medium",
        "event_session_confidence": "provider",
        "flags": {"calendar_date_conflict": True},
        "badges": ["DATE_CONFLICT"],
    }
    assert "calendar_date_conflict" in agent._event_gate_blockers(item, now=now)


@pytest.mark.unit
def test_authoritative_event_supersedes_weaker_conflicting_date():
    agent = _agent()
    now = datetime.now(timezone.utc)
    official_date = pe._iso(now + timedelta(days=3))
    rows = [
        {
            "symbol": "TEST",
            "date": official_date,
            "session": "BMO",
            "source": "official_ir",
            "source_url": "https://ir.example.test/results",
            "confirmed": True,
        },
        {
            "symbol": "TEST",
            "date": pe._iso(now + timedelta(days=1)),
            "session": "AMC",
            "source": "calendar_cache_unknown",
        },
    ]

    index = agent._build_earnings_index(rows)
    primary = agent._pick_primary_event(index["TEST"])
    assert primary["date"] == official_date
    assert primary["event_status"] == "UPCOMING"
    assert any(row["event_status"] == "SUPERSEDED" for row in index["TEST"])


@pytest.mark.unit
def test_one_decisive_post_results_headline_expires_event():
    now = datetime.now(timezone.utc)
    news = {
        "TEST": [
            {
                "ticker": "TEST",
                "title": "TEST Reports Second Quarter 2026 Financial Results",
                "published_at": pe._iso(now - timedelta(hours=2)),
                "_pe_relevance": 1.0,
            }
        ]
    }
    earnings = {
        "TEST": [
            {
                "symbol": "TEST",
                "date": pe._iso(now + timedelta(days=2)),
            }
        ]
    }

    flagged = detect_already_reported_symbols(news, earnings, now=now)
    assert flagged["TEST"]["reason"] == "ALREADY_REPORTED"
    assert flagged["TEST"]["decisive_hits"] == 1


@pytest.mark.unit
def test_ai_neutral_or_ai_ineligible_never_enters_top_predictions():
    agent = _agent()
    now = datetime.now(timezone.utc)

    def candidate(symbol: str, verdict: str, qualifies: bool = True) -> dict:
        return {
            "symbol": symbol,
            "earnings_time_utc": pe._iso(now + timedelta(days=2)),
            "session": "AMC",
            "event_status": "UPCOMING",
            "event_source": "finnhub",
            "event_verified_at": pe._iso(now),
            "event_date_confidence": "medium",
            "event_session_confidence": "provider",
            "gap_risk": 75,
            "gap_risk_numeric": 75,
            "direction_bias_score": 55,
            "direction_label": "LONG",
            "coverage_score": 80,
            "confidence_score": 75,
            "opportunity_score": 70,
            "news_digest": {
                "article_count": 5,
                "unique_sources": 3,
                "weighted_articles": 4.0,
                "fresh_articles": 4,
                "newest_utc": pe._iso(now - timedelta(hours=1)),
                "fingerprint": symbol,
            },
            "ai_decision": {
                "used": True,
                "verdict": verdict,
                "qualifies_pre_earnings": qualifies,
                "confidence": 0.8,
                "generated_at": pe._iso(now),
                "news_fingerprint": symbol,
                "news_articles_read": 5,
                "news_sources_read": 3,
                "rationale": "Current upcoming-event evidence.",
            },
            "flags": {},
            "badges": [],
        }

    good = candidate("GOOD", "LONG")
    neutral = candidate("FLAT", "NEUTRAL")
    rejected = candidate("DONE", "LONG", qualifies=False)
    top = agent._build_top_predictions(
        [good, neutral, rejected],
        dict(PreEarningsAgent.DEFAULT_CONFIG),
        now=now,
    )

    assert [row["symbol"] for row in top] == ["GOOD"]
    assert "ai_no_directional_edge" in neutral["recommendation_blockers"]
    assert "ai_rejected_event_eligibility" in rejected["recommendation_blockers"]


@pytest.mark.unit
def test_historical_post_earnings_wording_does_not_override_explicit_eligibility():
    agent = _agent()
    item = {
        "structured_direction_bias_score": 68,
        "direction_bias_score": 68,
        "direction_label": "LONG",
        "ai_decision": {
            "used": True,
            "verdict": "LONG",
            "direction_score": 72,
            "confidence": 0.8,
            "qualifies_pre_earnings": True,
            "rationale": "Historical post-earnings reactions were positive into prior reports.",
        },
        "badges": [],
    }

    agent._synchronize_direction_decision(item, dict(PreEarningsAgent.DEFAULT_CONFIG))

    assert item["direction_label"] == "LONG"
    assert item["direction_bias_score"] > 0
    assert "AI_INELIGIBLE" not in item["badges"]


@pytest.mark.unit
def test_verified_ranking_fails_closed_when_event_provenance_is_missing():
    agent = _agent()
    now = datetime.now(timezone.utc)
    strict_blockers = agent._event_gate_blockers(
        {
            "symbol": "MISS",
            "earnings_time_utc": pe._iso(now + timedelta(days=2)),
            "session": "AMC",
            "event_status": "UPCOMING",
        },
        now=now,
        require_verified_provenance=True,
    )
    details = agent._prediction_score_details(
        {
            "symbol": "MISS",
            "earnings_time_utc": pe._iso(now + timedelta(days=2)),
            "session": "AMC",
            "event_status": "UPCOMING",
            "direction_label": "LONG",
            "direction_bias_score": 60,
            "coverage_score": 80,
            "confidence_score": 80,
            "gap_risk": 75,
            "news_digest": {"article_count": 5, "unique_sources": 3},
            "ai_decision": {
                "used": True,
                "verdict": "LONG",
                "qualifies_pre_earnings": True,
                "confidence": 0.8,
            },
        },
        dict(PreEarningsAgent.DEFAULT_CONFIG),
        now=now,
    )

    assert details["eligible"] is False
    assert "event_source_required" in strict_blockers
    assert "event_source_url_required" in strict_blockers
    assert "event_verification_timestamp_required" in strict_blockers
    assert "event_source_required" in details["blockers"]
    assert "event_source_url_required" in details["blockers"]
    assert "event_verification_timestamp_required" in details["blockers"]
    assert "verified_event_date_confidence_required" in details["blockers"]
    assert "verified_event_session_confidence_required" in details["blockers"]


@pytest.mark.unit
def test_publisher_diversity_uses_real_domains_not_yahoo_ingestion_label():
    now = datetime.now(timezone.utc)
    rows = [
        {
            "title": "TEST earnings preview from source one",
            "url": "https://www.reuters.com/markets/test-preview",
            "source": "yahoo_rss",
            "published_at": pe._iso(now - timedelta(hours=1)),
        },
        {
            "title": "TEST guidance preview from source two",
            "url": "https://www.investopedia.com/test-preview",
            "source": "yahoo_rss",
            "published_at": pe._iso(now - timedelta(hours=2)),
        },
    ]
    result = PreEarningsAgent._prepare_news_evidence(rows, [], now=now)
    assert result["unique_sources"] == 2
    assert {row["_pe_publisher"] for row in result["rows"]} == {
        "reuters.com",
        "investopedia.com",
    }


@pytest.mark.unit
def test_yfinance_date_uses_session_only_after_same_day_calendar_cross_check():
    now = datetime.now(timezone.utc)
    day = pe._iso(now + timedelta(days=2))
    yahoo = [{
        "symbol": "TEST",
        "date": day,
        "session": "UNKNOWN",
        "source": "yfinance",
        "confirmed": True,
    }]
    calendar = [{
        "symbol": "TEST",
        "date": day,
        "when": "amc",
        "source": "finnhub",
    }]

    checked = PreEarningsAgent._cross_check_yfinance_sessions(yahoo, calendar)

    assert checked[0]["session"] == "AMC"
    assert checked[0]["session_confidence"] == "cross_provider"
    assert checked[0]["source"] == "yfinance+calendar"
    canonical = PreEarningsAgent._canonicalize_symbol_events("TEST", checked, now=now)
    assert canonical[0]["event_status"] == "UPCOMING"
    assert canonical[0]["date_confidence"] == "high"


@pytest.mark.unit
def test_ambiguous_dao_crypto_news_is_not_mapped_to_youdao(monkeypatch):
    agent = _agent()
    monkeypatch.setattr(pe, "get_news_tab_agent_if_ready", lambda: None)
    rows = [
        {
            "ticker": "MARKET",
            "title": "Kelp DAO reports a new crypto governance proposal",
            "published_at": pe._iso(datetime.now(timezone.utc)),
        },
        {
            "ticker": "MARKET",
            "title": "Youdao (NYSE: DAO) will report quarterly earnings next week",
            "published_at": pe._iso(datetime.now(timezone.utc)),
        },
    ]

    indexed = agent._build_news_index(rows, ["DAO"])

    assert [row["title"] for row in indexed["DAO"]] == [
        "Youdao (NYSE: DAO) will report quarterly earnings next week"
    ]
    assert indexed["DAO"][0]["_pe_relevance"] == 1.0


@pytest.mark.unit
def test_crypto_bb_article_is_not_blackberry_pre_earnings_evidence(monkeypatch):
    agent = _agent()
    monkeypatch.setattr(pe, "get_news_tab_agent_if_ready", lambda: None)
    rows = [
        {
            "ticker": "BB",
            "title": "BounceBit wallets transferred 286 million BB after exploit",
            "summary": "The crypto token moved across nine wallets.",
            "published_at": pe._iso(datetime.now(timezone.utc)),
            "is_crypto": True,
        },
        {
            "ticker": "BB",
            "title": "BlackBerry (NYSE: BB) expands its QNX partnership",
            "summary": "The company announced a new edge-AI integration.",
            "published_at": pe._iso(datetime.now(timezone.utc)),
            "is_crypto": False,
        },
    ]

    indexed = agent._build_news_index(rows, ["BB"])

    assert [row["title"] for row in indexed["BB"]] == [
        "BlackBerry (NYSE: BB) expands its QNX partnership"
    ]


@pytest.mark.unit
def test_youdao_result_release_in_summary_expires_dao_but_crypto_dao_does_not():
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    news = {
        "DAO": [
            {
                "ticker": "MARKET",
                "title": "Youdao Reports Second Quarter 2026 Unaudited Financial Results",
                "summary": "Youdao (NYSE: DAO) today announced its financial results.",
                "published_at": pe._iso(now - timedelta(hours=2)),
                "_pe_relevance": 1.0,
            },
            {
                "ticker": "MARKET",
                "title": "Binance says it stopped a DAO governance attack",
                "summary": "A crypto DAO proposal was blocked.",
                "published_at": pe._iso(now - timedelta(hours=1)),
                "_pe_relevance": 1.0,
            },
        ]
    }
    earnings = {"DAO": [{"symbol": "DAO", "date": pe._iso(now)}]}

    flagged = detect_already_reported_symbols(news, earnings, now=now)

    assert flagged["DAO"]["decisive_hits"] == 1
    assert flagged["DAO"]["headlines"] == [
        "Youdao Reports Second Quarter 2026 Unaudited Financial Results"
    ]


@pytest.mark.unit
def test_resolve_yf_earnings_rows_extracts_session_and_confidence(monkeypatch):
    agent = _agent()
    import pandas as pd

    # 2026-09-03: the event date must be dynamically upcoming — a fixed date
    # (Sep 1 2026) fell into the past and the lookahead filter legitimately
    # dropped the row, breaking this test two days after it was written.
    upcoming_dt = datetime.now(timezone.utc) + timedelta(days=2)
    upcoming_dt = upcoming_dt.replace(hour=16, minute=0, second=0, microsecond=0)
    mock_df = pd.DataFrame(
        {"EPS Estimate": [4.92, 2.96], "Reported EPS": [None, 4.86]},
        index=pd.DatetimeIndex([upcoming_dt, datetime(2026, 5, 28, 16, 0, tzinfo=timezone.utc)])
    )

    class MockTicker:
        def __init__(self, sym):
            self.sym = sym
        def get_earnings_dates(self, limit=8):
            return mock_df
        @property
        def calendar(self):
            return {"Earnings Date": [upcoming_dt.date()]}

    import yfinance as yf
    monkeypatch.setattr(yf, "Ticker", MockTicker)

    rows = agent._resolve_yf_earnings_rows(["DELL"], lookahead_days=5, force_refresh=True)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "DELL"
    assert rows[0]["session"] == "AMC"
    assert rows[0]["session_confidence"] == "provider"
    assert rows[0]["confirmed"] is True
    assert rows[0]["date_confidence"] == "high"
