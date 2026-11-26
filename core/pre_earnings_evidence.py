"""Immutable observations for evaluating earnings recommendations."""
import copy
import math


def clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def capture(item, config, captured_at, legacy):
    fields = (
        'symbol', 'earnings_time_utc', 'session', 'event_status', 'event_source',
        'event_source_url', 'event_verified_at', 'direction_label',
        'structured_direction_bias_score', 'final_direction_bias_score',
        'direction_bias_score', 'decision_alignment', 'trader_score',
        'trader_score_breakdown', 'trader_score_penalties', 'coverage_score',
        'confidence_score', 'gap_risk_numeric', 'recommendation_eligible',
        'recommendation_blockers', 'analyst_target_upside_pct', 'analyst_rec_mean',
        'beat_rate_historical', 'beat_rate_samples', 'historical_median_move_pct',
        'historical_sample_size', 'range_proxy_pct', 'range_proxy_source', 'peer_key',
    )
    ai = item.get('ai_decision') if isinstance(item.get('ai_decision'), dict) else {}
    return clean({
        'schema_version': 1, 'captured_at_utc': captured_at,
        'capture_quality': 'legacy_first_observed' if legacy else 'captured_at_decision',
        'target': 'EVENT_GAP_DIRECTION', 'probability_calibrated': False,
        'observations': {key: copy.deepcopy(item.get(key)) for key in fields},
        'policy': {key: value for key, value in config.items() if key.startswith('top_prediction')},
        'ai': {key: copy.deepcopy(ai.get(key)) for key in (
            'used', 'verdict', 'sentiment', 'confidence', 'model', 'generated_at',
            'rationale', 'news_fingerprint', 'news_evidence_ids',
        )},
        'news': [{key: row.get(key) for key in (
            'id', 'title', 'source', 'url', 'published_at', 'published_utc',
        )} for row in (item.get('top_news') or [])[:12] if isinstance(row, dict)],
    })


def attach_frozen_evidence(payload, previous, item, config, captured_at, in_shortlist):
    previous = previous if isinstance(previous, dict) else {}
    first = previous.get('first_direction_evidence')
    payload['first_direction_evidence'] = copy.deepcopy(first) if isinstance(first, dict) else capture(
        item, config, captured_at, bool(previous)
    )
    top = previous.get('top_direction_evidence')
    if isinstance(top, dict):
        payload['top_direction_evidence'] = copy.deepcopy(top)
    elif in_shortlist:
        payload['top_direction_evidence'] = capture(item, config, captured_at, bool(previous.get('was_top_prediction')))
    else:
        payload['top_direction_evidence'] = None


BLOCKER_TEXT = {
    'already_reported': 'Earnings for this event already reported.',
    'calendar_date_conflict': 'Calendar sources disagree on the earnings date.',
    'event_already_passed': 'The earnings event has already passed.',
    'event_unverified': 'Event not verified by the current calendar pipeline.',
    'event_date_low_confidence': 'Earnings date confidence is low.',
    'missing_event_time': 'No exact event time available.',
    'event_session_unverified': 'BMO/AMC session not verified.',
    'verified_event_required': 'Policy requires a verified event.',
    'event_source_required': 'Policy requires a named event source.',
    'event_source_url_required': 'Policy requires an event source URL.',
    'event_verification_timestamp_required': 'Policy requires an event verification timestamp.',
    'verified_event_date_confidence_required': 'Policy requires event date confidence.',
    'verified_event_session_confidence_required': 'Policy requires session confidence.',
    'verified_event_session_required': 'Policy requires a verified session.',
    'no_directional_edge': 'No directional signal (NEUTRAL/INSUFFICIENT_DATA).',
    'direction_below_threshold': 'Direction strength below the minimum bias gate.',
    'coverage_below_threshold': 'Data coverage below the minimum gate.',
    'gap_risk_unavailable': 'Gap risk could not be scored.',
    'ai_rejected_event_eligibility': 'AI review rejected event eligibility.',
    'ai_review_required': 'No AI review yet (policy requires one).',
    'ai_no_directional_edge': 'AI verdict is NEUTRAL.',
    'insufficient_relevant_news': 'Fewer relevant articles than the policy minimum.',
    'insufficient_news_sources': 'Fewer distinct news sources than the policy minimum.',
    'ai_read_too_few_articles': 'AI read fewer articles than the policy minimum.',
    'ai_read_too_few_sources': 'AI read fewer sources than the policy minimum.',
    'stale_news_context': 'Newest relevant headline is older than the policy window.',
    'ai_news_refresh_pending': 'Fresher news exists; AI re-analysis is pending.',
    'trader_score_below_threshold': 'Trader Score below the selection minimum.',
}


def blocker_text(code):
    return BLOCKER_TEXT.get(str(code or '').strip(), str(code or ''))


def summarize_evidence(evidence):
    """Compact view of a frozen evidence snapshot for the verification dossier."""
    if not isinstance(evidence, dict):
        return None
    obs = evidence.get('observations') if isinstance(evidence.get('observations'), dict) else {}
    policy = evidence.get('policy') if isinstance(evidence.get('policy'), dict) else {}
    ai = evidence.get('ai') if isinstance(evidence.get('ai'), dict) else {}
    news = evidence.get('news') if isinstance(evidence.get('news'), list) else []
    breakdown = obs.get('trader_score_breakdown')
    return {
        'capture_quality': evidence.get('capture_quality'),
        'captured_at_utc': evidence.get('captured_at_utc'),
        'target': evidence.get('target'),
        'probability_calibrated': bool(evidence.get('probability_calibrated')),
        'direction_label': obs.get('direction_label'),
        'direction_bias_score': obs.get('direction_bias_score'),
        'trader_score': obs.get('trader_score'),
        'min_trader_score': policy.get('top_prediction_min_trader_score'),
        'trader_score_breakdown': dict(breakdown) if isinstance(breakdown, dict) else None,
        'ai_used': bool(ai.get('used')),
        'ai_verdict': ai.get('verdict'),
        'ai_confidence': ai.get('confidence'),
        'ai_model': ai.get('model'),
        'news_count': len(news),
    }


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _item_flag(item, name):
    flags = item.get('flags') if isinstance(item.get('flags'), dict) else {}
    return bool(flags.get(name))


def _news_freshness_gap(item, config, entries):
    digest = item.get('news_digest') if isinstance(item.get('news_digest'), dict) else {}
    configured_articles = _finite(config.get('top_prediction_min_news_articles'))
    configured_sources = _finite(config.get('top_prediction_min_news_sources'))
    # An explicit 0 disables the gate; only a missing value uses the default.
    min_articles = max(0, int(3 if configured_articles is None else configured_articles))
    min_sources = max(0, int(1 if configured_sources is None else configured_sources))
    article_count = digest.get('article_count')
    if article_count is None:
        article_count = len(item.get('top_news') or [])
    article_count = int(_finite(article_count) or 0)
    if article_count < min_articles:
        entries.append({
            'code': 'news_thin',
            'detail': f'{article_count} indexed article(s); policy works with at least {min_articles}.',
        })
    source_count = _finite(digest.get('unique_sources'))
    if source_count is not None and source_count < min_sources:
        entries.append({
            'code': 'news_sources_thin',
            'detail': f'{int(source_count)} distinct source(s); policy wants at least {min_sources}.',
        })


def build_verification_dossier(item, config, now_utc=None):
    """Verification dossier for one Pre-Earnings row: opposing evidence,
    internal contradictions and data gaps, next to the failed gates.

    Pure observation of the item as served — nothing is invented: every entry
    cites a field that is actually present (or actually absent). This is an
    audit view, not a trading signal.
    """
    item = item if isinstance(item, dict) else {}
    config = config if isinstance(config, dict) else {}
    final_label = str(item.get('direction_label') or '').strip().upper()
    final_score = _finite(item.get('final_direction_bias_score'))
    if final_score is None:
        final_score = _finite(item.get('direction_bias_score'))
    structured_label = str(item.get('structured_direction_label') or '').strip().upper()
    structured_score = _finite(item.get('structured_direction_bias_score'))
    llm_score = _finite(item.get('llm_direction_score'))
    ai = item.get('ai_decision') if isinstance(item.get('ai_decision'), dict) else {}
    ai_used = bool(ai.get('used'))
    ai_verdict = str(ai.get('verdict') or 'NEUTRAL').strip().upper()
    ai_confidence = _finite(ai.get('confidence'))
    alignment = str(item.get('decision_alignment') or '').strip().upper()
    blockers = [str(code) for code in (item.get('recommendation_blockers') or [])]

    dossier = {
        'schema_version': 1,
        'probability_calibrated': False,
        'decision': {
            'final_label': final_label or None,
            'final_score': int(final_score) if final_score is not None else None,
            'structured_label': structured_label or None,
            'structured_score': int(structured_score) if structured_score is not None else None,
            'llm_score': int(llm_score) if llm_score is not None else None,
            'ai_used': ai_used,
            'ai_verdict': ai_verdict if ai_used else None,
            'ai_confidence': ai_confidence,
            'alignment': alignment or None,
        },
        'gates': {
            'eligible': bool(item.get('recommendation_eligible')),
            'trader_score': _finite(item.get('trader_score')),
            'band': item.get('trader_score_band'),
            'min_trader_score': _finite(config.get('top_prediction_min_trader_score')),
            'blockers': blockers,
            'blockers_human': [
                {'code': code, 'text': blocker_text(code)} for code in blockers
            ],
        },
        'opposing': [],
        'contradictions': [],
        'gaps': [],
    }
    opposing = dossier['opposing']
    contradictions = dossier['contradictions']
    gaps = dossier['gaps']

    # --- Opposing evidence: real inputs that lean against the final call. ---
    if final_label in {'LONG', 'SHORT'}:
        against = 'LONG' if final_label == 'SHORT' else 'SHORT'
        upside = _finite(item.get('analyst_target_upside_pct'))
        if upside is not None and ((final_label == 'SHORT' and upside > 0) or (final_label == 'LONG' and upside < 0)):
            opposing.append({
                'source': 'analyst_targets',
                'detail': (
                    f'Analyst price targets imply {upside:+.1f}% upside, leaning {against} '
                    f'against the {final_label} call.'
                ),
            })
        rec_mean = _finite(item.get('analyst_rec_mean'))
        if rec_mean is not None and ((final_label == 'SHORT' and rec_mean <= 2.5) or (final_label == 'LONG' and rec_mean >= 3.5)):
            lean = 'buy-side' if rec_mean <= 2.5 else 'sell-side'
            opposing.append({
                'source': 'analyst_consensus',
                'detail': (
                    f'Analyst recommendation mean {rec_mean:.1f} (1=buy…5=sell) is {lean}, '
                    f'against the {final_label} call.'
                ),
            })
        etoro = item.get('etoro_context') if isinstance(item.get('etoro_context'), dict) else {}
        buy_pct = _finite(etoro.get('buy_pct'))
        if buy_pct is not None and ((final_label == 'SHORT' and buy_pct >= 60) or (final_label == 'LONG' and buy_pct <= 40)):
            opposing.append({
                'source': 'etoro_retail',
                'detail': (
                    f'{buy_pct:.0f}% of tracked retail positions are long, '
                    f'against the {final_label} call (crowd sentiment, not a signal).'
                ),
            })
        peer = item.get('peer_readthrough') if isinstance(item.get('peer_readthrough'), dict) else {}
        peer_verdict = str(peer.get('verdict') or '').strip().lower()
        peer_against = 'bearish' if final_label == 'LONG' else 'bullish'
        if peer_verdict == peer_against:
            opposing.append({
                'source': 'peer_readthrough',
                'detail': f'Already-reported peers reacted {peer_verdict}, against the {final_label} call.',
            })
        reactions = [
            _finite(row.get('move_pct'))
            for row in (item.get('recent_reactions') or [])
            if isinstance(row, dict) and _finite(row.get('move_pct')) is not None
        ]
        opposite_reactions = [
            value for value in reactions
            if (final_label == 'LONG' and value < 0) or (final_label == 'SHORT' and value > 0)
        ]
        if len(reactions) >= 2 and len(opposite_reactions) == len(reactions):
            opposing.append({
                'source': 'recent_reactions',
                'detail': (
                    f'All {len(reactions)} recent verified post-earnings reactions moved '
                    f'{"down" if final_label == "LONG" else "up"}, against the {final_label} call.'
                ),
            })
        track = item.get('symbol_track_record') if isinstance(item.get('symbol_track_record'), dict) else {}
        track_total = int(_finite(track.get('total')) or 0)
        track_hits = int(_finite(track.get('hits')) or 0)
        if track_total >= 3 and track_hits / track_total < 0.5:
            opposing.append({
                'source': 'ticker_track_record',
                'detail': (
                    f'Our past calls on this ticker hit {track_hits}/{track_total} '
                    f'({track_hits / track_total * 100:.0f}%) — below coin flip on this sample.'
                ),
            })

    # --- Contradictions: signals inside the row that disagree with each other. ---
    if alignment == 'CONFLICT' and structured_label and ai_verdict:
        contradictions.append({
            'code': 'ai_vs_structured',
            'detail': (
                f'Structured view says {structured_label}, the AI verdict says {ai_verdict}. '
                'One of the two is wrong about this event.'
            ),
        })
    elif alignment == 'MIXED':
        contradictions.append({
            'code': 'mixed_alignment',
            'detail': (
                f'Structured view ({structured_label or "N/A"}) and AI verdict ({ai_verdict}) '
                'do not agree on a direction.'
            ),
        })
    if ai_used and ai_verdict in {'LONG', 'SHORT'} and final_label in {'LONG', 'SHORT'} and ai_verdict != final_label:
        contradictions.append({
            'code': 'ai_vs_final',
            'detail': (
                f'The AI verdict is {ai_verdict} but the blended final call is {final_label} '
                '(the structured side outweighed the AI vote).'
            ),
        })
    conflicting_dates = [str(value) for value in (item.get('event_conflicting_dates') or []) if str(value).strip()]
    if conflicting_dates:
        contradictions.append({
            'code': 'event_date_conflict',
            'detail': f'Calendar providers report conflicting event dates: {", ".join(conflicting_dates[:4])}.',
        })
    if _item_flag(item, 'calendar_news_mismatch'):
        contradictions.append({
            'code': 'calendar_news_mismatch',
            'detail': 'News flow contradicts the calendar event timing (possible stale or wrong-date event).',
        })
    if bool(item.get('position_direction_conflict')):
        context = item.get('active_position_context') if isinstance(item.get('active_position_context'), dict) else {}
        directions = ', '.join(str(value) for value in (context.get('active_directions') or [])) or 'open'
        contradictions.append({
            'code': 'open_position_conflict',
            'detail': f'An open {directions} position conflicts with the {final_label or "displayed"} call.',
        })
    if (
        ai_used and structured_label == 'NEUTRAL' and final_label in {'LONG', 'SHORT'}
        and not bool(item.get('news_only_call'))
    ):
        contradictions.append({
            'code': 'llm_carries_direction',
            'detail': (
                f'The structured view is NEUTRAL; the {final_label} label comes from the AI blend '
                '(thin structured base).'
            ),
        })

    # --- Gaps: data that is missing or too thin to verify the call. ---
    gap_risk = _finite(item.get('gap_risk_numeric'))
    if gap_risk is None:
        gap_risk = _finite(item.get('gap_risk'))
    if gap_risk is None:
        gaps.append({
            'code': 'gap_risk_unavailable',
            'detail': 'Gap risk is unscored (INSUFFICIENT_DATA) — no verified historical sample.',
        })
    session = str(item.get('session') or '').strip().upper()
    if not session or session in {'UNKNOWN', 'N/A', 'NONE'}:
        gaps.append({'code': 'session_unknown', 'detail': 'BMO/AMC session unknown; the entry window cannot be timed.'})
    if _finite(item.get('beat_rate_historical')) is None:
        gaps.append({
            'code': 'beat_rate_unavailable',
            'detail': 'No verified EPS beat history (no completed reports with estimated and reported EPS).',
        })
    if _finite(item.get('analyst_target_upside_pct')) is None and _finite(item.get('analyst_rec_mean')) is None:
        gaps.append({'code': 'analyst_unavailable', 'detail': 'No analyst targets or consensus available for this ticker.'})
    coverage = _finite(item.get('coverage_score'))
    min_coverage = _finite(config.get('top_prediction_min_coverage'))
    if coverage is not None and min_coverage is not None and coverage < min_coverage:
        gaps.append({
            'code': 'coverage_thin',
            'detail': f'Coverage {coverage:.0f}/100 is below the {min_coverage:.0f}/100 gate — too little verified data.',
        })
    _news_freshness_gap(item, config, gaps)
    if bool(item.get('news_only_call')):
        gaps.append({
            'code': 'news_only_call',
            'detail': 'Direction rests on news flow alone (no history, no gap score) — treat as thin.',
        })
    if bool(item.get('news_update_pending')):
        gaps.append({
            'code': 'ai_stale_vs_news',
            'detail': 'The saved AI verdict predates fresher news; re-analysis is pending.',
        })
    if _item_flag(item, 'already_reported'):
        gaps.append({
            'code': 'already_reported',
            'detail': 'The event has already been reported — any displayed call is historical, not actionable.',
        })
    warnings = [str(value) for value in (item.get('news_quality_warnings') or []) if str(value).strip()]
    for warning in warnings[:4]:
        gaps.append({'code': 'news_quality_warning', 'detail': warning})
    return clean(dossier)
