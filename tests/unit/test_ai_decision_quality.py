"""P0/P1/P2 decision-quality loop tests (2026-09-02, history-driven).

History that motivated these: accepted pre-earnings entries ran ~18% win rate
with gap-through-stop losses while approvals carried 0.42-0.55 confidence;
the no-trade evaluation was permanently INCONCLUSIVE (empty PIT bars); and
the §41 reviewer had no realized base rates to calibrate against.
"""
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.agent_manager import (
    _pre_earnings_entry_gate,
    _pre_earnings_gap_size_factor,
)

pytestmark = pytest.mark.unit


_CFG = {
    "pre_earnings_min_ai_confidence": 0.6,
    "pre_earnings_gap_risk_block_level": 60,
    "pre_earnings_gap_risk_medium_size_factor": 0.5,
}


def _signal(strategy="PRE_EARNINGS_AI_LONG", gap_risk=45):
    return {
        "strategy_type": strategy,
        "pre_earnings_context": {"gap_risk": gap_risk, "session": "AMC"},
    }


class TestPreEarningsEntryGate:
    def test_high_gap_risk_blocks_even_good_confidence(self):
        review = {"available": True, "recommendation": "approve", "confidence": 0.9}
        assert _pre_earnings_entry_gate(_signal(gap_risk=62), review, _CFG) == "pre_earnings_gap_risk_high"

    def test_low_confidence_blocks(self):
        review = {"available": True, "recommendation": "approve", "confidence": 0.55}
        assert _pre_earnings_entry_gate(_signal(gap_risk=30), review, _CFG) == "pre_earnings_low_ai_confidence"

    def test_confident_low_gap_approval_passes(self):
        review = {"available": True, "recommendation": "approve", "confidence": 0.72}
        assert _pre_earnings_entry_gate(_signal(gap_risk=35), review, _CFG) == ""

    def test_watch_and_unavailable_are_not_gated_here(self):
        watch = {"available": True, "recommendation": "watch", "confidence": 0.9}
        assert _pre_earnings_entry_gate(_signal(), watch, _CFG) == ""
        down = {"available": False, "recommendation": "approve", "confidence": 0.9}
        assert _pre_earnings_entry_gate(_signal(), down, _CFG) == ""

    def test_non_pre_earnings_untouched(self):
        review = {"available": True, "recommendation": "approve", "confidence": 0.42}
        assert _pre_earnings_entry_gate(_signal(strategy="SCANNER_AI_LONG", gap_risk=70), review, _CFG) == ""


class TestPreEarningsGapSizeFactor:
    def test_medium_gap_halves_size(self):
        assert _pre_earnings_gap_size_factor(_signal(gap_risk=45), _CFG) == 0.5
        assert _pre_earnings_gap_size_factor(_signal(gap_risk=40), _CFG) == 0.5
        assert _pre_earnings_gap_size_factor(_signal(gap_risk=59), _CFG) == 0.5

    def test_low_and_high_gap_keep_factor(self):
        assert _pre_earnings_gap_size_factor(_signal(gap_risk=20), _CFG) == 1.0
        # >=60 is entry-blocked upstream; factor stays neutral
        assert _pre_earnings_gap_size_factor(_signal(gap_risk=70), _CFG) == 1.0

    def test_non_pre_earnings_neutral(self):
        assert _pre_earnings_gap_size_factor(_signal(strategy="SCANNER_AI_SHORT", gap_risk=50), _CFG) == 1.0


class TestBaseRates:
    def test_computes_realized_stats_from_closed_positions(self):
        from core.ai import base_rates

        rows = [
            {"strategy_type": "PRE_EARNINGS_AI_LONG", "pnl_pct": 5.0},
            {"strategy_type": "PRE_EARNINGS_AI_LONG", "pnl_pct": -3.0},
            {"strategy_type": "PRE_EARNINGS_AI_LONG", "pnl_pct": -4.0},
            {"strategy_type": "PRE_EARNINGS_AI_LONG", "pnl_pct": -2.0},
        ]
        with patch.object(base_rates, "_closed_pnl_pct", return_value=rows), \
             patch.object(base_rates, "_cache", {"at": 0.0, "by_strategy": {}}):
            payload = base_rates.strategy_base_rates("PRE_EARNINGS_AI_LONG")
        assert payload["exact"]["n"] == 4
        assert payload["exact"]["win_rate_pct"] == 25.0
        assert payload["exact"]["avg_pnl_pct"] == round((5 - 3 - 4 - 2) / 4, 2)
        assert payload["exact"]["small_sample"] is True
        assert payload["family"]["n"] == 4

    def test_below_sample_floor_returns_empty_bucket(self):
        from core.ai import base_rates

        rows = [{"strategy_type": "X_LONG", "pnl_pct": 1.0}]
        with patch.object(base_rates, "_closed_pnl_pct", return_value=rows), \
             patch.object(base_rates, "_cache", {"at": 0.0, "by_strategy": {}}):
            payload = base_rates.strategy_base_rates("X_LONG")
        assert payload.get("exact") is None


class TestPromptAndCriticSeeBaseRates:
    def test_compact_signal_whitelists_strategy_base_rates(self):
        from core.ollama_decision import _compact_signal

        out = _compact_signal({
            "ticker": "CBAT",
            "strategy_base_rates": {"strategy": "PRE_EARNINGS_AI_LONG", "exact": {"n": 11}},
        })
        assert "strategy_base_rates" in out

    def test_trade_review_prompt_instructs_calibration(self):
        from core.ollama_decision import build_trade_review_prompt

        prompt = build_trade_review_prompt({
            "ticker": "CBAT",
            "strategy_base_rates": {"strategy": "PRE_EARNINGS_AI_LONG", "exact": {"n": 11}},
        }, {})
        assert "strategy_base_rates" in prompt
        assert "calibrate" in prompt

    def test_critic_compact_line_shows_base_rates(self):
        from core.ai.critic import _compact_signal

        out = _compact_signal({
            "ticker": "CBAT",
            "strategy_base_rates": {
                "strategy": "PRE_EARNINGS_AI_LONG",
                "exact": {"n": 11, "win_rate_pct": 18.2, "avg_pnl_pct": -2.15, "small_sample": True},
            },
        })
        assert "strategy_base_rates(n=11, win=18.2%" in out


class TestNewsHeadlineMetadata:
    """Fix #2 (2026-09-02 audit): the §41 reviewer saw bare titles only and
    complained "press releases with no publisher/URL/timestamp". Headlines must
    carry source + age metadata, and titles-only candidates must still get
    hydrated with the rich news_pulse payload."""

    def test_top_news_titles_include_source_and_age(self):
        from core.autonomous_candidates import _top_news_titles

        item = {
            "top_news": [
                {
                    "title": "CBAK Energy Secures $96M Battery Cell Order",
                    "source": "yahoo_rss",
                    "published_at": "2026-09-02T12:00:00+00:00",
                },
            ],
        }
        headlines = _top_news_titles(item)
        assert len(headlines) == 1
        assert headlines[0].startswith("[src=yahoo_rss | age=")
        assert "CBAK Energy Secures $96M Battery Cell Order" in headlines[0]

    def test_title_only_rows_stay_bare_for_compatibility(self):
        from core.autonomous_candidates import _top_news_titles

        headlines = _top_news_titles({"top_news": [{"title": "AAPL unveils new AI chip line"}]})
        assert headlines == ["AAPL unveils new AI chip line"]

    def test_titles_only_candidate_gets_hydrated_with_rich_pulse(self):
        """Pre-earnings candidates carry bare titles + a sentiment trend; the
        hydration attach must not mistake that for "news already attached"."""
        from core.agent_manager import _attach_cached_news_context_for_ai

        signal = {
            "ticker": "CBAT",
            "strategy_type": "PRE_EARNINGS_AI_LONG",
            "autonomous_source": "pre_earnings",
            "news_sentiment": "bullish",
            "news_headlines": ["CBAK Energy highlights Nanjing ramp-up"],
            "top_headlines": ["CBAK Energy highlights Nanjing ramp-up"],
        }
        rich_pulse = {
            "top_headlines": [{
                "title": "CBAK Energy Secures $96M Battery Cell Order",
                "source": "yahoo_rss",
                "time": "2026-09-02T12:00:00+00:00",
                "url": "https://example.com/cbat",
                "summary": "Order from an Indian customer for 32140 cells.",
            }],
            "sentiment_trend": "bullish",
            "score": 9.0,
            "hydration": {"requested": True},
        }

        with patch("core.news_pulse.build_news_pulse", return_value=rich_pulse):
            payload = _attach_cached_news_context_for_ai(signal, {})

        rich = [h for h in payload.get("top_headlines", []) if isinstance(h, dict)]
        assert rich, "titles-only candidate must be hydrated with rich pulse dicts"
        assert rich[0]["source"] == "yahoo_rss"
        # compact titles rebuilt from the rich items keep the source metadata
        assert any("yahoo_rss" in str(h) for h in payload.get("news_headlines", []))

    def test_rich_headlines_present_still_skips_refetch(self):
        from core.agent_manager import _attach_cached_news_context_for_ai

        signal = {
            "ticker": "CBAT",
            "strategy_type": "PRE_EARNINGS_AI_LONG",
            "autonomous_source": "pre_earnings",
            "news_sentiment": "bullish",
            "news_headlines": ["CBAK Energy highlights Nanjing ramp-up"],
            "top_headlines": [{
                "title": "CBAK Energy Secures $96M Battery Cell Order",
                "source": "yahoo_rss",
                "time": "2026-09-02T12:00:00+00:00",
                "url": "https://example.com/cbat",
            }],
            "news_context": {"source": "news_pulse", "hydration": {"requested": True}, "stale": False},
        }
        with patch("core.news_pulse.build_news_pulse") as mock_build:
            payload = _attach_cached_news_context_for_ai(signal, {})
        mock_build.assert_not_called()
        assert payload["top_headlines"][0]["source"] == "yahoo_rss"


class TestAcceptancePerformance:
    def _entry(self, decision_id, strategy, pnl):
        from core.decision_ledger.types import LedgerEntry

        return LedgerEntry(
            decision_id=decision_id, created_at="2026-09-01T00:00:00+00:00",
            ticker="X", action="OPEN_LONG", horizon="SWING", source="AGENT",
            status="accepted", strategy=strategy,
            ai={}, ai_proposal={}, validation={}, critic={}, risk={},
            capital_allowed={}, executed={},
            data_existed={},
            afterward={"basis": "position_close", "forward_return_pct": pnl,
                       "outcome": "FAVORABLE" if pnl > 0 else "ADVERSE"},
        )

    def test_aggregates_position_close_afterward(self):
        from api.ledger_service import _acceptance_performance

        entries = [
            self._entry("d1", "PRE_EARNINGS_AI_LONG", 5.0),
            self._entry("d2", "PRE_EARNINGS_AI_LONG", -2.0),
            self._entry("d3", "SCANNER_AI_SHORT", -1.0),
            SimpleNamespace(afterward=None),  # rejected, no afterward — ignored
        ]
        report = _acceptance_performance(entries)
        assert report["evaluated"] == 3
        assert report["wins"] == 1
        assert report["by_strategy"]["PRE_EARNINGS_AI_LONG"]["n"] == 2
        assert report["by_strategy"]["PRE_EARNINGS_AI_LONG"]["win_rate_pct"] == 50.0


class TestAfterwardRecording:
    def test_closed_position_writes_position_close_outcome(self):
        from core.capital_manager import CapitalManager

        recorded = []

        class _Store:
            def list_outcomes(self, decision_id):
                return []

            def append_outcome(self, outcome):
                recorded.append(outcome)
                return outcome, True

        cm = object.__new__(CapitalManager)
        cm._lock = threading.RLock()
        cm.positions = [{
            "position_id": "p1", "status": "CLOSED", "ticker": "AVNW",
            "strategy_type": "PRE_EARNINGS_AI_LONG", "direction": "LONG",
            "entry_price": 100.0, "exit_price": 96.0,
            "close_reason": "stop_loss", "net_pnl": -4.0,
            "decision_context": {"decision_id": "dec_x"},
        }]

        with patch("core.decision_ledger.store.get_decision_ledger_store", return_value=_Store()):
            cm._record_closed_position_afterward()

        assert len(recorded) == 1
        outcome = recorded[0]
        assert outcome.decision_id == "dec_x"
        assert outcome.basis == "position_close"
        assert outcome.outcome == "ADVERSE"
        assert outcome.forward_return_pct == pytest.approx(-4.0, abs=0.01)

    def test_skip_when_decision_id_missing_or_already_recorded(self):
        from core.capital_manager import CapitalManager

        recorded = []

        class _Store:
            def list_outcomes(self, decision_id):
                return [SimpleNamespace(basis="position_close")]

            def append_outcome(self, outcome):
                recorded.append(outcome)

        cm = object.__new__(CapitalManager)
        cm._lock = threading.RLock()
        cm.positions = [
            {"position_id": "p1", "status": "CLOSED", "decision_context": {}},  # no decision_id
            {"position_id": "p2", "status": "CLOSED",
             "decision_context": {"decision_id": "dec_y"},
             "entry_price": 10.0, "exit_price": 11.0, "direction": "LONG"},
        ]
        with patch("core.decision_ledger.store.get_decision_ledger_store", return_value=_Store()):
            cm._record_closed_position_afterward()
            assert recorded == []
            # second pass in the same process: position mark prevents re-check
            cm._record_closed_position_afterward()
            assert recorded == []
