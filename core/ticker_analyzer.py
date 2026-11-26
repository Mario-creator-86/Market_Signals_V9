# ticker_analyzer.py - Analyze a ticker and show indicators vs risk profile thresholds
#
# NEW VERSION with SCORING system (0-100) and 3 modes:
# - PULLBACK: Entry in trend direction after retracement
# - BREAKOUT: Entry at level breakout
# - CONTRA: Counter-trend scalp for extreme reversals
#
# All analyses are performed on 1-minute (1m) timeframe.

import json
import copy
import logging
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any
import time
from datetime import datetime
import pandas as pd
import requests
import yfinance as yf
from core.ollama_client import ollama

from core.ta import TechnicalAnalysis
from core.signal_generation import calculate_vwap, count_mtf_alignment
from core.config import CONFIG_PATH, DEFAULT_CONFIG, data_path, normalize_ticker
from core.hotlist_news import get_ticker_news, summarize_ticker_news_signals
from core.ai_analysis_schema import parse_structured_ai_output_with_deterministic_fallback
from core.prompt_evidence import UNTRUSTED_FIELDS_NOTICE, evidence_field_marker, quarantine_fields
from core.scoring_system import (
    calculate_atr,
    calculate_distance_in_atr,
    calculate_percentile_rank,
    compute_signal_score,
    detect_market_regime,
    allowed_modes_for_regime,
    RowContext,
    SCORE_THRESHOLDS,
)

logger = logging.getLogger(__name__)


_config_cache = {"mtime": 0.0, "data": None}

# The per-ticker "Analyze" AI call is a click the user waits on. It used the
# provider default (ollama.timeout, 120 s; a live call took 121 s). It now has
# its own bounded read timeout: config `ollama.analysis_timeout_seconds`.
ANALYSIS_AI_TIMEOUT_SECONDS = 60
AI_STATUS_UNAVAILABLE = "unavailable"


def analysis_ai_timeout_seconds(cfg: Optional[Dict[str, Any]] = None) -> int:
    """Read timeout (seconds) for the "Analyze" AI call, clamped to 5..300."""
    section = cfg.get("ollama") if isinstance(cfg, dict) else None
    raw = section.get("analysis_timeout_seconds") if isinstance(section, dict) else None
    if raw is None or isinstance(raw, bool):
        raw = ANALYSIS_AI_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float(ANALYSIS_AI_TIMEOUT_SECONDS)
    if value != value or value <= 0 or value == float("inf"):
        value = float(ANALYSIS_AI_TIMEOUT_SECONDS)
    return int(max(5, min(300, round(value))))


def quarantine_analysis_context(context_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Prompt copy of the analysis context with third-party text quarantined.

    `news_summary` (headlines, catalysts, risk events) is third-party text: it is
    cleaned with the shared helper and named in a `third_party_evidence` marker.
    The context itself is updated in place with the cleaned news, so the grounding
    check compares the model's reply with exactly what the model was given.
    """
    cleaned, fields = quarantine_fields(context_payload, ("news_summary",))
    if not fields:
        return dict(context_payload)
    context_payload["news_summary"] = cleaned["news_summary"]
    prompt_payload = dict(context_payload)
    prompt_payload["third_party_evidence"] = evidence_field_marker(fields)
    return prompt_payload


def _ai_error_is_timeout(error: Any) -> bool:
    text = str(error or "").lower()
    return "timed out" in text or "timeout" in text


def run_structured_analysis_call(
    prompt: str,
    system: str,
    context_payload: Dict[str, Any],
    *,
    timeout_seconds: int,
    client: Any = None,
) -> tuple:
    """One bounded "Analyze" AI call -> (structured_analysis, model_label).

    A failed call (timeout, HTTP error, circuit open, no model) is an explicit
    failure state: the rule-based summary that is returned carries
    `ai_status: "unavailable"`, `ai_error` and a `fallback_reason` naming the
    real cause, and the model label is not a model name. Before, a failed call
    was indistinguishable from "the AI replied with invalid JSON".
    """
    client = client if client is not None else ollama
    error = ""
    result: Any = None
    try:
        result = client.generate(prompt, system=system, timeout_override=int(timeout_seconds))
    except Exception as exc:  # e.g. no model installed: still a failed call
        error = str(exc) or exc.__class__.__name__
    if not error and not isinstance(result, dict):
        error = "AI provider returned no response"
    if not error and result.get("error"):
        error = str(result.get("error"))

    raw = "" if error else str(result.get("response") or "")
    structured = parse_structured_ai_output_with_deterministic_fallback(raw, context_payload=context_payload)
    if error:
        timed_out = _ai_error_is_timeout(error)
        structured["ai_status"] = AI_STATUS_UNAVAILABLE
        structured["ai_error"] = "timeout" if timed_out else "error"
        structured["ai_error_detail"] = error[:200]
        structured["ai_timeout_seconds"] = int(timeout_seconds)
        structured["fallback_reason"] = (
            (
                f"The AI model did not answer within {int(timeout_seconds)} s"
                if timed_out
                else "The AI call failed"
            )
            + "; this summary was computed by fixed rules, not by the AI."
        )
        return structured, "Rule-based (AI unavailable)"
    return structured, str(result.get("model") or "Ollama")


def _ensure_yfinance_cache_dir() -> None:
    try:
        cache_dir = data_path("yfinance_cache")  # configured data folder (MS_DATA_DIR)
        cache_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(yf, "set_tz_cache_location"):
            yf.set_tz_cache_location(str(cache_dir))
    except Exception as e:
        logger.debug(f"yfinance cache setup failed: {e}")

def load_config() -> dict:
    """Load config from file or return defaults (with cache)."""
    if CONFIG_PATH.exists():
        try:
            mtime = CONFIG_PATH.stat().st_mtime
            if _config_cache["data"] is not None and _config_cache["mtime"] == mtime:
                return _config_cache["data"]
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            _config_cache["mtime"] = mtime
            _config_cache["data"] = data
            return data
        except Exception:
            pass
    return copy.deepcopy(DEFAULT_CONFIG)

@dataclass
class IndicatorValue:
    """An indicator with value and status vs thresholds."""
    name: str
    value: float
    value_str: str
    status: str  # "OK", "WARN", "FAIL"
    details: str


@dataclass
class ModeScore:
    """Score for a specific mode (PULLBACK/BREAKOUT/CONTRA)."""
    mode: str
    long_score: int
    short_score: int
    long_valid: bool
    short_valid: bool
    best_direction: str
    best_score: int
    details: Dict[str, str]


@dataclass
class TickerAnalysis:
    """Complete analysis for a ticker."""
    ticker: str
    asset_type: str  # "crypto" or "stock"
    price: float
    change_pct: float

    # MTF Trends
    # MTF Trends
    trend_1d: str
    trend_4h: str
    trend_1h: str
    trend_30m: str
    trend_5m: str
    trend_1m: str  # Added 1m
    mtf_alignment: int

    # Indicator list
    indicators: List[IndicatorValue]

    # Raw data
    rsi: float
    macd_line: float
    macd_signal: float
    macd_hist: float
    vwap: float
    ema20: float
    distance_from_vwap_pct: float
    distance_from_ema20_pct: float

    # NEW: ATR data and normalized distances
    atr: float = 0.0
    distance_from_vwap_atr: float = 0.0
    distance_from_ema20_atr: float = 0.0

    # NEW: Scores for each mode and profile
    mode_scores: Dict[str, Dict[str, ModeScore]] = field(default_factory=dict)

    # NEW: Best signal found
    best_signal: Optional[Dict] = None

    # NEW: Fundamental Data (Yahoo Finance) and AI Analysis (Ollama)
    fundamentals: Dict = field(default_factory=dict)
    quote: Dict = field(default_factory=dict)
    ai_analysis: Optional[Dict[str, Any]] = None
    ai_analysis_text: str = ""
    model: str = "Technical"
    news: List[Dict] = field(default_factory=list)
    quote_batch: Dict = field(default_factory=dict)
    breaking_news: List[Dict] = field(default_factory=list)
    news_signals: Dict[str, Any] = field(default_factory=dict)



from core.stock_data_client import StockDataClient

# Shared client instance: these are instance methods; calling them unbound on the
# class passed the ticker as `self` and raised TypeError (2026-09-03 audit P0 —
# fundamentals/quote/news were silently emptied by the broad except).
_SDC = StockDataClient()
from core.http_utils import http_get


class BinanceAPI:
    """Helper for Binance API."""
    BASE_URL = "https://api.binance.com/api/v3"

    @classmethod
    def to_symbol(cls, ticker: str) -> str:
        t = ticker.upper().replace("-", "")
        if t.endswith("USD") and not t.endswith("USDT"):
            return t[:-3] + "USDT"
        return t

    @classmethod
    def fetch_klines(cls, ticker: str, interval: str = "1m", limit: int = 500) -> Optional[pd.DataFrame]:
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
    def get_last_price(cls, ticker: str) -> Optional[float]:
        symbol = cls.to_symbol(ticker)
        try:
            resp = http_get(
                f"{cls.BASE_URL}/ticker/price",
                provider="binance",
                params={"symbol": symbol},
                timeout=(2, 6),
                max_retries=1,
            )
            if resp is None:
                return None
            if resp.status_code != 200:
                return None
            return float(resp.json().get("price", 0))
        except Exception:
            return None

    @classmethod
    def get_pct_24h(cls, ticker: str) -> float:
        symbol = cls.to_symbol(ticker)
        try:
            resp = http_get(
                f"{cls.BASE_URL}/ticker/24hr",
                provider="binance",
                params={"symbol": symbol},
                timeout=(2, 6),
                max_retries=1,
            )
            if resp is None:
                return 0.0
            if resp.status_code == 200:
                return float(resp.json().get("priceChangePercent", 0))
        except Exception:
            pass
        return 0.0


def _first_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    if isinstance(value, dict):
        return value
    return {}


def _pick_first(mapping: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        if key in mapping and mapping.get(key) not in (None, ""):
            return mapping.get(key)
    return default


def _to_float_safe(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _build_fundamental_llm_snapshot(fundamental_data: Dict[str, Any]) -> Dict[str, Any]:
    owner = _first_dict(fundamental_data.get("owner_earnings") or [])
    ev = _first_dict(fundamental_data.get("enterprise_values") or [])
    inc_growth = _first_dict(fundamental_data.get("income_statement_growth") or [])
    cf_growth = _first_dict(fundamental_data.get("cash_flow_statement_growth") or [])
    income_statement = _first_dict(fundamental_data.get("income_statement") or [])
    report = _first_dict(fundamental_data.get("reporting_dates") or [])

    reporting_date = str(
        _pick_first(
            report,
            "date",
            "date_formatted",
            "reportDate",
            "fiscalDateEnding",
            default="unknown",
        )
    )
    if reporting_date == "unknown":
        fy = _pick_first(report, "fiscalYear")
        per = _pick_first(report, "period")
        if fy:
            reporting_date = f"{per or 'FY'} {fy}"

    enterprise_value = _to_float_safe(
        _pick_first(ev, "enterpriseValue", "enterprise_value"),
        0.0,
    )
    ev_to_ebitda = _to_float_safe(
        _pick_first(ev, "evToEbitda", "enterpriseValueOverEBITDA", "ev_to_ebitda"),
        0.0,
    )
    if ev_to_ebitda <= 0:
        ebitda_val = _to_float_safe(_pick_first(income_statement, "ebitda"), 0.0)
        if enterprise_value > 0 and ebitda_val > 0:
            ev_to_ebitda = enterprise_value / ebitda_val

    return {
        "owner_earnings": _to_float_safe(
            _pick_first(owner, "ownerEarnings", "ownersEarnings", "owner_earnings"),
            0.0,
        ),
        "owner_earnings_per_share": _to_float_safe(
            _pick_first(
                owner,
                "ownerEarningsPerShare",
                "ownersEarningsPerShare",
                "ownerEarningsPerShareDiluted",
            ),
            0.0,
        ),
        "enterprise_value": enterprise_value,
        "ev_to_ebitda": ev_to_ebitda,
        "revenue_growth_yoy": _to_float_safe(
            _pick_first(inc_growth, "growthRevenue", "revenueGrowth"),
            0.0,
        ),
        "net_income_growth_yoy": _to_float_safe(
            _pick_first(inc_growth, "growthNetIncome", "netIncomeGrowth"),
            0.0,
        ),
        "eps_growth_yoy": _to_float_safe(
            _pick_first(inc_growth, "growthEPS", "growthEps", "epsGrowth"),
            0.0,
        ),
        "free_cash_flow_growth_yoy": _to_float_safe(
            _pick_first(cf_growth, "growthFreeCashFlow", "freeCashFlowGrowth"),
            0.0,
        ),
        "reporting_date": reporting_date if reporting_date and reporting_date != "unknown" else "unknown",
    }


def analyze_ticker(ticker: str, include_heavy: bool = True, disable_ollama: bool = False) -> Optional[TickerAnalysis]:
    """
    Analyze a ticker and return all indicators compared against risk profile thresholds.

    NEW VERSION: Includes scoring system across 3 modes (PULLBACK, BREAKOUT, CONTRA)
    and 1-minute timeframe analysis.

    Args:
        ticker: Ticker symbol (e.g. "AAPL", "BTC-USD", "BTCUSDT")

    Returns:
        TickerAnalysis with all indicators and scores, or None on error
    """
    ticker = ticker.upper().strip()

    # Determine asset type
    is_crypto = ticker.endswith("-USD") or ticker.endswith("USDT") or ticker.endswith("USD") or ticker.endswith("-USDC")
    asset_type = "crypto" if is_crypto else "stock"
    y_ticker = normalize_ticker(ticker, provider="yahoo")
    is_commodity_or_forex = ("=F" in y_ticker) or ("=X" in y_ticker)
    is_equity = not is_crypto and not is_commodity_or_forex

    # Load configuration
    config = load_config()
    signal_settings = config.get("signal_settings", {})
    rsi_period = signal_settings.get("rsi_period", 14)
    macd_fast = signal_settings.get("macd_fast", 12)
    macd_slow = signal_settings.get("macd_slow", 26)
    macd_signal_period = signal_settings.get("macd_signal", 9)
    atr_period = signal_settings.get("atr_period", 14)

    # Fetch data - now on 1m for main analysis
    if is_crypto:
        # Parallel Binance fetches - all timeframes + price fetch fired simultaneously
        with ThreadPoolExecutor(max_workers=8) as _ex:
            _f1m   = _ex.submit(BinanceAPI.fetch_klines, ticker, "1m", 500)
            _f5m   = _ex.submit(BinanceAPI.fetch_klines, ticker, "5m", 200)
            _f30m  = _ex.submit(BinanceAPI.fetch_klines, ticker, "30m", 100)
            _f1h   = _ex.submit(BinanceAPI.fetch_klines, ticker, "1h", 100)
            _f4h   = _ex.submit(BinanceAPI.fetch_klines, ticker, "4h", 100)
            _f1d   = _ex.submit(BinanceAPI.fetch_klines, ticker, "1d", 100)
            _fprice = _ex.submit(BinanceAPI.get_last_price, ticker)
            _fpct   = _ex.submit(BinanceAPI.get_pct_24h, ticker)
        df_1m  = _f1m.result()
        df_5m  = _f5m.result()
        df_30m = _f30m.result()
        df_1h  = _f1h.result()
        df_4h  = _f4h.result()
        df_1d  = _f1d.result()
        price  = _fprice.result()
        change_pct = _fpct.result()
    else:
        # Stock - Yahoo Finance primary
        df_1m = None
        df_5m = None
        df_30m = None
        df_1h = None
        df_4h = None
        df_1d = None
        price = None
        change_pct = 0.0
        data_source = "unknown"

        norm_ticker = normalize_ticker(ticker, provider="yahoo")

        # Yahoo Finance primary - parallel chart fetches
        primary_success = False
        try:
            logger.info(f"Trying Yahoo Finance (primary, parallel) for {ticker}...")
            _yf_start = time.time()
            with ThreadPoolExecutor(max_workers=7) as _ex:
                _chart = StockDataClient.get_historical_chart
                _f1m   = _ex.submit(lambda f=_chart, s=norm_ticker, t="1min", l=500: f(s, t, l, as_dataframe=True))
                _f5m   = _ex.submit(lambda f=_chart, s=norm_ticker, t="5min", l=200: f(s, t, l, as_dataframe=True))
                _f30m  = _ex.submit(lambda f=_chart, s=norm_ticker, t="30min", l=100: f(s, t, l, as_dataframe=True))
                _f1h   = _ex.submit(lambda f=_chart, s=norm_ticker, t="1hour", l=100: f(s, t, l, as_dataframe=True))
                _f4h   = _ex.submit(lambda f=_chart, s=norm_ticker, t="4hour", l=100: f(s, t, l, as_dataframe=True))
                _f1d   = _ex.submit(lambda f=_chart, s=norm_ticker, t="1day", l=200: f(s, t, l, as_dataframe=True))
                _fprice = _ex.submit(StockDataClient.get_price_and_change, norm_ticker)
            df_1m  = _f1m.result()
            df_5m  = _f5m.result()
            df_30m = _f30m.result()
            df_1h  = _f1h.result()
            df_4h  = _f4h.result()
            df_1d  = _f1d.result()
            yf_price, yf_pct = _fprice.result()
            logger.info(f"Yahoo Finance parallel fetches done in {time.time() - _yf_start:.2f}s for {ticker}")

            if df_1m is None or df_1m.empty:
                if df_5m is not None and not df_5m.empty:
                    df_1m = df_5m
                    data_source = "yahoo_5m"
                    logger.info(f"Yahoo 1min unavailable, using 5min for {ticker}")

            if yf_price:
                price = yf_price
                change_pct = yf_pct

            if df_1m is not None and not df_1m.empty:
                primary_success = True
                if data_source == "unknown":
                    data_source = "yahoo"
        except Exception as e:
            logger.warning(f"Yahoo Finance primary failed for {ticker}: {e}")

        # Direct yfinance fallback (fill missing data or price)
        yahoo_success = False
        if not primary_success or price is None:
            try:
                logger.info(f"Trying direct yfinance (fallback) for {ticker}...")
                _ensure_yfinance_cache_dir()

                if is_commodity_or_forex:
                    # Futures/FX: avoid slow intraday downloads, use daily only
                    df_1d = yf.download(y_ticker, period="6mo", interval="1d", progress=False, auto_adjust=False)
                    df_1m = df_1d if df_1m is None or df_1m.empty else df_1m
                else:
                    if df_1m is None or df_1m.empty:
                        df_1m = yf.download(y_ticker, period="1d", interval="1m", progress=False, auto_adjust=False)
                    if df_5m is None or df_5m.empty:
                        df_5m = yf.download(y_ticker, period="5d", interval="5m", progress=False, auto_adjust=False)
                    if df_30m is None or df_30m.empty:
                        df_30m = yf.download(y_ticker, period="30d", interval="30m", progress=False, auto_adjust=False)
                    if df_1h is None or df_1h.empty:
                        df_1h = yf.download(y_ticker, period="60d", interval="1h", progress=False, auto_adjust=False)
                    if df_1d is None or df_1d.empty:
                        df_1d = yf.download(y_ticker, period="1y", interval="1d", progress=False, auto_adjust=False)

                # Normalize columns for yfinance
                for df in [df_1m, df_5m, df_30m, df_1h, df_1d]:
                    if df is not None and not df.empty:
                        if isinstance(df.columns, pd.MultiIndex):
                            df.columns = df.columns.get_level_values(0)
                        df.columns = [str(c).lower() for c in df.columns]

                # Resample 1H to 4H
                if df_1h is not None and not df_1h.empty:
                    try:
                        if not isinstance(df_1h.index, pd.DatetimeIndex):
                            df_1h.index = pd.to_datetime(df_1h.index)

                        agg_dict = {
                            'open': 'first',
                            'high': 'max',
                            'low': 'min',
                            'close': 'last',
                            'volume': 'sum'
                        }
                        agg_dict = {k: v for k, v in agg_dict.items() if k in df_1h.columns}

                        df_4h = df_1h.resample('4h').agg(agg_dict).dropna()
                    except Exception as e:
                        logger.warning(f"Resampling 1H to 4H failed: {e}")

                # Prefer daily data for futures/FX; avoid slow .info
                if price is None:
                    if is_commodity_or_forex and df_1d is not None and not df_1d.empty and "close" in df_1d.columns:
                        price = float(df_1d["close"].iloc[-1])
                        if len(df_1d["close"]) >= 2:
                            prev_close = float(df_1d["close"].iloc[-2])
                        else:
                            prev_close = price
                        change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
                    else:
                        stock = yf.Ticker(y_ticker)
                        price = None
                        prev_close = None
                        try:
                            fast_info = getattr(stock, "fast_info", None)
                            if isinstance(fast_info, dict) and fast_info:
                                price = fast_info.get("lastPrice") or fast_info.get("last_price")
                                prev_close = fast_info.get("previousClose") or fast_info.get("previous_close")
                        except Exception:
                            pass
                        if price is None:
                            info = stock.info
                            price = info.get("regularMarketPrice") or info.get("currentPrice")
                            prev_close = info.get("previousClose", price)
                        try:
                            price = float(price) if price is not None else None
                            prev_close = float(prev_close) if prev_close is not None else None
                        except Exception:
                            price = None
                            prev_close = None
                        change_pct = ((price - prev_close) / prev_close * 100) if prev_close and price else 0
                if price:
                    data_source = "yahoo"
                    yahoo_success = True
                    logger.debug(f"Yahoo fallback success for {ticker}: ${price:.2f}")
            except Exception as e:
                logger.warning(f"Yahoo fallback failed for {ticker}: {e}")

        # Last resort: StockDataClient price if still missing
        if price is None:
            try:
                fallback_price, fallback_pct = _SDC.get_price_and_change(norm_ticker)
                if fallback_price:
                    price = fallback_price
                    change_pct = fallback_pct
                    logger.info(f"Yahoo price fallback for {ticker}: ${price:.2f}")
            except Exception as e:
                logger.error(f"Yahoo price fallback failed for {ticker}: {e}")

    if df_1m is None or df_1m.empty:
        # Fallback: for futures/FX, use daily data if available
        if df_1d is not None and not df_1d.empty:
            df_1m = df_1d
            logger.info(f"Using daily data as 1m fallback for {ticker}")
        else:
            logger.error(f"No data available for {ticker}")
            return None

    if 'close' not in df_1m.columns:
        logger.error(f"Missing column 'close' for {ticker}")
        return None

    # Calculate indicators pe 1m (timeframe principal)
    close = df_1m["close"]

    # RSI
    rsi_series = TechnicalAnalysis.calc_rsi(close, rsi_period)
    rsi = float(rsi_series.iloc[-1]) if not rsi_series.empty else 50.0

    # MACD
    macd_line_s, macd_signal_s, macd_hist_s = TechnicalAnalysis.calc_macd(
        close, macd_fast, macd_slow, macd_signal_period
    )
    macd = float(macd_line_s.iloc[-1]) if not macd_line_s.empty else 0.0
    macd_sig = float(macd_signal_s.iloc[-1]) if not macd_signal_s.empty else 0.0
    macd_hist = float(macd_hist_s.iloc[-1]) if not macd_hist_s.empty else 0.0

    # VWAP
    vwap_series = calculate_vwap(df_1m)
    vwap = float(vwap_series.iloc[-1]) if not vwap_series.empty else price

    # EMA20
    ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1]) if len(close) >= 20 else price

    # ATR (NOU)
    atr_series = calculate_atr(df_1m, period=atr_period)
    atr = float(atr_series.iloc[-1]) if not atr_series.empty and not pd.isna(atr_series.iloc[-1]) else 0.01

    # Distances
    distance_from_vwap_pct = abs((price - vwap) / vwap * 100) if vwap else 0
    distance_from_ema20_pct = abs((price - ema20) / ema20 * 100) if ema20 else 0

    # Distances in ATR (NEW)
    distance_from_vwap_atr = calculate_distance_in_atr(price, vwap, atr)
    distance_from_ema20_atr = calculate_distance_in_atr(price, ema20, atr)

    # NOU: ADX and Volume SMA for filtre PRO
    adx_series = TechnicalAnalysis.calc_adx(df_1m["high"], df_1m["low"], df_1m["close"], period=14)
    adx = float(adx_series.iloc[-1]) if not adx_series.empty else 25.0

    vol_sma_series = df_1m["volume"].rolling(window=20).mean()
    vol_sma = float(vol_sma_series.iloc[-1]) if not vol_sma_series.empty else 0.0

    # Regime metrics from 5m when available
    adx_5m = adx
    bb_width_5m = 0.0
    atr_percentile_5m = 0.0
    if df_5m is not None and not df_5m.empty and "close" in df_5m.columns:
        try:
            adx_5m_series = TechnicalAnalysis.calc_adx(df_5m["high"], df_5m["low"], df_5m["close"], period=14)
            if not adx_5m_series.empty and not pd.isna(adx_5m_series.iloc[-1]):
                adx_5m = float(adx_5m_series.iloc[-1])
        except Exception:
            pass
        try:
            bb_width_series = TechnicalAnalysis.calc_bb_width(df_5m["close"], period=20, stddev=2.0)
            if not bb_width_series.empty and not pd.isna(bb_width_series.iloc[-1]):
                bb_width_5m = float(bb_width_series.iloc[-1])
        except Exception:
            pass
        try:
            atr_5m_series = calculate_atr(df_5m, period=atr_period).dropna()
            if not atr_5m_series.empty:
                atr_percentile_5m = calculate_percentile_rank(
                    atr_5m_series.tolist(),
                    float(atr_5m_series.iloc[-1])
                )
        except Exception:
            pass

    # Trenduri din fiecare timeframe
    def get_trend(df):
        if df is None or df.empty:
            return "N/A"
        close_col = df["close"] if "close" in df.columns else None
        if close_col is None or len(close_col) < 26:
            return "N/A"
        _, _, hist = TechnicalAnalysis.calc_macd(close_col, macd_fast, macd_slow, macd_signal_period)
        return TechnicalAnalysis.get_trend_from_macd(hist)

    trend_5m = get_trend(df_5m)
    trend_30m = get_trend(df_30m)
    trend_1h = get_trend(df_1h)
    trend_4h = get_trend(df_4h)
    trend_1d = get_trend(df_1d)
    trend_1m = get_trend(df_1m)

    # MTF Alignment - Add 1m for scalping confirmation
    mtf_trends = {"1d": trend_1d, "4h": trend_4h, "1h": trend_1h, "30m": trend_30m, "5m": trend_5m, "1m": trend_1m}
    potential_direction = "LONG" if rsi < 55 else "SHORT"
    expected_trend = "BULL" if potential_direction == "LONG" else "BEAR"
    mtf_alignment = count_mtf_alignment(mtf_trends, expected_trend)

    # Construim lista de indicatori
    indicators = []

    indicators.append(IndicatorValue(
        name="RSI",
        value=rsi,
        value_str=f"{rsi:.1f}",
        status="OK",
        details=f"Current RSI value on 1m"
    ))

    indicators.append(IndicatorValue(
        name="MACD Histogram",
        value=macd_hist,
        value_str=f"{macd_hist:.6f}",
        status="BULL" if macd_hist > 0 else "BEAR",
        details=f"{'Positive' if macd_hist > 0 else 'Negative'} momentum"
    ))

    indicators.append(IndicatorValue(
        name="MACD Line",
        value=macd,
        value_str=f"{macd:.6f}",
        status="OK",
        details=f"MACD = {macd:.6f}, Signal = {macd_sig:.6f}"
    ))

    macd_cross = "DA" if macd > macd_sig else "NU"
    indicators.append(IndicatorValue(
        name="MACD Cross (above signal)",
        value=1 if macd > macd_sig else 0,
        value_str=macd_cross,
        status="BULL" if macd > macd_sig else "BEAR",
        details=f"MACD {'>' if macd > macd_sig else '<'} Signal"
    ))

    # NOU: ATR
    indicators.append(IndicatorValue(
        name="ATR (14)",
        value=atr,
        value_str=f"{atr:.6f}",
        status="OK",
        details=f"Volatilitate medie pe 14 perioade"
    ))

    # Distance VWAP
    indicators.append(IndicatorValue(
        name="Distance from VWAP",
        value=distance_from_vwap_pct,
        value_str=f"{distance_from_vwap_pct:.2f}% ({distance_from_vwap_atr:.2f} ATR)",
        status="OK" if distance_from_vwap_atr < 1.0 else "WARN",
        details=f"Price: ${price:.4f}, VWAP: ${vwap:.4f}"
    ))

    # Distance EMA20
    indicators.append(IndicatorValue(
        name="Distance from EMA20",
        value=distance_from_ema20_pct,
        value_str=f"{distance_from_ema20_pct:.2f}% ({distance_from_ema20_atr:.2f} ATR)",
        status="OK" if distance_from_ema20_atr < 0.8 else "WARN",
        details=f"Price: ${price:.4f}, EMA20: ${ema20:.4f}"
    ))

    # MTF Alignment
    indicators.append(IndicatorValue(
        name="MTF Alignment",
        value=mtf_alignment,
        value_str=f"{mtf_alignment}/6",
        status="OK" if mtf_alignment >= 3 else "WARN",
        details=f"1D: {trend_1d}, 4H: {trend_4h}, 1H: {trend_1h}, 30M: {trend_30m}, 5M: {trend_5m}, 1M: {trend_1m}"
    ))

    # Calculate scores for sistemul de scoring
    mode_scores = {}
    best_signal = None
    best_total_score = 0

    # Create context for the last 1m bar
    row_context = RowContext(
        open=float(df_1m["open"].iloc[-1]),
        high=float(df_1m["high"].iloc[-1]),
        low=float(df_1m["low"].iloc[-1]),
        close=float(df_1m["close"].iloc[-1]),
        volume=float(df_1m["volume"].iloc[-1]) if "volume" in df_1m.columns else 0,
        rsi=rsi,
        macd=macd,
        macd_signal=macd_sig,
        macd_hist=macd_hist,
        ema20=ema20,
        vwap=vwap,
        atr=atr,
        adx=adx,
        vol_sma=vol_sma,
        adx_5m=adx_5m,
        bb_width_5m=bb_width_5m,
        atr_percentile_5m=atr_percentile_5m,
        market_regime="MIXED",
        trend_1d=trend_1d,
        trend_4h=trend_4h,
        trend_1h=trend_1h,
        trend_30m=trend_30m,
        trend_15m="N/A",
        trend_5m=trend_5m,
        trend_1m=trend_1m,
        dist_vwap_atr=distance_from_vwap_atr,
        dist_ema_atr=distance_from_ema20_atr,
        cooldown_active=False,
        cooldown_bars_remaining=0
    )

    market_regime, regime_details = detect_market_regime(row_context, signal_settings.get("regime"))
    row_context.market_regime = market_regime

    all_modes = ["PULLBACK", "BREAKOUT", "CONTRA"]
    allowed_modes = set(all_modes)
    if signal_settings.get("enable_regime_gating", True):
        allowed_modes = set(allowed_modes_for_regime(market_regime))

    for mode in all_modes:
        mode_scores[mode] = {}

        for profile in ["STRONG", "MEDIUM", "RELAX"]:
            if mode not in allowed_modes:
                mode_score = ModeScore(
                    mode=mode,
                    long_score=0,
                    short_score=0,
                    long_valid=False,
                    short_valid=False,
                    best_direction="-",
                    best_score=0,
                    details={"gate": f"regime_block_{market_regime.lower()}"}
                )
                mode_scores[mode][profile] = mode_score
                continue

            result = compute_signal_score(row_context, mode, profile, config.get("signal_settings", {}).get("scoring"))

            mode_score = ModeScore(
                mode=mode,
                long_score=result.total_score if result.direction == "LONG" else 0,
                short_score=result.total_score if result.direction == "SHORT" else 0,
                long_valid=result.is_valid and result.direction == "LONG",
                short_valid=result.is_valid and result.direction == "SHORT",
                best_direction=result.direction,
                best_score=result.total_score,
                details=result.details
            )

            mode_scores[mode][profile] = mode_score

            # Keep the best signal
            if result.is_valid and result.total_score > best_total_score:
                best_total_score = result.total_score
                best_signal = {
                    "mode": mode,
                    "profile": profile,
                    "direction": result.direction,
                    "score": result.total_score,
                    "probability_score": result.probability_score,
                    # Not a win probability: an uncalibrated transform of the score.
                    "probability_calibrated": False,
                    "score_kind": "heuristic_score",
                    "mtf_alignment": f"{result.mtf_alignment_count}/{result.mtf_alignment_total}",
                    "market_regime": market_regime,
                    "threshold": SCORE_THRESHOLDS[profile],
                    "details": result.details
                }

    # Prepare final analysis object
    analysis = TickerAnalysis(
        ticker=ticker,
        asset_type=asset_type,
        price=price or 0,
        change_pct=change_pct or 0,
        trend_1d=trend_1d,
        trend_4h=trend_4h,
        trend_1h=trend_1h,
        trend_30m=trend_30m,
        trend_5m=trend_5m,
        trend_1m=trend_1m,
        mtf_alignment=mtf_alignment,
        indicators=indicators,
        rsi=rsi,
        macd_line=macd,
        macd_signal=macd_sig,
        macd_hist=macd_hist,
        vwap=vwap,
        ema20=ema20,
        distance_from_vwap_pct=distance_from_vwap_pct,
        distance_from_ema20_pct=distance_from_ema20_pct,
        atr=atr,
        distance_from_vwap_atr=distance_from_vwap_atr,
        distance_from_ema20_atr=distance_from_ema20_atr,
        mode_scores=mode_scores,
        best_signal=best_signal
    )

    start_total = time.time()

    # ------------------------------------------------------------------
    # NEW: Fetch Fundamental Data (FMP) if stock
    # ------------------------------------------------------------------
    fundamental_data = {}
    news_7d: List[Dict[str, Any]] = []
    news_30d: List[Dict[str, Any]] = []
    news_90d: List[Dict[str, Any]] = []
    news_signals: Dict[str, Any] = {}
    if not is_crypto:
        try:
            fmp_start = time.time()
            start_ts = time.time()
            max_seconds = 20  # keep fundamentals responsive for UI
            def time_left() -> bool:
                return (time.time() - start_ts) < max_seconds

            fundamental_data = {}

            if is_equity:
                # Earnings-aware TTL: refresh around earnings, otherwise cache longer
                earnings = _SDC.get_calendar_earnings(ticker)
                fundamental_data["reporting_dates"] = earnings or _SDC.get_reporting_dates(ticker, ttl=14 * 24 * 3600)

                ttl_fundamentals = 14 * 24 * 3600  # 14 days default
                try:
                    if earnings and isinstance(earnings, list):
                        date_str = earnings[0].get("date") or earnings[0].get("date_formatted")
                        if date_str:
                            earn_date = datetime.fromisoformat(date_str[:10]).date()
                            today = datetime.utcnow().date()
                            days_to = (earn_date - today).days
                            if -7 <= days_to <= 7:
                                ttl_fundamentals = 24 * 3600  # 1 day near earnings
                except Exception:
                    pass

                # Core lightweight calls (cached for 14d, or 1d around earnings)
                fundamental_data["balance_sheet"] = _SDC.get_balance_sheet(ticker)
                fundamental_data["cash_flow"] = _SDC.get_cash_flow(ticker)
                fundamental_data["growth"] = _SDC.get_balance_sheet_growth(ticker)
                fundamental_data["as_reported"] = _SDC.get_balance_sheet_as_reported(ticker)
                fundamental_data["cash_flow_as_reported"] = _SDC.get_cash_flow_statement_as_reported(ticker)
                fundamental_data["analyst_estimates"] = _SDC.get_analyst_estimates(ticker)
                fundamental_data["owner_earnings"] = _SDC.get_owner_earnings(ticker)
                fundamental_data["enterprise_values"] = _SDC.get_enterprise_values(ticker)
                fundamental_data["income_statement_growth"] = StockDataClient.get_income_statement_growth(ticker)
                # Keep alias for compatibility with existing frontend expectations.
                fundamental_data["balance_sheet_growth"] = fundamental_data.get("growth", [])
                fundamental_data["cash_flow_statement_growth"] = _SDC.get_cash_flow_statement_growth(ticker)
                fundamental_data["financial_growth"] = _SDC.get_financial_growth(ticker)
                # Hot List Market Data (insider/ADX/ratings/sector)
                try:
                    fundamental_data["hot_list"] = _SDC.get_hot_list_data(ticker) or {}
                except Exception:
                    fundamental_data["hot_list"] = {}

                # New stable endpoints (guarded)
                if time_left():
                    fundamental_data["price_change"] = _SDC.get_stock_price_change(ticker)
                if time_left():
                    fundamental_data["income_statement"] = _SDC.get_income_statement(ticker)
                if time_left():
                    fundamental_data["cash_flow_statement"] = _SDC.get_latest_cash_flow_statement(ticker)
                if time_left():
                    fundamental_data["balance_sheet_statement"] = _SDC.get_latest_balance_sheet_statement(ticker)
                if include_heavy and time_left():
                    fundamental_data["financial_reports_json"] = _SDC.get_financial_reports_json(ticker)
                # Heavier/global endpoints - only if time remains and requested
                if include_heavy and time_left():
                    fundamental_data["latest_financial_statements"] = _SDC.get_latest_financial_statements(ticker)
                if include_heavy and time_left():
                    fundamental_data["historical_eod_light"] = _SDC.get_historical_eod_light(ticker)
                if include_heavy and time_left():
                    fundamental_data["historical_eod_full"] = _SDC.get_historical_eod_full(ticker)

                if not time_left():
                    fundamental_data["data_status"] = "partial"
                    fundamental_data["data_note"] = "Partial fundamentals: time budget exceeded"

                analysis.quote = StockDataClient.get_quote(ticker, ttl=60) or {}

                # Preserve the legacy field name, but source it from the same Starter-safe quote payload.
                if isinstance(analysis.quote, dict) and analysis.quote:
                    analysis.quote_batch = dict(analysis.quote)
                    analysis.quote_batch.setdefault("symbol", ticker)
                else:
                    analysis.quote_batch = {}
                logger.info(f"FMP fundamentals for {ticker} loaded in {time.time() - fmp_start:.2f}s (include_heavy={include_heavy})")
            analysis.fundamentals = fundamental_data

            # Fetch News Windows (7d / 30d / 90d) from NewsTabAgent cache
            try:
                news_start = time.time()
                news_7d = get_ticker_news(ticker, days=7, max_items=80)
                news_30d = get_ticker_news(ticker, days=30, max_items=120)
                news_90d = get_ticker_news(ticker, days=90, max_items=180)
                news_signals = summarize_ticker_news_signals(
                    ticker,
                    news_7d=news_7d,
                    news_30d=news_30d,
                    news_90d=news_90d,
                )

                analysis.news = [
                    {
                        "title": n.get("title"),
                        "publishedDate": n.get("published_at"),
                        "url": n.get("url"),
                        "site": n.get("source"),
                        "text": n.get("summary"),
                    }
                    for n in news_7d[:6]
                ]
                analysis.breaking_news = [
                    {
                        "title": n.get("title"),
                        "publishedDate": n.get("published_at"),
                        "site": n.get("source"),
                        "ticker": ticker,
                    }
                    for n in news_7d
                    if bool(n.get("is_breaking"))
                ][:5]
                analysis.news_signals = news_signals
                logger.info(
                    f"News windows for {ticker} loaded in {time.time() - news_start:.2f}s "
                    f"(7d={len(news_7d)}, 30d={len(news_30d)}, 90d={len(news_90d)})"
                )
            except Exception as e:
                logger.warning(f"Error fetching cached news for {ticker}: {e}")
                analysis.news = []
                analysis.breaking_news = []
                analysis.news_signals = {}
        except Exception as e:
            logger.error(f"Error fetching fundamental/news data for {ticker}: {e}")

    # ------------------------------------------------------------------
    # NEW: AI Earnings Play Engine (Two-pass + strict JSON output)
    # ------------------------------------------------------------------
    if config.get("news", {}).get("use_ollama", True) and not disable_ollama:
        try:
            if ollama.check_availability():
                ai_start = time.time()

                def _fmt_short(v: Any) -> str:
                    try:
                        num = float(v)
                    except Exception:
                        return str(v) if v not in (None, "") else "unknown"
                    if num >= 1e12:
                        return f"{num/1e12:.2f}T"
                    if num >= 1e9:
                        return f"{num/1e9:.2f}B"
                    if num >= 1e6:
                        return f"{num/1e6:.2f}M"
                    if num >= 1e3:
                        return f"{num/1e3:.1f}K"
                    return f"{num:.2f}"

                hot_list_data = fundamental_data.get("hot_list") or {}
                # 2026-09-03 audit: `ratings` from get_hot_list_data is a LIST
                # of {firm, grade, action} rows — treating it as a dict raised
                # AttributeError whenever recommendations were non-empty and
                # aborted the whole AI analysis block.
                raw_ratings = hot_list_data.get("ratings")
                ratings = raw_ratings if isinstance(raw_ratings, dict) else {}
                insider = hot_list_data.get("insider") or {}
                adx = hot_list_data.get("adx") or {}
                raw_price_change = fundamental_data.get("price_change") or [{}]
                price_change = raw_price_change[0] if isinstance(raw_price_change, list) and raw_price_change else {}

                earnings_list = fundamental_data.get("reporting_dates") or []
                earnings_date = "unknown"
                if isinstance(earnings_list, list) and earnings_list:
                    first_earn = earnings_list[0] or {}
                    earnings_date = str(first_earn.get("date") or first_earn.get("date_formatted") or "unknown")

                news_summary_payload = {
                    "last_7d_positive": (news_signals.get("catalysts_positive") or [])[:6],
                    "last_7d_negative": (news_signals.get("catalysts_negative") or [])[:6],
                    "last_30d_theme": news_signals.get("theme_30d", "unknown"),
                    "major_risk_events": (news_signals.get("risk_events") or [])[:3],
                    "counts": news_signals.get("counts", {}),
                    "top_7d_titles": (news_signals.get("top_headlines", {}).get("7d") or [])[:6],
                    "top_30d_titles": (news_signals.get("top_headlines", {}).get("30d") or [])[:6],
                }
                fundamental_snapshot = _build_fundamental_llm_snapshot(fundamental_data)

                context_payload = {
                    "ticker": ticker,
                    "asset_type": asset_type,
                    "price": price,
                    "change_pct": round(change_pct, 4),
                    "technical": {
                        "trend_1d": trend_1d,
                        "trend_4h": trend_4h,
                        "trend_1h": trend_1h,
                        "trend_30m": trend_30m,
                        "trend_5m": trend_5m,
                        "rsi": round(rsi, 4),
                        "macd_hist": round(macd_hist, 6),
                        "adx": adx.get("adx", "unknown"),
                        "momentum_1w": price_change.get("1W", price_change.get("5D", "unknown")),
                        "momentum_1m": price_change.get("1M", "unknown"),
                    },
                    "market_data": {
                        "insider_sentiment": insider.get("sentiment", "unknown"),
                        "insider_net_buying": _fmt_short(insider.get("netBuying")),
                        "insider_net_selling": _fmt_short(insider.get("netSelling")),
                        "rating": ratings.get("rating", "unknown"),
                        "rating_score": ratings.get("overallScore", "unknown"),
                    },
                    "fundamental_snapshot": fundamental_snapshot,
                    "earnings": {
                        "date": earnings_date,
                        "days": "unknown",
                    },
                    "news_summary": news_summary_payload,
                }
                # Headlines are third-party text: evidence, never instructions.
                prompt_payload = quarantine_analysis_context(context_payload)

                # Single-pass Ollama prompt (replaces 2-pass for speed)
                single_system = "You are a financial analysis engine. Output ONLY valid JSON, no commentary."
                single_prompt = (
                    "Analyze the market data below and return a single JSON object.\n"
                    "Use 'unknown' for any missing values. Do not invent numbers.\n"
                    f"{UNTRUSTED_FIELDS_NOTICE}\n\n"
                    "Required JSON schema (return exactly this structure):\n"
                    "{\n"
                    "  \"sentiment\": \"bullish|bearish|neutral\",\n"
                    "  \"earnings_play\": \"PLAY_PRE|PLAY_POST|WAIT|AVOID\",\n"
                    "  \"confidence\": 0.0,\n"
                    "  \"key_drivers\": [],\n"
                    "  \"risks\": [],\n"
                    "  \"news_summary\": {\n"
                    "    \"last_7d_positive\": [],\n"
                    "    \"last_7d_negative\": [],\n"
                    "    \"last_30d_theme\": \"\",\n"
                    "    \"major_risk_events\": []\n"
                    "  },\n"
                    "  \"scenario\": {\"bull_case\": \"\", \"base_case\": \"\", \"bear_case\": \"\"},\n"
                    "  \"trade_plan\": {\"direction\": \"LONG|SHORT|NONE\", \"entry_condition\": \"\", \"invalidations\": \"\", \"tp_logic\": \"ATR|levels|unknown\", \"notes\": \"\"}\n"
                    "}\n\n"
                    f"MARKET_DATA:\n{json.dumps(prompt_payload, ensure_ascii=False)}\n"
                )
                structured, ai_model_label = run_structured_analysis_call(
                    single_prompt,
                    single_system,
                    context_payload,
                    timeout_seconds=analysis_ai_timeout_seconds(config),
                )
                analysis.ai_analysis = structured
                analysis.ai_analysis_text = (
                    f"sentiment={structured.get('sentiment')} | "
                    f"play={structured.get('earnings_play')} | "
                    f"confidence={structured.get('confidence')}"
                )
                analysis.model = ai_model_label
                if structured.get("ai_status") == AI_STATUS_UNAVAILABLE:
                    logger.warning(
                        "AI analysis unavailable for %s after %.2fs (%s): rule-based summary returned",
                        ticker, time.time() - ai_start, structured.get("ai_error_detail"),
                    )
                else:
                    logger.info(f"Ollama single-pass analysis for {ticker} finished in {time.time() - ai_start:.2f}s")
        except Exception as e:
            logger.error(f"Error generating AI analysis for {ticker}: {e}")
    elif disable_ollama:
        logger.info(f"Ollama skipped for {ticker} (disable_ollama=True)")

    logger.info(f"Total analyze_ticker({ticker}) time: {time.time() - start_total:.2f}s")
    return analysis


def format_analysis_text(analysis: TickerAnalysis) -> str:
    """Format analysis results as readable text."""
    lines = []

    # Header
    lines.append("=" * 70)
    lines.append(f"  TICKER ANALYSIS: {analysis.ticker}")
    lines.append("=" * 70)
    lines.append("")

    # Basic info
    sign = "+" if analysis.change_pct >= 0 else ""
    lines.append(f"Type: {analysis.asset_type.upper()}")
    lines.append(f"Price: ${analysis.price:.4f} ({sign}{analysis.change_pct:.2f}%)")
    lines.append("")

    lines.append("-" * 50)
    lines.append("  MULTI-TIMEFRAME TRENDS")
    lines.append("-" * 50)
    lines.append(f"  1D:  {analysis.trend_1d}")
    lines.append(f"  4H:  {analysis.trend_4h}")
    lines.append(f"  1H:  {analysis.trend_1h}")
    lines.append(f"  30M: {analysis.trend_30m}")
    lines.append(f"  5M:  {analysis.trend_5m}")
    lines.append(f"  MTF Alignment: {analysis.mtf_alignment}/5")
    lines.append("")

    # Key indicators
    lines.append("-" * 50)
    lines.append("  KEY INDICATORS (1m)")
    lines.append("-" * 50)
    lines.append(f"  RSI:                {analysis.rsi:.1f}")
    lines.append(f"  MACD Line:          {analysis.macd_line:.6f}")
    lines.append(f"  MACD Signal:        {analysis.macd_signal:.6f}")
    lines.append(f"  MACD Histogram:     {analysis.macd_hist:.6f} {'(+)' if analysis.macd_hist > 0 else '(-)'}")
    lines.append(f"  MACD Cross:         {'DA (bullish)' if analysis.macd_line > analysis.macd_signal else 'NU (bearish)'}")
    lines.append(f"  ATR (14):           {analysis.atr:.6f}")
    lines.append(f"  VWAP:               ${analysis.vwap:.4f}")
    lines.append(f"  EMA20:              ${analysis.ema20:.4f}")
    lines.append(f"  Dist from VWAP:       {analysis.distance_from_vwap_pct:.2f}% ({analysis.distance_from_vwap_atr:.2f} ATR)")
    lines.append(f"  Dist from EMA20:      {analysis.distance_from_ema20_pct:.2f}% ({analysis.distance_from_ema20_atr:.2f} ATR)")
    lines.append("")

    # Scoruri pe moduri
    lines.append("=" * 70)
    lines.append("  SISTEM SCORING (0-100)")
    lines.append("=" * 70)

    for mode in ["PULLBACK", "BREAKOUT", "CONTRA"]:
        lines.append("")
        lines.append(f"  [{mode}]")
        lines.append("  " + "-" * 40)

        for profile in ["STRONG", "MEDIUM", "RELAX"]:
            score_data = analysis.mode_scores[mode][profile]
            threshold = SCORE_THRESHOLDS[profile]

            status = "VALID" if score_data.best_score >= threshold else "---"
            direction = score_data.best_direction

            lines.append(f"    {profile:8} | {direction:5} | Score: {score_data.best_score:3}/{threshold} | {status}")

    # Best signal
    if analysis.best_signal:
        lines.append("")
        lines.append("=" * 70)
        lines.append("  BEST SIGNAL FOUND")
        lines.append("=" * 70)
        bs = analysis.best_signal
        lines.append(f"  Mod:        {bs['mode']}")
        lines.append(f"  Profil:     {bs['profile']}")
        lines.append(f"  Direction:   {bs['direction']}")
        lines.append(f"  Score:       {bs['score']}/{bs['threshold']}")
        lines.append("")
        lines.append("  Details:")
        for key, val in bs['details'].items():
            lines.append(f"    - {key}: {val}")

    # Threshold reference
    lines.append("")
    lines.append("-" * 50)
    lines.append("  SCORING THRESHOLDS REFERENCE")
    lines.append("-" * 50)
    lines.append("")
    lines.append("  Profil     | Prag Scor | Descriere")
    lines.append("  " + "-" * 45)
    lines.append(f"  STRONG     | >= 85     | Very strict criteria")
    lines.append(f"  MEDIUM     | >= 70     | Balanced")
    lines.append(f"  RELAX      | >= 55     | Permissive")

    return "\n".join(lines)


if __name__ == "__main__":
    # Test
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "BTC-USD"
    print(f"Analyzing {ticker}...")
    result = analyze_ticker(ticker)
    if result:
        print(format_analysis_text(result))
    else:
        print(f"Could not analyze {ticker}")
