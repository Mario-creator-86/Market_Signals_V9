"""Paper experiment: weekly cross-sectional momentum basket on liquid crypto pairs.

Once a week the liquid Binance USDT pairs are ranked by their return over the
last 14 completed days. The strongest fifth is bought, the weakest fifth is
shorted, in equal size, and everything is closed before the next ranking. It is
the published "momentum factor" for crypto, built market neutral: the two legs
together, not a bet on the direction of the market.

Research (scripts/research/crypto_regime_study/study10.py and study11.py): the
only one of 78 classic rules that passed the two-year bar, positive in every
parameter variant, on rising and on falling days. The evidence is modest (see
XS_MOMENTUM_BACKTEST), so it runs as a labelled paper experiment.

The rule is measured as a basket. The AI reviewer still looks at every
candidate and its opinion is recorded, but for this experiment it is advisory:
a veto on single coins would unbalance the two legs. Paper mode only.

The module only reads public Binance pages. It never trades by itself; it hands
candidates to ``AgentManager._autonomous_monitor_once``.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.autonomous_candidates import _levels, _scanner_symbol_to_ticker
from core.config import get_bucharest_time
from core.delist_watcher import FUTURES_INFO_URL, _http_fetch

logger = logging.getLogger(__name__)

SOURCE_NAME = "xs_momentum_basket"
XS_ENTRY_STRATEGY = "xs_momentum_basket"
XS_STRATEGY_PREFIX = "XS_MOMENTUM_"
XS_LONG_STRATEGY_TYPE = "XS_MOMENTUM_LONG"
XS_SHORT_STRATEGY_TYPE = "XS_MOMENTUM_SHORT"
XS_SIGNAL_MODE = "AUTONOMOUS_FACTOR"

TICKERS_URL = "https://api.binance.com/api/v3/ticker/24hr"
KLINES_URL = "https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1d&limit={limit}"
PRICES_URL = "https://api.binance.com/api/v3/ticker/price"

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
MINUTE_MS = 60_000
_RETRY_MS = 10 * MINUTE_MS
_NOT_COINS = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "EURI", "AEUR", "USD1", "XUSD", "BFUSD", "USDE",
    "PYUSD", "RLUSD", "USDS", "FRAX", "GBP", "TRY", "BRL", "WBTC", "WBETH", "BNSOL",
    # Gold tokens follow the gold price, not the crypto market.
    "PAXG", "XAUT",
}

DEFAULT_XS_MOMENTUM_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "lookback_days": 14,
    # 0 = Monday. The ranking uses the daily bars completed at 00:00 UTC of that day.
    "rebalance_weekday": 0,
    "group_fraction": 5,
    "min_universe": 15,
    "min_avg_quote_volume_7d": 10_000_000,
    "max_universe_symbols": 120,
    # Entering one or two days late cost little in the test.
    "entry_window_hours": 36,
    # The test had no stop; this one only cuts off a runaway move.
    "stop_pct": 0.25,
    "rr_ratio": 3.0,
    # A fixed ranking value. It must clear the bot's minimum edge score (90)
    # for every member, including the small steps that order the two legs.
    "signal_score": 93.0,
    "max_review_attempts": 3,
    "ai_advisory": True,
}

# Research snapshot behind the rule: a backtest, not live results.
XS_MOMENTUM_BACKTEST: Dict[str, Any] = {
    "kind": "backtest",
    "as_of": "2026-11-23",
    "verdict": "not proven; the result depends on which coins are in the universe",
    "rule": (
        "every week rank the liquid USDT pairs by their 14-day return; long the top fifth, short the "
        "bottom fifth, equal weight, held one week"
    ),
    "costs": "6.5 bps per unit of turnover; still positive at 30 bps",
    "two_years": {
        "data": "71 Binance USDT pairs with >= $10M a day, daily bars, 2024-11..2026-11",
        "best_weekday": {"total_pct": 196, "sharpe": 1.98, "t_stat": 2.8, "max_drawdown_pct": -13},
        "average_of_all_weekdays": {"total_pct": 115, "sharpe": 1.6, "t_stat": 2.2, "max_drawdown_pct": -17,
                                    "profitable_quarters": "6 of 8"},
        "average_of_lookbacks_7_to_28_days": {"sharpe": 1.2, "t_stat": 1.7},
        "second_year_sharpe": 0.8,
    },
    "market_neutral": "beta to the average coin about 0; +0.13% a day when the market rose, +0.20% when it fell",
    "stricter_retest": {
        "as_of": "2026-11-23",
        "method": "Strategy Lab engine: fixed quantities, in and out at every ranking, funding paid, coins need 90 days "
                  "of history, search period 60% / unseen period 40%",
        "same_coins_as_the_first_study": {"pairs": 64, "sharpe": 1.26, "unseen_period_sharpe": 0.79, "total_pct": 92,
                                          "alpha_t": 1.64, "max_drawdown_pct": -22},
        "wider_universe": {"pairs": 138, "sharpe": 0.50, "unseen_period_sharpe": -0.33, "total_pct": 23,
                           "alpha_t": 0.48, "max_drawdown_pct": -27},
        "reading": "The rule works on the first study's coin list and not on a wider one: the evidence is not "
                   "robust to the choice of coins. It does not pass the lab's bar on either list.",
    },
    "note": (
        "Backtest of the basket, not live results. The result holds for the two legs together: the "
        "long leg alone lost money while the market fell 77%. It depends on a few coins (without its "
        "five best coins the gain almost disappears), it is weaker in the second year and with a "
        "higher liquidity floor, and the pair list is today's most-traded set (survivorship bias). "
        "A single coin's news is not part of the rule."
    ),
}

Fetch = Callable[[str], Tuple[int, Any]]
Exclude = Callable[[str], bool]


def xs_momentum_config(exec_cfg: Any) -> Dict[str, Any]:
    override = (exec_cfg or {}).get(SOURCE_NAME) if isinstance(exec_cfg, dict) else None
    return {**DEFAULT_XS_MOMENTUM_CONFIG, **(override if isinstance(override, dict) else {})}


def is_xs_momentum_signal(signal: Any) -> bool:
    payload = signal if isinstance(signal, dict) else {}
    strategy = str(payload.get("strategy_type") or payload.get("strategy_id") or "").upper()
    return (
        str(payload.get("autonomous_source") or "").lower() == SOURCE_NAME
        or strategy.startswith(XS_STRATEGY_PREFIX)
    )


def xs_momentum_ai_is_advisory(signal: Any, exec_cfg: Any) -> bool:
    """True when an AI rejection of this candidate is recorded but not obeyed.

    Only for this basket rule and only in paper mode.
    """
    if not is_xs_momentum_signal(signal):
        return False
    if str((exec_cfg or {}).get("mode", "paper") if isinstance(exec_cfg, dict) else "paper").lower() != "paper":
        return False
    return bool(xs_momentum_config(exec_cfg).get("ai_advisory", True))


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default


def week_start_ms(now_ms: int, rebalance_weekday: int = 0) -> int:
    """00:00 UTC of the most recent rebalance weekday (0 = Monday)."""
    day = now_ms // DAY_MS
    weekday = (day + 3) % 7                               # 1970-01-01 was a Thursday
    return int((day - ((weekday - int(rebalance_weekday)) % 7)) * DAY_MS)


class XsMomentumBasket:
    def __init__(self, state_path: Optional[Path] = None, *, fetch: Optional[Fetch] = None,
                 now_ms: Optional[Callable[[], int]] = None) -> None:
        if state_path is None:
            from core.config import data_path

            state_path = data_path("research", "xs_momentum_basket.json")
        self.state_path = Path(state_path)
        self._fetch = fetch or _http_fetch
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.Lock()
        self._last_error = ""
        self._last_attempt_ms: Optional[int] = None
        self._basket: Dict[str, Any] = self._load()

    # ------------------------------------------------------------------ state
    def _load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and isinstance(data.get("members"), list) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(self._basket, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            logger.warning("XsMomentumBasket: state not saved: %s", exc)

    # ---------------------------------------------------------------- ranking
    def _universe(self, cfg: Dict[str, Any], exclude: Optional[Exclude]) -> Optional[List[str]]:
        status, data = self._fetch(TICKERS_URL)
        if status != 200 or not isinstance(data, list):
            self._last_error = f"ticker list unavailable (status {status}): {str(data)[:160]}"
            return None
        status, info = self._fetch(FUTURES_INFO_URL)
        if status != 200 or not isinstance(info, dict):
            # Without the list of perpetuals a short cannot be confirmed: no basket.
            self._last_error = f"perpetual list unavailable (status {status}): {str(info)[:160]}"
            return None
        perps = {
            str(item.get("symbol")) for item in info.get("symbols") or []
            if item.get("contractType") == "PERPETUAL" and item.get("quoteAsset") == "USDT"
            and item.get("status") == "TRADING"
        }
        rows = []
        for item in data:
            symbol = str(item.get("symbol") or "")
            if not symbol.endswith("USDT") or not (symbol.isascii() and symbol.isalnum()):
                continue
            base = symbol[:-4]
            if base in _NOT_COINS or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
                continue
            if symbol not in perps and f"1000{symbol}" not in perps:
                continue
            if exclude is not None and exclude(symbol):
                continue
            rows.append((_number(item.get("quoteVolume")), symbol))
        rows.sort(reverse=True)
        return [symbol for _volume, symbol in rows[: max(1, int(_number(cfg.get("max_universe_symbols"), 120)))]]

    def _measure(self, symbol: str, start_ms: int, lookback: int) -> Optional[Dict[str, Any]]:
        status, bars = self._fetch(KLINES_URL.format(symbol=symbol, limit=lookback + 10))
        if status != 200 or not isinstance(bars, list):
            return None
        done = []
        for bar in bars:
            # [open time, open, high, low, close, volume, close time, quote volume, ...]
            if isinstance(bar, list) and len(bar) >= 8 and int(_number(bar[6])) < start_ms:
                done.append((_number(bar[4]), _number(bar[7])))
        if len(done) < lookback + 1:
            return None
        last, first = done[-1][0], done[-1 - lookback][0]
        if last <= 0 or first <= 0:
            return None
        return {
            "symbol": symbol,
            "return_pct": round((last / first - 1) * 100, 4),
            "avg_quote_volume_7d": round(sum(volume for _close, volume in done[-7:]) / 7, 2),
        }

    def _rank(self, start_ms: int, cfg: Dict[str, Any], exclude: Optional[Exclude]) -> Optional[Dict[str, Any]]:
        symbols = self._universe(cfg, exclude)
        if symbols is None:
            return None
        lookback = max(2, int(_number(cfg.get("lookback_days"), 14)))
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="xs-momentum") as pool:
            measured = [row for row in pool.map(lambda s: self._measure(s, start_ms, lookback), symbols) if row]
        floor = _number(cfg.get("min_avg_quote_volume_7d"), 10_000_000)
        liquid = sorted((row for row in measured if row["avg_quote_volume_7d"] >= floor),
                        key=lambda row: (row["return_pct"], row["symbol"]))
        need = max(2, int(_number(cfg.get("min_universe"), 15)))
        if len(liquid) < need:
            self._last_error = f"only {len(liquid)} liquid pairs with enough history (need {need}); no basket this week"
            return None
        size = max(1, len(liquid) // max(2, int(_number(cfg.get("group_fraction"), 5))))
        members = [{**row, "side": "LONG", "rank": i + 1} for i, row in enumerate(reversed(liquid[-size:]))]
        members += [{**row, "side": "SHORT", "rank": i + 1} for i, row in enumerate(liquid[:size])]
        return {
            "week_start_ms": start_ms, "ranked_at_ms": self._now_ms(), "lookback_days": lookback,
            "universe": len(liquid), "members": members, "attempts": {},
        }

    def _ensure_basket(self, now: int, cfg: Dict[str, Any], exclude: Optional[Exclude]) -> None:
        start = week_start_ms(now, int(_number(cfg.get("rebalance_weekday"), 0)))
        if int(self._basket.get("week_start_ms") or -1) == start:
            return
        if self._last_attempt_ms is not None and now - self._last_attempt_ms < _RETRY_MS:
            return
        self._last_attempt_ms = now
        basket = self._rank(start, cfg, exclude)
        if basket is None:
            logger.warning("XsMomentumBasket: %s", self._last_error)
            return
        self._last_error = ""
        self._basket = basket
        self._save()
        logger.info(
            "XsMomentumBasket: new basket from %d liquid pairs - long %s; short %s",
            basket["universe"],
            ", ".join(m["symbol"] for m in basket["members"] if m["side"] == "LONG"),
            ", ".join(m["symbol"] for m in basket["members"] if m["side"] == "SHORT"),
        )

    # -------------------------------------------------------------- candidates
    def candidates(self, exec_cfg: Any, exclude: Optional[Exclude] = None) -> List[Dict[str, Any]]:
        cfg = xs_momentum_config(exec_cfg)
        if not bool(cfg.get("enabled", True)):
            return []
        with self._lock:
            now = self._now_ms()
            self._ensure_basket(now, cfg, exclude)
            start = week_start_ms(now, int(_number(cfg.get("rebalance_weekday"), 0)))
            if int(self._basket.get("week_start_ms") or -1) != start:
                return []
            if now - start > _number(cfg.get("entry_window_hours"), 36) * HOUR_MS:
                return []
            attempts = self._basket.get("attempts") if isinstance(self._basket.get("attempts"), dict) else {}
            limit = max(1, int(_number(cfg.get("max_review_attempts"), 3)))
            open_members = [m for m in self._basket.get("members") or []
                            if int(attempts.get(self._signal_id(m), 0)) < limit
                            and str(m.get("symbol") or "")[:-4] not in _NOT_COINS]
            if not open_members:
                return []
            status, data = self._fetch(PRICES_URL)
            if status != 200 or not isinstance(data, list):
                self._last_error = f"prices unavailable (status {status}): {str(data)[:160]}"
                return []
            prices = {str(item.get("symbol")): _number(item.get("price")) for item in data if isinstance(item, dict)}
            signals = []
            for member in open_members:
                signal = self._build(member, prices.get(member["symbol"], 0.0), cfg, now, start)
                if signal:
                    signals.append(signal)
            return signals

    def _signal_id(self, member: Dict[str, Any]) -> str:
        week = datetime.fromtimestamp(int(self._basket.get("week_start_ms") or 0) / 1000, timezone.utc).strftime("%Y%m%d")
        return f"auto_xsmom_{member['symbol']}_{member['side'].lower()}_{week}"

    def note_reviewed(self, signal_id: Any) -> None:
        """Count a review of one member, so a refused member is not retried all week."""
        with self._lock:
            attempts = self._basket.setdefault("attempts", {})
            key = str(signal_id or "")
            if key and any(self._signal_id(m) == key for m in self._basket.get("members") or []):
                attempts[key] = int(attempts.get(key, 0)) + 1
                self._save()

    def _build(self, member: Dict[str, Any], price: float, cfg: Dict[str, Any], now: int, start: int) -> Optional[Dict[str, Any]]:
        symbol, side = member["symbol"], member["side"]
        ticker = _scanner_symbol_to_ticker(symbol)
        if price <= 0 or not ticker:
            return None
        action = "BUY" if side == "LONG" else "SHORT"
        stop_pct = _number(cfg.get("stop_pct"), 0.25)
        rr_ratio = _number(cfg.get("rr_ratio"), 3.0)
        # Everything closes an hour before the next ranking.
        time_stop = int(max(60, (start + 7 * DAY_MS - now) / MINUTE_MS - 60))
        # Ranking value only: the two legs alternate so that they fill evenly.
        score = _number(cfg.get("signal_score"), 93.0) - 0.02 * (int(member["rank"]) - 1) - (0.01 if side == "SHORT" else 0.0)
        score = round(min(95.0, max(0.0, score)), 2)
        lookback = int(self._basket.get("lookback_days") or 14)
        group = "strongest" if side == "LONG" else "weakest"
        reasons = [
            f"Weekly momentum basket: {symbol} is {member['return_pct']:+.1f}% over {lookback} days, rank {member['rank']} "
            f"among the {group} of {self._basket.get('universe')} liquid pairs -> {side}.",
            f"Entry {price:.6g}; exit before the next weekly ranking (in {time_stop / 60:.0f}h); protective stop {stop_pct * 100:.0f}%.",
            "The rule is measured as a market-neutral basket (strongest fifth long, weakest fifth short); "
            "see regime_evidence for the backtest and its limits.",
            "The AI review is recorded for this experiment but does not decide the entry (paper only).",
        ]
        factor = {
            "type": "cross_sectional_momentum", "side": side, "rank": member["rank"],
            "return_pct": member["return_pct"], "lookback_days": lookback, "universe": self._basket.get("universe"),
            "week_start": datetime.fromtimestamp(start / 1000, timezone.utc).isoformat(),
            "avg_quote_volume_7d": member["avg_quote_volume_7d"],
        }
        strategy_type = XS_LONG_STRATEGY_TYPE if side == "LONG" else XS_SHORT_STRATEGY_TYPE
        return {
            "id": self._signal_id(member),
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
            # An explicitly unvalidated paper experiment, like the scanner regimes.
            "strategy_status": "RESEARCH",
            "strategy_evidence": {
                "lifecycle_status": "RESEARCH",
                "source": "autonomous_candidate_definition",
                "strategy_id": strategy_type,
            },
            "entry_strategy": XS_ENTRY_STRATEGY,
            "signal_mode": XS_SIGNAL_MODE,
            "variant": "AUTO",
            "strategy_source": "XS_MOMENTUM_BASKET_V1",
            "entry_policy": "rule_basket_ai_advisory",
            "asset_class": "CRYPTO_FUTURES",
            "instrument_type": None,
            "underlying": None,
            "signal_score": score,
            "score": score,
            "raw_signal_score": score,
            # A fixed ranking value for a factor rule, not a measured setup quality.
            "signal_score_source": "factor_rule",
            "probability_score": round(score / 100.0, 4),
            "quality_score": score,
            "validation_score_status": "unavailable",
            "signal_threshold": score,
            "gap_risk": 0,
            "average_daily_volume_usd": member["avg_quote_volume_7d"],
            "news_score": 0.0,
            "news_sentiment": "neutral",
            "news_headlines": [],
            "top_headlines": [],
            "scanner_context": {
                "symbol": symbol, "source": SOURCE_NAME, "candidate_origin": SOURCE_NAME,
                "change_pct": member["return_pct"], "price": round(price, 8),
                "volume": member["avg_quote_volume_7d"], "dollar_volume": member["avg_quote_volume_7d"],
                "factor": factor,
            },
            "decision_reasons": reasons,
            "reasons": reasons,
            **_levels(price, action, stop_pct, rr_ratio),
            "time_stop_minutes": time_stop,
            "regime_evidence": dict(XS_MOMENTUM_BACKTEST),
        }

    # ------------------------------------------------------------------ status
    def status(self, exec_cfg: Any = None) -> Dict[str, Any]:
        cfg = xs_momentum_config(exec_cfg)

        def iso(ms: Optional[int]) -> Optional[str]:
            return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms else None

        with self._lock:
            now = self._now_ms()
            start = week_start_ms(now, int(_number(cfg.get("rebalance_weekday"), 0)))
            current = int(self._basket.get("week_start_ms") or -1) == start
            members = (self._basket.get("members") or []) if current else []
            window_open = current and now - start <= _number(cfg.get("entry_window_hours"), 36) * HOUR_MS
            return {
                "last_error": self._last_error,
                "phase": "no_basket" if not current else ("entry_window" if window_open else "holding"),
                "week_start": iso(start),
                "next_ranking_at": iso(start + 7 * DAY_MS),
                "ranked_at": iso(int(self._basket.get("ranked_at_ms") or 0)) if current else None,
                "universe": self._basket.get("universe") if current else None,
                "long": [m["symbol"] for m in members if m["side"] == "LONG"],
                "short": [m["symbol"] for m in members if m["side"] == "SHORT"],
            }


_BASKET: Optional[XsMomentumBasket] = None
_BASKET_LOCK = threading.Lock()


def get_xs_momentum_basket() -> XsMomentumBasket:
    global _BASKET
    with _BASKET_LOCK:
        if _BASKET is None:
            _BASKET = XsMomentumBasket()
        return _BASKET


__all__ = [
    "DEFAULT_XS_MOMENTUM_CONFIG", "SOURCE_NAME", "XS_ENTRY_STRATEGY", "XS_LONG_STRATEGY_TYPE",
    "XS_MOMENTUM_BACKTEST", "XS_SHORT_STRATEGY_TYPE", "XS_SIGNAL_MODE", "XsMomentumBasket",
    "get_xs_momentum_basket", "is_xs_momentum_signal", "week_start_ms", "xs_momentum_ai_is_advisory",
    "xs_momentum_config",
]
