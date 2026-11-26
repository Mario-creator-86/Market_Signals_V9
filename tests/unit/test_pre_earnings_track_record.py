"""
tests/unit/test_pre_earnings_track_record.py
============================================
Tests for the Pre-Earnings History / Track-Record panel backend (Task #11).

Covers:
- B1: AI provenance (verdict/confidence/catalyst/model) persisted into the
  feedback store's open_predictions AND echoed onto matured outcomes, so
  historical predictions can be sliced by AI verdict and confidence band.
  Legacy outcomes (no ai_decision) carry ai_verdict=None and are excluded
  from per-AI slices but kept in the overall count.
- B2: per-verdict (LONG/SHORT/NEUTRAL) and per-confidence-band accuracy
  metrics with concrete numbers (n, hits, hit_rate%, avg_realized_move),
  NaN-filtered, skip-on-zero-move.
- B3: aggregate AI summary + future-recommendation prompt (separate from the
  per-ticker prompt) via ollama.generate, non-fatal, cached.
"""

import json
import math
import sys
import threading
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents import pre_earnings_agent as pe
from agents.pre_earnings_agent import PreEarningsAgent


def _ai_decision(verdict="LONG", confidence=0.9, catalyst="GUIDANCE", model="gpt-oss:120b"):
    return {
        "used": True,
        "verdict": verdict,
        "confidence": float(confidence),
        "sentiment": 0.6 if verdict == "LONG" else -0.6,
        "rationale": "Bullish guidance raise.",
        "catalyst_label": catalyst,
        "model": model,
    }


def _item(symbol="AAPL", bias=40, gap=70, verdict="LONG", confidence=0.9, event_iso=None):
    return {
        "symbol": symbol,
        "earnings_time_utc": event_iso or "2026-07-15T12:00:00+00:00",
        "session": "BMO",
        "sector": "TECH",
        "market_cap": 200_000_000_000,
        "gap_risk": gap,
        "direction_bias_score": bias,
        "direction_label": verdict,
        "ai_decision": _ai_decision(verdict, confidence),
    }


def _agent(monkeypatch):
    """Build an agent with an empty feedback store (no disk I/O)."""
    agent = PreEarningsAgent()
    agent._feedback = {
        "version": 1,
        "updated_utc": "2026-07-15T00:00:00+00:00",
        "open_predictions": {},
        "outcomes": [],
        "weights": {},
        "last_recalibration_quarter": None,
        "calibration_model": {},
    }
    return agent


# ---------------------------------------------------------------------------
# B1: persist AI provenance into the feedback store
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_register_open_predictions_persists_ai_provenance(monkeypatch):
    agent = _agent(monkeypatch)
    cfg = agent._get_config()
    items = [_item("AAPL", bias=40, verdict="LONG", confidence=0.9),
             _item("MSFT", bias=-35, verdict="SHORT", confidence=0.8, event_iso="2026-07-15T13:00:00+00:00")]
    changed = agent._register_open_predictions(items, cfg)
    assert changed
    op = agent._feedback["open_predictions"]
    aapl = next(v for v in op.values() if v["symbol"] == "AAPL")
    assert aapl["predicted_ai_verdict"] == "LONG"
    assert aapl["predicted_ai_confidence"] == pytest.approx(0.9, abs=1e-4)
    assert aapl["predicted_ai_catalyst"] == "GUIDANCE"
    assert aapl["ai_model"] == "gpt-oss:120b"
    msft = next(v for v in op.values() if v["symbol"] == "MSFT")
    assert msft["predicted_ai_verdict"] == "SHORT"


@pytest.mark.unit
def test_register_without_ai_decision_carries_null_provenance(monkeypatch):
    agent = _agent(monkeypatch)
    cfg = agent._get_config()
    it = _item("AAPL", bias=40)
    it.pop("ai_decision")  # legacy / no-AI item
    agent._register_open_predictions([it], cfg)
    op = agent._feedback["open_predictions"]
    aapl = next(v for v in op.values() if v["symbol"] == "AAPL")
    assert aapl.get("predicted_ai_verdict") is None
    assert aapl.get("ai_model") is None


@pytest.mark.unit
def test_matured_outcome_echoes_ai_provenance(monkeypatch):
    agent = _agent(monkeypatch)
    cfg = agent._get_config()
    # Register a prediction with AI provenance.
    items = [_item("AAPL", bias=40, verdict="LONG", confidence=0.85,
                   event_iso="2026-07-01T12:00:00+00:00")]
    agent._register_open_predictions(items, cfg)

    # Force maturation: patch the realized-move source to a known +3.0% (a hit
    # for a LONG call) and run evaluation.
    with patch.object(agent, "_compute_event_gap_move_pct", return_value=3.0):
        changed = agent._evaluate_matured_feedback_predictions(cfg)
    assert changed
    outcomes = agent._feedback["outcomes"]
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o["predicted_ai_verdict"] == "LONG"
    assert o["predicted_ai_confidence"] == pytest.approx(0.85, abs=1e-4)
    assert o["predicted_ai_catalyst"] == "GUIDANCE"
    assert o["ai_model"] == "gpt-oss:120b"
    assert o["realized_signed_move_pct"] == pytest.approx(3.0, abs=1e-4)


@pytest.mark.unit
def test_top10_entry_snapshot_is_monotonic_across_refreshes(monkeypatch):
    """A later refresh cannot erase or rewrite the recommendation a trader saw."""
    agent = _agent(monkeypatch)
    cfg = agent._get_config()
    event_iso = "2026-07-15T12:00:00+00:00"
    first = _item("AAPL", bias=-30, verdict="SHORT", event_iso=event_iso)
    first["trader_score"] = 48
    first["trader_score_band"] = "watch"

    monkeypatch.setattr(agent, "_build_top_predictions", lambda *a, **k: [])
    assert agent._register_open_predictions([first], cfg) is True

    promoted = _item("AAPL", bias=42, verdict="LONG", event_iso=event_iso)
    promoted["trader_score"] = 77
    promoted["trader_score_band"] = "high"
    monkeypatch.setattr(
        agent,
        "_build_top_predictions",
        lambda *a, **k: [{
            "symbol": "AAPL", "earnings_time_utc": event_iso, "rank": 4,
            "side": "LONG", "trader_score": 77, "score_band": "high",
        }],
    )
    assert agent._register_open_predictions([promoted], cfg) is True
    saved = next(iter(agent._feedback["open_predictions"].values()))
    first_seen = saved["top_prediction_first_seen_utc"]
    assert saved["was_top_prediction"] is True
    assert saved["top_prediction_direction"] == "LONG"
    assert saved["top_prediction_bias"] == 42
    assert saved["top_prediction_trader_score"] == 77
    assert saved["top_prediction_entry_rank"] == 4
    assert saved["top_prediction_best_rank"] == 4
    assert saved["top_prediction_snapshot_quality"] == "captured_at_entry"

    # The event falls out of the current list and its generic signal flips.
    # The captured Top-10 recommendation remains LONG/rank 4/score 77.
    later = _item("AAPL", bias=-55, verdict="SHORT", event_iso=event_iso)
    later["trader_score"] = 50
    monkeypatch.setattr(agent, "_build_top_predictions", lambda *a, **k: [])
    assert agent._register_open_predictions([later], cfg) is True
    saved = next(iter(agent._feedback["open_predictions"].values()))
    assert saved["was_top_prediction"] is True
    assert saved["top_prediction_direction"] == "LONG"
    assert saved["top_prediction_bias"] == 42
    assert saved["top_prediction_trader_score"] == 77
    assert saved["top_prediction_best_rank"] == 4
    assert saved["top_prediction_first_seen_utc"] == first_seen


@pytest.mark.unit
def test_matured_top10_outcome_uses_captured_direction(monkeypatch):
    agent = _agent(monkeypatch)
    cfg = agent._get_config()
    event_dt = pe._utc_now() - timedelta(days=3)
    key = f"MSFT|{pe._iso(event_dt)}"
    agent._feedback["open_predictions"][key] = {
        "symbol": "MSFT",
        "event_utc": pe._iso(event_dt),
        "session": "AMC",
        "sector": "TECH",
        "predicted_direction_bias": 35,  # generic record disagrees
        "was_top_prediction": True,
        "top_prediction_first_seen_utc": pe._iso(event_dt - timedelta(days=2)),
        "top_prediction_direction": "SHORT",
        "top_prediction_bias": -44,
        "top_prediction_trader_score": 82,
        "top_prediction_score_band": "high",
        "top_prediction_entry_rank": 2,
        "top_prediction_best_rank": 1,
        "top_prediction_latest_rank": 3,
        "top_prediction_rank": 1,
        "top_prediction_snapshot_quality": "captured_at_entry",
    }

    with patch.object(agent, "_compute_event_gap_move_pct", return_value=-4.25):
        assert agent._evaluate_matured_feedback_predictions(cfg) is True

    outcome = agent._feedback["outcomes"][0]
    assert outcome["top_prediction_direction"] == "SHORT"
    assert outcome["top_prediction_trader_score"] == 82
    assert outcome["top_prediction_best_rank"] == 1
    assert outcome["top_prediction_hit"] is True

# ---------------------------------------------------------------------------
# B2: per-verdict + per-confidence-band accuracy metrics
# ---------------------------------------------------------------------------



def _outcome(bias, signed_move, ai_verdict=None, ai_conf=None, ai_catalyst=None, symbol="X"):
    """Build a matured outcome row matching the on-disk shape."""
    import math as _m
    rm = signed_move
    if rm is None:
        rm_v = None
    else:
        rm_v = float(rm)
        if not _m.isfinite(rm_v):
            rm_v = float("nan")
    return {
        "symbol": symbol,
        "predicted_direction_bias": int(bias),
        "predicted_ai_verdict": ai_verdict,
        "predicted_ai_confidence": float(ai_conf) if ai_conf is not None else None,
        "predicted_ai_catalyst": ai_catalyst,
        "realized_signed_move_pct": (round(rm_v, 6) if rm_v is not None and _m.isfinite(rm_v) else rm_v),
        "realized_gap_move_pct": (round(abs(rm_v), 6) if rm_v is not None and _m.isfinite(rm_v) else rm_v),
    }


@pytest.mark.unit
def test_track_record_metrics_per_verdict_concrete_numbers(monkeypatch):
    agent = _agent(monkeypatch)
    cfg = agent._get_config()
    # LONG: 2 hits (+3, +5), 1 miss (-2)  -> 2/3 = 66.7%
    # SHORT: 1 hit (-4)                  -> 1/1 = 100%
    # NEUTRAL: bias 0 -> counted in overall total but not a directional call
    agent._feedback["outcomes"] = [
        _outcome(40, 3.0, "LONG", 0.9),    # hit
        _outcome(35, 5.0, "LONG", 0.8),    # hit
        _outcome(45, -2.0, "LONG", 0.85),  # miss
        _outcome(-50, -4.0, "SHORT", 0.7),  # hit
        _outcome(0, 1.0),  # NEUTRAL: no directional call -> excluded from LONG/SHORT
    ]
    m = agent._compute_track_record_metrics(cfg)
    long_v = m["per_verdict"]["LONG"]
    assert long_v["n"] == 3
    assert long_v["hits"] == 2
    assert long_v["hit_rate"] == pytest.approx(66.7, abs=0.1)
    short_v = m["per_verdict"]["SHORT"]
    assert short_v["n"] == 1 and short_v["hits"] == 1 and short_v["hit_rate"] == pytest.approx(100.0, abs=0.1)
    # NEUTRAL recorded but not a directional call.
    assert m["per_verdict"]["NEUTRAL"]["n"] == 1
    assert m["per_verdict"]["NEUTRAL"]["hit_rate"] is None
    # Overall directional: LONG(3) + SHORT(1) = 4 calls, 3 hits -> 75%
    assert m["overall"]["total"] == 4
    assert m["overall"]["hits"] == 3
    assert m["overall"]["hit_rate"] == pytest.approx(75.0, abs=0.1)


@pytest.mark.unit
def test_track_record_metrics_count_one_row_per_symbol_and_event_day(monkeypatch):
    agent = _agent(monkeypatch)
    older = _outcome(40, -2.0, "LONG", 0.7, symbol="NFLX")
    older.update({
        "event_utc": "2026-04-16T00:00:00+00:00",
        "evaluation_utc": "2026-04-18T00:00:00+00:00",
        "was_top_prediction": True,
        "top_prediction_rank": 4,
    })
    newer = _outcome(40, 3.0, "LONG", 0.9, symbol="NFLX")
    newer.update({
        "event_utc": "2026-04-16T09:00:00+00:00",
        "evaluation_utc": "2026-04-19T00:00:00+00:00",
    })
    agent._feedback["outcomes"] = [older, newer]

    snapshot = agent.get_feedback_snapshot(deduplicate_outcomes=True)
    metrics = agent._compute_track_record_metrics({}, outcomes=snapshot["outcomes"])

    assert len(snapshot["outcomes"]) == 1
    assert snapshot["outcomes"][0]["was_top_prediction"] is True
    assert snapshot["outcomes"][0]["top_prediction_rank"] == 4
    assert metrics["overall"]["total"] == 1
    assert metrics["overall"]["hits"] == 1


@pytest.mark.unit
def test_feedback_persistence_serializes_concurrent_writers(monkeypatch, tmp_path):
    agent = _agent(monkeypatch)
    target = tmp_path / "pre_earnings_feedback.json"
    monkeypatch.setattr(pe, "PRE_EARNINGS_FEEDBACK_PATH", target)
    errors = []

    def writer(index):
        try:
            with agent._feedback_guard():
                agent._feedback["outcomes"].append({
                    "symbol": f"T{index}",
                    "event_utc": f"2026-04-{index + 1:02d}T00:00:00+00:00",
                    "evaluation_utc": f"2026-04-{index + 2:02d}T00:00:00+00:00",
                    "predicted_direction_bias": 40,
                    "realized_signed_move_pct": 2.0,
                })
                agent._persist_feedback_store()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    stored = json.loads(target.read_text(encoding="utf-8"))
    assert len(stored["outcomes"]) == 8
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.unit
def test_top10_metrics_use_only_captured_top_direction(monkeypatch):
    agent = _agent(monkeypatch)
    cfg = agent._get_config()
    top_short = _outcome(40, -3.0, "LONG", 0.8, symbol="TOP")
    top_short.update({
        "was_top_prediction": True,
        "top_prediction_direction": "SHORT",
        "top_prediction_bias": -50,
    })
    top_zero = _outcome(35, 0.0, "LONG", 0.7, symbol="TOP_ZERO")
    top_zero.update({
        "was_top_prediction": True,
        "top_prediction_direction": "LONG",
        "top_prediction_bias": 35,
    })
    ordinary_long = _outcome(45, 2.0, "LONG", 0.8, symbol="ORDINARY")
    agent._feedback["outcomes"] = [top_short, top_zero, ordinary_long]
    agent._feedback["open_predictions"] = {
        "PENDING": {"was_top_prediction": True},
        "NOT_TOP": {"was_top_prediction": False},
    }

    top = agent._compute_track_record_metrics(cfg)["top_predictions"]
    assert top["settled"] == 2
    assert top["total"] == 1
    assert top["unscored"] == 1
    assert top["hits"] == 1
    assert top["hit_rate"] == 100.0
    assert top["pending"] == 1
    assert top["per_side"]["SHORT"]["total"] == 1
    assert top["per_side"]["SHORT"]["hits"] == 1
    assert top["per_side"]["LONG"]["total"] == 0


@pytest.mark.unit
def test_track_record_metrics_skips_nan_and_zero_move(monkeypatch):
    agent = _agent(monkeypatch)
    cfg = agent._get_config()
    agent._feedback["outcomes"] = [
        _outcome(40, 3.0, "LONG", 0.9),       # hit
        _outcome(40, float("nan"), "LONG", 0.8),  # NaN -> skipped
        _outcome(40, 0.0, "LONG", 0.7),         # zero move -> ambiguous, skipped
        _outcome(40, None, "LONG", 0.6),        # None -> skipped
    ]
    m = agent._compute_track_record_metrics(cfg)
    long_v = m["per_verdict"]["LONG"]
    assert long_v["n"] == 1, "NaN/None/zero moves must not count as calls"
    assert long_v["hits"] == 1
    assert m["overall"]["total"] == 1


@pytest.mark.unit
def test_track_record_metrics_per_confidence_band(monkeypatch):
    agent = _agent(monkeypatch)
    cfg = agent._get_config()
    # Band sourced from predicted_ai_confidence (0-1 -> 0-100).
    agent._feedback["outcomes"] = [
        _outcome(40, 3.0, "LONG", 0.92),   # band 85-100, hit
        _outcome(40, -2.0, "LONG", 0.60),  # band 50-70,  miss
        _outcome(-40, -3.0, "SHORT", 0.55),  # band 50-70, hit
        _outcome(40, 4.0, "LONG", 0.30),   # band 0-50, hit (fallback not needed)
    ]
    m = agent._compute_track_record_metrics(cfg)
    bands = {b["band"]: b for b in m["per_band"]}
    assert bands["85-100"]["n"] == 1 and bands["85-100"]["hits"] == 1
    assert bands["50-70"]["n"] == 2 and bands["50-70"]["hits"] == 1
    assert bands["0-50"]["n"] == 1 and bands["0-50"]["hits"] == 1
    assert bands["70-85"]["n"] == 0


@pytest.mark.unit
def test_track_record_metrics_legacy_outcomes_use_bias_as_confidence_fallback(monkeypatch):
    """Legacy outcomes (no ai_confidence) fall back to abs(bias) for banding."""
    agent = _agent(monkeypatch)
    cfg = agent._get_config()
    agent._feedback["outcomes"] = [
        _outcome(90, 3.0),   # abs(bias)=90 -> band 85-100, hit
        _outcome(40, -2.0),  # abs(bias)=40 -> band 0-50, miss
    ]
    m = agent._compute_track_record_metrics(cfg)
    bands = {b["band"]: b for b in m["per_band"]}
    assert bands["85-100"]["n"] == 1
    assert bands["0-50"]["n"] == 1
    assert m["ai_coverage"] == 0, "no outcomes carry ai_verdict"


# ---------------------------------------------------------------------------
# B2c: per-verdict bucketing uses the ±15 directional rule, not bias sign
# (re-audit P2 — a +2 lean is not a LONG call)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_track_record_metrics_buckets_by_threshold_not_bias_sign(monkeypatch):
    """The headline LONG/SHORT buckets must hold the same population the app
    calls directional: bias >= +15 is LONG, bias <= -15 is SHORT, everything
    between is NEUTRAL (unscored). Under the old sign bucketing, a +2 coin
    flip counted as a LONG call (live: the LONG card mixed ~1,051
    sub-threshold rows into ~926 real ones)."""
    agent = _agent(monkeypatch)
    cfg = agent._get_config()
    agent._feedback["outcomes"] = [
        _outcome(2, 3.0, "LONG", 0.9),    # sub-threshold lean -> NEUTRAL (was a LONG "hit" by sign)
        _outcome(15, 3.0, "LONG", 0.9),   # exactly +15 -> LONG edge, hit
        _outcome(14, 3.0, "LONG", 0.9),   # just under -> NEUTRAL
        _outcome(-14, -2.0, "SHORT", 0.8),  # sub-threshold -> NEUTRAL
        _outcome(-15, -2.0, "SHORT", 0.8),  # exactly -15 -> SHORT edge, hit
        _outcome(-2, -2.0, "SHORT", 0.8),   # sub-threshold lean -> NEUTRAL
    ]
    m = agent._compute_track_record_metrics(cfg)

    long_v = m["per_verdict"]["LONG"]
    assert long_v["n"] == 1 and long_v["hits"] == 1, "only bias >= +15 is a LONG call"
    assert long_v["directional"] is True
    short_v = m["per_verdict"]["SHORT"]
    assert short_v["n"] == 1 and short_v["hits"] == 1, "only bias <= -15 is a SHORT call"
    assert short_v["directional"] is True
    neutral_v = m["per_verdict"]["NEUTRAL"]
    assert neutral_v["n"] == 4, "sub-threshold leans land in NEUTRAL"
    assert neutral_v["hit_rate"] is None, "NEUTRAL stays unscored"
    assert neutral_v["directional"] is False, "NEUTRAL disclosed as non-directional"
    # Overall/bands stay coherent with the same bucketing: 2 directional calls.
    assert m["overall"]["total"] == 2
    assert m["overall"]["hits"] == 2
    assert sum(b["n"] for b in m["per_band"]) == 2


@pytest.mark.unit
def test_track_record_metrics_neutral_moves_still_disclosed(monkeypatch):
    """Sub-threshold rows are not hidden: their count and avg realized move
    stay visible in the NEUTRAL bucket even though they are never scored."""
    agent = _agent(monkeypatch)
    cfg = agent._get_config()
    agent._feedback["outcomes"] = [
        _outcome(2, 4.0),   # NEUTRAL, |move| 4.0
        _outcome(-5, -2.0),  # NEUTRAL, |move| 2.0
    ]
    m = agent._compute_track_record_metrics(cfg)
    neutral_v = m["per_verdict"]["NEUTRAL"]
    assert neutral_v["n"] == 2
    assert neutral_v["avg_realized_move"] == pytest.approx(3.0, abs=0.01)
    assert m["overall"]["total"] == 0
    assert m["overall"]["hit_rate"] is None


# ---------------------------------------------------------------------------
# B2d: bounded one-time retro backfill of AI verdicts onto historical settled
# outcomes from the durable per-event registry (re-audit P1)
# ---------------------------------------------------------------------------


def _settled_outcome(symbol, event_dt, *, verdict=None, bias=20, move=3.0):
    return {
        "symbol": symbol,
        "event_utc": pe._iso(event_dt),
        "evaluation_utc": pe._iso(event_dt + timedelta(hours=36)),
        "session": "AMC",
        "sector": "TECH",
        "cap_bucket": "SMALL",
        "predicted_gap_risk": 60,
        "predicted_direction_bias": bias,
        "predicted_ai_verdict": verdict,
        "predicted_ai_confidence": 0.7 if verdict else None,
        "predicted_ai_catalyst": "GUIDANCE" if verdict else None,
        "ai_model": "registered-model" if verdict else None,
        "realized_signed_move_pct": move,
        "realized_gap_move_pct": abs(move),
        "realized_high_gap": abs(move) >= 5.0,
    }


def _registry_entry(symbol, event_dt, verdict="LONG", confidence=0.81, model="glm-5.2"):
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


def _retro_agent(tmp_path, monkeypatch, outcomes, registry_entries):
    """Agent with settled outcomes + an AI registry, persisting to tmp_path."""
    import threading

    monkeypatch.setattr(
        pe, "PRE_EARNINGS_FEEDBACK_PATH", tmp_path / "pre_earnings_feedback.json"
    )
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    agent._feedback = {
        "version": 1,
        "updated_utc": "2026-07-15T00:00:00+00:00",
        "open_predictions": {},
        "outcomes": outcomes,
    }
    agent._ai_cache = {
        f"{e['symbol']}|{e['event_utc']}": e for e in registry_entries
    }
    return agent


@pytest.mark.unit
def test_retro_backfill_fills_only_matching_events(tmp_path, monkeypatch):
    """Only outcomes whose (symbol, event window) provably matches a durable
    registry entry get repaired; everything else stays honestly null."""
    event_dt = pe._utc_now() - timedelta(days=10)
    drifted_dt = event_dt + timedelta(days=40)  # different quarterly event
    outcomes = [
        _settled_outcome("GCT", event_dt),                    # matches registry -> filled
        _settled_outcome("DRIFT", drifted_dt),                # registry event 40d away -> null
        _settled_outcome("NOSRC", event_dt),                  # no registry entry -> null
        _settled_outcome("KEPT", event_dt, verdict="SHORT"),  # already has verdict -> untouched
    ]
    registry = [
        _registry_entry("GCT", event_dt),
        _registry_entry("DRIFT", event_dt, verdict="SHORT"),
    ]
    agent = _retro_agent(tmp_path, monkeypatch, outcomes, registry)

    assert agent._compute_track_record_metrics({})["ai_coverage"] == 1  # only KEPT
    changed = agent._retro_backfill_outcome_ai_provenance()
    assert changed is True

    by_symbol = {o["symbol"]: o for o in agent._feedback["outcomes"]}
    gct = by_symbol["GCT"]
    assert gct["predicted_ai_verdict"] == "LONG"
    assert gct["predicted_ai_confidence"] == pytest.approx(0.81, abs=0.001)
    assert gct["predicted_ai_catalyst"] == "EARNINGS_VOLATILITY"
    assert gct["ai_model"] == "glm-5.2"
    assert gct["predicted_ai_provenance"] == "ai_cache_backfill"
    # A 40-day drift is a different earnings event: never inherit the verdict.
    assert by_symbol["DRIFT"]["predicted_ai_verdict"] is None
    assert "predicted_ai_provenance" not in by_symbol["DRIFT"]
    # No registry entry at all: honest null, no fabricated provenance.
    assert by_symbol["NOSRC"]["predicted_ai_verdict"] is None
    assert "predicted_ai_provenance" not in by_symbol["NOSRC"]
    # Monotonic: a verdict of record is never rewritten or re-tagged.
    kept = by_symbol["KEPT"]
    assert kept["predicted_ai_verdict"] == "SHORT"
    assert kept["ai_model"] == "registered-model"
    assert "predicted_ai_provenance" not in kept
    # ai_coverage rises accordingly (KEPT + backfilled GCT).
    assert agent._compute_track_record_metrics({})["ai_coverage"] == 2
    # Persisted through the normal feedback path (file exists, holds the fix).
    persisted = json.loads(
        (tmp_path / "pre_earnings_feedback.json").read_text(encoding="utf-8")
    )
    persisted_gct = next(o for o in persisted["outcomes"] if o["symbol"] == "GCT")
    assert persisted_gct["predicted_ai_provenance"] == "ai_cache_backfill"


@pytest.mark.unit
def test_retro_backfill_ignores_verdicts_saved_after_the_event(tmp_path, monkeypatch):
    """A verdict written after the report is hindsight, not a prediction."""
    event_dt = pe._utc_now() - timedelta(days=10)
    hindsight = dict(_registry_entry("GCT", event_dt, verdict="LONG", confidence=0.9))
    hindsight["saved_at"] = pe._iso(event_dt + timedelta(days=1))
    agent = _retro_agent(
        tmp_path, monkeypatch, [_settled_outcome("GCT", event_dt)], [hindsight]
    )

    assert agent._retro_backfill_outcome_ai_provenance() is False
    assert not agent._feedback["outcomes"][0].get("predicted_ai_verdict")


@pytest.mark.unit
def test_retro_backfill_matches_within_14_day_window_and_picks_newest(tmp_path, monkeypatch):
    """Same quarterly event = <=14-day date drift; the newest saved registry
    entry wins, mirroring _lookup_cached_ai_for_item."""
    event_dt = pe._utc_now() - timedelta(days=10)
    older = _registry_entry("GCT", event_dt - timedelta(days=2), verdict="SHORT", confidence=0.55)
    newer = dict(_registry_entry("GCT", event_dt + timedelta(days=2), verdict="LONG", confidence=0.9))
    newer["saved_at"] = pe._iso(event_dt - timedelta(hours=6))
    agent = _retro_agent(
        tmp_path, monkeypatch, [_settled_outcome("GCT", event_dt)], [older, newer]
    )

    assert agent._retro_backfill_outcome_ai_provenance() is True
    outcome = agent._feedback["outcomes"][0]
    assert outcome["predicted_ai_verdict"] == "LONG"
    assert outcome["predicted_ai_confidence"] == pytest.approx(0.9, abs=0.001)


@pytest.mark.unit
def test_retro_backfill_no_source_stays_null_and_never_persists(tmp_path, monkeypatch):
    """No matching registry entry -> outcome stays null, returns False, and
    the feedback file is NOT rewritten (bounded one-time repair)."""
    event_dt = pe._utc_now() - timedelta(days=10)
    agent = _retro_agent(
        tmp_path, monkeypatch, [_settled_outcome("NOSRC", event_dt)], []
    )
    persist_calls = []
    real_persist = agent._persist_feedback_store
    agent._persist_feedback_store = lambda: (persist_calls.append(1), real_persist())[1]

    assert agent._retro_backfill_outcome_ai_provenance() is False
    outcome = agent._feedback["outcomes"][0]
    assert outcome["predicted_ai_verdict"] is None
    assert agent._compute_track_record_metrics({})["ai_coverage"] == 0
    assert persist_calls == [], "nothing matched -> the store must not be rewritten"
    assert not (tmp_path / "pre_earnings_feedback.json").exists()


@pytest.mark.unit
def test_retro_backfill_second_run_is_noop(tmp_path, monkeypatch):
    """The repair is bounded: after the first pass nothing is left to fill, so
    the second run changes nothing and does not persist again."""
    event_dt = pe._utc_now() - timedelta(days=10)
    agent = _retro_agent(
        tmp_path,
        monkeypatch,
        [_settled_outcome("GCT", event_dt)],
        [_registry_entry("GCT", event_dt)],
    )
    assert agent._retro_backfill_outcome_ai_provenance() is True

    persist_calls = []
    real_persist = agent._persist_feedback_store
    agent._persist_feedback_store = lambda: (persist_calls.append(1), real_persist())[1]
    assert agent._retro_backfill_outcome_ai_provenance() is False
    assert persist_calls == []


# ---------------------------------------------------------------------------
# B3: aggregate AI summary + future-recommendation prompt (separate prompt)
# ---------------------------------------------------------------------------


def _ollama_chat_response(text: str, model="gpt-oss:120b"):
    return {"response": text, "model": model, "done": True, "_meta": {"model": model}}


@pytest.mark.unit
def test_track_record_ai_summary_uses_separate_prompt(monkeypatch):
    agent = _agent(monkeypatch)
    # Cloud + a couple of realized outcomes so metrics are non-empty.
    monkeypatch.setattr(pe.ollama, "is_cloud_host", lambda: True)
    monkeypatch.setattr(pe.ollama, "get_best_model", lambda: "gpt-oss:120b")
    agent._feedback["outcomes"] = [
        _outcome(40, 3.0, "LONG", 0.9),
        _outcome(40, -2.0, "LONG", 0.6),
    ]
    captured = {}

    def fake_generate(prompt, model=None, system="", ui_call=False, timeout_override=None):
        captured["system"] = system
        captured["prompt"] = prompt
        captured["ui_call"] = ui_call
        return _ollama_chat_response(json.dumps({
            "summary": "LONG calls hit 50% on a tiny sample.",
            "recommendation": "Raise the confidence threshold for LONG entries.",
        }))

    monkeypatch.setattr(pe.ollama, "generate", fake_generate)
    out = agent.get_track_record_ai_summary(agent._get_config())
    assert isinstance(out, dict)
    # SEPARATE prompt: the system prompt must be the track-record/recommendation
    # prompt, NOT the per-ticker catalyst classifier prompt.
    assert "track record" in captured["system"].lower() or "recommendation" in captured["system"].lower()
    assert "catalyst_label" not in captured["prompt"], "must not reuse the per-ticker prompt"
    # Concise shape (Task #13): summary + one recommendation line; data_verification dropped.
    assert out.get("summary")
    assert out.get("recommendation")
    assert not out.get("data_verification"), "concise shape must drop data_verification"
    assert not out.get("future_recommendations"), "recommendation replaces future_recommendations"
    assert out.get("model") == "gpt-oss:120b"


@pytest.mark.unit
def test_track_record_ai_summary_non_fatal_on_error(monkeypatch):
    agent = _agent(monkeypatch)
    monkeypatch.setattr(pe.ollama, "is_cloud_host", lambda: True)
    monkeypatch.setattr(pe.ollama, "get_best_model", lambda: "gpt-oss:120b")
    agent._feedback["outcomes"] = [_outcome(40, 3.0, "LONG", 0.9)]

    def boom(*a, **kw):
        raise RuntimeError("simulated outage")
    monkeypatch.setattr(pe.ollama, "generate", boom)
    out = agent.get_track_record_ai_summary(agent._get_config())
    assert out is None, "AI summary must be non-fatal -> None on error"


@pytest.mark.unit
def test_track_record_ai_summary_cached(monkeypatch):
    agent = _agent(monkeypatch)
    monkeypatch.setattr(pe.ollama, "is_cloud_host", lambda: True)
    monkeypatch.setattr(pe.ollama, "get_best_model", lambda: "gpt-oss:120b")
    agent._feedback["outcomes"] = [_outcome(40, 3.0, "LONG", 0.9)]

    calls = {"n": 0}

    def fake_generate(prompt, model=None, system="", ui_call=False, timeout_override=None):
        calls["n"] += 1
        return _ollama_chat_response(json.dumps({
            "summary": "s", "recommendation": "r"}))
    monkeypatch.setattr(pe.ollama, "generate", fake_generate)

    a = agent.get_track_record_ai_summary(agent._get_config())
    b = agent.get_track_record_ai_summary(agent._get_config())
    assert a == b
    assert calls["n"] == 1, "second call within TTL must hit cache, not re-call the LLM"


@pytest.mark.unit
def test_track_record_ai_summary_is_not_asked_again_while_the_record_is_unchanged(monkeypatch):
    """Seen live on 2026-11-24: the model was asked for the same summary every five
    minutes, although the settled outcomes it describes change a few times a day."""
    agent = _agent(monkeypatch)
    monkeypatch.setattr(pe.ollama, "is_cloud_host", lambda: True)
    monkeypatch.setattr(pe.ollama, "get_best_model", lambda: "gpt-oss:120b")
    agent._feedback["outcomes"] = [_outcome(40, 3.0, "LONG", 0.9)]
    calls = {"n": 0}

    def fake_generate(prompt, model=None, system="", ui_call=False, timeout_override=None):
        calls["n"] += 1
        return _ollama_chat_response(json.dumps({"summary": f"summary {calls['n']}", "recommendation": "r"}))

    monkeypatch.setattr(pe.ollama, "generate", fake_generate)
    cfg = agent._get_config()

    first = agent.get_track_record_ai_summary(cfg, ttl_seconds=0)       # ttl 0: the short cache never answers
    again = agent.get_track_record_ai_summary(cfg, ttl_seconds=0)
    assert calls["n"] == 1 and again == first and first["summary"] == "summary 1"

    agent._feedback["outcomes"].append(_outcome(40, -2.0, "LONG", 0.6))  # an outcome settled: a new question
    changed = agent.get_track_record_ai_summary(cfg, ttl_seconds=0)
    assert calls["n"] == 2 and changed["summary"] == "summary 2"


@pytest.mark.unit
def test_track_record_ai_summary_that_failed_is_asked_again(monkeypatch):
    agent = _agent(monkeypatch)
    monkeypatch.setattr(pe.ollama, "is_cloud_host", lambda: True)
    monkeypatch.setattr(pe.ollama, "get_best_model", lambda: "gpt-oss:120b")
    agent._feedback["outcomes"] = [_outcome(40, 3.0, "LONG", 0.9)]
    answers = [{"error": "outage"}, _ollama_chat_response(json.dumps({"summary": "s", "recommendation": "r"}))]
    monkeypatch.setattr(pe.ollama, "generate", lambda *a, **k: answers.pop(0))
    cfg = agent._get_config()

    assert agent.get_track_record_ai_summary(cfg, ttl_seconds=0) is None
    assert agent.get_track_record_ai_summary(cfg, ttl_seconds=0)["summary"] == "s"


# ---------------------------------------------------------------------------
# B5: defensive NaN backfill on load (legacy outcomes with literal NaN)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sanitize_outcome_moves_clears_legacy_nan(monkeypatch):
    import math as _m
    agent = _agent(monkeypatch)
    nan = float("nan")
    outcomes = [
        _outcome(40, 3.0, "LONG", 0.9),                 # clean
        {"predicted_direction_bias": 40, "realized_signed_move_pct": nan,
         "realized_gap_move_pct": nan},                 # legacy NaN row
        {"predicted_direction_bias": -30, "realized_signed_move_pct": float("inf"),
         "realized_gap_move_pct": float("inf")},         # legacy Inf row
    ]
    sanitized = agent._sanitize_outcome_moves(outcomes)
    assert sanitized[0]["realized_signed_move_pct"] == pytest.approx(3.0)
    assert sanitized[1]["realized_signed_move_pct"] is None
    assert sanitized[1]["realized_gap_move_pct"] is None
    assert sanitized[2]["realized_signed_move_pct"] is None
