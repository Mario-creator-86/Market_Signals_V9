"""Tab audit 2026-11-15 — Premarket tab + core.market_hours regressions.

Evidence (Sunday 2026-11-15, US market closed for the weekend): the Premarket
tab showed Friday's last prints as a live pre-market ("UPDATED 9:50:56 AM",
"next run 300s"), the AI was asked to analyse a pre-market that did not exist,
and the scanner spent a full provider/LLM cycle every 5 minutes all weekend.

Covered here:
  * core.market_hours: session snapshot (which session the prices belong to),
    NYSE calendar fixes (Jul 2 2027 is a full day, a holiday is not a "half
    day", Sat New Year is not observed, half-day after-hours ends 17:00 ET);
  * agent: session block on payload/candidates, closed-session budget guard,
    real next-refresh time, session-aware AI prompts, US/Eastern earnings day
    boundary, "has earnings" bonus only with a real earnings row, no "NONE"
    catalyst tag, frozen snapshot keeps its own timestamp;
  * API: /api/premarket state build no longer leaks a thread per timeout.
"""

import os
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

import core.market_hours as mh
from core.data_model.entities import TradingSession
from agents import premarket_scanner_agent as pm
from agents.premarket_scanner_agent import PremarketScannerAgent

UTC = timezone.utc
ET = ZoneInfo("America/New_York")

# The moment the defect was observed: Sunday 09:55 Bucharest = 07:55 UTC.
SUNDAY = datetime(2026, 11, 15, 7, 55, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_market_hours_override(monkeypatch):
    monkeypatch.delenv("MS_MARKET_HOURS_OVERRIDE", raising=False)


def _et(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


# ---------------------------------------------------------------------------
# core.market_hours — session snapshot
# ---------------------------------------------------------------------------
class TestSessionSnapshot:
    def test_sunday_is_closed_and_points_at_fridays_session(self):
        s = mh.us_equity_session_snapshot(SUNDAY)
        assert s["state"] == "CLOSED"
        assert s["closed_kind"] == "weekend"
        assert s["is_trading_day"] is False
        # The newest prints belong to Friday, whose after-hours ended 20:00 ET.
        assert s["quote_session_date"] == "2026-11-13"
        assert s["last_regular_session_date"] == "2026-11-13"
        assert s["last_extended_close_utc"] == "2026-11-14T01:00:00+00:00"
        # Next pre-market: Monday 04:00 EST = 09:00 UTC; open 09:30 EST.
        assert s["next_premarket_open_utc"] == "2026-11-16T09:00:00+00:00"
        assert s["next_regular_open_utc"] == "2026-11-16T14:30:00+00:00"
        assert s["seconds_since_last_trade"] == int((SUNDAY - datetime(2026, 11, 14, 1, 0, tzinfo=UTC)).total_seconds())
        assert "weekend" in s["label"]

    def test_real_premarket_window(self):
        s = mh.us_equity_session_snapshot(_et(2026, 11, 16, 5, 0))
        assert s["state"] == "PRE_MARKET"
        assert s["closed_kind"] is None
        assert s["quote_session_date"] == "2026-11-16"          # pre-market prints are today's
        assert s["last_regular_session_date"] == "2026-11-13"   # last COMPLETED regular session
        assert s["last_extended_close_utc"] is None
        assert s["next_regular_open_utc"] == "2026-11-16T14:30:00+00:00"

    def test_after_hours_and_overnight(self):
        ah = mh.us_equity_session_snapshot(_et(2026, 11, 16, 17, 0))
        assert ah["state"] == "AFTER_HOURS"
        assert ah["last_regular_session_date"] == "2026-11-16"
        night = mh.us_equity_session_snapshot(_et(2026, 11, 16, 22, 0))
        assert night["state"] == "CLOSED" and night["closed_kind"] == "overnight"
        assert night["quote_session_date"] == "2026-11-16"
        assert night["seconds_since_last_trade"] == 2 * 3600
        early = mh.us_equity_session_snapshot(_et(2026, 11, 17, 2, 0))
        assert early["state"] == "CLOSED" and early["closed_kind"] == "overnight"
        assert early["quote_session_date"] == "2026-11-16"
        assert early["next_premarket_open_utc"] == "2026-11-17T09:00:00+00:00"

    def test_holiday(self):
        s = mh.us_equity_session_snapshot(_et(2026, 11, 26, 10, 0))  # Thanksgiving
        assert s["state"] == "CLOSED" and s["closed_kind"] == "holiday"
        assert s["quote_session_date"] == "2026-11-25"
        assert s["next_regular_open_utc"] == "2026-11-27T14:30:00+00:00"

    def test_dst_changes_move_the_utc_offset(self):
        # Sun 2026-03-08: clocks spring forward. Monday 04:00 EDT = 08:00 UTC.
        spring = mh.us_equity_session_snapshot(datetime(2026, 3, 8, 12, 0, tzinfo=UTC))
        assert spring["next_premarket_open_utc"] == "2026-03-09T08:00:00+00:00"
        assert spring["next_regular_open_utc"] == "2026-03-09T13:30:00+00:00"
        # Friday before was still EST: after-hours ended 20:00 EST = 01:00 UTC.
        assert spring["last_extended_close_utc"] == "2026-03-07T01:00:00+00:00"
        # Sun 2026-11-01: clocks fall back. Monday 04:00 EST = 09:00 UTC.
        fall = mh.us_equity_session_snapshot(datetime(2026, 11, 1, 12, 0, tzinfo=UTC))
        assert fall["next_premarket_open_utc"] == "2026-11-02T09:00:00+00:00"
        # Friday Oct 30 was EDT: 20:00 EDT = 00:00 UTC.
        assert fall["last_extended_close_utc"] == "2026-10-31T00:00:00+00:00"

    def test_snapshot_is_json_serialisable(self):
        import json
        json.dumps(mh.us_equity_session_snapshot(SUNDAY))

    def test_local_bucharest_time_is_converted_not_reinterpreted(self):
        bucharest = SUNDAY.astimezone(ZoneInfo("Europe/Bucharest"))
        assert mh.us_equity_session_snapshot(bucharest) == mh.us_equity_session_snapshot(SUNDAY)


# ---------------------------------------------------------------------------
# core.market_hours — NYSE calendar
# ---------------------------------------------------------------------------
class TestNyseCalendar:
    def test_published_holidays_2026_to_2028(self):
        # https://www.nyse.com/markets/hours-calendars (2026 / 2027 / 2028)
        assert mh._us_equity_holidays(2026) == {
            date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
            date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
            date(2026, 11, 26), date(2026, 12, 25),
        }
        assert mh._us_equity_holidays(2027) == {
            date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
            date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
            date(2027, 11, 25), date(2027, 12, 24),
        }
        # 2028: New Year's Day is a Saturday -> NOT observed (no Dec 31 2027 closure).
        assert mh._us_equity_holidays(2028) == {
            date(2028, 1, 17), date(2028, 2, 21), date(2028, 4, 14), date(2028, 5, 29),
            date(2028, 6, 19), date(2028, 7, 4), date(2028, 9, 4), date(2028, 11, 23),
            date(2028, 12, 25),
        }

    def test_saturday_new_year_leaves_dec_31_and_jan_3_open(self):
        assert mh.is_us_equity_trading_day(date(2027, 12, 31)) is True
        assert mh.is_us_equity_trading_day(date(2028, 1, 3)) is True
        assert mh.us_equity_market_status(_et(2027, 12, 31, 11, 0)).is_open is True

    def test_early_closes_match_the_published_calendar(self):
        early = mh._us_equity_early_close_date
        assert early(date(2026, 11, 27)) and early(date(2027, 11, 26)) and early(date(2028, 11, 24))
        assert early(date(2026, 12, 24)) is True       # Thursday
        assert early(date(2028, 7, 3)) is True         # Monday before Tue Jul 4
        # Jul 4 2027 is a Sunday (observed Mon Jul 5): Fri Jul 2 is a FULL day.
        assert early(date(2027, 7, 2)) is False
        s = mh.us_equity_market_status(_et(2027, 7, 2, 14, 30))
        assert s.is_open is True and s.is_half_day is False
        assert s.close_time_et.strftime("%H:%M") == "16:00"

    def test_full_holiday_is_not_also_a_half_day(self):
        # Fri Jul 3 2026 (Independence Day observed) and Fri Dec 24 2027
        # (Christmas observed) are closed all day, not "early close".
        for day in (date(2026, 7, 3), date(2027, 12, 24)):
            assert mh._us_equity_early_close_date(day) is False
            s = mh.us_equity_market_status(datetime(day.year, day.month, day.day, 11, 0, tzinfo=ET))
            assert s.is_open is False and s.reason == "us_equity_holiday"
            assert s.is_half_day is False

    def test_half_day_after_hours_ends_at_17_et(self):
        assert mh.us_equity_market_status(_et(2026, 11, 27, 16, 30)).session is TradingSession.AFTER_HOURS
        late = mh.us_equity_market_status(_et(2026, 11, 27, 18, 0))
        assert late.session is TradingSession.CLOSED
        snap = mh.us_equity_session_snapshot(_et(2026, 11, 27, 18, 0))
        assert snap["last_extended_close_utc"] == "2026-11-27T22:00:00+00:00"   # 17:00 EST
        # A normal day is still after-hours at 18:00.
        assert mh.us_equity_market_status(_et(2026, 11, 16, 18, 0)).session is TradingSession.AFTER_HOURS

    def test_us_eastern_date(self):
        # Date-only calendar rows (midnight UTC) are exchange dates already.
        assert mh.us_eastern_date(datetime(2026, 8, 4, 0, 0, tzinfo=UTC)) == date(2026, 8, 4)
        # A real timestamp is converted: 01:00 UTC Aug 4 is still Aug 3 in New York.
        assert mh.us_eastern_date(datetime(2026, 8, 4, 1, 0, tzinfo=UTC)) == date(2026, 8, 3)
        assert mh.us_eastern_date(None, datetime(2026, 8, 4, 1, 0, tzinfo=UTC)) == date(2026, 8, 3)


# ---------------------------------------------------------------------------
# Agent helpers
# ---------------------------------------------------------------------------
def _bare_agent():
    """Agent without __init__ (no ConfigManager / disk), like the existing unit tests."""
    agent = PremarketScannerAgent.__new__(PremarketScannerAgent)
    agent._lock = threading.RLock()
    agent._wake_event = threading.Event()
    agent._stop_event = threading.Event()      # get_state() checks the scan loop is alive
    agent._thread = None
    agent._last_restart_attempt = 0.0
    agent._pending_refresh = False
    agent._force_full_cycle = False
    agent._cycle_inflight = False
    agent.config_manager = MagicMock()
    agent._state = {"status": {}, "candidates": [], "flags": {}}
    return agent


def _cfg(**over):
    cfg = dict(PremarketScannerAgent.DEFAULT_CONFIG)
    cfg.update(over)
    return cfg


def _mover(symbol="VEON", price=79.17, prev=75.15):
    return {
        "symbol": symbol,
        "gap_pct": (price - prev) / prev * 100.0,
        "premarket_price": price,
        "prev_close": prev,
        "premarket_volume": None,
        "avg_volume": 5_000_000.0,
        "source": "scanner",
    }


def _build(agent, movers, earnings_items=(), session=None, now=SUNDAY, movers_age=49):
    with patch("agents.premarket_scanner_agent._utc_now", return_value=now), \
         patch.object(agent, "_is_excluded_symbol", return_value=False):
        return agent._build_payload(
            _cfg(), list(movers), [], list(earnings_items), movers_age, 15, 279,
            news_available=True, earnings_available=True, watchlist_symbols=None,
            session=session,
        )


PREMARKET_NOW = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)            # Mon 05:00 ET


class _LazySession(dict):
    """Session snapshot computed on first use, i.e. inside a test, after the
    autouse fixture removed the suite-wide MS_MARKET_HOURS_OVERRIDE (which
    would otherwise turn every snapshot taken at import time into REGULAR)."""

    def __init__(self, when):
        super().__init__()
        self._when = when

    def _load(self):
        if not dict.__len__(self):
            dict.update(self, mh.us_equity_session_snapshot(self._when))
        return self

    def __getitem__(self, key):
        return dict.__getitem__(self._load(), key)

    def get(self, key, default=None):
        return dict.get(self._load(), key, default)

    def keys(self):
        return dict.keys(self._load())

    def __iter__(self):
        return dict.__iter__(self._load())

    def __len__(self):
        return dict.__len__(self._load())

    def __bool__(self):
        return True


CLOSED_SESSION = _LazySession(SUNDAY)
PREMARKET_SESSION = _LazySession(PREMARKET_NOW)


# ---------------------------------------------------------------------------
# Payload says which session the prices are from
# ---------------------------------------------------------------------------
class TestPayloadSession:
    def test_weekend_payload_is_last_session_not_updated_now(self):
        payload = _build(_bare_agent(), [_mover()], session=CLOSED_SESSION)
        sess = payload["session"]
        assert sess["state"] == "CLOSED" and sess["closed_kind"] == "weekend"
        assert sess["data_kind"] == pm.DATA_KIND_LAST_SESSION
        assert sess["data_session_date"] == "2026-11-13"
        # The feed was re-read 49s ago, but nothing traded since Fri 20:00 ET.
        assert sess["movers_fetched_at_utc"] == (SUNDAY - timedelta(seconds=49)).isoformat()
        assert sess["quotes_as_of_utc"] == "2026-11-14T01:00:00+00:00"
        assert sess["quotes_age_seconds"] == int((SUNDAY - datetime(2026, 11, 14, 1, 0, tzinfo=UTC)).total_seconds())
        assert "not a live pre-market" in sess["data_label"]
        cand = payload["candidates"][0]
        assert cand["price_session"] == pm.DATA_KIND_LAST_SESSION
        assert cand["quote_time_utc"] == "2026-11-14T01:00:00+00:00"
        assert payload["flags"]["session_state"] == "CLOSED"
        assert payload["flags"]["data_kind"] == pm.DATA_KIND_LAST_SESSION

    def test_live_premarket_payload_uses_the_fetch_time(self):
        payload = _build(_bare_agent(), [_mover()], session=PREMARKET_SESSION, now=PREMARKET_NOW, movers_age=40)
        sess = payload["session"]
        assert sess["data_kind"] == pm.DATA_KIND_PREMARKET
        assert sess["quotes_as_of_utc"] == (PREMARKET_NOW - timedelta(seconds=40)).isoformat()
        assert sess["quotes_age_seconds"] == 40
        assert payload["candidates"][0]["price_session"] == pm.DATA_KIND_PREMARKET

    def test_no_session_keeps_the_legacy_shape(self):
        payload = _build(_bare_agent(), [_mover()], session=None)
        assert payload["session"] is None
        assert payload["candidates"][0]["price_session"] is None

    def test_has_earnings_bonus_needs_a_real_earnings_row(self):
        # No news, no catalysts, no earnings row -> base catalyst score 40.
        # (It was 46 for every ticker: the normalized earnings dict is never empty.)
        no_row = _build(_bare_agent(), [_mover("VEON")])["candidates"][0]
        assert no_row["earnings"]["eps_actual"] is None
        assert no_row["catalyst_score"] == 40
        future = [{"symbol": "VEON", "date": "2026-11-20", "when": "bmo"}]
        with_row = _build(_bare_agent(), [_mover("VEON")], earnings_items=future)["candidates"][0]
        assert with_row["catalyst_score"] == 46


# ---------------------------------------------------------------------------
# Earnings "already reported" — US/Eastern day boundary
# ---------------------------------------------------------------------------
class TestEarningsDayBoundary:
    reported = staticmethod(PremarketScannerAgent._earnings_already_reported)

    def test_tomorrows_bmo_is_not_reported_the_evening_before(self):
        # Mon Aug 3 21:00 ET == Tue Aug 4 01:00 UTC. Tuesday's BMO has not
        # printed. With UTC dates it looked "same day, past 09:30 ET" -> dropped.
        row = {"date": "2026-08-04", "when": "bmo"}
        assert self.reported(row, datetime(2026, 8, 4, 1, 0, tzinfo=UTC)) is False
        assert self.reported(row, datetime(2026, 8, 4, 3, 59, tzinfo=UTC)) is False

    def test_tomorrows_amc_is_not_reported_the_evening_before(self):
        row = {"date": "2026-08-04", "when": "amc"}
        assert self.reported(row, datetime(2026, 8, 4, 1, 0, tzinfo=UTC)) is False

    def test_winter_boundary_is_05_utc(self):
        # EST: ET midnight is 05:00 UTC. Mon Nov 16 23:30 ET == Tue 04:30 UTC.
        row = {"date": "2026-11-17", "when": "bmo"}
        assert self.reported(row, datetime(2026, 11, 17, 4, 30, tzinfo=UTC)) is False
        # Tue 09:29 ET -> still upcoming; 09:30 ET -> reported.
        assert self.reported(row, datetime(2026, 11, 17, 14, 29, tzinfo=UTC)) is False
        assert self.reported(row, datetime(2026, 11, 17, 14, 30, tzinfo=UTC)) is True

    def test_todays_amc_stays_reported_after_utc_midnight(self):
        # Mon Aug 3 AMC printed at 16:05 ET; at 21:00 ET (Tue 01:00 UTC) it is reported.
        row = {"date": "2026-08-03", "when": "amc"}
        assert self.reported(row, datetime(2026, 8, 4, 1, 0, tzinfo=UTC)) is True

    def test_naive_now_is_treated_as_utc(self):
        row = {"date": "2026-08-04", "when": "bmo"}
        assert self.reported(row, datetime(2026, 8, 4, 1, 0)) is False

    def test_build_payload_keeps_tomorrows_reporter_in_the_evening(self):
        now = datetime(2026, 8, 4, 1, 0, tzinfo=UTC)
        earnings = [{"symbol": "VEON", "date": "2026-08-04", "when": "bmo"}]
        payload = _build(_bare_agent(), [_mover("VEON")], earnings_items=earnings, now=now)
        assert [c["symbol"] for c in payload["candidates"]] == ["VEON"]
        assert payload["reported_today"] == []


# ---------------------------------------------------------------------------
# Closed-session budget guard + real next-refresh time
# ---------------------------------------------------------------------------
class TestClosedSessionBudget:
    def _recap_state(self, built_at, session=CLOSED_SESSION):
        block = PremarketScannerAgent._describe_data_session(session, 49, built_at)
        return {
            "as_of_utc": built_at.isoformat(),
            "session": block,
            "candidates": [{"symbol": "VEON", "direction": "WATCH", "gap_pct": 5.35}],
            "pulse": {"summary": "recap", "kind": "last_session_recap"},
            "flags": {"pulse_used": True},
            "status": {"state": "OK", "last_full_cycle_time": built_at.isoformat(),
                       "last_success_time": built_at.isoformat()},
        }

    def _run(self, agent, now, session, enabled=True):
        calls = {"movers": 0, "llm": 0}

        def _movers(cfg):
            calls["movers"] += 1
            return [_mover()], 30

        with patch("agents.premarket_scanner_agent._utc_now", return_value=now), \
             patch.object(agent, "_is_enabled", return_value=enabled), \
             patch.object(PremarketScannerAgent, "_is_us_market_open", return_value=False), \
             patch.object(PremarketScannerAgent, "_session_info", return_value=dict(session)), \
             patch.object(agent, "_get_config", return_value=_cfg()), \
             patch.object(agent, "_load_movers", side_effect=_movers), \
             patch.object(agent, "_load_news_items", return_value=([], 10, False)), \
             patch.object(agent, "_load_earnings", return_value=([], 10, False)), \
             patch.object(agent, "_get_watchlist_symbols", return_value=[]), \
             patch.object(agent, "_is_excluded_symbol", return_value=False), \
             patch.object(agent, "_persist_locked"), \
             patch("agents.premarket_scanner_agent.update_agent_status"), \
             patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.get_best_model.return_value = None
            mock_ol.is_cloud_host.return_value = False
            mock_ol.check_availability.return_value = False
            mock_ol.generate.side_effect = lambda *a, **k: calls.__setitem__("llm", calls["llm"] + 1) or {}
            out = agent.run_cycle()
        return out, calls

    def test_weekend_tick_with_current_recap_spends_nothing(self):
        agent = _bare_agent()
        built = SUNDAY - timedelta(minutes=5)
        agent._state = self._recap_state(built)
        out, calls = self._run(agent, SUNDAY, CLOSED_SESSION)
        assert calls == {"movers": 0, "llm": 0}
        # Data and its timestamp are untouched: nothing is re-stamped "now".
        assert out["as_of_utc"] == built.isoformat()
        assert out["candidates"][0]["symbol"] == "VEON"
        assert out["pulse"]["kind"] == "last_session_recap"
        assert out["status"]["last_run_time"] == SUNDAY.isoformat()
        assert out["flags"]["idle_market_closed"] is True
        # Real next refresh: recap rebuild 6h after it was built (pre-market is later).
        assert out["status"]["next_refresh_at_utc"] == (built + timedelta(seconds=21600)).isoformat()
        assert out["status"]["next_run_in"] == 21600 - 300
        assert out["session"]["quotes_as_of_utc"] == "2026-11-14T01:00:00+00:00"

    def test_first_weekend_cycle_builds_the_recap_once(self):
        agent = _bare_agent()   # no stored session -> must build
        out, calls = self._run(agent, SUNDAY, CLOSED_SESSION)
        assert calls["movers"] == 1
        assert out["session"]["data_kind"] == pm.DATA_KIND_LAST_SESSION
        assert out["as_of_utc"] == SUNDAY.isoformat()
        assert out["status"]["last_full_cycle_time"] == SUNDAY.isoformat()
        assert out["status"]["next_refresh_at_utc"] == (SUNDAY + timedelta(seconds=21600)).isoformat()
        # ... and the very next tick is idle.
        later = SUNDAY + timedelta(minutes=5)
        later_session = mh.us_equity_session_snapshot(later)
        _, calls2 = self._run(agent, later, later_session)
        assert calls2["movers"] == 0

    def test_manual_refresh_forces_a_rebuild_while_closed(self):
        agent = _bare_agent()
        agent._state = self._recap_state(SUNDAY - timedelta(minutes=5))
        with patch.object(agent, "_is_enabled", return_value=True):
            assert agent.request_refresh() == {"status": "accepted"}
        assert agent._force_full_cycle is True
        _, calls = self._run(agent, SUNDAY, CLOSED_SESSION)
        assert calls["movers"] == 1
        assert agent._force_full_cycle is False

    def test_self_heal_wake_does_not_force_or_drop_the_pulse(self):
        agent = _bare_agent()
        agent._premarket_pulse_cache = ("ts", {"summary": "x"}, "fp")
        with patch.object(agent, "_is_enabled", return_value=True):
            agent.request_refresh(force=False)
        assert agent._force_full_cycle is False
        assert agent._premarket_pulse_cache is not None
        assert agent._wake_event.is_set()

    def test_recap_expires_after_closed_session_refresh(self):
        agent = _bare_agent()
        agent._state = self._recap_state(SUNDAY - timedelta(seconds=21601))
        _, calls = self._run(agent, SUNDAY, CLOSED_SESSION)
        assert calls["movers"] == 1

    def test_premarket_open_ends_the_idle(self):
        agent = _bare_agent()
        agent._state = self._recap_state(SUNDAY)
        out, calls = self._run(agent, PREMARKET_NOW, PREMARKET_SESSION)
        assert calls["movers"] == 1
        assert out["session"]["data_kind"] == pm.DATA_KIND_PREMARKET
        # Live session: normal cadence.
        assert out["status"]["next_refresh_at_utc"] == (PREMARKET_NOW + timedelta(seconds=300)).isoformat()

    def test_recap_of_an_older_session_is_rebuilt(self):
        agent = _bare_agent()
        monday_night = datetime(2026, 11, 17, 2, 0, tzinfo=UTC)          # Mon 21:00 ET
        agent._state = self._recap_state(monday_night - timedelta(minutes=5))   # still Friday's recap
        agent._state["session"]["data_session_date"] = "2026-11-13"
        _, calls = self._run(agent, monday_night, mh.us_equity_session_snapshot(monday_night))
        assert calls["movers"] == 1

    def test_empty_recap_retries_sooner_but_not_every_tick(self):
        agent = _bare_agent()
        agent._state = self._recap_state(SUNDAY - timedelta(minutes=5))
        agent._state["candidates"] = []
        _, calls = self._run(agent, SUNDAY, CLOSED_SESSION)
        assert calls["movers"] == 0                      # 5 min old: wait
        agent._state["status"]["last_full_cycle_time"] = (SUNDAY - timedelta(minutes=16)).isoformat()
        _, calls = self._run(agent, SUNDAY, CLOSED_SESSION)
        assert calls["movers"] == 1                      # 16 min old: retry

    def test_next_refresh_never_passes_the_premarket_open(self):
        late = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)                  # Mon 01:00 ET
        session = mh.us_equity_session_snapshot(late)
        due = PremarketScannerAgent._next_refresh_at(session, _cfg(), late, late)
        assert due == datetime(2026, 11, 16, 9, 0, tzinfo=UTC)           # 04:00 ET, not +6h
        live = PremarketScannerAgent._next_refresh_at(PREMARKET_SESSION, _cfg(), PREMARKET_NOW, PREMARKET_NOW)
        assert live == PREMARKET_NOW + timedelta(seconds=300)

    def test_get_state_counts_down_to_the_real_next_refresh(self):
        agent = _bare_agent()
        agent._state = self._recap_state(SUNDAY - timedelta(minutes=5))
        agent._state["status"]["last_run_time"] = SUNDAY.isoformat()
        agent._state["status"]["next_refresh_at_utc"] = (SUNDAY + timedelta(hours=2)).isoformat()
        with patch("agents.premarket_scanner_agent._utc_now", return_value=SUNDAY + timedelta(minutes=30)), \
             patch.object(agent, "_is_enabled", return_value=True), \
             patch.object(PremarketScannerAgent, "_is_us_market_open", return_value=False), \
             patch.object(PremarketScannerAgent, "_session_info", return_value=dict(CLOSED_SESSION)), \
             patch.object(agent, "_get_config", return_value=_cfg()):
            out = agent.get_state()
        assert out["status"]["next_run_in"] == 90 * 60          # not "300"
        assert out["session"]["data_kind"] == pm.DATA_KIND_LAST_SESSION


class TestFrozenSnapshotKeepsItsTimestamp:
    def test_market_open_tick_does_not_restamp_as_of(self):
        agent = _bare_agent()
        built = "2026-11-16T14:25:00+00:00"
        agent._state = {
            "as_of_utc": built,
            "status": {},
            "flags": {},
            "candidates": [{"symbol": "VEON", "direction": "LONG"}],
            "snapshot_candidates": [],
            "pulse": {"summary": "x"},
        }
        now = datetime(2026, 11, 16, 18, 40, tzinfo=UTC)                 # 13:40 ET, session open
        with patch("agents.premarket_scanner_agent._utc_now", return_value=now), \
             patch.object(agent, "_is_enabled", return_value=True), \
             patch.object(PremarketScannerAgent, "_is_us_market_open", return_value=True), \
             patch.object(agent, "_get_config", return_value=_cfg()), \
             patch.object(agent, "_persist_locked"), \
             patch("agents.premarket_scanner_agent.update_agent_status"):
            out = agent.run_cycle()
        assert out["as_of_utc"] == built
        assert out["snapshot_candidates"][0]["symbol"] == "VEON"
        assert out["status"]["last_run_time"] == now.isoformat()
        # Scanning resumes at the 16:00 ET close (21:00 UTC).
        assert out["status"]["next_refresh_at_utc"] == "2026-11-16T21:00:00+00:00"


# ---------------------------------------------------------------------------
# AI prompts carry the true session
# ---------------------------------------------------------------------------
VALID_PULSE = (
    '{"summary":"Miners ended Friday lower.","sentiment":"bearish",'
    '"top_long":[{"symbol":"VEON","reason":"up move"}],"top_short":[],"risks":"thin data"}'
)


def _pulse_candidates():
    return [{"symbol": "VEON", "direction": "WATCH", "gap_pct": 5.35,
             "premarket_score_long": 43, "premarket_score_short": 23,
             "catalysts": [], "news": []}]


class TestSessionAwareAi:
    def _pulse(self, session):
        agent = _bare_agent()
        seen = {}

        def _gen(prompt, model=None, system="", timeout_override=None, **_kw):
            seen["prompt"], seen["system"] = prompt, system
            return {"response": VALID_PULSE, "_meta": {"model": "m"}}

        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.is_cloud_host.return_value = True
            mock_ol.get_best_model.return_value = "m"
            mock_ol.generate.side_effect = _gen
            pulse = agent._compute_premarket_pulse(_cfg(), _pulse_candidates(), session=session)
        return pulse, seen

    def test_weekend_pulse_is_a_last_session_recap(self):
        session = PremarketScannerAgent._describe_data_session(CLOSED_SESSION, 49, SUNDAY)
        pulse, seen = self._pulse(session)
        assert pulse["kind"] == "last_session_recap"
        assert pulse["session_state"] == "CLOSED"
        assert pulse["data_session_date"] == "2026-11-13"
        assert pulse["data_age_seconds"] == session["quotes_age_seconds"]
        assert "CLOSED" in seen["system"] and "NO pre-market session" in seen["system"]
        assert "senior pre-market analyst" not in seen["system"]
        assert "Today's pre-market candidates" not in seen["prompt"]
        first = seen["prompt"].splitlines()[0]
        assert "CLOSED (weekend)" in first and "NO pre-market session" in first
        assert "2026-11-13" in first and "hours old" in first
        assert "Mon 2026-11-16 09:30 ET" in first
        # Format contract kept for every variant.
        assert "{symbol,reason}" in seen["system"] and "do not invent" in seen["system"]
        # "No news attached" is stated, not left for the model to fill in.
        assert "found NO catalyst or news" in seen["prompt"]

    def test_premarket_pulse_states_the_session(self):
        session = PremarketScannerAgent._describe_data_session(PREMARKET_SESSION, 40, PREMARKET_NOW)
        pulse, seen = self._pulse(session)
        assert pulse["kind"] == "premarket"
        assert "senior pre-market analyst" in seen["system"]
        assert seen["prompt"].startswith("Session: US pre-market")
        assert "Today's pre-market candidates" in seen["prompt"]

    def test_after_hours_pulse(self):
        now = datetime(2026, 11, 16, 22, 0, tzinfo=UTC)                  # 17:00 ET
        session = PremarketScannerAgent._describe_data_session(mh.us_equity_session_snapshot(now), 40, now)
        pulse, seen = self._pulse(session)
        assert pulse["kind"] == "after_hours"
        assert "not pre-market gaps" in seen["system"]
        assert seen["prompt"].startswith("Session: US after-hours")

    def test_pulse_cache_is_keyed_by_session(self):
        agent = _bare_agent()
        closed = PremarketScannerAgent._describe_data_session(CLOSED_SESSION, 49, SUNDAY)
        live = PremarketScannerAgent._describe_data_session(PREMARKET_SESSION, 40, PREMARKET_NOW)
        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.is_cloud_host.return_value = True
            mock_ol.get_best_model.return_value = "m"
            mock_ol.generate.return_value = {"response": VALID_PULSE, "_meta": {"model": "m"}}
            first = agent.get_premarket_pulse(_cfg(), _pulse_candidates(), session=closed)
            again = agent.get_premarket_pulse(_cfg(), _pulse_candidates(), session=closed)
            other = agent.get_premarket_pulse(_cfg(), _pulse_candidates(), session=live)
        assert first["kind"] == "last_session_recap" and again is first
        assert other["kind"] == "premarket"          # same book, new session -> rewritten
        assert mock_ol.generate.call_count == 2

    def test_ticker_prompt_states_the_closed_session(self):
        agent = _bare_agent()
        session = PremarketScannerAgent._describe_data_session(CLOSED_SESSION, 49, SUNDAY)
        seen = {}

        def _gen(prompt, model=None, timeout_override=None):
            seen["prompt"] = prompt
            return {"response": '{"catalyst":"none","summary":"No headline.","confidence":0.8}'}

        cand = {"symbol": "VEON", "gap_pct": 5.35, "news": [], "earnings": {}}
        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.generate.side_effect = _gen
            out = agent._ollama_enrich_candidate(cand, "m", 6, session=session)
        assert out["catalyst"] == "none"
        assert "the US stock market is CLOSED (weekend)" in seen["prompt"]
        assert "Headlines: none" in seen["prompt"]
        assert "do not guess a cause" in seen["prompt"]

    def test_none_catalyst_is_not_stored_as_a_tag(self):
        agent = _bare_agent()
        cands = [
            {"symbol": "AAA", "premarket_score_long": 60, "premarket_score_short": 5, "catalysts": ["EARNINGS"]},
            {"symbol": "BBB", "premarket_score_long": 50, "premarket_score_short": 5, "catalysts": []},
        ]

        def _enrich(cand, model, timeout_sec, session=None):
            if cand["symbol"] == "AAA":
                return {"catalyst": "none", "summary": "nothing found", "confidence": 0.8}
            return {"catalyst": "FDA approval", "summary": "approval", "confidence": 0.9}

        with patch("agents.premarket_scanner_agent.ollama") as mock_ol, \
             patch.object(agent, "_ollama_enrich_candidate", side_effect=_enrich):
            mock_ol.get_best_model.return_value = "m"
            assert agent._apply_ollama(cands, _cfg()) is True
        assert cands[0]["catalysts"] == ["EARNINGS"]          # no "NONE" chip
        assert cands[0]["ollama"]["summary"] == "nothing found"
        assert cands[1]["catalysts"] == ["FDA_APPROVAL"]


# ---------------------------------------------------------------------------
# Scoring: report-only checks (thresholds are NOT changed by this audit)
# ---------------------------------------------------------------------------
class TestDirectionThresholdsAreReachable:
    def test_big_gap_without_any_catalyst_reaches_long_and_short(self):
        agent = _bare_agent()
        up = _build(agent, [_mover("UP", price=112.0, prev=100.0)])["candidates"][0]
        down = _build(agent, [_mover("DOWN", price=88.0, prev=100.0)])["candidates"][0]
        assert up["direction"] == "LONG" and up["premarket_score_long"] >= 58
        assert down["direction"] == "SHORT" and down["premarket_score_short"] >= 58

    def test_weekend_sized_moves_stay_watch(self):
        # The live Sunday book: largest move +5.35% with no catalyst -> WATCH.
        cand = _build(_bare_agent(), [_mover("VEON")])["candidates"][0]
        assert cand["direction"] == "WATCH"
        assert cand["premarket_score_long"] < 58


# ---------------------------------------------------------------------------
# API: /api/premarket state build must not leak a thread per timeout
# ---------------------------------------------------------------------------
class TestPremarketStateBuildThread:
    def _threads(self):
        return [t for t in threading.enumerate() if t.name == "premarket-state-build" and t.is_alive()]

    def test_timed_out_requests_share_one_worker(self):
        import api.main as main

        release = threading.Event()
        started = {"n": 0}

        def _slow_build():
            started["n"] += 1
            release.wait(timeout=10.0)
            return ("agent", {"candidates": []})

        with patch.object(main, "_PREMARKET_BUILD_INFLIGHT", None):
            try:
                for _ in range(5):
                    assert main._premarket_state_build(_slow_build, 0.05) == (False, None)
                # Five timed-out requests, ONE build, ONE thread.
                assert started["n"] == 1
                assert len(self._threads()) == 1
            finally:
                release.set()
            deadline = time.time() + 5.0
            while self._threads() and time.time() < deadline:
                time.sleep(0.02)
            assert self._threads() == []
            # Once the stuck build has finished a new request builds again.
            finished, built = main._premarket_state_build(lambda: ("agent", {"candidates": [1]}), 2.0)
            assert finished is True and built[1] == {"candidates": [1]}

    def test_build_error_reaches_the_request(self):
        import api.main as main

        def _boom():
            raise RuntimeError("state build failed")

        with patch.object(main, "_PREMARKET_BUILD_INFLIGHT", None):
            with pytest.raises(RuntimeError, match="state build failed"):
                main._premarket_state_build(_boom, 2.0)


# ---------------------------------------------------------------------------
# Served / persisted payload keeps what the cycle selected
# ---------------------------------------------------------------------------
class TestServedCandidateCap:
    def test_watchlist_rows_do_not_push_movers_out_of_the_served_payload(self):
        agent = _bare_agent()
        watch = [{"symbol": f"W{i:02d}", "direction": "WATCH", "is_watchlist": True} for i in range(30)]
        movers = [{"symbol": f"M{i:02d}", "direction": "LONG", "is_watchlist": False} for i in range(5)]
        out = agent._sanitize_state_payload({"candidates": watch + movers}, _cfg(max_results=30))
        symbols = [c["symbol"] for c in out["candidates"]]
        # _build_payload allows watchlist + max_results; serving must not cut
        # that back to a flat 30 and drop every non-watchlist mover.
        assert len(symbols) == 35 and "M04" in symbols
        assert len(out["long_candidates"]) == 5 and len(out["watch_only"]) == 30

    def test_cap_without_watchlist_is_unchanged(self):
        agent = _bare_agent()
        rows = [{"symbol": f"S{i:03d}", "direction": "WATCH"} for i in range(50)]
        out = agent._sanitize_state_payload({"candidates": rows}, _cfg(max_results=30))
        assert len(out["candidates"]) == 30


# ---------------------------------------------------------------------------
# News window follows the last session, not just the wall clock
# ---------------------------------------------------------------------------
class TestNewsWindowAcrossClosedDays:
    def test_sunday_window_reaches_back_to_fridays_session(self):
        cutoff = PremarketScannerAgent._news_cutoff(SUNDAY, 24, CLOSED_SESSION)
        # Friday's regular close was 16:00 EST = 21:00 UTC; window opens 8h earlier.
        assert cutoff == datetime(2026, 11, 13, 13, 0, tzinfo=UTC)
        # A Friday-evening headline (after-hours catalyst) is inside the window;
        # the plain 24h rule (Sat 07:55 UTC) threw it away.
        friday_evening = datetime(2026, 11, 13, 22, 30, tzinfo=UTC)
        assert friday_evening >= cutoff
        assert friday_evening < SUNDAY - timedelta(hours=24)

    def test_monday_premarket_keeps_weekend_and_friday_close_news(self):
        cutoff = PremarketScannerAgent._news_cutoff(PREMARKET_NOW, 24, PREMARKET_SESSION)
        assert cutoff == datetime(2026, 11, 13, 13, 0, tzinfo=UTC)

    def test_midweek_window_is_the_configured_lookback(self):
        tuesday = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)               # Tue 05:00 ET
        cutoff = PremarketScannerAgent._news_cutoff(tuesday, 24, mh.us_equity_session_snapshot(tuesday))
        assert cutoff == tuesday - timedelta(hours=24)                    # Mon close - 8h is later

    def test_no_session_keeps_the_legacy_window(self):
        assert PremarketScannerAgent._news_cutoff(SUNDAY, 24, None) == SUNDAY - timedelta(hours=24)

    def test_load_news_items_applies_the_session_window(self):
        friday_evening = datetime(2026, 11, 13, 22, 30, tzinfo=UTC).isoformat()

        class _News:
            def get_news_tab_state(self, active_tickers=None):
                return {"last_update": SUNDAY.isoformat()}

            def get_all_articles_flat(self, active_tickers=None):
                return [{"ticker": "VEON", "title": "VEON raises guidance", "published_at": friday_evening}]

        agent = _bare_agent()
        agent._news_refresh_lock = threading.Lock()
        agent._news_refresh_inflight = False
        agent._last_news_refresh_ts = time.time()          # cooldown active: no remote refresh
        with patch("agents.premarket_scanner_agent._utc_now", return_value=SUNDAY), \
             patch("agents.premarket_scanner_agent.get_news_tab_agent_if_ready", return_value=_News()), \
             patch.object(agent, "_is_enabled", return_value=True):
            plain, _, _ = agent._load_news_items(_cfg(), ["VEON"])
            with_session, _, available = agent._load_news_items(_cfg(), ["VEON"], session=dict(CLOSED_SESSION))
        assert plain == []                                  # the old behaviour
        assert [r["title"] for r in with_session] == ["VEON raises guidance"]
        assert available is True
