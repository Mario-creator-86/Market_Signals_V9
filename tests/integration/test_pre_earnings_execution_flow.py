from datetime import datetime, timezone
import pytest
from core.market_hours import EASTERN_TZ
from core.autonomous_candidates import build_pre_earnings_candidate_signal
from core.capital_entry_guard import evaluate_capital_entry
from core.confidence_gates import ConfidenceGates
from core.config import ConfigManager


def test_pre_earnings_end_to_end_paper_execution_flow(monkeypatch):
    # Setup candidate item (BMO reporting tomorrow)
    item = {
        'symbol': 'BBW',
        'earnings_time_utc': '2026-08-27T00:00:00+00:00',
        'session': 'BMO',
        'event_status': 'UPCOMING',
        'recommendation_eligible': True,
        'gap_risk': 85,
        'direction_bias_score': 35,
        'direction_label': 'LONG',
        'coverage_score': 75,
        'data_confidence': 'HIGH',
        'expected_move_pct': 6.5,
    }
    cfg = {
        'pre_earnings_min_gap_risk': 65,
        'pre_earnings_min_abs_bias': 20,
        'pre_earnings_min_coverage_score': 50,
        'pre_earnings_min_score': 75.0,
        'mode': 'paper',
    }

    # 1. Build signal
    signal = build_pre_earnings_candidate_signal(item, 25.0, cfg)
    assert signal is not None
    assert signal['ticker'] == 'BBW'
    assert signal['action'] == 'BUY'
    assert signal['entry'] == 25.0
    assert signal['autonomous_source'] == 'pre_earnings'

    # Mock price integrity verification (as done by AgentManager._enrich_candidate_prices)
    signal['price_verified'] = True
    signal['price_integrity'] = {'status': 'verified', 'is_verified': True}
    signal['created_at'] = datetime.now(timezone.utc).isoformat()

    # Mock time to 14:30 ET on Wednesday (inside 2h pre-close window)
    fake_now = datetime(2026, 8, 26, 14, 30, tzinfo=EASTERN_TZ)
    import core.capital_entry_guard as ceg
    orig_is_win = ceg.is_pre_earnings_entry_window
    monkeypatch.setattr(ceg, 'is_pre_earnings_entry_window', lambda e, s, now=None, **a: orig_is_win(e, s, now=fake_now, **a))

    # 2. Capital Entry Guard evaluation
    capital_res = evaluate_capital_entry(signal, [], cfg)
    assert capital_res['allowed'] is True
    assert capital_res['check'] == 'pre_earnings'
    assert capital_res['paper_capital_experiment'] is True

    # 3. Confidence Gates evaluation (allows unvalidated fallback for pre_earnings autonomous source)
    gates = ConfidenceGates()
    cg_res = gates.evaluate(signal)
    assert cg_res['tier'] != 'SKIP'
    assert cg_res['adjusted_size_factor'] > 0.0
