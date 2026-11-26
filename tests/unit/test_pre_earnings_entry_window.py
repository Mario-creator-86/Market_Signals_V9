from datetime import datetime, timezone, date, time
from zoneinfo import ZoneInfo
import pytest

from core.market_hours import (
    EASTERN_TZ,
    get_next_us_equity_trading_day,
    get_previous_us_equity_trading_day,
    is_pre_earnings_entry_window,
    is_us_equity_trading_day,
)
from core.autonomous_candidates import (
    assess_pre_earnings_candidate,
    build_pre_earnings_candidate_signal,
)
from core.capital_entry_guard import evaluate_capital_entry
from core.agent_manager import AgentManager


def test_trading_day_helpers():
    wed = date(2026, 8, 26)
    thu = date(2026, 8, 27)
    fri = date(2026, 8, 28)
    sat = date(2026, 8, 29)
    sun = date(2026, 8, 30)
    mon = date(2026, 8, 31)

    assert is_us_equity_trading_day(wed) is True
    assert is_us_equity_trading_day(thu) is True
    assert is_us_equity_trading_day(fri) is True
    assert is_us_equity_trading_day(sat) is False
    assert is_us_equity_trading_day(sun) is False
    assert is_us_equity_trading_day(mon) is True

    assert get_previous_us_equity_trading_day(mon) == fri
    assert get_previous_us_equity_trading_day(thu) == wed
    assert get_next_us_equity_trading_day(fri) == mon


def test_amc_earnings_today_in_2h_window_opens():
    now_1430 = datetime(2026, 8, 26, 14, 30, tzinfo=EASTERN_TZ)
    is_open, reason, meta = is_pre_earnings_entry_window(
        '2026-08-26T00:00:00Z', 'AMC', now=now_1430
    )

    assert is_open is True
    assert meta['status'] == 'window_open'
    assert 'pre_earnings_window_open' in reason
    assert meta['session'] == 'AMC'
    assert meta['target_entry_date'] == '2026-08-26'


def test_amc_earnings_today_before_2h_window_is_waiting():
    now_1030 = datetime(2026, 8, 26, 10, 30, tzinfo=EASTERN_TZ)
    is_open, reason, meta = is_pre_earnings_entry_window(
        '2026-08-26T00:00:00Z', 'AMC', now=now_1030
    )

    assert is_open is False
    assert meta['status'] == 'waiting_for_pre_close_window'
    assert 'waiting for 2h pre-close entry window' in reason


def test_bmo_earnings_tomorrow_in_2h_window_today_opens():
    now_wed_1430 = datetime(2026, 8, 26, 14, 30, tzinfo=EASTERN_TZ)
    is_open, reason, meta = is_pre_earnings_entry_window(
        '2026-08-27T00:00:00Z', 'BMO', now=now_wed_1430
    )

    assert is_open is True
    assert meta['status'] == 'window_open'
    assert 'pre_earnings_window_open' in reason
    assert meta['session'] == 'BMO'
    assert meta['target_entry_date'] == '2026-08-26'


def test_bfo_synonym_bmo_earnings_tomorrow():
    now_wed_1500 = datetime(2026, 8, 26, 15, 0, tzinfo=EASTERN_TZ)
    is_open, reason, meta = is_pre_earnings_entry_window(
        '2026-08-27T00:00:00Z', 'BFO', now=now_wed_1500
    )

    assert is_open is True
    assert meta['status'] == 'window_open'
    assert meta['session'] == 'BMO'
    assert meta['target_entry_date'] == '2026-08-26'


def test_bmo_earnings_monday_opens_on_friday():
    now_fri_1430 = datetime(2026, 8, 28, 14, 30, tzinfo=EASTERN_TZ)
    is_open, reason, meta = is_pre_earnings_entry_window(
        '2026-08-31T00:00:00Z', 'BMO', now=now_fri_1430
    )

    assert is_open is True
    assert meta['status'] == 'window_open'
    assert meta['target_entry_date'] == '2026-08-28'

    now_wed_1430 = datetime(2026, 8, 26, 14, 30, tzinfo=EASTERN_TZ)
    is_open_wed, reason_wed, meta_wed = is_pre_earnings_entry_window(
        '2026-08-31T00:00:00Z', 'BMO', now=now_wed_1430
    )
    assert is_open_wed is False
    assert meta_wed['status'] == 'future_entry_date'
    assert 'target entry date is 2026-08-28' in reason_wed


def test_bmo_earnings_today_morning_is_passed():
    now_wed_1430 = datetime(2026, 8, 26, 14, 30, tzinfo=EASTERN_TZ)
    is_open, reason, meta = is_pre_earnings_entry_window(
        '2026-08-26T00:00:00Z', 'BMO', now=now_wed_1430
    )

    assert is_open is False
    assert 'BMO earnings is today morning' in reason


def test_assess_pre_earnings_candidate_with_entry_window():
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
    }
    cfg = {
        'pre_earnings_min_gap_risk': 65,
        'pre_earnings_min_abs_bias': 20,
        'pre_earnings_min_coverage_score': 50,
        'pre_earnings_min_score': 75.0,
    }

    now_1430 = datetime(2026, 8, 26, 14, 30, tzinfo=EASTERN_TZ)
    assessment = assess_pre_earnings_candidate(
        item, 25.0, cfg, require_entry_window=True, now=now_1430
    )

    assert assessment['eligible'] is True
    assert assessment['ranking_eligible'] is True
    assert assessment['executable_now'] is True
    assert assessment['eligibility_scope'] == 'execution'
    assert assessment['window_open'] is True
    assert assessment['entry_window']['status'] == 'window_open'
    assert assessment['entry_window']['target_entry_date'] == '2026-08-26'

    now_1030 = datetime(2026, 8, 26, 10, 30, tzinfo=EASTERN_TZ)
    assessment_morning = assess_pre_earnings_candidate(
        item, 25.0, cfg, require_entry_window=True, now=now_1030
    )

    assert assessment_morning['eligible'] is False
    assert assessment_morning['ranking_eligible'] is True
    assert assessment_morning['executable_now'] is False
    assert assessment_morning['window_open'] is False
    assert assessment_morning['first_blocker']['code'] == 'outside_pre_earnings_window'


def test_capital_entry_guard_admits_pre_earnings_in_window(monkeypatch):
    signal = {
        'ticker': 'BBW',
        'action': 'BUY',
        'direction': 'LONG',
        'entry': 25.0,
        'sl': 23.5,
        'tp': 28.0,
        'price_verified': True,
        'price_integrity': {'status': 'verified', 'is_verified': True},
        'created_at': datetime.now(timezone.utc).isoformat(),
        'autonomous_source': 'pre_earnings',
        'strategy_type': 'PRE_EARNINGS_AI_LONG',
        'signal_mode': 'AUTONOMOUS_PRE_EARNINGS',
        'pre_earnings_context': {
            'symbol': 'BBW',
            'earnings_time_utc': '2026-08-27T00:00:00+00:00',
            'session': 'BMO',
        },
    }
    execution_cfg = {
        'mode': 'paper',
        'allow_unvalidated_autonomous_fallback': True,
    }

    fake_now = datetime(2026, 8, 26, 14, 30, tzinfo=EASTERN_TZ)
    import core.capital_entry_guard as ceg
    orig_is_win = ceg.is_pre_earnings_entry_window
    monkeypatch.setattr(ceg, 'is_pre_earnings_entry_window', lambda e, s, now=None, **a: orig_is_win(e, s, now=fake_now, **a))

    result = evaluate_capital_entry(signal, [], execution_cfg)

    assert result['allowed'] is True
    assert result['check'] == 'pre_earnings'
    assert result['paper_capital_experiment'] is True
    assert result['checks']['pre_earnings_window']['allowed'] is True


def test_autonomous_shortlist_prioritizes_open_window_over_future_high_gap():
    fake_now = datetime(2026, 8, 26, 14, 30, tzinfo=EASTERN_TZ)
    current = {
        'symbol': 'SIMPE',
        'price': 100.0,
        'earnings_time_utc': '2026-08-26T00:00:00+00:00',
        'session': 'AMC',
        'event_status': 'UPCOMING',
        'recommendation_eligible': True,
        'gap_risk': 30,
        'direction_bias_score': 30,
        'direction_label': 'LONG',
        'coverage_score': 100,
        'data_confidence': 'HIGH',
        'expected_move_pct': 6.0,
    }
    future = [
        {
            **current,
            'symbol': f'FUT{index}',
            'earnings_time_utc': '2026-09-10T00:00:00+00:00',
            'gap_risk': 100,
        }
        for index in range(25)
    ]
    cfg = {
        'pre_earnings_min_gap_risk': 25,
        'pre_earnings_min_abs_bias': 15,
        'pre_earnings_min_coverage_score': 50,
        'pre_earnings_min_score': 65,
    }
    manager = AgentManager.__new__(AgentManager)
    manager._autonomous_source_stats = {}
    manager._load_pre_earnings_items = lambda: future + [current]

    candidates = manager._pre_earnings_candidates(cfg, now=fake_now)

    assert [candidate['ticker'] for candidate in candidates] == ['SIMPE']
    assert candidates[0]['pre_earnings_window_open'] is True
    stats = manager._autonomous_source_stats['pre_earnings']
    assert stats['evaluations'][0]['ticker'] == 'SIMPE'
    assert stats['evaluations'][0]['executable_now'] is True
