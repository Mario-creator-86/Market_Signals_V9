"""
tests/contract/test_pre_earnings_schema_contract.py
====================================================
Wires tests/contract/schemas/pre_earnings.schema.json — previously a dead,
unreferenced file — into an actual validation.

Pins two things the audit (T1) flagged:
1. The schema is LOADED and exercised (it was doc-mort: zero references).
2. The schema declares the field names the frontend actually reads, so a
   backend rename (the root cause of the dead "What To Watch" / "Rationale" /
   "Top Relevant News" cards, audit F1/F2/F3) is caught at contract level.

The payload fixture mirrors the real shape emitted by
PreEarningsAgent._build_item_for_symbol (agents/pre_earnings_agent.py:2055-2110)
and _build_payload (:3510). It is intentionally hand-built (not produced by
the agent) so the contract test stays deterministic and independent of the
agent's data-fetching path — it validates the SHAPE contract, not the data.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SCHEMA_PATH = ROOT / "tests" / "contract" / "schemas" / "pre_earnings.schema.json"

jsonschema = pytest.importorskip("jsonschema")


def _load_schema() -> dict:
    assert SCHEMA_PATH.exists(), f"schema missing at {SCHEMA_PATH}"
    with SCHEMA_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _realistic_payload() -> dict:
    """A representative /api/pre-earnings payload matching the real backend
    output shape (full-fidelity item + a disabled-state item)."""
    return {
        "state_revision": 42,
        "as_of_utc": "2026-08-01T12:00:00+00:00",
        "lookahead_days": 7,
        "refresh_seconds": 600,
        "items": [
            {
                "symbol": "AAPL",
                "earnings_time_utc": "2026-08-04T12:00:00+00:00",
                "session": "BMO",
                "confirmed": True,
                "gap_risk": 72,
                "gap_risk_level": "HIGH",
                "gap_risk_numeric": 72,
                "gap_risk_unavailable": False,
                "gap_risk_unavailable_reason": None,
                "gap_risk_history_source": "yfinance_fallback",
                "direction_bias": 0.13,
                "structured_direction_bias_score": 62,
                "structured_direction_label": "LONG",
                "final_direction_bias_score": 13,
                "llm_direction_score": -78,
                "direction_bias_score": 13,
                "direction_label": "NEUTRAL",
                "decision_alignment": "CONFLICT",
                "data_confidence": "HIGH",
                "data_message": None,
                "components": {
                    "historical": 80.0,
                    "analyst_revision": 72.5,
                    "options": 60.0,
                    "news_ai": 70.0,
                    "post_earnings_history": 55.0,
                    "range_proxy_move": 4.2,
                    "options_implied_move": 4.2,
                },
                "direction_components": {"news_ai": 70.0},
                "analyst_target_upside_pct": 12.4,
                "analyst_recommendation": "buy",
                "analyst_rec_mean": 1.8,
                "analyst_count": 34,
                "recent_reactions": [
                    {"event_utc": "2026-04-30T12:00:00+00:00", "move_pct": 3.1, "direction": "up"}
                ],
                "symbol_track_record": {"hits": 6, "total": 9},
                "track_record_overall": {"hits": 120, "total": 200},
                "expected_move_pct": 4.2,
                "range_proxy_pct": 4.2,
                "beat_rate_historical": 0.75,
                "beat_rate_beats": 6,
                "beat_rate_samples": 8,
                "beat_rate_source": "yfinance",
                "beat_rate_unavailable_reason": None,
                "historical_median_move_pct": 3.4,
                "avg_move_historical": 3.4,
                "historical_sample_size": 12,
                "sector": "Technology",
                "market_cap": 3000000000000.0,
                "market_cap_bucket": "mega",
                "coverage_score": 82,
                "coverage_label": "HIGH",
                "coverage_contributors": [
                    {"name": "event_window", "value": 1.0, "weight": 0.2, "note": "Event available"},
                    {"name": "news_context", "value": 0.7, "weight": 0.3, "note": "Three recent articles"},
                ],
                "opportunity_score": 74,
                "badges": ["HIGH_GAP_RISK", "AI_CONFLICT"],
                "why": ["Strong analyst consensus", "Positive pre-earnings sentiment"],
                "what_to_watch": ["Opening gap direction", "First 30m volume"],
                "bias_reasons": ["AI news sentiment 0.62 blended into direction."],
                "top_news": [
                    {"published_utc": "2026-08-01T10:00:00+00:00", "source": "Reuters", "title": "AAPL guidance raised", "url": "https://example.com/aapl"}
                ],
                "contributors": [
                    {"name": "historical_move", "value": 0.8, "weight": 0.35},
                    {"name": "news_intensity", "value": 0.7, "weight": 0.20},
                ],
                "confidence": {"gap_risk": {"score": 72, "ci_low": 60, "ci_high": 82}},
                "confidence_score": 70,
                "ai_sentiment": 0.62,
                "ai_decision": {"verdict": "LONG", "confidence": 0.7, "sentiment": 0.62, "rationale": "Bullish analyst + sentiment"},
                "ollama": {"sentiment": 0.62, "confidence": 0.7, "catalyst_label": "GUIDANCE"},
            },
            # Insufficient-data item: canonical UI fields remain present with
            # honest null/empty values; premium/AI enrichments stay optional.
            {
                "symbol": "FOO",
                "earnings_time_utc": None,
                "session": "UNKNOWN",
                "gap_risk": None,
                "gap_risk_level": "NONE",
                "gap_risk_unavailable": True,
                "gap_risk_unavailable_reason": "Insufficient historical samples",
                "direction_bias_score": None,
                "structured_direction_bias_score": None,
                "structured_direction_label": None,
                "llm_direction_score": None,
                "final_direction_bias_score": None,
                "decision_alignment": "NO_LLM",
                "direction_label": "INSUFFICIENT_DATA",
                "data_confidence": "NONE",
                "coverage_score": 10,
                "coverage_label": "LOW",
                "beat_rate_historical": None,
                "beat_rate_beats": 0,
                "beat_rate_samples": 0,
                "beat_rate_source": None,
                "beat_rate_unavailable_reason": "No completed reports with both estimated and reported EPS.",
                "range_proxy_pct": None,
                "opportunity_score": 0,
                "confidence_score": 0,
                "why": [],
                "what_to_watch": [],
                "bias_reasons": [],
                "top_news": [],
                "badges": [],
            },
        ],
        "pulse": {
            "summary": "Large-cap technology dominates the upcoming slate.",
            "sentiment": "bullish",
            "top_long": [{"symbol": "AAPL", "reason": "Positive revisions"}],
            "top_short": [],
            "catalysts": "Guidance and AI demand.",
            "risks": "Crowded positioning.",
            "model": "cloud-model",
            "generated_at": "2026-08-01T12:00:00+00:00",
        },
        "reported_today": [
            {
                "symbol": "MSFT",
                "earnings_time_utc": "2026-08-01T00:00:00+00:00",
                "session": "BMO",
                "gap_risk": 65,
                "direction_label": "LONG",
                "is_watchlist": True,
            }
        ],
        "flags": {
            "news_available": True,
            "earnings_available": True,
            "ollama_used": True,
            "pulse_used": True,
            "reported_today_count": 1,
        },
        "status": {
            "last_run_time": "2026-08-01T12:00:00+00:00",
            "last_success_time": "2026-08-01T12:00:00+00:00",
            "last_error": None,
            "next_run_in": 600,
            "last_recalibration_quarter": "2026Q3",
            "coverage_warmup": {"state": "idle", "processed": 0, "total": 0},
        },
        "disabled": False,
    }


@pytest.mark.contract
def test_schema_file_is_valid_json_schema():
    schema = _load_schema()
    # Must itself be a valid Draft 2020-12 schema.
    jsonschema.Draft202012Validator.check_schema(schema)


@pytest.mark.contract
def test_realistic_payload_validates_against_schema():
    schema = _load_schema()
    payload = _realistic_payload()
    jsonschema.validate(payload, schema)  # raises on violation


@pytest.mark.contract
def test_schema_rejects_missing_top_level_required():
    schema = _load_schema()
    payload = _realistic_payload()
    del payload["disabled"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


@pytest.mark.contract
def test_schema_rejects_missing_status_required_field():
    schema = _load_schema()
    payload = _realistic_payload()
    del payload["status"]["next_run_in"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


@pytest.mark.contract
def test_schema_rejects_item_without_symbol():
    schema = _load_schema()
    payload = _realistic_payload()
    payload["items"][0].pop("symbol")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


@pytest.mark.contract
@pytest.mark.parametrize(
    "field",
    [
        "direction_bias_score",
        "structured_direction_bias_score",
        "structured_direction_label",
        "llm_direction_score",
        "final_direction_bias_score",
        "decision_alignment",
        "direction_label",
        "coverage_score",
        "beat_rate_historical",
        "beat_rate_beats",
        "beat_rate_samples",
        "beat_rate_source",
        "range_proxy_pct",
        "why",
        "what_to_watch",
        "top_news",
    ],
)
def test_schema_rejects_missing_canonical_ui_item_field(field):
    schema = _load_schema()
    payload = _realistic_payload()
    payload["items"][0].pop(field)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


@pytest.mark.contract
def test_schema_rejects_wrong_typed_gap_risk():
    """Type drift is the real regression risk — gap_risk must stay int|null,
    not silently become a string or float."""
    schema = _load_schema()
    payload = _realistic_payload()
    payload["items"][0]["gap_risk"] = "72"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


@pytest.mark.contract
def test_schema_declares_frontend_critical_field_names():
    """The schema MUST declare the field names the frontend reads, so a
    backend rename is caught here instead of leaving a UI card dead
    (audit F1/F2/F3 root cause: contract drift)."""
    schema = _load_schema()
    item_props = schema["properties"]["items"]["items"]["properties"]
    required_frontend_fields = [
        "symbol",
        "gap_risk",
        "direction_bias_score",
        "structured_direction_bias_score",
        "final_direction_bias_score",
        "llm_direction_score",
        "decision_alignment",
        "direction_label",
        "gap_risk_level",
        "confidence_score",
        "coverage_score",
        "data_confidence",
        "why",
        "what_to_watch",
        "top_news",
        "badges",
        "ai_decision",
        "ai_sentiment",
        "beat_rate_historical",
        "beat_rate_beats",
        "beat_rate_samples",
        "beat_rate_source",
        "range_proxy_pct",
    ]
    for name in required_frontend_fields:
        assert name in item_props, f"schema missing frontend-critical field: {name}"


@pytest.mark.contract
def test_schema_declares_premium_top_level_fields():
    schema = _load_schema()
    props = schema["properties"]
    assert "pulse" in props
    assert "reported_today" in props
    assert "flags" in props
    flag_props = props["flags"]["properties"]
    assert "pulse_used" in flag_props
    assert "reported_today_count" in flag_props


@pytest.mark.contract
def test_schema_rejects_malformed_current_top_short():
    """REGRESSION (re-audit): `current_top_short` was accidentally nested INSIDE
    `top_short`'s schema definition (an unknown JSON Schema keyword there) instead
    of being a sibling property of the pulse object — so the validator never
    checked its type (it fell under pulse `additionalProperties: true`). A string
    where an array|null is expected must now be rejected."""
    schema = _load_schema()
    payload = _realistic_payload()
    payload["pulse"]["current_top_short"] = "not-an-array"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


@pytest.mark.contract
def test_schema_current_top_short_is_sibling_of_top_short():
    """The schema must declare `current_top_short` as a direct property of the
    pulse object (a sibling of `top_short`), not nested inside `top_short`."""
    schema = _load_schema()
    pulse_props = schema["properties"]["pulse"]["properties"]
    assert "current_top_short" in pulse_props, "current_top_short must be a pulse property"
    # And it must NOT be an (ignored) unknown keyword inside top_short's schema.
    top_short = pulse_props.get("top_short", {})
    assert "current_top_short" not in top_short, (
        "current_top_short must not be nested inside top_short's schema"
    )
