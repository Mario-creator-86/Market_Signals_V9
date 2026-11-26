"""I04 — verification dossier: opposing evidence, contradictions, gaps.

Covers the pure builder (core/pre_earnings_evidence.py), the frozen-evidence
summary, and the agent serving path (get_verification_dossier) including the
no-mutation guarantee on live state.
"""
import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agents import pre_earnings_agent as pe
from agents.pre_earnings_agent import PreEarningsAgent
from core.pre_earnings_evidence import (
    blocker_text,
    build_verification_dossier,
    summarize_evidence,
)


def _long_item(**overrides):
    item = {
        "symbol": "TEST",
        "earnings_time_utc": "2026-09-10T20:00:00+00:00",
        "session": "AMC",
        "event_status": "UPCOMING",
        "event_source": "finnhub",
        "event_source_url": "https://finnhub.io/",
        "event_verified_at": "2026-09-07T10:00:00+00:00",
        "event_date_confidence": "medium",
        "event_session_confidence": "provider",
        "direction_label": "LONG",
        "direction_bias_score": 70,
        "confidence_score": 85,
        "final_direction_bias_score": 55,
        "structured_direction_label": "LONG",
        "structured_direction_bias_score": 48,
        "llm_direction_score": 70,
        "decision_alignment": "ALIGNED",
        "ai_decision": {
            "used": True,
            "verdict": "LONG",
            "confidence": 0.7,
            "model": "test-model",
            "news_fingerprint": "fp-test",
            "news_articles_read": 9,
            "news_sources_read": 3,
        },
        "trader_score": 72,
        "trader_score_band": "B",
        "recommendation_eligible": True,
        "recommendation_blockers": [],
        "coverage_score": 80,
        "gap_risk": 75,
        "gap_risk_numeric": 75,
        "beat_rate_historical": 0.625,
        "analyst_target_upside_pct": 8.4,
        "analyst_rec_mean": 2.1,
        "symbol_track_record": {"hits": 4, "total": 5},
        "recent_reactions": [{"date": "2026-06-30", "move_pct": 3.4}],
        "news_digest": {
            "article_count": 9,
            "unique_sources": 3,
            "fingerprint": "fp-test",
            "newest_utc": "2026-09-07T11:00:00+00:00",
        },
        "top_news": [{"id": "n1"}, {"id": "n2"}, {"id": "n3"}],
        "flags": {},
        "badges": [],
    }
    item.update(overrides)
    return item


def _cfg(**overrides):
    cfg = dict(PreEarningsAgent.DEFAULT_CONFIG)
    cfg.update(overrides)
    return cfg


@pytest.mark.unit
def test_short_call_lists_all_opposing_evidence_sources():
    item = _long_item(
        direction_label="SHORT",
        final_direction_bias_score=-55,
        analyst_target_upside_pct=12.5,
        analyst_rec_mean=2.0,
        etoro_context={"buy_pct": 71.0, "sell_pct": 29.0},
        peer_readthrough={"verdict": "bullish", "count": 2},
        recent_reactions=[
            {"date": "2026-03-31", "move_pct": 4.1},
            {"date": "2025-12-30", "move_pct": 2.2},
        ],
        symbol_track_record={"hits": 1, "total": 4},
    )
    dossier = build_verification_dossier(item, _cfg())
    sources = {row["source"] for row in dossier["opposing"]}
    assert sources == {
        "analyst_targets",
        "analyst_consensus",
        "etoro_retail",
        "peer_readthrough",
        "recent_reactions",
        "ticker_track_record",
    }
    for row in dossier["opposing"]:
        if row["source"] == "analyst_targets":
            assert "SHORT" in row["detail"] and "+12.5%" in row["detail"]
    track = next(row for row in dossier["opposing"] if row["source"] == "ticker_track_record")
    assert "1/4" in track["detail"]


@pytest.mark.unit
def test_long_call_flags_sell_side_and_recent_down_reactions_as_opposing():
    item = _long_item(
        analyst_target_upside_pct=-3.0,
        analyst_rec_mean=3.8,
        peer_readthrough={"verdict": "bearish"},
        recent_reactions=[
            {"date": "2026-03-31", "move_pct": -4.1},
            {"date": "2025-12-30", "move_pct": -0.2},
        ],
    )
    dossier = build_verification_dossier(item, _cfg())
    sources = {row["source"] for row in dossier["opposing"]}
    assert sources == {
        "analyst_targets",
        "analyst_consensus",
        "peer_readthrough",
        "recent_reactions",
    }


@pytest.mark.unit
def test_contradictions_cover_ai_conflict_final_mismatch_dates_positions():
    item = _long_item(
        direction_label="SHORT",
        final_direction_bias_score=-55,
        decision_alignment="CONFLICT",
        structured_direction_label="SHORT",
        structured_direction_bias_score=-48,
        event_conflicting_dates=["2026-09-10", "2026-09-11"],
        position_direction_conflict=True,
        active_position_context={"active_directions": ["LONG"]},
    )
    item["ai_decision"]["verdict"] = "LONG"
    dossier = build_verification_dossier(item, _cfg())
    codes = {row["code"] for row in dossier["contradictions"]}
    assert codes == {
        "ai_vs_structured",
        "ai_vs_final",
        "event_date_conflict",
        "open_position_conflict",
    }
    ai_conflict = next(row for row in dossier["contradictions"] if row["code"] == "ai_vs_structured")
    assert "SHORT" in ai_conflict["detail"] and "LONG" in ai_conflict["detail"]
    position = next(row for row in dossier["contradictions"] if row["code"] == "open_position_conflict")
    assert "LONG" in position["detail"] and "SHORT" in position["detail"]


@pytest.mark.unit
def test_llm_carries_direction_and_mixed_alignment_are_reported():
    item = _long_item(
        decision_alignment="MIXED",
        structured_direction_label="NEUTRAL",
        structured_direction_bias_score=0,
    )
    dossier = build_verification_dossier(item, _cfg())
    codes = {row["code"] for row in dossier["contradictions"]}
    assert codes == {"mixed_alignment", "llm_carries_direction"}


@pytest.mark.unit
def test_gaps_list_missing_gap_risk_session_beat_rate_analyst_and_thin_news():
    item = _long_item(
        gap_risk=None,
        gap_risk_numeric=None,
        session=None,
        beat_rate_historical=None,
        analyst_target_upside_pct=None,
        analyst_rec_mean=None,
        coverage_score=30,
        news_digest={"article_count": 1, "unique_sources": 0},
        top_news=[{"id": "n1"}],
        news_quality_warnings=["Removed 2 crypto-token article(s) from equity evidence"],
        flags={"already_reported": True},
    )
    dossier = build_verification_dossier(item, _cfg())
    codes = {row["code"] for row in dossier["gaps"]}
    assert {
        "gap_risk_unavailable",
        "session_unknown",
        "beat_rate_unavailable",
        "analyst_unavailable",
        "coverage_thin",
        "news_thin",
        "news_sources_thin",
        "news_quality_warning",
        "already_reported",
    } <= codes
    coverage_gap = next(row for row in dossier["gaps"] if row["code"] == "coverage_thin")
    assert "30" in coverage_gap["detail"]


@pytest.mark.unit
def test_news_only_call_and_stale_ai_are_reported_as_thin_basis():
    item = _long_item(
        news_only_call=True,
        news_update_pending=True,
    )
    dossier = build_verification_dossier(item, _cfg())
    codes = {row["code"] for row in dossier["gaps"]}
    assert {"news_only_call", "ai_stale_vs_news"} <= codes


@pytest.mark.unit
def test_clean_eligible_row_has_no_opposing_contradictions_or_gaps():
    dossier = build_verification_dossier(_long_item(), _cfg())
    assert dossier["opposing"] == []
    assert dossier["contradictions"] == []
    assert dossier["gaps"] == []
    assert dossier["gates"]["eligible"] is True
    assert dossier["gates"]["blockers_human"] == []
    assert dossier["probability_calibrated"] is False


@pytest.mark.unit
def test_neutral_direction_skips_opposing_but_maps_blockers():
    item = _long_item(
        direction_label="NEUTRAL",
        final_direction_bias_score=0,
        recommendation_eligible=False,
        recommendation_blockers=["trader_score_below_threshold", "totally_unknown_code"],
    )
    dossier = build_verification_dossier(item, _cfg())
    assert dossier["opposing"] == []
    blockers = {row["code"]: row["text"] for row in dossier["gates"]["blockers_human"]}
    assert blockers["trader_score_below_threshold"] == (
        "Trader Score below the selection minimum."
    )
    assert blockers["totally_unknown_code"] == "totally_unknown_code"
    assert blocker_text("ai_no_directional_edge") == "AI verdict is NEUTRAL."


@pytest.mark.unit
def test_dossier_is_finite_and_json_serializable():
    item = _long_item(
        gap_risk=None,
        gap_risk_numeric=float("nan"),
        confidence_score=float("inf"),
    )
    dossier = build_verification_dossier(item, _cfg())
    json.dumps(dossier, allow_nan=False)


@pytest.mark.unit
def test_summarize_evidence_compacts_snapshot_and_labels_legacy():
    full = {
        "capture_quality": "captured_at_decision",
        "captured_at_utc": "2026-09-07T10:00:00+00:00",
        "target": "EVENT_GAP_DIRECTION",
        "probability_calibrated": False,
        "observations": {"direction_label": "LONG", "direction_bias_score": 42, "trader_score": 66},
        "policy": {"top_prediction_min_trader_score": 60},
        "ai": {"used": True, "verdict": "LONG", "confidence": 0.8, "model": "m"},
        "news": [{"id": "n1"}, {"id": "n2"}],
    }
    summary = summarize_evidence(full)
    assert summary == {
        "capture_quality": "captured_at_decision",
        "captured_at_utc": "2026-09-07T10:00:00+00:00",
        "target": "EVENT_GAP_DIRECTION",
        "probability_calibrated": False,
        "direction_label": "LONG",
        "direction_bias_score": 42,
        "trader_score": 66,
        "min_trader_score": 60,
        "trader_score_breakdown": None,
        "ai_used": True,
        "ai_verdict": "LONG",
        "ai_confidence": 0.8,
        "ai_model": "m",
        "news_count": 2,
    }
    assert summarize_evidence(None) is None
    legacy = summarize_evidence({"observations": {"confidence_score": float("nan")}})
    assert legacy["direction_label"] is None
    json.dumps(legacy, allow_nan=False)


def _serving_agent():
    agent = PreEarningsAgent()
    agent._feedback = {
        "version": 1,
        "updated_utc": "2026-09-07T00:00:00+00:00",
        "open_predictions": {},
        "outcomes": [],
        "weights": {},
        "last_recalibration_quarter": None,
        "calibration_model": {},
    }
    return agent


@pytest.mark.unit
def test_serving_path_returns_frozen_evidence_without_mutating_state():
    agent = _serving_agent()
    cfg = agent._get_config()
    item = _long_item()
    agent._state = {"items": [item]}
    now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    with patch.object(pe, "_utc_now", lambda: now):
        agent._register_open_predictions([item], cfg)
        payload = agent.get_verification_dossier("test")

    assert payload["symbol"] == "TEST"
    assert payload["earnings_time_utc"] == item["earnings_time_utc"]
    first = payload["first_direction_evidence"]
    assert first["capture_quality"] == "captured_at_decision"
    assert first["direction_label"] == "LONG"
    assert first["min_trader_score"] == cfg["top_prediction_min_trader_score"]
    # Eligible item with a passing score entered the shortlist at registration.
    top = payload["top_direction_evidence"]
    assert top is not None and top["direction_label"] == "LONG"
    assert payload["dossier"]["gates"]["eligible"] is True
    # The dossier is a read: after registration derived fields exist (scoring
    # writes them), but the read itself must not change anything.
    baseline = copy.deepcopy(agent._state["items"][0])
    assert "recommendation_blockers" in baseline  # written by registration
    # Same pinned clock: news staleness is time-dependent, so a read at the
    # real wall-clock date would legitimately add a stale-news blocker.
    with patch.object(pe, "_utc_now", lambda: now):
        payload_again = agent.get_verification_dossier("TEST")
    assert agent._state["items"][0] == baseline
    payload.pop("generated_at_utc")
    payload_again.pop("generated_at_utc")
    assert payload_again == payload


@pytest.mark.unit
def test_serving_path_unknown_symbol_returns_none():
    agent = _serving_agent()
    agent._state = {"items": [_long_item()]}
    assert agent.get_verification_dossier("NOPE") is None
    assert agent.get_verification_dossier("") is None


@pytest.mark.unit
def test_dossier_lock_timeout_does_not_read_state_or_release_unowned_lock():
    from unittest.mock import Mock
    agent = _serving_agent()
    agent._lock = Mock()
    agent._lock.acquire.return_value = False
    agent._state = None
    with pytest.raises(TimeoutError):
        agent.get_verification_dossier("TEST")
    agent._lock.release.assert_not_called()


@pytest.mark.unit
def test_frozen_evidence_never_borrows_another_event():
    agent = _serving_agent()
    agent._feedback["open_predictions"] = {"neighbor": {
        "symbol": "TEST", "event_utc": "2026-09-11T20:00:00+00:00",
        "first_direction_evidence": {"observations": {"direction_label": "SHORT"}},
    }}
    assert agent._lookup_frozen_evidence("TEST", "2026-09-10T20:00:00+00:00") == (None, None)
    assert agent._lookup_frozen_evidence("TEST", None) == (None, None)


@pytest.mark.unit
def test_frozen_evidence_settled_event_matches_equivalent_timezone_and_is_copied():
    agent = _serving_agent()
    evidence = {"observations": {"direction_label": "LONG"}}
    agent._feedback["outcomes"] = [{"symbol": "TEST", "event_utc": "2026-09-10T20:00:00+00:00",
                                     "first_direction_evidence": evidence}]
    first, top = agent._lookup_frozen_evidence("test", "2026-09-10T16:00:00-04:00")
    assert first == evidence and top is None
    first["observations"]["direction_label"] = "SHORT"
    assert evidence["observations"]["direction_label"] == "LONG"


@pytest.mark.unit
def test_failed_dossier_recomputation_cannot_preserve_old_eligibility():
    agent = _serving_agent()
    agent._state = {"items": [_long_item()]}
    with patch.object(agent, "_prediction_score_details", side_effect=ValueError("bad source")):
        result = agent.get_verification_dossier("TEST")
    assert result["dossier"]["gates"]["eligible"] is False
    assert agent._state["items"][0]["recommendation_eligible"] is True


@pytest.mark.unit
def test_dossier_endpoint_event_identity_and_error_statuses(client, monkeypatch):
    agent = _serving_agent()
    agent._state = {"items": [_long_item(), _long_item(earnings_time_utc="2026-09-11T20:00:00+00:00")]}
    monkeypatch.setattr(pe, "get_pre_earnings_agent_if_ready", lambda: agent)
    response = client.get("/api/pre-earnings/dossier", params={"symbol": "TEST", "event_utc": "2026-09-11T20:00:00+00:00"})
    assert response.status_code == 200
    assert response.json()["earnings_time_utc"] == "2026-09-11T20:00:00+00:00"
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/pre-earnings/dossier", params={"symbol": "TEST", "event_utc": "bad"}).status_code == 400
    assert client.get("/api/pre-earnings/dossier", params={"symbol": "TEST", "event_utc": "2026-09-12T20:00:00+00:00"}).status_code == 404
    with patch.object(agent, "get_verification_dossier", side_effect=TimeoutError):
        response = client.get("/api/pre-earnings/dossier", params={"symbol": "TEST"})
        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
    monkeypatch.setattr(pe, "get_pre_earnings_agent_if_ready", lambda: None)
    assert client.get("/api/pre-earnings/dossier", params={"symbol": "TEST"}).status_code == 503
