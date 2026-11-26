"""Unit tests for the keyless crypto per-ticker news path (Faza 4 Part D).

Locks in: base-asset extraction, crypto-vs-equity routing in _fetch_ticker_news,
keyword word-boundary filtering, cross-source dedup, days_back cutoff, and
best-effort degradation (one source failing does not break the others).
All network is mocked — no live calls.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
import time as _time

import pytest

from agents.news_tab_agent import NewsArticle, NewsTabAgent


def _ms_ago(minutes: int) -> int:
    return int((datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp() * 1000)


def _struct_ago(minutes: int):
    # feedparser gives a UTC struct_time; calendar.timegm reads it as UTC.
    # Use gmtime so the round-trip is timezone-correct regardless of host TZ.
    return _time.gmtime((datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp())


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _binance_payload(title, code="abc123"):
    return {
        "data": {
            "catalogs": [
                {"articles": [{"title": title, "code": code, "releaseDate": _ms_ago(30)}]}
            ]
        }
    }


def _kucoin_payload(title, desc="", url="https://www.kucoin.com/announcement/x", ctime=None):
    return {
        "data": {
            "items": [{"annTitle": title, "annDesc": desc, "annUrl": url, "cTime": ctime or _ms_ago(45)}]
        }
    }


def _fake_feed(entries):
    return SimpleNamespace(entries=entries)


def _entry(title, summary, link, mins_ago):
    # feedparser entries are dict-like (support .get); use a plain dict.
    return {
        "title": title,
        "summary": summary,
        "link": link,
        "published_parsed": _struct_ago(mins_ago),
    }


def _make_agent():
    with patch.object(NewsTabAgent, "_load_backup", return_value=None):
        return NewsTabAgent({})


@pytest.mark.parametrize(
    "ticker,expected",
    [
        ("BTC-USD", "BTC"),
        ("ETHUSDT", "ETH"),
        ("BTCUSDT", "BTC"),
        ("BTCUSD", "BTC"),
        ("SOL-USDC", "SOL"),
        ("DOGE-USD", "DOGE"),
        ("BTCBUSD", "BTC"),
        ("LINK-USD", "LINK"),
    ],
)
def test_crypto_base_asset_extraction(ticker, expected):
    assert NewsTabAgent._crypto_base_asset(ticker) == expected


def test_fetch_ticker_news_routes_crypto_to_crypto_path():
    """A crypto ticker must hit _fetch_crypto_ticker_news, not the equity path."""
    agent = _make_agent()
    captured = {}

    def fake_crypto(ticker, limit=10, days_back=3):
        captured["called"] = (ticker, limit, days_back)
        return [NewsArticle(id="x", title="t", url="u", source="binance",
                            published_at=datetime.now(timezone.utc), ticker=ticker)]

    agent._fetch_crypto_ticker_news = fake_crypto
    out = agent._fetch_ticker_news("SOL-USD", watchlist=[], limit=10, days_back=3, include_yahoo=False)
    assert captured["called"] == ("SOL-USD", 10, 3)
    assert len(out) == 1 and out[0].source == "binance"


def test_fetch_ticker_news_does_not_route_equity_to_crypto():
    """An equity ticker must NOT be routed to the crypto path."""
    agent = _make_agent()
    called = {"crypto": False}

    def fake_crypto(*a, **k):
        called["crypto"] = True
        return []

    agent._fetch_crypto_ticker_news = fake_crypto
    # Finnhub unavailable + yahoo disabled => equity path returns [] but must
    # not have invoked the crypto path.
    with patch.object(NewsTabAgent, "_fetch_rss_ticker", return_value=[]), \
         patch("agents.news_tab_agent.get_finnhub_client") as mock_fh:
        mock_fh.return_value.is_available.return_value = False
        agent._fetch_ticker_news("AAPL", watchlist=[], limit=10, days_back=3, include_yahoo=False)
    assert called["crypto"] is False


def test_crypto_keyword_filter_keeps_base_drops_unrelated():
    """Only articles mentioning the base asset (word-boundary) are kept."""
    agent = _make_agent()
    entries = [
        _entry("Binance Will List SOL", "", "https://x/1", 10),       # matches SOL
        _entry("Ethereum merger update", "", "https://x/2", 12),      # ETH, not SOL
        _entry("WSOL staking guide", "", "https://x/3", 14),          # WSOL, not \bSOL\b
        _entry("Random market news", "SOL rally continues", "https://x/4", 16),  # summary matches \bSOL\b
    ]
    binance_payload = _binance_payload("Unrelated Binance news BTC")  # no SOL -> dropped

    def fake_http_get(url, *a, **k):
        if "binance.com" in url:
            return _FakeResp(binance_payload)
        if "kucoin.com" in url:
            return _FakeResp(_kucoin_payload("KuCoin lists ADA", url="https://ku/1"))  # no SOL -> dropped
        return None

    with patch("agents.news_tab_agent.http_get", side_effect=fake_http_get), \
         patch.object(NewsTabAgent, "_parse_feed", return_value=_fake_feed(entries)):
        out = agent._fetch_crypto_ticker_news("SOL-USD", limit=10, days_back=3)

    titles = {a.title for a in out}
    # entry 1 (title SOL) and entry 4 (summary SOL) kept; entry 2 (ETH) and
    # entry 3 (WSOL, word-boundary miss) dropped; binance/kucoin non-matching dropped.
    assert "Binance Will List SOL" in titles
    assert "Random market news" in titles
    assert "Ethereum merger update" not in titles
    assert "WSOL staking guide" not in titles
    # All returned articles are tagged crypto + ticker-scoped.
    for a in out:
        assert a.is_crypto is True
        assert a.ticker == "SOL-USD"
        assert a.source_scope == "ticker"


def test_crypto_dedup_across_sources():
    """The same article id from two sources collapses to one."""
    agent = _make_agent()
    link = "https://www.binance.com/en/support/announcement/dup"
    title = "Binance Will List BTC"
    # Binance returns the article; RSS also returns an entry with the SAME link+title
    # => same _generate_id => deduped to one.
    entries = [_entry(title, "", link, 20)]

    def fake_http_get(url, *a, **k):
        if "binance.com" in url:
            return _FakeResp({"data": {"catalogs": [{"articles": [
                {"title": title, "code": "dup", "releaseDate": _ms_ago(20)}]}]}})
        if "kucoin.com" in url:
            return _FakeResp({"data": {"items": []}})
        return None

    with patch("agents.news_tab_agent.http_get", side_effect=fake_http_get), \
         patch.object(NewsTabAgent, "_parse_feed", return_value=_fake_feed(entries)):
        out = agent._fetch_crypto_ticker_news("BTC-USD", limit=10, days_back=3)

    ids = [a.id for a in out]
    assert len(ids) == len(set(ids)), "duplicate ids survived dedup"


def test_crypto_cutoff_drops_old_articles():
    """Articles older than days_back are dropped."""
    agent = _make_agent()
    entries = [
        _entry("BTC rally today", "", "https://x/new", 60),          # 1h old, within 3d
        _entry("BTC news from long ago", "", "https://x/old", 60 * 24 * 10),  # 10d old
    ]
    with patch("agents.news_tab_agent.http_get", return_value=_FakeResp({"data": {}})), \
         patch.object(NewsTabAgent, "_parse_feed", return_value=_fake_feed(entries)):
        out = agent._fetch_crypto_ticker_news("BTC-USD", limit=10, days_back=3)
    titles = {a.title for a in out}
    assert "BTC rally today" in titles
    assert "BTC news from long ago" not in titles


def test_crypto_best_effort_one_source_failing_others_survive():
    """If Binance + KuCoin error out, RSS still returns articles."""
    agent = _make_agent()
    entries = [_entry("BTC pumps hard", "", "https://x/1", 15)]

    def fake_http_get(url, *a, **k):
        raise RuntimeError("network down")

    with patch("agents.news_tab_agent.http_get", side_effect=fake_http_get), \
         patch.object(NewsTabAgent, "_parse_feed", return_value=_fake_feed(entries)):
        out = agent._fetch_crypto_ticker_news("BTC-USD", limit=10, days_back=3)
    assert any(a.title == "BTC pumps hard" for a in out)


def test_crypto_respects_limit():
    agent = _make_agent()
    entries = [_entry(f"BTC update {i}", "", f"https://x/{i}", 10 + i) for i in range(8)]
    with patch("agents.news_tab_agent.http_get", return_value=_FakeResp({"data": {}})), \
         patch.object(NewsTabAgent, "_parse_feed", return_value=_fake_feed(entries)):
        out = agent._fetch_crypto_ticker_news("BTC-USD", limit=3, days_back=3)
    assert len(out) == 3
    # Sorted by published_at desc => newest first.
    assert out[0].title == "BTC update 0"