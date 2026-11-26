"""Paper experiment: short a token after Binance announces its spot delisting.

Binance publishes "Binance Will Delist X, Y on DATE" on a public announcement
page. The 2023-2026 study (scripts/research/crypto_event_study) found that the
named tokens keep falling for hours after the notice; a short on the token's
perpetual, entered some minutes later and closed after four hours, was the
first rule in this project's research to pass the two-year, quarter-by-quarter
bar. It is still a small sample (27 notices), so it runs as a labelled paper
experiment, and the AI reviewer keeps its veto: the candidate goes through the
normal autonomous pipeline (risk, AI review, critic, Capital gates).

The watcher only reads public pages. It never trades by itself; it hands
candidates to ``AgentManager._autonomous_monitor_once``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from core.autonomous_candidates import _levels, _scanner_symbol_to_ticker
from core.config import get_bucharest_time

logger = logging.getLogger(__name__)

DELIST_ENTRY_STRATEGY = "delist_short"
DELIST_STRATEGY_TYPE = "DELIST_NOTICE_SHORT"
SOURCE_NAME = "delist_notice"

CMS_URL = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
           "?type=1&catalogId=161&pageNo=1&pageSize=20")
FUTURES_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
SPOT_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
NOTICE_URL = "https://www.binance.com/en/support/announcement/{code}"

MINUTE_MS = 60_000
_KEEP_NOTICES_MS = 7 * 24 * 60 * MINUTE_MS
_PERP_CACHE_MS = 60 * MINUTE_MS
_NOT_TOKENS = {"USDT", "USDC", "FDUSD", "USD", "BNB", "BTC", "ETH", "USDP", "TUSD", "BUSD", "DAI", "EUR", "AEUR", "USD1"}

DEFAULT_DELIST_SHORT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "poll_seconds": 60,
    # The first minutes after a notice overshoot and bounce on the perpetual;
    # the tested entry was "the next full hour", on average half an hour later.
    "min_age_minutes": 10,
    "max_age_minutes": 90,
    "stop_pct": 0.15,
    # The rule was tested with a time exit, not a target: keep the target far away.
    "rr_ratio": 3.0,
    "time_stop_minutes": 240,
    "signal_score": 92.0,
}

# Research snapshot behind the rule: a backtest, not live results.
DELIST_SHORT_BACKTEST: Dict[str, Any] = {
    "kind": "backtest",
    "as_of": "2026-11-23",
    "verdict": "passes the bar on a small sample",
    "rule": (
        "short the USDT perpetual of a token named in a 'Binance Will Delist ...' spot notice, "
        "entered at the next full hour after the notice, closed after 4 hours, 15% stop"
    ),
    "costs": "0.13% round trip, funding paid by the short included",
    "per_token": {
        "data": "64 tokens with a live Binance USDT perpetual, notices 2023-01..2026-11, perpetual prices",
        "mean_net_pct": 7.0, "win_rate_pct": 80, "profit_factor": 6.5,
        "with_15pct_stop": {"mean_net_pct": 5.7, "worst_trade_pct": -15},
    },
    "per_notice": {
        "notices": 27, "mean_net_pct": 7.1, "win_rate_pct": 74, "t_stat": 3.3,
        "by_year": "2023: 0 of 3 profitable, 2024: 5 of 7, 2025: 6 of 7, 2026: 9 of 10",
    },
    "timing": "price is flat before the notice timestamp and falls about 7% in 2 minutes, 5% more by "
              "+10 minutes and 7% more by +60 minutes",
    "note": (
        "Backtest of the entry rule, not live results. The sample is small (27 notices, tokens of one "
        "notice fall together) and 2023 lost. Holding longer than 4 hours is not safe: some delisted "
        "tokens were squeezed far above the entry within 24-72 hours. The delisting notice itself is "
        "the catalyst; no other news is expected. Books of delisted tokens are thin in the first minutes."
    ),
}

Fetch = Callable[[str], Tuple[int, Any]]


def _http_fetch(url: str) -> Tuple[int, Any]:
    """GET a public JSON endpoint. Trouble comes back as (status, text), never as an exception."""
    if os.environ.get("MS_ENV") == "test":
        # The test suite must never depend on, or load, a public website.
        return 0, "network disabled under MS_ENV=test"
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (research script)", "Accept": "application/json",
                      "clienttype": "web", "lang": "en"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:200]
    except Exception as exc:  # noqa: BLE001 - DNS, timeout, reset, bad JSON
        return 0, f"{exc.__class__.__name__}: {exc}"


def parse_delist_title(title: Any) -> List[str]:
    """Token symbols named in a spot delisting notice; [] for every other notice."""
    match = re.match(r"^Binance Will Delist (.+?) on \d{4}-\d{2}-\d{2}", str(title or "").strip())
    if not match:
        return []
    tokens: List[str] = []
    for part in re.split(r",|\band\b|&|\s+", re.sub(r"\(.*?\)", " ", match.group(1))):
        if re.fullmatch(r"[A-Z0-9]{2,12}", part or "") and part not in _NOT_TOKENS and part not in tokens:
            tokens.append(part)
    return tokens


def delist_short_config(exec_cfg: Any) -> Dict[str, Any]:
    override = (exec_cfg or {}).get("delist_short") if isinstance(exec_cfg, dict) else None
    return {**DEFAULT_DELIST_SHORT_CONFIG, **(override if isinstance(override, dict) else {})}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default


class DelistWatcher:
    def __init__(self, state_path: Optional[Path] = None, *, fetch: Optional[Fetch] = None,
                 now_ms: Optional[Callable[[], int]] = None) -> None:
        if state_path is None:
            from core.config import data_path

            state_path = data_path("research", "delist_watcher.json")
        self.state_path = Path(state_path)
        self._fetch = fetch or _http_fetch
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.Lock()
        self._last_poll_ms: Optional[int] = None
        self._last_poll_ok_ms: Optional[int] = None
        self._last_error = ""
        self._perps: Optional[set] = None
        self._perps_at_ms = 0
        self._notices: Dict[str, Dict[str, Any]] = self._load()

    # ------------------------------------------------------------------ state
    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            notices = data.get("notices") if isinstance(data, dict) else None
            return {str(k): v for k, v in notices.items() if isinstance(v, dict)} if isinstance(notices, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps({"notices": self._notices}, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            logger.warning("DelistWatcher: state not saved: %s", exc)

    # ------------------------------------------------------------------- poll
    def _poll(self, now: int) -> None:
        self._last_poll_ms = now
        status, data = self._fetch(CMS_URL)
        catalogs = ((data.get("data") or {}).get("catalogs") or []) if status == 200 and isinstance(data, dict) else None
        if not catalogs:
            self._last_error = f"announcement list unavailable (status {status}): {str(data)[:160]}"
            logger.warning("DelistWatcher: %s", self._last_error)
            return
        self._last_error = ""
        self._last_poll_ok_ms = now
        changed = False
        for article in catalogs[0].get("articles") or []:
            notice_id = str(article.get("id") or article.get("code") or "")
            bases = parse_delist_title(article.get("title"))
            release = int(_number(article.get("releaseDate")))
            if not notice_id or not bases or release <= 0 or now - release > _KEEP_NOTICES_MS:
                continue
            if notice_id not in self._notices:
                self._notices[notice_id] = {
                    "id": notice_id, "code": str(article.get("code") or ""), "title": str(article.get("title") or ""),
                    "release_ms": release, "bases": bases, "first_seen_ms": now,
                }
                changed = True
                logger.info("DelistWatcher: new delisting notice %s (%s)", article.get("title"), ", ".join(bases))
        stale = [key for key, item in self._notices.items() if now - int(item.get("release_ms") or 0) > _KEEP_NOTICES_MS]
        for key in stale:
            del self._notices[key]
        if changed or stale:
            self._save()

    def _perpetual(self, base: str, now: int) -> Optional[str]:
        if self._perps is None or now - self._perps_at_ms > _PERP_CACHE_MS:
            status, data = self._fetch(FUTURES_INFO_URL)
            if status == 200 and isinstance(data, dict):
                self._perps = {
                    str(item.get("symbol")) for item in data.get("symbols") or []
                    if item.get("contractType") == "PERPETUAL" and item.get("quoteAsset") == "USDT"
                    and item.get("status") == "TRADING"
                }
                self._perps_at_ms = now
            elif self._perps is None:
                return None  # cannot tell whether a short is possible: no candidate
        for symbol in (f"{base}USDT", f"1000{base}USDT"):
            if symbol in (self._perps or set()):
                return symbol
        return None

    # -------------------------------------------------------------- candidates
    def candidates(self, exec_cfg: Any) -> List[Dict[str, Any]]:
        cfg = delist_short_config(exec_cfg)
        if not bool(cfg.get("enabled", True)):
            return []
        with self._lock:
            now = self._now_ms()
            poll_ms = max(15.0, _number(cfg.get("poll_seconds"), 60.0)) * 1000
            if self._last_poll_ms is None or now - self._last_poll_ms >= poll_ms:
                self._poll(now)
            min_age = _number(cfg.get("min_age_minutes"), 10.0) * MINUTE_MS
            max_age = _number(cfg.get("max_age_minutes"), 90.0) * MINUTE_MS
            signals: List[Dict[str, Any]] = []
            for notice in sorted(self._notices.values(), key=lambda item: -int(item.get("release_ms") or 0)):
                age = now - int(notice.get("release_ms") or 0)
                if not (min_age <= age <= max_age):
                    continue
                for base in notice.get("bases") or []:
                    signal = self._build(notice, str(base), age, cfg, now)
                    if signal:
                        signals.append(signal)
            return signals

    def _build(self, notice: Dict[str, Any], base: str, age_ms: int, cfg: Dict[str, Any], now: int) -> Optional[Dict[str, Any]]:
        perpetual = self._perpetual(base, now)
        if not perpetual:
            return None
        status, quote_data = self._fetch(SPOT_TICKER_URL.format(symbol=quote(f"{base}USDT")))
        if status != 200 or not isinstance(quote_data, dict):
            return None
        price = _number(quote_data.get("lastPrice"))
        ticker = _scanner_symbol_to_ticker(f"{base}USDT")
        if price <= 0 or not ticker:
            return None
        stop_pct = _number(cfg.get("stop_pct"), 0.15)
        rr_ratio = _number(cfg.get("rr_ratio"), 3.0)
        time_stop = int(_number(cfg.get("time_stop_minutes"), 240))
        score = round(min(95.0, max(0.0, _number(cfg.get("signal_score"), 92.0))), 2)
        volume = _number(quote_data.get("quoteVolume"))
        change_pct = _number(quote_data.get("priceChangePercent"))
        age_minutes = round(age_ms / MINUTE_MS, 1)
        title = str(notice.get("title") or "")
        announced = datetime.fromtimestamp(int(notice["release_ms"]) / 1000, timezone.utc).isoformat()
        reasons = [
            f"Binance published '{title}' {age_minutes:.0f} minutes ago -> SHORT {ticker} (delisting notice).",
            f"Entry {price:.6g}; stop {stop_pct * 100:.0f}% above, exit after {time_stop / 60:.0f}h at the latest.",
            "Rule backtested on 2023-2026 notices (see regime_evidence): small sample, do not hold longer than the time stop.",
            "The AI reviewer must confirm that the notice is real, that this token is the one named, and that "
            "no risk conflict exists before execution.",
        ]
        event = {
            "type": "binance_spot_delisting_notice", "title": title, "announced_at": announced,
            "age_minutes": age_minutes, "url": NOTICE_URL.format(code=notice.get("code") or ""),
            "perpetual": perpetual, "change_24h_pct": round(change_pct, 4),
        }
        return {
            "id": f"auto_delist_{base}_short_{notice.get('id')}",
            "ticker": ticker,
            "action": "SHORT",
            "type": "SHORT",
            "status": "Open",
            "created_at": get_bucharest_time().isoformat(),
            "source": "autonomous_monitor",
            "autonomous_source": SOURCE_NAME,
            "autonomous_candidate_origin": SOURCE_NAME,
            "strategy_type": DELIST_STRATEGY_TYPE,
            "strategy_id": DELIST_STRATEGY_TYPE,
            # An explicitly unvalidated paper experiment, like the scanner regimes.
            "strategy_status": "RESEARCH",
            "strategy_evidence": {
                "lifecycle_status": "RESEARCH",
                "source": "autonomous_candidate_definition",
                "strategy_id": DELIST_STRATEGY_TYPE,
            },
            "entry_strategy": DELIST_ENTRY_STRATEGY,
            "signal_mode": "AUTONOMOUS_EVENT",
            "variant": "AUTO",
            "strategy_source": "BINANCE_DELIST_NOTICE_V1",
            "entry_policy": "event_rule_ai_veto",
            "asset_class": "CRYPTO_FUTURES",
            "instrument_type": None,
            "underlying": None,
            "signal_score": score,
            "score": score,
            "raw_signal_score": score,
            # A fixed ranking value for an event rule, not a measured setup quality.
            "signal_score_source": "event_rule",
            "probability_score": round(score / 100.0, 4),
            "quality_score": score,
            "validation_score_status": "unavailable",
            "signal_threshold": score,
            "gap_risk": 0,
            "average_daily_volume_usd": round(volume, 4),
            "news_score": 0.0,
            "news_sentiment": "bearish",
            "news_headlines": [title],
            "top_headlines": [title],
            "scanner_context": {
                "symbol": f"{base}USDT", "source": SOURCE_NAME, "candidate_origin": SOURCE_NAME,
                "change_pct": round(change_pct, 4), "price": round(price, 8), "volume": round(volume, 4),
                "dollar_volume": round(volume, 4), "event": event,
            },
            "decision_reasons": reasons,
            "reasons": reasons,
            **_levels(price, "SHORT", stop_pct, rr_ratio),
            "time_stop_minutes": time_stop,
            "regime_evidence": dict(DELIST_SHORT_BACKTEST),
        }

    # ------------------------------------------------------------------ status
    def status(self) -> Dict[str, Any]:
        def iso(ms: Optional[int]) -> Optional[str]:
            return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms else None

        with self._lock:
            newest = max((int(n.get("release_ms") or 0) for n in self._notices.values()), default=0)
            return {
                "last_error": self._last_error,
                "last_poll_at": iso(self._last_poll_ms),
                "last_successful_poll_at": iso(self._last_poll_ok_ms),
                "notices_known": len(self._notices),
                "newest_notice_at": iso(newest),
            }


_WATCHER: Optional[DelistWatcher] = None
_WATCHER_LOCK = threading.Lock()


def get_delist_watcher() -> DelistWatcher:
    global _WATCHER
    with _WATCHER_LOCK:
        if _WATCHER is None:
            _WATCHER = DelistWatcher()
        return _WATCHER


__all__ = [
    "DEFAULT_DELIST_SHORT_CONFIG", "DELIST_ENTRY_STRATEGY", "DELIST_SHORT_BACKTEST", "DELIST_STRATEGY_TYPE",
    "DelistWatcher", "delist_short_config", "get_delist_watcher", "parse_delist_title",
]
