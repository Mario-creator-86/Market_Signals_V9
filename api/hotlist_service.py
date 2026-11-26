"""HotList ticker intelligence dossier (2026-09-03 rebuild, owner-directed).

The HotList tab stops being a bare curated list: every ticker that lands in
the heat (curated hot list, scanner gainers/losers/volume) can be opened as a
dossier — everything the system already knows about it in one bounded view:

- quote + trend context (cache-only, no remote refresh on the read path)
- why it is hot (scanner categories it appears in)
- news bucket with source/age metadata
- our own trade record: open + closed paper positions, per-strategy stats,
  realized P&L totals
- AI decision history from the Decision Ledger (verdicts, models, debates)
- pre-earnings context (event date, gap risk) when available
- on-demand DEEP ANALYSIS: the TradingAgents-style bull/bear debate judged by
  a research manager (cached per ticker with TTL, singleflight per ticker)

Every section degrades honestly ("no data") instead of blocking the others.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import DATA_DIR, STATE_PATH, _write_json_atomic, is_crypto_ticker

logger = logging.getLogger("HotListService")

BASE_DIR = Path(__file__).resolve().parents[1]
DOSSIER_CACHE_TTL_SECONDS = 60.0
DEEP_ANALYSIS_TTL_SECONDS = 1800.0
DEEP_ANALYSIS_HISTORY = 3
MAX_NEWS_ITEMS = 6
MAX_LEDGER_ITEMS = 8
MAX_TRADES = 12

_dossier_cache: Dict[str, Any] = {}
_dossier_lock = threading.Lock()
_deep_lock = threading.Lock()
_deep_singleflights: Dict[str, threading.Lock] = defaultdict(threading.Lock)
DEEP_STORE_PATH = DATA_DIR / "hotlist_deep_analysis.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_ticker(ticker: str) -> str:
    return str(ticker or "").upper().strip()


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# ----------------------------------------------------------------- heat list

def _scanner_hot_data() -> Dict[str, Any]:
    try:
        from api.main import _build_hotlist_response

        categories: Dict[str, List[Dict[str, Any]]] = {}
        for cat in ("gainers", "losers", "most_active", "trending", "volume_leaders"):
            resp = _build_hotlist_response(category=cat, limit=12)
            rows = resp.get("data") if isinstance(resp, dict) else None
            # 2026-09-03 live test: data is a DICT keyed by category
            # ({"gainers": [...]}), not a bare list — the heat merge read it
            # as a list and silently produced an empty heat feed.
            if isinstance(rows, dict):
                rows = rows.get(cat) or []
            if isinstance(rows, list) and rows:
                categories[cat] = [r for r in rows if isinstance(r, dict)]
        return {"available": bool(categories), "categories": categories}
    except Exception as exc:
        logger.warning("scanner heat unavailable: %s", exc)
        return {"available": False, "categories": {}}


def build_heat_list() -> Dict[str, Any]:
    """Curated hot list + scanner heat, merged into one master feed.

    Stale-while-revalidate (2026-09-03 owner report: rebuilds cost 1.3-24s
    under load and the browser timed out at 30s): a cached payload — even a
    stale one — is served immediately while a background thread refreshes it.
    Only the first-ever call blocks on the build."""
    now = time.monotonic()
    cached = _heat_cache.get("payload")
    if cached is not None:
        age = now - float(_heat_cache.get("at") or 0.0)
        if age < _HEAT_CACHE_TTL_SECONDS:
            return cached
        if not _heat_cache.get("refreshing"):
            _heat_cache["refreshing"] = True

            def _refresh_heat():
                try:
                    fresh = _build_heat_list_uncached()
                    _heat_cache["at"] = time.monotonic()
                    _heat_cache["payload"] = fresh
                except Exception:
                    pass
                finally:
                    _heat_cache["refreshing"] = False

            threading.Thread(target=_refresh_heat, name="heat-swr", daemon=True).start()
        return cached
    # Cold start: ONE build, shared by every concurrent caller. Without this
    # each poll of the Hot List tab started its own 14-28 s uncached build in
    # parallel; they slowed each other past the browser timeout and the strip
    # stayed on "Loading market heat..." (tab audit 2026-11-15).
    with _heat_build_lock:
        cached = _heat_cache.get("payload")
        if cached is not None:
            return cached
        return _build_heat_list_uncached()


def _build_heat_list_uncached() -> Dict[str, Any]:

    curated: List[Dict[str, Any]] = []
    try:
        from core.hotlist_scoring import get_hotlist_state, sort_hotlist_by_priority

        state = get_hotlist_state()
        curated = sort_hotlist_by_priority(state.get("hotlist") or [])
    except Exception as exc:
        logger.warning("curated hotlist unavailable: %s", exc)

    scanner = _scanner_hot_data()
    heat: Dict[str, Dict[str, Any]] = {}

    def _touch(ticker: str, category: str, row: Dict[str, Any]) -> None:
        t = _norm_ticker(ticker)
        if not t:
            return
        item = heat.setdefault(t, {
            "ticker": t, "categories": [], "price": None, "change_pct": None,
            "volume": None, "name": row.get("name") or "",
        })
        if category not in item["categories"]:
            item["categories"].append(category)
        for src_key, dst_key in (("price", "price"), ("change_pct", "change_pct"),
                                 ("volume", "volume"), ("mktcap", "name")):
            if item.get(dst_key) in (None, "") and row.get(src_key) is not None:
                item[dst_key] = row.get(src_key)

    for category, rows in (scanner.get("categories") or {}).items():
        for row in rows[:12]:
            if isinstance(row, dict):
                _touch(row.get("ticker") or row.get("symbol"), category, row)

    payload = {
        "curated": curated,
        "heat": sorted(heat.values(), key=lambda r: -abs(float(r.get("change_pct") or 0.0))),
        "scanner_available": scanner.get("available", False),
        "as_of": _utc_now_iso(),
    }
    _heat_cache["at"] = time.monotonic()
    _heat_cache["payload"] = payload

    # 2026-09-03 owner feedback: the dossier read used to block on the bars
    # fetch (45s timeouts when the provider throttled). Warm the bars in the
    # background so subsequent dossier opens are cache-only and instant.
    try:
        from api.state_service import _warm_symbol_bars

        # Curated tickers first: the warmer is capped at 12 symbols, and with
        # ten heat rows in front only the first TWO curated tickers were ever
        # warmed - the rest kept "ADX / Momentum N/A" (tab audit 2026-11-15).
        # (get_hotlist_state above already warms the whole curated list.)
        warm_tickers = [str(r.get("ticker") or "").upper() for r in curated[:6]]
        warm_tickers += [r["ticker"] for r in payload["heat"][:10]]
        _warm_symbol_bars(list(dict.fromkeys(t for t in warm_tickers if t)))
    except Exception:
        pass

    return payload


_heat_cache: Dict[str, Any] = {"at": 0.0, "payload": None}
_heat_build_lock = threading.Lock()
_HEAT_CACHE_TTL_SECONDS = 15.0


# ------------------------------------------------------------------- dossier

def _quote_section(ticker: str) -> Dict[str, Any]:
    quote: Dict[str, Any] = {}
    try:
        from api.state_service import get_state

        state = get_state(
            include_news=False, include_hotlist=False, lean=True, live_prices=False,
        ) or {}
        for row in state.get("tickers", []) or []:
            if isinstance(row, dict) and _norm_ticker(row.get("ticker")) == ticker:
                keys = ("price", "change_pct", "rsi", "macd_hist", "trend_1d", "trend_4h",
                        "trend_1h", "trend_30m", "trend_5m", "sector", "asset_type",
                        "average_daily_volume_usd", "signal_score", "earnings_date",
                        "earnings_when", "earnings_days", "market_regime", "is_hot")
                quote.update({k: row.get(k) for k in keys if row.get(k) is not None})
                break
    except Exception as exc:
        logger.debug("quote watchlist read failed for %s: %s", ticker, exc)

    # Engine snapshot row. (This used to read the legacy <repo>/state.json,
    # a file last written months ago; the engine writes STATE_PATH.)
    engine_row: Dict[str, Any] = {}
    try:
        state_data = _read_json(STATE_PATH, {})
        candidate = (state_data.get("tickers") or {}).get(ticker) if isinstance(state_data, dict) else None
        if isinstance(candidate, dict):
            engine_row = candidate
    except Exception as exc:
        logger.debug("engine row read failed for %s: %s", ticker, exc)
    if "price" not in quote and engine_row.get("price"):
        for k in ("price", "change_pct", "rsi", "macd_hist", "trend_1d", "trend_4h",
                  "trend_1h", "trend_30m", "trend_5m", "sector", "asset_type"):
            if engine_row.get(k) is not None:
                quote.setdefault(k, engine_row.get(k))

    cache = _read_json(DATA_DIR / "live_prices_cache.json", {})
    live_row = ((cache.get("prices") or {}).get(ticker) or {}) if isinstance(cache, dict) else {}
    if not isinstance(live_row, dict):
        live_row = {}

    # Tab audit 2026-11-15: the dossier printed the engine snapshot price
    # (MARA $11.81, 68 days old) next to a scanner row saying $6.83, and the
    # deep-analysis debate was framed LONG/SHORT from that old change. Use the
    # freshest known quote and always say how old it is.
    try:
        from core.hotlist_scoring import is_hotlist_technicals_stale, resolve_hotlist_quote

        always_open = bool(is_crypto_ticker(ticker))
        resolved = resolve_hotlist_quote(
            engine_row,
            live_row,
            {"price": quote.get("price"), "change_pct": quote.get("change_pct")} if quote.get("price") else None,
            always_open=always_open,
        )
        if resolved.get("price") is not None:
            quote["price"] = resolved["price"]
            quote["change_pct"] = resolved["change_pct"]
            for k in ("price_as_of", "price_age_seconds", "price_source", "price_origin", "price_stale"):
                quote[k] = resolved.get(k)
        if any(quote.get(k) for k in ("trend_1d", "trend_4h")):
            quote["technicals_as_of"] = engine_row.get("last_update")
            quote["technicals_stale"] = bool(
                is_hotlist_technicals_stale(engine_row.get("last_update"), always_open=always_open)
            )
    except Exception as exc:
        logger.debug("quote freshness resolve failed for %s: %s", ticker, exc)

    if quote:
        quote = {"available": True, **quote}
    else:
        quote = {"available": False}
    return quote


def _news_section(ticker: str) -> Dict[str, Any]:
    try:
        from agents.news_tab_agent import get_news_tab_agent_if_ready

        agent = get_news_tab_agent_if_ready()
        if agent is None:
            return {"available": False, "reason": "news_agent_initializing"}
        bucket = agent.get_ticker_news(ticker) if hasattr(agent, "get_ticker_news") else None
        items = bucket if isinstance(bucket, list) else (bucket or {}).get("articles", [])
        articles = []
        for a in (items or [])[:MAX_NEWS_ITEMS]:
            if not isinstance(a, dict):
                continue
            articles.append({
                "title": str(a.get("title") or "")[:180],
                "source": str(a.get("source") or ""),
                "published_at": str(a.get("published_at") or ""),
                "sentiment": a.get("sentiment"),
                "url": str(a.get("url") or ""),
            })
        return {"available": bool(articles), "count": len(items or []), "articles": articles}
    except Exception as exc:
        logger.debug("news section failed for %s: %s", ticker, exc)
        return {"available": False, "reason": "news_error"}


def _trades_section(ticker: str) -> Dict[str, Any]:
    try:
        from core.sqlite_store import DB_PATH as _CAPITAL_DB_PATH

        con = sqlite3.connect(str(_CAPITAL_DB_PATH), timeout=2.0)
        try:
            row = con.execute(
                "SELECT value FROM kv_store WHERE key='capital_state'"
            ).fetchone()
        finally:
            con.close()
        if not row:
            return {"available": False, "reason": "no_capital_store"}
        data = json.loads(row[0])
        positions = data.get("positions", []) if isinstance(data, dict) else []
        open_pos: List[Dict[str, Any]] = []
        closed: List[Dict[str, Any]] = []
        for pos in positions or []:
            if not isinstance(pos, dict) or _norm_ticker(pos.get("ticker")) != ticker:
                continue
            entry = float(pos.get("entry_price") or 0.0)
            exit_price = float(pos.get("exit_price") or 0.0)
            direction = str(pos.get("direction") or "LONG").upper()
            if entry > 0 and exit_price > 0:
                raw = (exit_price - entry) / entry * 100.0
                pnl_pct = round(raw if direction in ("LONG", "BUY") else -raw, 2)
            else:
                pnl_pct = None
            slim = {
                "position_id": pos.get("position_id"),
                "strategy_type": pos.get("strategy_type"),
                "direction": direction,
                "status": str(pos.get("status") or "").upper(),
                "entry_price": entry or None,
                "exit_price": exit_price or None,
                "pnl_pct": pnl_pct,
                "net_pnl": pos.get("net_pnl") if pos.get("net_pnl") is not None else pos.get("pnl"),
                "close_reason": pos.get("close_reason"),
                "opened_at": pos.get("opened_at"),
                "closed_at": pos.get("closed_at"),
            }
            if slim["status"] == "OPEN":
                open_pos.append(slim)
            elif slim["status"] == "CLOSED":
                closed.append(slim)

        by_strategy: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"n": 0, "wins": 0, "sum_pnl": 0.0})
        for pos in closed:
            pnl = pos.get("pnl_pct")
            if pnl is None:
                continue
            st = str(pos.get("strategy_type") or "unknown")
            bucket = by_strategy[st]
            bucket["n"] += 1
            bucket["sum_pnl"] += float(pnl)
            if float(pnl) > 0:
                bucket["wins"] += 1
        strategies = [
            {"strategy": st, "n": b["n"],
             "win_rate_pct": round(b["wins"] / b["n"] * 100.0, 1),
             "avg_pnl_pct": round(b["sum_pnl"] / b["n"], 2),
             "total_pnl_pct": round(b["sum_pnl"], 2)}
            for st, b in sorted(by_strategy.items(), key=lambda kv: -kv[1]["n"])
        ]
        pnls = [p["pnl_pct"] for p in closed if p["pnl_pct"] is not None]
        closed.sort(key=lambda p: str(p.get("closed_at") or ""), reverse=True)
        return {
            "available": True,
            "open_positions": open_pos[:5],
            "closed_trades": closed[:MAX_TRADES],
            "closed_count": len(closed),
            "traded": bool(open_pos or closed),
            "totals": {
                "trades": len(pnls),
                "wins": sum(1 for p in pnls if p > 0),
                "win_rate_pct": round(sum(1 for p in pnls if p > 0) / len(pnls) * 100.0, 1) if pnls else None,
                "avg_pnl_pct": round(sum(pnls) / len(pnls), 2) if pnls else None,
                "total_pnl_pct": round(sum(pnls), 2) if pnls else None,
                "best_pnl_pct": max(pnls) if pnls else None,
                "worst_pnl_pct": min(pnls) if pnls else None,
            },
            "strategies": strategies[:6],
        }
    except Exception as exc:
        logger.debug("trades section failed for %s: %s", ticker, exc)
        return {"available": False, "reason": "trades_error"}


def _decisions_section(ticker: str) -> Dict[str, Any]:
    try:
        from core.decision_ledger.store import get_decision_ledger_store

        store = get_decision_ledger_store(None)
        entries = store.list_entries(limit=200, ticker=ticker)[:MAX_LEDGER_ITEMS]
        rows = []
        for entry in entries:
            q = entry.eleven_questions()
            proposal = q.get("ai_proposal") or {}
            risk = entry.risk or {}
            # Prefer real reasons; skip the "no_decision_recorded" placeholder
            # that decision_projection injects for legacy rows.
            candidates_reason = [
                proposal.get("reason"), proposal.get("thesis"),
                (entry.ai or {}).get("reason"), risk.get("reason"),
            ]
            reason = next(
                (str(r).strip() for r in candidates_reason
                 if r and str(r).strip() and str(r).strip() != "no_decision_recorded"),
                q.get("what_happened") or "",
            )
            rows.append({
                "decision_id": q.get("decision_id"),
                "timestamp": q.get("timestamp"),
                "status": q.get("status"),
                "action": q.get("action") or proposal.get("action"),
                "strategy": entry.strategy,
                "model": (q.get("ai_model") or {}).get("model"),
                "confidence": proposal.get("confidence"),
                # 2026-09-03 owner feedback: "avem REJECTED, dar de ce?" — the
                # reason must travel with the row, not live only in the ledger.
                "reason": str(reason)[:400],
                "debate_verdict": ((entry.debate or {}).get("verdict") or {}).get("recommendation")
                if isinstance(entry.debate, dict) else None,
                "afterward": (entry.afterward or {}).get("outcome") if isinstance(entry.afterward, dict) else None,
            })
        return {"available": bool(rows), "count": len(rows), "decisions": rows}
    except Exception as exc:
        logger.debug("decisions section failed for %s: %s", ticker, exc)
        return {"available": False, "reason": "ledger_error"}


_LAST_KNOWN_EARNINGS: Dict[str, Dict[str, Any]] = {}
_LAST_KNOWN_LOCK = threading.Lock()


_LAST_KNOWN_PATH = DATA_DIR / "last_known_earnings.json"

# seed the in-memory map from disk at import so restarts keep the history
try:
    _LAST_KNOWN_EARNINGS.update(json.loads(_LAST_KNOWN_PATH.read_text(encoding="utf-8")))
except Exception:
    pass


def _remember_earnings(ticker: str, info: Dict[str, Any]) -> None:
    """Keep the last valid earnings date per ticker: the engine's watchlist
    rows transiently lose earnings_date during calendar refreshes (observed
    'N/A' between cycles) AND across restarts, so the memory is persisted to
    data/last_known_earnings.json."""
    date = str(info.get("earnings_date") or "").strip()
    if not date or date.upper() == "N/A":
        return
    # days_until is a function of "today": persisting it froze the countdown
    # at the day it was written ("in 59 days" two months later). Store the
    # date only and derive the countdown on every read.
    record = {k: v for k, v in dict(info).items() if k not in ("days_until", "is_past")}
    with _LAST_KNOWN_LOCK:
        if _LAST_KNOWN_EARNINGS.get(ticker) == record:
            return
        _LAST_KNOWN_EARNINGS[ticker] = record
        snapshot = dict(_LAST_KNOWN_EARNINGS)
        try:
            # Atomic: a plain write_text could be read half-written by a
            # concurrent dossier build (and truncated by a crash mid-write).
            _write_json_atomic(_LAST_KNOWN_PATH, snapshot, indent=1)
        except Exception as exc:
            logger.warning("last-known earnings store write failed: %s", exc)


def _earnings_countdown(date_text: Any, today: Optional[datetime] = None) -> Optional[int]:
    """Days from today (local date, like the Watchlist) to an ISO earnings date."""
    try:
        target = datetime.fromisoformat(str(date_text)[:10]).date()
    except (TypeError, ValueError):
        return None
    base = (today or datetime.now()).date()
    return (target - base).days


def _pre_earnings_section(ticker: str, quote: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    quote = quote or {}
    try:
        data = _read_json(DATA_DIR / "pre_earnings.json", {})
        items = data.get("items", []) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and _norm_ticker(item.get("symbol")) == ticker:
                return {
                    "available": True,
                    "source": "pre_earnings_universe",
                    "earnings_time_utc": item.get("earnings_time_utc"),
                    "session": item.get("session"),
                    "gap_risk": item.get("gap_risk"),
                    "gap_risk_level": item.get("gap_risk_level"),
                    "direction_label": item.get("direction_label"),
                    "coverage_score": item.get("coverage_score"),
                    "score": item.get("score") or item.get("composite_score"),
                }
        # 2026-09-03 owner feedback: the watchlist row often KNOWS the earnings
        # date (state.json tickers: earnings_date/earnings_when) even when the
        # ticker is outside the pre-earnings scoring universe — show it instead
        # of claiming "no earnings tracked".
        if quote.get("earnings_date") and str(quote.get("earnings_date")).upper() != "N/A":
            days_until = _earnings_countdown(quote.get("earnings_date"))
            if days_until is None:
                days_until = quote.get("earnings_days")
            info = {
                "available": True,
                "source": "watchlist_state",
                "earnings_date": quote.get("earnings_date"),
                "earnings_when": quote.get("earnings_when"),
                "days_until": days_until,
                # A date behind us is the LAST report, not the "next earnings".
                "is_past": bool(isinstance(days_until, int) and days_until < 0),
            }
            _remember_earnings(ticker, info)
            return info
        remembered = _LAST_KNOWN_EARNINGS.get(ticker)
        if remembered is None and _LAST_KNOWN_PATH.exists():
            try:
                disk = json.loads(_LAST_KNOWN_PATH.read_text(encoding="utf-8"))
                if isinstance(disk, dict):
                    _LAST_KNOWN_EARNINGS.update(disk)
            except Exception:
                pass
            remembered = _LAST_KNOWN_EARNINGS.get(ticker)
        if remembered:
            days_until = _earnings_countdown(remembered.get("earnings_date"))
            return {
                "available": True,
                **{k: v for k, v in remembered.items() if k not in ("days_until", "is_past", "source")},
                "source": "last_known",
                "days_until": days_until,
                "is_past": bool(isinstance(days_until, int) and days_until < 0),
            }
        return {"available": False}
    except Exception as exc:
        logger.debug("pre-earnings section failed for %s: %s", ticker, exc)
        return {"available": False}


def build_ticker_dossier(ticker: str, *, use_cache: bool = True) -> Dict[str, Any]:
    ticker = _norm_ticker(ticker)
    if not ticker:
        return {"available": False, "error": "ticker_required"}
    now = time.monotonic()
    with _dossier_lock:
        cached = _dossier_cache.get(ticker)
        if use_cache and cached and now - cached["at"] < DOSSIER_CACHE_TTL_SECONDS:
            return cached["payload"]

    heat = build_heat_list()
    hot_context = next((r for r in heat.get("heat", []) if r.get("ticker") == ticker), None)
    curated_item = next((r for r in heat.get("curated", []) if _norm_ticker(r.get("ticker")) == ticker), None)

    dossier = {
        "ticker": ticker,
        "as_of": _utc_now_iso(),
        "in_curated_hotlist": curated_item is not None,
        "hot_context": hot_context,
        "curated": curated_item,
        "quote": _quote_section(ticker),
        "news": _news_section(ticker),
        "trades": _trades_section(ticker),
        "decisions": _decisions_section(ticker),
        "deep_analysis": get_deep_analysis(ticker),
    }
    # pre_earnings section last: its fallback reuses the quote (watchlist-known
    # earnings date even when the ticker is outside the scoring universe).
    dossier["pre_earnings"] = _pre_earnings_section(ticker, quote=dossier["quote"])
    with _dossier_lock:
        _dossier_cache[ticker] = {"at": now, "payload": dossier}
    return dossier


# ------------------------------------------------------- deep analysis (debate)

def get_deep_analysis(ticker: str) -> Optional[Dict[str, Any]]:
    store = _read_json(DEEP_STORE_PATH, {})
    row = (store.get(ticker) if isinstance(store, dict) else None) or {}
    generated_at = str(row.get("generated_at") or "")
    if generated_at:
        try:
            stamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            row = {**row, "age_seconds": round(max(0.0, age)), "fresh": age < DEEP_ANALYSIS_TTL_SECONDS}
        except ValueError:
            pass
    return row or None


def run_deep_analysis(ticker: str, *, config: Any, force: bool = False) -> Dict[str, Any]:
    """Bounded bull/bear debate on a hot ticker (no live trade decision)."""
    ticker = _norm_ticker(ticker)
    if not ticker:
        return {"available": False, "error": "ticker_required"}
    with _deep_singleflights[ticker]:
        cached = get_deep_analysis(ticker)
        # A failed debate (available: false) must not poison the retry path
        # for the whole TTL — owner taps "Run debate" again expecting a retry
        # (audit 2026-09-05 P2: the failed row came back cached for 30 min).
        if not force and cached and cached.get("fresh") and cached.get("available"):
            return {**cached, "cached": True}

        dossier = build_ticker_dossier(ticker, use_cache=True)
        quote = dossier.get("quote") or {}
        trades = dossier.get("trades") or {}
        pe = dossier.get("pre_earnings") or {}
        change_pct = quote.get("change_pct")
        direction = "SHORT" if isinstance(change_pct, (int, float)) and change_pct < 0 else "LONG"
        # 2026-09-03 owner feedback ("sustine ca nu are date"): the debate must
        # receive the FULL market picture — price, change, RSI, trends, regime,
        # sector, dollar volume — not just a ticker and headlines.
        evidence_signal: Dict[str, Any] = {
            "ticker": ticker,
            "action": direction,
            "strategy_type": "HOTLIST_DEEP_ANALYSIS",
            "signal_score": quote.get("signal_score"),
            "price": quote.get("price"),
            "change_pct": quote.get("change_pct"),
            "rsi": quote.get("rsi"),
            "macd_hist": quote.get("macd_hist"),
            "trend_1d": quote.get("trend_1d"),
            "trend_4h": quote.get("trend_4h"),
            "trend_1h": quote.get("trend_1h"),
            "trend_30m": quote.get("trend_30m"),
            "trend_5m": quote.get("trend_5m"),
            "market_regime": quote.get("market_regime"),
            "sector": quote.get("sector"),
            "average_daily_volume_usd": quote.get("average_daily_volume_usd"),
            "news_headlines": [
                f"[{a.get('source')}] {a.get('title')}" for a in (dossier.get("news") or {}).get("articles", [])[:5]
            ],
        }
        # Stale engine technicals (missed a full session) are not evidence
        # about today's market: leave them out and tell the debaters why.
        if quote.get("technicals_stale"):
            for stale_key in ("rsi", "macd_hist", "trend_1d", "trend_4h", "trend_1h", "trend_30m", "trend_5m"):
                evidence_signal.pop(stale_key, None)
            evidence_signal["technicals_note"] = (
                f"trend/RSI snapshot last updated {quote.get('technicals_as_of') or 'unknown'} - stale, omitted"
            )
        if quote.get("price_as_of"):
            evidence_signal["price_as_of"] = quote.get("price_as_of")
        if quote.get("price_stale"):
            evidence_signal["price_note"] = "quote is stale - treat price and change as outdated"
        evidence_signal = {k: v for k, v in evidence_signal.items() if v is not None}
        review: Dict[str, Any] = {
            "available": True,
            "action": direction,
            "recommendation": "approve",
            "confidence": None,
            "reason": (
                f"HotList deep analysis: appears in {', '.join((dossier.get('hot_context') or {}).get('categories', []) or ['heat'])}; "
                f"traded {len((trades.get('closed_trades') or []))} times before."
            ),
        }
        if pe.get("available"):
            evidence_signal["pre_earnings_context"] = {
                "earnings_date": pe.get("earnings_date") or pe.get("earnings_time_utc"),
                "session": pe.get("session"),
                "gap_risk": pe.get("gap_risk"),
                "days_until": pe.get("days_until"),
            }
        if trades.get("available"):
            totals = trades.get("totals") or {}
            evidence_signal["strategy_base_rates"] = {
                "strategy": f"{ticker} trade history",
                "exact": {
                    "n": totals.get("trades") or 0,
                    "win_rate_pct": totals.get("win_rate_pct"),
                    "avg_pnl_pct": totals.get("avg_pnl_pct"),
                    "small_sample": (totals.get("trades") or 0) < 30,
                } if totals.get("trades") else None,
            }

        from core.ai.debate import run_entry_debate

        result = run_entry_debate(evidence_signal, review, config=config)
        row = {
            "ticker": ticker,
            "direction_frame": direction,
            "generated_at": _utc_now_iso(),
            "available": bool(result.get("available")),
            "bull": result.get("bull", ""),
            "bear": result.get("bear", ""),
            "verdict": result.get("verdict") or {},
            "models": result.get("models") or {},
            "error": result.get("error", ""),
            "prompt_version": result.get("prompt_version", ""),
        }
        store = _read_json(DEEP_STORE_PATH, {})
        if not isinstance(store, dict):
            store = {}
        history = list(store.get(f"{ticker}__history", []))
        if row.get("available"):
            history.append({"generated_at": row["generated_at"],
                            "verdict": row.get("verdict", {}).get("recommendation"),
                            "confidence": row.get("verdict", {}).get("confidence")})
        store[ticker] = row
        store[f"{ticker}__history"] = history[-DEEP_ANALYSIS_HISTORY:]
        try:
            DEEP_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = DEEP_STORE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
            tmp.replace(DEEP_STORE_PATH)
        except Exception as exc:
            logger.warning("deep analysis store write failed: %s", exc)
        with _dossier_lock:
            _dossier_cache.pop(ticker, None)  # dossier re-reads fresh analysis
        return {**row, "cached": False}


__all__ = [
    "build_heat_list", "build_ticker_dossier", "run_deep_analysis", "get_deep_analysis",
]
