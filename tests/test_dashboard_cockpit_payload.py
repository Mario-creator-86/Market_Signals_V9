from api.dashboard_service import (
    _build_dashboard_earnings_7d,
    _build_dashboard_watchlist_priority,
    _build_dashboard_risk_flags,
)


def test_dashboard_earnings_7d_filters_and_orders():
    tickers = [
        {"ticker": "AAPL", "earnings_days": 3, "earnings_date": "2026-02-11", "news_pulse_score": 70, "has_open_signal": False},
        {"ticker": "TSLA", "earnings_days": 1, "earnings_date": "2026-02-09", "news_pulse_score": 30, "has_open_signal": True},
        {"ticker": "NVDA", "earnings_days": 10, "earnings_date": "2026-02-18", "news_pulse_score": 80, "has_open_signal": True},
    ]
    rows = _build_dashboard_earnings_7d(tickers)
    assert [r["ticker"] for r in rows] == ["TSLA", "AAPL"]


def test_dashboard_watchlist_priority_prefers_signal_and_near_earnings():
    tickers = [
        {
            "ticker": "MSFT",
            "price": 420,
            "change_pct": 1.2,
            "earnings_days": 6,
            "news_pulse_score": 40,
            "has_open_signal": False,
            "is_hot": False,
            "trend_alignment": "BULL_ALIGN",
        },
        {
            "ticker": "AMD",
            "price": 200,
            "change_pct": 4.1,
            "earnings_days": 1,
            "news_pulse_score": 75,
            "has_open_signal": True,
            "is_hot": True,
            "trend_alignment": "MIXED",
        },
    ]
    rows = _build_dashboard_watchlist_priority(tickers, ["AMD", "MSFT"])
    assert rows[0]["ticker"] == "AMD"
    assert rows[0]["priority_score"] > rows[1]["priority_score"]


def test_dashboard_risk_flags_builds_expected_types():
    tickers = [
        {
            "ticker": "NFLX",
            "earnings_days": 0,
            "earnings_when_desc": "After Hours",
            "trend_alignment": "MIXED",
            "risk_flags": ["high_news_risk"],
            "news_pulse_score": 88,
            "trend_1d": "BULL",
            "col_1h": "BEAR 10:30",
        }
    ]
    news_items = [
        {
            "ticker": "NFLX",
            "impact": "critical",
            "title": "Netflix guidance shock",
            "published_at": "2026-02-08T10:00:00+00:00",
        }
    ]
    flags = _build_dashboard_risk_flags(tickers, news_items)
    kinds = {f["type"] for f in flags}
    assert "NEWS_CRITICAL" in kinds
    assert "EARNINGS_SOON" in kinds
    assert "RISK_FLAG" in kinds
