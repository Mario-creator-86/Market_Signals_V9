"""Tests for the premium Pre-Market earnings-release elimination (Task #14 — B1).

A ticker whose earnings release has ALREADY occurred is a spent catalyst for a
pre-market scanner (the move is realized, not anticipated). It must be removed
from LONG/SHORT/WATCH and surfaced in a transparent `reported_today` list.

`_earnings_already_reported(row, now_utc)` is a pure function of (earnings row,
current UTC time). Session-aware: a BMO report that printed this morning is
reported, but an AMC report printing tonight is still upcoming (kept) until
after the after-hours print. Undated / malformed rows are kept (missing-data
flag, not a stale event) — mirrors `PreEarningsAgent._filter_stale_items`.
"""

import sys
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from agents.premarket_scanner_agent import PremarketScannerAgent, _utc_now


def _dt(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class TestEarningsAlreadyReported:
    """Pure-function checks over the (row, now_utc) space."""

    def test_past_date_is_reported(self):
        # Yesterday's reporter (any session) is already reported.
        row = {"date": "2026-08-01", "when": "bmo"}
        assert PremarketScannerAgent._earnings_already_reported(row, _dt(2026, 8, 2, 12, 0)) is True

    def test_future_date_is_upcoming(self):
        row = {"date": "2026-08-03", "when": "bmo"}
        assert PremarketScannerAgent._earnings_already_reported(row, _dt(2026, 8, 2, 12, 0)) is False

    def test_bmo_today_before_cutoff_is_upcoming(self):
        # 08:00 UTC is before the BMO cutoff (14:30 UTC) -> not yet reported.
        row = {"date": "2026-08-02", "when": "bmo"}
        assert PremarketScannerAgent._earnings_already_reported(row, _dt(2026, 8, 2, 8, 0)) is False

    def test_bmo_today_after_cutoff_is_reported(self):
        # 15:00 UTC is after the BMO cutoff (14:30 UTC) -> reported.
        row = {"date": "2026-08-02", "when": "bmo"}
        assert PremarketScannerAgent._earnings_already_reported(row, _dt(2026, 8, 2, 15, 0)) is True

    def test_amc_today_before_cutoff_is_upcoming(self):
        # 16:00 UTC is before the AMC cutoff (22:00 UTC) -> tonight, not yet.
        row = {"date": "2026-08-02", "when": "amc"}
        assert PremarketScannerAgent._earnings_already_reported(row, _dt(2026, 8, 2, 16, 0)) is False

    def test_amc_today_after_cutoff_is_reported(self):
        # 23:00 UTC is after the AMC cutoff (22:00 UTC) -> reported.
        row = {"date": "2026-08-02", "when": "amc"}
        assert PremarketScannerAgent._earnings_already_reported(row, _dt(2026, 8, 2, 23, 0)) is True

    def test_unknown_session_today_is_kept(self):
        # Ambiguous same-day session must NOT be silently dropped.
        row = {"date": "2026-08-02", "when": ""}
        assert PremarketScannerAgent._earnings_already_reported(row, _dt(2026, 8, 2, 23, 59)) is False

    def test_no_date_is_kept(self):
        # Undated -> missing-data flag, keep.
        row = {"when": "bmo"}
        assert PremarketScannerAgent._earnings_already_reported(row, _dt(2026, 8, 2, 15, 0)) is False

    def test_malformed_date_is_kept(self):
        row = {"date": "not-a-date", "when": "bmo"}
        assert PremarketScannerAgent._earnings_already_reported(row, _dt(2026, 8, 2, 15, 0)) is False

    def test_non_dict_row_is_kept(self):
        assert PremarketScannerAgent._earnings_already_reported(None, _dt(2026, 8, 2, 15, 0)) is False
        assert PremarketScannerAgent._earnings_already_reported("AAPL", _dt(2026, 8, 2, 15, 0)) is False

    def test_session_alias_field_supported(self):
        # The yfinance fallback uses `session` instead of `when`.
        row = {"date": "2026-08-02", "session": "BMO"}
        assert PremarketScannerAgent._earnings_already_reported(row, _dt(2026, 8, 2, 15, 0)) is True

    def test_past_date_amc_is_reported(self):
        # Yesterday's AMC must be reported regardless of session cutoff.
        row = {"date": "2026-08-01", "when": "amc"}
        assert PremarketScannerAgent._earnings_already_reported(row, _dt(2026, 8, 2, 9, 0)) is True


class TestBuildPayloadElimination:
    """End-to-end: already-reported movers leave `candidates` and enter
    `reported_today`; upcoming-earnings movers stay in `candidates`."""

    def _agent(self):
        with patch("agents.premarket_scanner_agent.ConfigManager"):
            agent = PremarketScannerAgent.__new__(PremarketScannerAgent)
            agent._lock = __import__("threading").RLock()
            agent._state = {"items": [], "status": {}}
            agent._last_run_time = 0.0
            return agent

    def _cfg(self):
        cfg = dict(PremarketScannerAgent.DEFAULT_CONFIG)
        cfg["refresh_seconds"] = 300
        return cfg

    def test_already_reported_mover_excluded_and_listed(self):
        agent = self._agent()
        cfg = self._cfg()
        # AAPL reported BMO this morning; NVDA reports AMC tonight (upcoming).
        now = _dt(2026, 8, 2, 15, 0)
        movers = [
            {"symbol": "AAPL", "gap_pct": 6.0, "premarket_price": 190.0,
             "prev_close": 180.0, "premarket_volume": 100000.0,
             "avg_volume": 5000000.0, "source": "scanner"},
            {"symbol": "NVDA", "gap_pct": 5.0, "premarket_price": 130.0,
             "prev_close": 124.0, "premarket_volume": 100000.0,
             "avg_volume": 5000000.0, "source": "scanner"},
        ]
        earnings_items = [
            {"symbol": "AAPL", "date": "2026-08-02", "when": "bmo",
             "eps_estimated": 1.40, "eps_actual": 1.50},
            {"symbol": "NVDA", "date": "2026-08-02", "when": "amc",
             "eps_estimated": 0.60, "eps_actual": None},
        ]
        with patch("agents.premarket_scanner_agent._utc_now", return_value=now), \
             patch.object(agent, "_build_news_index", return_value={}), \
             patch.object(agent, "_derive_catalysts", return_value=[]), \
             patch.object(agent, "_deduplicate_candidates", side_effect=lambda c: c), \
             patch.object(agent, "_build_direction_groups",
                          side_effect=lambda c: {"LONG": c, "SHORT": [], "WATCH": []}), \
             patch.object(agent, "_is_excluded_symbol", return_value=False), \
             patch.object(agent, "_get_watchlist_symbols", return_value=[]):
            payload = agent._build_payload(
                cfg, movers, [], earnings_items, 10, 10, 10,
                news_available=True, earnings_available=True, watchlist_symbols=None,
            )
        syms = [c["symbol"] for c in payload["candidates"]]
        assert "AAPL" not in syms  # already reported -> excluded
        assert "NVDA" in syms       # upcoming tonight -> kept
        reported = payload["reported_today"]
        assert isinstance(reported, list)
        reported_syms = [r["symbol"] for r in reported]
        assert "AAPL" in reported_syms
        assert "NVDA" not in reported_syms
        aapl_rec = next(r for r in reported if r["symbol"] == "AAPL")
        assert aapl_rec["session"] == "bmo"
        assert aapl_rec["eps_surprise_pct"] is not None  # normalized surprise carried
        assert payload["flags"]["reported_today_count"] == len(reported)

    def test_disable_flag_keeps_reported_in_candidates(self):
        agent = self._agent()
        cfg = self._cfg()
        cfg["eliminate_reported"] = False
        now = _dt(2026, 8, 2, 15, 0)
        movers = [
            {"symbol": "AAPL", "gap_pct": 6.0, "premarket_price": 190.0,
             "prev_close": 180.0, "premarket_volume": 100000.0,
             "avg_volume": 5000000.0, "source": "scanner"},
        ]
        earnings_items = [{"symbol": "AAPL", "date": "2026-08-02", "when": "bmo"}]
        with patch("agents.premarket_scanner_agent._utc_now", return_value=now), \
             patch.object(agent, "_build_news_index", return_value={}), \
             patch.object(agent, "_derive_catalysts", return_value=[]), \
             patch.object(agent, "_deduplicate_candidates", side_effect=lambda c: c), \
             patch.object(agent, "_build_direction_groups",
                          side_effect=lambda c: {"LONG": c, "SHORT": [], "WATCH": []}), \
             patch.object(agent, "_is_excluded_symbol", return_value=False), \
             patch.object(agent, "_get_watchlist_symbols", return_value=[]):
            payload = agent._build_payload(
                cfg, movers, [], earnings_items, 10, 10, 10,
                news_available=True, earnings_available=True, watchlist_symbols=None,
            )
        assert "AAPL" in [c["symbol"] for c in payload["candidates"]]
        assert payload["reported_today"] == []

    def test_no_earnings_row_is_kept(self):
        agent = self._agent()
        cfg = self._cfg()
        now = _dt(2026, 8, 2, 15, 0)
        movers = [
            {"symbol": "XYZ", "gap_pct": 7.0, "premarket_price": 50.0,
             "prev_close": 47.0, "premarket_volume": 100000.0,
             "avg_volume": 5000000.0, "source": "scanner"},
        ]
        with patch("agents.premarket_scanner_agent._utc_now", return_value=now), \
             patch.object(agent, "_build_news_index", return_value={}), \
             patch.object(agent, "_derive_catalysts", return_value=[]), \
             patch.object(agent, "_deduplicate_candidates", side_effect=lambda c: c), \
             patch.object(agent, "_build_direction_groups",
                          side_effect=lambda c: {"LONG": c, "SHORT": [], "WATCH": []}), \
             patch.object(agent, "_is_excluded_symbol", return_value=False), \
             patch.object(agent, "_get_watchlist_symbols", return_value=[]):
            payload = agent._build_payload(
                cfg, movers, [], [], 10, 10, 10,
                news_available=True, earnings_available=True, watchlist_symbols=None,
            )
        assert "XYZ" in [c["symbol"] for c in payload["candidates"]]
        assert payload["reported_today"] == []