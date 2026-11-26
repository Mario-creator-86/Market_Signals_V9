# core/config.py - config, paths, file helpers, time

import copy
import json
import logging
import os
import re
import shutil
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from core.sqlite_store import get_sqlite_store

# ========= PATHS / DIRECTORIES =========

# This file is in core/, so BASE_DIR is one level up (project root).
BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path(os.getenv("MS_RUNTIME_DIR") or (BASE_DIR / "runtime"))
DATA_DIR = Path(os.getenv("MS_DATA_DIR") or (BASE_DIR / "data"))

# Local V7/V9 installations historically stored mutable JSON next to source.
# Move forward by copying it once into the ignored runtime boundary. Tests and
# explicit deployments own their paths and must never import operator data.
_IS_TEST_RUNTIME = str(
    os.getenv("TEST_MODE") or os.getenv("MS_TEST_MODE") or "0"
).strip().lower() in {"1", "true", "yes", "on"}
if not os.getenv("MS_RUNTIME_DIR") and not _IS_TEST_RUNTIME:
    from core.runtime_layout import migrate_legacy_runtime_layout

    migrate_legacy_runtime_layout(BASE_DIR, RUNTIME_DIR, DATA_DIR)

CONFIG_PATH = Path(os.getenv("MS_CONFIG_PATH") or (RUNTIME_DIR / "config.json"))
# Env overrides let the test suite redirect runtime state to a temp dir
# instead of mutating the developer's real state files.
STATE_PATH = Path(os.getenv("MS_STATE_PATH") or (RUNTIME_DIR / "state.json"))
SIGNALS_PATH = Path(os.getenv("MS_SIGNALS_PATH") or (RUNTIME_DIR / "signals_state.json"))
SYMBOL_CACHE_PATH = Path(
    os.getenv("MS_SYMBOL_CACHE_PATH") or (RUNTIME_DIR / "symbol_status.json")
)
WATCHLIST_PATH = Path(
    os.getenv("MS_WATCHLIST_PATH") or (RUNTIME_DIR / "watchlist.json")
)
PRESETS_PATH = Path(os.getenv("MS_PRESETS_PATH") or (RUNTIME_DIR / "presets.json"))
HOTLIST_CACHE_PATH = Path(
    os.getenv("MS_HOTLIST_CACHE_PATH") or (RUNTIME_DIR / "hotlist_cache.json")
)
SUPPRESSED_TICKERS_PATH = Path(
    os.getenv("MS_SUPPRESSED_TICKERS_PATH")
    or (RUNTIME_DIR / "suppressed_tickers.json")
)
BACKTEST_RESULTS_PATH = Path(
    os.getenv("MS_BACKTEST_RESULTS_PATH") or (DATA_DIR / "backtest_results.json")
)
TRADE_RADAR_STATE_PATH = Path(
    os.getenv("MS_TRADE_RADAR_STATE_PATH") or (DATA_DIR / "trade_radar_state.json")
)
LOG_DIR = Path(os.getenv("MS_LOG_DIR") or (BASE_DIR / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)


def data_path(*parts: str) -> Path:
    """A path inside the configured data folder (``MS_DATA_DIR``).

    Every module that keeps mutable state in ``data/`` builds its path here
    (or from ``DATA_DIR``). A module that hard-codes ``<repo>/data`` or the
    working directory makes a second instance, started with its own
    ``MS_DATA_DIR``, read and write the first instance's files.
    """
    return Path(DATA_DIR).joinpath(*parts)


def runtime_path(*parts: str) -> Path:
    """A path inside the configured runtime folder (``MS_RUNTIME_DIR``)."""
    return Path(RUNTIME_DIR).joinpath(*parts)


def resolve_state_path(value: Any, default: Optional[Path] = None) -> Path:
    """Resolve a state-file path that came from the config file.

    Absolute paths are used as given. A relative path is never resolved
    against the working directory (the server may be started from anywhere):
    ``data/...`` means the configured data folder, ``runtime/...`` the
    configured runtime folder, anything else is relative to the install.
    """
    raw = str(value or "").strip()
    if not raw:
        if default is None:
            raise ValueError("resolve_state_path needs a value or a default")
        return Path(default)
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    parts = path.parts
    if parts and parts[0] == "data":
        return data_path(*parts[1:])
    if parts and parts[0] == "runtime":
        return runtime_path(*parts[1:])
    return BASE_DIR / path

SQLITE_KEY_CONFIG = "config"
SQLITE_KEY_CONFIG_MTIME = "config_file_mtime"
SQLITE_KEY_WATCHLIST = "watchlist"

BUCHAREST_TZ = pytz.timezone("Europe/Bucharest")

logger = logging.getLogger("MarketEngine")
_CONFIG_PROCESS_LOCK = threading.RLock()


@contextmanager
def _config_write_lock(path: Path):
    """Serialize config mutations across API and engine processes."""
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _CONFIG_PROCESS_LOCK:
        with open(lock_path, "a+b") as lock_file:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                lock_file.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _discard_temp_file(temp_path: Optional[Path]) -> bool:
    """Remove an atomic-write temp file; True when it is gone.

    On Windows a just-written file can stay locked for a moment (virus
    scanner, indexer). A single failed ``unlink`` used to be swallowed and the
    multi-MB temp file stayed in the data folder for good, so the removal is
    retried briefly. Whatever still survives is picked up by
    ``sweep_orphan_temp_files`` at the next start.
    """
    if temp_path is None:
        return True
    import time

    target = Path(temp_path)
    for attempt in range(5):
        try:
            target.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(0.05 * (attempt + 1))
    return not target.exists()


def _write_json_atomic(path: Path, data: Any, *, indent: int = 2, ensure_ascii: bool = False) -> None:
    """Atomic JSON write to avoid partial/empty file reads during concurrent access."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
        temp_path = Path(tmp_name)
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)  # an open descriptor would keep the temp file undeletable
            raise
        with handle as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)
        import time
        for attempt in range(5):
            try:
                os.replace(str(temp_path), str(path))
                temp_path = None
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1)
    finally:
        # Reached with a temp file only when the write or the replace failed.
        if temp_path is not None:
            _discard_temp_file(temp_path)


# ========= TEMP-FILE HYGIENE =========

# The names the app's own atomic writers give their temp files. The random
# part is tempfile.mkstemp's: 8 characters from [a-z0-9_].
_ORPHAN_TEMP_PATTERNS = (
    # mkstemp(suffix=".tmp") with the default prefix: engine.atomic_write_json,
    # the news backup, research/promotion/review/auto-repair stores.
    re.compile(r"^tmp[a-z0-9_]{8}\.tmp$"),
    # mkstemp(prefix="<name>.", suffix=".tmp"): state.json, signals_state.json,
    # capital.json, config/watchlist, trade_decisions, journal, caches.
    re.compile(r"^.+\.[a-z0-9_]{8}\.tmp$"),
    re.compile(r"^(?:calendar_cache|analysis_store)_[a-z0-9_]{8}\.tmp$"),
    # ".<name>.<pid>.<thread>.tmp": SEC cache, pre-earnings stores.
    re.compile(r"^\..+\.\d+\.\d+(?:\.schema-upgrade)?\.tmp$"),
    # "<name>.<pid>.tmp": Telegram delivery status.
    re.compile(r"^.+\.\d+\.tmp$"),
    # ".<name>.XXXXXXXX.migrate": runtime-layout migration copy.
    re.compile(r"^\..+\.[a-z0-9_]{8}\.migrate$"),
)
ORPHAN_TEMP_MIN_AGE_SECONDS = 15 * 60


def is_orphan_temp_name(name: str) -> bool:
    """True when ``name`` has the shape of one of the app's own temp files."""
    return any(pattern.match(str(name)) for pattern in _ORPHAN_TEMP_PATTERNS)


def sweep_orphan_temp_files(
    directories: Optional[List[Path]] = None,
    *,
    min_age_seconds: float = ORPHAN_TEMP_MIN_AGE_SECONDS,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Delete temp files that interrupted atomic writes left behind.

    Every writer removes its temp file when a write fails, but a process that
    is killed inside the write window cannot. Nothing else deleted those files
    (149 files / 2.2 GB had piled up in ``data/`` and ``runtime/``). Called
    once when the server and the engine start.

    Only regular files directly inside the data and runtime folders are
    considered, only when the name matches a pattern the app's own writers
    use, and only when the file has not been written for ``min_age_seconds``
    (a write in flight is seconds old). Sub-folders are never entered.
    """
    import time

    roots = [DATA_DIR, RUNTIME_DIR] if directories is None else list(directories)
    cutoff = (time.time() if now is None else float(now)) - max(0.0, float(min_age_seconds))
    removed = 0
    freed_bytes = 0
    failed = 0
    seen: set = set()
    for directory in roots:
        try:
            root = Path(directory).resolve()
        except OSError:
            continue
        if root in seen:
            continue
        seen.add(root)
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue  # folder missing or unreadable: nothing to sweep
        for entry in entries:
            try:
                if not is_orphan_temp_name(entry.name):
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                info = entry.stat(follow_symlinks=False)
                if info.st_mtime >= cutoff:
                    continue
                os.unlink(entry.path)
                removed += 1
                freed_bytes += int(info.st_size)
            except OSError:
                failed += 1
    freed_mb = round(freed_bytes / (1024 * 1024), 1)
    result = {
        "removed": removed,
        "freed_bytes": freed_bytes,
        "freed_mb": freed_mb,
        "failed": failed,
        "directories": [str(path) for path in seen],
    }
    if removed or failed:
        logger.info(
            "Temp-file sweep: removed %d orphaned temp file(s), %.1f MB freed, %d could not be removed (%s)",
            removed, freed_mb, failed, ", ".join(sorted(result["directories"])),
        )
    else:
        logger.info("Temp-file sweep: no orphaned temp files (%s)", ", ".join(sorted(result["directories"])))
    return result


def startup_storage_check(role: str = "api") -> Dict[str, Any]:
    """Log where this process keeps its state, then sweep orphaned temp files.

    Called once from the server and the engine start paths. The log line is the
    evidence that an instance started with its own ``MS_DATA_DIR`` /
    ``MS_RUNTIME_DIR`` really uses them; an instance that has its own folders
    but still resolves the default database is reported as a warning.
    """
    from core import sqlite_store

    def _resolved(path: Path) -> Path:
        try:
            return Path(path).resolve()
        except OSError:
            return Path(path)

    data_dir = _resolved(DATA_DIR)
    runtime_dir = _resolved(RUNTIME_DIR)
    db_path = _resolved(sqlite_store.DB_PATH)
    logger.info(
        "Storage layout [%s]: data=%s runtime=%s config=%s sqlite=%s (path from %s)",
        role, data_dir, runtime_dir, _resolved(CONFIG_PATH), db_path,
        sqlite_store.DB_PATH_SOURCE,
    )
    default_data = _resolved(BASE_DIR / "data")
    default_runtime = _resolved(BASE_DIR / "runtime")
    own_folders = data_dir != default_data or runtime_dir != default_runtime
    shares_default_db = db_path == default_data / sqlite_store.DB_FILENAME
    if own_folders and shares_default_db:
        logger.warning(
            "Storage layout [%s]: this instance has its own data/runtime folder but uses the "
            "default database %s; it shares watchlist, config and capital state with the "
            "default instance. Set MS_DATA_DIR (or MS_SQLITE_DB_PATH) to separate them.",
            role, db_path,
        )
    try:
        sweep = sweep_orphan_temp_files()
    except Exception as exc:  # never block a start on housekeeping
        logger.warning("Temp-file sweep failed: %s", exc)
        sweep = {"removed": 0, "freed_bytes": 0, "freed_mb": 0.0, "failed": 0, "error": str(exc)}
    return {
        "role": role,
        "data_dir": str(data_dir),
        "runtime_dir": str(runtime_dir),
        "sqlite_path": str(db_path),
        "sqlite_path_source": sqlite_store.DB_PATH_SOURCE,
        "shares_default_database": bool(own_folders and shares_default_db),
        "sweep": sweep,
    }

# ========= DEFAULT CONFIG =========

FALLBACK_STOCKS = ["NVDA", "AAPL", "MSFT", "META", "AMZN", "TSLA"]
FALLBACK_CRYPTOS = ["BTC-USD", "ETH-USD"]

DEFAULT_CONFIG: Dict = {
    "signal_settings": {
        "profit_target_pct": 4.0,
        "stop_loss_pct": 2.0,
        "max_live_age_seconds": 900,
        "backtest_min_trades_for_validation": 30,
        "atr_multiplier_tp": 2.0,
        "atr_multiplier_sl": 1.5,

        "exit_mode": "ATR",
        "atr_period": 14,
        "atr_timeframe": "5m",
        "atr_multipliers_by_mode": {
            "PULLBACK": {"tp": 3.0, "sl": 1.5},
            "BREAKOUT": {"tp": 2.5, "sl": 1.2},
            "CONTRA": {"tp": 2.0, "sl": 0.8}
        },
        "min_tp_pct": 0.2,
        "dynamic_sizing": True,
        "enable_regime_gating": True,
        "variant_mode_default": "STRICT",
        "scoring": {
            "mtf_alignment_min": 6,
            "min_probability": 0.9,
            "logistic_coeffs": {
                "bias": -1.0,
                "mtf": 2.2,
                "macd": 1.3,
                "rsi": 0.9,
                "vwap": 0.7,
                "ema": 0.7
            },
            "macd_norm_scale": 0.01,
            "distance_atr_max": {
                "PULLBACK": 1.5,
                "BREAKOUT": 2.5,
                "CONTRA": 3.0
            }
        },
        "regime": {
            "adx_trend_min": 30,
            "adx_range_max": 12,
            "bb_width_max": 0.03,
            "atr_percentile_high": 85,
            "atr_percentile_low": 40,
            "allow_volatile": False,
            "lookback_days": 20,
            "timeframe": "5m"
        },
        "gate": {
            "enabled": True,
            "adx_trend_min": 35,
            "adx_range_max": 22,
            "contra_vwap_extreme_atr": 3.0,
            "atr_pct_min": 0.001,
            "atr_pct_max": 0.04,
            "cooldown_bars": 20,
            "one_signal_per_swing": True,
            "swing_retrace_atr": 0.5
        },
        "variant_strict": {
            "scoring": {
                "mtf_alignment_min": 6,
                "min_probability": 0.9
            },
            "regime": {
                "adx_trend_min": 30,
                "adx_range_max": 12,
                "bb_width_max": 0.03,
                "atr_percentile_high": 85,
                "atr_percentile_low": 40,
                "allow_volatile": False
            },
            "gate": {
                "adx_trend_min": 35,
                "adx_range_max": 22,
                "contra_vwap_extreme_atr": 3.0,
                "atr_pct_min": 0.001,
                "atr_pct_max": 0.04,
                "cooldown_bars": 20
            }
        },
        "variant_custom": {
            "scoring": {
                "mtf_alignment_min": 4,
                "min_probability": 0.6
            },
            "regime": {
                "adx_trend_min": 20,
                "adx_range_max": 15,
                "bb_width_max": 0.04,
                "atr_percentile_high": 80,
                "atr_percentile_low": 20,
                "allow_volatile": True
            },
            "gate": {
                "adx_trend_min": 20,
                "adx_range_max": 25,
                "contra_vwap_extreme_atr": 2.5,
                "atr_pct_min": 0.0005,
                "atr_pct_max": 0.05,
                "cooldown_bars": 10
            }
        },
        "sizing": {
            "score_min_factor": 0.2,
            "score_max_factor": 1.0,
            "expectancy_min_factor": 0.5,
            "expectancy_max_factor": 1.5,
            "kelly_limit": 0.25,
            "kelly_min_factor": 0.5,
            "kelly_max_factor": 1.5
        },
        "rsi_period": 14,
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "min_win_rate": 0,
        "signal_cooldown_minutes": 15,
        "stop_lockout_minutes": 240,
        "duplicate_tolerance_pct": 0.5,
        "manual_delete_suppress_minutes": 60,
        "price_integrity": {
            "max_deviation_pct": {
                "stock": 3.0,
                "crypto": 2.5,
                "forex": 1.0,
                "commodity": 2.0,
            }
        },
    },
    "signal_variants": {
        "variant_a_mtf_min": 6,
        "variant_a_quality_min": 5,
        "variant_a_macd_hist_min_pos": 0.034,
        "variant_a_macd_hist_min_neg": -0.034,
        "variant_b_mtf_min": 4,
        "variant_b_quality_min": 3,
        "variant_b_macd_hist_min_pos": 0.032,
        "variant_b_macd_hist_min_neg": -0.032,
        "variant_c_mtf_min": 0,
        "variant_c_quality_min": 0,
        "variant_c_macd_hist_min_pos": 0.0001,
        "variant_c_macd_hist_min_neg": -0.0001,
        "weak_c_mtf_threshold": 2,
        "block_weak_c_signals": True,
        "require_macd_hist_threshold": True,
    },
    "filters": {
        "min_rsi_for_buy": 35,
        "max_rsi_for_short": 65,
    },
    "scan_settings": {
        "scan_interval_seconds": 60,
        "crypto_liquid_universe_size": 100,
        "crypto_hot_list_size": 50,
        "stock_refresh_seconds": 300,
        "crypto_price_interval_seconds": 30,
        "crypto_full_scan_seconds": 300,
        "stock_scan_max_workers": 2,
        "crypto_scan_max_workers": 2,
        "defer_initial_heavy_scan_seconds": 60,
    },
    "news": {
        "news_refresh_minutes": 5,
        "actionability_threshold": 60,
        "auto_summarize_top_n": 10,
        "narrative_ttl_seconds": 900,
        "hide_duplicates_default": True,
        "dedup_max_compare": 120,
        "startup_refresh_enabled": False,
        "defer_initial_refresh": True,
    },
    "news_v9": {
        "fallback_enabled": True,
        "duplicate_similarity": 0.78,
        "finbert": {
            "enabled": True,
            "model": "ProsusAI/finbert",
            "revision": "4556d13015211d73dccd3fdd39d39232506f3e43",
            "local_files_only": True,
            "max_chars": 4000,
        },
    },
    "sec": {
        "identity_env": "EDGAR_IDENTITY",
        "cache_path": str(DATA_DIR / "sec_filings_cache.json"),
        "cache_ttl_seconds": 3600,
        "max_filings": 20,
        "history_limit": 500,
        "forms": ["10-K", "10-Q", "8-K", "4"],
    },
    "data_sources": {
        "alpaca": {
            "enabled": True,
            "prefer_for_stocks": True,
            "crypto_fallback": True,
            "stock_feed": "iex",
            "crypto_feed": "us",
            "currency": "USD",
            "snapshot_ttl_seconds": 5,
            "data_base_url": "https://data.alpaca.markets",
            "trading_base_url": "https://paper-api.alpaca.markets",
        },
    },
    "backtest": {
        "enabled": True,
        "days": 30,
        "auto_run_on_config_change": True,
        "cache_ttl_seconds": 900,
    },
    "sounds": {
        "enabled": True,
        "signal_sound": "alert1",
        "win_sound": "success1",
        "loss_sound": "error1",
    },
    "strategy_notes": "",
    "ui": {
        "refresh_interval_ms": 5000,
        "max_log_lines": 500,
    },
    "watchlist": {
        "default_sort": "ticker",
        "filters": {
            "asset_type": "all",
            "signals_enabled": "all",
            "has_open_signal": "all",
            "earnings_bucket": "all",
            "news_actionability_min": 0,
            "trend_regime": "all",
            "earnings_next_7d": False,
        },
    },
    "news_pulse": {
        "ttl_seconds": 45,
    },
    "quick_ai": {
        "enabled": True,
        "ttl_near_earnings": 600,
        "ttl_normal": 1800,
    },
    "strategy_v2": {
        "signal_mode": "V2",
        "min_rr": 1.6,
        "target_win_rate": 0.60,
        "confluence_weight": 0.12,
        "max_signals_per_day_per_asset": {
            "US_STOCKS": 4,
            "CRYPTO_FUTURES": 6,
            "FOREX_MAJORS": 4,
            "COMMODITIES": 4,
        },
        "event_block_hours": 6,
        "min_sample_size": 24,
        "min_validation_score": 48,
        "allow_low_sample_fallback": False,
        "allow_low_validation_fallback": False,
        "require_profitable_performance": True,
        "lifecycle_enforcement_enabled": False,
        "emit_unvalidated_research_candidates": False,
        "research_only_strategy_ids": ["S7_LIQUIDITY_SWEEP_REVERSAL", "S8_SUPERTREND_BOS"],
        "research_signal_min_score": 40.0,
        "required_validation_method": "faithful_v2_walk_forward",
        "min_profit_factor": 1.05,
        "min_expectancy_R": 0.0,
        "max_drawdown_pct": 30.0,
        "performance_ttl_seconds": {
            "US_STOCKS": 86400,
            "CRYPTO_FUTURES": 21600,
            "FOREX_MAJORS": 43200,
            "COMMODITIES": 43200,
            "DEFAULT": 43200,
        },
        "costs_by_asset": {
            "US_STOCKS": {"commission_bps": 2.0, "entry_slippage_bps": 1.5, "slippage_sl_bps": 3.0, "slippage_tp_bps": 1.5},
            "CRYPTO_FUTURES": {"commission_bps": 4.0, "entry_slippage_bps": 2.5, "slippage_sl_bps": 5.0, "slippage_tp_bps": 2.5},
            "FOREX_MAJORS": {"commission_bps": 1.0, "entry_slippage_bps": 1.0, "slippage_sl_bps": 2.0, "slippage_tp_bps": 1.0},
            "COMMODITIES": {"commission_bps": 3.0, "entry_slippage_bps": 2.0, "slippage_sl_bps": 4.0, "slippage_tp_bps": 2.0},
        },
        "enabled_strategies_by_asset_class": {
            "US_STOCKS": ["S1_TREND_PULLBACK", "S2_BREAKOUT_RETEST", "S3_VOL_SQUEEZE", "S7_LIQUIDITY_SWEEP_REVERSAL", "S8_SUPERTREND_BOS"],
            "CRYPTO_FUTURES": ["S2_BREAKOUT_RETEST", "S3_VOL_SQUEEZE", "S5_MOMENTUM_CONTINUATION", "S4_MEAN_REVERSION", "S7_LIQUIDITY_SWEEP_REVERSAL", "S8_SUPERTREND_BOS"],
            "FOREX_MAJORS": ["S1_TREND_PULLBACK", "S2_BREAKOUT_RETEST", "S4_MEAN_REVERSION", "S6_FX_SESSION_BREAKOUT"],
            "COMMODITIES": ["S1_TREND_PULLBACK", "S4_MEAN_REVERSION", "S5_MOMENTUM_CONTINUATION", "S7_LIQUIDITY_SWEEP_REVERSAL", "S8_SUPERTREND_BOS"],
        },
        "research_agent": {
            "enabled": True,
            "interval_sec": 900,
            "max_symbols_per_cycle": 8,
            "selection_days": 90,
            "confirmation_days": 30,
            "walk_forward_train_days": 20,
            "walk_forward_test_days": 10,
            "max_windows": 4,
            "local_cache_only": False,
        },
    },
    "premarket": {
        "enabled": False,
        "refresh_seconds": 300,
        "lookback_hours": 24,
        "min_gap_pct": 4.0,
        "max_results": 30,
        "timeout_seconds": 8,
        "cache_ttl_seconds": 300,
        "use_ollama": True,
        "ollama_max_tickers": 10,
        "ollama_timeout_seconds": 6,
        "eliminate_reported": True,
        "pulse_enabled": True,
        "pulse_ttl_seconds": 300,
        "pulse_timeout_seconds": 12,
    },
    "pre_earnings": {
        "enabled": False,
        "refresh_seconds": 600,
        "lookahead_days": 10,
        "lookback_hours": 336,
        "max_results": 400,
        "cache_ttl_seconds": 600,
        "use_ollama": True,
        "ollama_max_tickers": 25,
        "ollama_timeout_seconds": 12,
        "reported_session_filter": True,
        "pulse_enabled": True,
        "pulse_ttl_seconds": 300,
        "pulse_timeout_seconds": 12,
        "coverage_warmup_max_symbols": 5,
        "feedback_lookback_days": 365,
        "feedback_min_samples": 24,
        "recalibration_min_samples": 40,
        "high_gap_threshold_pct": 5.0,
        "decision_enabled": True,
        "decision_max_tickers": 25,
        # A Pre-Earnings decision is kept this long while the ticker's inputs are
        # unchanged; a changed input is decided again at once. 0 = every cycle.
        "decision_reuse_minutes": 120,
        "continuous_scan": {
            "enabled": False,
            "interval_seconds": 60,
            "refresh_wait_seconds": 180,
        },
    },
    "bot": {
        "enabled": False,
        "mode": "paper",
        "min_edge_score": 64,
        "self_assessment_enabled": True,
        "self_assessment_sample_size": 100,
        "self_assessment_raise_profit_factor": 1.0,
        "self_assessment_lower_profit_factor": 1.8,
        "self_assessment_lower_min_win_rate": 0.50,
        "self_assessment_lower_min_expectancy": 0.0,
        "self_assessment_lower_max_drawdown_pct": 10.0,
        "self_assessment_step": 5,
        "self_assessment_min_edge_score": 52,
        "self_assessment_max_edge_score": 90,
        "circuit_breaker_drawdown_pct": 10.0,
        "circuit_breaker_daily_loss_pct": 1.0,
        "max_trades_per_day": 0,
        "max_same_strategy_concurrent": 0,
        "consecutive_loss_cooldown": 1,
        "require_strategy_performance": True,
        "allow_unvalidated_autonomous_fallback": False,
        "unvalidated_autonomous_size_factor": 0.25,
        "min_strategy_sample_size": 24,
        "min_strategy_profit_factor": 1.05,
        "min_strategy_expectancy_r": 0.0,
        "max_strategy_drawdown_pct": 30.0,
        "require_trade_radar_confirmation": True,
        "trailing_enabled": True,
        "trailing_activation_r": 1.0,
        "trailing_factor": 0.5,
        "time_exit_enabled": False,
        "time_exit_min_profit_r": 0.0,
        "max_holding_minutes": 720,
        "max_holding_bars": 0,
        "bar_seconds": 60,
        # Scanner research observations are short-horizon: close them when the
        # technical thesis has not progressed instead of leaving stale risk open.
        # Scoped to SCANNER positions only. 0 disables.
        "scanner_time_stop_minutes": 90,
        "pre_earnings_post_event_ai_review": True,
        "pre_earnings_exit_grace_minutes": 30,
    },
    "agents": {
        "execution": {
            "enabled": False,
            "mode": "paper",  # paper / live
            "exchange": "binance",
            "max_open_trades": 3,
            "balance_usage_pct": 0.5,
            "unprofitable_balance_usage_cap_pct": 0.5,
            "unprofitable_balance_usage_hard_cap_pct": 0.5,
            "profitability_min_sample": 24,
            "profitability_min_profit_factor": 1.05,
            "profitability_min_expectancy": 0.0,
            "autonomous_profitability_gate": {
                "enabled": True,
                "min_sample": 24,
                "min_profit_factor": 1.05,
                "min_expectancy_usd": 0.0,
                "max_drawdown_pct": 30.0,
            },
            "autonomous_news_gate": {
                "enabled": True,
                "require_fresh_context": True,
                "min_articles_24h": 1,
                "require_source_urls": True,
                "require_ai_analysis": True,
                "min_ai_analyzed_articles_24h": 1,
                "require_source_backed_ai_analysis": True,
                "min_source_backed_ai_analyzed_articles_24h": 1,
                "block_conflicting_sentiment": True,
                "require_news_for_non_major_crypto": False,
                "major_crypto_bases_requiring_news": [],
                "allow_blocking_fetch": True,
                "ttl_seconds": 45,
                "scanner_auto_fetch": True,
                "scanner_force_refresh_on_signal": False,
                "scanner_news_max_items": 5,
            },
            # Mandatory for every automated Capital entry.  Signals can still be
            # shown for research while this evidence bundle blocks execution.
            "capital_entry_gate": {
                "enabled": True,
                "require_ai_review": True,
                "data": {
                    "max_signal_age_seconds": 300,
                    "min_risk_reward_ratio": 1.1,
                    "require_price_integrity": True,
                },
                "backtest": {
                    "required_validation_method": "faithful_v2_walk_forward",
                    "min_sample": 24,
                    "min_profit_factor": 1.05,
                    "min_expectancy_r": 0.0,
                    "max_drawdown_pct": 30.0,
                    "max_age_seconds": 21600,
                    "max_age_seconds_by_asset": {
                        "US_STOCKS": 86400,
                        "CRYPTO_FUTURES": 21600,
                        "FOREX_MAJORS": 43200,
                        "COMMODITIES": 43200,
                    },
                },
                "realized_performance": {
                    "enabled": True,
                    "min_sample": 24,
                    "min_profit_factor": 1.05,
                    "min_expectancy_usd": 0.0,
                    "max_drawdown_pct": 30.0,
                },
                "news": {
                    "enabled": True,
                    "require_fresh_context": True,
                    "min_articles_24h": 1,
                    "require_source_urls": True,
                    "require_ai_analysis": True,
                    "min_ai_analyzed_articles_24h": 1,
                    "require_source_backed_ai_analysis": True,
                    "min_source_backed_ai_analyzed_articles_24h": 1,
                    "block_conflicting_sentiment": True,
                    "require_news_for_non_major_crypto": False,
                    "major_crypto_bases_requiring_news": [],
                    "allow_blocking_fetch": True,
                    "ttl_seconds": 45,
                },
            },
            # Explicit, simulated-only admission for selected unvalidated
            # strategy experiments. The active paper environment must opt in.
            "paper_capital": {
                "enabled": False,
                "allow_unvalidated_s7": False,
                "allow_unvalidated_strategies": False,
                "strategy_ids": ["S7_LIQUIDITY_SWEEP_REVERSAL", "S8_SUPERTREND_BOS"],
                "strategy_sources": ["V2", "V2_FORWARD_RESEARCH"],
            },
            "force_one_x_until_profitable": True,
            "require_ai_review": True,
            "allow_signal_only_ai_fallback": False,
            "ai_unavailable_size_factor": 0.5,
            "ai_review_timeout_seconds": 6,
            # P1 pre-earnings entry discipline (2026-09-02, history-driven):
            # accepted pre-earnings entries ran ~18% win rate with gap-through-
            # stop losses while approvals carried 0.42-0.55 confidence.
            "pre_earnings_min_ai_confidence": 0.6,
            "pre_earnings_gap_risk_block_level": 60,
            "pre_earnings_gap_risk_medium_size_factor": 0.5,
            # TradingAgents adoption: bull/bear entry debate + research-manager
            # verdict on surviving entry proposals (3 LLM calls per candidate).
            "debate_enabled": True,
            "debate_scope": "pre_earnings",
            "auto_execute_existing_signals": False,
            "max_reconcile_per_start": 0,
            "max_reconcile_per_cycle": 0,
            "reconcile_interval_seconds": 60,
            "min_reconcile_score": 75.0,
            "reconcile_reject_cooldown_seconds": 900,
            "reconcile_skip_when_risk_circuit_open": True,
            "reconcile_circuit_log_interval_seconds": 120,
            "autonomous_candidates_enabled": False,
            "autonomous_sources": {
                "scanner": True,
                "watchlist": True,
                "pre_earnings": True,
                "learning": True,
            },
            # Learning mode (paper only, owner 2026-11-24): Opus chooses the side of
            # scanner movers and turns fresh high-impact news into small trades,
            # toward 10,000 measured trades. See core/learning_mode/settings.py.
            "learning_mode": {
                "enabled": True,
                "min_confidence": 0.55,
                "position_pct_of_equity": 1.0,
                "max_new_per_day": 100,
                "target_closed_trades": 10000,
            },
            "autonomous_allocation": {
                "enabled": False,
                "buckets_pct": {
                    "stocks": 33.0,
                    "crypto": 33.0,
                    "pre_earnings": 33.0,
                },
                "fallback_when_empty": True,
            },
            "autonomous_monitor_interval_seconds": 120,
            "autonomous_cooldown_minutes": 45,
            "autonomous_ticker_lockout_hours": 6.0,
            "autonomous_allow_scale_in": False,
            "autonomous_require_ai_review": True,
            "research_prefer_us_stocks": False,
            # Small, isolated prospective sample for research hypotheses that
            # cannot satisfy the executable Capital evidence gate yet.
            "research_cohort": {
                "enabled": False,
                "paper_only": True,
                "allowed_sources": ["scanner", "v2_engine_research"],
                "allowed_strategy_sources": ["V2"],
                "max_open_positions": 1,
                "notional_pct_of_balance": 0.25,
                "min_notional_usd": 20.0,
                "max_notional_usd": 250.0,
                "max_signal_age_seconds": 180,
                "min_signal_score": 68.0,
                "min_risk_reward_ratio": 1.2,
                "max_entry_deviation_pct": 3.6,
                "ai_review_enabled": True,
                "block_ai_veto": True,
                "news_policy": {
                    "allow_missing_context": False,
                    "require_context_for_major_crypto": False,
                    "block_directional_conflict": True,
                },
            },
            "max_autonomous_candidates_per_cycle": 0,
            "max_autonomous_trades_per_cycle": 1,
            "autonomous_scanner_max_symbols_per_bucket": 50,
            "autonomous_watchlist_max_symbols": 100,
            "scanner_min_abs_change_pct": 5.0,
            "scanner_min_volume_24h": 1000000,
            "scanner_stop_pct": 0.04,
            "scanner_crypto_stop_pct": 0.03,
            # Crypto may run its own scanner entry regime; "" follows
            # scanner_entry_mode. "market_dip": long liquid coins that fell
            # hard while BTC sold off, with the stop/target/time stop below
            # (the exit the rule was backtested with).
            "scanner_crypto_entry_mode": "",
            "scanner_dip_min_drop_pct": 8.0,
            "scanner_dip_max_drop_pct": 25.0,
            "scanner_dip_btc_max_change_pct": -2.0,
            "scanner_dip_stop_pct": 0.06,
            # Target = 1.25 x the stop (7.5%). Every risk gate requires at least 1.2;
            # at 1.0 the rule could never trade. Backtest: no worse than 1.0.
            "scanner_dip_rr_ratio": 1.25,
            # Paper only: the AI's verdict on a dip candidate is recorded, not obeyed
            # (owner, 2026-11-24). false = the AI may veto it again.
            "scanner_dip_ai_advisory": True,
            "scanner_dip_time_stop_minutes": 1440,
            "scanner_rr_ratio": 1.8,
            "scanner_min_score": 84.0,
            "scanner_max_unconfirmed_abs_change_pct": 18.0,
            "scanner_momentum_max_move_pct": 18.0,
            "scanner_min_dollar_volume": 10_000_000,
            "scanner_require_late_momentum_confirmation": False,
            "scanner_late_min_pullback_pct": 0.6,
            "scanner_late_max_pullback_pct": 6.0,
            "scanner_late_min_consolidation_score": 60.0,
            "scanner_late_min_trade_radar_opportunity": 60.0,
            "scanner_late_min_news_score": 65.0,
            "scanner_late_min_relative_volume": 2.5,
            "pre_earnings_min_gap_risk": 30,
            "pre_earnings_min_abs_bias": 15,
            "pre_earnings_min_coverage_score": 50,
            "pre_earnings_stop_pct_min": 0.025,
            "pre_earnings_stop_pct_max": 0.08,
            "pre_earnings_rr_ratio": 1.8,
            "pre_earnings_min_score": 65.0,
            # AI's canonical OPEN/WATCH verdict is advisory unless the owner
            # explicitly promotes it to a hard autonomous-entry gate.
            "pre_earnings_require_canonical_decision": False,
            "pre_earnings_require_directional_edge": True,
            "pre_earnings_min_directional_sample": 100,
            "pre_earnings_min_directional_hit_rate": 50.0,
            "pre_earnings_window_hours": 2.0,
            "allow_ai_leverage": False,
            "max_ai_leverage": 1,
            "paper_strategies": {
                "enabled": False,
                "tickers": ["BTC-USD"],
                "prefer": ["bollinger_rsi_short_4h", "donchian_short_1h"],
                "validation": {
                    "enabled": True,
                    "lookback_bars_1h": 720,
                    "lookback_bars_4h": 360,
                    "default": {
                        "leverage": 1.0,
                        "cost_bps_per_side": 4.0,
                        "min_trades": 4,
                        "min_win_rate": 45.0,
                        "min_profit_factor": 1.2,
                        "max_drawdown_pct": 35.0,
                        "min_total_return_pct": 3.0,
                    },
                    "bollinger_rsi_short_4h": {
                        "min_trades": 3,
                        "min_win_rate": 55.0,
                        "max_drawdown_pct": 20.0,
                    },
                    "donchian_short_1h": {
                        "min_trades": 8,
                        "min_profit_factor": 1.5,
                        "max_drawdown_pct": 35.0,
                        "min_total_return_pct": 5.0,
                    },
                },
                "donchian": {
                    "enabled": True,
                    "fast": 18,
                    "slow": 36,
                    "entry_lookback": 24,
                    "exit_lookback": 8,
                    "rr_ratio": 2.2,
                    "min_stop_pct": 0.006,
                    "max_stop_pct": 0.04,
                    "signal_score": 84.0,
                    "position_size_pct": 0.5,
                },
                "bollinger": {
                    "enabled": True,
                    "fast": 12,
                    "slow": 96,
                    "bb_period": 24,
                    "bb_std": 1.5,
                    "entry_rsi": 55.0,
                    "exit_rsi": 50.0,
                    "rr_ratio": 1.8,
                    "sl_buffer_pct": 0.0025,
                    "signal_score": 88.0,
                    "position_size_pct": 0.5,
                },
            },
            "ticker_learning": {
                "enabled": True,
                "window": 20,
                "loss_streak_reduce": 3,
                "loss_streak_tighten": 4,
                "loss_streak_block": 5,
                "block_cooldown_hours": 6,
                "win_streak_boost": 3,
                "win_streak_boost2": 5,
                "max_leverage_cap": 5,
                "size_mult_l3": 0.5,
                "size_mult_l4": 0.25,
                "min_score_l3": 90.0,
                "min_score_l4": 93.0,
                "rr_mult_w3": 1.25,
                "rr_mult_w5": 1.5,
                "baseline_min_score": 78.0,
            },
        },
        "risk": {
            "enabled": True,
            "max_daily_loss_pct": 1.0,
            "max_daily_loss_trades": 2,
            "max_drawdown_pct": 5.0,
            "max_open_trades": 3,
            "max_exposure_pct": 15.0,
            "max_position_size_pct": 0.5,
            "max_risk_per_trade_pct": 0.5,
            "check_interval_sec": 60,
            "position_size_pct": 0.5,
        },
        "scanner": {
            "enabled": True,
            "startup_lazy_refresh_delay_seconds": 60,
            "watchlist_market_scan_enabled": True,
            "watchlist_market_scan_interval_seconds": 300,
            "watchlist_market_scan_max_symbols": 100,
        },
        "sentiment": {
            "enabled": False,
            "provider": "yahoo",
            "min_sentiment_score": 0.2,
            "news_impact_threshold": "High",
        },
        "news_tab": {
            "enabled": True,
        },
        "economic_calendar": {
            "enabled": True,
        },
        "trade_radar": {
            "enabled": True,
        },
        "strategy_research": {
            "enabled": True,
        },
        "premarket_scanner": {
            "enabled": False,
        },
        "pre_earnings": {
            "enabled": False,
        },
        "research_factory": {
            "enabled": True,
        },
        "coding_agent": {
            "enabled": True,
        },
        "critic_agent": {
            "enabled": True,
        },
        "ui_inspector": {
            "enabled": True,
            "interval_sec": 300,
        },
    },
    "ollama": {
        "model": "gemma3:12b",
        "cloud_model": "gpt-oss:120b",
        "local_cloud_model": "gpt-oss:120b-cloud",
        "base_url": "http://localhost:11434",
        "cloud_base_url": "https://ollama.com",
        "api_key_env": "OLLAMA_API_KEY",
        "use_cloud_when_api_key_present": True,
        "use_local_cloud_model_fallback": True,
        "timeout": 120,
        # Read timeout of the per-ticker "Analyze" AI call (Watchlist / Hot List /
        # Trade Radar). Bounded separately from `timeout` so a click never waits 120 s.
        "analysis_timeout_seconds": 60,
        "decision_enabled": True,
        "decision_veto_enabled": True,
        "decision_min_confidence": 0.65,
        "decision_allow_leverage": True,
        "decision_max_leverage": 2,
        "decision_timeout_seconds": 45,
        "exit_decision_enabled": True,
        "exit_decision_min_confidence": 0.8,
        "exit_tighten_stop_enabled": True,
        "exit_tighten_stop_min_confidence": 0.75,
        "exit_close_technical_risk_fraction": 0.75,
        "exit_close_post_earnings_flow_min_abs_score": 25.0,
        "exit_close_post_earnings_flow_min_confidence": 0.35,
        "exit_close_post_earnings_min_relative_volume": 1.5,
        # TradingAgents adoption: reflect on closed pre-earnings positions
        # (one bounded LLM call each) and re-inject lessons into §41 prompts.
        "reflection_enabled": True,
        "exit_decision_timeout_seconds": 45,
        "post_earnings_exit_decision_timeout_seconds": 90,
        # Each open position is reviewed by the AI at most once an hour (owner, 2026-11-24).
        "exit_decision_cooldown_seconds": 3600,
        "news_enabled": True,
    },
    "ai": {
        "providers": {
            "ollama": {"enabled": True, "type": "ollama"},
            "openai": {
                "enabled": False,
                "type": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "OPENAI_API_KEY",
                "default_model": "",
                "timeout": 60,
            },
            "anthropic": {
                "enabled": False,
                "type": "anthropic",
                "base_url": "https://api.anthropic.com",
                "api_key_env": "ANTHROPIC_API_KEY",
                "default_model": "",
                "timeout": 60,
            },
            "gemini": {
                "enabled": False,
                "type": "gemini",
                "base_url": "https://generativelanguage.googleapis.com",
                "api_key_env": "GEMINI_API_KEY",
                "default_model": "",
                "timeout": 60,
            },
            "xai": {
                "enabled": False,
                "type": "openai_compatible",
                "base_url": "https://api.x.ai/v1",
                "api_key_env": "XAI_API_KEY",
                "default_model": "",
                "timeout": 60,
            },
            "deepseek": {
                "enabled": False,
                "type": "openai_compatible",
                "base_url": "https://api.deepseek.com/v1",
                "api_key_env": "DEEPSEEK_API_KEY",
                "default_model": "",
                "timeout": 60,
            },
            "mistral": {
                "enabled": False,
                "type": "openai_compatible",
                "base_url": "https://api.mistral.ai/v1",
                "api_key_env": "MISTRAL_API_KEY",
                "default_model": "",
                "timeout": 60,
            },
            "openrouter": {
                "enabled": False,
                "type": "openai_compatible",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
                "default_model": "",
                "timeout": 60,
            },
            # The owner's Claude subscription through the Claude Code CLI
            # (`claude -p`): no API key, usage counts against the subscription.
            # Serves only roles whose model is a Claude model.
            "claude_subscription": {
                "enabled": False,
                "type": "claude_code",
                "executable": "",        # "" = find `claude` on PATH / ~/.local/bin
                "default_model": "claude-opus-5-5",
                # Models offered in the settings page for this provider.
                "models": ["claude-opus-5-5", "claude-fable-5-2", "claude-sonnet-5-5", "claude-haiku-5-5"],
                "effort": "",            # "" = the CLI default
                "timeout": 300,
                # The subscription is shared with the owner's own Claude use:
                # at most this many calls per day (0 = no limit) and this many
                # CLI processes at once. Beyond that, and whenever the
                # subscription cannot answer, calls use fallback_model on Ollama.
                "daily_call_limit": 500,
                "max_concurrent": 2,
                "fallback_model": "",
            },
        },
        "registry": [],
        "assignment": {
            "all": None,
            "market_analyst": None,
            "pre_earnings_analyst": None,
            "news_analyst": None,
            "quant_analyst": None,
            "portfolio_manager": None,
            "risk_critic": None,
            "research_scientist": None,
            "coding_agent": None,
            "code_reviewer": None,
            "security_reviewer": None,
        },
        "router": {
            "triage": {"model": None},
            "analysis": {"model": None},
            "critic": {"model": None},
        },
        "fallback": {"enabled": True, "chain": []},
        "all_unavailable_no_trade": True,
        "decision": {
            "strict_schema": False,              # §43 — reject (not silently strip) AI authoritative-number fields when True
            "critic_enabled": True,              # §44 — can be disabled in low autonomy modes; forced on in LIVE_AUTO (4C)
            "critic_force_no_trade_on_fail": True,  # §44 — a failing critic verdict forces NO_TRADE
            "critic_poor_setup_score": 68.0,
            "critic_good_setup_score": 78.0,
            "critic_poor_setup_veto_severity": 0.65,
            "critic_veto_min_severity": 0.80,
            "critic_good_setup_veto_severity": 0.90,
            "critic_critical_veto_severity": 0.95,
            "critic_reduce_min_severity": 0.50,
            "critic_min_size_factor": 0.50,
            "critic_low_confidence_threshold": 0.50,
            "critic_low_confidence_veto_severity": 0.75,
            "critic_structural_veto_severity": 0.80,
            "critic_structural_min_categories": 2,
        },
    },
    "risk_constitution": {
        "owner_confirmed": False,
        "max_risk_per_trade_pct": None,
        "max_position_size_pct": None,
        "max_total_exposure_pct": None,
        "max_leverage": None,
        "minimum_cash_pct": None,
        "max_sector_exposure_pct": None,
        "correlation_threshold": None,
        "max_correlated_exposure_pct": None,
        "max_positions": None,
        "max_liquidity_participation_pct": None,
        "max_daily_loss_pct": None,
        "max_daily_loss_trades": None,
        "max_drawdown_pct": None,
        "max_data_age_seconds": None,
        "block_data_conflicts": None,
        "require_approved_strategy": None,
    },
    "autonomy": {
        "mode": "ANALYZE",                   # §45 — default NOT LIVE_AUTO
        "kill_switch_active": False,         # §74 — owner-set; True halts new automated risk (monitoring stays active)
        "live_auto_owner_enabled": False,    # §45 — owner-explicit
        "paper_auto_acceptance_required": True,  # §46 — LIVE_AUTO gated by Paper-Auto acceptance
        "paper_auto_acceptance_passed": False,  # §46 — recorded result; set True only after the acceptance battery passes (owner/CI)
    },
    "paper_execution": {
        "store_path": "data/execution.db",
        # Owner-selected in the Agents UI. PAPER_AUTO remains unavailable while null.
        "reduce_fraction_pct": None,
    },
    "signal_engine": {
        "market_regime_universe_min": 10,    # §49 — min per-ticker regimes for a non-stale market-wide regime
    },
    "ml": {
        "registry": [],                      # §54 — ModelRegistry persistence (list of ModelRecord dicts)
        "qlib_enabled": False,               # §54 — Qlib ADAPTER opt-in (Phase 2 manifest; zero new deps §118)
        "baseline_feature_version": "v1",   # §54 — feature_version stamp for the simple baseline
    },
    "backtest_engine": {
        "default_engine": "faithful_replay",
        "overfitting": {
            "n_folds": 5,
            "purge_bars": 0,
            "embargo_bars": 0,
            "min_oos_trades": 24,
            "pbo_max": 0.25,
            "dsr_min": 1.0,
            "param_relative_std_max": 0.25,
        },
        "evidence_min_sample": 24,
    },
    "mappings": {
        "symbol_map": {
            "SI=F": "SLV",   # Silver ETF proxy
            "GC=F": "GLD",   # Gold ETF proxy
            "CL=F": "CL",   # Crude Oil
            "HG=F": "HG",   # Copper
            "PL=F": "PL",   # Platinum
            "PA=F": "PA",   # Palladium
            "NG=F": "NG",   # Natural Gas
        },
        "news_search_terms": {
            "SI=F": "Silver",
            "GC=F": "Gold",
            "GOLD": "Gold", # Force "Gold" search even for Barrick Gold ticker if desired, or handle externally
            "CL=F": "Crude Oil",
            "BTC-USD": "Bitcoin",
            "ETH-USD": "Ethereum",
        },
        "news_entity_aliases": {
            "AAPL": ["Apple", "Apple Inc"],
            "MSFT": ["Microsoft", "Microsoft Corp"],
            "NVDA": ["Nvidia", "NVIDIA Corp"],
            "TSLA": ["Tesla", "Tesla Inc"],
            "META": ["Meta Platforms", "Facebook"],
            "AMZN": ["Amazon", "Amazon.com"],
            "GOOGL": ["Alphabet", "Google"],
            "BTC-USD": ["Bitcoin", "BTC"],
            "ETH-USD": ["Ethereum", "Ether", "ETH"],
            "SOL-USD": ["Solana", "SOL"],
        }
    },
}

# ========= UTILITY FUNCTIONS =========

_FX_FIAT_CODES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD",
    "SEK", "NOK", "DKK", "CNH", "HKD", "SGD", "MXN", "ZAR",
    "TRY", "PLN", "HUF", "CZK", "RON",
}

def get_bucharest_time() -> datetime:
    return datetime.now(BUCHAREST_TZ)


def normalize_ticker(ticker: str, provider: str = "yahoo") -> str:
    """
    Ticker normalization - ensures consistency between Engine and API.
    Examples:
    - 'GOLD' -> 'GC=F' (Yahoo)
    - 'EUR/USD' -> 'EURUSD=X' (Yahoo)
    """
    token = _sanitize_ticker_token(ticker)
    if not token:
        return ""

    # Friendly names -> Yahoo Core Symbols
    commodity_mapping = {
        "GOLD": "GC=F",
        "SILVER": "SI=F",
        "OIL": "CL=F",
        "CRUDE": "CL=F",
        "PLATINUM": "PL=F",
        "COPPER": "HG=F",
        "NATGAS": "NG=F",
    }

    if token in commodity_mapping:
        core_symbol = commodity_mapping[token]
    else:
        core_symbol = token

    # Normalize compact crypto symbols like BTCUSD -> BTC-USD (for Yahoo/provider neutral flows)
    if (
        core_symbol.endswith("USD")
        and "-" not in core_symbol
        and "/" not in core_symbol
        and not core_symbol.endswith(("USDT", "USDC"))
        and len(core_symbol) >= 6
    ):
        core_symbol = f"{core_symbol[:-3]}-USD"

    # Yahoo Finance normalization
    # Forex e.g., 'EUR/USD' -> 'EURUSD=X'
    if "/" in core_symbol and not core_symbol.endswith("=X"):
        return core_symbol.replace("/", "") + "=X"
    return core_symbol


def normalize_market_ticker(ticker: str) -> str:
    """
    Canonical ticker used by watchlist/UI/state.

    For crypto, collapse USD stable quote aliases to a single display symbol:
    - BTCUSDT -> BTC-USD
    - BTC-USDT -> BTC-USD
    - BTCUSDC -> BTC-USD

    This keeps one watchlist row per asset instead of separate rows for
    provider-specific aliases of the same market.
    """
    raw_token = _sanitize_ticker_token(ticker)
    if (
        raw_token.endswith("USD")
        and "-" not in raw_token
        and "/" not in raw_token
        and not raw_token.endswith(("USDT", "USDC"))
        and raw_token[:-3] in _FX_FIAT_CODES
    ):
        return f"{raw_token}=X"

    token = _sanitize_ticker_token(normalize_ticker(ticker, provider="yahoo"))
    # A provider-side stablecoin row used to be converted to the invalid
    # symbol "-USD" and persisted by Scanner Agent.  Treat quote-only tokens
    # as invalid at the canonical boundary so old polluted rows are removed by
    # the normal watchlist migration as well as rejected on future writes.
    if token in {"-USD", "-USDT", "-USDC"}:
        return ""
    if token.endswith("-USD") and token[:-4] in _FX_FIAT_CODES:
        return f"{token[:-4]}USD=X"
    if not token or token.endswith("=X") or "/" in token or token.endswith("-USD"):
        return token

    for suffix in ("-USDT", "-USDC", "USDT", "USDC"):
        if not token.endswith(suffix):
            continue
        base = token[: -len(suffix)].rstrip("-")
        if not base or base in _FX_FIAT_CODES:
            return token
        return f"{base}-USD"

    return token


def format_bucharest_time(dt: datetime | None = None) -> str:
    if dt is None:
        dt = get_bucharest_time()
    return dt.strftime("%H:%M:%S")


def deep_merge(base: dict, override: dict) -> dict:
    """Recursive merge of two dicts (override over base)."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _sanitize_ticker_token(ticker: str) -> str:
    token = (ticker or "").upper().strip()
    token = token.replace(" ", "")
    token = token.replace("_", "-")
    token = re.sub(r"[^A-Z0-9=./:-]", "", token)
    return token


def is_crypto_ticker(ticker: str) -> bool:
    token = _sanitize_ticker_token(ticker)
    if not token:
        return False
    if token.endswith(("-USD", "-USDT", "-USDC", "USDT", "USDC")):
        return True
    if token.endswith("USD") and "-" not in token and len(token) >= 6:
        return True
    return False


def _normalize_watchlist_ticker(raw: str, is_crypto: bool) -> str:
    return normalize_market_ticker(raw)


def _dedupe_keep_order(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items or []:
        key = str(item or "").upper()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _sanitize_allowed_strategies(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    allowed = []
    seen = set()
    for raw in value:
        text = str(raw or "").upper().strip()
        if "/" not in text:
            continue
        risk, trade = text.split("/", 1)
        risk = risk.strip()
        trade = trade.strip()
        if risk == "PRO":
            risk = "STRONG"
        if risk not in {"STRONG", "MEDIUM", "RELAX"}:
            continue
        if trade not in {"PULLBACK", "BREAKOUT", "CONTRA"}:
            continue
        normalized = f"{risk}/{trade}"
        if normalized in seen:
            continue
        seen.add(normalized)
        allowed.append(normalized)
    return allowed


def _sanitize_ticker_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(settings or {})
    out: Dict[str, Any] = {}

    out["signals_enabled"] = bool(data.get("signals_enabled", True))
    out["strategy_notes"] = str(data.get("strategy_notes", "") or "")

    try:
        min_wr = float(data.get("min_win_rate", 0))
    except Exception:
        min_wr = 0.0
    out["min_win_rate"] = max(0.0, min(100.0, min_wr))

    for key in ("tp_pct", "sl_pct"):
        raw = data.get(key, None)
        if raw in ("", None):
            out[key] = None
            continue
        try:
            num = float(raw)
        except Exception:
            num = None
        if num is None or num <= 0:
            out[key] = None
        else:
            out[key] = num

    out["allowed_strategies"] = _sanitize_allowed_strategies(data.get("allowed_strategies"))
    return out


# ========= CONFIG MANAGER =========

class ConfigManager:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        try:
            self._store = get_sqlite_store()
        except Exception as e:
            logger.error(f"SQLite store unavailable: {e}")
            self._store = None
        self._config: Dict = {}
        self._last_modified: float = 0.0
        self._watchlist: Dict = {}
        self._ticker_settings: Dict = {}  # Per-ticker config (Min WR, TP/SL, etc.)
        self._watchlist_mtime: int = 0
        self._config_changed: bool = False
        # Outcome of the last attempt to write config.json. A failed write used
        # to be logged and swallowed, so callers reported "saved". Callers can
        # check these (or the bool returned by save()/update_values()).
        self.save_ok: bool = True
        self.last_save_error: Optional[str] = None
        self.last_save_error_at: Optional[str] = None
        self.unsaved_changes: bool = False

        # Performance: Cache merged config properties to avoid repeated dict unpacking
        self._cached_signal: Dict = {}
        self._cached_signal_variants: Dict = {}
        self._cached_backtest: Dict = {}
        self._cached_scan: Dict = {}
        self._cached_filters: Dict = {}
        self._cached_news: Dict = {}
        self._cached_sounds: Dict = {}
        self._cached_ui: Dict = {}
        self._cached_watchlist_cfg: Dict = {}
        self._cached_news_pulse: Dict = {}
        self._cached_quick_ai: Dict = {}
        self._cached_strategy_v2: Dict = {}
        self._cached_bot: Dict = {}

        self.load()

    @property
    def config(self) -> Dict:
        return self._config

    # ---- load / save ----

    def _invalidate_cache(self) -> None:
        """Invalidate all cached config properties after config changes."""
        self._cached_signal = {}
        self._cached_signal_variants = {}
        self._cached_backtest = {}
        self._cached_scan = {}
        self._cached_filters = {}
        self._cached_news = {}
        self._cached_sounds = {}
        self._cached_ui = {}
        self._cached_watchlist_cfg = {}
        self._cached_news_pulse = {}
        self._cached_quick_ai = {}
        self._cached_strategy_v2 = {}
        self._cached_bot = {}

    def load(self, prefer_file: bool = False) -> None:
        loaded: Optional[Dict[str, Any]] = None

        file_mtime: Optional[float] = None
        try:
            file_mtime = self.path.stat().st_mtime if self.path.exists() else None
        except Exception:
            file_mtime = None

        # Risk Manager and the other control-plane editors persist atomically to
        # SQLite and mirror to config.json.  Keep that durable snapshot
        # authoritative unless an operator has explicitly edited a newer file.
        # This prevents a stale JSON copy from replacing owner-confirmed limits.
        if self._store is not None and not prefer_file and file_mtime is not None:
            try:
                synced = self._store.get_text(SQLITE_KEY_CONFIG_MTIME, None)
                if synced is not None and file_mtime > float(synced) + 1e-6:
                    prefer_file = True
                    logger.info("config.json newer than SQLite snapshot — loading from file")
            except Exception:
                pass

        if self._store is not None and not prefer_file:
            try:
                from_sqlite = self._store.get_json(SQLITE_KEY_CONFIG, None)
                if isinstance(from_sqlite, dict):
                    loaded = from_sqlite
                    logger.debug("Config loaded from SQLite")
            except Exception as e:
                logger.error(f"Error reading config from SQLite: {e}")

        if loaded is None and self.path.exists():
            import time
            for attempt in range(5):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    logger.info("Config loaded from JSON")
                    break
                except (json.JSONDecodeError, IOError, OSError) as e:
                    if attempt == 4 or isinstance(e, json.JSONDecodeError):
                        logger.error(f"Error reading config: {e}")
                        break
                    time.sleep(0.1)

        if isinstance(loaded, dict):
            self._config = deep_merge(DEFAULT_CONFIG, loaded)
        else:
            self._config = copy.deepcopy(DEFAULT_CONFIG)
            self.save()
        if isinstance(loaded, dict) and self._store is not None:
            try:
                self._store.set_json(SQLITE_KEY_CONFIG, self._config)
                if file_mtime is not None:
                    self._store.set_text(SQLITE_KEY_CONFIG_MTIME, repr(file_mtime))
            except Exception as e:
                logger.error(f"Error syncing config to SQLite: {e}")

        try:
            self._last_modified = self.path.stat().st_mtime if self.path.exists() else datetime.now().timestamp()
        except Exception:
            self._last_modified = datetime.now().timestamp()

        self._apply_env_overrides()
        self._load_watchlist()
        self._migrate_macd_config()
        self._sync_watchlist_to_store()
        self._invalidate_cache()  # Invalidate cache after loading config

    def _save_unlocked(self) -> bool:
        """Persist the config. Returns False when config.json was not written.

        config.json is what the engine process and the file readers see, so a
        failed file write is a failed save even when the SQLite snapshot took
        the change. The failure is logged at ERROR, kept in `last_save_error`,
        and the in-memory config is marked `unsaved_changes` (it is ahead of the
        file until a later save succeeds).
        """
        store_ok = True
        if self._store is not None:
            try:
                self._store.set_json(SQLITE_KEY_CONFIG, self._config)
            except Exception as e:
                store_ok = False
                logger.error(f"Error saving config to SQLite: {e}")
        saved = False
        try:
            _write_json_atomic(self.path, self._config)
            saved = True
            try:
                self._last_modified = self.path.stat().st_mtime
                if self._store is not None:
                    self._store.set_text(SQLITE_KEY_CONFIG_MTIME, repr(self._last_modified))
            except Exception:
                pass
        except (IOError, OSError, TypeError) as e:
            snapshot = (
                "the SQLite snapshot holds the change"
                if (self._store is not None and store_ok)
                else "nothing was persisted"
            )
            self.last_save_error = (
                f"config.json could not be written ({e.__class__.__name__}: {e}); {snapshot}"
            )
            self.last_save_error_at = datetime.now().isoformat(timespec="seconds")
            logger.error(
                "Config save FAILED for %s: %s. In-memory settings are ahead of the file; "
                "other processes will not see this change.",
                self.path, self.last_save_error,
            )
        self.save_ok = saved
        self.unsaved_changes = not saved
        if saved:
            self.last_save_error = None
            self.last_save_error_at = None
        self._invalidate_cache()
        return saved

    def save(self) -> bool:
        """Write the config. Returns False (and sets `last_save_error`) on failure."""
        with _config_write_lock(self.path):
            return self._save_unlocked()

    def update_value(self, path: List[str], value: Any) -> bool:
        """Atomically update one config value without overwriting concurrent changes."""
        return self.update_values([(path, value)])

    def update_values(self, updates: List[Tuple[List[str], Any]]) -> bool:
        """Atomically update multiple config paths in one durable write.

        Returns False when the write to config.json failed (see `last_save_error`).
        """
        normalized_updates: List[Tuple[List[str], Any]] = []
        for path, value in updates or []:
            parts = [str(part).strip() for part in path if str(part).strip()]
            if not parts:
                raise ValueError("Config update path cannot be empty")
            normalized_updates.append((parts, value))
        if not normalized_updates:
            raise ValueError("At least one config update is required")

        with _config_write_lock(self.path):
            latest: Optional[Dict[str, Any]] = None
            file_mtime: Optional[float] = None
            if self.path.exists():
                try:
                    file_mtime = self.path.stat().st_mtime
                    with open(self.path, "r", encoding="utf-8") as config_file:
                        payload = json.load(config_file)
                    if isinstance(payload, dict):
                        latest = payload
                except (json.JSONDecodeError, IOError, OSError) as exc:
                    logger.warning("Could not reload config before atomic update: %s", exc)
            if self._store is not None:
                try:
                    store_payload = self._store.get_json(SQLITE_KEY_CONFIG, None)
                    synced_mtime = self._store.get_text(SQLITE_KEY_CONFIG_MTIME, None)
                    file_is_newer = (
                        file_mtime is not None
                        and synced_mtime is not None
                        and file_mtime > float(synced_mtime) + 1e-6
                    )
                    if isinstance(store_payload, dict) and not file_is_newer:
                        latest = store_payload
                except Exception as exc:
                    logger.warning("Could not reload SQLite config before atomic update: %s", exc)
            self._config = deep_merge(DEFAULT_CONFIG, latest or self._config)
            for parts, value in normalized_updates:
                target = self._config
                for part in parts[:-1]:
                    child = target.get(part)
                    if not isinstance(child, dict):
                        child = {}
                        target[part] = child
                    target = child
                target[parts[-1]] = value
            return self._save_unlocked()

    # ---- env var overrides ----

    def _apply_env_overrides(self) -> None:
        """Override config API keys from environment variables (secrets management)."""
        env_map = {
            "FINNHUB_API_KEY": [("news", "finnhub_api_key")],
        }
        for env_var, config_paths in env_map.items():
            env_val = os.environ.get(env_var, "").strip()
            if not env_val:
                continue
            for path_parts in config_paths:
                section = self._config
                for key in path_parts[:-1]:
                    section = section.setdefault(key, {})
                if section.get(path_parts[-1], "") != env_val:
                    section[path_parts[-1]] = env_val
                    logger.info(f"Config override from env: {env_var} -> {'.'.join(path_parts)}")

    # ---- migration / watchlist ----

    def _migrate_macd_config(self) -> None:
        """Migrate old MACD format (single) to +/- separate."""
        variants = self._config.get("signal_variants", {})
        changed = False

        for var in ["a", "b", "c"]:
            old_key = f"variant_{var}_macd_hist_min"
            pos_key = f"variant_{var}_macd_hist_min_pos"
            neg_key = f"variant_{var}_macd_hist_min_neg"

            if old_key in variants and pos_key not in variants:
                old_val = abs(variants[old_key])
                variants[pos_key] = old_val
                variants[neg_key] = -old_val
                del variants[old_key]
                changed = True

        if changed:
            self.save()

    def _backup_watchlist_file(self) -> None:
        try:
            if not WATCHLIST_PATH.exists():
                return
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = WATCHLIST_PATH.with_suffix(f".json.migration.{stamp}.bak")
            shutil.copy2(WATCHLIST_PATH, backup_path)
        except Exception as e:
            logger.warning(f"Could not create watchlist backup: {e}")

    def _load_watchlist(self) -> None:
        with _config_write_lock(WATCHLIST_PATH):
            self._load_watchlist_unlocked()
            self._alerts_snapshot = copy.deepcopy(self._watchlist.get("alerts", []))

    def _load_watchlist_unlocked(self) -> None:
        data: Optional[Dict[str, Any]] = None
        data_source = "none"
        json_load_error: Optional[Exception] = None

        if WATCHLIST_PATH.exists():
            import time
            for attempt in range(5):
                try:
                    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
                        loaded = json.load(f) or {}
                    if isinstance(loaded, dict):
                        data = loaded
                        data_source = "file"
                    break
                except (json.JSONDecodeError, IOError, OSError) as e:
                    if attempt == 4 or isinstance(e, json.JSONDecodeError):
                        json_load_error = e
                        logger.warning(f"Watchlist JSON invalid/unreadable, trying SQLite fallback: {e}")
                        break
                    time.sleep(0.1)

        if data is None and self._store is not None:
            try:
                loaded = self._store.get_json(SQLITE_KEY_WATCHLIST, None)
                if isinstance(loaded, dict):
                    data = loaded
                    data_source = "sqlite"
                    logger.info("Watchlist restored from SQLite")
                    if json_load_error is not None:
                        logger.info("Watchlist JSON mirror will be healed from SQLite snapshot")
            except Exception as e:
                logger.error(f"Error loading watchlist from SQLite: {e}")

        if isinstance(data, dict):
            try:
                raw_stocks = data.get("stocks")
                if raw_stocks is None:
                    raw_stocks = data.get("yahoo")

                raw_crypto = data.get("crypto")
                if raw_crypto is None:
                    raw_crypto = data.get("cryptos")
                if raw_crypto is None:
                    raw_crypto = data.get("binance")

                # Important: explicit empty lists (e.g., user removed all tickers)
                # must stay empty and not be repopulated from fallback defaults.
                stocks_source = FALLBACK_STOCKS if raw_stocks is None else raw_stocks
                crypto_source = FALLBACK_CRYPTOS if raw_crypto is None else raw_crypto
                stocks = _dedupe_keep_order([
                    _normalize_watchlist_ticker(x, is_crypto=False)
                    for x in (stocks_source or [])
                ])
                crypto = _dedupe_keep_order([
                    _normalize_watchlist_ticker(x, is_crypto=True)
                    for x in (crypto_source or [])
                ])

                moved_to_crypto = [t for t in stocks if is_crypto_ticker(t)]
                if moved_to_crypto:
                    stocks = [t for t in stocks if t not in moved_to_crypto]
                    crypto = _dedupe_keep_order(crypto + moved_to_crypto)

                all_tickers = _dedupe_keep_order(stocks + crypto)

                requested_order = [
                    _normalize_watchlist_ticker(x, is_crypto=is_crypto_ticker(str(x or "")))
                    for x in (data.get("order") or [])
                ]
                order = [t for t in _dedupe_keep_order(requested_order) if t in all_tickers]
                for t in all_tickers:
                    if t not in order:
                        order.append(t)

                hot_list = [
                    _normalize_watchlist_ticker(x, is_crypto=is_crypto_ticker(str(x or "")))
                    for x in (data.get("hot_list") or [])
                ]
                hot_list = [t for t in _dedupe_keep_order(hot_list) if t in all_tickers]

                scanner_exclusions = [
                    _normalize_watchlist_ticker(x, is_crypto=is_crypto_ticker(str(x or "")))
                    for x in (data.get("scanner_exclusions") or [])
                ]
                scanner_exclusions = [t for t in _dedupe_keep_order(scanner_exclusions) if t]

                raw_settings = data.get("settings", {}) or {}
                settings: Dict[str, Dict[str, Any]] = {}
                for raw_ticker, raw_cfg in raw_settings.items():
                    norm_ticker = _normalize_watchlist_ticker(raw_ticker, is_crypto=is_crypto_ticker(str(raw_ticker or "")))
                    if not norm_ticker:
                        continue
                    settings[norm_ticker] = _sanitize_ticker_settings(raw_cfg if isinstance(raw_cfg, dict) else {})

                migrated_payload = {
                    **data,
                    "alerts": list(data.get("alerts") or []),
                    "stocks": stocks,
                    "crypto": crypto,
                    "hot_list": hot_list,
                    "order": order,
                    "settings": settings,
                    "scanner_exclusions": scanner_exclusions,
                    "schema_version": 2,
                }

                original_marker = {
                    **data,
                    "alerts": list(data.get("alerts") or []),
                    "stocks": data.get("stocks", data.get("yahoo", [])),
                    "crypto": data.get("crypto", data.get("cryptos", data.get("binance", []))),
                    "hot_list": data.get("hot_list", []),
                    "order": data.get("order", []),
                    "settings": data.get("settings", {}),
                    "scanner_exclusions": data.get("scanner_exclusions", []),
                    "schema_version": data.get("schema_version"),
                }
                if original_marker != migrated_payload and data_source == "file":
                    self._backup_watchlist_file()

                # Keep JSON mirror for compatibility, but avoid rewriting it on every read-only reload.
                should_write_watchlist_mirror = (data_source != "file") or (original_marker != migrated_payload) or (not WATCHLIST_PATH.exists())
                if should_write_watchlist_mirror:
                    try:
                        _write_json_atomic(WATCHLIST_PATH, migrated_payload, indent=2, ensure_ascii=False)
                    except Exception as e:
                        logger.error(f"Error writing watchlist JSON mirror: {e}")

                # Load alerts from disk so AlertManager.check_alerts() in the
                # MarketEngine subprocess works from the persisted list.
                raw_alerts = data.get("alerts")
                alerts_list = list(raw_alerts) if isinstance(raw_alerts, list) else []
                self._watchlist = {
                    "yahoo": stocks,
                    "binance": crypto,
                    "order": order,
                    "hot_list": hot_list,
                    "scanner_exclusions": scanner_exclusions,
                    "alerts": alerts_list,
                }
                self._ticker_settings = settings
                try:
                    self._watchlist_mtime = WATCHLIST_PATH.stat().st_mtime_ns
                except Exception:
                    self._watchlist_mtime = 0
                self._sync_watchlist_to_store(migrated_payload)
                return
            except Exception as e:
                logger.error(f"Error loading watchlist: {e}")

        self._watchlist = {
            "yahoo": _dedupe_keep_order([_normalize_watchlist_ticker(x, is_crypto=False) for x in FALLBACK_STOCKS]),
            "binance": _dedupe_keep_order([_normalize_watchlist_ticker(x, is_crypto=True) for x in FALLBACK_CRYPTOS]),
            "order": [],
            "hot_list": [],
            "scanner_exclusions": [],
            "alerts": [],
        }
        self._ticker_settings = {}
        self._watchlist_mtime = 0
        # A clean deployment must materialize its fallback watchlist so the
        # JSON mirror and disaster-recovery snapshot are immediately complete.
        # The caller (_load_watchlist) already holds the watchlist write lock,
        # so use the unlocked writer — taking the lock again would deadlock.
        self._save_watchlist_unlocked()

    def _sync_watchlist_to_store(self, payload: Optional[Dict[str, Any]] = None) -> None:
        if self._store is None:
            return
        try:
            if payload is None:
                payload = {
                    "stocks": list(self._watchlist.get("yahoo", []) or []),
                    "crypto": list(self._watchlist.get("binance", []) or []),
                    "hot_list": list(self._watchlist.get("hot_list", []) or []),
                    "order": list(self._watchlist.get("order", []) or []),
                    "settings": dict(self._ticker_settings or {}),
                    "scanner_exclusions": list(self._watchlist.get("scanner_exclusions", []) or []),
                    "alerts": list(self._watchlist.get("alerts", []) or []),
                    "schema_version": 2,
                }
            self._store.set_json(SQLITE_KEY_WATCHLIST, payload)
        except Exception as e:
            logger.error(f"Error syncing watchlist to SQLite: {e}")

    def reload_watchlist_if_changed(self) -> None:
        if WATCHLIST_PATH.exists():
            mtime_ns = WATCHLIST_PATH.stat().st_mtime_ns
            if mtime_ns != getattr(self, "_watchlist_mtime", 0):
                self._load_watchlist()

    def reload_if_changed(self) -> bool:
        if self.path.exists():
            mtime = self.path.stat().st_mtime
            if mtime > self._last_modified:
                self.load(prefer_file=True)  # file edit wins over SQLite snapshot
                self._config_changed = True
                return True
        return False

    def was_config_changed(self) -> bool:
        """Returns True once after a change has been detected."""
        if self._config_changed:
            self._config_changed = False
            return True
        return False

    # ---- properties (view over config) - with caching ----

    @property
    def signal(self) -> Dict:
        if not self._cached_signal:
            defaults = DEFAULT_CONFIG["signal_settings"]
            configured = self._config.get("signal_settings", {})
            configured = configured if isinstance(configured, dict) else {}
            self._cached_signal = {**defaults, **configured}
            # JSON `null` is useful as an owner-facing empty field, but it must
            # not erase executable scalar defaults consumed by signal/backtest
            # arithmetic. Keep the raw config unchanged and normalize only the
            # runtime view.
            for key, default in defaults.items():
                if not isinstance(default, dict) and self._cached_signal.get(key) is None:
                    self._cached_signal[key] = default
        return self._cached_signal

    @property
    def signal_variants(self) -> Dict:
        if not self._cached_signal_variants:
            self._cached_signal_variants = {**DEFAULT_CONFIG["signal_variants"], **self._config.get("signal_variants", {})}
        return self._cached_signal_variants

    @property
    def backtest(self) -> Dict:
        if not self._cached_backtest:
            self._cached_backtest = {**DEFAULT_CONFIG["backtest"], **self._config.get("backtest", {})}
        return self._cached_backtest

    @property
    def scan(self) -> Dict:
        if not self._cached_scan:
            self._cached_scan = {**DEFAULT_CONFIG["scan_settings"], **self._config.get("scan_settings", {})}
        return self._cached_scan

    @property
    def filters(self) -> Dict:
        if not self._cached_filters:
            self._cached_filters = {**DEFAULT_CONFIG["filters"], **self._config.get("filters", {})}
        return self._cached_filters

    @property
    def news(self) -> Dict:
        if not self._cached_news:
            self._cached_news = {**DEFAULT_CONFIG["news"], **self._config.get("news", {})}
        return self._cached_news

    @property
    def sounds(self) -> Dict:
        if not self._cached_sounds:
            self._cached_sounds = {**DEFAULT_CONFIG["sounds"], **self._config.get("sounds", {})}
        return self._cached_sounds

    @property
    def ui(self) -> Dict:
        if not self._cached_ui:
            self._cached_ui = {**DEFAULT_CONFIG["ui"], **self._config.get("ui", {})}
        return self._cached_ui

    @property
    def watchlist_settings(self) -> Dict:
        if not self._cached_watchlist_cfg:
            self._cached_watchlist_cfg = deep_merge(DEFAULT_CONFIG["watchlist"], self._config.get("watchlist", {}))
        return self._cached_watchlist_cfg

    @property
    def news_pulse(self) -> Dict:
        if not self._cached_news_pulse:
            self._cached_news_pulse = {**DEFAULT_CONFIG["news_pulse"], **self._config.get("news_pulse", {})}
        return self._cached_news_pulse

    @property
    def quick_ai(self) -> Dict:
        if not self._cached_quick_ai:
            self._cached_quick_ai = {**DEFAULT_CONFIG["quick_ai"], **self._config.get("quick_ai", {})}
        return self._cached_quick_ai

    @property
    def bot(self) -> Dict:
        if not self._cached_bot:
            self._cached_bot = deep_merge(DEFAULT_CONFIG["bot"], self._config.get("bot", {}))
        return self._cached_bot

    @property
    def strategy_v2(self) -> Dict:
        if not self._cached_strategy_v2:
            self._cached_strategy_v2 = deep_merge(DEFAULT_CONFIG["strategy_v2"], self._config.get("strategy_v2", {}))
        return self._cached_strategy_v2

    @property
    def watchlist(self) -> Dict:
        return self._watchlist

    @property
    def hot_list(self) -> List[str]:
        return self._watchlist.get("hot_list", [])

    @property
    def agents(self) -> Dict:
        return deep_merge(DEFAULT_CONFIG["agents"], self._config.get("agents", {}))

    @property
    def data_sources(self) -> Dict:
        if not hasattr(self, "_cached_data_sources") or not self._cached_data_sources:
            self._cached_data_sources = {**DEFAULT_CONFIG["data_sources"], **self._config.get("data_sources", {})}
        return self._cached_data_sources

    # ---- ticker settings ----

    def get_ticker_settings(self, ticker: str) -> Dict:
        """Returns settings for a ticker (or empty dict)."""
        key = _normalize_watchlist_ticker(ticker, is_crypto=is_crypto_ticker(ticker))
        if not key:
            return {}
        settings = self._ticker_settings.get(key, {})
        return _sanitize_ticker_settings(settings if isinstance(settings, dict) else {})

    def get_effective_ticker_settings(self, ticker: str) -> Dict:
        """Returns effective settings (ticker override over global values)."""
        base = {
            "signals_enabled": True,
            "min_win_rate": float(self.signal.get("min_win_rate", 0) or 0),
            "tp_pct": self.signal.get("profit_target_pct"),
            "sl_pct": self.signal.get("stop_loss_pct"),
            "allowed_strategies": [],
            "strategy_notes": "",
        }
        custom = self.get_ticker_settings(ticker)
        merged = deep_merge(base, custom)
        return _sanitize_ticker_settings(merged)

    def set_ticker_settings(self, ticker: str, settings: Dict) -> None:
        """Updates settings for a specific ticker and saves to watchlist.json."""
        ticker = _normalize_watchlist_ticker(ticker, is_crypto=is_crypto_ticker(ticker))
        if not ticker:
            return
        if not self._ticker_settings:
            self._ticker_settings = {}

        raw = settings if isinstance(settings, dict) else {}
        incoming = _sanitize_ticker_settings(raw)

        # Merge with existing settings or set new
        if ticker in self._ticker_settings:
            # A partial update changes only the fields it names. The sanitiser
            # fills every missing field with its default, and merging those
            # defaults erased the rest: saving the settings dialog (or "Apply
            # to all") wiped the ticker's notebook, and saving a notebook note
            # reset min win rate / TP / SL / strategies and re-enabled signals.
            named = {key: value for key, value in incoming.items() if key in raw}
            merged = deep_merge(self._ticker_settings[ticker], named)
            self._ticker_settings[ticker] = _sanitize_ticker_settings(merged)
        else:
            self._ticker_settings[ticker] = incoming

        # Update file
        self._save_watchlist()

    def toggle_hot_list(self, ticker: str) -> bool:
        """Add/remove ticker from hot list. Returns True if in list, False otherwise."""
        ticker = _normalize_watchlist_ticker(ticker, is_crypto=is_crypto_ticker(ticker))
        if not ticker:
            return False
        hot_list = _dedupe_keep_order(self._watchlist.get("hot_list", []))
        all_tickers = set((self._watchlist.get("yahoo", []) or []) + (self._watchlist.get("binance", []) or []))
        if ticker not in all_tickers:
            if is_crypto_ticker(ticker):
                crypto = _dedupe_keep_order(self._watchlist.get("binance", []) or [])
                if ticker not in crypto:
                    crypto.append(ticker)
                self._watchlist["binance"] = _dedupe_keep_order(crypto)
            else:
                stocks = _dedupe_keep_order(self._watchlist.get("yahoo", []) or [])
                if ticker not in stocks:
                    stocks.append(ticker)
                self._watchlist["yahoo"] = _dedupe_keep_order(stocks)

            order = _dedupe_keep_order(self._watchlist.get("order", []) or [])
            if ticker not in order:
                order.append(ticker)
            self._watchlist["order"] = _dedupe_keep_order(order)
            all_tickers.add(ticker)

        # Clicking HOT is an explicit user action, so it also clears a prior
        # scanner exclusion for this symbol.
        self._watchlist["scanner_exclusions"] = [
            item for item in _dedupe_keep_order(self._watchlist.get("scanner_exclusions", []))
            if item != ticker
        ]

        if ticker in hot_list:
            hot_list.remove(ticker)
            is_in = False
        else:
            hot_list.append(ticker)
            is_in = True

        self._watchlist["hot_list"] = _dedupe_keep_order(hot_list)
        self._save_watchlist()
        return is_in

    def _save_watchlist(self, *, alert_baseline=None, desired_alerts=None):
        """Merge alert edits under the same interprocess lock used by migration."""
        with _config_write_lock(WATCHLIST_PATH):
            before = alert_baseline if alert_baseline is not None else getattr(self, "_alerts_snapshot", [])
            try:
                latest = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8")) if WATCHLIST_PATH.exists() else {}
                if not isinstance(latest, dict):
                    raise ValueError("watchlist root is not an object")
            except Exception as exc:
                # An unreadable file must not block the save that repairs it.
                # Treat our last-known alerts as current so local edits apply.
                logger.warning(f"Watchlist unreadable during save, rewriting from memory: {exc}")
                latest = {"alerts": copy.deepcopy(before or [])}
            desired = desired_alerts if desired_alerts is not None else (self._watchlist.get("alerts", []) or [])
            old = {a["id"]: a for a in before if isinstance(a, dict) and a.get("id")}
            new = {a["id"]: a for a in desired if isinstance(a, dict) and a.get("id")}
            current = {a["id"]: a for a in latest.get("alerts", []) if isinstance(a, dict) and a.get("id")}
            applied = set()
            for key in old.keys() | new.keys():
                if old.get(key) == new.get(key):
                    continue
                # A deletion, toggle or trigger based on stale data cannot undo
                # another process's newer change or resurrect a deleted alert.
                if current.get(key) != old.get(key):
                    continue
                if key in new:
                    current[key] = new[key]
                else:
                    current.pop(key, None)
                applied.add(key)
            self._watchlist["alerts"] = list(current.values())
            self._save_watchlist_unlocked()
            self._alerts_snapshot = copy.deepcopy(self._watchlist["alerts"])
            return applied

    def _save_watchlist_unlocked(self) -> None:
        """Helper to save current watchlist and settings to file."""
        stocks = _dedupe_keep_order([_normalize_watchlist_ticker(t, is_crypto=False) for t in self._watchlist.get("yahoo", [])])
        crypto = _dedupe_keep_order([_normalize_watchlist_ticker(t, is_crypto=True) for t in self._watchlist.get("binance", [])])
        all_tickers = _dedupe_keep_order(stocks + crypto)
        order = [t for t in _dedupe_keep_order(self._watchlist.get("order", [])) if t in all_tickers]
        for ticker in all_tickers:
            if ticker not in order:
                order.append(ticker)
        hot_list = [t for t in _dedupe_keep_order(self._watchlist.get("hot_list", [])) if t in all_tickers]
        scanner_exclusions = [
            t for t in _dedupe_keep_order(self._watchlist.get("scanner_exclusions", [])) if t
        ]

        clean_settings = {}
        for raw_ticker, raw_cfg in (self._ticker_settings or {}).items():
            ticker = _normalize_watchlist_ticker(raw_ticker, is_crypto=is_crypto_ticker(raw_ticker))
            if not ticker:
                continue
            clean_settings[ticker] = _sanitize_ticker_settings(raw_cfg if isinstance(raw_cfg, dict) else {})

        # Preserve alerts: load from in-memory watchlist (set by AlertManager) so
        # that a save triggered by any other operation (add ticker, toggle hot, etc.)
        # never silently drops the user's alert list.
        alerts = list(self._watchlist.get("alerts") or [])
        data = {
            "stocks": stocks,
            "crypto": crypto,
            "hot_list": hot_list,
            "order": order,
            "settings": clean_settings,
            "scanner_exclusions": scanner_exclusions,
            "alerts": alerts,
            "schema_version": 2,
        }
        self._sync_watchlist_to_store(data)
        try:
            _write_json_atomic(WATCHLIST_PATH, data, indent=2, ensure_ascii=False)
            self._watchlist = {
                "yahoo": stocks,
                "binance": crypto,
                "order": order,
                "hot_list": hot_list,
                "scanner_exclusions": scanner_exclusions,
                "alerts": alerts,
            }
            self._ticker_settings = clean_settings
            try:
                self._watchlist_mtime = WATCHLIST_PATH.stat().st_mtime_ns
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Error saving watchlist settings: {e}")
            raise
