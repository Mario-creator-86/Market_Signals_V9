import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from .base_agent import BaseAgent
from core.agent_metrics import track_agent_run
from core.http_utils import http_get
from core.request_context import get_request_context, request_context
from core.config import DATA_DIR, is_crypto_ticker, normalize_market_ticker

try:
    import yfinance as yf
except ImportError:
    yf = None

logger = logging.getLogger("MarketEngine")


def _quote_number(payload: Dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = (payload or {}).get(key)
        if isinstance(value, dict):
            value = value.get("raw")
        try:
            number = float(value or 0.0)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 0.0


# Binance lists pairs whose base asset is not ASCII (e.g. Chinese-named meme
# tokens).  The app's canonical ticker (core.config.normalize_market_ticker)
# strips those characters, so every such pair collapses to the quote-only
# symbol "USDT": it cannot be charted, added to the Watchlist or traded, and
# two different pairs become indistinguishable.  Keep only addressable pairs.
_ADDRESSABLE_BINANCE_SYMBOL = re.compile(r"^[A-Z0-9]+USDT$")

# Yahoo "trending" mixes futures, FX, indices and crypto with equities.  The
# Stocks list must only contain instruments the rest of the app treats as
# stocks/ETFs.
_NON_EQUITY_QUOTE_TYPES = {"FUTURE", "INDEX", "CURRENCY", "CRYPTOCURRENCY", "OPTION", "MUTUALFUND"}

# A Binance "<TICKER>B" pair is tagged as a tokenised stock only when its
# price is this close to the same-named US stock seen in the same scan.
_TOKENIZED_STOCK_PRICE_BAND = (0.70, 1.30)
_TOKENIZED_STOCK_LOOKUPS_PER_SCAN = 5


def _is_equity_symbol(symbol: str) -> bool:
    """True for plain US equity/ETF tickers (no futures, FX, index, crypto)."""
    token = str(symbol or "").upper().strip()
    if not token:
        return False
    return not any(ch in token for ch in ("=", "^", ".", "-", "/", ":"))


def _epoch_iso(value: Any) -> Optional[str]:
    """Yahoo quote time (epoch seconds, plain or {raw: N}) as UTC ISO text."""
    if isinstance(value, dict):
        value = value.get("raw")
    try:
        seconds = float(value or 0.0)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _session_fields(quote: Dict[str, Any], session: str, regular_price: float) -> Dict[str, Any]:
    """Say what a stock row's ``change_pct`` measures.

    ``change_pct`` is the extended-hours move against the regular close when a
    pre/post-market price exists, otherwise the regular-session day move.  On
    a weekend the first one is Friday's after-hours drift, which is not the
    "top mover" number a reader expects, so the row also carries the regular
    session's own change and the time of the quote that produced the price.
    """
    raw_regular_change = (quote or {}).get("regularMarketChangePercent")
    if isinstance(raw_regular_change, dict):
        raw_regular_change = raw_regular_change.get("raw")
    try:
        regular_change: Optional[float] = round(float(raw_regular_change), 2)
    except (TypeError, ValueError):
        regular_change = None
    time_key = {"post": "postMarketTime", "pre": "preMarketTime"}.get(session, "regularMarketTime")
    quote_time = _epoch_iso((quote or {}).get(time_key))
    market_state = str((quote or {}).get("marketState") or "").upper().strip()
    return {
        "session": session,
        "regular_change_pct": regular_change,
        "regular_price": round(regular_price, 4) if regular_price > 0 else None,
        "quote_time_utc": quote_time,
        # ``premarket_price`` is the newest print of whichever session Yahoo
        # had (kept under that name for existing consumers). These two say
        # which session that is and when the print was made, so a regular
        # close or an after-hours print is never read as a pre-market price.
        "price_session": session,
        "price_time_utc": quote_time,
        "market_state": market_state or None,
    }


_PRE_MARKET_STATES = frozenset({"PRE", "PREPRE"})
_POST_MARKET_STATES = frozenset({"POST", "POSTPOST", "CLOSED"})
_SESSION_TIME_KEYS = {"pre": "preMarketTime", "post": "postMarketTime", "regular": "regularMarketTime"}


def pick_session_price(
    quote: Dict[str, Any],
    pre_price: float,
    post_price: float,
    regular_price: float,
) -> tuple:
    """Newest of Yahoo's three prices as ``(session, price)``.

    A quote can carry all three at once: during the pre-market it still has
    yesterday's ``postMarketPrice``, and after the close it still has this
    morning's ``preMarketPrice``. Taking "post if present, else pre" therefore
    showed yesterday's after-hours print during the pre-market. Yahoo's
    ``marketState`` decides; without it the print with the newest market time
    wins, and without times the old order (post, pre, regular) is kept.
    """
    prices = {"pre": float(pre_price or 0.0), "post": float(post_price or 0.0), "regular": float(regular_price or 0.0)}
    state = str((quote or {}).get("marketState") or "").upper().strip()
    if state in _PRE_MARKET_STATES:
        order = ("pre", "post", "regular")
    elif state == "REGULAR":
        order = ("regular", "post", "pre")
    elif state in _POST_MARKET_STATES:
        # This morning's pre-market print is older than the regular close.
        order = ("post", "regular", "pre")
    else:
        order = ("post", "pre", "regular")
        timed = []
        for rank, session in enumerate(order):
            if prices[session] <= 0:
                continue
            raw_time = (quote or {}).get(_SESSION_TIME_KEYS[session])
            if isinstance(raw_time, dict):
                raw_time = raw_time.get("raw")
            try:
                stamp = float(raw_time or 0.0)
            except (TypeError, ValueError):
                stamp = 0.0
            if stamp > 0:
                timed.append((-stamp, rank, session))
        if timed:
            newest = min(timed)[2]
            order = (newest,) + tuple(session for session in order if session != newest)
    for session in order:
        if prices[session] > 0:
            return session, prices[session]
    return "regular", prices["regular"]


# Dollar-pegged tokens quoted against USDT. Their price is pinned near 1.00,
# so they are never "movers", and a row for one must say what it is.
_STABLECOIN_BASES = frozenset({
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USD1", "PYUSD", "USDE",
    "USDD", "USDS", "FRAX", "GUSD", "LUSD", "SUSD", "RLUSD", "BFUSD", "XUSD",
    "USDX", "USD0", "CRVUSD", "GHO", "USDJ", "AUSD", "EURI", "AEUR",
})
_STABLECOIN_PRICE_BAND = (0.95, 1.05)
_STABLECOIN_NAME_PRICE_BAND = (0.98, 1.02)
_EURO_STABLECOIN_BASES = frozenset({"EURI", "AEUR"})


def is_stablecoin_pair(symbol: Any, price: Any = None) -> bool:
    """True for a stablecoin-vs-stablecoin pair such as ``USDCUSDT``.

    A listed dollar stablecoin counts while it trades inside its peg band (a
    de-pegged one is a real mover). Any other base whose name contains "USD"
    counts only inside a tighter band. Euro stablecoins are matched by name.
    """
    token = str(symbol or "").upper().strip().replace("-", "")
    base = ""
    for quote in ("USDT", "USDC", "FDUSD", "USD"):
        if token.endswith(quote) and len(token) > len(quote):
            base = token[: -len(quote)]
            break
    if not base:
        return False
    if base in _EURO_STABLECOIN_BASES:
        return True
    try:
        last = float(price or 0.0)
    except (TypeError, ValueError):
        last = 0.0
    if last <= 0:
        return base in _STABLECOIN_BASES
    if base in _STABLECOIN_BASES:
        low, high = _STABLECOIN_PRICE_BAND
        return low <= last <= high
    if "USD" in base:
        low, high = _STABLECOIN_NAME_PRICE_BAND
        return low <= last <= high
    return False


def _tag_stablecoin(row: Dict[str, Any]) -> bool:
    """Mark ``row`` as a stablecoin pair (additive). Returns True when tagged."""
    if not isinstance(row, dict) or not is_stablecoin_pair(row.get("symbol"), row.get("price")):
        return False
    row["instrument_type"] = "stablecoin"
    row["is_stablecoin"] = True
    return True


def tokenized_stock_underlying(symbol: str, price: float, stock_prices: Dict[str, float]) -> Optional[str]:
    """Return the US ticker a Binance ``<TICKER>BUSDT`` pair tracks, if verified.

    The name alone is not evidence (ARB, BNB, TRB are ordinary tokens), so the
    pair must also trade within a narrow band of the stock's own last price.
    """
    token = str(symbol or "").upper().strip()
    if not token.endswith("USDT"):
        return None
    base = token[:-4]
    if len(base) < 3 or not base.endswith("B"):
        return None
    underlying = base[:-1]
    try:
        reference = float((stock_prices or {}).get(underlying) or 0.0)
        last = float(price or 0.0)
    except (TypeError, ValueError):
        return None
    if reference <= 0 or last <= 0:
        return None
    low, high = _TOKENIZED_STOCK_PRICE_BAND
    return underlying if low <= (last / reference) <= high else None


# The API and MarketEngine can run in separate processes.  A scanner cache that
# only lives in process memory therefore makes the UI look healthy while the
# execution process sees no opportunities.  This small, atomically-written
# snapshot is the hand-off between those processes; it contains observations,
# never orders or account state.
SCANNER_SHARED_STATE_PATH = DATA_DIR / "scanner_state.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class ScannerAgent(BaseAgent):
    def __init__(self, config_manager):
        super().__init__(config_manager, "scanner")
        scan_cfg = self.config_manager.scan if isinstance(getattr(self.config_manager, "scan", None), dict) else {}
        # Keep discovery freshness aligned with the V2 forward age gate.  The
        # previous fixed five-minute cadence made a 120-second execution-data
        # limit fail closed for most of each scan interval.
        self.watchlist_update_interval = max(
            30.0,
            min(300.0, float(scan_cfg.get("scan_interval_seconds", 300) or 300)),
        )
        self.crypto_liquid_universe_size = max(
            1,
            int(scan_cfg.get("crypto_liquid_universe_size", 100) or 100),
        )
        self.crypto_hot_list_size = max(
            1,
            int(scan_cfg.get("crypto_hot_list_size", 50) or 50),
        )
        scanner_cfg = self.config_manager.agents.get("scanner", {}) if hasattr(self.config_manager, "agents") else {}
        self.startup_lazy_refresh_delay = max(
            0.0,
            float(scanner_cfg.get("startup_lazy_refresh_delay_seconds", 60.0) or 60.0),
        )
        # Defer the first sweep by the startup delay only, not a full scan interval.
        self.last_scan_time = time.time() - self.watchlist_update_interval + min(self.startup_lazy_refresh_delay, self.watchlist_update_interval)
        self.cached_hot_list: List[Dict[str, Any]] = []
        # Watchlist observations stay separate from the public hot-movers list.
        # AgentManager merges both pools only for autonomous stock admission,
        # which keeps Scanner discovery semantics intact while allowing an
        # owner-selected stock to compete for the Stocks allocation lane.
        self.cached_watchlist_stock_list: List[Dict[str, Any]] = []
        self.cached_watchlist_crypto_list: List[Dict[str, Any]] = []
        # The volatile list is useful for the Scanner UI, but it is not an
        # adequate universe for systematic trend discovery.  Retain the full
        # liquid CEX universe separately so V2 can rotate through majors and
        # liquid alts instead of only today's largest 24h moves.
        self.cached_liquid_crypto_list: List[Dict[str, Any]] = []
        self._state_lock = threading.Lock()
        self._scan_run_lock = threading.Lock()
        self._refresh_inflight = False
        self._last_refresh_attempt = 0.0
        self._refresh_retry_cooldown = 15.0

        self._last_run_dt: Optional[datetime] = None
        self._last_success_dt: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._status_state: str = "OK"
        self._shared_snapshot_mtime_ns = 0
        self._load_shared_snapshot_if_newer(force=True)

    @staticmethod
    def _parse_utc(value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Optional[Path] = None
        try:
            fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
            temp_path = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(str(temp_path), str(path))
        finally:
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def _publish_shared_snapshot(self, hot_tickers: List[Dict[str, Any]]) -> None:
        """Persist the latest completed scan for the other application process."""
        with self._state_lock:
            payload = {
                "schema_version": 1,
                "published_at": _iso(_utc_now()),
                "hot_tickers": [item for item in hot_tickers if isinstance(item, dict)],
                "liquid_crypto_tickers": [
                    item for item in self.cached_liquid_crypto_list if isinstance(item, dict)
                ],
                "watchlist_stock_tickers": [
                    item for item in self.cached_watchlist_stock_list if isinstance(item, dict)
                ],
                "watchlist_crypto_tickers": [
                    item for item in self.cached_watchlist_crypto_list if isinstance(item, dict)
                ],
                "last_run_time": _iso(self._last_run_dt),
                "last_success_time": _iso(self._last_success_dt),
                "last_error": self._last_error,
                "stocks_as_of": _iso(getattr(self, "_stock_leg_dt", None)),
                "crypto_as_of": _iso(getattr(self, "_crypto_leg_dt", None)),
            }
        try:
            self._write_json_atomic(SCANNER_SHARED_STATE_PATH, payload)
            self._shared_snapshot_mtime_ns = SCANNER_SHARED_STATE_PATH.stat().st_mtime_ns
        except OSError as exc:
            # A transient filesystem problem must not turn a completed scan into
            # an execution candidate with unknown freshness.  The local process
            # can still render its cache and the engine will fail closed on age.
            logger.warning("Scanner shared snapshot publish failed: %s", exc)

    def _load_shared_snapshot_if_newer(self, *, force: bool = False) -> bool:
        """Load an API-produced scan only when it is a newer complete snapshot."""
        try:
            stat = SCANNER_SHARED_STATE_PATH.stat()
        except OSError:
            return False
        if not force and stat.st_mtime_ns <= self._shared_snapshot_mtime_ns:
            return False

        try:
            with SCANNER_SHARED_STATE_PATH.open("r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Scanner shared snapshot read failed: %s", exc)
            return False

        if not isinstance(payload, dict) or int(payload.get("schema_version", 0) or 0) != 1:
            logger.warning("Scanner shared snapshot rejected: unsupported schema")
            return False
        items = payload.get("hot_tickers")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            logger.warning("Scanner shared snapshot rejected: invalid hot_tickers")
            return False
        liquid_crypto = payload.get("liquid_crypto_tickers", [])
        if not isinstance(liquid_crypto, list) or not all(isinstance(item, dict) for item in liquid_crypto):
            logger.warning("Scanner shared snapshot rejected: invalid liquid_crypto_tickers")
            return False
        watchlist_stocks = payload.get("watchlist_stock_tickers", [])
        if not isinstance(watchlist_stocks, list) or not all(isinstance(item, dict) for item in watchlist_stocks):
            logger.warning("Scanner shared snapshot rejected: invalid watchlist_stock_tickers")
            return False
        watchlist_crypto = payload.get("watchlist_crypto_tickers", [])
        if not isinstance(watchlist_crypto, list) or not all(isinstance(item, dict) for item in watchlist_crypto):
            logger.warning("Scanner shared snapshot rejected: invalid watchlist_crypto_tickers")
            return False

        last_run_dt = self._parse_utc(payload.get("last_run_time"))
        last_success_dt = self._parse_utc(payload.get("last_success_time"))
        published_dt = self._parse_utc(payload.get("published_at"))
        freshness_dt = last_success_dt or published_dt or last_run_dt
        if freshness_dt is None:
            logger.warning("Scanner shared snapshot rejected: missing timestamp")
            return False

        with self._state_lock:
            self.cached_hot_list = list(items)
            self.cached_liquid_crypto_list = list(liquid_crypto)
            self.cached_watchlist_stock_list = list(watchlist_stocks)
            self.cached_watchlist_crypto_list = list(watchlist_crypto)
            self._last_run_dt = last_run_dt or freshness_dt
            self._last_success_dt = last_success_dt or freshness_dt
            self.last_scan_time = freshness_dt.timestamp()
            # The publishing process already decided whether a leg failed; do
            # not relabel a degraded snapshot as healthy just because it was
            # read from disk.
            snapshot_error = payload.get("last_error")
            self._last_error = str(snapshot_error) if snapshot_error else None
            self._status_state = "OK"
            self._stock_leg_dt = self._parse_utc(payload.get("stocks_as_of")) or (
                freshness_dt
                if any(str(item.get("source") or "").lower().startswith("yahoo") for item in items)
                else None
            )
            self._crypto_leg_dt = self._parse_utc(payload.get("crypto_as_of")) or (
                freshness_dt if liquid_crypto else None
            )
            self._shared_snapshot_mtime_ns = stat.st_mtime_ns
        return True

    def _get_watchlist_symbols(self) -> List[str]:
        """Get stock symbols from the watchlist config."""
        try:
            reload_watchlist = getattr(self.config_manager, "reload_watchlist_if_changed", None)
            if callable(reload_watchlist):
                reload_watchlist()
            wl = getattr(self.config_manager, "watchlist", None)
            if callable(wl):
                wl = wl()
            if not isinstance(wl, dict):
                cfg = getattr(self.config_manager, "config", {}) or {}
                wl = cfg.get("watchlist", {}) if isinstance(cfg, dict) else {}
            stocks = wl.get("yahoo", wl.get("stocks", []))
            settings = wl.get("settings", {}) if isinstance(wl.get("settings"), dict) else {}
            exclusions = {
                str(symbol or "").upper().strip()
                for symbol in (wl.get("scanner_exclusions", []) or [])
            }
            # Filter to stock-only (not crypto)
            crypto_suffixes = ("-USD", "-USDT", "-USDC", "-BTC", "-ETH")
            symbols: List[str] = []
            seen: set[str] = set()
            for raw_symbol in stocks or []:
                symbol = str(raw_symbol or "").upper().strip()
                ticker_settings = settings.get(symbol, {}) if isinstance(settings.get(symbol), dict) else {}
                if (
                    not symbol
                    or symbol in seen
                    or symbol in exclusions
                    or any(symbol.endswith(suffix) for suffix in crypto_suffixes)
                    or ticker_settings.get("signals_enabled") is False
                ):
                    continue
                seen.add(symbol)
                symbols.append(symbol)
            return symbols
        except Exception:
            return []

    def _get_watchlist_crypto_symbols(self) -> List[str]:
        """Return enabled owner-selected crypto as Binance USDT symbols."""
        try:
            reload_watchlist = getattr(self.config_manager, "reload_watchlist_if_changed", None)
            if callable(reload_watchlist):
                reload_watchlist()
            wl = getattr(self.config_manager, "watchlist", None)
            if callable(wl):
                wl = wl()
            if not isinstance(wl, dict):
                cfg = getattr(self.config_manager, "config", {}) or {}
                wl = cfg.get("watchlist", {}) if isinstance(cfg, dict) else {}
            cryptos = wl.get("binance", wl.get("crypto", []))
            settings = wl.get("settings", {}) if isinstance(wl.get("settings"), dict) else {}
            exclusions = {
                str(symbol or "").upper().strip()
                for symbol in (wl.get("scanner_exclusions", []) or [])
            }
            symbols: List[str] = []
            seen: set[str] = set()
            for raw_symbol in cryptos or []:
                canonical = normalize_market_ticker(str(raw_symbol or ""))
                if not canonical or not is_crypto_ticker(canonical):
                    continue
                base = canonical[:-4] if canonical.endswith("-USD") else ""
                exchange_symbol = f"{base}USDT" if base else ""
                ticker_settings = settings.get(canonical, {}) if isinstance(settings.get(canonical), dict) else {}
                if (
                    not exchange_symbol
                    or exchange_symbol in seen
                    or canonical in exclusions
                    or exchange_symbol in exclusions
                    or ticker_settings.get("signals_enabled") is False
                ):
                    continue
                seen.add(exchange_symbol)
                symbols.append(exchange_symbol)
            return symbols
        except Exception:
            return []

    def _capture_watchlist_crypto_observations(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Select Watchlist crypto from the already-fetched Binance market snapshot."""
        scanner_cfg = self.config_manager.agents.get("scanner", {}) if hasattr(self.config_manager, "agents") else {}
        if not bool(scanner_cfg.get("watchlist_market_scan_enabled", True)):
            self.cached_watchlist_crypto_list = []
            return []
        wanted = set(self._get_watchlist_crypto_symbols())
        observations: List[Dict[str, Any]] = []
        for item in rows if isinstance(rows, list) else []:
            symbol = str(item.get("symbol") or "").upper().strip()
            if symbol not in wanted:
                continue
            try:
                price = float(item.get("lastPrice") or item.get("price") or 0.0)
                change_pct = float(item.get("priceChangePercent") or item.get("change_pct") or 0.0)
                quote_volume = float(item.get("quoteVolume") or item.get("volume") or 0.0)
                base_volume = float(item.get("volume") or item.get("baseVolume") or 0.0)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            observation = {
                "symbol": symbol,
                "change_pct": round(change_pct, 2),
                "price": price,
                "volume": quote_volume,
                "quoteVolume": quote_volume,
                "baseVolume": base_volume,
                "source": "binance_watchlist",
                "watchlist_member": True,
                "candidate_origin": "watchlist",
            }
            _tag_stablecoin(observation)
            self._apply_known_tokenized_tag(observation)
            observations.append(observation)
        self.cached_watchlist_crypto_list = observations
        return list(observations)

    @staticmethod
    def _drop_known_missing(symbols: List[str]) -> List[str]:
        """Symbols worth asking Yahoo about (not negatively cached)."""
        try:
            from core.stock_data_client import is_known_missing
        except Exception:  # pragma: no cover - defensive
            return list(symbols or [])
        kept: List[str] = []
        for symbol in symbols or []:
            try:
                if is_known_missing(symbol):
                    continue
            except Exception:
                pass
            kept.append(symbol)
        return kept

    @staticmethod
    def _watchlist_history_frame(data: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Extract one symbol from either yfinance single- or multi-ticker output."""
        if not isinstance(data, pd.DataFrame) or data.empty:
            return pd.DataFrame()
        if not isinstance(data.columns, pd.MultiIndex):
            return data
        for level in range(data.columns.nlevels):
            values = {str(value).upper() for value in data.columns.get_level_values(level)}
            if symbol in values:
                try:
                    return data.xs(symbol, axis=1, level=level, drop_level=True)
                except (KeyError, TypeError, ValueError):
                    return pd.DataFrame()
        return pd.DataFrame()

    def _scan_watchlist_stocks(self, hot_stocks: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        """Refresh read-only market observations for enabled Watchlist stocks."""
        scanner_cfg = self.config_manager.agents.get("scanner", {}) if hasattr(self.config_manager, "agents") else {}
        if not bool(scanner_cfg.get("watchlist_market_scan_enabled", True)):
            return []
        refresh_seconds = max(
            30.0,
            float(scanner_cfg.get("watchlist_market_scan_interval_seconds", 300.0) or 300.0),
        )
        if time.time() - getattr(self, "last_watchlist_stock_scan_time", 0.0) < refresh_seconds:
            return list(self.cached_watchlist_stock_list)

        symbols = self._get_watchlist_symbols()
        if not symbols:
            self.cached_watchlist_stock_list = []
            self.last_watchlist_stock_scan_time = time.time()
            return []

        max_symbols = max(1, int(scanner_cfg.get("watchlist_market_scan_max_symbols", 100) or 100))
        symbols = symbols[:max_symbols]
        by_symbol = {
            str(item.get("symbol") or "").upper().strip(): item
            for item in (hot_stocks or [])
            if isinstance(item, dict) and str(item.get("symbol") or "").strip()
        }
        observations: List[Dict[str, Any]] = []
        missing: List[str] = []
        for symbol in symbols:
            hot_item = by_symbol.get(symbol)
            if hot_item is None:
                missing.append(symbol)
                continue
            observations.append({
                **hot_item,
                "watchlist_member": True,
                "candidate_origin": "watchlist",
            })

        # A delisted Watchlist symbol (SLP, CPRX) was requested from Yahoo on
        # every scan although the app already knows Yahoo has no such listing.
        missing = self._drop_known_missing(missing)
        if missing and yf is not None:
            try:
                history = yf.download(
                    missing,
                    period="1mo",
                    interval="1d",
                    progress=False,
                    threads=False,
                    auto_adjust=False,
                    group_by="ticker",
                )
                for symbol in missing:
                    frame = self._watchlist_history_frame(history, symbol)
                    if frame.empty or "Close" not in frame:
                        continue
                    closes = pd.to_numeric(frame["Close"], errors="coerce").dropna()
                    if len(closes) < 2:
                        continue
                    price = float(closes.iloc[-1])
                    prev_close = float(closes.iloc[-2])
                    if price <= 0 or prev_close <= 0:
                        continue
                    volume = 0.0
                    avg_volume: Optional[float] = None
                    if "Volume" in frame:
                        volumes = pd.to_numeric(frame["Volume"], errors="coerce").dropna()
                        if not volumes.empty:
                            volume = float(volumes.iloc[-1])
                            completed = volumes[volumes > 0].tail(20)
                            if not completed.empty:
                                avg_volume = float(completed.mean())
                    change_pct = ((price - prev_close) / prev_close) * 100.0
                    observations.append({
                        "symbol": symbol,
                        "change_pct": round(change_pct, 2),
                        "gap_pct": round(change_pct, 2),
                        "price": round(price, 4),
                        "prev_close": round(prev_close, 4),
                        "name": symbol,
                        "exchange": "",
                        "volume": round(volume, 4),
                        "avg_volume": round(avg_volume, 4) if avg_volume is not None else None,
                        "source": "yahoo_watchlist",
                        "watchlist_member": True,
                        "candidate_origin": "watchlist",
                    })
            except Exception as exc:
                logger.warning("Watchlist stock market scan failed: %s", exc)

        self.cached_watchlist_stock_list = observations
        self.last_watchlist_stock_scan_time = time.time()
        logger.info(
            "Refreshed %s/%s enabled Watchlist stock observations",
            len(observations),
            len(symbols),
        )
        return list(observations)

    def loop(self):
        logger.info("Scanner Agent initialized.")
        if self.startup_lazy_refresh_delay > 0 and self._stop_event.wait(
            self.startup_lazy_refresh_delay
        ):
            return
        while not self._stop_event.is_set():
            try:
                self._beat()
                cfg = self.config_manager.scan
                _ = cfg.get("scan_interval_seconds", 60)

                if time.time() - self.last_scan_time > self.watchlist_update_interval:
                    with request_context(
                        reason="AGENT_REFRESH",
                        path="/agent/scanner",
                        cache_only=False,
                    ):
                        with track_agent_run("scanner"):
                            self.run_scan()
                    self.last_scan_time = time.time()

            except Exception as e:
                logger.error(f"Scanner Agent loop error: {e}")

            time.sleep(5)


    def request_refresh(self) -> Dict[str, Any]:
        with self._state_lock:
            if self._refresh_inflight:
                return {"status": "already_refreshing"}
            self._refresh_inflight = True
            self._last_refresh_attempt = time.time()
            threading.Thread(target=self._refresh_worker, daemon=True, name="ScannerManualRefresh").start()
            return {"status": "accepted"}

    def get_state(self):
        """Return public state for API.

        Non-blocking read path:
        - return cached snapshot immediately
        - schedule refresh in background when cache is empty/stale
        """
        # Pick up a refresh performed by the API process before deciding whether
        # this process should launch another external scan.
        self._load_shared_snapshot_if_newer()
        now_ts = time.time()
        ctx = get_request_context()
        forced_cache = bool(ctx and getattr(ctx, "cache_only", False))
        with self._state_lock:
            startup_delay_active = (
                not self.cached_hot_list
                and (now_ts - float(self.last_scan_time or 0.0)) < float(self.startup_lazy_refresh_delay or 0.0)
            )
            should_refresh = (
                not self.cached_hot_list
                or (now_ts - float(self.last_scan_time or 0.0)) > float(self.watchlist_update_interval)
            )
            can_trigger = (
                should_refresh
                and not self._refresh_inflight
                and not forced_cache
                and not startup_delay_active
                and (now_ts - float(self._last_refresh_attempt or 0.0)) >= float(self._refresh_retry_cooldown)
            )
            if can_trigger:
                self._refresh_inflight = True
                self._last_refresh_attempt = now_ts
                threading.Thread(target=self._refresh_worker, daemon=True, name="ScannerLazyRefresh").start()

            payload = list(self.cached_hot_list)
            liquid_crypto = list(self.cached_liquid_crypto_list)
            watchlist_stocks = list(self.cached_watchlist_stock_list)
            watchlist_crypto = list(self.cached_watchlist_crypto_list)
            refreshing = bool(self._refresh_inflight)

            if self._last_error:
                # Rows are still served when only one leg failed: that is a
                # degraded scan, not a dead one.
                self._status_state = "DEGRADED" if payload else "ERROR"
            else:
                self._status_state = "OK"
            legs = {
                "stocks": {
                    "as_of_utc": _iso(getattr(self, "_stock_leg_dt", None)),
                    "error": getattr(self, "_stock_leg_error", None),
                    "rows": sum(
                        1 for item in payload
                        if isinstance(item, dict) and str(item.get("source") or "").lower() != "binance"
                    ),
                },
                "crypto": {
                    "as_of_utc": _iso(getattr(self, "_crypto_leg_dt", None)),
                    "error": getattr(self, "_crypto_leg_error", None),
                    "rows": sum(
                        1 for item in payload
                        if isinstance(item, dict) and str(item.get("source") or "").lower() == "binance"
                    ),
                },
            }

            if self._last_run_dt is None:
                next_run_in: Optional[int] = 0
            else:
                elapsed = max(0.0, (_utc_now() - self._last_run_dt).total_seconds())
                next_run_in = max(0, int(self.watchlist_update_interval - elapsed))

            data_age_seconds = None
            if self._last_success_dt is not None:
                data_age_seconds = max(0, int((_utc_now() - self._last_success_dt).total_seconds()))

            status = {
                "state": self._status_state,
                "last_run_time": _iso(self._last_run_dt),
                "last_success_time": _iso(self._last_success_dt),
                "next_run_in": next_run_in,
                "last_error": self._last_error,
            }
            flags = {
                "refreshing": refreshing,
                "data_available": bool(payload),
            }

        return {
            "hot_tickers": payload,
            "liquid_crypto_tickers": liquid_crypto,
            "watchlist_stock_tickers": watchlist_stocks,
            "watchlist_crypto_tickers": watchlist_crypto,
            "refreshing": refreshing,
            "status": status,
            "flags": flags,
            "data_age_seconds": data_age_seconds,
            "as_of_utc": _iso(self._last_success_dt) or _iso(self._last_run_dt),
            "refresh_seconds": int(self.watchlist_update_interval),
            "legs": legs,
            "us_market": self._us_market_snapshot(),
        }

    @staticmethod
    def _us_market_snapshot() -> Dict[str, Any]:
        """US equity session right now, so the UI can label stock moves."""
        try:
            from core.market_hours import us_equity_market_status

            status = us_equity_market_status()
            session = getattr(status.session, "value", str(status.session))
            return {"session": str(session), "is_open": bool(status.is_open), "reason": str(status.reason)}
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("US market session unavailable: %s", exc)
            return {"session": "UNKNOWN", "is_open": None, "reason": "unavailable"}

    def _stock_reference_price(self, underlying: str) -> tuple:
        """Last regular price of a US stock/ETF, cached; ``(price|None, fetched)``."""
        cache = getattr(self, "_stock_reference_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._stock_reference_cache = cache
        now_ts = time.time()
        hit = cache.get(underlying)
        if hit is not None and now_ts < hit[1]:
            return hit[0], False
        if not self._drop_known_missing([underlying]):
            return None, False  # Yahoo already said it has no such listing
        price: Optional[float] = None
        ttl = 600.0  # transport failure: try again soon
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{underlying}?range=1d&interval=1d"
            res = http_get(url, provider="yahoo", headers={'User-Agent': 'Mozilla/5.0'}, timeout=(2, 6), max_retries=1)
            if res is not None and res.status_code in (200, 404):
                ttl = 6 * 3600.0  # a definite answer (found, or no such ticker)
                if res.status_code == 200:
                    result = ((res.json() or {}).get("chart") or {}).get("result") or []
                    meta = (result[0] or {}).get("meta") or {} if result else {}
                    if str(meta.get("instrumentType") or "").upper() in {"EQUITY", "ETF"}:
                        price = float(meta.get("regularMarketPrice") or 0.0) or None
        except Exception as exc:
            logger.debug("Reference price lookup failed for %s: %s", underlying, exc)
        cache[underlying] = (price, now_ts + ttl)
        return price, True

    def _apply_known_tokenized_tag(self, row: Dict[str, Any]) -> None:
        """Tag a freshly built row with an already verified tokenised-stock mapping.

        Rows are rebuilt on every scan; without this a list read between the
        rebuild and the tagging pass would briefly lose the tag.
        """
        known = getattr(self, "_tokenized_stock_map", None)
        if not isinstance(known, dict) or not isinstance(row, dict):
            return
        underlying = known.get(str(row.get("symbol") or "").upper().strip())
        if underlying:
            row["instrument_type"] = "tokenized_stock"
            row["underlying"] = underlying
            row["is_tokenized_stock"] = True

    def _tag_tokenized_stocks(
        self,
        crypto_rows: List[Dict[str, Any]],
        stock_rows: List[Dict[str, Any]],
    ) -> int:
        """Mark Binance pairs that are tokenised US stocks (e.g. NVDABUSDT).

        Binance lists them as ordinary spot pairs, so without this the whole
        app treats "MSTRB-USD" as a crypto asset.  The tag is additive
        (``instrument_type`` / ``underlying``); what to do with such a pair
        downstream is the consumer's decision.
        """
        known = getattr(self, "_tokenized_stock_map", None)
        if not isinstance(known, dict):
            known = {}
            self._tokenized_stock_map = known
        prices: Dict[str, float] = {}
        for item in stock_rows or []:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper().strip()
            try:
                price = float(item.get("price") or 0.0)
            except (TypeError, ValueError):
                continue
            if symbol and price > 0 and _is_equity_symbol(symbol):
                prices[symbol] = price
        # Most tokenised stocks are not on today's movers list or the
        # Watchlist, so there is no price to compare against.  Ask Yahoo for
        # the missing ones: a few per scan, remembered, never for sub-$1
        # tokens (the stock scan itself ignores stocks under $1).
        lookups = 0
        for row in crypto_rows or []:
            if not isinstance(row, dict) or lookups >= _TOKENIZED_STOCK_LOOKUPS_PER_SCAN:
                continue
            symbol = str(row.get("symbol") or "").upper().strip()
            base = symbol[:-4] if symbol.endswith("USDT") else ""
            try:
                last = float(row.get("price") or 0.0)
            except (TypeError, ValueError):
                continue
            if symbol in known or len(base) < 3 or not base.endswith("B") or last < 1.0:
                continue
            underlying = base[:-1]
            if underlying in prices:
                continue
            reference, fetched = self._stock_reference_price(underlying)
            lookups += 1 if fetched else 0
            if reference:
                prices[underlying] = reference
        tagged = 0
        for row in crypto_rows or []:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper().strip()
            underlying = tokenized_stock_underlying(symbol, row.get("price") or 0.0, prices)
            if underlying:
                known[symbol] = underlying
            else:
                # Once verified, keep the tag when the stock drops off today's
                # movers list (the pair did not stop being a tokenised stock).
                underlying = known.get(symbol)
            if underlying:
                row["instrument_type"] = "tokenized_stock"
                row["underlying"] = underlying
                row["is_tokenized_stock"] = True
                tagged += 1
        return tagged

    def _refresh_worker(self):
        try:
            with request_context(
                reason="AGENT_REFRESH_ONESHOT",
                path="/agent/scanner",
                cache_only=False,
            ):
                with track_agent_run("scanner"):
                    self.run_scan()
            with self._state_lock:
                self.last_scan_time = time.time()
        except Exception as e:
            logger.error(f"Scanner lazy refresh failed: {e}")
            with self._state_lock:
                self._last_error = str(e)
                self._status_state = "ERROR"
        finally:
            with self._state_lock:
                self._refresh_inflight = False

    def run_scan(self):
        """Execute one market scan and collapse overlapping refreshes."""
        scan_lock = getattr(self, "_scan_run_lock", None)
        if scan_lock is None:
            scan_lock = threading.Lock()
            self._scan_run_lock = scan_lock
        if not scan_lock.acquire(blocking=False):
            logger.info("Scanner refresh collapsed into active scan")
            with self._state_lock:
                return list(self.cached_hot_list)
        try:
            return self._run_scan_impl()
        finally:
            scan_lock.release()

    def _run_scan_impl(self):
        """Execute market scan logic."""
        logger.info("Running Market Scan...")
        hot_tickers: List[Dict[str, Any]] = []

        with self._state_lock:
            self._last_run_dt = _utc_now()

        # 1. Scan Stocks (Yahoo Finance)
        stocks = self._scan_stocks()
        if stocks:
            logger.info(f"Scanner found stocks: {[s['symbol'] for s in stocks]}")
            hot_tickers.extend(stocks)

        # Watchlist observations are stored in their own pool so they can be
        # considered by autonomous Stocks without diluting the Scanner UI's
        # hot-mover list.
        watchlist_stocks = self._scan_watchlist_stocks(stocks)
        with self._state_lock:
            self.cached_watchlist_stock_list = list(watchlist_stocks)

        # 2. Scan Crypto (Binance)
        crypto = self._scan_crypto()
        if crypto:
            logger.info(f"Scanner found crypto: {[c['symbol'] for c in crypto]}")
            hot_tickers.extend(crypto)

        stock_error = getattr(self, "_stock_leg_error", None)
        crypto_error = getattr(self, "_crypto_leg_error", None)
        if crypto_error:
            # _scan_crypto leaves the previous liquid universe and Watchlist
            # observations in place when Binance fails.  They carry no time of
            # their own, so consumers would read old prices under a fresh scan
            # age.  Tolerate one missed cycle, then fail closed.
            crypto_leg_dt = getattr(self, "_crypto_leg_dt", None)
            grace = max(120.0, 2.0 * float(self.watchlist_update_interval))
            if crypto_leg_dt is None or (_utc_now() - crypto_leg_dt).total_seconds() > grace:
                with self._state_lock:
                    self.cached_liquid_crypto_list = []
                    self.cached_watchlist_crypto_list = []
        try:
            with self._state_lock:
                liquid_rows = list(self.cached_liquid_crypto_list)
                # Watchlist crypto is published too (and feeds Trade Radar):
                # it must carry the same tag as the hot and liquid lists.
                watchlist_crypto_rows = list(self.cached_watchlist_crypto_list)
            self._tag_tokenized_stocks(
                list(crypto or []) + liquid_rows + watchlist_crypto_rows,
                list(stocks or []) + list(watchlist_stocks or []),
            )
        except Exception as exc:
            logger.debug("Tokenised stock tagging skipped: %s", exc)

        problems: List[str] = []
        if stock_error:
            stock_leg_dt = getattr(self, "_stock_leg_dt", None)
            problems.append(
                f"Stock movers not refreshed ({stock_error}); "
                + (f"list is from {_iso(stock_leg_dt)}" if stock_leg_dt else "no stock list available")
            )
        if crypto_error:
            problems.append(f"Binance movers unavailable ({crypto_error})")

        with self._state_lock:
            self.cached_hot_list = hot_tickers
            self.last_scan_time = time.time()
            # A scan that only re-served an old stock cache refreshed nothing:
            # do not let it reset the data age.
            if hot_tickers and not (stock_error and crypto_error):
                self._last_success_dt = _utc_now()
            self._status_state = "OK"
            self._last_error = "; ".join(problems) or None

        self._publish_shared_snapshot(hot_tickers)
        # Scanner discovery is intentionally read-only with respect to the
        # user's Watchlist. Candidates are consumed from the shared scanner
        # snapshot by AgentManager/Trade Radar; turning Scanner ON must never
        # imply consent to mutate a personal list.

        return hot_tickers

    def _scan_stocks(self) -> List[Dict]:
        """Scan for hot stocks using Yahoo Finance screener (gainers, losers, most active)."""
        hot_tickers: List[Dict[str, Any]] = []

        # Throttle stock scans to every 5 minutes
        if time.time() - getattr(self, "last_stock_scan_time", 0) < 300:
            return getattr(self, "cached_stock_list", [])

        self.last_stock_scan_time = time.time()
        seen_symbols = set()
        headers = {'User-Agent': 'Mozilla/5.0'}

        screener_configs = [
            ("day_gainers", "yahoo_gainers"),
            ("day_losers", "yahoo_losers"),
            ("most_actives", "yahoo_active"),
        ]

        for scr_id, source_name in screener_configs:
            try:
                url = f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds={scr_id}&count=25"
                res = http_get(url, provider="yahoo", headers=headers, timeout=(2, 8), max_retries=1)
                if res is None:
                    continue

                if res.status_code == 200:
                    data = res.json()
                    quotes = data.get('finance', {}).get('result', [{}])
                    if quotes:
                        quotes = quotes[0].get('quotes', [])
                    else:
                        quotes = []

                    for q in quotes:
                        sym = q.get('symbol', '')
                        if not sym or sym in seen_symbols or not _is_equity_symbol(sym):
                            continue
                        if str(q.get('quoteType') or '').upper() in _NON_EQUITY_QUOTE_TYPES:
                            continue
                        # Yahoo returns price as plain number or nested {raw: N}
                        raw_post_price = q.get('postMarketPrice', 0)
                        raw_pre_price = q.get('preMarketPrice', 0)
                        raw_reg_price = q.get('regularMarketPrice', 0)
                        raw_prev_close = q.get('regularMarketPreviousClose', 0) or q.get('previousClose', 0)

                        post_price = float(raw_post_price.get('raw', 0) if isinstance(raw_post_price, dict) else raw_post_price or 0)
                        pre_price = float(raw_pre_price.get('raw', 0) if isinstance(raw_pre_price, dict) else raw_pre_price or 0)
                        reg_price = float(raw_reg_price.get('raw', 0) if isinstance(raw_reg_price, dict) else raw_reg_price or 0)
                        prev_close = float(raw_prev_close.get('raw', 0) if isinstance(raw_prev_close, dict) else raw_prev_close or 0)

                        # Newest print of the session Yahoo says is current
                        # (see pick_session_price).
                        session, price = pick_session_price(q, pre_price, post_price, reg_price)
                        if session in ("post", "pre"):
                            ref_close = reg_price if reg_price > 0 else (prev_close if prev_close > 0 else price)
                            change_pct = ((price - ref_close) / ref_close * 100.0) if ref_close > 0 else 0.0
                        else:
                            ref_close = prev_close if prev_close > 0 else reg_price
                            raw_change = q.get('regularMarketChangePercent', 0)
                            change_pct = float(raw_change.get('raw', 0) if isinstance(raw_change, dict) else raw_change or 0)

                        name = q.get('shortName', '') or q.get('longName', '') or sym
                        volume = int(_quote_number(q, "regularMarketVolume"))
                        average_volume = int(_quote_number(
                            q,
                            "averageDailyVolume10Day",
                            "averageDailyVolume3Month",
                            "averageVolume",
                        ))
                        premarket_volume = int(_quote_number(q, "preMarketVolume"))
                        postmarket_volume = int(_quote_number(q, "postMarketVolume"))

                        if price < 1:
                            continue

                        seen_symbols.add(sym)
                        hot_tickers.append({
                            "symbol": sym,
                            "change_pct": round(change_pct, 2),
                            "gap_pct": round(change_pct, 2),
                            "price": round(price, 4),
                            "premarket_price": round(price, 4),
                            "prev_close": round(ref_close, 4) if ref_close > 0 else None,
                            "name": name,
                            "exchange": q.get('exchange', ''),
                            "volume": volume,
                            # regularMarketVolume is the completed/regular
                            # session volume and must never masquerade as
                            # extended-hours volume.
                            "premarket_volume": premarket_volume or None,
                            "postmarket_volume": postmarket_volume or None,
                            "avg_volume": average_volume or None,
                            "source": source_name,
                            **_session_fields(q, session, reg_price),
                        })
                elif res.status_code == 429:
                    logger.warning(f"Yahoo screener rate limited for {scr_id}")
                else:
                    logger.debug(f"Yahoo screener {scr_id} returned status {res.status_code}")
            except Exception as e:
                logger.debug(f"Yahoo screener {scr_id} error: {e}")

        # Extended Hours Trending discovery (captures after-hours / premarket movers like earnings breakouts)
        try:
            trend_url = "https://query1.finance.yahoo.com/v1/finance/trending/US"
            trend_res = http_get(trend_url, provider="yahoo", headers=headers, timeout=(2, 6), max_retries=1)
            if trend_res and trend_res.status_code == 200:
                trend_data = trend_res.json()
                trend_quotes = (((trend_data.get('finance', {}) or {}).get('result', [{}]))[0] or {}).get('quotes', [])
                trend_syms = [q.get('symbol', '').upper().strip() for q in trend_quotes if q.get('symbol')]
                trend_syms = [s for s in trend_syms if s and s not in seen_symbols and _is_equity_symbol(s)][:25]
                if trend_syms:
                    import yfinance as yf
                    ts = yf.Tickers(' '.join(trend_syms))
                    for sym, t in ts.tickers.items():
                        if sym in seen_symbols:
                            continue
                        info = getattr(t, 'info', {}) or {}
                        if str(info.get('quoteType') or '').upper() in _NON_EQUITY_QUOTE_TYPES:
                            continue
                        reg = float(info.get('regularMarketPrice') or 0.0)
                        post = float(info.get('postMarketPrice') or 0.0)
                        pre = float(info.get('preMarketPrice') or 0.0)
                        raw_prev = float(info.get('regularMarketPreviousClose') or info.get('previousClose') or 0.0)
                        session, price = pick_session_price(info, pre, post, reg)
                        if session in ("post", "pre"):
                            ref_close = reg if reg > 0 else (raw_prev if raw_prev > 0 else price)
                            change_pct = ((price - ref_close) / ref_close * 100.0) if ref_close > 0 else 0.0
                        else:
                            ref_close = raw_prev if raw_prev > 0 else reg
                            change_pct = float(info.get('regularMarketChangePercent') or 0.0)
                        if price < 1:
                            continue
                        seen_symbols.add(sym)
                        hot_tickers.append({
                            "symbol": sym,
                            "change_pct": round(change_pct, 2),
                            "gap_pct": round(change_pct, 2),
                            "price": round(price, 4),
                            "premarket_price": round(price, 4),
                            "prev_close": round(ref_close, 4) if ref_close > 0 else None,
                            "name": info.get('shortName') or sym,
                            "exchange": info.get('exchange', ''),
                            "volume": int(info.get('regularMarketVolume') or 0),
                            "premarket_volume": int(info.get("preMarketVolume") or 0) or None,
                            "postmarket_volume": int(info.get("postMarketVolume") or 0) or None,
                            "avg_volume": int(
                                info.get("averageDailyVolume10Day")
                                or info.get("averageDailyVolume3Month")
                                or info.get("averageVolume")
                                or 0
                            ) or None,
                            "source": "yahoo_trending",
                            **_session_fields(info, session, reg),
                        })
        except Exception as e:
            logger.debug(f"Yahoo trending fetch skipped: {e}")

        # Fallback: if Yahoo screener failed, try yfinance for watchlist stocks
        if not hot_tickers:
            try:
                watchlist_symbols = self._drop_known_missing(list(set(self._get_watchlist_symbols())))
                if watchlist_symbols:
                    logger.info(f"Yahoo screener empty, fetching prices for {len(watchlist_symbols)} watchlist stocks")
                    import yfinance as yf
                    data = yf.download(watchlist_symbols, period="1d", progress=False, auto_adjust=False, group_by="ticker")
                    for sym in watchlist_symbols:
                        try:
                            if len(watchlist_symbols) == 1:
                                sym_data = data
                            else:
                                sym_data = data[sym] if sym in data else None
                            if sym_data is not None and not sym_data.empty:
                                close = float(sym_data['Close'].iloc[-1])
                                prev_close = float(sym_data['Close'].iloc[0]) if len(sym_data) > 1 else close
                                change_pct = round(((close - prev_close) / prev_close) * 100, 2) if prev_close and prev_close > 0 else 0
                                hot_tickers.append({
                                    "symbol": sym,
                                    "change_pct": change_pct,
                                    "price": round(close, 2),
                                    "session": "regular",
                                    "price_session": "regular",
                                    "price_time_utc": None,
                                    "name": sym,
                                    "exchange": "",
                                    "source": "yahoo_watchlist",
                                })
                                seen_symbols.add(sym)
                        except Exception:
                            pass
            except Exception as e:
                logger.debug(f"Watchlist stock price fallback error: {e}")

        if hot_tickers:
            hot_tickers.sort(key=lambda x: abs(x.get('change_pct', 0) or 0), reverse=True)
            self.cached_stock_list = hot_tickers[:150]
            self._stock_leg_dt = _utc_now()
            self._stock_leg_error = None
            logger.info(f"Found {len(hot_tickers)} hot stocks via Yahoo Finance")
            return hot_tickers[:150]

        # Every source came back empty: the list below is whatever an earlier
        # scan cached.  Say so instead of presenting it as this scan's result.
        self._stock_leg_error = "Yahoo screener returned no rows"
        logger.warning("Stock scan returned no rows; serving the previous stock list")
        return getattr(self, "cached_stock_list", [])



    def _scan_crypto(self) -> List[Dict]:
        """Scan for hot crypto using Binance API - returns top most volatile pairs."""
        try:
            url = "https://api.binance.com/api/v3/ticker/24hr"
            res = http_get(url, provider="binance", timeout=(2, 6), max_retries=1)
            if res is None:
                raise requests.RequestException("Binance circuit open")
            if res.status_code != 200:
                self._crypto_leg_error = f"Binance HTTP {res.status_code}"
                logger.warning("Crypto scan failed: Binance HTTP %s", res.status_code)
                return []

            data = res.json()
            self._capture_watchlist_crypto_observations(data)
            usdt_pairs = [
                d for d in data
                if _ADDRESSABLE_BINANCE_SYMBOL.match(str(d.get('symbol') or ''))
                and float(d.get('lastPrice') or 0.0) > 0
            ]
            usdt_pairs.sort(key=lambda x: float(x['quoteVolume']), reverse=True)
            top_liquid = usdt_pairs[:self.crypto_liquid_universe_size]
            liquid_universe = []
            for item in top_liquid:
                quote_volume = float(item.get('quoteVolume') or 0.0)
                liquid_row = {
                    "symbol": item['symbol'],
                    "change_pct": round(float(item['priceChangePercent']), 2),
                    "price": float(item['lastPrice']),
                    "volume": quote_volume,
                    "quoteVolume": quote_volume,
                    "baseVolume": float(item.get('volume') or 0.0),
                    "source": "binance",
                }
                # Kept in the liquid universe (it is what the venue lists) but
                # tagged, so consumers can tell a dollar peg from a crypto asset.
                _tag_stablecoin(liquid_row)
                self._apply_known_tokenized_tag(liquid_row)
                liquid_universe.append(liquid_row)
            with self._state_lock:
                self.cached_liquid_crypto_list = liquid_universe
            # A stablecoin-vs-stablecoin pair (USDCUSDT, FDUSDUSDT...) is pinned
            # near 1.00 and is never a mover.
            movers = [
                item for item in top_liquid
                if not is_stablecoin_pair(item.get('symbol'), item.get('lastPrice'))
            ]
            movers.sort(key=lambda x: abs(float(x['priceChangePercent'])), reverse=True)

            hot = []
            for item in movers[:self.crypto_hot_list_size]:
                change = float(item['priceChangePercent'])
                quote_volume = float(item.get('quoteVolume') or 0.0)
                base_volume = float(item.get('volume') or 0.0)
                hot_row = {
                    "symbol": item['symbol'],
                    "change_pct": round(change, 2),
                    "price": float(item['lastPrice']),
                    "volume": quote_volume,
                    "quoteVolume": quote_volume,
                    "baseVolume": base_volume,
                    "source": "binance",
                }
                self._apply_known_tokenized_tag(hot_row)
                hot.append(hot_row)

            logger.info(f"Found {len(hot)} hot crypto: {[h['symbol'] for h in hot[:10]]}...")
            self._crypto_leg_dt = _utc_now()
            self._crypto_leg_error = None
            return hot

        except Exception as e:
            logger.error(f"Crypto scan error: {e}")
            self._crypto_leg_error = (str(e) or type(e).__name__)[:160]
            return []


# Singleton Instance
_scanner_agent_instance = None


def get_scanner_agent(config_manager=None):
    global _scanner_agent_instance
    if _scanner_agent_instance is None:
        from core.config import ConfigManager
        if config_manager is None:
            config_manager = ConfigManager()
        _scanner_agent_instance = ScannerAgent(config_manager)
    return _scanner_agent_instance
