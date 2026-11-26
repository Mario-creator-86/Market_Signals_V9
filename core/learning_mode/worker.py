"""Learning mode worker: asks Opus about one scanner mover a minute and about fresh news every 5 minutes.

Runs as a daemon thread in the engine process, next to the AgentManager. What
Opus decides to trade goes into the LearningBook; the AgentManager's
autonomous cycle takes it from there through the normal Risk, price and
Capital gates. Every input is a callable so that one failing source (scanner,
news, quotes, Opus) is reported and never stops the others.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from core.learning_mode import trader
from core.learning_mode.book import LearningBook
from core.learning_mode.settings import CRYPTO_MAJORS, MACRO_INSTRUMENTS, learning_config

logger = logging.getLogger("LearningMode")

_US_TICKER = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


def _default_session(now: datetime) -> Dict[str, Any]:
    from zoneinfo import ZoneInfo

    from core.market_hours import is_us_equity_market_open

    open_now = bool(is_us_equity_market_open(now))
    clock = now.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M ET")
    return {"us_regular": open_now,
            "label": f"US regular session ({clock})" if open_now else f"US stock market closed ({clock}); crypto trades 24/7"}


def _default_usage() -> Dict[str, Any]:
    from core.ai.subscription_routing import subscription_usage

    return subscription_usage()


def _ticker(symbol: Any) -> str:
    from core.autonomous_candidates import _scanner_symbol_to_ticker

    return _scanner_symbol_to_ticker(symbol)


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and abs(number) != float("inf") else 0.0


class LearningWorker:
    def __init__(self, *, book: LearningBook, config: Callable[[], Dict[str, Any]],
                 scanner_state: Callable[[], Dict[str, Any]], news_rows: Callable[[], List[Dict[str, Any]]],
                 positions: Callable[[], List[Dict[str, Any]]], quote: Callable[[str, str], Optional[float]],
                 ask_mover: Callable[..., Dict[str, Any]] = trader.ask_mover,
                 ask_news: Callable[..., Dict[str, Any]] = trader.ask_news,
                 usage: Callable[[], Dict[str, Any]] = _default_usage,
                 session: Callable[[datetime], Dict[str, Any]] = _default_session,
                 excluded: Callable[[str], bool] = lambda ticker: False,
                 ticker_news: Callable[[str], List[Dict[str, Any]]] = lambda ticker: [],
                 market_context: Optional[Callable[[Dict[str, Any]], Any]] = None,
                 should_run: Callable[[], bool] = lambda: True,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.book = book
        self._config, self._scanner_state, self._news_rows = config, scanner_state, news_rows
        self._positions, self._quote, self._ask_mover, self._ask_news = positions, quote, ask_mover, ask_news
        self._usage, self._session, self._excluded, self._ticker_news = usage, session, excluded, ticker_news
        self._market_context, self._clock, self._should_run = market_context, clock, should_run
        self._last_news_scan: Optional[datetime] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_result: Dict[str, Any] = {"status": "not_run"}

    # --- one pass --------------------------------------------------------------------------------
    def run_once(self) -> Dict[str, Any]:
        now = self._clock()
        result: Dict[str, Any] = {"at": now.isoformat(), "errors": []}
        try:
            exec_cfg = self._config() or {}
        except Exception as exc:  # noqa: BLE001
            return self._done({**result, "status": "error", "reason": f"config: {exc}"})
        cfg = learning_config(exec_cfg)
        if str(exec_cfg.get("mode", "paper")).lower() != "paper":
            return self._done({**result, "status": "off", "reason": "live mode: learning mode trades on paper only"})
        if not cfg.get("enabled", True):
            return self._done({**result, "status": "off", "reason": "learning mode switched off"})
        if not self._safe(result, "switches", self._should_run):
            return self._done({**result, "status": "off",
                               "reason": "the bot, the autonomous cycle or the learning source is switched off"})

        positions = self._safe(result, "positions", self._positions) or []
        self._safe(result, "ledger", lambda: self.book.sync_closed(positions))
        self._positions_seen, self._cfg_seen = positions, cfg
        paused = self._pause_reason(cfg, positions, now, result)
        if paused:
            return self._done({**result, "status": "paused", "reason": paused})

        session = self._safe(result, "session", lambda: self._session(now)) or {"us_regular": False, "label": "unknown"}
        scanner = self._safe(result, "scanner", self._scanner_state) or {}
        if not self._scanner_fresh(scanner, exec_cfg):
            result["errors"].append(f"scanner snapshot is {scanner.get('data_age_seconds')} s old: not used")
            scanner = {}
        held = {str(p.get("ticker") or "").upper() for p in positions
                if isinstance(p, dict) and str(p.get("status") or "").upper() == "OPEN"}
        market = self._safe(result, "market context", lambda: self._market_context(scanner)) if self._market_context else None
        context = {"session": session.get("label"), "market": market}

        result["mover"] = self._safe(result, "mover", lambda: self._ask_one_mover(cfg, exec_cfg, scanner, session, held, context, now))
        interval = float(cfg.get("news_interval_seconds", 300))
        if self._last_news_scan is None or (now - self._last_news_scan).total_seconds() >= interval:
            self._last_news_scan = now
            result["news"] = self._safe(result, "news", lambda: self._ask_news_once(cfg, scanner, session, held, context, now))
        return self._done({**result, "status": "ran"})

    def _pause_reason(self, cfg, positions, now, result) -> str:
        usage = self._safe(result, "opus usage", self._usage) or {}
        reserve = int(cfg.get("opus_reserve_calls", 600) or 0)
        for name, row in (usage.items() if isinstance(usage, dict) else []):
            limit = int(_number((row or {}).get("daily_call_limit")))
            calls = int(_number((row or {}).get("calls_today")))
            if limit > 0 and calls >= limit - reserve:
                return f"{name}: {calls} of {limit} calls used today; the last {reserve} are kept for the app"
        share = int(cfg.get("max_open_positions", 15) or 0)
        open_learning = sum(1 for p in positions if isinstance(p, dict) and str(p.get("status") or "").upper() == "OPEN"
                            and str(p.get("strategy_type") or "").upper().startswith(("AI_MOVER_", "AI_NEWS_")))
        if share and open_learning >= share:
            return f"{open_learning} of {share} learning positions open: the rest stay free for the other experiments"
        cap = int(cfg.get("max_new_per_day", 100) or 0)
        taken = self.book.opened_today(positions, now) + self.book.pending_count(now, float(cfg["idea_ttl_minutes"]))
        if cap and taken >= cap:
            return f"daily cap reached: {taken} of {cap} learning trades today"
        return ""

    def _movers(self, cfg, exec_cfg, scanner, session, held) -> List[Dict[str, Any]]:
        threshold = float(cfg.get("mover_min_abs_change_pct", 5.0))
        min_dollar = float(exec_cfg.get("scanner_min_dollar_volume", 10_000_000) or 0)
        rows: List[Dict[str, Any]] = []
        groups = (("stock", scanner.get("hot_tickers")), ("stock", scanner.get("watchlist_stock_tickers")),
                  ("crypto", scanner.get("liquid_crypto_tickers")), ("crypto", scanner.get("watchlist_crypto_tickers")))
        seen = set()
        for kind, items in groups:
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                ticker = _ticker(item.get("symbol") or item.get("ticker"))
                price, change = _number(item.get("price") or item.get("lastPrice")), _number(item.get("change_pct"))
                volume = _number(item.get("volume") or item.get("quoteVolume"))
                crypto = ticker.endswith("-USD")
                dollar_volume = volume if crypto else price * volume
                if (not ticker or ticker in seen or price <= 0 or abs(change) < threshold or ticker in held
                        or (min_dollar and dollar_volume < min_dollar) or (not crypto and not session.get("us_regular"))
                        or self._excluded(ticker)):
                    continue
                seen.add(ticker)
                rows.append({"ticker": ticker, "price": price, "change_pct": round(change, 4), "volume": volume,
                             "dollar_volume": round(dollar_volume, 2), "source": item.get("source") or kind,
                             "asset_class": "CRYPTO_FUTURES" if crypto else "US_STOCKS", "_item": item})
        return sorted(rows, key=lambda row: abs(row["change_pct"]), reverse=True)

    def _ask_one_mover(self, cfg, exec_cfg, scanner, session, held, context, now) -> Optional[Dict[str, Any]]:
        cooldown = float(cfg.get("mover_ask_cooldown_minutes", 60))
        for row in self._movers(cfg, exec_cfg, scanner, session, held):
            if not self.book.may_ask_mover(row["ticker"], now, cooldown):
                continue
            headlines = list(self._safe({}, "ticker news", lambda: self._ticker_news(row["ticker"])) or [])[:8]
            evidence = None
            if row["asset_class"] == "US_STOCKS":
                from core.autonomous_candidates import MOVER_REPLAY_EVIDENCE

                evidence = MOVER_REPLAY_EVIDENCE
            item = {k: v for k, v in row.items() if k != "_item"}
            reply = self._ask_mover(item, {**context, "headlines": headlines, "evidence": evidence}, cfg)
            self.book.note_mover_asked(row["ticker"], now)
            idea = reply.get("idea")
            if idea:
                self.book.add_idea(idea, ticker=row["ticker"], source="mover", price=row["price"],
                                   asset_class=row["asset_class"], headlines=headlines,
                                   model=str(reply.get("model") or ""), now=now, dollar_volume=row["dollar_volume"])
                logger.info("Learning mode: Opus chose %s %s (confidence %.2f)", idea["side"], row["ticker"], idea["confidence"])
            return {"ticker": row["ticker"], "side": idea["side"] if idea else "NONE", "queued": bool(idea),
                    **({"error": reply["error"]} if reply.get("error") else {})}
        return None

    def _tradable(self, session, headlines, scanner) -> List[str]:
        named: List[str] = []
        for row in headlines:
            for raw in [row.get("ticker"), *(row.get("related_tickers") or [])]:
                symbol = str(raw or "").strip().upper()
                if symbol and symbol not in named:
                    named.append(symbol)
        crypto_named = [_ticker(s) if not s.endswith("-USD") else s for s in named
                        if s.endswith(("-USD", "USDT")) or s in {"BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"}]
        crypto_named = [s if s.endswith("-USD") else f"{s}-USD" for s in crypto_named if s]
        out = list(CRYPTO_MAJORS) + [s for s in crypto_named if s not in CRYPTO_MAJORS]
        if session.get("us_regular"):
            stocks = [s for s in named if _US_TICKER.match(s) and s not in {"BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"}]
            out = list(MACRO_INSTRUMENTS) + [s for s in stocks if s not in MACRO_INSTRUMENTS] + out
        return [s for s in dict.fromkeys(out) if not self._excluded(s)]

    @staticmethod
    def _scanner_fresh(scanner: Dict[str, Any], exec_cfg: Dict[str, Any]) -> bool:
        """False when the snapshot is older than the autonomous cycle accepts (it refuses it too)."""
        age = scanner.get("data_age_seconds") if isinstance(scanner, dict) else None
        if not isinstance(age, (int, float)):
            return True
        return age <= float((exec_cfg or {}).get("autonomous_scanner_max_data_age_seconds", 120.0) or 120.0)

    def latest_scanner_price(self, ticker: str) -> Optional[tuple]:
        """(the scanner's newest price, when it was observed) for a ticker, or None (not listed, or stale)."""
        scanner = self._scanner_state() or {}
        if not self._scanner_fresh(scanner, self._config() or {}):
            return None
        age = scanner.get("data_age_seconds")
        observed = self._clock() - timedelta(seconds=float(age)) if isinstance(age, (int, float)) else self._clock()
        for key in ("hot_tickers", "watchlist_stock_tickers", "liquid_crypto_tickers", "watchlist_crypto_tickers"):
            for item in scanner.get(key) or []:
                if isinstance(item, dict) and _ticker(item.get("symbol") or item.get("ticker")) == ticker:
                    price = _number(item.get("price") or item.get("lastPrice"))
                    return (price, observed) if price > 0 else None
        return None

    def _price_of(self, ticker: str, side: str, scanner) -> float:
        for key in ("hot_tickers", "watchlist_stock_tickers", "liquid_crypto_tickers", "watchlist_crypto_tickers"):
            for item in scanner.get(key) or []:
                if isinstance(item, dict) and _ticker(item.get("symbol") or item.get("ticker")) == ticker:
                    price = _number(item.get("price") or item.get("lastPrice"))
                    if price > 0:
                        return price
        return _number(self._quote(ticker, side))

    def _ask_news_once(self, cfg, scanner, session, held, context, now) -> Optional[Dict[str, Any]]:
        rows = self._news_rows() or []
        fresh = self.book.unseen_headlines(rows, now, max_age_minutes=float(cfg["news_max_age_minutes"]),
                                           min_impact=float(cfg["news_min_impact_score"]))
        if not fresh:
            return {"headlines": 0}
        fresh = sorted(fresh, key=lambda r: (-_number(r.get("impact_score")), r.get("age_minutes", 0)))
        fresh = fresh[: int(cfg.get("news_max_headlines", 12) or 12)]
        tradable = self._tradable(session, fresh, scanner)
        headlines = [{"id": r["id"], "title": r.get("title"), "source": r.get("source"), "age_minutes": r.get("age_minutes"),
                      "impact": r.get("impact"), "impact_score": r.get("impact_score"), "summary": r.get("summary"),
                      "tickers": [t for t in [r.get("ticker"), *(r.get("related_tickers") or [])] if t]} for r in fresh]
        reply = self._ask_news(headlines, tradable, context, cfg)
        if reply.get("error"):
            return {"headlines": len(headlines), "queued": [], "error": reply["error"]}   # asked again next scan
        self.book.note_headlines_seen(fresh, now)
        queued: List[str] = []
        for idea in reply.get("ideas") or []:
            ticker = idea["ticker"]
            price = self._price_of(ticker, idea["side"], scanner) if ticker not in held else 0.0
            if price <= 0:
                continue
            used = [h for h in headlines if h["id"] in set(idea.get("headline_ids") or [])] or headlines[:3]
            self.book.add_idea({k: v for k, v in idea.items() if k != "ticker"}, ticker=ticker, source="news",
                               price=price, asset_class="CRYPTO_FUTURES" if ticker.endswith("-USD") else "US_STOCKS",
                               headlines=[{"title": h["title"], "source": h["source"], "age": f"{h['age_minutes']}m"}
                                          for h in used],
                               model=str(reply.get("model") or ""), now=now)
            queued.append(f"{idea['side']} {ticker}")
        if queued:
            logger.info("Learning mode: Opus traded the news: %s", ", ".join(queued))
        return {"headlines": len(headlines), "queued": queued, **({"error": reply["error"]} if reply.get("error") else {})}

    # --- plumbing --------------------------------------------------------------------------------
    @staticmethod
    def _safe(result: Dict[str, Any], label: str, work: Callable[[], Any]) -> Any:
        try:
            return work()
        except Exception as exc:  # noqa: BLE001 - one source failing must not stop the others
            result.setdefault("errors", []).append(f"{label}: {type(exc).__name__}: {exc}"[:240])
            return None

    def _done(self, result: Dict[str, Any]) -> Dict[str, Any]:
        self.last_result = result
        try:
            cfg = getattr(self, "_cfg_seen", None) or learning_config(self._config() or {})
            positions = getattr(self, "_positions_seen", None) or []
            now = self._clock()
            self.book.write_status({
                "updated_at": now.isoformat(),
                "status": result.get("status"),
                "paused_reason": result.get("reason", "") if result.get("status") in {"off", "paused", "error"} else "",
                "last_pass": {k: result.get(k) for k in ("at", "mover", "news", "errors")},
                "opened_today": self.book.opened_today(positions, now),
                "daily_cap": int(cfg.get("max_new_per_day", 100) or 0),
                "progress": self.book.progress(cfg, positions),
            })
        except Exception:  # noqa: BLE001 - the status file is a view, never a reason to stop
            logger.warning("Learning mode: status could not be written", exc_info=True)
        return result

    def start(self, interval_seconds: Optional[float] = None) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception:  # noqa: BLE001
                    logger.exception("Learning mode: worker pass failed")
                every = interval_seconds or float(learning_config(self._safe({}, "config", self._config) or {})
                                                  .get("worker_interval_seconds", 60))
                self._stop.wait(max(10.0, every))

        self._thread = threading.Thread(target=loop, name="LearningModeWorker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


_WORKER: Optional[LearningWorker] = None
_WORKER_LOCK = threading.Lock()


def get_learning_worker(build: Optional[Callable[[], LearningWorker]] = None) -> Optional[LearningWorker]:
    """The process's worker, built (and started) on first use; None until a builder is given."""
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None and build is not None:
            _WORKER = build()
            _WORKER.start()
        return _WORKER


def headlines_from_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compact headline rows (title, source, age) for a prompt."""
    return [{"title": r.get("title"), "source": r.get("source"), "age": r.get("age") or r.get("time")}
            for r in rows if isinstance(r, dict) and r.get("title")]


__all__ = ["LearningWorker", "get_learning_worker", "headlines_from_rows"]
