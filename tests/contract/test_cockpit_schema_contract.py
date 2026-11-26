import json
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "tests" / "contract" / "schemas" / "cockpit.schema.json").read_text(encoding="utf-8"))


def test_cockpit_schema_is_valid():
    Draft202012Validator.check_schema(SCHEMA)


def test_cockpit_endpoint_matches_schema(client):
    payload = {
        "schema_version": 1,
        "state": "SUCCESS",
        "generated_at": "2026-08-13T12:00:00+00:00",
        "header": {
            "version": "V9", "market": {}, "autonomy": {}, "ai": {}, "regime": {}, "capital": {}, "risk": {},
            "kill_switch_active": False, "reduce_fraction_pct": None,
        },
        "sections": {
            name: {"state": "EMPTY", "items": [], "count": 0}
            for name in ("opportunities", "risks", "upcoming_earnings", "sentiment_shifts", "major_news", "ai_decisions", "active_positions", "strategy_degradation", "research_activity")
        },
        "strategy_lab": {"state": "EMPTY", "champions": [], "challengers": [], "shadow": [], "degraded": [], "leaderboard": [], "promotion_history": [], "rollback_history": [], "ai_quant_commentary": {}},
        "decision_ledger": {"state": "EMPTY", "items": [], "count": 0},
        "research_factory": {"state": "EMPTY", "activity": [], "capabilities": []},
        "system_health": {"state": "SUCCESS", "overall": "DEGRADED", "services": [], "counts": {}, "checked_at": "2026-08-13T12:00:00+00:00"},
        "source_errors": {}, "deferred": {},
    }
    with patch("api.main._endpoint_payload_get", return_value=None), \
         patch("api.main._endpoint_payload_set"), \
         patch("api.main.get_cockpit_payload", return_value=payload):
        response = client.get("/api/cockpit")

    assert response.status_code == 200
    Draft202012Validator(SCHEMA, format_checker=FormatChecker()).validate(response.json())
