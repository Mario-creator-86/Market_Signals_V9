"""Strategy Lab: search -> honest backtest -> research lead weighs in -> paper trading -> retire.

One run per day and per market:
1. refresh the price history;
2. test the lab's own rule space, the rules the research lead proposed last
   time, and every rule that is already trading or being watched;
3. the rules that pass the statistical bar are finalists; a few near misses
   are kept as candidates for a labelled exploration experiment;
4. the research lead (Claude Opus) gives a verdict on each and proposes new
   rules for the next run;
5. statistics and verdict together decide: a finalist is paper-traded only
   when it passed the bar AND the research lead approved it; an approved near
   miss may run as one small "explore" experiment; no usable verdict = watch;
6. a trading rule whose evidence fades for several runs, or which the research
   lead retires, stops opening positions;
7. for every trading rule the current members (what to hold long, what to
   hold short, until when) are written to the registry, where the live app
   picks them up.

Everything is recorded in ``data/research/strategy_lab/registry.json``.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import numpy as np

from core.strategy_lab import data as lab_data
from core.strategy_lab.engine import (
    Panel, default_space, describe_spec, distinct, evaluate, members_at, normalize_spec, rank_key, run_search, spec_id,
)
from core.strategy_lab.review import Ask, review_market, review_model

logger = logging.getLogger(__name__)

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
NY = ZoneInfo("America/New_York")
EPOCH = dt.date(1970, 1, 1)
REGISTRY_FILE = "registry.json"
TRADING = ("paper", "explore")

DEFAULT_LAB_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "run_hour_utc": 0,
    "run_minute_utc": 15,
    "finalists": 5,
    "near_misses": 4,
    # Rules trading at the same time per market (paper + explore together).
    "max_trading_per_market": 1,
    "allow_exploration": True,
    "retire_after_failed_runs": 3,
    "rejected_cooldown_days": 30,
    "entry_window_hours": {"crypto": 12, "stocks": 30},
    "refresh_data": True,
    # Kinds of rule already traded by a fixed experiment (the weekly momentum basket on crypto).
    "occupied_kinds": {"crypto": [["return", "follow", "both"]]},
}


def lab_config(root_config: Any) -> Dict[str, Any]:
    override = (root_config or {}).get("strategy_lab") if isinstance(root_config, dict) else None
    return {**DEFAULT_LAB_CONFIG, **(override if isinstance(override, dict) else {})}


def registry_path() -> Path:
    return lab_data.lab_dir() / REGISTRY_FILE


def load_registry(path: Optional[Path] = None, *, strict: bool = False) -> Dict[str, Any]:
    """The registry, or an empty one when the file does not exist.

    A reader (the app, the UI) gets an empty registry for an unreadable file. The
    writer asks with ``strict`` and stops instead: it must never replace a
    registry it could not read with an empty one.
    """
    path = Path(path) if path else registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        if strict:
            raise
        data = {}
    if not isinstance(data, dict):
        if strict:
            raise ValueError(f"{path} is not a JSON object")
        data = {}
    data.setdefault("version", 1)
    data.setdefault("runs", [])
    data.setdefault("markets", {})
    data.setdefault("strategies", {})
    return data


def save_registry(registry: Dict[str, Any], path: Optional[Path] = None) -> None:
    path = Path(path) if path else registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    registry["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(registry, ensure_ascii=False, indent=1), encoding="utf-8")
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:                              # Windows: a reader has the file open for a moment
            if attempt == 7:
                raise
            time.sleep(0.25)


def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat()


def _date(day: int) -> dt.date:
    return EPOCH + dt.timedelta(days=int(day))


def trading_ordinal(market: str, day: int) -> int:
    """A counter that grows by one per trading day of the market."""
    if market == "crypto":
        return int(day)
    return int(np.busday_count(EPOCH.isoformat(), _date(day).isoformat()))


def lab_strategy_types(strategy_id: str) -> Dict[str, str]:
    key = str(strategy_id).upper()
    return {"LONG": f"LAB_{key}_LONG", "SHORT": f"LAB_{key}_SHORT"}


# ------------------------------------------------------------ live schedule
def member_adv(panel: Panel, symbols: List[str], days: int = 20) -> Dict[str, float]:
    """Average daily traded value (USD) of each member over the last ``days`` bars.

    The Risk Constitution refuses a candidate whose liquidity cannot be shown.
    """
    out: Dict[str, float] = {}
    for symbol in symbols:
        if symbol in panel.symbols:
            window = panel.dollar_volume[-days:, panel.symbols.index(symbol)]
            if np.isfinite(window).any():
                out[symbol] = round(float(np.nanmean(window)), 2)
    return out


def live_plan(market: str, panel: Panel, spec: Dict[str, Any], now_ms: int, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Members chosen at the last completed day and the window in which they are entered and closed."""
    as_of = int(panel.days[-1])
    hold = int(spec["hold"])
    longs, shorts = members_at(panel, spec, panel.shape[0] - 1)
    if market == "crypto":
        enter_from = (as_of + 1) * DAY_MS
        exit_at = (as_of + 1 + hold) * DAY_MS - HOUR_MS
    else:
        first = lab_data.session_after(_date(as_of), 1)
        last = lab_data.session_after(_date(as_of), hold)
        enter_from = int(dt.datetime.combine(first, dt.time(9, 35), NY).timestamp() * 1000)
        exit_at = int(dt.datetime.combine(last, lab_data.stock_exit_time(last), NY).timestamp() * 1000)
    window = float((cfg.get("entry_window_hours") or {}).get(market, 12)) * HOUR_MS
    # Never enter when less than half of the holding period is left.
    enter_until = int(min(enter_from + window, enter_from + 0.5 * (exit_at - enter_from)))
    return {
        "as_of_day": as_of, "as_of_date": _date(as_of).isoformat(), "ranked_at": _iso(now_ms),
        # The day whose close the next ranking uses: the day the members are closed (sessions, not weekdays).
        "next_rebalance_day": _rebalance_day(market, as_of, hold),
        "enter_from_ms": int(enter_from), "enter_until_ms": enter_until, "exit_at_ms": int(exit_at),
        "enter_from": _iso(int(enter_from)), "enter_until": _iso(enter_until), "exit_at": _iso(int(exit_at)),
        "long": [panel.symbols[j] for j in longs], "short": [panel.symbols[j] for j in shorts],
        "adv_usd": member_adv(panel, [panel.symbols[j] for j in longs + shorts]),
    }


def _rebalance_day(market: str, as_of: int, hold: int) -> int:
    """The day (since 1970-01-01) on which members ranked on ``as_of`` are closed and the next ranking is made."""
    if market == "crypto":
        return int(as_of) + int(hold)
    return (lab_data.session_after(_date(as_of), hold) - EPOCH).days


def last_row_complete(panel: Panel, share: float = 0.9, window: int = 10) -> bool:
    """False when the newest day has bars for only part of the instruments (a refresh that stopped half-way)."""
    if panel.shape[0] <= window:
        return True
    expected = np.isfinite(panel.close[-1 - window:-1]).any(axis=0)
    have = np.isfinite(panel.close[-1]) & expected
    return bool(expected.sum()) and bool(have.sum() >= share * expected.sum())


def _refresh_live(market: str, panel: Panel, strategy: Dict[str, Any], now_ms: int, cfg: Dict[str, Any]) -> None:
    live = strategy.get("live") if isinstance(strategy.get("live"), dict) else {}
    last_day = int(panel.days[-1])
    if live:
        due = live.get("next_rebalance_day")
        if due is None:                                      # a plan written before the schedule counted sessions
            due = _rebalance_day(market, int(live.get("as_of_day") or last_day), int(strategy["spec"]["hold"]))
            if live.get("missed_window"):
                due = _rebalance_day(market, int(live.get("as_of_day") or last_day), 1)
            live["next_rebalance_day"] = due
        if last_day < int(due):
            if "adv_usd" not in live:                        # a plan written before liquidity was recorded
                live["adv_usd"] = member_adv(panel, list(live.get("long") or []) + list(live.get("short") or []))
            return                                           # between two rankings: the members stay
    plan = live_plan(market, panel, strategy["spec"], now_ms, cfg)
    if not last_row_complete(panel):
        # Ranking a day that only part of the instruments have reported would trade a basket nobody tested.
        plan.update({"long": [], "short": [], "adv_usd": {}, "incomplete_data": True,
                     "next_rebalance_day": _rebalance_day(market, last_day, 1)})
    elif plan["enter_until_ms"] < now_ms:
        # The run came too late to enter this ranking (the app was off, or the rule was promoted
        # in the middle of the day): rank again on the next run instead of idling a whole period.
        plan["missed_window"] = True
        plan["next_rebalance_day"] = _rebalance_day(market, last_day, 1)
    strategy["live"] = plan


# ------------------------------------------------------------ paper results
def _journal_closed(journal_path: Optional[Path]) -> Optional[List[Dict[str, Any]]]:
    """Closed trades from the app's trade journal, or None when it cannot be read."""
    try:
        if journal_path is None:
            from core.feedback_loop import FEEDBACK_JOURNAL_PATH

            journal_path = Path(FEEDBACK_JOURNAL_PATH)
        rows = json.loads(Path(journal_path).read_text(encoding="utf-8"))
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else None
    except (OSError, ValueError, ImportError):
        return None


def paper_results(db_path: Optional[Path] = None, journal_path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Closed and open paper positions per lab rule, read-only.

    Open positions come from the app's database. Closed ones come from the trade
    journal, which keeps far more history than the database (that one holds only
    the account's last 300 closed positions, of all strategies together). When
    the journal cannot be read the database is used and the result says so.
    """
    if db_path is None:
        try:
            from core.sqlite_store import DB_PATH

            db_path = Path(DB_PATH)
        except Exception:  # noqa: BLE001
            return {}
    try:
        connection = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            row = connection.execute("select value from kv_store where key='capital_state'").fetchone()
        finally:
            connection.close()
        positions = (json.loads(row[0]) if row else {}).get("positions") or []
    except (sqlite3.Error, ValueError, TypeError, OSError) as exc:
        logger.warning("Strategy lab: paper results unavailable (%s)", exc)
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    journal = _journal_closed(journal_path)

    def bucket(strategy_type: Any) -> Optional[Dict[str, Any]]:
        name = str(strategy_type or "").upper()
        if not name.startswith("LAB_") or len(name.split("_")) < 3:
            return None
        return out.setdefault(name.split("_")[1].lower(), {
            "open": 0, "closed": 0, "wins": 0, "net_pnl_usd": 0.0, "sum_pct": 0.0,
            "closed_from": "trade journal" if journal is not None else "database (last 300 closed positions of the account)",
        })

    def add_closed(row: Dict[str, Any], pnl: Any, pct: Any) -> None:
        try:
            pnl, pct = float(pnl or 0.0), float(pct or 0.0)
        except (TypeError, ValueError):
            return
        row["closed"] += 1
        row["wins"] += 1 if pnl > 0 else 0
        row["net_pnl_usd"] += pnl
        row["sum_pct"] += pct

    for position in positions:
        row = bucket((position or {}).get("strategy_type"))
        if row is None:
            continue
        if str(position.get("status") or "").upper() == "OPEN":
            row["open"] += 1
        elif journal is None:
            add_closed(row, position.get("net_pnl"), position.get("pnl_pct_notional"))
    for trade in journal or []:
        row = bucket(trade.get("strategy_type"))
        if row is not None:
            add_closed(row, trade.get("net_pnl"), trade.get("pnl_pct"))
    for row in out.values():
        row["net_pnl_usd"] = round(row["net_pnl_usd"], 2)
        total_pct = row.pop("sum_pct")
        row["mean_pct"] = round(total_pct / row["closed"], 3) if row["closed"] else None
        row["win_rate_pct"] = round(100 * row["wins"] / row["closed"], 1) if row["closed"] else None
    return out


# ---------------------------------------------------------------- one market
def _kind(spec: Dict[str, Any]) -> List[str]:
    return [spec["metric"], spec["direction"], spec["sides"]]


def _still_holds(result: Dict[str, Any], status: str) -> bool:
    full, holdout = result["full"], result["holdout"]
    if not (full.get("ok") and holdout.get("ok")):
        return False
    floor = 1.5 if status == "paper" else 1.0
    return bool(full["alpha_t"] >= floor and holdout["mean_daily_pct"] > 0)


PERSISTENCE_BARS = (5, 10, 21)


def persists(panel: Panel, spec: Dict[str, Any]) -> bool:
    """True when the rule also passed the bar as the history stood 5, 10 and 21 bars ago.

    The lab runs every day on almost the same data, so a rule that clears the bar
    on one end date and not on the others is a blip, not a finding.
    """
    for back in PERSISTENCE_BARS:
        if panel.shape[0] - back < 250 or not evaluate(panel.truncated(back), spec)["passes"]:
            return False
    return True


def _stats(result: Dict[str, Any]) -> Dict[str, Any]:
    return {"train": result["train"], "holdout": result["holdout"], "full": result["full"],
            "passes": bool(result["passes"]), "train_until": result.get("train_until")}


def run_market(market: str, registry: Dict[str, Any], cfg: Dict[str, Any], now_ms: int, *,
               panel: Optional[Panel] = None, ask: Optional[Ask] = None, model: str = "",
               results_by_rule: Optional[Dict[str, Dict[str, Any]]] = None,
               space: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Run the lab for one market and update the registry in place. Returns the run record."""
    started = time.time()
    record: Dict[str, Any] = {"at": _iso(now_ms), "market": market, "rules_tested": 0, "passed": 0, "error": ""}
    panel = panel if panel is not None else lab_data.load_panel(market, now_ms)
    if panel is None:
        record["error"] = "not enough price history yet"
        return record
    market_cfg = lab_data.MARKETS[market]
    cap = int(market_cfg["max_names"])
    board = registry["markets"].setdefault(market, {})
    strategies: Dict[str, Dict[str, Any]] = registry["strategies"]
    mine = {sid: s for sid, s in strategies.items() if s.get("market") == market}

    min_hold = int(market_cfg.get("min_hold", 1))
    base = default_space(cap, min_hold) if space is None else [s for s in (normalize_spec({**raw, "max_names": cap}) for raw in space) if s]
    specs = list(base)
    for raw in board.get("proposals") or []:
        spec = normalize_spec({**raw, "max_names": cap}) if isinstance(raw, dict) else None
        if spec and spec["hold"] >= min_hold:
            specs.append(spec)
    proposed_ids = {spec_id(s) for s in specs[len(base):]} - {spec_id(s) for s in base}
    specs += [s["spec"] for s in mine.values() if normalize_spec(s.get("spec"))]
    results = run_search(panel, specs)
    by_id = {r["id"]: r for r in results}
    record.update({
        "rules_tested": len(results), "passed": sum(1 for r in results if r["passes"]),
        "data": f"{panel.shape[1]} instruments, {panel.shape[0]} daily bars, {_date(int(panel.days[0]))} .. {_date(int(panel.days[-1]))}",
        "data_last_day": _date(int(panel.days[-1])).isoformat(),
    })

    # 1. Rules already in the registry: fresh statistics, and the fade-out rule for the ones trading.
    limit = max(1, int(cfg.get("retire_after_failed_runs", 3)))
    for sid, strategy in mine.items():
        result = by_id.get(sid)
        if result is None:
            continue
        strategy["stats"] = _stats(result)
        strategy["last_evaluated_at"] = _iso(now_ms)
        if strategy.get("status") in TRADING:
            strategy["fail_streak"] = 0 if _still_holds(result, strategy["status"]) else int(strategy.get("fail_streak") or 0) + 1
            if strategy["fail_streak"] >= limit:
                _set_status(strategy, "retired", now_ms,
                            f"the evidence did not hold in {strategy['fail_streak']} consecutive runs")

    # 2. Finalists and near misses.
    occupied = [list(k) for k in (cfg.get("occupied_kinds") or {}).get(market, [])]
    cooldown_ms = float(cfg.get("rejected_cooldown_days", 30)) * DAY_MS

    def fresh(result: Dict[str, Any]) -> bool:
        known = mine.get(result["id"])
        if _kind(result["spec"]) in occupied:
            return False
        if known is None:
            return True
        if known.get("status") in TRADING:
            return False
        if known.get("status") in ("rejected", "retired"):
            changed = dt.datetime.fromisoformat(known.get("status_changed_at") or _iso(0)).timestamp() * 1000
            return now_ms - changed >= cooldown_ms
        return True

    passing = distinct([r for r in results if r["passes"] and fresh(r)], int(cfg.get("finalists", 5)))
    finalists = [r for r in passing if persists(panel, r["spec"])]
    blips = [{**r, "passed_only_on_the_latest_data": True} for r in passing if r not in finalists]
    record["passed_today_only"] = len(blips)
    record["incomplete_data"] = not last_row_complete(panel)
    near = [r for r in results if not r["passes"] and fresh(r)
            and r["train"].get("ok") and r["holdout"].get("ok")
            and r["train"]["alpha_t"] >= 1.0 and r["holdout"]["alpha_t"] >= 0.5 and r["holdout"]["mean_daily_pct"] > 0
            and r["full"]["max_drawdown_pct"] >= -50.0]
    near_misses = distinct(blips + near, int(cfg.get("near_misses", 4)))

    # 3. The research lead weighs in.
    live_results = results_by_rule if results_by_rule is not None else paper_results()
    trading_rows = []
    for sid, strategy in mine.items():
        if strategy.get("status") in TRADING:
            trading_rows.append({"id": sid, "status": strategy["status"], "rule": strategy.get("description"),
                                 "statistics_now": strategy.get("stats"), "paper_results": live_results.get(sid),
                                 "runs_in_a_row_below_the_bar": strategy.get("fail_streak", 0)})
    review = review_market(market, finalists=finalists, near_misses=near_misses, trading=trading_rows,
                           summary={"data": record["data"], "rules_tested": record["rules_tested"], "passed": record["passed"]},
                           model=model or review_model(None), ask=ask, best_of_the_rest=distinct(results, 6))
    record["review"] = {"available": review["available"], "model": review.get("model"), "error": review.get("error", "")}

    # 4. Statistics and verdict together decide.
    def slots() -> int:
        active = sum(1 for s in strategies.values() if s.get("market") == market and s.get("status") in TRADING)
        return max(0, int(cfg.get("max_trading_per_market", 1)) - active)

    def upsert(result: Dict[str, Any], origin: str) -> Dict[str, Any]:
        sid = result["id"]
        strategy = strategies.get(sid)
        if strategy is None:
            strategy = strategies[sid] = {
                "id": sid, "market": market, "spec": result["spec"], "description": describe_spec(result["spec"], market),
                "status": "new", "origin": origin, "created_at": _iso(now_ms), "fail_streak": 0,
                "strategy_types": lab_strategy_types(sid),
            }
        strategy["stats"] = _stats(result)
        strategy["last_evaluated_at"] = _iso(now_ms)
        return strategy

    for result in finalists:
        strategy = upsert(result, "research lead" if result["id"] in proposed_ids else "search")
        verdict = review["verdicts"].get(result["id"]) if review["available"] else None
        strategy["review"] = {**(verdict or {"verdict": "none", "confidence": 0.0, "reason": review.get("error") or "no verdict given"}),
                              "model": review.get("model"), "at": _iso(now_ms)}
        if verdict is None:
            _set_status(strategy, "watch", now_ms, "passed the statistical bar; waiting for the research lead's verdict")
        elif verdict["verdict"] == "approve":
            if slots() > 0:
                _set_status(strategy, "paper", now_ms, "passed the statistical bar and the research lead approved it")
            else:
                _set_status(strategy, "watch", now_ms, "passed the bar and was approved; waiting for a free trading slot")
        elif verdict["verdict"] == "reject":
            _set_status(strategy, "rejected", now_ms, "passed the statistical bar but the research lead rejected it")
        elif verdict["verdict"] == "explore" and bool(cfg.get("allow_exploration", True)) and slots() > 0:
            # Passed, but the research lead does not call it an approved strategy: trade it as a labelled experiment.
            _set_status(strategy, "explore", now_ms,
                        "passed the statistical bar; the research lead wants it run only as a small labelled paper experiment")
        else:
            _set_status(strategy, "watch", now_ms, "passed the statistical bar; the research lead wants more evidence")

    if review["available"] and bool(cfg.get("allow_exploration", True)):
        for result in near_misses:
            verdict = review["verdicts"].get(result["id"])
            if not verdict or verdict["verdict"] != "explore" or slots() <= 0:
                continue
            strategy = upsert(result, "research lead" if result["id"] in proposed_ids else "search")
            strategy["review"] = {**verdict, "model": review.get("model"), "at": _iso(now_ms)}
            _set_status(strategy, "explore", now_ms,
                        "did not pass the statistical bar; the research lead asked for a small labelled paper experiment")

    for sid, strategy in mine.items():
        verdict = review["verdicts"].get(sid) if review["available"] else None
        if verdict and strategy.get("status") in TRADING:
            strategy["review"] = {**verdict, "model": review.get("model"), "at": _iso(now_ms)}
            if verdict["verdict"] in ("retire", "reject"):
                _set_status(strategy, "retired", now_ms, "retired by the research lead: " + verdict["reason"])

    # 5. What the trading rules hold now.
    for strategy in strategies.values():
        if strategy.get("market") != market:
            continue
        if strategy.get("status") in TRADING:
            _refresh_live(market, panel, strategy, now_ms, cfg)
        else:
            strategy.pop("live", None)
        strategy["paper_results"] = live_results.get(strategy["id"])

    # 6. The board for the owner.
    tested = {r["id"] for r in results}
    board.update({
        "updated_at": _iso(now_ms), "data": record["data"], "rules_tested": record["rules_tested"],
        "passed": record["passed"],
        "leaderboard": [{"id": r["id"], "description": describe_spec(r["spec"], market), "spec": r["spec"],
                         **_stats(r), "origin": "research lead" if r["id"] in proposed_ids else "search",
                         "score": round(rank_key(r), 2)} for r in distinct(results, 12)],
        "proposals": [s for s in review["proposals"]
                      if spec_id(normalize_spec({**s, "max_names": cap}) or s) not in tested] if review["available"] else (board.get("proposals") or []),
        "commentary": review["commentary"] if review["available"] else board.get("commentary", ""),
        "commentary_at": _iso(now_ms) if review["available"] else board.get("commentary_at"),
        "review_model": review.get("model"),
        "review_error": review.get("error", ""),
    })
    record["seconds"] = round(time.time() - started, 1)
    record["trading"] = sorted(sid for sid, s in strategies.items() if s.get("market") == market and s.get("status") in TRADING)
    return record


def _set_status(strategy: Dict[str, Any], status: str, now_ms: int, decision: str) -> None:
    if strategy.get("status") != status:
        strategy.setdefault("history", []).append({"at": _iso(now_ms), "from": strategy.get("status"), "to": status, "why": decision})
        strategy["history"] = strategy["history"][-20:]
        strategy["status"] = status
        strategy["status_changed_at"] = _iso(now_ms)
        if status in TRADING:
            strategy["fail_streak"] = 0
    strategy["decision"] = decision


# --------------------------------------------------------------- whole run
def run_once(now_ms: Optional[int] = None, *, config: Any = None, refresh: Optional[bool] = None,
             markets: Optional[List[str]] = None, ask: Optional[Ask] = None,
             path: Optional[Path] = None) -> Dict[str, Any]:
    """One full lab run: refresh the data, run every market, save the registry."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    cfg = lab_config(config)
    registry = load_registry(path, strict=True)              # never replace a registry that could not be read
    refresh = bool(cfg.get("refresh_data", True)) if refresh is None else refresh
    data_status: Dict[str, Any] = {}
    if refresh:
        try:
            data_status["crypto"] = lab_data.refresh_crypto(now_ms=now_ms)
        except Exception as exc:  # noqa: BLE001 - the run continues on the cached history
            data_status["crypto"] = {"ok": False, "reason": f"{exc.__class__.__name__}: {exc}"}
        try:
            data_status["stocks"] = lab_data.refresh_stocks(now_ms=now_ms)
            data_status["stocks"]["symbol_directory"] = lab_data.refresh_stock_meta(now_ms=now_ms)
        except Exception as exc:  # noqa: BLE001
            data_status["stocks"] = {"ok": False, "reason": f"{exc.__class__.__name__}: {exc}"}
        try:
            data_status["crypto"]["funding"] = lab_data.refresh_funding(now_ms=now_ms)
        except Exception as exc:  # noqa: BLE001
            data_status["crypto"]["funding"] = {"ok": False, "reason": f"{exc.__class__.__name__}: {exc}"}
    model = review_model(config)
    for market in markets or list(lab_data.MARKETS):
        try:
            record = run_market(market, registry, cfg, now_ms, ask=ask, model=model)
        except Exception as exc:  # noqa: BLE001 - one market must not stop the other
            logger.exception("Strategy lab: %s run failed", market)
            record = {"at": _iso(now_ms), "market": market, "rules_tested": 0, "passed": 0,
                      "error": f"{exc.__class__.__name__}: {exc}"}
        record["data_refresh"] = data_status.get(market)
        registry["runs"] = (registry["runs"] + [record])[-60:]
        save_registry(registry, path)                        # one market's result must not wait for the other
    done = markets or list(lab_data.MARKETS)
    data_ok = all(bool((data_status.get(market) or {}).get("ok", True)) for market in done)
    run_ok = data_ok and not any(record.get("error") for record in registry["runs"][-len(done):]
                                 if record.get("error") != "not enough price history yet")
    registry["last_run_at"] = _iso(now_ms)
    registry["last_run_ok"] = bool(run_ok)
    if run_ok:
        # A day counts as done only when its data arrived whole; otherwise the scheduler tries again later.
        registry["last_run_day"] = now_ms // DAY_MS
    save_registry(registry, path)
    return registry


__all__ = [
    "DEFAULT_LAB_CONFIG", "TRADING", "evaluate", "lab_config", "lab_strategy_types", "live_plan", "load_registry",
    "last_row_complete", "paper_results", "persists", "registry_path", "run_market", "run_once", "save_registry",
    "trading_ordinal",
]
