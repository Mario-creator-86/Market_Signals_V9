"""Wave 3 regression tests for the API layer ("api-fixes" zone).

1. /api/live-prices: a stock quote with no exchange trade time is LAST (not
   LIVE) while the US market is closed; crypto is unaffected.
2. Ticker rows forward the engine's session / data-quality facts and carry a
   null-safe `price_display_state`; the heuristic score is not a "probability".
3. Agents tab: last-run outcome fields reach the rows and drive "attention".
4. /api/news/analyze attaches a validated briefing to the in-memory article;
   news routes reject days <= 0; the export labels its times as UTC.
5. A hot-list toggle invalidates every cached response that embeds membership.
6. Add ticker refuses a symbol the provider does not know (unless force).
7. Bulk remove reports `failed: [{ticker, error}]` like the global-settings route.
8. Quick AI context gets `session` / `price_as_of` from the row.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from api import agent_runtime_service as runtime_service
from api import market_data_service
from api import state_service


def _iso_ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


# --------------------------------------------------------------------------
# 1. /api/live-prices: no trade time + closed US session => LAST, not LIVE
# --------------------------------------------------------------------------

@pytest.fixture
def live_prices_env(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("api.main.DATA_DIR", data_dir)
    monkeypatch.setenv("MS_DISABLE_LIVE_PRICE_REMOTE", "1")
    monkeypatch.setattr(
        "api.actions_service._load_watchlist_payload",
        lambda: {"stocks": ["BMNR", "AAPL"], "crypto": ["BTC-USD"]},
    )
    monkeypatch.setattr("api.main.load_state_payload", lambda default: {"tickers": {}})
    now = datetime.now(timezone.utc).isoformat()
    # Quotes verified a second ago by the capital tracker: live=True, and none
    # of them carries the exchange's own trade time.
    monkeypatch.setattr(
        "core.live_price_cache.get_live_prices",
        lambda symbols: {
            s: {"price": 17.56, "change_pct": None, "source": "etoro", "timestamp": now}
            for s in symbols if s in {"BMNR", "BTC-USD"}
        },
    )
    return data_dir


def test_live_prices_without_trade_time_are_last_when_the_us_market_is_closed(client, monkeypatch, live_prices_env):
    monkeypatch.setenv("MS_MARKET_HOURS_OVERRIDE", "closed")
    # AAPL comes from the persisted cache WITH a trade time: that path is unchanged.
    (live_prices_env / "live_prices_cache.json").write_text(json.dumps({
        "ts": time.time(),
        "prices": {"AAPL": {"price": 356.27, "change_pct": 0.39, "source": "yahoo",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "market_time": _iso_ago(hours=39), "market_open": False}},
    }), encoding="utf-8")

    prices = client.get("/api/live-prices", params={"symbols": "BMNR,BTC-USD,AAPL"}).json()["prices"]

    assert prices["BMNR"]["freshness"] == "LAST"
    assert prices["BMNR"]["live"] is False
    assert prices["BMNR"]["market_age_seconds"] is None
    assert prices["BMNR"]["age_seconds"] < 30          # field names are kept
    # Crypto trades around the clock: unaffected.
    assert prices["BTC-USD"]["freshness"] == "LIVE"
    assert prices["BTC-USD"]["live"] is True
    # A quote with a trade time keeps the existing CLOSED verdict.
    assert prices["AAPL"]["freshness"] == "CLOSED"


def test_live_prices_without_trade_time_stay_live_inside_the_session(client, monkeypatch, live_prices_env):
    monkeypatch.setenv("MS_MARKET_HOURS_OVERRIDE", "open")
    prices = client.get("/api/live-prices", params={"symbols": "BMNR"}).json()["prices"]
    assert prices["BMNR"]["freshness"] == "LIVE"
    assert prices["BMNR"]["live"] is True


def test_live_prices_cached_state_quote_is_last_when_closed(client, monkeypatch, live_prices_env):
    monkeypatch.setenv("MS_MARKET_HOURS_OVERRIDE", "closed")
    monkeypatch.setattr("core.live_price_cache.get_live_prices", lambda symbols: {})
    monkeypatch.setattr(
        "api.main.load_state_payload",
        lambda default: {"tickers": {
            "BMNR": {"price": 17.5, "change_pct": 1.0, "last_update": datetime.now(timezone.utc).isoformat()},
            "OLD": {"price": 9.0, "change_pct": 0.0, "last_update": _iso_ago(days=3)},
        }},
    )
    prices = client.get("/api/live-prices", params={"symbols": "BMNR,OLD"}).json()["prices"]
    assert prices["BMNR"]["freshness"] == "LAST"      # was CACHED
    assert prices["OLD"]["freshness"] == "STALE"      # an old quote stays STALE


# --------------------------------------------------------------------------
# 2. Ticker rows: session / data-quality fields and price_display_state
# --------------------------------------------------------------------------

def _build_rows(monkeypatch, tickers_state, watch, workspace_rows=None):
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
        def get_market_data(ticker):
            return dict((workspace_rows or {}).get(ticker) or {})

    monkeypatch.setattr(state_service, "get_ticker_workspace_store", lambda: Workspace())
    monkeypatch.setattr(state_service, "build_news_pulse_bulk", lambda *_a, **_k: {})
    monkeypatch.setattr("agents.economic_calendar_agent.get_economic_calendar_agent", lambda _config: object())
    monkeypatch.setattr(
        state_service, "_resolve_earnings_from_sources",
        lambda _calendar, _ticker, _today, allow_remote=True: ("N/A", -1, "", ""),
    )
    monkeypatch.setattr(state_service, "DATA_DIR", state_service.DATA_DIR / "wave3_api_missing_dir")
    rows = state_service.build_tickers_list(
        {"tickers": tickers_state}, {"open": {}, "closed": []}, config, allow_remote=False,
    )
    return {row["ticker"]: row for row in rows}


_CLOSED_ROW = {
    "price": 356.27, "change_pct": 0.39, "data_quality": "market_closed",
    "session": "closed", "session_label": "Market closed (weekend)",
    "next_regular_open_utc": "2026-11-16T14:30:00+00:00", "price_as_of": "2026-11-13",
    "trend_1d": "BULL", "trend_4h": "BULL", "trend_1h": "NEUTRAL",
    "rsi": 61.2, "rsi_timeframe": "1h", "macd_hist": 0.4,
}


def test_ticker_rows_forward_the_engine_session_fields(monkeypatch):
    rows = _build_rows(
        monkeypatch,
        {
            "AAPL": {**_CLOSED_ROW, "last_update": datetime.now(timezone.utc).isoformat()},
            "MSFT": {"price": 500.0, "change_pct": 0.5, "data_quality": "verified",
                     "trend_1d": "BULL", "trend_4h": "BULL", "trend_1h": "BULL", "rsi": 55.0, "macd_hist": 0.1},
        },
        ["AAPL", "MSFT"],
    )
    aapl = rows["AAPL"]
    assert aapl["session"] == "closed"
    assert aapl["session_label"] == "Market closed (weekend)"
    assert aapl["price_as_of"] == "2026-11-13"
    assert aapl["rsi_timeframe"] == "1h"
    assert aapl["data_quality"] == "market_closed"
    assert aapl["price_available"] is True
    assert aapl["next_regular_open_utc"] == "2026-11-16T14:30:00+00:00"
    assert aapl["price_display_state"] == "last_close"
    assert aapl["price"] == 356.27

    msft = rows["MSFT"]
    assert msft["price_display_state"] == "ok"
    assert msft["data_quality"] == "verified"
    assert msft["session"] is None and msft["price_as_of"] is None


def test_no_data_and_inactive_rows_get_a_null_safe_display_state(monkeypatch):
    rows = _build_rows(
        monkeypatch,
        {
            # The engine's "no data from any provider" row: price 0.0.
            "CPRX": {"price": 0.0, "price_available": False, "data_quality": "no_data",
                     "session": None, "price_as_of": None, "change_pct": 0.0,
                     "trend_1d": "N/A", "trend_4h": "N/A", "trend_1h": "N/A", "rsi": None, "macd_hist": None},
            # Delisted: last known price, dated.
            "SLP": {"price": 18.5, "data_quality": "listing_inactive", "session": "inactive",
                    "price_as_of": "2026-10-05", "change_pct": 0.0,
                    "trend_1d": "N/A", "trend_4h": "N/A", "trend_1h": "N/A", "rsi": None, "macd_hist": None},
        },
        ["CPRX", "SLP", "NEWB"],
    )
    cprx = rows["CPRX"]
    # The fallback loop replaces the price-less row with a placeholder; the
    # engine's verdict must survive it.
    assert cprx["data_quality"] == "no_data"
    assert cprx["price_display_state"] == "no_data"
    assert cprx["price_available"] is False
    # `price` stays numeric (0 = no price): other code compares it with <= 0.
    assert cprx["price"] == 0 and isinstance(cprx["price"], (int, float))

    slp = rows["SLP"]
    assert slp["data_quality"] == "listing_inactive"
    assert slp["price_display_state"] == "listing_inactive"
    assert slp["price_as_of"] == "2026-10-05"
    assert slp["price_available"] is True

    # A watchlist ticker the engine has no row for at all.
    newb = rows["NEWB"]
    assert newb["price_display_state"] == "unavailable"
    assert newb["data_quality"] is None and newb["price_available"] is False


def test_signal_text_calls_the_heuristic_a_setup_score_not_a_probability():
    spans = state_service.format_signal_spans({
        "id": "s1", "ticker": "AAPL", "action": "BUY", "variant": "A", "entry": 100.0, "tp": 104.0, "sl": 98.0,
        "probability_score": 0.62, "market_regime": "TREND",
        "strict_backed": True, "strict_variant": "A", "strict_signal_mode": "PULLBACK",
        "strict_signal_score": 87, "strict_probability_score": 91, "strict_mtf_alignment": "3/3",
    })
    text = "".join(span["text"] for span in spans)
    assert "Setup score 62/100 (not a probability)" in text
    assert "setup score 91/100 (not a probability)" in text
    assert "Prob" not in text

    assert state_service._setup_score_text(None) == "setup score n/a"
    assert state_service._setup_score_text(0) == "setup score n/a"
    assert state_service._setup_score_text(1) == "setup score 100/100 (not a probability)"


# --------------------------------------------------------------------------
# 3. Agents tab: last-run outcome fields and "needs attention"
# --------------------------------------------------------------------------

_OUTCOME_FIELDS = (
    "last_run_ok", "last_run_error", "last_run_error_type",
    "last_run_error_at", "consecutive_failures", "last_run_finished_at",
)


def test_engine_heartbeat_forwards_the_run_outcome(monkeypatch):
    assert set(_OUTCOME_FIELDS) <= set(runtime_service._ENGINE_HEARTBEAT_DETAIL_FIELDS)
    failed_at = _iso_ago(minutes=2)
    records = {
        "scanner": {"running": True, "last_heartbeat": _iso_ago(seconds=20), "last_success": _iso_ago(hours=1),
                    "last_run_ok": False, "last_run_error": "Yahoo screener returned HTTP 503",
                    "last_run_error_type": "RuntimeError", "last_run_error_at": failed_at,
                    "consecutive_failures": 3, "last_run_finished_at": failed_at},
        # A successful run clears the error with empty strings / zero.
        "sentiment": {"running": True, "last_heartbeat": _iso_ago(seconds=20),
                      "last_run_ok": True, "last_run_error": "", "last_run_error_type": "",
                      "last_run_error_at": "", "consecutive_failures": 0, "last_run_finished_at": _iso_ago(seconds=30)},
    }
    monkeypatch.setattr(runtime_service, "_engine_agent_heartbeats", lambda: records)

    scanner = runtime_service._engine_heartbeat_details("scanner")
    assert scanner["last_run_ok"] is False
    assert scanner["last_run_error"] == "Yahoo screener returned HTTP 503"
    assert scanner["last_run_error_type"] == "RuntimeError"
    assert scanner["last_run_error_at"] == failed_at
    assert scanner["consecutive_failures"] == 3

    sentiment = runtime_service._engine_heartbeat_details("sentiment")
    assert sentiment["last_run_ok"] is True
    assert sentiment["last_run_error"] == "" and sentiment["consecutive_failures"] == 0

    # Through the snapshot: a stale API-process error must not survive the
    # engine's "last run ok", and the failing worker needs attention.
    config = {"agents": {"scanner": {"enabled": True}, "sentiment": {"enabled": True}}}
    result = runtime_service.snapshot_agent_runtimes(
        config=config,
        observed_status={"sentiment": {"last_run_ok": False, "last_run_error": "old API-side error",
                                       "consecutive_failures": 5}},
        engine_running=True,
        agent_names=["scanner", "sentiment"],
    )
    assert result["scanner"]["health"] == "attention"
    assert "3 runs failed in a row" in result["scanner"]["health_reason"]
    assert "HTTP 503" in result["scanner"]["health_reason"]
    assert result["sentiment"]["health"] == "ok"
    assert result["sentiment"]["last_run_error"] == ""
    assert result["sentiment"]["consecutive_failures"] == 0


def _running(**extra):
    return {"enabled": True, "running": True, "last_success": _iso_ago(minutes=30), **extra}


def test_health_needs_attention_on_repeated_or_newer_failures():
    two = runtime_service.assess_agent_health(_running(
        last_run_ok=False, consecutive_failures=2, last_run_error="boom", last_run_error_at=_iso_ago(minutes=1)))
    assert two["health"] == "attention" and two["health_reason"] == "2 runs failed in a row: boom"

    # One failure, newer than the last success.
    newer = runtime_service.assess_agent_health(_running(
        last_run_ok=False, consecutive_failures=1, last_run_error="timeout", last_run_error_at=_iso_ago(minutes=5)))
    assert newer["health"] == "attention" and newer["health_reason"] == "last run failed: timeout"

    # One failure and no success ever recorded.
    never = runtime_service.assess_agent_health({
        "enabled": True, "running": True, "last_run_ok": False, "consecutive_failures": 1,
        "last_run_error": "", "last_run_error_type": "KeyError", "last_run_error_at": _iso_ago(minutes=5)})
    assert never["health"] == "attention" and never["health_reason"] == "last run failed: KeyError"

    # A success after the failure: healthy again.
    recovered = runtime_service.assess_agent_health(_running(
        last_run_ok=True, consecutive_failures=0, last_run_error="", last_run_error_at=""))
    assert recovered["health"] == "ok" and recovered["health_reason"] == ""

    # A failure older than the last success does not flag the agent.
    older = runtime_service.assess_agent_health(_running(
        last_run_ok=False, consecutive_failures=1, last_run_error="x", last_run_error_at=_iso_ago(hours=3)))
    assert older["health"] == "ok"

    # No tracked run at all: nothing is invented.
    untouched = runtime_service.assess_agent_health(_running())
    assert untouched["health"] == "ok"

    # A disabled agent stays "disabled" whatever its last run said.
    disabled = runtime_service.assess_agent_health({
        "enabled": False, "running": False, "last_run_ok": False, "consecutive_failures": 9})
    assert disabled["health"] == "disabled"


def test_control_plane_agent_rows_carry_the_outcome_fields():
    from api.agents_service import build_control_plane_payload

    payload = build_control_plane_payload(
        config=SimpleNamespace(agents={}, config={}, news={}, strategy_v2={}, bot={}),
        bot_status={"running": False},
        agents_status={
            "scanner": {"enabled": True, "running": True, "last_run_ok": False, "consecutive_failures": "2",
                        "last_run_error": " feed down ", "last_run_error_at": "2026-11-15T10:00:00+00:00"},
            "news_tab": {"enabled": True, "running": True},
        },
        engine_running=False,
    )
    scanner = payload["agents"]["scanner"]
    assert scanner["last_run_ok"] is False
    assert scanner["consecutive_failures"] == 2
    assert scanner["last_run_error"] == "feed down"
    news = payload["agents"]["news_tab"]
    for field in _OUTCOME_FIELDS:
        assert field in news
    assert news["last_run_ok"] is None          # no tracked run yet: not invented
    assert news["consecutive_failures"] == 0 and news["last_run_error"] == ""


def test_control_plane_route_serves_the_outcome_fields(client):
    response = client.get("/api/agents/control-plane")
    assert response.status_code == 200
    agents = response.json()["agents"]
    assert agents
    for row in agents.values():
        for field in _OUTCOME_FIELDS:
            assert field in row


# --------------------------------------------------------------------------
# 4. News: briefing attached to the in-memory article, days validation, UTC
# --------------------------------------------------------------------------

_ARTICLE = {
    "id": "art-1", "url": "https://example.com/a1", "title": "Apple beats on earnings",
    "source": "Reuters", "published_at": "2026-11-15T09:00:00+00:00", "ticker": "AAPL",
    "summary": "Apple reported record revenue.",
}

_MODEL_JSON = json.dumps({
    "briefing_version": 3, "asset": "AAPL",
    "article_explainer": "Apple reported record quarterly revenue.",
    "market_implication": "Supports large-cap tech sentiment.",
    "market_direction": "risk-on",
    "ticker_implication": {"ticker": "AAPL", "has_direct_link": True, "relationship": "Own results",
                           "direction": "up", "rationale": "Beat on revenue", "confidence": 70},
    "overall_sentiment": "bullish", "confidence": 70, "impact_level": "high",
    "summary": "Apple beat expectations.", "overall_summary": "Apple beat expectations.",
})


class _NewsAgentBase:
    _news_cfg = {"ai_briefing_timeout_seconds": 30}

    def get_article_details(self, article_id):
        return dict(_ARTICLE) if article_id == "art-1" else None

    def infer_briefing_tickers(self, article):
        return ["AAPL"]

    def _normalize_micro_summary(self, parsed):
        return dict(parsed)


def _wire_news_analysis(monkeypatch, agent, response_text=_MODEL_JSON):
    from core.analysis_store import analysis_store
    from core.ollama_client import ollama

    saved = []
    monkeypatch.setattr("agents.news_tab_agent.get_news_tab_agent", lambda: agent)
    monkeypatch.setattr("core.content_fetcher.fetch_article_content", lambda url, max_length=7000: "Apple text " * 20)
    monkeypatch.setattr(ollama, "check_availability", lambda: True)
    monkeypatch.setattr(ollama, "get_best_model", lambda: "test-model")
    monkeypatch.setattr(ollama, "generate", lambda prompt, **kwargs: {"response": response_text, "model": "test-model"})
    monkeypatch.setattr(analysis_store, "get_article_analysis", lambda *a, **k: None)
    monkeypatch.setattr(analysis_store, "get_analysis", lambda *a, **k: None)
    monkeypatch.setattr(analysis_store, "save_article_analysis", lambda **kwargs: saved.append(kwargs))
    monkeypatch.setattr(analysis_store, "save_analysis", lambda *a, **k: saved.append(a))
    return saved


def test_news_analyze_attaches_the_briefing_to_the_in_memory_article(client, monkeypatch):
    import api.main as main

    class Agent(_NewsAgentBase):
        def __init__(self):
            self.attached = []

        def attach_briefing(self, article_id, analysis, analysis_type="deep", model=""):
            self.attached.append((article_id, analysis, analysis_type, model))
            return True

    agent = Agent()
    saved = _wire_news_analysis(monkeypatch, agent)
    with main._NEWS_TAB_STATE_CACHE_LOCK:
        main._NEWS_TAB_STATE_CACHE.update({"key": "k", "ts": time.time(), "data": {"x": 1}, "payload": b"{}"})

    response = client.post("/api/news/analyze", json={"article_id": "art-1", "prompt_type": "deep"})

    assert response.status_code == 200
    body = response.json()
    assert body["cached"] is False and body["attached"] is True
    assert len(saved) == 1
    assert len(agent.attached) == 1
    article_id, analysis, analysis_type, model = agent.attached[0]
    assert article_id == "art-1" and analysis_type == "deep" and model == "test-model"
    assert analysis["article_explainer"] == "Apple reported record quarterly revenue."
    assert analysis == body["analysis"]
    # The cached news-tab state is dropped so the next poll carries the briefing.
    assert main._NEWS_TAB_STATE_CACHE["payload"] is None


def test_news_analyze_uses_the_alternate_method_name_and_its_signature(client, monkeypatch):
    class Agent(_NewsAgentBase):
        def __init__(self):
            self.calls = []

        def store_article_briefing(self, article_id, analysis):      # no keyword extras
            self.calls.append((article_id, analysis))

    agent = Agent()
    _wire_news_analysis(monkeypatch, agent)
    response = client.post("/api/news/analyze", json={"article_id": "art-1", "prompt_type": "deep"})
    assert response.status_code == 200 and response.json()["attached"] is True
    assert [call[0] for call in agent.calls] == ["art-1"]


def test_news_analyze_survives_an_agent_without_the_method_or_a_failing_one(client, monkeypatch):
    _wire_news_analysis(monkeypatch, _NewsAgentBase())
    plain = client.post("/api/news/analyze", json={"article_id": "art-1", "prompt_type": "deep"})
    assert plain.status_code == 200 and plain.json()["attached"] is False

    class Broken(_NewsAgentBase):
        def attach_briefing(self, article_id, analysis, **kwargs):
            raise RuntimeError("article store locked")

    _wire_news_analysis(monkeypatch, Broken())
    broken = client.post("/api/news/analyze", json={"article_id": "art-1", "prompt_type": "deep"})
    assert broken.status_code == 200 and broken.json()["attached"] is False
    assert broken.json()["analysis"]["article_explainer"]


def test_news_analyze_does_not_attach_an_invalid_model_answer(client, monkeypatch):
    class Agent(_NewsAgentBase):
        def __init__(self):
            self.attached = []

        def attach_briefing(self, article_id, analysis, **kwargs):
            self.attached.append(article_id)

    agent = Agent()
    saved = _wire_news_analysis(monkeypatch, agent, response_text="I cannot help with that.")
    response = client.post("/api/news/analyze", json={"article_id": "art-1", "prompt_type": "deep"})
    assert response.status_code == 502
    assert agent.attached == [] and saved == []


@pytest.mark.parametrize("days", [0, -3])
def test_news_export_rejects_non_positive_days(client, monkeypatch, days):
    monkeypatch.setattr(
        "agents.news_tab_agent.get_news_tab_agent",
        lambda: pytest.fail("the export must be refused before the agent is touched"),
    )
    response = client.get("/api/news-tab/export", params={"days": days})
    assert response.status_code == 400
    assert "days must be at least 1" in response.json()["detail"]


@pytest.mark.parametrize("path", ["/api/news-tab/history", "/api/news-tab/history/start"])
def test_news_history_rejects_non_positive_days(client, monkeypatch, path):
    monkeypatch.setattr(
        "agents.news_tab_agent.get_news_tab_agent",
        lambda: pytest.fail("the fetch must be refused before the agent is touched"),
    )
    response = client.post(path, json={"ticker": "AAPL", "days": 0})
    assert response.status_code == 400
    assert "days must be at least 1" in response.json()["detail"]


def test_news_export_labels_its_times_as_utc(client, monkeypatch):
    published = datetime.now(timezone.utc) - timedelta(hours=3)

    class Agent:
        def get_all_articles_flat(self):
            return [{"id": "a1", "title": "Apple beats", "url": "https://example.com/a1", "source": "Reuters",
                     # Published with a +02:00 offset: the export prints UTC.
                     "published_at": published.astimezone(timezone(timedelta(hours=2))).isoformat(),
                     "ticker": "AAPL", "impact": "high"}]

    monkeypatch.setattr("agents.news_tab_agent.get_news_tab_agent", lambda: Agent())
    monkeypatch.setattr("api.main._agent_enabled", lambda name, *a, **k: True)
    response = client.get("/api/news-tab/export", params={"days": 365})
    assert response.status_code == 200
    text = response.text
    assert "All times are UTC." in text
    generated = next(line for line in text.splitlines() if line.startswith("Generated:"))
    assert generated.endswith(" UTC")
    assert f"[{published.strftime('%Y-%m-%d %H:%M')} UTC] [AAPL] [HIGH]" in text


# --------------------------------------------------------------------------
# 5. Hot-list toggle invalidates every cache that embeds membership
# --------------------------------------------------------------------------

@pytest.fixture
def hot_config(monkeypatch):
    membership = []

    class Config:
        hot_list = membership

        def toggle_hot_list(self, ticker):
            if ticker in membership:
                membership.remove(ticker)
                return False
            membership.append(ticker)
            return True

    monkeypatch.setattr("api.state_service.get_shared_config", lambda: Config())
    return membership


def test_hot_list_toggle_clears_state_and_hot_list_caches(client, monkeypatch, hot_config):
    import api.main as main
    from api import hotlist_service

    heat_refreshes = []
    monkeypatch.setattr(main, "IS_TEST_MODE", False)   # the refresh kick is skipped in test mode
    monkeypatch.setattr(hotlist_service, "build_heat_list", lambda: heat_refreshes.append(1) or {})
    monkeypatch.setitem(hotlist_service._heat_cache, "payload", {"curated": [], "heat": []})
    monkeypatch.setitem(hotlist_service._heat_cache, "at", time.monotonic())

    for key in ("api_state:True:True:False:False::False", "api_state:False:False:False:True::False",
                "hotlist_state", "watchlist_state", "scanner_state"):
        main._endpoint_payload_set(key, b'{"stale": true}')
    epoch_before = main._hotlist_membership_epoch()

    response = client.post("/api/watchlist/hotlist/toggle", json={"ticker": "AAPL"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "ticker": "AAPL", "is_in": True}
    assert main._hotlist_membership_epoch() == epoch_before + 1
    for key in ("api_state:True:True:False:False::False", "api_state:False:False:False:True::False",
                "hotlist_state", "watchlist_state"):
        assert main._endpoint_payload_get_stale(key) == (None, None), key
    # Unrelated caches are left alone.
    assert main._endpoint_payload_get_stale("scanner_state")[0] is not None
    main._endpoint_payload_clear("scanner_state")
    # The heat snapshot is aged out and its refresh started.
    assert hotlist_service._heat_cache["at"] == 0.0
    deadline = time.time() + 5
    while not heat_refreshes and time.time() < deadline:
        time.sleep(0.01)
    assert heat_refreshes == [1]


def test_hot_list_state_reflects_a_toggle_immediately(client, monkeypatch, hot_config):
    import api.main as main

    monkeypatch.setattr(
        "core.hotlist_scoring.get_hotlist_state",
        lambda: {"hotlist": [{"ticker": t} for t in hot_config], "hot_list": [{"ticker": t} for t in hot_config],
                 "last_update": None},
    )
    main._invalidate_hotlist_caches()

    assert client.get("/api/hotlist/state").json()["hot_list"] == []
    # Both the 10 s response cache and the 5 s single-flight result hold [].
    assert client.get("/api/hotlist/state").json()["hot_list"] == []

    assert client.post("/api/watchlist/hotlist/toggle", json={"ticker": "NVDA"}).json()["is_in"] is True
    assert client.get("/api/hotlist/state").json()["hot_list"] == [{"ticker": "NVDA"}]

    assert client.post("/api/watchlist/hotlist/toggle", json={"ticker": "NVDA"}).json()["is_in"] is False
    assert client.get("/api/hotlist/state").json()["hot_list"] == []
    main._invalidate_hotlist_caches()


def test_a_state_rebuild_that_started_before_a_toggle_is_not_cached(monkeypatch):
    import api.main as main

    cache_key = "api_state:wave3:inflight"
    main._endpoint_payload_clear(cache_key)
    params = {
        "cache_key": cache_key, "include_flags": set(), "include_news": False, "include_hotlist": True,
        "hotlist_details": False, "lean": False, "state_live_prices": False, "capital_live_prices": False,
    }

    def build_during_a_toggle(**_kwargs):
        main._invalidate_hotlist_caches()          # membership changes mid-build
        return {"hot_list": [{"ticker": "OLD"}]}

    monkeypatch.setattr(main, "get_state", build_during_a_toggle)
    main._build_and_cache_state_payload(params)
    assert main._endpoint_payload_get_stale(cache_key) == (None, None)

    # Without a concurrent change the rebuild is cached as before.
    monkeypatch.setattr(main, "get_state", lambda **_kwargs: {"hot_list": [{"ticker": "NEW"}]})
    main._build_and_cache_state_payload(params)
    cached, _age = main._endpoint_payload_get_stale(cache_key)
    assert json.loads(cached)["hot_list"] == [{"ticker": "NEW"}]
    main._endpoint_payload_clear(cache_key)


def test_removing_tickers_also_invalidates_the_hot_list_caches(client, monkeypatch):
    import api.main as main

    monkeypatch.setattr("api.main.remove_tickers", lambda tickers, return_state=True: {
        "status": "ok", "removed": list(tickers), "watchlist": {}})
    monkeypatch.setattr("agents.economic_calendar_agent.get_economic_calendar_agent",
                        lambda *a, **k: SimpleNamespace(refresh_data=lambda: None))
    main._endpoint_payload_set("hotlist_state", b"{}")
    response = client.request("DELETE", "/api/tickers", json={"tickers": ["AAPL"], "return_state": False})
    assert response.status_code == 200
    assert main._endpoint_payload_get_stale("hotlist_state") == (None, None)


# --------------------------------------------------------------------------
# 6. Add ticker: existence check
# --------------------------------------------------------------------------

@pytest.fixture
def add_env(monkeypatch):
    added = []
    monkeypatch.setenv("MS_ADD_TICKER_VERIFY", "1")
    monkeypatch.setattr(
        "api.main.add_ticker",
        lambda ticker, return_state=True: added.append(ticker) or {"status": "ok", "ticker": ticker, "added": True},
    )
    monkeypatch.setattr("api.main._agent_enabled", lambda name, *a, **k: False)
    return added


def _verdict(monkeypatch, status, **extra):
    calls = []

    def fake(ticker, timeout_seconds=5.0):
        calls.append((ticker, timeout_seconds))
        return {"status": status, "ticker": ticker, "source": "test", "reason": extra.pop("reason", ""), **extra}

    monkeypatch.setattr(market_data_service, "verify_symbol_exists", fake)
    return calls


def test_add_ticker_refuses_a_symbol_without_a_quote(client, monkeypatch, add_env):
    calls = _verdict(monkeypatch, "not_found")
    response = client.post("/api/tickers", json={"ticker": "xyzq", "return_state": False})
    assert response.status_code == 422
    body = response.json()
    assert body["detail"] == "No quote found for XYZQ — check the symbol"
    assert body["error_code"] == "symbol_not_found" and body["can_force"] is True
    assert body["code"] == "no_quote" and body["requires_force"] is True and body["quote_found"] is False
    assert body["added"] is False and body["verified"] is False
    assert add_env == [] and calls == [("XYZQ", 5.0)]


def test_add_ticker_refuses_a_delisted_symbol_with_its_last_trade_date(client, monkeypatch, add_env):
    _verdict(monkeypatch, "inactive", last_trade="2026-10-05T20:00:01+00:00")
    response = client.post("/api/tickers", json={"ticker": "SLP", "return_state": False})
    assert response.status_code == 422
    body = response.json()
    assert body["detail"] == "No live quote for SLP — last trade 2026-10-05, it looks delisted or suspended"
    assert body["code"] == "no_quote" and body["requires_force"] is True and body["verify_status"] == "inactive"
    assert add_env == []


def test_add_ticker_force_skips_the_check_and_adds(client, monkeypatch, add_env):
    calls = _verdict(monkeypatch, "not_found")
    response = client.post("/api/tickers", json={"ticker": "XYZQ", "return_state": False, "force": True})
    assert response.status_code == 200
    body = response.json()
    assert body["added"] is True and body["verified"] is False and body["verify_status"] == "forced"
    assert add_env == ["XYZQ"] and calls == []


def test_add_ticker_is_not_blocked_by_a_network_failure(client, monkeypatch, add_env):
    _verdict(monkeypatch, "unverified", reason="data provider not reachable")
    response = client.post("/api/tickers", json={"ticker": "AAPL", "return_state": False})
    assert response.status_code == 200
    body = response.json()
    assert body["added"] is True and body["verified"] is False
    assert body["verify_status"] == "unverified" and body["verify_note"] == "data provider not reachable"
    assert add_env == ["AAPL"]

    # A lookup that raises is treated the same way.
    monkeypatch.setattr(market_data_service, "verify_symbol_exists",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    crashed = client.post("/api/tickers", json={"ticker": "MSFT", "return_state": False})
    assert crashed.status_code == 200 and crashed.json()["verified"] is False


def test_add_ticker_reports_a_verified_symbol(client, monkeypatch, add_env):
    _verdict(monkeypatch, "found")
    body = client.post("/api/tickers", json={"ticker": "AAPL", "return_state": False}).json()
    assert body["added"] is True and body["verified"] is True and body["verify_status"] == "found"
    assert "verify_note" not in body


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.content = b"x"

    def json(self):
        return self._payload


def _spark_ok(symbol, market_epoch):
    return _Resp(200, {"spark": {"result": [{"symbol": symbol, "response": [{
        "meta": {"currency": "USD", "regularMarketPrice": 18.5, "regularMarketTime": market_epoch,
                 "previousClose": 18.0},
        "timestamp": [], "indicators": {"quote": [{"close": []}]},
    }]}]}})


@pytest.fixture
def clean_symbol_caches(monkeypatch):
    monkeypatch.setattr(market_data_service.YahooClient, "_quote_cache", {})
    monkeypatch.setattr("core.stock_data_client.is_known_missing", lambda symbol: False)


def test_verify_symbol_exists_classifies_provider_answers(monkeypatch, clean_symbol_caches):
    answers = {}
    monkeypatch.setattr(market_data_service, "http_get", lambda url, **kwargs: answers["next"](kwargs))

    answers["next"] = lambda kwargs: _Resp(404, {"spark": {"result": None, "error": {
        "code": "Not Found", "description": "No data found for spark symbols"}}})
    assert market_data_service.verify_symbol_exists("ZZZZQ")["status"] == "not_found"

    answers["next"] = lambda kwargs: _spark_ok("AAPL", time.time() - 3600)
    found = market_data_service.verify_symbol_exists("AAPL")
    assert found["status"] == "found" and found["source"] == "yahoo"
    # Second lookup is served by the quote cache (no request).
    answers["next"] = lambda kwargs: pytest.fail("cached symbol must not be fetched again")
    assert market_data_service.verify_symbol_exists("AAPL")["source"] == "yahoo_quote_cache"

    answers["next"] = lambda kwargs: _spark_ok("SLP", time.time() - 41 * 86400)
    inactive = market_data_service.verify_symbol_exists("SLP")
    assert inactive["status"] == "inactive" and inactive["last_trade"]

    # 200 with no entry for the symbol: the provider has no quote for it.
    answers["next"] = lambda kwargs: _Resp(200, {"spark": {"result": []}})
    assert market_data_service.verify_symbol_exists("NOPE")["status"] == "not_found"


def test_verify_symbol_exists_never_reads_a_network_failure_as_unknown(monkeypatch, clean_symbol_caches):
    outcomes = [
        lambda url, **kwargs: None,                                            # circuit open / no response
        lambda url, **kwargs: _Resp(429, {}),                                  # throttled
        lambda url, **kwargs: _Resp(503, {}),
        lambda url, **kwargs: _Resp(404, {}),                                  # 404 without the provider's "Not Found"
        lambda url, **kwargs: (_ for _ in ()).throw(ConnectionError("down")),
    ]
    for index, outcome in enumerate(outcomes):
        monkeypatch.setattr(market_data_service, "http_get", outcome)
        assert market_data_service.verify_symbol_exists(f"NET{index}")["status"] == "unverified", index

    def slow(url, **kwargs):
        time.sleep(3)
        return _spark_ok("SLOW", time.time())

    monkeypatch.setattr(market_data_service, "http_get", slow)
    started = time.time()
    timed_out = market_data_service.verify_symbol_exists("SLOW", timeout_seconds=1)
    assert timed_out["status"] == "unverified" and "timed out" in timed_out["reason"]
    assert time.time() - started < 2.5


def test_verify_symbol_exists_uses_the_known_missing_list_and_crypto_venues(monkeypatch, clean_symbol_caches):
    monkeypatch.setattr("core.stock_data_client.is_known_missing", lambda symbol: symbol == "GONE")
    monkeypatch.setattr(market_data_service, "http_get", lambda url, **kwargs: pytest.fail("no request expected"))
    assert market_data_service.verify_symbol_exists("GONE")["status"] == "not_found"

    binance, kucoin = market_data_service.BinanceClient, market_data_service.KuCoinClient
    monkeypatch.setattr(binance, "get_last_price", classmethod(lambda cls, ticker, ttl=2: 97000.0))
    assert market_data_service.verify_symbol_exists("BTC-USD") == {
        "status": "found", "ticker": "BTC-USD", "source": "binance", "reason": ""}

    # Both venues answer definitively that the pair does not exist.
    monkeypatch.setattr(binance, "get_last_price", classmethod(lambda cls, ticker, ttl=2: None))
    monkeypatch.setattr(binance, "_is_known_unsupported", classmethod(lambda cls, symbol: True))
    monkeypatch.setattr(kucoin, "get_last_price", classmethod(lambda cls, ticker, ttl=2: 0.0))
    assert market_data_service.verify_symbol_exists("NOPECOIN-USD")["status"] == "not_found"

    # Venues unreachable: not a verdict.
    monkeypatch.setattr(binance, "_is_known_unsupported", classmethod(lambda cls, symbol: False))
    monkeypatch.setattr(kucoin, "get_last_price", classmethod(lambda cls, ticker, ttl=2: None))
    assert market_data_service.verify_symbol_exists("NOPECOIN-USD")["status"] == "unverified"


# --------------------------------------------------------------------------
# 7. Bulk remove: the same partial-failure shape as global settings
# --------------------------------------------------------------------------

@pytest.fixture
def remove_env(monkeypatch):
    present = {"AAPL", "MSFT"}

    def fake_remove(tickers, return_state=True):
        removed = [t for t in tickers if t in present]
        return {"status": "ok", "removed": removed, "watchlist": {"order": sorted(present - set(removed))}}

    monkeypatch.setattr("api.main.remove_tickers", fake_remove)
    monkeypatch.setattr("agents.economic_calendar_agent.get_economic_calendar_agent",
                        lambda *a, **k: SimpleNamespace(refresh_data=lambda: None))


def test_bulk_remove_reports_failed_tickers_in_the_shared_shape(client, remove_env):
    partial = client.request("DELETE", "/api/tickers", json={"tickers": ["AAPL", "ZZZZ"], "return_state": False})
    assert partial.status_code == 207
    body = partial.json()
    assert body["status"] == "partial" and body["ok"] is False
    assert body["removed"] == ["AAPL"]
    assert body["failed"] == [{"ticker": "ZZZZ", "error": "not_in_watchlist"}]
    assert body["tickers_removed"] == 1 and body["total_tickers"] == 2
    assert "ZZZZ" in body["detail"] and "1 of 2" in body["detail"]
    assert "watchlist" in body

    full = client.request("DELETE", "/api/tickers", json={"tickers": ["AAPL", "MSFT"], "return_state": False})
    assert full.status_code == 200
    assert full.json()["failed"] == [] and full.json()["ok"] is True and full.json()["status"] == "ok"

    none = client.request("DELETE", "/api/tickers", json={"tickers": ["ZZZZ"], "return_state": False})
    assert none.status_code == 404
    assert none.json()["status"] == "error" and none.json()["ok"] is False
    assert none.json()["failed"] == [{"ticker": "ZZZZ", "error": "not_in_watchlist"}]
    assert "ZZZZ" in none.json()["detail"]


def test_global_settings_and_bulk_remove_share_the_failed_item_shape(client, monkeypatch, remove_env):
    def flaky_update(ticker, settings):
        if ticker == "MSFT":
            raise OSError("disk full")
        return {}

    monkeypatch.setattr("api.actions_service.update_ticker_settings", flaky_update)
    monkeypatch.setattr("core.config.ConfigManager",
                        lambda *a, **k: SimpleNamespace(watchlist={"order": ["AAPL", "MSFT"]}))
    monkeypatch.setattr("api.main.preview_global_settings_apply", lambda settings: {"changed_fields": []})
    monkeypatch.setattr("api.main.load_state_payload", lambda default: {})
    monkeypatch.setattr("api.main.save_state_payload", lambda payload: None)

    settings = client.post("/api/watchlist/settings/global", json={"config": {"min_win_rate": 55}})
    assert settings.status_code == 207
    removal = client.request("DELETE", "/api/tickers", json={"tickers": ["AAPL", "ZZZZ"], "return_state": False})

    for body in (settings.json(), removal.json()):
        assert body["status"] == "partial" and body["ok"] is False
        assert isinstance(body["detail"], str) and body["detail"]
        assert body["failed"] and all(set(item) == {"ticker", "error"} for item in body["failed"])


# --------------------------------------------------------------------------
# 8. Quick AI context: session / price_as_of come from the row
# --------------------------------------------------------------------------

def test_quick_ai_context_receives_the_closed_session_from_the_row(monkeypatch):
    from api.actions_service import _build_quick_ai_context_from_row

    rows = _build_rows(
        monkeypatch,
        {"AAPL": {**_CLOSED_ROW, "last_update": _iso_ago(minutes=4)}},
        ["AAPL"],
    )
    context = _build_quick_ai_context_from_row(rows["AAPL"])
    assert context["session"] == "closed"
    assert context["price_as_of"] == "2026-11-13"
    # A closed-session row is refreshed every few minutes: its price is usable.
    assert context["price"] == 356.27
    assert "price_omitted_reason" not in context


def test_quick_ai_context_omits_the_price_of_a_row_older_than_24h(monkeypatch):
    from api.actions_service import _build_quick_ai_context_from_row

    rows = _build_rows(
        monkeypatch,
        {"AAPL": {**_CLOSED_ROW, "last_update": _iso_ago(hours=30)}},
        ["AAPL"],
    )
    context = _build_quick_ai_context_from_row(rows["AAPL"])
    assert "price" not in context and "change_pct" not in context
    assert "too old" in context["price_omitted_reason"]
    # What the stale number was is still described, not presented as current.
    assert context["session"] == "closed" and context["price_as_of"] == "2026-11-13"
