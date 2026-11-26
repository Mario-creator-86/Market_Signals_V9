"""
State Service for Market Signals V6 - Web
Handles reading state and preparing data for the web UI
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
import sys
import threading
import time
from statistics import mean, pstdev

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import (
    BASE_DIR,
    DATA_DIR,
    ConfigManager,
    STATE_PATH,
    SIGNALS_PATH,
    WATCHLIST_PATH,
    HOTLIST_CACHE_PATH,
    TRADE_RADAR_STATE_PATH,
    BUCHAREST_TZ,
    get_bucharest_time,
    is_crypto_ticker,
    normalize_market_ticker,
    normalize_ticker,
)
from core.agent_toggles import is_agent_enabled
from core.sectors import get_sector
from core.capital_manager import (
    get_capital_manager,
    is_research_position,
    resolve_deployable_balance,
)
from core.trade_audit import get_trade_audit
from datetime import datetime, timezone
from agents.news_tab_agent import (
    get_news_tab_agent_status,
    kickoff_news_tab_agent_init,
)
from core.hotlist_news import summarize_ticker_news_signals
from core.hotlist_scoring import (
    apply_hotlist_data_guard,
    apply_hotlist_defaults,
    compute_earnings_edge_score,
    compute_hotlist_composite_metrics,
    is_hotlist_technicals_stale,
    resolve_hotlist_quote,
    sort_hotlist_by_priority,
)
from core.news_pulse import build_news_pulse_bulk
from core.request_context import get_request_context
from core.state_persistence import load_state_payload, load_signals_payload
from core.ticker_workspace import get_ticker_workspace_store
from core.trade_decision import summarize_position_explainability

logger = logging.getLogger("MarketSignalsAPI")


def _compact_trade_audit(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    compact: List[Dict[str, Any]] = []
    for item in records or []:
        if not isinstance(item, dict):
            continue
        compact.append({
            "event_id": item.get("event_id", ""),
            "timestamp": item.get("timestamp", ""),
            "event_type": item.get("event_type", ""),
            "status": item.get("status", ""),
            "ticker": item.get("ticker", ""),
            "signal_id": item.get("signal_id", ""),
            "position_id": item.get("position_id", ""),
            "strategy_type": item.get("strategy_type", ""),
            "summary": item.get("summary", ""),
        })
    return compact


def _compact_autonomous_cycle_status() -> Dict[str, Any]:
    try:
        from core.autonomous_control import get_autonomous_cycle_status

        status = get_autonomous_cycle_status()
    except Exception:
        return {}
    if not isinstance(status, dict):
        return {}
    result = status.get("result") if isinstance(status.get("result"), dict) else {}
    cycle = result.get("cycle") if isinstance(result.get("cycle"), dict) else {}
    sources = cycle.get("sources") if isinstance(cycle.get("sources"), dict) else {}
    return {
        "updated_at": status.get("updated_at", ""),
        "request_id": status.get("request_id", ""),
        "status": result.get("status", ""),
        "accepted": bool(result.get("accepted", False)),
        "cycle_status": cycle.get("status", ""),
        "candidate_count": int(cycle.get("candidate_count", 0) or 0),
        "submitted": int(cycle.get("submitted", 0) or 0),
        "reviewed": int(cycle.get("reviewed", 0) or 0),
        "sources": {
            str(name): {
                "status": details.get("status", ""),
                "candidate_count": int(details.get("candidate_count", 0) or 0),
            }
            for name, details in sources.items()
            if isinstance(details, dict)
        },
    }

# Global Config Manager (Shared across poll requests)
_shared_config = None
def get_shared_config():
    global _shared_config
    if _shared_config is None:
        _shared_config = ConfigManager()
    else:
        _shared_config.reload_if_changed()
        _shared_config.reload_watchlist_if_changed()
    return _shared_config

# Cache JSON file reads by mtime to reduce disk I/O on frequent polls
_state_cache = {"mtime": 0.0, "data": {}}
_signals_cache = {"mtime": 0.0, "data": {"open": {}, "closed": []}}
_traderadar_cache = {"mtime": 0.0, "data": {}}
_capital_exec_quote_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_capital_exec_quote_cache_lock = threading.Lock()
_last_signal_repair_ts = 0.0


def _repair_open_paper_signals_if_needed() -> None:
    global _last_signal_repair_ts
    now = time.time()
    if now - _last_signal_repair_ts < 15.0:
        return
    _last_signal_repair_ts = now
    try:
        from core.signals import get_signal_manager

        manager = get_signal_manager()
        manager.restore_open_paper_signals()
        manager.cleanup_expired_unexecuted_signals(get_shared_config())
    except Exception as exc:
        logger.debug(f"Skipped open paper signal repair: {exc}")


def _capital_quote_ttl_seconds() -> float:
    try:
        return max(5.0, min(float(os.getenv("MS_CAPITAL_QUOTE_TTL_SECONDS", "20") or 20), 120.0))
    except Exception:
        return 20.0


def _is_remote_exec_quote(exec_data: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(exec_data, dict):
        return False
    source = str(exec_data.get("source") or "").strip().lower()
    return source in {
        "binance_orderbook",
        "yahoo_bid_ask",
        "yahoo_quote",
        "etoro_bid_ask",
        "etoro_quote",
        "etoro_live_rates",
    }


def _capital_exec_payload_from_cached_price(ticker: str, side: str, price: float, source: str) -> Dict[str, Any]:
    from core.config import ConfigManager
    from core.data_quality import QualifiedValue
    from core.trading_costs import resolve_trading_cost_model

    symbol = str(ticker or "").upper().strip()
    cost_model = resolve_trading_cost_model(
        symbol,
        strategy_v2_config=ConfigManager().strategy_v2,
    )
    # A cached last/mid does not contain a contemporaneous bid/ask.  Keep it as
    # a display-only estimate instead of fabricating a side-specific spread.
    execution_price = float(price)
    qval = QualifiedValue.estimated(
        execution_price,
        source or "capital_price_cache",
        "Cached price lacks a contemporaneous bid/ask and cannot authorize a paper fill.",
    )
    return {
        "execution_price": execution_price,
        "execution_price_quality": qval.to_dict(),
        "spread_pct": 0.0,
        "slippage_pct": 0.0,
        "commission_rate": cost_model.commission_fraction,
        "cost_model": cost_model.to_dict(),
        "total_cost_pct": cost_model.commission_fraction * 100,
        "price_timestamp": get_bucharest_time().isoformat(),
        "source": source or "capital_price_cache",
    }


def _capital_exec_unavailable(source: str = "cache_miss") -> Dict[str, Any]:
    from core.data_quality import QualifiedValue

    qval = QualifiedValue.unavailable(source, "No cached quote available for cache-only UI request")
    return {
        "execution_price": 0.0,
        "execution_price_quality": qval.to_dict(),
        "spread_pct": 0.0,
        "slippage_pct": 0.0,
        "commission_rate": 0.0,
        "total_cost_pct": 0.0,
        "price_timestamp": get_bucharest_time().isoformat(),
        "source": source,
    }


def _get_capital_exec_quote_cached(cm, ticker: str, side: str, *, allow_remote: bool = True) -> Dict[str, Any]:
    key = f"{str(ticker or '').upper().strip()}:{str(side or '').upper().strip()}"
    now_ts = time.time()
    ttl = _capital_quote_ttl_seconds()
    stale_payload = None
    with _capital_exec_quote_cache_lock:
        cached = _capital_exec_quote_cache.get(key)
        if cached:
            stale_payload = dict(cached[1])
            # Fresh TTL cache short-circuits on BOTH paths. Previously this was
            # gated on `not allow_remote`, so the live-quote pool (allow_remote=True)
            # ALWAYS fell through to cm._get_execution_price(use_cache=False) and
            # re-fetched every quote on every Capital poll — warm calls were as slow
            # as cold (~18s), the 8.5s frontend poll piled up against the ~14s
            # response, and the tab appeared frozen. Now warm polls hit the cache
            # instantly; only the periodic refresh (once per TTL) goes remote, and
            # that is bounded by the pool deadline.
            if (now_ts - float(cached[0] or 0.0)) <= ttl:
                return stale_payload
    if not allow_remote:
        try:
            cached_price = cm._get_cached_price(ticker)
            if cached_price:
                price, source = cached_price
                exec_data = _capital_exec_payload_from_cached_price(ticker, side, float(price), str(source or "capital_price_cache"))
                with _capital_exec_quote_cache_lock:
                    _capital_exec_quote_cache[key] = (time.time(), dict(exec_data))
                return exec_data
        except Exception:
            pass
        if stale_payload is not None:
            stale_payload["source"] = f"{stale_payload.get('source') or 'capital_quote_cache'}_stale"
            return stale_payload
        return _capital_exec_unavailable()
    exec_data = cm._get_execution_price(ticker, side, use_cache=False)
    if isinstance(exec_data, dict):
        if _is_remote_exec_quote(exec_data):
            with _capital_exec_quote_cache_lock:
                _capital_exec_quote_cache[key] = (time.time(), dict(exec_data))
        else:
            with _capital_exec_quote_cache_lock:
                _capital_exec_quote_cache.pop(key, None)
    return exec_data

def _load_json_cached(path: Path, cache: dict, default, *, include_closed: bool = True):
    if path == STATE_PATH:
        data = load_state_payload(default)
        cache["data"] = data
        return data
    if path == SIGNALS_PATH:
        if include_closed:
            _repair_open_paper_signals_if_needed()
        data = load_signals_payload(default, include_closed=include_closed)
        data = normalize_signal_payload_scores(data)
        cache["data"] = data
        return data
    try:
        if path.exists():
            mtime = path.stat().st_mtime
            if cache.get("mtime") == mtime:
                return cache.get("data", default)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cache["mtime"] = mtime
            cache["data"] = data
            return data
    except Exception as e:
        logger.error(f"Error loading {path.name}: {e}")
    return default


def _canonical_ticker_key(raw_ticker: Any) -> str:
    return str(normalize_market_ticker(str(raw_ticker or "")) or "").upper().strip()


def _payload_richness(payload: Dict[str, Any]) -> int:
    if not isinstance(payload, dict):
        return -1
    score = 0
    if _safe_float(payload.get("price"), 0.0) > 0:
        score += 4
    if any(str(payload.get(k) or "").upper() not in {"", "N/A"} for k in ("trend_1d", "trend_4h", "trend_1h")):
        score += 2
    if str(payload.get("action") or payload.get("type") or "").upper().strip():
        score += 2
    if (
        _safe_float(payload.get("signal_score"), 0.0) > 0
        or _safe_float(payload.get("probability_score"), 0.0) > 0
        or _safe_float(payload.get("quality_score"), 0.0) > 0
    ):
        score += 2
    if payload.get("news_pulse_score") not in (None, ""):
        score += 1
    return score


def _merge_ticker_payload(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    if _payload_richness(incoming) > _payload_richness(existing):
        primary, secondary = dict(incoming or {}), dict(existing or {})
    else:
        primary, secondary = dict(existing or {}), dict(incoming or {})

    merged = dict(primary)
    for key, value in secondary.items():
        current = merged.get(key)
        if current in (None, "", [], {}, ()):
            merged[key] = value
            continue
        if key in {"price", "change_pct", "signal_score", "probability_score", "quality_score"}:
            if _safe_float(current, 0.0) <= 0 < _safe_float(value, 0.0):
                merged[key] = value
    return merged


def _canonicalize_ticker_rows(rows: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    normalized: Dict[str, Dict[str, Any]] = {}
    exact_match: Dict[str, bool] = {}

    for raw_key, raw_payload in (rows or {}).items():
        ticker = _canonical_ticker_key(raw_key)
        if not ticker:
            continue

        payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
        payload["ticker"] = ticker
        incoming_exact = str(raw_key or "").upper().strip() == ticker

        existing = normalized.get(ticker)
        if existing is None:
            normalized[ticker] = payload
            exact_match[ticker] = incoming_exact
            continue

        existing_exact = bool(exact_match.get(ticker))
        if incoming_exact and not existing_exact:
            normalized[ticker] = _merge_ticker_payload(payload, existing)
            exact_match[ticker] = True
            continue

        normalized[ticker] = _merge_ticker_payload(existing, payload)
        exact_match[ticker] = existing_exact or incoming_exact

    return normalized


def _effective_signal_score(sig: Dict[str, Any]) -> float:
    if not isinstance(sig, dict):
        return 0.0
    for key in ("signal_score", "score", "raw_signal_score", "raw_score"):
        try:
            value = float(sig.get(key, 0.0) or 0.0)
            if value > 0:
                return value
        except Exception:
            continue
    try:
        probability = float(sig.get("probability_score", 0.0) or 0.0)
        return probability * 100.0 if 0 < probability <= 1 else probability
    except Exception:
        return 0.0


def _normalize_one_signal_score(sig: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(sig, dict):
        return sig
    data = dict(sig)
    score = _effective_signal_score(data)
    if score <= 0:
        return data

    rounded = round(score, 2)
    for key in ("signal_score", "score"):
        try:
            current = float(data.get(key, 0.0) or 0.0)
        except Exception:
            current = 0.0
        if current <= 0:
            data[key] = rounded

    try:
        raw_score = float(data.get("raw_signal_score", 0.0) or 0.0)
    except Exception:
        raw_score = 0.0
    if raw_score <= 0:
        data["raw_signal_score"] = data.get("raw_score", rounded)
    return data


def normalize_signal_payload_scores(signals_data: Dict[str, Any]) -> Dict[str, Any]:
    """Fill missing/zero public signal scores from raw scores for API consumers."""
    if not isinstance(signals_data, dict):
        return signals_data

    normalized = dict(signals_data)
    open_raw = normalized.get("open", {})
    closed_raw = normalized.get("closed", [])

    if isinstance(open_raw, dict):
        normalized["open"] = {
            sid: _normalize_one_signal_score(sig) if isinstance(sig, dict) else sig
            for sid, sig in open_raw.items()
        }
    if isinstance(closed_raw, list):
        normalized["closed"] = [
            _normalize_one_signal_score(sig) if isinstance(sig, dict) else sig
            for sig in closed_raw
        ]
    return normalized


def _build_capital_opportunities(
    tickers_list: List[Dict[str, Any]],
    signals_data: Optional[Dict[str, Any]] = None,
    positions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    config = get_shared_config()
    agents_cfg = config.agents or {}
    execution_cfg = (agents_cfg.get("execution") or {}) if isinstance(agents_cfg, dict) else {}
    scanner_cfg = (agents_cfg.get("scanner") or {}) if isinstance(agents_cfg, dict) else {}
    bot_cfg = config.bot or {}
    capital_cfg = (config.config.get("capital") or {}) if isinstance(config.config, dict) else {}
    min_signal_score = float(capital_cfg.get("autonomous_min_signal_score", execution_cfg.get("min_reconcile_score", 75.0)) or 75.0)
    capital_gate_cfg = execution_cfg.get("capital_entry_gate") if isinstance(execution_cfg.get("capital_entry_gate"), dict) else {}
    capital_data_cfg = capital_gate_cfg.get("data") if isinstance(capital_gate_cfg.get("data"), dict) else {}
    max_signal_age_seconds = max(1, int(float(capital_data_cfg.get("max_signal_age_seconds", 300) or 300)))

    ticker_map = {
        str(row.get("ticker") or "").upper().strip(): row
        for row in (tickers_list or [])
        if isinstance(row, dict) and str(row.get("ticker") or "").strip()
    }
    open_raw = (signals_data or {}).get("open", {}) if isinstance(signals_data, dict) else {}
    raw_signal_rows: List[Dict[str, Any]] = []
    if isinstance(open_raw, dict):
        for sig_id, sig in open_raw.items():
            if not isinstance(sig, dict):
                continue
            ticker = str(sig.get("ticker") or "").upper().strip()
            if not ticker:
                continue
            action = str(sig.get("action") or sig.get("type") or "").upper().strip()
            score = _effective_signal_score(sig)
            row = ticker_map.get(ticker, {})

            created_at = sig.get("created_at") or sig.get("timestamp")
            created_dt = None
            try:
                if created_at:
                    created_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                    if created_dt.tzinfo is None:
                        created_dt = BUCHAREST_TZ.localize(created_dt)
                    created_dt = created_dt.astimezone(timezone.utc)
            except Exception:
                created_dt = None
            age_seconds = max(0.0, (datetime.now(timezone.utc) - created_dt).total_seconds()) if created_dt else None

            decision_context = sig.get("decision_context") if isinstance(sig.get("decision_context"), dict) else {}
            capital_gate = sig.get("capital_entry_gate") if isinstance(sig.get("capital_entry_gate"), dict) else {}
            if not capital_gate and isinstance(decision_context.get("capital_entry_gate"), dict):
                capital_gate = decision_context["capital_entry_gate"]
            gate_checks = capital_gate.get("checks") if isinstance(capital_gate.get("checks"), dict) else {}

            def _gate_stage(check_name: str) -> str:
                check = gate_checks.get(check_name)
                if isinstance(check, dict):
                    return "passed" if bool(check.get("allowed")) else "failed"
                if str(capital_gate.get("check") or "").lower() == check_name:
                    return "failed"
                return "not_evaluated"

            paper_position_id = sig.get("paper_position_id")
            paper_status = str(sig.get("paper_status") or "").upper().strip()
            research_only = bool(sig.get("research_only"))
            execution_eligible = bool(sig.get("capital_execution_eligible", False))
            is_open_paper = bool(paper_position_id and paper_status == "OPEN")
            is_stale = bool(not is_open_paper and (age_seconds is None or age_seconds > max_signal_age_seconds))

            if is_open_paper and research_only:
                decision_status = "open_research"
                reason = str(
                    sig.get("capital_execution_block_reason")
                    or capital_gate.get("reason")
                    or "Forward-paper research position is open; it is excluded from deployable Capital"
                )
            elif is_open_paper:
                decision_status = "open_capital"
                reason = str(
                    sig.get("entry_reason")
                    or capital_gate.get("reason")
                    or "Capital position is open"
                )
            elif age_seconds is None:
                decision_status = "invalid_timestamp"
                reason = "Signal has no valid creation timestamp and cannot enter Capital"
            elif is_stale:
                decision_status = "expired"
                reason = f"Signal expired after {int(age_seconds)}s; Capital accepts at most {max_signal_age_seconds}s"
            elif capital_gate and not bool(capital_gate.get("allowed")):
                decision_status = "blocked"
                reason = str(capital_gate.get("reason") or "Capital entry gate rejected this signal")
            elif research_only:
                decision_status = "research_only"
                reason = str(
                    sig.get("capital_execution_block_reason")
                    or "Research-only signal cannot open Capital"
                )
            elif bool(capital_gate.get("allowed")) and execution_eligible:
                decision_status = "eligible"
                reason = str(capital_gate.get("reason") or "All recorded Capital entry checks passed")
            elif score < min_signal_score:
                decision_status = "below_score"
                reason = f"Signal score {score:.0f} is below the Capital threshold {min_signal_score:.0f}"
            else:
                decision_status = "awaiting_validation"
                reason = "No complete Capital gate decision is recorded for this signal"

            raw_signal_rows.append(
                {
                    "source": "signal",
                    "signal_id": sig.get("id") or sig_id,
                    "ticker": ticker,
                    "direction": "LONG" if action == "BUY" else ("SHORT" if action == "SHORT" else ""),
                    "action": action,
                    "status": sig.get("status") or "Pending",
                    "decision_status": decision_status,
                    "capital_gate_check": capital_gate.get("check"),
                    "capital_gate_status": capital_gate.get("status"),
                    "gate_progress": {
                        "data": _gate_stage("data"),
                        "backtest": _gate_stage("backtest"),
                        "realized_performance": _gate_stage("realized_performance"),
                        "news": _gate_stage("news"),
                    },
                    "paper_position_id": paper_position_id,
                    "paper_status": sig.get("paper_status"),
                    "paper_opened_at": sig.get("paper_opened_at"),
                    "paper_capital_experiment": bool(sig.get("paper_capital_experiment")),
                    "research_only": research_only,
                    "capital_execution_eligible": execution_eligible,
                    "execution_outcome": (
                        "research_position_open"
                        if decision_status == "open_research"
                        else ("capital_position_open" if decision_status == "open_capital" else "not_executed")
                    ),
                    "created_at": str(created_at or ""),
                    "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
                    "max_signal_age_seconds": max_signal_age_seconds,
                    "is_stale": is_stale,
                    "score": round(score, 2),
                    "raw_score": round(float(sig.get("raw_signal_score", sig.get("raw_score", score)) or score), 2),
                    "entry": sig.get("entry") or sig.get("entry_price"),
                    "tp": sig.get("tp"),
                    "sl": sig.get("sl"),
                    "change_pct": row.get("change_pct", 0.0),
                    "volume_ratio": row.get("volume_ratio") or row.get("relative_volume"),
                    "rsi": sig.get("rsi"),
                    "macd_hist": sig.get("macd_hist"),
                    "trade_mode": sig.get("trade_mode") or sig.get("signal_mode"),
                    "risk_mode": sig.get("risk_mode"),
                    "strategy_source": sig.get("strategy_source") or "V1",
                    "strategy_type": sig.get("strategy_type") or sig.get("strategy_id") or "",
                    "qualified": decision_status == "eligible",
                    "reason": reason,
                    "ticker_learning": sig.get("ticker_learning"),
                }
            )

    # Capital presents one current decision per instrument. Historical opposite
    # directions remain in the Signals audit, but can never look like two
    # simultaneously actionable Capital entries.
    grouped_rows: Dict[str, List[Dict[str, Any]]] = {}
    for item in raw_signal_rows:
        grouped_rows.setdefault(str(item.get("ticker") or ""), []).append(item)

    signal_rows: List[Dict[str, Any]] = []
    stale_signal_count = 0
    conflict_signal_count = 0
    for ticker, candidates in grouped_rows.items():
        stale_signal_count += sum(1 for item in candidates if item.get("is_stale"))
        visible = [
            item
            for item in candidates
            if not item.get("is_stale")
            or item.get("decision_status") in {"open_capital", "open_research"}
        ]
        if not visible:
            continue
        visible.sort(
            key=lambda item: (
                item.get("decision_status") == "open_capital",
                item.get("decision_status") == "open_research",
                item.get("decision_status") == "eligible",
                float(item.get("age_seconds") or 0.0) * -1,
            ),
            reverse=True,
        )
        chosen = visible[0]
        active_directions = {str(item.get("direction") or "") for item in visible if item.get("direction")}
        if (
            len(active_directions) > 1
            and chosen.get("decision_status") not in {"open_capital", "open_research"}
        ):
            conflict_signal_count += 1
            chosen["decision_status"] = "conflict"
            chosen["qualified"] = False
            chosen["reason"] = f"Conflicting current directions for {ticker}: {', '.join(sorted(active_directions))}; no Capital entry allowed"
            chosen["conflicting_directions"] = sorted(active_directions)

        try:
            news_signals = summarize_ticker_news_signals(ticker) or {}
        except Exception:
            news_signals = {}
        headline_groups = news_signals.get("top_headlines") if isinstance(news_signals.get("top_headlines"), dict) else {}
        chosen["news_sentiment"] = (
            news_signals.get("sentiment_label")
            or news_signals.get("sentiment")
            or news_signals.get("sentiment_trend")
        )
        chosen["news_score"] = news_signals.get("score", news_signals.get("news_catalyst_score"))
        chosen["news_headlines"] = list((headline_groups.get("7d") or headline_groups.get("30d") or [])[:3])
        signal_rows.append(chosen)

    scanner_rows: List[Dict[str, Any]] = []
    try:
        from agents.scanner_agent import get_scanner_agent

        scanner_state = get_scanner_agent(config).get_state()
        hot = scanner_state.get("hot_tickers", []) if isinstance(scanner_state, dict) else []
        watchlist_symbols = set(ticker_map.keys())
        for item in hot[:20] if isinstance(hot, list) else []:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper().strip()
            if not symbol:
                continue
            if symbol.endswith("USDT") or symbol.endswith("USDC"):
                # Stablecoin itself (USDT/USDC with no base asset) is not a
                # tradeable pair. Pre-fix this produced "-USD" (empty base) — a
                # garbage ticker that polluted Home signals and failed price
                # fetches. Skip it (mirror F2/F3 guard in scanner/autonomous).
                base = symbol[:-4]
                ticker = f"{base}-USD" if base else ""
            else:
                ticker = normalize_market_ticker(symbol)
            ticker = str(ticker or "").upper().strip()
            if not ticker:
                continue
            change = float(item.get("change_pct", 0.0) or 0.0)
            scanner_rows.append(
                {
                    "source": "scanner",
                    "ticker": ticker,
                    "raw_symbol": symbol,
                    "direction": "LONG" if change >= 0 else "SHORT",
                    "change_pct": round(change, 2),
                    "price": item.get("price"),
                    "volume": item.get("volume"),
                    "provider": item.get("source") or item.get("exchange") or "scanner",
                    "in_watchlist": ticker in watchlist_symbols,
                    "watchlist_added": bool(item.get("watchlist_added")),
                    "qualified": ticker in watchlist_symbols,
                    "reason": "Added to watchlist; waiting for news/technical confirmation" if ticker in watchlist_symbols else "Scanner candidate; watchlist promotion pending",
                }
            )
    except Exception as exc:
        logger.debug(f"Capital scanner opportunities unavailable: {exc}")

    status_priority = {
        "open_capital": 6,
        "open_research": 5,
        "eligible": 4,
        "blocked": 3,
        "research_only": 2,
        "awaiting_validation": 1,
    }
    signal_rows.sort(
        key=lambda x: (status_priority.get(str(x.get("decision_status") or ""), 0), float(x.get("score", 0))),
        reverse=True,
    )
    scanner_rows.sort(key=lambda x: abs(float(x.get("change_pct", 0.0) or 0.0)), reverse=True)
    bot_enabled = bool(bot_cfg.get("enabled", False))
    execution_enabled = bool(execution_cfg.get("enabled", False))
    candidates_enabled = bool(execution_cfg.get("autonomous_candidates_enabled", False))

    open_positions = [
        item
        for item in (positions or [])
        if isinstance(item, dict) and str(item.get("status") or "").upper() == "OPEN"
    ]

    def _is_research_position(item: Dict[str, Any]) -> bool:
        return bool(item.get("research_only")) or str(item.get("position_type") or "").lower() == "research_cohort"

    research_positions = [item for item in open_positions if _is_research_position(item)]
    capital_positions = [item for item in open_positions if not _is_research_position(item)]
    paper_experiments = [item for item in capital_positions if bool(item.get("paper_capital_experiment"))]
    validated_capital_positions = [
        item for item in capital_positions if not bool(item.get("paper_capital_experiment"))
    ]
    position_summary = {
        "total_open": len(open_positions),
        "capital_open": len(capital_positions),
        "validated_capital_open": len(validated_capital_positions),
        "paper_experiment_open": len(paper_experiments),
        "research_open": len(research_positions),
    }

    def _passed_stage(item: Dict[str, Any], stage: str) -> bool:
        progress = item.get("gate_progress")
        return isinstance(progress, dict) and progress.get(stage) == "passed"

    gate_funnel = {
        "current_signals": len(signal_rows),
        "price_data_passed": sum(1 for item in signal_rows if _passed_stage(item, "data")),
        "faithful_oos_passed": sum(1 for item in signal_rows if _passed_stage(item, "backtest")),
        "realized_edge_passed": sum(
            1 for item in signal_rows if _passed_stage(item, "realized_performance")
        ),
        "news_ai_passed": sum(1 for item in signal_rows if _passed_stage(item, "news")),
        "capital_eligible": sum(
            1
            for item in signal_rows
            if item.get("decision_status") in {"eligible", "open_capital"}
        ),
        "paper_experiment_eligible": sum(
            1 for item in signal_rows if bool(item.get("paper_capital_experiment"))
        ),
        "capital_positions_open": len(capital_positions),
        "research_positions_open": len(research_positions),
    }

    blocker_groups: Dict[tuple[str, str, str], int] = {}
    for item in signal_rows:
        if item.get("decision_status") in {"eligible", "open_capital"}:
            continue
        check = str(item.get("capital_gate_check") or item.get("decision_status") or "pending")
        status = str(item.get("capital_gate_status") or item.get("decision_status") or "pending")
        reason = str(item.get("reason") or "Capital admission is pending")
        key = (check, status, reason)
        blocker_groups[key] = blocker_groups.get(key, 0) + 1
    blocker_summary = [
        {"check": check, "status": status, "reason": reason, "count": count}
        for (check, status, reason), count in sorted(
            blocker_groups.items(),
            key=lambda row: (-row[1], row[0][0], row[0][1], row[0][2]),
        )
    ]

    if validated_capital_positions:
        readiness_status = "capital_position_open"
        readiness_reason = "At least one evidence-qualified Capital position is open."
    elif paper_experiments:
        readiness_status = "paper_experiment_open"
        readiness_reason = (
            "A bounded Paper Auto Capital experiment is collecting prospective evidence."
        )
    elif research_positions:
        readiness_status = "collecting_research_evidence"
        readiness_reason = (
            "Research positions are open, but deployable Capital remains blocked until "
            "faithful V2 OOS, realised edge, and AI/News evidence pass."
        )
    elif gate_funnel["capital_eligible"]:
        readiness_status = "capital_eligible"
        readiness_reason = "A current signal passed its recorded Capital checks."
    elif signal_rows:
        readiness_status = "signal_blocked"
        readiness_reason = (
            blocker_summary[0]["reason"]
            if blocker_summary
            else "Current signals have not completed the Capital evidence funnel."
        )
    else:
        readiness_status = "waiting_for_signal"
        readiness_reason = "The engine is waiting for a fresh setup."

    return {
        "autonomous_enabled": bool(bot_enabled and execution_enabled and candidates_enabled),
        "bot_enabled": bot_enabled,
        "execution_agent_enabled": execution_enabled,
        "autonomous_candidates_enabled": candidates_enabled,
        "master_switch_source": "config.bot.enabled",
        "min_signal_score": min_signal_score,
        "max_signal_age_seconds": max_signal_age_seconds,
        "signal_summary": {
            "visible": len(signal_rows),
            "eligible": sum(1 for item in signal_rows if item.get("decision_status") == "eligible"),
            "open_capital": sum(1 for item in signal_rows if item.get("decision_status") == "open_capital"),
            "open_research": sum(1 for item in signal_rows if item.get("decision_status") == "open_research"),
            "open_paper": sum(
                1
                for item in signal_rows
                if item.get("decision_status") in {"open_capital", "open_research"}
            ),
            "blocked": sum(1 for item in signal_rows if item.get("decision_status") in {"blocked", "research_only", "awaiting_validation", "below_score", "invalid_timestamp", "conflict"}),
            "expired_hidden": stale_signal_count,
            "conflicts": conflict_signal_count,
        },
        "position_summary": position_summary,
        "gate_funnel": gate_funnel,
        "blocker_summary": blocker_summary,
        "readiness": {
            "status": readiness_status,
            "reason": readiness_reason,
        },
        "last_cycle": _compact_autonomous_cycle_status(),
        "pipeline": [
            "Fresh signal + verified execution price",
            "Exact V2 identity + faithful walk-forward OOS",
            "Positive realised edge for the same strategy and direction",
            "Source-backed AI News + trade review",
            "Bounded paper Capital position",
        ],
        "signals": signal_rows[:12],
        "scanner": scanner_rows[:12],
    }


_capital_price_map: Dict[str, Any] = {}
_capital_price_map_lock = threading.Lock()


def _enrich_open_signals_current_price(signals_data: Dict[str, Any]) -> None:
    """Enrich open signals with a live current_price + current_pnl_pct.

    Open scanner signals share tickers with open paper positions, whose live prices the
    P&L tracker maintains in cm._price_cache and _build_capital_payload publishes into
    _capital_price_map. Without this the Signals tab showed 'Waiting for live price'."""
    try:
        if not isinstance(signals_data, dict):
            return
        open_sigs = signals_data.get("open", {})
        items = list(open_sigs.values()) if isinstance(open_sigs, dict) else (open_sigs if isinstance(open_sigs, list) else [])
        if not items:
            return
        now_ts = time.time()
        pmap: Dict[str, float] = {}
        # 1) continuous P&L-tracker price cache (raw; allow up to 120s)
        try:
            cm = get_capital_manager()
            with cm._price_cache_lock:
                for sym, val in cm._price_cache.items():
                    try:
                        price, ts = val
                        if price and float(price) > 0 and (now_ts - float(ts)) < 120:
                            pmap[_canonical_ticker_key(sym)] = float(price)
                    except Exception:
                        continue
        except Exception:
            pass
        # 2) last capital build price map
        with _capital_price_map_lock:
            for tk, p in _capital_price_map.items():
                if tk == "__ts__":
                    continue
                pmap.setdefault(tk, float(p))
        if not pmap:
            return
        for sig in items:
            if not isinstance(sig, dict):
                continue
            tk = _canonical_ticker_key(sig.get("ticker"))
            price = pmap.get(tk)
            if not price or price <= 0:
                continue
            sig["current_price"] = price
            sig["current_price_live"] = True
            entry = _safe_float(sig.get("entry") if sig.get("entry") is not None else sig.get("entry_price"), 0.0)
            if entry > 0:
                direction = str(sig.get("action") or sig.get("type") or sig.get("direction") or "BUY").upper()
                pnl = ((price - entry) / entry * 100.0) if direction in ("BUY", "LONG") else ((entry - price) / entry * 100.0)
                sig["current_pnl_pct"] = round(pnl, 2)
    except Exception:
        pass


_EQUITY_POINT_MIN_INTERVAL_SECONDS = 15.0
_equity_point_lock = threading.Lock()
_equity_point_last = {"ts": 0.0}


def _capital_position_is_manual(position: Dict[str, Any]) -> bool:
    """Same predicate the manager uses when it decides the evidence marker."""
    from core.capital_manager import CapitalManager

    try:
        return bool(CapitalManager._is_manual_position(position))
    except Exception:
        return str((position or {}).get("position_type") or "").lower() == "manual"


def _capital_read_snapshot(cm, ledger_limit: int = 100) -> Dict[str, Any]:
    """Read positions, ledger, accounting and cash with ONE storage sync.

    Every public CapitalManager getter re-reads and re-parses the complete
    capital blob from SQLite (11.7 MB / ~0.3 s measured 2026-11-15). The read
    model used to call seven of them per build, so a single /api/state or
    /api/cockpit build spent ~2.4 s (4-14 s under agent load) inside
    ``cm._lock``, serialising every concurrent UI request behind it. Taking one
    consistent snapshot under the lock removes six of the seven re-reads and
    also guarantees positions/accounting/ledger describe the same instant.

    Test doubles and legacy managers keep the public-getter path.
    """
    from core.capital_manager import CapitalManager

    if isinstance(cm, CapitalManager):
        try:
            with cm._lock:
                # Read-only sync: an unchanged capital row is not parsed again
                # (CapitalManager._sync_for_read). Rows are copied so the
                # annotations added below never reach the manager's objects.
                cm._sync_for_read()
                positions = [dict(p) if isinstance(p, dict) else p for p in cm.positions]
                ledger = [
                    cm._enrich_ledger_entry_unlocked(entry)
                    for entry in cm.history[-max(1, int(ledger_limit)):]
                    if isinstance(entry, dict)
                ]
                accounting = dict(cm._capital_accounting_unlocked())
                raw_cash = float(cm.balance or 0.0)
            return {
                "positions": positions,
                "ledger": ledger,
                "accounting": accounting,
                "raw_cash_balance": raw_cash,
                "deployable_cash_balance": float(
                    accounting.get("deployable_cash_balance", raw_cash) or 0.0
                ),
                "single_sync": True,
            }
        except Exception as exc:
            logger.warning("Capital single-sync snapshot failed; using getters: %s", exc)

    positions = cm.get_positions()
    ledger = cm.get_ledger(limit=ledger_limit)
    accounting_method = getattr(cm, "get_capital_accounting", None)
    accounting = accounting_method() if callable(accounting_method) else {}
    if not isinstance(accounting, dict):
        accounting = {}
    raw_cash = float(cm.get_balance() or 0.0)
    return {
        "positions": positions,
        "ledger": ledger,
        "accounting": accounting,
        "raw_cash_balance": raw_cash,
        "deployable_cash_balance": resolve_deployable_balance(cm),
        "single_sync": False,
    }


def _equity_point_due() -> bool:
    """Throttle the equity-curve write that rides along the read model.

    ``record_equity_point`` re-syncs the whole capital blob and rewrites the
    equity history; the curve itself is deduplicated per UTC minute, so one
    write every 15 s keeps the latest point current without paying a second
    full blob parse on every UI poll.
    """
    now = time.monotonic()
    with _equity_point_lock:
        if (now - float(_equity_point_last["ts"] or 0.0)) < _EQUITY_POINT_MIN_INTERVAL_SECONDS and _equity_point_last["ts"]:
            return False
        _equity_point_last["ts"] = now
        return True


def _build_capital_payload(
    tickers_list: List[Dict[str, Any]],
    signals_data: Optional[Dict[str, Any]] = None,
    fast_only: bool = False,
    include_opportunities: bool = True,
    include_decisions: bool = True,
) -> Dict[str, Any]:
    cm = get_capital_manager()
    # ONE storage sync for the whole read model (was 7 full re-reads of the
    # multi-MB capital blob per build; see _capital_read_snapshot).
    capital_snapshot = _capital_read_snapshot(cm, ledger_limit=100)
    positions_raw = capital_snapshot["positions"]
    ticker_prices: Dict[str, float] = {}
    ticker_price_meta: Dict[str, Dict[str, Any]] = {}
    for row in tickers_list or []:
        if not isinstance(row, dict):
            continue
        ticker = _canonical_ticker_key(row.get("ticker"))
        price = _safe_float(row.get("price"), 0.0)
        if ticker and price > 0:
            ticker_prices[ticker] = price
            ticker_price_meta[ticker] = {
                "source": str(row.get("price_source") or row.get("source") or "state_cache"),
                "last_update": row.get("last_update") or row.get("timestamp") or row.get("ts"),
            }

    # Capital positions should prefer the freshest local live-price cache over
    # the slower state payload so the tab does not fall back to entry prices.
    try:
        cache_path = DATA_DIR / "live_prices_cache.json"
        if cache_path.exists():
            cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
            cached_prices = cached_payload.get("prices", {}) if isinstance(cached_payload, dict) else {}
            if isinstance(cached_prices, dict):
                for raw_ticker, cached_row in cached_prices.items():
                    if not isinstance(cached_row, dict):
                        continue
                    ticker = _canonical_ticker_key(raw_ticker)
                    price = _safe_float(cached_row.get("price"), 0.0)
                    if ticker and price > 0:
                        ticker_prices[ticker] = price
                        ticker_price_meta[ticker] = {
                            "source": "live_prices_cache",
                            "last_update": cached_row.get("last_update") or cached_payload.get("ts"),
                        }
    except Exception as e:
        logger.debug(f"Capital live price cache unavailable: {e}")

    # The engine's P&L tracker runs in a separate process. It persists every
    # verified quote in SQLite so this API process can publish current Capital
    # prices without starting another remote quote fanout. Apply it last because
    # it is the authoritative, continuously refreshed position-price source.
    try:
        from core.live_price_cache import get_live_prices

        position_tickers = [
            _canonical_ticker_key((pos or {}).get("ticker") or (pos or {}).get("symbol"))
            for pos in positions_raw or []
            if isinstance(pos, dict) and str(pos.get("status", "")).upper() == "OPEN"
        ]
        for raw_ticker, cached_row in get_live_prices(position_tickers).items():
            ticker = _canonical_ticker_key(raw_ticker)
            price = _safe_float(cached_row.get("price"), 0.0)
            if ticker and price > 0:
                ticker_prices[ticker] = price
                ticker_price_meta[ticker] = {
                    "source": str(cached_row.get("source") or "capital_price_cache"),
                    "last_update": cached_row.get("timestamp"),
                }
    except Exception as e:
        logger.debug(f"Capital SQLite live price cache unavailable: {e}")

    # eToro Bulk Live Rates for open positions (fast 1-shot network call for all positions, ~150ms)
    try:
        from core.etoro_client import get_etoro_client
        et = get_etoro_client()
        if et.is_available():
            open_pos_tickers = []
            entry_by_canon = {}
            for pos in positions_raw or [] if isinstance(positions_raw, list) else []:
                if isinstance(pos, dict) and str(pos.get("status", "")).upper() == "OPEN":
                    tk = _canonical_ticker_key(pos.get("ticker") or pos.get("symbol"))
                    if tk:
                        open_pos_tickers.append(tk)
                        ep = _safe_float(pos.get("entry_price"), 0.0)
                        if ep > 0:
                            entry_by_canon[tk] = ep

            iids = []
            id_to_canon = {}
            id_to_mult = {}
            for raw_sym in open_pos_tickers:
                sym = raw_sym
                for suf in ("-USD", "-USDT", "USDT", "USD"):
                    if sym.endswith(suf) and len(sym) > len(suf):
                        sym = sym[:-len(suf)]
                        break
                # Cache-only UI builds must not start instrument-search
                # requests (2 calls x 10 s timeout per unmapped symbol).
                inst = et.search_instrument(sym, cache_only=bool(fast_only))
                if inst and inst.get("instrument_id"):
                    iid = int(inst["instrument_id"])
                    iids.append(iid)
                    id_to_canon[iid] = raw_sym
                    id_to_mult[iid] = float(inst.get("unit_multiplier") or 1.0)
            if iids:
                from core.etoro_client import rate_age_seconds, resolve_rate_price

                rates = et.get_live_rates(iids, timeout=2.5 if fast_only else 10)
                now_dt = datetime.now(timezone.utc)
                now_iso = now_dt.isoformat()
                now_ts = time.time()
                for iid, r in rates.items():
                    canon_tk = id_to_canon.get(iid)
                    if not canon_tk:
                        continue
                    mult = id_to_mult.get(iid, 1.0)
                    # lastExecution can be an old trade (thin book / closed
                    # market): use the live book mid when the last execution
                    # is outside it instead of presenting it as the price.
                    raw_last, _rate_basis = resolve_rate_price(r)
                    raw_last = _safe_float(raw_last)
                    raw_bid = _safe_float(r.get("bid")) or raw_last
                    raw_ask = _safe_float(r.get("ask")) or raw_last
                    if not raw_last or raw_last <= 0:
                        continue
                    # Quote time is eToro's own, not "now": a Friday close read
                    # on Sunday must not be stamped (and badged) as live.
                    quote_age = rate_age_seconds(r, now_dt)
                    quote_is_fresh = quote_age is None or quote_age <= 120.0
                    quote_iso = now_iso
                    if quote_age is not None:
                        quote_iso = datetime.fromtimestamp(
                            now_dt.timestamp() - quote_age, tz=timezone.utc
                        ).isoformat()
                    if not quote_is_fresh and ticker_prices.get(canon_tk, 0.0) > 0:
                        # A stale eToro rate never replaces a price another
                        # verified source already supplied.
                        continue
                    last_p = raw_last / mult
                    bid_p = raw_bid / mult
                    ask_p = raw_ask / mult

                    # Sanity check: reject extreme order-of-magnitude discrepancies
                    entry_p = entry_by_canon.get(canon_tk)
                    if entry_p and entry_p > 0 and last_p > 0:
                        ratio = last_p / entry_p
                        if ratio > 30.0 or ratio < 0.03:
                            logger.warning(
                                f"Rejected eToro quote magnitude mismatch for {canon_tk}: quote={last_p} vs entry={entry_p} (ratio={ratio:.2f})"
                            )
                            continue

                    ticker_prices[canon_tk] = last_p
                    ticker_price_meta[canon_tk] = {
                        "source": "eToro_live_rates" if quote_is_fresh else "eToro_last_rate_stale",
                        "last_update": quote_iso,
                    }
                    if not quote_is_fresh:
                        # Stale book: display-only, never a VERIFIED quote.
                        continue
                    # Warm capital execution cache
                    with _capital_exec_quote_cache_lock:
                        _capital_exec_quote_cache[f"{canon_tk}:BUY"] = (now_ts, {
                            "execution_price": ask_p,
                            "source": "eToro_bid_ask",
                            "price_timestamp": now_iso,
                            "execution_price_quality": {"level": "VERIFIED", "is_valid": True},
                        })
                        _capital_exec_quote_cache[f"{canon_tk}:SELL"] = (now_ts, {
                            "execution_price": bid_p,
                            "source": "eToro_bid_ask",
                            "price_timestamp": now_iso,
                            "execution_price_quality": {"level": "VERIFIED", "is_valid": True},
                        })
    except Exception as e:
        logger.debug(f"eToro bulk capital rates prefetch error: {e}")

    def _is_recent_price_ts(value: Any, max_age_seconds: float = 45.0) -> bool:
        try:
            if isinstance(value, (int, float)):
                return (time.time() - float(value)) <= max_age_seconds
            if isinstance(value, str) and value.strip():
                dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() <= max_age_seconds
        except Exception:
            return False
        return False

    def _apply_live_pnl(pos: Dict[str, Any]) -> None:
        if not isinstance(pos, dict):
            return
        if str(pos.get("status", "")).upper() != "OPEN":
            return
        current_price = _safe_float(pos.get("current_price"), 0.0)
        entry_price = _safe_float(pos.get("entry_price"), 0.0)
        qty = _safe_float(pos.get("qty") or pos.get("quantity"), 0.0)
        if current_price <= 0 or entry_price <= 0 or qty <= 0:
            return
        direction = str(pos.get("direction", "LONG")).upper()
        gross_pnl = (current_price - entry_price) * qty if direction == "LONG" else (entry_price - current_price) * qty
        notional = entry_price * qty

        # Deduct entry commission (already paid from balance)
        entry_commission = 0.0
        if "commission_usd" in pos:
            entry_commission = _safe_float(pos.get("commission_usd"), 0.0)
        elif "fees_estimate" in pos:
            entry_commission = _safe_float(pos.get("fees_estimate"), 0.0)

        # Estimate exit commission based on stored rate or derived from entry data.
        # A legacy/manual position may not carry enough fee information for a
        # net figure.  Its gross PnL remains factual and useful, but must not be
        # relabelled as a net result with a made-up commission rate.
        net_pnl_available = True
        exit_commission_rate = _safe_float(pos.get("commission_rate"), 0.0)
        if exit_commission_rate <= 0:
            usd_size = _safe_float(pos.get("usd_size"), 0.0)
            if usd_size > 0 and entry_commission > 0 and "commission_usd" in pos:
                exit_commission_rate = entry_commission / usd_size
            elif "fees_bps" in pos:
                exit_commission_rate = _safe_float(pos.get("fees_bps"), 0.0) / 10000.0
            else:
                net_pnl_available = False
                pos["unrealized_pnl_cost_warning"] = "Missing recorded commission rate; net PnL is unavailable."
        leverage = max(1, int(pos.get("leverage", 1) or 1))

        pos["current_pnl"] = round(gross_pnl, 4)
        pos["current_pnl_pct"] = round((gross_pnl / notional) * 100.0, 4) if notional > 0 else 0.0
        pos["unrealized_pnl_gross"] = round(gross_pnl, 4)
        if not net_pnl_available:
            pos.pop("unrealized_pnl", None)
            pos.pop("unrealized_pnl_pct", None)
            pos.pop("unrealized_pnl_pct_margin", None)
            return

        exit_notional_est = current_price * qty
        exit_commission_est = exit_notional_est * exit_commission_rate
        net_pnl = gross_pnl - entry_commission - exit_commission_est
        pos["unrealized_pnl"] = round(net_pnl, 4)
        pos["unrealized_pnl_pct"] = round((net_pnl / notional) * 100.0, 4) if notional > 0 else 0.0
        pos["unrealized_pnl_pct_margin"] = round(pos["unrealized_pnl_pct"] * leverage, 4)

    def _apply_explainability(pos: Dict[str, Any]) -> None:
        explainability = summarize_position_explainability(pos)
        pos["explainability"] = explainability
        pos["entry_reason"] = pos.get("entry_reason") or explainability.get("reason", "")
        pos["strategy_type"] = pos.get("strategy_type") or explainability.get("strategy_type", "")
        pos["score_snapshot"] = explainability.get("scoring", {})
        pos["sizing_context"] = explainability.get("sizing", {})
        pos["risk_context"] = explainability.get("risk", {})
        sizing = pos.get("sizing_context") if isinstance(pos.get("sizing_context"), dict) else {}
        if not pos.get("leverage") and sizing.get("leverage"):
            pos["leverage"] = sizing.get("leverage")
        if not pos.get("usd_size") and sizing.get("usd_size"):
            pos["usd_size"] = sizing.get("usd_size")
        if not pos.get("margin_used") and sizing.get("invested_amount"):
            pos["margin_used"] = sizing.get("invested_amount")

    positions: List[Dict[str, Any]] = []
    for pos in positions_raw or []:
        if not isinstance(pos, dict):
            continue
        if str(pos.get("status", "")).upper() != "OPEN":
            continue
        enriched = dict(pos)
        if not enriched.get("source_signal_id") and enriched.get("signal_id"):
            enriched["source_signal_id"] = enriched.get("signal_id")
        ticker = _canonical_ticker_key(enriched.get("ticker") or enriched.get("symbol"))
        live_price = ticker_prices.get(ticker, 0.0)
        if live_price > 0:
            price_meta = ticker_price_meta.get(ticker, {})
            enriched["current_price"] = live_price
            enriched["current_price_live"] = _is_recent_price_ts(price_meta.get("last_update"))
            enriched["current_price_source"] = str(price_meta.get("source") or "state_cache")
            if price_meta.get("last_update") is not None:
                enriched["current_price_timestamp"] = str(price_meta.get("last_update"))
            _apply_live_pnl(enriched)
        _apply_explainability(enriched)
        positions.append(enriched)

    def _verified_exec_price(exec_data: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(exec_data, dict):
            return False
        source = str(exec_data.get("source") or "").strip().lower()
        if source == "fallback":
            return False
        quality_meta = exec_data.get("execution_price_quality") or {}
        if not isinstance(quality_meta, dict):
            return False
        level = str(quality_meta.get("level") or quality_meta.get("quality") or "").strip().upper()
        if quality_meta.get("is_valid") is False:
            return False
        try:
            return level == "VERIFIED" and float(exec_data.get("execution_price") or 0.0) > 0
        except Exception:
            return False

    # Enrich open positions with live current_price from capital_manager real-time quotes.
    # Skip in fast_only mode (lean polls) to avoid blocking the state endpoint.
    #
    # Open positions must always reflect a live current_price so P&L is correct — the
    # cache alone is not reliably fresh for every held ticker. The cost is the per-position
    # remote quote, which previously ran serially (≈1s x N → 20s+ at 22 positions). We keep
    # the live enrichment but fan the remote fetches across a bounded pool so the Capital tab
    # and non-lean /api/state stay responsive.
    if not fast_only:
        def _apply_exec_quote(pos: Dict[str, Any], closing_side: str, exec_data: Optional[Dict[str, Any]]) -> None:
            try:
                if not isinstance(exec_data, dict):
                    pos["current_price_live"] = False
                    return
                live_price = float(exec_data.get("execution_price") or 0.0)
                if _verified_exec_price(exec_data):
                    pos["current_price"] = live_price
                    pos["current_price_live"] = True
                    pos["current_price_side"] = closing_side
                    pos["current_price_source"] = str(exec_data.get("source") or "")
                    pos["current_price_timestamp"] = str(exec_data.get("price_timestamp") or "")
                    pos["current_price_quality"] = exec_data.get("execution_price_quality") or {}
                    _apply_live_pnl(pos)
                else:
                    pos["current_price_live"] = False
                    pos["current_price_source"] = str(exec_data.get("source") or "unavailable")
            except Exception:
                pos["current_price_live"] = False
            # NOTE: no _apply_explainability here — it already ran for every
            # position in the enrichment loop above, and explainability fields
            # (entry_reason/strategy_type/score_snapshot/sizing/risk/leverage/
            # usd_size/margin_used) do not depend on current_price, which is the
            # only thing this function mutates. Re-running it per position per
            # poll was ~3s of redundant work on the Capital path (warm AND cold).

        try:
            jobs: List[tuple] = []
            for pos in positions:
                if not isinstance(pos, dict):
                    continue
                if str(pos.get("status", "")).upper() != "OPEN":
                    continue
                ticker = _canonical_ticker_key(pos.get("ticker") or pos.get("symbol"))
                if not ticker:
                    continue
                direction = str(pos.get("direction", "LONG")).upper()
                closing_side = "SELL" if direction == "LONG" else "BUY"
                jobs.append((pos, ticker, closing_side))

            def _fetch(job):
                _pos, _ticker, _side = job
                try:
                    return job, _get_capital_exec_quote_cached(
                        cm, _ticker, _side, allow_remote=True
                    )
                except Exception:
                    return job, None

            # Live quotes hit the network per position; fetching N positions serially made
            # the Capital tab block ~1s x N (20s+ at 22 positions). Fan the remote fetches out
            # across a bounded pool, then apply results serially so position dict mutations
            # stay single-threaded.
            #
            # DEADLINE: a few obscure tickers retry with backoff and can take 10s+ each.
            # ex.map blocked until the SLOWEST job finished (~15s uncontended, 34-50s under
            # poll contention), blowing past the frontend's 30s timeout -> cache fallback ->
            # the Capital tab appeared frozen. as_completed + a hard deadline caps the wait:
            # quotes that resolve in budget are applied; the rest keep their cached/entry
            # price (zero-price fallback below). shutdown(wait=False, cancel_futures=True)
            # stops us from joining the still-running worker threads.
            _deadline_s = float(os.getenv("MS_CAPITAL_QUOTE_DEADLINE_SECONDS", "8") or 8)
            results: List[tuple] = []
            if len(jobs) > 1:
                from concurrent.futures import (
                    ThreadPoolExecutor,
                    as_completed,
                    TimeoutError as _FuturesTimeout,
                )

                ex = ThreadPoolExecutor(
                    max_workers=min(8, len(jobs)), thread_name_prefix="capquote"
                )
                try:
                    futures = {ex.submit(_fetch, job): job for job in jobs}
                    try:
                        for fut in as_completed(futures, timeout=_deadline_s):
                            try:
                                results.append(fut.result())
                            except Exception:
                                pass
                    except _FuturesTimeout:
                        # Deadline reached: apply whatever resolved, abandon the rest.
                        pass
                except Exception:
                    # Pool itself failed to run: fall back to serial (rare).
                    ex.shutdown(wait=False, cancel_futures=True)
                    results = [_fetch(job) for job in jobs]
                else:
                    ex.shutdown(wait=False, cancel_futures=True)
            else:
                results = [_fetch(job) for job in jobs]

            for (pos, _ticker, closing_side), exec_data in results:
                _apply_exec_quote(pos, closing_side, exec_data)

            # ZERO-PRICE FALLBACK: any OPEN position still without a usable current_price
            # (live quote timed out / failed AND no cached price) renders "$0.0000" on the
            # card, which looked like the Capital tab was broken. Use its entry price so the
            # card shows a real number + CACHE and PnL=0 (current == entry) instead.
            for pos in positions:
                if not isinstance(pos, dict):
                    continue
                if str(pos.get("status", "")).upper() != "OPEN":
                    continue
                if _safe_float(pos.get("current_price"), 0.0) > 0:
                    continue
                entry_price = _safe_float(pos.get("entry_price"), 0.0)
                if entry_price > 0:
                    pos["current_price"] = entry_price
                    pos["current_price_live"] = False
                    pos["current_price_source"] = str(pos.get("current_price_source") or "entry_price_fallback")
                    _apply_live_pnl(pos)
        except Exception:
            pass

    recent_ledger = capital_snapshot["ledger"]
    capital_history = [
        entry
        for entry in recent_ledger
        if str((entry or {}).get("account_scope") or "").lower() == "capital"
    ]

    # Publish {ticker: current_price} so open signals (same tickers as positions) can
    # show a live price + P&L without a second fetch.
    try:
        pmap = {}
        for p in positions:
            if not isinstance(p, dict):
                continue
            tk = _canonical_ticker_key(p.get("ticker") or p.get("symbol"))
            cp = _safe_float(p.get("current_price"), 0.0)
            if tk and cp > 0:
                pmap[tk] = cp
        if pmap:
            with _capital_price_map_lock:
                _capital_price_map.update(pmap)
                _capital_price_map["__ts__"] = time.time()
    except Exception:
        pass

    # Record the exact equity represented by this response. Fast polls use the
    # same verified shared-cache / entry fallback prices shown on the cards, so
    # the curve's latest snapshot stays aligned with the Portfolio Equity KPI
    # without introducing any remote quote calls. The manager dedups per minute.
    try:
        eq_prices: Dict[str, float] = {}
        for p in positions:
            if isinstance(p, dict) and str(p.get("status", "")).upper() == "OPEN":
                cp = _safe_float(p.get("current_price"), 0.0)
                if cp > 0:
                    eq_prices[str(p.get("ticker") or p.get("symbol") or "")] = cp
        # The real manager re-reads the whole capital blob for every point, so
        # ride-along writes are throttled (the curve is per-minute anyway).
        if not capital_snapshot.get("single_sync") or _equity_point_due():
            cm.record_equity_point(eq_prices)
    except Exception:
        pass

    all_positions = positions_raw
    research_positions = [p for p in positions if is_research_position(p)]
    capital_positions = [p for p in positions if not is_research_position(p)]
    accounting = dict(capital_snapshot.get("accounting") or {})
    raw_cash_balance = float(capital_snapshot.get("raw_cash_balance") or 0.0)
    accounting.setdefault("raw_cash_balance", raw_cash_balance)
    accounting.setdefault(
        "deployable_cash_balance",
        float(capital_snapshot.get("deployable_cash_balance") or 0.0),
    )
    ledger_scope_window = list(recent_ledger)
    ledger_scope_window_start = str(accounting.get("last_reset_at") or "").strip()
    if ledger_scope_window_start:
        try:
            reset_dt = datetime.fromisoformat(ledger_scope_window_start.replace("Z", "+00:00"))
            if reset_dt.tzinfo is None:
                reset_dt = reset_dt.replace(tzinfo=timezone.utc)
            reset_dt = reset_dt.astimezone(timezone.utc)

            def _is_post_reset(entry: Dict[str, Any]) -> bool:
                try:
                    entry_dt = datetime.fromisoformat(
                        str((entry or {}).get("timestamp") or "").replace("Z", "+00:00")
                    )
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                    return entry_dt.astimezone(timezone.utc) >= reset_dt
                except Exception:
                    return False

            ledger_scope_window = [
                entry for entry in recent_ledger if _is_post_reset(entry)
            ]
        except Exception:
            ledger_scope_window_start = ""
    research_unrealized_pnl = sum(
        _safe_float(p.get("unrealized_pnl", p.get("current_pnl")), 0.0)
        for p in research_positions
    )
    accounting = {
        **dict(accounting or {}),
        "capital_open_positions": len(capital_positions),
        "research_open_positions": len(research_positions),
        "research_unrealized_pnl": round(research_unrealized_pnl, 6),
        "recent_capital_ledger_entries": sum(
            1
            for entry in ledger_scope_window
            if str((entry or {}).get("account_scope") or "").lower() == "capital"
        ),
        "recent_research_ledger_entries": sum(
            1
            for entry in ledger_scope_window
            if str((entry or {}).get("account_scope") or "").lower() == "research"
        ),
        "recent_unclassified_ledger_entries": sum(
            1
            for entry in ledger_scope_window
            if str((entry or {}).get("account_scope") or "").lower() == "unclassified"
        ),
        "ledger_scope_window_start": ledger_scope_window_start,
        "ledger_reconciliation_status": (
            "reconciled"
            if all(
                str((entry or {}).get("account_scope") or "").lower()
                in {"capital", "research"}
                for entry in ledger_scope_window
            )
            else "unclassified_entries_present"
        ),
    }

    # Capital performance must exclude forward-paper research cohorts.
    closed_positions = [
        p for p in all_positions
        if str(p.get("status", "")).upper() == "CLOSED"
        and not is_research_position(p)
    ]
    # A trade whose exit was delayed by an outage (evidence_excluded) stays in
    # the ledger, cash and equity but is not strategy evidence: it is left out
    # of the win rate and reported under excluded_trades.
    evidence_closed = [p for p in closed_positions if p.get("evidence_excluded") is not True]
    total_closed = len(evidence_closed)
    total_wins = sum(1 for p in evidence_closed if float(p.get("pnl", 0.0) or 0.0) > 0)
    win_rate = round((total_wins / total_closed * 100.0) if total_closed > 0 else 0.0, 1)

    # A bare win rate is unreadable without its sample, period and break-even
    # bar; a stock "current price" quoted while the US session is closed is not
    # a tradable price, and a position past its target then waits for the open.
    win_rate_summary: Dict[str, Any] = {}
    try:
        from core.portfolio.reporting import position_market_state, summarize_closed_trades

        win_rate_summary = summarize_closed_trades(closed_positions)
        for p in positions:
            if isinstance(p, dict) and str(p.get("status", "")).upper() == "OPEN":
                p["market_state"] = position_market_state(p)
    except Exception as exc:
        logger.debug("Capital reporting annotations unavailable: %s", exc)

    # Per open position: since when its exit has been due (the fact the
    # evidence rule measures the exit delay from), and a validation score that
    # was never measured as null + "unavailable" instead of a number.
    try:
        from core.portfolio.evidence import (
            exit_delay_exclusion_hours,
            exit_due_since,
            validation_fields,
        )

        evidence_config = get_shared_config()
        exclusion_hours = exit_delay_exclusion_hours(evidence_config)
        try:
            scanner_stop_minutes = float(
                (getattr(evidence_config, "bot", None) or {}).get("scanner_time_stop_minutes", 0) or 0
            )
        except (TypeError, ValueError, AttributeError):
            scanner_stop_minutes = 0.0
        evidence_now = datetime.now(timezone.utc)
        for p in positions:
            if not isinstance(p, dict) or str(p.get("status", "")).upper() != "OPEN":
                continue
            p.update(validation_fields(p))
            p.setdefault("exit_condition_since", None)
            # The moment the evidence rule measures the exit delay from (the
            # same function decides the exclusion when the position closes).
            due = (
                None
                if _capital_position_is_manual(p)
                else exit_due_since(p, scanner_time_stop_minutes=scanner_stop_minutes)
            )
            if due is not None and due[0] <= evidence_now:
                delay_hours = (evidence_now - due[0]).total_seconds() / 3600.0
                p["exit_due_since"] = due[0].isoformat()
                p["exit_due_basis"] = due[1]
                p["exit_delay_hours"] = round(delay_hours, 2)
                p["evidence_exclusion_expected"] = bool(delay_hours > exclusion_hours)
            else:
                p["exit_due_since"] = None
                p["exit_due_basis"] = None
                p["exit_delay_hours"] = None
                p["evidence_exclusion_expected"] = False
            p["evidence_exclusion_threshold_hours"] = exclusion_hours
            market_state = p.get("market_state")
            if isinstance(market_state, dict):
                market_state["exit_condition_since"] = p.get("exit_condition_since")
                market_state["exit_condition"] = p.get("exit_condition")
                market_state["exit_due_since"] = p.get("exit_due_since")
    except Exception as exc:
        logger.debug("Capital exit-condition annotations unavailable: %s", exc)

    capital_save_health: Dict[str, Any] = {}
    try:
        from core.capital_manager import read_capital_save_health

        capital_save_health = read_capital_save_health(cm)
    except Exception as exc:
        logger.debug("Capital save health unavailable: %s", exc)

    equity_curve = cm.get_equity_curve(limit=500)
    equity_curve_meta: Dict[str, Any] = {}
    try:
        from core.portfolio.equity_history import describe_equity_curve

        equity_curve_meta = describe_equity_curve(equity_curve)
    except Exception as exc:
        logger.debug("Capital equity-curve description unavailable: %s", exc)

    # Attach the CURRENT per-ticker learning/safety state to each open position so the UI
    # can show a small chip (why this ticker is gated/boosted). Derived from closed trades
    # by the same pure function the execution agent uses; skipped on lean polls.
    if not fast_only:
        try:
            from core.ticker_learning import get_ticker_adjustment

            exec_cfg = (get_shared_config().agents or {}).get("execution", {}) or {}
            for p in positions:
                if not isinstance(p, dict) or str(p.get("status", "")).upper() != "OPEN":
                    continue
                adj = get_ticker_adjustment(
                    p.get("ticker") or p.get("symbol"),
                    p.get("direction"),
                    closed_positions,
                    exec_cfg,
                )
                p["ticker_learning"] = {
                    "streak_type": adj.get("streak_type"),
                    "streak_len": adj.get("streak_len"),
                    "action": adj.get("action"),
                    "size_mult": adj.get("size_mult"),
                    "min_score_required": adj.get("min_score_required"),
                    "leverage_bonus": adj.get("leverage_bonus"),
                    "rr_mult": adj.get("rr_mult"),
                    "win_rate": adj.get("win_rate"),
                    "cooldown_until": adj.get("cooldown_until"),
                    "reason": adj.get("reason"),
                }
        except Exception:
            pass

    return {
        "balance": float(accounting.get("deployable_cash_balance", raw_cash_balance) or 0.0),
        "raw_cash_balance": float(accounting.get("raw_cash_balance", raw_cash_balance) or 0.0),
        "accounting": accounting,
        "history": capital_history,
        "positions": positions,
        "capital_positions": capital_positions,
        "research_positions": research_positions,
        # The Capital UI now has one unified history card. Return the complete
        # reconciled window so it does not silently lose older research rows.
        "ledger": recent_ledger,
        # Tiered history (daily -> hourly -> per minute), each point tagged with
        # its resolution; equity_curve_meta.segments says which stretch is which.
        "equity_curve": equity_curve,
        "equity_curve_meta": equity_curve_meta,
        "win_rate": win_rate,
        "total_closed_trades": total_closed,
        "win_rate_summary": win_rate_summary,
        "excluded_trades": win_rate_summary.get("excluded_trades") or {"count": 0, "total_pnl_usd": 0.0, "net_pnl": 0.0, "reasons": {}},
        # System Health reads save_health.status ("ok" | "failing" | "unknown").
        "save_health": capital_save_health,
        "decisions": _compact_trade_audit(get_trade_audit(limit=20)) if include_decisions else [],
        "opportunities": (
            _build_capital_opportunities(tickers_list, signals_data, positions)
            if include_opportunities
            else {}
        ),
    }


def _uses_yahoo_futures_price(ticker: str) -> bool:
    return "=F" in (normalize_ticker(ticker, provider="yahoo") or "")


def get_capital_state(
    live_prices: bool = True,
    include_opportunities: bool = False,
    include_decisions: bool = False,
) -> Dict[str, Any]:
    """Lightweight state payload for the Capital tab."""
    config = get_shared_config()
    state_payload = _load_json_cached(STATE_PATH, _state_cache, {})
    signals_data = _load_json_cached(
        SIGNALS_PATH,
        _signals_cache,
        {"open": {}, "closed": []},
        include_closed=False,
    )
    refresh_interval_ms = config.ui.get("refresh_interval_ms", 5000)
    tickers_list: List[Dict[str, Any]] = []
    raw_tickers = (state_payload or {}).get("tickers", {}) if isinstance(state_payload, dict) else {}
    if isinstance(raw_tickers, dict):
        for raw_key, row in raw_tickers.items():
            if not isinstance(row, dict):
                continue
            payload = dict(row)
            payload["ticker"] = payload.get("ticker") or raw_key
            tickers_list.append(payload)
    return {
        "refresh_interval_ms": refresh_interval_ms,
        "timestamp": get_bucharest_time().isoformat(),
        "capital": _build_capital_payload(
            tickers_list,
            signals_data,
            fast_only=not bool(live_prices),
            include_opportunities=include_opportunities,
            include_decisions=include_decisions,
        ),
    }


def _asset_type_for_ticker(ticker: str) -> str:
    if is_crypto_ticker(ticker):
        return "crypto"
    if _uses_yahoo_futures_price(ticker):
        return "commodity"
    return "stock"


def _sector_for_ticker(ticker: str) -> str:
    if _uses_yahoo_futures_price(ticker):
        return "Commodity"
    return get_sector(ticker).title()


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


def load_traderadar_state(default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fallback = default if isinstance(default, dict) else {
        "as_of": None,
        "config": {},
        "candidates": [],
        "suppressed": {},
    }
    payload = _load_json_cached(TRADE_RADAR_STATE_PATH, _traderadar_cache, fallback)
    return payload if isinstance(payload, dict) else dict(fallback)


def save_traderadar_state(state: Dict[str, Any]) -> Dict[str, Any]:
    payload = state if isinstance(state, dict) else {}
    _write_json_atomic(TRADE_RADAR_STATE_PATH, payload)
    try:
        mtime = TRADE_RADAR_STATE_PATH.stat().st_mtime
    except Exception:
        mtime = 0.0
    _traderadar_cache["mtime"] = mtime
    _traderadar_cache["data"] = payload
    return payload


def get_traderadar_state(limit: int = 50) -> Dict[str, Any]:
    data = load_traderadar_state()
    candidates = data.get("candidates", [])
    if not isinstance(candidates, list):
        candidates = []
    try:
        lim = max(1, min(200, int(limit or 50)))
    except Exception:
        lim = 50

    def _rank(item: Dict[str, Any]) -> Tuple[float, float]:
        analysis = item.get("analysis") if isinstance(item, dict) else {}
        conf = 0.0
        try:
            conf = float((analysis or {}).get("confidence", 0.0) or 0.0)
        except Exception:
            conf = 0.0
        try:
            ch = abs(float((item or {}).get("change_pct", 0.0) or 0.0))
        except Exception:
            ch = 0.0
        return conf, ch

    ordered = sorted([c for c in candidates if isinstance(c, dict)], key=_rank, reverse=True)
    return {
        "as_of": data.get("as_of"),
        "config": data.get("config", {}),
        "suppressed": data.get("suppressed", {}),
        "meta": data.get("meta", {}),
        "total_candidates": len(ordered),
        "candidates": ordered[:lim],
    }

# Hot list cache to reduce repeated API calls on frequent /api/state polls
_hot_list_cache_lock = threading.Lock()
_hot_list_cache = {"ts": 0.0, "key": None, "data": []}
_HOT_LIST_CACHE_TTL_SECONDS = 30

_hot_list_disk_cache_lock = threading.Lock()
_hot_list_disk_cache = {"ts": 0.0, "key": None, "data": []}
_HOT_LIST_DISK_CACHE_TTL_SECONDS = 1800
_HOT_LIST_DISK_CACHE_EARNINGS_TTL_SECONDS = 600

_DATA_DIR = DATA_DIR
_PRE_EARNINGS_PATH = _DATA_DIR / "pre_earnings.json"
_PRICE_CACHE_PATH = Path(__file__).parent.parent / "price_cache.json"
_CALENDAR_EVENTS_CACHE_PATH = DATA_DIR / "calendar_events_cache.json"

_pre_earnings_cache_lock = threading.Lock()
_pre_earnings_cache = {"mtime": 0.0, "map": {}}

_fmp_cache_map_lock = threading.Lock()
_fmp_cache_map = {"mtime": 0.0, "data": {}}

_calendar_earnings_map_lock = threading.Lock()
_calendar_earnings_map = {"mtime": 0.0, "data": {}}

_hotlist_earnings_context_lock = threading.Lock()
_hotlist_earnings_context = {"ts": 0.0, "data": {}}
_HOTLIST_EARNINGS_CONTEXT_TTL_SECONDS = 300

def _load_hot_list_cache_from_disk():
    try:
        if HOTLIST_CACHE_PATH.exists():
            with open(HOTLIST_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                ts = float(data.get("ts", 0.0))
                key = data.get("key") or []
                cache_key = tuple([str(k).upper() for k in key])
                items = data.get("data", [])
                if isinstance(items, list):
                    items = [apply_hotlist_defaults(i) for i in items if isinstance(i, dict)]
                    return {"ts": ts, "key": cache_key, "data": items}
    except Exception as e:
        logger.error(f"Error loading hot list cache: {e}")
    return None

def _save_hot_list_cache_to_disk(cache_key, items, ts):
    try:
        payload = {"ts": ts, "key": list(cache_key), "data": items}
        _write_json_atomic(Path(HOTLIST_CACHE_PATH), payload)
    except Exception as e:
        logger.error(f"Error saving hot list cache: {e}")


def _extract_momentum_from_price_change(price_change_data: Any) -> tuple[Any, Any]:
    item = {}
    if isinstance(price_change_data, list) and price_change_data:
        first = price_change_data[0]
        if isinstance(first, dict):
            item = first
    elif isinstance(price_change_data, dict):
        item = price_change_data
    if not item:
        return None, None
    momentum_1w = item.get("1W", item.get("5D", item.get("1w")))
    momentum_1m = item.get("1M", item.get("1m"))
    try:
        if momentum_1w is not None:
            momentum_1w = float(momentum_1w)
    except Exception:
        momentum_1w = None
    try:
        if momentum_1m is not None:
            momentum_1m = float(momentum_1m)
    except Exception:
        momentum_1m = None
    return momentum_1w, momentum_1m


def _dynamic_hot_list_disk_ttl(items: List[dict]) -> int:
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            days = int(item.get("earnings_days", -1))
        except Exception:
            days = -1
        if 0 <= days <= 5:
            return _HOT_LIST_DISK_CACHE_EARNINGS_TTL_SECONDS
    return _HOT_LIST_DISK_CACHE_TTL_SECONDS


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        f = float(value)
        return default if (f != f or f == float("inf") or f == float("-inf")) else f
    except Exception:
        return default


def _parse_iso_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        try:
            dt = datetime.fromisoformat(text[:19].replace("T", " "))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_pre_earnings_map_cached() -> Dict[str, Dict[str, Any]]:
    try:
        if not _PRE_EARNINGS_PATH.exists():
            return {}
        mtime = _PRE_EARNINGS_PATH.stat().st_mtime
        with _pre_earnings_cache_lock:
            if _pre_earnings_cache.get("mtime") == mtime:
                return dict(_pre_earnings_cache.get("map") or {})
        with open(_PRE_EARNINGS_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f) or {}
        items = payload.get("items", []) if isinstance(payload, dict) else []
        mapped: Dict[str, Dict[str, Any]] = {}
        for row in items:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper().strip()
            if not symbol:
                continue
            mapped[symbol] = row
        with _pre_earnings_cache_lock:
            _pre_earnings_cache["mtime"] = mtime
            _pre_earnings_cache["map"] = mapped
        return dict(mapped)
    except Exception as e:
        logger.debug(f"Failed loading pre-earnings map: {e}")
        return {}


def _load_fmp_cache_map_cached() -> Dict[str, Any]:
    # FMP deprecated -- return empty, cache no longer maintained
    return {}


def _load_calendar_earnings_map_cached() -> Dict[str, List[Dict[str, Any]]]:
    try:
        if not _CALENDAR_EVENTS_CACHE_PATH.exists():
            return {}
        mtime = _CALENDAR_EVENTS_CACHE_PATH.stat().st_mtime
        with _calendar_earnings_map_lock:
            if _calendar_earnings_map.get("mtime") == mtime:
                return dict(_calendar_earnings_map.get("data") or {})
        with open(_CALENDAR_EVENTS_CACHE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f) or {}
        earnings_raw = payload.get("earnings", {}) if isinstance(payload, dict) else {}
        earnings_map: Dict[str, List[Dict[str, Any]]] = {}
        if isinstance(earnings_raw, dict):
            for symbol, rows in earnings_raw.items():
                sym = str(symbol or "").upper().strip()
                if not sym:
                    continue
                if isinstance(rows, list):
                    earnings_map[sym] = [r for r in rows if isinstance(r, dict)]
        with _calendar_earnings_map_lock:
            _calendar_earnings_map["mtime"] = mtime
            _calendar_earnings_map["data"] = earnings_map
        return dict(earnings_map)
    except Exception as e:
        logger.debug(f"Failed loading calendar earnings cache: {e}")
        return {}


def _normalize_earnings_session(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"BMO", "BEFORE", "BEFORE MARKET OPEN"}:
        return "BMO"
    if text in {"AMC", "AFTER", "AFTER MARKET CLOSE"}:
        return "AMC"
    return "UNKNOWN"


_SYMBOL_BARS_CACHE: Dict[str, Any] = {}
_SYMBOL_BARS_CACHE_LOCK = threading.Lock()
_SYMBOL_BARS_POSITIVE_TTL = 6 * 3600
_SYMBOL_BARS_NEGATIVE_TTL = 600.0
_SYMBOL_BARS_FETCH_LOCKS: Dict[str, threading.Lock] = {}
_SYMBOL_BARS_FETCH_LOCKS_GUARD = threading.Lock()


def _load_symbol_ohlc_bars_from_cache(symbol: str, *, allow_fetch: bool = False) -> List[Dict[str, Any]]:
    """Daily OHLC bars for gap-stat / expected-move computations.

    Two-tier cache: a successful fetch lives 6h; an empty result (rate-limited
    or unavailable provider) is negatively cached for 10 min so a throttled
    provider cannot block dossier/state reads. The network fetch only runs when
    `allow_fetch=True` — read paths serve cache-only and the hot-list heat
    endpoint warms the store in the background."""
    symbol_u = str(symbol or "").upper().strip()
    if not symbol_u:
        return []
    now_ts = time.time()
    with _SYMBOL_BARS_CACHE_LOCK:
        cached = _SYMBOL_BARS_CACHE.get(symbol_u)
        if cached is not None:
            bars, fetched_at = cached
            ttl = _SYMBOL_BARS_POSITIVE_TTL if bars else _SYMBOL_BARS_NEGATIVE_TTL
            if now_ts - fetched_at < ttl:
                return bars
    if not allow_fetch:
        return []
    with _SYMBOL_BARS_FETCH_LOCKS_GUARD:
        lock = _SYMBOL_BARS_FETCH_LOCKS.setdefault(symbol_u, threading.Lock())
    with lock:
        now_ts = time.time()
        with _SYMBOL_BARS_CACHE_LOCK:
            cached = _SYMBOL_BARS_CACHE.get(symbol_u)
            if cached is not None:
                bars, fetched_at = cached
                ttl = _SYMBOL_BARS_POSITIVE_TTL if bars else _SYMBOL_BARS_NEGATIVE_TTL
                if now_ts - fetched_at < ttl:
                    return bars
        try:
            from core.stock_data_client import StockDataClient

            frame = StockDataClient().get_historical_eod_light(symbol_u)
        except Exception:
            frame = None
        bars: List[Dict[str, Any]] = []
        if frame is not None and not getattr(frame, "empty", True):
            for idx, row in frame.iterrows():
                try:
                    o = float(row.get("Open"))
                    h = float(row.get("High"))
                    low = float(row.get("Low"))
                    c = float(row.get("Close"))
                except (TypeError, ValueError):
                    continue
                if min(o, h, low, c) <= 0 or o != o or h != h:
                    continue
                ts = idx.isoformat() if hasattr(idx, "isoformat") else str(idx)
                dt = _parse_iso_ts(ts)
                if dt is None:
                    continue
                bars.append({"dt": dt, "open": o, "high": h, "low": low, "close": c})
        with _SYMBOL_BARS_CACHE_LOCK:
            _SYMBOL_BARS_CACHE[symbol_u] = (bars, time.time())
        return bars


def _warm_symbol_bars(tickers: List[str]) -> None:
    """Background warmer: fetch daily bars so dossier/earnings reads never
    block on the network (bounded to the first 12 tickers)."""

    def _worker():
        for tk in tickers[:12]:
            try:
                _load_symbol_ohlc_bars_from_cache(tk, allow_fetch=True)
            except Exception:
                pass

    threading.Thread(target=_worker, name="hotlist-bars-warmer", daemon=True).start()


def _compute_event_gap_move_pct(
    bars: List[Dict[str, Any]],
    event_dt: datetime,
    session: str,
) -> Optional[float]:
    if not bars:
        return None
    event_date = event_dt.date()
    sess = _normalize_earnings_session(session)
    if sess == "BMO":
        prev_candidates = [b for b in bars if b["dt"].date() < event_date]
        next_candidates = [b for b in bars if b["dt"].date() >= event_date]
    elif sess == "AMC":
        prev_candidates = [b for b in bars if b["dt"].date() <= event_date]
        next_candidates = [b for b in bars if b["dt"].date() > event_date]
    else:
        prev_candidates = [b for b in bars if b["dt"] <= event_dt]
        next_candidates = [b for b in bars if b["dt"] > event_dt]
    if not prev_candidates or not next_candidates:
        return None
    close_prev = _safe_float(prev_candidates[-1].get("close"), None)
    open_next = _safe_float(next_candidates[0].get("open"), None)
    if close_prev in (None, 0.0) or open_next is None:
        return None
    return float((open_next - close_prev) / close_prev * 100.0)


def _compute_historical_earnings_gap_stats(symbol: str, max_events: int = 8) -> Dict[str, Any]:
    symbol_u = str(symbol or "").upper().strip()
    if not symbol_u:
        return {"sample_size": 0, "moves_signed": [], "avg_signed": None, "std_signed": None, "avg_abs": None}

    earnings_map = _load_calendar_earnings_map_cached()
    rows = [r for r in earnings_map.get(symbol_u, []) if isinstance(r, dict)]
    if not rows:
        return {"sample_size": 0, "moves_signed": [], "avg_signed": None, "std_signed": None, "avg_abs": None}

    bars = _load_symbol_ohlc_bars_from_cache(symbol_u)
    if not bars:
        return {"sample_size": 0, "moves_signed": [], "avg_signed": None, "std_signed": None, "avg_abs": None}

    now_utc = datetime.now(timezone.utc)
    rows.sort(
        key=lambda r: _parse_iso_ts(r.get("date") or r.get("date_formatted")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    moves: List[float] = []
    for row in rows:
        if len(moves) >= max_events:
            break
        dt = _parse_iso_ts(row.get("date") or row.get("date_formatted"))
        if dt is None or dt >= now_utc:
            continue
        session = row.get("when") or row.get("session")
        move = _compute_event_gap_move_pct(bars, dt, str(session or ""))
        if move is None:
            continue
        moves.append(float(move))

    if not moves:
        return {"sample_size": 0, "moves_signed": [], "avg_signed": None, "std_signed": None, "avg_abs": None}

    abs_moves = [abs(m) for m in moves]
    return {
        "sample_size": len(moves),
        "moves_signed": moves[:max_events],
        "avg_signed": float(mean(moves)),
        "std_signed": float(pstdev(moves)) if len(moves) >= 2 else 0.0,
        "avg_abs": float(mean(abs_moves)),
    }


def _estimate_expected_move_pct_from_cache(symbol: str, fallback_price: Optional[float] = None) -> Optional[float]:
    symbol_u = str(symbol or "").upper().strip()
    if not symbol_u:
        return None
    # 2026-09-03 audit: this read the deprecated FMP quote cache (hardcoded {})
    # and returned None -> the card showed a fake 0.00%. Estimate from the REAL
    # daily bars now loaded by _load_symbol_ohlc_bars_from_cache (yfinance,
    # 6h TTL): expected move ~ average true range over the last 10 sessions.
    bars = _load_symbol_ohlc_bars_from_cache(symbol_u)
    if bars:
        recent = bars[-10:]
        trs: List[float] = []
        prev_close = None
        for b in recent:
            high = _safe_float(b.get("high"), None)
            low = _safe_float(b.get("low"), None)
            close = _safe_float(b.get("close"), None)
            if high is None or low is None or close is None:
                continue
            tr = high - low if prev_close is None else max(
                high - low, abs(high - prev_close), abs(low - prev_close)
            )
            if close:
                trs.append(tr / close * 100.0)
            prev_close = close
        valid = [t for t in trs if t > 0]
        if valid:
            return round(sum(valid) / len(valid), 3)
    cache = _load_fmp_cache_map_cached()
    quote = (cache.get(f"quote_{symbol_u}", {}) or {}).get("data")
    if not isinstance(quote, dict):
        return None
    day_low = _safe_float(quote.get("dayLow"), None)
    day_high = _safe_float(quote.get("dayHigh"), None)
    price = _safe_float(quote.get("price"), None)
    if price in (None, 0.0):
        price = _safe_float(fallback_price, None)
    if day_low is None or day_high is None or price in (None, 0.0):
        return None
    return float(abs(day_high - day_low) / abs(price) * 100.0)


def _compute_atr_percentile_and_regime(
    bars: List[Dict[str, Any]],
    fallback_atr: Optional[float] = None,
    fallback_price: Optional[float] = None,
) -> Tuple[Optional[float], str]:
    if not bars or len(bars) < 25:
        if fallback_atr is None or fallback_atr <= 0 or fallback_price is None or fallback_price <= 0:
            return None, "unknown"
        atr_pct = abs(float(fallback_atr) / float(fallback_price)) * 100.0
        # Approximate percentile from absolute ATR%, conservative baseline.
        percentile = _safe_float((atr_pct / 8.0) * 100.0, 50.0)
        percentile = max(0.0, min(100.0, percentile))
    else:
        tr_values: List[float] = []
        prev_close = None
        for b in bars:
            high = _safe_float(b.get("high"), None)
            low = _safe_float(b.get("low"), None)
            close = _safe_float(b.get("close"), None)
            if high is None or low is None or close is None:
                prev_close = close
                continue
            if prev_close is None:
                tr = high - low
            else:
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_values.append(float(max(0.0, tr)))
            prev_close = close
        if len(tr_values) < 25:
            return None, "normal"
        window = 14
        atr_series: List[float] = []
        for idx in range(window - 1, len(tr_values)):
            chunk = tr_values[idx - window + 1 : idx + 1]
            atr_series.append(float(sum(chunk) / float(window)))
        if not atr_series:
            return None, "normal"
        current = atr_series[-1]
        less_or_equal = sum(1 for x in atr_series if x <= current)
        percentile = (less_or_equal / float(len(atr_series))) * 100.0

    if percentile >= 85:
        regime = "extreme"
    elif percentile >= 65:
        regime = "high"
    elif percentile >= 40:
        regime = "normal"
    elif percentile >= 20:
        regime = "low"
    else:
        regime = "compression"
    return round(percentile, 2), regime


def _compute_adx14(bars: List[Dict[str, Any]], period: int = 14) -> Optional[float]:
    """Wilder ADX from OHLC bars (2026-09-03 audit: the hotlist card showed
    ADX N/A forever because nothing computed it after the FMP retirement)."""
    if len(bars) < period * 2:
        return None
    trs: List[float] = []
    plus_dm: List[float] = []
    minus_dm: List[float] = []
    prev_high = prev_low = prev_close = None
    for b in bars:
        high = _safe_float(b.get("high"), None)
        low = _safe_float(b.get("low"), None)
        close = _safe_float(b.get("close"), None)
        if high is None or low is None or close is None:
            continue
        if prev_high is not None:
            up_move = high - prev_high
            down_move = prev_low - low
            plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
            minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_high, prev_low, prev_close = high, low, close
    if len(trs) < period:
        return None
    atr = sum(trs[:period])
    adxp = sum(plus_dm[:period])
    adxm = sum(minus_dm[:period])
    dx_values: List[float] = []
    for i in range(period, len(trs)):
        atr = atr - atr / period + trs[i]
        adxp = adxp - adxp / period + plus_dm[i - 1]
        adxm = adxm - adxm / period + minus_dm[i - 1]
        if atr > 0:
            pdi = 100.0 * adxp / atr
            mdi = 100.0 * adxm / atr
            denom = pdi + mdi
            dx_values.append(100.0 * abs(pdi - mdi) / denom if denom else 0.0)
    if not dx_values:
        return None
    return round(sum(dx_values[-period:]) / min(len(dx_values), period), 1)


# momentum + ADX per-symbol TTL caches (hot-list scale, one call per 6h)
_MOMENTUM_CACHE: Dict[str, Any] = {}
_MOMENTUM_CACHE_LOCK = threading.Lock()
_MOMENTUM_TTL_SECONDS = 6 * 3600
_MOMENTUM_FAILURE_TTL_SECONDS = 60


def _hotlist_momentum(symbol: str) -> Dict[str, Any]:
    symbol_u = str(symbol or "").upper().strip()
    if not symbol_u:
        return {"momentum_1w": None, "momentum_1m": None}
    now_ts = time.time()
    with _MOMENTUM_CACHE_LOCK:
        cached = _MOMENTUM_CACHE.get(symbol_u)
        if cached and (now_ts - cached[1]) < _MOMENTUM_TTL_SECONDS:
            m = cached[0]
            has_value = bool(m) and (m.get("momentum_1w") is not None or m.get("momentum_1m") is not None)
            # A None/empty result (bars not cached yet at compute time) is retried
            # on a short TTL instead of poisoning the symbol for the full 6h window.
            if has_value or (now_ts - cached[1]) < _MOMENTUM_FAILURE_TTL_SECONDS:
                return m
    momentum: Dict[str, Any] = {"momentum_1w": None, "momentum_1m": None}
    # Compute from the cached daily bars: yfinance .info no longer carries the
    # 5Day/1MonthPriceChangePercent keys, so the client call returned 0.0/0.0
    # (fake zeros — 2026-09-03 owner report). Bars give the true close-to-close.
    bars = _load_symbol_ohlc_bars_from_cache(symbol_u)
    closes = [b["close"] for b in bars]
    if len(closes) >= 22:
        momentum = {
            "momentum_1w": round((closes[-1] - closes[-6]) / closes[-6] * 100.0, 2),
            "momentum_1m": round((closes[-1] - closes[-22]) / closes[-22] * 100.0, 2),
        }
    elif len(closes) >= 6:
        momentum = {"momentum_1w": round((closes[-1] - closes[-6]) / closes[-6] * 100.0, 2),
                    "momentum_1m": None}
    with _MOMENTUM_CACHE_LOCK:
        _MOMENTUM_CACHE[symbol_u] = (momentum, now_ts)
    return momentum


def _hotlist_adx(symbol: str) -> Optional[float]:
    now_ts = time.time()
    with _SYMBOL_BARS_CACHE_LOCK:
        cached = _SYMBOL_BARS_CACHE.get(symbol_u := str(symbol or "").upper().strip())
        bars = cached[0] if cached else None
    if not bars:
        bars = _load_symbol_ohlc_bars_from_cache(symbol_u)
    return _compute_adx14(bars)


def _build_hotlist_earnings_context(symbol: str, seed_item: Dict[str, Any]) -> Dict[str, Any]:
    ticker = str(symbol or "").upper().strip()
    if not ticker or is_crypto_ticker(ticker):
        return {
            "historical_gap_avg": None,
            "historical_gap_std": None,
            "avg_earnings_move_8q": None,
            "atr_percentile": None,
            "ATR_percentile": None,
            "volatility_regime": "normal",
            "earnings_history_summary": {"last_8": {"up_count": 0, "down_count": 0, "avg_gap_pct": 0.0}},
            "expected_move_vs_avg": {"expected_move_pct": None, "avg_move_8q": None, "edge_pct": None},
            "expected_move_pct": None,
        }

    pre_map = _load_pre_earnings_map_cached()
    pre = pre_map.get(ticker, {}) if isinstance(pre_map, dict) else {}
    stats = _compute_historical_earnings_gap_stats(ticker, max_events=8)
    moves_signed = list(stats.get("moves_signed", []) or [])
    up_count = len([m for m in moves_signed if m > 0])
    down_count = len([m for m in moves_signed if m < 0])

    hist_avg = stats.get("avg_signed")
    hist_std = stats.get("std_signed")
    avg_move_8q = stats.get("avg_abs")
    if avg_move_8q is None:
        avg_move_8q = _safe_float(pre.get("historical_median_move_pct"), None)
    if hist_avg is None and avg_move_8q is not None:
        hist_avg = float(avg_move_8q)
    if hist_std is None and stats.get("sample_size", 0) == 1:
        hist_std = 0.0

    expected_move_pct = _safe_float(pre.get("expected_move_pct"), None)
    if expected_move_pct is None:
        expected_move_pct = _estimate_expected_move_pct_from_cache(ticker, fallback_price=_safe_float(seed_item.get("price"), None))

    bars = _load_symbol_ohlc_bars_from_cache(ticker)
    adx_val = seed_item.get("atr")
    atr_percentile, vol_regime = _compute_atr_percentile_and_regime(
        bars,
        fallback_atr=_safe_float(adx_val, None),
        fallback_price=_safe_float(seed_item.get("price"), None),
    )
    edge_pct = None
    if expected_move_pct is not None and avg_move_8q is not None:
        edge_pct = float(expected_move_pct) - float(avg_move_8q)

    return {
        "historical_gap_avg": round(hist_avg, 4) if hist_avg is not None else None,
        "historical_gap_std": round(hist_std, 4) if hist_std is not None else None,
        "avg_earnings_move_8q": round(avg_move_8q, 4) if avg_move_8q is not None else None,
        "atr_percentile": atr_percentile,
        "ATR_percentile": atr_percentile,
        "volatility_regime": vol_regime,
        "earnings_history_summary": {
            "last_8": {
                "up_count": int(up_count),
                "down_count": int(down_count),
                "avg_gap_pct": round(_safe_float(hist_avg, 0.0), 4) if moves_signed else None,
            },
            "sample_size": len(moves_signed),
        },
        "expected_move_vs_avg": {
            "expected_move_pct": round(expected_move_pct, 4) if expected_move_pct is not None else None,
            "avg_move_8q": round(avg_move_8q, 4) if avg_move_8q is not None else None,
            "edge_pct": round(edge_pct, 4) if edge_pct is not None else None,
        },
        "expected_move_pct": round(expected_move_pct, 4) if expected_move_pct is not None else None,
    }


def _get_hotlist_earnings_context(symbol: str, seed_item: Dict[str, Any]) -> Dict[str, Any]:
    ticker = str(symbol or "").upper().strip()
    now_ts = time.time()
    with _hotlist_earnings_context_lock:
        if (now_ts - _hotlist_earnings_context.get("ts", 0.0)) > _HOTLIST_EARNINGS_CONTEXT_TTL_SECONDS:
            _hotlist_earnings_context["ts"] = now_ts
            _hotlist_earnings_context["data"] = {}
        cached = (_hotlist_earnings_context.get("data") or {}).get(ticker)
        if isinstance(cached, dict):
            return dict(cached)

    computed = _build_hotlist_earnings_context(ticker, seed_item)
    # A context computed before the daily bars were warmed has no expected
    # move / ATR percentile / history. Caching that empty result pinned the
    # card to "N/A" for the whole 5-minute TTL after the bars had arrived
    # (tab audit 2026-11-15) — only cache a context that has bar data.
    has_bar_data = (
        is_crypto_ticker(ticker)
        or computed.get("atr_percentile") is not None
        or computed.get("expected_move_pct") is not None
        or computed.get("avg_earnings_move_8q") is not None
    )
    if has_bar_data:
        with _hotlist_earnings_context_lock:
            bucket = _hotlist_earnings_context.setdefault("data", {})
            bucket[ticker] = computed
    return dict(computed)


def _extract_watchlist_signal_score(sig_payload: Dict[str, Any]) -> float:
    """
    Return a normalized 0..100 signal score for Watchlist rows.

    Priority:
    1) `signal_score` / `score` when already populated
    2) `raw_signal_score` / `raw_score` for historical signals saved before normalization
    3) `probability_score` (legacy/aux, 0..1 -> scaled to 0..100)
    4) `quality_score` (legacy 0..10 -> scaled to 0..100)
    """
    payload = sig_payload or {}

    for score_key in ("signal_score", "score", "raw_signal_score", "raw_score"):
        raw_signal = payload.get(score_key, None)
        if raw_signal is None:
            continue
        score = _safe_float(raw_signal, 0.0)
        if score > 0:
            return max(0.0, min(100.0, score))

    raw_prob = payload.get("probability_score", None)
    if raw_prob is not None:
        prob = _safe_float(raw_prob, 0.0)
        # Most producers send 0..1; preserve 0..100 if already scaled.
        score = prob * 100.0 if 0.0 <= prob <= 1.0 else prob
        return max(0.0, min(100.0, score))

    quality = _safe_float(payload.get("quality_score", 0.0), 0.0)
    # Legacy quality is 0..10; keep support for already-scaled values.
    score = quality * 10.0 if 0.0 <= quality <= 10.0 else quality
    return max(0.0, min(100.0, score))


def _enrich_hotlist_item(item: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(item or {})
    ticker = str(data.get("ticker") or "").upper().strip()
    if not ticker:
        return apply_hotlist_defaults(data)

    news_signals = summarize_ticker_news_signals(ticker)
    earnings_context = _get_hotlist_earnings_context(ticker, data)
    data.update(earnings_context)
    # 2026-09-03 audit: ADX and momentum had no producer after the FMP
    # retirement — the card showed N/A / fake 0.0% forever. Compute both from
    # the TTL-cached daily bars / price-change client (hot-list scale only).
    if not _safe_float(data.get("adx"), None):
        _adx_val = _hotlist_adx(ticker)
        if _adx_val is not None:
            data["adx"] = _adx_val
    if data.get("momentum_1w") is None or data.get("momentum_1m") is None:
        data.update({k: v for k, v in _hotlist_momentum(ticker).items()
                     if data.get(k) is None and v is not None})
    market_data = {
        "insider": data.get("insider", {}),
        "adx": data.get("adx", {}),
        "ratings": data.get("ratings", {}),
    }
    # Crypto has no earnings — skip earnings-edge scoring. Pre-fix the engine
    # ran compute_earnings_edge_score on crypto with earnings_days=-1, which
    # the scoring map treats as "no upcoming earnings" and drives play=WAIT/
    # AVOID purely off the news score. That invented an "earnings risk" verdict
    # for assets that have none (e.g. ENJ-USD flagged AVOID). Set neutral N/A
    # fields instead (directive: nothing invented, every value correct).
    if is_crypto_ticker(ticker):
        scoring = {
            "earnings_edge_score": 50,
            "earnings_setup": "N/A",
            "earnings_play": "N/A",
            "earnings_risk_flags": [],
            "risk_level": "low",
            "top_risk": "N/A — crypto (no earnings)",
        }
    else:
        # Tab audit 2026-11-15: engine trend/RSI rows that missed a full session
        # (live case: 68 days old) must not drive the score as if current. They
        # stay on the card, marked stale, but are scored as unknown.
        scoring_view = dict(data)
        if data.get("technicals_stale"):
            for _trend_key in ("trend_1d", "trend_4h", "trend_1h", "trend_30m", "trend_5m"):
                scoring_view[_trend_key] = "N/A"
            scoring_view["rsi"] = None
        scoring = compute_earnings_edge_score(scoring_view, news_signals=news_signals, market_data=market_data)
    data.update(scoring)
    composite_view = dict(data)
    if data.get("technicals_stale"):
        for _trend_key in ("trend_1d", "trend_4h", "trend_1h", "trend_30m", "trend_5m"):
            composite_view[_trend_key] = "N/A"
        composite_view["rsi"] = None
    composite = compute_hotlist_composite_metrics(composite_view, news_signals=news_signals, market_data=market_data)
    data["news_signals"] = news_signals
    data.update(composite)
    # Keep one source of truth for hotlist news score.
    data["news_pulse_score"] = int(round(_safe_float(data.get("news_score"), 0.0)))
    # Honest verdict: missing inputs -> INSUFFICIENT_DATA, no upcoming earnings
    # -> N/A, confidence scaled by input coverage, Top Risk never a data gap.
    apply_hotlist_data_guard(data, has_earnings_cycle=not is_crypto_ticker(ticker))

    def _trend_known(value: Any) -> bool:
        text = str(value or "").strip().upper()
        return bool(text) and text != "N/A"

    _has_fundamentals = bool(data.get("ratings") or data.get("insider"))
    _has_bar_metrics = bool(_safe_float(data.get("adx"), 0.0) if not isinstance(data.get("adx"), dict)
                            else _safe_float((data.get("adx") or {}).get("adx"), 0.0)) or (
        data.get("momentum_1w") is not None or data.get("momentum_1m") is not None
    )
    # Data quality indicator -- helps frontend show meaningful placeholders.
    # ("N/A" strings used to count as present, so has_trends / has_earnings
    # were always True.)
    data["_data_quality"] = {
        "has_trends": _trend_known(data.get("trend_1d")) or _trend_known(data.get("trend_4h")),
        "has_price": bool(_safe_float(data.get("price"), 0.0) > 0),
        "price_stale": bool(data.get("price_stale")),
        "technicals_stale": bool(data.get("technicals_stale")),
        "has_earnings": _trend_known(data.get("earnings_date")),
        "has_bar_metrics": bool(_has_bar_metrics),
        "has_fundamental_details": _has_fundamentals,
        # FMP was retired and nothing replaced it: insider / ratings cannot
        # load, so their absence is "no provider", not a loading state.
        "fundamentals_provider": False,
        "missing_inputs": list(data.get("missing_inputs") or []),
        "coverage_pct": data.get("data_coverage_pct"),
        "source": "full" if _has_fundamentals else "fast",
    }
    return apply_hotlist_defaults(data)
def _pick(d: dict, *keys, default=None):
    """Return first non-empty value for given keys from dict."""
    for k in keys:
        v = d.get(k)
        if v is not None and v != "":
            return v
    return default

def _fmt_time(ts: str) -> str:
    """
    Format ISO time to 'YYYY-MM-DD HH:MM:SS' when possible.
    Falls back to raw or N/A.
    """
    if not ts:
        return "N/A"
    try:
        # handles ...Z too
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        # common fallback: keep first 19 chars
        if isinstance(ts, str) and len(ts) >= 19 and "T" in ts:
            return ts[:19].replace("T", " ")
        return str(ts)

def _calc_pnl(action: str, entry: float, exit_price: float):
    """
    Returns (pnl_abs, pnl_pct) or (None, None) if not computable.
    pnl_abs is per 1 unit of asset.
    """
    try:
        entry = float(entry)
        exit_price = float(exit_price)
    except Exception:
        return None, None

    if entry <= 0 or exit_price <= 0:
        return None, None

    a = (action or "").upper()
    if a == "BUY":
        pnl_abs = exit_price - entry
        pnl_pct = (exit_price - entry) / entry * 100.0
    else:
        # SHORT
        pnl_abs = entry - exit_price
        pnl_pct = (entry - exit_price) / entry * 100.0

    return pnl_abs, pnl_pct


def _normalize_earnings_when(value: str) -> str:
    if not value:
        return ""
    when = str(value).strip().lower()
    if when in ("bmo", "amc"):
        return when
    if "before" in when:
        return "bmo"
    if "after" in when:
        return "amc"
    return ""


def _earnings_when_desc(when: str) -> str:
    if when == "bmo":
        return "Pre-Market"
    if when == "amc":
        return "After Hours"
    return ""


_yf_earnings_cache: Dict[str, tuple] = {}  # ticker -> (fetch_ts, "YYYY-MM-DD" or None)
_yf_earnings_cache_lock = threading.Lock()
_YF_EARNINGS_TTL_SECONDS = 6 * 3600  # 6h — earnings dates change rarely


def _fetch_yf_earnings_date(ticker: str) -> Optional[str]:
    """Fetch the next earnings date for a stock via yfinance (no API key). 'YYYY-MM-DD' or None."""
    from datetime import date as _date
    try:
        import yfinance as yf
        y_symbol = normalize_ticker(ticker, provider="yahoo") or ticker
        cal = yf.Ticker(y_symbol).calendar
        ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
        raw = (ed[0] if isinstance(ed, (list, tuple)) and ed else ed)
        if raw is not None:
            dt = raw if isinstance(raw, _date) else datetime.fromisoformat(str(raw)[:10]).date()
            return dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    return None


def _resolve_earnings_from_yfinance(ticker: str, today, allow_remote: bool = True) -> tuple[str, int, str, str]:
    """Next-earnings-date fallback via yfinance (no API key). Cached per-ticker.

    Replaces the FMP remote fallback that was removed: without this, every cache miss
    returned 'N/A' (e.g. AAPL showed earnings_date_unknown even though yfinance has it).
    Cache-only callers never initiate background provider work; scheduled agents own
    cache warming so a UI read cannot create untracked external traffic."""
    import time as _time
    key = str(ticker or "").upper().strip()
    if not key:
        return ("N/A", -1, "", "")
    now_ts = _time.time()
    with _yf_earnings_cache_lock:
        cached = _yf_earnings_cache.get(key)
    fresh = cached is not None and (now_ts - cached[0]) <= _YF_EARNINGS_TTL_SECONDS

    if not fresh:
        if allow_remote:
            date_str = _fetch_yf_earnings_date(key)
            with _yf_earnings_cache_lock:
                _yf_earnings_cache[key] = (now_ts, date_str)
        else:
            return ("N/A", -1, "", "")
    else:
        date_str = cached[1]

    if not date_str:
        return ("N/A", -1, "", "")
    try:
        dt = datetime.fromisoformat(date_str).date()
        return (date_str, (dt - today).days, "", "")
    except Exception:
        return ("N/A", -1, "", "")


def _resolve_earnings_from_sources(calendar, ticker: str, today, allow_remote: bool = True) -> tuple[str, int, str, str]:
    """
    Resolve earnings info for ticker using cache first.
    Returns (earnings_date, days_until, earnings_when, earnings_when_desc).
    """
    earnings_date = "N/A"
    days_until = -1
    earnings_when = ""
    earnings_when_desc = ""

    # 1) Cached calendar
    try:
        nonblocking_reader = getattr(calendar, "try_get_earnings_for_ticker", None)
        ev = (
            nonblocking_reader(ticker)
            if not allow_remote and callable(nonblocking_reader)
            else calendar.get_earnings_for_ticker(ticker)
        )
    except Exception:
        ev = None

    if ev:
        date_str = ev.get("date") or ev.get("date_formatted")
        if date_str:
            try:
                dt = datetime.fromisoformat(str(date_str)[:10]).date()
                earnings_date = dt.strftime("%Y-%m-%d")
                days_until = (dt - today).days
            except Exception:
                pass
        earnings_when = _normalize_earnings_when(ev.get("when") or "")
        earnings_when_desc = ev.get("when_desc") or _earnings_when_desc(earnings_when)
        return earnings_date, days_until, earnings_when.upper(), earnings_when_desc

    # 2) yfinance fallback (no API key) for stocks — FMP removal left cache misses empty.
    #    Cache-only callers (allow_remote=False) only read the existing TTL cache.
    if not is_crypto_ticker(ticker) and not _uses_yahoo_futures_price(ticker):
        return _resolve_earnings_from_yfinance(ticker, today, allow_remote=allow_remote)

    return earnings_date, days_until, earnings_when, earnings_when_desc

def format_closed_signal_spans(sig: dict) -> list[dict]:
    """
    Multi-line closed signal display with:
    open/close times, entry/exit, pnl.
    Works with both snake_case and legacy keys.
    """
    signal_id = _pick(sig, "id", default="")
    status = _pick(sig, "status", default="CLOSED")
    variant = _pick(sig, "variant", default="C")
    variant_mode = str(_pick(sig, "variant_mode", default="STRICT")).upper()
    action = _pick(sig, "action", "type", default="")
    ticker = _pick(sig, "ticker", default="")

    created_at = _pick(sig, "createdat", "created_at", default="")
    closed_at = _pick(sig, "closedat", "closed_at", default="")

    entry = _pick(sig, "entry", "entryprice", "entry_price", default=0.0)
    exit_price = _pick(sig, "exitprice", "exit_price", default=None)

    tag = "win" if status == "WIN" else "lost" if status == "LOST" else "info"
    icon = "v" if status == "WIN" else "x" if status == "LOST" else "o"

    # PnL only if we have exit_price (WIN/LOST normally have it)
    pnl_abs, pnl_pct = (None, None)
    if exit_price is not None:
        pnl_abs, pnl_pct = _calc_pnl(action, entry, exit_price)

    # Format numbers
    try:
        entry_f = float(entry)
    except Exception:
        entry_f = 0.0

    exit_str = "N/A"
    if exit_price is not None:
        try:
            exit_str = f"{float(exit_price):.4f}"
        except Exception:
            exit_str = "N/A"

    pnl_str = "PnL: N/A"
    if pnl_abs is not None and pnl_pct is not None:
        sign = "+" if pnl_pct >= 0 else ""
        pnl_str = f"PnL: {sign}{pnl_pct:.2f}% ({sign}{pnl_abs:.4f})"

    spans = [
        {"text": f" {icon} [{variant}|{variant_mode}] {action} {ticker} @{entry_f:.4f} -> {status}\n", "tag": tag, "signal_id": signal_id, "ticker": ticker},
        {"text": f"     Open:  {_fmt_time(created_at)}\n", "tag": "info"},
        {"text": f"     Close: {_fmt_time(closed_at)}\n", "tag": "info"},
        {"text": f"     Exit:  {exit_str} | {pnl_str}\n\n", "tag": "info"},
    ]
    if sig.get("strict_backed"):
        sb_variant = sig.get("strict_variant", "?")
        sb_mode = sig.get("strict_signal_mode", "")
        spans.insert(3, {"text": f"     Backed by STRICT [{sb_variant}] {sb_mode}\n", "tag": "info"})
    return spans


from datetime import datetime, timedelta

def _hotlist_live_price_row(symbol: str) -> Optional[Dict[str, Any]]:
    """Fresh price row from data/live_prices_cache.json (P&L tracker writes it
    continuously). Returns {} when absent — never raises."""
    try:
        payload = json.loads((DATA_DIR / "live_prices_cache.json").read_text(encoding="utf-8"))
        row = (payload.get("prices") or {}).get(str(symbol or "").upper().strip())
        return row if isinstance(row, dict) else {}
    except Exception:
        return {}


def build_hot_list_fast(
    config: ConfigManager,
    state_data: dict,
    open_signals: Dict = None,
    tickers_list: List[dict] = None
) -> List[dict]:
    """Fast hot list built only from state.json (no external calls)."""
    from agents.economic_calendar_agent import get_economic_calendar_agent

    hot_tickers = config.hot_list
    if not hot_tickers:
        return []

    tickers_data = state_data.get("tickers", {}) or {}
    watchlist_map = {}
    calendar = get_economic_calendar_agent(config.config)
    today = datetime.now().date()
    if tickers_list:
        for item in tickers_list:
            t = (item.get("ticker") or "").upper()
            if t:
                watchlist_map[t] = item
    hot_list_data = []

    for ticker in hot_tickers:
        ticker = (ticker or "").upper()
        if not ticker:
            continue
        is_crypto = ticker.endswith("-USD") or ticker.endswith("USDT") or ticker.endswith("-USDC")
        data = tickers_data.get(ticker, {})
        wl_item = watchlist_map.get(ticker, {})
        best_strategy = {}
        if open_signals and ticker in open_signals:
            best_strategy = open_signals[ticker].get("best_strategy", {}) or {}

        # Prefer watchlist data (already built for UI) to avoid extra lookups
        def _trend_from_wl(key: str) -> str:
            val = wl_item.get(key, "")
            if isinstance(val, str) and val:
                return val.split(" ")[0]
            return "N/A"

        price = wl_item.get("price", None)
        change_pct = wl_item.get("change_pct", None)
        trend_1d = wl_item.get("trend_1d", None) or data.get("trend_1d", "N/A")
        trend_4h = wl_item.get("trend_4h", None) or data.get("trend_4h", "N/A")
        trend_1h = _trend_from_wl("col_1h") if wl_item else data.get("trend_1h", "N/A")
        trend_30m = _trend_from_wl("col_30m") if wl_item else data.get("trend_30m", "N/A")
        trend_5m = _trend_from_wl("col_5m") if wl_item else data.get("trend_5m", "N/A")
        rsi = wl_item.get("rsi", None)
        macd_hist = wl_item.get("macd_hist", None)
        strategy_notes = wl_item.get("strategy_notes", None)
        if price is None:
            price = data.get("price", 0)
        if change_pct is None:
            change_pct = data.get("change_pct", 0)
        # 2026-09-03 owner report (BMNR card all-N/A): a hot ticker whose
        # engine snapshot is missing/stale still has a fresh price in the
        # persisted live-prices cache — use it instead of rendering N/A.
        # Tab audit 2026-11-15: the fallback above only ran when the engine row
        # had NO price, so a 68-day-old engine price (NVDA $225.25 while the
        # Watchlist showed $161.01) was rendered as current. Always take the
        # freshest of engine row / live-quote store / watchlist row and carry
        # its age, so the card can never show an old price as a live one.
        quote = resolve_hotlist_quote(
            data, _hotlist_live_price_row(ticker), wl_item, always_open=is_crypto,
        )
        price = quote["price"]
        change_pct = quote["change_pct"]
        technicals_as_of = data.get("last_update") if isinstance(data, dict) else None
        has_engine_technicals = any(
            str(v or "").strip().upper() not in {"", "N/A"} for v in (trend_1d, trend_4h)
        )
        technicals_stale = bool(has_engine_technicals) and is_hotlist_technicals_stale(
            technicals_as_of, always_open=is_crypto,
        )
        if rsi is None:
            rsi = data.get("rsi")
        if macd_hist is None:
            macd_hist = data.get("macd_hist", 0)
        if strategy_notes is None:
            strategy_notes = data.get("strategy_notes", "")
        momentum_1w = wl_item.get("momentum_1w", data.get("momentum_1w"))
        momentum_1m = wl_item.get("momentum_1m", data.get("momentum_1m"))

        # Earnings from cache
        earnings_date = "N/A"
        days_until = -1
        earnings_when = ""
        earnings_when_desc = ""
        if not is_crypto:
            try:
                earnings_date, days_until, earnings_when, earnings_when_desc = _resolve_earnings_from_sources(
                    # Fast path must avoid network-bound calls.
                    calendar, ticker, today, allow_remote=False
                )
            except Exception:
                pass

        base_item = {
            "ticker": ticker,
            "price": _safe_float(price, 0.0),
            # None stays None: a missing change must not be printed as +0.00%.
            "change_pct": _safe_float(change_pct, None) if change_pct is not None else None,
            "price_as_of": quote["price_as_of"],
            "price_age_seconds": quote["price_age_seconds"],
            "price_source": quote["price_source"],
            "price_origin": quote["price_origin"],
            "price_stale": bool(quote["price_stale"]),
            "technicals_as_of": str(technicals_as_of) if technicals_as_of else None,
            "technicals_stale": technicals_stale,
            "trend_1d": trend_1d or "N/A",
            "trend_4h": trend_4h or "N/A",
            "trend_1h": trend_1h or "N/A",
            "trend_30m": trend_30m or "N/A",
            "trend_5m": trend_5m or "N/A",
            "rsi": _safe_float(rsi, 50.0) if rsi is not None else None,
            "macd_hist": _safe_float(macd_hist, 0.0),
            "atr": _safe_float(wl_item.get("atr", data.get("atr", 0)), 0.0),
            "earnings_date": earnings_date,
            "earnings_days": days_until,
            "earnings_when": earnings_when,
            "earnings_when_desc": earnings_when_desc,
            "momentum_1w": momentum_1w,
            "momentum_1m": momentum_1m,
            "strategy_notes": strategy_notes or "",
            "best_strategy": best_strategy,
            "adx": data.get("adx", {}),
            "insider": data.get("insider", {}),
            "ratings": data.get("ratings", {}),
            "_details_loaded": False,
        }
        hot_list_data.append(_enrich_hotlist_item(base_item))

    return hot_list_data


def _merge_hot_list_details(fast_list: List[dict], detailed_list: List[dict]) -> List[dict]:
    if not fast_list:
        return detailed_list or []
    if not detailed_list:
        return fast_list

    detailed_map = {d.get("ticker"): d for d in detailed_list if d}
    merged = []
    fast_fields = [
        "price", "change_pct", "trend_1d", "trend_4h", "trend_1h",
        "trend_30m", "trend_5m", "rsi", "macd_hist",
        "strategy_notes", "best_strategy", "earnings_date", "earnings_days", "earnings_when", "earnings_when_desc",
        "momentum_1w", "momentum_1m", "adx",
        "earnings_edge_score", "earnings_play", "earnings_setup",
        "earnings_risk_flags", "risk_level", "top_risk", "news_signals",
        "historical_gap_avg", "historical_gap_std", "avg_earnings_move_8q",
        "atr_percentile", "ATR_percentile", "volatility_regime",
        "earnings_history_summary", "expected_move_vs_avg", "expected_move_pct",
        "momentum_score", "news_score", "technical_alignment", "regime_alignment",
        "overall_score", "priority_score", "confidence_score",
        "risk_penalty", "time_weight", "earnings_time_factor", "news_pulse_score",
        # Quote age + data-sufficiency verdict always come from the fresh build,
        # never from an older cached detail row.
        "price_as_of", "price_age_seconds", "price_source", "price_origin", "price_stale",
        "technicals_as_of", "technicals_stale", "verdict_status", "data_coverage_pct",
        "missing_inputs", "risk_factor_flags", "data_gap_flags", "_data_quality",
    ]

    for f in fast_list:
        ticker = f.get("ticker")
        d = detailed_map.get(ticker)
        if not d:
            merged.append(apply_hotlist_defaults(f))
            continue
        item = d.copy()
        for k in fast_fields:
            if k not in f:
                continue
            val = f.get(k)
            if k == "price":
                try:
                    if val is None or float(val) <= 0:
                        continue
                except Exception:
                    continue
            if k.startswith("trend_") and (not val or val == "N/A"):
                continue
            if k == "strategy_notes" and (val is None or val == ""):
                continue
            if k in ("momentum_1w", "momentum_1m"):
                if val is None:
                    continue
                try:
                    if isinstance(val, str):
                        raw = val.replace("%", "").strip()
                        if not raw or raw.upper() == "N/A":
                            continue
                        val = float(raw)
                    else:
                        val = float(val)
                except Exception:
                    continue
            if k == "adx":
                adx_val = val
                if isinstance(adx_val, dict):
                    adx_val = adx_val.get("adx")
                try:
                    adx_num = float(adx_val)
                except Exception:
                    continue
                if adx_num <= 0:
                    continue
            item[k] = val
        merged.append(apply_hotlist_defaults(item))

    return merged


def _get_hot_list_details_cached(config: ConfigManager, state_data: dict, open_signals: Dict = None, allow_build: bool = True) -> List[dict]:
    hot_tickers = config.hot_list
    if not hot_tickers:
        return []

    def _refresh_earnings(items: List[dict]) -> bool:
        if not items:
            return False
        from agents.economic_calendar_agent import get_economic_calendar_agent
        calendar = get_economic_calendar_agent(config.config)
        today = datetime.now().date()
        changed = False
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            ticker = (item.get("ticker") or "").upper()
            if not ticker:
                continue
            if ticker.endswith("-USD") or ticker.endswith("USDT") or ticker.endswith("-USDC"):
                continue
            earnings_date, days_until, earnings_when, earnings_when_desc = _resolve_earnings_from_sources(
                calendar, ticker, today, allow_remote=False
            )
            if (
                item.get("earnings_date") != earnings_date
                or item.get("earnings_days") != days_until
                or item.get("earnings_when") != earnings_when
                or item.get("earnings_when_desc") != earnings_when_desc
            ):
                item["earnings_date"] = earnings_date
                item["earnings_days"] = days_until
                item["earnings_when"] = earnings_when
                item["earnings_when_desc"] = earnings_when_desc
                changed = True
            enriched = _enrich_hotlist_item(item)
            if enriched != item:
                items[idx] = enriched
                changed = True
        return changed

    def _core_missing(item: dict) -> bool:
        if not item or not item.get("_details_loaded"):
            return True
        try:
            price = float(item.get("price", 0) or 0)
        except Exception:
            price = 0
        if price <= 0:
            return True
        trends = [
            item.get("trend_1h"),
            item.get("trend_30m"),
            item.get("trend_5m"),
        ]
        has_trend = any(t and str(t).upper() != "N/A" for t in trends)
        return not has_trend

    cache_key = tuple([t.upper() for t in hot_tickers])
    now_ts = time.time()

    with _hot_list_disk_cache_lock:
        mem_ttl = _dynamic_hot_list_disk_ttl(_hot_list_disk_cache.get("data", []))
        if _hot_list_disk_cache["key"] == cache_key and (now_ts - _hot_list_disk_cache["ts"]) < mem_ttl:
            if allow_build and any(_core_missing(i) for i in _hot_list_disk_cache.get("data", []) if i):
                pass
            else:
                items = _hot_list_disk_cache["data"]
                if _refresh_earnings(items):
                    _hot_list_disk_cache["ts"] = now_ts
                    _hot_list_disk_cache["data"] = items
                    _save_hot_list_cache_to_disk(cache_key, items, now_ts)
                return [apply_hotlist_defaults(i) for i in items if isinstance(i, dict)]

    disk_cache = _load_hot_list_cache_from_disk()
    disk_ttl = _dynamic_hot_list_disk_ttl((disk_cache or {}).get("data", []))
    if disk_cache and disk_cache.get("key") == cache_key and (now_ts - disk_cache.get("ts", 0.0)) < disk_ttl:
        if allow_build and any(_core_missing(i) for i in disk_cache.get("data", []) if i):
            pass
        else:
            with _hot_list_disk_cache_lock:
                items = disk_cache.get("data", [])
                if _refresh_earnings(items):
                    disk_cache["ts"] = now_ts
                    disk_cache["data"] = items
                    _save_hot_list_cache_to_disk(cache_key, items, now_ts)
                _hot_list_disk_cache.update(disk_cache)
            return [apply_hotlist_defaults(i) for i in items if isinstance(i, dict)]

    if not allow_build:
        # Aggregate UI GETs are cache-only and can never reach the background
        # detail builder, so an empty cache returned [] and "Load Details"
        # wiped the populated cards (audit 2026-09-05 P1, regression from
        # a43dbf3). Degrade to the fast state-only build instead; the detail
        # builder still fills the cache on its next authorized cycle.
        try:
            fallback = build_hot_list_fast(config, state_data, open_signals, None)
            return [apply_hotlist_defaults(i) for i in fallback if isinstance(i, dict)]
        except Exception:
            logger.warning("Hot list fast fallback failed", exc_info=True)
            return []

    # Build fresh details and persist in background to prevent 30s UI block
    def _bg_build():
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    time.sleep(30 * attempt)  # Wait before retry (30s, 60s)
                logger.info("_bg_build: starting build_hot_list for %d tickers (attempt %d/%d)",
                            len(hot_tickers), attempt + 1, max_retries)
                details = build_hot_list(config, state_data, open_signals)
                # Verify at least some items have FMP data loaded
                loaded = sum(1 for d in details if d.get("_details_loaded"))
                with _hot_list_disk_cache_lock:
                    _hot_list_disk_cache["key"] = cache_key
                    _hot_list_disk_cache["ts"] = time.time()
                    _hot_list_disk_cache["data"] = details
                    _save_hot_list_cache_to_disk(cache_key, details, _hot_list_disk_cache["ts"])
                logger.info("_bg_build: success, %d/%d items with details", loaded, len(details))
                return  # Success
            except Exception as e:
                import traceback as _tb
                logger.error("Background hotlist build attempt %d failed: %s\n%s",
                             attempt + 1, e, _tb.format_exc())
        logger.error("_bg_build: all %d attempts failed", max_retries)
            
    import threading
    threading.Thread(target=_bg_build, daemon=True).start()

    # Return fast built list while we wait, to prevent 30s UI hang
    fallback = build_hot_list_fast(config, state_data, open_signals, [])
    # Merge whatever is in disk cache to the fast list
    existing_details = disk_cache.get("data", []) if disk_cache else _hot_list_disk_cache.get("data", [])
    if existing_details:
        fallback = _merge_hot_list_details(fallback, existing_details)
        
    return [apply_hotlist_defaults(i) for i in fallback if isinstance(i, dict)]


def build_hot_list(config: ConfigManager, state_data: dict, open_signals: Dict = None) -> List[dict]:
    """Build detailed data for hot list tickers including earnings countdown."""
    from agents.economic_calendar_agent import get_economic_calendar_agent
    from api.market_data_service import get_ticker_data

    hot_tickers = config.hot_list
    if not hot_tickers:
        return []

    # Cache based on hot tickers list; data updates frequently via engine
    cache_key = tuple(hot_tickers)
    now_ts = time.time()
    with _hot_list_cache_lock:
        if _hot_list_cache["key"] == cache_key and (now_ts - _hot_list_cache["ts"]) < _HOT_LIST_CACHE_TTL_SECONDS:
            return [apply_hotlist_defaults(i) for i in _hot_list_cache["data"] if isinstance(i, dict)]

    # Initialize clients
    calendar = get_economic_calendar_agent(config.config)

    tickers_data = state_data.get("tickers", {})

    from concurrent.futures import ThreadPoolExecutor

    start_date = datetime.now()
    # Earnings from cache
    earnings_map = {}
    for t in hot_tickers:
        t_up = (t or "").upper()
        if not t_up:
            continue
        if t_up.endswith("-USD") or t_up.endswith("USDT") or t_up.endswith("-USDC"):
            continue
        try:
            earnings_map[t_up] = _resolve_earnings_from_sources(calendar, t_up, start_date.date(), allow_remote=False)
        except Exception:
            continue

    def get_ticker_hot_data(ticker):
        ticker = _canonical_ticker_key(ticker)
        is_crypto = ticker.endswith("-USD") or ticker.endswith("USDT") or ticker.endswith("-USDC")
        data = dict(tickers_data.get(ticker, {}) or {})
        best_strategy = {}
        if open_signals and ticker in open_signals:
            best_strategy = open_signals[ticker].get("best_strategy", {}) or {}

        # If ticker isn't in state (e.g. added from Scanner/Hot List), fetch live data
        def _is_missing(val):
            if val is None:
                return True
            if isinstance(val, (int, float)) and val <= 0:
                return True
            if isinstance(val, str) and val.strip().upper() == "N/A":
                return True
            return False

        needs_live = (
            _is_missing(data.get("price")) or
            _is_missing(data.get("trend_1h")) or
            _is_missing(data.get("trend_30m")) or
            _is_missing(data.get("trend_5m"))
        )

        engine_row = dict(data)
        fetched_now = None
        if needs_live:
            try:
                live = get_ticker_data(ticker, lightweight=True) or {}
                # Merge: prefer live for missing or invalid values
                for k, v in live.items():
                    if _is_missing(data.get(k)):
                        data[k] = v
                if _safe_float(live.get("price"), 0.0) > 0:
                    fetched_now = {
                        "price": live.get("price"),
                        "change_pct": live.get("change_pct"),
                        "price_timestamp": datetime.now(timezone.utc).isoformat(),
                        "price_source": live.get("data_source") or "live_fetch",
                    }
            except Exception as e:
                logger.warning(f"Hot list live fetch failed for {ticker}: {e}")
        # Same quote resolution as the fast path: freshest of engine row /
        # live-quote store / the quote fetched just now, with its real age.
        quote = resolve_hotlist_quote(
            engine_row, _hotlist_live_price_row(ticker), fetched_now, always_open=is_crypto,
        )
        technicals_as_of = engine_row.get("last_update")
        technicals_stale = (
            fetched_now is None
            and any(str(engine_row.get(k) or "").strip().upper() not in {"", "N/A"} for k in ("trend_1d", "trend_4h"))
            and is_hotlist_technicals_stale(technicals_as_of, always_open=is_crypto)
        )

        # Determine earnings date (from cache with fallback)
        earnings_date = "N/A"
        days_until = -1
        earnings_when = ""
        earnings_when_desc = ""
        if not is_crypto:
            try:
                res = earnings_map.get(ticker)
                if res:
                    earnings_date, days_until, earnings_when, earnings_when_desc = res
            except Exception:
                pass

        # Use existing momentum from watchlist data
        momentum_1w = data.get("momentum_1w")
        momentum_1m = data.get("momentum_1m")
        fmp_data = {}

        base_item = {
            "ticker": ticker,
            "price": quote["price"] if quote["price"] is not None else data.get("price", 0),
            "change_pct": quote["change_pct"] if quote["price"] is not None else data.get("change_pct"),
            "price_as_of": quote["price_as_of"],
            "price_age_seconds": quote["price_age_seconds"],
            "price_source": quote["price_source"],
            "price_origin": quote["price_origin"],
            "price_stale": bool(quote["price_stale"]),
            "technicals_as_of": str(technicals_as_of) if technicals_as_of else None,
            "technicals_stale": bool(technicals_stale),
            "trend_1d": data.get("trend_1d", "N/A"),
            "trend_4h": data.get("trend_4h", "N/A"),
            "trend_1h": data.get("trend_1h", "N/A"),
            "trend_30m": data.get("trend_30m", "N/A"),
            "trend_5m": data.get("trend_5m", "N/A"),
            "rsi": data.get("rsi"),
            "macd_hist": data.get("macd_hist", 0),
            "atr": data.get("atr", 0),
            "earnings_date": earnings_date,
            "earnings_days": days_until,
            "earnings_when": earnings_when,
            "earnings_when_desc": earnings_when_desc,
            "momentum_1w": momentum_1w,
            "momentum_1m": momentum_1m,
            "strategy_notes": data.get("strategy_notes", ""),
            "best_strategy": best_strategy,
            # FMP Data (new structure)
            "insider": fmp_data.get("insider", {}),
            "adx": fmp_data.get("adx") if fmp_data.get("adx") else data.get("adx", {}),  # Preserve existing ADX
            "ratings": fmp_data.get("ratings", {}),
            "sector": fmp_data.get("sector", {}),
            "target": fmp_data.get("target", {}),
            "financialScores": fmp_data.get("financialScores", {}),
            "targetSummary": fmp_data.get("targetSummary", {}),
            "congressTrades": fmp_data.get("congressTrades", {}),
            # Extended fundamentals for HotList insight panel / AI context.
            "ownerEarnings": fmp_data.get("ownerEarnings", []),
            "enterpriseValues": fmp_data.get("enterpriseValues", []),
            "incomeGrowth": fmp_data.get("incomeGrowth", []),
            "balanceSheetGrowth": fmp_data.get("balanceSheetGrowth", []),
            "cashFlowGrowth": fmp_data.get("cashFlowGrowth", []),
            "financialGrowth": fmp_data.get("financialGrowth", []),
            "reportingDates": fmp_data.get("reportingDates", []),
            "financialReportsJson": fmp_data.get("financialReportsJson", []),
            "cashFlowAsReported": fmp_data.get("cashFlowAsReported", []),
            "fundamentalSnapshot": fmp_data.get("fundamentalSnapshot", {}),
            "_details_loaded": True,
        }
        return _enrich_hotlist_item(base_item)

    # Parallel processing
    with ThreadPoolExecutor(max_workers=min(len(hot_tickers), 10)) as executor:
        hot_list_data = list(executor.map(get_ticker_hot_data, hot_tickers))

    with _hot_list_cache_lock:
        _hot_list_cache["key"] = cache_key
        _hot_list_cache["ts"] = now_ts
        _hot_list_cache["data"] = hot_list_data

    return [apply_hotlist_defaults(i) for i in hot_list_data if isinstance(i, dict)]


def get_state(
    include_news: bool = True,
    include_hotlist: bool = True,
    hotlist_details: bool = False,
    lean: bool = False,
    live_prices: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Get complete application state for the web UI.
    Returns:
      - tickers
      - signals
      - stats
      - backtest_summary
      - news
      - log
      - refresh_interval_ms

    ``live_prices``: None keeps the historical caller-driven behavior (remote
    refresh allowed unless the request context is cache-only); False forces
    engine/cached prices only — the hot /api/state UI poll passes False so a
    cache miss never performs a sequential whole-watchlist remote refresh.
    """
    tickers_list: List[Dict] = []
    try:
        config = get_shared_config()

        # Load state.json (cached by mtime)
        state_data = _load_json_cached(STATE_PATH, _state_cache, {})

        # /api/state is an aggregate UI route, not a history-export route. Keep its
        # payload bounded for every mode; complete history is explicitly paginated
        # through /api/signals instead.
        signals_data = _load_json_cached(
            SIGNALS_PATH,
            _signals_cache,
            {"open": {}, "closed": []},
            include_closed=False,
        )

        # Show a live current_price + P&L on open signals (they share tickers with the
        # live-priced open positions). Fixes the Signals tab "Waiting for live price".
        _enrich_open_signals_current_price(signals_data)

        allow_remote = True
        if live_prices is False:
            allow_remote = False
        ctx = get_request_context()
        if ctx is not None and bool(getattr(ctx, "cache_only", False)):
            # UI aggregate polls should stay deterministic and avoid remote provider churn.
            allow_remote = False
        aggregate_fast_only = bool(lean or not allow_remote)

        try:
            tickers = build_tickers_list(state_data, signals_data, config, allow_remote=allow_remote)
            tickers_list = tickers
        except Exception as e:
            logger.error(f"Error building tickers list: {e}")
            tickers = []
            tickers_list = []
        signals = build_signals_list(signals_data)
        stats = build_stats(signals_data)

        news = {}
        breaking = []
        market_news = []
        ticker_news = {}

        # Light news status only (full news served by /api/news-tab/state)
        if include_news and not lean:
            try:
                news_enabled = is_agent_enabled("news_tab", config=config.config, default=True)
                if news_enabled:
                    if allow_remote:
                        kickoff_news_tab_agent_init(config.config)
                    status = get_news_tab_agent_status(config.config)
                else:
                    status = {"initializing": False, "error": None}
                news = {
                    "breaking_news": [],
                    "market_news": [],
                    "ticker_news": {},
                    "loading": bool(news_enabled),
                    "disabled": not news_enabled,
                    "initializing": bool(status.get("initializing")),
                    "error": status.get("error"),
                }
            except Exception as e:
                logger.error(f"Error checking NewsTabAgent status: {e}")
                news = {
                    "breaking_news": [],
                    "market_news": [],
                    "ticker_news": {},
                    "loading": True,
                }
        refresh_interval_ms = config.ui.get("refresh_interval_ms", 5000)
        if include_hotlist and not lean:
            open_signals = signals_data.get("open", {})
            if hotlist_details:
                # Aggregate UI GETs are cache-only. Starting the background
                # detail builder here used to escape request attribution and
                # fan out remote provider work after the response returned.
                hot_list = _get_hot_list_details_cached(
                    config, state_data, open_signals, allow_build=allow_remote
                )
            else:
                hot_list = build_hot_list_fast(config, state_data, open_signals, tickers_list)
                cached_details = _get_hot_list_details_cached(config, state_data, open_signals, allow_build=False)
                if cached_details:
                    hot_list = _merge_hot_list_details(hot_list, cached_details)
            hot_list = sort_hotlist_by_priority(hot_list)
        else:
            hot_list = []

        if lean:
            # Keep lean payload truly lean: large closed-history blobs can stall UI JSON parse/render.
            open_raw = signals_data.get("open", {}) if isinstance(signals_data, dict) else {}
            closed_raw = signals_data.get("closed", []) if isinstance(signals_data, dict) else []
            if not isinstance(open_raw, dict):
                open_raw = {}
            if not isinstance(closed_raw, list):
                closed_raw = []

            closed_limit = int((config.ui or {}).get("lean_closed_signals_limit", 30) or 30)
            if closed_limit < 0:
                closed_limit = 0

            def _compact_closed_signal(sig: Dict[str, Any]) -> Dict[str, Any]:
                if not isinstance(sig, dict):
                    return {}
                keep = (
                    "id",
                    "ticker",
                    "action",
                    "type",
                    "entry",
                    "tp",
                    "sl",
                    "created_at",
                    "closed_at",
                    "closedat",
                    "status",
                    "pnl_pct",
                    "pnl_abs",
                    "variant",
                    "variant_mode",
                    "signal_mode",
                    "quality_score",
                    "best_strategy",
                    "signal_thesis",
                    "timeframe",
                    "expected_holding_period",
                    "dollar_risk",
                    "signal_age_seconds",
                    "outcome_data_valid",
                    "outcome_validation",
                )
                out = {}
                for k in keep:
                    if k in sig:
                        out[k] = sig.get(k)
                return out

            compact_closed = [_compact_closed_signal(s) for s in (closed_raw[:closed_limit] if closed_limit else [])]
            compact_closed = [s for s in compact_closed if isinstance(s, dict)]
            compact_signals_raw = {"open": open_raw, "closed": compact_closed}

            from core.api_stats_logger import get_api_stats_history
            response = {
                "tickers": tickers,
                "signals": signals,
                "signals_raw": compact_signals_raw,
                "stats": stats,
                "watchlist": config.watchlist,
                "refresh_interval_ms": refresh_interval_ms,
                "timestamp": get_bucharest_time().isoformat(),
                # Recent fires ride along even in lean responses: the Home-tab
                # core loop polls lean and is the one that toasts them.
                "alerts_triggered": list(state_data.get("alerts_triggered", []) or [])[-10:],
                "api_stats": {
                    "history": get_api_stats_history()[-180:],
                },
                "capital": _build_capital_payload(tickers_list, signals_data, fast_only=aggregate_fast_only),
            }
        else:
            backtest_summary = state_data.get(
                "backtest_summary",
                {"total_wins": 0, "total_losses": 0, "win_pct": 0.0, "loss_pct": 0.0},
            )
            log = state_data.get("log", [])
            if not log or (len(log) == 1 and "System ready" in log[0]):
                # If empty or just placeholder, try to keep previous logs if available or show placeholder
                pass
            if not log:
                log = [f"[{get_bucharest_time().strftime('%H:%M:%S')}] System ready"]

            response = {
                "tickers": tickers,
                "hot_list": hot_list,
                "signals": signals,
                "signals_raw": signals_data,
                "stats": stats,
                "backtest_summary": backtest_summary,
                "news": news,
                "breaking_news": breaking,
                "market_news": market_news,
                "ticker_news": ticker_news,
                "log": log[-120:],
                "activity_log": list(state_data.get("activity_log", []) or [])[:300],
                "watchlist": config.watchlist,
                "refresh_interval_ms": refresh_interval_ms,
                "timestamp": get_bucharest_time().isoformat(),
                "watchlist_events": state_data.get("watchlist_events", []),
                # Recent Smart-Alert fires so the UI can toast/sound them
                # (audit 2026-09-05 P1: the field existed in the snapshot but
                # was stripped here, so fired alerts reached nobody).
                "alerts_triggered": list(state_data.get("alerts_triggered", []) or [])[-10:],
                "capital": _build_capital_payload(tickers_list, signals_data, fast_only=aggregate_fast_only),
                "capital_decisions": _compact_trade_audit(get_trade_audit(limit=10)),
            }

        if not include_news or lean:
            response.pop("news", None)
            response.pop("breaking_news", None)
            response.pop("market_news", None)
            response.pop("ticker_news", None)
        return response

    except Exception as e:
        logger.error(f"Error in get_state: {e}")
        base = {
            "tickers": [],
            "signals": [],
            "stats": {"open": 0, "wins": 0, "losses": 0, "winrate": 0.0},
            "backtest_summary": {},
            "news": {"breaking_news": [], "market_news": [], "ticker_news": {}},
            "log": [f"Error loading state: {str(e)}"],
            "refresh_interval_ms": 5000,
            "timestamp": get_bucharest_time().isoformat(),
        }
        if lean:
            from core.api_stats_logger import get_api_stats_history
            base = {
                "tickers": [],
                "signals": [],
                "stats": {"open": 0, "wins": 0, "losses": 0, "winrate": 0.0},
                "refresh_interval_ms": 5000,
                "timestamp": get_bucharest_time().isoformat(),
                "api_stats": {"history": get_api_stats_history()[-180:]},
            }
        if not include_news or lean:
            base.pop("news", None)
        return base


# Engine row facts forwarded unchanged to the API ticker row (wave 3): which
# session the price belongs to, the date of that price, the timeframe the RSI
# was computed on and the engine's own data-quality verdict.
_ENGINE_ROW_FORWARD_FIELDS = (
    "session",
    "session_label",
    "price_as_of",
    "rsi_timeframe",
    "data_quality",
    "price_available",
    "next_regular_open_utc",
)


def _ticker_row_session_fields(engine_meta: Dict[str, Any], price: Any) -> Dict[str, Any]:
    """Session / quality fields of one API ticker row.

    ``price`` stays numeric on the row (0 = no price) because other code
    compares it; ``price_display_state`` tells the UI what that number is:

      ok                a price the engine did not flag
      last_close        US session closed: the last regular-session close
      listing_inactive  last known price of a symbol the provider dropped
      no_data           no price from any provider (do not print 0.00)
      unavailable       no price and no engine verdict (row not analysed yet)
    """
    meta = engine_meta if isinstance(engine_meta, dict) else {}
    quality = str(meta.get("data_quality") or "").strip().lower()
    session = meta.get("session") or None
    has_price = _safe_float(price, 0.0) > 0
    if quality == "listing_inactive":
        state = "listing_inactive" if has_price else "no_data"
    elif not has_price:
        state = "no_data" if quality == "no_data" else "unavailable"
    elif str(session or "").lower() == "closed":
        state = "last_close"
    else:
        state = "ok"
    return {
        "session": session,
        "session_label": meta.get("session_label") or None,
        "price_as_of": meta.get("price_as_of") or None,
        "rsi_timeframe": meta.get("rsi_timeframe") or None,
        "data_quality": quality or None,
        "price_available": bool(has_price),
        "next_regular_open_utc": meta.get("next_regular_open_utc") or None,
        "price_display_state": state,
    }


def build_tickers_list(state_data: Dict, signals_data: Dict, config: ConfigManager, allow_remote: bool = True) -> List[Dict]:
    """Build list of ticker data for the table."""
    from api.market_data_service import get_ticker_data

    tickers_list: List[Dict] = []
    tickers_data = _canonicalize_ticker_rows(state_data.get("tickers", {}) or {})
    # Session / data-quality facts the engine wrote on its own row. Captured
    # before the fallback loops below, which replace a price-less engine row
    # (data_quality "no_data") wholesale with a placeholder and would drop them.
    engine_row_meta: Dict[str, Dict[str, Any]] = {
        str(_t): {_k: _row.get(_k) for _k in _ENGINE_ROW_FORWARD_FIELDS if _k in _row}
        for _t, _row in tickers_data.items()
        if isinstance(_row, dict)
    }
    open_signals = signals_data.get("open", {}) or {}
    hot_tickers = config.hot_list
    workspace = get_ticker_workspace_store()

    # Use ConfigManager watchlist (already normalized/migrated).
    watchlist = config.watchlist or {}
    stocks = list(watchlist.get("yahoo", []) or watchlist.get("stocks", []) or [])
    crypto = list(watchlist.get("binance", []) or watchlist.get("crypto", []) or watchlist.get("cryptos", []) or [])

    all_watchlist_tickers = list(watchlist.get("order", []) or [])
    if not all_watchlist_tickers:
        all_watchlist_tickers = stocks + crypto
    # Keep order stable, no duplicates.
    normalized_watchlist = []
    display_ticker_map: Dict[str, str] = {}
    for raw_ticker in all_watchlist_tickers:
        display_ticker = str(raw_ticker or "").upper().strip()
        ticker = _canonical_ticker_key(raw_ticker)
        if ticker:
            display_ticker_map.setdefault(ticker, display_ticker or ticker)
            normalized_watchlist.append(ticker)
    all_watchlist_tickers = list(dict.fromkeys(normalized_watchlist))

    # Ensure every watchlist ticker is present in tickers_data (engine or fallback)
    # OPTIMIZATION: Batch fetch missing stocks to avoid sequential requests
    missing_stocks = []
    all_stocks_for_refresh = []  # Non-futures stocks refreshed from FMP.
    all_futures_for_refresh = []  # Commodity futures refreshed from Yahoo GC=F-style symbols.
    for ticker in all_watchlist_tickers:
        ticker = (ticker or "").strip().upper()
        if not ticker: continue
        is_crypto = is_crypto_ticker(ticker)
        uses_yahoo_futures = _uses_yahoo_futures_price(ticker) if not is_crypto else False
        if ticker not in tickers_data or tickers_data.get(ticker, {}).get("price", 0) <= 0:
            ws_market = workspace.get_market_data(ticker)
            ws_price = _safe_float(ws_market.get("price"), 0.0)
            if ws_price and ws_price > 0:
                tickers_data[ticker] = {
                    **dict(ws_market),
                    "price": ws_price,
                    "sector": _sector_for_ticker(ticker),
                    "asset_type": _asset_type_for_ticker(ticker),
                }
                # Still need FMP refresh for this stock
                if not is_crypto:
                    if uses_yahoo_futures:
                        all_futures_for_refresh.append(ticker)
                    else:
                        all_stocks_for_refresh.append(ticker)
                continue
        # Collect ALL non-crypto tickers for price refresh (not just missing)
        if not is_crypto:
            if uses_yahoo_futures:
                all_futures_for_refresh.append(ticker)
            else:
                all_stocks_for_refresh.append(ticker)
        # If not in engine data and is a stock, append to missing
        if (
            not is_crypto
            and not uses_yahoo_futures
            and (ticker not in tickers_data or tickers_data[ticker].get("price", 0) <= 0)
        ):
             missing_stocks.append(ticker)

    # Primary: Batch-refresh ALL stock prices via fast Yahoo chart API.
    # FMP is dead (circuit open / 429) -- always use Yahoo Finance.
    refresh_targets = all_stocks_for_refresh if allow_remote else missing_stocks
    yahoo_primary_targets = all_futures_for_refresh if allow_remote else []
    if allow_remote and (refresh_targets or yahoo_primary_targets):
        updated_stocks = set()

        yahoo_updated = set()
        fallback_stocks = [ticker for ticker in refresh_targets if ticker not in updated_stocks]
        yahoo_targets = list(dict.fromkeys(list(yahoo_primary_targets) + fallback_stocks))
        if yahoo_targets:
            try:
                from api.market_data_service import YahooClient

                for ticker in yahoo_targets:
                    y_data = YahooClient.get_stock_data(ticker, ttl=30) or {}
                    y_price = _safe_float(y_data.get("price"), 0.0)
                    if y_price <= 0:
                        continue
                    yahoo_updated.add(ticker)
                    if ticker in tickers_data:
                        tickers_data[ticker]["price"] = y_price
                        tickers_data[ticker]["change_pct"] = _safe_float(y_data.get("change_pct"), 0.0)
                    else:
                        tickers_data[ticker] = {
                            "price": y_price,
                            "change_pct": _safe_float(y_data.get("change_pct"), 0.0),
                            "sector": _sector_for_ticker(ticker),
                            "asset_type": _asset_type_for_ticker(ticker),
                            "trend_1h": "N/A", "trend_30m": "N/A", "trend_5m": "N/A",
                            "trend_1d": "N/A", "trend_4h": "N/A",
                            "ts_1h": "", "ts_30m": "", "ts_5m": "",
                            "ts_1d": "", "ts_4h": "",
                            "rsi": 50, "macd_hist": 0
                        }
            except Exception as e:
                logger.error(f"Yahoo stock refresh fallback failed: {e}")

        already_refreshed = updated_stocks | yahoo_updated
    else:
        already_refreshed = set()

    # Local, always-on fallback (cheap file read, no network): fill any ticker
    # still missing a fresh price from the persisted live-prices snapshot —
    # including cache-only requests, which never touch the remote refresh.
    cache_candidates = list(dict.fromkeys(list(refresh_targets) + list(yahoo_primary_targets)))
    cache_fallback_stocks = [ticker for ticker in cache_candidates if ticker not in already_refreshed]
    if cache_fallback_stocks:
        try:
            cache_path = DATA_DIR / "live_prices_cache.json"
            if cache_path.exists():
                cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
                cached_prices = cached_payload.get("prices", {}) if isinstance(cached_payload, dict) else {}
                if isinstance(cached_prices, dict):
                    for ticker in cache_fallback_stocks:
                        cached_row = cached_prices.get(ticker) or {}
                        cached_price = _safe_float(cached_row.get("price"), 0.0)
                        if cached_price <= 0:
                            continue
                        if ticker in tickers_data and tickers_data[ticker].get("price", 0) > 0:
                            tickers_data[ticker]["price"] = cached_price
                            tickers_data[ticker]["change_pct"] = _safe_float(cached_row.get("change_pct"), 0.0)
                        else:
                            tickers_data[ticker] = {
                                "price": cached_price,
                                "change_pct": _safe_float(cached_row.get("change_pct"), 0.0),
                                "sector": _sector_for_ticker(ticker),
                                "asset_type": _asset_type_for_ticker(ticker),
                                "trend_1h": "N/A", "trend_30m": "N/A", "trend_5m": "N/A",
                                "trend_1d": "N/A", "trend_4h": "N/A",
                                "ts_1h": "", "ts_30m": "", "ts_5m": "",
                                "ts_1d": "", "ts_4h": "",
                                "rsi": 50, "macd_hist": 0
                            }
        except Exception as e:
            logger.error(f"Live prices cache fallback failed: {e}")

    for ticker in all_watchlist_tickers:
        ticker = (ticker or "").strip().upper()
        if not ticker:
            continue

        is_crypto = is_crypto_ticker(ticker)
        asset_type = _asset_type_for_ticker(ticker)

        # Single source of truth for sector + normalize display
        sector = _sector_for_ticker(ticker)
        ws_market = workspace.get_market_data(ticker)
        ws_price = _safe_float(ws_market.get("price"), 0.0)
        if (ticker not in tickers_data or tickers_data.get(ticker, {}).get("price", 0) <= 0) and ws_price and ws_price > 0:
            tickers_data[ticker] = {
                **dict(ws_market),
                "price": ws_price,
                "sector": sector,
                "asset_type": asset_type,
            }

        if ticker in tickers_data and tickers_data[ticker].get("price", 0) > 0:
            # Engine data exists -> only enforce consistency
            tickers_data[ticker]["sector"] = sector
            tickers_data[ticker]["asset_type"] = asset_type
            if asset_type == "commodity":
                no_intraday_coverage = (
                    str(tickers_data[ticker].get("trend_5m") or "N/A").upper() == "N/A"
                    and not str(tickers_data[ticker].get("ts_5m") or "").strip()
                )
                if no_intraday_coverage:
                    if tickers_data[ticker].get("rsi") in (None, 50, 50.0):
                        tickers_data[ticker]["rsi"] = None
                    if tickers_data[ticker].get("macd_hist") in (None, 0, 0.0):
                        tickers_data[ticker]["macd_hist"] = None
        else:
            # Fallback: only fetch live for crypto (fast) OR open signals (critical). Stocks stay placeholder otherwise.
            is_open_signal = ticker in open_signals
            if allow_remote and (is_crypto or is_open_signal):
                # Use lightweight only if we already have trend data (fixes missing KuCoin trends)
                has_trend = ticker in tickers_data and tickers_data.get(ticker, {}).get('trend_1h') not in (None, 'N/A', '')
                use_lightweight = has_trend and not is_open_signal
                live_data = get_ticker_data(ticker, lightweight=use_lightweight)
                tickers_data[ticker] = {
                    "price": live_data.get("price", 0),
                    "change_pct": live_data.get("change_pct", 0),

                    "trend_1h": live_data.get("trend_1h", "N/A"),
                    "trend_30m": live_data.get("trend_30m", "N/A"),
                    "trend_5m": live_data.get("trend_5m", "N/A"),
                    "trend_1d": live_data.get("trend_1d", "N/A"),
                    "trend_4h": live_data.get("trend_4h", "N/A"),

                    "ts_1h": live_data.get("ts_1h", ""),
                    "ts_30m": live_data.get("ts_30m", ""),
                    "ts_5m": live_data.get("ts_5m", ""),
                    "ts_1d": live_data.get("ts_1d", ""),
                    "ts_4h": live_data.get("ts_4h", ""),

                    "rsi": live_data.get("rsi", 50),
                    "macd_hist": live_data.get("macd_hist", 0),

                    "sector": sector,
                    "asset_type": asset_type,
                }
            else:
                tickers_data[ticker] = {
                    "price": 0,
                    "change_pct": 0,
                    "trend_1h": "N/A",
                    "trend_30m": "N/A",
                    "trend_5m": "N/A",
                    "trend_1d": "N/A",
                    "trend_4h": "N/A",
                    "ts_1h": "",
                    "ts_30m": "",
                    "ts_5m": "",
                    "ts_1d": "",
                    "ts_4h": "",
                    "rsi": 50,
                    "macd_hist": 0,
                    "sector": sector,
                    "asset_type": asset_type,
                }
            
        # Add per-ticker settings payload used by UI.
        t_settings = config.get_ticker_settings(ticker)
        effective = config.get_effective_ticker_settings(ticker) if hasattr(config, "get_effective_ticker_settings") else t_settings
        tickers_data[ticker]["strategy_notes"] = t_settings.get("strategy_notes", "")
        tickers_data[ticker]["signals_enabled"] = bool(effective.get("signals_enabled", True))
        tickers_data[ticker]["allowed_strategies"] = list(effective.get("allowed_strategies", []) or [])
        tickers_data[ticker]["min_win_rate"] = float(effective.get("min_win_rate", 0) or 0)
        tickers_data[ticker]["tp_pct"] = effective.get("tp_pct")
        tickers_data[ticker]["sl_pct"] = effective.get("sl_pct")

    # Prepare earnings snapshot for stocks.
    earnings_snapshot: Dict[str, Dict[str, Any]] = {}
    calendar = None
    try:
        from agents.economic_calendar_agent import get_economic_calendar_agent
        calendar = get_economic_calendar_agent(config.config)
    except Exception as e:
        logger.debug(f"Calendar agent unavailable in build_tickers_list: {e}")
    today = datetime.now(timezone.utc).date()
    for ticker in all_watchlist_tickers:
        is_crypto = is_crypto_ticker(ticker)
        if is_crypto:
            earnings_snapshot[ticker] = {
                "earnings_date": "N/A",
                "earnings_days": -1,
                "earnings_when": "",
                "earnings_when_desc": "",
            }
            continue
        if calendar is not None:
            ed, days, when, when_desc = _resolve_earnings_from_sources(
                calendar,
                ticker,
                today,
                allow_remote=allow_remote,
            )
        else:
            ed, days, when, when_desc = "N/A", -1, "", ""
        earnings_snapshot[ticker] = {
            "earnings_date": ed,
            "earnings_days": int(days) if isinstance(days, (int, float)) else -1,
            "earnings_when": (when or "").upper(),
            "earnings_when_desc": when_desc or "",
        }

    # Persist the earnings snapshot to the SQLite store under the key the engine
    # reads (core/engine.py::_analyze_ticker does state_store.get_json(
    # "watchlist_snapshot")). Pre-fix that read site NEVER found a writer, so
    # earnings_days was always -1 -> hours_to_event always 999.0 -> the V2
    # event_block quality gate (block entries within event_block_hours of
    # earnings) was inert for every pre-earnings ticker. Writing here on the
    # hot-list refresh makes the event_block live. Reuses the calendar
    # resolution already performed above (cache-first, no new API call).
    if allow_remote:
        try:
            from core.sqlite_store import get_sqlite_store
            _store = get_sqlite_store()
            if _store is not None and earnings_snapshot:
                _store.set_json("watchlist_snapshot", earnings_snapshot)
        except Exception as e:
            logger.debug(f"watchlist_snapshot persist skipped: {e}")

    # Aggregate watchlist news pulse once (cached in core.news_pulse).
    # Always call build_news_pulse_bulk - it uses an in-process TTL cache so it
    # returns instantly when data is already loaded.  Passing allow_blocking=False
    # ensures we never block the fast UI poll path waiting on the news agent.
    pulse_ttl = int((config.news_pulse or {}).get("ttl_seconds", 45))
    try:
        news_pulse_map = build_news_pulse_bulk(
            all_watchlist_tickers,
            ttl_seconds=pulse_ttl,
            allow_blocking=allow_remote,
        )
    except TypeError:
        news_pulse_map = build_news_pulse_bulk(
            all_watchlist_tickers,
            ttl_seconds=pulse_ttl,
        )

    def _trend_bucket(value: Any) -> str:
        text = str(value or "").upper()
        if "BULL" in text:
            return "BULL"
        if "BEAR" in text:
            return "BEAR"
        if "NEUTRAL" in text:
            return "NEUTRAL"
        return "UNKNOWN"

    def _trend_alignment_label(day: Any, h4: Any, h1: Any) -> str:
        d1 = _trend_bucket(day)
        f4 = _trend_bucket(h4)
        one = _trend_bucket(h1)
        vals = [x for x in (d1, f4, one) if x in {"BULL", "BEAR", "NEUTRAL"}]
        if len(vals) < 2:
            return "UNKNOWN"
        if vals.count("BULL") >= 2:
            return "BULL_ALIGN"
        if vals.count("BEAR") >= 2:
            return "BEAR_ALIGN"
        return "MIXED"

    # Map ticker -> most recent open signal.
    signal_map: Dict[str, dict] = {}
    for sig in _valid_open_signal_rows(open_signals):
        t = (sig.get("ticker", "") or "").upper()
        if not t:
            continue
        prev = signal_map.get(t)
        if prev is None:
            signal_map[t] = sig
            continue
        if str(sig.get("created_at", "")) >= str(prev.get("created_at", "")):
            signal_map[t] = sig

    # Build final list (only watchlist)
    for ticker in all_watchlist_tickers:
        ticker = (ticker or "").strip().upper()
        if not ticker:
            continue

        data = tickers_data.get(ticker, {})
        if not data:
            continue

        # Tag
        tag = ""
        if ticker in signal_map:
            tag = "signal"
        else:
            trend_5m = (data.get("trend_5m", "") or "").upper()
            if "BULL" in trend_5m:
                tag = "bull"
            elif "BEAR" in trend_5m:
                tag = "bear"

        # Signal string
        sig_str = ""
        if ticker in signal_map:
            sig = signal_map[ticker]
            variant = sig.get("variant", "")
            action = sig.get("action", sig.get("type", ""))
            time_str = sig.get("created_at_time", "")
            tp = sig.get("tp", 0)
            sl = sig.get("sl", 0)

            bt_info = ""
            bt_30d = sig.get("quick_backtest_30d", {}) or {}
            bt_7d = sig.get("quick_backtest_7d", {}) or {}
            quick_bt = bt_30d if (bt_30d.get("total", 0) or 0) > 0 else (bt_7d or bt_30d)
            if quick_bt:
                bt_info = f" BT{quick_bt.get('win_rate', 0):.0f}W"

            sig_str = f"{variant} {action} {time_str} TP{tp:.2f} SL{sl:.2f}{bt_info}"

        # Price: keep numeric for UI sorting; provide display string if needed
        price = data.get("price", 0)
        if price:
            if price >= 1000:
                price_str = f"{price:.2f}"
            elif price >= 1:
                price_str = f"{price:.2f}"
            else:
                price_str = f"{price:.4f}"
        else:
            price_str = "N/A"

        def format_mtf(trend_key: str, ts_key: str) -> str:
            trend = data.get(trend_key, "N/A")
            ts = data.get(ts_key, "")
            return f"{trend} {ts}" if ts else trend

        sig_payload = signal_map.get(ticker) or {}
        best_strategy = sig_payload.get("best_strategy", {}) or {}
        signal_status = str(sig_payload.get("status") or ("open" if sig_payload else "none")).lower()
        has_open_signal = bool(sig_payload)
        signal_score = _extract_watchlist_signal_score(sig_payload)
        signal_action = str(sig_payload.get("action") or sig_payload.get("type") or "").upper()
        if signal_action == "BUY":
            signal_direction = "LONG"
        elif signal_action in {"SHORT", "SELL"}:
            signal_direction = "SHORT"
        else:
            signal_direction = ""

        pulse = news_pulse_map.get(ticker) or {}
        pulse_24h = pulse.get("24h", {}) or {}
        pulse_7d = pulse.get("7d", {}) or {}
        news_score = int(pulse.get("score", 0) or 0)
        news_sentiment = str(pulse.get("sentiment_trend") or "unknown")

        earnings = earnings_snapshot.get(ticker, {})
        earnings_days_val = earnings.get("earnings_days", -1)
        earnings_days = int(earnings_days_val) if earnings_days_val is not None and earnings_days_val != "" else -1
        earnings_when = str(earnings.get("earnings_when") or "").upper()

        trend_alignment = _trend_alignment_label(data.get("trend_1d"), data.get("trend_4h"), data.get("trend_1h"))
        trend_regime_1d = _trend_bucket(data.get("trend_1d"))
        trend_regime_4h = _trend_bucket(data.get("trend_4h"))

        # Watchlist honesty (tab audit 2026-11-15): say WHEN the engine last
        # analysed this ticker (trend/RSI/MACD carry only a time-of-day stamp,
        # so a months-old row looked current) and whether any analysis exists
        # at all (rows without one hold placeholder RSI 50 / MACD 0).
        analysis_updated_at = str(data.get("last_update") or "").strip()
        analysis_age_seconds = None
        if analysis_updated_at:
            try:
                _analysed = datetime.fromisoformat(analysis_updated_at.replace("Z", "+00:00"))
                if _analysed.tzinfo is None:
                    _analysed = _analysed.replace(tzinfo=timezone.utc)
                analysis_age_seconds = round(
                    max(0.0, (datetime.now(timezone.utc) - _analysed).total_seconds()), 1
                )
            except (TypeError, ValueError):
                analysis_age_seconds = None
        analysis_available = any(
            _trend_bucket(data.get(_tf)) != "UNKNOWN"
            for _tf in ("trend_1d", "trend_4h", "trend_1h", "trend_30m", "trend_5m")
        )

        risk_flags: List[str] = []
        if 0 <= earnings_days <= 5:
            risk_flags.append("earnings_soon")
        if not bool(data.get("signals_enabled", True)):
            risk_flags.append("signal_disabled")
        if news_sentiment == "bearish" and news_score >= 60:
            risk_flags.append("high_news_risk")
        if int(pulse_24h.get("breaking_count", 0) or 0) > 0 and int(pulse_24h.get("avg_impact_score", 0) or 0) >= 80:
            risk_flags.append("breaking_news")
        if trend_alignment == "MIXED":
            risk_flags.append("trend_conflict")

        pl_value = 0.0
        if has_open_signal and price > 0:
            entry_price = sig_payload.get("entry", 0)
            if entry_price > 0:
                if signal_direction == "LONG":
                    pl_value = ((price - entry_price) / entry_price) * 100
                elif signal_direction == "SHORT":
                    pl_value = ((entry_price - price) / entry_price) * 100

        market_regime = "range"
        if sig_payload and sig_payload.get("market_regime"):
            mr = str(sig_payload.get("market_regime", "")).upper()
            if mr == "TREND":
                market_regime = "trending"
            elif mr == "VOLATILE":
                market_regime = "volatile"
            else:
                market_regime = "range"
                
        tickers_list.append(
            {
                "sector": data.get("sector", "Other"),
                "ticker": display_ticker_map.get(ticker, ticker),
                "price": price,
                "price_display": price_str,
                "col_1h": format_mtf("trend_1h", "ts_1h"),
                "col_30m": format_mtf("trend_30m", "ts_30m"),
                "col_5m": format_mtf("trend_5m", "ts_5m"),
                "trend_1d": data.get("trend_1d", "N/A"),
                "trend_4h": data.get("trend_4h", "N/A"),
                "ts_1d": data.get("ts_1d", ""),
                "ts_4h": data.get("ts_4h", ""),
                "signal": sig_str,
                "tag": tag,
                "asset_type": data.get("asset_type", "stock"),
                "strategy_notes": data.get("strategy_notes", ""),
                # Additional data for hover tooltip
                "change_pct": _safe_float(data.get("change_pct"), 0.0),
                "rsi": _safe_float(data.get("rsi"), 50.0) if data.get("rsi") is not None else None,
                "macd_hist": _safe_float(data.get("macd_hist"), 0.0),
                "raw_price": _safe_float(data.get("price"), 0.0),
                "is_hot": ticker in hot_tickers,
                "best_strategy": best_strategy,
                "signals_enabled": bool(data.get("signals_enabled", True)),
                "allowed_strategies": data.get("allowed_strategies", []),
                "min_win_rate": data.get("min_win_rate", 0),
                "tp_pct": data.get("tp_pct"),
                "sl_pct": data.get("sl_pct"),
                "has_open_signal": has_open_signal,
                "signal_status": signal_status,
                "signal_action": signal_action,
                "signal_direction": signal_direction,
                "signal_score": round(signal_score, 2),
                "pl_value": round(pl_value, 2),
                "market_regime": market_regime,
                "trend_alignment": trend_alignment,
                "trend_regime_1d": trend_regime_1d,
                "trend_regime_4h": trend_regime_4h,
                "analysis_available": analysis_available,
                "analysis_updated_at": analysis_updated_at or None,
                "analysis_age_seconds": analysis_age_seconds,
                # session, session_label, price_as_of, rsi_timeframe,
                # data_quality, price_available, next_regular_open_utc,
                # price_display_state
                **_ticker_row_session_fields(engine_row_meta.get(ticker) or {}, price),
                "earnings_date": earnings.get("earnings_date", "N/A"),
                "earnings_days": earnings_days,
                "earnings_when": earnings_when,
                "earnings_when_desc": earnings.get("earnings_when_desc", ""),
                "news_pulse": pulse,
                "news_pulse_score": news_score,
                "news_actionability": int(pulse_24h.get("avg_actionability", 0) or 0),
                "news_sentiment_trend": news_sentiment,
                "risk_flags": risk_flags,
                "top_risk_flag": risk_flags[0] if risk_flags else "",
                "quick_ai": None,
                # Signal intelligence fields
                "signal_thesis": sig_payload.get("signal_thesis", ""),
                "timeframe": sig_payload.get("timeframe", ""),
                "expected_holding_period": sig_payload.get("expected_holding_period", ""),
                "dollar_risk": sig_payload.get("dollar_risk", 0),
                "signal_age_seconds": sig_payload.get("signal_age_seconds", 0),
            }
        )
        # PERF FIX: skip workspace persistence in fast path (allow_remote=False).
        # Each upsert serializes + writes the entire workspace JSON to disk.
        # With 49 tickers, that's 49 full file writes = 5+ seconds.
        if allow_remote:
          try:
            workspace.upsert_market_data(
                ticker,
                {
                    "price": _safe_float(price, 0.0),
                    "change_pct": _safe_float(data.get("change_pct"), 0.0),
                    "trend_1h": data.get("trend_1h", "N/A"),
                    "trend_30m": data.get("trend_30m", "N/A"),
                    "trend_5m": data.get("trend_5m", "N/A"),
                    "trend_1d": data.get("trend_1d", "N/A"),
                    "trend_4h": data.get("trend_4h", "N/A"),
                    "rsi": _safe_float(data.get("rsi"), 50.0),
                    "macd_hist": _safe_float(data.get("macd_hist"), 0.0),
                    "asset_type": data.get("asset_type", asset_type),
                    "sector": data.get("sector", sector),
                },
            )
          except Exception:
            pass

    # tickers_list.sort(key=lambda x: (x["sector"], x["ticker"]))
    return tickers_list


def build_signals_list(signals_data: Dict) -> List[Dict]:
    """Build list of signal spans for display."""
    signals_list = []

    signals_list.append({"text": "=" * 55 + "\n", "tag": "header"})
    signals_list.append({"text": " OPEN SIGNALS\n", "tag": "header"})
    signals_list.append({"text": "=" * 55 + "\n", "tag": "header"})

    open_signals = _valid_open_signal_rows(signals_data.get("open", {}))
    if not open_signals:
        signals_list.append({"text": "\n No open signals\n", "tag": "info"})
    else:
        sorted_signals = sorted(
            open_signals,
            key=lambda x: x.get("created_at", ""),
            reverse=True,
        )
        for sig in sorted_signals:
            signals_list.extend(format_signal_spans(sig))

    signals_list.append({"text": "\n" + "=" * 55 + "\n", "tag": "header"})
    signals_list.append({"text": " INCHISE (ultimele 10)\n", "tag": "header"})
    signals_list.append({"text": "=" * 55 + "\n", "tag": "header"})

    closed_signals = signals_data.get("closed", [])
    if not closed_signals:
        signals_list.append({"text": "\n No closed signals\n", "tag": "info"})
    else:
        recent_closed = sorted(
            closed_signals,
            key=lambda x: _pick(x, "closedat", "closed_at", "createdat", "created_at", default=""),
            reverse=True,
        )[:10]

        for sig in recent_closed:
            signals_list.extend(format_closed_signal_spans(sig)
            )

    return signals_list


def _setup_score_text(value: Any) -> str:
    """Label the engine's heuristic `probability_score` for display.

    It is a rule-based setup score, not a calibrated chance of winning, so
    it is never printed as a probability. Stored as 0..1 or 0..100."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "setup score n/a"
    if score != score or score <= 0:
        return "setup score n/a"
    if score <= 1:
        score *= 100.0
    return f"setup score {min(score, 100.0):.0f}/100 (not a probability)"


def format_signal_spans(sig: Dict) -> List[Dict]:
    """Format a single signal into display spans."""
    spans = []
    signal_id = sig.get("id", "")
    variant = sig.get("variant", "C")
    variant_mode = str(sig.get("variant_mode", "STRICT")).upper()
    action = sig.get("action", sig.get("type", ""))
    ticker = sig.get("ticker", "")
    time_str = sig.get("created_at_time", "")
    signal_mode = sig.get("signal_mode", "PULLBACK")  # Trade type: PULLBACK, BREAKOUT, CONTRA
    entry = sig.get("entry", 0)
    tp = sig.get("tp", 0)
    sl = sig.get("sl", 0)
    tp_pct = sig.get("tp_pct", 0)
    sl_pct = sig.get("sl_pct", 0)
    exit_mode = str(sig.get("exit_mode", "") or "").upper()
    atr_period = sig.get("atr_period")
    atr_tf = sig.get("atr_exit_timeframe") or sig.get("atr_timeframe") or ""
    atr_exit = sig.get("atr_exit")
    tp_mult = sig.get("tp_atr_mult")
    sl_mult = sig.get("sl_atr_mult")
    prob_val = sig.get("probability_score", 0)
    regime = sig.get("market_regime", "")
    size_factor = sig.get("size_factor")
    position_size_pct = sig.get("position_size_pct")
    rsi = sig.get("rsi", 0)
    macd_hist = sig.get("macd_hist", 0)
    mtf_score = sig.get("mtf_score", 0)
    quality = sig.get("quality_score", 0)
    signal_thesis = sig.get("signal_thesis", "")
    timeframe = sig.get("timeframe", "")
    expected_holding_period = sig.get("expected_holding_period", "")
    dollar_risk = sig.get("dollar_risk", 0)
    signal_age_seconds = sig.get("signal_age_seconds", 0)
    
    # Best strategy info
    best_strategy = sig.get("best_strategy", {}) or {}
    best_str = ""
    if best_strategy and best_strategy.get("total_signals", 0) > 0:
        bs_risk = best_strategy.get("risk_mode", "")
        bs_trade = best_strategy.get("trade_mode", "")
        # Map risk mode to variant letter
        variant_map = {"STRONG": "A", "MEDIUM": "B", "RELAX": "C"}
        bs_variant = variant_map.get(bs_risk, "?")
        best_str = f" -- BEST: {bs_risk} [{bs_variant}] / {bs_trade}"

    variant_tag = f"variant-{variant.lower()}" if variant else "info"
    action_tag = "buy" if action == "BUY" else "short"

    spans.append({"text": "\n" + "-" * 55 + "\n", "tag": "info"})
    # Include signal_id, trade type (signal_mode), and best strategy in title
    spans.append({"text": f" {time_str} - [{variant}|{variant_mode}] {action} {ticker} - {signal_mode}{best_str}\n", "tag": variant_tag, "signal_id": signal_id, "ticker": ticker})
    spans.append(
        {
            "text": f" Entry: ${entry:.4f} | TP: ${tp:.4f} (+{tp_pct:.1f}%) | SL: ${sl:.4f} (-{sl_pct:.1f}%)\n",
            "tag": action_tag,
        }
    )
    if exit_mode == "ATR":
        try:
            atr_exit_f = float(atr_exit) if atr_exit is not None else 0.0
        except Exception:
            atr_exit_f = 0.0
        spans.append(
            {
                "text": f" Exit: ATR P{atr_period} {atr_tf} ATR={atr_exit_f:.6f} TPx{tp_mult} SLx{sl_mult}\n",
                "tag": "info",
            }
        )
    elif exit_mode:
        spans.append(
            {
                "text": f" Exit: {exit_mode} (TP%={tp_pct:.1f} SL%={sl_pct:.1f})\n",
                "tag": "info",
            }
        )
    try:
        prob_f = float(prob_val) if prob_val is not None else 0.0
    except Exception:
        prob_f = 0.0
    try:
        size_f = float(size_factor) if size_factor is not None else None
    except Exception:
        size_f = None
    try:
        risk_f = float(position_size_pct) if position_size_pct is not None else None
    except Exception:
        risk_f = None
    if prob_f or regime or size_f is not None or risk_f is not None:
        size_txt = "-" if size_f is None else f"{size_f:.2f}x"
        risk_txt = "-" if risk_f is None else f"{risk_f:.2f}%"
        spans.append(
            {
                "text": f" {_setup_score_text(prob_f).capitalize()} | Regime: {regime} | Size: {size_txt} ({risk_txt})\n",
                "tag": "info",
            }
        )
    spans.append(
        {"text": f" RSI: {rsi:.1f} | MACD_H: {macd_hist:.6f} | MTF: {mtf_score} | Q: {quality}\n", "tag": "info"}
    )

    # Signal intelligence: thesis, timeframe, holding period
    if signal_thesis:
        spans.append({"text": f" Thesis: {signal_thesis}\n", "tag": "info"})
    tf_parts = []
    if timeframe:
        tf_parts.append(f"TF: {timeframe}")
    if expected_holding_period:
        tf_parts.append(f"Hold: {expected_holding_period}")
    if dollar_risk:
        try:
            dr_f = float(dollar_risk)
            tf_parts.append(f"Risk$: ${dr_f:.2f}")
        except Exception:
            pass
    if signal_age_seconds:
        try:
            age_f = float(signal_age_seconds)
            if age_f < 60:
                tf_parts.append(f"Age: {age_f:.0f}s")
            elif age_f < 3600:
                tf_parts.append(f"Age: {age_f/60:.1f}m")
            else:
                tf_parts.append(f"Age: {age_f/3600:.1f}h")
        except Exception:
            pass
    if tf_parts:
        spans.append({"text": f" {' | '.join(tf_parts)}\n", "tag": "info"})

    trigger = sig.get("trigger_info", {}) or {}
    mtf_1h = trigger.get("mtf_1h", "N/A")
    mtf_30m = trigger.get("mtf_30m", "N/A")
    mtf_5m = trigger.get("mtf_5m", "N/A")
    spans.append({"text": f" MTF: 1H={mtf_1h} | 30M={mtf_30m} | 5M={mtf_5m}\n", "tag": "info"})

    reasons = trigger.get("reasons", []) or []
    if reasons:
        spans.append({"text": f" {', '.join(reasons)}\n", "tag": "info"})

    if sig.get("strict_backed"):
        sb_variant = sig.get("strict_variant", "?")
        sb_mode = sig.get("strict_signal_mode", "")
        sb_score = sig.get("strict_signal_score", "")
        sb_prob = sig.get("strict_probability_score", "")
        sb_mtf = sig.get("strict_mtf_alignment", "")
        spans.append(
            {
                "text": f" OK Backed by STRICT [{sb_variant}] {sb_mode} | Score {sb_score} | {_setup_score_text(sb_prob)} | MTF {sb_mtf}\n",
                "tag": "info",
            }
        )

    if sig.get("strict_replaced"):
        rep = sig.get("replaced_signal") or {}
        rep_action = rep.get("action", "")
        rep_variant = rep.get("variant", "?")
        rep_mode = rep.get("variant_mode", "")
        rep_score = rep.get("signal_score", "")
        spans.append(
            {
                "text": f" [STRICT] Replaced [{rep_variant}|{rep_mode}] {rep_action} (score {rep_score})\n",
                "tag": "info",
            }
        )

    # Get signal's strategy info
    signal_risk = sig.get("risk_mode", "MEDIUM")
    signal_trade = sig.get("signal_mode", "PULLBACK")
    
    # Best strategy from all 9 combinations
    best_strategy = sig.get("best_strategy", {}) or {}
    bs_risk = best_strategy.get("risk_mode", "")
    bs_trade = best_strategy.get("trade_mode", "")
    
    # Check if signal matches BEST strategy
    is_optimal = (signal_risk == bs_risk and signal_trade == bs_trade)
    
    # Backtest for current signal's strategy
    bt_30d = sig.get("quick_backtest_30d", {}) or {}
    bt_7d = sig.get("quick_backtest_7d", {}) or {}
    quick_bt = bt_30d if (bt_30d.get("total", 0) or 0) > 0 else (bt_7d or bt_30d)
    if quick_bt and quick_bt.get("total", 0) > 0:
        wins = quick_bt.get("wins", 0)
        losses = quick_bt.get("losses", 0)
        total = quick_bt.get("total", 0)
        win_rate = quick_bt.get("win_rate", 0)
        risk_mode = quick_bt.get("risk_mode", "MEDIUM")
        trade_mode_bt = quick_bt.get("trade_mode", "")
        bt_label = f"{risk_mode}/{trade_mode_bt}" if trade_mode_bt else risk_mode
        bt_days = "30d" if quick_bt is bt_30d else "7d"
        
        # Use special tag if signal matches BEST
        bt_tag = "backtest-optimal" if is_optimal else "backtest"
        prefix = "OK " if is_optimal else " "
        
        spans.append(
            {
                "text": f"{prefix}Backtest {bt_days} ({bt_label}): WIN {wins}/{total} ({win_rate:.1f}%) | "
                        f"LOSS {losses}/{total} ({100 - win_rate:.1f}%)\n",
                "tag": bt_tag,
            }
        )

    # Best strategy display
    if best_strategy and best_strategy.get("total_signals", 0) > 0:
        bs_win_rate = best_strategy.get("win_rate", 0)
        bs_wins = best_strategy.get("wins", 0)
        bs_losses = best_strategy.get("losses", 0)
        bs_total = best_strategy.get("total_signals", 0)
        variant_map = {"STRONG": "A", "MEDIUM": "B", "RELAX": "C"}
        bs_variant = variant_map.get(bs_risk, "?")
        
        # Same format, different tag/emoji if signal matches BEST
        if is_optimal:
            spans.append(
                {
                    "text": f" BEST: {bs_risk} [{bs_variant}] / {bs_trade} - {bs_win_rate:.1f}% win ({bs_wins}W/{bs_losses}L, {bs_total} signals) == OPTIMAL ==\n",
                    "tag": "best-strategy-match",
                }
            )
        else:
            spans.append(
                {
                    "text": f" BEST: {bs_risk} [{bs_variant}] / {bs_trade} - {bs_win_rate:.1f}% win ({bs_wins}W/{bs_losses}L, {bs_total} signals)\n",
                    "tag": "best-strategy",
                }
            )

    return spans


def _valid_open_signal_rows(open_signals: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not isinstance(open_signals, dict):
        return rows
    now = get_bucharest_time()
    for sig in open_signals.values():
        if not isinstance(sig, dict):
            continue
        ticker = str(sig.get("ticker") or "").upper().strip()
        if not ticker:
            continue
        has_payload = any(
            sig.get(key) not in (None, "", 0, 0.0)
            for key in ("id", "action", "type", "created_at", "entry", "tp", "sl")
        )
        if not has_payload:
            continue
        # Refresh signal_age_seconds dynamically for open signals
        created_at_str = sig.get("created_at", "")
        if created_at_str:
            try:
                from datetime import datetime as _dt
                created_dt = _dt.fromisoformat(created_at_str.replace("Z", "+00:00"))
                if created_dt.tzinfo is None:
                    created_dt = BUCHAREST_TZ.localize(created_dt)
                sig["signal_age_seconds"] = round(max(0, (now - created_dt).total_seconds()), 1)
            except Exception:
                pass
        # Compute dollar_risk if not present
        if "dollar_risk" not in sig or sig.get("dollar_risk") is None:
            try:
                invested = float(sig.get("invested_amount", 0) or 0)
                risk_pct = float(sig.get("risk_pct_used", sig.get("base_risk_pct", 0)) or 0)
                sig["dollar_risk"] = round(invested * (risk_pct / 100.0), 2) if invested > 0 and risk_pct > 0 else 0.0
            except Exception:
                sig["dollar_risk"] = 0.0
        rows.append(sig)
    return rows


def build_stats(signals_data: Dict) -> Dict:
    """Build statistics from signals data."""
    open_signals = signals_data.get("open", {})
    closed_signals = signals_data.get("closed", [])

    summary = signals_data.get("_summary") if isinstance(signals_data, dict) else None
    if isinstance(summary, dict):
        open_count = int(summary.get("open_count") or 0)
        closed_count = int(summary.get("closed_count") or 0)
        wins = int(summary.get("wins") or 0)
        losses = int(summary.get("losses") or 0)
        total = wins + losses
        return {
            "open": open_count,
            "closed": closed_count,
            "wins": wins,
            "losses": losses,
            "winrate": round(wins / total * 100, 1) if total > 0 else 0.0,
            "verified_outcomes": int(summary.get("verified_outcomes") or total),
            "unverified_outcomes": int(summary.get("unverified_outcomes") or 0),
            "performance_basis": str(summary.get("performance_basis") or "verified_price_provenance"),
        }

    open_count = sum(1 for s in open_signals.values() if str(s.get("status", "")) in {"Pending", "Open"})
    verified_closed = [
        s for s in closed_signals
        if (
            isinstance(s, dict)
            and s.get("outcome_data_valid") is True
            and s.get("research_only") is not True
        )
    ]
    wins = sum(1 for s in verified_closed if s.get("status") == "WIN")
    losses = sum(1 for s in verified_closed if s.get("status") == "LOST")
    total = wins + losses
    observed_outcomes = sum(
        1 for s in closed_signals
        if isinstance(s, dict) and str(s.get("status", "")).upper() in {"WIN", "LOST"}
    )
    winrate = round(wins / total * 100, 1) if total > 0 else 0.0

    return {
        "open": open_count,
        "closed": len(closed_signals),
        "wins": wins,
        "losses": losses,
        "winrate": winrate,
        "verified_outcomes": total,
        "unverified_outcomes": max(0, observed_outcomes - total),
        "performance_basis": "verified_price_provenance",
    }
