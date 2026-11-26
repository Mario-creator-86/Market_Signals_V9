from unittest.mock import MagicMock
from unittest.mock import patch
from datetime import datetime, timezone
import threading

from agents.pre_earnings_agent import PreEarningsAgent


def _bare_agent():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._is_enabled = MagicMock(return_value=True)
    agent.request_refresh = MagicMock(return_value={"status": "accepted"})
    return agent


def test_symbol_analysis_waits_for_enrichment_before_rescore():
    agent = _bare_agent()
    agent.refresh_symbol_calendar = MagicMock(return_value={"status": "ok"})
    agent.request_coverage_warmup = MagicMock(
        return_value={"status": "accepted", "total": 1}
    )

    result = PreEarningsAgent.refresh_symbol_analysis(agent, "gct")

    agent.refresh_symbol_calendar.assert_called_once_with(
        "GCT", request_rescore=False
    )
    agent.request_coverage_warmup.assert_called_once_with(
        symbols=["GCT"],
        batch_size=1,
        only_needs_coverage=False,
        allow_remote_on_miss=True,
    )
    agent.request_refresh.assert_not_called()
    assert result["coverage"]["status"] == "accepted"


def test_symbol_analysis_rescores_when_no_enrichment_job_starts():
    agent = _bare_agent()
    agent.refresh_symbol_calendar = MagicMock(return_value={"status": "ok"})
    agent.request_coverage_warmup = MagicMock(
        return_value={"status": "no_targets"}
    )

    PreEarningsAgent.refresh_symbol_analysis(agent, "hut")

    agent.request_refresh.assert_called_once_with()


def test_explicit_symbol_is_queued_behind_running_coverage_job():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    now = datetime.now(timezone.utc).isoformat()
    agent._lock = threading.RLock()
    agent._state = {
        "items": [],
        "status": {
            "coverage_warmup": {
                "running": True,
                "job_id": "cov-current",
                "last_progress_at": now,
            }
        },
    }
    agent._coverage_thread = MagicMock()
    agent._coverage_thread.is_alive.return_value = True
    agent._coverage_pending_symbols = []
    agent._resolve_coverage_targets = MagicMock(return_value=["GCT"])

    result = agent.request_coverage_warmup(
        symbols=["GCT"],
        batch_size=1,
        only_needs_coverage=False,
        allow_remote_on_miss=True,
    )

    assert result["status"] == "queued"
    assert result["queued_symbols"] == ["GCT"]
    assert agent._coverage_pending_symbols == ["GCT"]
    assert agent._state["status"]["coverage_warmup"]["queued_symbols"] == ["GCT"]


def _decision_item(event_utc="2026-08-06T12:00:00+00:00"):
    return {
        "symbol": "GCT",
        "earnings_time_utc": event_utc,
        "structured_direction_bias_score": 68,
        "direction_bias_score": 68,
        "direction_bias": 0.68,
        "direction_label": "LONG",
        "gap_risk": 60,
        "coverage_score": 80,
        "badges": ["LONG_BIAS"],
        "confidence": {"direction_bias": {"low": 45, "high": 75, "width": 30}},
        "bias_reasons": ["fresh structured context"],
    }


def test_saved_ai_decision_survives_same_event_rebuild():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    fresh_item = _decision_item()
    previous_item = {
        "symbol": "GCT",
        "earnings_time_utc": "2026-08-03T12:00:00+00:00",
        "ai_decision": {
            "used": True,
            "verdict": "LONG",
            "confidence": 0.2,
            "sentiment": 0.34,
            "rationale": "saved analysis",
            "weight": 0.35,
        },
    }

    carried = agent._carry_forward_ai_decision(
        fresh_item,
        previous_item,
        {"ollama_decision_weight": 0.35},
    )

    assert carried is True
    assert fresh_item["ai_decision"]["rationale"] == "saved analysis"
    assert fresh_item["ai_decision"]["source"] == "persisted"
    assert fresh_item["structured_direction_bias_score"] == 68
    assert fresh_item["direction_bias_score"] == 57
    assert fresh_item["direction_label"] == "LONG"


def test_saved_ai_decision_does_not_cross_quarter_boundary():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    fresh_item = _decision_item("2026-11-05T12:00:00+00:00")
    previous_item = {
        "symbol": "GCT",
        "earnings_time_utc": "2026-08-06T12:00:00+00:00",
        "ai_decision": {"used": True, "verdict": "LONG", "sentiment": 0.4},
    }

    assert agent._carry_forward_ai_decision(
        fresh_item,
        previous_item,
        {"ollama_decision_weight": 0.35},
    ) is False
    assert "ai_decision" not in fresh_item


def test_apply_ollama_resynchronizes_saved_verdict_without_provider_call():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    item = _decision_item()
    item["ai_decision"] = {
        "used": True,
        "verdict": "SHORT",
        "confidence": 0.75,
        "sentiment": -0.70,
        "rationale": "saved",
        "weight": 0.35,
    }

    with patch(
        "agents.pre_earnings_agent.ollama.is_cloud_host",
        side_effect=AssertionError("no provider call is needed for a saved verdict"),
    ):
        used = agent._apply_ollama(
            [item],
            {"use_ollama": True, "ollama_max_tickers": 25, "ollama_timeout_seconds": 12},
        )

    assert used is False
    assert item["structured_direction_bias_score"] == 68
    assert item["direction_bias_score"] == 17
    assert item["decision_alignment"] == "CONFLICT"
