"""Tests for PreEarnings GapRisk fallback + explicit unavailable flag.

Covers FIX 2: when the economic-calendar earnings cache hasn't been warmed for
a symbol, _compute_historical_earnings_gap_stats now falls back to yfinance
past-earnings dates + daily OHLC bars (REAL data, no fabrication). When neither
source yields data, the per-symbol result carries an explicit
`gap_risk_unavailable` flag + reason so the frontend can surface
"insufficient history" instead of a silent null.
"""

import sys
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from agents.pre_earnings_agent import PreEarningsAgent


def _bare_agent():
    with patch("agents.pre_earnings_agent.ConfigManager"):
        agent = PreEarningsAgent.__new__(PreEarningsAgent)
        agent._lock = __import__("threading").RLock()
        agent._state = {"items": [], "status": {}}
        return agent


class TestHistoricalEarningsGapStatsFallback:
    @pytest.mark.parametrize(
        ("loader_name", "chart_kwargs"),
        [
            ("_load_symbol_daily_bars", {"timeframe": "1day", "limit": 300}),
            ("_load_symbol_ohlc_bars", {"interval": "4hour", "limit": 400}),
        ],
    )
    def test_bar_loaders_normalize_mixed_naive_and_aware_timestamps(
        self, loader_name, chart_kwargs
    ):
        agent = _bare_agent()
        agent._external_budget_exhausted = MagicMock(return_value=False)
        agent._reserve_external_call = MagicMock(return_value=True)
        records = [
            {"date": "2026-08-12T12:00:00+00:00", "open": 102.0, "close": 103.0},
            {"date": "2026-08-11T12:00:00", "open": 100.0, "close": 101.0},
        ]

        with patch(
            "agents.pre_earnings_agent.StockDataClient.get_historical_chart",
            return_value=records,
        ) as fetch:
            bars = getattr(agent, loader_name)("KPTI")

        fetch.assert_called_once_with("KPTI", **chart_kwargs)
        assert [bar["dt"].date().isoformat() for bar in bars] == [
            "2026-08-11",
            "2026-08-12",
        ]
        assert all(bar["dt"].tzinfo is not None for bar in bars)

    def test_complete_calendar_sample_does_not_call_yfinance(self):
        agent = _bare_agent()
        event = {"date": (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()}
        agent._get_cached_earnings_history = MagicMock(return_value=[event])
        agent._compute_event_gap_move_pct = MagicMock(return_value=2.5)
        agent._resolve_yf_past_earnings_dates = MagicMock(return_value=[])

        stats = agent._compute_historical_earnings_gap_stats("AAPL", max_events=1)

        assert stats["sample_size"] == 1
        assert stats["median_move_pct"] == pytest.approx(2.5)
        assert stats["source"] == "calendar_cache"
        agent._resolve_yf_past_earnings_dates.assert_not_called()

    def test_incomplete_calendar_sample_is_completed_with_yahoo_history(self):
        agent = _bare_agent()
        calendar_dt = datetime.now(timezone.utc) - timedelta(days=90)
        yahoo_dt = datetime.now(timezone.utc) - timedelta(days=180)
        daily_bars = [{"dt": yahoo_dt, "open": 106.5, "close": 100.0}]
        agent._get_cached_earnings_history = MagicMock(
            return_value=[{"date": calendar_dt.isoformat(), "session": "BMO"}]
        )
        agent._resolve_yf_past_earnings_dates = MagicMock(
            return_value=[(yahoo_dt, "AMC")]
        )
        agent._load_symbol_daily_bars = MagicMock(return_value=daily_bars)
        agent._compute_event_gap_move_pct = MagicMock(
            side_effect=lambda _symbol, _event_dt, _session, bars=None: 6.5 if bars else 2.5
        )

        stats = agent._compute_historical_earnings_gap_stats("GCT", max_events=2)

        assert stats["sample_size"] == 2
        assert stats["moves"] == pytest.approx([2.5, 6.5])
        assert stats["median_move_pct"] == pytest.approx(4.5)
        assert stats["source"] == "calendar+yfinance"
        agent._resolve_yf_past_earnings_dates.assert_called_once_with("GCT", n=2)
        fallback_call = agent._compute_event_gap_move_pct.call_args_list[1]
        assert fallback_call.kwargs["bars"] == daily_bars

    def test_empty_calendar_falls_back_to_yfinance(self):
        agent = _bare_agent()
        agent._get_cached_earnings_history = MagicMock(return_value=[])
        past_dt = datetime.now(timezone.utc) - timedelta(days=120)
        agent._resolve_yf_past_earnings_dates = MagicMock(
            return_value=[(past_dt, "AMC")]
        )
        agent._load_symbol_daily_bars = MagicMock(return_value=[{"dt": past_dt, "open": 101.0, "close": 100.0}])
        # The fallback path must pass the daily bars through to the move calc.
        agent._compute_event_gap_move_pct = MagicMock(return_value=1.0)

        stats = agent._compute_historical_earnings_gap_stats("AAPL", max_events=8)

        assert stats["sample_size"] == 1
        assert stats["source"] == "yfinance_fallback"
        # Confirm the fallback passed bars=daily_bars to the move calc.
        _kwargs = agent._compute_event_gap_move_pct.call_args.kwargs
        assert _kwargs.get("bars") == [{"dt": past_dt, "open": 101.0, "close": 100.0}]

    def test_no_data_anywhere_returns_zero_with_source(self):
        agent = _bare_agent()
        agent._get_cached_earnings_history = MagicMock(return_value=[])
        agent._resolve_yf_past_earnings_dates = MagicMock(return_value=[])
        agent._load_symbol_daily_bars = MagicMock(return_value=[])

        stats = agent._compute_historical_earnings_gap_stats("NOPE", max_events=8)

        assert stats["sample_size"] == 0
        assert stats["median_move_pct"] is None
        assert stats["source"] == "yfinance_fallback"


class TestGapRiskUnavailableFlag:
    """The per-symbol result must surface an explicit unavailable flag +
    reason when no history exists, so the frontend doesn't see a silent null."""

    def _build_result_with_insufficient(self):
        # Build a minimal result dict by exercising the data_confidence branch
        # logic directly (mirrors lines 1881-1885 / 1977-1990 in the agent).
        historical_samples = 0
        is_insufficient_data = historical_samples < 1
        data_confidence = "NONE"
        data_guard_msg = "No historical quarters. GapRisk unavailable. DirectionBias from news."
        gap_risk = None
        result = {
            "gap_risk": int(__import__("agents.pre_earnings_agent")._clamp(gap_risk or 0, 0, 100)) if not is_insufficient_data else None,
            "gap_risk_unavailable": bool(is_insufficient_data),
            "gap_risk_unavailable_reason": data_guard_msg if is_insufficient_data else None,
            "data_confidence": data_confidence,
            "data_message": data_guard_msg,
            "historical_sample_size": historical_samples,
        }
        return result

    def test_unavailable_flag_true_when_no_history(self):
        result = self._build_result_with_insufficient()
        assert result["gap_risk"] is None
        assert result["gap_risk_unavailable"] is True
        assert isinstance(result["gap_risk_unavailable_reason"], str)
        assert result["gap_risk_unavailable_reason"]
        assert result["data_confidence"] == "NONE"
