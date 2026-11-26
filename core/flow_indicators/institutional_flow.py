"""Institutional Flow PROXY — free-stock-data evidence, honestly labelled.

This is a PROXY, not true US Level 2 institutional flow. Free sources cannot
see the whole market; every output carries `proxy: True` and the disclaimer
below, and each sub-component reports its own availability. Nothing here may
veto or originate trades — it is evidence for existing setups only.

Sources:
- Yahoo Finance chart API (public, already used by Market Scanner):
  intraday 5m bars -> up/down volume split, volume acceleration, absorption
  proxy; daily bars -> relative volume.
- Alpaca IEX snapshots (only when credentials exist; already integrated in
  core/alpaca_client.py) -> bid/ask sizes (quote pressure).
- FINRA OTC transparency API (public) -> weekly off-exchange context.

Fail-safe contract: provider failure degrades the affected component to
UNAVAILABLE; total failure -> bias=UNAVAILABLE with no invented values.
"""
from __future__ import annotations

import logging
import threading
import time
import math
from typing import Any, Dict, List, Optional, Tuple

from core.http_utils import http_get

logger = logging.getLogger(__name__)

PROXY_LABEL = "INSTITUTIONAL FLOW PROXY"
PROXY_DISCLAIMER = (
    "Built from free data (Yahoo intraday bars, optional Alpaca IEX quotes, "
    "FINRA weekly OTC volumes). This does NOT represent complete US Level 2 "
    "institutional flow — treat as directional proxy evidence only."
)

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "ttl_seconds": 90.0,
    "stale_after_seconds": 300.0,
    "large_print_min_usd": 250_000.0,
    "request_timeout": (2, 5),
}

_CFG = dict(DEFAULTS)
_UA = {"User-Agent": "Mozilla/5.0"}

_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_INFLIGHT: Dict[str, float] = {}


def configure(config: Optional[Dict[str, Any]] = None) -> None:
    if not isinstance(config, dict):
        return
    for key in DEFAULTS:
        if key in config:
            try:
                _CFG[key] = config[key] if isinstance(DEFAULTS[key], tuple) else type(DEFAULTS[key])(config[key])
            except (TypeError, ValueError):
                logger.warning("institutional_flow: ignoring bad config value for %s", key)


# Last `flow_indicators` section applied from the shared config, so decision-
# pipeline callers (which bypass the /api/flow endpoints that apply it) still
# honor owner configuration like `flow_indicators.enabled: false`.
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
        configure({**section, **(section.get("institutional") or {})})


def _clamp(value: float, low: float, high: float) -> float:
    # NaN must clamp to the neutral floor: Python's min/max would happily
    # turn a NaN into `high` (max-bullish) via min(1, nan) == 1.
    if value != value:  # NaN check — math.isfinite rejects inf too
        return low
    return max(low, min(high, value))


def _is_stock_symbol(ticker: str) -> bool:
    t = str(ticker or "").upper().strip()
    if not t or "-" in t or "=" in t or "^" in t:
        return False
    return t.isalpha() and 1 <= len(t) <= 6


def _stock_symbol(ticker: str) -> str:
    return str(ticker).upper().strip().replace("-", "")


# ── Yahoo chart (intraday + daily volume context) ──────────────────────────
def _yahoo_chart(symbol: str, range_: str, interval: str) -> Optional[Dict[str, Any]]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    res = http_get(url, provider="yahoo", headers=_UA, timeout=_CFG["request_timeout"],
                   max_retries=0, params={"range": range_, "interval": interval,
                                          "includePrePost": "false", "events": "div,splits"})
    if res is None or res.status_code != 200:
        return None
    try:
        payload = res.json()
    except ValueError:
        return None
    result = ((payload or {}).get("chart") or {}).get("result") or []
    return result[0] if result else None


def _bars(chart: Dict[str, Any]) -> List[Dict[str, Any]]:
    stamps = chart.get("timestamp") or []
    quote = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    bars = []
    for i, ts in enumerate(stamps):
        close = closes[i] if i < len(closes) else None
        vol = volumes[i] if i < len(volumes) else None
        if close is None or vol is None:
            continue
        try:
            close_f = float(close)
            vol_f = float(vol)
        except (TypeError, ValueError):
            continue
        # Yahoo can emit NaN cells; a non-finite close/volume would poison the
        # averages downstream (NaN volume ⇒ NaN acceleration ⇒ clamped to +1).
        if not (math.isfinite(close_f) and math.isfinite(vol_f)):
            continue
        bars.append({"ts": float(ts), "close": close_f, "volume": vol_f})
    return bars


def _intraday_metrics(bars: List[Dict[str, Any]], now: float) -> Dict[str, Any]:
    """Up/down volume split, acceleration, absorption proxy from 5m bars."""
    out: Dict[str, Any] = {
        "up_volume": None, "down_volume": None, "up_down_ratio": None,
        "volume_acceleration": None, "absorption_bias": None, "bar_count": len(bars),
    }
    if len(bars) < 6:
        return out
    up = down = 0.0
    for prev, cur in zip(bars, bars[1:]):
        vol = cur["volume"]
        if cur["close"] > prev["close"]:
            up += vol
        elif cur["close"] < prev["close"]:
            down += vol
    total = up + down
    out["up_volume"] = round(up, 0)
    out["down_volume"] = round(down, 0)
    if total > 0:
        out["up_down_ratio"] = round(up / down, 3) if down > 0 else None

    # acceleration: last 6 bars' pace vs the session average pace
    recent = bars[-6:]
    older = bars[:-6]
    recent_pace = (sum(b["volume"] for b in recent) / (len(recent) * 5.0)) if recent else 0.0
    older_pace = (sum(b["volume"] for b in older) / (len(older) * 5.0)) if older else 0.0
    if older_pace > 0:
        out["volume_acceleration"] = round(recent_pace / older_pace, 3)

    # absorption proxy: heavy volume with small net price progress = one side
    # absorbing the other's supply. Down-volume dominance while price holds
    # up => bullish absorption (bids absorbing sells), and vice versa.
    first, last = bars[0]["close"], bars[-1]["close"]
    price_move_pct = ((last - first) / first * 100.0) if first > 0 else 0.0
    if total > 0 and abs(price_move_pct) < 1.0:
        imbalance = (up - down) / total  # >0 more up-volume
        # small move + volume imbalance => absorption against the imbalance's
        # obvious direction: flat price on heavy SELL volume = bid absorption.
        out["absorption_bias"] = round(-imbalance, 4) if abs(imbalance) > 0.15 else 0.0
    else:
        out["absorption_bias"] = 0.0
    return out


def _relative_volume(daily_bars: List[Dict[str, Any]]) -> Optional[float]:
    """Today's cumulative volume pace vs prior days' average (partial-day aware)."""
    if len(daily_bars) < 3:
        return None
    today = daily_bars[-1]
    prior = daily_bars[1:-1]
    # today's daily bar opens at the session open; elapsed fraction of the
    # 6.5h US session (clamped: pre-open ~0.05, after close 1.0)
    span_secs = max(1.0, time.time() - today["ts"])
    elapsed_frac = _clamp(span_secs / (6.5 * 3600), 0.05, 1.0)
    avg_prior = sum(b["volume"] for b in prior) / len(prior)
    if avg_prior <= 0:
        return None
    return round(today["volume"] / (avg_prior * elapsed_frac), 3)


# ── Alpaca IEX quote pressure (optional, credentials-gated) ────────────────
def _alpaca_quote_pressure(symbol: str) -> Dict[str, Any]:
    out = {"available": False, "bid_size": None, "ask_size": None,
           "bid_ask_ratio": None, "source": None}
    try:
        from core.alpaca_client import get_alpaca_client
        client = get_alpaca_client()
        if not client.is_available():
            return out
        rows = client.get_stock_snapshots([symbol], ttl=30) or {}
        row = rows.get(symbol) or rows.get(_stock_symbol(symbol)) or {}
        bid_size = row.get("bid_size") or row.get("bidSize")
        ask_size = row.get("ask_size") or row.get("askSize")
        src = row.get("source") or "alpaca_iex"
        if bid_size is None or ask_size is None:
            out["source"] = src
            return out
        bid_size, ask_size = float(bid_size), float(ask_size)
        out.update({
            "available": True, "bid_size": bid_size, "ask_size": ask_size,
            "bid_ask_ratio": round(bid_size / ask_size, 3) if ask_size > 0 else None,
            "source": src,
        })
    except Exception as exc:
        logger.debug("institutional_flow: alpaca quote pressure unavailable: %s", exc)
        out["error"] = str(exc)[:120]
    return out


# ── FINRA OTC weekly context (optional, public) ─────────────────────────────
def _finra_off_exchange(symbol: str) -> Dict[str, Any]:
    out = {"available": False, "weekly_otc_shares": None, "weekly_otc_trades": None,
           "week_start": None, "note": "FINRA OTC weekly summary publishes with ~2-4 week lag"}
    try:
        from datetime import datetime, timedelta, timezone
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=42)
        # The v2 API only honours filters in a POST body — as GET query params
        # they are silently ignored and the raw dataset head (2023) comes back.
        body = {
            "limit": 500,
            "compareFilters": [{"fieldName": "issueSymbolIdentifier",
                                "fieldValue": symbol, "compareType": "EQUAL"}],
            "dateRangeFilters": [{"fieldName": "weekStartDate",
                                  "startDate": start.strftime("%Y-%m-%d"),
                                  "endDate": end.strftime("%Y-%m-%d")}],
        }
        from core.http_utils import http_post
        res = http_post("https://api.finra.org/data/group/otcMarket/name/weeklySummary",
                        provider="finra",
                        headers={"Content-Type": "application/json", "Accept": "application/json"},
                        timeout=_CFG["request_timeout"], max_retries=0, json=body)
        if res is None or res.status_code != 200:
            out["error"] = f"HTTP {getattr(res, 'status_code', 'none')}"
            return out
        rows = res.json()
        if not isinstance(rows, list) or not rows:
            return out
        # rows are per-venue breakdowns; sum shares/trades per week, keep newest
        weeks: Dict[str, Dict[str, float]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            wk = str(row.get("weekStartDate") or "")
            if not wk:
                continue
            slot = weeks.setdefault(wk, {"shares": 0.0, "trades": 0.0})
            try:
                slot["shares"] += float(row.get("totalWeeklyShareQuantity") or 0.0)
                slot["trades"] += float(row.get("totalWeeklyTradeCount") or 0.0)
            except (TypeError, ValueError):
                continue
        if not weeks:
            return out
        newest = max(weeks)
        out.update({
            "available": weeks[newest]["shares"] > 0,
            "weekly_otc_shares": round(weeks[newest]["shares"], 2),
            "weekly_otc_trades": round(weeks[newest]["trades"], 2),
            "week_start": newest,
            "weeks_available": sorted(weeks, reverse=True)[:4],
        })
    except Exception as exc:
        logger.debug("institutional_flow: finra context unavailable: %s", exc)
        out["error"] = str(exc)[:120]
    return out


# ── aggregation ─────────────────────────────────────────────────────────────
def _unavailable(reason: str) -> Dict[str, Any]:
    return {
        "indicator": "institutional_flow_proxy",
        "proxy": True,
        "proxy_scope": PROXY_DISCLAIMER,
        "asset_class": "stock",
        "bias": "UNAVAILABLE",
        "score": None,
        "confidence": 0.0,
        "buy_pressure": None,
        "sell_pressure": None,
        "large_trade_bias": None,
        "volume_quote_pressure": None,
        "absorption_bias": None,
        "relative_volume": None,
        "off_exchange_context": None,
        "data_freshness": {"stale": True, "reason": reason},
        "sources_ok": [],
    }


def _build(symbol: str) -> Dict[str, Any]:
    now = time.time()
    intraday_chart = _yahoo_chart(symbol, "1d", "5m")
    daily_chart = _yahoo_chart(symbol, "5d", "1d")
    intraday = _bars(intraday_chart) if intraday_chart else []
    daily = _bars(daily_chart) if daily_chart else []
    if len(intraday) < 6 and len(daily) < 3:
        return _unavailable("no usable bars from Yahoo chart API")

    tape = _intraday_metrics(intraday, now)
    rel_volume = _relative_volume(daily)
    quote = _alpaca_quote_pressure(symbol)
    finra = _finra_off_exchange(symbol)

    components: List[float] = []
    weights: List[float] = []
    sources_ok: List[str] = ["yahoo_chart"]

    # 1. up/down volume tape (executed proxy — the core of the indicator)
    if tape["up_volume"] is not None and tape["down_volume"] is not None:
        total_v = tape["up_volume"] + tape["down_volume"]
        if total_v > 0:
            components.append((tape["up_volume"] - tape["down_volume"]) / total_v)
            weights.append(0.45)
    # 2. quote pressure (book proxy, only with Alpaca)
    if quote["available"] and quote["bid_ask_ratio"] is not None:
        ratio = math.log(max(1e-6, quote["bid_ask_ratio"]))
        components.append(_clamp(ratio / 1.5, -1.0, 1.0))
        weights.append(0.25)
        sources_ok.append("alpaca_iex")
    # 3. volume acceleration (attention/urgency proxy)
    if tape["volume_acceleration"] is not None:
        accel = (tape["volume_acceleration"] - 1.0) / 2.0  # 1x -> 0, 3x -> +1
        components.append(_clamp(accel, -1.0, 1.0))
        weights.append(0.15)
    # 4. absorption (contrarian tilt: flat price under one-sided volume)
    if tape["absorption_bias"] is not None:
        components.append(tape["absorption_bias"])
        weights.append(0.15)
    if finra["available"]:
        sources_ok.append("finra_otc_weekly")

    if not components:
        result = _unavailable("no flow components computable")
        result["tape"] = tape
        return result

    wsum = sum(weights)
    score = round(_clamp(sum(c * w for c, w in zip(components, weights)) / wsum * 100.0, -100.0, 100.0))
    bias = "BULLISH" if score >= 20 else ("BEARISH" if score <= -20 else "NEUTRAL")

    confidence = 0.3 + 0.1 * len(sources_ok)
    if rel_volume is not None and rel_volume >= 1.5:
        confidence += 0.1  # unusual volume makes the tape more meaningful
    confidence = round(_clamp(confidence, 0.05, 0.9), 3)

    total_v = (tape["up_volume"] or 0.0) + (tape["down_volume"] or 0.0)
    return {
        "indicator": "institutional_flow_proxy",
        "proxy": True,
        "proxy_scope": PROXY_DISCLAIMER,
        "asset_class": "stock",
        "bias": bias,
        "score": score,
        "confidence": confidence,
        "buy_pressure": round((tape["up_volume"] or 0.0) / total_v, 4) if total_v > 0 else None,
        "sell_pressure": round((tape["down_volume"] or 0.0) / total_v, 4) if total_v > 0 else None,
        "large_trade_bias": None,  # free tape hides trade sizes — honest absence
        "volume_quote_pressure": {
            "up_down_ratio": tape["up_down_ratio"],
            "volume_acceleration": tape["volume_acceleration"],
            "bid_ask_ratio": quote.get("bid_ask_ratio"),
            "quote_source": quote.get("source"),
        },
        "absorption_bias": tape["absorption_bias"],
        "relative_volume": rel_volume,
        "off_exchange_context": finra if finra["available"] else {
            "available": False, "reason": finra.get("error") or "FINRA weekly data not returned",
        },
        "data_freshness": {
            "as_of_epoch": round(now, 3),
            "age_seconds": 0,
            "stale": False,
            "intraday_bars": tape["bar_count"],
            "daily_bars": len(daily),
        },
        "tape": tape,
        "sources_ok": sources_ok,
    }


def get_institutional_flow(ticker: str, *, force: bool = False) -> Dict[str, Any]:
    """Proxy institutional-flow evidence for a stock ticker (cached, fail-safe)."""
    _ensure_runtime_config()
    if not _CFG["enabled"]:
        return _unavailable("indicator disabled")
    if not _is_stock_symbol(ticker):
        return _unavailable(f"not a plain stock symbol: {ticker}")
    key = _stock_symbol(ticker)
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and not force and (now - hit[0]) < float(_CFG["ttl_seconds"]):
            payload = dict(hit[1])
            age = now - hit[0]
            fresh = {**payload.get("data_freshness", {}), "age_seconds": round(age, 1),
                     "stale": age > float(_CFG["stale_after_seconds"])}
            payload["data_freshness"] = fresh
            if fresh["stale"]:
                return _unavailable("cached snapshot stale")
            return payload
        if key in _INFLIGHT and (now - _INFLIGHT[key]) < 8.0:
            return _unavailable("fetch in progress")
        _INFLIGHT[key] = now
    try:
        result = _build(key)
        with _CACHE_LOCK:
            _CACHE[key] = (time.time(), result)
        return result
    except Exception as exc:
        logger.warning("institutional_flow build failed for %s: %s", key, exc)
        return _unavailable(f"build error: {str(exc)[:120]}")
    finally:
        with _CACHE_LOCK:
            _INFLIGHT.pop(key, None)


def institutional_flow_status() -> Dict[str, Any]:
    with _CACHE_LOCK:
        cached = {k: {"age_seconds": round(time.time() - ts, 1),
                      "bias": v[1].get("bias")} for k, (ts, v) in _CACHE.items()}
    return {"cache": cached, "config": dict(_CFG)}
