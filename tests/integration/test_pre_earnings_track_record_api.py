"""
tests/integration/test_pre_earnings_track_record_api.py
=======================================================
HTTP-level coverage for GET /api/pre-earnings/track-record (Task #11, B4).

The agent methods (B1/B2/B3/B5) are unit-tested in
tests/unit/test_pre_earnings_track_record.py, but the FastAPI handler at
api/main.py:6019 — which wires the metrics + bounded AI summary + recent
outcomes into a cache-first JSON payload — had no test. These tests pin the
endpoint contract: response shape, per-verdict/per-band presence, hit
computation on recent outcomes, NaN→null safety, the ai_summary=null graceful
path, and that the 60s endpoint-payload cache serves identical bytes on the
second call.
"""

import json

import pytest

pytestmark = pytest.mark.integration


def _metrics():
    return {
        "sample_size": 5,
        "ai_coverage": 4,
        "overall": {"total": 4, "hits": 3, "hit_rate": 75.0, "avg_realized_move": 2.1},
        "per_verdict": {
            "LONG": {"n": 2, "hits": 2, "hit_rate": 100.0, "avg_realized_move": 3.0},
            "SHORT": {"n": 2, "hits": 1, "hit_rate": 50.0, "avg_realized_move": -1.0},
            "NEUTRAL": {"n": 0, "hits": 0, "hit_rate": None, "avg_realized_move": 0.0},
        },
        "per_band": [
            {"band": "0-50", "n": 1, "hits": 0, "hit_rate": 0.0, "avg_realized_move": -1.0},
            {"band": "50-70", "n": 1, "hits": 1, "hit_rate": 100.0, "avg_realized_move": 2.0},
            {"band": "70-85", "n": 1, "hits": 1, "hit_rate": 100.0, "avg_realized_move": 3.0},
            {"band": "85-100", "n": 1, "hits": 1, "hit_rate": 100.0, "avg_realized_move": 4.0},
        ],
    }


def _ai_summary():
    return {
        "summary": "LONG calls well-calibrated; SHORT band 50-70 underperforming.",
        "recommendation": "Raise confidence floor for SHORT to 0.70 before sizing.",
        "model": "gpt-oss:120b",
        "generated_at": "2026-08-02T00:00:00+00:00",
    }


def _outcomes():
    """Five settled outcomes exercising hit/miss/neutral/no-move paths."""
    base = {
        "session": "BMO",
        "sector": "TECH",
        "cap_bucket": "MEGA",
        "predicted_gap_risk": 70,
        "predicted_ai_catalyst": "GUIDANCE",
    }
    return [
        {**base, "symbol": "AAPL", "event_utc": "2026-07-15T12:00:00+00:00",
         "evaluation_utc": "2026-07-16T12:00:00+00:00",
         "predicted_direction_bias": 40, "predicted_ai_verdict": "LONG",
         "predicted_ai_confidence": 0.9, "realized_signed_move_pct": 3.0,
         "realized_high_gap": True},
        {**base, "symbol": "MSFT", "event_utc": "2026-07-14T12:00:00+00:00",
         "evaluation_utc": "2026-07-15T12:00:00+00:00",
         "predicted_direction_bias": -50, "predicted_ai_verdict": "SHORT",
         "predicted_ai_confidence": 0.8, "realized_signed_move_pct": -2.0,
         "realized_high_gap": False},
        {**base, "symbol": "GOOG", "event_utc": "2026-07-13T12:00:00+00:00",
         "evaluation_utc": "2026-07-14T12:00:00+00:00",
         "predicted_direction_bias": 30, "predicted_ai_verdict": "LONG",
         "predicted_ai_confidence": 0.6, "realized_signed_move_pct": -1.0,
         "realized_high_gap": False},
        {**base, "symbol": "NEUT", "event_utc": "2026-07-12T12:00:00+00:00",
         "evaluation_utc": "2026-07-13T12:00:00+00:00",
         "predicted_direction_bias": 0, "predicted_ai_verdict": "NEUTRAL",
         "predicted_ai_confidence": 0.4, "realized_signed_move_pct": 2.0,
         "realized_high_gap": False},
        {**base, "symbol": "NOMV", "event_utc": "2026-07-11T12:00:00+00:00",
         "evaluation_utc": "2026-07-12T12:00:00+00:00",
         "predicted_direction_bias": 40, "predicted_ai_verdict": "LONG",
         "predicted_ai_confidence": 0.85, "realized_signed_move_pct": None,
         "realized_high_gap": None},
    ]


class _StubAgent:
    """Minimal stand-in exposing only the surface the handler touches."""

    def __init__(self, metrics, ai_summary, outcomes):
        self._metrics = metrics
        self._ai_summary = ai_summary
        self._feedback = {"outcomes": outcomes}

    def _get_config(self):
        return {"track_record_recent_rows": 200}

    def _compute_track_record_metrics(self, cfg):
        return self._metrics

    def get_track_record_ai_summary(self, cfg):
        return self._ai_summary


def _clear_endpoint_cache():
    from api.main import _endpoint_payload_clear

    _endpoint_payload_clear("pre_earnings_track_record")


def _patch_agent(monkeypatch, agent):
    monkeypatch.setattr(
        "agents.pre_earnings_agent.get_pre_earnings_agent",
        lambda *a, **kw: agent,
    )


def test_track_record_endpoint_shape_and_metrics(client, monkeypatch):
    _clear_endpoint_cache()
    _patch_agent(monkeypatch, _StubAgent(_metrics(), _ai_summary(), _outcomes()))

    r = client.get("/api/pre-earnings/track-record")
    assert r.status_code == 200
    body = r.json()

    assert body["directional_threshold"] == 15
    assert body["sample_size"] == 5
    assert body["ai_coverage"] == 4
    assert body["overall"] == _metrics()["overall"]
    assert set(body["per_verdict"].keys()) == {"LONG", "SHORT", "NEUTRAL"}
    assert body["per_direction"] == body["per_verdict"]
    assert "per_ai_verdict" in body
    assert "per_ai_band" in body
    assert "top_predictions" in body
    assert body["per_verdict"]["LONG"]["hit_rate"] == 100.0
    assert [b["band"] for b in body["per_band"]] == ["0-50", "50-70", "70-85", "85-100"]
    assert body["ai_summary"]["summary"].startswith("LONG calls")
    assert body["ai_summary"]["recommendation"]
    assert not body["ai_summary"].get("data_verification"), "concise shape drops data_verification"
    assert "as_of_utc" in body and body["as_of_utc"]


def test_track_record_endpoint_outcomes_hit_computation(client, monkeypatch):
    _clear_endpoint_cache()
    _patch_agent(monkeypatch, _StubAgent(_metrics(), _ai_summary(), _outcomes()))

    body = client.get("/api/pre-earnings/track-record").json()
    rows = {r["symbol"]: r for r in body["outcomes_recent"]}
    assert len(body["outcomes_recent"]) == 5

    # Sorted newest-first by evaluation_utc.
    assert body["outcomes_recent"][0]["symbol"] == "AAPL"

    assert rows["AAPL"]["hit"] is True       # LONG, move +3.0
    assert rows["MSFT"]["hit"] is True       # SHORT, move -2.0
    assert rows["GOOG"]["hit"] is False      # LONG, move -1.0 (miss)
    assert rows["NEUT"]["hit"] is None       # bias == 0
    assert rows["NOMV"]["hit"] is None       # realized move missing
    assert rows["NOMV"]["realized_signed_move_pct"] is None
    # Each recent row carries the AI-provenance fields the UI renders.
    for sym, row in rows.items():
        assert "predicted_ai_verdict" in row
        assert "predicted_ai_confidence" in row
        assert "predicted_ai_catalyst" in row


def test_track_record_endpoint_exposes_dedicated_top10_history_and_pending(client, monkeypatch):
    _clear_endpoint_cache()
    outcomes = _outcomes()
    # The generic prediction says LONG, but the captured Top-10 call was SHORT.
    outcomes[0].update({
        "was_top_prediction": True,
        "top_prediction_direction": "SHORT",
        "top_prediction_bias": -42,
        "top_prediction_trader_score": 81,
        "top_prediction_entry_rank": 4,
        "top_prediction_best_rank": 2,
        "top_prediction_first_seen_utc": "2026-07-10T09:00:00+00:00",
        "top_prediction_snapshot_quality": "captured_at_entry",
    })
    agent = _StubAgent(_metrics(), _ai_summary(), outcomes)
    agent._feedback["open_predictions"] = {
        "MSFT|2026-08-15": {
            "symbol": "MSFT",
            "event_utc": "2026-08-15T20:00:00+00:00",
            "session": "AMC",
            "was_top_prediction": True,
            "top_prediction_direction": "LONG",
            "top_prediction_bias": 38,
            "top_prediction_trader_score": 73,
            "top_prediction_entry_rank": 6,
            "top_prediction_best_rank": 5,
            "top_prediction_first_seen_utc": "2026-08-09T08:00:00+00:00",
            "top_prediction_snapshot_quality": "captured_at_entry",
        },
        "GOOG|2026-08-16": {"symbol": "GOOG", "was_top_prediction": False},
    }
    _patch_agent(monkeypatch, agent)

    body = client.get("/api/pre-earnings/track-record").json()
    assert len(body["top_predictions_history"]) == 1
    settled = body["top_predictions_history"][0]
    assert settled["symbol"] == "AAPL"
    assert settled["direction"] == "SHORT"
    assert settled["rank"] == 2
    assert settled["entry_rank"] == 4
    assert settled["trader_score"] == 81
    assert settled["realized_signed_move_pct"] == 3.0
    assert settled["hit"] is False
    assert settled["status"] == "SETTLED"

    assert len(body["top_predictions_pending"]) == 1
    pending = body["top_predictions_pending"][0]
    assert pending["symbol"] == "MSFT"
    assert pending["direction"] == "LONG"
    assert pending["best_rank"] == 5
    assert pending["trader_score"] == 73
    assert body["top_predictions"]["pending"] == 1


def test_track_record_endpoint_no_nan_literal_in_body(client, monkeypatch):
    _clear_endpoint_cache()
    outcomes = _outcomes()
    # Inject a non-finite value the handler must drop (mirrors legacy disc rows).
    outcomes[0]["realized_signed_move_pct"] = float("nan")
    _patch_agent(monkeypatch, _StubAgent(_metrics(), _ai_summary(), outcomes))

    raw = client.get("/api/pre-earnings/track-record").content
    assert b"NaN" not in raw and b"Infinity" not in raw and b"-Infinity" not in raw
    body = json.loads(raw)
    aapl = next(r for r in body["outcomes_recent"] if r["symbol"] == "AAPL")
    assert aapl["realized_signed_move_pct"] is None
    assert aapl["hit"] is None  # NaN move → no directional verdict


def test_track_record_endpoint_ai_summary_null_is_graceful(client, monkeypatch):
    _clear_endpoint_cache()
    _patch_agent(monkeypatch, _StubAgent(_metrics(), None, _outcomes()))

    body = client.get("/api/pre-earnings/track-record").json()
    assert body["ai_summary"] is None
    # Metrics + outcomes still served when AI is unavailable.
    assert body["sample_size"] == 5
    assert len(body["outcomes_recent"]) == 5


def test_track_record_endpoint_serves_cached_payload_on_second_call(client, monkeypatch):
    _clear_endpoint_cache()
    _patch_agent(monkeypatch, _StubAgent(_metrics(), _ai_summary(), _outcomes()))

    first = client.get("/api/pre-earnings/track-record")
    assert first.status_code == 200
    first_as_of = first.json()["as_of_utc"]

    # Second call must hit the 60s endpoint-payload cache → identical bytes.
    second = client.get("/api/pre-earnings/track-record")
    assert second.content == first.content
    assert second.json()["as_of_utc"] == first_as_of

def test_track_record_recent_rows_carry_threshold_based_label(client, monkeypatch):
    """Audit P0-3: the per-row label uses the app's ±15 directional threshold
    (not bias>0). AAPL +2 / PRM -5 rendered LONG/SHORT before this fix."""
    _clear_endpoint_cache()
    outcomes = [
        {"symbol": "AAPL", "event_utc": "2026-07-15T12:00:00+00:00",
         "evaluation_utc": "2026-07-16T12:00:00+00:00",
         "predicted_direction_bias": 2, "realized_signed_move_pct": 1.0},
        {"symbol": "PRM", "event_utc": "2026-07-14T12:00:00+00:00",
         "evaluation_utc": "2026-07-15T12:00:00+00:00",
         "predicted_direction_bias": -5, "realized_signed_move_pct": -1.0},
        {"symbol": "EDGL", "event_utc": "2026-07-13T12:00:00+00:00",
         "evaluation_utc": "2026-07-14T12:00:00+00:00",
         "predicted_direction_bias": 15, "realized_signed_move_pct": 1.0},
        {"symbol": "EDGS", "event_utc": "2026-07-12T12:00:00+00:00",
         "evaluation_utc": "2026-07-13T12:00:00+00:00",
         "predicted_direction_bias": -15, "realized_signed_move_pct": -1.0},
    ]
    _patch_agent(monkeypatch, _StubAgent(_metrics(), _ai_summary(), outcomes))

    body = client.get("/api/pre-earnings/track-record").json()
    rows = {r["symbol"]: r for r in body["outcomes_recent"]}
    assert rows["AAPL"]["label"] == "NEUTRAL"
    assert rows["PRM"]["label"] == "NEUTRAL"
    assert rows["EDGL"]["label"] == "LONG"
    assert rows["EDGS"]["label"] == "SHORT"


# ---------------------------------------------------------------------------
# Re-audit P1: AI-verdict provenance must be visible through the API
# ---------------------------------------------------------------------------


def test_track_record_recent_rows_carry_ai_provenance(client, monkeypatch):
    """Every recent-outcomes row must expose the full AI provenance triple —
    verdict, confidence, AND provenance — so the UI can distinguish a verdict
    registered at prediction time from one backfilled from the durable
    per-event registry ("ai_cache_backfill"). Additive, contract-safe."""
    _clear_endpoint_cache()
    outcomes = _outcomes()
    outcomes[0]["predicted_ai_provenance"] = "ai_cache_backfill"  # AAPL
    _patch_agent(monkeypatch, _StubAgent(_metrics(), _ai_summary(), outcomes))

    body = client.get("/api/pre-earnings/track-record").json()
    rows = {r["symbol"]: r for r in body["outcomes_recent"]}
    for sym, row in rows.items():
        assert "predicted_ai_verdict" in row
        assert "predicted_ai_confidence" in row
        assert "predicted_ai_provenance" in row
    assert rows["AAPL"]["predicted_ai_provenance"] == "ai_cache_backfill"
    assert rows["AAPL"]["predicted_ai_verdict"] == "LONG"
    assert rows["AAPL"]["predicted_ai_confidence"] == pytest.approx(0.9, abs=1e-4)
    # Rows without backfill carry an explicit null (never a missing key).
    assert rows["MSFT"]["predicted_ai_provenance"] is None


class _RetroStubAgent(_StubAgent):
    """Stub exposing the retro-backfill hook: fills one null-verdict outcome
    the way the real agent would from the durable per-event registry."""

    def __init__(self, metrics, ai_summary, outcomes):
        super().__init__(metrics, ai_summary, outcomes)
        self.retro_calls = 0

    def _retro_backfill_outcome_ai_provenance(self):
        self.retro_calls += 1
        changed = False
        for o in self._feedback.get("outcomes", []):
            if o.get("symbol") == "LEGACY" and not o.get("predicted_ai_verdict"):
                o["predicted_ai_verdict"] = "LONG"
                o["predicted_ai_confidence"] = 0.77
                o["predicted_ai_provenance"] = "ai_cache_backfill"
                changed = True
        return changed


def test_track_record_endpoint_runs_retro_backfill_before_serving(client, monkeypatch):
    """The endpoint must invoke the bounded retro repair before computing
    metrics/rows so historical outcomes settled without provenance surface
    their registry verdict through the API instead of reading null forever."""
    _clear_endpoint_cache()
    outcomes = _outcomes() + [
        {"symbol": "LEGACY", "event_utc": "2026-07-10T12:00:00+00:00",
         "evaluation_utc": "2026-07-11T12:00:00+00:00",
         "predicted_direction_bias": 30, "predicted_ai_verdict": None,
         "predicted_ai_confidence": None, "realized_signed_move_pct": 2.5,
         "realized_high_gap": False},
    ]
    agent = _RetroStubAgent(_metrics(), _ai_summary(), outcomes)
    _patch_agent(monkeypatch, agent)

    body = client.get("/api/pre-earnings/track-record").json()
    assert agent.retro_calls == 1, "retro repair runs once per payload computation"
    legacy = next(r for r in body["outcomes_recent"] if r["symbol"] == "LEGACY")
    assert legacy["predicted_ai_verdict"] == "LONG"
    assert legacy["predicted_ai_confidence"] == pytest.approx(0.77, abs=1e-4)
    assert legacy["predicted_ai_provenance"] == "ai_cache_backfill"


# ---------------------------------------------------------------------------
# Peer read-through grouping fields (owner requirement, 2026-11)
# ---------------------------------------------------------------------------


def test_track_record_recent_rows_carry_peer_grouping_fields(client, monkeypatch):
    """History must be groupable by sector/theme and by day client-side:
    every recent row exposes `sector` (pass-through), `peer_key_label`
    (null for legacy rows settled before peer keys existed — honest) and
    `event_day` (YYYY-MM-DD of event_utc). Additive, contract-safe."""
    _clear_endpoint_cache()
    outcomes = _outcomes()
    outcomes[0]["peer_key_label"] = "BTC Miners"  # grouped row (post-feature)
    _patch_agent(monkeypatch, _StubAgent(_metrics(), _ai_summary(), outcomes))

    body = client.get("/api/pre-earnings/track-record").json()
    rows = {r["symbol"]: r for r in body["outcomes_recent"]}
    for sym, row in rows.items():
        assert "sector" in row
        assert "peer_key_label" in row
        assert "event_day" in row
    assert rows["AAPL"]["peer_key_label"] == "BTC Miners"
    assert rows["AAPL"]["sector"] == "TECH"
    assert rows["AAPL"]["event_day"] == "2026-07-15"
    # Legacy rows (no peer_key_label on the outcome) read as explicit null.
    assert rows["MSFT"]["peer_key_label"] is None
    assert rows["MSFT"]["event_day"] == "2026-07-14"
    assert rows["NOMV"]["sector"] == "TECH"
    assert rows["NOMV"]["event_day"] == "2026-07-11"


def test_track_record_top10_history_sorted_by_day_desc_and_trader_score_desc_with_daily_ranks(client, monkeypatch):
    """Top 10 settled history must be ordered by event day descending, and within
    each day by Trader Score descending (de la mare la mic), with day_rank computed 1..N."""
    _clear_endpoint_cache()
    outcomes = [
        # 26 Aug events with various scores and out-of-order evaluations
        {
            "symbol": "NVDA", "event_utc": "2026-08-26T20:00:00+00:00",
            "evaluation_utc": "2026-08-27T08:00:00+00:00",
            "was_top_prediction": True, "top_prediction_direction": "LONG",
            "top_prediction_trader_score": 73, "realized_signed_move_pct": 6.0,
        },
        {
            "symbol": "HPQ", "event_utc": "2026-08-26T20:00:00+00:00",
            "evaluation_utc": "2026-08-27T09:00:00+00:00",
            "was_top_prediction": True, "top_prediction_direction": "SHORT",
            "top_prediction_trader_score": 59, "realized_signed_move_pct": -8.7,
        },
        {
            "symbol": "OKTA", "event_utc": "2026-08-26T20:00:00+00:00",
            "evaluation_utc": "2026-08-27T07:00:00+00:00",
            "was_top_prediction": True, "top_prediction_direction": "LONG",
            "top_prediction_trader_score": 67, "realized_signed_move_pct": 23.2,
        },
        # 25 Aug events
        {
            "symbol": "HEI", "event_utc": "2026-08-25T20:00:00+00:00",
            "evaluation_utc": "2026-08-26T08:00:00+00:00",
            "was_top_prediction": True, "top_prediction_direction": "LONG",
            "top_prediction_trader_score": 60, "realized_signed_move_pct": 4.5,
        },
        {
            "symbol": "DKS", "event_utc": "2026-08-25T20:00:00+00:00",
            "evaluation_utc": "2026-08-26T09:00:00+00:00",
            "was_top_prediction": True, "top_prediction_direction": "LONG",
            "top_prediction_trader_score": 64, "realized_signed_move_pct": -20.7,
        },
    ]
    agent = _StubAgent(_metrics(), _ai_summary(), outcomes)
    _patch_agent(monkeypatch, agent)

    body = client.get("/api/pre-earnings/track-record").json()
    top_hist = body["top_predictions_history"]
    assert len(top_hist) == 5

    # 26 Aug rows: NVDA (73, rank 1), OKTA (67, rank 2), HPQ (59, rank 3)
    assert top_hist[0]["symbol"] == "NVDA"
    assert top_hist[0]["event_day"] == "2026-08-26"
    assert top_hist[0]["trader_score"] == 73
    assert top_hist[0]["day_rank"] == 1

    assert top_hist[1]["symbol"] == "OKTA"
    assert top_hist[1]["event_day"] == "2026-08-26"
    assert top_hist[1]["trader_score"] == 67
    assert top_hist[1]["day_rank"] == 2

    assert top_hist[2]["symbol"] == "HPQ"
    assert top_hist[2]["event_day"] == "2026-08-26"
    assert top_hist[2]["trader_score"] == 59
    assert top_hist[2]["day_rank"] == 3

    # 25 Aug rows: DKS (64, rank 1), HEI (60, rank 2)
    assert top_hist[3]["symbol"] == "DKS"
    assert top_hist[3]["event_day"] == "2026-08-25"
    assert top_hist[3]["trader_score"] == 64
    assert top_hist[3]["day_rank"] == 1

    assert top_hist[4]["symbol"] == "HEI"
    assert top_hist[4]["event_day"] == "2026-08-25"
    assert top_hist[4]["trader_score"] == 60
    assert top_hist[4]["day_rank"] == 2

