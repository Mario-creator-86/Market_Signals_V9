from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agents import pre_earnings_agent as pe
from agents.pre_earnings_agent import PreEarningsAgent
from agents.pre_earnings_agent import guard_pre_earnings_recommendation_freshness


def _agent() -> PreEarningsAgent:
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._get_config = lambda: dict(PreEarningsAgent.DEFAULT_CONFIG)
    return agent


def _news(index: int, published: datetime, source: str = "Reuters") -> dict:
    return {
        "id": f"n{index}",
        "ticker": "TEST",
        "title": f"TEST earnings guidance update {index}",
        "summary": f"Verified company update number {index} with revenue context.",
        "source": source,
        "url": f"https://example.com/{index}",
        "published_at": pe._iso(published),
    }


@pytest.mark.unit
def test_news_evidence_expires_stale_dedupes_and_keeps_twelve_articles():
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    fresh = [
        _news(i, now - timedelta(hours=i), source=f"source-{i % 4}")
        for i in range(15)
    ]
    duplicate = dict(fresh[0])
    duplicate["id"] = "duplicate-id"
    duplicate["url"] = "https://syndicated.example/duplicate"
    stale_previous = _news(99, now - timedelta(days=30))

    result = PreEarningsAgent._prepare_news_evidence(
        fresh + [duplicate],
        [stale_previous],
        now=now,
        lookback_hours=336,
        evidence_limit=12,
    )

    assert result["article_count"] == 15
    assert len(result["evidence_rows"]) == 12
    assert result["unique_sources"] == 4
    assert result["material_articles"] == 15
    assert result["fingerprint"]
    assert all("99" not in str(row.get("id")) for row in result["rows"])


@pytest.mark.unit
def test_ai_prompt_reads_full_news_evidence_set_not_only_four_headlines():
    agent = _agent()
    now = datetime.now(timezone.utc)
    rows = [
        PreEarningsAgent._compact_news_row(_news(i, now - timedelta(minutes=i)))
        for i in range(12)
    ]
    item = {
        "symbol": "TEST",
        "earnings_time_utc": pe._iso(now + timedelta(days=1)),
        "session": "AMC",
        "sector": "Technology",
        "gap_risk_numeric": 72,
        "gap_risk_level": "high",
        "structured_direction_bias_score": 42,
        "historical_median_move_pct": 7.2,
        "historical_sample_size": 8,
        "beat_rate_historical": 0.625,
        "beat_rate_beats": 5,
        "beat_rate_samples": 8,
        "recent_reactions": [],
        "top_news": rows,
        "news_digest": {"unique_sources": 4, "evidence_count": 12},
    }
    captured = {}

    def generate(prompt, **kwargs):
        captured["prompt"] = prompt
        return {
            "response": (
                '{"why_bullets":[],"what_to_watch":[],"catalyst_label":"GUIDANCE",'
                '"sentiment":0.4,"confidence":0.7,"decision":"LONG",'
                '"decision_confidence":0.7,"decision_rationale":"Evidence supports a long lean."}'
            )
        }

    with patch("agents.pre_earnings_agent.ollama.generate", side_effect=generate):
        result = agent._ollama_enrich_item(item, "model", 6)

    assert result["decision"] == "LONG"
    assert "News evidence reviewed: 12 articles from 4 sources." in captured["prompt"]
    assert "TEST earnings guidance update 11" in captured["prompt"]


@pytest.mark.unit
def test_newer_unseen_news_marks_saved_ai_verdict_stale():
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    old = PreEarningsAgent._compact_news_row(_news(1, now - timedelta(hours=3)))
    new = PreEarningsAgent._compact_news_row(_news(2, now - timedelta(minutes=15)))
    ai = {
        "used": True,
        "generated_at": pe._iso(now - timedelta(hours=1)),
        "news_fingerprint": "old",
        "news_evidence_ids": [old["id"]],
    }
    item = {
        "top_news": [new, old],
        "news_digest": {"fingerprint": "new"},
    }

    assert PreEarningsAgent._ai_verdict_stale_by_news_evidence(item, ai) is True
    ai["news_evidence_ids"].append(new["id"])
    assert PreEarningsAgent._ai_verdict_stale_by_news_evidence(item, ai) is False


def _candidate(index: int, now: datetime, *, side: str = "LONG") -> dict:
    signed = 70 - index if side == "LONG" else -(70 - index)
    generated = now - timedelta(minutes=10)
    return {
        "symbol": f"T{index}",
        "earnings_time_utc": pe._iso(now + timedelta(days=1 + index % 3)),
        "session": "AMC",
        "event_status": "UPCOMING",
        "event_source": "finnhub",
        "event_source_url": "https://finnhub.io/",
        "event_verified_at": pe._iso(now),
        "event_date_confidence": "medium",
        "event_session_confidence": "provider",
        "gap_risk": 80 - index,
        "gap_risk_numeric": 80 - index,
        "direction_bias_score": signed,
        "direction_label": side,
        "coverage_score": 90 - index,
        "confidence_score": 82 - index,
        "opportunity_score": 78 - index,
        "symbol_track_record": {"hits": 6, "total": 10},
        "news_count": 12,
        "news_digest": {
            "article_count": 12,
            "unique_sources": 4,
            "fingerprint": f"fp-{index}",
            "newest_utc": pe._iso(generated - timedelta(minutes=5)),
        },
        "top_news": [],
        "ai_decision": {
            "used": True,
            "verdict": side,
            "confidence": 0.75,
            "generated_at": pe._iso(generated),
            "news_fingerprint": f"fp-{index}",
            "news_evidence_ids": [],
            "rationale": f"Verified evidence for T{index}.",
        },
        "decision_alignment": "ALIGNED",
        "flags": {},
        "badges": [],
        "why": [f"Structured evidence for T{index}."],
    }


@pytest.mark.unit
def test_daily_top10_is_ranked_bounded_and_excludes_non_actionable_rows():
    agent = _agent()
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    items = [_candidate(i, now, side="SHORT" if i % 4 == 0 else "LONG") for i in range(12)]
    neutral = _candidate(20, now)
    neutral["direction_label"] = "NEUTRAL"
    no_gap = _candidate(21, now)
    no_gap["gap_risk"] = no_gap["gap_risk_numeric"] = None
    reported = _candidate(22, now)
    reported["flags"] = {"already_reported": True}
    items.extend([neutral, no_gap, reported])

    top = agent._build_top_predictions(items, dict(PreEarningsAgent.DEFAULT_CONFIG), now=now)

    assert len(top) == 10
    assert [row["rank"] for row in top] == list(range(1, 11))
    assert [row["trader_score"] for row in top] == sorted(
        [row["trader_score"] for row in top], reverse=True
    )
    assert all(row["side"] in {"LONG", "SHORT"} for row in top)
    assert all(row["ai_reviewed"] is True for row in top)
    assert {row["symbol"] for row in top}.isdisjoint({"T20", "T21", "T22"})


@pytest.mark.unit
def test_daily_top10_uses_earlier_event_as_equal_score_tiebreak(monkeypatch):
    agent = _agent()
    now = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)
    dell = _candidate(1, now)
    dell["symbol"] = "DELL"
    dell["earnings_time_utc"] = pe._iso(now + timedelta(days=2))
    bb = _candidate(2, now)
    bb["symbol"] = "BB"
    bb["earnings_time_utc"] = pe._iso(now + timedelta(days=25))
    for item in (dell, bb):
        item["trader_score"] = 62
        item["trader_score_band"] = "watch"
        item["recommendation_eligible"] = True
        item["recommendation_blockers"] = []
        item["news_update_pending"] = False

    monkeypatch.setattr(agent, "_refresh_prediction_scores", lambda *args, **kwargs: None)
    top = agent._build_top_predictions([bb, dell], dict(PreEarningsAgent.DEFAULT_CONFIG), now=now)

    assert [row["symbol"] for row in top] == ["DELL", "BB"]
    assert [row["trader_score"] for row in top] == [62, 62]


@pytest.mark.unit
def test_daily_top10_counts_news_from_legacy_snapshot_rows():
    agent = _agent()
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    item = _candidate(1, now)
    item.pop("news_count")
    item.pop("news_digest")
    item["top_news"] = [
        PreEarningsAgent._compact_news_row(_news(1, now - timedelta(hours=1), "Reuters")),
        PreEarningsAgent._compact_news_row(_news(2, now - timedelta(hours=2), "Bloomberg")),
        PreEarningsAgent._compact_news_row(_news(3, now - timedelta(hours=3), "Reuters")),
    ]

    top = agent._build_top_predictions([item], dict(PreEarningsAgent.DEFAULT_CONFIG), now=now)

    assert top[0]["news_articles"] == 3
    assert top[0]["news_sources"] == 2
    assert top[0]["score_breakdown"]["news_breadth"] > 0

    payload = {
        "as_of_utc": pe._iso(datetime.now(timezone.utc)),
        "status": {"last_success_time": pe._iso(datetime.now(timezone.utc))},
        "items": [item],
    }
    agent._attach_daily_ranking(payload, dict(PreEarningsAgent.DEFAULT_CONFIG))
    assert payload["ranking"]["news_articles_scanned"] == 3


@pytest.mark.unit
def test_calendar_coverage_counts_scored_and_explicit_validation_rejections():
    agent = _agent()
    now = datetime.now(timezone.utc)
    payload = {
        "as_of_utc": pe._iso(now),
        "status": {"last_success_time": pe._iso(now)},
        "items": [_candidate(1, now), _candidate(2, now)],
        "reported_today": [{
            "symbol": "REPORTED",
            "earnings_time_utc": pe._iso(now + timedelta(days=1)),
            "reason": "ALREADY_REPORTED_EVIDENCE",
        }],
        "calendar_validation_rejections": [{
            "symbol": "CONFLICT",
            "earnings_time_utc": pe._iso(now + timedelta(days=2)),
            "reason": "RECENT_PRIOR_EARNINGS_CONFLICT",
        }],
    }

    agent._attach_daily_ranking(
        payload,
        dict(PreEarningsAgent.DEFAULT_CONFIG),
        universe_event_count=4,
        scored_count=2,
    )

    assert payload["ranking"]["deep_scored_count"] == 2
    assert payload["ranking"]["screened_out_count"] == 2
    assert payload["ranking"]["covered_count"] == 4
    assert payload["ranking"]["full_universe_covered"] is True
    assert payload["ranking"]["full_universe_deep_scored"] is False


@pytest.mark.unit
def test_exact_calendar_coverage_ignores_rows_outside_active_universe():
    agent = _agent()
    now = datetime.now(timezone.utc)
    active = _candidate(1, now)
    active["symbol"] = "ACTIVE"
    outside = _candidate(2, now)
    outside["symbol"] = "OUTSIDE"
    payload = {
        "as_of_utc": pe._iso(now),
        "status": {"last_success_time": pe._iso(now)},
        "items": [active, outside],
        "reported_today": [{
            "symbol": "REPORTED",
            "earnings_time_utc": pe._iso(now + timedelta(days=2)),
        }],
        "calendar_validation_rejections": [{
            "symbol": "ALSO_OUTSIDE",
            "earnings_time_utc": pe._iso(now + timedelta(days=2)),
        }],
    }

    agent._attach_daily_ranking(
        payload,
        dict(PreEarningsAgent.DEFAULT_CONFIG),
        universe_event_count=2,
        universe_symbols={"ACTIVE", "REPORTED"},
        scored_count=2,
    )

    ranking = payload["ranking"]
    assert ranking["coverage_basis"] == "active_calendar_symbols"
    assert ranking["universe_event_count"] == 2
    assert ranking["scored_count"] == 1
    assert ranking["screened_out_count"] == 1
    assert ranking["covered_count"] == 2
    assert ranking["full_universe_covered"] is True


@pytest.mark.unit
def test_top10_explains_cross_horizon_conflict_with_active_position(monkeypatch):
    agent = _agent()
    item = _candidate(1, datetime.now(timezone.utc), side="LONG")
    item["symbol"] = "BB"
    item["earnings_time_utc"] = pe._iso(datetime.now(timezone.utc) + timedelta(days=20))

    class Capital:
        @staticmethod
        def get_positions():
            return [{
                "position_id": "pos_bb_short",
                "ticker": "BB",
                "direction": "SHORT",
                "status": "OPEN",
                "position_type": "paper_signal",
                "strategy_type": "SCANNER_AI_SHORT",
            }]

    monkeypatch.setattr("core.capital_manager.get_capital_manager", lambda: Capital())
    contexts = agent._attach_open_position_context(
        [item],
        dict(PreEarningsAgent.DEFAULT_CONFIG),
    )
    top = agent._build_top_predictions(
        [item],
        dict(PreEarningsAgent.DEFAULT_CONFIG),
        now=datetime.now(timezone.utc),
    )

    assert contexts["BB"]["direction_conflict"] is True
    assert contexts["BB"]["conflict_type"] == "cross_horizon"
    assert contexts["BB"]["execution_eligible_now"] is False
    assert top[0]["position_direction_conflict"] is True
    assert top[0]["execution_state"] == "RESEARCH_RANKING_FUTURE_EVENT"
    assert top[0]["active_position_context"]["active_directions"] == ["SHORT"]


@pytest.mark.unit
def test_stale_snapshot_suppresses_daily_recommendations():
    agent = _agent()
    now = datetime.now(timezone.utc)
    payload = {
        "as_of_utc": pe._iso(now - timedelta(hours=3)),
        "status": {"last_success_time": pe._iso(now - timedelta(hours=3))},
        "flags": {},
        "items": [_candidate(i, now) for i in range(10)],
    }

    agent._attach_daily_ranking(payload, dict(PreEarningsAgent.DEFAULT_CONFIG))

    assert payload["ranking"]["state"] == "STALE"
    assert payload["top_predictions"] == []
    assert payload["flags"]["recommendations_stale"] is True


@pytest.mark.unit
def test_cache_only_freshness_guard_marks_legacy_snapshot_stale():
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    payload = {
        "as_of_utc": pe._iso(now - timedelta(days=2)),
        "refresh_seconds": 600,
        "status": {"last_success_time": pe._iso(now - timedelta(days=2))},
        "items": [],
        "top_predictions": [{"symbol": "OLD"}],
    }

    guarded = guard_pre_earnings_recommendation_freshness(payload, now=now)

    assert guarded["top_predictions"] == []
    assert guarded["ranking"]["state"] == "STALE"
    assert guarded["flags"]["recommendations_stale"] is True


@pytest.mark.unit
def test_freshness_uses_completed_snapshot_time_when_cycle_start_is_older():
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    payload = {
        "as_of_utc": pe._iso(now - timedelta(seconds=15)),
        "refresh_seconds": 600,
        "status": {"last_success_time": pe._iso(now - timedelta(minutes=20))},
        "top_predictions": [{"symbol": "FRESH"}],
    }

    guarded = guard_pre_earnings_recommendation_freshness(payload, now=now)

    assert guarded["ranking"]["snapshot_age_seconds"] == 15
    assert guarded["ranking"]["state"] == "FRESH"
    assert guarded["top_predictions"] == [{"symbol": "FRESH"}]
