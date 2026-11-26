"""Point-in-time price features before an earnings event.

Only completed daily sessions strictly before the capture date are used, so a
feature captured at registration cannot contain the event reaction.

The short-term reversal rule below was found in an exploratory scan of 12
variants on 2026-09-15 (fading a 10%+ five-day move versus SPY: 56.9% on 489
events vs 50.3% always-LONG, p=0.032 before correction). With 12 variants that
is NOT significant, so the rule is pre-registered here, unchanged, and judged
only on events captured after that date.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

PRICE_FEATURE_FIELDS = (
    "predicted_rel_return_5d_pct",
    "predicted_rel_return_20d_pct",
    "predicted_price_features_as_of",
)
REVERSAL_THRESHOLD_PCT = 10.0
REVERSAL_HYPOTHESIS_REGISTERED_UTC = "2026-09-15T00:00:00+00:00"
MAX_STALENESS_DAYS = 7


def _completed_closes(bars: Sequence[Mapping[str, Any]], as_of: datetime) -> List[Tuple[str, float]]:
    out: Dict[str, float] = {}
    cutoff = as_of.date()
    for bar in bars or []:
        dt = bar.get("dt") if isinstance(bar, Mapping) else None
        if not isinstance(dt, datetime) or dt.date() >= cutoff:
            continue
        try:
            close = float(bar.get("close"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(close) and close > 0:
            out[dt.date().isoformat()] = close
    return sorted(out.items())


def relative_returns(symbol_bars: Sequence[Mapping[str, Any]], benchmark_bars: Sequence[Mapping[str, Any]],
                     as_of: datetime) -> Optional[Dict[str, Any]]:
    """Symbol return minus benchmark return over 5 and 20 completed sessions."""
    symbol = _completed_closes(symbol_bars, as_of)
    benchmark = dict(_completed_closes(benchmark_bars, as_of))
    common = [(day, close) for day, close in symbol if day in benchmark]
    if len(common) < 6:
        return None
    last_day = common[-1][0]
    if as_of.date() - datetime.fromisoformat(last_day).date() > timedelta(days=MAX_STALENESS_DAYS):
        return None

    def rel(k: int) -> Optional[float]:
        if len(common) <= k:
            return None
        start_day, start_close = common[-1 - k]
        end_close = common[-1][1]
        stock = end_close / start_close - 1.0
        index = benchmark[last_day] / benchmark[start_day] - 1.0
        return round((stock - index) * 100.0, 4)

    return {
        "predicted_rel_return_5d_pct": rel(5),
        "predicted_rel_return_20d_pct": rel(20),
        "predicted_price_features_as_of": last_day,
    }


def reversal_direction(record: Mapping[str, Any], threshold_pct: float = REVERSAL_THRESHOLD_PCT) -> Optional[int]:
    """Pre-registered contrarian call: 1 = up after a big drop, 0 = down after a big rally."""
    try:
        value = float(record.get("predicted_rel_return_5d_pct"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or abs(value) < threshold_pct:
        return None
    return 1 if value < 0 else 0
