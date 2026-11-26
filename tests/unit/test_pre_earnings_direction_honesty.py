"""Trader-grade audit regression tests: direction honesty on thin data.

Covers the backend audit fixes around manufactured conviction and honesty of
missing data (docs/PRE_EARNINGS_SYSTEM.md: "missing components stay None,
never faked to neutral"):

- P0-2: beat-rate sample-support damping (a 1/1 quarter is a coin flip, not a
  100% beat rate) + NEWS_ONLY_CALL clamp for GapRisk-unavailable rows whose
  only evidence is news sentiment.
- P0-4: why-bullets must say "GapRisk unavailable" instead of printing the
  internal placeholder int as a real score.
- P1-5: `options_implied_move` must not alias the recent-range proxy.
- P1-6: settled outcomes backfill AI provenance from the durable per-event
  registry instead of settling with a null verdict.
- P2-7: backend GapRisk bands match the documented 0-39 / 40-69 / 70-100.
- P2-8: top-level `confidence_score` always equals the average of the served
  interval scores (one coherent definition).
"""

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from agents import pre_earnings_agent as pe
from agents.pre_earnings_agent import PreEarningsAgent, compute_direction_bias_score


# ─────────────────────────────────────────────────────────────────────────────
# Lightweight agent builder (pattern from test_pre_earnings_enrichment_persistence)
# ─────────────────────────────────────────────────────────────────────────────

def _agent(
    *,
    hist_samples=0,
    hist_median=None,
    beat=None,
    analyst=None,
    expected_move=None,
    reaction_tilt=None,
):
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._resolve_sector = lambda _symbol, _row: None
    agent._resolve_calibration_factor = lambda *_args: (1.0, 0)
    agent._get_track_record_cached = lambda: {"per_symbol": {}, "overall": {}}
    agent._external_budget_exhausted = lambda: False
    agent._reserve_external_call = lambda _provider: False
    agent._get_cached_quote = lambda _symbol: {}
    agent._get_analyst_estimates_cached = lambda _symbol: None
    agent._compute_historical_earnings_gap_stats = lambda _symbol: {
        "median_move_pct": hist_median,
        "sample_size": hist_samples,
        "moves": [],
        "source": "fixture" if hist_samples else "provider_miss",
    }
    agent._estimate_expected_move_pct = lambda _symbol: expected_move
    agent._estimate_earnings_trend_score = lambda _symbol: None
    agent._compute_recent_earnings_reactions = lambda _symbol, n=3: []
    agent._compute_recent_reaction_tilt = lambda _reactions: reaction_tilt
    agent._enrich_with_verified_data = lambda _symbol: dict(analyst or {})
    agent._compute_historical_beat_stats = lambda _symbol: (
        beat
        if beat is not None
        else {
            "rate": None,
            "beats": 0,
            "samples": 0,
            "source": None,
            "unavailable_reason": "provider miss",
        }
    )
    return agent


def _build(agent, news_rows):
    event_utc = (datetime.now(timezone.utc) + timedelta(days=4)).isoformat()
    return agent._build_item_for_symbol(
        symbol="GCT",
        news_rows=news_rows,
        earnings_rows=[{"symbol": "GCT", "date": event_utc, "session": "AMC"}],
        mismatch_info=None,
        cfg={},
        state_row={},
        as_of_utc=datetime.now(timezone.utc).isoformat(),
    )


def _bearish_news(n=6):
    return [
        {
            "title": f"GCT cuts guidance amid downgrade {index}",
            "url": f"https://example.test/gct/{index}",
            "source": "fixture",
        }
        for index in range(n)
    ]


# ─────────────────────────────────────────────────────────────────────────────
# P0-2 Part A: beat-rate sample-support damping
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_single_beat_quarter_is_damped_and_not_extreme():
    """1/1 beat is a coin flip, not a 100% beat rate: c1 damped to 62.5 and
    the final call stays out of LONG conviction territory on news alone."""
    result = compute_direction_bias_score(
        "GCT",
        {"beat_rate_historical": 1.0, "beat_rate_samples": 1},
        0.0,
    )
    assert result["components"]["historical"] == pytest.approx(62.5, abs=0.01)
    label = pe.direction_label_from_score(int(round(result["bias_score"])))
    assert label != "LONG"


@pytest.mark.unit
def test_zero_of_one_quarter_is_damped_toward_neutral_not_zero():
    result = compute_direction_bias_score(
        "GCT",
        {"beat_rate_historical": 0.0, "beat_rate_samples": 1},
        0.0,
    )
    # support = 1/4 → 50 + (0-50)*0.25 = 37.5 (not a fake 0% "always misses")
    assert result["components"]["historical"] == pytest.approx(37.5, abs=0.01)


@pytest.mark.unit
def test_full_sample_support_restores_raw_beat_rate():
    """4/4 quarters → full-strength component (support = 1)."""
    for samples in (4, 8):
        result = compute_direction_bias_score(
            "GCT",
            {"beat_rate_historical": 1.0, "beat_rate_samples": samples},
            0.0,
        )
        assert result["components"]["historical"] == pytest.approx(100.0, abs=0.01)
        label = pe.direction_label_from_score(int(round(result["bias_score"])))
        assert label == "LONG"


@pytest.mark.unit
def test_partial_support_two_quarters_damps_by_half():
    result = compute_direction_bias_score(
        "GCT",
        {"beat_rate_historical": 0.8, "beat_rate_samples": 2},
        0.0,
    )
    # support = 2/4 → 50 + (80-50)*0.5 = 65
    assert result["components"]["historical"] == pytest.approx(65.0, abs=0.01)


@pytest.mark.unit
def test_missing_rate_stays_none_and_rate_without_support_is_neutral():
    # No rate → component stays None (never faked).
    result = compute_direction_bias_score("GCT", {}, 0.0)
    assert result["components"]["historical"] is None
    assert "c1_historical" in result["missing_components"]
    # A rate arriving without any sample metadata contributes neutral, not
    # the raw number (defensive: production always passes samples with rate).
    result_no_support = compute_direction_bias_score(
        "GCT", {"beat_rate_historical": 0.9}, 0.0
    )
    assert result_no_support["components"]["historical"] == pytest.approx(50.0, abs=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# P0-2 Part B: NEWS_ONLY_CALL clamp (GapRisk unavailable + <2 non-news components)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_news_only_call_clamps_label_but_keeps_score_for_transparency():
    agent = _agent()  # 0 historical quarters, no analyst/beat/tilt/options
    item = _build(agent, _bearish_news())

    assert item["gap_risk_unavailable"] is True
    # Bearish headlines alone would have pushed the raw score to SHORT
    # (news-only c4 ≈ 26 → bias ≈ -47) — the label must not follow it.
    assert item["direction_bias_score"] is not None
    assert item["direction_bias_score"] <= -15
    assert item["direction_label"] == "NEUTRAL"
    assert item["news_only_call"] is True
    assert "NEWS_ONLY_CALL" in item["badges"]
    assert "SHORT_BIAS" not in item["badges"]


@pytest.mark.unit
def test_two_non_news_components_avoid_the_news_only_clamp():
    agent = _agent(
        analyst={
            "analyst_target_upside_pct": 20.0,
            "analyst_rec_mean": 1.5,
            "analyst_recommendation": "buy",
            "analyst_count": 5,
        },
        reaction_tilt=0.5,
    )
    item = _build(agent, _bearish_news())

    assert item["gap_risk_unavailable"] is True
    assert item["news_only_call"] is False
    assert "NEWS_ONLY_CALL" not in item["badges"]
    # Analyst + reaction tilt are real evidence → the label follows the score.
    assert item["direction_label"] == "LONG"


@pytest.mark.unit
def test_synchronize_respects_clamp_without_ai_and_allows_ai_tiebreak():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    cfg = {"ollama_decision_weight": 0.35}
    item = {
        "symbol": "GCT",
        "structured_direction_bias_score": -47,
        "direction_bias_score": -47,
        "direction_bias": -0.47,
        "news_only_call": True,
        "badges": ["NEWS_ONLY_CALL"],
        "bias_reasons": [],
    }
    # No AI verdict → clamp survives the recompute.
    agent._synchronize_direction_decision(item, cfg)
    assert item["direction_label"] == "NEUTRAL"
    assert item["structured_direction_label"] == "NEUTRAL"
    assert item["direction_bias_score"] == -47  # score stays for transparency
    assert "SHORT_BIAS" not in item["badges"]
    assert "NEWS_ONLY_CALL" in item["badges"]

    # An explicit AI verdict MAY break the tie (documented AI participation);
    # the NEWS_ONLY_CALL badge stays on the row either way.
    item_ai = dict(item)
    item_ai["badges"] = ["NEWS_ONLY_CALL"]
    item_ai["ai_decision"] = {
        "used": True,
        "verdict": "LONG",
        "confidence": 0.9,
        "sentiment": 0.8,
        "weight": 0.35,
    }
    agent._synchronize_direction_decision(item_ai, cfg)
    assert "NEWS_ONLY_CALL" in item_ai["badges"]
    assert item_ai["structured_direction_label"] == "NEUTRAL"


# ─────────────────────────────────────────────────────────────────────────────
# P0-4: why-bullets must not print a fake GapRisk number
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_why_bullets_print_honest_reason_when_gap_unavailable():
    agent = _agent()  # 0 historical quarters → gap_risk_unavailable
    item = _build(agent, _bearish_news())

    first = item["why"][0]
    assert "GapRisk unavailable" in first
    assert "GapRisk=" not in first  # no fabricated number (was "GapRisk=50")


@pytest.mark.unit
def test_why_bullets_keep_numeric_gap_when_available():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    out = agent._build_why_bullets(
        symbol="GCT",
        gap_risk=50,
        direction_bias=-10,
        news_count=3,
        severity_label="",
        mismatch=False,
        event={},
        contributors=[],
    )
    assert out[0] == "GCT: GapRisk=50 from deterministic weighted contributors."


# ─────────────────────────────────────────────────────────────────────────────
# P1-5: options_implied_move must not alias the range proxy
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_range_proxy_is_not_aliased_as_options_implied_move():
    agent = _agent(hist_samples=4, hist_median=6.0, expected_move=5.0)
    item = _build(agent, _bearish_news())

    assert item["components"]["range_proxy_move"] == pytest.approx(5.0, abs=0.001)
    # No real options feed exists → the field that claims options-implied
    # volatility stays absent instead of re-labeling the range proxy.
    assert "options_implied_move" not in item["components"]


# ─────────────────────────────────────────────────────────────────────────────
# P2-7: backend GapRisk bands match docs (0-39 / 40-69 / 70-100)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_gap_risk_level_bands_match_documented_edges(monkeypatch):
    agent = _agent(hist_samples=4, hist_median=6.0)
    for value, expected in ((39, "low"), (40, "med"), (44, "med"),
                            (45, "med"), (69, "med"), (70, "high")):
        monkeypatch.setattr(
            pe,
            "compute_gap_risk_score",
            MagicMock(return_value=(value, [])),
        )
        item = _build(agent, [])
        assert item["gap_risk"] == value
        assert item["gap_risk_level"] == expected, (value, expected)


# ─────────────────────────────────────────────────────────────────────────────
# P2-8: confidence_score always equals the average of the interval scores
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_confidence_score_coherent_with_interval_scores_when_gap_unavailable():
    agent = _agent()  # insufficient data row
    item = _build(agent, _bearish_news())

    gap_score = float(item["confidence"]["gap_risk"]["score"])
    bias_score = float(item["confidence"]["direction_bias"]["score"])
    expected = int(round(100.0 * (gap_score + bias_score) / 2.0))
    assert item["confidence_score"] == expected
    # The row is N/A for gap risk, but the intervals are real (wide) — the
    # top-level score must not disagree with them by collapsing to 0.
    assert item["confidence_score"] != 0
    assert item["gap_risk_unavailable"] is True


# ─────────────────────────────────────────────────────────────────────────────
# P1-6: AI verdict provenance at prediction settlement
# ─────────────────────────────────────────────────────────────────────────────

def _open_prediction(symbol, event_dt, *, with_verdict=False):
    pred = {
        "symbol": symbol,
        "event_utc": pe._iso(event_dt),
        "session": "AMC",
        "sector": "TECH",
        "cap_bucket": "SMALL",
        "predicted_gap_risk": 60,
        "predicted_direction_bias": 20,
        "predicted_prob_high_gap": 0.6,
        "contributors": {},
        "high_gap_threshold_pct": 5.0,
        "created_utc": pe._iso(event_dt - timedelta(days=2)),
        "updated_utc": pe._iso(event_dt - timedelta(days=2)),
        "predicted_ai_verdict": None,
        "predicted_ai_confidence": None,
        "predicted_ai_catalyst": None,
        "ai_model": None,
    }
    if with_verdict:
        pred.update({
            "predicted_ai_verdict": "SHORT",
            "predicted_ai_confidence": 0.66,
            "predicted_ai_catalyst": "GUIDANCE",
            "ai_model": "registered-model",
        })
    return pred


def _sidecar_entry(symbol, event_dt, verdict="LONG", confidence=0.81, model="glm-5.2"):
    return {
        "symbol": symbol,
        "event_utc": pe._iso(event_dt),
        "saved_at": pe._iso(event_dt - timedelta(days=1)),
        "ai_decision": {
            "used": True,
            "verdict": verdict,
            "confidence": confidence,
            "sentiment": 0.5,
            "catalyst_label": "EARNINGS_VOLATILITY",
            "model": model,
            "event_utc": pe._iso(event_dt),
        },
    }


@pytest.mark.unit
def test_matured_prediction_backfills_ai_verdict_from_sidecar():
    """A prediction registered before AI provenance was persisted must settle
    with the verdict the user was shown (from the durable registry), not null
    — this is what restores ai_coverage > 0 (audit P1-6)."""
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    event_dt = pe._utc_now() - timedelta(days=2)
    key = f"GCT|{pe._iso(event_dt)}"
    agent._feedback = {
        "open_predictions": {key: _open_prediction("GCT", event_dt)},
        "outcomes": [],
    }
    agent._ai_cache = {key: _sidecar_entry("GCT", event_dt)}
    agent._compute_event_gap_move_pct = lambda *_args, **_kwargs: 3.25

    changed = agent._evaluate_matured_feedback_predictions({"high_gap_threshold_pct": 5.0})

    assert changed
    assert agent._feedback["open_predictions"] == {}
    outcome = agent._feedback["outcomes"][0]
    assert outcome["predicted_ai_verdict"] == "LONG"
    assert outcome["predicted_ai_confidence"] == pytest.approx(0.81, abs=0.001)
    assert outcome["predicted_ai_catalyst"] == "EARNINGS_VOLATILITY"
    assert outcome["ai_model"] == "glm-5.2"
    assert outcome["predicted_ai_provenance"] == "ai_cache_backfill"
    # And the track-record AI coverage now counts the settled verdict.
    metrics = agent._compute_track_record_metrics({})
    assert metrics["ai_coverage"] == 1


@pytest.mark.unit
def test_matured_prediction_keeps_registered_verdict_over_sidecar():
    """Monotonic provenance: a verdict registered at prediction time is the
    fact of record; the sidecar never overwrites it at settlement."""
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    event_dt = pe._utc_now() - timedelta(days=2)
    key = f"GCT|{pe._iso(event_dt)}"
    agent._feedback = {
        "open_predictions": {key: _open_prediction("GCT", event_dt, with_verdict=True)},
        "outcomes": [],
    }
    agent._ai_cache = {key: _sidecar_entry("GCT", event_dt, verdict="LONG")}
    agent._compute_event_gap_move_pct = lambda *_args, **_kwargs: -2.0

    agent._evaluate_matured_feedback_predictions({})

    outcome = agent._feedback["outcomes"][0]
    assert outcome["predicted_ai_verdict"] == "SHORT"  # registered verdict wins
    assert outcome["ai_model"] == "registered-model"
    assert "predicted_ai_provenance" not in outcome


@pytest.mark.unit
def test_matured_prediction_without_any_ai_source_settles_honestly_null():
    """No registered verdict and no sidecar entry → null stays null (never
    faked), the outcome just isn't part of the AI-sliced metrics."""
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    event_dt = pe._utc_now() - timedelta(days=2)
    key = f"GCT|{pe._iso(event_dt)}"
    agent._feedback = {
        "open_predictions": {key: _open_prediction("GCT", event_dt)},
        "outcomes": [],
    }
    agent._ai_cache = {}
    agent._compute_event_gap_move_pct = lambda *_args, **_kwargs: 1.0

    agent._evaluate_matured_feedback_predictions({})

    outcome = agent._feedback["outcomes"][0]
    assert outcome["predicted_ai_verdict"] is None
    assert agent._compute_track_record_metrics({})["ai_coverage"] == 0
