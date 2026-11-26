"""
Dashboard Service for Home Cockpit V8.
Keeps dashboard computation isolated from /api/state.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from agents.news_tab_agent import get_news_tab_agent_if_ready
from api.state_service import _valid_open_signal_rows, build_tickers_list, get_shared_config
from core.request_context import get_request_context
from core.state_persistence import load_signals_payload, load_state_payload
from core.ticker_workspace import get_ticker_workspace_store

logger = logging.getLogger("MarketSignalsDashboard")

_DASHBOARD_CACHE_TTL_SECONDS = 20
_dashboard_cache_lock = threading.Lock()
_dashboard_cache = {"ts": 0.0, "sig": "", "data": {}}

# News shown on the dashboard/cockpit must be CURRENT. The news agent ranks
# its whole retained corpus by impact with no time window, so a pool larger
# than the display limit is read and filtered here by publication time.
_DASHBOARD_NEWS_LIMIT = 12
_DASHBOARD_NEWS_POOL = 150
_DASHBOARD_NEWS_MAX_AGE_SECONDS = 72 * 3600  # spans a weekend
# The news agent holds its cache lock while it refreshes; a dashboard build
# must not wait on that for longer than this.
_NEWS_READ_TIMEOUT_SECONDS = 2.0
_news_read_lock = threading.Lock()
_news_read_state: Dict[str, Any] = {"inflight": None, "ts": 0.0, "items": None, "narrative": None}


def _headline_key(title: Any) -> str:
    """Normalized headline used to collapse re-posts of one story."""
    return " ".join("".join(ch if ch.isalnum() else " " for ch in str(title or "").casefold()).split())


def _select_current_news(
    items: Any,
    now: Optional[datetime] = None,
    limit: int = _DASHBOARD_NEWS_LIMIT,
    max_age_seconds: float = _DASHBOARD_NEWS_MAX_AGE_SECONDS,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Keep the upstream ranking but drop what is not current news.

    Removed: items older than the window, items without a publication time,
    items the news agent flagged as duplicates, and repeated headlines (the
    same post published twice under two ids).
    """
    current = now or datetime.now(timezone.utc)
    kept: List[Dict[str, Any]] = []
    seen: set = set()
    dropped = {"stale": 0, "undated": 0, "duplicates": 0}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        published = _parse_iso_utc(item.get("published_at"))
        if published is None:
            dropped["undated"] += 1
            continue
        if (current - published).total_seconds() > max_age_seconds:
            dropped["stale"] += 1
            continue
        key = _headline_key(item.get("title"))
        if item.get("is_duplicate") is True or (key and key in seen):
            dropped["duplicates"] += 1
            continue
        if key:
            seen.add(key)
        if len(kept) < max(1, int(limit or _DASHBOARD_NEWS_LIMIT)):
            kept.append(item)
    return kept, {
        "window_hours": int(max_age_seconds // 3600),
        "dropped_stale": dropped["stale"],
        "dropped_undated": dropped["undated"],
        "dropped_duplicates": dropped["duplicates"],
    }


def _read_news_bounded(
    news_agent: Any,
    watchlist_order: List[str],
    timeout: float = _NEWS_READ_TIMEOUT_SECONDS,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[Dict[str, Any]], bool]:
    """Read the news pool + narrative without letting a busy agent block us.

    Returns ``(items, narrative, live)``. When the agent does not answer in
    time, the previous successful read is served (``live=False``); every item
    and the narrative carry their own timestamps, so staleness stays visible.
    The read keeps running in the background and feeds the next build.
    """
    def work(done: threading.Event) -> None:
        try:
            items = news_agent.get_dashboard_news_items(active_tickers=watchlist_order, limit=_DASHBOARD_NEWS_POOL)
            narrative = news_agent.get_market_narrative()
            with _news_read_lock:
                _news_read_state["ts"] = time.time()
                _news_read_state["items"] = items
                _news_read_state["narrative"] = narrative
        except Exception as exc:
            logger.debug(f"Dashboard news read failed: {exc}")
        finally:
            with _news_read_lock:
                if _news_read_state.get("inflight") is done:
                    _news_read_state["inflight"] = None
            done.set()

    started = time.time()
    with _news_read_lock:
        done = _news_read_state.get("inflight")
        if done is None:
            done = threading.Event()
            _news_read_state["inflight"] = done
            threading.Thread(target=work, args=(done,), name="dashboard-news-read", daemon=True).start()
    finished = done.wait(max(0.05, float(timeout)))
    with _news_read_lock:
        items = _news_read_state.get("items")
        narrative = _news_read_state.get("narrative")
        read_ts = float(_news_read_state.get("ts") or 0.0)
    live = bool(finished and read_ts >= started - 0.5)
    if not finished:
        logger.warning(
            "Dashboard news read exceeded %.1fs (news agent busy); serving the previous read (%s).",
            timeout,
            f"{int(time.time() - read_ts)}s old" if read_ts else "none available yet",
        )
    return (
        list(items) if isinstance(items, list) else None,
        dict(narrative) if isinstance(narrative, dict) else None,
        live,
    )


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        f = float(value)
        return default if (f != f or f == float("inf") or f == float("-inf")) else f
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def _parse_iso_utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _dashboard_watchlist_order(config) -> List[str]:
    watchlist = config.watchlist or {}
    order = list(watchlist.get("order", []) or [])
    if not order:
        order = list(watchlist.get("yahoo", []) or []) + list(watchlist.get("binance", []) or [])
    return list(dict.fromkeys([str(t or "").upper().strip() for t in order if str(t or "").strip()]))


def _build_dashboard_earnings_7d(tickers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for t in tickers or []:
        days = _safe_int(t.get("earnings_days", -1), -1)
        if days < 0 or days > 7:
            continue
        rows.append(
            {
                "ticker": str(t.get("ticker") or "").upper(),
                "earnings_days": days,
                "earnings_date": t.get("earnings_date") or "N/A",
                "earnings_when": str(t.get("earnings_when") or "").upper(),
                "earnings_when_desc": t.get("earnings_when_desc") or "",
                "price": _safe_float(t.get("price"), 0.0),
                "change_pct": _safe_float(t.get("change_pct"), 0.0),
                "has_open_signal": bool(t.get("has_open_signal")),
                "news_pulse_score": _safe_int(t.get("news_pulse_score", 0), 0),
                "is_hot": bool(t.get("is_hot")),
            }
        )
    rows.sort(key=lambda x: (x["earnings_days"], -int(x["has_open_signal"]), -x["news_pulse_score"]))
    return rows[:10]


def _build_dashboard_watchlist_priority(tickers: List[Dict[str, Any]], order: List[str]) -> List[Dict[str, Any]]:
    order_rank = {tk: idx for idx, tk in enumerate(order or [])}
    ranked: List[Tuple[float, Dict[str, Any]]] = []
    for t in tickers or []:
        ticker = str(t.get("ticker") or "").upper()
        if not ticker:
            continue

        score = 0.0
        earnings_days = _safe_int(t.get("earnings_days", -1), -1)
        news_pulse = _safe_int(t.get("news_pulse_score", 0), 0)
        change_abs = abs(_safe_float(t.get("change_pct"), 0.0))
        has_signal = bool(t.get("has_open_signal"))

        if has_signal:
            score += 55
        if 0 <= earnings_days <= 7:
            score += max(8, 28 - (earnings_days * 3))
        score += min(35, news_pulse * 0.35)
        if change_abs >= 2.0:
            score += min(18, change_abs * 2.0)
        if bool(t.get("is_hot")):
            score += 8
        if str(t.get("trend_alignment") or "").upper() == "MIXED":
            score += 5

        row = {
            "ticker": ticker,
            "price": _safe_float(t.get("price"), 0.0),
            "change_pct": _safe_float(t.get("change_pct"), 0.0),
            "trend_1d": str(t.get("trend_1d") or "N/A"),
            "trend_1h": str(t.get("col_1h") or t.get("trend_1h") or "N/A"),
            "col_1h": str(t.get("col_1h") or "N/A"),
            "earnings_days": earnings_days,
            "earnings_when": str(t.get("earnings_when") or "").upper(),
            "earnings_when_desc": t.get("earnings_when_desc") or "",
            "news_pulse_score": news_pulse,
            "has_open_signal": has_signal,
            "is_hot": bool(t.get("is_hot")),
            "priority_score": round(score, 2),
            "order_rank": order_rank.get(ticker, 9999),
        }
        ranked.append((score, row))

    ranked.sort(key=lambda pair: (pair[0], -pair[1]["change_pct"]), reverse=True)
    return [row for _, row in ranked[:12]]


def _build_dashboard_risk_flags(
    tickers: List[Dict[str, Any]],
    dashboard_news_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    severity_order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    out: Dict[str, Dict[str, Any]] = {}

    def put(flag: Dict[str, Any]) -> None:
        key = f"{flag.get('type')}|{flag.get('ticker')}|{flag.get('title')}"
        prev = out.get(key)
        if prev is None:
            out[key] = flag
            return
        if severity_order.get(flag.get("severity", "low"), 1) > severity_order.get(prev.get("severity", "low"), 1):
            out[key] = flag

    now_utc = datetime.now(timezone.utc)
    for item in dashboard_news_items or []:
        impact = str(item.get("impact") or "low").lower()
        if impact not in {"high", "critical"}:
            continue
        ticker = str(item.get("ticker") or "").upper()
        if not ticker or ticker == "MARKET":
            continue
        ts = _parse_iso_utc(item.get("published_at"))
        if ts is None:
            continue
        if (now_utc - ts).total_seconds() > 24 * 3600:
            continue
        put(
            {
                "type": "NEWS_CRITICAL",
                "ticker": ticker,
                "severity": "critical" if impact == "critical" else "high",
                "title": str(item.get("title") or "High-impact news"),
                "detail": f"{impact.upper()} impact news in last 24h",
            }
        )

    for t in tickers or []:
        ticker = str(t.get("ticker") or "").upper()
        if not ticker:
            continue
        days = _safe_int(t.get("earnings_days", -1), -1)
        if 0 <= days <= 2:
            put(
                {
                    "type": "EARNINGS_SOON",
                    "ticker": ticker,
                    "severity": "high" if days <= 1 else "medium",
                    "title": f"Earnings in {days}d",
                    "detail": str(t.get("earnings_when_desc") or t.get("earnings_when") or "Upcoming earnings"),
                }
            )

        if str(t.get("trend_alignment") or "").upper() == "MIXED":
            put(
                {
                    "type": "RISK_FLAG",
                    "ticker": ticker,
                    "severity": "medium",
                    "title": "Trend conflict",
                    "detail": f"1D: {t.get('trend_1d', 'N/A')} - 1H: {t.get('col_1h', 'N/A')}",
                }
            )

        risk_flags = [str(x) for x in (t.get("risk_flags") or [])]
        if "high_news_risk" in risk_flags:
            put(
                {
                    "type": "NEWS_CRITICAL",
                    "ticker": ticker,
                    "severity": "high",
                    "title": "Bearish news pulse",
                    "detail": f"News pulse score {int(t.get('news_pulse_score') or 0)}",
                }
            )

    rows = list(out.values())
    rows.sort(
        key=lambda x: (
            severity_order.get(str(x.get("severity") or "low").lower(), 1),
            str(x.get("ticker") or ""),
        ),
        reverse=True,
    )
    return rows[:10]


def _normalize_regime(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in {"risk-on", "risk_on", "riskon", "bullish", "bull"}:
        return "risk-on"
    if value in {"risk-off", "risk_off", "riskoff", "bearish", "bear"}:
        return "risk-off"
    if value in {"mixed", "neutral", "range"}:
        return "mixed"
    if value:
        return value
    return "mixed"


def _normalize_sentiment(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in {"bullish", "risk-on", "risk_on", "positive"}:
        return "bullish"
    if value in {"bearish", "risk-off", "risk_off", "negative"}:
        return "bearish"
    return "neutral"


def _compute_dashboard_regime(
    market_narrative: Dict[str, Any],
    tickers: List[Dict[str, Any]],
    signals_raw: Dict[str, Any],
) -> Dict[str, Any]:
    narrative = market_narrative if isinstance(market_narrative, dict) else {}
    regime = _normalize_regime(narrative.get("risk_regime") or narrative.get("tone"))
    sentiment = _normalize_sentiment(narrative.get("tone") or narrative.get("sentiment"))

    open_signals = (signals_raw.get("open") or {}) if isinstance(signals_raw, dict) else {}
    open_list = list(open_signals.values()) if isinstance(open_signals, dict) else []
    regime_votes: Dict[str, int] = {"risk-on": 0, "risk-off": 0, "mixed": 0}
    for sig in open_list:
        sig_regime = str((sig or {}).get("market_regime") or "").upper()
        if sig_regime == "TREND":
            regime_votes["risk-on"] += 1
        elif sig_regime == "VOLATILE":
            regime_votes["risk-off"] += 1
        else:
            regime_votes["mixed"] += 1

    if sum(regime_votes.values()) > 0:
        dominant = max(regime_votes, key=lambda k: regime_votes[k])
        if regime == "mixed":
            regime = dominant

    trend_votes = {"BULL": 0, "BEAR": 0, "MIXED": 0}
    for t in tickers or []:
        align = str((t or {}).get("trend_alignment") or "MIXED").upper()
        if "BULL" in align:
            trend_votes["BULL"] += 1
        elif "BEAR" in align:
            trend_votes["BEAR"] += 1
        else:
            trend_votes["MIXED"] += 1
    if sentiment == "neutral":
        if trend_votes["BULL"] > trend_votes["BEAR"] * 1.2:
            sentiment = "bullish"
        elif trend_votes["BEAR"] > trend_votes["BULL"] * 1.2:
            sentiment = "bearish"

    narrative_conf = _safe_float(narrative.get("confidence"), -1.0)
    if narrative_conf > 1:
        narrative_conf = max(0.0, min(100.0, narrative_conf))
    elif narrative_conf >= 0:
        narrative_conf = max(0.0, min(100.0, narrative_conf * 100.0))
    else:
        dominance_total = max(1, sum(regime_votes.values()))
        dominance = max(regime_votes.values()) / dominance_total
        narrative_conf = 40.0 + (dominance * 45.0)
        if len(open_list) >= 3:
            narrative_conf += 8.0

    return {
        "regime": regime,
        "sentiment": sentiment,
        "confidence": round(max(0.0, min(100.0, narrative_conf)), 1),
    }


def _compute_signal_pnl_abs(sig: Dict[str, Any]) -> float:
    pnl_abs = _safe_float(sig.get("pnl_abs"), float("nan"))
    if pnl_abs == pnl_abs:
        return pnl_abs
    entry = _safe_float(sig.get("entry"), 0.0)
    exit_price = _safe_float(sig.get("exit_price"), 0.0)
    if entry <= 0 or exit_price <= 0:
        return 0.0
    action = str(sig.get("action") or sig.get("type") or "BUY").upper()
    if action in {"SHORT", "SELL"}:
        return entry - exit_price
    return exit_price - entry


def _compute_signal_return_pct(sig: Dict[str, Any]) -> Optional[float]:
    """Direction-adjusted percent return of one closed signal.

    This is the unit profit factor must be summed in. Raw per-unit price
    differences are not comparable across instruments: a 21 USD move on one
    coin and a 0.0002 USD move on another produced "profit factor 1551".
    """
    recorded = _safe_float(sig.get("pnl_pct"), float("nan"))
    if recorded == recorded:
        return recorded
    entry = _safe_float(sig.get("entry"), 0.0)
    exit_price = _safe_float(sig.get("exit_price"), 0.0)
    if entry <= 0 or exit_price <= 0:
        return None
    action = str(sig.get("action") or sig.get("type") or "BUY").upper()
    move = (entry - exit_price) if action in {"SHORT", "SELL"} else (exit_price - entry)
    return move / entry * 100.0


def _compute_r_multiple(sig: Dict[str, Any]) -> Optional[float]:
    entry = _safe_float(sig.get("entry"), 0.0)
    sl = _safe_float(sig.get("sl"), 0.0)
    exit_price = _safe_float(sig.get("exit_price"), 0.0)
    if entry <= 0 or sl <= 0 or exit_price <= 0:
        return None
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    action = str(sig.get("action") or sig.get("type") or "BUY").upper()
    pnl = (entry - exit_price) if action in {"SHORT", "SELL"} else (exit_price - entry)
    return pnl / risk


def _closed_signals_in_days(closed: List[Dict[str, Any]], days: int) -> List[Dict[str, Any]]:
    now_utc = datetime.now(timezone.utc)
    out: List[Dict[str, Any]] = []
    for sig in closed or []:
        dt = _parse_iso_utc(sig.get("closed_at") or sig.get("closedat") or sig.get("created_at"))
        if dt is None:
            continue
        if (now_utc - dt).total_seconds() <= (days * 24 * 3600):
            out.append(sig)
    return out


def _compute_win_rate(signals: List[Dict[str, Any]]) -> float:
    if not signals:
        return 0.0
    wins = 0
    for sig in signals:
        status = str(sig.get("status") or "").upper()
        pnl = _compute_signal_pnl_abs(sig)
        if status == "WIN" or pnl > 0:
            wins += 1
    return round((wins / max(1, len(signals))) * 100.0, 1)


def _compute_dashboard_strategy_health(signals_raw: Dict[str, Any]) -> Dict[str, Any]:
    open_map = (signals_raw.get("open") or {}) if isinstance(signals_raw, dict) else {}
    closed_rows = (signals_raw.get("closed") or []) if isinstance(signals_raw, dict) else []
    if not isinstance(open_map, dict):
        open_map = {}
    if not isinstance(closed_rows, list):
        closed_rows = []

    # Dashboard health must use the same evidence-qualified population as the
    # execution gates.  Legacy rows remain available for audit elsewhere, but
    # cannot manufacture a win rate or profit factor here.
    closed_rows = [
        row for row in closed_rows
        if isinstance(row, dict) and row.get("outcome_data_valid") is True
    ]

    closed_7d = _closed_signals_in_days(closed_rows, 7)
    closed_30d = _closed_signals_in_days(closed_rows, 30)
    win_rate_7d = _compute_win_rate(closed_7d)
    win_rate_30d = _compute_win_rate(closed_30d)

    profit_sum = 0.0
    loss_sum = 0.0
    r_values: List[float] = []
    for sig in closed_30d:
        # Percent returns, not raw price differences (see _compute_signal_return_pct).
        pnl = _compute_signal_return_pct(sig)
        if pnl is None:
            pnl = 0.0
        if pnl >= 0:
            profit_sum += pnl
        else:
            loss_sum += abs(pnl)
        r_mult = _compute_r_multiple(sig)
        if r_mult is not None:
            r_values.append(r_mult)

    profit_factor_30d = round(profit_sum / loss_sum, 2) if loss_sum > 0 else None

    avg_r_30d = round((sum(r_values) / len(r_values)) if r_values else 0.0, 2)

    now_utc = datetime.now(timezone.utc)
    signals_today = 0
    for sig in list(open_map.values()) + closed_rows:
        created_dt = _parse_iso_utc(sig.get("created_at"))
        if created_dt is None:
            continue
        if created_dt.date() == now_utc.date():
            signals_today += 1

    active_risk_exposure_pct = 0.0
    for sig in open_map.values():
        pos = _safe_float(sig.get("position_size_pct"), _safe_float(sig.get("risk_pct_used"), 0.0))
        active_risk_exposure_pct += max(0.0, pos)
    active_risk_exposure_pct = round(max(0.0, min(100.0, active_risk_exposure_pct)), 1)

    return {
        "win_rate_7d": win_rate_7d,
        "win_rate_30d": win_rate_30d,
        "profit_factor_30d": profit_factor_30d,
        "profit_factor_basis": "sum of winning % returns / sum of losing % returns, closed signals, 30 days",
        "closed_signals_30d": len(closed_30d),
        "closed_signals_7d": len(closed_7d),
        "avg_R_30d": avg_r_30d,
        "signals_today": int(signals_today),
        "active_risk_exposure_pct": active_risk_exposure_pct,
    }


def _compute_dashboard_confidence_index(
    regime: Dict[str, Any],
    strategy_health: Dict[str, Any],
    risk_flags: List[Dict[str, Any]],
) -> int:
    regime_conf = _safe_float(regime.get("confidence"), 50.0)
    win_rate = _safe_float(strategy_health.get("win_rate_30d"), 0.0)
    profit_factor = _safe_float(strategy_health.get("profit_factor_30d"), 0.0)
    active_risk = _safe_float(strategy_health.get("active_risk_exposure_pct"), 0.0)

    pf_component = max(0.0, min(100.0, profit_factor * 40.0))
    risk_component = max(0.0, 100.0 - active_risk)

    score = (0.35 * regime_conf) + (0.30 * win_rate) + (0.20 * pf_component) + (0.15 * risk_component)
    severe_flags = sum(
        1
        for f in (risk_flags or [])
        if str((f or {}).get("severity") or "").lower() in {"critical", "high"}
    )
    score -= min(20.0, severe_flags * 4.0)
    return int(round(max(0.0, min(100.0, score))))


def _dashboard_cache_sig(
    watchlist_order: List[str],
    tickers: List[Dict[str, Any]],
    signals_raw: Dict[str, Any],
) -> str:
    open_map = (signals_raw.get("open") or {}) if isinstance(signals_raw, dict) else {}
    closed_list = (signals_raw.get("closed") or []) if isinstance(signals_raw, dict) else []
    open_count = len(_valid_open_signal_rows(open_map))
    summary = signals_raw.get("_summary") if isinstance(signals_raw, dict) else {}
    closed_count = int(summary.get("closed_count") or 0) if isinstance(summary, dict) else 0
    if not closed_count:
        closed_count = len(closed_list) if isinstance(closed_list, list) else 0

    latest_closed = str(summary.get("latest_closed_at") or "") if isinstance(summary, dict) else ""
    if not latest_closed and isinstance(closed_list, list) and closed_list:
        c0 = closed_list[0] if isinstance(closed_list[0], dict) else {}
        latest_closed = str(c0.get("closed_at") or c0.get("created_at") or "")
    return "|".join(watchlist_order[:120]) + f"|{len(tickers)}|{open_count}|{closed_count}|{latest_closed}"


def get_dashboard_payload() -> Dict[str, Any]:
    now_ts = time.time()
    with _dashboard_cache_lock:
        cached = _dashboard_cache.get("data")
        if (
            isinstance(cached, dict)
            and cached
            and (now_ts - float(_dashboard_cache.get("ts", 0.0))) < _DASHBOARD_CACHE_TTL_SECONDS
        ):
            return dict(cached)

    config = get_shared_config()
    state_data = load_state_payload({})
    signals_data = load_signals_payload({"open": {}, "closed": []}, include_closed=False)
    if not isinstance(state_data, dict):
        state_data = {}
    if not isinstance(signals_data, dict):
        signals_data = {"open": {}, "closed": []}

    allow_remote = True
    ctx = get_request_context()
    if ctx is not None and bool(getattr(ctx, "cache_only", False)):
        # Dashboard is polled by the UI; prefer deterministic cached data over remote refresh stalls.
        allow_remote = False

    tickers_list = build_tickers_list(state_data, signals_data, config, allow_remote=allow_remote)
    workspace = get_ticker_workspace_store()
    for row in tickers_list:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").upper().strip()
        if not ticker:
            continue
        ws_md = workspace.get_market_data(ticker)
        if not isinstance(ws_md, dict):
            ws_md = {}
        if _safe_float(row.get("price"), 0.0) <= 0 and _safe_float(ws_md.get("price"), 0.0) > 0:
            row["price"] = _safe_float(ws_md.get("price"), 0.0)
        if _safe_float(row.get("change_pct"), 0.0) == 0.0 and _safe_float(ws_md.get("change_pct"), 0.0) != 0.0:
            row["change_pct"] = _safe_float(ws_md.get("change_pct"), 0.0)
    watchlist_order = _dashboard_watchlist_order(config)
    cache_sig = _dashboard_cache_sig(watchlist_order, tickers_list, signals_data)
    with _dashboard_cache_lock:
        if (
            _dashboard_cache.get("sig") == cache_sig
            and (now_ts - float(_dashboard_cache.get("ts", 0.0))) < _DASHBOARD_CACHE_TTL_SECONDS
            and isinstance(_dashboard_cache.get("data"), dict)
            and _dashboard_cache.get("data")
        ):
            return dict(_dashboard_cache.get("data"))

    dashboard_news_items: List[Dict[str, Any]] = []
    dashboard_news_meta: Dict[str, Any] = {
        "window_hours": int(_DASHBOARD_NEWS_MAX_AGE_SECONDS // 3600),
        "dropped_stale": 0, "dropped_undated": 0, "dropped_duplicates": 0,
        "live": False,
    }
    # Default narrative is explicitly UNAVAILABLE (no fabricated "unknown"
    # strings). The frontend renders a graceful empty state from
    # ``available: false`` instead of literal "unknown" bullets that look like
    # real analysis output to a buyer.
    dashboard_market_narrative: Dict[str, Any] = {
        "available": False,
        "tone": "mixed",
        "top_catalysts": [],
        "what_matters_today": [],
        "watchlist_implications": [],
        "risk_regime": "mixed",
        "bullets": [],
        "note": "Market narrative unavailable until the news agent runs.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        news_agent = get_news_tab_agent_if_ready()
        if news_agent is not None:
            news_pool, narrative, news_live = _read_news_bounded(news_agent, watchlist_order)
            dashboard_news_items, dashboard_news_meta = _select_current_news(news_pool)
            dashboard_news_meta["live"] = bool(news_live)
            if narrative and isinstance(narrative, dict):
                # Mark as available only if there is real content (not the empty
                # default shape). Detect content via any non-empty catalyst/bullet.
                has_content = any(
                    narrative.get(k)
                    for k in ("top_catalysts", "what_matters_today", "bullets", "watchlist_implications")
                    if isinstance(narrative.get(k), list)
                )
                narrative.setdefault("available", bool(has_content))
                dashboard_market_narrative = narrative
    except Exception as e:
        logger.debug(f"Dashboard news payload unavailable: {e}")

    dashboard_earnings_7d = _build_dashboard_earnings_7d(tickers_list)
    dashboard_watchlist_priority = _build_dashboard_watchlist_priority(tickers_list, watchlist_order)
    dashboard_risk_flags = _build_dashboard_risk_flags(tickers_list, dashboard_news_items)
    dashboard_regime = _compute_dashboard_regime(dashboard_market_narrative, tickers_list, signals_data)
    dashboard_strategy_health = _compute_dashboard_strategy_health(signals_data)
    dashboard_confidence_index = _compute_dashboard_confidence_index(
        dashboard_regime,
        dashboard_strategy_health,
        dashboard_risk_flags,
    )
    open_map = (signals_data.get("open") or {}) if isinstance(signals_data, dict) else {}
    signals_count = int(len(_valid_open_signal_rows(open_map)))
    tickers_count = int(len(tickers_list))
    generated_at = datetime.now(timezone.utc).isoformat()
    stats = {
        "confidence_index": int(dashboard_confidence_index),
        "win_rate_30d": _safe_float(dashboard_strategy_health.get("win_rate_30d"), 0.0),
        "profit_factor_30d": dashboard_strategy_health.get("profit_factor_30d"),
        "regime": str(dashboard_regime.get("regime") or "mixed"),
        "sentiment": str(dashboard_regime.get("sentiment") or "neutral"),
    }

    payload = {
        "generated_at": generated_at,
        "stats": stats,
        "signals_count": signals_count,
        "tickers_count": tickers_count,
        "dashboard_version": 2,
        "dashboard_news_items": dashboard_news_items,
        "dashboard_news_meta": dashboard_news_meta,
        "dashboard_market_narrative": dashboard_market_narrative,
        "dashboard_earnings_7d": dashboard_earnings_7d,
        "dashboard_watchlist_priority": dashboard_watchlist_priority,
        "dashboard_risk_flags": dashboard_risk_flags,
        "dashboard_regime": dashboard_regime,
        "dashboard_strategy_health": dashboard_strategy_health,
        "dashboard_confidence_index": dashboard_confidence_index,
        "dashboard_generated_at": generated_at,
    }

    with _dashboard_cache_lock:
        _dashboard_cache["ts"] = now_ts
        _dashboard_cache["sig"] = cache_sig
        _dashboard_cache["data"] = dict(payload)
    return payload
