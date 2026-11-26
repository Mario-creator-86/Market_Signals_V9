import copy
import json
from datetime import datetime, timezone

from core.pre_earnings_evidence import attach_frozen_evidence
from tests.unit.test_pre_earnings_daily_top10 import _agent, _candidate
from agents.pre_earnings_agent import PreEarningsAgent


def test_threshold_rejects_without_filling_shortlist_and_preserves_existing_gates():
    agent = _agent()
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    item = _candidate(1, now)
    cfg = dict(PreEarningsAgent.DEFAULT_CONFIG)
    score = agent._prediction_score_details(item, cfg, now=now)['score']
    cfg['top_prediction_min_trader_score'] = score + 1
    assert agent._build_top_predictions([item], cfg, now=now) == []
    assert 'trader_score_below_threshold' in item['recommendation_blockers']
    cfg['top_prediction_min_trader_score'] = score
    assert len(agent._build_top_predictions([item], cfg, now=now)) == 1
    item['flags']['already_reported'] = True
    cfg['top_prediction_min_trader_score'] = 0
    assert agent._build_top_predictions([item], cfg, now=now) == []


def test_first_and_shortlist_evidence_freeze_at_different_decisions():
    item = {'symbol': 'TEST', 'direction_label': 'LONG', 'trader_score_breakdown': {'direction': 20}}
    first = {}
    attach_frozen_evidence(first, None, item, {}, 't1', False)
    item['direction_label'] = 'SHORT'
    item['trader_score_breakdown']['direction'] = 70
    second = {}
    attach_frozen_evidence(second, first, item, {}, 't2', True)
    assert second['first_direction_evidence']['observations']['direction_label'] == 'LONG'
    assert second['top_direction_evidence']['observations']['direction_label'] == 'SHORT'
    before = copy.deepcopy(second)
    third = {}
    item['direction_label'] = 'LONG'
    attach_frozen_evidence(third, second, item, {}, 't3', False)
    assert third == before
    third['first_direction_evidence']['observations']['direction_label'] = 'MUTATED'
    assert second == before


def test_legacy_capture_is_not_presented_as_original_prediction():
    payload = {}
    attach_frozen_evidence(payload, {'was_top_prediction': True},
                           {'confidence_score': float('nan')}, {}, 'now', True)
    assert payload['top_direction_evidence']['capture_quality'] == 'legacy_first_observed'
    assert payload['top_direction_evidence']['observations']['confidence_score'] is None
    json.dumps(payload, allow_nan=False)


def test_registered_evidence_reaches_settled_outcome(monkeypatch):
    from tests.unit.test_pre_earnings_track_record import _agent as feedback_agent, _item
    agent = feedback_agent(monkeypatch)
    cfg = agent._get_config()
    item = _item('TEST', bias=40, verdict='LONG', event_iso='2026-07-01T12:00:00+00:00')
    agent._register_open_predictions([item], cfg)
    saved = next(iter(agent._feedback['open_predictions'].values()))
    original = copy.deepcopy(saved['first_direction_evidence'])
    monkeypatch.setattr(agent, '_compute_event_gap_move_pct', lambda *a, **k: -3.)
    assert agent._evaluate_matured_feedback_predictions(cfg)
    outcome = agent._feedback['outcomes'][0]
    assert outcome['first_direction_evidence'] == original
    assert outcome['first_direction_evidence']['observations']['direction_bias_score'] == 40
    assert outcome['realized_signed_move_pct'] == -3.
