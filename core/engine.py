# engine.py - Market Signals Engine V4 - ENHANCED (refactor pe module)

import copy
import json
import logging
import math
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

import pandas as pd
import pytz
import yfinance as yf
import requests

from core.http_utils import http_get
from core.stock_data_client import StockDataClient
from core.config import (
    BASE_DIR,
    WATCHLIST_PATH,
    CONFIG_PATH,
    LOG_DIR,
    SUPPRESSED_TICKERS_PATH,
    FALLBACK_STOCKS,
    FALLBACK_CRYPTOS,
    ConfigManager,
    BUCHAREST_TZ,
    get_bucharest_time,
    format_bucharest_time,
    normalize_ticker,
)
from core.ta import TechnicalAnalysis
from core.signals import SignalManager, get_signal_manager
from core.signal_generation import calculate_vwap
from core.scoring_system import (
    calculate_atr,
    calculate_distance_in_atr,
    compute_signal_score,
    detect_market_regime,
    allowed_modes_for_regime,
    calculate_percentile_rank,
    RowContext,
    SCORE_THRESHOLDS,
    derive_timeframe,
    derive_expected_holding_period,
)
from core.backtester import Backtester
from core.quick_backtester import QuickBacktester
from core.position_sizing import compute_size_factor
from core.capital_manager import get_capital_manager, resolve_deployable_balance
from core.agent_manager import AgentManager
from core.agent_toggles import is_agent_enabled
from core.market_hours import (
    is_24x5_instrument as _market_hours_is_24x5_instrument,
    is_us_equity_market_open,
)
from core.paper_btc_strategy import generate_paper_btc_signal
from agents.news_tab_agent import (
    get_news_tab_agent,
    get_news_tab_agent_if_ready,
    kickoff_news_tab_agent_init,
)
from core.alerts import AlertManager
from core.state_persistence import load_state_payload, save_state_payload
from core.data_quality import get_health_dashboard, QualityLevel, QualifiedValue
from core.price_integrity import validate_price_consistency


# ============ CONSTANTS ============

# Minimum data requirements for analysis
MIN_DATA_FOR_ANALYSIS = 50  # Minimum candles required for reliable indicator calculation
MIN_DATA_FOR_MTF = 30  # Minimum candles required for multi-timeframe analysis
MIN_DATA_FOR_QUALITY = 20  # Minimum candles required for quality score calculation

# Cache TTL (Time To Live) in seconds
CACHE_TTL_CRYPTO = 30  # Cache duration for crypto MTF trends
CACHE_TTL_STOCK = 300  # Cache duration for stock MTF trends

# ============ SIGNAL FILTER CONSTANTS ============
# These are configurable thresholds for the signal filtering system

# Direction filter: score improvement required to replace same-direction signal
SAME_DIRECTION_SCORE_THRESHOLD = 10  # New signal must be +10 score to replace existing

# Direction filter: requirements for opposite direction (reversal) signals
REVERSAL_SCORE_THRESHOLD = 15  # New signal must be +15 score for reversal
REVERSAL_REQUIRED_MTF_ALIGNMENT = 3  # Must have 3/3 MTF alignment for reversal

# Per-ticker cooldown after signal closes (in minutes)
DEFAULT_TICKER_COOLDOWN_MINUTES = 15
# After a losing close the ticker stays locked for this long (0 disables).
DEFAULT_STOP_LOCKOUT_MINUTES = 240
# Largest step between two fast polls that still counts as the same verified price.
FAST_POLL_MAX_STEP = 0.025
# While a scan cycle is busy (heavy scan, AI entry review, news refresh) the
# exit watch re-checks open-signal stops/targets on fresh exchange prices at
# this cadence, so an exit never waits for the slow work to finish.
EXIT_WATCH_INTERVAL_SECONDS = max(1.0, float(os.getenv("MS_EXIT_WATCH_SECONDS", "5") or 5))
# Minutes after the regular close during which 1-minute bars can still be
# fresh; later than this an equity row is refreshed from daily/hourly candles.
CLOSED_SESSION_GRACE_SECONDS = 300.0
# How often a closed-session equity row is rebuilt (candles do not move).
CLOSED_SESSION_REFRESH_SECONDS = 1800.0
# A watchlist row older than this is not evidence for settling a signal.
SIGNAL_EXIT_MAX_ROW_AGE_SECONDS = 900.0
# Engine subprocess only: how often the news the API process fetched is read
# (read-only) from its backup file into this process.
NEWS_MIRROR_REFRESH_SECONDS = 180.0
NEWS_MIRROR_MIN_REFRESH_SECONDS = 30.0
NEWS_MIRROR_MAX_BYTES = 256 * 1024 * 1024
NEWS_MIRROR_MAX_ACTIVITY_ROWS = 25


def news_owned_by_api_process() -> bool:
    """True when this process is the engine child started by the API process.

    ``api/engine_service.py`` starts the child with ``MS_PROCESS_ROLE=engine``.
    The API process then owns the News tab agent: the refresh loop, the AI
    briefings and ``data/news_backup.json``. In thread mode (one process) the
    role is ``api`` and the engine keeps refreshing news itself.
    """
    return str(os.getenv("MS_PROCESS_ROLE", "") or "").strip().lower() == "engine"


def news_backup_path() -> str:
    """Path of the backup file the News tab agent persists (resolved late)."""
    from agents import news_tab_agent as _news_tab_module

    return str(_news_tab_module.NEWS_BACKUP_PATH)


def read_news_backup_readonly(path: Optional[str] = None) -> Optional[Dict]:
    """Load the news backup written by the API process. Never writes.

    The bytes are read in one call and the handle is closed before parsing, so
    the writer's atomic replace is not blocked while a 20+ MB JSON is decoded.
    Returns ``None`` when the file is missing, empty, oversized or not a JSON
    object (a half-written file cannot be observed: the writer replaces it
    atomically).
    """
    target = str(path or news_backup_path())
    try:
        size = os.path.getsize(target)
        if size <= 0 or size > NEWS_MIRROR_MAX_BYTES:
            return None
        with open(target, "rb") as handle:
            raw = handle.read()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def hydratable_news_payload(data: Dict) -> Tuple[Dict, Dict[str, Dict]]:
    """Backup payload reduced to rows the news agent can hydrate silently.

    ``NewsTabAgent.hydrate_state`` appends every row it rejects (no title, no
    date, non-English) to the quarantine file. That is right once at start-up,
    but a mirror that reloads every few minutes would append the same rows on
    every reload, so they are dropped here first. Returns the reduced payload
    and ``{article_id: row}`` in feed order (used to spot new articles).
    """
    try:
        from agents.news_tab_agent import _is_english_text as _english_check
    except Exception:  # pragma: no cover - helper renamed/removed
        _english_check = None

    seen: Dict[str, Dict] = {}

    def _keep(row) -> bool:
        if not isinstance(row, dict):
            return False
        title = row.get("title") or row.get("headline")
        if not title or not row.get("published_at"):
            return False
        if _english_check is not None:
            try:
                if not _english_check(str(title), str(row.get("summary") or "")):
                    return False
            except Exception:
                pass
        article_id = str(row.get("id") or "").strip()
        if article_id and article_id not in seen:
            seen[article_id] = row
        return True

    payload = dict(data or {})
    for bucket in ("market_news", "breaking_news"):
        rows = payload.get(bucket)
        payload[bucket] = [row for row in rows if _keep(row)] if isinstance(rows, list) else []
    ticker_news = payload.get("ticker_news")
    cleaned: Dict[str, List[Dict]] = {}
    if isinstance(ticker_news, dict):
        for ticker, rows in ticker_news.items():
            if not isinstance(rows, list):
                continue
            kept = [row for row in rows if _keep(row)]
            if kept:
                cleaned[str(ticker)] = kept
    payload["ticker_news"] = cleaned
    return payload, seen

# STRICT override stability thresholds
STRICT_OVERRIDE_SCORE_DELTA = 12.0
STRICT_OVERRIDE_MIN_MTF = 5

# Win-rate filter: minimum requirements to apply the filter
WINRATE_FILTER_MIN_TRADES = 5  # Need at least 50 trades to apply filter
WINRATE_FILTER_MIN_PCT = 25.0  # Block signals if win rate below 45%
WINRATE_FILTER_DAYS = 7  # Look back period for win rate calculation
WINRATE_FILTER_SCORE_PENALTY = 10  # Extra score required if win rate is low

# ============ US MARKET HOURS ============
# US Stock Market hours (Eastern Time)
US_MARKET_OPEN_HOUR = 9
US_MARKET_OPEN_MINUTE = 30
US_MARKET_CLOSE_HOUR = 16
US_MARKET_CLOSE_MINUTE = 0


def is_us_market_open() -> bool:
    """
    Check if US stock market is currently open.
    
    US Market Hours: 9:30 AM - 4:00 PM Eastern Time (ET), Monday to Friday
    
    Returns:
        True if market is open, False otherwise
    """
    return is_us_equity_market_open()

def is_24x5_instrument(ticker: str) -> bool:
    """True for instruments that trade nearly 24/5 (commodities/forex)."""
    return _market_hours_is_24x5_instrument(ticker)

# ============ LOGGING ============

LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"engine_{datetime.now().strftime('%Y%m%d')}.log"

# Retention: daily engine logs are never rotated by the handler (one file per
# day), so prune files older than 14 days here or logs/ grows without bound.
try:
    import time as _time
    _log_cutoff = _time.time() - 14 * 86400
    for _old in LOG_DIR.glob("engine_*.log"):
        try:
            if _old.is_file() and _old.stat().st_mtime < _log_cutoff:
                _old.unlink()
        except OSError:
            continue
except Exception:
    pass

# Force FileHandler attachment even if logging is already configured
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# Check if FileHandler already exists for this file
has_file_handler = False
for h in root_logger.handlers:
    if isinstance(h, logging.FileHandler) and str(h.baseFilename) == str(log_file.resolve()):
        has_file_handler = True
        break

if not has_file_handler:
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    # Rotating handler: caps a single day's file at 20 MB (3 backups) so a
    # log-spamming loop can't fill the disk before the 14-day prune runs.
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler(
        log_file, encoding="utf-8", maxBytes=20 * 1024 * 1024, backupCount=3
    )
    fh.setFormatter(formatter)
    root_logger.addHandler(fh)
    
    # Ensure StreamHandler exists (console output)
    has_stream_handler = any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers)
    if not has_stream_handler:
        sh = logging.StreamHandler()
        sh.setFormatter(formatter)
        root_logger.addHandler(sh)

logger = logging.getLogger("MarketEngine")


# ============ HELPER FUNCTIONS ============

def atomic_write_json(file_path: str, data: dict, **json_kwargs) -> None:
    """
    Write JSON data to file atomically using temp file + rename pattern.
    Prevents file corruption from race conditions when multiple scans overlap.
    On Windows, includes retry logic for file locking issues.
    """
    import time
    
    # Get directory of target file
    dir_name = os.path.dirname(file_path) or '.'

    # Create temp file in same directory (ensures same filesystem for atomic rename)
    fd, temp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp', text=True)
    try:
        # Write to temp file
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, **json_kwargs)

        # Atomic rename with retry for Windows file locking
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if os.path.exists(file_path):
                    os.replace(temp_path, file_path)
                else:
                    os.rename(temp_path, file_path)
                return  # Success
            except PermissionError as e:
                if attempt < max_retries - 1:
                    time.sleep(0.1 * (attempt + 1))  # Backoff: 0.1s, 0.2s, 0.3s
                    continue
                else:
                    # Last resort: direct write (not atomic but works)
                    logger.warning(f"Atomic write failed after {max_retries} retries, using direct write")
                    try:
                        with open(file_path, 'w', encoding='utf-8') as f:
                            json.dump(data, f, **json_kwargs)
                        # Clean up temp file
                        if os.path.exists(temp_path):
                            os.unlink(temp_path)
                        return
                    except Exception as direct_e:
                        logger.error(f"Direct write also failed: {direct_e}")
                        raise e
    except Exception as e:
        # Clean up temp file on error
        logger.error(f"Atomic write failed: {e}")
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception as cleanup_e:
                logger.error(f"Failed to cleanup temp file: {cleanup_e}")
        raise


# ============ CLIENTI EXTERNI (Binance, News) ============

class BinanceClient:
    BASE_URL = "https://api.binance.com/api/v3"
    _pct_cache: Dict[str, dict] = {}
    _price_cache: Dict[str, dict] = {}
    _pct_cache_lock = threading.Lock()
    _price_cache_lock = threading.Lock()
    # When Binance is unreachable the last known price may stand in briefly,
    # but an old price must never be published as the current one.
    MAX_STALE_PRICE_SECONDS = 120

    @classmethod
    def _recent_cached_price(cls, cached, now) -> Optional[float]:
        if not cached or not cached.get("ts"):
            return None
        if (now - cached["ts"]).total_seconds() > cls.MAX_STALE_PRICE_SECONDS:
            return None
        return cached.get("val")
    _bulk_price_cache: Dict[str, object] = {"ts": None, "data": {}}
    _bulk_pct_cache: Dict[str, object] = {"ts": None, "data": {}}

    @staticmethod
    def to_symbol(ticker: str) -> str:
        t = ticker.upper().replace("-", "")
        if t.endswith("USD") and not t.endswith("USDT"):
            return t[:-3] + "USDT"
        return t

    @classmethod
    def get_bulk_last_prices(cls, tickers: List[str], ttl: int = 5) -> Dict[str, Optional[float]]:
        symbols = {str(ticker): cls.to_symbol(str(ticker)) for ticker in tickers or []}
        now = datetime.now()
        with cls._price_cache_lock:
            bulk_ts = cls._bulk_price_cache.get("ts")
            bulk_data = cls._bulk_price_cache.get("data") if isinstance(cls._bulk_price_cache.get("data"), dict) else {}
            if bulk_ts and (now - bulk_ts).total_seconds() < ttl:
                return {ticker: bulk_data.get(symbol) for ticker, symbol in symbols.items()}

        all_prices: Dict[str, float] = {}
        try:
            resp = http_get(
                f"{cls.BASE_URL}/ticker/price",
                provider="binance",
                timeout=(2, 8),
                max_retries=1,
            )
            if resp is not None and resp.status_code == 200:
                for row in resp.json() or []:
                    try:
                        symbol = str(row.get("symbol") or "")
                        if symbol:
                            all_prices[symbol] = float(row.get("price"))
                    except Exception:
                        continue
        except Exception as e:
            logger.error(f"Binance bulk price fetch failed: {e}")

        with cls._price_cache_lock:
            if all_prices:
                cls._bulk_price_cache = {"ts": now, "data": all_prices}
                for symbol in set(symbols.values()):
                    if symbol in all_prices:
                        cls._price_cache[symbol] = {"val": all_prices[symbol], "ts": now}
            else:
                bulk_ts = cls._bulk_price_cache.get("ts")
                bulk_fresh = bool(bulk_ts) and (now - bulk_ts).total_seconds() <= cls.MAX_STALE_PRICE_SECONDS
                all_prices = (
                    cls._bulk_price_cache.get("data")
                    if bulk_fresh and isinstance(cls._bulk_price_cache.get("data"), dict)
                    else {}
                )
            out: Dict[str, Optional[float]] = {}
            for ticker, symbol in symbols.items():
                val = all_prices.get(symbol)
                if val is None:
                    val = cls._recent_cached_price(cls._price_cache.get(symbol), now)
                out[ticker] = val
            return out

    @classmethod
    def get_bulk_pct_24h(cls, tickers: List[str], ttl: int = 5) -> Dict[str, float]:
        symbols = {str(ticker): cls.to_symbol(str(ticker)) for ticker in tickers or []}
        now = datetime.now()
        with cls._pct_cache_lock:
            bulk_ts = cls._bulk_pct_cache.get("ts")
            bulk_data = cls._bulk_pct_cache.get("data") if isinstance(cls._bulk_pct_cache.get("data"), dict) else {}
            if bulk_ts and (now - bulk_ts).total_seconds() < ttl:
                return {ticker: float(bulk_data.get(symbol, 0.0) or 0.0) for ticker, symbol in symbols.items()}

        all_pct: Dict[str, float] = {}
        try:
            resp = http_get(
                f"{cls.BASE_URL}/ticker/24hr",
                provider="binance",
                timeout=(2, 8),
                max_retries=1,
            )
            if resp is not None and resp.status_code == 200:
                for row in resp.json() or []:
                    try:
                        symbol = str(row.get("symbol") or "")
                        if symbol:
                            all_pct[symbol] = float(row.get("priceChangePercent", 0.0) or 0.0)
                    except Exception:
                        continue
        except Exception as e:
            logger.error(f"Binance bulk 24h fetch failed: {e}")

        with cls._pct_cache_lock:
            if all_pct:
                cls._bulk_pct_cache = {"ts": now, "data": all_pct}
                for symbol in set(symbols.values()):
                    if symbol in all_pct:
                        cls._pct_cache[symbol] = {"val": all_pct[symbol], "ts": now}
            else:
                all_pct = cls._bulk_pct_cache.get("data") if isinstance(cls._bulk_pct_cache.get("data"), dict) else {}
            return {ticker: float(all_pct.get(symbol, 0.0) or 0.0) for ticker, symbol in symbols.items()}

    @classmethod
    def fetch_klines(
        cls, ticker: str, interval: str = "1m", limit: int = 500, ttl: int = 0
    ) -> Optional[pd.DataFrame]:
        # `ttl` is accepted for callers that pass a cache hint; klines are
        # always fetched fresh.
        symbol = cls.to_symbol(ticker)
        try:
            resp = http_get(
                f"{cls.BASE_URL}/klines",
                provider="binance",
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=(2, 6),
                max_retries=1,
            )
            if resp is None:
                return None
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data:
                return None
            rows = [
                {
                    "open_time": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                }
                for k in data
            ]
            df = pd.DataFrame(rows)
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            df = df.set_index("open_time")
            return df
        except Exception as e:
            logger.error(f"Binance error {symbol}: {e}")
            return None

    @classmethod
    def get_pct_24h(cls, ticker: str, ttl: int = 5) -> float:
        now = datetime.now()
        symbol = cls.to_symbol(ticker)

        with cls._pct_cache_lock:
            cached = cls._pct_cache.get(symbol)
            if cached and (now - cached["ts"]).total_seconds() < ttl:
                return cached["val"]

        try:
            resp = http_get(
                f"{cls.BASE_URL}/ticker/24hr",
                provider="binance",
                params={"symbol": symbol},
                timeout=(2, 6),
                max_retries=1,
            )
            if resp is None:
                raise requests.RequestException("Binance circuit open")
            val = (
                float(resp.json().get("priceChangePercent", 0))
                if resp.status_code == 200
                else 0.0
            )
        except Exception as e:
            logger.error(f"Failed to get 24h % for {symbol}: {e}")
            val = 0.0

        with cls._pct_cache_lock:
            cls._pct_cache[symbol] = {"val": val, "ts": now}
        return val

    @classmethod
    def get_last_price(cls, ticker: str, ttl: int = 5) -> Optional[float]:
        symbol = cls.to_symbol(ticker)
        now = datetime.now()

        with cls._price_cache_lock:
            cached = cls._price_cache.get(symbol)
            if cached and (now - cached["ts"]).total_seconds() < ttl:
                return cached["val"]

        try:
            resp = http_get(
                f"{cls.BASE_URL}/ticker/price",
                provider="binance",
                params={"symbol": symbol},
                timeout=(2, 6),
                max_retries=1,
            )
            if resp is None:
                return cls._recent_cached_price(cached, now)
            if resp.status_code != 200:
                with cls._price_cache_lock:
                    return cls._recent_cached_price(cached, now)
            data = resp.json()
            price_str = data.get("price")
            if price_str is None:
                with cls._price_cache_lock:
                    return cls._recent_cached_price(cached, now)
            price = float(price_str)
            with cls._price_cache_lock:
                cls._price_cache[symbol] = {"val": price, "ts": now}
            return price
        except Exception as e:
            logger.error(f"Binance last price {symbol}: {e}")
            with cls._price_cache_lock:
                return cls._recent_cached_price(cached, now)



# ============ LOGGING HELPER ============

from collections import deque
import logging

class MemoryLogHandler(logging.Handler):
    def __init__(self, buffer: deque):
        super().__init__()
        self.buffer = buffer
        self.formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    def emit(self, record):
        try:
            msg = self.format(record)
            self.buffer.append(msg)
        except Exception:
            self.handleError(record)


# ============ MARKET ENGINE ============

class MarketEngine:
    VARIANT_DESC = {
        "A": "Trend Puternic",
        "B": "Trend Moderat",
        "C": "Trend Relaxat Intraday",
    }

    def __init__(self):
        self.config = ConfigManager()
        self.signals = get_signal_manager()
        self.backtester = Backtester(self.config)

        # FIX: Create yfinance lock early so it can be shared with QuickBacktester
        self.yfinance_lock = threading.Lock()
        self.quick_backtester = QuickBacktester(self.config, self.yfinance_lock)
        self.alert_manager = AlertManager(self.config)

        # In-memory Log Buffer (maxlen 500)
        self.log_buffer = deque(maxlen=500)
        
        # Attach Memory Handler to MarketEngine logger
        mem_handler = MemoryLogHandler(self.log_buffer)
        logger.addHandler(mem_handler)
        logger.setLevel(logging.INFO)

        # Validate critical config values
        self._validate_config()

        self.last_signal_time: Dict[str, datetime] = {}
        self.last_signal_indices: Dict[str, int] = {}  # Track last signal index per ticker for cooldown
        defer_heavy_scan = max(0.0, float(self.config.scan.get("defer_initial_heavy_scan_seconds", 0) or 0))
        initial_heavy_scan_ts = get_bucharest_time() if defer_heavy_scan > 0 else None
        self.last_stock_scan: Optional[datetime] = initial_heavy_scan_ts
        
        # Track watchlist changes for activity log
        self.previous_watchlist: set = set()
        self.watchlist_events = deque(maxlen=50)  # Store recent watchlist add/remove events
        self.last_news_check: Optional[datetime] = (
            get_bucharest_time()
            if bool(self.config.news.get("defer_initial_refresh", True))
            else None
        )
        self.last_crypto_price_update: Optional[datetime] = initial_heavy_scan_ts  # heavy scan (klines)
        self.last_crypto_price_poll: Optional[datetime] = None    # fast price poll

        # Initialize news_cache with default structure
        self.news_cache: dict = {
             "ticker_news": {},
             "market_news": [],
             "breaking_news": [],
             "last_update": "",
             "active_filters": [],
             "total_articles": 0,
             "loading": True,
        }
        
        # Try to restore news cache and log buffer from file
        prev_news_state = {}
        try:
            content = load_state_payload({})
            if isinstance(content, dict):
                prev_news_state = content.get("news", {})
                # Restore buffer from saved logs if possible (optional, but nice)
                saved_logs = content.get("log", [])
                if saved_logs:
                    self.log_buffer.extend(saved_logs)
        except Exception as e:
            logger.warning(f"Could not load news cache from state: {e}")
        
        # Testing log buffer
        self.log_buffer.append(f"[{get_bucharest_time().strftime('%H:%M:%S')}] Engine initialized (Memory Buffer Active)")

        self.autostart_enabled = os.environ.get("MS_DISABLE_ENGINE_AUTOSTART", "0") != "1"

        # News agent initializes lazily in background to keep API startup fast.
        self.news_agent = None
        if self._is_agent_enabled("news_tab", True) and self.autostart_enabled:
            kickoff_news_tab_agent_init(self.config.config)
        else:
            logger.info("News flow initialization skipped.")
        
        # FIX: Remove self.news_agent.hydrate_state(prev_news_state) from state.json
        # The NewsTabAgent already manages its own persistence via news_backup.json
        
        # FIX: Ensure news is refreshed on startup (in background)
        if self.autostart_enabled:
            self.start_background_news_refresh()
        
        self.mtf_cache: Dict[str, Dict[str, str]] = {}
        self.mtf_cache_time: Dict[str, datetime] = {}
        self.mtf_frame_cache: Dict[str, Dict[str, pd.DataFrame]] = {}
        self.mtf_cache_lock = threading.Lock()
        
        # Yahoo Fallback Cache (Prevent 429/Delisted errors)
        self._yahoo_cache: Dict[str, tuple] = {} # key -> (data, timestamp)
        self._yahoo_cache_lock = threading.Lock()
        
        # Yahoo REQUEST TRACKING (for monitoring)
        from collections import deque as collections_deque
        self._yahoo_request_timestamps = collections_deque(maxlen=1000)
        
        self.snapshot_lock = threading.Lock()  # FIX: Lock for snapshot dictionary updates
        # Note: yfinance_lock is created earlier (before QuickBacktester init) and shared

        self.last_signal_outcomes: Dict[str, str] = {}
        self.new_signals: List[str] = []
        self.last_backtest_results: Optional[dict] = None
        self.paper_strategy_emitted: Dict[str, str] = {}

        # Exit watch: signal stop/target checks independent of the scan loop.
        self._signal_status_lock = threading.RLock()
        self._scan_started_monotonic: Optional[float] = None
        self._exit_watch_rows: Dict[str, Dict] = {}
        self._exit_watch_outcomes: Dict[str, str] = {}
        self._exit_watch_checks = 0
        self._exit_watch_stop = threading.Event()
        self._exit_watch_thread: Optional[threading.Thread] = None

        # Initialize Agent Manager
        self.agent_manager = AgentManager(self.config)
        self._agent_config_reload_lock = threading.RLock()
        self._agent_config_watch_stop = threading.Event()
        
        from core.stock_data_client import stock_data_client
        self.stock_data_client = stock_data_client
        
        if self.autostart_enabled:
            self.agent_manager.start_agents()
            self._start_agent_config_watcher()
            
            # Proactive Calendar Refresh (on startup)
            self._start_background_calendar_refresh()

        # Performance: In-memory state cache to avoid repeated file reads
        self.state_cache: Dict = {}
        self.state_cache_lock = threading.Lock()
        
        if self.autostart_enabled:
            # Start API Stats Logger (monitoring Yahoo request rates)
            from core.api_stats_logger import start_api_stats_logger
            start_api_stats_logger(self)

    def _prefetch_stock_data_quotes(self, tickers: List[str], chunk_size: int = 25) -> Dict[str, Dict]:
        """
        Fetch Yahoo Finance stock quotes once per scan and reuse per ticker.
        """
        symbols = [str(t or "").upper().strip() for t in (tickers or []) if str(t or "").strip()]
        if not symbols:
            return {}

        out: Dict[str, Dict] = {}
        for i in range(0, len(symbols), max(1, int(chunk_size))):
            chunk = symbols[i : i + max(1, int(chunk_size))]
            try:
                data = StockDataClient.get_batch_quote(chunk, ttl=60) or []
            except Exception as e:
                logger.debug(f"Batch quote fetch failed for chunk {i // max(1, int(chunk_size)) + 1}: {e}")
                data = []

            for row in data:
                if not isinstance(row, dict):
                    continue
                sym = str(row.get("symbol") or row.get("ticker") or "").upper().strip()
                if sym:
                    out[sym] = row
        return out

    def _validate_config(self) -> None:
        """Validate critical config values to prevent runtime errors."""
        cfg = self.config.signal or {}
        scan_cfg = self.config.scan or {}

        # Validate signal config
        # Calculate TP/SL using exit_mode (default ATR)
        exit_mode = str(cfg.get("exit_mode") or "ATR").upper()
        if exit_mode == "FIXED_PCT":
            tp = cfg.get("profit_target_pct")
            sl = cfg.get("stop_loss_pct")
            if tp is None or (isinstance(tp, (int, float)) and tp <= 0):
                cfg["profit_target_pct"] = 4.0
            if sl is None or (isinstance(sl, (int, float)) and sl <= 0):
                cfg["stop_loss_pct"] = 2.0
        else:
            if float(cfg.get("atr_period") or 0) <= 0:
                cfg["atr_period"] = 14
            if float(cfg.get("atr_multiplier_tp") or 0) <= 0:
                cfg["atr_multiplier_tp"] = 5.0
            if float(cfg.get("atr_multiplier_sl") or 0) <= 0:
                cfg["atr_multiplier_sl"] = 2.0
        if float(cfg.get("rsi_period") or 0) <= 0:
            cfg["rsi_period"] = 14
        if float(cfg.get("macd_fast") or 0) <= 0:
            cfg["macd_fast"] = 12
        if float(cfg.get("macd_slow") or 0) <= 0:
            cfg["macd_slow"] = 26
        if float(cfg.get("macd_signal") or 0) <= 0:
            cfg["macd_signal"] = 9

        # Validate scan config
        if float(scan_cfg.get("scan_interval_seconds") or 0) <= 0:
            scan_cfg["scan_interval_seconds"] = 10

        logger.info("✓ Config validation passed")

    def _merge_signal_cfg(self, base_cfg: Dict, override: Dict) -> Dict:
        """Deep merge signal config overrides without mutating the base config."""
        merged = copy.deepcopy(base_cfg) if isinstance(base_cfg, dict) else {}
        if not isinstance(override, dict):
            return merged
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = self._merge_signal_cfg(merged.get(key, {}), value)
            else:
                merged[key] = value
        return merged

    def _get_variant_signal_cfg(self, variant_mode: str = None) -> Dict:
        """Build an effective signal config using STRICT or CUSTOM overrides."""
        base_cfg = self.config.signal
        mode = (variant_mode or base_cfg.get("variant_mode_default", "STRICT")).upper()
        if mode == "CUSTOM":
            override = base_cfg.get("variant_custom", {}) or {}
        else:
            # Default to STRICT for safety
            override = base_cfg.get("variant_strict", {}) or {}
            mode = "STRICT"
        merged = self._merge_signal_cfg(base_cfg, override)
        merged["variant_mode"] = mode
        return merged

    def _get_recent_logs(self, max_lines: int = 500) -> List[str]:
        """Returns logs from the in-memory buffer."""
        # max_lines is ignored as deque has a fixed maxlen
        return list(self.log_buffer)

    def _save_snapshot_debug(self, now, prev_state):
        """Helper to save state during scan for debugging."""
        snapshot = {
            "last_update": now.isoformat(),
            "generated_at": now.isoformat(),
            "tickers": prev_state.get("tickers", {}),
            "stats": self.signals.get_stats(),
            "new_signals": [],
            "signal_outcomes": {},
            "news": self.news_cache,
            "log": self._get_recent_logs(),
            "watchlist_events": list(self.watchlist_events),
        }
        with self.snapshot_lock:
             with self.state_cache_lock:
                 self.state_cache = snapshot
             save_state_payload(snapshot)

    def _is_agent_enabled(self, agent_key: str, default: bool = True) -> bool:
        try:
            return is_agent_enabled(agent_key, config=self.config.config, default=default)
        except Exception:
            return bool(default)

    def _reload_agent_config_if_changed(self) -> bool:
        """Hot-apply Agent Fleet toggles without waiting for the next market scan."""
        with self._agent_config_reload_lock:
            changed = self.config.reload_if_changed()
            if changed:
                self.agent_manager.update_config()
            return bool(changed)

    def _start_agent_config_watcher(self) -> None:
        def _watch() -> None:
            while not self._agent_config_watch_stop.wait(1.0):
                try:
                    self._reload_agent_config_if_changed()
                except Exception as exc:
                    logger.error("Agent Fleet config watcher failed: %s", exc)

        threading.Thread(
            target=_watch,
            daemon=True,
            name="AgentFleetConfigWatcher",
        ).start()

    def _get_news_agent_nonblocking(self):
        if not self._is_agent_enabled("news_tab", True):
            return None
        if self.news_agent is not None:
            return self.news_agent
        try:
            self.news_agent = get_news_tab_agent_if_ready()
            if self.news_agent is None:
                kickoff_news_tab_agent_init(self.config.config)
        except Exception as e:
            logger.error(f"News agent availability check failed: {e}")
        return self.news_agent

    # ── news in the engine subprocess: read-only mirror ───────────────────
    def _news_mirror_interval_seconds(self) -> float:
        try:
            raw = float(
                self.config.news.get("engine_mirror_refresh_seconds", NEWS_MIRROR_REFRESH_SECONDS)
                or NEWS_MIRROR_REFRESH_SECONDS
            )
        except (AttributeError, TypeError, ValueError):
            raw = NEWS_MIRROR_REFRESH_SECONDS
        return max(NEWS_MIRROR_MIN_REFRESH_SECONDS, raw)

    def _mirror_news_from_backup(self, agent, *, force: bool = False) -> List[Dict]:
        """Engine subprocess: take the news the API process fetched. Read-only.

        With the engine in its own process both processes used to run the full
        news refresh: the same feeds were fetched twice and each process saved
        its own in-memory state over ``data/news_backup.json``, so articles and
        AI briefings found by one were lost when the other saved. The API
        process owns the refresh; this process only re-reads the backup (at
        most every ``news.engine_mirror_refresh_seconds`` and only when the
        file changed) into the in-process news agent, which the signal context,
        the sentiment/radar/pre-earnings agents and ``snapshot["news"]`` read.
        It never calls a refresh or a save.

        Returns the articles (dicts) that were not in the previous mirror.
        """
        mirror = self.__dict__.setdefault("_news_mirror", {})
        monotonic_now = time.monotonic()
        checked = mirror.get("checked_monotonic")
        if (
            not force
            and checked is not None
            and (monotonic_now - checked) < self._news_mirror_interval_seconds()
        ):
            return []
        mirror["checked_monotonic"] = monotonic_now
        mirror["checked_at"] = datetime.now(pytz.UTC).isoformat()

        path = news_backup_path()
        try:
            stat = os.stat(path)
        except OSError:
            mirror["error"] = "news backup file not found"
            return []
        signature = (int(stat.st_mtime_ns), int(stat.st_size))
        if mirror.get("signature") == signature:
            mirror["error"] = ""
            return []  # the API process has not saved anything new

        data = read_news_backup_readonly(path)
        if data is None:
            mirror["error"] = "news backup unreadable"
            return []
        payload, rows_by_id = hydratable_news_payload(data)
        agent.hydrate_state(payload, reanalyze_existing=False)

        previous_ids = mirror.get("article_ids")
        mirror.update(
            {
                "signature": signature,
                "article_ids": set(rows_by_id),
                "articles": len(rows_by_id),
                "loaded_at": mirror["checked_at"],
                "backup_modified_at": datetime.fromtimestamp(stat.st_mtime, tz=pytz.UTC).isoformat(),
                "last_update": data.get("last_update"),
                "error": "",
            }
        )
        if previous_ids is None:
            return []  # first mirror: nothing to compare against
        return [row for article_id, row in rows_by_id.items() if article_id not in previous_ids]

    def _news_mirror_status(self) -> Dict:
        """Where ``snapshot["news"]`` came from in the engine subprocess."""
        mirror = self.__dict__.get("_news_mirror") or {}
        return {
            "source": "api_process_backup",
            "read_only": True,
            "loaded_at": mirror.get("loaded_at"),
            "checked_at": mirror.get("checked_at"),
            "backup_modified_at": mirror.get("backup_modified_at"),
            "articles": mirror.get("articles"),
            "error": mirror.get("error") or "",
        }

    def _update_snapshot_news(self, snapshot: Dict, now: datetime) -> None:
        """Fill ``snapshot["news"]`` (and the news rows of the activity log).

        Single process (thread mode): this engine refreshes the news itself.
        Engine subprocess: the API process owns the refresh and the backup
        file; this process mirrors that backup read-only.
        """
        try:
            if not self._is_agent_enabled("news_tab", True):
                snapshot["news"] = {**self.news_cache, "loading": False, "disabled": True}
            else:
                news_cfg = self.config.news
                news_interval = news_cfg.get("news_refresh_minutes", 5)
                agent = self._get_news_agent_nonblocking()

                news_mirrored = news_owned_by_api_process()

                if agent is None:
                    snapshot["news"] = {**self.news_cache, "loading": True}
                elif news_mirrored:
                    # Engine subprocess: the API process fetches and saves the
                    # news; this process only mirrors its backup, read-only.
                    mirrored_rows = self._mirror_news_from_backup(agent)
                    if mirrored_rows:
                        if "activity_log" not in snapshot:
                            snapshot["activity_log"] = []
                        for row in reversed(mirrored_rows[:NEWS_MIRROR_MAX_ACTIVITY_ROWS]):
                            snapshot["activity_log"].insert(0, {
                                "time": now.isoformat(),
                                "desc": f"News: {row.get('title') or row.get('headline')} ({row.get('ticker')})",
                                "type": "news",
                                "sentiment": str(row.get("sentiment") or "neutral"),
                                "impact": str(row.get("impact") or "neutral"),
                            })
                        snapshot["activity_log"] = snapshot["activity_log"][:300]
                elif (
                    self.last_news_check is None
                    or (now - self.last_news_check).total_seconds() >= news_interval * 60
                ):
                    tickers = list(snapshot["tickers"].keys())
                    logger.info("📰 Refreshing news via NewsTabAgent...")
                    _, new_articles = agent.refresh_all_news(tickers)
                    self.last_news_check = now

                    if new_articles:
                        if "activity_log" not in snapshot:
                            snapshot["activity_log"] = []

                        # Log new articles (Newest First)
                        for article in new_articles:
                            try:
                                entry = {
                                    "time": now.isoformat(),
                                    "desc": f"News: {article.title} ({article.ticker})",
                                    "type": "news",
                                    "sentiment": getattr(article, "sentiment", "neutral").value if hasattr(getattr(article, "sentiment", None), "value") else "neutral",
                                    "impact": getattr(article, "impact", "neutral").value if hasattr(getattr(article, "impact", None), "value") else "neutral"
                                }
                                snapshot["activity_log"].insert(0, entry)
                            except Exception as log_err:
                                logger.error(f"Failed to log article activity: {log_err}")

                        # Trim activity log to last 300 items
                        snapshot["activity_log"] = snapshot["activity_log"][:300]

                if agent is not None:
                    self.news_agent = agent
                    snapshot["news"] = {**agent.get_news_tab_state(), "loading": False}
                    if news_mirrored:
                        snapshot["news"]["mirror"] = self._news_mirror_status()
            
        except Exception as e:
            logger.error(f"News update failed: {e}")
            # Keep previous state if available
            if self.news_agent is not None:
                snapshot["news"] = {**self.news_agent.get_news_tab_state(), "loading": False}
            else:
                snapshot["news"] = {**self.news_cache, "loading": True}

    def start_background_news_refresh(self):
        """Start a background thread to refresh news on startup."""
        if not self._is_agent_enabled("news_tab", True):
            return
        if news_owned_by_api_process():
            logger.info("Startup news refresh skipped: the API process owns the news refresh.")
            return
        if not bool(self.config.news.get("startup_refresh_enabled", False)):
            logger.info("Startup news refresh skipped by news.startup_refresh_enabled=false.")
            return

        def _refresh():
            try:
                # Wait briefly for main thread to stabilize
                import time
                time.sleep(5)
                if not self._is_agent_enabled("news_tab", True):
                    logger.info("Startup news refresh skipped because news flow is disabled.")
                    return
                logger.info("📰 Triggering startup news refresh...")
                # Get initial tickers from config
                stocks = self.config.watchlist.get("yahoo", [])
                cryptos = self.config.watchlist.get("binance", [])
                initial_tickers = list(set(stocks + cryptos))
                
                agent = self.news_agent or get_news_tab_agent(self.config.config)
                self.news_agent = agent
                agent.refresh_all_news(initial_tickers)
                
                # Update local cache so next scan picks it up
                with self.state_cache_lock:
                    self.news_cache = agent.get_news_tab_state()
                    self.news_cache["loading"] = False
                    if self.state_cache:
                         self.state_cache["news"] = self.news_cache
                         
                # FIX: Force write to disk immediately so API sees it (if running in separate process context)
                try:
                    save_state_payload(self.state_cache)
                except Exception as e:
                    logger.error(f"Startup news save failed: {e}")
                
                logger.info(f"📰 Startup news refresh complete. Market News: {len(self.news_cache.get('market_news', []))}")
            except Exception as e:
                logger.error(f"Startup news refresh failed: {e}")

        t = threading.Thread(target=_refresh, daemon=True, name="StartupNewsRefresh")
        t.start()

    def _start_background_calendar_refresh(self):
        """Start a background thread to refresh calendar data on startup."""
        if not self._is_agent_enabled("economic_calendar", True):
            logger.info("Economic calendar flow disabled; skipping startup calendar refresh.")
            return

        def _refresh():
            try:
                import time
                time.sleep(8)  # Wait for other agents to stabilize
                if not self._is_agent_enabled("economic_calendar", True):
                    logger.info("Startup calendar refresh skipped because economic calendar flow is disabled.")
                    return
                logger.info("📅 Triggering startup calendar refresh...")
                from agents.economic_calendar_agent import get_economic_calendar_agent
                agent = get_economic_calendar_agent()
                agent.refresh_data(days_before=30, days_after=90)
                logger.info("📅 Startup calendar refresh complete.")
            except Exception as e:
                logger.error(f"Startup calendar refresh failed: {e}")
        
        t = threading.Thread(target=_refresh, daemon=True, name="StartupCalendarRefresh")
        t.start()

    # ---- bucla de scan ----
    # ── exit watch ────────────────────────────────────────────────────────
    @staticmethod
    def _fast_poll_price_evidence(prev_ticker: Dict, live_price: float) -> Tuple[bool, Dict]:
        """Carry price verification across a fast exchange poll.

        A direct exchange quote that continues a verified price (one small
        step away from it) stays verified; anything else waits for the next
        full scan. Shared by the scan loop's fast poll and the exit watch so
        both apply exactly the same rule.
        """
        prev = prev_ticker if isinstance(prev_ticker, dict) else {}
        prev_integrity = prev.get("price_integrity")
        try:
            prev_price = float(prev.get("price") or 0.0)
        except (TypeError, ValueError):
            prev_price = 0.0
        fast_verified = bool(
            prev.get("price_verified") is True
            and isinstance(prev_integrity, dict)
            and prev_integrity.get("is_verified")
            and prev_price > 0
            and abs(float(live_price) - prev_price) / prev_price <= FAST_POLL_MAX_STEP
        )
        integrity = (
            {**prev_integrity, "carried_by": "binance_fast_poll"}
            if fast_verified
            else {"is_verified": False, "status": "awaiting_full_scan"}
        )
        return fast_verified, integrity

    def _signal_status_guard(self) -> "threading.RLock":
        lock = self.__dict__.get("_signal_status_lock")
        if lock is None:
            lock = self.__dict__.setdefault("_signal_status_lock", threading.RLock())
        return lock

    @staticmethod
    def _row_age_seconds(row: Dict, now: Optional[datetime] = None) -> Optional[float]:
        """Seconds since a watchlist row was written; None when undated."""
        raw = (row or {}).get("last_update") if isinstance(row, dict) else None
        if not raw:
            return None
        try:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            current = now or get_bucharest_time()
            if stamp.tzinfo is None:
                # Undated zone: rows are written in engine-local time.
                return max(0.0, (current.replace(tzinfo=None) - stamp).total_seconds())
            return max(0.0, (current - stamp).total_seconds())
        except Exception:
            return None

    def _check_signal_exits(self, tickers_data: Dict) -> Dict[str, str]:
        """Run the unchanged stop/target rule; serialised across threads.

        Rows that were written long ago are left out: a row keeps its
        ``price_verified`` flag forever, and a stock row last written weeks
        earlier (market closed, engine not running in US hours) must not
        settle a signal at that old price.
        """
        now = get_bucharest_time()
        usable: Dict = {}
        for ticker, row in (tickers_data or {}).items():
            age = self._row_age_seconds(row, now)
            if age is not None and age > SIGNAL_EXIT_MAX_ROW_AGE_SECONDS:
                continue
            usable[ticker] = row
        with self._signal_status_guard():
            return self.signals.update_signal_status(usable) or {}

    def _data_guard_state(self) -> Tuple[Dict[str, str], bool]:
        """Freshness of the sources THIS process fetches.

        The guard tracks successful provider calls per process. With the
        engine in its own process the news refresh belongs to the API process,
        so the engine never sees a news call: after five minutes it reported
        ``news=STALE`` for good, flagged every snapshot ``is_data_stale`` and
        stamped every signal's quality as STALE although nothing was late.
        A source this process does not fetch is reported as such, not as stale.
        """
        from core.data_guard import get_data_guard

        raw_state = get_data_guard().check_freshness()
        state = dict(raw_state) if isinstance(raw_state, dict) else {}
        if str(os.getenv("MS_PROCESS_ROLE", "") or "").strip().lower() == "engine":
            if state.get("news") in {"STALE", "UNKNOWN"}:
                state["news"] = "OWNED_BY_API_PROCESS"
        return state, any(status == "STALE" for status in state.values())

    # ── stock rows without a fresh 1-minute feed ──────────────────────────
    def _equity_data_session_closed(self, ticker: str) -> bool:
        """True when ``ticker`` cannot have fresh regular-session bars now."""
        try:
            from core.market_hours import (
                is_equity_market_hours_enforced,
                us_equity_market_status,
                us_equity_session_snapshot,
            )

            if not is_equity_market_hours_enforced(ticker):
                return False
            if us_equity_market_status().is_open:
                return False
            raw_close = us_equity_session_snapshot().get("last_regular_close_utc")
            if not raw_close:
                return True
            last_close = datetime.fromisoformat(str(raw_close).replace("Z", "+00:00"))
            now_utc = datetime.now(pytz.UTC)
            return (now_utc - last_close).total_seconds() > CLOSED_SESSION_GRACE_SECONDS
        except Exception as exc:
            logger.debug(f"Session check failed for {ticker}: {exc}")
            return False

    def _stock_trend_fields(self, ticker: str) -> Dict:
        """Trend / MACD fields shared by the display-only stock rows."""
        mtf = self._get_mtf_trends(ticker, False) or {}
        with self.mtf_cache_lock:
            cached_mtf = dict(self.mtf_cache.get(ticker, {}))

        def _trend(key: str) -> str:
            for source in (mtf, cached_mtf):
                value = source.get(key)
                if value and value != "N/A":
                    return value
            return "N/A"

        fields: Dict = {}
        for tf in ("1d", "4h", "1h", "30m", "15m", "5m"):
            fields[f"trend_{tf}"] = _trend(tf)
            fields[f"ts_{tf}"] = mtf.get(f"ts_{tf}", cached_mtf.get(f"ts_{tf}", ""))
        macd_hist = None
        for key in ("hybrid_macd_4h", "raw_macd_1d", "raw_macd_4h"):
            value = mtf.get(key)
            if value is None:
                value = cached_mtf.get(key)
            if value is not None:
                try:
                    macd_hist = float(value)
                    break
                except (TypeError, ValueError):
                    continue
        fields["macd_hist"] = macd_hist
        return fields

    def _higher_timeframe_rsi(self, ticker: str, daily: Optional[pd.DataFrame]) -> Tuple[Optional[float], str]:
        """RSI from the most granular completed frame (1h, else 1d)."""
        period = int(self.config.signal.get("rsi_period") or 14)
        with self.mtf_cache_lock:
            frames = dict(getattr(self, "mtf_frame_cache", {}).get(ticker, {}))
        for label, frame in (("1h", frames.get("1h")), ("1d", daily)):
            try:
                if isinstance(frame, pd.DataFrame) and "close" in frame.columns and len(frame) > period + 1:
                    value = float(TechnicalAnalysis.calc_rsi(frame["close"], period).iloc[-1])
                    if not pd.isna(value):
                        return value, label
            except Exception as exc:
                logger.debug(f"Stock {ticker}: {label} RSI unavailable: {exc}")
        return None, ""

    def _drop_pre_close_yahoo_cache(self, ticker: str) -> None:
        """Forget frames fetched before the last regular close.

        The daily frame is cached for 12 hours; one fetched during the session
        ends in a half-formed bar, which is not the session's close.
        """
        try:
            from core.market_hours import us_equity_session_snapshot

            raw_close = us_equity_session_snapshot().get("last_regular_close_utc")
            if not raw_close:
                return
            close_local = datetime.fromtimestamp(
                datetime.fromisoformat(str(raw_close).replace("Z", "+00:00")).timestamp()
            )
            with self._yahoo_cache_lock:
                for interval in ("1d", "1h", "30m", "15m", "5m"):
                    key = f"{ticker}_{interval}"
                    cached = self._yahoo_cache.get(key)
                    if cached and cached[1] < close_local:
                        self._yahoo_cache.pop(key, None)
        except Exception as exc:
            logger.debug(f"Stock {ticker}: pre-close cache check failed: {exc}")

    def _unavailable_stock_result(
        self,
        ticker: str,
        now: datetime,
        prev_ticker: Optional[Dict],
        *,
        known_missing: bool,
    ) -> Optional[Dict]:
        """Row for a stock no provider returned data for. Written once.

        * The ticker already has a priced row and the symbol is on the
          known-missing list (delisted / unknown to Yahoo): that row kept
          ``data_quality="verified"`` / ``price_verified=True`` for good,
          because every refresh path returned nothing (SLP, delisted
          2026-10-05, still showed its 2026-09-08 row as verified on
          2026-11-15). It is marked ``listing_inactive`` with the last known
          price and the date of that price.
        * The ticker has no row at all (CPRX): it gets a minimal ``no_data``
          row so every tab can say "no data" instead of omitting the symbol.
          ``price`` is 0.0 -- the value the rest of the app already treats as
          "no price" -- and ``price_available`` is False; no number is invented.
        * A priced row whose symbol is not known-missing is left alone: one
          empty provider answer does not prove a delisting.

        Returns ``None`` when the row already carries the right marking.
        """
        prev = prev_ticker if isinstance(prev_ticker, dict) else {}
        prev_quality = str(prev.get("data_quality") or "")
        try:
            last_price = float(prev.get("price") or 0.0)
        except (TypeError, ValueError):
            last_price = 0.0
        if not math.isfinite(last_price) or last_price < 0:
            last_price = 0.0
        unknown_trends = {
            "trend_1d": "N/A", "trend_4h": "N/A", "trend_1h": "N/A",
            "trend_30m": "N/A", "trend_15m": "N/A", "trend_5m": "N/A",
            "rsi": None,
            "macd_hist": None,
        }

        if last_price > 0:
            if not known_missing or prev_quality == "listing_inactive":
                return None
            price_as_of = str(prev.get("price_as_of") or "").strip()
            if not price_as_of:
                price_as_of = str(prev.get("last_update") or "").strip()[:10]
            since = f"since {price_as_of}" if price_as_of else "for a long time"
            return {
                "ticker": ticker,
                "data": {
                    "price": last_price,
                    "technical_price": last_price,
                    "change_pct": 0.0,
                    "last_update": now.isoformat(),
                    "asset_type": "stock",
                    "data_source": str(prev.get("data_source") or "yahoo"),
                    "data_quality": "listing_inactive",
                    "price_verified": False,
                    "price_integrity": {
                        "asset_type": "stock",
                        "is_verified": False,
                        "status": "listing_inactive",
                        "reason": (
                            f"No market data {since}; the data provider no longer knows "
                            "the symbol (delisted, acquired, renamed or suspended)"
                        ),
                    },
                    "session": "inactive",
                    "price_as_of": price_as_of,
                    **unknown_trends,
                },
                "signal": None,
            }

        if prev_quality == "no_data":
            return None
        reason = f"No price data is available for {ticker} from any provider"
        if known_missing:
            reason += " (the symbol is unknown to the data provider: delisted, renamed or not listed)"
        return {
            "ticker": ticker,
            "data": {
                "price": 0.0,
                "price_available": False,
                "technical_price": None,
                "change_pct": 0.0,
                "last_update": now.isoformat(),
                "asset_type": "stock",
                "data_source": "none",
                "data_quality": "no_data",
                "price_verified": False,
                "price_integrity": {
                    "asset_type": "stock",
                    "is_verified": False,
                    "status": "no_data",
                    "reason": reason,
                },
                "session": "inactive" if known_missing else None,
                "price_as_of": None,
                "no_data_since": now.isoformat(),
                **unknown_trends,
            },
            "signal": None,
        }

    def _closed_session_stock_result(self, ticker: str, now: datetime, prev_ticker: Dict) -> Optional[Dict]:
        """Refresh a stock row from daily/hourly candles while the session is closed.

        Outside the regular session the newest 1-minute bar is older than the
        freshness guard allows, so the scan used to drop the ticker without
        writing anything: trends, RSI, MACD and the row price stayed at the
        last value written during a regular session (2026-09-08 for every
        stock on 2026-11-15, because the engine had only run on nights and
        weekends since). The row is now rebuilt from completed candles, marked
        ``session="closed"`` and left unverified, so nothing can open or
        settle a signal from it.
        """
        try:
            interval = max(
                300.0,
                float(self.config.scan.get("closed_market_refresh_seconds", CLOSED_SESSION_REFRESH_SECONDS)
                      or CLOSED_SESSION_REFRESH_SECONDS),
            )
        except (TypeError, ValueError):
            interval = CLOSED_SESSION_REFRESH_SECONDS
        refreshed_at = self.__dict__.setdefault("_closed_refresh_at", {})
        last = refreshed_at.get(ticker)
        if (
            last is not None
            and str((prev_ticker or {}).get("session") or "") in {"closed", "inactive"}
            and (time.monotonic() - last) < interval
        ):
            return None  # the row from the last closed-session refresh stands

        from core.stock_data_client import is_known_missing as _yahoo_known_missing

        yahoo_symbol = normalize_ticker(ticker, provider="yahoo")
        if _yahoo_known_missing(yahoo_symbol):
            # Delisted / unknown on Yahoo: nothing to refresh, ask again later.
            # The row must still stop looking like a verified live price.
            refreshed_at[ticker] = time.monotonic()
            return self._unavailable_stock_result(ticker, now, prev_ticker, known_missing=True)

        self._drop_pre_close_yahoo_cache(ticker)
        daily = self._fetch_market_data(ticker, "1d", "1y")
        if daily is None or daily.empty or "close" not in daily.columns:
            # Retry in five minutes instead of on every scan.
            refreshed_at[ticker] = time.monotonic() - interval + 300.0
            return self._unavailable_stock_result(
                ticker, now, prev_ticker, known_missing=_yahoo_known_missing(yahoo_symbol)
            )
        closes = pd.to_numeric(daily["close"], errors="coerce").dropna()
        if closes.empty:
            refreshed_at[ticker] = time.monotonic() - interval + 300.0
            return None
        last_close = float(closes.iloc[-1])
        if last_close <= 0:
            return None
        change_pct = 0.0
        if len(closes) >= 2 and float(closes.iloc[-2]) > 0:
            change_pct = (last_close - float(closes.iloc[-2])) / float(closes.iloc[-2]) * 100.0
        try:
            price_as_of = pd.Timestamp(closes.index[-1]).date().isoformat()
        except Exception:
            price_as_of = ""

        # A listing whose newest daily bar is more than a week old did not
        # trade in the last session: delisted, acquired or suspended. Its
        # final print is not "the last close" and must not look like one.
        try:
            days_since_bar = (now.date() - pd.Timestamp(closes.index[-1]).date()).days
        except Exception:
            days_since_bar = 0
        if days_since_bar > 7:
            try:
                from core.stock_data_client import mark_missing as _yahoo_mark_missing

                _yahoo_mark_missing(normalize_ticker(ticker, provider="yahoo"), "listing_inactive")
            except Exception:
                pass
            refreshed_at[ticker] = time.monotonic()
            return {
                "ticker": ticker,
                "data": {
                    "price": last_close,
                    "technical_price": last_close,
                    "change_pct": 0.0,
                    "last_update": now.isoformat(),
                    "asset_type": "stock",
                    "data_source": "yahoo",
                    "data_quality": "listing_inactive",
                    "price_verified": False,
                    "price_integrity": {
                        "asset_type": "stock",
                        "is_verified": False,
                        "status": "listing_inactive",
                        "reason": (
                            f"No trade since {price_as_of}; the ticker looks delisted, "
                            "acquired or suspended"
                        ),
                    },
                    "session": "inactive",
                    "price_as_of": price_as_of,
                    "trend_1d": "N/A", "trend_4h": "N/A", "trend_1h": "N/A",
                    "trend_30m": "N/A", "trend_15m": "N/A", "trend_5m": "N/A",
                    "rsi": None,
                    "macd_hist": None,
                },
                "signal": None,
            }

        trend_fields = self._stock_trend_fields(ticker)
        rsi_value, rsi_timeframe = self._higher_timeframe_rsi(ticker, daily)

        session: Dict = {}
        try:
            from core.market_hours import us_equity_session_snapshot

            session = us_equity_session_snapshot() or {}
        except Exception:
            session = {}

        macd_hist = trend_fields.pop("macd_hist", None)
        data = {
            "price": last_close,
            "technical_price": last_close,
            "change_pct": change_pct,
            "last_update": now.isoformat(),
            "asset_type": "stock",
            "data_source": "yahoo",
            "data_quality": "market_closed",
            "price_verified": False,
            "price_integrity": {
                "asset_type": "stock",
                "is_verified": False,
                "status": "market_closed",
                "reason": "US regular session is closed; price is the last regular-session close",
                "technical_source": "yahoo_history_1d",
            },
            "session": "closed",
            "session_state": session.get("state"),
            "session_closed_kind": session.get("closed_kind"),
            "session_label": session.get("label"),
            "next_regular_open_utc": session.get("next_regular_open_utc"),
            "price_as_of": price_as_of,
            **trend_fields,
            "rsi": rsi_value if rsi_value is not None else (prev_ticker or {}).get("rsi", 50.0),
            "rsi_timeframe": rsi_timeframe or (prev_ticker or {}).get("rsi_timeframe", ""),
            "macd_hist": macd_hist if macd_hist is not None else (prev_ticker or {}).get("macd_hist", 0.0),
        }
        refreshed_at[ticker] = time.monotonic()
        return {"ticker": ticker, "data": data, "signal": None}

    def _stale_intraday_stock_result(
        self,
        ticker: str,
        now: datetime,
        prev_ticker: Dict,
        live_price: Optional[float],
        pct: float,
        data_source: str,
        df: pd.DataFrame,
        data_age: float,
    ) -> Optional[Dict]:
        """Display row for a stock whose 1-minute bars lag in the regular session.

        The freshness guard still blocks every signal, but returning nothing
        also froze the row (price, trends, last_update). The row is refreshed
        as unverified so the tabs show current data labelled as lagging.
        """
        try:
            raw_close = df["close"].iloc[-1]
            technical_price = float(raw_close.item() if hasattr(raw_close, "item") else raw_close)
        except Exception:
            technical_price = 0.0
        price = float(live_price) if live_price and live_price > 0 else technical_price
        if price <= 0:
            return None
        if not pct:
            try:
                pct = float((prev_ticker or {}).get("change_pct") or 0.0)
            except (TypeError, ValueError):
                pct = 0.0
        trend_fields = self._stock_trend_fields(ticker)
        macd_hist = trend_fields.pop("macd_hist", None)
        age_text = "unknown" if data_age == float("inf") else f"{data_age:.0f}s"
        data = {
            "price": price,
            "technical_price": technical_price,
            "change_pct": pct,
            "last_update": now.isoformat(),
            "asset_type": "stock",
            "data_source": data_source if (live_price and live_price > 0) else "yahoo_history",
            "data_quality": "stale_intraday",
            "price_verified": False,
            "price_integrity": {
                "asset_type": "stock",
                "is_verified": False,
                "status": "stale_intraday_data",
                "reason": f"Newest 1-minute bar is {age_text} old (limit 120s); signals are blocked",
            },
            "session": "regular",
            **trend_fields,
            "rsi": (prev_ticker or {}).get("rsi", 50.0),
            "macd_hist": macd_hist if macd_hist is not None else (prev_ticker or {}).get("macd_hist", 0.0),
        }
        return {"ticker": ticker, "data": data, "signal": None}

    @staticmethod
    def _alert_texts(alert_data: Dict, price, row: Optional[Dict]) -> Dict:
        """Wording for a triggered alert (activity log, feed and Telegram).

        A VOLATILITY alert has no price target: its ``target`` is a percent
        threshold on the day's move (24h for crypto). The message used to read
        "VOLATILITY target reached" / "Target: 5", which says neither what
        happened nor that the 5 is a percentage.
        """
        alert_data = alert_data if isinstance(alert_data, dict) else {}
        ticker = str(alert_data.get("ticker") or "")
        target = alert_data.get("target")
        a_type = str(alert_data.get("type") or "")
        if a_type != "VOLATILITY":
            return {
                "log": f"🔔 ALERT: {ticker} hit {a_type} target {target} (@{price})",
                "event": f"Smart Alert: {ticker} {a_type.replace('_', ' ')} target {target} reached!",
                "title": f"🔔 {ticker} — {a_type.replace('_', ' ')} target reached",
                "lines": [f"Target: {target}", f"Price now: {price}"],
            }

        try:
            threshold_text = f"{float(target):g}%"
        except (TypeError, ValueError):
            threshold_text = f"{target}%"
        row = row if isinstance(row, dict) else {}
        try:
            change: Optional[float] = float(row.get("change_pct"))
            if not math.isfinite(change):
                change = None
        except (TypeError, ValueError):
            change = None
        is_crypto = str(row.get("asset_type") or "").lower() == "crypto"
        window = "in the last 24h" if is_crypto else "today"
        if change is None:
            moved = f"moved {threshold_text} or more {window}"
            lines = [f"Threshold: {threshold_text} move", f"Price now: {price}"]
        else:
            moved = f"moved {change:+.2f}% {window} (threshold {threshold_text})"
            lines = [
                f"Move {window}: {change:+.2f}%",
                f"Threshold: {threshold_text} move",
                f"Price now: {price}",
            ]
        return {
            "log": f"🔔 ALERT: {ticker} {moved} (@{price})",
            "event": f"Smart Alert: {ticker} {moved}",
            "title": f"🔔 {ticker} — {moved}",
            "lines": lines,
        }

    def _drain_exit_watch_outcomes(self) -> Dict[str, str]:
        with self._signal_status_guard():
            pending = dict(self.__dict__.get("_exit_watch_outcomes") or {})
            self._exit_watch_outcomes = {}
        return pending

    def _exit_watch_tick(self) -> Dict[str, str]:
        """Check signal exits on fresh crypto prices while a scan is busy.

        The scan loop is sequential: fast poll -> heavy stock scan -> heavy
        crypto scan -> AI entry reviews -> exit check -> news refresh. A slow
        step used to postpone the exit check by the whole cycle (tens of
        seconds to minutes). This tick does not change any rule: it feeds
        ``SignalManager.update_signal_status`` the same verified-price rows
        the fast poll would produce, only sooner. Stocks are not polled here
        (their prices come from the scan itself).
        """
        started = self.__dict__.get("_scan_started_monotonic")
        if started is None or (time.monotonic() - float(started)) < EXIT_WATCH_INTERVAL_SECONDS:
            return {}
        if not self.signals.open_signals:
            return {}
        watchlist = self.config.watchlist or {}
        cryptos = list(watchlist.get("binance", watchlist.get("crypto", [])) or [])
        open_tickers = {
            str((sig or {}).get("ticker") or "")
            for sig in list(self.signals.open_signals.values())
            if isinstance(sig, dict)
        }
        pairs = [pair for pair in cryptos if pair in open_tickers]
        if not pairs:
            return {}
        bulk_prices = BinanceClient.get_bulk_last_prices(pairs, int(EXIT_WATCH_INTERVAL_SECONDS)) or {}
        if not bulk_prices:
            return {}
        with self.state_cache_lock:
            base_rows = dict((self.state_cache or {}).get("tickers") or {})
        carried = self.__dict__.setdefault("_exit_watch_rows", {})
        view: Dict[str, Dict] = {}
        for pair in pairs:
            live_price = bulk_prices.get(pair)
            if live_price is None:
                continue
            prev_row = carried.get(pair) or base_rows.get(pair) or {}
            verified, integrity = self._fast_poll_price_evidence(prev_row, live_price)
            row = {
                "price": live_price,
                "price_verified": verified,
                "price_integrity": integrity,
            }
            if verified:
                carried[pair] = row
            view[pair] = row
        if not view:
            return {}
        outcomes = self._check_signal_exits(view)
        self._exit_watch_checks = int(self.__dict__.get("_exit_watch_checks") or 0) + 1
        if outcomes:
            with self._signal_status_guard():
                self.__dict__.setdefault("_exit_watch_outcomes", {}).update(outcomes)
            logger.info(
                "Exit watch closed signal(s) during a busy scan: %s",
                ", ".join(f"{ticker}={result}" for ticker, result in outcomes.items()),
            )
        return outcomes

    def _exit_watch_loop(self) -> None:
        stop_event = self._exit_watch_stop
        while not stop_event.wait(EXIT_WATCH_INTERVAL_SECONDS):
            try:
                self._exit_watch_tick()
            except Exception as exc:
                logger.error(f"Exit watch tick failed: {exc}", exc_info=True)

    def start_exit_watch(self) -> None:
        thread = self.__dict__.get("_exit_watch_thread")
        if thread is not None and thread.is_alive():
            return
        self._exit_watch_stop = threading.Event()
        self._exit_watch_thread = threading.Thread(
            target=self._exit_watch_loop, daemon=True, name="ExitWatch"
        )
        self._exit_watch_thread.start()
        logger.info(f"Exit watch started ({EXIT_WATCH_INTERVAL_SECONDS:.0f}s cadence while a scan is busy)")

    def run_scan(self) -> None:
        """One scan cycle, flagged as busy so the exit watch can cover it."""
        self._scan_started_monotonic = time.monotonic()
        try:
            self._run_scan_impl()
        finally:
            self._scan_started_monotonic = None
            # The next busy period restarts from this scan's verified prices.
            self._exit_watch_rows = {}

    def _run_scan_impl(self) -> None:
        now = get_bucharest_time()
        
        # Explicit log to buffer
        msg_start = f"[{now.strftime('%H:%M:%S')}] Starting scan..."
        self.log_buffer.append(msg_start)

        # config changes + backtest auto
        self._reload_agent_config_if_changed()
            
        # Hot-reload signals if changed externally (e.g. by manual clear script)
        self.signals.reload_if_changed()
        
        self.config.reload_watchlist_if_changed()
        stocks = self.config.watchlist.get("yahoo", self.config.watchlist.get("stocks", FALLBACK_STOCKS))
        cryptos = self.config.watchlist.get("binance", self.config.watchlist.get("crypto", FALLBACK_CRYPTOS))

        # Track watchlist changes for activity log
        current_watchlist = set(stocks + cryptos)
        
        if self.previous_watchlist:  # Skip on first run
            added = current_watchlist - self.previous_watchlist
            removed = self.previous_watchlist - current_watchlist
            
            for ticker in added:
                self.watchlist_events.append({
                    "type": "WATCHLIST_ADD",
                    "ticker": ticker,
                    "timestamp": now.isoformat()
                })
                logger.info(f"📥 Watchlist: {ticker} added")
            
            for ticker in removed:
                self.watchlist_events.append({
                    "type": "WATCHLIST_REMOVE",
                    "ticker": ticker,
                    "timestamp": now.isoformat()
                })
                logger.info(f"📤 Watchlist: {ticker} eliminat")
        
        self.previous_watchlist = current_watchlist.copy()
        
        # FIX: Clean up signals for tickers removed from watchlist
        # Pass tickers data from previous state for exit price calculation
        with self.state_cache_lock:
            prev_tickers_data = self.state_cache.get("tickers", {}) if self.state_cache else {}
        orphans_removed = self.signals.cleanup_orphaned_signals(current_watchlist, prev_tickers_data)
        if orphans_removed > 0:
            logger.info(f"🧹 Removed {orphans_removed} orphaned signal(s) for tickers not in watchlist")

        # Remove signals that no longer match current ticker settings
        invalid_removed = self.signals.cleanup_invalid_signals(self.config, prev_tickers_data)
        if invalid_removed > 0:
            logger.info(f"🧹 Removed {invalid_removed} signal(s) that no longer match settings")
        expired_removed = self.signals.cleanup_expired_unexecuted_signals(self.config)
        if expired_removed > 0:
            logger.info(f"🧹 Archived {expired_removed} expired unexecuted signal(s)")

        stock_interval = self.config.scan.get("stock_refresh_seconds", 60)
        crypto_price_interval = self.config.scan.get("crypto_price_interval_seconds", 5)

        scan_stocks = (
            self.last_stock_scan is None
            or (now - self.last_stock_scan).total_seconds() >= stock_interval
        )
        
        if scan_stocks:
             self.log_buffer.append(f"[{now.strftime('%H:%M:%S')}] Scanning -> Stocks: {len(stocks)} | Cryptos: {len(cryptos)}")

        # Performance: Use in-memory state cache instead of reading file every scan
        with self.state_cache_lock:
            if not self.state_cache:
                try:
                    loaded_state = load_state_payload({})
                    self.state_cache = loaded_state if isinstance(loaded_state, dict) else {}
                except Exception as e:
                    logger.error(f"Failed to load state: {e}")
                    self.state_cache = {}
            prev_state = self.state_cache
            
        # DEBUG: Save immediately to update UI logs
        self._save_snapshot_debug(now, prev_state)

        # FIX: Filter tickers to only include those in current watchlist (remove deleted ones)
        current_watchlist = set(stocks + cryptos)
        prev_tickers = prev_state.get("tickers", {})
        filtered_tickers = {k: v for k, v in prev_tickers.items() if k in current_watchlist}

        # ==== DATA FRESHNESS GUARD ====
        freshness_state, is_stale = self._data_guard_state()

        if is_stale:
            stale_sources = sorted(src for src, status in freshness_state.items() if status == "STALE")
            notice_key = ",".join(stale_sources)
            last_notice = self.__dict__.get("_stale_notice") or ("", 0.0)
            # Say it when the set of stale sources changes, then every 5 min:
            # one identical warning per scan buried the rest of the log.
            if last_notice[0] != notice_key or (time.monotonic() - last_notice[1]) >= 300.0:
                self._stale_notice = (notice_key, time.monotonic())
                logger.warning(f"DataGuard: stale source(s) {notice_key}; flagging UI.")
                self.log_buffer.append(
                    f"[{now.strftime('%H:%M:%S')}] ⚠️ WARNING: Data is stale ({notice_key})"
                )

        snapshot = {
            "last_update": now.isoformat(),
            "generated_at": now.isoformat(),
            "data_freshness": freshness_state,
            "is_data_stale": is_stale,
            "data_health": get_health_dashboard().get_live_dashboard(),
            "tickers": filtered_tickers,
            "stats": self.signals.get_stats(),
            "new_signals": [],
            "signal_outcomes": {},
            "news": self.news_cache,  # Include news cache from start
            "log": self._get_recent_logs(), # FIX: Populate logs from memory buffer
            "watchlist_events": list(self.watchlist_events),
        }

        # ==== FAST CRYPTO UPDATE (LIVE price for UI) ====
        # NOTE: Fast crypto update only fetches live price from Binance ticker API.
        # MTF trends are NOT calculated here - they are only computed during full crypto scan
        # (see "CRYPTO scan greu" section below) which fetches klines and runs technical analysis.
        # This fast update keeps UI responsive by updating price every few seconds without

        # the overhead of downloading 500 candles and calculating MACD for every ticker.
        if cryptos:
            should_fast_update = (
                self.last_crypto_price_poll is None
                or (now - self.last_crypto_price_poll).total_seconds() >= crypto_price_interval
            )

            if should_fast_update:
                self.last_crypto_price_poll = now
                logger.debug(f"⚡ Fast crypto price poll for {len(cryptos)} perechi...")

                bulk_prices = BinanceClient.get_bulk_last_prices(cryptos, crypto_price_interval)
                bulk_pct_24h = BinanceClient.get_bulk_pct_24h(cryptos, crypto_price_interval)

                # Register Binance fast-poll data quality
                if bulk_prices:
                    get_health_dashboard().register(
                        "binance_prices", QualifiedValue.verified("bulk_ok", "binance")
                    )
                else:
                    get_health_dashboard().register(
                        "binance_prices", QualifiedValue.unavailable("binance", "Bulk price fetch returned empty")
                    )

                for pair in cryptos:
                    try:
                        # Live price directly from Binance (without klines)
                        live_price = bulk_prices.get(pair)
                        if live_price is None:
                            continue

                        pct_24h = float(bulk_pct_24h.get(pair, 0.0) or 0.0)

                        # Preserve existing info (trends/timestamps) if available
                        prev_ticker = snapshot["tickers"].get(pair, {})
                        asset_type = prev_ticker.get("asset_type", "crypto")

                        with self.mtf_cache_lock:
                            cached_mtf = self.mtf_cache.get(pair, {})
                        # A direct exchange quote that continues a verified price
                        # stays verified, so stops are checked on every poll and
                        # not only after the next full scan.
                        fast_verified, fast_integrity = self._fast_poll_price_evidence(
                            prev_ticker, live_price
                        )
                        snapshot["tickers"][pair] = {
                            "price_verified": fast_verified,
                            "price_integrity": fast_integrity,
                            "price": live_price,
                            "change_pct": pct_24h,
                            "last_update": now.isoformat(),
                            "asset_type": asset_type,
                            "data_source": "binance",
                            "data_quality": prev_ticker.get("data_quality", "verified"),
                            "trend_1d": prev_ticker.get(
                                "trend_1d", cached_mtf.get("1d")
                            ),
                            "trend_4h": prev_ticker.get(
                                "trend_4h", cached_mtf.get("4h")
                            ),
                            "trend_1h": prev_ticker.get(
                                "trend_1h", cached_mtf.get("1h")
                            ),
                            "trend_30m": prev_ticker.get(
                                "trend_30m", cached_mtf.get("30m")
                            ),
                            "trend_5m": prev_ticker.get(
                                "trend_5m", cached_mtf.get("5m")
                            ),
                            "ts_1d": cached_mtf.get(
                                "ts_1d", prev_ticker.get("ts_1d", "")
                            ),
                            "ts_4h": cached_mtf.get(
                                "ts_4h", prev_ticker.get("ts_4h", "")
                            ),
                            "ts_1h": cached_mtf.get(
                                "ts_1h", prev_ticker.get("ts_1h", "")
                            ),
                            "ts_30m": cached_mtf.get(
                                "ts_30m", prev_ticker.get("ts_30m", "")
                            ),
                            "ts_5m": cached_mtf.get(
                                "ts_5m", prev_ticker.get("ts_5m", "")
                            ),
                            # Preserve RSI and MACD from previous full scan
                            "rsi": prev_ticker.get("rsi", 50.0),
                            "macd_hist": prev_ticker.get("macd_hist", 0.0),
                        }
                    except Exception as e:
                        logger.error(f"Fast crypto price {pair}: {e}")

                # Write fast snapshot so UI immediately sees new crypto price
                try:
                    save_state_payload(snapshot)
                except Exception as e:
                    logger.error(f"Fast crypto state save error: {e}")

                # Check exits on these fresh prices NOW instead of after the
                # heavy scans below (same rule, same evidence, just not late).
                try:
                    early_rows = {
                        pair: snapshot["tickers"][pair]
                        for pair in cryptos
                        if pair in snapshot["tickers"] and bulk_prices.get(pair) is not None
                    }
                    early_outcomes = self._check_signal_exits(early_rows) if early_rows else {}
                    if early_outcomes:
                        with self._signal_status_guard():
                            self.__dict__.setdefault("_exit_watch_outcomes", {}).update(early_outcomes)
                except Exception as e:
                    logger.error(f"Early signal exit check failed: {e}")

        # ==== STOCKS (scan greu, cu yfinance + MTF) - PARALLEL ====
        if scan_stocks and stocks:
            logger.info(f"🔍 Scan STOCKS: {len(stocks)} (parallel)")
            self.last_stock_scan = now

            stock_workers = max(1, min(5, int(self.config.scan.get("stock_scan_max_workers", 2) or 2)))
            # Equities whose session is closed are refreshed from completed
            # candles (see _closed_session_stock_result): no live quotes.
            quote_targets = [t for t in stocks if not self._equity_data_session_closed(t)]
            batch_quotes = self._prefetch_stock_data_quotes(quote_targets) if quote_targets else {}
            with ThreadPoolExecutor(max_workers=stock_workers) as executor:
                # Submit all stock processing tasks
                futures = {
                    executor.submit(
                        self._process_stock_ticker,
                        ticker,
                        now,
                        batch_quotes.get(str(ticker or "").upper().strip(), {}),
                    ): ticker
                    for ticker in stocks
                }

                # Process results as they complete
                for future in as_completed(futures):
                    ticker = futures[future]
                    try:
                        result_dict = future.result()
                        if result_dict:
                            # FIX: Use lock to protect snapshot updates from race conditions
                            with self.snapshot_lock:
                                result_ticker = result_dict["ticker"]
                                snapshot["tickers"][result_ticker] = result_dict["data"]
                                if result_dict["signal"]:
                                    self._process_signal(result_ticker, result_dict["signal"], "stock")
                                    snapshot["tickers"][result_ticker]["status"] = "SIGNAL"
                    except Exception as e:
                        logger.error(f"Stock future {ticker}: {e}")

        # ==== CRYPTO (heavy scan, with klines + signals) - PARALLEL ====
        # Heavy scan runs less often (default 30s) because it takes longer
        crypto_full_scan_interval = self.config.scan.get("crypto_full_scan_seconds", 30)
        if cryptos:
            should_full_scan = (
                self.last_crypto_price_update is None
                or (now - self.last_crypto_price_update).total_seconds()
                >= crypto_full_scan_interval
            )

            if should_full_scan:
                self.last_crypto_price_update = now
                logger.info(f"🔍 Scan CRYPTO: {len(cryptos)} (parallel, interval {crypto_full_scan_interval}s)")

                crypto_workers = max(1, min(5, int(self.config.scan.get("crypto_scan_max_workers", 2) or 2)))
                with ThreadPoolExecutor(max_workers=crypto_workers) as executor:
                    # Submit all crypto processing tasks
                    futures = {executor.submit(self._process_crypto_ticker, pair, now, crypto_price_interval): pair for pair in cryptos}

                    # Process results as they complete
                    for future in as_completed(futures):
                        pair = futures[future]
                        try:
                            result_dict = future.result()
                            if result_dict:
                                # FIX: Use lock to protect snapshot updates from race conditions
                                with self.snapshot_lock:
                                    snapshot["tickers"][result_dict["ticker"]] = result_dict["data"]
                                    if result_dict["signal"]:
                                        self._process_signal(result_dict["ticker"], result_dict["signal"], "crypto")
                                        snapshot["tickers"][result_dict["ticker"]]["status"] = "SEMNAL"
                        except Exception as e:
                            logger.error(f"Crypto future {pair}: {e}")

        self._run_paper_crypto_strategies(snapshot, cryptos, now)

        # ==== outcomes + stats + news ====
        outcomes = self._check_signal_exits(snapshot["tickers"])
        # Exits already taken earlier in this cycle (fast poll / exit watch).
        for early_ticker, early_result in self._drain_exit_watch_outcomes().items():
            outcomes.setdefault(early_ticker, early_result)
        paper_outcomes = self.signals.sync_paper_positions()
        if paper_outcomes:
            outcomes.update(paper_outcomes)
        snapshot["signal_outcomes"] = outcomes
        self.last_signal_outcomes = outcomes

        snapshot["new_signals"] = self.new_signals.copy()
        self.new_signals.clear()

        snapshot["stats"] = self.signals.get_stats()

        if self.last_backtest_results:
            snapshot["backtest_summary"] = self.last_backtest_results.get("summary", {})

        # Update news using NewsTabAgent
        self._update_snapshot_news(snapshot, now)

        # ==== Alert Checking ====
        try:
            # run_scan's reload_watchlist_if_changed() may have pulled alerts
            # created via the API process (UI) from disk — re-sync so new
            # alerts actually fire instead of being wiped by the next save.
            self.alert_manager.reload_from_config()
            triggered_alerts = self.alert_manager.check_alerts(snapshot["tickers"])
            if triggered_alerts:
                if "alerts_triggered" not in snapshot:
                    snapshot["alerts_triggered"] = []
                snapshot["alerts_triggered"].extend(triggered_alerts)
                
                # Also log to activity
                for ta in triggered_alerts:
                    alert_data = ta["alert"]
                    ticker = alert_data["ticker"]
                    price = ta["current_price"]

                    alert_texts = self._alert_texts(
                        alert_data, price, snapshot["tickers"].get(ticker)
                    )

                    self.log_buffer.append(alert_texts["log"])

                    # Add to watchlist events for persistence in state activity feed
                    self.watchlist_events.append({
                        "type": "SMART_ALERT",
                        "ticker": ticker,
                        "desc": alert_texts["event"],
                        "timestamp": now.isoformat()
                    })

                    # Owner-selected Telegram delivery (routing set in Alerts tab)
                    try:
                        from core.telegram_service import notify_telegram

                        notify_telegram(
                            "alerts",
                            alert_texts["title"],
                            alert_texts["lines"],
                        )
                    except Exception:
                        logger.debug("Telegram alert notify skipped", exc_info=True)
        except Exception as e:
            logger.error(f"Alert check failed: {e}")

        save_state_payload(snapshot)

        # Update in-memory state cache
        with self.state_cache_lock:
            self.state_cache = snapshot

        self.signals.save()

        logger.info(
            f"✓ Scan: {len(snapshot['tickers'])} tickere | "
            f"Open: {snapshot['stats']['open']} | "
            f"WinRate: {snapshot['stats']['win_rate']}%"
        )


    # ---- parallel processing helpers ----

    def _process_stock_ticker(
        self,
        ticker: str,
        now: datetime,
        prefetched_quote: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Process a single stock ticker - used for parallel execution."""
        try:
            # Read previous snapshot data for stability in fallback paths
            with self.state_cache_lock:
                prev_ticker = (self.state_cache or {}).get("tickers", {}).get(ticker, {}) or {}

            if self._equity_data_session_closed(ticker):
                return self._closed_session_stock_result(ticker, now, prev_ticker)

            # FIX: Always fetch live price first, don't rely on historical data close price
            # This prevents price corruption from yfinance caching issues in parallel execution
            last_price = None
            pct = 0.0  # Default value
            previous_close = None  # For fallback pct calculation
            data_source = "unknown"

            # 1. Yahoo Finance direct fetch
            if last_price is None:
                try:
                    # FIX: Normalize ticker using global utility
                    y_ticker = normalize_ticker(ticker, provider="yahoo")

                    from core.stock_data_client import is_known_missing as _yahoo_known_missing

                    if _yahoo_known_missing(y_ticker):
                        # Yahoo already answered "unknown symbol" recently
                        # (delisted watchlist ticker): do not ask every scan.
                        live_info = {}
                    else:
                        # Track this Yahoo API call
                        self._yahoo_request_timestamps.append(time.time())

                        with self.yfinance_lock:
                            ticker_obj = yf.Ticker(y_ticker)
                            live_info = ticker_obj.info.copy() if ticker_obj.info else {}
                    if 'regularMarketPrice' in live_info and live_info['regularMarketPrice']:
                        last_price = float(live_info['regularMarketPrice'])
                        data_source = "yahoo"
                        logger.debug(f"Stock {ticker}: Yahoo fallback price=${last_price:.2f}")
                        get_health_dashboard().register(
                            "yahoo_stocks", QualifiedValue.verified(last_price, "yfinance")
                        )

                        # FIX: Always calculate change manually for consistency (Price - PrevClose)
                        if 'regularMarketPreviousClose' in live_info and live_info['regularMarketPreviousClose']:
                            previous_close = float(live_info['regularMarketPreviousClose'])
                            if previous_close > 0:
                                pct = ((last_price - previous_close) / previous_close) * 100

                        # Fallback to API field if calc not possible
                        elif 'regularMarketChangePercent' in live_info and live_info['regularMarketChangePercent'] is not None:
                            pct = float(live_info['regularMarketChangePercent'])
                except Exception as e:
                    err_str = str(e).lower()
                    if "401" in err_str or "unauthorized" in err_str or "invalid crumb" in err_str:
                        logger.debug(f"Stock {ticker}: Yahoo 401 Unauthorized - API blocked")
                        get_health_dashboard().register(
                            "yahoo_stocks", QualifiedValue.unavailable("yfinance", f"401 Unauthorized for {ticker}")
                        )
                    else:
                        logger.warning(f"Stock {ticker}: Yahoo fallback failed: {e}")
                        get_health_dashboard().register(
                            "yahoo_stocks", QualifiedValue.unavailable("yfinance", f"Error for {ticker}: {e}")
                        )

            # Fallback 2: Finnhub (via shared client — token stays in params, no URL leak)
            if last_price is None:
                try:
                    from core.finnhub_client import get_finnhub_client
                    fh = get_finnhub_client()
                    if fh.is_available():
                        data = fh.get_quote(ticker)
                        if data and 'c' in data and data['c'] > 0:
                            last_price = float(data['c'])
                            data_source = "finnhub"
                            get_health_dashboard().register(
                                "finnhub_stocks", QualifiedValue.estimated(last_price, "finnhub", "Finnhub fallback for stock price")
                            )

                            # Finnhub: c=current, pc=previous_close, dp=percent_change
                            if 'pc' in data and data['pc'] > 0:
                                pc = float(data['pc'])
                                pct = ((last_price - pc) / pc) * 100
                                logger.debug(f"Stock {ticker}: Finnhub price=${last_price:.2f}, calc change={pct:+.2f}%")
                            elif 'dp' in data:
                                pct = float(data['dp'])
                                logger.debug(f"Stock {ticker}: Finnhub price=${last_price:.2f}, API change={pct:+.2f}%")
                except Exception as e:
                    logger.warning(f"Stock {ticker}: Finnhub fallback failed: {e}")
            
            # Fetch historical data for technical analysis (MTF, MACD, RSI)
            # FIX: Use 5d period to ensure enough candles for indicators (MACD slow=26) at market open
            df = self._fetch_market_data(ticker, "1m", "5d")
            if df is not None and not df.empty and "close" in df.columns:
                # Modul 1A - Data Freshness Guard
                is_fresh, data_age = self._validate_data_freshness(df, "1m", ticker)
                if not is_fresh:
                    # Log failure to buffer for UI visibility
                    self.log_buffer.append(f"[{get_bucharest_time().strftime('%H:%M:%S')}] ⚠️ {ticker} blocked: Stale data ({data_age:.0f}s)")
                    # Signals stay blocked; the row itself is still refreshed
                    # (unverified) instead of being frozen at its last value.
                    return self._stale_intraday_stock_result(
                        ticker, now, prev_ticker, last_price, pct, data_source, df, data_age
                    )
                try:
                    raw_technical_price = df["close"].iloc[-1]
                    technical_price = float(raw_technical_price.item() if hasattr(raw_technical_price, "item") else raw_technical_price)
                except Exception as e:
                    logger.error(f"Stock {ticker}: Failed to extract technical close: {e}")
                    return None

                live_quote_price = last_price
                # Fallback to the technical close for watchlist display only.  It
                # is intentionally not sufficient to create or settle a signal.
                if last_price is None:
                    try:
                        last_price = technical_price
                        logger.debug(f"Stock {ticker}: Using df close price=${last_price:.2f}")
                    except Exception as e:
                        logger.error(f"Stock {ticker}: Failed to extract close price from df: {e}")
                        return None
                        
                    # Calculate fallback percentage from first candle if live % not available
                    if pct == 0.0:
                        try:
                            prev_change = prev_ticker.get("change_pct", 0.0)
                            if prev_change is None:
                                prev_change = 0.0
                            pct = float(prev_change)
                        except Exception:
                            pct = 0.0
                
                # Validate price before proceeding
                if last_price is None or last_price <= 0:
                    logger.error(f"Stock {ticker}: Invalid price, skipping...")
                    return None

                integrity_config = self.config.signal.get("price_integrity", {})
                if not isinstance(integrity_config, dict):
                    integrity_config = {}
                price_integrity = validate_price_consistency(
                    live_price=live_quote_price,
                    technical_price=technical_price,
                    asset_type="stock",
                    live_source=data_source,
                    technical_source="yahoo_history",
                    config=integrity_config,
                )
                if not price_integrity["is_verified"]:
                    logger.warning(
                        "Stock %s signal blocked by price integrity: %s (live=%s, technical=%s, deviation=%s%%)",
                        ticker,
                        price_integrity["status"],
                        live_quote_price,
                        technical_price,
                        price_integrity.get("deviation_pct", "n/a"),
                    )
                    self.log_buffer.append(
                        f"[{get_bucharest_time().strftime('%H:%M:%S')}] ⚠️ {ticker} blocked: price-source {price_integrity['status']}"
                    )
                    return {
                        "ticker": ticker,
                        "data": {
                            "price": last_price,
                            "technical_price": technical_price,
                            "change_pct": pct,
                            "last_update": now.isoformat(),
                            "asset_type": "stock",
                            "data_source": data_source,
                            "data_quality": price_integrity["status"],
                            "price_verified": False,
                            "price_integrity": price_integrity,
                        },
                        "signal": None,
                    }

                # Make a copy of df and calculate indicators before passing to _analyze_ticker
                df_copy = df.copy()
                df_copy = self._calculate_indicators(df_copy)
                
                strict_signal = self._analyze_ticker(
                    ticker, df_copy, False, variant_mode="STRICT",
                    data_source=data_source, data_age=data_age
                )
                custom_signal = self._analyze_ticker(
                    ticker, df_copy, False, variant_mode="CUSTOM",
                    data_source=data_source, data_age=data_age
                )
                result = self._choose_signal_with_strict_override(ticker, strict_signal, custom_signal)
                if result:
                    result["price_integrity"] = price_integrity
                    result["price_verified"] = True
                    result["technical_price"] = technical_price
                # FIX: Always compute MTF trends for UI display, not just when there's a signal
                mtf = self._get_mtf_trends(ticker, False)

                # Note: pct is already set from live_info['regularMarketChangePercent']
                # or calculated as fallback above

                with self.mtf_cache_lock:
                    cached_mtf = self.mtf_cache.get(ticker, {})

                # FIX: Preserve trends - use cached value if new calculation returns None or N/A
                def get_valid_trend(new_val, cached_val, default="N/A"):
                    """Get valid trend value, preferring new over cached, but keeping valid cached if new is invalid."""
                    if new_val and new_val != "N/A":
                        return new_val
                    if cached_val and cached_val != "N/A":
                        return cached_val
                    return default

                # Extract RSI and MACD from DataFrame for hover tooltip
                rsi_val = 50.0
                macd_hist_val = 0.0
                try:
                    if "rsi" in df_copy.columns and not df_copy["rsi"].empty:
                        rsi_val = float(df_copy["rsi"].iloc[-1])
                        if pd.isna(rsi_val):
                            rsi_val = 50.0
                    
                    # Hybrid 4H MACD
                    hybrid_4h = mtf.get("hybrid_macd_4h") or cached_mtf.get("hybrid_macd_4h")
                    
                    # Fallback to Daily
                    raw_1d = mtf.get("raw_macd_1d") or cached_mtf.get("raw_macd_1d")
                    raw_4h = mtf.get("raw_macd_4h") or cached_mtf.get("raw_macd_4h")
                    
                    if hybrid_4h is not None:
                        macd_hist_val = float(hybrid_4h)
                        # logger.debug(f"Stock {ticker}: Using Hybrid MACD 4H = {macd_hist_val:.4f}")
                    elif raw_1d is not None:
                        macd_hist_val = float(raw_1d)
                    elif raw_4h is not None:
                         macd_hist_val = float(raw_4h)
                    elif "macd_hist" in df_copy.columns and not df_copy["macd_hist"].empty:
                        macd_hist_val = float(df_copy["macd_hist"].iloc[-1])
                        if pd.isna(macd_hist_val):
                            macd_hist_val = 0.0
                except Exception as e:
                    logger.debug(f"Stock {ticker}: Could not extract RSI/MACD: {e}")

                ticker_data = {
                    "price": last_price,
                    "change_pct": pct,
                    "last_update": now.isoformat(),
                    "asset_type": "stock",
                    "data_source": data_source,
                    "data_quality": "verified",
                    "technical_price": technical_price,
                    "price_verified": True,
                    "price_integrity": price_integrity,
                    "session": "regular",
                    "trend_1d": get_valid_trend(mtf.get("1d"), cached_mtf.get("1d")),
                    "trend_4h": get_valid_trend(mtf.get("4h"), cached_mtf.get("4h")),
                    "trend_1h": get_valid_trend(mtf.get("1h"), cached_mtf.get("1h")),
                    "trend_30m": get_valid_trend(mtf.get("30m"), cached_mtf.get("30m")),
                    "trend_15m": get_valid_trend(mtf.get("15m"), cached_mtf.get("15m")),
                    "trend_5m": get_valid_trend(mtf.get("5m"), cached_mtf.get("5m")),
                    "ts_1d": mtf.get("ts_1d", cached_mtf.get("ts_1d", "")),
                    "ts_4h": mtf.get("ts_4h", cached_mtf.get("ts_4h", "")),
                    "ts_1h": mtf.get("ts_1h", cached_mtf.get("ts_1h", "")),
                    "ts_30m": mtf.get("ts_30m", cached_mtf.get("ts_30m", "")),
                    "ts_15m": mtf.get("ts_15m", cached_mtf.get("ts_15m", "")),
                    "ts_5m": mtf.get("ts_5m", cached_mtf.get("ts_5m", "")),
                    "rsi": rsi_val,
                    "macd_hist": macd_hist_val,
                }

                return {
                    "ticker": ticker,
                    "data": ticker_data,
                    "signal": result
                }

            # No candles at all for this ticker (regular session).
            from core.stock_data_client import is_known_missing as _yahoo_known_missing

            return self._unavailable_stock_result(
                ticker,
                now,
                prev_ticker,
                known_missing=_yahoo_known_missing(normalize_ticker(ticker, provider="yahoo")),
            )
        except Exception as e:
            logger.error(f"Stock {ticker}: {e}")
        return None

    def _process_crypto_ticker(self, pair: str, now: datetime, crypto_price_interval: int) -> Optional[Dict]:
        """Process a single crypto ticker - used for parallel execution."""
        try:
            df = BinanceClient.fetch_klines(pair, "1m", 500)
            if df is not None and not df.empty and "close" in df.columns:
                get_health_dashboard().register(
                    "binance_crypto", QualifiedValue.verified("klines_ok", "binance")
                )
                # Modul 1A - Data Freshness Guard
                is_fresh, data_age = self._validate_data_freshness(df, "1m", pair)
                if not is_fresh:
                    self.log_buffer.append(f"[{get_bucharest_time().strftime('%H:%M:%S')}] ⚠️ {pair} blocked: Stale data ({data_age:.0f}s)")
                    get_health_dashboard().register(
                        "binance_crypto", QualifiedValue.stale("klines_stale", "binance", int(data_age))
                    )
                    return None
                try:
                    raw_technical_price = df["close"].iloc[-1]
                    technical_price = float(raw_technical_price.item() if hasattr(raw_technical_price, "item") else raw_technical_price)
                except Exception as e:
                    logger.error(f"Crypto {pair}: Failed to extract technical close: {e}")
                    return None

                live_price = BinanceClient.get_last_price(pair, crypto_price_interval)
                price = live_price if live_price is not None else technical_price
                price_integrity = validate_price_consistency(
                    live_price=live_price,
                    technical_price=technical_price,
                    asset_type="crypto",
                    live_source="binance_ticker",
                    technical_source="binance_klines",
                    config=self.config.signal.get("price_integrity", {}),
                )
                if not price_integrity["is_verified"]:
                    logger.warning(
                        "Crypto %s signal blocked by price integrity: %s (live=%s, technical=%s, deviation=%s%%)",
                        pair,
                        price_integrity["status"],
                        live_price,
                        technical_price,
                        price_integrity.get("deviation_pct", "n/a"),
                    )
                    self.log_buffer.append(
                        f"[{get_bucharest_time().strftime('%H:%M:%S')}] ⚠️ {pair} blocked: price-source {price_integrity['status']}"
                    )
                    return {
                        "ticker": pair,
                        "data": {
                            "price": price,
                            "technical_price": technical_price,
                            "last_update": now.isoformat(),
                            "asset_type": "crypto",
                            "data_source": "binance",
                            "data_quality": price_integrity["status"],
                            "price_verified": False,
                            "price_integrity": price_integrity,
                        },
                        "signal": None,
                    }

                # Calculate indicators first
                df = self._calculate_indicators(df)
                
                strict_signal = self._analyze_ticker(
                    pair, df, True, variant_mode="STRICT",
                    data_source="binance", data_age=data_age
                )
                custom_signal = self._analyze_ticker(
                    pair, df, True, variant_mode="CUSTOM",
                    data_source="binance", data_age=data_age
                )
                result = self._choose_signal_with_strict_override(pair, strict_signal, custom_signal)
                if result:
                    result["price_integrity"] = price_integrity
                    result["price_verified"] = True
                    result["technical_price"] = technical_price
                # FIX: Always compute MTF trends for UI display, not just when there's a signal
                mtf = self._get_mtf_trends(pair, True)

                pct = BinanceClient.get_pct_24h(pair, 5)

                with self.mtf_cache_lock:
                    cached_mtf = self.mtf_cache.get(pair, {})

                # FIX: Preserve trends - use cached value if new calculation returns None or N/A
                def get_valid_trend(new_val, cached_val, default="N/A"):
                    """Get valid trend value, preferring new over cached, but keeping valid cached if new is invalid."""
                    if new_val and new_val != "N/A":
                        return new_val
                    if cached_val and cached_val != "N/A":
                        return cached_val
                    return default

                # Extract RSI and MACD from DataFrame for hover tooltip
                rsi_val = 50.0
                macd_hist_val = 0.0
                try:
                    if "rsi" in df.columns and len(df) > 0:
                        last_rsi = df["rsi"].iloc[-1]
                        if pd.notna(last_rsi):
                            rsi_val = float(last_rsi)
                            logger.debug(f"Crypto {pair}: RSI={rsi_val:.2f}")
                    
                    # FIX: Prefer Daily MACD from MTF analysis for Watchlist consistency
                    # If 1D raw value is available in MTF cache, use it.
                    # Otherwise fallback to 4H, then dataframe (1m/5m) value.
                    raw_1d = mtf.get("raw_macd_1d") or cached_mtf.get("raw_macd_1d")
                    raw_4h = mtf.get("raw_macd_4h") or cached_mtf.get("raw_macd_4h")
                    
                    if raw_1d is not None:
                        macd_hist_val = float(raw_1d)
                    elif raw_4h is not None:
                         macd_hist_val = float(raw_4h)
                    elif "macd_hist" in df.columns and len(df) > 0:
                        last_macd = df["macd_hist"].iloc[-1]
                        if pd.notna(last_macd):
                            macd_hist_val = float(last_macd)
                            logger.debug(f"Crypto {pair}: MACD_HIST={macd_hist_val:.4f}")
                except Exception as e:
                    logger.warning(f"Crypto {pair}: Could not extract RSI/MACD: {e}")

                ticker_data = {
                    "price": price,
                    "change_pct": pct,
                    "last_update": now.isoformat(),
                    "asset_type": "crypto",
                    "data_source": "binance",
                    "data_quality": "verified",
                    "technical_price": technical_price,
                    "price_verified": True,
                    "price_integrity": price_integrity,
                    "trend_1d": get_valid_trend(mtf.get("1d"), cached_mtf.get("1d")),
                    "trend_4h": get_valid_trend(mtf.get("4h"), cached_mtf.get("4h")),
                    "trend_1h": get_valid_trend(mtf.get("1h"), cached_mtf.get("1h")),
                    "trend_30m": get_valid_trend(mtf.get("30m"), cached_mtf.get("30m")),
                    "trend_15m": get_valid_trend(mtf.get("15m"), cached_mtf.get("15m")),
                    "trend_5m": get_valid_trend(mtf.get("5m"), cached_mtf.get("5m")),
                    "ts_1d": mtf.get("ts_1d", cached_mtf.get("ts_1d", "")),
                    "ts_4h": mtf.get("ts_4h", cached_mtf.get("ts_4h", "")),
                    "ts_1h": mtf.get("ts_1h", cached_mtf.get("ts_1h", "")),
                    "ts_30m": mtf.get("ts_30m", cached_mtf.get("ts_30m", "")),
                    "ts_15m": mtf.get("ts_15m", cached_mtf.get("ts_15m", "")),
                    "ts_5m": mtf.get("ts_5m", cached_mtf.get("ts_5m", "")),
                    "rsi": rsi_val,
                    "macd_hist": macd_hist_val,
                }

                return {
                    "ticker": pair,
                    "data": ticker_data,
                    "signal": result
                }
            else:
                get_health_dashboard().register(
                    "binance_crypto", QualifiedValue.unavailable("binance", f"No kline data for {pair}")
                )
        except Exception as e:
            logger.error(f"Crypto {pair}: {e}")
            get_health_dashboard().register(
                "binance_crypto", QualifiedValue.unavailable("binance", f"Error for {pair}: {e}")
            )
        return None

    def _validate_data_freshness(self, df: pd.DataFrame, interval: str, ticker: str) -> Tuple[bool, float]:
        """
        Validate that the latest data point in the DataFrame is not too old.
        Returns: Tuple (is_fresh: bool, age_seconds: float)
        """
        try:
            if df is None or df.empty:
                return False, 0.0
            
            # Get latest timestamp from index
            latest_ts = df.index[-1]
            if not isinstance(latest_ts, datetime):
                latest_ts = pd.to_datetime(latest_ts)
            
            # Ensure it's offset-aware and in Bucharest time for comparison
            if latest_ts.tzinfo is None:
                latest_ts = pytz.UTC.localize(latest_ts)
            
            latest_ts_local = latest_ts.astimezone(BUCHAREST_TZ)
            now_local = get_bucharest_time()
            
            age_seconds = (now_local - latest_ts_local).total_seconds()
            
            # Define thresholds (Modul 1A requirements)
            thresholds = {
                "1m": 120,    # 2 mins (allowing some lag for US stocks)
                "5m": 360,    # 6 mins
                "15m": 1000,
                "30m": 2000,
                "1h": 4000,
                "4h": 16000,
                "1d": 90000,  # ~25 hours
            }
            
            # Default threshold (max 3 bars or something reasonable)
            threshold = thresholds.get(interval, 600)
            
            is_fresh = age_seconds <= threshold
            
            if not is_fresh:
                logger.warning(f"⚠️ {ticker} ({interval}) data is STALE: {age_seconds:.0f}s old (Threshold: {threshold}s)")
            
            return is_fresh, age_seconds
            
        except Exception as e:
            logger.error(f"Error validating freshness for {ticker}: {e}")
            # Unknown age is not fresh: never trade on data we could not date.
            return False, float("inf")

    # ---- data fetch + indicators ----

    def _fetch_market_data(
        self, ticker: str, interval: str = "1m", period: str = "1d"
    ) -> Optional[pd.DataFrame]:
        """
        Fetch market data using Yahoo Finance as the sole source.

        Args:
            ticker: Ticker symbol
            interval: Interval (1m, 5m, 30m, 1h, etc.)
            period: Period (1d, 5d, 30d, etc.) - relevant for Yahoo

        Returns:
            DataFrame with OHLCV data or None
        """
        # Yahoo Finance is the sole source for market data
        data = self._fetch_from_yahoo(ticker, interval, period)

        if data is not None and not data.empty:
            logger.info(f"✅ Yahoo success for {ticker}: {len(data)} candles")
            return data

        try:
            from core.stock_data_client import is_known_missing as _yahoo_known_missing

            if _yahoo_known_missing(normalize_ticker(ticker, provider="yahoo")):
                # Already reported once by the negative cache; not an outage.
                logger.debug(f"Yahoo has no data for {ticker} (negative cache)")
                return None
        except Exception:
            pass
        logger.error(f"❌ Yahoo failed for {ticker}")
        return None

    def _normalize_for_yahoo(self, ticker: str) -> str:
        """DEPRECATED: Use core.config.normalize_ticker instead."""
        return normalize_ticker(ticker, provider="yahoo")

    def _ensure_yfinance_cache_dir(self) -> None:
        if getattr(self, "_yfinance_cache_ready", False):
            return
        try:
            from core.config import data_path  # configured data folder (MS_DATA_DIR)
            cache_dir = data_path("yfinance_cache")
            cache_dir.mkdir(parents=True, exist_ok=True)
            if hasattr(yf, "set_tz_cache_location"):
                yf.set_tz_cache_location(str(cache_dir))
            self._yfinance_cache_ready = True
        except Exception as e:
            logger.debug(f"yfinance cache setup failed: {e}")
            self._yfinance_cache_ready = True

    def _fetch_from_yahoo(
        self, ticker: str, interval: str = "1m", period: str = "1d"
    ) -> Optional[pd.DataFrame]:
        """Fetch data from Yahoo Finance."""
        self._ensure_yfinance_cache_dir()
        # 1. Check Cache
        cache_key = f"{ticker}_{interval}"
        now = datetime.now()
        
        # Extended TTL Map for Yahoo Fallback (Crucial to prevent rate limits)
        ttl_map = {
            "1min": 60, "1m": 60,
            "5min": 300, "5m": 300,
            "15min": 900, "15m": 900,
            "30min": 1800, "30m": 1800,
            "1hour": 3600, "1h": 3600,
            "4hour": 14400, "4h": 14400,
            "1day": 43200, "1d": 43200  # 12h
        }
        ttl = ttl_map.get(interval, 300)

        with self._yahoo_cache_lock:
            cached = self._yahoo_cache.get(cache_key)
            if cached:
                data, ts = cached
                age = (now - ts).total_seconds()
                if age < ttl:
                    if data is None:
                        # Cool-down active
                        return None 
                    # logger.debug(f"Yahoo cache hit for {ticker} ({age:.1f}s old)")
                    return data.copy()

        try:
            # FIX: Normalize ticker using global utility
            y_ticker = normalize_ticker(ticker, provider="yahoo")
            
            from core.stock_data_client import is_known_missing as _yahoo_known_missing

            if _yahoo_known_missing(y_ticker):
                # Unknown/delisted on Yahoo (learned from an earlier answer):
                # skip the request until the negative-cache entry expires.
                return None

            # FIX: Use lock to prevent yfinance data corruption in parallel execution
            with self.yfinance_lock:
                data = yf.download(
                    y_ticker,
                    period=period,
                    interval=interval,
                    auto_adjust=False,
                    progress=False,
                    threads=False,  # FIX: Disable threading to prevent data corruption in parallel execution
                )
                # Make a copy immediately to avoid shared state issues
                if not data.empty:
                    data = data.copy()
            if data.empty:
                logger.warning(f"Yahoo returned EMPTY data for {ticker} (tried {y_ticker})")
                return None

            # DEBUG: Log data shape before processing
            logger.debug(f"Yahoo {ticker}: received {len(data)} rows, columns={data.columns.tolist()}")

            # FIX: Handle MultiIndex columns from yfinance
            if isinstance(data.columns, pd.MultiIndex):
                data.columns = data.columns.get_level_values(0)

            # FIX: Alternative flatten method if columns are still nested
            try:
                if any(isinstance(col, (tuple, list)) for col in data.columns):
                    logger.warning(f"Yahoo {ticker}: Columns still nested after initial flatten, attempting alternative method")
                    data.columns = [col[0] if isinstance(col, (tuple, list)) else col for col in data.columns]
            except Exception as e:
                logger.warning(f"Yahoo {ticker}: Alternative flatten failed: {e}")

            # Normalize column names to lowercase
            data.columns = [str(col).lower() for col in data.columns]

            # FIX: Deduplicate columns (keep first)
            # This handles cases where yfinance might return multiple 'close' columns or artifacts
            data = data.loc[:, ~data.columns.duplicated()]

            # FIX: Validate that 'close' column exists and contains valid data
            if "close" not in data.columns:
                logger.error(f"Yahoo data for {ticker} missing 'close' column after flatten. Columns: {data.columns.tolist()}")
                return None

            # FIX: Check if 'close' column is still a MultiIndex or contains invalid data
            try:
                # Ensure close is float
                data['close'] = pd.to_numeric(data['close'], errors='coerce')
                
                test_val = data['close'].iloc[-1]
                if isinstance(test_val, pd.Series):
                    logger.error(f"Yahoo {ticker}: 'close' column is still a Series after flatten/dedup - data corruption detected")
                    return None
            except Exception as e:
                logger.error(f"Yahoo {ticker}: Cannot access close data: {e}")
                return None

            # DEBUG: Log last close price from raw data
            if not data.empty:
                try:
                    last_close = data['close'].iloc[-1]
                    if hasattr(last_close, 'item'):
                        last_close = last_close.item()
                    logger.debug(f"Yahoo {ticker}: last close=${float(last_close):.2f}")
                except Exception as e:
                    logger.warning(f"Yahoo {ticker}: Could not log last close price: {e}")
        
            # Remove any NaN rows that might cause issues
            data = data.dropna(subset=["close"])

            if data.empty:
                logger.error(f"Yahoo data for {ticker} is empty after cleaning")
                return None

            # Reset index to ensure clean DatetimeIndex
            if data.index.name is None:
                data.index.name = "Datetime"

            # 2. Save to Cache
            with self._yahoo_cache_lock:
                self._yahoo_cache[cache_key] = (data.copy(), now)

            get_health_dashboard().register(
                "yahoo_history", QualifiedValue.verified("df_ok", "yfinance")
            )
            return data
        except Exception as e:
            err_str = str(e).lower()
            if "rate limit" in err_str or "429" in err_str:
                logger.warning(f"Yahoo Rate Limit hit for {ticker}. Pausing requests for 60s...")
                # FIX: Cache the failure (None) to prevent retrying every 5s and extending the ban
                with self._yahoo_cache_lock:
                    self._yahoo_cache[cache_key] = (None, now)
                    # Default TTL logic checks age, so next call finds None and returns None immediately
                get_health_dashboard().register(
                    "yahoo_history", QualifiedValue.stale("df_rate_limited", "yfinance", 60)
                )
            elif "401" in err_str or "unauthorized" in err_str or "invalid crumb" in err_str:
                # FIX: Yahoo 401 "Invalid Crumb" errors indicate blocking - use longer cooldown
                logger.debug(f"Yahoo 401 Unauthorized for {ticker}. Yahoo is blocking API access. Cooldown 5 minutes.")
                with self._yahoo_cache_lock:
                    # Use 5 minute cooldown for 401 errors (more severe than rate limit)
                    self._yahoo_cache[cache_key] = (None, now)
                get_health_dashboard().register(
                    "yahoo_history", QualifiedValue.unavailable("yfinance", f"401 Unauthorized for {ticker}")
                )
            else:
                logger.error(f"Yahoo error {ticker}: {e}")
                get_health_dashboard().register(
                    "yahoo_history", QualifiedValue.unavailable("yfinance", f"Error for {ticker}: {e}")
                )
            return None

    def _calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        if "close" not in df.columns or df.empty:
            return df
        cfg = self.config.signal
        df["rsi"] = TechnicalAnalysis.calc_rsi(df["close"], cfg["rsi_period"])
        macd, macd_sig, macd_hist = TechnicalAnalysis.calc_macd(
            df["close"],
            cfg["macd_fast"],
            cfg["macd_slow"],
            cfg["macd_signal"],
        )
        df["macd"], df["macd_signal"], df["macd_hist"] = macd, macd_sig, macd_hist
        df["ema_20"] = df["close"].ewm(span=20, adjust=False).mean()
        df["ema_50"] = df["close"].ewm(span=50, adjust=False).mean()
        
        # New Indicators for PRO Logic
        df["adx"] = TechnicalAnalysis.calc_adx(df["high"], df["low"], df["close"], 14)
        df["vol_sma"] = TechnicalAnalysis.calc_sma(df["volume"], 20)
        
        # Debug: log last RSI value
        if "rsi" in df.columns and len(df) > 0:
            last_rsi = df["rsi"].iloc[-1]
            if pd.notna(last_rsi):
                logger.debug(f"_calculate_indicators: RSI calculated = {last_rsi:.2f}")
        
        return df

    
    def _resolve_atr_multipliers(self, trade_mode: str, cfg: Dict) -> Tuple[float, float]:
        mode = (trade_mode or "PULLBACK").upper()
        per_mode = cfg.get("atr_multipliers_by_mode", {}) or {}
        mode_cfg = per_mode.get(mode, {}) if isinstance(per_mode, dict) else {}
        tp_mult = float(mode_cfg.get("tp", cfg.get("atr_multiplier_tp", 2.0)))
        sl_mult = float(mode_cfg.get("sl", cfg.get("atr_multiplier_sl", 1.5)))
        return tp_mult, sl_mult

    def _calculate_exit_levels(
        self,
        entry: float,
        direction: str,
        exit_mode: str,
        atr: float,
        tp_mult: float,
        sl_mult: float,
        tp_pct: float,
        sl_pct: float
    ) -> Tuple[float, float]:
        mode = str(exit_mode or "ATR").upper()
        if mode == "FIXED_PCT":
            if direction == "LONG":
                tp = entry * (1 + tp_pct)
                sl = entry * (1 - sl_pct)
            else:
                tp = entry * (1 - tp_pct)
                sl = entry * (1 + sl_pct)
            return tp, sl

        use_atr = atr if atr and atr > 0 else 0.0
        # Runda-6 D1: when ATR is 0/NaN (calculate_atr warmup, illiquid bar, any
        # NaN), `use_atr=0` -> sl_dist=tp_dist=0 -> sl=entry (ZERO risk -> instant
        # stop-out) and tp=entry. That is degenerate and never tradeable. Mirror
        # the strategy_backtester.py:369 flat-fallback guard: fall back to the
        # FIXED_PCT flat levels (the production exit_mode) so live never emits a
        # zero-risk stop. `not (use_atr > 0)` catches NaN (NaN>0 is False) like L3.
        if not (use_atr > 0):
            if direction == "LONG":
                return entry * (1 + tp_pct), entry * (1 - sl_pct)
            return entry * (1 - tp_pct), entry * (1 + sl_pct)
        sl_dist = use_atr * sl_mult
        tp_dist = use_atr * tp_mult
        min_tp_pct = 0.0
        if self is not None and getattr(self, "config", None) is not None:
            min_tp_pct = float(self.config.signal.get("min_tp_pct", 0.0) or 0.0)
        min_tp_dist = entry * (min_tp_pct / 100.0) if entry > 0 and min_tp_pct > 0 else 0.0
        if direction == "LONG":
            tp = entry + max(tp_dist, min_tp_dist)
            return tp, entry - sl_dist
        tp = entry - max(tp_dist, min_tp_dist)
        return tp, entry + sl_dist

    def _fetch_crypto_history_days(self, ticker: str, interval: str, days: int) -> Optional[pd.DataFrame]:
        try:
            symbol = BinanceClient.to_symbol(ticker)
            end_ts = int(datetime.now().timestamp() * 1000)
            start_ts = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
            params = {"symbol": symbol, "interval": interval, "limit": 1000}
            all_rows = []
            cur = start_ts
            while cur < end_ts:
                params["startTime"] = cur
                params["endTime"] = end_ts
                resp = http_get(
                    f"{BinanceClient.BASE_URL}/klines",
                    provider="binance",
                    params=params,
                    timeout=(2, 6),
                    max_retries=1,
                )
                if resp is None:
                    break
                if resp.status_code != 200:
                    break
                data = resp.json()
                if not data:
                    break
                for k in data:
                    all_rows.append(
                        {
                            "timestamp": datetime.fromtimestamp(k[0] / 1000),
                            "open": float(k[1]),
                            "high": float(k[2]),
                            "low": float(k[3]),
                            "close": float(k[4]),
                            "volume": float(k[5]),
                        }
                    )
                cur = data[-1][0] + 1
                if len(data) < 1000:
                    break

            if not all_rows:
                return None
            df = pd.DataFrame(all_rows)
            return df
        except Exception as e:
            logger.warning(f"Crypto regime fetch failed for {ticker}: {e}")
            return None

    def _get_atr_for_timeframe(
        self,
        ticker: str,
        is_crypto: bool,
        atr_tf: str,
        atr_period: int,
        fallback_atr: float,
        lookback_days: int
    ) -> Tuple[float, str]:
        tf = str(atr_tf or "5m").lower()
        if tf.endswith("min"):
            tf = tf.replace("min", "m")
        if tf in ["1m"]:
            return fallback_atr, "1m"

        df_tf = None
        if is_crypto:
            df_tf = self._fetch_crypto_history_days(ticker, tf, max(1, int(lookback_days)))
        else:
            period = f"{int(lookback_days)}d"
            df_tf = self._fetch_market_data(ticker, tf, period)

        if df_tf is None or df_tf.empty:
            return fallback_atr, "1m"

        if "timestamp" not in df_tf.columns:
            df_tf = df_tf.reset_index().rename(columns={"index": "timestamp"})
        df_tf.columns = [c.lower() for c in df_tf.columns]

        atr_series = calculate_atr(df_tf, period=atr_period).dropna()
        if atr_series.empty:
            return fallback_atr, "1m"
        return float(atr_series.iloc[-1]), tf

    def _get_regime_metrics(
        self,
        ticker: str,
        is_crypto: bool,
        regime_cfg: Dict,
        signal_cfg: Dict
    ) -> Dict[str, float]:
        tf = str(regime_cfg.get("timeframe", "5m")).lower()
        if tf.endswith("min"):
            tf = tf.replace("min", "m")
        lookback_days = int(regime_cfg.get("lookback_days", 20) or 20)
        atr_period = int(signal_cfg.get("atr_period", 14))

        if is_crypto:
            df = self._fetch_crypto_history_days(ticker, tf, lookback_days)
        else:
            period = f"{lookback_days}d"
            df = self._fetch_market_data(ticker, tf, period)

        if df is None or df.empty:
            return {"adx_5m": 0.0, "bb_width_5m": 0.0, "atr_percentile_5m": 0.0}

        if "timestamp" not in df.columns:
            df = df.reset_index().rename(columns={"index": "timestamp"})
        df.columns = [c.lower() for c in df.columns]

        adx_series = TechnicalAnalysis.calc_adx(df["high"], df["low"], df["close"], 14)
        bb_width_series = TechnicalAnalysis.calc_bb_width(df["close"], 20, 2.0)
        atr_series = calculate_atr(df, period=atr_period)

        adx_val = float(adx_series.dropna().iloc[-1]) if not adx_series.dropna().empty else 0.0
        bb_width_val = float(bb_width_series.dropna().iloc[-1]) if not bb_width_series.dropna().empty else 0.0

        atr_series = atr_series.dropna()
        if atr_series.empty:
            atr_pct = 0.0
        else:
            lookback = atr_series.tail(len(atr_series))
            atr_pct = calculate_percentile_rank(lookback.tolist(), float(atr_series.iloc[-1]))

        return {
            "adx_5m": adx_val,
            "bb_width_5m": bb_width_val,
            "atr_percentile_5m": float(atr_pct),
        }

# ---- MTF helpers ----

    def _get_mtf_trends(self, ticker: str, is_crypto: bool) -> Dict[str, str]:
        """
        Get multi-timeframe (MTF) trend analysis for a ticker.

        Analyzes trends across 1h, 30m, and 5m timeframes using MACD histogram.
        Results are cached to improve performance.

        Args:
            ticker: The ticker symbol to analyze
            is_crypto: Whether this is a cryptocurrency (affects data source and cache TTL)

        Returns:
            Dictionary with keys '1h', '30m', '5m' containing trend values:
            'BULL', 'BEAR', or 'N/A' if insufficient data.
        """
        """
        Get multi-timeframe (MTF) trend analysis for a ticker.

        Analyzes trends across 1h, 30m, and 5m timeframes using MACD histogram.
        Results are cached to improve performance.

        Args:
            ticker: The ticker symbol to analyze
            is_crypto: Whether this is a cryptocurrency (affects data source and cache TTL)

        Returns:
            Dictionary with keys '1h', '30m', '5m' containing trend values:
            'BULL', 'BEAR', or 'N/A' if insufficient data.
        """
        now = get_bucharest_time()
        cache_ttl = CACHE_TTL_CRYPTO if is_crypto else CACHE_TTL_STOCK

        with self.mtf_cache_lock:
            if (
                ticker in self.mtf_cache_time
                and (now - self.mtf_cache_time[ticker]).total_seconds() < cache_ttl
            ):
                return self.mtf_cache.get(
                    ticker, {"1d": "N/A", "4h": "N/A", "1h": "N/A", "30m": "N/A", "15m": "N/A", "5m": "N/A"}
                )

        trends = {"1d": "N/A", "4h": "N/A", "1h": "N/A", "30m": "N/A", "15m": "N/A", "5m": "N/A"}
        timestamps = {"ts_1d": "", "ts_4h": "", "ts_1h": "", "ts_30m": "", "ts_15m": "", "ts_5m": ""}
        raw_values = {} # FIX: Store raw values for UI display (e.g., Daily MACD)

        cfg = self.config.signal
        
        # FIX: Load existing cache to preserve trends that fail to update
        with self.mtf_cache_lock:
            existing_cache = self.mtf_cache.get(ticker, {})
            existing_frames = dict(getattr(self, "mtf_frame_cache", {}).get(ticker, {}))

        timeframe_frames: Dict[str, pd.DataFrame] = existing_frames

        timeframes = (
            [("1d", 100), ("4h", 100), ("1h", 100), ("30m", 100), ("15m", 150), ("5m", 200)]
            if is_crypto
            else [("1d", "1y"), ("4h", "60d"), ("1h", "1mo"), ("30m", "5d"), ("15m", "5d"), ("5m", "5d")]
        )

        for tf_data in timeframes:
            try:
                if is_crypto:
                    tf, limit = tf_data
                    df = BinanceClient.fetch_klines(ticker, tf, limit)
                else:
                    tf, period = tf_data
                    # FIX: Use new generic fetch method
                    fetch_interval = "1h" if tf == "4h" else tf
                    df = self._fetch_market_data(ticker, fetch_interval, period)
                    if tf == "4h" and isinstance(df, pd.DataFrame) and not df.empty:
                        df = df.resample("4h").agg({
                            "open": "first", "high": "max", "low": "min",
                            "close": "last", "volume": "sum",
                        }).dropna(subset=["open", "high", "low", "close"])
                if isinstance(df, pd.DataFrame) and not df.empty:
                    timeframe_frames[tf] = df.tail(350).copy()
                if df is not None and len(df) >= MIN_DATA_FOR_MTF:
                    _, _, macd_hist = TechnicalAnalysis.calc_macd(
                        df["close"],
                        cfg["macd_fast"],
                        cfg["macd_slow"],
                        cfg["macd_signal"],
                    )
                    
                    # Store raw value
                    if not macd_hist.empty:
                        last_val = macd_hist.iloc[-1]
                        if pd.notna(last_val):
                            raw_values[f"raw_macd_{tf}"] = float(last_val)

                    new_trend = TechnicalAnalysis.get_trend_from_macd(macd_hist)
                    # FIX: Only update if we got a valid trend (not N/A)
                    if new_trend and new_trend != "N/A":
                        trends[tf] = new_trend
                        timestamps[f"ts_{tf}"] = format_bucharest_time()
                    else:
                        # Keep existing cached value if available
                        if existing_cache.get(tf) and existing_cache.get(tf) != "N/A":
                            trends[tf] = existing_cache[tf]
                            timestamps[f"ts_{tf}"] = existing_cache.get(f"ts_{tf}", "")
                            # Also keep raw value
                            if f"raw_macd_{tf}" in existing_cache:
                                raw_values[f"raw_macd_{tf}"] = existing_cache[f"raw_macd_{tf}"]
                else:
                    # Insufficient data - keep existing cached value if available
                    logger.warning(f"MTF {ticker} {tf}: Insufficient data ({len(df) if df is not None else 0} < {MIN_DATA_FOR_MTF})")
                    if existing_cache.get(tf) and existing_cache.get(tf) != "N/A":
                        trends[tf] = existing_cache[tf]
                        timestamps[f"ts_{tf}"] = existing_cache.get(f"ts_{tf}", "")
                    if f"raw_macd_{tf}" in existing_cache:
                        raw_values[f"raw_macd_{tf}"] = existing_cache[f"raw_macd_{tf}"]
                
                # FIX: Use Yahoo Finance / StockDataClient hybrid MACD
                if not is_crypto and tf == "4h":
                    try:
                        hybrid_val = StockDataClient().get_hybrid_macd(ticker, "4hour")
                        if hybrid_val is not None:
                            raw_values["hybrid_macd_4h"] = hybrid_val
                        elif "hybrid_macd_4h" in existing_cache:
                             raw_values["hybrid_macd_4h"] = existing_cache["hybrid_macd_4h"]
                    except Exception as e:
                        logger.warning(f"MTF {ticker} Hybrid MACD failed: {e}")
                        if "hybrid_macd_4h" in existing_cache:
                             raw_values["hybrid_macd_4h"] = existing_cache["hybrid_macd_4h"]

            except Exception as e:
                logger.warning(f"MTF {ticker} {tf}: {e}")
                # Keep existing cached value on error
                if existing_cache.get(tf) and existing_cache.get(tf) != "N/A":
                    trends[tf] = existing_cache[tf]
                    timestamps[f"ts_{tf}"] = existing_cache.get(f"ts_{tf}", "")
                if f"raw_macd_{tf}" in existing_cache:
                    raw_values[f"raw_macd_{tf}"] = existing_cache[f"raw_macd_{tf}"]

        with self.mtf_cache_lock:
            # Merge everything
            full_data = {**trends, **timestamps, **raw_values}
            self.mtf_cache[ticker] = full_data
            self.mtf_cache_time[ticker] = now
            if not hasattr(self, "mtf_frame_cache"):
                self.mtf_frame_cache = {}
            self.mtf_frame_cache[ticker] = timeframe_frames
            
        return full_data

    def _get_s8_timeframe_frames(self, ticker: str) -> Dict[str, pd.DataFrame]:
        """Return immutable copies of the 30m/1h/4h frames fetched by MTF."""
        with self.mtf_cache_lock:
            cached = dict(getattr(self, "mtf_frame_cache", {}).get(ticker, {}))
        return {
            timeframe: frame.copy()
            for timeframe in ("30m", "1h", "4h")
            for frame in [cached.get(timeframe)]
            if isinstance(frame, pd.DataFrame) and not frame.empty
        }

    def _calculate_mtf_score(self, signal_type: str, mtf_trends: Dict[str, str]) -> int:
        expected = "BULL" if signal_type == "BUY" else "BEAR"
        # Include 4h and 1d but keep max score somewhat consistent or balanced
        score = 0
        if mtf_trends.get("1d") == expected: score += 2
        if mtf_trends.get("4h") == expected: score += 2
        if mtf_trends.get("1h") == expected: score += 2
        if mtf_trends.get("30m") == expected: score += 2
        if mtf_trends.get("5m") == expected: score += 2
        
        # Max score is now 10 (was 6). This might affect thresholds.
        # To maintain compatibility with existing config (variants A/B/C use score > 6, > 4 etc causes issues if score range changes significantly)
        # However, more confirmations = better signal. We can cap it or just let it be higher.
        # With 10 max points, thresholds should ideally be adjusted, but even with old thresholds (6, 4, 0),
        # having more confirming timeframes will just make it easier to reach "A" quality, which is fine.
        
        return score

    def _calculate_quality_score(self, ticker_data: dict, variant: str) -> int:
        """
        Calculate quality score (0-10) based on indicator convergence.

        Factors:
        - MTF Score (trend alignment)
        - RSI (oversold/overbought depth)
        - MACD (histogram strength)
        """
        score = 0
        
        # 1. MTF Components (max 5 pts)
        # Simplified: map full MTF score (0-10) to 0-5 range
        mtf_score = ticker_data.get("mtf_score", 0)
        score += min(5, mtf_score // 2)
        
        # 2. RSI Strength (max 3 pts)
        rsi = ticker_data.get("rsi", 50)
        signal_type = ticker_data.get("type", "")
        
        if signal_type == "BUY":
            if rsi < 30: score += 3
            elif rsi < 35: score += 2
            elif rsi < 40: score += 1
        elif signal_type == "SHORT":
            if rsi > 70: score += 3
            elif rsi > 65: score += 2
            elif rsi > 60: score += 1
            
        # 3. MACD Strength (max 2 pts)
        hist = abs(ticker_data.get("macd_hist", 0))
        if hist > 0.05: score += 2
        elif hist > 0.02: score += 1
        
        return min(10, score)

    def _calculate_variant(
        self, macd_hist: float, signal_type: str, mtf_score: int, cfg: dict
    ) -> Optional[str]:
        """
        Determine signal variant (A, B, C) based on strength and MTF.

        A = Strong (High prob, requires strong trend alignment)
        B = Medium (Good prob, requires some trend alignment)
        C = Weak/Reversal (Lower prob, weak or no alignment)
        """
        # A variant requirements
        if mtf_score >= cfg["variant_a_mtf_min"]:
            if signal_type == "BUY" and macd_hist >= cfg["variant_a_macd_hist_min_pos"]:
                return "A"
            if signal_type == "SHORT" and macd_hist <= cfg["variant_a_macd_hist_min_neg"]:
                return "A"

        # B variant requirements
        if mtf_score >= cfg["variant_b_mtf_min"]:
            if signal_type == "BUY" and macd_hist >= cfg["variant_b_macd_hist_min_pos"]:
                return "B"
            if signal_type == "SHORT" and macd_hist <= cfg["variant_b_macd_hist_min_neg"]:
                return "B"

        # C variant requirements
        # Use simple threshold if configured
        if cfg.get("require_macd_hist_threshold", True):
            require_hist = True
        else:
            require_hist = False

        # C
        if cfg.get("block_weak_c_signals", True) and mtf_score < cfg.get(
            "weak_c_mtf_threshold", 2
        ):
            return None
        if require_hist:
            if signal_type == "BUY" and macd_hist < cfg.get(
                "variant_c_macd_hist_min_pos", 0.0001
            ):
                return None
            if signal_type == "SHORT" and macd_hist > cfg.get(
                "variant_c_macd_hist_min_neg", -0.0001
            ):
                return None
        return "C"

    @staticmethod
    def _parse_mtf_alignment_count(mtf_alignment: Optional[str]) -> int:
        try:
            raw = str(mtf_alignment or "").strip()
            if "/" in raw:
                return int(raw.split("/", 1)[0])
            return int(raw) if raw else 0
        except Exception:
            return 0

    @staticmethod
    def _compute_rr_ratio(entry: float, tp: float, sl: float) -> float:
        try:
            reward = abs(float(tp) - float(entry))
            risk = abs(float(entry) - float(sl))
            if risk <= 0:
                return 0.0
            return reward / risk
        except Exception:
            return 0.0

    @staticmethod
    def _compute_regime_strength(regime: str, regime_details: Optional[Dict]) -> float:
        details = regime_details or {}
        try:
            adx = float(details.get("adx", 0) or 0)
        except Exception:
            adx = 0.0
        try:
            atr_pct = float(details.get("atr_percentile", 0) or 0)
        except Exception:
            atr_pct = 0.0
        try:
            mtf_consensus = str(details.get("mtf_consensus", "0/6"))
            mtf_n = float((mtf_consensus.split("/", 1)[0] or 0))
        except Exception:
            mtf_n = 0.0

        regime_norm = str(regime or "MIXED").upper()
        if regime_norm == "TREND":
            raw = (adx * 1.8) + (mtf_n * 8.0)
        elif regime_norm == "RANGE":
            raw = 45.0 + (mtf_n * 6.0)
        elif regime_norm == "VOLATILE":
            raw = 40.0 + (atr_pct * 0.6)
        elif regime_norm == "LOW_VOL":
            raw = max(5.0, 25.0 - atr_pct * 0.5)
        else:
            raw = (adx * 1.1) + (mtf_n * 5.0) + (atr_pct * 0.2)
        return round(max(0.0, min(100.0, raw)), 2)

    def _choose_signal_with_strict_override(
        self,
        ticker: str,
        strict_signal: Optional[Dict],
        custom_signal: Optional[Dict],
    ) -> Optional[Dict]:
        if strict_signal is None and custom_signal is None:
            return None
        if strict_signal is None:
            custom_signal["strict_disagreement"] = False
            return custom_signal
        if custom_signal is None:
            strict_signal["strict_disagreement"] = False
            return strict_signal

        strict_score = float(strict_signal.get("signal_score", 0) or 0)
        custom_score = float(custom_signal.get("signal_score", 0) or 0)
        strict_mtf = self._parse_mtf_alignment_count(strict_signal.get("mtf_alignment"))
        strict_mode = str(strict_signal.get("signal_mode", "")).upper()
        custom_mode = str(custom_signal.get("signal_mode", "")).upper()
        strict_action = str(strict_signal.get("type", "")).upper()
        custom_action = str(custom_signal.get("type", "")).upper()
        disagreement = (strict_mode != custom_mode) or (strict_action != custom_action)

        can_override = (
            strict_score >= (custom_score + STRICT_OVERRIDE_SCORE_DELTA)
            and strict_mtf >= STRICT_OVERRIDE_MIN_MTF
        )
        if can_override:
            strict_signal["strict_disagreement"] = disagreement
            strict_signal["strict_override_applied"] = True
            strict_signal["strict_override_reason"] = (
                f"strict_score {strict_score:.1f} >= custom_score {custom_score:.1f} + "
                f"{STRICT_OVERRIDE_SCORE_DELTA:.0f} and MTF {strict_mtf}/6 >= {STRICT_OVERRIDE_MIN_MTF}/6"
            )
            return strict_signal

        custom_signal["strict_disagreement"] = disagreement
        custom_signal["strict_override_applied"] = False
        custom_signal["strict_candidate"] = {
            "score": round(strict_score, 2),
            "mtf_alignment": strict_signal.get("mtf_alignment", ""),
            "mode": strict_mode,
            "action": strict_action,
        }
        logger.info(
            f"STRICT override skipped for {ticker}: strict={strict_score:.1f}, custom={custom_score:.1f}, mtf={strict_mtf}/6"
        )
        return custom_signal

    # ---- ticker analysis + signals (NEW: scoring system 0-100) ----

    def _analyze_ticker(
        self,
        ticker: str,
        df: pd.DataFrame,
        is_crypto: bool = False,
        variant_mode: Optional[str] = None,
        signal_cfg: Optional[Dict] = None,
        data_source: str = "unknown",
        data_age: float = 0.0
    ) -> Optional[dict]:
        """
        Analyze price data for a ticker and generate trading signals.

        NEW SYSTEM: Uses scoring 0-100 with 3 modes (PULLBACK, BREAKOUT, CONTRA)
        and risk profiles (STRONG >= 85, MEDIUM >= 70, RELAX >= 55).

        All decisions are made on the 1-minute (1m) timeframe.
        Higher timeframes (5m, 30m, 1h) are used only for MTF context.

        Args:
            ticker: Ticker symbol
            df: DataFrame with OHLCV data
            is_crypto: True if cryptocurrency

        Returns:
            Dict with signal details or None if no valid signal exists
        """
        # Validate ticker
        if not ticker or not isinstance(ticker, str):
            logger.warning(f"Invalid ticker format: {ticker}")
            return None

        ticker = ticker.strip().upper()
        if not ticker:
            logger.warning("Ticker empty after strip")
            return None

        # Validate ticker format
        if is_crypto:
            if not (ticker.endswith("-USD") or ticker.endswith("-USDC") or ticker.endswith("USDT")):
                logger.warning(f"Format crypto ticker invalid: {ticker}")
                return None
        else:
            # FIX: Allow forward slash for Forex (e.g. EUR/USD) and equals for Futures (e.g. GC=F)
            safe_ticker = ticker.replace(".", "").replace("-", "").replace("/", "").replace("=", "")
            if not safe_ticker.isalnum():
                logger.warning(f"Format stock ticker invalid: {ticker}")
                return None

        # Build variant-specific signal config early because global guards depend on it.
        if signal_cfg is None:
            signal_cfg = self._get_variant_signal_cfg(variant_mode)
        cfg = signal_cfg

        # === [NEW] Check if signal generation is enabled for this ticker ===
        ticker_settings = self.config.get_ticker_settings(ticker)
        signals_enabled = ticker_settings.get("signals_enabled", True)  # Default to True
        if not signals_enabled:
            logger.info(f"🚫 Signal generation disabled for {ticker}")
            return None

        # === [NEW] Check global signal enable/disable ===
        global_signals_enabled = bool(cfg.get("signals_enabled", True))
        if not global_signals_enabled:
            logger.info("🚫 Global signal generation disabled")
            return None

        # Verify minimum data
        if df is None or df.empty or len(df) < MIN_DATA_FOR_ANALYSIS:
            return None

        # Calculate indicators pe 1m
        df = self._calculate_indicators(df)

        # Calculate VWAP if it doesn't exist
        if "vwap" not in df.columns:
            df["vwap"] = calculate_vwap(df)

        variant_mode = (cfg.get("variant_mode") or variant_mode or "STRICT").upper()

        # Calculate ATR
        df["atr"] = calculate_atr(df, period=int(cfg.get("atr_period", 14)))

        # Get MTF trends for context
        mtf_trends = self._get_mtf_trends(ticker, is_crypto)

        # Get profile preference from config
        regime_cfg = cfg.get("regime", {}) or {}
        regime_metrics = self._get_regime_metrics(ticker, is_crypto, regime_cfg, cfg)
        adx_5m = float(regime_metrics.get("adx_5m", 0.0) or 0.0)
        bb_width_5m = float(regime_metrics.get("bb_width_5m", 0.0) or 0.0)
        atr_percentile_5m = float(regime_metrics.get("atr_percentile_5m", 0.0) or 0.0)
        preferred_profile = cfg.get("risk_mode", "MEDIUM").upper()

        # Validate profile
        if preferred_profile not in SCORE_THRESHOLDS:
            logger.warning(f"Invalid profile '{preferred_profile}', using MEDIUM")
            preferred_profile = "MEDIUM"

        # Extract data from the last 1m candle
        last_row = df.iloc[-1]
        close = float(last_row["close"])

        # Calculate distances in ATR
        vwap = float(last_row.get("vwap", close))
        ema20 = float(last_row.get("ema_20", close))
        atr = float(last_row.get("atr", 0.01))
        avg_atr_20d = float(df["atr"].tail(20).mean()) if ("atr" in df.columns and len(df) >= 20) else atr
        spread_estimate = abs(float(last_row.get("high", close)) - float(last_row.get("low", close)))

        if atr <= 0:
            atr = 0.01  # Fallback to avoid division by zero

        dist_vwap_atr = calculate_distance_in_atr(close, vwap, atr)
        dist_ema_atr = calculate_distance_in_atr(close, ema20, atr)

        # Calculate 1m trend (based on current MACD hist)
        trend_1m = "BULL" if last_row.get("macd_hist", 0) > 0 else "BEAR"

        # Create context for scoring
        row_context = RowContext(
            open=float(last_row["open"]),
            high=float(last_row["high"]),
            low=float(last_row["low"]),
            close=close,
            volume=float(last_row.get("volume", 0)),
            rsi=float(last_row.get("rsi", 50)),
            macd=float(last_row.get("macd", 0)),
            macd_signal=float(last_row.get("macd_signal", 0)),
            macd_hist=float(last_row.get("macd_hist", 0)),
            ema20=ema20,
            vwap=vwap,
            atr=atr,
            adx=float(last_row.get("adx", 0)),
            vol_sma=float(last_row.get("vol_sma", 0)),
            adx_5m=adx_5m,
            bb_width_5m=bb_width_5m,
            atr_percentile_5m=atr_percentile_5m,
            market_regime="MIXED",
            trend_1d=mtf_trends.get("1d", "N/A"),
            trend_4h=mtf_trends.get("4h", "N/A"),
            trend_1h=mtf_trends.get("1h", "N/A"),
            trend_30m=mtf_trends.get("30m", "N/A"),
            trend_15m=mtf_trends.get("15m", "N/A"),
            trend_5m=mtf_trends.get("5m", "N/A"),
            trend_1m=trend_1m,
            dist_vwap_atr=dist_vwap_atr,
            dist_ema_atr=dist_ema_atr,
            cooldown_active=False,
            cooldown_bars_remaining=0
        )

        market_regime, regime_details = detect_market_regime(row_context, regime_cfg)
        row_context.market_regime = market_regime
        regime_strength = self._compute_regime_strength(market_regime, regime_details)


        # Check cooldown
        # Variant-specific cooldown tracking so STRICT and CUSTOM can run in parallel
        last_signal_key = f"{ticker}_signal_{variant_mode}"
        last_signal_idx = self.last_signal_indices.get(last_signal_key, -999)
        current_idx = len(df) - 1
        min_candles_between = cfg.get("min_candles_between_signals", 20)

        if last_signal_idx >= 0 and (current_idx - last_signal_idx) < min_candles_between:
            # Still in cooldown
            return None

        # === V2 Strategy Integration ===
        # Try V2 strategy selector first. If V2 produces a high-quality candidate, use it.
        # Otherwise, fall back to V1 scoring below.
        v2_candidate = None
        # V2 exit-level locals must be initialised here (not only inside the
        # `if v2_candidate is not None:` block below) because _apply_v2_override
        # is called UNCONDITIONALLY before the return (~line 2762). Python
        # evaluates the call arguments before entering the function, so if these
        # were only assigned inside the not-None branch, every bar where V2
        # produces no qualifying candidate (V1 mode, or BOTH_AUTO_BEST with no
        # >=min_edge_score candidate — the common case) would raise
        # UnboundLocalError on the argument. That exception is swallowed by the
        # caller's broad `except Exception -> return None`, silently dropping
        # EVERY V1-valid signal (the engine emitted zero/near-zero signals).
        # _apply_v2_override early-returns unchanged when v2_candidate is None,
        # so these defaults are never used to mutate the signal.
        v2_tp = 0.0
        v2_sl = 0.0
        v2_rr = 0.0
        v2_tp_pct = 0.0
        v2_sl_pct = 0.0
        v2_research_candidate = False
        bot_cfg = self.config.bot if hasattr(self.config, "bot") else {}
        min_edge_score = int(bot_cfg.get("min_edge_score", 75))
        signal_mode_v2 = str(self.config.strategy_v2.get("signal_mode", "V1")).upper() if hasattr(self.config, "strategy_v2") else "V1"
        strategy_v2_cfg = self.config.strategy_v2 if hasattr(self.config, "strategy_v2") else {}
        research_min_edge_score = float((strategy_v2_cfg or {}).get("research_signal_min_score", 50.0) or 50.0)

        # === Unconditional news pulse (directive: "integra AI-ul in decizii
        # prin analiza news") ===
        # Fetched once per ticker-bar for BOTH purposes:
        #   (1) V2 catalyst_score (via watch_row -> context_builder -> evaluators
        #       _news_catalyst_score, 15% of V2 total_score);
        #   (2) the AI decision layer -- ollama_decision._compact_signal
        #       whitelists news_sentiment/news_score/news_headlines/top_headlines
        #       and the review prompt instructs the model to "use
        #       news_headlines/news_sentiment as part of the decision when
        #       present". Pre-fix the pulse was fetched ONLY inside the V2
        #       block and NEVER attached to the returned signal, so V1 signals
        #       carried no news and the AI received zero news context for any
        #       signal. Now it is attached to `result` below for both V1 & V2.
        # build_news_pulse has a module-level 45s TTL cache -> cheap per bar;
        # allow_blocking=False keeps the engine loop non-blocking.
        pulse = {"score": 0, "sentiment_trend": "unknown", "top_headlines": []}
        try:
            from core.news_pulse import build_news_pulse
            _pulse = build_news_pulse(ticker, allow_blocking=False)
            if isinstance(_pulse, dict):
                pulse = _pulse
        except Exception as exc:
            logger.debug(f"news_pulse unavailable for {ticker}: {exc}")
        news_pulse_score = float(pulse.get("score") or 0.0)
        news_sentiment_trend = str(pulse.get("sentiment_trend") or "neutral").lower()
        _news_top = pulse.get("top_headlines") or []
        news_top_headlines = list(_news_top)[:3]
        news_headlines = [h.get("title", "") for h in news_top_headlines if isinstance(h, dict)]

        if signal_mode_v2 in ("V2", "BOTH", "BOTH_AUTO_BEST", "V2_AUTO"):
            try:
                from core.strategy_v2.context_builder import build_context as v2_build_context
                from core.strategy_v2.selector import StrategySelector
                from core.strategy_v2.context_builder import infer_asset_class

                # Keep futures/commodities in their own V2 strategy and cost
                # bucket.  Treating every non-crypto ticker as US_STOCKS made
                # commodity-specific strategies unreachable in the live scan.
                asset_class = infer_asset_class(ticker)
                watch_row = {"price": close, "change_pct": 0.0, "volume": float(last_row.get("volume", 0))}
                # Reuse the unconditional pulse above to feed V2 catalyst scoring
                # (15% of V2 total_score via _news_catalyst_score).
                watch_row["news_sentiment_trend"] = news_sentiment_trend
                watch_row["news_pulse_score"] = news_pulse_score
                # Enrich with data from engine's state if available
                try:
                    snapshot = self.signals.state_store.get_json("watchlist_snapshot", None) if hasattr(self.signals, "state_store") else None
                    if isinstance(snapshot, dict):
                        for k, v in snapshot.get(ticker, {}).items():
                            if isinstance(v, (int, float, str, bool)):
                                watch_row[k] = v
                except Exception:
                    pass
                # Derive event proximity (hours_to_event) from the
                # snapshot-enriched earnings fields (cache-first: reuses the
                # watchlist_snapshot already loaded above -- no new API call,
                # per CLAUDE.md §5). Mirrors actions_service._hours_to_event_from_row.
                # build_context reads hours_to_event from its PARAMETER (default
                # 999.0), not from watch_row, so it MUST be passed explicitly --
                # pre-fix it was omitted -> event_risk=False always -> the V2
                # event_block quality gate was inert for every pre-earnings ticker.
                # 999.0 == no upcoming event (safe fallback preserved).
                hours_to_event = 999.0
                try:
                    _ed = int(watch_row.get("earnings_days", -1))
                except Exception:
                    _ed = -1
                if _ed >= 0:
                    _when = str(watch_row.get("earnings_when") or "").upper()
                    if _when in {"BMO", "BEFORE_OPEN", "BEFORE MARKET OPEN"}:
                        _offset = 2.0
                    elif _when in {"AMC", "AFTER_CLOSE", "AFTER MARKET CLOSE"}:
                        _offset = 10.0
                    else:
                        _offset = 0.0
                    hours_to_event = max(0.0, float(_ed) * 24.0 + _offset)
                v2_context = v2_build_context(
                    symbol=ticker,
                    asset_class=asset_class,
                    watch_row=watch_row,
                    ohlc_df=df,
                    row_context=row_context,
                    hours_to_event=float(hours_to_event),
                    multi_timeframe_frames=self._get_s8_timeframe_frames(ticker),
                )

                if not hasattr(self, "_v2_selector"):
                    self._v2_selector = StrategySelector(config=self.config.strategy_v2)

                v2_result = self._v2_selector.select(ticker, asset_class, v2_context)

                if v2_result and v2_result.get("best_candidate"):
                    bc = v2_result["best_candidate"]
                    v2_score = float(bc.get("total_score", 0))
                    if v2_score >= min_edge_score:
                        v2_candidate = self._build_v2_candidate_dict(bc, close, v2_score)
                if v2_candidate is None and v2_result:
                    research = v2_result.get("research_candidates", [])
                    if isinstance(research, list):
                        eligible = [
                            item for item in research
                            if isinstance(item, dict)
                            and bool((item.get("meta") or {}).get("research_only"))
                            and float(item.get("total_score", 0.0) or 0.0) >= research_min_edge_score
                        ]
                        if eligible:
                            bc = max(eligible, key=lambda item: float(item.get("total_score", 0.0) or 0.0))
                            v2_score = float(bc.get("total_score", 0.0) or 0.0)
                            v2_candidate = self._build_v2_candidate_dict(
                                bc,
                                close,
                                v2_score,
                                research_signal_threshold=research_min_edge_score,
                            )
                            v2_research_candidate = True
            except Exception as e:
                logger.debug(f"V2 strategy evaluation failed for {ticker}: {e}")

        # "V2" is a quality mode, not a scoring preference.  Falling back to
        # V1 here silently reintroduced unvalidated setups into the Signals tab
        # (and wasted execution review cycles) whenever no V2 candidate passed
        # its OOS performance gate.  In V2-only mode, no validated edge means
        # no signal; BOTH modes remain available for research comparison.
        if signal_mode_v2 in {"V2", "V2_ONLY", "V2_AUTO"} and v2_candidate is None:
            return None

        # If V2 produced a valid candidate, build the signal from it
        if v2_candidate is not None:
            v2_tp = v2_candidate["tp"]
            v2_sl = v2_candidate["sl"]
            if v2_tp <= 0 or v2_sl <= 0:
                # V2 didn't compute exit levels; compute them using engine's ATR method
                v2_exit_atr, _ = self._get_atr_for_timeframe(
                    ticker, is_crypto,
                    str(cfg.get("atr_timeframe", "5m")),
                    int(cfg.get("atr_period", 14)),
                    atr,
                    cfg.get("regime", {}).get("lookback_days", 20)
                )
                tp_mult, sl_mult = self._resolve_atr_multipliers("PULLBACK", cfg)
                v2_tp, v2_sl = self._calculate_exit_levels(
                    close, v2_candidate["direction"],
                    str(cfg.get("exit_mode", "ATR")).upper(),
                    v2_exit_atr if v2_exit_atr and v2_exit_atr > 0 else atr,
                    tp_mult, sl_mult,
                    cfg.get("profit_target_pct", 5.0) / 100,
                    cfg.get("stop_loss_pct", 2.0) / 100,
                )

            v2_rr = self._compute_rr_ratio(close, v2_tp, v2_sl) if v2_tp > 0 and v2_sl > 0 else 0.0
            v2_tp_pct = abs((v2_tp - close) / close * 100) if v2_tp > 0 else 0.0
            v2_sl_pct = abs((v2_sl - close) / close * 100) if v2_sl > 0 else 0.0

        # === End V2 Strategy Integration ===

        # Search order for modes (prioritize PULLBACK > BREAKOUT > CONTRA)
        modes = ["PULLBACK", "BREAKOUT", "CONTRA"]
        if cfg.get("enable_regime_gating", True):
            allowed = allowed_modes_for_regime(market_regime, regime_cfg)
            if not allowed:
                logger.info(f"Regime LOW_VOL for {ticker} - signal blocked")
                return None
            modes = [m for m in modes if m in allowed]

        # Search order for profiles based on preference.
        # PRO is a real profile (SCORE_THRESHOLDS["PRO"]=75, scoring_system.py:24-29,
        # with its own MTF/volume/ADX gates at :507-518 / :1192-1208). Pre-fix PRO was
        # never inserted into any `profiles` list, so a config of risk_mode:"PRO" was
        # silently routed to the else-branch (MEDIUM/STRONG/RELAX) -- configured-but-
        # unreachable dead config. Honor it by leading with PRO when preferred.
        if preferred_profile == "PRO":
            profiles = ["PRO", "STRONG", "MEDIUM", "RELAX"]
        elif preferred_profile == "STRONG":
            profiles = ["STRONG", "MEDIUM", "RELAX"]
        elif preferred_profile == "RELAX":
            profiles = ["RELAX", "MEDIUM", "STRONG"]
        else:
            profiles = ["MEDIUM", "STRONG", "RELAX"]

        # Search for the best valid signal
        best_signal = None
        best_signal_score = 0

        for mode in modes:
            for profile in profiles:
                result = compute_signal_score(
                    row_context, mode, profile, cfg.get("scoring"),
                    data_source=data_source,
                    data_age_seconds=data_age
                )

                if result.is_valid and result.total_score > best_signal_score:
                    from core.data_guard import get_data_guard
                    from core.data_quality import QualifiedValue, QualityLevel
                    from datetime import datetime, timezone
                    
                    dg = get_data_guard()
                    # Process-aware first: a source this process does not
                    # fetch (news in the engine subprocess) is not "stale".
                    is_stale = self._data_guard_state()[1] and bool(dg.is_global_refresh_blocked())
                    
                    if is_stale:
                        quality = QualifiedValue(result.total_score, QualityLevel.STALE, "scoring_system", datetime.now(timezone.utc), "Data is stale")
                    else:
                        quality = QualifiedValue.verified(result.total_score, "scoring_system")

                    best_signal_score = result.total_score
                    best_signal = {
                        "mode": mode,
                        "profile": profile,
                        "direction": result.direction,
                        "signal_score": result.total_score,
                        "signal_quality": quality.to_dict(),
                        "probability_score": result.probability_score,
                        "raw_logit_value": result.raw_logit_value,
                        "mtf_alignment": f"{result.mtf_alignment_count}/{result.mtf_alignment_total}",
                        "mtf_alignment_count": int(result.mtf_alignment_count),
                        "mtf_alignment_total": int(result.mtf_alignment_total),
                        "threshold": SCORE_THRESHOLDS[profile],
                        "details": result.details,
                        "mtf_score": result.mtf_score,
                        "rsi_score": result.rsi_score,
                        "vwap_score": result.vwap_score,
                        "ema_score": result.ema_score,
                        "macd_score": result.macd_score,
                        "price_action_score": result.price_action_score,
                        "signal_thesis": result.signal_thesis,
                        "timeframe": result.timeframe,
                        "expected_holding_period": result.expected_holding_period,
                        "score_breakdown": {
                            "mtf": result.mtf_score,
                            "macd": result.macd_score,
                            "rsi": result.rsi_score,
                            "vwap": result.vwap_score,
                            "ema": result.ema_score,
                        },
                    }

        if best_signal is None:
            # V2 is evaluated before V1 and has its own setup, risk and
            # validation gates.  Requiring a second, unrelated V1 setup in the
            # same bar made an otherwise valid V2 candidate disappear.  That
            # not only blocked legitimate V2 research signals, it also meant a
            # faithful V2 replay could not reproduce the engine.  Build the
            # minimal common envelope here; _apply_v2_override below supplies
            # the V2 direction, TP/SL, score and performance provenance.
            if v2_candidate is None:
                return None
            best_signal_score = float(v2_candidate.get("signal_score", 0.0) or 0.0)
            best_signal = {
                "mode": "V2",
                "profile": "V2",
                "direction": str(v2_candidate.get("direction") or "LONG").upper(),
                "signal_score": best_signal_score,
                "signal_quality": QualifiedValue.verified(best_signal_score, "strategy_v2").to_dict(),
                "probability_score": 0.0,
                "raw_logit_value": 0.0,
                "mtf_alignment": "V2",
                "mtf_alignment_count": 0,
                "mtf_alignment_total": 0,
                "threshold": float(min_edge_score),
                "details": {"mtf": "V2", "rsi": "V2", "vwap": "V2"},
                "mtf_score": 0.0,
                "rsi_score": 0.0,
                "vwap_score": 0.0,
                "ema_score": 0.0,
                "macd_score": 0.0,
                "price_action_score": 0.0,
                "signal_thesis": "V2 strategy candidate",
                "timeframe": "intraday",
                "expected_holding_period": "1-4h",
                "score_breakdown": {},
            }

        # Found a valid signal! Update cooldown
        self.last_signal_indices[last_signal_key] = current_idx

        # Calculate TP/SL using exit_mode (default ATR)
        exit_mode = str(cfg.get("exit_mode", "ATR")).upper()
        atr_tf = str(cfg.get("atr_timeframe", "5m"))
        atr_period = int(cfg.get("atr_period", 14))
        exit_atr, exit_atr_tf = self._get_atr_for_timeframe(
            ticker, is_crypto, atr_tf, atr_period, atr, cfg.get("regime", {}).get("lookback_days", 20)
        )

        tp_mult, sl_mult = self._resolve_atr_multipliers(best_signal["mode"], cfg)
        tp_pct = cfg.get("profit_target_pct", 5.0) / 100
        sl_pct = cfg.get("stop_loss_pct", 2.0) / 100
        tp, sl = self._calculate_exit_levels(
            close,
            best_signal["direction"],
            exit_mode,
            exit_atr if exit_atr and exit_atr > 0 else atr,
            tp_mult,
            sl_mult,
            tp_pct,
            sl_pct
        )
        rr_ratio = self._compute_rr_ratio(close, tp, sl)

        # Map direction la tipul de semnal
        signal_type = "BUY" if best_signal["direction"] == "LONG" else "SHORT"

        # Determine variant based on score (A = very good, B = good, C = acceptable)
        # and map risk_mode consistently with variant
        if best_signal_score >= 85:
            variant = "A"
            effective_risk_mode = "STRONG"
        elif best_signal_score >= 70:
            variant = "B"
            effective_risk_mode = "MEDIUM"
        else:
            variant = "C"
            effective_risk_mode = "RELAX"

        # Calculate quality score (kept for compatibility)
        ticker_data_for_quality = {
            "mtf_score": best_signal["mtf_score"],
            "rsi": row_context.rsi,
            "type": signal_type,
            "macd_hist": row_context.macd_hist,
        }
        quality_score = self._calculate_quality_score(ticker_data_for_quality, variant)

        # Build the response
        reasons = [
            f"Mod: {best_signal['mode']}",
            f"Score: {best_signal['signal_score']}/{best_signal['threshold']}", # Changed 'score' to 'signal_score'
            f"Prob: {best_signal.get('probability_score', 0):.2f}",
            f"MTF: {best_signal['details'].get('mtf', 'N/A')}",
            f"Regime: {market_regime}",
            f"RSI: {best_signal['details'].get('rsi', 'N/A')}",
            f"VWAP: {best_signal['details'].get('vwap', 'N/A')}",
        ]

        tp_pct = abs((tp - close) / close * 100)
        sl_pct = abs((sl - close) / close * 100)

        result = {
            "type": signal_type,
            "entry": close,
            "tp": round(tp, 4),
            "sl": round(sl, 4),
            "tp_pct": round(tp_pct, 2),
            "sl_pct": round(sl_pct, 2),
            "exit_mode": exit_mode,
            "atr_period": atr_period,
            "atr_timeframe": atr_tf,
            "atr_exit_timeframe": exit_atr_tf,
            "atr_exit": round(exit_atr or atr, 6) if (exit_atr or atr) else 0.0,
            "tp_atr_mult": tp_mult if exit_mode != "FIXED_PCT" else None,
            "sl_atr_mult": sl_mult if exit_mode != "FIXED_PCT" else None,
            "probability_score": best_signal.get("probability_score", 0),
            "raw_logit_value": float(best_signal.get("raw_logit_value", 0.0) or 0.0),
            "mtf_alignment": best_signal.get("mtf_alignment", ""),
            "market_regime": market_regime,
            "regime_detected": market_regime,
            "regime_strength": regime_strength,
            "regime_details": regime_details,
            "adx_5m": adx_5m,
            "bb_width_5m": bb_width_5m,
            "atr_percentile_5m": atr_percentile_5m,
            "mtf_score": best_signal["mtf_score"],
            "quality_score": quality_score,
            "variant": variant,
            "variant_mode": variant_mode,
            "rsi": row_context.rsi,
            "macd": row_context.macd,
            "macd_hist": row_context.macd_hist,
            "mtf_trends": mtf_trends,
            "reasons": reasons,
            "risk_mode": effective_risk_mode,  # Consistent cu varianta: A=STRONG, B=MEDIUM, C=RELAX
            # NEW: data from scoring system
            "signal_mode": best_signal["mode"],
            "signal_score": best_signal["signal_score"], # Changed 'score' to 'signal_score'
            "signal_threshold": best_signal["threshold"],
            "signal_thesis": best_signal.get("signal_thesis", ""),
            "timeframe": best_signal.get("timeframe", "intraday"),
            "expected_holding_period": best_signal.get("expected_holding_period", "1-4h"),
            "atr": atr,
            "atr_value": atr,
            "avg_atr_20d": avg_atr_20d,
            "spread_estimate": spread_estimate,
            "dist_vwap_atr": dist_vwap_atr,
            "dist_ema_atr": dist_ema_atr,
            "rr_ratio": round(rr_ratio, 3),
            "score_breakdown": best_signal.get("score_breakdown", {}),
            "strict_disagreement": bool(best_signal.get("strict_disagreement", False)),
            "details": best_signal["details"], # Added details to return
            # News context for the AI decision layer (ollama_decision._compact_signal
            # whitelists news_score/news_sentiment/news_headlines/top_headlines).
            # Attached unconditionally so BOTH V1 and V2 signals carry news to the
            # AI reviewer (directive: integrate AI via news analysis).
            "news_score": news_pulse_score,
            "news_sentiment": news_sentiment_trend,
            "news_headlines": news_headlines,
            "news_top_headlines": news_top_headlines,
            "top_headlines": news_top_headlines,  # _compact_signal whitelist key
            "news_sentiment_trend": news_sentiment_trend,
            "news_pulse_score": news_pulse_score,
        }

        # Override signal data with V2 candidate if available.
        # NOTE: this block was previously physically AFTER `return {...}` above and was
        # therefore unreachable dead code — V2 candidates were computed (selector run,
        # exit levels recomputed) but never applied to the returned signal, so the V2
        # strategy layer was silently inert. The signal dict is now assigned to `result`
        # and the override is applied before the single return below.
        result = self._apply_v2_override(result, v2_candidate, v2_tp_pct, v2_sl_pct, v2_rr, min_edge_score)
        if v2_research_candidate:
            result["research_only"] = True
        return result

    @staticmethod
    def _build_v2_candidate_dict(
        bc: dict,
        close: float,
        v2_score: float,
        research_signal_threshold: Optional[float] = None,
    ) -> dict:
        """Build the V2 candidate override dict from a selector ``best_candidate``.

        ``bc`` is the dict returned by ``StrategySelector.select(...)["best_candidate"]``,
        i.e. ``CandidateSignal.to_dict()``. Note ``CandidateSignal.to_dict()``
        emits the take-profit under key **``tp1``** (not ``tp``) — reading
        ``bc.get("tp", 0)`` here would silently yield 0, which then makes
        ``_apply_v2_override`` skip the TP override (the live signal keeps the
        V1 ATR TP while taking V2's structure SL → mixed risk models) and, when
        V2 flips direction vs V1, leaves ``result["tp"]`` on the wrong side of
        entry so ``signals.add_signal``'s TP-ordering validation drops the V2
        reversal signal entirely. Read ``tp1`` to apply V2's true exit levels.
        """
        meta = dict(bc.get("meta") or {}) if isinstance(bc.get("meta"), dict) else {}
        research_only = bool(meta.get("research_only"))
        confluence = dict(meta.get("confluence") or {}) if isinstance(meta.get("confluence"), dict) else {}
        strategy_evidence = dict(meta.get("strategy_evidence") or {}) if isinstance(meta.get("strategy_evidence"), dict) else {}
        if confluence:
            strategy_evidence.setdefault("confluence", confluence)
        return {
            "type": "BUY" if bc.get("direction") == "LONG" else "SHORT",
            "entry": close,
            "tp": float(bc.get("tp1", 0) or bc.get("tp", 0)),
            "sl": float(bc.get("sl", 0)),
            "direction": bc.get("direction", "LONG"),
            "signal_score": float(v2_score),
            "strategy_source": "V2",
            "strategy_type": bc.get("strategy_id", ""),
            "validation_score": float(bc.get("validation_score", 0)),
            "setup_score": float(bc.get("setup_score", 0)),
            "risk_score": float(bc.get("risk_score", 0)),
            "catalyst_score": float(bc.get("catalyst_score", 0)),
            "confluence_score": float(
                bc.get("confluence_score")
                if bc.get("confluence_score") is not None
                else confluence.get("score", 50)
            ),
            "confluence": confluence,
            "strategy_name": str(bc.get("strategy_name") or ""),
            "asset_class": str(bc.get("asset_class") or "US_STOCKS").upper(),
            "strategy_reasons": list(bc.get("reasons") or []),
            "strategy_evidence": strategy_evidence,
            "research_only": research_only,
            # Research-only V2 signals originate in the engine scan rather
            # than the autonomous scanner.  Label them explicitly so the
            # isolated paper cohort can admit and audit S7 across stocks,
            # crypto, and commodities without ever promoting them to Capital.
            "autonomous_source": "v2_engine_research" if research_only else "",
            "capital_execution_eligible": not research_only,
            "capital_execution_block_reason": str(meta.get("capital_execution_block_reason") or ""),
            "validation_rejection": str(meta.get("validation_rejection") or ""),
            "signal_threshold": float(research_signal_threshold) if research_only and research_signal_threshold else 0.0,
            # Preserve the exact selector snapshot that admitted this candidate.
            # Capital re-reads the cache before execution, but retaining this
            # provenance makes the signal explainable and exposes cache drift.
            "performance_snapshot": dict(bc.get("perf_snapshot") or {}),
        }

    def _apply_v2_override(
        self,
        result: dict,
        v2_candidate: Optional[dict],
        v2_tp_pct: float,
        v2_sl_pct: float,
        v2_rr: float,
        min_edge_score: float = 0.0,
    ) -> dict:
        """Apply V2 strategy candidate fields onto a generated signal dict.

        Returns ``result`` unchanged when no V2 candidate is present. Extracted from
        ``_analyze_ticker`` so the override is reachable (it was dead code after a
        premature ``return``) and unit-testable in isolation.
        """
        if v2_candidate is None:
            return result
        result["strategy_source"] = "V2"
        result["strategy_type"] = v2_candidate.get("strategy_type", "")
        result["strategy_name"] = v2_candidate.get("strategy_name", "")
        result["asset_class"] = str(v2_candidate.get("asset_class") or result.get("asset_class") or "US_STOCKS").upper()
        result["type"] = v2_candidate["type"]
        result["direction"] = v2_candidate["direction"]
        result["signal_score"] = v2_candidate["signal_score"]
        result["signal_mode"] = v2_candidate.get("strategy_type", "V2")
        # M8: a V2 signal must carry the V2 threshold (min_edge_score), not the
        # stale V1 `best_signal["threshold"]` set at :2808. The Open/Pending
        # cutoff at :3539-3541 (`open_cutoff = max(70, threshold+10)`) otherwise
        # gates V2 signals on the V1 profile threshold, so a V2 signal that
        # cleared the V2 gate (score >= min_edge_score) was marked Open/Pending
        # on a V1 threshold -- inconsistent label vs the gate that admitted it,
        # and the UI/ollama layers read `signal_threshold` to interpret the
        # score. Setting the V2 threshold makes the cutoff follow the same
        # `score >= threshold+10` rule as V1, with the V2 threshold. Optional
        # (default 0.0 -> not set) so legacy 5-arg callers / unit tests that
        # exercise _apply_v2_override in isolation are unaffected.
        candidate_threshold = float(v2_candidate.get("signal_threshold", 0.0) or 0.0)
        if candidate_threshold > 0:
            result["signal_threshold"] = candidate_threshold
        elif min_edge_score and float(min_edge_score) > 0:
            result["signal_threshold"] = float(min_edge_score)
        # Recompute variant/risk_mode from the V2 score so the label tracks the
        # score that actually produced the TP/SL. Pre-fix these stayed stale from
        # the V1 score band (engine.py:2731-2739), so a V2 signal carried V2
        # tp/sl/rr alongside a V1-derived variant/risk_mode -- mismatching the
        # win-rate filter (_passes_winrate_filter via get_variant_stats) and the
        # ollama decision layer (signal.get("risk_mode")), which gate on the
        # label. Same 85/70 bands as the V1 path for consistency.
        _v2_score = float(v2_candidate["signal_score"])
        if _v2_score >= 85:
            result["variant"] = "A"
            result["risk_mode"] = "STRONG"
        elif _v2_score >= 70:
            result["variant"] = "B"
            result["risk_mode"] = "MEDIUM"
        else:
            result["variant"] = "C"
            result["risk_mode"] = "RELAX"
        if v2_candidate["tp"] > 0:
            result["tp"] = v2_candidate["tp"]
            result["tp_pct"] = v2_tp_pct
        if v2_candidate["sl"] > 0:
            result["sl"] = v2_candidate["sl"]
            result["sl_pct"] = v2_sl_pct
        result["rr_ratio"] = round(v2_rr, 3)
        result["validation_score"] = v2_candidate.get("validation_score", 0)
        result["setup_score"] = v2_candidate.get("setup_score", 0)
        result["risk_score"] = v2_candidate.get("risk_score", 0)
        result["catalyst_score"] = v2_candidate.get("catalyst_score", 0)
        result["confluence_score"] = v2_candidate.get("confluence_score", 50)
        result["confluence"] = dict(v2_candidate.get("confluence") or {})
        result["performance_snapshot"] = dict(v2_candidate.get("performance_snapshot") or {})
        result["strategy_evidence"] = dict(v2_candidate.get("strategy_evidence") or {})
        result["strategy_reasons"] = list(v2_candidate.get("strategy_reasons") or [])
        if v2_candidate.get("research_only"):
            result["research_only"] = True
            result["autonomous_source"] = str(v2_candidate.get("autonomous_source") or "v2_engine_research")
            result["capital_execution_eligible"] = False
            result["capital_execution_block_reason"] = str(
                v2_candidate.get("capital_execution_block_reason")
                or "Research-only strategy candidate has no current faithful OOS validation"
            )
            result["validation_rejection"] = str(v2_candidate.get("validation_rejection") or "")
        result["reasons"].append(f"Strategy: {v2_candidate.get('strategy_type', 'V2')}")
        result["reasons"].append(f"V2 Score: {v2_candidate['signal_score']:.1f}")
        result["reasons"].extend(
            reason for reason in v2_candidate.get("strategy_reasons", [])
            if reason and reason not in result["reasons"]
        )
        return result

    def delete_signal(self, signal_id: str) -> bool:
        """Delete a signal by ID (open sau closed)."""
        return self.signals.delete_signal(signal_id)

    def _should_accept_new_signal(
        self,
        existing_signal: dict,
        new_signal: dict,
        existing_action: str,
        new_action: str,
        existing_score: int,
        new_score: int,
        existing_mode: str,
        new_mode: str,
        new_mtf_trends: dict
    ) -> bool:
        """
        Determine if a new signal should replace or be blocked by an existing signal.

        Rules:
        - Same direction: Only replace if new score is +10 higher AND same mode
        - Opposite direction (reversal): Only allow if:
          - New mode is PULLBACK or BREAKOUT (not CONTRA)
          - MTF alignment is complete (3/3 in new direction)
          - New score is +15 higher than existing

        Args:
            existing_signal: The currently open signal for this ticker
            new_signal: The new signal being considered
            existing_action: BUY or SHORT of existing signal
            new_action: BUY or SHORT of new signal
            existing_score: Score of existing signal
            new_score: Score of new signal
            existing_mode: Trading mode of existing signal
            new_mode: Trading mode of new signal
            new_mtf_trends: MTF trend data for new signal

        Returns:
            True if new signal should be accepted, False to reject it
        """
        ticker = existing_signal.get("ticker", "")

        # Same direction case
        if existing_action == new_action:
            # Only replace if same mode AND score improvement >= threshold
            if new_mode == existing_mode and new_score >= existing_score + SAME_DIRECTION_SCORE_THRESHOLD:
                logger.info(
                    f"🔄 Signal {ticker} {new_action} replacing existing: "
                    f"score {existing_score} -> {new_score} (+{new_score - existing_score})"
                )
                # Close existing position at the new signal entry so PnL/capital are recorded.
                replacement_entry = new_signal.get("entry", new_signal.get("entry_price"))
                try:
                    exit_price = float(replacement_entry) if replacement_entry is not None else None
                except Exception:
                    exit_price = None
                self.signals.close_signal_manual(
                    ticker,
                    f"REPLACED > {new_action} (score {new_score})",
                    exit_price=exit_price
                )
                return True
            else:
                logger.debug(
                    f"🚫 Signal {ticker} {new_action} blocked: same direction, "
                    f"score {new_score} not enough improvement over {existing_score}"
                )
                return False

        # Opposite direction (reversal) case
        else:
            # Rule 1: Must be PULLBACK or BREAKOUT mode (not CONTRA)
            if new_mode not in ["PULLBACK", "BREAKOUT"]:
                logger.info(
                    f"🚫 Signal {ticker} {new_action} reversal blocked: "
                    f"mode {new_mode} not allowed for reversal (need PULLBACK/BREAKOUT)"
                )
                return False

            # Rule 2: Must have complete MTF alignment (3/3) in new direction
            expected_trend = "BULL" if new_action == "BUY" else "BEAR"
            aligned_count = sum(
                1 for tf in ["1h", "30m", "5m"]
                if new_mtf_trends.get(tf, "").upper() == expected_trend
            )

            if aligned_count < REVERSAL_REQUIRED_MTF_ALIGNMENT:
                logger.info(
                    f"🚫 Signal {ticker} {new_action} reversal blocked: "
                    f"MTF alignment {aligned_count}/3 (need {REVERSAL_REQUIRED_MTF_ALIGNMENT}/3 {expected_trend})"
                )
                return False

            # Rule 3: Must have score improvement >= reversal threshold
            # Dynamic threshold: if current signal is already very strong (>=85), allow switch with smaller diff
            reversal_threshold = 5 if existing_score >= 85 else REVERSAL_SCORE_THRESHOLD
            
            if new_score < existing_score + reversal_threshold:
                logger.info(
                    f"🚫 Signal {ticker} {new_action} reversal blocked: "
                    f"score {new_score} not enough improvement over {existing_score} (need +{reversal_threshold})"
                )
                return False

            # All rules passed - allow reversal
            logger.info(
                f"🔄 Signal {ticker} reversal {existing_action} -> {new_action}: "
                f"score {existing_score} -> {new_score}, MTF {aligned_count}/3 {expected_trend}"
            )
            # Close existing position at the new signal entry so PnL/capital are recorded.
            replacement_entry = new_signal.get("entry", new_signal.get("entry_price"))
            try:
                exit_price = float(replacement_entry) if replacement_entry is not None else None
            except Exception:
                exit_price = None
            self.signals.close_signal_manual(
                ticker,
                f"REVERSAL > {new_action} (score {new_score})",
                exit_price=exit_price
            )
            return True

    def _passes_winrate_filter(self, variant: str, score: int, threshold: int, variant_mode: Optional[str] = None) -> bool:
        """
        Check if signal passes the win-rate based performance filter.

        This is a pattern-level performance filter that blocks signals from
        variants that have poor historical performance.

        Rules:
        - If we have >= WINRATE_FILTER_MIN_TRADES trades for this variant in last N days
        - AND win rate is below WINRATE_FILTER_MIN_PCT
        - THEN require extra score (score >= threshold + WINRATE_FILTER_SCORE_PENALTY)

        Args:
            variant: Signal variant (A, B, or C)
            score: Signal score
            threshold: Score threshold for the current profile

        Returns:
            True if signal passes filter, False to reject it
        """
        stats = self.signals.get_variant_stats(
            variant,
            days=WINRATE_FILTER_DAYS,
            variant_mode=variant_mode,
            verified_only=True,
        )
        total_trades = stats.get("total", 0)
        win_pct = stats.get("win_pct", 0.0)

        # Not enough data to apply filter
        if total_trades < WINRATE_FILTER_MIN_TRADES:
            return True

        # Win rate is acceptable
        if win_pct >= WINRATE_FILTER_MIN_PCT:
            return True

        # Win rate is poor - require extra score to pass
        required_score = threshold + WINRATE_FILTER_SCORE_PENALTY
        if score >= required_score:
            logger.info(
                f"⚠️ Variant {variant} has low win rate ({win_pct:.1f}%), "
                f"but signal passes with high score ({score} >= {required_score})"
            )
            return True

        # Signal doesn't meet elevated requirements
        logger.info(
            f"📉 Variant {variant} blocked: win rate {win_pct:.1f}% < {WINRATE_FILTER_MIN_PCT}% "
            f"(trades: {total_trades}), score {score} < required {required_score}"
        )
        return False

    def _find_best_strategy(
        self,
        ticker: str,
        is_crypto: bool,
        days: int = 7,
        df: pd.DataFrame = None,
        exit_mode: str = None,
        custom_tp_pct: float = None,
        custom_sl_pct: float = None,
        variant_mode: str = None
    ) -> Optional[Dict]:
        """
        Find the best strategy combination for a ticker by comparing all risk/trade modes.
        
        Args:
            ticker: The ticker symbol
            is_crypto: Whether it's a cryptocurrency
            days: Backtest period (default 7 days)
            df: Optional pre-fetched DataFrame (uses same data as signal backtest)
            exit_mode: Exit mode override (ATR or FIXED_PCT)
            custom_tp_pct: Custom Take Profit %
            custom_sl_pct: Custom Stop Loss %
            
        Returns:
            Dict with best strategy info or None if no valid strategy found
        """
        risk_modes = ["PRO", "STRONG", "MEDIUM", "RELAX"]
        trade_modes = ["PULLBACK", "BREAKOUT", "CONTRA"]
        
        eff_exit_mode = str(exit_mode or self.config.signal.get("exit_mode", "ATR")).upper()
        use_custom_tp = custom_tp_pct if eff_exit_mode == "FIXED_PCT" else None
        use_custom_sl = custom_sl_pct if eff_exit_mode == "FIXED_PCT" else None

        best_result = None
        best_win_rate = -1
        
        for risk_mode in risk_modes:
            for trade_mode in trade_modes:
                try:
                    result = self.quick_backtester.quick_backtest(
                        ticker=ticker,
                        risk_mode=risk_mode,
                        is_crypto=is_crypto,
                        days=days,
                        trade_mode=trade_mode,
                        df=df,  # Use same data for all strategies
                        exit_mode=eff_exit_mode,
                        custom_tp_pct=use_custom_tp,
                        custom_sl_pct=use_custom_sl,
                        variant_mode=variant_mode
                    )
                    
                    # Only consider strategies with at least 1 signal
                    if result["total_signals"] > 0:
                        win_rate = result["win_rate"]
                        
                        # Prefer higher win rate, but also consider signal count
                        # (avoid strategies with 1 signal and 100% win rate)
                        if win_rate > best_win_rate or (
                            win_rate == best_win_rate and 
                            best_result and 
                            result["total_signals"] > best_result["total_signals"]
                        ):
                            best_win_rate = win_rate
                            best_result = {
                                "risk_mode": risk_mode,
                                "trade_mode": trade_mode,
                                "win_rate": win_rate,
                                "total_signals": result["total_signals"],
                                "wins": result["wins"],
                                "losses": result["losses"],
                            }
                except Exception as e:
                    logger.debug(f"Strategy test {risk_mode}/{trade_mode} failed for {ticker}: {e}")
                    continue
        
        # [NEW] Also evaluate ORB_FVG for US Stocks (not crypto)
        if not is_crypto:
            try:
                from core.orb_fvg_backtester import run_orb_fvg_backtest
                orb_result = run_orb_fvg_backtest(
                    ticker=ticker,
                    days=days,
                    entry_type="BOTH"  # Test both entry types
                )
                
                # Check if ORB produced valid trades
                total_orb = len(orb_result.get("trades", []))
                if total_orb > 0:
                    orb_win_rate = orb_result.get("win_rate", 0)
                    
                    if orb_win_rate > best_win_rate or (
                        orb_win_rate == best_win_rate and
                        best_result and
                        total_orb > best_result["total_signals"]
                    ):
                        best_win_rate = orb_win_rate
                        best_result = {
                            "risk_mode": "OPENING_RANGE",
                            "trade_mode": "ORB_FVG",
                            "win_rate": orb_win_rate,
                            "total_signals": total_orb,
                            "wins": orb_result.get("wins", 0),
                            "losses": orb_result.get("losses", 0),
                        }
            except Exception as e:
                logger.debug(f"ORB_FVG test failed for {ticker}: {e}")
        
        return best_result

    def _is_ticker_suppressed(
        self,
        ticker: str,
        action: str = None,
        signal_mode: str = None,
        variant_mode: str = None,
    ) -> bool:
        """
        Check if an exact signal signature is suppressed (manually deleted signal).
        Only blocks the same ticker + direction + strategy mode + variant mode.
        Suppression expires after the configured time (default 60 minutes).
        
        Args:
            ticker: The ticker symbol to check
            action: The signal action (BUY or SHORT) to check
            signal_mode: Trading mode (e.g. PULLBACK/BREAKOUT/CONTRA)
            variant_mode: Variant mode (STRICT/CUSTOM)
            
        Returns:
            True if exact signal signature is suppressed, False otherwise
        """
        try:
            from datetime import datetime
            
            suppressed_path = SUPPRESSED_TICKERS_PATH
            
            if not suppressed_path.exists():
                return False
            
            with open(suppressed_path, "r", encoding="utf-8") as f:
                suppressed = json.load(f)
            
            ticker_norm = str(ticker or "").upper().strip()
            action_norm = str(action or "").upper().strip()
            mode_norm = str(signal_mode or "UNKNOWN").upper().strip()
            variant_norm = str(variant_mode or "STRICT").upper().strip()

            # Check exact signal signature key (e.g., "BTC-USD_BUY_PULLBACK_STRICT")
            suppress_key = (
                f"{ticker_norm}_{action_norm}_{mode_norm}_{variant_norm}"
                if ticker_norm and action_norm
                else None
            )
            
            if suppress_key and suppress_key in suppressed:
                entry = suppressed[suppress_key]
                suppressed_at_str = entry.get("suppressed_at", "")
                default_minutes = self.config.signal.get("manual_delete_suppress_minutes", 60)
                try:
                    default_minutes = int(default_minutes or 60)
                except Exception:
                    default_minutes = 60
                default_minutes = max(1, default_minutes)
                suppress_minutes = entry.get("suppress_minutes", default_minutes)
                try:
                    suppress_minutes = int(suppress_minutes or default_minutes)
                except Exception:
                    suppress_minutes = default_minutes
                suppress_minutes = max(1, suppress_minutes)
                
                if suppressed_at_str:
                    try:
                        suppressed_at = datetime.fromisoformat(suppressed_at_str)
                        now = get_bucharest_time()
                        elapsed_minutes = (now - suppressed_at).total_seconds() / 60
                        
                        if elapsed_minutes >= suppress_minutes:
                            # Suppression expired, remove from list
                            del suppressed[suppress_key]
                            with open(suppressed_path, "w", encoding="utf-8") as f:
                                json.dump(suppressed, f, indent=2, ensure_ascii=False)
                            logger.info(
                                f"🔓 {ticker_norm} {action_norm} {mode_norm}/{variant_norm} "
                                f"suppression expired ({suppress_minutes} min)"
                            )
                            return False
                        
                        # Still suppressed
                        remaining = suppress_minutes - elapsed_minutes
                        logger.debug(
                            f"{ticker_norm} {action_norm} {mode_norm}/{variant_norm} "
                            f"suppressed for {remaining:.1f} more minutes"
                        )
                        return True
                    except Exception:
                        pass
            
            return False
            
        except Exception as e:
            logger.warning(f"Error checking suppressed ticker {ticker}: {e}")
            return False

    def _process_signal(self, ticker: str, signal: dict, asset_type: str) -> None:
        now = get_bucharest_time()
        is_research_only = bool(signal.get("research_only"))
        signal_asset_class = str(signal.get("asset_class") or "").upper()
        if not signal_asset_class:
            try:
                from core.strategy_v2.context_builder import infer_asset_class
                signal_asset_class = infer_asset_class(ticker)
            except Exception:
                signal_asset_class = "CRYPTO_FUTURES" if asset_type == "crypto" else "US_STOCKS"
        new_action = str(signal.get("type", "")).upper()
        new_mode = str(signal.get("signal_mode", "PULLBACK")).upper()
        new_mtf_trends = signal.get("mtf_trends", {}) or {}
        cfg = self.config.signal
        new_variant_mode = str(signal.get("variant_mode") or cfg.get("variant_mode_default", "STRICT")).upper()
        signal["variant_mode"] = new_variant_mode

        signal["gating_priority_order"] = [
            "1_market_regime",
            "2_risk_profile_mtf",
            "3_logistic_scoring",
            "4_strict_override",
            "5_expectancy_performance",
            "6_cooldown_duplicate",
        ]

        ticker_settings = self.config.get_ticker_settings(ticker)
        custom_tp_pct = ticker_settings.get("tp_pct")
        custom_sl_pct = ticker_settings.get("sl_pct")
        eff_exit_mode = str(signal.get("exit_mode") or cfg.get("exit_mode", "ATR")).upper()
        signal["exit_mode"] = eff_exit_mode
        allowed_strategies = ticker_settings.get("allowed_strategies", []) or []

        risk_mode = str(signal.get("risk_mode", "MEDIUM")).upper()
        trade_mode = str(signal.get("signal_mode", "PULLBACK")).upper()
        current_strategy = f"{risk_mode}/{trade_mode}"
        if allowed_strategies and current_strategy not in allowed_strategies:
            logger.info(f"Signal {ticker} {new_action} filtered: strategy {current_strategy} not allowed")
            return

        custom_override_active = eff_exit_mode == "FIXED_PCT" and (custom_tp_pct is not None or custom_sl_pct is not None)
        if custom_override_active:
            entry_price = float(signal.get("entry", 0) or 0)
            if entry_price > 0:
                eff_tp_pct = float(custom_tp_pct if custom_tp_pct is not None else signal.get("tp_pct", 0) or 0)
                eff_sl_pct = float(custom_sl_pct if custom_sl_pct is not None else signal.get("sl_pct", 0) or 0)
                signal["tp_pct"] = eff_tp_pct
                signal["sl_pct"] = eff_sl_pct
                if new_action == "BUY":
                    signal["tp"] = entry_price * (1 + eff_tp_pct / 100.0)
                    signal["sl"] = entry_price * (1 - eff_sl_pct / 100.0)
                else:
                    signal["tp"] = entry_price * (1 - eff_tp_pct / 100.0)
                    signal["sl"] = entry_price * (1 + eff_sl_pct / 100.0)
                signal["exit_mode"] = "FIXED_PCT"
                signal["tp_atr_mult"] = None
                signal["sl_atr_mult"] = None

        custom_tp_eff = signal.get("tp_pct", signal.get("tp", 0)) if custom_override_active else None
        custom_sl_eff = signal.get("sl_pct", signal.get("sl", 0)) if custom_override_active else None

        if signal_asset_class == "US_STOCKS" and asset_type == "stock" and not is_24x5_instrument(ticker) and not is_us_market_open():
            logger.info(f"Signal {ticker} {new_action} filtered: US market closed")
            return

        # 4) STRICT override logic
        skip_cooldown = False
        replace_custom_with_strict = False
        replace_exit_price = None
        replace_info = None
        existing = self.signals.has_open_signal_for_ticker(ticker)
        if existing:
            existing_action = str(existing.get("action") or existing.get("type") or "").upper()
            existing_score = float(existing.get("signal_score", 0) or 0)
            existing_mode = str(existing.get("signal_mode", "")).upper()
            existing_variant_mode = str(existing.get("variant_mode") or cfg.get("variant_mode_default", "STRICT")).upper()

            if existing_variant_mode == "STRICT" and new_variant_mode == "CUSTOM":
                logger.info(f"Signal {ticker} {new_action} (CUSTOM) ignored: STRICT already open")
                return

            if new_variant_mode == "STRICT" and existing_variant_mode == "CUSTOM":
                strict_score = float(signal.get("signal_score", 0) or 0)
                strict_mtf = self._parse_mtf_alignment_count(signal.get("mtf_alignment"))
                if strict_score >= (existing_score + STRICT_OVERRIDE_SCORE_DELTA) and strict_mtf >= STRICT_OVERRIDE_MIN_MTF:
                    replacement_entry = signal.get("entry", signal.get("entry_price"))
                    try:
                        replacement_entry = float(replacement_entry) if replacement_entry is not None else None
                    except Exception:
                        replacement_entry = None
                    replace_custom_with_strict = True
                    replace_exit_price = replacement_entry
                    replace_info = {
                        "id": existing.get("id"),
                        "action": existing_action,
                        "variant": existing.get("variant"),
                        "variant_mode": existing_variant_mode,
                        "signal_mode": existing_mode,
                        "signal_score": existing_score,
                    }
                    existing = None
                    skip_cooldown = True
                else:
                    signal["strict_disagreement"] = True
                    logger.info(
                        f"STRICT replacement blocked for {ticker}: score {strict_score:.1f} vs {existing_score:.1f}, "
                        f"mtf {strict_mtf}/6"
                    )
                    return

        # 5) Expectancy-based performance gating
        quick_bt_results = None
        best_strategy = None
        backtest_df = None
        is_crypto = (asset_type == "crypto")
        backtest_enabled = bool(self.config.backtest.get("enabled", True))

        if backtest_enabled:
            try:
                backtest_df, _ = self.quick_backtester.fetch_backtest_data(
                    ticker=ticker,
                    is_crypto=is_crypto,
                    days=30,
                )
                if backtest_df is not None and not getattr(backtest_df, "empty", False) and len(backtest_df) >= 100:
                    quick_bt_results = self.quick_backtester.quick_backtest(
                        ticker=ticker,
                        risk_mode=risk_mode,
                        is_crypto=is_crypto,
                        days=30,
                        trade_mode=trade_mode,
                        df=backtest_df,
                        exit_mode=eff_exit_mode,
                        custom_tp_pct=custom_tp_eff,
                        custom_sl_pct=custom_sl_eff,
                        variant_mode=new_variant_mode,
                    )
                    best_strategy = self._find_best_strategy(
                        ticker,
                        is_crypto,
                        days=30,
                        df=backtest_df,
                        exit_mode=eff_exit_mode,
                        custom_tp_pct=custom_tp_eff,
                        custom_sl_pct=custom_sl_eff,
                        variant_mode=new_variant_mode,
                    )
            except Exception as e:
                logger.warning(f"Quick backtest failed for {ticker}: {e}")

        perf_source = None
        if quick_bt_results and int(quick_bt_results.get("total_signals", 0) or 0) > 0:
            perf_source = quick_bt_results
        elif best_strategy and int(best_strategy.get("total_signals", 0) or 0) > 0:
            perf_source = {
                "expectancy_r": 0.0,
                "profit_factor": 0.0,
                "win_rate": float(best_strategy.get("win_rate", 0) or 0),
                "trade_count": int(best_strategy.get("total_signals", 0) or 0),
                "total_signals": int(best_strategy.get("total_signals", 0) or 0),
            }

        expectancy_30d = float((perf_source or {}).get("expectancy_r", 0.0) or 0.0)
        profit_factor_30d = float((perf_source or {}).get("profit_factor", 0.0) or 0.0)
        win_rate_30d = float((perf_source or {}).get("win_rate_trades", (perf_source or {}).get("win_rate", 0.0)) or 0.0)
        trades_30d = int(
            (perf_source or {}).get("trade_count", (perf_source or {}).get("closed_trades", (perf_source or {}).get("total_signals", 0)))
            or 0
        )

        if not is_research_only and perf_source is not None and expectancy_30d < 0:
            logger.info(f"Signal {ticker} {new_action} blocked: expectancy_R_30d {expectancy_30d:.2f} < 0")
            return

        performance_penalty = 0.0
        penalized_by_performance = False
        if not is_research_only and perf_source is not None and profit_factor_30d < 1.1 and win_rate_30d < 40.0:
            performance_penalty = 10.0
            penalized_by_performance = True

        raw_score = float(signal.get("signal_score", 0) or 0)
        allow_low_sample = bool((self.config.strategy_v2 or {}).get("allow_low_sample_fallback", True))
        if is_research_only:
            # S7 already passed its causal setup/quality gates.  The legacy quick
            # backtest is neither the S7 strategy nor faithful V2 validation, so
            # it must not silently downgrade or hide an observational signal.
            confidence_factor = 1.0
        elif perf_source is None or trades_30d <= 0:
            # No historical sample should not zero a live setup. Keep the signal
            # score usable for UI/execution and carry sample confidence separately.
            confidence_factor = 1.0 if allow_low_sample else 0.75
        else:
            confidence_factor = min(1.0, max(0.25, float(trades_30d) / 10.0))
        score_after_penalty = max(0.0, raw_score - performance_penalty)
        final_score = round(score_after_penalty * confidence_factor, 2)

        signal["raw_signal_score"] = raw_score
        signal["performance_penalty"] = performance_penalty
        signal["penalized_by_performance"] = penalized_by_performance
        signal["confidence_factor"] = confidence_factor
        signal["signal_score"] = final_score
        signal["score"] = final_score

        new_score = final_score

        # 6) Cooldown & duplicate rules
        if self._is_ticker_suppressed(ticker, new_action, new_mode, new_variant_mode):
            logger.info(f"Signal {ticker} {new_action} {new_mode}/{new_variant_mode} filtered: suppressed")
            return

        if existing:
            existing_action = str(existing.get("action") or existing.get("type") or "").upper()
            existing_score = float(existing.get("signal_score", 0) or 0)
            existing_mode = str(existing.get("signal_mode", "")).upper()
            if not self._should_accept_new_signal(
                existing_signal=existing,
                new_signal=signal,
                existing_action=existing_action,
                new_action=new_action,
                existing_score=existing_score,
                new_score=new_score,
                existing_mode=existing_mode,
                new_mode=new_mode,
                new_mtf_trends=new_mtf_trends,
            ):
                return

        cooldown_minutes = int(cfg.get("signal_cooldown_minutes", DEFAULT_TICKER_COOLDOWN_MINUTES) or DEFAULT_TICKER_COOLDOWN_MINUTES)
        stop_lockout_minutes = int(cfg.get("stop_lockout_minutes", DEFAULT_STOP_LOCKOUT_MINUTES) or 0)
        if not skip_cooldown and not self.signals.can_open_new_signal_for_ticker(
            ticker, cooldown_minutes, stop_lockout_minutes
        ):
            logger.info(f"Signal {ticker} {new_action} filtered: per-ticker cooldown ({cooldown_minutes}min)")
            return

        atr_value = float(signal.get("atr_value", signal.get("atr", 0.0)) or 0.0)
        spread_estimate = float(signal.get("spread_estimate", 0.0) or 0.0)
        if spread_estimate <= 0:
            spread_estimate = max(abs(float(signal.get("entry", 0) or 0)) * 0.0002, 0.0001)
        if atr_value > 0 and atr_value < (spread_estimate * 3.0):
            logger.info(
                f"Signal {ticker} {new_action} blocked: ATR sanity failed (atr={atr_value:.6f} < spread*3={spread_estimate * 3.0:.6f})"
            )
            return

        signal_id = f"{ticker}_{now.strftime('%H%M%S%f')}"
        # NOTE: the STRICT replacement close used to happen HERE (before the new
        # signal was built/validated). If add_signal below then raised (invalid
        # TP/SL ordering, insufficient funds), the prior CUSTOM position was
        # already settled/closed with NO replacement — the ticker silently lost
        # its position. The close is now deferred until AFTER add_signal succeeds
        # (see below), and excludes the just-added signal's id so we close only
        # the prior CUSTOM signal, not the replacement.

        signal_threshold_value = float(signal.get("signal_threshold", 60) or 60)
        open_cutoff = max(70.0, signal_threshold_value + 10.0)
        initial_signal_status = "Open" if (
            final_score >= (signal_threshold_value if is_research_only else open_cutoff)
        ) else "Pending"

        bt_payload_30d = None
        if quick_bt_results:
            bt_payload_30d = {
                "wins": int(quick_bt_results.get("wins", 0) or 0),
                "losses": int(quick_bt_results.get("losses", 0) or 0),
                "total": int(quick_bt_results.get("total_signals", 0) or 0),
                "trade_count": int(quick_bt_results.get("trade_count", 0) or 0),
                "win_rate": round(float(quick_bt_results.get("win_rate", 0) or 0), 1),
                "win_rate_trades": round(float(quick_bt_results.get("win_rate_trades", quick_bt_results.get("win_rate", 0)) or 0), 1),
                "expectancy_r": round(float(quick_bt_results.get("expectancy_r", 0) or 0), 3),
                "profit_factor": round(float(quick_bt_results.get("profit_factor", 0) or 0), 3),
                "payoff_ratio": round(float(quick_bt_results.get("payoff_ratio", 0) or 0), 3),
                "risk_mode": risk_mode,
                "trade_mode": trade_mode,
                "variant_mode": new_variant_mode,
                "fallback_used": False,
            }
            if (bt_payload_30d.get("total", 0) or 0) <= 0 and best_strategy and (best_strategy.get("total_signals", 0) or 0) > 0:
                bt_payload_30d.update(
                    {
                        "wins": int(best_strategy.get("wins", 0) or 0),
                        "losses": int(best_strategy.get("losses", 0) or 0),
                        "total": int(best_strategy.get("total_signals", 0) or 0),
                        "trade_count": int(best_strategy.get("total_signals", 0) or 0),
                        "win_rate": round(float(best_strategy.get("win_rate", 0) or 0), 1),
                        "win_rate_trades": round(float(best_strategy.get("win_rate", 0) or 0), 1),
                        "expectancy_r": 0.0,
                        "profit_factor": 0.0,
                        "fallback_used": True,
                        "fallback_reason": "no_signals_for_generated_strategy",
                    }
                )

        signal_data = {
            "id": signal_id,
            "ticker": ticker,
            "type": signal["type"],
            "action": signal["type"],
            "entry": signal["entry"],
            "tp": signal.get("tp", signal.get("entry", 0)),
            "sl": signal.get("sl", signal.get("entry", 0)),
            "tp_pct": signal.get("tp_pct", 0),
            "sl_pct": signal.get("sl_pct", 0),
            "exit_mode": signal.get("exit_mode"),
            "atr_period": signal.get("atr_period"),
            "atr_timeframe": signal.get("atr_timeframe"),
            "atr_exit_timeframe": signal.get("atr_exit_timeframe"),
            "atr_exit": signal.get("atr_exit"),
            "tp_atr_mult": signal.get("tp_atr_mult"),
            "sl_atr_mult": signal.get("sl_atr_mult"),
            "probability_score": signal.get("probability_score", 0),
            "raw_logit_value": float(signal.get("raw_logit_value", 0.0) or 0.0),
            "mtf_alignment": signal.get("mtf_alignment", ""),
            "market_regime": signal.get("market_regime", ""),
            "regime_detected": signal.get("regime_detected", signal.get("market_regime", "")),
            "regime_strength": float(signal.get("regime_strength", 0.0) or 0.0),
            "regime_details": signal.get("regime_details", {}),
            "adx_5m": signal.get("adx_5m", 0),
            "bb_width_5m": signal.get("bb_width_5m", 0),
            "atr_percentile_5m": signal.get("atr_percentile_5m", 0),
            "variant": signal["variant"],
            "variant_description": self.VARIANT_DESC.get(signal["variant"], ""),
            "variant_mode": new_variant_mode,
            "mtf_score": signal["mtf_score"],
            "quality_score": signal["quality_score"],
            "rsi": signal["rsi"],
            "macd": signal["macd"],
            "macd_hist": signal["macd_hist"],
            "risk_mode": risk_mode,
            "score": final_score,
            "raw_score": raw_score,
            "signal_score": final_score,
            "trade_mode": signal.get("signal_mode", "PULLBACK"),
            "signal_mode": signal.get("signal_mode", "PULLBACK"),
            "signal_threshold": signal_threshold_value,
            "score_breakdown": signal.get("score_breakdown", {}),
            "signal_thesis": signal.get("signal_thesis", ""),
            "timeframe": signal.get("timeframe", "intraday"),
            "expected_holding_period": signal.get("expected_holding_period", "1-4h"),
            "strict_backed": signal.get("strict_backed", False),
            "strict_backed_at": signal.get("strict_backed_at"),
            "strict_variant": signal.get("strict_variant"),
            "strict_risk_mode": signal.get("strict_risk_mode"),
            "strict_signal_mode": signal.get("strict_signal_mode"),
            "strict_signal_score": signal.get("strict_signal_score"),
            "strict_probability_score": signal.get("strict_probability_score"),
            "strict_mtf_alignment": signal.get("strict_mtf_alignment"),
            "strict_replaced": True if replace_custom_with_strict else False,
            "strict_disagreement": bool(signal.get("strict_disagreement", False)),
            "replaced_signal": replace_info,
            "created_at": now.isoformat(),
            "created_at_time": now.strftime("%H:%M:%S"),
            "status": initial_signal_status,
            "asset_type": asset_type,
            "asset_class": signal_asset_class,
            "technical_price": float(signal.get("technical_price", signal.get("entry", 0.0)) or 0.0),
            "price_verified": bool(signal.get("price_verified", False)),
            "price_integrity": dict(signal.get("price_integrity") or {}),
            # Preserve the explicit source for isolated V2 research candidates.
            # AgentManager uses it to route S7 through the bounded paper cohort;
            # it never makes the signal Capital-executable.
            "autonomous_source": str(signal.get("autonomous_source") or ""),
            "strategy_source": str(signal.get("strategy_source") or "V1").upper(),
            # V1 generations previously reached Capital without a stable strategy
            # identity.  They remain visible in Signals, but cannot be confused
            # with a validated V2 strategy in performance/backtest evidence.
            "strategy_type": str(
                signal.get("strategy_type")
                or signal.get("strategy_id")
                or f"V1_{asset_type}_{risk_mode}_{trade_mode}_{new_variant_mode}"
            ).upper(),
            "strategy_id": str(
                signal.get("strategy_id")
                or signal.get("strategy_type")
                or f"V1_{asset_type}_{risk_mode}_{trade_mode}_{new_variant_mode}"
            ).upper(),
            "performance_snapshot": dict(signal.get("performance_snapshot") or {}),
            "research_only": is_research_only,
            "capital_execution_eligible": bool(signal.get("capital_execution_eligible", not is_research_only)),
            "capital_execution_block_reason": str(signal.get("capital_execution_block_reason") or ""),
            "validation_rejection": str(signal.get("validation_rejection") or ""),
            "strategy_name": str(signal.get("strategy_name") or ""),
            "strategy_reasons": list(signal.get("strategy_reasons") or []),
            "confluence_score": float(
                signal.get("confluence_score") if signal.get("confluence_score") is not None else 50.0
            ),
            "confluence": dict(signal.get("confluence") or {}),
            "strategy_evidence": dict(signal.get("strategy_evidence") or {}),
            "entry_strategy": str(signal.get("entry_strategy") or f"{risk_mode}_{trade_mode}_{new_variant_mode}").lower(),
            "atr": signal.get("atr"),
            "atr_value": atr_value,
            "avg_atr_20d": float(signal.get("avg_atr_20d", atr_value) or atr_value),
            "spread_estimate": spread_estimate,
            "rr_ratio": float(signal.get("rr_ratio", 0.0) or 0.0),
            "dist_vwap_atr": signal.get("dist_vwap_atr"),
            "dist_ema_atr": signal.get("dist_ema_atr"),
            "expectancy_R_30d": round(expectancy_30d, 3),
            "profit_factor_30d": round(profit_factor_30d, 3),
            "win_rate_30d": round(win_rate_30d, 2),
            "trades_30d": trades_30d,
            "performance_penalty": performance_penalty,
            "penalized_by_performance": penalized_by_performance,
            "confidence_factor": round(confidence_factor, 4),
            "trigger_info": {
                "rsi": signal["rsi"],
                "probability_score": signal.get("probability_score", 0),
                "market_regime": signal.get("market_regime", ""),
                "mtf_alignment": signal.get("mtf_alignment", ""),
                "macd_hist": signal["macd_hist"],
                "mtf_1h": signal["mtf_trends"].get("1h", "N/A"),
                "mtf_30m": signal["mtf_trends"].get("30m", "N/A"),
                "mtf_5m": signal["mtf_trends"].get("5m", "N/A"),
                "mtf": signal.get("details", {}).get("mtf", "N/A"),
                "reasons": signal.get("reasons", []),
                "confluence_score": float(
                    signal.get("confluence_score") if signal.get("confluence_score") is not None else 50.0
                ),
                "strategy_evidence": dict(signal.get("strategy_evidence") or {}),
            },
        }

        signal_data["quick_backtest_30d"] = bt_payload_30d or {
            "wins": 0,
            "losses": 0,
            "total": 0,
            "trade_count": 0,
            "win_rate": 0.0,
            "win_rate_trades": 0.0,
            "expectancy_r": 0.0,
            "profit_factor": 0.0,
            "payoff_ratio": 0.0,
            "risk_mode": risk_mode,
            "trade_mode": trade_mode,
            "pending": True,
            "reason": "backtest_data_unavailable",
        }
        if is_research_only:
            signal_data["quick_backtest_30d"] = {
                "pending": True,
                "research_only": True,
                "reason": "S7 is tracked by faithful V2 out-of-sample validation, not the legacy quick backtest",
            }
        if best_strategy:
            signal_data["best_strategy"] = {
                "risk_mode": best_strategy["risk_mode"],
                "trade_mode": best_strategy["trade_mode"],
                "win_rate": round(float(best_strategy["win_rate"]), 1),
                "total_signals": int(best_strategy["total_signals"]),
                "wins": int(best_strategy["wins"]),
                "losses": int(best_strategy["losses"]),
            }

        size_factor = 1.0
        size_info = {
            "size_factor": 1.0,
            "base_factor": 1.0,
            "kelly_fraction": 0.0,
            "expectancy_r": None,
            "win_rate": None,
            "payoff_ratio": None,
        }
        if cfg.get("dynamic_sizing", True):
            bt_wr = None
            bt_payoff = None
            bt_expectancy = None
            if quick_bt_results:
                bt_wr = quick_bt_results.get("win_rate_trades", quick_bt_results.get("win_rate"))
                bt_payoff = quick_bt_results.get("payoff_ratio")
                bt_expectancy = quick_bt_results.get("expectancy_r")
            size_info = compute_size_factor(
                score=signal_data.get("signal_score", 0),
                cfg=cfg.get("sizing", {}),
                expectancy_r=bt_expectancy,
                win_rate=bt_wr,
                payoff_ratio=bt_payoff,
            )
            size_factor = float(size_info.get("size_factor", 1.0) or 1.0)

        # `agents.risk.position_size_pct` (the "Risk per trade (%)" field) is the
        # BASE POSITION NOTIONAL in % of the deployable balance: that is how
        # every reader that sizes a paper entry uses it (signals.add_signal,
        # AgentManager size caps). This block used to call it "risk %" and
        # derived risk_per_trade / position_size as if that share of equity
        # were at risk (10% "risk" on a 2% stop = a 500% position). Nothing
        # sized a position from those two fields; they were wrong on the
        # record only. Loss at the stop is limited by max_risk_per_trade_pct.
        from agents.risk_agent import resolve_position_limits

        position_limits = resolve_position_limits(
            self.config.agents.get("risk", {}), default_base_notional_pct=2.0
        )
        base_risk_pct = float(position_limits["legacy_position_size_pct"] or 2.0)
        position_size_pct = base_risk_pct * size_factor
        position_notional_pct = position_size_pct
        if position_limits["max_position_notional_pct"] is not None:
            # The cap signals.add_signal applies to the precomputed value
            # (which stays as it was, so add_signal still records the capping).
            position_notional_pct = min(position_size_pct, float(position_limits["max_position_notional_pct"]))
        equity = resolve_deployable_balance(get_capital_manager())
        position_notional = equity * (position_notional_pct / 100.0)
        entry_for_size = float(signal_data.get("entry", 0) or 0)
        stop_distance = abs(entry_for_size - float(signal_data.get("sl", 0) or 0))
        stop_fraction = (stop_distance / entry_for_size) if entry_for_size > 0 else 0.0
        # USD lost if the stop is hit, and the units the notional buys.
        risk_per_trade = position_notional * stop_fraction
        position_size = (position_notional / entry_for_size) if entry_for_size > 0 else 0.0

        avg_atr_20d = float(signal_data.get("avg_atr_20d", 0.0) or 0.0)
        atr_reduction_applied = False
        if avg_atr_20d > 0 and atr_value > (avg_atr_20d * 2.0):
            position_size *= 0.7
            atr_reduction_applied = True

        signal_data["size_factor"] = round(size_factor, 3)
        signal_data["base_risk_pct"] = round(base_risk_pct, 3)
        signal_data["position_size_pct"] = round(position_size_pct, 3)
        signal_data["risk_per_trade"] = round(risk_per_trade, 6)
        signal_data["position_size"] = round(position_size, 8)
        # Explicit names for the two different quantities.
        signal_data["position_notional_pct"] = round(position_notional_pct, 3)
        signal_data["risk_per_trade_pct"] = round(position_notional_pct * stop_fraction, 4)
        signal_data["max_risk_per_trade_pct"] = round(float(position_limits["max_risk_per_trade_pct"]), 4)
        signal_data["atr_position_reduction_applied"] = atr_reduction_applied
        signal_data["kelly_fraction"] = size_info.get("kelly_fraction")
        signal_data["expectancy_r_used"] = size_info.get("expectancy_r")
        signal_data["win_rate_used"] = size_info.get("win_rate")
        signal_data["payoff_ratio_used"] = size_info.get("payoff_ratio")
        if isinstance(signal_data.get("trigger_info"), dict):
            signal_data["trigger_info"]["size_factor"] = signal_data.get("size_factor")
            signal_data["trigger_info"]["confidence_factor"] = signal_data.get("confidence_factor")
            signal_data["trigger_info"]["performance_penalty"] = signal_data.get("performance_penalty")

        self.signals.add_signal(signal_data)
        # STRICT replacement: now that the new signal is successfully stored, close
        # the prior CUSTOM signal for this ticker (settling its P&L at the entry of
        # the replacement). exclude_sids keeps the just-added replacement open. This
        # runs only on the success path, so an add_signal failure leaves the prior
        # position intact instead of silently dropping it (H3).
        if replace_custom_with_strict:
            try:
                self.signals.close_signal_manual(
                    ticker,
                    "REPLACED_BY_STRICT",
                    exit_price=replace_exit_price,
                    exclude_sids={signal_id},
                )
            except Exception as exc:
                logger.error(f"STRICT replacement close failed for {ticker}: {exc}")
        self.agent_manager.on_new_signal(signal_data)
        self.last_signal_time[ticker] = now
        self.new_signals.append(signal_id)

        logger.info(
            f"[{signal['variant']}/{new_variant_mode}] {ticker} {signal['type']} score={final_score:.2f} "
            f"conf={confidence_factor:.2f} exp30={expectancy_30d:.2f} pf30={profit_factor_30d:.2f} wr30={win_rate_30d:.1f}%"
        )

    def _run_paper_crypto_strategies(self, snapshot: dict, cryptos: List[str], now: datetime) -> None:
        execution_cfg = self.config.agents.get("execution", {}) or {}
        if not execution_cfg.get("enabled", False):
            return
        if str(execution_cfg.get("mode", "paper")).lower() != "paper":
            return

        paper_cfg = execution_cfg.get("paper_strategies", {}) or {}
        if not paper_cfg.get("enabled", False):
            return

        configured_tickers = {
            str(ticker or "").upper().strip()
            for ticker in (paper_cfg.get("tickers", []) or [])
            if str(ticker or "").strip()
        }
        targets = [
            ticker for ticker in (cryptos or [])
            if not configured_tickers or str(ticker or "").upper().strip() in configured_tickers
        ]
        if not targets:
            return

        validation_cfg = paper_cfg.get("validation", {}) or {}
        lookback_1h = max(240, int(validation_cfg.get("lookback_bars_1h", 240) or 240))
        lookback_4h = max(240, int(validation_cfg.get("lookback_bars_4h", 240) or 240))

        for ticker in targets:
            try:
                df_1h = BinanceClient.fetch_klines(ticker, "1h", limit=lookback_1h, ttl=30)
                df_4h = BinanceClient.fetch_klines(ticker, "4h", limit=lookback_4h, ttl=60)
                candidate = generate_paper_btc_signal(ticker, df_1h, df_4h, config=paper_cfg)
                if not candidate:
                    continue

                strategy_key = str(candidate.get("paper_strategy_key") or "").strip()
                if strategy_key and strategy_key in self.paper_strategy_emitted:
                    continue

                signal_id = self._ingest_paper_strategy_signal(candidate, "crypto", now)
                if not signal_id:
                    continue

                if strategy_key:
                    self.paper_strategy_emitted[strategy_key] = signal_id
                snapshot.setdefault("tickers", {}).setdefault(ticker, {})
                snapshot["tickers"][ticker]["status"] = "SEMNAL"
            except Exception as e:
                logger.error(f"Paper BTC strategy failed for {ticker}: {e}")

    def _ingest_paper_strategy_signal(self, signal: dict, asset_type: str, now: datetime) -> Optional[str]:
        ticker = str(signal.get("ticker") or "").upper().strip()
        action = str(signal.get("type") or signal.get("action") or "").upper()
        strategy_id = str(signal.get("strategy_id") or "paper_strategy").strip()
        if not ticker or action not in {"BUY", "SHORT"}:
            return None

        ticker_settings = self.config.get_effective_ticker_settings(ticker) or {}
        if ticker_settings.get("signals_enabled") is False:
            logger.info(f"Paper strategy {strategy_id} ignored for {ticker}: ticker signals disabled")
            return None

        allowed_strategies = ticker_settings.get("allowed_strategies", []) or []
        current_strategy = f"{str(signal.get('risk_mode', 'MEDIUM')).upper()}/{str(signal.get('signal_mode', 'PULLBACK')).upper()}"
        if allowed_strategies and current_strategy not in allowed_strategies:
            logger.info(f"Paper strategy {strategy_id} ignored for {ticker}: strategy {current_strategy} not allowed")
            return None

        if self.signals.has_open_signal_for_ticker(ticker):
            logger.info(f"Paper strategy {strategy_id} ignored for {ticker}: active signal already open")
            return None

        cooldown_minutes = int(
            self.config.signal.get("signal_cooldown_minutes", DEFAULT_TICKER_COOLDOWN_MINUTES)
            or DEFAULT_TICKER_COOLDOWN_MINUTES
        )
        if not self.signals.can_open_new_signal_for_ticker(
            ticker,
            cooldown_minutes,
            int(self.config.signal.get("stop_lockout_minutes", DEFAULT_STOP_LOCKOUT_MINUTES) or 0),
        ):
            logger.info(f"Paper strategy {strategy_id} ignored for {ticker}: cooldown active")
            return None

        signal_id = f"{ticker}_paper_{now.strftime('%Y%m%d%H%M%S%f')}"
        signal_score = float(signal.get("signal_score", signal.get("score", 80.0)) or 80.0)
        # News context for the AI decision layer. The equity V1/V2 path
        # (_analyze_ticker) attaches news unconditionally; the paper-crypto path
        # pre-fix did NOT, so every paper-crypto signal was AI-reviewed news-blind
        # (contra "integrează AI prin analiza news"). Fetch the pulse here too
        # (45s TTL cache -> cheap; allow_blocking=False never stalls the engine).
        _paper_pulse = {"score": 0, "sentiment_trend": "unknown", "top_headlines": []}
        try:
            from core.news_pulse import build_news_pulse as _bnp
            _pp = _bnp(ticker, allow_blocking=False)
            if isinstance(_pp, dict):
                _paper_pulse = _pp
        except Exception as _exc:
            logger.debug(f"news_pulse unavailable for paper {ticker}: {_exc}")
        _paper_news_score = float(_paper_pulse.get("score") or 0.0)
        _paper_news_sent = str(_paper_pulse.get("sentiment_trend") or "neutral").lower()
        _paper_top = list(_paper_pulse.get("top_headlines") or [])[:3]
        _paper_headlines = [h.get("title", "") for h in _paper_top if isinstance(h, dict)]
        signal_data = {
            "id": signal_id,
            "ticker": ticker,
            "type": action,
            "action": action,
            "entry": float(signal.get("entry", 0.0) or 0.0),
            "tp": float(signal.get("tp", 0.0) or 0.0),
            "sl": float(signal.get("sl", 0.0) or 0.0),
            "tp_pct": float(signal.get("tp_pct", 0.0) or 0.0),
            "sl_pct": float(signal.get("sl_pct", 0.0) or 0.0),
            "exit_mode": str(signal.get("exit_mode", "FIXED_PCT")),
            "atr": float(signal.get("atr", signal.get("atr_value", 0.0)) or 0.0),
            "atr_value": float(signal.get("atr_value", signal.get("atr", 0.0)) or 0.0),
            "avg_atr_20d": float(signal.get("avg_atr_20d", signal.get("atr_value", 0.0)) or 0.0),
            "spread_estimate": float(signal.get("spread_estimate", 0.0) or 0.0),
            "rr_ratio": float(signal.get("rr_ratio", 0.0) or 0.0),
            "probability_score": float(signal.get("probability_score", 0.0) or 0.0),
            "raw_logit_value": float(signal.get("raw_logit_value", 0.0) or 0.0),
            "mtf_alignment": str(signal.get("mtf_alignment", "4h bearish filter")),
            "market_regime": str(signal.get("market_regime", "BEAR_TREND")),
            "regime_detected": str(signal.get("regime_detected", signal.get("market_regime", "BEAR_TREND"))),
            "regime_strength": float(signal.get("regime_strength", 0.0) or 0.0),
            "regime_details": dict(signal.get("regime_details", {}) or {}),
            "adx_5m": float(signal.get("adx_5m", 0.0) or 0.0),
            "bb_width_5m": float(signal.get("bb_width_5m", 0.0) or 0.0),
            "atr_percentile_5m": float(signal.get("atr_percentile_5m", 0.0) or 0.0),
            "variant": str(signal.get("variant", "A")),
            "variant_description": str(signal.get("variant_description", "BTC paper strategy")),
            "variant_mode": str(signal.get("variant_mode", "STRICT")),
            "mtf_score": int(signal.get("mtf_score", 6) or 6),
            "quality_score": int(signal.get("quality_score", 5) or 5),
            "rsi": float(signal.get("rsi", 50.0) or 50.0),
            "macd": float(signal.get("macd", signal.get("macd_hist", 0.0)) or 0.0),
            "macd_hist": float(signal.get("macd_hist", 0.0) or 0.0),
            "risk_mode": str(signal.get("risk_mode", "STRONG")).upper(),
            "score": signal_score,
            "raw_score": signal_score,
            "signal_score": signal_score,
            "trade_mode": str(signal.get("signal_mode", "BREAKOUT")).upper(),
            "signal_mode": str(signal.get("signal_mode", "BREAKOUT")).upper(),
            "signal_threshold": float(signal.get("signal_threshold", 60.0) or 60.0),
            "score_breakdown": dict(signal.get("score_breakdown", {}) or {}),
            # News context for the AI reviewer (ollama_decision._compact_signal
            # whitelists these). Attached on the paper-crypto path too (A3).
            "news_score": _paper_news_score,
            "news_sentiment": _paper_news_sent,
            "news_headlines": _paper_headlines,
            "news_top_headlines": _paper_top,
            "top_headlines": _paper_top,
            "news_sentiment_trend": _paper_news_sent,
            "news_pulse_score": _paper_news_score,
            "created_at": now.isoformat(),
            "created_at_time": now.strftime("%H:%M:%S"),
            "status": str(signal.get("status", "Open")),
            "asset_type": asset_type,
            "asset_class": "CRYPTO_FUTURES" if asset_type == "crypto" else "US_STOCKS",
            "strategy_source": "PAPER_STRATEGY",
            "strategy_type": str(strategy_id).upper(),
            "expectancy_R_30d": float(signal.get("expectancy_R_30d", 0.0) or 0.0),
            "profit_factor_30d": float(signal.get("profit_factor_30d", 0.0) or 0.0),
            "win_rate_30d": float(signal.get("win_rate_30d", 0.0) or 0.0),
            "trades_30d": int(signal.get("trades_30d", 0) or 0),
            "performance_penalty": 0.0,
            "penalized_by_performance": False,
            "confidence_factor": float(signal.get("confidence_factor", 1.0) or 1.0),
            "size_factor": 1.0,
            "position_size_pct": float(signal.get("position_size_pct", self.config.agents.get("execution", {}).get("balance_usage_pct", 10.0)) or 10.0),
            "paper_strategy_key": signal.get("paper_strategy_key"),
            "strategy_id": strategy_id,
            "strategy_family": str(signal.get("strategy_family", "paper_btc")),
            "strategy_timeframe": str(signal.get("timeframe", "")),
            "strategy_candle_time": signal.get("strategy_candle_time"),
            "trigger_info": {
                "strategy_id": strategy_id,
                "timeframe": signal.get("timeframe"),
                "reasons": list(signal.get("reasons", []) or []),
                "paper_trading": True,
            },
        }
        signal_data["quick_backtest_30d"] = {
            "wins": int(signal.get("backtest_wins", 0) or 0),
            "losses": int(signal.get("backtest_losses", 0) or 0),
            "total": int(signal.get("backtest_trade_count", 0) or 0),
            "trade_count": int(signal.get("backtest_trade_count", 0) or 0),
            "win_rate": float(signal.get("win_rate_30d", 0.0) or 0.0),
            "win_rate_trades": float(signal.get("win_rate_30d", 0.0) or 0.0),
            "expectancy_r": float(signal.get("expectancy_R_30d", 0.0) or 0.0),
            "profit_factor": float(signal.get("profit_factor_30d", 0.0) or 0.0),
            "payoff_ratio": float(signal.get("rr_ratio", 0.0) or 0.0),
            "risk_mode": signal_data["risk_mode"],
            "trade_mode": signal_data["signal_mode"],
            "variant_mode": signal_data["variant_mode"],
            "paper_strategy": True,
        }

        try:
            self.signals.add_signal(signal_data)
        except Exception as e:
            logger.warning(f"Paper strategy {strategy_id} rejected for {ticker}: {e}")
            return None

        self.agent_manager.on_new_signal(signal_data)
        self.last_signal_time[ticker] = now
        self.new_signals.append(signal_id)
        logger.info(
            f"[PAPER/{strategy_id}] {ticker} {action} score={signal_score:.2f} "
            f"entry={signal_data['entry']:.2f} tp={signal_data['tp']:.2f} sl={signal_data['sl']:.2f}"
        )
        return signal_id

    def get_watchlist_state(self) -> List[dict]:
        """
        Get the current state of the watchlist (public API).
        Returns a list of ticker data dictionaries.
        """
        with self.state_cache_lock:
            if not self.state_cache:
                try:
                    loaded_state = load_state_payload({})
                    self.state_cache = loaded_state if isinstance(loaded_state, dict) else {}
                except Exception as e:
                    logger.error(f"Failed to load state: {e}")
                    self.state_cache = {}

            tickers = self.state_cache.get("tickers", {})
            # Return list of values, adding ticker key if missing
            results = []
            for ticker, data in tickers.items():
                item = data.copy()
                item["ticker"] = ticker
                results.append(item)
            if results:
                return results

        # Fallback: build a minimal list from watchlist if state cache is empty
        tickers = []
        try:
            if WATCHLIST_PATH.exists():
                with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    tickers.extend(data.get("stocks", []) or [])
                    tickers.extend(data.get("crypto", []) or [])
        except Exception as e:
            logger.error(f"Failed to load watchlist: {e}")

        if not tickers:
            tickers = FALLBACK_STOCKS + FALLBACK_CRYPTOS

        return [{"ticker": t} for t in tickers if t]

    def run_forever(self) -> None:
        logger.info("═" * 50)
        logger.info("🚀 Market Engine V4 ENHANCED (modularizat)")
        logger.info("═" * 50)

        # Log which data/runtime folders and database this process uses and
        # remove temp files that interrupted atomic writes left behind.
        try:
            from core.config import startup_storage_check
            startup_storage_check("engine")
        except Exception as e:
            logger.warning(f"Startup storage check failed: {e}")

        # NOTE: Initial backtest disabled - backtest runs automatically on each signal now
        # Quick backtest (7 days) is performed when a signal is generated
        # No need to backtest all tickers at startup
        # if self.config.backtest.get("enabled", True):
        #     logger.info("📊 Running initial backtest...")
        #     self.last_backtest_results = self.backtester.run_full_backtest(
        #         self.config.backtest.get("days", 30)
        #     )

        self.start_exit_watch()

        while True:
            try:
                self.run_scan()
            except KeyboardInterrupt:
                logger.info("🛑 Oprit - Ai apasat STOP")
                break
            except Exception as e:
                logger.error(f"❌ {e}", exc_info=True)
            time.sleep(self.config.scan.get("scan_interval_seconds", 5))


if __name__ == "__main__":
    MarketEngine().run_forever()
