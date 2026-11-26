"""Hermetic tests for the point-in-time week replay script."""
import json
from datetime import datetime, timezone

import pytest

from scripts import replay_pre_earnings_week as rp


def test_default_week_window_is_last_complete_monday_to_sunday():
    wed = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    start, end = rp.default_week_window(wed)
    assert start == datetime(2026, 8, 31, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 7, tzinfo=timezone.utc)
    # A Monday itself still replays the previous week until it is complete.
    mon = datetime(2026, 9, 7, 8, tzinfo=timezone.utc)
    start2, end2 = rp.default_week_window(mon)
    assert start2 == datetime(2026, 8, 31, tzinfo=timezone.utc)


def test_load_population_filters_window_and_excludes_duplicate_groups():
    feedback = {"outcomes": [
        {"symbol": "A", "event_utc": "2026-09-02T12:00:00+00:00", "predicted_direction_bias": 30},
        {"symbol": "B", "event_utc": "2026-09-03T12:00:00+00:00"},
        {"symbol": "B", "event_utc": "2026-09-03T20:00:00+00:00"},  # duplicate group
        {"symbol": "C", "event_utc": "2026-08-20T12:00:00+00:00"},  # outside window
        {"symbol": "", "event_utc": "2026-09-04T12:00:00+00:00"},   # no symbol
    ]}
    window = (datetime(2026, 8, 31, tzinfo=timezone.utc),
              datetime(2026, 9, 7, tzinfo=timezone.utc))
    population = rp.load_population(feedback, window)
    assert [row["symbol"] for row in population] == ["A"]


def test_collect_news_cuts_at_event_and_guard_ledger_with_observed_at():
    cutoff = datetime(2026, 9, 3, 20, tzinfo=timezone.utc)
    ticker_news = {
        "TEST": [
            {"title": "pre new", "source": "S1", "url": "https://x/1",
             "published_at": "2026-09-03T10:00:00+00:00", "summary": "s"},
            {"title": "post leak", "source": "S1", "url": "https://x/2",
             "published_at": "2026-09-03T23:00:00+00:00"},
            {"title": "pre dup", "source": "S2", "url": "https://x/1",
             "published_at": "2026-09-02T10:00:00+00:00"},  # same url as first
        ]
    }
    ledger_rows = [
        # Published before the event and observed before it: valid evidence.
        {"ticker": "TEST", "title": "ledger old", "source": "L", "url": "https://x/3",
         "published_at": "2026-09-01T09:00:00+00:00",
         "observed_at": "2026-09-01T10:00:00+00:00"},
        # Published before the event but our pipeline only saw it after: excluded.
        {"ticker": "TEST", "title": "late backfill", "source": "L", "url": "https://x/4",
         "published_at": "2026-09-02T09:00:00+00:00",
         "observed_at": "2026-09-05T10:00:00+00:00"},
        {"ticker": "OTHER", "title": "other sym", "source": "L", "url": "https://x/5",
         "published_at": "2026-09-02T09:00:00+00:00",
         "observed_at": "2026-09-02T10:00:00+00:00"},
    ]
    news, stats = rp.collect_news("TEST", cutoff, ticker_news, ledger_rows, limit=12)
    titles = [row["title"] for row in news]
    assert titles == ["pre new", "ledger old"]
    assert stats["backup_pre"] == 2 and stats["ledger_pre"] == 1
    assert stats["dropped_after"] == 0
    assert all(row["published_utc"] <= cutoff.isoformat() for row in news)


def test_collect_news_caps_and_counts_dropped():
    cutoff = datetime(2026, 9, 3, 20, tzinfo=timezone.utc)
    rows = [
        {"title": f"n{i}", "source": "S", "url": f"https://x/{i}",
         "published_at": f"2026-09-0{3 - i // 5}T{i:02d}:00:00+00:00"}
        for i in range(15)
    ]
    news, stats = rp.collect_news("TEST", cutoff, {"TEST": rows}, [], limit=5)
    assert len(news) == 5
    assert stats["dropped_after"] == 10
    assert news == sorted(news, key=lambda r: r["published_utc"], reverse=True)


def test_historical_reactions_only_uses_earlier_events():
    rp.outcomes_source[0] = [
        {"symbol": "TEST", "event_utc": "2026-06-01T12:00:00+00:00", "realized_signed_move_pct": 2.0},
        {"symbol": "TEST", "event_utc": "2026-07-01T12:00:00+00:00", "realized_signed_move_pct": -1.5},
        {"symbol": "TEST", "event_utc": "2026-08-01T12:00:00+00:00", "realized_signed_move_pct": 0.5},
        {"symbol": "TEST", "event_utc": "2026-09-05T12:00:00+00:00", "realized_signed_move_pct": 9.0},
        {"symbol": "OTHER", "event_utc": "2026-05-01T12:00:00+00:00", "realized_signed_move_pct": 3.0},
    ]
    reactions = rp.historical_reactions(
        "TEST", datetime(2026, 9, 3, 20, tzinfo=timezone.utc), rp.outcomes_source[0]
    )
    assert reactions == [
        {"date": "2026-06-01", "move_pct": 2.0},
        {"date": "2026-07-01", "move_pct": -1.5},
        {"date": "2026-08-01", "move_pct": 0.5},
    ]


def test_build_replay_item_carries_only_point_in_time_fields():
    item = rp.build_replay_item(
        {"symbol": "TEST", "event_utc": "2026-09-03T20:00:00+00:00",
         "session": "AMC", "sector": "TECH"},
        [{"title": "t", "source": "s", "published_utc": "2026-09-01T00:00:00+00:00"}],
        [{"date": "2026-06-01", "move_pct": 2.0}],
    )
    assert item["symbol"] == "TEST" and item["news_digest"]["article_count"] == 1
    assert item["recent_reactions"] == [{"date": "2026-06-01", "move_pct": 2.0}]
    for absent in ("gap_risk", "beat_rate_historical", "analyst_rec_mean", "etoro_context"):
        assert absent not in item


def test_replay_row_records_ai_error_as_abstention():
    rp.outcomes_source[0] = []
    outcome = {"symbol": "TEST", "event_utc": "2026-09-03T20:00:00+00:00",
               "predicted_direction_bias": 30, "realized_signed_move_pct": 1.5}
    news = [{"title": "t", "source": "s", "url": "https://x/1",
             "published_utc": "2026-09-01T00:00:00+00:00"}]
    ticker_news = {"TEST": [dict(news[0], published_at=news[0]["published_utc"])]}

    def failing(item):
        raise RuntimeError("ollama down")

    row = rp.replay_row(outcome, failing, ticker_news, [], min_news=1, news_limit=12)
    assert row["replay"]["error"].startswith("ollama down")
    assert row["stored"]["label"] == "LONG"

    def provider_error(item):
        return {"_error": "CIRCUIT_OPEN"}

    row = rp.replay_row(outcome, provider_error, ticker_news, [], min_news=1, news_limit=12)
    assert row["replay"] == {"error": "CIRCUIT_OPEN"}


def test_run_replay_end_to_end_with_fake_ai_and_resume():
    feedback = {"outcomes": [
        {"symbol": "A", "event_utc": "2026-09-02T20:00:00+00:00", "session": "AMC",
         "predicted_direction_bias": 40, "predicted_ai_verdict": "LONG",
         "realized_signed_move_pct": 2.5},
        {"symbol": "B", "event_utc": "2026-09-04T12:00:00+00:00", "session": "BMO",
         "predicted_direction_bias": -30, "predicted_ai_verdict": "SHORT",
         "realized_signed_move_pct": -3.0},
    ]}
    ticker_news = {
        "A": [{"title": "a", "source": "S", "url": "https://x/a",
               "published_at": "2026-09-01T00:00:00+00:00"}],
        "B": [],  # below min_news -> honest skip
    }
    window = (datetime(2026, 8, 31, tzinfo=timezone.utc),
              datetime(2026, 9, 7, tzinfo=timezone.utc))

    def fake_ai(item):
        return {"decision": "LONG", "decision_confidence": 0.6, "sentiment": 0.4,
                "catalyst_label": "GUIDANCE", "decision_rationale": "r"}

    report = rp.run_replay(feedback, ticker_news, [], window,
                           ai_fn=fake_ai, min_news=1)
    assert report["population"] == 2
    rows = report["rows"]
    assert rows[0]["replay"]["decision"] == "LONG"
    assert rows[1]["replay_skipped_reason"] == "pre_event_news_below_1"
    # Stored tier: one LONG call hit (+2.5); one SHORT call hit (-3.0).
    assert report["stored_bias_tier"]["directional_nonflat_calls"] == 2
    assert report["stored_bias_tier"]["hits"] == 2
    assert report["replay_tier"]["directional_nonflat_calls"] == 1
    assert report["replay_tier"]["hits"] == 1
    assert report["news_coverage"]["events_with_zero_pre_event_articles"] == 1

    # Resume: rows already carrying a replay attempt are not re-run.
    calls = {"n": 0}

    def counting_ai(item):
        calls["n"] += 1
        return {"decision": "SHORT", "decision_confidence": 0.1,
                "decision_rationale": "different"}

    resumed = rp.run_replay(feedback, ticker_news, [], window,
                            ai_fn=counting_ai, min_news=1, previous_rows=rows)
    assert calls["n"] == 0
    assert resumed["rows"][0]["replay"]["decision"] == "LONG"


def test_finalize_report_agreement_counts_directional_pairs():
    rows = [
        {"stored": {"ai_verdict": "LONG", "label": "LONG", "bias": 40,
                    "was_top_prediction": True, "top_side": "LONG"},
         "replay": {"decision": "LONG"}, "news": {"used": 1},
         "event_utc": "2026-09-02T20:00:00+00:00", "realized_signed_move_pct": 1.5},
        {"stored": {"ai_verdict": "LONG", "label": "LONG", "bias": 40,
                    "was_top_prediction": False, "top_side": None},
         "replay": {"decision": "SHORT"}, "news": {"used": 1},
         "event_utc": "2026-08-20T20:00:00+00:00", "realized_signed_move_pct": -2.0},
        {"stored": {"ai_verdict": "NEUTRAL", "label": None, "bias": None,
                    "was_top_prediction": False, "top_side": None},
         "replay": {"decision": "NEUTRAL"}, "news": {"used": 0},
         "event_utc": "2026-09-04T20:00:00+00:00", "realized_signed_move_pct": 0.7},
    ]
    report = {"rows": rows}
    rp.finalize_report(report)
    assert report["stored_vs_replay_ai_agreement"]["n"] == 2
    assert report["stored_vs_replay_ai_agreement"]["agreement_pct"] == 50
    assert report["replay_errors"] == 0
    # Top-10 tier uses the frozen top side and only was_top_prediction rows.
    top = report["top_predictions_tier"]
    assert top["directional_nonflat_calls"] == 1 and top["hits"] == 1
    # Monthly buckets by event month with both stored tiers.
    months = report["monthly"]
    assert set(months) == {"2026-08", "2026-09"}
    assert months["2026-08"]["events"] == 1
    assert months["2026-09"]["stored_bias_tier"]["directional_nonflat_calls"] == 1
    assert months["2026-09"]["stored_ai_tier"]["directional_nonflat_calls"] == 1


def test_report_is_json_serializable():
    feedback = {"outcomes": [
        {"symbol": "A", "event_utc": "2026-09-02T20:00:00+00:00",
         "predicted_direction_bias": 40, "realized_signed_move_pct": None},
    ]}
    window = (datetime(2026, 8, 31, tzinfo=timezone.utc),
              datetime(2026, 9, 7, tzinfo=timezone.utc))
    report = rp.run_replay(feedback, {}, [], window, ai_fn=None, min_news=1)
    json.dumps(report, allow_nan=False)
