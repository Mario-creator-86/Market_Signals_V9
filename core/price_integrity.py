"""Price-source concordance checks used before generating or settling signals.

A technical candle and a quote can legitimately differ by a small amount, but
they must still describe the same market.  Treating a large discrepancy as a
valid fill corrupts signal outcomes and can make a losing strategy look
profitable.  This module is intentionally deterministic and side-effect free
so both the engine and execution guards can fail closed on the same evidence.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Dict, Optional


DEFAULT_MAX_DEVIATION_PCT = {
    "stock": 3.0,
    "crypto": 2.5,
    "forex": 1.0,
    "commodity": 2.0,
}


def _positive_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number > 0 else None


def max_deviation_pct(asset_type: str, config: Optional[Dict[str, Any]] = None) -> float:
    """Return a bounded, per-asset concordance threshold in percentage points."""
    asset_key = str(asset_type or "").lower().strip()
    configured = (config or {}).get("max_deviation_pct", {}) if isinstance(config, dict) else {}
    raw = configured.get(asset_key) if isinstance(configured, dict) else configured
    value = _positive_number(raw)
    if value is None:
        value = DEFAULT_MAX_DEVIATION_PCT.get(asset_key, 2.0)
    return min(value, 25.0)


def validate_price_consistency(
    *,
    live_price: Any,
    technical_price: Any,
    asset_type: str,
    live_source: str,
    technical_source: str,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compare a live quote with the last technical close.

    ``verified`` means two independent observations agree within the configured
    deviation.  A single technical source can still render the watchlist, but
    is deliberately *not* accepted as outcome or Capital-entry evidence.
    """
    live = _positive_number(live_price)
    technical = _positive_number(technical_price)
    asset_key = str(asset_type or "").lower().strip() or "unknown"
    threshold = max_deviation_pct(asset_key, config)
    base = {
        "asset_type": asset_key,
        "live_price": live,
        "technical_price": technical,
        "live_source": str(live_source or "unknown"),
        "technical_source": str(technical_source or "unknown"),
        "max_deviation_pct": round(threshold, 4),
        "is_verified": False,
    }

    if technical is None:
        return {
            **base,
            "status": "invalid_technical_price",
            "reason": "Latest technical close is missing or invalid",
        }
    if live is None:
        return {
            **base,
            "status": "single_source",
            "reason": "No independent live quote is available for price verification",
        }

    deviation = abs(live - technical) / technical * 100.0
    if deviation > threshold:
        return {
            **base,
            "status": "conflict",
            "reason": "Live quote and technical close exceed the permitted deviation",
            "deviation_pct": round(deviation, 6),
        }
    return {
        **base,
        "status": "verified",
        "reason": "Live quote and technical close are concordant",
        "deviation_pct": round(deviation, 6),
        "is_verified": True,
    }
