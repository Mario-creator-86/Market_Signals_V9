from copy import deepcopy
from datetime import datetime, timedelta, timezone

from agents.pre_earnings_agent import PreEarningsAgent


def _event_utc(days_from_now: int = 5) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(days=days_from_now)
    ).replace(microsecond=0).isoformat()


def _persisted_gct(event_utc: str) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    verified_fields = (
        "beat_history",
        "historical_move_history",
        "recent_reactions",
        "analyst_consensus",
        "range_proxy",
        "liquidity_context",
        "earnings_trend",
        "news_context",
    )
    return {
        "symbol": "GCT",
        "earnings_time_utc": event_utc,
        "session": "AMC",
        "updated_utc": now,
        "structured_direction_bias_score": 99,
        "coverage_score": 100,
        "coverage_label": "HIGH",
        "beat_rate_historical": 0.875,
        "beat_rate_beats": 7,
        "beat_rate_samples": 8,
        "beat_rate_source": "yfinance",
        "historical_median_move_pct": 4.2,
        "avg_move_historical": 4.2,
        "historical_sample_size": 8,
        "gap_risk_history_source": "calendar+yfinance",
        "recent_reactions": [
            {"date": "2026-05-01", "move_pct": 8.9, "dir": "UP"},
            {"date": "2026-02-01", "move_pct": -3.1, "dir": "DOWN"},
            {"date": "2025-11-01", "move_pct": 5.5, "dir": "UP"},
        ],
        "range_proxy_pct": 4.984,
        "expected_move_pct": 4.984,
        "analyst_target_upside_pct": 29.7,
        "analyst_recommendation": "buy",
        "analyst_rec_mean": 1.9,
        "analyst_count": 8,
        "market_cap": 2_400_000_000,
        "avg_volume": 1_250_000,
        "sector": "Industrials",
        "earnings_trend_score": 0.24,
        "top_news": [
            {
                "title": f"GCT earnings preview {index}",
                "url": f"https://example.test/gct/{index}",
                "source": "fixture",
            }
            for index in range(6)
        ],
        "enrichment_verified_at_utc": {
            field: now for field in verified_fields
        },
    }


def _agent_with_provider_results(
    *,
    fail_if_called: bool = False,
) -> PreEarningsAgent:
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._resolve_sector = lambda _symbol, _row: None
    agent._resolve_calibration_factor = lambda *_args: (1.0, 0)
    agent._get_track_record_cached = lambda: {
        "per_symbol": {},
        "overall": {},
    }
    agent._external_budget_exhausted = lambda: False
    agent._reserve_external_call = lambda _provider: False

    if fail_if_called:
        def _unexpected(*_args, **_kwargs):
            raise AssertionError("fresh provider path should stay cache-first")

        agent._get_cached_quote = _unexpected
        agent._get_analyst_estimates_cached = _unexpected
        agent._compute_historical_earnings_gap_stats = _unexpected
        agent._estimate_expected_move_pct = _unexpected
        agent._estimate_earnings_trend_score = _unexpected
        agent._compute_recent_earnings_reactions = _unexpected
        agent._enrich_with_verified_data = _unexpected
        agent._compute_historical_beat_stats = _unexpected
    else:
        agent._get_cached_quote = lambda _symbol: {}
        agent._get_analyst_estimates_cached = lambda _symbol: None
        agent._compute_historical_earnings_gap_stats = lambda _symbol: {
            "median_move_pct": None,
            "sample_size": 0,
            "moves": [],
            "source": "provider_miss",
        }
        agent._estimate_expected_move_pct = lambda _symbol: None
        agent._estimate_earnings_trend_score = lambda _symbol: None
        agent._compute_recent_earnings_reactions = (
            lambda _symbol, n=3: []
        )
        agent._enrich_with_verified_data = lambda _symbol: {}
        agent._compute_historical_beat_stats = lambda _symbol: {
            "rate": None,
            "beats": 0,
            "samples": 0,
            "source": None,
            "unavailable_reason": "provider miss",
        }
    return agent


def _rebuild(
    agent: PreEarningsAgent,
    event_utc: str,
    previous: dict,
    *,
    refresh_enrichment: bool,
) -> dict:
    return agent._build_item_for_symbol(
        symbol="GCT",
        news_rows=[],
        earnings_rows=[
            {
                "symbol": "GCT",
                "date": event_utc,
                "session": "AMC",
            }
        ],
        mismatch_info=None,
        cfg={},
        state_row={},
        as_of_utc=datetime.now(timezone.utc).isoformat(),
        previous_item=previous,
        refresh_enrichment=refresh_enrichment,
    )


def test_same_event_full_rebuild_after_restart_is_cache_first():
    event_utc = _event_utc()
    previous = _persisted_gct(event_utc)
    agent = _agent_with_provider_results(fail_if_called=True)

    rebuilt = _rebuild(
        agent,
        event_utc,
        previous,
        refresh_enrichment=False,
    )

    # Legacy undated headlines are no longer allowed to live forever. The
    # verified non-news inputs remain cache-first, while news coverage drops
    # until a source provides timestamped evidence.
    assert rebuilt["coverage_score"] == 70
    assert rebuilt["news_count"] == 0
    assert rebuilt["beat_rate_historical"] == 0.875
    assert rebuilt["beat_rate_samples"] == 8
    assert rebuilt["historical_sample_size"] == 8
    assert rebuilt["range_proxy_pct"] == 4.984
    assert rebuilt["analyst_count"] == 8
    assert rebuilt["market_cap"] == 2_400_000_000
    assert rebuilt["avg_volume"] == 1_250_000
    assert rebuilt["earnings_trend_score"] == 0.24
    assert rebuilt["enrichment_value_sources"]["beat_history"] == (
        "persisted_same_event"
    )
    # The structured score is recalculated from restored verified inputs; the
    # old final/structured number itself is not copied.
    assert rebuilt["structured_direction_bias_score"] != 99


def test_targeted_refresh_provider_miss_keeps_better_same_event_sample():
    event_utc = _event_utc()
    previous = _persisted_gct(event_utc)
    previous["enrichment_verified_at_utc"] = {
        field: "2020-01-01T00:00:00+00:00"
        for field in previous["enrichment_verified_at_utc"]
    }
    agent = _agent_with_provider_results()

    rebuilt = _rebuild(
        agent,
        event_utc,
        previous,
        refresh_enrichment=True,
    )

    assert rebuilt["coverage_score"] == 70
    assert rebuilt["news_count"] == 0
    assert rebuilt["beat_rate_beats"] == 7
    assert rebuilt["beat_rate_samples"] == 8
    assert rebuilt["historical_median_move_pct"] == 4.2
    assert len(rebuilt["recent_reactions"]) == 3
    assert rebuilt["analyst_target_upside_pct"] == 29.7
    assert rebuilt["sector"] == "Industrials"
    assert set(rebuilt["enrichment_value_sources"].values()) == {
        "persisted_same_event"
    }
    assert set(rebuilt["enrichment_refresh_after_utc"]) >= {
        "beat_history",
        "historical_move_history",
        "analyst_consensus",
        "range_proxy",
        "liquidity_context",
    }

    # The failed attempt writes a persistent cooldown. A new process rebuilding
    # the same event immediately afterwards must not repeat provider calls.
    restarted_agent = _agent_with_provider_results(fail_if_called=True)
    rebuilt_after_restart = _rebuild(
        restarted_agent,
        event_utc,
        rebuilt,
        refresh_enrichment=False,
    )
    assert rebuilt_after_restart["coverage_score"] == 70
    assert rebuilt_after_restart["beat_rate_samples"] == 8


def test_new_quarter_does_not_inherit_previous_enrichment():
    previous_event = _event_utc()
    next_quarter_event = (
        datetime.fromisoformat(previous_event) + timedelta(days=91)
    ).isoformat()
    previous = _persisted_gct(previous_event)
    agent = _agent_with_provider_results()

    rebuilt = _rebuild(
        agent,
        next_quarter_event,
        deepcopy(previous),
        refresh_enrichment=True,
    )

    assert rebuilt["coverage_score"] == 20
    assert rebuilt["coverage_label"] == "LOW"
    assert rebuilt["beat_rate_historical"] is None
    assert rebuilt["beat_rate_samples"] == 0
    assert rebuilt["historical_sample_size"] == 0
    assert rebuilt["range_proxy_pct"] is None
    assert rebuilt["analyst_count"] is None
    assert rebuilt["market_cap"] is None
    assert rebuilt["avg_volume"] is None
    assert rebuilt["earnings_trend_score"] is None
    assert rebuilt["recent_reactions"] == []
    assert "persisted_same_event" not in set(
        rebuilt["enrichment_value_sources"].values()
    )
