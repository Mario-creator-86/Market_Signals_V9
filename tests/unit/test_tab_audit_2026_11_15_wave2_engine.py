"""Tab audit 2026-11-15, wave 2 (engine zone) -- regression tests.

Defects confirmed in code and in ``logs/engine_20261115.log``:

* The engine subprocess ran its own full news refresh every five minutes next
  to the API process ("Refreshing news via NewsTabAgent...", "RSS ticker
  fetch", "News backup saved"): double provider traffic and two processes
  overwriting ``data/news_backup.json``.
* A VOLATILITY alert was announced as "VOLATILITY target reached".
* A delisted Watchlist symbol (SLP) kept a "verified" row from 2026-09-08 for
  good; a symbol with no data at all (CPRX) had no row.
* The scanner preferred yesterday's after-hours print during the pre-market
  and gave no session/time for ``premarket_price``; it asked Yahoo about
  symbols already known to be missing on every scan.
* Stablecoin pairs sat in the movers list; tokenised-stock tags were missing
  on the Watchlist crypto list.
* A failed agent run left no error in the agent status.
"""
import json
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import agents.scanner_agent as scanner_module
import core.engine as engine_mod
from agents.scanner_agent import ScannerAgent, is_stablecoin_pair, pick_session_price
from core.agent_metrics import redact_error_text, track_agent_run
from core.observability import snapshot_agent_status


# ─────────────────────────────────────────────────────────────────────────────
# Engine harness
# ─────────────────────────────────────────────────────────────────────────────


def _bare_engine(tickers=None, news=None):
    engine = object.__new__(engine_mod.MarketEngine)
    engine.config = SimpleNamespace(
        watchlist={"binance": ["BTC-USD"], "yahoo": ["AAPL"]},
        scan={},
        signal={"rsi_period": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9},
        news=dict(news or {}),
        config={},
    )
    engine.state_cache = {"tickers": dict(tickers or {})}
    engine.state_cache_lock = threading.Lock()
    engine.mtf_cache = {}
    engine.mtf_frame_cache = {}
    engine.mtf_cache_lock = threading.Lock()
    engine._yahoo_cache = {}
    engine._yahoo_cache_lock = threading.Lock()
    engine._yahoo_request_timestamps = deque(maxlen=100)
    engine.yfinance_lock = threading.Lock()
    engine.log_buffer = deque(maxlen=50)
    engine.news_agent = None
    engine.news_cache = {"ticker_news": {}, "market_news": [], "breaking_news": [], "loading": True}
    engine.last_news_check = None
    engine._is_agent_enabled = lambda key, default=True: True
    return engine


def _article(article_id, title, ticker="AAPL", published=None, **extra):
    row = {
        "id": article_id,
        "title": title,
        "url": f"https://news.example.com/{article_id}",
        "source": "Example Wire",
        "published_at": (published or datetime.now(timezone.utc)).isoformat(),
        "ticker": ticker,
        "category": "general",
        "sentiment": "positive",
        "sentiment_score": 0.4,
        "sentiment_confidence": 0.8,
        "impact": "medium",
        "impact_score": 55,
        "action_score": 40,
        "summary": "Shares moved after the company reported quarterly results.",
    }
    row.update(extra)
    return row


ITALIAN_TITLE = "Gli analisti sono convinti che il prezzo della azione sia anche più alto"
ITALIAN_SUMMARY = "Però nel mercato delle criptovalute gli investitori hanno quindi dei dubbi"


def _write_backup(path, ticker_rows, market_rows=()):
    payload = {
        "ticker_news": {"AAPL": list(ticker_rows)},
        "market_news": list(market_rows),
        "breaking_news": [],
        "last_update": datetime.now(timezone.utc).isoformat(),
        "active_filters": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


class _MirrorAgent:
    """Stand-in for NewsTabAgent: any refresh or save is a test failure."""

    def __init__(self):
        self.hydrated = []

    def hydrate_state(self, payload, reanalyze_existing=True):
        assert reanalyze_existing is False, "the mirror must not re-run sentiment analysis"
        self.hydrated.append(payload)

    def get_news_tab_state(self, active_tickers=None):
        latest = self.hydrated[-1] if self.hydrated else {}
        return {
            "ticker_news": latest.get("ticker_news", {}),
            "market_news": latest.get("market_news", []),
            "breaking_news": [],
            "last_update": latest.get("last_update"),
        }

    def refresh_all_news(self, *args, **kwargs):
        raise AssertionError("the engine subprocess must not fetch news")

    def refresh_single_ticker_news(self, *args, **kwargs):
        raise AssertionError("the engine subprocess must not fetch news")

    def _save_backup(self, *args, **kwargs):
        raise AssertionError("the engine subprocess must not write the news backup")

    _save_backup_from_snapshot = _save_backup


@pytest.fixture
def backup_path(tmp_path, monkeypatch):
    import agents.news_tab_agent as news_module

    path = tmp_path / "news_backup.json"
    monkeypatch.setattr(news_module, "NEWS_BACKUP_PATH", str(path))
    monkeypatch.setattr(news_module, "NEWS_QUARANTINE_PATH", str(tmp_path / "news_quarantine.json"))
    monkeypatch.setattr(news_module, "NEWS_SUPPRESSION_PATH", str(tmp_path / "news_suppression.json"))
    return path


# ─────────────────────────────────────────────────────────────────────────────
# 1. News: one refresh owner
# ─────────────────────────────────────────────────────────────────────────────


def test_engine_role_is_detected_from_the_child_process_marker(monkeypatch):
    monkeypatch.setenv("MS_PROCESS_ROLE", "engine")
    assert engine_mod.news_owned_by_api_process() is True
    monkeypatch.setenv("MS_PROCESS_ROLE", "api")
    assert engine_mod.news_owned_by_api_process() is False
    monkeypatch.delenv("MS_PROCESS_ROLE", raising=False)
    assert engine_mod.news_owned_by_api_process() is False


def test_engine_subprocess_mirrors_the_backup_and_never_fetches_or_saves(monkeypatch, backup_path):
    monkeypatch.setenv("MS_PROCESS_ROLE", "engine")
    first = _article("a1", "Apple (AAPL) beats on quarterly revenue")
    _write_backup(backup_path, [first])
    before = backup_path.read_bytes()
    before_mtime = backup_path.stat().st_mtime_ns

    engine = _bare_engine()
    agent = _MirrorAgent()
    engine._get_news_agent_nonblocking = lambda: agent
    now = datetime.now(timezone.utc)
    snapshot = {"tickers": {"AAPL": {}}}
    engine._update_snapshot_news(snapshot, now)

    assert len(agent.hydrated) == 1
    assert snapshot["news"]["loading"] is False
    assert snapshot["news"]["ticker_news"]["AAPL"][0]["id"] == "a1"
    mirror = snapshot["news"]["mirror"]
    assert mirror["source"] == "api_process_backup" and mirror["read_only"] is True
    assert mirror["articles"] == 1 and mirror["error"] == ""
    # The first mirror has nothing to compare against: no "new article" rows.
    assert "activity_log" not in snapshot
    # Read-only: the file is byte-identical and was not rewritten.
    assert backup_path.read_bytes() == before
    assert backup_path.stat().st_mtime_ns == before_mtime

    # Next scan a few seconds later: not due yet, the file is not even read.
    monkeypatch.setattr(
        engine_mod, "read_news_backup_readonly",
        lambda path=None: (_ for _ in ()).throw(AssertionError("mirror re-read before it was due")),
    )
    engine._update_snapshot_news({"tickers": {}}, now)
    assert len(agent.hydrated) == 1


def test_engine_mirror_reloads_only_when_the_api_process_saved_something_new(monkeypatch, backup_path):
    monkeypatch.setenv("MS_PROCESS_ROLE", "engine")
    first = _article("a1", "Apple (AAPL) beats on quarterly revenue")
    _write_backup(backup_path, [first])
    engine = _bare_engine()
    agent = _MirrorAgent()
    assert engine._mirror_news_from_backup(agent, force=True) == []
    assert len(agent.hydrated) == 1

    # Due again, same file: no reload.
    assert engine._mirror_news_from_backup(agent, force=True) == []
    assert len(agent.hydrated) == 1

    # The API process saved a new article.
    second = _article("a2", "Apple announces a new product line", sentiment="negative", impact="high")
    _write_backup(backup_path, [second, first])
    new_rows = engine._mirror_news_from_backup(agent, force=True)
    assert [row["id"] for row in new_rows] == ["a2"]
    assert len(agent.hydrated) == 2

    # Through the scan path the new article reaches the activity log.
    third = _article("a3", "Apple supplier raises guidance", sentiment="positive", impact="low")
    _write_backup(backup_path, [third, second, first])
    engine._news_mirror["checked_monotonic"] = None
    engine._get_news_agent_nonblocking = lambda: agent
    now = datetime.now(timezone.utc)
    snapshot = {"tickers": {}}
    engine._update_snapshot_news(snapshot, now)
    assert snapshot["activity_log"] == [{
        "time": now.isoformat(),
        "desc": "News: Apple supplier raises guidance (AAPL)",
        "type": "news",
        "sentiment": "positive",
        "impact": "low",
    }]


def test_engine_mirror_survives_a_missing_or_broken_backup(monkeypatch, backup_path):
    monkeypatch.setenv("MS_PROCESS_ROLE", "engine")
    engine = _bare_engine()
    agent = _MirrorAgent()
    assert engine._mirror_news_from_backup(agent, force=True) == []
    assert agent.hydrated == [] and engine._news_mirror_status()["error"] == "news backup file not found"

    backup_path.write_text("{ not json", encoding="utf-8")
    assert engine._mirror_news_from_backup(agent, force=True) == []
    assert agent.hydrated == [] and engine._news_mirror_status()["error"] == "news backup unreadable"
    assert backup_path.read_text(encoding="utf-8") == "{ not json"


def test_mirror_payload_drops_rows_the_agent_would_quarantine_on_every_reload():
    good = _article("a1", "Apple (AAPL) beats on quarterly revenue")
    payload, rows = engine_mod.hydratable_news_payload({
        "ticker_news": {
            "AAPL": [
                good,
                _article("a2", ""),
                {**_article("a3", "No date on this one"), "published_at": ""},
                _article("a4", ITALIAN_TITLE, summary=ITALIAN_SUMMARY),
                "not-a-dict",
            ],
            "EMPTY": [_article("a5", "")],
        },
        "market_news": [good, None],
        "breaking_news": "corrupt",
        "last_update": "2026-11-15T08:57:15+00:00",
    })
    assert [row["id"] for row in payload["ticker_news"]["AAPL"]] == ["a1"]
    assert "EMPTY" not in payload["ticker_news"]
    assert payload["market_news"] == [good] and payload["breaking_news"] == []
    assert list(rows) == ["a1"] and payload["last_update"] == "2026-11-15T08:57:15+00:00"


def test_mirror_feeds_the_real_news_agent_without_any_write(monkeypatch, backup_path, tmp_path):
    """End to end with the real agent: readers see what the API process saved,
    the sentiment model is not loaded, and nothing is written to disk."""
    import agents.news_tab_agent as news_module

    monkeypatch.setenv("MS_PROCESS_ROLE", "engine")
    monkeypatch.setattr(
        news_module, "get_financial_sentiment_service",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("the mirror must not load the sentiment model")),
    )
    agent = news_module.NewsTabAgent({})
    monkeypatch.setattr(
        agent, "_save_backup_from_snapshot",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("the mirror must not save the backup")),
    )
    assert agent.get_ticker_news("AAPL") == []

    rows = [
        _article("a1", "Apple (AAPL) beats on quarterly revenue"),
        _article("a2", ITALIAN_TITLE, summary=ITALIAN_SUMMARY),
    ]
    _write_backup(backup_path, rows)
    before = backup_path.read_bytes()

    engine = _bare_engine()
    engine._mirror_news_from_backup(agent, force=True)
    titles = [row["title"] for row in agent.get_ticker_news("AAPL")]
    assert titles == ["Apple (AAPL) beats on quarterly revenue"]
    assert backup_path.read_bytes() == before
    assert not (tmp_path / "news_quarantine.json").exists()
    assert not (tmp_path / "news_backup.json.bak").exists()


def test_thread_mode_engine_still_refreshes_news_itself(monkeypatch, backup_path):
    """Single process (MS_PROCESS_ROLE=api): behaviour is unchanged."""
    monkeypatch.setenv("MS_PROCESS_ROLE", "api")
    calls = []

    class _OwnerAgent:
        def refresh_all_news(self, tickers):
            calls.append(list(tickers))
            article = SimpleNamespace(
                title="Apple beats on quarterly revenue", ticker="AAPL",
                sentiment=SimpleNamespace(value="positive"), impact=SimpleNamespace(value="high"),
            )
            return {}, [article]

        def get_news_tab_state(self, active_tickers=None):
            return {"ticker_news": {}, "market_news": [], "breaking_news": []}

        def hydrate_state(self, *args, **kwargs):
            raise AssertionError("thread mode must not mirror the backup")

    engine = _bare_engine(news={"news_refresh_minutes": 5})
    agent = _OwnerAgent()
    engine._get_news_agent_nonblocking = lambda: agent
    now = datetime.now(timezone.utc)
    snapshot = {"tickers": {"AAPL": {}, "BTC-USD": {}}}
    engine._update_snapshot_news(snapshot, now)

    assert calls == [["AAPL", "BTC-USD"]]
    assert engine.last_news_check == now
    assert "mirror" not in snapshot["news"] and snapshot["news"]["loading"] is False
    assert snapshot["activity_log"][0] == {
        "time": now.isoformat(), "desc": "News: Apple beats on quarterly revenue (AAPL)",
        "type": "news", "sentiment": "positive", "impact": "high",
    }
    # Within the refresh interval nothing is fetched again.
    engine._update_snapshot_news({"tickers": {"AAPL": {}}}, now + timedelta(minutes=1))
    assert len(calls) == 1


def test_startup_news_refresh_is_skipped_in_the_engine_subprocess(monkeypatch):
    engine = _bare_engine(news={"startup_refresh_enabled": True})
    started = []
    monkeypatch.setattr(
        engine_mod.threading, "Thread",
        lambda *a, **k: started.append(k.get("name")) or SimpleNamespace(start=lambda: None),
    )
    monkeypatch.setenv("MS_PROCESS_ROLE", "engine")
    engine.start_background_news_refresh()
    assert started == []
    monkeypatch.setenv("MS_PROCESS_ROLE", "api")
    engine.start_background_news_refresh()
    assert started == ["StartupNewsRefresh"]


# ─────────────────────────────────────────────────────────────────────────────
# 2. Alert wording
# ─────────────────────────────────────────────────────────────────────────────


def test_volatility_alert_text_says_what_moved_and_by_how_much():
    alert = {"ticker": "NVDA", "type": "VOLATILITY", "target": 5.0}
    texts = engine_mod.MarketEngine._alert_texts(alert, 187.3, {"change_pct": -6.234, "asset_type": "stock"})
    assert texts["title"] == "\U0001F514 NVDA — moved -6.23% today (threshold 5%)"
    assert texts["lines"] == ["Move today: -6.23%", "Threshold: 5% move", "Price now: 187.3"]
    assert texts["event"] == "Smart Alert: NVDA moved -6.23% today (threshold 5%)"
    for text in (texts["title"], texts["event"], texts["log"], *texts["lines"]):
        assert "target" not in text.lower()

    crypto = engine_mod.MarketEngine._alert_texts(
        {"ticker": "BTC-USD", "type": "VOLATILITY", "target": 7.5}, 64000.0,
        {"change_pct": 8.1, "asset_type": "crypto"},
    )
    assert crypto["title"].endswith("moved +8.10% in the last 24h (threshold 7.5%)")

    # No row for the ticker: the threshold is still described, no number invented.
    bare = engine_mod.MarketEngine._alert_texts(alert, 187.3, None)
    assert bare["title"].endswith("moved 5% or more today")
    assert bare["lines"] == ["Threshold: 5% move", "Price now: 187.3"]


def test_price_alert_text_is_unchanged():
    texts = engine_mod.MarketEngine._alert_texts(
        {"ticker": "AAPL", "type": "PRICE_UP", "target": 360.0}, 361.2, {"change_pct": 1.1}
    )
    assert texts["title"] == "\U0001F514 AAPL — PRICE UP target reached"
    assert texts["lines"] == ["Target: 360.0", "Price now: 361.2"]
    assert texts["event"] == "Smart Alert: AAPL PRICE UP target 360.0 reached!"
    assert texts["log"] == "\U0001F514 ALERT: AAPL hit PRICE_UP target 360.0 (@361.2)"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Delisted / unknown symbols
# ─────────────────────────────────────────────────────────────────────────────


def _closed_weekend(monkeypatch):
    import core.market_hours as market_hours

    monkeypatch.setenv("MS_MARKET_HOURS_OVERRIDE", "closed")
    monkeypatch.setattr(
        market_hours,
        "us_equity_session_snapshot",
        lambda now=None: {
            "state": "CLOSED", "closed_kind": "weekend", "label": "Market closed (weekend)",
            "last_regular_close_utc": (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat(),
        },
    )


def test_known_missing_symbol_row_is_marked_inactive_once(monkeypatch):
    import core.stock_data_client as sdc_module

    _closed_weekend(monkeypatch)
    monkeypatch.setattr(sdc_module, "is_known_missing", lambda symbol: True)
    old_row = {
        "price": 5.11, "change_pct": 1.2, "last_update": "2026-09-08T22:56:46+03:00",
        "data_quality": "verified", "price_verified": True, "session": None,
        "price_integrity": {"is_verified": True, "status": "verified"},
        "data_source": "yahoo", "trend_1d": "BULL", "rsi": 47.2, "macd_hist": 0.3,
    }
    engine = _bare_engine(tickers={"SLP": old_row})
    engine._fetch_market_data = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a known-missing symbol must not be requested")
    )
    now = datetime.now(timezone.utc)
    result = engine._process_stock_ticker("SLP", now)
    data = result["data"]
    assert result["signal"] is None
    assert data["session"] == "inactive" and data["data_quality"] == "listing_inactive"
    assert data["price_verified"] is False and data["price_integrity"]["is_verified"] is False
    assert data["price_integrity"]["status"] == "listing_inactive"
    assert "2026-09-08" in data["price_integrity"]["reason"]
    # Last known price, dated by the day it was actually seen.
    assert data["price"] == 5.11 and data["price_as_of"] == "2026-09-08"
    assert data["change_pct"] == 0.0 and data["rsi"] is None and data["trend_1d"] == "N/A"
    assert data["last_update"] == now.isoformat()

    # Marked once: the next scans leave the row alone.
    engine.state_cache["tickers"]["SLP"] = data
    assert engine._process_stock_ticker("SLP", now) is None
    engine._closed_refresh_at.clear()
    assert engine._process_stock_ticker("SLP", now) is None
    assert engine._unavailable_stock_result("SLP", now, data, known_missing=True) is None


def test_symbol_without_any_data_gets_a_no_data_row(monkeypatch):
    import core.stock_data_client as sdc_module

    _closed_weekend(monkeypatch)
    monkeypatch.setattr(sdc_module, "is_known_missing", lambda symbol: False)
    engine = _bare_engine(tickers={})
    engine._fetch_market_data = lambda *a, **k: None
    now = datetime.now(timezone.utc)
    result = engine._process_stock_ticker("CPRX", now)
    data = result["data"]
    assert result["signal"] is None
    assert data["data_quality"] == "no_data" and data["price_integrity"]["status"] == "no_data"
    assert data["price_verified"] is False and data["price_available"] is False
    # 0.0 is the app's "no price" value; nothing is invented.
    assert data["price"] == 0.0 and data["technical_price"] is None and data["price_as_of"] is None
    assert "CPRX" in data["price_integrity"]["reason"]
    assert data["asset_type"] == "stock" and data["rsi"] is None and data["trend_1d"] == "N/A"

    # Written once.
    engine.state_cache["tickers"]["CPRX"] = data
    engine._closed_refresh_at.clear()
    assert engine._process_stock_ticker("CPRX", now) is None


def test_one_empty_answer_does_not_mark_a_priced_row_inactive(monkeypatch):
    import core.stock_data_client as sdc_module

    _closed_weekend(monkeypatch)
    monkeypatch.setattr(sdc_module, "is_known_missing", lambda symbol: False)
    row = {"price": 316.02, "last_update": "2026-11-13T22:00:00+02:00", "data_quality": "verified", "price_verified": True}
    engine = _bare_engine(tickers={"AAPL": row})
    engine._fetch_market_data = lambda *a, **k: None
    assert engine._process_stock_ticker("AAPL", datetime.now(timezone.utc)) is None


def test_regular_session_without_candles_reports_no_data(monkeypatch):
    import core.finnhub_client as finnhub_module
    import core.stock_data_client as sdc_module

    monkeypatch.setenv("MS_MARKET_HOURS_OVERRIDE", "open")
    monkeypatch.setattr(sdc_module, "is_known_missing", lambda symbol: True)
    monkeypatch.setattr(
        finnhub_module, "get_finnhub_client", lambda: SimpleNamespace(is_available=lambda: False)
    )
    monkeypatch.setattr(
        "core.engine.yf.Ticker", lambda *a, **k: (_ for _ in ()).throw(AssertionError("known missing: no Yahoo call"))
    )
    engine = _bare_engine(tickers={})
    engine._fetch_market_data = lambda *a, **k: None
    result = engine._process_stock_ticker("CPRX", datetime.now(timezone.utc))
    assert result["data"]["data_quality"] == "no_data" and result["data"]["session"] == "inactive"
    assert "unknown to the data provider" in result["data"]["price_integrity"]["reason"]


# ─────────────────────────────────────────────────────────────────────────────
# 4. Scanner: which session a price belongs to
# ─────────────────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _binance_row(symbol, last, change, quote_volume):
    return {
        "symbol": symbol,
        "lastPrice": str(last),
        "priceChangePercent": str(change),
        "quoteVolume": str(quote_volume),
        "volume": "1000",
    }


QUOTE = {
    "symbol": "HTFL",
    "quoteType": "EQUITY",
    "shortName": "Heartflow, Inc.",
    "exchange": "NMS",
    "regularMarketPrice": 48.46,
    "regularMarketPreviousClose": 59.92,
    "regularMarketChangePercent": -19.1255,
    "regularMarketTime": 1794603601,
    "regularMarketVolume": 3704634,
    "postMarketPrice": 48.15,
    "postMarketTime": 1794616178,
    "preMarketPrice": 50.10,
    "preMarketTime": 1794663000,
}


@pytest.fixture
def scanner(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner_module, "SCANNER_SHARED_STATE_PATH", tmp_path / "scanner_state.json")
    return ScannerAgent(SimpleNamespace(scan={"scan_interval_seconds": 60}))


def _serve(monkeypatch, *, binance=None, screener=None):
    def fake_http_get(url, **_kwargs):
        if "binance.com" in url:
            return binance
        if "screener/predefined" in url:
            return screener if "day_gainers" in url else _Resp({"finance": {"result": [{"quotes": []}]}})
        return None

    monkeypatch.setattr(scanner_module, "http_get", fake_http_get)


def _screener(quote):
    return _Resp({"finance": {"result": [{"quotes": [quote]}]}})


def test_pick_session_price_follows_the_market_state():
    # Pre-market: this morning's print, not yesterday's after-hours one.
    assert pick_session_price({"marketState": "PRE"}, 50.10, 48.15, 48.46) == ("pre", 50.10)
    assert pick_session_price({"marketState": "PREPRE"}, 0.0, 48.15, 48.46) == ("post", 48.15)
    # After the close: the after-hours print; never this morning's pre-market one.
    assert pick_session_price({"marketState": "POST"}, 50.10, 48.15, 48.46) == ("post", 48.15)
    assert pick_session_price({"marketState": "CLOSED"}, 50.10, 0.0, 48.46) == ("regular", 48.46)
    # Regular session: the live regular price.
    assert pick_session_price({"marketState": "REGULAR"}, 50.10, 48.15, 48.46) == ("regular", 48.46)
    # No market state: the print with the newest market time wins...
    timed = {"preMarketTime": 300, "postMarketTime": 200, "regularMarketTime": 100}
    assert pick_session_price(timed, 50.10, 48.15, 48.46) == ("pre", 50.10)
    # ...and without times the previous order (post, pre, regular) is kept.
    assert pick_session_price({}, 50.10, 48.15, 48.46) == ("post", 48.15)
    assert pick_session_price({}, 0.0, 0.0, 48.46) == ("regular", 48.46)


@pytest.mark.parametrize(
    "state, session, price, time_key",
    [
        ("PRE", "pre", 50.10, "preMarketTime"),
        ("POST", "post", 48.15, "postMarketTime"),
        ("CLOSED", "post", 48.15, "postMarketTime"),
        ("REGULAR", "regular", 48.46, "regularMarketTime"),
    ],
)
def test_scanner_row_says_which_session_and_when(monkeypatch, scanner, state, session, price, time_key):
    _serve(monkeypatch, screener=_screener({**QUOTE, "marketState": state}))
    rows = scanner._scan_stocks()
    row = next(item for item in rows if item["symbol"] == "HTFL")
    expected_time = datetime.fromtimestamp(QUOTE[time_key], tz=timezone.utc).isoformat()
    assert row["price_session"] == session and row["session"] == session
    assert row["price_time_utc"] == expected_time and row["quote_time_utc"] == expected_time
    assert row["market_state"] == state
    # Kept for existing consumers, now labelled by the two fields above.
    assert row["premarket_price"] == price and row["price"] == price
    if session == "regular":
        assert row["change_pct"] == -19.13
    else:
        assert row["change_pct"] == round((price - 48.46) / 48.46 * 100.0, 2)
    assert row["regular_price"] == 48.46 and row["regular_change_pct"] == -19.13


def test_watchlist_scan_does_not_ask_yahoo_about_known_missing_symbols(monkeypatch):
    import core.stock_data_client as sdc_module

    monkeypatch.setattr(sdc_module, "is_known_missing", lambda symbol: symbol in {"SLP", "CPRX"})
    agent = ScannerAgent(SimpleNamespace(
        scan={"scan_interval_seconds": 60},
        agents={"scanner": {}},
        watchlist={"yahoo": ["AAPL", "SLP", "CPRX"], "settings": {}},
    ))
    requested = []

    class _YF:
        @staticmethod
        def download(symbols, **kwargs):
            requested.append(list(symbols))
            import pandas as pd

            index = pd.date_range(end="2026-11-13", periods=3, freq="B")
            return pd.DataFrame({"Close": [100.0, 101.0, 102.0], "Volume": [10, 11, 12]}, index=index)

    monkeypatch.setattr(scanner_module, "yf", _YF)
    rows = agent._scan_watchlist_stocks([])
    assert requested == [["AAPL"]]
    assert [row["symbol"] for row in rows] == ["AAPL"]

    # Every missing symbol is known-missing: Yahoo is not called at all.
    monkeypatch.setattr(sdc_module, "is_known_missing", lambda symbol: True)
    agent.last_watchlist_stock_scan_time = 0.0
    requested.clear()
    assert agent._scan_watchlist_stocks([]) == []
    assert requested == []


# ─────────────────────────────────────────────────────────────────────────────
# 5. Stablecoins and tokenised stocks
# ─────────────────────────────────────────────────────────────────────────────


def test_stablecoin_pairs_are_recognised_by_peg_not_by_name_alone():
    for symbol in ("USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "USDPUSDT", "DAIUSDT", "USD1USDT", "USDC-USD"):
        assert is_stablecoin_pair(symbol, 1.0003), symbol
    assert is_stablecoin_pair("RLUSDUSDT", 0.9991)      # unlisted name containing USD, on the peg
    assert is_stablecoin_pair("EURIUSDT", 1.16)         # euro stablecoin
    assert not is_stablecoin_pair("BTCUSDT", 63956.94)
    assert not is_stablecoin_pair("BTCUSDC", 63950.0)
    assert not is_stablecoin_pair("NVDABUSDT", 159.65)
    assert not is_stablecoin_pair("USTCUSDT", 0.02)     # de-pegged: a real mover
    assert not is_stablecoin_pair("DAIUSDT", 0.62)      # lost its peg
    assert not is_stablecoin_pair("WUSDXUSDT", 3.4)     # name only, price nowhere near 1
    assert not is_stablecoin_pair("USDT", 1.0)


def test_stablecoins_are_tagged_and_never_listed_as_movers(monkeypatch, scanner):
    rows = [
        _binance_row("BTCUSDT", 63956.94, -4.82, 2_500_000_000),
        _binance_row("USDCUSDT", 1.0004, 0.9, 1_900_000_000),
        _binance_row("FDUSDUSDT", 0.9992, -0.7, 900_000_000),
        _binance_row("KAVAUSDT", 0.02858, -58.15, 23_000_000),
        _binance_row("ARBUSDT", 0.11, 0.2, 8_000_000),
    ]
    _serve(monkeypatch, binance=_Resp(rows))
    scanner.crypto_hot_list_size = 3
    hot = scanner._scan_crypto()
    # Without the exclusion USDCUSDT (+0.9%) and FDUSDUSDT (-0.7%) outrank ARB (+0.2%).
    assert [row["symbol"] for row in hot] == ["KAVAUSDT", "BTCUSDT", "ARBUSDT"]
    liquid = {row["symbol"]: row for row in scanner.cached_liquid_crypto_list}
    # Still in the liquid universe (owner decision), but labelled.
    assert liquid["USDCUSDT"]["instrument_type"] == "stablecoin" and liquid["USDCUSDT"]["is_stablecoin"] is True
    assert liquid["FDUSDUSDT"]["instrument_type"] == "stablecoin"
    assert "instrument_type" not in liquid["BTCUSDT"] and "is_stablecoin" not in liquid["BTCUSDT"]


def test_tokenised_stock_tag_is_on_every_published_list(monkeypatch, scanner, tmp_path):
    scanner.config_manager = SimpleNamespace(
        scan={"scan_interval_seconds": 60},
        agents={"scanner": {}},
        watchlist={"binance": ["NVDAB-USD", "BTC-USD", "USDC-USD"], "yahoo": [], "settings": {}},
    )
    rows = [
        _binance_row("BTCUSDT", 63956.94, -4.82, 2_500_000_000),
        _binance_row("NVDABUSDT", 159.65, -1.4, 24_900_000),
        _binance_row("USDCUSDT", 1.0004, 0.01, 1_900_000_000),
    ]
    nvda = {
        "symbol": "NVDA", "quoteType": "EQUITY", "shortName": "NVIDIA", "exchange": "NMS",
        "regularMarketPrice": 160.0, "regularMarketPreviousClose": 162.0,
        "regularMarketChangePercent": -1.23, "regularMarketTime": 1794603601, "marketState": "CLOSED",
    }
    _serve(monkeypatch, binance=_Resp(rows), screener=_screener(nvda))
    scanner.run_scan()
    state = scanner.get_state()

    for key in ("hot_tickers", "liquid_crypto_tickers", "watchlist_crypto_tickers"):
        row = next(item for item in state[key] if item["symbol"] == "NVDABUSDT")
        assert row["instrument_type"] == "tokenized_stock", key
        assert row["underlying"] == "NVDA" and row["is_tokenized_stock"] is True, key
        btc = next(item for item in state[key] if item["symbol"] == "BTCUSDT")
        assert "instrument_type" not in btc and "underlying" not in btc, key
    watch = {row["symbol"]: row for row in state["watchlist_crypto_tickers"]}
    assert watch["USDCUSDT"]["instrument_type"] == "stablecoin"

    # The hand-off file read by the other process carries the same tags.
    published = json.loads((tmp_path / "scanner_state.json").read_text(encoding="utf-8"))
    for key in ("hot_tickers", "liquid_crypto_tickers", "watchlist_crypto_tickers"):
        row = next(item for item in published[key] if item["symbol"] == "NVDABUSDT")
        assert row["instrument_type"] == "tokenized_stock" and row["underlying"] == "NVDA", key

    # The next scan rebuilds every row; the verified tag is there from the start.
    _serve(monkeypatch, binance=_Resp(rows), screener=_screener(nvda))
    monkeypatch.setattr(scanner, "_tag_tokenized_stocks", lambda *a, **k: 0)
    hot = scanner._scan_crypto()
    assert next(item for item in hot if item["symbol"] == "NVDABUSDT")["underlying"] == "NVDA"
    liquid = {row["symbol"]: row for row in scanner.cached_liquid_crypto_list}
    assert liquid["NVDABUSDT"]["instrument_type"] == "tokenized_stock"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Agent status keeps the last failure
# ─────────────────────────────────────────────────────────────────────────────


def test_failed_run_is_recorded_and_cleared_by_the_next_success():
    name = "wave2_outcome_agent"
    with track_agent_run(name):
        pass
    status = snapshot_agent_status()[name]
    assert status["last_run_ok"] is True and status["last_run_error"] == ""
    assert status["consecutive_failures"] == 0

    for attempt in (1, 2):
        with pytest.raises(RuntimeError):
            with track_agent_run(name):
                raise RuntimeError("Yahoo screener returned HTTP 503")
        status = snapshot_agent_status()[name]
        assert status["last_run_ok"] is False
        assert status["last_run_error"] == "Yahoo screener returned HTTP 503"
        assert status["last_run_error_type"] == "RuntimeError"
        assert status["consecutive_failures"] == attempt
        failed_at = datetime.fromisoformat(status["last_run_error_at"])
        assert abs((datetime.now(timezone.utc) - failed_at).total_seconds()) < 60
        # Existing fields are still published.
        assert "last_run_duration_ms" in status and "last_run_external_fetches" in status

    with track_agent_run(name):
        pass
    status = snapshot_agent_status()[name]
    assert status["last_run_ok"] is True
    assert status["last_run_error"] == "" and status["last_run_error_at"] == ""
    assert status["last_run_error_type"] == "" and status["consecutive_failures"] == 0
    assert status["last_run_finished_at"]


def test_run_error_is_bounded_and_carries_no_credentials():
    name = "wave2_redaction_agent"
    secret_url = (
        "https://finnhub.io/api/v1/quote?symbol=AAPL&token=c0ffee1234567890SECRET "
        "then https://api.telegram.org/bot123456789:AAE-secretTokenValue_xyz/sendMessage "
        "and https://user:hunter2@proxy.example.com/x?apikey=ABCDEF123456&plain=ok "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
    )
    with pytest.raises(ValueError):
        with track_agent_run(name):
            raise ValueError(secret_url + " " + "x" * 2000)
    error = snapshot_agent_status()[name]["last_run_error"]
    assert len(error) <= 300 and error.endswith("...")
    for secret in ("c0ffee1234567890SECRET", "AAE-secretTokenValue_xyz", "hunter2", "ABCDEF123456", "eyJhbGciOiJIUzI1NiJ9"):
        assert secret not in error
    assert "token=***" in error and "bot***" in error and "apikey=***" in error
    assert "symbol=AAPL" in error and "plain=ok" in error

    assert redact_error_text("") == "" and redact_error_text(None) == ""
    assert redact_error_text("line one\n   line two") == "line one line two"
    # An exception without a message still says what failed.
    with pytest.raises(KeyError):
        with track_agent_run(name):
            raise KeyError()
    assert snapshot_agent_status()[name]["last_run_error"] == "KeyError"
