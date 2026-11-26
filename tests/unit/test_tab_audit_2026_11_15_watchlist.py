"""Regression tests for the Watchlist tab audit (2026-11-15).

Evidence from the running app on a Sunday (US market closed):
- every stock row carried a green LIVE badge for a Friday close, and delisted
  names (EA: last trade 2026-08-04) showed LIVE +0.00%;
- /api/live-prices took 8-13 s per batch of 8 symbols (two yfinance requests
  per symbol);
- VANTI.PA (Paris, EUR) was printed with a dollar sign;
- the Trend / RSI / MACD of every row dated from 2026-09-08 but carried only a
  time-of-day stamp;
- a failed Quick AI run was shown as a "neutral / WAIT" verdict.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from api import state_service
from api.market_data_service import YahooClient
from core import watchlist_quick_ai as quick_ai


# --------------------------------------------------------------------------
# Yahoo quote snapshot parsing
# --------------------------------------------------------------------------

FRIDAY_CLOSE = 1794603601  # 2026-11-13 21:00:01 UTC
SUNDAY_NOON = 1794744000   # 2026-11-15 12:00:00 UTC


def _spark_block(**meta):
    base = {
        "currency": "USD",
        "fullExchangeName": "NasdaqGS",
        "regularMarketPrice": 356.27,
        "regularMarketTime": FRIDAY_CLOSE,
        "previousClose": 354.88,
        "currentTradingPeriod": {
            "pre": {"start": FRIDAY_CLOSE - 43200, "end": FRIDAY_CLOSE - 23400},
            "regular": {"start": FRIDAY_CLOSE - 23400, "end": FRIDAY_CLOSE},
            "post": {"start": FRIDAY_CLOSE, "end": FRIDAY_CLOSE + 14400},
        },
    }
    base.update(meta)
    return {"meta": base, "timestamp": [], "indicators": {"quote": [{"close": []}]}}


def test_snapshot_keeps_the_exchange_last_trade_time_on_a_closed_market():
    quote = YahooClient.parse_spark_quote(_spark_block(), now_ts=SUNDAY_NOON)

    assert quote["price"] == 356.27
    assert quote["market_time"] == "2026-11-13T21:00:01+00:00"
    assert quote["market_open"] is False
    assert quote["currency"] == "USD"
    assert quote["change_pct"] == round((356.27 - 354.88) / 354.88 * 100, 2)


def test_snapshot_prefers_a_newer_extended_hours_bar():
    block = _spark_block()
    block["timestamp"] = [FRIDAY_CLOSE - 60, FRIDAY_CLOSE + 14380]
    block["indicators"]["quote"][0]["close"] = [356.2, 355.3849]

    quote = YahooClient.parse_spark_quote(block, now_ts=SUNDAY_NOON)

    assert quote["price"] == 355.3849
    assert quote["session"] == "extended"
    assert quote["market_time"] == datetime.fromtimestamp(FRIDAY_CLOSE + 14380, tz=timezone.utc).isoformat()
    # Extended-hours change is still measured against the previous close.
    assert quote["change_pct"] == 0.14


def test_snapshot_never_invents_a_zero_change_or_a_price():
    # EA after the buy-out: a last price from August, no previous close.
    delisted = YahooClient.parse_spark_quote(
        _spark_block(regularMarketPrice=209.7, regularMarketTime=1785873600, previousClose=0.0, chartPreviousClose=0.0),
        now_ts=SUNDAY_NOON,
    )
    assert delisted["change_pct"] is None
    assert delisted["market_time"].startswith("2026-08-04")

    # CPRX: the provider has no price at all -> no quote, not a 0.00 quote.
    assert YahooClient.parse_spark_quote(
        {"meta": {"currency": None, "regularMarketPrice": None}, "timestamp": [], "indicators": {"quote": [{}]}}
    ) is None
    assert YahooClient.parse_spark_quote(None) is None


def test_snapshot_reports_the_quote_currency_and_venue():
    quote = YahooClient.parse_spark_quote(
        _spark_block(currency="EUR", fullExchangeName="Paris", regularMarketPrice=0.084, previousClose=0.0824),
        now_ts=SUNDAY_NOON,
    )
    assert quote["currency"] == "EUR"
    assert quote["exchange"] == "Paris"


def test_quote_snapshots_use_one_request_for_the_whole_batch(monkeypatch):
    calls = []

    class Resp:
        status_code = 200
        content = b"x"

        def __init__(self, symbols):
            self._symbols = symbols

        def json(self):
            return {"spark": {"result": [
                {"symbol": s, "response": [_spark_block()]} for s in self._symbols if s != "CPRX"
            ] + [{"symbol": "CPRX", "response": [{"meta": {"regularMarketPrice": None}}]}]}}

    def fake_http_get(url, **kwargs):
        symbols = kwargs["params"]["symbols"].split(",")
        calls.append(symbols)
        return Resp(symbols)

    monkeypatch.setattr("api.market_data_service.http_get", fake_http_get)
    monkeypatch.setattr(YahooClient, "_quote_cache", {})

    symbols = ["AAPL", "NVDA", "MSFT", "TSLA", "AMZN", "EA", "CPRX", "SLP"]
    quotes = YahooClient.get_quote_snapshots(symbols, ttl=20)

    assert len(calls) == 1, "8 symbols must cost one provider request, not 16"
    assert set(quotes) == set(symbols) - {"CPRX"}, "an unpriced symbol is absent, not zero"

    # Within the ttl nothing is re-fetched (CPRX is retried: it was never cached).
    YahooClient.get_quote_snapshots(["AAPL", "NVDA"], ttl=20)
    assert len(calls) == 1


def test_quote_snapshots_never_raise_when_the_provider_is_down(monkeypatch):
    def boom(url, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("api.market_data_service.http_get", boom)
    monkeypatch.setattr(YahooClient, "_quote_cache", {})
    assert YahooClient.get_quote_snapshots(["AAPL"], ttl=0) == {}


def test_get_stock_data_flags_a_placeholder_change(monkeypatch):
    class FakeTicker:
        def __init__(self, _symbol):
            self.fast_info = {"lastPrice": 209.7, "previousClose": None}
            self.info = {}

        @staticmethod
        def get_history_metadata():
            return {"regularMarketTime": 1785873600, "currency": "USD", "exchangeName": "NMS"}

    monkeypatch.setattr("api.market_data_service.yf.Ticker", FakeTicker)
    monkeypatch.setattr(YahooClient, "_cache", {})

    data = YahooClient.get_stock_data("EA", ttl=0)

    assert data["price"] == 209.7
    assert data["change_pct"] == 0.0            # legacy numeric field is kept
    assert data["change_pct_known"] is False    # ...but flagged as a placeholder
    assert data["market_time"].startswith("2026-08-04")
    assert data["currency"] == "USD"


# --------------------------------------------------------------------------
# /api/live-prices: the badge follows the exchange's last trade, not our fetch
# --------------------------------------------------------------------------

@pytest.fixture
def live_prices_env(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("api.main.DATA_DIR", data_dir)
    monkeypatch.setenv("MS_DISABLE_LIVE_PRICE_REMOTE", "0")
    monkeypatch.setattr("api.actions_service._load_watchlist_payload", lambda: {"stocks": [], "crypto": []})
    monkeypatch.setattr("api.main.load_state_payload", lambda default: {"tickers": {}})
    monkeypatch.setattr("core.live_price_cache.get_live_prices", lambda symbols: {})

    class NoAlpaca:
        @staticmethod
        def is_available():
            return False

        @staticmethod
        def get_market_data(ticker, crypto=False, ttl=5):
            return None

    monkeypatch.setattr("core.alpaca_client.get_alpaca_client", lambda: NoAlpaca())
    return data_dir


def _iso_ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def test_live_prices_label_a_closed_market_quote_closed_not_live(client, monkeypatch, live_prices_env):
    batches = []

    def snapshots(cls, tickers, ttl=20):
        batches.append(list(tickers))
        return {
            "AAPL": {"price": 356.27, "change_pct": 0.39, "market_time": _iso_ago(hours=39),
                     "market_open": False, "currency": "USD", "exchange": "NasdaqGS", "source": "yahoo"},
            "VANTI.PA": {"price": 0.084, "change_pct": 1.94, "market_time": _iso_ago(hours=43),
                         "market_open": False, "currency": "EUR", "exchange": "Paris", "source": "yahoo"},
            "EA": {"price": 209.7, "change_pct": 0.0, "market_time": _iso_ago(days=103),
                   "market_open": False, "currency": "USD", "exchange": "NasdaqGS", "source": "yahoo"},
            "MSFT": {"price": 500.0, "change_pct": 0.5, "market_time": _iso_ago(seconds=20),
                     "market_open": True, "currency": "USD", "exchange": "NasdaqGS", "source": "yahoo"},
        }

    monkeypatch.setattr(YahooClient, "get_quote_snapshots", classmethod(snapshots))
    monkeypatch.setattr(
        YahooClient, "get_stock_data",
        classmethod(lambda cls, ticker, ttl=2: pytest.fail(f"per-symbol yfinance fetch for {ticker}")),
    )

    response = client.get(
        "/api/live-prices",
        params={"symbols": "AAPL,VANTI.PA,EA,MSFT", "remote": "true", "cache_fallback": "true", "max_age_seconds": 45},
    )
    assert response.status_code == 200
    prices = response.json()["prices"]

    assert len(batches) == 1 and sorted(batches[0]) == ["AAPL", "EA", "MSFT", "VANTI.PA"]

    # Friday close read on a Sunday: fetched seconds ago, but NOT live.
    assert prices["AAPL"]["freshness"] == "CLOSED"
    assert prices["AAPL"]["live"] is False
    assert prices["AAPL"]["age_seconds"] < 5
    assert prices["AAPL"]["market_age_seconds"] > 39 * 3600 - 60

    # Paris listing keeps its currency and venue.
    assert prices["VANTI.PA"]["currency"] == "EUR"
    assert prices["VANTI.PA"]["exchange"] == "Paris"
    assert prices["VANTI.PA"]["freshness"] == "CLOSED"

    # Delisted: months-old last trade is STALE and has no day change.
    assert prices["EA"]["freshness"] == "STALE"
    assert prices["EA"]["live"] is False
    assert prices["EA"]["change_pct"] is None

    # A trade from 20 seconds ago is live.
    assert prices["MSFT"]["freshness"] == "LIVE"
    assert prices["MSFT"]["live"] is True

    # The persisted cache keeps the market time, so a cache-only read cannot
    # promote the Friday close back to a plain "recent" quote.
    cached = client.get("/api/live-prices", params={"symbols": "AAPL,EA", "remote": "false"}).json()["prices"]
    assert cached["AAPL"]["freshness"] == "CLOSED"
    assert cached["AAPL"]["market_time"] == prices["AAPL"]["market_time"]
    assert cached["EA"]["freshness"] == "STALE"
    assert cached["EA"]["change_pct"] is None


def test_live_prices_fall_back_per_symbol_and_keep_unknown_change_null(client, monkeypatch, live_prices_env):
    monkeypatch.setattr(YahooClient, "get_quote_snapshots", classmethod(lambda cls, tickers, ttl=20: {}))
    monkeypatch.setattr(
        YahooClient, "get_stock_data",
        classmethod(lambda cls, ticker, ttl=2: {
            "price": 209.7, "change_pct": 0.0, "change_pct_known": False,
            "market_time": _iso_ago(hours=40), "currency": "USD", "exchange": "NMS",
        }),
    )

    prices = client.get(
        "/api/live-prices", params={"symbols": "EA", "remote": "true", "cache_fallback": "true"},
    ).json()["prices"]

    assert prices["EA"]["price"] == 209.7
    assert prices["EA"]["change_pct"] is None, "a placeholder 0.0 must not be served as +0.00%"
    assert prices["EA"]["freshness"] == "CLOSED"


def test_live_prices_delayed_when_the_session_is_open_but_the_trade_is_old(client, monkeypatch, live_prices_env):
    monkeypatch.setattr(
        YahooClient, "get_quote_snapshots",
        classmethod(lambda cls, tickers, ttl=20: {
            "CMBMF": {"price": 0.0002, "change_pct": 0.0, "market_time": _iso_ago(hours=2),
                      "market_open": True, "currency": "USD", "exchange": "OTC Markets OTCPK", "source": "yahoo"},
        }),
    )
    prices = client.get(
        "/api/live-prices", params={"symbols": "CMBMF", "remote": "true", "cache_fallback": "true"},
    ).json()["prices"]
    assert prices["CMBMF"]["freshness"] == "DELAYED"
    assert prices["CMBMF"]["live"] is False


# --------------------------------------------------------------------------
# Add Ticker: only real symbols reach the watchlist
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["<img src=x onerror=alert(1)>", "AA PL", "AAPL'); alert(1);//", "A" * 40])
def test_add_ticker_rejects_non_symbols(client, monkeypatch, bad):
    monkeypatch.setattr("api.main.add_ticker", lambda *a, **k: pytest.fail("invalid symbol reached the watchlist"))
    response = client.post("/api/tickers", json={"ticker": bad, "return_state": False})
    assert response.status_code == 400
    assert "Invalid ticker symbol" in response.json()["detail"]


@pytest.mark.parametrize("good", ["aapl", "BRK.B", "BTC-USD", "GC=F", "^GSPC", "VANTI.PA"])
def test_add_ticker_accepts_real_symbol_shapes(client, monkeypatch, good):
    seen = []
    monkeypatch.setattr(
        "api.main.add_ticker",
        lambda ticker, return_state=True: seen.append(ticker) or {"status": "ok", "ticker": ticker, "added": True},
    )
    monkeypatch.setattr("api.main._agent_enabled", lambda name: False)
    response = client.post("/api/tickers", json={"ticker": good, "return_state": False})
    assert response.status_code == 200
    assert seen == [good.upper()]


# --------------------------------------------------------------------------
# Watchlist rows say when the engine analysed them
# --------------------------------------------------------------------------

def _build_rows(monkeypatch, tickers_state, watch):
    config = SimpleNamespace(
        hot_list=[],
        watchlist={"order": list(watch), "yahoo": list(watch)},
        news_pulse={"ttl_seconds": 45},
        config={},
        get_ticker_settings=lambda _ticker: {},
        get_effective_ticker_settings=lambda _ticker: {},
    )

    class Workspace:
        @staticmethod
        def get_market_data(_ticker):
            return {}

    monkeypatch.setattr(state_service, "get_ticker_workspace_store", lambda: Workspace())
    monkeypatch.setattr(state_service, "build_news_pulse_bulk", lambda *_a, **_k: {})
    monkeypatch.setattr("agents.economic_calendar_agent.get_economic_calendar_agent", lambda _config: object())
    monkeypatch.setattr(
        state_service, "_resolve_earnings_from_sources",
        lambda _calendar, _ticker, _today, allow_remote=True: ("N/A", -1, "", ""),
    )
    rows = state_service.build_tickers_list(
        {"tickers": tickers_state}, {"open": {}, "closed": []}, config, allow_remote=False,
    )
    return {row["ticker"]: row for row in rows}


def test_watchlist_rows_expose_the_analysis_age(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(days=68)).isoformat()
    rows = _build_rows(
        monkeypatch,
        {
            "AAPL": {"price": 316.02, "change_pct": -1.23, "last_update": old,
                     "trend_1d": "BULL", "trend_4h": "BEAR", "trend_1h": "BEAR",
                     "ts_1h": "22:56:48", "rsi": 47.2, "macd_hist": 1.0},
            "CMBMF": {"price": 0.0002, "change_pct": 0.0,
                      "trend_1d": "N/A", "trend_4h": "N/A", "trend_1h": "N/A", "rsi": 50, "macd_hist": 0},
        },
        ["AAPL", "CMBMF"],
    )

    aapl = rows["AAPL"]
    assert aapl["analysis_available"] is True
    assert aapl["analysis_updated_at"] == old
    assert aapl["analysis_age_seconds"] == pytest.approx(68 * 86400, abs=120)

    # No trend on any timeframe = no analysis; the RSI 50 / MACD 0 in the row
    # are placeholders and the row says so.
    nodata = rows["CMBMF"]
    assert nodata["analysis_available"] is False
    assert nodata["analysis_updated_at"] is None
    assert nodata["analysis_age_seconds"] is None
    assert nodata["trend_alignment"] == "UNKNOWN"


# --------------------------------------------------------------------------
# Quick AI: a failed run is not a "neutral / WAIT" verdict
# --------------------------------------------------------------------------

class _FakeOllama:
    def __init__(self, available=True, model="test-model", response=""):
        self._available = available
        self._model = model
        self.response = response
        self.prompts = []

    def check_availability(self):
        return self._available

    def get_best_model(self):
        return self._model

    def generate(self, prompt, model, system="", **kwargs):
        # **kwargs: the quick-AI call now passes a bounded timeout_override
        # (wave 3, ollama.quick_ai_timeout_seconds).
        self.prompts.append(prompt)
        return {"response": self.response}


@pytest.fixture
def clean_quick_ai_cache(monkeypatch):
    monkeypatch.setattr(quick_ai, "_CACHE", {})


CTX = {"price": 100, "change_pct": 1.0, "trend_alignment": "BULL_ALIGN", "earnings_days": 20}


def test_quick_ai_unreadable_answer_is_flagged_not_shown_as_neutral(monkeypatch, clean_quick_ai_cache):
    fake = _FakeOllama(response="I think it looks fine, hard to say.")
    monkeypatch.setattr(quick_ai, "ollama", fake)

    out = quick_ai.run_quick_ai_for_ticker("AAPL", CTX)

    assert out["available"] is False
    assert "could not be read" in out["error"]
    assert out["analysis"] == quick_ai.default_quick_ai_payload()


def test_quick_ai_empty_answer_and_offline_are_flagged(monkeypatch, clean_quick_ai_cache):
    monkeypatch.setattr(quick_ai, "ollama", _FakeOllama(response=""))
    empty = quick_ai.run_quick_ai_for_ticker("AAPL", CTX)
    assert empty["available"] is False and "no answer" in empty["error"]

    monkeypatch.setattr(quick_ai, "_CACHE", {})
    monkeypatch.setattr(quick_ai, "ollama", _FakeOllama(available=False))
    offline = quick_ai.run_quick_ai_for_ticker("AAPL", CTX)
    assert offline["available"] is False and offline["error"] == "ollama unavailable"

    disabled = quick_ai.run_quick_ai_for_ticker("AAPL", CTX, quick_ai_cfg={"enabled": False})
    assert disabled["available"] is False and disabled["error"]


def test_quick_ai_valid_answer_is_available_and_confidence_is_0_to_100(monkeypatch, clean_quick_ai_cache):
    answer = json.dumps({
        "bias": "bullish", "confidence": 0.8, "time_horizon": "1-7d",
        "top_reasons": ["trend up"], "top_risks": ["earnings"], "action": "BUY", "notes": "ok",
    })
    monkeypatch.setattr(quick_ai, "ollama", _FakeOllama(response=answer))

    out = quick_ai.run_quick_ai_for_ticker("AAPL", CTX)

    assert out["available"] is True
    assert "error" not in out
    assert out["analysis"]["confidence"] == 80, "0.8 on a 0-1 scale is 80/100, not 1/100"
    assert quick_ai.parse_quick_ai_output('{"bias":"bearish","confidence":65,"action":"SELL"}')["confidence"] == 65


def test_quick_ai_failure_is_retried_soon_but_success_is_cached(monkeypatch, clean_quick_ai_cache):
    fake = _FakeOllama(response="garbage")
    monkeypatch.setattr(quick_ai, "ollama", fake)
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(quick_ai.time, "time", lambda: clock["now"])

    quick_ai.run_quick_ai_for_ticker("AAPL", CTX)
    clock["now"] += 30
    assert quick_ai.run_quick_ai_for_ticker("AAPL", CTX)["cached"] is True      # within 60 s
    clock["now"] += 60
    fake.response = json.dumps({"bias": "neutral", "confidence": 40, "action": "WAIT", "notes": "flat"})
    retried = quick_ai.run_quick_ai_for_ticker("AAPL", CTX)
    assert retried["cached"] is False and retried["available"] is True          # failure expired
    assert len(fake.prompts) == 2

    clock["now"] += 600
    assert quick_ai.run_quick_ai_for_ticker("AAPL", CTX)["cached"] is True      # success keeps the TTL
    assert len(fake.prompts) == 2


def test_quick_ai_prompt_never_passes_a_stale_trend_as_current():
    fresh = quick_ai._prompt_from_context("AAPL", {**CTX, "analysis_age_seconds": 3600})
    assert "TrendAlignment(1D/4H/1H): BULL_ALIGN" in fresh

    stale = quick_ai._prompt_from_context("AAPL", {**CTX, "analysis_age_seconds": 68 * 86400})
    assert "BULL_ALIGN" not in stale
    assert "68 days old" in stale

    missing = quick_ai._prompt_from_context("AAPL", {**CTX, "analysis_available": False})
    assert "BULL_ALIGN" not in missing
    assert "no technical analysis available" in missing

    assert "integer from 0 to 100" in fresh
