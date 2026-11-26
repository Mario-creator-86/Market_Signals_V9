"""Tests for FIX 3: premarket avg_volume null handling + earnings-cache
max-age guard.

(a) When avg_volume / premarket_vol_rel can't be derived from the upstream
    mover, the candidate now carries explicit `avg_volume_unavailable` /
    `premarket_vol_rel_unavailable` flags so the frontend can surface
    "volume data unavailable" instead of a silent null. Win-rate-neutral: the
    scoring helpers already handle None deterministically (neutral 45.0).
(b) When the earnings cache age exceeds `earnings_max_age_seconds`, the payload
    now surfaces `earnings_stale: True` and marks earnings unavailable, so
    months-old earnings data is never served as current.
"""

import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from agents.premarket_scanner_agent import (
    PremarketScannerAgent,
    _normalize_mover_volume,
    _score_liquidity,
    _score_volume_activity,
)


class TestVolumeUnavailableFlags:
    def _agent(self):
        with patch("agents.premarket_scanner_agent.ConfigManager"):
            agent = PremarketScannerAgent.__new__(PremarketScannerAgent)
            agent._lock = __import__("threading").RLock()
            agent._state = {"items": [], "status": {}}
            agent._last_run_time = 0.0
            return agent

    def test_regular_session_volume_is_not_relabelled_as_premarket(self):
        normalized = _normalize_mover_volume(
            {
                "symbol": "NVDA",
                "regularMarketVolume": 124_033_835,
                "avg_volume": 141_582_320,
            }
        )

        assert normalized.get("premarket_volume") is None
        assert normalized.get("premarket_vol_rel") is None

    def test_normalize_mover_requires_explicit_extended_session_volume(self):
        agent = self._agent()

        normalized = agent._normalize_mover(
            {
                "symbol": "NVDA",
                "price": 220.0,
                "premarket_price": 220.0,
                "prev_close": 218.0,
                "volume": 124_033_835,
                "avg_volume": 141_582_320,
            }
        )

        assert normalized["premarket_volume"] is None
        assert normalized["avg_volume"] == 141_582_320

    def test_scanner_cache_loads_cross_process_snapshot_and_watchlist_volume(self, monkeypatch):
        agent = self._agent()
        agent.config_manager = MagicMock()
        scanner = MagicMock()
        scanner._state_lock = __import__("threading").RLock()
        scanner.cached_hot_list = [
            {
                "symbol": "BMNR",
                "gap_pct": 7.0,
                "premarket_price": 42.0,
                "prev_close": 39.25,
                "premarket_volume": 180_000,
                "avg_volume": None,
                "source": "scanner",
            }
        ]
        scanner.cached_watchlist_stock_list = [
            {
                "symbol": "BMNR",
                "gap_pct": 6.9,
                "premarket_price": 41.96,
                "prev_close": 39.25,
                "premarket_volume": 175_000,
                "avg_volume": 4_500_000,
                "source": "scanner",
            }
        ]
        scanner.last_scan_time = 1_000.0

        def load_shared_snapshot():
            scanner.last_scan_time = 1_090.0
            return True

        scanner._load_shared_snapshot_if_newer.side_effect = load_shared_snapshot
        monkeypatch.setattr("agents.scanner_agent.get_scanner_agent", lambda _config: scanner)

        movers, age = agent._scanner_cached_movers(now_ts=1_100.0)

        assert scanner._load_shared_snapshot_if_newer.call_count == 1
        assert age == 10
        assert len(movers) == 1
        assert movers[0]["symbol"] == "BMNR"
        assert movers[0]["gap_pct"] == pytest.approx((42.0 / 39.25 - 1.0) * 100.0)
        assert movers[0]["avg_volume"] == 4_500_000

    def test_candidate_flags_avg_volume_unavailable_when_none(self):
        agent = self._agent()
        cfg = dict(PremarketScannerAgent.DEFAULT_CONFIG)
        # Build a minimal payload with a single mover that has no avg_volume.
        mover = {
            "symbol": "AAPL",
            "gap_pct": 6.0,
            "premarket_price": 190.0,
            "prev_close": 180.0,
            "premarket_volume": 100000.0,
            "avg_volume": None,  # missing
            "source": "scanner",
        }
        with patch.object(
            agent, "_build_news_index", return_value={}
        ), patch.object(
            agent, "_build_earnings_index", return_value={}
        ), patch.object(
            agent, "_normalize_earnings_payload", return_value={}
        ), patch.object(
            agent, "_derive_catalysts", return_value=[]
        ), patch.object(
            agent, "_deduplicate_candidates", side_effect=lambda c: c
        ), patch.object(
            agent, "_build_direction_groups", return_value={"LONG": [], "SHORT": [], "WATCH": []}
        ), patch.object(
            agent, "_is_excluded_symbol", return_value=False
        ), patch.object(
            agent, "_get_watchlist_symbols", return_value=[]
        ):
            payload = agent._build_payload(
                cfg,
                movers=[mover],
                news_items=[],
                earnings_items=[],
                movers_age=10,
                news_age=10,
                earnings_age=10,
                news_available=True,
                earnings_available=True,
                watchlist_symbols=None,
            )
        cand = payload["candidates"][0]
        assert cand["avg_volume"] is None
        assert cand["avg_volume_unavailable"] is True
        assert cand["premarket_vol_rel_unavailable"] is True

    def test_candidate_flags_false_when_volume_present(self):
        agent = self._agent()
        cfg = dict(PremarketScannerAgent.DEFAULT_CONFIG)
        mover = {
            "symbol": "AAPL",
            "gap_pct": 6.0,
            "premarket_price": 190.0,
            "prev_close": 180.0,
            "premarket_volume": 100000.0,
            "avg_volume": 5000000.0,
            "source": "scanner",
        }
        with patch.object(agent, "_build_news_index", return_value={}), \
             patch.object(agent, "_build_earnings_index", return_value={}), \
             patch.object(agent, "_normalize_earnings_payload", return_value={}), \
             patch.object(agent, "_derive_catalysts", return_value=[]), \
             patch.object(agent, "_deduplicate_candidates", side_effect=lambda c: c), \
             patch.object(agent, "_build_direction_groups", return_value={"LONG": [], "SHORT": [], "WATCH": []}), \
             patch.object(agent, "_is_excluded_symbol", return_value=False), \
             patch.object(agent, "_get_watchlist_symbols", return_value=[]):
            payload = agent._build_payload(
                cfg, [mover], [], [], 10, 10, 10, True, True, watchlist_symbols=None,
            )
        cand = payload["candidates"][0]
        assert cand["avg_volume"] == 5000000.0
        assert cand["avg_volume_unavailable"] is False
        assert cand["premarket_vol_rel_unavailable"] is False

    def test_scoring_neutral_when_volume_none(self):
        # Win-rate-neutral sanity: None volume must not bias the score.
        assert _score_liquidity(None) == 45.0
        assert _score_volume_activity(None) == 45.0


class TestNewsRefreshBatchAndStaleFlag:
    """REGRESSION (audit #1 + #14): the news refresh batch must be large enough
    to cover a typical mover list in a single cycle (eliminating asymmetric
    staleness across the ranked list), and a `news_stale` flag must surface
    news-cache staleness symmetrically with `earnings_stale`."""

    def _agent(self):
        with patch("agents.premarket_scanner_agent.ConfigManager"):
            agent = PremarketScannerAgent.__new__(PremarketScannerAgent)
            agent._lock = __import__("threading").RLock()
            agent._state = {"items": [], "status": {}}
            agent._last_run_time = 0.0
            agent.config_manager = MagicMock()
            return agent

    def test_default_news_refresh_batch_covers_full_mover_list(self):
        # A 30-mover list (the default max_results) must refresh in ONE cycle,
        # not 6 cycles (~30 min) of asymmetric staleness.
        agent = self._agent()
        cfg = agent._get_config()
        assert cfg["news_refresh_batch_size"] >= 30

    def _stale_news_payload(self, news_age):
        agent = self._agent()
        cfg = dict(PremarketScannerAgent.DEFAULT_CONFIG)
        with patch.object(agent, "_build_news_index", return_value={}), \
             patch.object(agent, "_build_earnings_index", return_value={}), \
             patch.object(agent, "_normalize_earnings_payload", return_value={}), \
             patch.object(agent, "_derive_catalysts", return_value=[]), \
             patch.object(agent, "_deduplicate_candidates", side_effect=lambda c: c), \
             patch.object(agent, "_build_direction_groups", return_value={"LONG": [], "SHORT": [], "WATCH": []}), \
             patch.object(agent, "_is_excluded_symbol", return_value=False), \
             patch.object(agent, "_get_watchlist_symbols", return_value=[]):
            return agent._build_payload(
                cfg, [], [], [], 10, news_age, 10,
                news_available=True, earnings_available=True,
                watchlist_symbols=None,
            )

    def test_news_stale_flag_set_when_news_age_exceeds_ttl(self):
        # cache_ttl_seconds default is 300; 600s > 300 -> stale.
        payload = self._stale_news_payload(600)
        assert payload["flags"]["news_stale"] is True

    def test_news_stale_flag_false_when_news_fresh(self):
        payload = self._stale_news_payload(60)
        assert payload["flags"]["news_stale"] is False


class TestEarningsStaleGuard:
    def _agent(self):
        with patch("agents.premarket_scanner_agent.ConfigManager"):
            agent = PremarketScannerAgent.__new__(PremarketScannerAgent)
            agent._lock = __import__("threading").RLock()
            agent._state = {"items": [], "status": {}}
            agent._last_run_time = 0.0
            return agent

    def test_stale_earnings_flagged_and_marked_unavailable(self):
        agent = self._agent()
        cfg = dict(PremarketScannerAgent.DEFAULT_CONFIG)
        # 87 days = well over the 86400s (1 day) default cap.
        earnings_age = 87 * 86400
        with patch.object(agent, "_build_news_index", return_value={}), \
             patch.object(agent, "_build_earnings_index", return_value={}), \
             patch.object(agent, "_normalize_earnings_payload", return_value={}), \
             patch.object(agent, "_derive_catalysts", return_value=[]), \
             patch.object(agent, "_deduplicate_candidates", side_effect=lambda c: c), \
             patch.object(agent, "_build_direction_groups", return_value={"LONG": [], "SHORT": [], "WATCH": []}), \
             patch.object(agent, "_is_excluded_symbol", return_value=False), \
             patch.object(agent, "_get_watchlist_symbols", return_value=[]):
            payload = agent._build_payload(
                cfg, [], [], [], 10, 10, earnings_age,
                news_available=True, earnings_available=True,
                watchlist_symbols=None,
            )
        assert payload["flags"]["earnings_stale"] is True
        assert payload["flags"]["earnings_available"] is False
        assert payload["inputs_used"]["earnings_cache_age_seconds"] == earnings_age

    def _stale_elimination_payload(self, earnings_age):
        """Build a payload with one mover whose earnings already fired (past
        date -> _earnings_already_reported True). earnings_index +
        _normalize_earnings_payload run for real so the elimination path is
        exercised end-to-end."""
        agent = self._agent()
        cfg = dict(PremarketScannerAgent.DEFAULT_CONFIG)
        mover = {
            "symbol": "AAPL",
            "gap_pct": 6.0,
            "premarket_price": 190.0,
            "prev_close": 180.0,
            "premarket_volume": 100000.0,
            "avg_volume": 5000000.0,
            "source": "scanner",
        }
        # Past-date earnings -> already reported -> would be eliminated when fresh.
        earnings_items = [
            {
                "symbol": "AAPL",
                "date": "2020-01-01",
                "when": "bmo",
                "eps_actual": 2.0,
                "eps_estimated": 1.5,
                "revenue_actual": 100.0,
                "revenue_estimated": 90.0,
            }
        ]
        with patch.object(agent, "_build_news_index", return_value={}), \
             patch.object(agent, "_derive_catalysts", return_value=[]), \
             patch.object(agent, "_deduplicate_candidates", side_effect=lambda c: c), \
             patch.object(agent, "_build_direction_groups", return_value={"LONG": [], "SHORT": [], "WATCH": []}), \
             patch.object(agent, "_is_excluded_symbol", return_value=False), \
             patch.object(agent, "_get_watchlist_symbols", return_value=[]):
            payload = agent._build_payload(
                cfg, [mover], [], earnings_items, 10, 10, earnings_age,
                news_available=True, earnings_available=True,
                watchlist_symbols=None,
            )
        return payload

    def test_stale_earnings_do_not_eliminate_reported_ticker(self):
        """REGRESSION (audit #2): when the earnings cache is stale (>max age),
        a past-date earnings row must NOT drive earnings-elimination — stale
        earnings are likely from a prior quarter and would wrongly drop a ticker
        that has nothing to do with today's session."""
        payload = self._stale_elimination_payload(87 * 86400)
        assert payload["flags"]["earnings_stale"] is True
        # Ticker is NOT eliminated: it stays in candidates, not in reported_today.
        syms = [c["symbol"] for c in payload["candidates"]]
        assert "AAPL" in syms
        reported = [r["symbol"] for r in payload.get("reported_today", [])]
        assert "AAPL" not in reported

    def test_fresh_earnings_still_eliminate_reported_ticker(self):
        """Control: with fresh earnings, the same past-date row DOES eliminate
        the ticker (spent catalyst). Guards against over-fixing #2."""
        payload = self._stale_elimination_payload(60)
        assert payload["flags"]["earnings_stale"] is False
        syms = [c["symbol"] for c in payload["candidates"]]
        assert "AAPL" not in syms
        reported = [r["symbol"] for r in payload.get("reported_today", [])]
        assert "AAPL" in reported

    def test_fresh_earnings_not_flagged_stale(self):
        agent = self._agent()
        cfg = dict(PremarketScannerAgent.DEFAULT_CONFIG)
        with patch.object(agent, "_build_news_index", return_value={}), \
             patch.object(agent, "_build_earnings_index", return_value={}), \
             patch.object(agent, "_normalize_earnings_payload", return_value={}), \
             patch.object(agent, "_derive_catalysts", return_value=[]), \
             patch.object(agent, "_deduplicate_candidates", side_effect=lambda c: c), \
             patch.object(agent, "_build_direction_groups", return_value={"LONG": [], "SHORT": [], "WATCH": []}), \
             patch.object(agent, "_is_excluded_symbol", return_value=False), \
             patch.object(agent, "_get_watchlist_symbols", return_value=[]):
            payload = agent._build_payload(
                cfg, [], [], [], 10, 10, 60,
                news_available=True, earnings_available=True,
                watchlist_symbols=None,
            )
        assert payload["flags"]["earnings_stale"] is False
        assert payload["flags"]["earnings_available"] is True
