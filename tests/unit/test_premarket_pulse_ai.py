"""Tests for the aggregate Pre-Market Pulse AI (Task #14 — B2).

One cached LLM call over the top pre-market candidates produces a session-level
pulse: {summary, sentiment, top_long, top_short, risks, model, generated_at}.
Cloud-only (local models too slow for an aggregate call), non-fatal (None on
any error / circuit / parse failure / disabled), TTL-cached on the agent
instance (None is NOT served from cache — a failed call retries next cycle).
"""

import sys
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from agents.premarket_scanner_agent import PremarketScannerAgent


def _candidates():
    return [
        {"symbol": "AAPL", "direction": "LONG", "gap_pct": 6.0,
         "premarket_score_long": 82, "premarket_score_short": 10,
         "catalysts": ["EARNINGS_BEAT"], "news": [{"title": "AAPL beats Q3"}]},
        {"symbol": "TSLA", "direction": "SHORT", "gap_pct": -5.0,
         "premarket_score_long": 8, "premarket_score_short": 75,
         "catalysts": ["DOWNGRADE"], "news": [{"title": "TSLA downgraded"}]},
    ]


def _agent():
    with patch("agents.premarket_scanner_agent.ConfigManager"):
        agent = PremarketScannerAgent.__new__(PremarketScannerAgent)
        agent._lock = __import__("threading").RLock()
        return agent


def _cfg(**over):
    cfg = dict(PremarketScannerAgent.DEFAULT_CONFIG)
    cfg.update(over)
    return cfg


VALID_JSON = (
    '{"summary":"Risk-on pre-market led by earnings beats.","sentiment":"bullish",'
    '"top_long":[{"symbol":"AAPL","reason":"Strong earnings beat + 6% gap up."}],'
    '"top_short":[{"symbol":"TSLA","reason":"Downgrade, weak price action."}],'
    '"risks":"Low breadth — gains concentrated in a few names."}'
)


class TestComputePremarketPulse:
    def test_disabled_returns_none(self):
        agent = _agent()
        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            assert agent._compute_premarket_pulse(_cfg(pulse_enabled=False), _candidates()) is None
            mock_ol.is_cloud_host.assert_not_called()

    def test_empty_candidates_returns_none(self):
        agent = _agent()
        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            assert agent._compute_premarket_pulse(_cfg(), []) is None
            mock_ol.is_cloud_host.assert_not_called()

    def test_local_host_returns_none(self):
        agent = _agent()
        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.is_cloud_host.return_value = False
            assert agent._compute_premarket_pulse(_cfg(), _candidates()) is None

    def test_no_model_returns_none(self):
        agent = _agent()
        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.is_cloud_host.return_value = True
            mock_ol.get_best_model.return_value = None
            assert agent._compute_premarket_pulse(_cfg(), _candidates()) is None

    def test_generate_error_returns_none(self):
        agent = _agent()
        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.is_cloud_host.return_value = True
            mock_ol.get_best_model.return_value = "gpt-oss:120b"
            mock_ol.generate.return_value = {"error": "Ollama circuit open"}
            assert agent._compute_premarket_pulse(_cfg(), _candidates()) is None

    def test_unparseable_json_returns_none(self):
        agent = _agent()
        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.is_cloud_host.return_value = True
            mock_ol.get_best_model.return_value = "gpt-oss:120b"
            mock_ol.generate.return_value = {"response": "no json here", "_meta": {"model": "gpt-oss:120b"}}
            assert agent._compute_premarket_pulse(_cfg(), _candidates()) is None

    def test_valid_json_returns_structured_pulse(self):
        agent = _agent()
        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.is_cloud_host.return_value = True
            mock_ol.get_best_model.return_value = "gpt-oss:120b"
            mock_ol.generate.return_value = {"response": VALID_JSON, "_meta": {"model": "gpt-oss:120b"}}
            pulse = agent._compute_premarket_pulse(_cfg(), _candidates())
        assert pulse is not None
        assert "Risk-on" in pulse["summary"]
        assert pulse["sentiment"] == "bullish"
        assert pulse["top_long"] == [{"symbol": "AAPL", "reason": "Strong earnings beat + 6% gap up."}]
        assert pulse["top_short"][0]["symbol"] == "TSLA"
        assert pulse["model"] == "gpt-oss:120b"
        assert pulse["generated_at"]  # ISO timestamp present

    def test_empty_pulse_content_returns_none(self):
        agent = _agent()
        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.is_cloud_host.return_value = True
            mock_ol.get_best_model.return_value = "gpt-oss:120b"
            mock_ol.generate.return_value = {"response": '{"summary":"","sentiment":"","top_long":[],"top_short":[],"risks":""}', "_meta": {"model": "gpt-oss:120b"}}
            assert agent._compute_premarket_pulse(_cfg(), _candidates()) is None

    def test_generate_exception_is_non_fatal(self):
        agent = _agent()
        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.is_cloud_host.return_value = True
            mock_ol.get_best_model.return_value = "gpt-oss:120b"
            mock_ol.generate.side_effect = Exception("boom")
            assert agent._compute_premarket_pulse(_cfg(), _candidates()) is None

    def test_timeout_passed_from_config(self):
        agent = _agent()
        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.is_cloud_host.return_value = True
            mock_ol.get_best_model.return_value = "gpt-oss:120b"
            mock_ol.generate.return_value = {"response": VALID_JSON, "_meta": {"model": "gpt-oss:120b"}}
            agent._compute_premarket_pulse(_cfg(pulse_timeout_seconds=20), _candidates())
            _, kwargs = mock_ol.generate.call_args
            assert kwargs.get("timeout_override") == 20

    def test_system_prompt_is_passed_to_llm(self):
        """REGRESSION (re-audit): the detailed system prompt (format spec: top_long
        as array of {symbol,reason}, 'do not invent symbols') was defined but never
        passed to ollama.generate -> the LLM never received the formatting guidance,
        degrading pulse output quality (missing reasons). It must be sent."""
        agent = _agent()
        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.is_cloud_host.return_value = True
            mock_ol.get_best_model.return_value = "gpt-oss:120b"
            mock_ol.generate.return_value = {"response": VALID_JSON, "_meta": {"model": "gpt-oss:120b"}}
            agent._compute_premarket_pulse(_cfg(), _candidates())
            _, kwargs = mock_ol.generate.call_args
        system = kwargs.get("system") or ""
        assert system, "system prompt must be passed to ollama.generate (was dead code)"
        assert "top_long" in system and "{symbol,reason}" in system
        assert "do not invent" in system


class TestPulseCache:
    def test_cache_serves_within_ttl(self):
        agent = _agent()
        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.is_cloud_host.return_value = True
            mock_ol.get_best_model.return_value = "gpt-oss:120b"
            mock_ol.generate.return_value = {"response": VALID_JSON, "_meta": {"model": "gpt-oss:120b"}}
            first = agent.get_premarket_pulse(_cfg(pulse_ttl_seconds=300), _candidates())
            second = agent.get_premarket_pulse(_cfg(pulse_ttl_seconds=300), _candidates())
        assert first == second
        assert mock_ol.generate.call_count == 1  # served from cache on 2nd call

    def test_failed_call_retries_next_cycle(self):
        # None must NOT be cached — a transient failure retries on the next call.
        agent = _agent()
        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.is_cloud_host.return_value = True
            mock_ol.get_best_model.return_value = "gpt-oss:120b"
            mock_ol.generate.side_effect = [{"error": "circuit"}, {"response": VALID_JSON, "_meta": {"model": "gpt-oss:120b"}}]
            first = agent.get_premarket_pulse(_cfg(), _candidates())
            second = agent.get_premarket_pulse(_cfg(), _candidates())
        assert first is None
        assert second is not None
        assert mock_ol.generate.call_count == 2


class TestPulseMarketOpenClearance:
    """REGRESSION (audit #3): a market-open cycle must clear the stored pulse so
    the API never serves a pulse whose top_long/top_short reference symbols that
    are no longer in the (emptied) candidate list."""

    def test_market_open_cycle_clears_pulse(self):
        agent = _agent()
        agent.config_manager = MagicMock()
        agent._state = {
            "status": {},
            "candidates": [{"symbol": "AAPL"}],
            "snapshot_candidates": [],
            "pulse": {"summary": "old pulse", "top_long": [{"symbol": "AAPL"}]},
            "flags": {},
        }
        with patch.object(agent, "_is_enabled", return_value=True), \
             patch.object(PremarketScannerAgent, "_is_us_market_open", return_value=True), \
             patch.object(agent, "_persist_locked"), \
             patch("agents.premarket_scanner_agent.update_agent_status"):
            agent.run_cycle()
        assert agent._state.get("pulse") is None


class TestRunCycleSingleflight:
    """REGRESSION (audit #11): run_cycle must not race on self._state if two
    threads enter it concurrently. A second entry while one is in flight skips
    (returns the current state) instead of building a competing payload."""

    def test_run_cycle_skips_when_already_inflight(self):
        agent = _agent()
        agent.config_manager = MagicMock()
        agent._state = {"status": {}, "candidates": [], "flags": {}}
        agent._cycle_inflight = True  # simulate a concurrent cycle in flight
        with patch.object(agent, "_is_enabled", return_value=True), \
             patch.object(PremarketScannerAgent, "_is_us_market_open", return_value=False), \
             patch.object(agent, "_load_movers") as mock_movers, \
             patch.object(agent, "_get_watchlist_symbols", return_value=[]), \
             patch.object(agent, "_persist_locked"), \
             patch("agents.premarket_scanner_agent.update_agent_status"):
            agent.run_cycle()
        mock_movers.assert_not_called()  # skipped — no second concurrent cycle


class TestPulseCacheInvalidation:
    """REGRESSION (audit #4): the pulse cache must be invalidated when the
    candidate set changes (so a new cycle never serves last cycle's pulse over
    new candidates) and on manual refresh."""

    def test_changed_candidates_recompute_pulse(self):
        agent = _agent()
        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.is_cloud_host.return_value = True
            mock_ol.get_best_model.return_value = "gpt-oss:120b"
            mock_ol.generate.return_value = {"response": VALID_JSON, "_meta": {"model": "gpt-oss:120b"}}
            first = agent.get_premarket_pulse(_cfg(pulse_ttl_seconds=300), _candidates())
            # Same TTL window but a DIFFERENT candidate set -> must recompute.
            changed = [{"symbol": "MSFT", "direction": "LONG", "gap_pct": 7.0,
                        "premarket_score_long": 90, "premarket_score_short": 5,
                        "catalysts": [], "news": []}]
            second = agent.get_premarket_pulse(_cfg(pulse_ttl_seconds=300), changed)
        assert mock_ol.generate.call_count == 2  # not served from stale cache

    def test_invalidate_pulse_cache_clears_it(self):
        agent = _agent()
        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.is_cloud_host.return_value = True
            mock_ol.get_best_model.return_value = "gpt-oss:120b"
            mock_ol.generate.return_value = {"response": VALID_JSON, "_meta": {"model": "gpt-oss:120b"}}
            agent.get_premarket_pulse(_cfg(pulse_ttl_seconds=300), _candidates())
            agent.invalidate_pulse_cache()
            agent.get_premarket_pulse(_cfg(pulse_ttl_seconds=300), _candidates())
        assert mock_ol.generate.call_count == 2  # invalidated -> recompute

    def test_request_refresh_invalidates_pulse_cache(self):
        agent = _agent()
        agent.config_manager = MagicMock()
        agent._pending_refresh = False
        agent._wake_event = __import__("threading").Event()
        with patch.object(agent, "_is_enabled", return_value=True), \
             patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.is_cloud_host.return_value = True
            mock_ol.get_best_model.return_value = "gpt-oss:120b"
            mock_ol.generate.return_value = {"response": VALID_JSON, "_meta": {"model": "gpt-oss:120b"}}
            agent.get_premarket_pulse(_cfg(pulse_ttl_seconds=300), _candidates())
            agent.request_refresh()
            agent.get_premarket_pulse(_cfg(pulse_ttl_seconds=300), _candidates())
        assert mock_ol.generate.call_count == 2  # manual refresh invalidated cache


class TestPulsePicksValidation:
    """REGRESSION (audit #5): AI pulse picks must be filtered to symbols
    actually present in the candidate batch — a hallucinated symbol must never
    surface as a 'best long setup'."""

    def test_hallucinated_symbol_filtered_out(self):
        agent = _agent()
        # Candidates are AAPL + TSLA only.
        hallucinated = (
            '{"summary":"ok","sentiment":"bullish",'
            '"top_long":[{"symbol":"NVDA","reason":"bogus"},{"symbol":"AAPL","reason":"real"}],'
            '"top_short":[{"symbol":"TSLA","reason":"real"}],"risks":"none"}'
        )
        with patch("agents.premarket_scanner_agent.ollama") as mock_ol:
            mock_ol.is_cloud_host.return_value = True
            mock_ol.get_best_model.return_value = "gpt-oss:120b"
            mock_ol.generate.return_value = {"response": hallucinated, "_meta": {"model": "gpt-oss:120b"}}
            pulse = agent._compute_premarket_pulse(_cfg(), _candidates())
        assert pulse is not None
        long_syms = [p["symbol"] for p in pulse["top_long"]]
        assert "NVDA" not in long_syms  # hallucinated -> dropped
        assert "AAPL" in long_syms     # real -> kept
        assert [p["symbol"] for p in pulse["top_short"]] == ["TSLA"]

class TestHungCycleDetection:
    """The loop restarts a cycle that has been in flight longer than its limit (kept from the app's
    self-repair on 2026-11-25); a cycle whose start was never recorded is not judged stuck."""

    def test_a_cycle_in_flight_past_its_limit_is_stuck_and_a_recent_one_is_not(self):
        import time as _time

        agent = _agent()
        agent._cycle_inflight = True
        agent._cycle_started = _time.monotonic() - 1000
        assert agent._cycle_stuck_locked(900) is True
        agent._cycle_started = _time.monotonic() - 10
        assert agent._cycle_stuck_locked(900) is False
        agent._cycle_inflight = False
        agent._cycle_started = _time.monotonic() - 1000
        assert agent._cycle_stuck_locked(900) is False
        del agent._cycle_started
        agent._cycle_inflight = True
        assert agent._cycle_stuck_locked(900) is False
