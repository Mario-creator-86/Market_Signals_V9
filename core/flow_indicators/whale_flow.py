"""Whale Flow indicator — free public crypto order-flow evidence.

Sources (public REST, no API keys, no paid feeds):
- Binance  GET /api/v3/depth, /api/v3/aggTrades
- Bybit    GET /v5/market/orderbook, /v5/market/recent-trade
- OKX      GET /api/v5/market/books, /api/v5/market/trades

What it measures per exchange and aggregated:
- large resting BUY/SELL orders (walls), nearest + strongest, distance from mid
- large executed trades (whale tape): buy vs sell whale pressure
- order persistence across consecutive book snapshots
- added / removed liquidity and cancellation activity
- spoofing risk heuristic (far-from-price walls that churn without executions)

Fail-safe contract (§5 of the owner spec): a provider that fails or returns
stale data is dropped from the aggregate, never invented. If no provider
survives, bias=UNAVAILABLE and every numeric field is None — the caller's
existing logic must continue unchanged.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from core.http_utils import http_get

logger = logging.getLogger(__name__)

# ── tunables (owner-adjustable via configure()) ────────────────────────────
DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "book_ttl_seconds": 45.0,        # cached aggregate freshness budget
    "stale_after_seconds": 120.0,    # older than this -> UNAVAILABLE
    "wall_min_usd": 100_000.0,       # absolute floor for "wall" notional
    "wall_median_mult": 25.0,        # ...or N x median level notional
    "whale_trade_min_usd": 50_000.0, # absolute floor for "whale" print
    "whale_trade_median_mult": 20.0, # ...or N x median trade notional
    "book_depth": 400,               # levels per side requested
    "history_snapshots": 8,          # book snapshots kept for persistence
    "request_timeout": (2, 4),       # (connect, read) seconds per provider
}

_CFG = dict(DEFAULTS)

# bases we will ever query (top-liquid USDT pairs only — keeps the public
# endpoints honest and avoids mapping mistakes on exotic symbols)
SUPPORTED_BASES = {
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK",
    "TON", "TRX", "DOT", "MATIC", "LTC", "BCH", "NEAR", "UNI", "APT",
    "ARB", "OP", "FIL", "ATOM", "ETC", "HBAR", "INJ", "SUI", "SEI",
    "PEPE", "WIF", "SHIB", "AAVE", "RUNE", "TIA",
}

_UA = {"User-Agent": "Mozilla/5.0"}


def configure(config: Optional[Dict[str, Any]] = None) -> None:
    """Apply owner overrides from the `flow_indicators.whale` config section."""
    if not isinstance(config, dict):
        return
    for key in DEFAULTS:
        if key in config:
            try:
                _CFG[key] = type(DEFAULTS[key])(config[key]) if not isinstance(DEFAULTS[key], tuple) else config[key]
            except (TypeError, ValueError):
                logger.warning("whale_flow: ignoring bad config value for %s", key)


# Last `flow_indicators` section applied from the shared config — see
# institutional_flow._ensure_runtime_config for why the getters sync it.
_LAST_SHARED_SECTION: Dict[str, Any] = {}


def _ensure_runtime_config() -> None:
    global _LAST_SHARED_SECTION
    try:
        from core.config import ConfigManager
        section = (ConfigManager().config or {}).get("flow_indicators") or {}
    except Exception:
        return
    if isinstance(section, dict) and section and section != _LAST_SHARED_SECTION:
        _LAST_SHARED_SECTION = dict(section)
        configure({**section, **(section.get("whale") or {})})


def _clamp(value: float, low: float, high: float) -> float:
    # NaN must clamp to the neutral floor: min/max would turn NaN into `high`.
    if value != value:  # NaN check — math.isfinite rejects inf too
        return low
    return max(low, min(high, value))


def _is_crypto_symbol(ticker: str) -> bool:
    """BTC-USD / BTCUSDT / ETH-USD style -> True for supported bases."""
    t = str(ticker or "").upper().replace("-", "").replace("/", "")
    for base in SUPPORTED_BASES:
        if t == base or t == base + "USD" or t == base + "USDT":
            return True
    return False


def _base_asset(ticker: str) -> Optional[str]:
    t = str(ticker or "").upper()
    t = t.replace("/", "-")
    for suffix in ("-USDT", "-USD", "-USD-C", "-PERP"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
            break
    t = t.replace("-", "")
    if t in SUPPORTED_BASES:
        return t
    # _is_crypto_symbol also accepts the concatenated BTCUSD/BTCUSDT forms —
    # strip the same quote suffixes here so classification and base
    # extraction agree (otherwise classified-crypto tickers fail as
    # "unsupported crypto symbol").
    for quote in ("USDT", "USD"):
        if t.endswith(quote) and t[: -len(quote)] in SUPPORTED_BASES:
            return t[: -len(quote)]
    return None


# ── per-(exchange, symbol) book history for persistence / cancellations ────
class _BookHistory:
    __slots__ = ("snapshots",)

    def __init__(self, keep: int):
        self.snapshots: deque = deque(maxlen=keep)


_HIST_LOCK = threading.Lock()
_HISTORY: Dict[str, _BookHistory] = {}


def _record_book(exchange: str, symbol: str, levels: Dict[float, float], side: str, ts: float) -> None:
    key = f"{exchange}:{symbol}:{side}"
    with _HIST_LOCK:
        hist = _HISTORY.setdefault(key, _BookHistory(int(_CFG["history_snapshots"])))
        hist.snapshots.append((ts, dict(levels)))


def _persistence_metrics(exchange: str, symbol: str, side: str, now: float) -> Dict[str, Any]:
    """Wall persistence + add/remove liquidity between consecutive snapshots."""
    key = f"{exchange}:{symbol}:{side}"
    with _HIST_LOCK:
        hist = _HISTORY.get(key)
        snaps = list(hist.snapshots) if hist else []
    if len(snaps) < 2:
        return {"persistence": None, "added_usd": None, "removed_usd": None,
                "snapshots": len(snaps)}
    latest_ts, latest = snaps[-1]
    prev_ts, prev = snaps[-2]
    window_s = max(1e-6, latest_ts - prev_ts)
    # liquidity added/removed at the top-N price levels of the latest snapshot
    prices = sorted(latest, reverse=(side == "bids"))[: _CFG["book_depth"]]
    added = removed = 0.0
    for price in prices:
        cur = latest.get(price, 0.0)
        old = prev.get(price, 0.0)
        if cur > old:
            added += (cur - old) * price
        elif cur < old:
            removed += (old - cur) * price
    # persistence: how much of the current visible depth existed last snapshot
    total_now = sum(qty * price for price, qty in latest.items())
    survived = sum(min(qty, prev.get(price, 0.0)) * price for price, qty in latest.items())
    persistence = (survived / total_now) if total_now > 0 else None
    return {
        "persistence": round(persistence, 4) if persistence is not None else None,
        "added_usd": round(added, 2),
        "removed_usd": round(removed, 2),
        "churn_per_minute": round((added + removed) / window_s * 60.0, 2),
        "snapshots": len(snaps),
    }


# ── provider fetchers (each returns normalized snapshot or raises) ─────────
class _ProviderFailure(Exception):
    pass


def _get_json(url: str, params: Dict[str, Any], provider: str, *, allow_list: bool = False) -> Any:
    res = http_get(url, provider=provider, headers=_UA,
                   timeout=_CFG["request_timeout"], max_retries=0, params=params)
    if res is None:
        raise _ProviderFailure(f"{provider}: no response (circuit/timeout)")
    if res.status_code != 200:
        raise _ProviderFailure(f"{provider}: HTTP {res.status_code}")
    try:
        payload = res.json()
    except ValueError as exc:
        raise _ProviderFailure(f"{provider}: non-JSON body") from exc
    if isinstance(payload, list) and allow_list:
        return payload
    if not isinstance(payload, dict):
        raise _ProviderFailure(f"{provider}: unexpected payload shape")
    return payload


def _normalize_levels(raw: List, price_idx: int, qty_idx: int) -> List[Tuple[float, float]]:
    out = []
    for entry in raw or []:
        try:
            price = float(entry[price_idx])
            qty = float(entry[qty_idx])
        except (TypeError, ValueError, IndexError):
            continue
        if price > 0 and qty > 0 and math.isfinite(price) and math.isfinite(qty):
            out.append((price, qty))
    return out


def _binance_snapshot(base: str) -> Dict[str, Any]:
    sym = f"{base}USDT"
    book = _get_json("https://api.binance.com/api/v3/depth",
                     {"symbol": sym, "limit": min(1000, int(_CFG["book_depth"]) * 2)}, "binance")
    bids = _normalize_levels(book.get("bids"), 0, 1)
    asks = _normalize_levels(book.get("asks"), 0, 1)
    if not bids or not asks:
        raise _ProviderFailure("binance: empty book")
    trades_raw = _get_json("https://api.binance.com/api/v3/aggTrades",
                           {"symbol": sym, "limit": 1000}, "binance", allow_list=True) or []
    # aggTrades: m=True -> buyer is maker -> the AGGRESSOR sold
    trades = [{"notional": float(t.get("q", 0)) * float(t.get("p", 0)),
               "side": "SELL" if t.get("m") else "BUY"} for t in trades_raw]
    trades = [t for t in trades if math.isfinite(t["notional"]) and t["notional"] > 0]
    return {"exchange": "binance", "symbol": sym, "bids": bids, "asks": asks, "trades": trades}


def _bybit_snapshot(base: str) -> Dict[str, Any]:
    sym = f"{base}USDT"
    payload = _get_json("https://api.bybit.com/v5/market/orderbook",
                        {"category": "spot", "symbol": sym, "limit": 200}, "bybit")
    if payload.get("retCode") not in (0, None):
        raise _ProviderFailure(f"bybit: retCode {payload.get('retCode')}")
    result = payload.get("result") or {}
    bids = _normalize_levels(result.get("b"), 0, 1)
    asks = _normalize_levels(result.get("a"), 0, 1)
    if not bids or not asks:
        raise _ProviderFailure("bybit: empty book")
    tpayload = _get_json("https://api.bybit.com/v5/market/recent-trade",
                         {"category": "spot", "symbol": sym, "limit": 1000}, "bybit")
    tlist = (tpayload.get("result") or {}).get("list") or []
    trades = [{"notional": float(t.get("size", 0)) * float(t.get("price", 0)),
               "side": str(t.get("side") or "").upper()} for t in tlist]
    trades = [t for t in trades
              if math.isfinite(t["notional"]) and t["notional"] > 0 and t["side"] in ("BUY", "SELL")]
    return {"exchange": "bybit", "symbol": sym, "bids": bids, "asks": asks, "trades": trades}


def _okx_snapshot(base: str) -> Dict[str, Any]:
    inst = f"{base}-USDT"
    payload = _get_json("https://www.okx.com/api/v5/market/books",
                        {"instId": inst, "sz": 400}, "okx")
    data = payload.get("data") or []
    rows = data[0] if data else {}
    bids = _normalize_levels(rows.get("bids"), 0, 1)
    asks = _normalize_levels(rows.get("asks"), 0, 1)
    if not bids or not asks:
        raise _ProviderFailure("okx: empty book")
    tpayload = _get_json("https://www.okx.com/api/v5/market/trades",
                         {"instId": inst, "limit": 500}, "okx")
    tlist = tpayload.get("data") or []
    trades = [{"notional": float(t.get("sz", 0)) * float(t.get("px", 0)),
               "side": str(t.get("side") or "").upper()} for t in tlist]
    trades = [t for t in trades
              if math.isfinite(t["notional"]) and t["notional"] > 0 and t["side"] in ("BUY", "SELL")]
    return {"exchange": "okx", "symbol": inst, "bids": bids, "asks": asks, "trades": trades}


# ── per-exchange analytics ──────────────────────────────────────────────────
def _side_walls(levels: List[Tuple[float, float]], mid: float, side: str) -> Dict[str, Any]:
    """Walls = levels above threshold; returns nearest/strongest + book pressure."""
    notionals = sorted(q * p for p, q in levels)
    if not notionals:
        return {"wall_threshold_usd": None, "nearest": None, "strongest": None,
                "wall_count": 0, "top_depth_usd": 0.0}
    median = notionals[len(notionals) // 2]
    threshold = max(float(_CFG["wall_min_usd"]), float(_CFG["wall_median_mult"]) * median)
    walls = [(p, q, q * p) for p, q in levels if q * p >= threshold]
    nearest = strongest = None
    if walls:
        if side == "bids":
            nearest = max(walls, key=lambda w: w[0])            # highest bid wall
            strongest = max(walls, key=lambda w: w[2])
        else:
            nearest = min(walls, key=lambda w: w[0])            # lowest ask wall
            strongest = max(walls, key=lambda w: w[2])

        def wall_view(w):
            return {"price": round(w[0], 6), "usd": round(w[2], 2),
                    "distance_pct": round(abs(w[0] - mid) / mid * 100.0, 3)}
    else:
        wall_view = None
    top = sorted(levels, key=lambda l: l[0], reverse=(side == "bids"))[:25]
    top_depth = sum(q * p for p, q in top)
    return {
        "wall_threshold_usd": round(threshold, 2),
        "nearest": wall_view(nearest) if nearest else None,
        "strongest": wall_view(strongest) if strongest else None,
        "wall_count": len(walls),
        "top_depth_usd": round(top_depth, 2),
    }


def _whale_tape(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Executed whale prints: buy vs sell notional above the whale threshold."""
    if not trades:
        return {"whale_threshold_usd": None, "buy_usd": None, "sell_usd": None,
                "buy_count": 0, "sell_count": 0}
    notionals = sorted(t["notional"] for t in trades)
    median = notionals[len(notionals) // 2]
    threshold = max(float(_CFG["whale_trade_min_usd"]),
                    float(_CFG["whale_trade_median_mult"]) * median)
    whale = [t for t in trades if t["notional"] >= threshold]
    buy = sum(t["notional"] for t in whale if t["side"] == "BUY")
    sell = sum(t["notional"] for t in whale if t["side"] == "SELL")
    return {
        "whale_threshold_usd": round(threshold, 2),
        "buy_usd": round(buy, 2),
        "sell_usd": round(sell, 2),
        "buy_count": sum(1 for t in whale if t["side"] == "BUY"),
        "sell_count": sum(1 for t in whale if t["side"] == "SELL"),
    }


def _analyze_exchange(snap: Dict[str, Any], now: float) -> Dict[str, Any]:
    best_bid, _ = max(snap["bids"], key=lambda l: l[0])
    best_ask, _ = min(snap["asks"], key=lambda l: l[0])
    mid = (best_bid + best_ask) / 2.0
    _record_book(snap["exchange"], snap["symbol"], dict(snap["bids"]), "bids", now)
    _record_book(snap["exchange"], snap["symbol"], dict(snap["asks"]), "asks", now)
    bid_side = _side_walls(snap["bids"], mid, "bids")
    ask_side = _side_walls(snap["asks"], mid, "asks")
    bid_hist = _persistence_metrics(snap["exchange"], snap["symbol"], "bids", now)
    ask_hist = _persistence_metrics(snap["exchange"], snap["symbol"], "asks", now)
    tape = _whale_tape(snap["trades"])
    persistence = [h["persistence"] for h in (bid_hist, ask_hist) if h["persistence"] is not None]
    churn = [h.get("churn_per_minute") for h in (bid_hist, ask_hist) if h.get("churn_per_minute") is not None]
    # Spoofing heuristic: big walls far from mid + heavy add/remove churn +
    # thin executions near those walls = classic paint-the-book behaviour.
    far_walls = 0
    for side_data in (bid_side, ask_side):
        for key in ("nearest", "strongest"):
            w = side_data.get(key)
            if w and w["distance_pct"] > 1.0:
                far_walls += 1
    churn_score = _clamp((max(churn) / max(1.0, bid_side["top_depth_usd"])) if churn else 0.0, 0.0, 1.0)
    spoofing = _clamp(0.25 * min(far_walls, 4) + 0.5 * churn_score, 0.0, 1.0)
    return {
        "exchange": snap["exchange"],
        "symbol": snap["symbol"],
        "mid": round(mid, 6),
        "best_bid": round(best_bid, 6),
        "best_ask": round(best_ask, 6),
        "buy_walls": bid_side,
        "sell_walls": ask_side,
        "whale_tape": tape,
        "persistence": round(sum(persistence) / len(persistence), 4) if persistence else None,
        "cancellation_activity": round(sum(churn) / len(churn), 2) if churn else None,
        "spoofing_risk": round(spoofing, 3),
        "as_of": now,
    }


# ── aggregation ─────────────────────────────────────────────────────────────
_PROVIDERS = (
    ("binance", _binance_snapshot),
    ("bybit", _bybit_snapshot),
    ("okx", _okx_snapshot),
)

_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_INFLIGHT: Dict[str, float] = {}


def _aggregate(analyses: List[Dict[str, Any]], now: float) -> Dict[str, Any]:
    mids = [a["mid"] for a in analyses]
    ref_mid = sum(mids) / len(mids)
    buy_usd = sum((a["whale_tape"]["buy_usd"] or 0.0) for a in analyses)
    sell_usd = sum((a["whale_tape"]["sell_usd"] or 0.0) for a in analyses)
    tape_total = buy_usd + sell_usd
    tape_imbalance = ((buy_usd - sell_usd) / tape_total) if tape_total > 0 else None

    # Wall pressure: nearer + stronger walls dominate. Each side scores its
    # walls by strength / distance; the side balance feeds the final score.
    def side_score(key_near: str, which: str) -> float:
        total = 0.0
        for a in analyses:
            for wall_key in ("nearest", "strongest"):
                w = a[key_near].get(wall_key)
                if not w:
                    continue
                dist = max(0.05, w["distance_pct"])
                total += w["usd"] / dist
        return total

    buy_wall_score = side_score("buy_walls", "bids")
    sell_wall_score = side_score("sell_walls", "asks")
    wall_total = buy_wall_score + sell_wall_score
    wall_imbalance = ((buy_wall_score - sell_wall_score) / wall_total) if wall_total > 0 else None

    components, weights = [], []
    if tape_imbalance is not None:
        components.append(tape_imbalance)
        weights.append(0.55)
    if wall_imbalance is not None:
        components.append(wall_imbalance)
        weights.append(0.45)
    if not components:
        score = None
    else:
        wsum = sum(weights)
        score = round(_clamp(sum(c * w for c, w in zip(components, weights)) / wsum * 100.0, -100.0, 100.0))

    # Spoofing discount: high spoofing risk weakens the wall component's say
    # and lowers confidence — walls that churn are less trustworthy evidence.
    spoof_values = [a["spoofing_risk"] for a in analyses]
    spoofing = round(sum(spoof_values) / len(spoof_values), 3) if spoof_values else None
    persistence_values = [a["persistence"] for a in analyses if a["persistence"] is not None]
    persistence = round(sum(persistence_values) / len(persistence_values), 4) if persistence_values else None
    cancellation = [a["cancellation_activity"] for a in analyses if a["cancellation_activity"] is not None]
    cancellation_activity = round(sum(cancellation) / len(cancellation), 2) if cancellation else None

    if score is None:
        bias = "UNAVAILABLE"
    elif score >= 20:
        bias = "BULLISH"
    elif score <= -20:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    # confidence: more exchanges + whale tape present + low spoofing = higher
    confidence = 0.25 + 0.15 * (len(analyses) - 1)
    if tape_imbalance is not None:
        confidence += 0.2
    if wall_imbalance is not None:
        confidence += 0.1
    if spoofing is not None:
        confidence -= 0.25 * spoofing
    confidence = round(_clamp(confidence, 0.05, 0.95), 3)

    def nearest(side_attr: str) -> Optional[Dict[str, Any]]:
        candidates = []
        for a in analyses:
            w = a[side_attr].get("nearest")
            if w:
                candidates.append({**w, "exchange": a["exchange"], "symbol": a["symbol"]})
        return min(candidates, key=lambda w: w["distance_pct"]) if candidates else None

    def strongest(side_attr: str) -> Optional[Dict[str, Any]]:
        candidates = []
        for a in analyses:
            w = a[side_attr].get("strongest")
            if w:
                candidates.append({**w, "exchange": a["exchange"], "symbol": a["symbol"]})
        return max(candidates, key=lambda w: w["usd"]) if candidates else None

    tape_buy_count = sum(a["whale_tape"]["buy_count"] for a in analyses)
    tape_sell_count = sum(a["whale_tape"]["sell_count"] for a in analyses)
    pressure_total = buy_usd + sell_usd
    return {
        "indicator": "whale_flow",
        "asset_class": "crypto",
        "bias": bias,
        "score": score,
        "confidence": confidence,
        "buy_pressure": round(buy_usd / pressure_total, 4) if pressure_total > 0 else None,
        "sell_pressure": round(sell_usd / pressure_total, 4) if pressure_total > 0 else None,
        "whale_buy_usd": round(buy_usd, 2),
        "whale_sell_usd": round(sell_usd, 2),
        "whale_buy_count": tape_buy_count,
        "whale_sell_count": tape_sell_count,
        "nearest_buy_wall": nearest("buy_walls"),
        "nearest_sell_wall": nearest("sell_walls"),
        "strongest_buy_wall": strongest("buy_walls"),
        "strongest_sell_wall": strongest("sell_walls"),
        "persistence": persistence,
        "cancellation_activity": cancellation_activity,
        "spoofing_risk": spoofing,
        "reference_mid": round(ref_mid, 6),
        "data_freshness": {
            "as_of_epoch": round(now, 3),
            "age_seconds": 0,
            "stale": False,
            "sources": {a["exchange"]: {"symbol": a["symbol"], "age_seconds": 0} for a in analyses},
        },
        "exchanges": analyses,
        "sources_ok": [a["exchange"] for a in analyses],
    }


def _unavailable(reason: str, errors: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return {
        "indicator": "whale_flow",
        "asset_class": "crypto",
        "bias": "UNAVAILABLE",
        "score": None,
        "confidence": 0.0,
        "buy_pressure": None,
        "sell_pressure": None,
        "nearest_buy_wall": None,
        "nearest_sell_wall": None,
        "strongest_buy_wall": None,
        "strongest_sell_wall": None,
        "persistence": None,
        "cancellation_activity": None,
        "spoofing_risk": None,
        "data_freshness": {"stale": True, "reason": reason,
                           "errors": errors or {}},
        "exchanges": [],
        "sources_ok": [],
    }


def get_whale_flow(ticker: str, *, force: bool = False) -> Dict[str, Any]:
    """Aggregate whale-flow evidence for a crypto ticker (cached, fail-safe)."""
    _ensure_runtime_config()
    if not _CFG["enabled"]:
        return _unavailable("indicator disabled")
    base = _base_asset(ticker)
    if base is None:
        return _unavailable(f"unsupported crypto symbol: {ticker}")
    key = base.upper()
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and not force and (now - hit[0]) < float(_CFG["book_ttl_seconds"]):
            payload = dict(hit[1])
            age = now - hit[0]
            payload["data_freshness"] = {**payload.get("data_freshness", {}),
                                         "age_seconds": round(age, 1),
                                         "stale": age > float(_CFG["stale_after_seconds"])}
            if payload["data_freshness"]["stale"]:
                return _unavailable("cached snapshot stale", payload["data_freshness"].get("errors"))
            return payload
        if key in _INFLIGHT and (now - _INFLIGHT[key]) < 8.0:
            return _unavailable("fetch in progress")  # singleflight: no stacking
        _INFLIGHT[key] = now
    try:
        errors: Dict[str, str] = {}
        analyses: List[Dict[str, Any]] = []
        for name, fetcher in _PROVIDERS:
            try:
                snap = fetcher(base)
                analyses.append(_analyze_exchange(snap, time.time()))
            except _ProviderFailure as exc:
                errors[name] = str(exc)[:160]
            except Exception as exc:  # never let one provider kill the rest
                errors[name] = f"{type(exc).__name__}: {str(exc)[:120]}"
                logger.debug("whale_flow provider %s failed for %s: %s", name, base, exc)
        if not analyses:
            result = _unavailable("all providers failed", errors)
        else:
            result = _aggregate(analyses, time.time())
            result["data_freshness"]["errors"] = errors
        with _CACHE_LOCK:
            _CACHE[key] = (time.time(), result)
        return result
    finally:
        with _CACHE_LOCK:
            _INFLIGHT.pop(key, None)


def whale_flow_status() -> Dict[str, Any]:
    with _CACHE_LOCK:
        cached = {k: {"age_seconds": round(time.time() - ts, 1),
                      "bias": v.get("bias")} for k, (ts, v) in _CACHE.items()}
    return {"cache": cached, "history_keys": len(_HISTORY), "config": dict(_CFG)}
