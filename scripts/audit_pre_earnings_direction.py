"""Read-only directional diagnostics. Does not fit models or alter trading policy."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path


def finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def bias_side(row):
    value = finite(row.get('predicted_direction_bias'))
    if value is None:
        return None
    return 'LONG' if value >= 15 else 'SHORT' if value <= -15 else 'NEUTRAL'


def wilson(hits, count):
    if not count:
        return None
    z = 1.959963984540054
    p = hits / count
    denominator = 1 + z*z/count
    center = (p + z*z/(2*count)) / denominator
    radius = z * math.sqrt(p*(1-p)/count + z*z/(4*count*count)) / denominator
    return [round(100*(center-radius), 2), round(100*(center+radius), 2)]


def summarize(rows, side):
    calls, reasons = [], Counter()
    for row in rows:
        direction = side(row)
        move = finite(row.get('realized_signed_move_pct'))
        if direction not in {'LONG', 'SHORT'}:
            reasons['neutral_or_missing_direction'] += 1
        elif move is None:
            reasons['missing_or_invalid_outcome'] += 1
        elif move == 0:
            reasons['flat_outcome'] += 1
        else:
            calls.append((direction, move))
    count = len(calls)
    hits = sum((direction == 'LONG') == (move > 0) for direction, move in calls)
    up = sum(move > 0 for _, move in calls)
    return {
        'rows': len(rows), 'directional_nonflat_calls': count, 'hits': hits,
        'accuracy_pct': round(100*hits/count, 2) if count else None,
        'wilson_95_pct_descriptive': wilson(hits, count),
        'always_long_accuracy_same_cases_pct': round(100*up/count, 2) if count else None,
        'always_short_accuracy_same_cases_pct': round(100*(count-up)/count, 2) if count else None,
        'long_calls': sum(direction == 'LONG' for direction, _ in calls),
        'short_calls': sum(direction == 'SHORT' for direction, _ in calls),
        'excluded': dict(reasons),
    }


def audit(payload):
    raw = payload.get('outcomes', [])
    rows = [row for row in raw if isinstance(row, dict)]
    keys = Counter((str(r.get('symbol') or '').upper(), str(r.get('event_utc') or '')[:10]) for r in rows)
    # Ambiguous duplicates are excluded as a group; never select the better outcome.
    unique = [r for r in rows if keys[(str(r.get('symbol') or '').upper(), str(r.get('event_utc') or '')[:10])] == 1
              and r.get('symbol') and r.get('event_utc')]
    top = [r for r in unique if r.get('was_top_prediction') is True]
    selectors = {
        'all_recorded_bias': (unique, bias_side),
        'current_score_version': ([r for r in unique if r.get('prediction_score_version') == 'two_stage_evidence_weighted_v2'], bias_side),
        'recorded_ai_verdict': (unique, lambda r: r.get('predicted_ai_verdict')),
        'top10_frozen_side': (top, lambda r: r.get('top_prediction_direction')),
    }
    cohorts = {}
    for name, (population, side) in selectors.items():
        cohorts[name] = summarize(population, side)
        cohorts[name]['by_side'] = {s: summarize([r for r in population if side(r) == s], side) for s in ('LONG', 'SHORT')}
    return {
        'scope': 'Descriptive saved-event direction accuracy; not validated OOS performance or trade P/L',
        'raw_rows': len(raw), 'unique_usable_event_keys': len(unique),
        'excluded_duplicate_or_missing_keys': len(rows)-len(unique),
        'cohorts': cohorts,
        'quality': {
            'unknown_session_rows': sum(r.get('session') not in {'BMO', 'AMC'} for r in unique),
            'ai_cache_backfill_rows': sum(r.get('predicted_ai_provenance') == 'ai_cache_backfill' for r in unique),
            'top_missing_capture_time': sum(not r.get('top_prediction_first_seen_utc') for r in top),
            'top_captured_same_day_or_later_needs_session_check': sum(str(r.get('top_prediction_first_seen_utc') or '')[:10] >= str(r.get('event_utc') or '')[:10] for r in top),
            'absolute_move_over_100_needs_provider_verification': [
                {k: r.get(k) for k in ('symbol', 'event_utc', 'realized_signed_move_pct')}
                for r in unique if (value := finite(r.get('realized_signed_move_pct'))) is not None and abs(value) > 100
            ],
        },
        'limitations': [
            'No raw reference prices or corporate-action proof validated by this tool.',
            'Event date is not necessarily actual publication timestamp; same-day captures need session verification.',
            'Historical AI backfill does not by itself prove a pre-event prediction.',
            'Wilson intervals assume independent Bernoulli outcomes; shared event dates/sectors can violate this.',
            'Neutral/missing calls and flat outcomes are disclosed, not counted as losses or wins.',
            'No thresholds or weights selected from these descriptive cohorts.',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--feedback', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.feedback.resolve() == args.output.resolve():
        parser.error('Output must not overwrite the feedback store')
    content = args.feedback.read_bytes()
    result = audit(json.loads(content.decode('utf-8-sig')))
    result.update(source=str(args.feedback.resolve()), source_sha256=hashlib.sha256(content).hexdigest(),
                  audited_at_utc=datetime.now(timezone.utc).isoformat())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    print(json.dumps(result['cohorts'], indent=2))


if __name__ == '__main__':
    main()
