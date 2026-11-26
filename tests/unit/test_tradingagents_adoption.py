"""Tests for the TradingAgents/Karpathy adoption (2026-09-02).

Covers: bull/bear entry debate + research-manager verdict, post-outcome
reflection lessons, ledger debate projection, and the autoresearch
KEEP/DISCARD summary.
"""
import json
from unittest.mock import patch

import pytest

from core.ai.debate import (
    _extract_json,
    _norm_verdict,
    run_entry_debate,
    should_debate,
)

pytestmark = pytest.mark.unit


_CFG = {
    "ai": {"providers": {}},
}


class _Resp:
    def __init__(self, text):
        self.text = text


class _FakeProvider:
    name = "fake"

    def __init__(self, texts):
        self.texts = list(texts)
        self.prompts = []

    def generate(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return _Resp(self.texts.pop(0) if self.texts else "")


def _signal(**overrides):
    base = {
        "ticker": "CBAT",
        "action": "LONG",
        "strategy_type": "PRE_EARNINGS_AI_LONG",
        "signal_score": 83.9,
        "news_headlines": ["[src=yahoo_rss | age=7h] CBAK order news"],
    }
    base.update(overrides)
    return base


def _review():
    return {"available": True, "recommendation": "approve", "confidence": 0.55,
            "risk_flags": ["gap risk medium"]}


class TestEntryDebate:
    def test_full_debate_produces_manager_verdict(self):
        bull = _FakeProvider([
            "Bull case: strong order backlog and bullish flow.",
            "Bear case: thin liquidity, one-source news.",
        ])
        manager = _FakeProvider([
            json.dumps({
                "recommendation": "approve", "confidence": 0.7,
                "rationale": "Bull side answered the liquidity concern.",
                "bull_score": 7, "bear_score": 4,
            }),
        ])

        def fake_build(config, svc, role="market_analyst"):
            if role == "portfolio_manager":
                return manager, "mgr-model", []
            return bull, "debate-model", []

        with patch("core.ai.decision_engine._build_providers", side_effect=fake_build):
            result = run_entry_debate(_signal(), _review(), config=_CFG)

        assert result["available"] is True
        assert "Bull case" in result["bull"]
        assert "Bear case" in result["bear"]
        assert result["verdict"]["recommendation"] == "approve"
        assert result["verdict"]["confidence"] == pytest.approx(0.7)
        assert result["models"]["debater"] == "fake/debate-model"
        assert result["models"]["manager"] == "fake/mgr-model"
        # debate must quote the evidence: base rates and headlines flow in
        assert any("news:" in p for p in bull.prompts)

    def test_manager_watch_is_normalised(self):
        bull = _FakeProvider(["bull", "bear"])
        manager = _FakeProvider(['{"recommendation": "WATCH", "confidence": 1.4, '
                                 '"rationale": "balanced", "bull_score": 99, "bear_score": 5}'])

        def fake_build(config, svc, role="market_analyst"):
            return (manager, None, []) if role == "portfolio_manager" else (bull, None, [])

        with patch("core.ai.decision_engine._build_providers", side_effect=fake_build):
            result = run_entry_debate(_signal(), _review(), config=_CFG)

        assert result["verdict"]["recommendation"] == "watch"
        assert result["verdict"]["confidence"] == 1.0  # clamped
        assert result["verdict"]["bull_score"] == 10  # clamped

    def test_provider_failure_is_fail_open(self):
        def fake_build(config, svc, role="market_analyst"):
            return None, None, []

        with patch("core.ai.decision_engine._build_providers", side_effect=fake_build):
            result = run_entry_debate(_signal(), _review(), config=_CFG)

        assert result["available"] is False
        assert result["error"] == "no_provider"

    def test_unparseable_manager_verdict_fails_open(self):
        bull = _FakeProvider(["bull", "bear"])
        manager = _FakeProvider(["I think approve, sounds good!"])

        def fake_build(config, svc, role="market_analyst"):
            return (manager, None, []) if role == "portfolio_manager" else (bull, None, [])

        with patch("core.ai.decision_engine._build_providers", side_effect=fake_build):
            result = run_entry_debate(_signal(), _review(), config=_CFG)

        assert result["available"] is False
        assert result["error"] == "manager_verdict_unparseable"


class TestDebatePolicy:
    def test_pre_earnings_scope_only(self):
        due, why = should_debate(_signal(strategy_type="SCANNER_AI_LONG"), _review(),
                                 {"debate_enabled": True, "debate_scope": "pre_earnings"})
        assert due is False and why == "scope_pre_earnings_only"

        due, why = should_debate(_signal(), _review(),
                                 {"debate_enabled": True, "debate_scope": "pre_earnings"})
        assert due is True

    def test_not_run_for_watch_or_unavailable(self):
        watch = {"available": True, "recommendation": "watch", "confidence": 0.9}
        assert should_debate(_signal(), watch, {"debate_scope": "all"})[0] is False
        down = {"available": False, "recommendation": "approve", "confidence": 0.9}
        assert should_debate(_signal(), down, {"debate_scope": "all"})[0] is False

    def test_disabled(self):
        assert should_debate(_signal(), _review(), {"debate_enabled": False})[0] is False


class TestReflection:
    def test_lesson_stored_and_deduped(self, tmp_path):
        from core.ai import reflection

        with patch.object(reflection, "REFLECTIONS_PATH", tmp_path / "ai_reflections.jsonl"), \
             patch("core.ai.decision_engine._build_providers", return_value=(_FakeProvider(["Gap risk beat the stop; halve size on medium gap prints."]), "m", [])):
            row = reflection.reflect_on_closed_position(
                {"ticker": "IREN", "strategy_type": "PRE_EARNINGS_AI_LONG",
                 "decision_context": {"decision_id": "dec_z"}},
                decision_summary="IREN LONG entry 41.1; thesis: momentum",
                pnl_pct=-8.99, close_reason="stop_loss", config={},
            )
            assert row and row["lesson"].startswith("Gap risk")
            # second call for the same decision is a no-op
            again = reflection.reflect_on_closed_position(
                {"ticker": "IREN", "strategy_type": "PRE_EARNINGS_AI_LONG",
                 "decision_context": {"decision_id": "dec_z"}},
                decision_summary="dup", pnl_pct=-8.99, close_reason="stop_loss", config={},
            )
            assert again is None
            lessons = reflection.load_lessons("PRE")
        assert len(lessons) == 1
        assert lessons[0]["decision_id"] == "dec_z"

    def test_disabled_when_config_off(self):
        from core.ai import reflection

        assert reflection.reflection_enabled({"ollama": {"reflection_enabled": False}}) is False
        assert reflection.reflection_enabled({"ollama": {"reflection_enabled": True}}) is True


class TestLedgerDebateField:
    def test_build_ledger_entry_projects_debate(self):
        from core.decision_ledger.recorder import build_ledger_entry

        entry = build_ledger_entry(
            {"ticker": "CBAT", "id": "s1"},
            {"decision_id": "dec_d1", "decision_status": "accepted",
             "ai_debate": {"available": True, "prompt_version": "entry_debate_v1",
                           "bull": "bull text", "bear": "bear text",
                           "verdict": {"recommendation": "approve", "confidence": 0.7,
                                       "rationale": "r", "bull_score": 7, "bear_score": 4},
                           "models": {"manager": "fake/mgr"}}},
        )
        assert entry.debate["available"] is True
        assert entry.debate["verdict"]["recommendation"] == "approve"
        rendered = entry.eleven_questions()
        assert rendered["debate"]["verdict"]["confidence"] == pytest.approx(0.7)

    def test_entry_without_debate_is_none(self):
        from core.decision_ledger.recorder import build_ledger_entry

        entry = build_ledger_entry(
            {"ticker": "AAPL", "id": "s2"},
            {"decision_id": "dec_d2", "decision_status": "rejected"},
        )
        assert entry.debate is None


class TestStrategyLessonsInjection:
    def test_compact_signal_whitelists_strategy_lessons(self):
        from core.ollama_decision import _compact_signal

        out = _compact_signal({
            "ticker": "CBAT",
            "strategy_lessons": [{"lesson": "Gap risk beat the stop.", "pnl_pct": -8.99}],
        })
        assert "strategy_lessons" in out

    def test_prompt_mentions_lessons(self):
        from core.ollama_decision import build_trade_review_prompt

        prompt = build_trade_review_prompt({
            "ticker": "CBAT",
            "strategy_lessons": [{"lesson": "Halve size on medium gap prints."}],
        }, {})
        assert "strategy_lessons" in prompt


class TestAutoresearchSummary:
    def test_auto_cycle_status_carries_autoresearch(self):
        from api.research_factory_service import auto_cycle_status
        from unittest.mock import patch as _patch

        real_registry = "core.research_factory.experiments.ExperimentRegistry"

        class _FakeRegistry:
            def __init__(self, path=None):
                pass

            def status_counts(self):
                return {"APPLIED": 2, "FAILED": 4, "REJECTED": 76}

            def list_records(self, *, limit=100, status=None):
                assert status == "APPLIED"
                row = type("R", (), {})()
                row.experiment_id = "exp_1"
                row.strategy_id = "S7"
                row.description = "winning tweak"
                row.updated_at = "2026-09-02T00:00:00+00:00"
                return [row]

        with _patch(real_registry, _FakeRegistry):
            status = auto_cycle_status()
        assert status["autoresearch"]["keep"] == 2
        assert status["autoresearch"]["discard"] == 80
        assert status["autoresearch"]["current_best"]["strategy_id"] == "S7"
