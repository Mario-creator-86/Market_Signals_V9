"""Tab audit 2026-11-15 — Calendar + Alerts zone regression tests.

Each test pins one defect that was confirmed in the running app or in code:
Telegram token leak / unescaped markup / "Active" without proof, alert input
that could never fire, superseded earnings estimates listed twice, a bulk
earnings window narrower than the months on screen, and missing-data states
that looked like "no events".
"""
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
import requests

import core.telegram_service as tg
from agents.economic_calendar_agent import EarningsEvent, EconomicCalendarAgent
from core.alerts import (
    AlertManager,
    AlertType,
    find_duplicate_alert,
    validate_alert_input,
)
from core.config import ConfigManager

TOKEN = "123456789:AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-bs"
CHAT = "1874173098"


# ----------------------------------------------------------------- Telegram
class _FakeConfig:
    def __init__(self, telegram=None):
        self.config = {"telegram": dict(telegram or {})}

    def update_values(self, updates):
        for path, value in updates:
            node = self.config
            for part in path[:-1]:
                node = node.setdefault(part, {})
            node[path[-1]] = value


class _Resp:
    def __init__(self, status_code=200, description=""):
        self.status_code = status_code
        self._description = description

    def json(self):
        return {"ok": self.status_code == 200, "description": self._description}


@pytest.fixture
def tg_env(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "_status_path", lambda: str(tmp_path / "telegram_delivery_status.json"))
    monkeypatch.setattr(tg, "MIN_SEND_INTERVAL_SECONDS", 0.0)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    return tmp_path


def test_build_message_escapes_third_party_text():
    text = tg.build_message(
        "📰 BREAKING · A<B & Co",
        ["Profit <b>up</b> 5% & rising", "https://x.test/a?b=1&c=2"],
        footer="Market Signals V9 · 10:00:00",
    )
    assert text.startswith("<b>📰 BREAKING · A&lt;B &amp; Co</b>")
    assert "&lt;b&gt;up&lt;/b&gt; 5% &amp; rising" in text
    assert "a?b=1&amp;c=2" in text
    # only the markup added by build_message itself remains
    assert text.count("<b>") == 1 and text.count("<i>") == 1


def test_build_message_truncates_without_cutting_markup():
    lines = ["x" * 500 + " & <tail>"] * 40
    text = tg.build_message("Title", lines, footer="footer")
    assert len(text) <= tg.MAX_MESSAGE_CHARS
    assert text.endswith("<i>footer</i>")
    assert "\n• …" in text
    # every entity that was opened is complete (no "&am" / "&l" fragments)
    assert text.count("&amp;") == text.count("&lt;tail&gt;")


def test_send_failure_never_leaks_token(tg_env, monkeypatch, caplog):
    def boom(url, **_kwargs):
        raise requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {url}")

    monkeypatch.setattr(tg.requests, "post", boom)
    with caplog.at_level("WARNING"):
        ok, detail = tg._send_message("hi", {"bot_token": TOKEN, "chat_id": CHAT})
    assert ok is False
    assert TOKEN not in detail and TOKEN.split(":")[1] not in detail
    assert TOKEN not in caplog.text and TOKEN.split(":")[1] not in caplog.text
    status = json.loads((tg_env / "telegram_delivery_status.json").read_text())
    assert status["ok"] is False and TOKEN not in json.dumps(status)


def test_active_badge_requires_a_verified_send(tg_env, monkeypatch):
    config = _FakeConfig({"enabled": True, "bot_token": TOKEN, "chat_id": CHAT})
    settings = tg.get_telegram_settings(config)
    assert settings["enabled"] is True
    assert settings["delivery_state"] == "unverified" and settings["verified"] is False

    monkeypatch.setattr(tg.requests, "post", lambda *a, **k: _Resp(200))
    assert tg._send_message("hi", {"bot_token": TOKEN, "chat_id": CHAT}) == (True, "sent")
    settings = tg.get_telegram_settings(config)
    assert settings["delivery_state"] == "ok" and settings["verified"] is True
    assert settings["last_delivery"]["ok"] is True

    monkeypatch.setattr(tg.requests, "post", lambda *a, **k: _Resp(401, "Unauthorized"))
    ok, detail = tg._send_message("hi", {"bot_token": TOKEN, "chat_id": CHAT})
    assert ok is False and "401" in detail
    settings = tg.get_telegram_settings(config)
    assert settings["delivery_state"] == "failing" and settings["verified"] is False
    assert settings["last_delivery"]["last_ok_at"]  # history of the last good send kept

    # A different chat id has never been proven → back to "unverified".
    other = _FakeConfig({"enabled": True, "bot_token": TOKEN, "chat_id": "-1001234567890"})
    assert tg.get_telegram_settings(other)["delivery_state"] == "unverified"

    off = _FakeConfig({"enabled": False, "bot_token": TOKEN, "chat_id": CHAT})
    assert tg.get_telegram_settings(off)["delivery_state"] == "off"
    assert tg.get_telegram_settings(_FakeConfig({"enabled": True}))["delivery_state"] == "not_configured"


def test_settings_payload_never_contains_raw_token(tg_env):
    settings = tg.get_telegram_settings(_FakeConfig({"enabled": True, "bot_token": TOKEN, "chat_id": CHAT}))
    dumped = json.dumps(settings)
    assert TOKEN not in dumped and TOKEN.split(":")[1] not in dumped
    assert settings["bot_token_masked"] and "…" in settings["bot_token_masked"]


def test_save_rejects_masked_placeholder_and_keeps_stored_token(tg_env):
    config = _FakeConfig({"enabled": True, "bot_token": TOKEN, "chat_id": CHAT})
    masked = tg.get_telegram_settings(config)["bot_token_masked"]
    with pytest.raises(ValueError):
        tg.save_telegram_settings(bot_token=masked, config=config)
    with pytest.raises(ValueError):
        tg.save_telegram_settings(bot_token=f"stored ({masked}) — leave empty to keep", config=config)
    with pytest.raises(ValueError):
        tg.save_telegram_settings(chat_id="my chat", config=config)
    assert config.config["telegram"]["bot_token"] == TOKEN
    assert config.config["telegram"]["chat_id"] == CHAT

    # empty token = keep; a well-formed token replaces it
    tg.save_telegram_settings(bot_token="", chat_id="-1001234567890", config=config)
    assert config.config["telegram"]["bot_token"] == TOKEN
    assert config.config["telegram"]["chat_id"] == "-1001234567890"
    new_token = "987654321:BBEhBOweik6ad9r_QXMENQjcrGbqCr4K-zz"
    tg.save_telegram_settings(bot_token=new_token, config=config)
    assert config.config["telegram"]["bot_token"] == new_token


def test_parse_error_is_retried_as_plain_text(tg_env, monkeypatch):
    calls = []

    def post(url, json=None, timeout=None):
        calls.append(json)
        if json.get("parse_mode"):
            return _Resp(400, "Bad Request: can't parse entities: Unsupported start tag")
        return _Resp(200)

    monkeypatch.setattr(tg.requests, "post", post)
    ok, detail = tg._send_message("<b>Title &amp; more</b>\n• line", {"bot_token": TOKEN, "chat_id": CHAT})
    assert (ok, detail) == (True, "sent")
    assert len(calls) == 2 and "parse_mode" not in calls[1]
    assert calls[1]["text"] == "Title & more\n• line"


def test_overlong_message_is_sent_as_plain_text_not_cut_html(tg_env, monkeypatch):
    sent = {}

    def post(url, json=None, timeout=None):
        sent.update(json)
        return _Resp(200)

    monkeypatch.setattr(tg.requests, "post", post)
    tg._send_message("<b>" + "a&amp;" * 2000 + "</b>", {"bot_token": TOKEN, "chat_id": CHAT})
    assert "parse_mode" not in sent
    assert len(sent["text"]) <= tg.MAX_MESSAGE_CHARS and "<b>" not in sent["text"]


def test_notify_delivers_escaped_news_headline_and_respects_routes(tg_env, monkeypatch):
    import time

    sent = []
    monkeypatch.setattr(tg.requests, "post", lambda url, json=None, timeout=None: sent.append(json) or _Resp(200))
    monkeypatch.setattr(tg, "_raw_settings_for_send", lambda: {"bot_token": TOKEN, "chat_id": CHAT})
    settings = {"enabled": True, "routes": {"news": True, "alerts": False}}

    tg.notify_telegram("alerts", "🔔 AAPL", ["Target: 1"], settings=settings)      # route off
    tg.notify_telegram("unknown", "x", [], settings=settings)                      # bad category
    tg.notify_telegram("news", "📰 BREAKING · AT&T", ["Q3 <guidance> cut & shares fall"], settings=settings)
    deadline = time.time() + 5
    while not sent and time.time() < deadline:
        time.sleep(0.02)
    assert len(sent) == 1
    assert sent[0]["parse_mode"] == "HTML"
    assert "<b>📰 BREAKING · AT&amp;T</b>" in sent[0]["text"]
    assert "• Q3 &lt;guidance&gt; cut &amp; shares fall" in sent[0]["text"]


# ------------------------------------------------------------------- Alerts
@pytest.fixture
def config_factory(tmp_path, monkeypatch):
    import core.config as module

    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps({"stocks": ["AAPL", "NVDA"], "crypto": ["BTC-USD"], "alerts": []}))
    monkeypatch.setattr(module, "WATCHLIST_PATH", path)

    def factory():
        cfg = ConfigManager(path=tmp_path / "config.json")
        cfg._store = None
        return cfg

    return factory, path


@pytest.mark.parametrize(
    "ticker, alert_type, target",
    [
        ("AAPL", "PRICE_ABOVE", 100),      # unknown type: check_alerts never fires it
        ("AAPL", "PRICE_UP", float("nan")),
        ("AAPL", "PRICE_UP", float("inf")),
        ("AAPL", "PRICE_UP", -5),
        ("AAPL", "PRICE_DOWN", 0),
        ("AAPL", "VOLATILITY", 5000),      # a price typed into the percent field
        ("AAPL", "PRICE_UP", "abc"),
        ("", "PRICE_UP", 100),
        ("AA PL<script>", "PRICE_UP", 100),
    ],
)
def test_validate_alert_input_rejects_alerts_that_can_never_work(ticker, alert_type, target):
    with pytest.raises(ValueError):
        validate_alert_input(ticker, alert_type, target)


def test_validate_alert_input_normalizes_valid_requests():
    assert validate_alert_input(" btc-usd ", "price_up", "70000.5") == ("BTC-USD", "PRICE_UP", 70000.5)
    assert validate_alert_input("AAPL", "VOLATILITY", 5) == ("AAPL", "VOLATILITY", 5.0)


def test_fired_alert_records_when_and_at_what_price_and_rearm_clears_it(config_factory):
    make, path = config_factory
    manager = AlertManager(make())
    alert = manager.add_alert("AAPL", AlertType.PRICE_UP, 100)
    assert "triggered_at" not in alert.to_dict()

    fired = manager.check_alerts({"AAPL": {"price": 101.5}})
    assert len(fired) == 1 and fired[0]["alert"]["triggered_price"] == 101.5
    stored = json.loads(path.read_text())["alerts"][0]
    assert stored["active"] is False and stored["triggered_at"] and stored["triggered_price"] == 101.5

    # survives a restart (fresh manager reads the file) and does not fire again
    restarted = AlertManager(make())
    assert restarted.get_alerts()[0]["triggered_price"] == 101.5
    assert restarted.check_alerts({"AAPL": {"price": 105}}) == []

    assert restarted.toggle_alert(alert.id) is True
    rearmed = json.loads(path.read_text())["alerts"][0]
    assert rearmed["active"] is True and "triggered_at" not in rearmed


def test_stale_price_never_fires_an_alert(config_factory):
    make, _path = config_factory
    manager = AlertManager(make())
    manager.add_alert("BTC-USD", AlertType.PRICE_DOWN, 50000)
    assert manager.check_alerts({"BTC-USD": {"price": 40000, "stale": True}}) == []
    assert len(manager.check_alerts({"BTC-USD": {"price": 40000}})) == 1


def test_create_alert_rejects_invalid_duplicate_unwatched_and_already_crossed(config_factory, monkeypatch):
    import api.actions_service as actions
    import api.engine_service as engine_service

    make, path = config_factory
    monkeypatch.setattr(actions, "get_shared_config", make)
    monkeypatch.setattr(engine_service, "get_engine", lambda: None)
    monkeypatch.setattr(actions, "ConfigManager", make, raising=False)
    import core.config as config_module
    monkeypatch.setattr(config_module, "ConfigManager", make)
    state = {"tickers": {"AAPL": {"price": 316.02}, "NVDA": {"price": 140.0, "stale": True}}}
    monkeypatch.setattr(actions, "load_state_payload", lambda default=None: state)

    def stored():
        return json.loads(path.read_text())["alerts"]

    assert actions.create_alert("AAPL", "PRICE_SIDEWAYS", 100)["status_code"] == 400
    assert actions.create_alert("AAPL", "PRICE_UP", float("nan"))["status_code"] == 400
    assert actions.create_alert("AAPL", "PRICE_UP", -1)["status_code"] == 400
    assert actions.create_alert("ZZZZ", "PRICE_UP", 10)["status_code"] == 400      # not watched
    # target already crossed at the last scanned price → would fire instantly
    crossed = actions.create_alert("AAPL", "PRICE_UP", 300)
    assert crossed["status_code"] == 400 and "already trades" in crossed["error"]
    assert actions.create_alert("AAPL", "PRICE_DOWN", 400)["status_code"] == 400
    assert stored() == []

    created = actions.create_alert("aapl", "price_up", 350)
    assert created["ticker"] == "AAPL" and created["type"] == "PRICE_UP" and created["active"] is True
    duplicate = actions.create_alert("AAPL", "PRICE_UP", 350.0)
    assert duplicate["status_code"] == 409
    assert len(stored()) == 1

    # a stale reference price is not used to refuse an alert
    assert actions.create_alert("NVDA", "PRICE_UP", 100).get("active") is True
    # volatility threshold is a percent, never checked against the price
    assert actions.create_alert("AAPL", "VOLATILITY", 5).get("active") is True


def test_find_duplicate_alert_ignores_inactive_rows():
    rows = [{"ticker": "AAPL", "type": "PRICE_UP", "target": 350, "active": False}]
    assert find_duplicate_alert(rows, "AAPL", "PRICE_UP", 350) is None
    rows[0]["active"] = True
    assert find_duplicate_alert(rows, "aapl", "PRICE_UP", 350.0) is rows[0]
    assert find_duplicate_alert(rows, "AAPL", "PRICE_DOWN", 350) is None


# ----------------------------------------------------------------- Calendar
def _new_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = EconomicCalendarAgent()
    agent._cache_path = str(tmp_path / "calendar_events_cache.json")
    agent._events_cache = {"economic": {}, "earnings": {}, "ipo": {}, "earnings_meta": {}}
    agent._earnings_bulk_fetched_at = None
    agent._earnings_bulk_window = None
    agent._earnings_bulk_last_error = None
    return agent


def _estimate(symbol, day, verified, period="Oct/2026", **extra):
    row = {
        "id": f"earn_{symbol}_{day.replace('-', '')}",
        "symbol": symbol,
        "date": f"{day}T00:00:00",
        "date_formatted": day,
        "category": "earnings",
        "when": "amc",
        "source": "nasdaq_zacks_estimate",
        "confirmed": False,
        "eps_actual": None,
        "revenue_actual": None,
        "verified_at": verified,
        "fiscal_period": period,
        "date_confidence": "estimated",
    }
    row.update(extra)
    return row


def test_superseded_estimate_is_not_listed_twice(tmp_path, monkeypatch):
    """Live 2026-11-15: ZS showed 24 Nov (fetched 22 Oct) AND 2 Dec (fetched
    12 Nov) for the same Oct/2026 quarter — 393 symbols were doubled."""
    agent = _new_agent(tmp_path, monkeypatch)
    agent._events_cache["earnings"] = {
        "ZS": [
            _estimate("ZS", "2026-11-24", "2026-10-22T10:46:58+00:00"),
            _estimate("ZS", "2026-12-02", "2026-11-12T07:03:12+00:00"),
        ],
        # different fiscal periods → two real reports, both stay
        "RFL": [
            _estimate("RFL", "2026-11-04", "2026-10-22T10:46:58+00:00", period="Jul/2026"),
            _estimate("RFL", "2026-12-10", "2026-11-12T07:03:12+00:00", period="Oct/2026"),
        ],
        # a confirmed row replaces a nearby unconfirmed estimate without a period
        "NVDA": [
            _estimate("NVDA", "2026-11-19", "2026-11-14T00:00:00+00:00", period=None),
            _estimate("NVDA", "2026-11-17", "2026-11-12T00:00:00+00:00", period=None,
                      source="yfinance+calendar", confirmed=True),
        ],
    }
    rows = agent._get_cached_earnings_range(datetime(2026, 11, 1), datetime(2026, 12, 31))
    listed = sorted((r["symbol"], r["date_formatted"]) for r in rows)
    assert listed == [
        ("NVDA", "2026-11-17"),
        ("RFL", "2026-11-04"),
        ("RFL", "2026-12-10"),
        ("ZS", "2026-12-02"),
    ]
    # the replacing row may sit outside the requested range — still superseded
    assert agent._get_cached_earnings_range(datetime(2026, 11, 20), datetime(2026, 11, 30)) == []


def test_reported_rows_are_never_dropped_as_superseded():
    rows = [
        _estimate("AAA", "2026-11-03", "2026-10-01T00:00:00+00:00", eps_actual=1.2),
        _estimate("AAA", "2026-11-05", "2026-11-01T00:00:00+00:00"),
    ]
    kept = EconomicCalendarAgent._drop_superseded_estimates(rows)
    assert [r["date_formatted"] for r in kept] == ["2026-11-03"]


def test_range_rows_are_copies_with_title_and_fresh_upcoming_flag(tmp_path, monkeypatch):
    agent = _new_agent(tmp_path, monkeypatch)
    past = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
    future = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
    cached_past = _estimate("OLD", past, "2026-01-01T00:00:00+00:00", period="A", is_upcoming=True)
    cached_future = {  # row written by another agent: no title, no is_upcoming
        "id": f"earn_NEW_{future.replace('-', '')}", "symbol": "NEW",
        "date": f"{future}T00:00:00+00:00", "date_formatted": future,
        "source": "yfinance", "confirmed": True,
    }
    agent._events_cache["earnings"] = {"OLD": [cached_past], "NEW": [cached_future]}
    rows = {r["symbol"]: r for r in agent._get_cached_earnings_range(
        datetime.now() - timedelta(days=60), datetime.now() + timedelta(days=30))}
    assert rows["OLD"]["is_upcoming"] is False        # was frozen True at fetch time
    assert rows["NEW"]["is_upcoming"] is True
    assert rows["NEW"]["title"] == "NEW Earnings"     # rendered as "undefined" before
    rows["NEW"]["is_watchlist"] = True                # callers annotate rows…
    assert "is_watchlist" not in cached_future        # …without writing into the cache


def test_earnings_event_default_session_is_unknown_not_premarket():
    event = EarningsEvent(symbol="AMAT", date=datetime(2026, 8, 13))
    data = event.to_dict()
    assert data["when"] == "unknown" and data["when_desc"] == "Time Unconfirmed"


def test_finnhub_row_without_date_is_skipped_not_stamped_today(tmp_path, monkeypatch):
    agent = _new_agent(tmp_path, monkeypatch)
    agent._fh = MagicMock()
    agent._fh.is_available.return_value = True
    agent._fh.get_earnings_calendar.return_value = [
        {"symbol": "NODATE", "date": "", "hour": "amc"},
        {"symbol": "GOOD", "date": "2026-11-20", "hour": "bmo"},
    ]
    events = agent._fetch_finnhub_earnings_calendar(
        datetime(2026, 11, 1), datetime(2026, 11, 30), restrict_to_watchlist=False)
    assert [e.symbol for e in events] == ["GOOD"]


def test_coverage_reports_real_bulk_window_and_survives_restart(tmp_path, monkeypatch):
    """Live 2026-11-15: the Nasdaq fallback fetched 14 Nov..18 Dec but logged
    (and implied) the full requested 16 Oct..13 Feb range."""
    agent = _new_agent(tmp_path, monkeypatch)
    agent._fh = MagicMock()
    agent._fh.is_available.return_value = False
    window = (datetime(2026, 11, 14).date(), datetime(2026, 12, 18).date())

    def _nasdaq(*_args):
        agent._nasdaq_last_fetch_succeeded = True
        agent._nasdaq_last_window = window
        agent._nasdaq_last_day_counts = (35, 35)
        return [EarningsEvent(symbol="AMAT", date=datetime(2026, 11, 20), source="nasdaq_zacks_estimate")]

    monkeypatch.setattr(agent, "_fetch_nasdaq_earnings_calendar", _nasdaq)
    before = agent.get_calendar_coverage()
    assert before["earnings"]["fetched_at"] is None and before["earnings"]["stale"] is True
    assert before["ipo"]["available"] is False and before["ipo"]["reason"]

    agent._do_bulk_earnings_fetch(datetime(2026, 10, 16), datetime(2027, 2, 13))
    coverage = agent.get_calendar_coverage()["earnings"]
    assert coverage["window_start"] == "2026-11-14" and coverage["window_end"] == "2026-12-18"
    assert coverage["provider"] == "nasdaq" and coverage["dates_are_estimates"] is True
    assert coverage["stale"] is False and coverage["last_error"] is None
    assert coverage["age_seconds"] is not None and coverage["age_seconds"] < 60

    # the window and fetch time are persisted, so the age is known after a restart
    restarted = EconomicCalendarAgent()
    restarted._cache_path = agent._cache_path
    restarted._earnings_bulk_fetched_at = None
    restarted._earnings_bulk_window = None
    restarted._load_cache_from_disk()
    again = restarted.get_calendar_coverage()["earnings"]
    assert again["window_end"] == "2026-12-18" and again["fetched_at"] is not None


def test_failed_bulk_refresh_is_reported_not_swallowed(tmp_path, monkeypatch):
    agent = _new_agent(tmp_path, monkeypatch)
    agent._fh = MagicMock()
    agent._fh.is_available.return_value = False

    def _nasdaq_down(*_args):
        agent._nasdaq_last_fetch_succeeded = False
        return []

    monkeypatch.setattr(agent, "_fetch_nasdaq_earnings_calendar", _nasdaq_down)
    agent._do_bulk_earnings_fetch(datetime(2026, 11, 1), datetime(2026, 12, 1))
    coverage = agent.get_calendar_coverage()["earnings"]
    assert coverage["last_error"] and coverage["fetched_at"] is None
    assert coverage["last_attempt_at"] is not None

    def _partial(*_args):
        agent._nasdaq_last_fetch_succeeded = True
        agent._nasdaq_last_window = (datetime(2026, 11, 1).date(), datetime(2026, 12, 1).date())
        agent._nasdaq_last_day_counts = (20, 31)
        return [EarningsEvent(symbol="AMAT", date=datetime(2026, 11, 20), source="nasdaq_zacks_estimate")]

    monkeypatch.setattr(agent, "_fetch_nasdaq_earnings_calendar", _partial)
    agent._do_bulk_earnings_fetch(datetime(2026, 11, 1), datetime(2026, 12, 1))
    assert "partial refresh" in agent.get_calendar_coverage()["earnings"]["last_error"]


def test_macro_coverage_names_missing_years(tmp_path, monkeypatch):
    import api.main as main

    coverage = main._macro_calendar_coverage(datetime(2026, 11, 1), datetime(2027, 2, 6))
    assert 2026 in coverage["years_available"]
    # no 2027 schedule file is bundled → the UI must be told, not shown an
    # empty January as "no macro events"
    assert (2027 in coverage["years_missing"]) == (not coverage["complete"])
    assert sorted(coverage["years_available"] + coverage["years_missing"]) == [2026, 2027]


def test_macro_release_time_is_converted_from_eastern_time():
    import api.main as main

    events = []
    main._merge_macro_calendar_events(datetime(2026, 7, 1), datetime(2026, 12, 31), events)
    by_id = {e["id"]: e for e in events}
    # 08:30 ET is 12:30 UTC in summer (EDT) and 13:30 UTC in winter (EST)
    assert by_id["macro_jobs_20260702"]["date"].startswith("2026-07-02T12:30:00")
    assert by_id["macro_jobs_20261204"]["date"].startswith("2026-12-04T13:30:00")
    assert by_id["macro_jobs_20261204"]["date_formatted"] == "2026-12-04 08:30 ET"
    assert all(e["time_precision"] == "published_time" for e in events)


def test_calendar_range_endpoint_validates_range_and_reports_coverage(client):
    assert client.get("/api/calendar/range", params={"start": "2026-12-01", "end": "2026-11-01"}).status_code == 400
    assert client.get("/api/calendar/range", params={"start": "2020-01-01", "end": "2026-11-01"}).status_code == 400
    assert client.get("/api/calendar/range", params={"start": "nope", "end": "2026-11-01"}).status_code == 400

    response = client.get("/api/calendar/range", params={"start": "2026-11-01", "end": "2027-02-06"})
    assert response.status_code == 200
    coverage = response.json()["coverage"]
    assert {"earnings", "ipo", "fomc", "macro", "generated_at"} <= set(coverage)
    assert coverage["ipo"]["available"] is False
    assert 2027 in (coverage["macro"]["years_available"] + coverage["macro"]["years_missing"])


def test_calendar_range_watchlist_flag_is_reset_for_removed_tickers(client, monkeypatch):
    from agents.economic_calendar_agent import get_economic_calendar_agent

    agent = get_economic_calendar_agent()
    row = _estimate("ZZTOP", "2026-11-20", "2026-11-15T00:00:00+00:00", is_watchlist=True)
    with agent._cache_lock:
        previous = agent._events_cache.get("earnings", {}).get("ZZTOP")
        agent._events_cache.setdefault("earnings", {})["ZZTOP"] = [row]
    try:
        payload = client.get("/api/calendar/range", params={"start": "2026-11-15", "end": "2026-11-30"}).json()
        listed = [e for e in payload["earnings"] if e["symbol"] == "ZZTOP"]
        assert len(listed) == 1
        assert "ZZTOP" not in payload["watchlist_tickers"]
        assert listed[0]["is_watchlist"] is False   # stale cached star removed
        assert row["is_watchlist"] is True           # cache itself untouched
    finally:
        with agent._cache_lock:
            if previous is None:
                agent._events_cache["earnings"].pop("ZZTOP", None)
            else:
                agent._events_cache["earnings"]["ZZTOP"] = previous
