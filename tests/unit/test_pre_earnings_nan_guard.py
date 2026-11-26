"""
tests/unit/test_pre_earnings_nan_guard.py
==========================================
Regression guard for the NaN-corruption bug (audit B1).

Root cause: yfinance OHLC bars can carry `float('nan')` for open/close on
illiquid tickers. `_compute_event_gap_move_pct` only guarded against
`close_prev in (None, 0.0)` — which does NOT match `nan` — so the computed
gap move became `nan`, propagated into persisted outcomes, and from there
into `avg_realized_move_pct` aggregations. `json.dump` then emits the
non-standard `NaN` literal, producing an RFC-8259-invalid JSON file on disc
(`data/pre_earnings.json`).

These tests pin three layers of defense:
1. `_compute_event_gap_move_pct` returns None when reference prices are NaN.
2. `_compute_live_calibration_metrics` skips non-finite realized moves so
   aggregations never become NaN (self-heals already-corrupted stores).
3. `_evaluate_matured_feedback_predictions` never persists a NaN outcome.
"""

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents import pre_earnings_agent as pe


# ═══════════════════════════════════════════════════════════════════════════════
# 1. _compute_event_gap_move_pct must reject NaN reference prices
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_event_gap_move_pct_returns_none_when_close_prev_is_nan():
    """yfinance can deliver NaN close_prev; the guard `in (None, 0.0)` does
    NOT catch NaN, so without an isfinite check the result would be NaN."""
    agent = pe.PreEarningsAgent()
    event_dt = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
    with patch.object(
        agent, "_get_event_reference_prices", return_value=(float("nan"), 100.0)
    ):
        result = agent._compute_event_gap_move_pct("TEST", event_dt, "BMO")
    assert result is None, f"expected None for NaN close_prev, got {result}"


@pytest.mark.unit
def test_event_gap_move_pct_returns_none_when_open_next_is_nan():
    agent = pe.PreEarningsAgent()
    event_dt = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
    with patch.object(
        agent, "_get_event_reference_prices", return_value=(100.0, float("nan"))
    ):
        result = agent._compute_event_gap_move_pct("TEST", event_dt, "BMO")
    assert result is None, f"expected None for NaN open_next, got {result}"


@pytest.mark.unit
def test_event_gap_move_pct_finite_when_prices_valid():
    agent = pe.PreEarningsAgent()
    event_dt = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
    with patch.object(
        agent, "_get_event_reference_prices", return_value=(100.0, 105.0)
    ):
        result = agent._compute_event_gap_move_pct("TEST", event_dt, "BMO")
    assert result is not None and math.isfinite(result)
    assert result == pytest.approx(5.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. _compute_live_calibration_metrics must not aggregate NaN
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_live_calibration_metrics_skip_nan_realized_moves():
    """Already-corrupted stores may contain NaN realized moves. Aggregation
    must filter them so avg_realized_move_pct is finite, not NaN."""
    agent = pe.PreEarningsAgent()
    agent._feedback["outcomes"] = [
        {
            "evaluation_utc": "2026-02-20T10:00:00+00:00",
            "predicted_gap_risk": 80,
            "predicted_prob_high_gap": 0.8,
            "predicted_direction_bias": 0,
            "realized_high_gap": True,
            "realized_gap_move_pct": float("nan"),  # corrupted
            "realized_signed_move_pct": float("nan"),
        },
        {
            "evaluation_utc": "2026-02-21T10:00:00+00:00",
            "predicted_gap_risk": 30,
            "predicted_prob_high_gap": 0.3,
            "predicted_direction_bias": 0,
            "realized_high_gap": False,
            "realized_gap_move_pct": 2.0,
            "realized_signed_move_pct": -1.2,
        },
    ]
    cfg = agent._get_config()
    model = {"sample_size": 2, "global_factor": 1.0, "group_factors": {}, "group_samples": {}}
    metrics = agent._compute_live_calibration_metrics(cfg, model)

    # Top-level aggregate must be finite (this is what gets persisted & serialized).
    assert math.isfinite(metrics["avg_realized_move_pct"]), metrics
    # Every bucket aggregate must be finite or None — never NaN.
    for b in metrics["buckets"]:
        val = b.get("avg_realized_move_pct")
        assert val is None or math.isfinite(val), b
    # Brier/MAE must be finite.
    assert math.isfinite(metrics["brier"])
    assert math.isfinite(metrics["mae"])
    # The NaN row must not poison the high-gap bucket's move average.
    high_bucket = next(b for b in metrics["buckets"] if b["bucket"] == "70-100")
    assert high_bucket["avg_realized_move_pct"] is None or math.isfinite(high_bucket["avg_realized_move_pct"])


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Persisted outcomes must never contain NaN (JSON-serializable strictly)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_evaluate_matured_predictions_no_nan_outcome(tmp_path):
    """A matured prediction whose gap move resolves to NaN (e.g. illiquid
    ticker) must NOT produce a NaN outcome — it must be skipped (None)."""
    agent = pe.PreEarningsAgent()
    # Force a matured prediction (>36h old) for an illiquid ticker.
    event_dt = datetime.now(timezone.utc) - timedelta(days=5)
    agent._feedback["open_predictions"] = {
        "TEST|2026-01-05": {
            "symbol": "TEST",
            "event_utc": event_dt.isoformat(),
            "session": "BMO",
            "predicted_gap_risk": 70,
            "predicted_direction_bias": 40,
            "predicted_prob_high_gap": 0.7,
            "contributors": {},
            "sector": "UNKNOWN",
            "cap_bucket": "UNKNOWN",
        }
    }
    agent._feedback["outcomes"] = []

    # Simulate yfinance returning NaN reference prices.
    with patch.object(
        pe.PreEarningsAgent,
        "_compute_event_gap_move_pct",
        return_value=float("nan"),
    ):
        agent._evaluate_matured_feedback_predictions(agent._get_config())

    outcomes = agent._feedback.get("outcomes", [])
    # No outcome should have been persisted for a NaN move.
    assert outcomes == [], f"expected no outcome for NaN move, got {outcomes}"
    # And the prediction must remain open (not staled-out as a NaN realization).
    # (It may be stale-dropped after 21d, but not here at 5d.)
    assert "TEST|2026-01-05" in agent._feedback.get("open_predictions", {})


@pytest.mark.unit
def test_outcome_json_strict_serializable_no_nan_literal():
    """Any persisted outcome must round-trip through strict JSON (no NaN
    literal). Guards the on-disc file contract."""
    agent = pe.PreEarningsAgent()
    agent._feedback["outcomes"] = [
        {
            "evaluation_utc": "2026-02-20T10:00:00+00:00",
            "predicted_gap_risk": 80,
            "predicted_prob_high_gap": 0.8,
            "predicted_direction_bias": 0,
            "realized_high_gap": True,
            "realized_gap_move_pct": 9.0,
            "realized_signed_move_pct": 4.0,
        }
    ]
    cfg = agent._get_config()
    model = {"sample_size": 1, "global_factor": 1.0, "group_factors": {}, "group_samples": {}}
    metrics = agent._compute_live_calibration_metrics(cfg, model)
    blob = json.dumps(metrics)  # default json.dumps rejects NaN by default? No — it allows.
    # The hard contract: the serialized form must NOT contain a NaN/Infinity literal.
    assert "NaN" not in blob
    assert "Infinity" not in blob
    # And must be parseable by a strict parser (raises on NaN).
    import json as _json
    _json.loads(blob)  # would raise if strict? Actually Python allows. Use parse_constant guard:
    strict_blob = _json.dumps(metrics, allow_nan=False)
    _json.loads(strict_blob)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Persist-boundary backstop: json.dump must refuse to write a NaN literal
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
def test_persist_locked_refuses_to_write_nan_literal(tmp_path, monkeypatch):
    """REGRESSION (re-audit, defense-in-depth layer 4): even if a NaN escapes the
    source guards (layers 1-3), the persist boundary must NOT write a non-standard
    `NaN` literal to the on-disc JSON (RFC-8259-invalid). `json.dump` with
    `allow_nan=False` raises, which `_persist_locked` catches (logs + skips), so
    the output file is never corrupted with a NaN literal."""
    output_path = tmp_path / "pre_earnings.json"
    monkeypatch.setattr(pe, "PRE_EARNINGS_OUTPUT_PATH", output_path)
    monkeypatch.setattr(pe, "PRE_EARNINGS_AI_CACHE_PATH", tmp_path / "ai_cache.json")
    agent = pe.PreEarningsAgent()
    # Inject a NaN that bypasses the source guards (simulates a guard escape).
    agent._state["items"] = [{"symbol": "TEST", "gap_risk": float("nan")}]
    agent._state["state_revision"] = 1
    # Must not raise (persist failure is caught + logged internally).
    agent._persist_locked()
    # The output file must NOT contain a NaN literal. Either it was not written
    # (persist skipped on the ValueError) or, if written, it must be NaN-free.
    if output_path.exists():
        blob = output_path.read_bytes()
        assert b"NaN" not in blob, "persist must not write a NaN literal to disc"
        assert b"Infinity" not in blob


@pytest.mark.unit
def test_persist_feedback_store_refuses_to_write_nan_literal(tmp_path, monkeypatch):
    """Same persist-boundary backstop for the feedback store sidecar."""
    feedback_path = tmp_path / "pre_earnings_feedback.json"
    monkeypatch.setattr(pe, "PRE_EARNINGS_FEEDBACK_PATH", feedback_path)
    agent = pe.PreEarningsAgent()
    agent._feedback["outcomes"] = [{"realized_gap_move_pct": float("nan")}]
    agent._persist_feedback_store()  # must not raise
    if feedback_path.exists():
        blob = feedback_path.read_bytes()
        assert b"NaN" not in blob, "feedback persist must not write a NaN literal"
        assert b"Infinity" not in blob