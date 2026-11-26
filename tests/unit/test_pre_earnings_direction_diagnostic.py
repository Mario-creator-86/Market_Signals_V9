import copy

from scripts.audit_pre_earnings_direction import audit, summarize, bias_side, wilson


def test_direction_diagnostic_exposes_majority_baseline_and_abstentions():
    rows = [
        {'predicted_direction_bias': 30, 'realized_signed_move_pct': 3},
        {'predicted_direction_bias': -30, 'realized_signed_move_pct': 2},
        {'predicted_direction_bias': 0, 'realized_signed_move_pct': 2},
        {'predicted_direction_bias': 30, 'realized_signed_move_pct': 0},
        {'predicted_direction_bias': 30, 'realized_signed_move_pct': None},
    ]
    result = summarize(rows, bias_side)
    assert result['accuracy_pct'] == 50
    assert result['always_long_accuracy_same_cases_pct'] == 100
    assert result['excluded'] == {'neutral_or_missing_direction': 1, 'flat_outcome': 1, 'missing_or_invalid_outcome': 1}
    assert wilson(0, 0) is None
    assert wilson(20, 39) == [36.2, 66.13]


def test_duplicate_events_are_quarantined_without_mutating_source():
    payload = {'outcomes': [
        {'symbol': 'A', 'event_utc': '2026-08-03T00:00:00Z', 'realized_signed_move_pct': 2},
        {'symbol': 'A', 'event_utc': '2026-08-03T20:00:00Z', 'realized_signed_move_pct': -2},
        {'symbol': 'B', 'event_utc': '2026-08-04', 'predicted_direction_bias': 30, 'realized_signed_move_pct': 1},
    ]}
    original = copy.deepcopy(payload)
    result = audit(payload)
    assert result['excluded_duplicate_or_missing_keys'] == 2
    assert result['cohorts']['all_recorded_bias']['directional_nonflat_calls'] == 1
    assert payload == original
