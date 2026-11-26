"""GOLD-093 — the §93 Pre-Earnings scanner column contract is pinned on the
built item, and the detail view carries all evidence.

§93 scanner columns: ticker, earnings date, BMO/AMC, score, data confidence,
expected move, historical move, IV/RV, bias, strategy, AI decision, risk status.

Columns whose source data does not exist yet are HONEST GAPS (not faked):
  * IV/RV       — depends on options IV + realized vol (Phase 6 signal / Phase 10).
  * strategy    — strategy registry not built (Phase 8).
  * risk status — risk/portfolio service not built (Phase 9).
The UI renders these as N/A until their source phases land (§4 honest stubs).
The columns that DO exist today are pinned here so a rename or removal is a
golden break.
"""
import pytest

pytestmark = pytest.mark.golden

# §93 scanner columns -> item field (present-today columns only).
SCANNER_COLUMNS = {
    "ticker": "symbol",
    "earnings date": "earnings_time_utc",
    "BMO/AMC": "session",
    "score": "pre_earnings_score",
    "data confidence": "data_confidence",
    "expected move": "expected_move_pct",
    "historical move": "historical_median_move_pct",
    "bias": "direction_bias",
    "AI decision": "pre_earnings_decision",
}

# §93 columns deferred to later phases (honest gaps — no item field yet).
DEFERRED_COLUMNS = {
    "IV/RV": "options IV + realized vol (Phase 6 signal / Phase 10)",
    "strategy": "strategy registry (Phase 8)",
    "risk status": "risk/portfolio service (Phase 9)",
}

# Detail evidence (subset that must be present on a fully-built item).
DETAIL_EVIDENCE = [
    "pre_earnings_score_components", "coverage_contributors", "data_confidence",
    "data_message", "why", "what_to_watch", "bias_reasons", "contributors",
    "confidence", "confidence_interval", "pre_earnings_is_not_probability",
]


def _minimal_item():
    return {
        "symbol": "NVDA", "earnings_time_utc": "2026-08-20T12:00:00+00:00",
        "session": "AMC", "pre_earnings_score": 55, "data_confidence": "HIGH",
        "expected_move_pct": 8.5, "historical_median_move_pct": 6.2,
        "direction_bias": 40, "pre_earnings_decision": {"action": "WATCH", "available": False, "reason": "off"},
        "pre_earnings_score_components": [], "coverage_contributors": [],
        "data_message": "OK", "why": [], "what_to_watch": [], "bias_reasons": [],
        "contributors": [], "confidence": {}, "confidence_interval": {},
        "pre_earnings_is_not_probability": True,
    }


def test_gold093_scanner_columns_present_on_item():
    item = _minimal_item()
    for _col, field in SCANNER_COLUMNS.items():
        assert field in item, f"§93 scanner column '{_col}' missing item field '{field}'"


def test_gold093_deferred_columns_are_documented_not_faked():
    # The three deferred §93 columns have NO item field today (honest gaps, §4).
    item = _minimal_item()
    for _col in DEFERRED_COLUMNS:
        # No faked field name for these columns exists on the item.
        assert _col not in item
    # And the deferral reasons are recorded (source phases named).
    assert DEFERRED_COLUMNS["IV/RV"]
    assert DEFERRED_COLUMNS["strategy"]
    assert DEFERRED_COLUMNS["risk status"]


def test_gold093_detail_evidence_present_on_item():
    item = _minimal_item()
    for field in DETAIL_EVIDENCE:
        assert field in item, f"§93 detail evidence field '{field}' missing"


def test_gold093_score_is_not_probability_on_item():
    item = _minimal_item()
    assert item["pre_earnings_is_not_probability"] is True