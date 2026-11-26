# api/actions_service.py - Actions service for web API

import logging
import json
import os
import threading
import time
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

import pandas as pd

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import (
    ConfigManager,
    _config_write_lock,
    WATCHLIST_PATH,
    BACKTEST_RESULTS_PATH,
    SUPPRESSED_TICKERS_PATH,
    SQLITE_KEY_WATCHLIST,
    get_bucharest_time,
    BASE_DIR,
    normalize_market_ticker,
    is_crypto_ticker,
    deep_merge,
)
from core.sqlite_store import get_sqlite_store
from core.watchlist_quick_ai import run_quick_ai_for_ticker
from core.prompt_evidence import clean_lines
from core.ticker_workspace import get_ticker_workspace_store
from core.state_persistence import (
    load_signals_payload,
    save_signals_payload,
    load_state_payload,
    save_state_payload,
)
from core.strategy_v2 import (
    StrategyPerformanceCache,
    StrategySelector,
    build_context,
    infer_asset_class,
    load_cached_ohlc,
)

from api.state_service import get_state

logger = logging.getLogger("ActionsService")

# Shared config to reduce repeated file reads/log spam
_shared_config = None
def get_shared_config():
    global _shared_config
    if _shared_config is None:
        _shared_config = ConfigManager()
    else:
        _shared_config.reload_if_changed()
        _shared_config.reload_watchlist_if_changed()
    return _shared_config


def _dedupe_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for raw in items or []:
        ticker = _normalize_input_ticker(str(raw or ""))
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        out.append(ticker)
    return out


def _normalize_input_ticker(raw_ticker: str) -> str:
    return str(normalize_market_ticker(raw_ticker) or "").upper().strip()


def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
        temp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        import time
        for attempt in range(5):
            try:
                os.replace(str(temp_path), str(path))
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1)
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass


def _load_watchlist_payload() -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "stocks": [],
        "crypto": [],
        "hot_list": [],
        "order": [],
        "settings": {},
        "scanner_exclusions": [],
        "schema_version": 2,
    }
    if WATCHLIST_PATH.exists():
        try:
            with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
                payload = json.load(f) or payload
        except Exception as e:
            logger.warning(f"Could not load watchlist: {e}")
            # Fallback to SQLite snapshot for resilience during concurrent writes.
            try:
                store_payload = get_sqlite_store().get_json(SQLITE_KEY_WATCHLIST, None)
                if isinstance(store_payload, dict):
                    payload = store_payload
                    logger.info("Watchlist restored from SQLite in actions_service")
                else:
                    payload = {"stocks": [], "crypto": [], "hot_list": [], "order": [], "settings": {}, "scanner_exclusions": [], "schema_version": 2}
            except Exception:
                payload = {"stocks": [], "crypto": [], "hot_list": [], "order": [], "settings": {}, "scanner_exclusions": [], "schema_version": 2}

    stock_candidates = _dedupe_keep_order([_normalize_input_ticker(t) for t in (payload.get("stocks") or payload.get("yahoo") or [])])
    crypto = _dedupe_keep_order([_normalize_input_ticker(t) for t in (payload.get("crypto") or payload.get("cryptos") or payload.get("binance") or [])])
    moved_to_crypto = [t for t in stock_candidates if is_crypto_ticker(t)]
    stocks = [t for t in stock_candidates if t and not is_crypto_ticker(t)]
    crypto = _dedupe_keep_order(crypto + moved_to_crypto)

    all_tickers = _dedupe_keep_order(stocks + crypto)
    order = [_normalize_input_ticker(t) for t in (payload.get("order") or [])]
    order = [t for t in _dedupe_keep_order(order) if t in all_tickers]
    for ticker in all_tickers:
        if ticker not in order:
            order.append(ticker)

    hot_list = [_normalize_input_ticker(t) for t in (payload.get("hot_list") or [])]
    hot_list = [t for t in _dedupe_keep_order(hot_list) if t in all_tickers]

    raw_settings = payload.get("settings") or {}
    settings: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw_settings, dict):
        for raw_ticker, raw_cfg in raw_settings.items():
            ticker = _normalize_input_ticker(raw_ticker)
            if not ticker:
                continue
            settings[ticker] = raw_cfg if isinstance(raw_cfg, dict) else {}

    scanner_exclusions = _dedupe_keep_order([
        _normalize_input_ticker(t) for t in (payload.get("scanner_exclusions") or [])
    ])

    return {
        "stocks": stocks,
        "crypto": crypto,
        "hot_list": hot_list,
        "order": order,
        "settings": settings,
        "scanner_exclusions": scanner_exclusions,
        "schema_version": 2,
    }


def _save_watchlist_payload(payload: Dict[str, Any]) -> None:
    with _config_write_lock(WATCHLIST_PATH):
        try:
            data = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8")) if WATCHLIST_PATH.exists() else {}
            if not isinstance(data, dict):
                raise ValueError("watchlist root is not an object")
        except Exception:
            # Unreadable file: fall back to the SQLite snapshot loader so the
            # save that repairs the file is not blocked.
            data = _load_watchlist_payload()
        # Ticker/settings operations do not own alerts. Preserve the latest
        # alert list even when the submitted watchlist snapshot is stale.
        data.update({k: v for k, v in (payload or {}).items() if k != "alerts"})
        _write_json_atomic(WATCHLIST_PATH, data)
        get_sqlite_store().set_json(SQLITE_KEY_WATCHLIST, data)
    # Keep ConfigManager synchronized after manual writes.
    try:
        get_shared_config().reload_watchlist_if_changed()
    except Exception:
        pass

# Cache analysis results briefly to avoid repeated heavy calls
_analysis_cache: Dict[str, Dict] = {}
_analysis_lock = threading.Lock()
_ANALYSIS_TTL_SECONDS = 3600  # 1 hour - cache analysis for faster reuse
_ANALYSIS_TTL_HEAVY_SECONDS = 3600  # 1 hour for heavy analysis too

def _analysis_cache_key(ticker: str, include_heavy: bool, disable_ollama: bool) -> str:
    return f"{ticker}|heavy={int(bool(include_heavy))}|ollama={int(bool(disable_ollama))}"

def _get_cached_analysis(key: str, ttl: int) -> Optional[Dict]:
    with _analysis_lock:
        entry = _analysis_cache.get(key)
        if not entry:
            return None
        if time.time() - entry.get("ts", 0) < ttl:
            return entry.get("data")
    return None

def _set_cached_analysis(key: str, data: Dict):
    # A failed or timed-out AI call (ai_status "unavailable") is not kept for
    # the hour-long TTL: the next "Analyze" click asks the AI again.
    ai = data.get("ai_analysis") if isinstance(data, dict) else None
    if isinstance(ai, dict) and ai.get("ai_status") == "unavailable":
        with _analysis_lock:
            _analysis_cache.pop(key, None)
        return
    with _analysis_lock:
        _analysis_cache[key] = {"ts": time.time(), "data": data}


def _parse_date_only(date_str: str):
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(str(date_str)[:10]).date()
    except Exception:
        return None


def _get_earnings_days_for_ticker(ticker: str) -> int:
    ticker = (ticker or "").upper().strip()
    if not ticker or is_crypto_ticker(ticker):
        return -1

    # 1) Calendar cache (fast path)
    try:
        from agents.economic_calendar_agent import get_economic_calendar_agent
        config = get_shared_config()
        calendar = get_economic_calendar_agent(config.config)
        ev = calendar.get_earnings_for_ticker(ticker)
        if isinstance(ev, dict):
            dt = _parse_date_only(ev.get("date") or ev.get("date_formatted"))
            if dt:
                today = datetime.now(timezone.utc).date()
                return int((dt - today).days)
    except Exception:
        pass

    return -1


def _analysis_ttl_seconds(ticker: str, include_heavy: bool, disable_ollama: bool) -> int:
    # Adaptive TTL for details/AI around earnings window.
    # OPTIMIZED: Longer cache for faster subsequent loads
    if include_heavy or not disable_ollama:
        days = _get_earnings_days_for_ticker(ticker)
        if 0 <= days <= 5:
            return 1800  # 30 minutes near earnings (was 600)
        return 3600  # 1 hour outside earnings window (was 1800)
    return _ANALYSIS_TTL_SECONDS


_BACKTEST_HISTORY_LOCK = threading.Lock()
_BACKTEST_MIN_SAMPLE_DEFAULT = 30
_BACKTEST_MAX_DAYS = 365
_BACKTEST_TRADE_KEYS = ("signals", "trades", "trade_list", "closed_oos_trades", "equity_curve")


def _json_safe(value: Any) -> Any:
    """Plain-JSON copy of a backtest result (dates, Timestamps and numpy
    scalars become strings/numbers; NaN/inf become null)."""
    def default(obj: Any) -> Any:
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        if hasattr(obj, "item"):
            return obj.item()
        return str(obj)

    def clean(obj: Any) -> Any:
        if isinstance(obj, float):
            return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
        if isinstance(obj, dict):
            return {str(k): clean(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [clean(v) for v in obj]
        return obj

    return clean(json.loads(json.dumps(value, default=default)))


def save_backtest_result(result: Dict) -> None:
    """Save backtest result to history file.

    Failed runs are not history: an error record used to be listed under
    "Recent simulations" as if it were a result. The read-modify-write is
    serialised because one "Run Analysis" click issues several requests in
    parallel, and the ORB result (dates, Timestamps, numpy scalars) is made
    JSON-safe first - it used to fail json.dump and was never saved.
    """
    try:
        if not isinstance(result, dict) or result.get("error"):
            return
        record = _json_safe(result)
        with _BACKTEST_HISTORY_LOCK:
            data_dir = BACKTEST_RESULTS_PATH.parent
            data_dir.mkdir(parents=True, exist_ok=True)

            history = []
            if BACKTEST_RESULTS_PATH.exists():
                try:
                    with open(BACKTEST_RESULTS_PATH, "r", encoding="utf-8") as f:
                        history = json.load(f)
                except Exception as e:
                    logger.warning(f"Could not load backtest history: {e}")
            if not isinstance(history, list):
                history = []

            # Add new result at the beginning
            history.insert(0, record)

            # Keep only last 50 results
            history = history[:50]

            _write_json_atomic(Path(BACKTEST_RESULTS_PATH), history)

    except Exception as e:
        logger.error(f"Error saving backtest result: {e}")


def _strip_backtest_trades(row: Any) -> Any:
    """Drop per-trade arrays at every level of a history record."""
    if isinstance(row, list):
        return [_strip_backtest_trades(item) for item in row]
    if not isinstance(row, dict):
        return row
    out: Dict[str, Any] = {}
    for key, value in row.items():
        if key in _BACKTEST_TRADE_KEYS and isinstance(value, list):
            continue
        out[key] = _strip_backtest_trades(value) if isinstance(value, (dict, list)) else value
    return out


def get_backtest_history(
    *,
    limit: Optional[int] = None,
    include_trades: bool = True,
) -> List[Dict]:
    """Get list of recent backtest results (newest FIRST, as they are stored).

    2026-09-03 audit P2: comparison records embed full per-combo signal lists,
    so the unbounded payload reached ~150KB for 3 records and grows toward
    several MB at the 50-record cap. `limit` bounds the count and
    `include_trades=False` strips the heavy per-trade arrays (the list view
    only renders ticker/mode/time; detail views pass include_trades=True).

    `limit` keeps the N most recent runs. It used to slice the tail of a
    newest-first list, i.e. it returned the N OLDEST. `include_trades=False`
    used to strip only a top-level `signals` key, which comparison records do
    not have, so their nested trade arrays (the bulk of the payload) stayed."""
    try:
        if BACKTEST_RESULTS_PATH.exists():
            with open(BACKTEST_RESULTS_PATH, "r", encoding="utf-8") as f:
                history = json.load(f)
            if isinstance(history, list):
                rows = history if limit is None else history[:max(1, int(limit))]
                from core.backtest_summary import normalize_history_record
                rows = [normalize_history_record(row) for row in rows if isinstance(row, dict)]
                if not include_trades:
                    rows = [_strip_backtest_trades(row) for row in rows]
                return rows
    except Exception as e:
        logger.error(f"Error getting backtest history: {e}")
    return []



def add_ticker(ticker: str, return_state: bool = True) -> Dict:
    """
    Add a ticker to the watchlist.

    Args:
        ticker: Ticker symbol (e.g., "NVDA" or "BTC-USD")

    Returns:
        Updated state dictionary
    """
    try:
        ticker = _normalize_input_ticker(ticker)
        if not ticker:
            raise ValueError("Ticker cannot be empty")

        watchlist = _load_watchlist_payload()

        # Determine if crypto or stock
        is_crypto = is_crypto_ticker(ticker)

        added = False
        if is_crypto:
            if ticker not in watchlist.get("crypto", []):
                watchlist.setdefault("crypto", []).append(ticker)
                logger.info(f"Added crypto ticker: {ticker}")
                added = True
            else:
                logger.info(f"Ticker already in watchlist: {ticker}")
        else:
            if ticker not in watchlist.get("stocks", []):
                watchlist.setdefault("stocks", []).append(ticker)
                logger.info(f"Added stock ticker: {ticker}")
                added = True
            else:
                logger.info(f"Ticker already in watchlist: {ticker}")

        # Update order list
        order = _dedupe_keep_order(watchlist.get("order", []))
        if ticker not in order:
            order.append(ticker)
            watchlist["order"] = order

        # A manual add is an explicit override of a previous removal.
        watchlist["scanner_exclusions"] = [
            item for item in watchlist.get("scanner_exclusions", []) if item != ticker
        ]

        _save_watchlist_payload(watchlist)
        try:
            ws = get_ticker_workspace_store()
            seed_payload = {
                "price": 0.0,
                "change_pct": 0.0,
                "trend_1h": "N/A",
                "trend_30m": "N/A",
                "trend_5m": "N/A",
            }
            try:
                if is_crypto:
                    from api.market_data_service import get_ticker_data

                    live_data = get_ticker_data(ticker, lightweight=True) or {}
                    live_price = float(live_data.get("price", 0.0) or 0.0)
                    if live_price > 0:
                        seed_payload.update(
                            {
                                "price": live_price,
                                "change_pct": float(live_data.get("change_pct", 0.0) or 0.0),
                                "trend_1h": live_data.get("trend_1h", "N/A"),
                                "trend_30m": live_data.get("trend_30m", "N/A"),
                                "trend_5m": live_data.get("trend_5m", "N/A"),
                            }
                        )
                else:
                    pass
            except Exception:
                pass
            ws.upsert_market_data(
                ticker,
                seed_payload,
            )
            ws.bump_version()
        except Exception:
            pass

        if return_state:
            return get_state()
        return {
            "status": "ok",
            "ticker": ticker,
            "added": added,
            "message": "ticker_added" if added else "ticker_exists",
            "watchlist": watchlist,
        }

    except Exception as e:
        logger.error(f"Error adding ticker {ticker}: {e}")
        raise


def get_ticker_settings(ticker: str) -> Dict:
    """Get settings for a specific ticker from ConfigManager."""
    try:
        config = get_shared_config()
        raw = config.get_ticker_settings(ticker)
        effective = config.get_effective_ticker_settings(ticker) if hasattr(config, "get_effective_ticker_settings") else raw
        out = dict(raw or {})
        out["effective_settings"] = effective
        return out
    except Exception as e:
        logger.error(f"Error getting settings for {ticker}: {e}")
        return {}


def update_ticker_settings(ticker: str, settings: Dict) -> Dict:
    """Update settings for a specific ticker."""
    try:
        config = get_shared_config()
        config.set_ticker_settings(ticker, settings)
        return {
            "status": "ok",
            "settings": config.get_ticker_settings(ticker),
            "effective_settings": config.get_effective_ticker_settings(ticker) if hasattr(config, "get_effective_ticker_settings") else config.get_ticker_settings(ticker),
        }
    except Exception as e:
        logger.error(f"Error updating settings for {ticker}: {e}")
        raise


def preview_global_settings_apply(settings: Dict) -> Dict[str, Any]:
    config = get_shared_config()
    wl = config.watchlist or {}
    all_tickers = _dedupe_keep_order((wl.get("order") or []) + (wl.get("yahoo") or []) + (wl.get("binance") or []))
    affected = 0
    changed_fields = set()
    payload = settings or {}

    for ticker in all_tickers:
        current = config.get_ticker_settings(ticker)
        will_change = False
        for key, value in payload.items():
            if key == "strategy_notes":
                continue
            if key not in {"min_win_rate", "tp_pct", "sl_pct", "signals_enabled", "allowed_strategies"}:
                continue
            if current.get(key) != value:
                changed_fields.add(key)
                will_change = True
        if will_change:
            affected += 1

    return {
        "total_tickers": len(all_tickers),
        "affected_tickers": affected,
        "changed_fields": sorted(changed_fields),
    }


def remove_tickers(tickers: List[str], return_state: bool = True) -> Dict:
    """
    Remove tickers from the watchlist.

    Args:
        tickers: List of ticker symbols to remove

    Returns:
        Updated state dictionary
    """
    try:
        tickers = [_normalize_input_ticker(t) for t in tickers if str(t or "").strip()]
        tickers = _dedupe_keep_order(tickers)
        if not tickers:
            raise ValueError("No tickers provided")

        watchlist = _load_watchlist_payload()

        removed = []
        for ticker in tickers:
            is_crypto = is_crypto_ticker(ticker)

            if is_crypto:
                if ticker in watchlist.get("crypto", []):
                    watchlist["crypto"].remove(ticker)
                    removed.append(ticker)
            else:
                if ticker in watchlist.get("stocks", []):
                    watchlist["stocks"].remove(ticker)
                    removed.append(ticker)

        # Update order list
        order = watchlist.get("order", [])
        watchlist["order"] = [t for t in order if t not in removed]
        # Keep hot list/settings clean after removals.
        watchlist["hot_list"] = [t for t in watchlist.get("hot_list", []) if t not in removed]
        settings = watchlist.get("settings", {}) or {}
        if isinstance(settings, dict):
            for ticker in removed:
                settings.pop(ticker, None)
            watchlist["settings"] = settings
        # Manual deletion is durable even if scanner auto-promotion is enabled
        # again later. A subsequent manual add clears this exclusion.
        scanner_exclusions = _dedupe_keep_order(watchlist.get("scanner_exclusions", []))
        for ticker in removed:
            if ticker not in scanner_exclusions:
                scanner_exclusions.append(ticker)
        watchlist["scanner_exclusions"] = scanner_exclusions

        _save_watchlist_payload(watchlist)
        try:
            get_ticker_workspace_store().invalidate_tickers(removed)
        except Exception:
            pass

        logger.info(f"Removed tickers: {removed}")
        if return_state:
            return get_state()
        return {"status": "ok", "removed": removed, "watchlist": watchlist}

    except Exception as e:
        logger.error(f"Error removing tickers: {e}")
        raise


def reorder_watchlist(ticker_order: List[str]) -> Dict:
    """
    Reorder the watchlist tickers.
    
    Args:
        ticker_order: Complete list of tickers in the desired order
        
    Returns:
        Updated state dictionary
    """
    try:
        if not ticker_order:
            raise ValueError("Ticker order cannot be empty")

        watchlist = _load_watchlist_payload()
        existing_stocks = _dedupe_keep_order(watchlist.get("stocks", []))
        existing_crypto = _dedupe_keep_order(watchlist.get("crypto", []))
        existing_order = _dedupe_keep_order(watchlist.get("order", []))
        existing_all = _dedupe_keep_order(existing_order + existing_stocks + existing_crypto)
        if not existing_all:
            raise ValueError("Watchlist is empty")

        # Sanitize request and keep only known tickers.
        requested = _dedupe_keep_order([_normalize_input_ticker(t) for t in ticker_order if str(t or "").strip()])
        requested = [t for t in requested if t in existing_all]

        # Ensure full set present and stable fallback order.
        final_order = list(requested)
        for ticker in existing_all:
            if ticker not in final_order:
                final_order.append(ticker)

        existing_stock_set = set(existing_stocks)
        existing_crypto_set = set(existing_crypto)
        new_stocks = [t for t in final_order if t in existing_stock_set and not is_crypto_ticker(t)]
        new_crypto = [t for t in final_order if t in existing_crypto_set or is_crypto_ticker(t)]
        # Keep any old items that were missing from sets due legacy payload drift.
        for ticker in final_order:
            if ticker not in new_stocks and ticker not in new_crypto:
                if is_crypto_ticker(ticker):
                    new_crypto.append(ticker)
                else:
                    new_stocks.append(ticker)

        watchlist["stocks"] = _dedupe_keep_order(new_stocks)
        watchlist["crypto"] = _dedupe_keep_order(new_crypto)
        watchlist["order"] = _dedupe_keep_order(final_order)
        watchlist["hot_list"] = [t for t in _dedupe_keep_order(watchlist.get("hot_list", [])) if t in set(watchlist["order"])]

        _save_watchlist_payload(watchlist)
        try:
            get_ticker_workspace_store().bump_version()
        except Exception:
            pass

        logger.info(f"Reordered watchlist. New order size: {len(watchlist['order'])}")
        return get_state()

    except Exception as e:
        logger.error(f"Error reordering watchlist: {e}")
        raise


# A row the engine has not refreshed for more than a day does not hold a
# current price (a closed-session row is refreshed every few minutes with the
# last regular close, so it stays young and is fine).
QUICK_AI_PRICE_MAX_AGE_SECONDS = 86400.0


def _quick_ai_row_age_seconds(row: Dict[str, Any]) -> Optional[float]:
    """Age of the engine row in seconds, or None when it is not known."""
    raw_age = row.get("analysis_age_seconds")
    if raw_age is not None and not isinstance(raw_age, bool):
        try:
            age = float(raw_age)
            if age == age and age >= 0:
                return age
        except (TypeError, ValueError):
            pass
    stamp = str(row.get("analysis_updated_at") or row.get("last_update") or "").strip()
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def _build_quick_ai_context_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    news_pulse = row.get("news_pulse", {}) or {}
    n24 = news_pulse.get("24h", {}) or {}
    n7 = news_pulse.get("7d", {}) or {}
    top_heads = []
    for item in (news_pulse.get("top_headlines", []) or []):
        if isinstance(item, dict):
            top_heads.append(str(item.get("title") or "unknown"))

    # Price honesty: the model is only given a price it may treat as current.
    # A row older than 24 h, or a row without a real price (the placeholder row
    # has price 0), contributes no `price` / `change_pct` key at all, so the
    # prompt prints "unknown" instead of a months-old or invented number.
    row_age = _quick_ai_row_age_seconds(row)
    try:
        price_value = float(row.get("price"))
    except (TypeError, ValueError):
        price_value = 0.0
    price_fields: Dict[str, Any] = {}
    if not (price_value == price_value and price_value > 0):
        price_fields["price_omitted_reason"] = "no price available for this ticker"
    elif row_age is not None and row_age > QUICK_AI_PRICE_MAX_AGE_SECONDS:
        price_fields["price_omitted_reason"] = (
            f"last engine update is {int(row_age // 86400)} day(s) old - too old to use as the current price"
        )
    else:
        price_fields["price"] = row.get("price")
        price_fields["change_pct"] = row.get("change_pct")
    return {
        **price_fields,
        # What the price is: the date of the bar it comes from and the session
        # ("closed" = last regular close, "regular" = live session).
        "price_as_of": row.get("price_as_of") or None,
        "session": row.get("session") or None,
        "trend_alignment": row.get("trend_alignment", "unknown"),
        # How old the technical read is, so the prompt can leave out a trend
        # that was computed days ago instead of presenting it as current.
        "analysis_available": row.get("analysis_available"),
        "analysis_age_seconds": row.get("analysis_age_seconds"),
        "signal_status": row.get("signal_status", "none"),
        "signal_score": row.get("signal_score", 0),
        "earnings_days": row.get("earnings_days", -1),
        "earnings_when": row.get("earnings_when", ""),
        "news_24h_count": n24.get("article_count", 0),
        "news_24h_impact": n24.get("avg_impact_score", 0),
        "news_7d_count": n7.get("article_count", 0),
        "news_7d_impact": n7.get("avg_impact_score", 0),
        "news_score": row.get("news_pulse_score", 0),
        "news_sentiment": row.get("news_sentiment_trend", "unknown"),
        # Headlines are third-party text: control characters and chat-template
        # role markers are removed before they are placed in the prompt.
        "top_headlines": clean_lines(top_heads, limit=3, max_chars=240),
        # Optional advanced fundamentals (if present in row payload).
        "owner_earnings_per_share": row.get("owner_earnings_per_share"),
        "ev_to_ebitda": row.get("ev_to_ebitda"),
        "revenue_growth_yoy": row.get("revenue_growth_yoy"),
        "free_cash_flow_growth_yoy": row.get("free_cash_flow_growth_yoy"),
    }


def quick_analyze_ticker(ticker: str, force_refresh: bool = False) -> Dict[str, Any]:
    symbol = _normalize_input_ticker(ticker)
    if not symbol:
        raise ValueError("Ticker cannot be empty")

    state = get_state(include_news=False, include_hotlist=False)
    tickers = state.get("tickers", []) or []
    row = next((item for item in tickers if str(item.get("ticker", "")).upper() == symbol), None)
    if row is None:
        # Fallback minimal row if ticker is not currently in state list.
        row = {
            "ticker": symbol,
            "price": 0,
            "change_pct": 0,
            "trend_alignment": "unknown",
            "signal_status": "none",
            "signal_score": 0,
            "earnings_days": _get_earnings_days_for_ticker(symbol),
            "earnings_when": "",
            "news_pulse_score": 0,
            "news_pulse": {"24h": {}, "7d": {}, "top_headlines": []},
            "news_sentiment_trend": "unknown",
        }

    config = get_shared_config()
    context = _build_quick_ai_context_from_row(row)
    quick_payload = run_quick_ai_for_ticker(
        symbol,
        context=context,
        quick_ai_cfg=config.quick_ai,
        force_refresh=force_refresh,
    )
    return {
        "ticker": symbol,
        "context": context,
        "quick_ai": quick_payload,
    }


def analyze_ticker(
    ticker: str,
    include_heavy: bool = True,
    disable_ollama: bool = False,
    force_refresh: bool = False
) -> Dict:
    """
    Analyze a specific ticker.

    Args:
        ticker: Ticker symbol to analyze

    Returns:
        Analysis results dictionary
    """
    try:
        ticker = ticker.strip().upper()
        if not ticker:
            raise ValueError("Ticker cannot be empty")

        # Get last signal for this ticker from signals_state.json
        last_signal = _get_last_signal_for_ticker(ticker)

        # Get strategy notes from config
        config = get_shared_config()
        t_settings = config.get_ticker_settings(ticker)
        strategy_notes = t_settings.get("strategy_notes", "")

        # Short-lived cache for heavy analysis calls
        cache_key = _analysis_cache_key(ticker, include_heavy, disable_ollama)
        ttl = _analysis_ttl_seconds(ticker, include_heavy, disable_ollama)
        if not force_refresh:
            cached = _get_cached_analysis(cache_key, ttl)
            if cached:
                cached["cached"] = True
                return cached

        # Try to import and use ticker_analyzer
        try:
            from core.ticker_analyzer import analyze_ticker as do_analyze, format_analysis_text
            result = do_analyze(ticker, include_heavy=include_heavy, disable_ollama=disable_ollama)

            if result is None:
                # Return basic info when analysis fails
                fallback = {
                    "ticker": ticker,
                    "price": 0,
                    "change_pct": 0,
                    "asset_type": "crypto" if is_crypto_ticker(ticker) else "stock",
                    "trend_1h": "N/A",
                    "trend_30m": "N/A",
                    "trend_5m": "N/A",
                    "rsi": 50,
                    "macd_hist": 0,
                    "vwap": 0,
                    "ema20": 0,
                    "distance_from_vwap_pct": 0,
                    "distance_from_ema20_pct": 0,
                    "atr": 0,
                    "best_signal": None,
                    "last_signal": last_signal,
                    "mode_scores": {},
                    "quote": {},
                    "quote_batch": {},
                    "breaking_news": [],
                    "news": [],
                    "news_signals": {},
                    "ai_analysis": None,
                    "ai_analysis_text": "",
                    "text": f"Could not fetch data for {ticker}. Check if the ticker is correct.",
                    "error": f"Could not fetch data for {ticker}",
                    "timestamp": get_bucharest_time().isoformat()
                }
                # A failure is not cached: it used to be served as the
                # "CACHED" analysis for the next hour after one bad fetch.
                return fallback

            text = format_analysis_text(result)

            # Normalize best signal payload for UI
            best_signal = None
            if hasattr(result, 'best_signal') and result.best_signal:
                raw_bs = result.best_signal if isinstance(result.best_signal, dict) else {}
                profile_map = {"STRONG": "AGGRESSIVE", "MEDIUM": "MODERATE", "RELAX": "CONSERVATIVE"}
                best_signal = {
                    "direction": raw_bs.get("direction"),
                    "mode": raw_bs.get("mode"),
                    "profile": profile_map.get(raw_bs.get("profile"), raw_bs.get("profile")),
                    "score": raw_bs.get("score", 0),
                }

            # Get strategy comparison (all 9 combinations)
            strategy_comparison = _get_strategy_comparison(
                ticker,
                variant_mode=config.signal.get("variant_mode_default", "STRICT")
            )
            
            # Extract current scores for all modes/profiles
            current_scores = _extract_current_scores(result)

            # Build mode scores map for UI (Aggressive/Moderate/Conservative)
            mode_scores_ui = {}
            if hasattr(result, "mode_scores") and result.mode_scores:
                profile_map = {"STRONG": "AGGRESSIVE", "MEDIUM": "MODERATE", "RELAX": "CONSERVATIVE"}
                for mode, profiles in result.mode_scores.items():
                    mode_scores_ui[mode] = {}
                    for profile, score_obj in profiles.items():
                        ui_profile = profile_map.get(profile, profile)
                        mode_scores_ui[mode][ui_profile] = {
                            "best_score": getattr(score_obj, "best_score", 0),
                            "best_direction": getattr(score_obj, "best_direction", "-"),
                            "long_score": getattr(score_obj, "long_score", 0),
                            "short_score": getattr(score_obj, "short_score", 0),
                            "long_valid": getattr(score_obj, "long_valid", False),
                            "short_valid": getattr(score_obj, "short_valid", False),
                            "details": getattr(score_obj, "details", {})
                        }

            analysis = {
                "ticker": ticker,
                "price": getattr(result, "price", 0),
                "change_pct": getattr(result, "change_pct", 0),
                "asset_type": getattr(result, "asset_type", "unknown"),
                "trend_1d": getattr(result, "trend_1d", "N/A"),
                "trend_4h": getattr(result, "trend_4h", "N/A"),
                "trend_1h": getattr(result, "trend_1h", "N/A"),
                "trend_30m": getattr(result, "trend_30m", "N/A"),
                "trend_5m": getattr(result, "trend_5m", "N/A"),
                "rsi": getattr(result, "rsi", 50),
                "macd_hist": getattr(result, "macd_hist", 0),
                "vwap": getattr(result, "vwap", 0),
                "ema20": getattr(result, "ema20", 0),
                "distance_from_vwap_pct": getattr(result, "distance_from_vwap_pct", 0),
                "distance_from_ema20_pct": getattr(result, "distance_from_ema20_pct", 0),
                "atr": getattr(result, "atr", 0),
                "best_signal": best_signal,
                "last_signal": last_signal,
                "strategy_comparison": strategy_comparison,
                "current_scores": current_scores,
                "mode_scores": mode_scores_ui,
                "strategy_notes": strategy_notes,
                "fundamentals": getattr(result, "fundamentals", {}),
                "quote": getattr(result, "quote", {}),
                "quote_batch": getattr(result, "quote_batch", {}),
                "breaking_news": getattr(result, "breaking_news", []),
                "news": getattr(result, "news", []),
                "news_signals": getattr(result, "news_signals", {}),
                "ai_analysis": getattr(result, "ai_analysis", None),
                "ai_analysis_text": getattr(result, "ai_analysis_text", ""),
                "model": getattr(result, "model", "Technical"),
                "text": text,
                "timestamp": get_bucharest_time().isoformat()
            }
            _set_cached_analysis(cache_key, analysis)
            return analysis

        except ImportError:
            # Fallback if ticker_analyzer not available
            logger.warning("ticker_analyzer not available, using basic analysis")

            # Basic analysis using yfinance
            import yfinance as yf
            stock = yf.Ticker(ticker)
            info = stock.info
            hist = stock.history(period="5d", interval="1h")

            price = info.get("currentPrice", info.get("regularMarketPrice", 0))
            prev_close = info.get("previousClose", price)
            change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0

            fallback = {
                "ticker": ticker,
                "price": price,
                "change_pct": round(change_pct, 2),
                "asset_type": "crypto" if is_crypto_ticker(ticker) else "stock",
                "trend_1h": "N/A",
                "trend_30m": "N/A",
                "trend_5m": "N/A",
                "rsi": 50,
                "macd_hist": 0,
                "vwap": 0,
                "ema20": 0,
                "distance_from_vwap_pct": 0,
                "distance_from_ema20_pct": 0,
                "atr": 0,
                "best_signal": None,
                "last_signal": last_signal,
                "strategy_notes": strategy_notes,
                "mode_scores": {},
                "quote": {},
                "quote_batch": {},
                "breaking_news": [],
                "news": [],
                "news_signals": {},
                "ai_analysis": None,
                "ai_analysis_text": "",
                "text": f"Basic analysis for {ticker}\nPrice: ${price:.2f}\nChange: {change_pct:.2f}%",
                "timestamp": get_bucharest_time().isoformat()
            }
            _set_cached_analysis(cache_key, fallback)
            return fallback

    except Exception as e:
        logger.error(f"Error analyzing ticker {ticker}: {e}")
        # Graceful fallback instead of 500
        err_payload = {
            "ticker": ticker,
            "error": str(e),
            "text": f"Analysis could not be generated for {ticker}. Internal error: {str(e)}",
            "timestamp": get_bucharest_time().isoformat(),
            "price": 0,
            "change_pct": 0,
            "rsi": 50,
            "macd_hist": 0,
            "best_signal": None,
            "vwap": 0,
            "ema20": 0,
            "distance_from_vwap_pct": 0,
            "distance_from_ema20_pct": 0,
            "atr": 0,
            "mode_scores": {},
            "quote": {},
            "quote_batch": {},
            "breaking_news": [],
            "news": [],
            "news_signals": {},
            "ai_analysis": None,
            "ai_analysis_text": "",
        }
        # A failure is not cached (see the fetch-failure branch above); the
        # old `_set_cached_analysis(cache_key, ...)` here could also raise
        # NameError when the failure happened before `cache_key` was set.
        return err_payload


def _get_watchlist_row(ticker: str) -> Dict[str, Any]:
    symbol = str(ticker or "").upper().strip()
    try:
        state = get_state(include_news=False, include_hotlist=False, lean=True) or {}
        for row in state.get("tickers", []) or []:
            if str((row or {}).get("ticker") or "").upper().strip() == symbol:
                return row if isinstance(row, dict) else {}
    except Exception as e:
        logger.debug(f"V2 watchlist row unavailable for {symbol}: {e}")
    return {}


def _hours_to_event_from_row(row: Dict[str, Any]) -> float:
    try:
        days = int((row or {}).get("earnings_days", -1))
    except Exception:
        days = -1
    if days < 0:
        return 999.0
    when = str((row or {}).get("earnings_when") or "").upper()
    # Use rough intra-day offset so event filters can block near release time.
    offset = 0.0
    if when in {"BMO", "BEFORE_OPEN", "BEFORE MARKET OPEN"}:
        offset = 2.0
    elif when in {"AMC", "AFTER_CLOSE", "AFTER MARKET CLOSE"}:
        offset = 10.0
    return max(0.0, float(days) * 24.0 + offset)


def _v1_comparable_score(v1_analysis: Dict[str, Any]) -> float:
    if not isinstance(v1_analysis, dict):
        return 0.0
    best = v1_analysis.get("best_signal", {}) if isinstance(v1_analysis.get("best_signal"), dict) else {}
    score = best.get("score")
    try:
        if score is not None:
            return max(0.0, min(100.0, float(score)))
    except Exception:
        pass
    current_scores = v1_analysis.get("current_scores", []) if isinstance(v1_analysis.get("current_scores"), list) else []
    if current_scores:
        try:
            top = max(float((x or {}).get("score", 0.0) or 0.0) for x in current_scores if isinstance(x, dict))
            return max(0.0, min(100.0, top))
        except Exception:
            return 0.0
    return 0.0


def analyze_ticker_v2(
    ticker: str,
    force_refresh: bool = False,
    disable_ollama: bool = False,
) -> Dict[str, Any]:
    symbol = str(ticker or "").upper().strip()
    if not symbol:
        raise ValueError("Ticker cannot be empty")

    cfg = get_shared_config()
    strategy_cfg = cfg.strategy_v2 or {}
    strategy_notes = cfg.get_ticker_settings(symbol).get("strategy_notes", "")
    last_signal = _get_last_signal_for_ticker(symbol)

    cache_key = f"{symbol}|v2|mode=V2"
    if not force_refresh:
        cached = _get_cached_analysis(cache_key, ttl=120)
        if cached:
            cached["cached"] = True
            return cached

    row = _get_watchlist_row(symbol)
    asset_class = infer_asset_class(symbol)
    hours_to_event = _hours_to_event_from_row(row)
    ohlc_df = load_cached_ohlc(symbol)
    context = build_context(
        symbol=symbol,
        asset_class=asset_class,
        watch_row=row,
        ohlc_df=ohlc_df,
        hours_to_event=hours_to_event,
    )

    selector = StrategySelector(config=strategy_cfg, perf_cache=StrategyPerformanceCache())
    selected = selector.select(symbol=symbol, asset_class=asset_class, context=context)
    best = selected.get("best_candidate") if isinstance(selected.get("best_candidate"), dict) else None
    research_candidates = selected.get("research_candidates", []) if isinstance(selected.get("research_candidates"), list) else []
    research_best = max(
        (candidate for candidate in research_candidates if isinstance(candidate, dict)),
        key=lambda candidate: float(candidate.get("total_score", 0.0) or 0.0),
        default=None,
    )

    best_signal = None
    if best:
        best_signal = {
            "direction": str(best.get("direction") or "").upper(),
            "mode": str(best.get("strategy_id") or "").upper(),
            "profile": "V2",
            "score": round(float(best.get("total_score", 0.0) or 0.0), 2),
        }

    text = f"V2 analysis for {symbol}: "
    if best:
        text += (
            f"{best.get('direction')} via {best.get('strategy_id')} | "
            f"entry={float(best.get('entry', 0.0)):.6f} "
            f"SL={float(best.get('sl', 0.0)):.6f} TP1={float(best.get('tp1', 0.0)):.6f} "
            f"RR={float(best.get('rr', 0.0)):.2f}"
        )
    else:
        if research_best:
            text += (
                f"Research-only {research_best.get('direction')} via {research_best.get('strategy_id')} | "
                f"entry={float(research_best.get('entry', 0.0)):.6f} "
                f"SL={float(research_best.get('sl', 0.0)):.6f} "
                f"TP1={float(research_best.get('tp1', 0.0)):.6f} "
                f"RR={float(research_best.get('rr', 0.0)):.2f}; Capital execution is blocked pending OOS validation."
            )
        else:
            text += "No V2 setup passed quality/validation gates."

    # Compute indicator fields the frontend expects
    _v2_price = float(context.get("price", 0.0) or 0.0)
    _v2_ema20 = float(context.get("ema20", _v2_price) or _v2_price)
    _v2_dist_ema_pct = abs((_v2_price - _v2_ema20) / _v2_ema20 * 100) if _v2_ema20 else 0.0
    _v2_rsi_raw = context.get("rsi")
    try:
        _v2_rsi = float(_v2_rsi_raw) if _v2_rsi_raw is not None else None
    except (TypeError, ValueError):
        _v2_rsi = None
    if _v2_rsi is not None and pd.isna(_v2_rsi):
        _v2_rsi = None

    out = {
        "ticker": symbol,
        "asset_type": str((row or {}).get("asset_type") or "").lower() or ("crypto" if is_crypto_ticker(symbol) else "stock"),
        "price": _v2_price,
        "change_pct": float(context.get("change_pct", 0.0) or 0.0),
        "rsi": _v2_rsi,
        "macd_hist": float(context.get("macd_hist", 0.0) or 0.0),
        "atr": float(context.get("atr", 0.0) or 0.0),
        # Multi-timeframe trends (from watchlist row computed by trade radar)
        "trend_1d": str((row or {}).get("trend_1d") or "N/A"),
        "trend_4h": str((row or {}).get("trend_4h") or "N/A"),
        "trend_1h": str((row or {}).get("trend_1h") or "N/A"),
        "trend_30m": str((row or {}).get("trend_30m") or "N/A"),
        "trend_5m": str((row or {}).get("trend_5m") or "N/A"),
        # VWAP approximated as current price (V2 has no intraday VWAP pipeline)
        "vwap": _v2_price,
        "ema20": _v2_ema20,
        "distance_from_vwap_pct": 0.0,
        "distance_from_ema20_pct": round(_v2_dist_ema_pct, 4),
        "best_signal": best_signal,
        "last_signal": last_signal,
        # 2026-09-03 audit P1: V2 served mode_scores: {} so the Strategy
        # Scoring Matrix rendered only "-"/N/A. Populate it from the V2
        # selector's own scored candidates (honest V2-native axes).
        # 2026-09-03 field audit: the matrix iterates mode_scores as
        # {mode: {profile: cells}} — flat strategy keys rendered as garbage
        # "profiles". Nest candidates under the single V2 mode.
        "mode_scores": {
            "V2": {
                str(cand.get("strategy_id") or "unknown").upper(): {
                    "best_score": round(float(cand.get("total_score", 0.0) or 0.0), 2),
                    "best_direction": str(cand.get("direction") or "-").upper(),
                    "long_score": round(float(cand.get("total_score", 0.0) or 0.0), 2)
                    if str(cand.get("direction") or "").upper() == "LONG" else 0,
                    "short_score": round(float(cand.get("total_score", 0.0) or 0.0), 2)
                    if str(cand.get("direction") or "").upper() == "SHORT" else 0,
                    "long_valid": str(cand.get("direction") or "").upper() == "LONG"
                    and float(cand.get("total_score", 0.0) or 0.0) > 0,
                    "short_valid": str(cand.get("direction") or "").upper() == "SHORT"
                    and float(cand.get("total_score", 0.0) or 0.0) > 0,
                    "details": {"source": "V2 selector candidate"},
                }
                for cand in [best] + [c for c in research_candidates if isinstance(c, dict) and c is not best]
                if isinstance(cand, dict)
            }
        },
        "current_scores": [],
        "strategy_notes": strategy_notes,
        "fundamentals": {},
        "quote": {},
        "quote_batch": {},
        "breaking_news": [],
        "news": [],
        "news_signals": {},
        "ai_analysis": None,
        "ai_analysis_text": "",
        "signal_engine": "V2",
        "v2": {
            "asset_class": asset_class,
            "regime": selected.get("regime", {}),
            "best_candidate": best,
            "best_long": selected.get("best_long"),
            "best_short": selected.get("best_short"),
            "research_candidates": research_candidates,
            "research_best_candidate": research_best,
            "research_candidates_considered": selected.get("research_candidates_considered", 0),
            "reverse_consideration": selected.get("reverse_consideration", {}),
            "rejected": selected.get("rejected", []),
            "candidates_considered": selected.get("candidates_considered", 0),
        },
        "text": text,
        "timestamp": get_bucharest_time().isoformat(),
    }

    # ------------------------------------------------------------------
    # Ollama AI Analysis (two-pass, same approach as V1)
    # ------------------------------------------------------------------
    if not disable_ollama and cfg.config.get("news", {}).get("use_ollama", True):
        try:
            from core.ollama_client import ollama

            if ollama.check_availability():
                ai_start = time.time()
                price = out["price"]
                context_payload = {
                    "ticker": symbol,
                    "asset_type": out["asset_type"],
                    "price": price,
                    "change_pct": out["change_pct"],
                    "signal_engine": "V2",
                    "technical": {
                        "trend_1d": out["trend_1d"],
                        "trend_4h": out["trend_4h"],
                        "trend_1h": out["trend_1h"],
                        "trend_30m": out["trend_30m"],
                        "trend_5m": out["trend_5m"],
                        "rsi": out["rsi"],
                        "macd_hist": out["macd_hist"],
                        "atr": out["atr"],
                        "ema20": out["ema20"],
                        "adx": float(context.get("adx", 0.0) or 0.0),
                    },
                    "v2_strategy": {
                        "best_candidate": out["best_signal"],
                        "research_best_candidate": research_best,
                        "regime": (out.get("v2") or {}).get("regime", {}),
                    },
                }

                # Single-pass Ollama prompt (replaces 2-pass for speed)
                single_system = "You are a financial analysis engine. Output ONLY valid JSON, no commentary."
                single_prompt = (
                    "Analyze the market data below and return a single JSON object.\n"
                    "Use 'unknown' for any missing values. Do not invent numbers.\n\n"
                    "Required JSON schema (return exactly this structure):\n"
                    "{\n"
                    "  \"sentiment\": \"bullish|bearish|neutral\",\n"
                    "  \"earnings_play\": \"PLAY_PRE|PLAY_POST|WAIT|AVOID\",\n"
                    "  \"confidence\": 0.0,\n"
                    "  \"key_drivers\": [],\n"
                    "  \"risks\": [],\n"
                    "  \"scenario\": {\"bull_case\": \"\", \"base_case\": \"\", \"bear_case\": \"\"},\n"
                    "  \"trade_plan\": {\"direction\": \"LONG|SHORT|NONE\", \"entry_condition\": \"\", \"invalidations\": \"\", \"tp_logic\": \"ATR|levels|unknown\", \"notes\": \"\"}\n"
                    "}\n\n"
                    f"MARKET_DATA:\n{json.dumps(context_payload, ensure_ascii=False)}\n"
                )
                # Bounded call (was the 120 s provider default); a failed or
                # timed-out call is marked ai_status "unavailable", not served
                # as if the AI had answered.
                from core.ticker_analyzer import (
                    analysis_ai_timeout_seconds,
                    run_structured_analysis_call,
                )
                structured, ai_model_label = run_structured_analysis_call(
                    single_prompt,
                    single_system,
                    context_payload,
                    timeout_seconds=analysis_ai_timeout_seconds(cfg.config),
                    client=ollama,
                )
                out["ai_analysis"] = structured
                out["ai_analysis_text"] = (
                    f"sentiment={structured.get('sentiment')} | "
                    f"play={structured.get('earnings_play')} | "
                    f"confidence={structured.get('confidence')}"
                )
                out["model"] = ai_model_label
                if structured.get("ai_status") == "unavailable":
                    logger.warning(
                        "V2 AI analysis unavailable for %s after %.2fs (%s): rule-based summary returned",
                        symbol, time.time() - ai_start, structured.get("ai_error_detail"),
                    )
                else:
                    logger.info(f"V2 Ollama single-pass AI analysis for {symbol} finished in {time.time() - ai_start:.2f}s")
        except Exception as e:
            logger.error(f"V2 Ollama AI error for {symbol}: {e}")

    _set_cached_analysis(cache_key, out)
    return out


def analyze_ticker_mode(
    ticker: str,
    include_heavy: bool = True,
    disable_ollama: bool = False,
    force_refresh: bool = False,
    mode: Optional[str] = None,
) -> Dict[str, Any]:
    cfg = get_shared_config()
    configured_mode = str((cfg.strategy_v2 or {}).get("signal_mode") or "V1").upper()
    selected_mode = str(mode or configured_mode or "V1").upper()

    if selected_mode == "V2":
        result = analyze_ticker_v2(ticker=ticker, force_refresh=force_refresh, disable_ollama=disable_ollama)
        result["selected_mode"] = "V2"
        result["mode_decision"] = {"selected": "V2", "why": "Signal mode forced to V2"}
        return result

    if selected_mode == "BOTH_AUTO_BEST":
        v1 = analyze_ticker(ticker=ticker, include_heavy=include_heavy, disable_ollama=disable_ollama, force_refresh=force_refresh)
        v2 = analyze_ticker_v2(ticker=ticker, force_refresh=force_refresh, disable_ollama=disable_ollama)
        v1_score = _v1_comparable_score(v1)
        v2_best = (((v2 or {}).get("v2") or {}).get("best_candidate") or {}) if isinstance(v2, dict) else {}
        v2_score = float(v2_best.get("total_score", 0.0) or 0.0)

        if v2_score > v1_score:
            winner = dict(v2)
            winner["selected_mode"] = "V2"
            winner["mode_decision"] = {
                "selected": "V2",
                "v1_score": round(v1_score, 2),
                "v2_score": round(v2_score, 2),
                "why": "V2 score higher than V1 comparable score",
                "chosen_over_other": "V2 selected in BOTH_AUTO_BEST comparator",
            }
            winner["v1_shadow"] = {
                "best_signal": v1.get("best_signal"),
                "score": round(v1_score, 2),
            }
            # Carry V1 rich data (AI, fundamentals, news, quotes) into V2 winner
            for _key in (
                "ai_analysis", "ai_analysis_text", "model",
                "fundamentals", "news", "news_signals",
                "breaking_news", "quote", "quote_batch",
                "strategy_comparison", "current_scores",
            ):
                if not winner.get(_key) and v1.get(_key):
                    winner[_key] = v1[_key]
            return winner

        winner = dict(v1)
        winner["selected_mode"] = "V1"
        winner["mode_decision"] = {
            "selected": "V1",
            "v1_score": round(v1_score, 2),
            "v2_score": round(v2_score, 2),
            "why": "V1 comparable score >= V2 score",
            "chosen_over_other": "V1 selected in BOTH_AUTO_BEST comparator",
        }
        winner["v2_shadow"] = {
            "best_candidate": v2_best,
            "score": round(v2_score, 2),
            "reverse_consideration": ((v2 or {}).get("v2") or {}).get("reverse_consideration", {}),
        }
        return winner

    result = analyze_ticker(
        ticker=ticker,
        include_heavy=include_heavy,
        disable_ollama=disable_ollama,
        force_refresh=force_refresh,
    )
    result["selected_mode"] = "V1"
    result["mode_decision"] = {"selected": "V1", "why": "Signal mode default/forced to V1"}
    return result


def get_strategy_config() -> Dict[str, Any]:
    cfg = get_shared_config()
    return cfg.strategy_v2


def save_strategy_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    cfg = get_shared_config()
    current = dict(cfg.config.get("strategy_v2", {}) or {})
    merged = deep_merge(current, updates or {})
    cfg.config["strategy_v2"] = merged
    if cfg.save() is False:
        # The write failed (ConfigManager records it instead of raising):
        # do not answer "saved".
        raise RuntimeError(
            "strategy_v2 settings were not saved: "
            + str(getattr(cfg, "last_save_error", "") or "config.json could not be written")
        )
    cfg.reload_if_changed()
    return cfg.strategy_v2


def get_strategy_performance(symbol: Optional[str] = None) -> Dict[str, Any]:
    cache = StrategyPerformanceCache()
    if symbol:
        entries = cache.get_symbol_entries(symbol)
    else:
        payload = cache.load()
        entries = payload.get("entries", {}) if isinstance(payload.get("entries"), dict) else {}
    rows = []
    for key, value in entries.items():
        if not isinstance(value, dict):
            continue
        parts = str(key).split("|")
        row = {
            "key": key,
            "symbol": parts[0] if len(parts) > 0 else "",
            "asset_class": parts[1] if len(parts) > 1 else "",
            "strategy_id": parts[2] if len(parts) > 2 else "",
            "direction": parts[3] if len(parts) > 3 else "",
            "win_rate": float(value.get("win_rate", 0.0) or 0.0),
            "profit_factor": float(value["profit_factor"]) if value.get("profit_factor") is not None else None,
            "expectancy_R": float(value.get("expectancy_R", 0.0) or 0.0),
            "avg_R": float(value.get("avg_R", 0.0) or 0.0),
            "max_drawdown": float(value.get("max_drawdown", 0.0) or 0.0),
            "sample_size": int(value.get("sample_size", 0) or 0),
            "last_run": value.get("last_run"),
            "costs_used": value.get("costs_used", {}),
            "walk_forward": value.get("walk_forward", {}),
            "validation_status": value.get("validation_status") or "unverified",
            "validation_method": value.get("validation_method") or None,
            "validation_reason": value.get("validation_reason") or None,
            "validation_as_of": value.get("validation_as_of"),
        }
        rows.append(row)
    rows.sort(key=lambda x: (x["symbol"], x["strategy_id"], x["direction"]))
    return {"status": "ok", "count": len(rows), "entries": rows}


def _backtest_min_sample(config: Any = None) -> int:
    """Closed trades needed before backtest metrics count as evidence."""
    try:
        signal_cfg = getattr(config or get_shared_config(), "signal", {}) or {}
        value = int(signal_cfg.get("backtest_min_trades_for_validation") or _BACKTEST_MIN_SAMPLE_DEFAULT)
        return max(1, value)
    except Exception:
        return _BACKTEST_MIN_SAMPLE_DEFAULT


def _backtest_evidence(closed_trades: Any, minimum_sample: int) -> Dict[str, Any]:
    """Same shape the faithful replay reports, for every other mode."""
    try:
        sample = max(0, int(closed_trades or 0))
    except (TypeError, ValueError):
        sample = 0
    sufficient = sample >= minimum_sample
    return {
        "sample_size": sample,
        "minimum_sample": minimum_sample,
        "sufficient": sufficient,
        "status": "validated_sample_size" if sufficient else "insufficient_sample",
    }


def _backtest_data_coverage(df: Any, requested_days: Any) -> Dict[str, Any]:
    """What the fetched candles really cover, versus what was asked for.

    The crypto fetch is capped (30 pages of 1-minute candles, about 20.8 days)
    and Yahoo limits intraday history, so "90 days" could silently mean three
    weeks. The result must say which span it describes.
    """
    coverage: Dict[str, Any] = {"requested_days": requested_days, "bars": 0, "truncated": False}
    try:
        if df is None or len(df) == 0:
            return coverage
        coverage["bars"] = int(len(df))
        if isinstance(df.index, pd.DatetimeIndex):
            first, last = df.index[0], df.index[-1]
        elif "timestamp" in getattr(df, "columns", []):
            stamps = pd.to_datetime(df["timestamp"])
            first, last = stamps.iloc[0], stamps.iloc[-1]
        else:
            return coverage
        span_days = (last - first).total_seconds() / 86_400
        coverage.update({
            "data_start": first.isoformat(),
            "data_end": last.isoformat(),
            "actual_span_days": round(span_days, 2),
        })
        requested = float(requested_days or 0)
        if requested > 0 and (requested - span_days) > max(3.0, 0.15 * requested):
            coverage["truncated"] = True
            coverage["warning"] = (
                f"Requested {int(requested)} days, but the data source returned only "
                f"{span_days:.1f} days ({coverage['bars']} bars). Every figure below "
                f"describes that shorter span."
            )
    except Exception as exc:
        logger.debug("Backtest data coverage unavailable: %s", exc)
    return coverage


def _validate_backtest_request(
    days: Any,
    custom_tp_pct: Any = None,
    custom_sl_pct: Any = None,
) -> Tuple[Optional[str], Optional[int], Optional[float], Optional[float]]:
    """Return (error, days, tp, sl). Values the form cannot mean are refused
    with a message instead of being simulated (a zero or negative stop, a
    non-finite percentage, a negative period)."""
    import math

    try:
        days_int = int(days)
    except (TypeError, ValueError):
        return "Days must be a whole number.", None, None, None
    if days_int < 1 or days_int > _BACKTEST_MAX_DAYS:
        return f"Days must be between 1 and {_BACKTEST_MAX_DAYS}.", None, None, None

    def pct(value: Any, label: str, upper: float) -> Tuple[Optional[str], Optional[float]]:
        if value is None or value == "":
            return None, None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return f"{label} must be a number.", None
        if not math.isfinite(number):
            return f"{label} must be a finite number.", None
        if number <= 0:
            return f"{label} must be greater than 0%.", None
        if number > upper:
            return f"{label} must be at most {upper:g}%.", None
        return None, number

    error, tp = pct(custom_tp_pct, "Custom take profit", 500.0)
    if error:
        return error, None, None, None
    # A stop 100% away can never be hit on a long; cap well below that.
    error, sl = pct(custom_sl_pct, "Custom stop loss", 95.0)
    if error:
        return error, None, None, None
    return None, days_int, tp, sl


class _VariantReplayConfig:
    """Read-only view of the shared config whose ``signal`` carries the
    STRICT/CUSTOM variant overrides. Everything else is delegated."""

    def __init__(self, base: Any, signal: Dict[str, Any]):
        self._base = base
        self._signal = signal

    @property
    def signal(self) -> Dict[str, Any]:
        return self._signal

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


def _variant_replay_config(config: Any, variant_mode: Any) -> Tuple[Any, bool]:
    """Return (config for the faithful replay, whether a variant was applied).

    Mirrors ``MarketEngine._get_variant_signal_cfg``: CUSTOM layers
    ``signal.variant_custom`` over the signal settings, anything else layers
    ``signal.variant_strict``.
    """
    try:
        base_signal = dict(getattr(config, "signal", {}) or {})
        mode = str(variant_mode or base_signal.get("variant_mode_default") or "STRICT").upper().strip()
        mode = "CUSTOM" if mode == "CUSTOM" else "STRICT"
        override = base_signal.get("variant_custom" if mode == "CUSTOM" else "variant_strict") or {}
        if not isinstance(override, dict) or not override:
            return config, False
        merged = deep_merge(base_signal, override)
        merged["variant_mode"] = mode
        return _VariantReplayConfig(config, merged), True
    except Exception as exc:
        logger.warning("Variant replay config unavailable (%s); using base signal settings", exc)
        return config, False


def _get_strategy_comparison(
    ticker: str,
    days: int = 7,
    custom_tp_pct: float = None,
    custom_sl_pct: float = None,
    variant_mode: str = None,
    meta: Optional[Dict[str, Any]] = None,
) -> List[Dict]:
    """
    Get backtest results for all 12 combinations on one shared data snapshot.

    Args:
        ticker: The ticker symbol
        days: Backtest period (default 7 days)
        custom_tp_pct: Custom Take Profit % (override)
        custom_sl_pct: Custom Stop Loss % (override)
        meta: optional dict filled with `data_coverage`, `warnings` and, when
            nothing could be run, `error` (why the list is empty)

    Returns:
        List of strategy results sorted by win rate
    """
    if meta is None:
        meta = {}
    try:
        from core.quick_backtester import QuickBacktester
        config = get_shared_config()
        
        # [NEW] Auto-detect custom TP/SL from ticker settings if not provided
        if custom_tp_pct is None or custom_sl_pct is None:
            t_settings = config.get_ticker_settings(ticker)
            if custom_tp_pct is None:
                custom_tp_pct = t_settings.get("tp_pct")
            if custom_sl_pct is None:
                custom_sl_pct = t_settings.get("sl_pct")
            
            if custom_tp_pct is not None or custom_sl_pct is not None:
                logger.info(f"Using custom TP/SL for {ticker} comparison: {custom_tp_pct}% / {custom_sl_pct}%")

        backtester = QuickBacktester(config)
        
        is_crypto = is_crypto_ticker(ticker)
        comparison_df, data_warnings = backtester.fetch_backtest_data(
            ticker, is_crypto=is_crypto, days=days,
        )
        if comparison_df is None or comparison_df.empty:
            logger.warning("Strategy comparison has no data for %s: %s", ticker, data_warnings)
            meta["warnings"] = list(data_warnings or [])
            meta["error"] = (
                f"No historical price data was returned for {ticker}. "
                "Check the symbol (e.g. AAPL, BTC-USD) or try again later."
            )
            return []

        coverage = _backtest_data_coverage(comparison_df, days)
        meta["data_coverage"] = coverage
        meta["warnings"] = list(data_warnings or [])
        if coverage.get("warning"):
            meta["warnings"].append(coverage["warning"])
        min_sample = _backtest_min_sample(config)

        risk_modes = ["PRO", "STRONG", "MEDIUM", "RELAX"]
        trade_modes = ["PULLBACK", "BREAKOUT", "CONTRA"]
        variant_map = {"PRO": "P", "STRONG": "A", "MEDIUM": "B", "RELAX": "C"}

        results = []

        for risk_mode in risk_modes:
            for trade_mode in trade_modes:
                try:
                    result = backtester.quick_backtest(
                        ticker=ticker,
                        risk_mode=risk_mode,
                        is_crypto=is_crypto,
                        days=days,
                        trade_mode=trade_mode,
                        custom_tp_pct=custom_tp_pct,
                        custom_sl_pct=custom_sl_pct,
                        variant_mode=variant_mode,
                        df=comparison_df,
                    )
                    
                    results.append({
                        "risk_mode": risk_mode,
                        "variant": variant_map.get(risk_mode, "?"),
                        "trade_mode": trade_mode,
                        "total_signals": result.get("total_signals", 0),
                        "wins": result.get("wins", 0),
                        "losses": result.get("losses", 0),
                        "win_rate": round(result.get("win_rate", 0), 1),
                        "win_rate_trades": round(result.get("win_rate_trades", 0), 1),
                        "trade_count": result.get("trade_count", 0),
                        "profit_factor": round(result["profit_factor"], 2) if result.get("profit_factor") is not None else None,
                        "max_drawdown_pct": round(result.get("max_drawdown_pct", 0), 1),
                        "expectancy_r": result.get("expectancy_r", 0),
                        "payoff_ratio": result.get("payoff_ratio", 0),
                        "avg_win_r": result.get("avg_win_r", 0),
                        "avg_loss_r": result.get("avg_loss_r", 0),
                        "warnings": result.get("warnings", []),
                        "signals": result.get("signals", []),
                        "trades": result.get("signals", []), # Alias for frontend compat
                        # What was actually simulated for this row, and how
                        # much it is worth as evidence.
                        "closed_trades": result.get("closed_trades", len(result.get("signals", []) or [])),
                        "open_trades": result.get("open_trades", 0),
                        "evidence": _backtest_evidence(
                            result.get("closed_trades", len(result.get("signals", []) or [])),
                            min_sample,
                        ),
                        "effective_exit_mode": result.get("effective_exit_mode"),
                        "effective_tp_pct": result.get("effective_tp_pct"),
                        "effective_sl_pct": result.get("effective_sl_pct"),
                        "effective_atr_multiplier_tp": result.get("effective_atr_multiplier_tp"),
                        "effective_atr_multiplier_sl": result.get("effective_atr_multiplier_sl"),
                        "custom_exit_applied": bool(result.get("custom_exit_applied", False)),
                        "max_drawdown_r": result.get("max_drawdown_r"),
                        "max_drawdown_basis": result.get("max_drawdown_basis"),
                        "insufficient_data": bool(result.get("insufficient_data", False)),
                        "simulation_scope": result.get("simulation_scope"),
                        **({"error": str(result["error"])} if result.get("error") else {}),
                    })
                except Exception as e:
                    logger.warning(f"Strategy {risk_mode}/{trade_mode} failed for {ticker}: {e}")
                    results.append({
                        "risk_mode": risk_mode,
                        "variant": variant_map.get(risk_mode, "?"),
                        "trade_mode": trade_mode,
                        "total_signals": 0,
                        "wins": 0,
                        "losses": 0,
                        "win_rate": 0,
                        "profit_factor": None,
                        "max_drawdown": 0,
                        "max_drawdown_pct": None,
                        "expectancy_r": None,
                        "evidence": _backtest_evidence(0, min_sample),
                        "error": str(e),
                    })

        # Rows with a usable sample first, then by win rate and by total
        # signals (descending). A 1-trade "100%" row must not lead the list.
        results.sort(
            key=lambda x: (
                bool((x.get("evidence") or {}).get("sufficient")),
                x["win_rate"],
                x["total_signals"],
            ),
            reverse=True,
        )

        failed = [row for row in results if row.get("error")]
        if failed and len(failed) == len(results):
            meta["error"] = f"The backtest failed for {ticker}: {failed[0]['error']}"
        elif results and all(row.get("insufficient_data") for row in results):
            meta["error"] = (
                f"Not enough price history for {ticker}: {coverage.get('bars', 0)} bars "
                "were returned and at least 100 are needed."
            )

        return results
    except Exception as e:
        logger.error(f"Error comparing strategies for {ticker}: {e}")
        meta["error"] = f"The strategy comparison failed for {ticker}: {e}"
        return []


def _summarize_bt_result(result: Dict) -> Dict:
    """Reduce backtest result to summary fields for reporting."""
    return {
        "wins": result.get("wins", 0),
        "losses": result.get("losses", 0),
        "win_rate": round(result.get("win_rate", 0), 2),
        "win_rate_trades": round(result.get("win_rate_trades", 0), 2),
        "trade_count": result.get("trade_count", 0),
        # `closed_trades` feeds the confidence term of _composite_score. A
        # missing value must fall back to the trade count, not to 0 (0 zeroed
        # every score, which made the walk-forward selection meaningless).
        "closed_trades": result.get("closed_trades", result.get("trade_count", 0)) or 0,
        "open_trades": result.get("open_trades", 0),
        "total_signals": result.get("total_signals", 0),
        "profit_factor": round(result["profit_factor"], 2) if result.get("profit_factor") is not None else None,
        "max_drawdown_pct": round(result.get("max_drawdown_pct", 0), 2),
        "expectancy_r": round(result.get("expectancy_r", 0), 4),
        "payoff_ratio": round(result["payoff_ratio"], 2) if result.get("payoff_ratio") is not None else None,
        "avg_win_r": round(result.get("avg_win_r", 0), 4),
        "avg_loss_r": round(result.get("avg_loss_r", 0), 4),
        "warnings": result.get("warnings", []),
    }


def _composite_score(summary: Dict) -> float:
    """
    Composite score used for ranking/selection (train -> pick best, test -> evaluate).

    Notes:
    - expectancy_r is in R/trade (typically small), so it gets a larger weight.
    - win_rate_trades is in percent (0..100).
    - profit_factor and max_drawdown_pct are weak regularizers.
    - confidence down-weights tiny samples to reduce overfitting.
    """
    try:
        wr = float(summary.get("win_rate_trades", summary.get("win_rate", 0)) or 0.0)
        exp_r = float(summary.get("expectancy_r", 0) or 0.0)
        pf = float(summary.get("profit_factor", 0) or 0.0)
        dd = float(summary.get("max_drawdown_pct", 0) or 0.0)
        trades = int(summary.get("closed_trades", summary.get("trade_count", 0)) or 0)

        # Confidence: sqrt scaling, caps at 1.0 around ~30 closed trades.
        if trades <= 0:
            confidence = 0.0
        else:
            confidence = min(1.0, (trades / 30.0)) ** 0.5

        pf_clip = min(max(pf, 0.0), 5.0)
        raw = (exp_r * 80.0) + (wr * 0.6) + ((pf_clip - 1.0) * 10.0) - (dd * 0.3)
        return float(raw) * float(confidence)
    except Exception:
        return 0.0


def run_walk_forward_backtest(
    ticker: str,
    days: int = 60,
    train_days: int = 20,
    test_days: int = 10,
    risk_modes: Optional[List[str]] = None,
    trade_modes: Optional[List[str]] = None,
    ui_interval: str = "5min",
    custom_tp_pct: Optional[float] = None,
    custom_sl_pct: Optional[float] = None,
    max_windows: int = 6,
    variant_mode: str = "STRICT"
) -> Dict:
    """
    Walk-forward backtest: select best strategy on each train window,
    then evaluate on subsequent test window.
    """
    try:
        from core.quick_backtester import QuickBacktester
        from core.backtester import Backtester

        ticker = _normalize_input_ticker(ticker)
        if not ticker:
            return {"status": "error", "error": "Please enter a ticker symbol."}
        error, days, custom_tp_pct, custom_sl_pct = _validate_backtest_request(
            days if days is not None else 60, custom_tp_pct, custom_sl_pct
        )
        if error:
            return {"status": "error", "error": error}
        try:
            train_days = int(train_days if train_days is not None else 20)
            test_days = int(test_days if test_days is not None else 10)
            max_windows = int(max_windows if max_windows is not None else 6)
        except (TypeError, ValueError):
            return {"status": "error", "error": "Train days, test days and max windows must be whole numbers."}
        if train_days < 1 or test_days < 1:
            return {"status": "error", "error": "Train days and test days must each be at least 1."}
        if train_days + test_days > days:
            return {
                "status": "error",
                "error": (
                    f"The period ({days} days) is shorter than one train + test window "
                    f"({train_days} + {test_days} days)."
                ),
            }
        max_windows = max(1, min(max_windows, 24))

        config = get_shared_config()
        backtester = QuickBacktester(config)
        min_bars = int(getattr(Backtester, "MIN_BARS", 100))
        min_sample = _backtest_min_sample(config)

        is_crypto = is_crypto_ticker(ticker)

        allowed_risk = ["PRO", "STRONG", "MEDIUM", "RELAX"]
        allowed_trade = ["PULLBACK", "BREAKOUT", "CONTRA"]
        if risk_modes:
            risk_modes = [m for m in (str(x).upper().strip() for x in risk_modes) if m in allowed_risk]
        if trade_modes:
            trade_modes = [m for m in (str(x).upper().strip() for x in trade_modes) if m in allowed_trade]
        if not risk_modes:
            risk_modes = list(allowed_risk)
        if not trade_modes:
            trade_modes = list(allowed_trade)

        df, warnings = backtester.fetch_backtest_data(ticker, is_crypto, days, ui_interval)
        if df is None:
            return {
                "status": "error",
                "error": (
                    f"No historical price data was returned for {ticker}. "
                    "Check the symbol (e.g. AAPL, BTC-USD) or try again later."
                ),
                "warnings": warnings
            }
        warnings = list(warnings or [])

        df = df.copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp")

        start_ts = df["timestamp"].min()
        end_ts = df["timestamp"].max()
        coverage = _backtest_data_coverage(df, days)
        if coverage.get("warning"):
            warnings.append(coverage["warning"])

        train_td = pd.Timedelta(days=train_days)
        test_td = pd.Timedelta(days=test_days)
        step_td = test_td

        windows = []
        skipped = 0
        skipped_reasons: Dict[str, int] = {}
        cursor = start_ts

        def _skip(reason: str) -> None:
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1

        while cursor + train_td + test_td <= end_ts and len(windows) < max_windows:
            train_start = cursor
            train_end = cursor + train_td
            test_start = train_end
            test_end = train_end + test_td

            train_df = df[(df["timestamp"] >= train_start) & (df["timestamp"] < train_end)]
            test_df = df[(df["timestamp"] >= test_start) & (df["timestamp"] < test_end)]

            # The engine needs min_bars per slice (indicator warm-up). The old
            # 80/40 limits let slices through that it then refused silently,
            # so those windows were reported as "0 trades, 0% win rate".
            if len(train_df) < min_bars or len(test_df) < min_bars:
                skipped += 1
                _skip("too_few_bars")
                cursor += step_td
                continue

            best = None
            best_score = None

            for risk_mode in risk_modes:
                for trade_mode in trade_modes:
                    res = backtester.quick_backtest(
                        ticker=ticker,
                        risk_mode=risk_mode,
                        is_crypto=is_crypto,
                        days=train_days,
                        trade_mode=trade_mode,
                        df=train_df,
                        ui_interval=ui_interval,
                        custom_tp_pct=custom_tp_pct,
                        custom_sl_pct=custom_sl_pct,
                        variant_mode=variant_mode
                    )
                    train_summary = _summarize_bt_result(res)
                    # A combination with no closed train trade has no evidence
                    # to be selected on. (Every score used to be 0 because the
                    # closed-trade count was never passed through, so the first
                    # combination in the list was always "selected".)
                    if int(train_summary.get("closed_trades") or 0) <= 0:
                        continue
                    score = _composite_score(train_summary)
                    if best is None or score > best_score:
                        best = {
                            "risk_mode": risk_mode,
                            "trade_mode": trade_mode,
                            "score": round(score, 4),
                            "train": train_summary
                        }
                        best_score = score

            if not best:
                skipped += 1
                _skip("no_train_trades")
                cursor += step_td
                continue

            test_res = backtester.quick_backtest(
                ticker=ticker,
                risk_mode=best["risk_mode"],
                is_crypto=is_crypto,
                days=test_days,
                trade_mode=best["trade_mode"],
                df=test_df,
                ui_interval=ui_interval,
                custom_tp_pct=custom_tp_pct,
                custom_sl_pct=custom_sl_pct,
                variant_mode=variant_mode
            )
            test_summary = _summarize_bt_result(test_res)
            test_score = _composite_score(test_summary)

            windows.append({
                "train_start": train_start.isoformat(),
                "train_end": train_end.isoformat(),
                "test_start": test_start.isoformat(),
                "test_end": test_end.isoformat(),
                "best_strategy": {
                    "risk_mode": best["risk_mode"],
                    "trade_mode": best["trade_mode"],
                    "train_score": best["score"],
                    "test_score": round(test_score, 4)
                },
                "train_summary": best["train"],
                "test_summary": test_summary
            })

            cursor += step_td

        # Aggregate summary
        win_total = 0
        loss_total = 0
        trades_total = 0
        exp_sum = 0.0
        train_score_sum = 0.0
        test_score_sum = 0.0
        strat_counts: Dict[str, int] = {}

        weighted_r_sum = 0.0
        for w in windows:
            test = w["test_summary"]
            win_total += test.get("wins", 0)
            loss_total += test.get("losses", 0)
            window_trades = test.get("closed_trades", 0) or test.get("trade_count", 0) or 0
            trades_total += window_trades
            exp_sum += test.get("expectancy_r", 0) or 0
            weighted_r_sum += (test.get("expectancy_r", 0) or 0) * window_trades
            train_score_sum += w["best_strategy"].get("train_score", 0) or 0
            test_score_sum += w["best_strategy"].get("test_score", 0) or 0

            key = f"{w['best_strategy']['risk_mode']}|{w['best_strategy']['trade_mode']}"
            strat_counts[key] = strat_counts.get(key, 0) + 1

        total_trades = win_total + loss_total
        # No windows / no out-of-sample trades means there is nothing to
        # report: null, not a confident-looking 0%.
        win_rate_trades = round((win_total / total_trades) * 100, 2) if total_trades > 0 else None
        avg_expectancy = round(exp_sum / len(windows), 4) if windows else None
        # Per-trade expectancy across all test windows. The per-window mean
        # above gives a 1-trade window the same weight as a 40-trade window.
        expectancy_per_trade = round(weighted_r_sum / trades_total, 4) if trades_total > 0 else None
        avg_train_score = round(train_score_sum / len(windows), 4) if windows else None
        avg_test_score = round(test_score_sum / len(windows), 4) if windows else None
        evidence = _backtest_evidence(total_trades, min_sample)

        status = "ok"
        reason = ""
        if not windows:
            status = "insufficient_data"
            span = coverage.get("actual_span_days")
            needed = train_days + test_days
            if span is not None and span < needed:
                reason = (
                    f"Only {span:.1f} days of price history are available for {ticker}, but one "
                    f"walk-forward window needs {needed} days ({train_days} train + {test_days} test). "
                    "Shorten the train/test days."
                )
            elif skipped_reasons.get("too_few_bars"):
                reason = (
                    f"Every window had fewer than {min_bars} price bars in its train or test part. "
                    "Use longer train/test days or a shorter period (finer candles)."
                )
            elif skipped_reasons.get("no_train_trades"):
                reason = (
                    "No strategy produced a single closed trade in any training window, "
                    "so nothing could be selected or tested."
                )
            else:
                reason = "No complete train + test window fits in the available price history."
        elif not evidence["sufficient"]:
            warnings.append(
                f"Insufficient evidence: {total_trades} closed out-of-sample trades; at least "
                f"{min_sample} are needed before these figures mean anything."
            )

        return {
            "status": status,
            "reason": reason,
            "mode": "walk_forward",
            "ticker": ticker,
            "days": days,
            "train_days": train_days,
            "test_days": test_days,
            "risk_modes": risk_modes,
            "trade_modes": trade_modes,
            "variant_mode": (variant_mode or "STRICT").upper(),
            "windows": windows,
            "data_coverage": coverage,
            "evidence": evidence,
            "summary": {
                "windows": len(windows),
                "skipped_windows": skipped,
                "skipped_reasons": skipped_reasons,
                "total_trades": total_trades,
                "win_rate_trades": win_rate_trades,
                "expectancy_r": expectancy_per_trade,
                "avg_expectancy_r": avg_expectancy,
                "avg_train_score": avg_train_score,
                "avg_test_score": avg_test_score,
                "strategy_counts": strat_counts
            },
            "timestamp": get_bucharest_time().isoformat(),
            "warnings": warnings
        }

    except Exception as e:
        logger.error(f"Walk-forward backtest error for {ticker}: {e}")
        return {"status": "error", "error": str(e)}


def _extract_current_scores(result) -> List[Dict]:
    """
    Extract current scores for all risk profiles and trade modes.
    Shows how close each combination is to generating a signal.
    
    Args:
        result: TickerAnalysis object
        
    Returns:
        List of score dictionaries sorted by score (highest first)
    """
    try:
        from core.scoring_system import SCORE_THRESHOLDS
        
        mode_scores = getattr(result, "mode_scores", {})
        if not mode_scores:
            return []
        
        variant_map = {"STRONG": "A", "MEDIUM": "B", "RELAX": "C"}
        scores_list = []
        
        # Structure is: mode_scores[TRADE_MODE][RISK_PROFILE] = ModeScore
        for trade_mode, profiles in mode_scores.items():
            for risk_mode, mode_score in profiles.items():
                # SCORE_THRESHOLDS is a simple dict: {"STRONG": 88, "MEDIUM": 72, "RELAX": 60}
                threshold = SCORE_THRESHOLDS.get(risk_mode, 60)
                variant = variant_map.get(risk_mode, "?")
                
                if hasattr(mode_score, 'best_score'):
                    score = mode_score.best_score
                    direction = mode_score.best_direction
                    long_score = mode_score.long_score
                    short_score = mode_score.short_score
                    
                    # Calculate how much is needed for signal
                    needed = max(0, threshold - score)
                    progress_pct = min(100, (score / threshold) * 100) if threshold > 0 else 0
                    
                    scores_list.append({
                        "risk_mode": risk_mode,
                        "variant": variant,
                        "trade_mode": trade_mode,
                        "score": score,
                        "threshold": threshold,
                        "needed": needed,
                        "progress_pct": round(progress_pct, 1),
                        "direction": direction,
                        "long_score": long_score,
                        "short_score": short_score,
                        "is_valid": score >= threshold,
                    })
        
        # Sort by score (highest first)
        scores_list.sort(key=lambda x: x["score"], reverse=True)
        
        return scores_list
        
    except Exception as e:
        logger.error(f"Error extracting current scores: {e}")
        return []


def _get_last_signal_for_ticker(ticker: str) -> Optional[Dict]:
    """Get the last signal (open or closed) for a specific ticker."""
    try:
        signals_data = load_signals_payload({"open": {}, "closed": []})
        if not isinstance(signals_data, dict):
            return None

        # Check open signals first
        open_signals = signals_data.get("open", {})
        for sig_id, sig in open_signals.items():
            if sig.get("ticker") == ticker:
                return {
                    "status": sig.get("status", "Pending"),
                    "action": sig.get("action", sig.get("type", "")),
                    "variant": sig.get("variant", ""),
                    "variant_mode": sig.get("variant_mode", "STRICT"),
                    "strict_backed": sig.get("strict_backed", False),
                    "entry": sig.get("entry", 0),
                    "tp": sig.get("tp", 0),
                    "sl": sig.get("sl", 0),
                    "tp_pct": sig.get("tp_pct", 0),
                    "sl_pct": sig.get("sl_pct", 0),
                    "created_at": sig.get("created_at", ""),
                    "created_at_time": sig.get("created_at_time", ""),
                    "rsi": sig.get("rsi", 0),
                    "macd_hist": sig.get("macd_hist", 0),
                    "mtf_score": sig.get("mtf_score", 0),
                    "quality_score": sig.get("quality_score", 0),
                    "trigger_info": sig.get("trigger_info", {}),
                    "quick_backtest_7d": sig.get("quick_backtest_7d", {}),
                    "is_open": True
                }

        # Check closed signals (get most recent)
        closed_signals = signals_data.get("closed", [])
        ticker_closed = [s for s in closed_signals if s.get("ticker") == ticker]

        if ticker_closed:
            # Sort by closed_at or created_at descending
            ticker_closed.sort(
                key=lambda x: x.get("closed_at", x.get("created_at", "")),
                reverse=True
            )
            last_closed = ticker_closed[0]
            return {
                "status": last_closed.get("status", "CLOSED"),
                "action": last_closed.get("action", last_closed.get("type", "")),
                "variant": last_closed.get("variant", ""),
                "variant_mode": last_closed.get("variant_mode", "STRICT"),
                "strict_backed": last_closed.get("strict_backed", False),
                "entry": last_closed.get("entry", 0),
                "exit_price": last_closed.get("exit_price", 0),
                "tp": last_closed.get("tp", 0),
                "sl": last_closed.get("sl", 0),
                "tp_pct": last_closed.get("tp_pct", 0),
                "sl_pct": last_closed.get("sl_pct", 0),
                "created_at": last_closed.get("created_at", ""),
                "closed_at": last_closed.get("closed_at", ""),
                "rsi": last_closed.get("rsi", 0),
                "macd_hist": last_closed.get("macd_hist", 0),
                "trigger_info": last_closed.get("trigger_info", {}),
                "close_reason": last_closed.get("close_reason", ""),
                "pnl_pct": last_closed.get("pnl_pct", 0),
                "is_open": False
            }

        return None

    except Exception as e:
        logger.warning(f"Error getting last signal for {ticker}: {e}")
        return None


def run_backtest(
    days: int = 30,
    risk_mode: str = "MEDIUM",
    trade_mode: str = "PULLBACK",
    tickers: Optional[List[str]] = None,
    compare_all: bool = False,
    orb_entry_type: str = "MIDPOINT",
    custom_tp_pct: float = None,
    custom_sl_pct: float = None,
    variant_mode: str = "STRICT"
) -> Dict:
    """
    Run backtest for configured days.

    Args:
        days: Number of days to backtest (default 30)
        risk_mode: Risk profile (STRONG/MEDIUM/RELAX)
        trade_mode: Trading mode (PULLBACK/BREAKOUT/CONTRA)
        tickers: List of tickers to test (None = all from watchlist)
        compare_all: If True, run all 9 strategy combos for the (single) ticker

    Returns:
        Backtest results dictionary
    """
    try:
        config = get_shared_config()
        # Capture raw risk_mode before normalization: FAITHFUL is a mode selector
        # (live replay via strategy_backtester), not a risk profile, and would be
        # silently collapsed to MEDIUM by the alias/validate step below.
        raw_risk_mode = (risk_mode or "").upper().strip()
        raw_trade_mode = (trade_mode or "").upper().strip()
        # Normalize risk_mode for compatibility (accept UI synonyms)
        risk_mode = (risk_mode or "MEDIUM").upper().strip()
        risk_aliases = {
            "HIGH": "STRONG",
            "AGGRESSIVE": "STRONG",
            "MODERATE": "MEDIUM",
            "CONSERVATIVE": "RELAX",
            "LOW": "RELAX",
        }
        risk_mode = risk_aliases.get(risk_mode, risk_mode)
        if risk_mode not in {"STRONG", "MEDIUM", "RELAX", "PRO"}:
            # FAITHFUL is a mode selector (live replay via strategy_backtester),
            # not a risk profile — handled by the dedicated FAITHFUL branch below,
            # which returns early and never reads this normalized value. Suppress
            # the misleading "invalid risk_mode" warning for it; still default the
            # fallback path to MEDIUM in case the branch doesn't catch it.
            if raw_risk_mode != "FAITHFUL":
                logger.warning(f"Invalid risk_mode '{risk_mode}', defaulting to MEDIUM")
            risk_mode = "MEDIUM"

        # Use configured days if not specified
        if days is None:
            days = config.backtest.get("days", 30)

        # Refuse values the form cannot mean (zero/negative/non-finite TP or
        # SL, a non-positive period) instead of simulating them.
        request_error, days, custom_tp_pct, custom_sl_pct = _validate_backtest_request(
            days, custom_tp_pct, custom_sl_pct
        )
        if request_error:
            return {"error": request_error}
        user_custom_exit = custom_tp_pct is not None or custom_sl_pct is not None

        # One canonical symbol per asset: BTCUSDT / BTC-USDT / btc-usd are the
        # same market and must not produce differently-labelled results.
        if tickers:
            tickers = _dedupe_keep_order([
                _normalize_input_ticker(t) for t in tickers if str(t or "").strip()
            ])
            if not tickers:
                return {"error": "Please enter a valid ticker symbol."}

        min_sample = _backtest_min_sample(config)

        # Handle Compare All case (Single Ticker)
        if compare_all:
             if not tickers or len(tickers) != 1:
                 return {"error": "Compare All requires exactly one ticker."}

             ticker = tickers[0].strip().upper()
             logger.info(f"Running Strategy Comparison for {ticker} ({days} days)...")

             # Reuse existing logic
             comparison_meta: Dict[str, Any] = {}
             comparison = _get_strategy_comparison(
                 ticker,
                 days,
                 custom_tp_pct=custom_tp_pct,
                 custom_sl_pct=custom_sl_pct,
                 variant_mode=variant_mode,
                 meta=comparison_meta,
             )
             if comparison_meta.get("error") or not comparison:
                 # An empty comparison used to be returned (and saved to the
                 # history) as a normal result with no rows.
                 return {
                     "mode": "comparison",
                     "ticker": ticker,
                     "days": days,
                     "error": comparison_meta.get("error") or f"No backtest result could be produced for {ticker}.",
                     "warnings": comparison_meta.get("warnings", []),
                 }
             res = {
                 "mode": "comparison",
                 "ticker": ticker,
                 "days": days,
                 "results": comparison,
                 "variant_mode": (variant_mode or "STRICT").upper(),
                 "data_coverage": comparison_meta.get("data_coverage", {}),
                 "warnings": comparison_meta.get("warnings", []),
                 "minimum_sample": min_sample,
                 "custom_tp_pct": custom_tp_pct,
                 "custom_sl_pct": custom_sl_pct,
                 "timestamp": get_bucharest_time().isoformat()
             }
             save_backtest_result(res)
             return res

        # Special handling for ORB_FVG - return raw results from orb_fvg_backtester
        if trade_mode == "ORB_FVG":
            if not tickers or len(tickers) == 0:
                return {"error": "ORB_FVG requires at least one ticker"}
            
            ticker = tickers[0].strip().upper()
            is_crypto = is_crypto_ticker(ticker)
            
            if is_crypto:
                return {
                    "mode": "orb_fvg",
                    "error": "OPENING RANGE is only available for US Stocks. Crypto is not supported.",
                    "wins": 0, "losses": 0, "win_rate": 0.0
                }
            
            try:
                from core.orb_fvg_backtester import run_orb_fvg_backtest
                orb_result = run_orb_fvg_backtest(
                    ticker=ticker,
                    days=days,
                    entry_type=orb_entry_type
                )
                orb_result["mode"] = "orb_fvg"
                orb_result["ticker"] = ticker
                orb_result["risk_mode"] = "OPENING_RANGE"
                orb_result["timestamp"] = get_bucharest_time().isoformat()
                # ORB sizes its own stop/target from the gap; say so rather
                # than let a typed TP/SL look as if it had been applied.
                if user_custom_exit:
                    orb_result.setdefault("warnings", []).append(
                        "Custom TP/SL % does not apply to OPENING RANGE: the stop sits beyond the "
                        "gap candle and the target is a fixed 2R."
                    )
                save_backtest_result(orb_result)
                return orb_result
            except Exception as e:
                logger.error(f"ORB_FVG backtest error: {e}")
                return {"mode": "orb_fvg", "error": str(e), "wins": 0, "losses": 0}

        # FAITHFUL (live replay) — uses core.strategy_backtester which replays the
        # LIVE compute_signal_score over historical 1m+TF bars (look-ahead-safe),
        # not the legacy RSI/MACD rule. Crypto-only (Binance 1m+TF history).
        # Triggered by trade_mode=="FAITHFUL" or risk_mode=="FAITHFUL" (UI bt-risk).
        if raw_trade_mode == "FAITHFUL" or raw_risk_mode == "FAITHFUL":
            if not tickers:
                return {"error": "FAITHFUL requires at least one ticker"}
            ticker = tickers[0].strip().upper()
            if not is_crypto_ticker(ticker):
                return {
                    "mode": "faithful",
                    "error": "FAITHFUL (live replay) currently supports crypto only (Binance 1m+TF).",
                    "wins": 0, "losses": 0, "win_rate": 0.0,
                }
            try:
                from core.strategy_backtester import build_dataframe, backtest
                df = build_dataframe(ticker, days)
                if df is None:
                    # backtest(df=None) would fetch everything a second time
                    # and then answer with the bare code "insufficient_data".
                    return {
                        "mode": "faithful", "ticker": ticker, "days": days,
                        "error": (
                            f"No usable Binance 1-minute history was returned for {ticker} "
                            f"({days} days). Check the symbol (e.g. BTC-USD) or try again later."
                        ),
                        "wins": 0, "losses": 0, "win_rate": 0.0,
                    }
                tp_override = (float(custom_tp_pct) / 100.0) if custom_tp_pct else None
                sl_override = (float(custom_sl_pct) / 100.0) if custom_sl_pct else None
                # Variant Mode: the live engine never scores on the bare signal
                # settings; it always layers variant_strict or variant_custom on
                # top (engine._get_variant_signal_cfg). Replay the same way, so
                # the STRICT/CUSTOM selector changes something.
                variant_cm, variant_applied = _variant_replay_config(config, variant_mode)
                res = backtest(ticker, days, df=df,
                               tp_pct_override=tp_override, sl_pct_override=sl_override,
                               **({"config_manager": variant_cm} if variant_applied else {}))
                if res.get("error"):
                    faithful_errors = {
                        "insufficient_data": f"Not enough price history for {ticker} to replay {days} days.",
                        "cost_model_unavailable": (
                            f"No trading-cost model is configured for {ticker}: "
                            f"{res.get('detail') or 'see strategy_v2.costs_by_asset'}."
                        ),
                    }
                    return {
                        "mode": "faithful", "ticker": ticker, "days": days,
                        "error": faithful_errors.get(str(res["error"]), str(res["error"])),
                        "wins": 0, "losses": 0, "win_rate": 0.0,
                    }
                res["mode"] = "faithful"
                res["ticker"] = ticker
                res["variant_mode"] = (variant_mode or "STRICT").upper()
                res["variant_applied"] = variant_applied
                attrs = dict(getattr(df, "attrs", {}) or {})
                if attrs.get("actual_span_days") is not None:
                    span = float(attrs["actual_span_days"])
                    res["data_coverage"] = {
                        "requested_days": days,
                        "actual_span_days": round(span, 2),
                        "data_start": attrs.get("actual_start"),
                        "data_end": attrs.get("actual_end"),
                        "bars": attrs.get("received_bars"),
                        "truncated": bool(attrs.get("source_truncated")) or (days - span) > max(3.0, 0.15 * days),
                    }
                    if res["data_coverage"]["truncated"]:
                        res.setdefault("warnings", []).append(
                            f"Requested {days} days, but the data source returned only {span:.1f} days. "
                            "Every figure describes that shorter span."
                        )
                exit_mode_cfg = str((config.signal or {}).get("exit_mode", "ATR")).upper()
                if user_custom_exit and exit_mode_cfg != "FIXED_PCT":
                    res.setdefault("warnings", []).append(
                        f"Custom TP/SL % was not applied: the live exit mode is {exit_mode_cfg}, "
                        "which sizes exits from ATR. The replay follows the live exit rules."
                    )
                res["timestamp"] = get_bucharest_time().isoformat()
                # Adapter to the comparison-row contract App.renderBacktestResults reads.
                trade_list = res.get("trade_list", []) or []
                res.setdefault("win_rate_trades", res.get("win_rate", 0.0))
                res.setdefault("win_pct", res.get("win_rate", res.get("win_rate_trades", 0.0)))
                res.setdefault("expectancy_r", res.get("avg_R", 0.0))
                res.setdefault("payoff_ratio", res.get("rr", 0.0))
                res.setdefault("wins", sum(1 for t in trade_list if t.get("net_pct", 0) > 0))
                res.setdefault("losses", sum(1 for t in trade_list if t.get("net_pct", 0) <= 0))
                res.setdefault("trades", res.get("trades", len(trade_list)))
                save_backtest_result(res)
                return res
            except Exception as e:
                logger.error(f"FAITHFUL backtest error: {e}")
                return {"mode": "faithful", "error": str(e), "wins": 0, "losses": 0}

        results = {
            "mode": "standard",
            "days": days,
            "risk_mode": risk_mode,
            "trade_mode": trade_mode,
            "variant_mode": (variant_mode or "STRICT").upper(),
            "total_wins": 0,
            "total_losses": 0,
            "win_pct": 0.0,
            "loss_pct": 0.0,
            "signals": [],
            "timestamp": get_bucharest_time().isoformat()
        }

        try:
            from core.quick_backtester import QuickBacktester

            backtester = QuickBacktester(config)

            # Get tickers - use provided list or load from watchlist
            if tickers:
                all_tickers = [t.strip().upper() for t in tickers if t.strip()]
            else:
                watchlist = {"stocks": [], "crypto": []}
                if WATCHLIST_PATH.exists():
                    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
                        watchlist = json.load(f)
                all_tickers = watchlist.get("stocks", []) + watchlist.get("crypto", [])

            total_wins = 0
            total_losses = 0
            failed_tickers: List[Dict[str, str]] = []
            coverage_by_ticker: Dict[str, Dict[str, Any]] = {}
            results["tickers"] = list(all_tickers)
            if len(all_tickers) == 1:
                results["ticker"] = all_tickers[0]

            # Process all tickers (no limit)
            for ticker in all_tickers:
                is_crypto = is_crypto_ticker(ticker)

                # Auto-select interval based on days to maximize data
                ui_interval = "5min"
                if not is_crypto:
                    if days > 7:
                        ui_interval = "5min"
                    if days > 60:
                        ui_interval = "1h"
                
                try:
                    # [NEW] Copy globally requested overrides
                    iter_tp = custom_tp_pct
                    iter_sl = custom_sl_pct
                    t_settings = config.get_ticker_settings(ticker)
                    global_exit_mode = str(config.signal.get("exit_mode", "ATR")).upper()
                    iter_exit_mode = str(t_settings.get("exit_mode") or global_exit_mode).upper()

                    # V8.x: Respect manual overrides for ALL modes
                    if iter_tp is None:
                        iter_tp = t_settings.get("tp_pct")
                    if iter_sl is None:
                        iter_sl = t_settings.get("sl_pct")

                    bt_result = backtester.quick_backtest(
                        ticker=ticker,
                        risk_mode=risk_mode,
                        is_crypto=is_crypto,
                        days=days,
                        trade_mode=trade_mode,
                        ui_interval=ui_interval,
                        orb_entry_type=orb_entry_type,
                        # A TP/SL % typed into the form can only be honoured by
                        # percentage exits; in ATR mode it was silently ignored.
                        exit_mode="FIXED_PCT" if user_custom_exit else iter_exit_mode,
                        custom_tp_pct=iter_tp,
                        custom_sl_pct=iter_sl,
                        variant_mode=variant_mode
                    )

                    # A run that could not happen is not a run with 0 trades.
                    if bt_result.get("error"):
                        failed_tickers.append({"ticker": ticker, "reason": str(bt_result["error"])})
                        continue
                    if bt_result.get("insufficient_data"):
                        bars = bt_result.get("bars") or 0
                        failed_tickers.append({
                            "ticker": ticker,
                            "reason": (
                                "no historical price data was returned" if not bars
                                else f"only {bars} price bars were returned (at least "
                                     f"{bt_result.get('min_bars_required') or 100} are needed)"
                            ),
                        })
                        continue
                    span_days = bt_result.get("actual_span_days")
                    ticker_coverage = {
                        "requested_days": days,
                        "bars": bt_result.get("bars"),
                        "data_start": bt_result.get("data_start"),
                        "data_end": bt_result.get("data_end"),
                        "actual_span_days": span_days,
                        "truncated": bool(
                            span_days is not None and (days - float(span_days)) > max(3.0, 0.15 * days)
                        ),
                    }
                    coverage_by_ticker[ticker] = ticker_coverage
                    ticker_warnings = list(bt_result.get("warnings", []) or [])
                    if ticker_coverage["truncated"]:
                        ticker_warnings.append(
                            f"Requested {days} days, but the data source returned only "
                            f"{float(span_days):.1f} days ({ticker_coverage['bars']} bars). "
                            "Every figure describes that shorter span."
                        )
                    bt_result["warnings"] = ticker_warnings

                    total_wins += bt_result.get("wins", 0)
                    total_losses += bt_result.get("losses", 0)

                    results["signals"].append({
                        "ticker": ticker,
                        "wins": bt_result.get("wins", 0),
                        "losses": bt_result.get("losses", 0),
                        "win_rate": bt_result.get("win_rate", 0),
                        "win_rate_trades": bt_result.get("win_rate_trades", 0),
                        "trade_count": bt_result.get("trade_count", 0),
                        "total_signals": bt_result.get("total_signals", 0),
                        "profit_factor": bt_result.get("profit_factor", 0),
                        "max_drawdown": bt_result.get("max_drawdown_pct", 0),
                        "expectancy_r": bt_result.get("expectancy_r", 0),
                        "payoff_ratio": bt_result.get("payoff_ratio", 0),
                        "avg_win_r": bt_result.get("avg_win_r", 0),
                        "avg_loss_r": bt_result.get("avg_loss_r", 0),
                        "warnings": bt_result.get("warnings", []),
                        "trades": bt_result.get("signals", []),  # Full trade list with entry/exit details
                        "closed_trades": bt_result.get("closed_trades", len(bt_result.get("signals", []) or [])),
                        "open_trades": bt_result.get("open_trades", 0),
                        "evidence": _backtest_evidence(
                            bt_result.get("closed_trades", len(bt_result.get("signals", []) or [])),
                            min_sample,
                        ),
                        "effective_exit_mode": bt_result.get("effective_exit_mode"),
                        "effective_tp_pct": bt_result.get("effective_tp_pct"),
                        "effective_sl_pct": bt_result.get("effective_sl_pct"),
                        "effective_atr_multiplier_tp": bt_result.get("effective_atr_multiplier_tp"),
                        "effective_atr_multiplier_sl": bt_result.get("effective_atr_multiplier_sl"),
                        "custom_exit_applied": bool(bt_result.get("custom_exit_applied", False)),
                        "max_drawdown_r": bt_result.get("max_drawdown_r"),
                        "max_drawdown_basis": bt_result.get("max_drawdown_basis"),
                        "data_coverage": ticker_coverage,
                    })

                except Exception as e:
                    logger.warning(f"Backtest failed for {ticker}: {e}")
                    failed_tickers.append({"ticker": ticker, "reason": str(e)})

            if failed_tickers:
                results["failed_tickers"] = failed_tickers
            if all_tickers and len(failed_tickers) == len(all_tickers):
                # Nothing ran: report it, do not present (or save) an empty
                # result as if the strategy had simply found no trades.
                first = failed_tickers[0]
                return {
                    "mode": "standard",
                    "days": days,
                    "risk_mode": risk_mode,
                    "trade_mode": trade_mode,
                    "error": (
                        f"Backtest could not run for {first['ticker']}: {first['reason']}. "
                        "Check the symbol (e.g. AAPL, BTC-USD) or try again later."
                        if len(failed_tickers) == 1 else
                        f"Backtest could not run for any of the {len(failed_tickers)} tickers "
                        f"(first: {first['ticker']} - {first['reason']})."
                    ),
                    "failed_tickers": failed_tickers,
                }
            if len(coverage_by_ticker) == 1:
                results["data_coverage"] = next(iter(coverage_by_ticker.values()))

            total = total_wins + total_losses
            results["total_wins"] = total_wins
            results["total_losses"] = total_losses
            results["win_pct"] = round(total_wins / total * 100, 1) if total > 0 else 0.0
            results["loss_pct"] = round(total_losses / total * 100, 1) if total > 0 else 0.0

            from core.backtest_summary import summarize_trades
            trades = [dict(t, ticker=t.get("ticker") or row.get("ticker"))
                      for row in results.get("signals", []) if isinstance(row, dict)
                      for t in (row.get("trades") or []) if isinstance(t, dict)]
            results["trade_list"] = trades
            results.update(summarize_trades(trades))
            results["evidence"] = _backtest_evidence(len(trades), min_sample)
            results["minimum_sample"] = min_sample

        except ImportError:
            logger.warning("QuickBacktester not available")

        logger.info(f"Backtest completed: {results['total_wins']}W / {results['total_losses']}L ({risk_mode}/{trade_mode})")
        save_backtest_result(results)
        return results

    except Exception as e:
        logger.error(f"Error running backtest: {e}")
        raise


def delete_signal(signal_id: str) -> Dict:
    """
    Delete a signal by ID from both open and closed signals.
    Also adds the exact signal signature to suppressed list to prevent same signal regeneration.
    
    Args:
        signal_id: The signal ID to delete
        
    Returns:
        Dict with success status and message
    """
    try:
        suppressed_path = SUPPRESSED_TICKERS_PATH

        signals_data = load_signals_payload({"open": {}, "closed": []})
        if not isinstance(signals_data, dict):
            return {"success": False, "error": "Signals payload invalid"}
        
        deleted = False
        deleted_from = None
        deleted_ticker = None
        deleted_action = None
        deleted_signal_mode = None
        deleted_variant_mode = None
        
        # Check open signals - capture ticker and action BEFORE deletion
        open_signals = signals_data.get("open", {})
        if signal_id in open_signals:
            sig = open_signals[signal_id]
            deleted_ticker = sig.get("ticker")
            deleted_action = sig.get("action") or sig.get("type")
            deleted_signal_mode = sig.get("signal_mode")
            deleted_variant_mode = sig.get("variant_mode")
            del open_signals[signal_id]
            signals_data["open"] = open_signals
            deleted = True
            deleted_from = "open"
            logger.info(
                f"Deleted open signal: {signal_id} "
                f"({deleted_ticker} {deleted_action} {deleted_signal_mode}/{deleted_variant_mode})"
            )
        
        # Check closed signals
        if not deleted:
            closed_signals = signals_data.get("closed", [])
            for sig in closed_signals:
                if sig.get("id") == signal_id:
                    deleted_ticker = sig.get("ticker")
                    deleted_action = sig.get("action") or sig.get("type")
                    deleted_signal_mode = sig.get("signal_mode")
                    deleted_variant_mode = sig.get("variant_mode")
                    break
            original_len = len(closed_signals)
            closed_signals = [s for s in closed_signals if s.get("id") != signal_id]
            if len(closed_signals) < original_len:
                signals_data["closed"] = closed_signals
                deleted = True
                deleted_from = "closed"
                logger.info(
                    f"Deleted closed signal: {signal_id} "
                    f"({deleted_ticker} {deleted_action} {deleted_signal_mode}/{deleted_variant_mode})"
                )
        
        if deleted:
            # Save updated signals
            save_signals_payload(signals_data)
            
            # Add exact signal signature to suppressed list
            if deleted_ticker and deleted_action:
                cfg = get_shared_config()
                try:
                    suppress_minutes = int(cfg.signal.get("manual_delete_suppress_minutes", 60) or 60)
                except Exception:
                    suppress_minutes = 60
                suppress_minutes = max(1, suppress_minutes)

                suppressed = {}
                if suppressed_path.exists():
                    try:
                        with open(suppressed_path, "r", encoding="utf-8") as f:
                            suppressed = json.load(f)
                    except Exception:
                        suppressed = {}
                
                # Use ticker+action+strategy signature so another BUY on another strategy is allowed.
                ticker_norm = str(deleted_ticker).upper().strip()
                action_norm = str(deleted_action).upper().strip()
                signal_mode_norm = str(deleted_signal_mode or "UNKNOWN").upper().strip()
                variant_mode_norm = str(deleted_variant_mode or "STRICT").upper().strip()
                suppress_key = f"{ticker_norm}_{action_norm}_{signal_mode_norm}_{variant_mode_norm}"
                suppressed[suppress_key] = {
                    "ticker": ticker_norm,
                    "action": action_norm,
                    "signal_mode": signal_mode_norm,
                    "variant_mode": variant_mode_norm,
                    "suppressed_at": get_bucharest_time().isoformat(),
                    "suppress_minutes": suppress_minutes,
                }
                
                _write_json_atomic(Path(suppressed_path), suppressed)
                
                logger.info(
                    f"Suppressed {ticker_norm} {action_norm} {signal_mode_norm}/{variant_mode_norm} "
                    f"for {suppress_minutes} min (different strategy still allowed)"
                )

            # [NEW] Add to Activity Log in state.json
            try:
                state_data = load_state_payload({})
                if not isinstance(state_data, dict):
                    state_data = {}

                if "activity_log" not in state_data:
                    state_data["activity_log"] = []

                # Create log entry
                log_entry = {
                    "time": get_bucharest_time().isoformat(),
                    "type": "CLOSE", # Use CLOSE to inherit red/green styling logic or generic
                    "subtype": "MANUAL",
                    "icon": "fa-trash",
                    "desc": f"MANUAL DELETE<br>{deleted_action} {deleted_ticker} (Removed by User)"
                }

                # Append and limit to 300
                state_data["activity_log"].insert(0, log_entry)
                state_data["activity_log"] = state_data["activity_log"][:300]
                save_state_payload(state_data)
                        
            except Exception as ex:
                logger.error(f"Failed to log deletion activity: {ex}")
            
            return {"success": True, "deleted_from": deleted_from, "ticker_suppressed": deleted_ticker}
        else:
            return {"success": False, "error": f"Signal {signal_id} not found"}
            
    except Exception as e:
        logger.error(f"Error deleting signal {signal_id}: {e}")
        return {"success": False, "error": str(e)}


def recalculate_backtest_for_open_signals() -> Dict:
    """
    Recalculate backtest results for all open signals using consistent data.
    This ensures backtest 7d and best_strategy use the same data source.
    
    Returns:
        Dict with success status and count of updated signals
    """
    try:
        from core.quick_backtester import QuickBacktester
        import threading

        signals_data = load_signals_payload({"open": {}, "closed": []})
        if not isinstance(signals_data, dict):
            return {"success": False, "error": "Signals payload invalid"}
        
        open_signals = signals_data.get("open", {})
        if not open_signals:
            return {"success": True, "message": "No open signals to update", "updated": 0}
        
        config = get_shared_config()
        backtester = QuickBacktester(config, threading.Lock())
        
        updated_count = 0
        
        for sig_id, sig in open_signals.items():
            ticker = sig.get("ticker", "")
            existing_bt_30d = sig.get("quick_backtest_30d", {}) or {}
            existing_best = sig.get("best_strategy", {}) or {}
            risk_mode = (
                sig.get("risk_mode")
                or existing_bt_30d.get("risk_mode")
                or "MEDIUM"
            )
            trade_mode = (
                sig.get("trade_mode")
                or sig.get("signal_mode")
                or existing_bt_30d.get("trade_mode")
                or "PULLBACK"
            )
            risk_mode = str(risk_mode).upper().strip()
            trade_mode = str(trade_mode).upper().strip()
            variant_mode = sig.get("variant_mode", "STRICT")
            asset_type = sig.get("asset_type", "stock")
            is_crypto = asset_type == "crypto"
            
            logger.info(f"Recalculating backtest for {ticker} ({risk_mode}/{trade_mode})...")
            
            try:
                # Fetch data ONCE
                df, _ = backtester.fetch_backtest_data(ticker, is_crypto, days=7)
                
                if df is None or len(df) < 100:
                    logger.warning(f"Insufficient data for {ticker}, skipping...")
                    continue
                
                # Run backtest for signal's strategy
                bt_result = backtester.quick_backtest(
                    ticker=ticker,
                    risk_mode=risk_mode,
                    is_crypto=is_crypto,
                    days=7,
                    trade_mode=trade_mode,
                    df=df,
                    variant_mode=variant_mode
                )
                
                # Find best strategy using SAME data
                risk_modes = ["STRONG", "MEDIUM", "RELAX"]
                trade_modes = ["PULLBACK", "BREAKOUT", "CONTRA"]
                best_result = None
                best_win_rate = -1
                
                for rm in risk_modes:
                    for tm in trade_modes:
                        result = backtester.quick_backtest(
                            ticker=ticker,
                            risk_mode=rm,
                            is_crypto=is_crypto,
                            days=7,
                            trade_mode=tm,
                            df=df,
                            variant_mode=variant_mode
                        )
                        
                        if result["total_signals"] > 0:
                            if result["win_rate"] > best_win_rate or (
                                result["win_rate"] == best_win_rate and 
                                best_result and 
                                result["total_signals"] > best_result["total_signals"]
                            ):
                                best_win_rate = result["win_rate"]
                                best_result = {
                                    "risk_mode": rm,
                                    "trade_mode": tm,
                                    "win_rate": round(result["win_rate"], 1),
                                    "total_signals": result["total_signals"],
                                    "wins": result["wins"],
                                    "losses": result["losses"],
                                }

                # Update quick_backtest_7d and auto-fallback if generated strategy has 0 trades.
                bt_payload = {
                    "wins": bt_result["wins"],
                    "losses": bt_result["losses"],
                    "total": bt_result["total_signals"],
                    "win_rate": round(bt_result["win_rate"], 1),
                    "risk_mode": risk_mode,
                    "trade_mode": trade_mode,
                    "fallback_used": False,
                }
                if (bt_payload["total"] or 0) <= 0 and best_result and (best_result.get("total_signals", 0) or 0) > 0:
                    bt_payload.update(
                        {
                            "wins": best_result["wins"],
                            "losses": best_result["losses"],
                            "total": best_result["total_signals"],
                            "win_rate": best_result["win_rate"],
                            "risk_mode": best_result["risk_mode"],
                            "trade_mode": best_result["trade_mode"],
                            "fallback_used": True,
                            "fallback_reason": "no_signals_for_generated_strategy",
                        }
                    )
                sig["quick_backtest_7d"] = bt_payload
                
                chosen_best = existing_best
                if best_result and (best_result.get("total_signals", 0) or 0) > 0:
                    if (best_result.get("total_signals", 0) or 0) >= (existing_best.get("total_signals", 0) or 0):
                        chosen_best = best_result
                if chosen_best and (chosen_best.get("total_signals", 0) or 0) > 0:
                    sig["best_strategy"] = chosen_best

                # Backfill 30d payload for older OPEN signals that were saved with total=0.
                bt30_total = int(existing_bt_30d.get("total", 0) or 0)
                chosen_total = int((chosen_best or {}).get("total_signals", 0) or 0)
                if bt30_total <= 0 and chosen_total > 0:
                    sig["quick_backtest_30d"] = {
                        "wins": int(chosen_best.get("wins", 0) or 0),
                        "losses": int(chosen_best.get("losses", 0) or 0),
                        "total": chosen_total,
                        "win_rate": round(float(chosen_best.get("win_rate", 0) or 0), 1),
                        "risk_mode": str(chosen_best.get("risk_mode") or risk_mode).upper(),
                        "trade_mode": str(chosen_best.get("trade_mode") or trade_mode).upper(),
                        "variant_mode": str(variant_mode).upper(),
                        "fallback_used": True,
                        "fallback_reason": "recalc_backfill_from_best_strategy",
                    }
                    logger.info(
                        f"  Backfilled 30d for {ticker}: "
                        f"{chosen_best.get('risk_mode')}/{chosen_best.get('trade_mode')} "
                        f"{chosen_total} signals"
                    )
                
                updated_count += 1
                logger.info(f"  Updated {ticker}: BT={bt_result['win_rate']:.1f}%, BEST={best_result['risk_mode']}/{best_result['trade_mode']} {best_result['win_rate']:.1f}%" if best_result else f"  Updated {ticker}: No best strategy found")
                
            except Exception as e:
                logger.error(f"Error recalculating {ticker}: {e}")
                continue
        
        # Save updated signals
        save_signals_payload(signals_data)
        
        logger.info(f"✅ Recalculated backtest for {updated_count} signals")
        return {"success": True, "updated": updated_count}
        
    except Exception as e:
        logger.error(f"Error in recalculate_backtest_for_open_signals: {e}")
        return {"success": False, "error": str(e)}

# --- Smart Alerts ---

def get_alerts(ticker: Optional[str] = None):
    from api.engine_service import get_engine
    engine = get_engine()
    if engine:
        return engine.alert_manager.get_alerts(ticker)
    try:
        from core.alerts import AlertManager
        from core.config import ConfigManager
        return AlertManager(ConfigManager()).get_alerts(ticker)
    except Exception:
        return []

def create_alert(ticker: str, alert_type: str, target: float):
    # The engine evaluates alerts only against watchlist prices (check_alerts
    # silently skips unknown tickers) — reject unwatched tickers up front
    # instead of showing a permanently "Active" alert that can never fire.
    from core.alerts import AlertType, find_duplicate_alert, validate_alert_input

    # Tab audit 2026-11-15: type and target were stored unchecked — an unknown
    # type, a NaN/negative/zero target or an exact duplicate was accepted and
    # shown as "Active" although check_alerts() could never fire it sensibly.
    try:
        ticker_u, alert_type, target = validate_alert_input(ticker, alert_type, target)
    except ValueError as exc:
        return {"error": str(exc), "status_code": 400}
    watch = get_shared_config().watchlist or {}
    known = {str(t).upper() for t in (watch.get("yahoo") or watch.get("stocks") or [])}
    known |= {str(t).upper() for t in (watch.get("binance") or watch.get("crypto") or [])}
    if ticker_u not in known:
        return {"error": f"{ticker_u} is not in the watchlist — alerts can only be set for watched tickers.",
                "status_code": 400}
    try:
        existing = get_alerts(ticker_u)
    except Exception:
        existing = []
    if find_duplicate_alert(existing, ticker_u, alert_type, target):
        return {"error": f"An active {alert_type.replace('_', ' ')} alert for {ticker_u} at {target:g} already exists.",
                "status_code": 409}
    # A "crosses up" target below the current price (or "crosses down" above
    # it) is already satisfied and would fire on the very next scan — tell the
    # user instead of sending a meaningless "target reached". Best effort: only
    # when a fresh price is on hand, exactly as check_alerts() would judge it.
    if alert_type in (AlertType.PRICE_UP, AlertType.PRICE_DOWN):
        current = _alert_reference_price(ticker_u)
        if current is not None:
            if alert_type == AlertType.PRICE_UP and current >= target:
                return {"error": f"{ticker_u} already trades at {current:g}, at or above {target:g}. "
                                 "Set a higher target, or use Price Crosses Down.",
                        "status_code": 400}
            if alert_type == AlertType.PRICE_DOWN and current <= target:
                return {"error": f"{ticker_u} already trades at {current:g}, at or below {target:g}. "
                                 "Set a lower target, or use Price Crosses Up.",
                        "status_code": 400}
    from api.engine_service import get_engine
    engine = get_engine()
    if not engine:
        try:
            from core.alerts import AlertManager
            from core.config import ConfigManager
            alert = AlertManager(ConfigManager()).add_alert(ticker_u, alert_type, target)
            return alert.to_dict()
        except Exception:
            logger.error("Alert could not be saved", exc_info=True)
            return {"error": "Alert could not be saved — see the server log.", "status_code": 500}

    alert = engine.alert_manager.add_alert(ticker_u, alert_type, target)
    return alert.to_dict()


def _alert_reference_price(ticker_u: str) -> Optional[float]:
    """Last scanned price for a ticker, or None when absent/stale/invalid."""
    import math
    try:
        tickers = (load_state_payload({}) or {}).get("tickers")
        row = None
        if isinstance(tickers, dict):
            row = tickers.get(ticker_u)
        elif isinstance(tickers, list):
            row = next((t for t in tickers if isinstance(t, dict) and str(t.get("ticker") or "").upper() == ticker_u), None)
        if not isinstance(row, dict):
            return None
        if row.get("stale") is True or str(row.get("freshness", "")).lower() in {"stale", "expired", "unavailable"}:
            return None
        price = float(row.get("price"))
        return price if math.isfinite(price) and price > 0 else None
    except Exception:
        return None

def delete_alert(alert_id: str):
    from api.engine_service import get_engine
    engine = get_engine()
    if not engine:
        try:
            from core.alerts import AlertManager
            from core.config import ConfigManager
            success = AlertManager(ConfigManager()).remove_alert(alert_id)
            return {"success": success}
        except Exception:
            return {"success": False, "error": "Engine not running"}
    
    success = engine.alert_manager.remove_alert(alert_id)
    return {"success": success}

def toggle_alert(alert_id: str):
    from api.engine_service import get_engine
    engine = get_engine()
    if not engine:
        try:
            from core.alerts import AlertManager
            from core.config import ConfigManager
            new_state = AlertManager(ConfigManager()).toggle_alert(alert_id)
            if new_state is None:
                return {"success": False, "error": "Alert not found"}
            return {"success": True, "active": new_state}
        except Exception:
            return {"success": False, "error": "Engine not running"}
    
    new_state = engine.alert_manager.toggle_alert(alert_id)
    if new_state is None:
        return {"success": False, "error": "Alert not found"}
    return {"success": True, "active": new_state}
