"""Hot List tab audit 2026-11-15 — regression tests.

Live evidence (Sunday 2026-11-15 09:54):
- cards showed NVDA $225.25 / BMNR $24.71 / MARA $11.81 (engine snapshot rows
  last updated 2026-09-08) while the Watchlist showed $161.01 / $17.27 / $6.83;
- every card printed AVOID / RISK HIGH / Risk Heat 95% / "TOP RISK: momentum
  unknown" with ADX, momentum and the earnings history all missing;
- MARA had "Priority -1.2"; the dossier said "Next earnings 2026-11-03 · in
  -12 days"; the market-heat strip stayed on "Loading market heat...".
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api import hotlist_service
from core import hotlist_scoring as hs

pytestmark = pytest.mark.unit

# Sunday 2026-11-15 08:05 UTC — US market closed, last close Fri 2026-11-13 21:00 UTC.
SUNDAY = datetime(2026, 11, 15, 8, 5, tzinfo=timezone.utc)
STALE_ENGINE_ROW = {
    "price": 225.25,
    "change_pct": -2.218,
    "last_update": "2026-09-08T22:56:46.578656+03:00",
    "data_source": "yahoo",
    "trend_1d": "BULL",
    "trend_4h": "BULL",
    "rsi": 38.9,
}
FRESH_LIVE_ROW = {
    "price": 161.01,
    "change_pct": -3.12,
    "live": False,
    "source": "yahoo",
    "timestamp": "2026-11-15T08:00:58.896333+00:00",
}


@pytest.fixture(autouse=True)
def _real_market_calendar(monkeypatch):
    """conftest forces MS_MARKET_HOURS_OVERRIDE=open; these tests need the real calendar."""
    monkeypatch.delenv("MS_MARKET_HOURS_OVERRIDE", raising=False)


def _news(pos=0, neg=0, seven_d=0, risk_events=0):
    signals = hs.default_news_signals()
    signals["counts"].update({"positive": pos, "negative": neg, "7d": seven_d, "risk_events": risk_events})
    signals["risk_events"] = [f"headline {i}" for i in range(risk_events)]
    return signals


def _scored(item, news=None, market=None, **guard_kwargs):
    news = news or _news()
    data = dict(item)
    data.update(hs.compute_earnings_edge_score(data, news, market or {}))
    data.update(hs.compute_hotlist_composite_metrics(data, news, market or {}))
    data["news_signals"] = news
    return hs.apply_hotlist_data_guard(data, **guard_kwargs)


# ───────────────────────────── quote freshness ─────────────────────────────

class TestQuoteResolution:
    def test_fresh_live_quote_beats_two_month_old_engine_row(self):
        quote = hs.resolve_hotlist_quote(
            STALE_ENGINE_ROW, FRESH_LIVE_ROW, {"price": 225.25, "change_pct": -2.218}, now=SUNDAY,
        )
        assert quote["price"] == pytest.approx(161.01)
        assert quote["change_pct"] == pytest.approx(-3.12)
        assert quote["price_origin"] == "live_quote"
        assert quote["price_stale"] is False
        assert quote["price_as_of"].startswith("2026-11-15T08:00:58")
        assert quote["price_age_seconds"] == pytest.approx(241.1, abs=0.2)

    def test_only_an_old_engine_row_is_reported_stale_with_its_age(self):
        quote = hs.resolve_hotlist_quote(STALE_ENGINE_ROW, {}, {"price": 225.25}, now=SUNDAY)
        assert quote["price"] == pytest.approx(225.25)
        assert quote["price_stale"] is True
        assert quote["price_origin"] == "engine_snapshot"
        assert quote["price_age_seconds"] > 60 * 86400  # 68 days, never "current"

    def test_missing_change_stays_none_not_zero(self):
        live = {"price": 17.56, "change_pct": None, "source": "eToro_bid_ask_mid",
                "timestamp": "2026-11-15T07:53:24+00:00"}
        quote = hs.resolve_hotlist_quote({"price": 24.71, "change_pct": -1.04,
                                          "last_update": "2026-09-08T22:45:39+03:00"}, live, None, now=SUNDAY)
        assert quote["price"] == pytest.approx(17.56)
        assert quote["change_pct"] is None

    def test_no_quote_at_all(self):
        quote = hs.resolve_hotlist_quote({}, {}, None, now=SUNDAY)
        assert quote["price"] is None
        assert quote["price_stale"] is True

    def test_weekend_quote_from_the_last_close_is_current(self):
        # Friday close is 21:00 UTC; a quote taken at/after 20:40 UTC is the close.
        assert hs.last_us_equity_close(SUNDAY) == datetime(2026, 11, 13, 21, 0, tzinfo=timezone.utc)
        assert hs.is_hotlist_quote_stale("2026-11-13T20:45:00+00:00", now=SUNDAY) is False
        assert hs.is_hotlist_quote_stale("2026-11-13T18:00:00+00:00", now=SUNDAY) is True
        assert hs.is_hotlist_quote_stale(None, now=SUNDAY) is True

    def test_crypto_quote_goes_stale_after_twenty_minutes(self):
        assert hs.is_hotlist_quote_stale("2026-11-15T07:50:00+00:00", now=SUNDAY, always_open=True) is False
        assert hs.is_hotlist_quote_stale("2026-11-15T07:30:00+00:00", now=SUNDAY, always_open=True) is True

    def test_engine_technicals_that_missed_a_session_are_stale(self):
        assert hs.is_hotlist_technicals_stale("2026-09-08T22:56:46+03:00", now=SUNDAY) is True
        assert hs.is_hotlist_technicals_stale("2026-11-13T19:00:00+00:00", now=SUNDAY) is False
        assert hs.is_hotlist_technicals_stale("", now=SUNDAY) is True


# ───────────────────────────── honest verdict ─────────────────────────────

class TestDataGuard:
    def test_missing_inputs_give_insufficient_data_not_avoid(self):
        """NVDA live case: stale price + stale trends + no bars -> was AVOID / RISK HIGH."""
        item = {
            "ticker": "NVDA", "price": 225.25, "price_stale": True,
            "trend_1d": "BULL", "trend_4h": "BULL", "technicals_stale": True,
            "earnings_days": 2, "earnings_date": "2026-11-17", "earnings_when": "AMC",
        }
        out = _scored(item, _news(pos=22, neg=22, seven_d=23, risk_events=2))
        assert out["earnings_play"] == "INSUFFICIENT_DATA"
        assert out["earnings_setup"] == "N/A"
        assert out["verdict_status"] == "insufficient_data"
        assert {"price", "trend", "momentum", "adx", "earnings_history"} <= set(out["missing_inputs"])
        # Risk factors exclude data gaps; Top Risk is a real risk, not "momentum unknown".
        assert out["risk_factor_flags"] == ["risk_events_present"]
        assert "momentum_unknown" in out["data_gap_flags"]
        assert "price_stale" in out["data_gap_flags"] and "technicals_stale" in out["data_gap_flags"]
        assert out["top_risk"] == "risk_events_present"
        # Confidence is scaled by coverage, no longer a copy of the overall score.
        assert out["data_coverage_pct"] < 50
        assert out["confidence_score"] == int(round(out["overall_score"] * out["data_coverage_pct"] / 100.0)) or \
            abs(out["confidence_score"] - out["overall_score"] * out["data_coverage_pct"] / 100.0) <= 1.0
        assert out["confidence_score"] < out["overall_score"]

    def test_only_gap_flags_never_become_the_top_risk(self):
        out = _scored({"ticker": "BMNR", "price": 24.7, "trend_1d": "BULL", "trend_4h": "BEAR",
                       "earnings_days": 58, "earnings_date": "2027-01-12"}, _news(pos=3, neg=1, seven_d=4))
        assert out["verdict_status"] == "insufficient_data"
        assert out["risk_factor_flags"] == []
        assert out["top_risk"].startswith("not assessable")
        assert "unknown" not in out["top_risk"]

    def test_complete_inputs_keep_the_computed_verdict(self):
        item = {
            "ticker": "AAPL", "price": 200.0, "price_stale": False,
            "trend_1d": "BULL", "trend_4h": "BULL", "technicals_stale": False,
            "earnings_days": 1, "earnings_date": "2026-11-16", "earnings_when": "AMC",
            "momentum_1w": 4.0, "momentum_1m": 6.0, "adx": 38.0,
            "avg_earnings_move_8q": 6.0, "atr_percentile": 70.0,
        }
        out = _scored(item, _news(pos=8, neg=1, seven_d=9), {"adx": 38.0})
        assert out["verdict_status"] == "ok"
        assert out["missing_inputs"] == []
        assert out["data_coverage_pct"] == 100
        assert out["earnings_play"] in {"PLAY_PRE", "WAIT", "AVOID"}
        assert out["earnings_setup"] in {"A", "B", "C"}
        assert out["confidence_score"] == int(round(out["overall_score"]))

    def test_past_earnings_have_no_earnings_play(self):
        """MARA live case: earnings 12 days ago, no next date -> was AVOID."""
        item = {
            "ticker": "MARA", "price": 6.83, "price_stale": False,
            "trend_1d": "BEAR", "trend_4h": "BEAR",
            "earnings_days": -12, "earnings_date": "2026-11-03",
            "momentum_1w": 10.08, "momentum_1m": -16.09, "adx": 38.7,
        }
        out = _scored(item, _news(pos=2, neg=6, seven_d=10, risk_events=2), {"adx": 38.7})
        assert out["verdict_status"] == "no_upcoming_earnings"
        assert out["earnings_play"] == "N/A"
        assert out["earnings_setup"] == "N/A"

    def test_post_earnings_window_still_gets_a_play(self):
        item = {"ticker": "X", "price": 10.0, "earnings_days": -1, "earnings_date": "2026-11-14",
                "momentum_1w": 3.0, "momentum_1m": 5.0, "adx": 30.0}
        out = _scored(item, _news(pos=5, seven_d=5), {"adx": 30.0})
        assert out["verdict_status"] == "ok"
        assert out["earnings_play"] in {"PLAY_POST", "WAIT", "AVOID"}
        # -1 with a real date is "reported yesterday", not the "no date" sentinel
        assert "earnings_date_unknown" not in out["earnings_risk_flags"]
        assert "earnings_date" not in out["missing_inputs"]
        assert out["time_weight"] == 32.0

    def test_minus_one_without_a_date_is_still_unknown(self):
        scored = hs.compute_earnings_edge_score({"earnings_days": -1}, _news(), {})
        assert "earnings_date_unknown" in scored["earnings_risk_flags"]
        scored = hs.compute_earnings_edge_score({"earnings_days": -1, "earnings_date": "N/A"}, _news(), {})
        assert "earnings_date_unknown" in scored["earnings_risk_flags"]
        assert hs.compute_hotlist_composite_metrics({"earnings_days": -1})["time_weight"] == 12.0
        out = _scored({"ticker": "Y", "price": 10.0, "earnings_days": -1, "earnings_date": "N/A",
                       "momentum_1w": 1.0, "adx": 30.0}, _news(pos=1, seven_d=1), {"adx": 30.0})
        assert out["verdict_status"] == "no_upcoming_earnings"
        assert out["earnings_play"] == "N/A"

    def test_crypto_has_no_earnings_inputs(self):
        data = {"ticker": "BTC-USD", "price": 60000.0, "momentum_1w": 2.0, "adx": 27.0,
                "trend_1d": "BULL", "earnings_play": "N/A", "earnings_setup": "N/A",
                "earnings_days": -1, "earnings_date": "N/A", "overall_score": 60.0,
                "top_risk": "N/A — crypto (no earnings)", "earnings_risk_flags": [],
                "news_signals": _news(pos=1, seven_d=1)}
        out = hs.apply_hotlist_data_guard(data, has_earnings_cycle=False)
        assert out["verdict_status"] == "ok"
        assert out["earnings_play"] == "N/A"
        assert "earnings_date" not in out["missing_inputs"]
        assert out["top_risk"] == "N/A — crypto (no earnings)"

    def test_defaults_preserve_new_fields(self):
        out = hs.apply_hotlist_defaults({
            "ticker": "T", "earnings_play": "INSUFFICIENT_DATA", "earnings_setup": "N/A",
            "verdict_status": "insufficient_data", "missing_inputs": ["price"],
            "earnings_history_summary": {"last_8": {"up_count": 1, "down_count": 0, "avg_gap_pct": 6.3},
                                         "sample_size": 1},
        })
        assert out["earnings_play"] == "INSUFFICIENT_DATA"
        assert out["earnings_setup"] == "N/A"
        assert out["verdict_status"] == "insufficient_data"
        # the card can now label "Earnings History (8Q)" with the real sample
        assert out["earnings_history_summary"]["sample_size"] == 1


class TestPriorityBounds:
    def test_priority_is_never_negative(self):
        """MARA: time_weight 8 * 0.9 + 0.4*29 - 20 = -1.2 -> floor at 0."""
        out = hs.compute_hotlist_composite_metrics(
            {"earnings_edge_score": 29, "risk_level": "high", "earnings_days": -12, "earnings_when": ""},
        )
        assert out["priority_score"] == 0.0
        worst = hs.compute_hotlist_composite_metrics(
            {"earnings_edge_score": 0, "risk_level": "high", "earnings_days": 40},
        )
        assert worst["priority_score"] >= 0.0

    def test_priority_order_is_unchanged_above_the_floor(self):
        near = hs.compute_hotlist_composite_metrics({"earnings_edge_score": 50, "earnings_days": 1})
        far = hs.compute_hotlist_composite_metrics({"earnings_edge_score": 50, "earnings_days": 30})
        assert near["priority_score"] > far["priority_score"] > 0


# ───────────────────────── state_service hot-list build ─────────────────────────

class TestFastBuild:
    def _build(self, live_row):
        import api.state_service as ss

        config = SimpleNamespace(hot_list=["NVDA"], config={})
        state_data = {"tickers": {"NVDA": dict(STALE_ENGINE_ROW)}}
        watchlist_rows = [{"ticker": "NVDA", "price": 225.25, "change_pct": -2.218,
                           "trend_1d": "BULL", "trend_4h": "BULL", "rsi": 38.9}]
        with patch("agents.economic_calendar_agent.get_economic_calendar_agent", return_value=object()), \
             patch.object(ss, "_resolve_earnings_from_sources",
                          return_value=("2026-11-17", 2, "AMC", "After Hours")), \
             patch.object(ss, "summarize_ticker_news_signals", return_value=_news(pos=2, neg=2, seven_d=4)), \
             patch.object(ss, "_hotlist_live_price_row", return_value=live_row), \
             patch.object(ss, "_hotlist_adx", return_value=None), \
             patch.object(ss, "_hotlist_momentum", return_value={"momentum_1w": None, "momentum_1m": None}), \
             patch.object(ss, "_get_hotlist_earnings_context", return_value={}):
            return ss.build_hot_list_fast(config, state_data, {}, watchlist_rows)[0]

    def test_hot_list_uses_the_fresh_live_quote(self):
        now_iso = datetime.now(timezone.utc).isoformat()
        card = self._build({"price": 161.01, "change_pct": -3.12, "source": "yahoo", "timestamp": now_iso})
        assert card["price"] == pytest.approx(161.01)
        assert card["change_pct"] == pytest.approx(-3.12)
        assert card["price_stale"] is False
        assert card["price_origin"] == "live_quote"
        assert card["price_as_of"]

    def test_stale_engine_price_is_marked_and_not_scored_as_current(self):
        card = self._build({})
        assert card["price"] == pytest.approx(225.25)
        assert card["price_stale"] is True
        assert card["technicals_stale"] is True
        assert card["earnings_play"] == "INSUFFICIENT_DATA"
        assert card["verdict_status"] == "insufficient_data"
        assert card["_data_quality"]["price_stale"] is True
        assert card["_data_quality"]["technicals_stale"] is True
        assert card["_data_quality"]["has_bar_metrics"] is False
        assert card["_data_quality"]["fundamentals_provider"] is False
        # stale BULL/BULL trends must not lift the technical score above neutral
        assert card["technical_alignment"] == pytest.approx(50.0)
        assert "technicals_stale" in card["earnings_risk_flags"]


class TestEarningsContextCache:
    def test_barless_context_is_not_cached(self):
        import api.state_service as ss

        empty = {"atr_percentile": None, "expected_move_pct": None, "avg_earnings_move_8q": None}
        filled = {"atr_percentile": 71.0, "expected_move_pct": 7.2, "avg_earnings_move_8q": 6.3}
        results = [empty, filled]
        with patch.object(ss, "_hotlist_earnings_context", {"ts": time.time(), "data": {}}), \
             patch.object(ss, "_build_hotlist_earnings_context", side_effect=lambda *a, **k: results.pop(0)):
            first = ss._get_hotlist_earnings_context("ZZTEST", {})
            second = ss._get_hotlist_earnings_context("ZZTEST", {})
            third = ss._get_hotlist_earnings_context("ZZTEST", {})  # now cached
        assert first["expected_move_pct"] is None
        assert second["expected_move_pct"] == 7.2  # the 5-minute cache did not pin the empty result
        assert third["expected_move_pct"] == 7.2
        assert results == []


# ───────────────────────────── hotlist_service ─────────────────────────────

class TestLastKnownEarnings:
    def test_store_is_written_atomically_without_a_frozen_countdown(self, tmp_path):
        path = tmp_path / "last_known_earnings.json"
        written = {}

        def fake_atomic(target, data, **kwargs):
            written["path"] = target
            written["data"] = {k: dict(v) for k, v in data.items()}

        with patch.object(hotlist_service, "_LAST_KNOWN_PATH", path), \
             patch.object(hotlist_service, "_LAST_KNOWN_EARNINGS", {}), \
             patch.object(hotlist_service, "_write_json_atomic", side_effect=fake_atomic) as writer:
            info = {"available": True, "source": "watchlist_state", "earnings_date": "2026-11-17",
                    "earnings_when": "AMC", "days_until": 2, "is_past": False}
            hotlist_service._remember_earnings("NVDA", info)
            hotlist_service._remember_earnings("NVDA", dict(info, days_until=1))  # same date -> no rewrite
        assert writer.call_count == 1
        assert written["path"] == path
        assert written["data"]["NVDA"]["earnings_date"] == "2026-11-17"
        assert "days_until" not in written["data"]["NVDA"]
        assert not path.exists()  # nothing bypassed the atomic writer

    def test_past_date_is_last_earnings_with_recomputed_countdown(self, tmp_path):
        with patch.object(hotlist_service, "_read_json", return_value={}), \
             patch.object(hotlist_service, "_remember_earnings"):
            section = hotlist_service._pre_earnings_section(
                "MARA", quote={"earnings_date": "2020-01-10", "earnings_days": 59, "earnings_when": ""},
            )
        assert section["available"] is True
        assert section["is_past"] is True
        assert section["days_until"] < 0  # recomputed from the date, not the stale 59

    def test_remembered_date_gets_a_live_countdown(self, tmp_path):
        remembered = {"MARA": {"available": True, "source": "watchlist_state",
                               "earnings_date": "2020-01-10", "days_until": 59}}
        with patch.object(hotlist_service, "_read_json", return_value={}), \
             patch.object(hotlist_service, "_LAST_KNOWN_EARNINGS", remembered):
            section = hotlist_service._pre_earnings_section("MARA", quote={})
        assert section["source"] == "last_known"
        assert section["is_past"] is True
        assert section["days_until"] < 0
        assert hotlist_service._earnings_countdown("2026-11-17", datetime(2026, 11, 15)) == 2


class TestDossierQuote:
    def test_quote_section_prefers_the_fresh_live_quote(self):
        now_iso = datetime.now(timezone.utc).isoformat()

        def fake_read(path, default):
            name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
            if name == "live_prices_cache.json":
                return {"prices": {"MARA": {"price": 6.83, "change_pct": -5.13, "source": "yahoo",
                                            "timestamp": now_iso}}}
            return {"tickers": {"MARA": {"price": 11.81, "change_pct": 4.42, "trend_1d": "BULL",
                                         "last_update": "2026-09-08T22:56:46+03:00"}}}

        state = {"tickers": [{"ticker": "MARA", "price": 11.81, "change_pct": 4.42, "trend_1d": "BULL"}]}
        with patch("api.state_service.get_state", return_value=state), \
             patch.object(hotlist_service, "_read_json", side_effect=fake_read):
            quote = hotlist_service._quote_section("MARA")
        assert quote["available"] is True
        assert quote["price"] == pytest.approx(6.83)
        assert quote["change_pct"] == pytest.approx(-5.13)  # debate direction now framed on today's move
        assert quote["price_stale"] is False
        assert quote["technicals_stale"] is True

    def test_quote_section_flags_a_stale_engine_price(self):
        def fake_read(path, default):
            name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
            if name == "live_prices_cache.json":
                return {"prices": {}}
            return {"tickers": {"MARA": {"price": 11.81, "change_pct": 4.42,
                                         "last_update": "2026-09-08T22:56:46+03:00"}}}

        with patch("api.state_service.get_state", return_value={"tickers": []}), \
             patch.object(hotlist_service, "_read_json", side_effect=fake_read):
            quote = hotlist_service._quote_section("MARA")
        assert quote["price"] == pytest.approx(11.81)
        assert quote["price_stale"] is True
        assert quote["price_age_seconds"] > 30 * 86400


class TestHeat:
    def test_cold_build_is_single_flight(self):
        calls = {"n": 0}
        payload = {"curated": [], "heat": [], "scanner_available": True, "as_of": "now"}

        def slow_build():
            calls["n"] += 1
            time.sleep(0.25)
            hotlist_service._heat_cache["at"] = time.monotonic()
            hotlist_service._heat_cache["payload"] = payload
            return payload

        results = []
        with patch.object(hotlist_service, "_heat_cache", {"at": 0.0, "payload": None}), \
             patch.object(hotlist_service, "_build_heat_list_uncached", side_effect=slow_build):
            threads = [threading.Thread(target=lambda: results.append(hotlist_service.build_heat_list()))
                       for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        assert calls["n"] == 1  # six concurrent cold callers, one build
        assert len(results) == 6 and all(r is payload for r in results)

    def test_curated_tickers_are_warmed_before_scanner_heat(self):
        warmed = []
        heat_rows = [{"ticker": f"H{i}", "price": 1.0, "change_pct": float(i)} for i in range(12)]
        curated = [{"ticker": "NVDA"}, {"ticker": "MARA"}, {"ticker": "BMNR"}]
        with patch.object(hotlist_service, "_heat_cache", {"at": 0.0, "payload": None}), \
             patch.object(hotlist_service, "_scanner_hot_data",
                          return_value={"available": True, "categories": {"trending": heat_rows}}), \
             patch("core.hotlist_scoring.get_hotlist_state", return_value={"hotlist": curated}), \
             patch("api.state_service._warm_symbol_bars", side_effect=lambda tickers: warmed.append(list(tickers))):
            hotlist_service.build_heat_list()
        assert warmed, "the bar warmer was not started"
        # the warmer keeps only the first 12 symbols: all curated tickers must be inside
        assert {"NVDA", "MARA", "BMNR"} <= set(warmed[0][:12])
        assert warmed[0][:3] == ["NVDA", "MARA", "BMNR"] or set(warmed[0][:3]) == {"NVDA", "MARA", "BMNR"}

    def test_bar_warmup_never_touches_the_network_under_pytest(self):
        with patch("api.state_service._warm_symbol_bars") as warm:
            assert hs.warm_curated_daily_bars(["NVDA", "MARA"]) == 0
        warm.assert_not_called()


class TestStateErrors:
    def test_failed_state_build_is_flagged_not_an_empty_list(self):
        with patch("api.state_service.get_state", side_effect=RuntimeError("boom")):
            payload = hs.get_hotlist_state()
        assert payload["hotlist"] == []
        assert payload["error"] == "hotlist_state_unavailable"


# ───────────────────────── scanner-backed /api/hotlist ─────────────────────────

class TestScannerCategories:
    def test_a_falling_ticker_is_not_listed_as_a_gainer(self):
        from api import main as api_main

        rows = [
            {"symbol": "VEON", "change_pct": 5.35, "price": 79.17, "volume": 10, "source": "yahoo_gainers"},
            {"symbol": "CBRS", "change_pct": -2.5, "price": 106.0, "volume": 20, "source": "yahoo_gainers"},
            {"symbol": "UPL", "change_pct": 1.2, "price": 9.0, "volume": 30, "source": "yahoo_losers"},
            {"symbol": "DWN", "change_pct": -4.0, "price": 9.0, "volume": 40, "source": "yahoo_losers"},
        ]
        normalized = [api_main._normalize_scanner_item(r) for r in rows]
        gainers = api_main._filter_hotlist_items(normalized, category="gainers")
        losers = api_main._filter_hotlist_items(normalized, category="losers")
        trending = api_main._filter_hotlist_items(normalized, category="trending")
        assert [g["ticker"] for g in gainers] == ["VEON"]
        assert [g["ticker"] for g in losers] == ["DWN"]
        assert {t["ticker"] for t in trending} == {"VEON", "CBRS", "UPL", "DWN"}  # nothing hidden

    def test_rows_carry_the_scan_time_not_the_request_time(self):
        from api import main as api_main

        item = api_main._normalize_scanner_item({
            "symbol": "AAPL", "change_pct": 1.0, "price": 200.0, "source": "yahoo_gainers",
            "_as_of_utc": "2026-11-15T06:00:00+00:00",
        })
        assert item["updated"].startswith("2026-11-15T06:00:00")
        fresh = api_main._normalize_scanner_item({"symbol": "AAPL", "change_pct": 1.0, "price": 200.0})
        assert abs(fresh["timestamp"] - time.time()) < 5
