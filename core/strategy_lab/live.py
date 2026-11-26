"""Strategy Lab, live side: the rules the lab is trading become paper candidates.

The lab (a separate process, once a day) writes for every trading rule what to
hold long, what to hold short, from when, until when. This module reads that
and offers the members as candidates to ``AgentManager._autonomous_monitor_once``
during the entry window. Every candidate still passes the normal pipeline
(risk, Capital gates, price integrity). As for the weekly momentum basket, the
AI's verdict on a single name is recorded but advisory, and the members keep
equal size: the rule is measured as a basket. Paper mode only.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.autonomous_candidates import _levels, _scanner_symbol_to_ticker
from core.config import get_bucharest_time
from core.delist_watcher import _http_fetch

logger = logging.getLogger(__name__)

SOURCE_NAME = "strategy_lab"
LAB_PREFIX = "LAB_"
LAB_SIGNAL_MODE = "AUTONOMOUS_FACTOR"
PRICES_URL = "https://api.binance.com/api/v3/ticker/price"
MINUTE_MS = 60_000
_QUOTE_TTL_MS = 5 * MINUTE_MS

DEFAULT_LAB_LIVE_CONFIG: Dict[str, Any] = {
    "enabled": True,
    # The backtests have no stop; this one only cuts off a runaway move.
    "stop_pct": 0.25,
    "rr_ratio": 3.0,
    # Must clear the bot's minimum edge score (at most 90) for every member.
    "signal_score": 93.0,
    "max_review_attempts": 3,
    "ai_advisory": True,
}

Fetch = Callable[[str], Tuple[int, Any]]
Quotes = Callable[[List[str]], Dict[str, float]]
Exclude = Callable[[str], bool]


def _us_market_open() -> bool:
    try:
        from core.market_hours import is_us_equity_market_open

        return bool(is_us_equity_market_open())
    except Exception:  # noqa: BLE001 - when unsure, do not quote or offer stocks
        return False


def lab_live_config(exec_cfg: Any) -> Dict[str, Any]:
    override = (exec_cfg or {}).get(SOURCE_NAME) if isinstance(exec_cfg, dict) else None
    return {**DEFAULT_LAB_LIVE_CONFIG, **(override if isinstance(override, dict) else {})}


def is_lab_signal(signal: Any) -> bool:
    payload = signal if isinstance(signal, dict) else {}
    strategy = str(payload.get("strategy_type") or payload.get("strategy_id") or "").upper()
    return str(payload.get("autonomous_source") or "").lower() == SOURCE_NAME or strategy.startswith(LAB_PREFIX)


def lab_ai_is_advisory(signal: Any, exec_cfg: Any) -> bool:
    """True when an AI rejection of this lab candidate is recorded but not obeyed. Paper mode only."""
    if not is_lab_signal(signal):
        return False
    if str((exec_cfg or {}).get("mode", "paper") if isinstance(exec_cfg, dict) else "paper").lower() != "paper":
        return False
    return bool(lab_live_config(exec_cfg).get("ai_advisory", True))


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default


def _yahoo_quotes(symbols: List[str]) -> Dict[str, float]:
    """Last traded price per stock symbol. Missing symbols are simply absent."""
    if os.environ.get("MS_ENV") == "test":
        return {}
    import yfinance as yf

    out: Dict[str, float] = {}
    for symbol in symbols:
        try:
            price = _number(yf.Ticker(symbol).fast_info["last_price"])
        except Exception:  # noqa: BLE001 - one symbol without a quote must not stop the others
            continue
        if price > 0:
            out[symbol] = price
    return out


class LabBaskets:
    def __init__(self, registry_path: Optional[Path] = None, state_path: Optional[Path] = None, *,
                 fetch: Optional[Fetch] = None, stock_quotes: Optional[Quotes] = None,
                 now_ms: Optional[Callable[[], int]] = None, market_open: Optional[Callable[[], bool]] = None) -> None:
        if registry_path is None or state_path is None:
            from core.strategy_lab.data import lab_dir

            registry_path = registry_path or lab_dir() / "registry.json"
            state_path = state_path or lab_dir() / "live_state.json"
        self.registry_path, self.state_path = Path(registry_path), Path(state_path)
        self._fetch = fetch or _http_fetch
        self._stock_quotes = stock_quotes or _yahoo_quotes
        self._market_open = market_open or _us_market_open
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.Lock()
        self._registry: Dict[str, Any] = {}
        self._registry_mtime = -1.0
        self._quotes: Dict[str, Tuple[int, float]] = {}
        self._last_error = ""
        self._attempts: Dict[str, int] = self._load_attempts()
        self._blocked: Dict[str, str] = {}

    # ------------------------------------------------------------------ state
    def _load_attempts(self) -> Dict[str, int]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            attempts = data.get("attempts") if isinstance(data, dict) else None
            return {str(k): int(v) for k, v in attempts.items()} if isinstance(attempts, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_attempts(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps({"attempts": self._attempts}, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            logger.warning("LabBaskets: state not saved: %s", exc)

    def _strategies(self) -> List[Dict[str, Any]]:
        try:
            mtime = self.registry_path.stat().st_mtime
        except OSError:
            self._registry, self._registry_mtime = {}, -1.0
            return []
        if mtime != self._registry_mtime:
            try:
                data = json.loads(self.registry_path.read_text(encoding="utf-8"))
                self._registry = data if isinstance(data, dict) else {}
                self._registry_mtime = mtime
            except (OSError, ValueError):
                return []                                       # being rewritten: keep the previous copy for now
        strategies = self._registry.get("strategies") if isinstance(self._registry.get("strategies"), dict) else {}
        return [s for s in strategies.values()
                if isinstance(s, dict) and s.get("status") in ("paper", "explore") and isinstance(s.get("live"), dict)]

    # ----------------------------------------------------------------- prices
    def _crypto_prices(self) -> Dict[str, float]:
        status, data = self._fetch(PRICES_URL)
        if status != 200 or not isinstance(data, list):
            self._last_error = f"crypto prices unavailable (status {status}): {str(data)[:120]}"
            return {}
        return {str(item.get("symbol")): _number(item.get("price")) for item in data if isinstance(item, dict)}

    def _stock_prices(self, symbols: List[str], now: int) -> Dict[str, float]:
        missing = [s for s in symbols if s not in self._quotes or now - self._quotes[s][0] > _QUOTE_TTL_MS]
        if missing:
            try:
                fresh = self._stock_quotes(missing)
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"stock quotes unavailable: {exc.__class__.__name__}"
                fresh = {}
            for symbol in missing:
                self._quotes[symbol] = (now, _number(fresh.get(symbol)))
        return {s: self._quotes[s][1] for s in symbols if s in self._quotes and self._quotes[s][1] > 0}

    # -------------------------------------------------------------- candidates
    def _signal_id(self, strategy: Dict[str, Any], symbol: str, side: str) -> str:
        return f"auto_lab_{strategy['id']}_{symbol}_{side.lower()}_{strategy['live'].get('as_of_day')}"

    def candidates(self, exec_cfg: Any, exclude: Optional[Exclude] = None) -> List[Dict[str, Any]]:
        cfg = lab_live_config(exec_cfg)
        if not bool(cfg.get("enabled", True)):
            return []
        with self._lock:
            now = self._now_ms()
            limit = max(1, int(_number(cfg.get("max_review_attempts"), 3)))
            wanted: List[Tuple[Dict[str, Any], str, str, int]] = []
            stocks_open: Optional[bool] = None
            for strategy in self._strategies():
                live = strategy["live"]
                if not int(live.get("enter_from_ms") or 0) <= now <= int(live.get("enter_until_ms") or 0):
                    continue
                if strategy.get("market") == "stocks":
                    # Stocks can only be entered in the regular session: no quotes, no candidates outside it.
                    if stocks_open is None:
                        stocks_open = bool(self._market_open())
                    if not stocks_open:
                        continue
                for side, key in (("LONG", "long"), ("SHORT", "short")):
                    for rank, symbol in enumerate(live.get(key) or [], start=1):
                        symbol = str(symbol)
                        if exclude is not None and strategy.get("market") == "crypto" and exclude(symbol):
                            continue
                        if self._attempts.get(self._signal_id(strategy, symbol, side), 0) >= limit:
                            continue
                        wanted.append((strategy, symbol, side, rank))
            if not wanted:
                return []
            crypto = self._crypto_prices() if any(s.get("market") == "crypto" for s, *_ in wanted) else {}
            stock_symbols = sorted({symbol for s, symbol, *_ in wanted if s.get("market") == "stocks"})
            stocks = self._stock_prices(stock_symbols, now) if stock_symbols else {}
            signals = []
            for strategy, symbol, side, rank in wanted:
                price = (crypto if strategy.get("market") == "crypto" else stocks).get(symbol, 0.0)
                signal = self._build(strategy, symbol, side, rank, price, cfg, now)
                if signal:
                    signals.append(signal)
            return signals

    def note_reviewed(self, signal_id: Any) -> None:
        """Count a review of one member, so a refused member is not retried for the whole holding period."""
        key = str(signal_id or "")
        if not key.startswith("auto_lab_"):
            return
        with self._lock:
            self._attempts[key] = int(self._attempts.get(key, 0)) + 1
            if len(self._attempts) > 2000:                      # old rankings: keep the most recent entries
                self._attempts = dict(list(self._attempts.items())[-1000:])
            self._save_attempts()

    def note_blocked(self, signal_id: Any, reason: str) -> None:
        """Remember that a member could not be entered (another strategy holds the instrument)."""
        key = str(signal_id or "")
        if key.startswith("auto_lab_"):
            with self._lock:
                self._blocked[key] = str(reason)[:120]
                if len(self._blocked) > 500:
                    self._blocked = dict(list(self._blocked.items())[-250:])

    def _build(self, strategy: Dict[str, Any], symbol: str, side: str, rank: int, price: float,
               cfg: Dict[str, Any], now: int) -> Optional[Dict[str, Any]]:
        market = str(strategy.get("market") or "")
        ticker = _scanner_symbol_to_ticker(symbol) if market == "crypto" else symbol.upper()
        live = strategy["live"]
        minutes_left = (int(live.get("exit_at_ms") or 0) - now) / MINUTE_MS
        if price <= 0 or not ticker or minutes_left < 30:
            return None
        adv = _number((live.get("adv_usd") or {}).get(symbol) if isinstance(live.get("adv_usd"), dict) else 0.0)
        action = "BUY" if side == "LONG" else "SHORT"
        stop_pct, rr_ratio = _number(cfg.get("stop_pct"), 0.25), _number(cfg.get("rr_ratio"), 3.0)
        # Ranking value only: the two legs alternate so that they fill evenly.
        score = _number(cfg.get("signal_score"), 93.0) - 0.02 * (rank - 1) - (0.01 if side == "SHORT" else 0.0)
        score = round(min(95.0, max(0.0, score)), 2)
        types = strategy.get("strategy_types") or {}
        strategy_type = str(types.get(side) or f"{LAB_PREFIX}{str(strategy['id']).upper()}_{side}")
        status = str(strategy.get("status"))
        stats = (strategy.get("stats") or {})
        review = strategy.get("review") or {}
        if status == "paper":
            label = "passed the lab's statistical bar and was approved by the research lead"
        elif bool(stats.get("passes")):
            label = ("passed the lab's statistical bar, but the research lead did not approve it as a strategy; "
                     "it runs as a small exploration experiment")
        else:
            label = "did NOT pass the statistical bar; it runs as a small exploration experiment the research lead asked for"
        reasons = [
            f"Strategy Lab rule {strategy['id']} ({status}): {strategy.get('description')}.",
            f"{symbol} is member {rank} of the {'long' if side == 'LONG' else 'short'} group ranked on {live.get('as_of_date')} -> {side}.",
            f"Entry {price:.6g}; closed before the next ranking (in {minutes_left / 60:.0f}h); protective stop {stop_pct * 100:.0f}%.",
            f"The rule {label}. It is measured as a basket; the AI review of a single name is recorded but does not decide the entry (paper only).",
        ]
        evidence = {
            "kind": "backtest", "source": "strategy_lab", "rule": strategy.get("description"), "status": status,
            "passes_the_bar": bool(stats.get("passes")),
            "train": stats.get("train"), "holdout": stats.get("holdout"), "whole_history": stats.get("full"),
            "research_lead": {"verdict": review.get("verdict"), "confidence": review.get("confidence"),
                              "reason": review.get("reason"), "model": review.get("model")},
            "paper_results_so_far": strategy.get("paper_results"),
            "note": "Backtest of the whole basket on daily bars, not live results. One member alone is not the tested exposure.",
        }
        return {
            "id": self._signal_id(strategy, symbol, side),
            "ticker": ticker,
            "action": action,
            "type": "LONG" if side == "LONG" else "SHORT",
            "status": "Open",
            "created_at": get_bucharest_time().isoformat(),
            "source": "autonomous_monitor",
            "autonomous_source": SOURCE_NAME,
            "autonomous_candidate_origin": SOURCE_NAME,
            "strategy_type": strategy_type,
            "strategy_id": strategy_type,
            "strategy_status": "RESEARCH",
            "strategy_evidence": {"lifecycle_status": "RESEARCH", "source": "strategy_lab", "strategy_id": strategy_type},
            "entry_strategy": f"lab_{strategy['id']}",
            "signal_mode": LAB_SIGNAL_MODE,
            "variant": "AUTO",
            "strategy_source": "STRATEGY_LAB_V1",
            "entry_policy": "rule_basket_ai_advisory",
            "asset_class": "CRYPTO_FUTURES" if market == "crypto" else "US_STOCKS",
            "instrument_type": None,
            "underlying": None,
            "signal_score": score,
            "score": score,
            "raw_signal_score": score,
            "signal_score_source": "factor_rule",
            "probability_score": round(score / 100.0, 4),
            "quality_score": score,
            "validation_score_status": "unavailable",
            "signal_threshold": score,
            "gap_risk": 0,
            # The Risk Constitution needs it to prove liquidity headroom; without it the candidate is refused.
            "average_daily_volume_usd": round(adv, 2) if adv > 0 else None,
            "news_score": 0.0,
            "news_sentiment": "neutral",
            "news_headlines": [],
            "top_headlines": [],
            "scanner_context": {
                "symbol": symbol, "source": SOURCE_NAME, "candidate_origin": SOURCE_NAME, "price": round(price, 8),
                "volume": round(adv, 2) if adv > 0 else None, "dollar_volume": round(adv, 2) if adv > 0 else None,
                "lab": {"strategy_id": strategy["id"], "status": status, "side": side, "rank": rank,
                        "ranked_on": live.get("as_of_date"), "exit_at": live.get("exit_at")},
            },
            "decision_reasons": reasons,
            "reasons": reasons,
            **_levels(price, action, stop_pct, rr_ratio),
            "time_stop_minutes": int(max(30, minutes_left)),
            "regime_evidence": evidence,
        }

    # ------------------------------------------------------------------ status
    def status(self) -> Dict[str, Any]:
        with self._lock:
            now = self._now_ms()
            rows = []
            for strategy in self._strategies():
                live = strategy["live"]
                window = int(live.get("enter_from_ms") or 0) <= now <= int(live.get("enter_until_ms") or 0)
                prefix = f"auto_lab_{strategy['id']}_"
                blocked = {key[len(prefix):].rsplit("_", 2)[0]: why for key, why in self._blocked.items()
                           if key.startswith(prefix) and key.endswith(f"_{live.get('as_of_day')}")}
                rows.append({"id": strategy["id"], "market": strategy.get("market"), "status": strategy.get("status"),
                             "phase": "entry_window" if window else ("holding" if now < int(live.get("exit_at_ms") or 0) else "waiting_for_next_ranking"),
                             "long": live.get("long"), "short": live.get("short"), "exit_at": live.get("exit_at"),
                             # Members this ranking could not enter: the paper basket is then not the tested one.
                             "not_entered": blocked})
            return {"last_error": self._last_error, "trading_rules": rows,
                    "registry_updated_at": self._registry.get("updated_at"),
                    "checked_at": datetime.fromtimestamp(now / 1000, timezone.utc).isoformat()}


_BASKETS: Optional[LabBaskets] = None
_BASKETS_LOCK = threading.Lock()


def get_lab_baskets() -> LabBaskets:
    global _BASKETS
    with _BASKETS_LOCK:
        if _BASKETS is None:
            _BASKETS = LabBaskets()
        return _BASKETS


__all__ = [
    "DEFAULT_LAB_LIVE_CONFIG", "LAB_PREFIX", "LabBaskets", "SOURCE_NAME", "get_lab_baskets", "is_lab_signal",
    "lab_ai_is_advisory", "lab_live_config",
]
