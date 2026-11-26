"""HotList rebuild tests (2026-09-03): ticker dossier, deep analysis, heat list,
and the /api/hotlist/state latency fix (live_prices=False)."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from api import hotlist_service

pytestmark = pytest.mark.unit


class TestDossier:
    def test_ticker_normalized_and_sections_aggregated(self, tmp_path):
        fake_dossier_sections = {
            "quote": {"available": True, "price": 355.5, "change_pct": -3.82},
            "news": {"available": True, "count": 2, "articles": [{"title": "t", "source": "yahoo_rss"}]},
            "trades": {"available": True, "traded": True, "open_positions": [{"position_id": "p1"}],
                       "closed_trades": [], "closed_count": 0, "totals": {"trades": 0},
                       "strategies": []},
            "decisions": {"available": True, "count": 1, "decisions": [{"decision_id": "d1"}]},
            "pre_earnings": {"available": False},
        }

        def fake_section(ticker):
            return fake_dossier_sections["quote"]

        with patch.object(hotlist_service, "_quote_section", side_effect=fake_section), \
             patch.object(hotlist_service, "_news_section", return_value=fake_dossier_sections["news"]), \
             patch.object(hotlist_service, "_trades_section", return_value=fake_dossier_sections["trades"]), \
             patch.object(hotlist_service, "_decisions_section", return_value=fake_dossier_sections["decisions"]), \
             patch.object(hotlist_service, "_pre_earnings_section", return_value=fake_dossier_sections["pre_earnings"]), \
             patch.object(hotlist_service, "build_heat_list", return_value={
                 "curated": [{"ticker": "AVGO", "priority_score": 9}],
                 "heat": [{"ticker": "AVGO", "categories": ["gainers"], "change_pct": -3.82}],
                 "scanner_available": True,
             }), \
             patch.object(hotlist_service, "get_deep_analysis", return_value=None):
            dossier = hotlist_service.build_ticker_dossier(" avgo ", use_cache=False)

        assert dossier["ticker"] == "AVGO"
        assert dossier["in_curated_hotlist"] is True
        assert dossier["hot_context"]["categories"] == ["gainers"]
        assert dossier["quote"]["price"] == 355.5
        assert dossier["trades"]["traded"] is True
        assert dossier["decisions"]["count"] == 1

    def test_dossier_cache_short_circuits(self, tmp_path):
        calls = {"n": 0}
        with patch.object(hotlist_service, "_quote_section", return_value={"available": False}), \
             patch.object(hotlist_service, "build_heat_list", return_value={"curated": [], "heat": [], "scanner_available": False}), \
             patch.object(hotlist_service, "get_deep_analysis", return_value=None):
            def bumped(ticker):
                calls["n"] += 1
                return {"available": False}

            with patch.object(hotlist_service, "_news_section", side_effect=bumped):
                hotlist_service.build_ticker_dossier("TESTZ", use_cache=True)
                hotlist_service.build_ticker_dossier("TESTZ", use_cache=True)
        assert calls["n"] == 1  # second read came from the 60s cache


class TestDeepAnalysis:
    def test_runs_debate_stores_and_caches(self, tmp_path):
        store_path = tmp_path / "deep.json"
        debate_result = {
            "available": True, "prompt_version": "entry_debate_v1",
            "bull": "bull case", "bear": "bear case",
            "verdict": {"recommendation": "avoid", "confidence": 0.7,
                        "rationale": "r", "bull_score": 4, "bear_score": 8},
            "models": {"manager": "fake/mgr"}, "error": "",
        }

        with patch.object(hotlist_service, "DEEP_STORE_PATH", store_path), \
             patch.object(hotlist_service, "build_ticker_dossier", return_value={
                 "ticker": "AVGO", "quote": {"available": True, "change_pct": -3.8},
                 "news": {"available": True, "articles": [{"title": "x", "source": "s"}]},
                 "trades": {"available": True, "closed_trades": [], "totals": {}},
                 "pre_earnings": {"available": False},
                 "hot_context": {"categories": ["losers"]},
             }), \
             patch("core.ai.debate.run_entry_debate", return_value=debate_result) as mock_debate:
            first = hotlist_service.run_deep_analysis("AVGO", config={}, force=True)
            assert first["available"] is True
            assert first["verdict"]["recommendation"] == "avoid"
            assert first["cached"] is False
            # debate evidence carried our data: direction framed SHORT on a loser
            sent_signal = mock_debate.call_args[0][0]
            assert sent_signal["action"] == "SHORT"
            assert sent_signal["news_headlines"]

            second = hotlist_service.run_deep_analysis("AVGO", config={}, force=False)
            assert second["cached"] is True
            assert mock_debate.call_count == 1  # TTL cache short-circuited

            history = json.loads(store_path.read_text(encoding="utf-8"))
            assert history["AVGO"]["available"] is True
            assert len(history["AVGO__history"]) == 1

    def test_failed_debate_is_stored_honestly(self, tmp_path):
        store_path = tmp_path / "deep.json"
        with patch.object(hotlist_service, "DEEP_STORE_PATH", store_path), \
             patch.object(hotlist_service, "build_ticker_dossier", return_value={
                 "ticker": "X", "quote": {"available": False}, "news": {"available": False, "articles": []},
                 "trades": {"available": False}, "pre_earnings": {"available": False},
                 "hot_context": None,
             }), \
             patch("core.ai.debate.run_entry_debate",
                   return_value={"available": False, "bull": "", "bear": "", "verdict": {},
                                 "models": {}, "error": "no_provider", "prompt_version": "v1"}):
            result = hotlist_service.run_deep_analysis("X", config={}, force=True)
        assert result["available"] is False
        assert result["error"] == "no_provider"


class TestHeatList:
    def test_merges_curated_and_scanner_heat(self):
        with patch.object(hotlist_service, "_scanner_hot_data", return_value={
            "available": True,
            "categories": {"gainers": [{"ticker": "abc", "price": 10, "change_pct": 8.1}],
                           "losers": [{"ticker": "ABC", "price": 10, "change_pct": 8.1}]},
        }), \
             patch.object(hotlist_service, "get_deep_analysis", return_value=None):
            from core.hotlist_scoring import sort_hotlist_by_priority
            with patch("core.hotlist_scoring.get_hotlist_state",
                       return_value={"hotlist": [{"ticker": "XYZ"}], "last_update": "ts"}):
                heat = hotlist_service.build_heat_list()
        assert [c["ticker"] for c in heat["curated"]] == ["XYZ"]
        tickers = {r["ticker"]: r for r in heat["heat"]}
        # same ticker in two categories is merged once with both categories
        assert tickers["ABC"]["categories"] == ["gainers", "losers"]
        assert heat["scanner_available"] is True


class TestStateLatencyFix:
    def test_get_hotlist_state_is_cache_only(self):
        import core.hotlist_scoring as hs

        with patch("api.state_service.get_state") as mock_get_state:
            mock_get_state.return_value = {"hot_list": [{"ticker": "AVGO"}], "last_update": "ts"}
            hs.get_hotlist_state()
        kwargs = mock_get_state.call_args.kwargs
        assert kwargs["live_prices"] is False
