from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from core.sqlite_store import get_sqlite_store


LIVE_PRICE_KEY_PREFIX = "live_price:"


def _normalize_symbol(symbol: Any) -> str:
    return str(symbol or "").upper().strip()


def _cache_key(symbol: Any) -> str:
    return f"{LIVE_PRICE_KEY_PREFIX}{_normalize_symbol(symbol)}"


def store_live_price(
    symbol: Any,
    price: Any,
    *,
    source: str,
    timestamp: Optional[str] = None,
    change_pct: Optional[float] = None,
) -> bool:
    """Persist one verified quote for cross-process Capital reads."""
    ticker = _normalize_symbol(symbol)
    try:
        numeric_price = float(price or 0.0)
    except (TypeError, ValueError):
        return False
    if not ticker or numeric_price <= 0:
        return False

    observed_at = timestamp or datetime.now(timezone.utc).isoformat()
    payload: Dict[str, Any] = {
        "ticker": ticker,
        "price": numeric_price,
        "source": str(source or "capital_price_cache"),
        "timestamp": observed_at,
    }
    if change_pct is not None:
        try:
            payload["change_pct"] = float(change_pct)
        except (TypeError, ValueError):
            pass
    get_sqlite_store().set_json(_cache_key(ticker), payload)
    return True


def get_live_prices(symbols: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    """Read requested quotes without network I/O or a shared-file race."""
    store = get_sqlite_store()
    prices: Dict[str, Dict[str, Any]] = {}
    for raw_symbol in symbols or []:
        ticker = _normalize_symbol(raw_symbol)
        if not ticker or ticker in prices:
            continue
        payload = store.get_json(_cache_key(ticker), None)
        if not isinstance(payload, dict):
            continue
        try:
            price = float(payload.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        prices[ticker] = {
            **payload,
            "ticker": ticker,
            "price": price,
        }
    return prices
