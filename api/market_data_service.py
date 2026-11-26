# api/market_data_service.py - Service for fetching LIVE market data
# Uses Binance API for crypto (real-time) and Yahoo Finance for stocks

import logging
import math
import time
import threading
from typing import Any, Dict, Optional
from datetime import datetime, timezone

import pandas as pd
import requests
import yfinance as yf

from core.http_utils import http_get
from core.ta import TechnicalAnalysis
from core.config import BUCHAREST_TZ, is_crypto_ticker, normalize_ticker

logger = logging.getLogger("MarketDataService")

_CRYPTO_LAST_GOOD_TTL_SECONDS = 30 * 60
_CRYPTO_LAST_GOOD: Dict[str, Dict[str, Any]] = {}
_CRYPTO_LAST_GOOD_LOCK = threading.Lock()


def _uses_yahoo_futures_price(ticker: str) -> bool:
    return "=F" in (normalize_ticker(ticker, provider="yahoo") or "")


def _asset_type_for_ticker(ticker: str) -> str:
    if is_crypto_ticker(ticker):
        return "crypto"
    if _uses_yahoo_futures_price(ticker):
        return "commodity"
    return "stock"

# ==================== BINANCE CLIENT (CRYPTO - LIVE PRICES) ====================

class BinanceClient:
    """Binance API client for real-time crypto prices and data."""
    BASE_URL = "https://api.binance.com/api/v3"

    _price_cache: Dict[str, dict] = {}
    _change_cache: Dict[str, dict] = {}
    _klines_cache: Dict[str, dict] = {}
    _unsupported_symbols: Dict[str, float] = {}
    _price_cache_lock = threading.Lock()
    _change_cache_lock = threading.Lock()
    _klines_cache_lock = threading.Lock()
    _unsupported_symbols_lock = threading.Lock()
    _UNSUPPORTED_SYMBOL_TTL_SECONDS = 60 * 60

    @staticmethod
    def to_symbol(ticker: str) -> str:
        """
        Convert ticker to Binance symbol format.
        Examples:
          BTC-USD -> BTCUSDT
          BTCUSDT -> BTCUSDT
          ETH-USD -> ETHUSDT
        """
        t = (ticker or "").upper().replace("-", "").strip()
        if t.endswith("USD") and not t.endswith("USDT"):
            return t[:-3] + "USDT"
        return t

    @classmethod
    def _is_known_unsupported(cls, symbol: str) -> bool:
        now = time.monotonic()
        with cls._unsupported_symbols_lock:
            marked_at = cls._unsupported_symbols.get(symbol)
            if marked_at is None:
                return False
            if now - marked_at < cls._UNSUPPORTED_SYMBOL_TTL_SECONDS:
                return True
            cls._unsupported_symbols.pop(symbol, None)
        return False

    @classmethod
    def _remember_invalid_symbol(cls, symbol: str, response: Any) -> None:
        if response is None or getattr(response, "status_code", None) != 400:
            return
        try:
            payload = response.json() or {}
        except Exception:
            payload = {}
        message = str(payload.get("msg") or payload.get("message") or "").lower()
        if payload.get("code") != -1121 and "invalid symbol" not in message:
            return
        with cls._unsupported_symbols_lock:
            cls._unsupported_symbols[symbol] = time.monotonic()

    @classmethod
    def get_last_price(cls, ticker: str, ttl: int = 2) -> Optional[float]:
        """Get real-time price with minimal cache (2 seconds)."""
        symbol = cls.to_symbol(ticker)
        now = datetime.now()

        with cls._price_cache_lock:
            cached = cls._price_cache.get(symbol)
            if cached and (now - cached["ts"]).total_seconds() < ttl:
                return cached["val"]

        if cls._is_known_unsupported(symbol):
            return cached["val"] if cached else None

        try:
            resp = http_get(
                f"{cls.BASE_URL}/ticker/price",
                provider="binance",
                params={"symbol": symbol},
                timeout=(2, 6),
                max_retries=1,
            )
            if resp is None:
                return cached["val"] if cached else None
            if resp.status_code != 200:
                cls._remember_invalid_symbol(symbol, resp)
                return cached["val"] if cached else None

            data = resp.json() or {}
            price = float(data.get("price", 0) or 0)

            with cls._price_cache_lock:
                cls._price_cache[symbol] = {"val": price, "ts": now}
            return price
        except Exception as e:
            logger.error(f"Binance price error {symbol}: {e}")
            return cached["val"] if cached else None

    @classmethod
    def get_24h_change(cls, ticker: str, ttl: int = 10) -> float:
        """Get 24h price change percentage."""
        symbol = cls.to_symbol(ticker)
        now = datetime.now()

        with cls._change_cache_lock:
            cached = cls._change_cache.get(symbol)
            if cached and (now - cached["ts"]).total_seconds() < ttl:
                return cached["val"]

        if cls._is_known_unsupported(symbol):
            return cached["val"] if cached else 0.0

        try:
            resp = http_get(
                f"{cls.BASE_URL}/ticker/24hr",
                provider="binance",
                params={"symbol": symbol},
                timeout=(2, 6),
                max_retries=1,
            )
            if resp is None:
                return cached["val"] if cached else 0.0
            if resp.status_code == 200:
                val = float((resp.json() or {}).get("priceChangePercent", 0) or 0)
                with cls._change_cache_lock:
                    cls._change_cache[symbol] = {"val": val, "ts": now}
                return val
            cls._remember_invalid_symbol(symbol, resp)
        except Exception as e:
            logger.error(f"Binance 24h change error {symbol}: {e}")
        return 0.0

    @classmethod
    def fetch_klines(
        cls,
        ticker: str,
        interval: str = "1m",
        limit: int = 500,
        ttl: int = 30,
    ) -> Optional[pd.DataFrame]:
        """
        Fetch klines/candlestick data from Binance.
        Returns a DataFrame with DatetimeIndex (UTC) and columns:
          Open, High, Low, Close, Volume
        """
        symbol = cls.to_symbol(ticker)
        cache_key = f"{symbol}_{interval}"
        now = datetime.now()

        with cls._klines_cache_lock:
            cached = cls._klines_cache.get(cache_key)
            if cached and (now - cached["ts"]).total_seconds() < ttl:
                return cached["data"]

        if cls._is_known_unsupported(symbol):
            return None

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
                cls._remember_invalid_symbol(symbol, resp)
                return None

            data = resp.json()
            if not data:
                return None

            rows = []
            for k in data:
                # k format: [openTime, open, high, low, close, volume, closeTime, ...]
                rows.append(
                    {
                        "open_time": int(k[0]),
                        "Open": float(k[1]),
                        "High": float(k[2]),
                        "Low": float(k[3]),
                        "Close": float(k[4]),
                        "Volume": float(k[5]),
                    }
                )

            df = pd.DataFrame(rows)
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            df = df.set_index("open_time")
            df.index.name = "Datetime"

            with cls._klines_cache_lock:
                cls._klines_cache[cache_key] = {"data": df, "ts": now}

            return df
        except Exception as e:
            logger.error(f"Binance klines error {symbol}: {e}")
            return None


# ==================== KUCOIN CLIENT (CRYPTO - SCANNERS) ====================

class KuCoinClient:
    """KuCoin API client for market scanners (Gainers/Losers)."""
    BASE_URL = "https://api.kucoin.com/api/v1"

    _movers_cache: Dict[str, dict] = {} # Key: "gainers" or "losers"
    _cache_lock = threading.Lock()
    _price_cache: Dict[str, dict] = {}
    _change_cache: Dict[str, dict] = {}
    _price_cache_lock = threading.Lock()
    _change_cache_lock = threading.Lock()
    _klines_cache: Dict[str, dict] = {}
    _klines_cache_lock = threading.Lock()
    # F10: max age (seconds) beyond which a stale movers cache is no longer
    # returned as "stale but usable" -- older data would mislead the UI into
    # rendering a stale list as if it were live. 10 min matches the same
    # freshness bound used elsewhere for scanner caches.
    _STALE_MAX_AGE_SECONDS = 600

    @staticmethod
    def to_symbol(ticker: str) -> str:
        t = (ticker or "").upper().replace("/", "").strip()
        if "-" in t:
            base, quote = t.split("-", 1)
            if quote in ("USD", "USDC"):
                quote = "USDT"
            return f"{base}-{quote}"
        if t.endswith("USDT"):
            return f"{t[:-4]}-USDT"
        if t.endswith("USDC"):
            return f"{t[:-4]}-USDT"
        if t.endswith("USD"):
            return f"{t[:-3]}-USDT"
        return t

    @classmethod
    def get_last_price(cls, ticker: str, ttl: int = 2) -> Optional[float]:
        symbol = cls.to_symbol(ticker)
        now = datetime.now()

        with cls._price_cache_lock:
            cached = cls._price_cache.get(symbol)
            if cached and (now - cached["ts"]).total_seconds() < ttl:
                return cached["val"]

        try:
            resp = http_get(
                f"{cls.BASE_URL}/market/orderbook/level1",
                provider="kucoin",
                params={"symbol": symbol},
                timeout=(2, 6),
                max_retries=1,
            )
            if resp is None:
                return cached["val"] if cached else None
            if resp.status_code != 200:
                return cached["val"] if cached else None
            data = resp.json() or {}
            if data.get("code") != "200000":
                return cached["val"] if cached else None
            price = float((data.get("data") or {}).get("price", 0) or 0)
            with cls._price_cache_lock:
                cls._price_cache[symbol] = {"val": price, "ts": now}
            return price
        except Exception as e:
            logger.error(f"KuCoin price error {symbol}: {e}")
            return cached["val"] if cached else None

    @classmethod
    def get_24h_change(cls, ticker: str, ttl: int = 10) -> float:
        symbol = cls.to_symbol(ticker)
        now = datetime.now()

        with cls._change_cache_lock:
            cached = cls._change_cache.get(symbol)
            if cached and (now - cached["ts"]).total_seconds() < ttl:
                return cached["val"]

        try:
            resp = http_get(
                f"{cls.BASE_URL}/market/stats",
                provider="kucoin",
                params={"symbol": symbol},
                timeout=(2, 6),
                max_retries=1,
            )
            if resp is None:
                return cached["val"] if cached else 0.0
            if resp.status_code == 200:
                data = resp.json() or {}
                if data.get("code") == "200000":
                    change_rate = float((data.get("data") or {}).get("changeRate", 0) or 0)
                    val = change_rate * 100.0
                    with cls._change_cache_lock:
                        cls._change_cache[symbol] = {"val": val, "ts": now}
                    return val
        except Exception as e:
            logger.error(f"KuCoin 24h change error {symbol}: {e}")
        return 0.0

    @classmethod
    def fetch_klines(
        cls,
        ticker: str,
        interval: str = "1min",
        limit: int = 200,
        ttl: int = 30,
    ) -> Optional[pd.DataFrame]:
        """
        Fetch klines/candlestick data from KuCoin.
        Returns a DataFrame with DatetimeIndex (UTC) and columns:
          Open, High, Low, Close, Volume
        """
        symbol = cls.to_symbol(ticker)
        cache_key = f"{symbol}_{interval}"
        now = datetime.now()

        with cls._klines_cache_lock:
            cached = cls._klines_cache.get(cache_key)
            if cached and (now - cached["ts"]).total_seconds() < ttl:
                return cached["data"]

        # Approximate window for last N candles
        interval_sec_map = {
            "1min": 60,
            "3min": 180,
            "5min": 300,
            "15min": 900,
            "30min": 1800,
            "1hour": 3600,
            "2hour": 7200,
            "4hour": 14400,
            "6hour": 21600,
            "8hour": 28800,
            "12hour": 43200,
            "1day": 86400,
            "1week": 604800,
        }
        interval_sec = interval_sec_map.get(interval)
        params = {"symbol": symbol, "type": interval}
        if interval_sec:
            params["startAt"] = int(time.time()) - (interval_sec * max(limit, 2))

        try:
            resp = http_get(
                f"{cls.BASE_URL}/market/candles",
                provider="kucoin",
                params=params,
                timeout=(2, 6),
                max_retries=1,
            )
            if resp is None:
                return None
            if resp.status_code != 200:
                return None
            data = resp.json() or {}
            if data.get("code") != "200000":
                return None
            candles = data.get("data") or []
            if not candles:
                return None

            rows = []
            for c in candles:
                # KuCoin format: [time, open, close, high, low, volume, turnover]
                try:
                    ts = int(c[0])
                    rows.append(
                        {
                            "open_time": ts * 1000,
                            "Open": float(c[1]),
                            "Close": float(c[2]),
                            "High": float(c[3]),
                            "Low": float(c[4]),
                            "Volume": float(c[5]),
                        }
                    )
                except Exception:
                    continue

            if not rows:
                return None

            df = pd.DataFrame(rows)
            df = df.sort_values("open_time")
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            df = df.set_index("open_time")
            df.index.name = "Datetime"

            with cls._klines_cache_lock:
                cls._klines_cache[cache_key] = {"data": df, "ts": now}

            return df
        except Exception as e:
            logger.error(f"KuCoin klines error {symbol}: {e}")
            return None
    
    @staticmethod
    def _slice_movers(pairs, limit: int) -> Dict[str, list]:
        """Slice the cached sorted USDT pairs for one request. The cache holds
        the full list so different ?limit= callers each get their own window.
        A legacy cached dict ({"gainers": [...], "losers": [...]}) passes
        through unchanged."""
        if isinstance(pairs, dict):
            return {"gainers": list(pairs.get("gainers") or []),
                    "losers": list(pairs.get("losers") or [])}
        lim = max(1, int(limit or 0))
        return {
            "gainers": list(pairs[:lim]),
            "losers": sorted(pairs[-lim:], key=lambda x: x['change_pct']),
        }

    @classmethod
    def get_top_movers(cls, limit: int = 5, ttl: int = 60) -> Dict[str, list]:
        """
        Get top gainers and losers (USDT pairs only).
        Returns: {"gainers": [...], "losers": [...], "available": bool, "stale": bool, "reason": str}
        TTL: 60 seconds (API data doesn't change THAT fast for top list)

        F10: when the source is stale/unavailable the response now carries an
        explicit ``available: False`` flag (+ ``stale: True`` + a human ``reason``)
        so the frontend can distinguish "movers unavailable" from a genuine
        "no high-volatility pairs" result. A stale cache older than
        ``_STALE_MAX_AGE_SECONDS`` (10 min) is NOT returned (it would mislead the
        UI into rendering a stale list as live); the empty fallback then carries
        ``available: False, stale: False``. Backward compatible: callers that
        only read ``gainers``/``losers`` keep working (extra keys are ignored).
        """
        now = datetime.now()
        with cls._cache_lock:
            cached = cls._movers_cache.get("all")
            if cached and (now - cached["ts"]).total_seconds() < ttl:
                data = cached["data"]  # full sorted USDT pairs, limit-independent
                return {**cls._slice_movers(data, limit), "available": True, "stale": False}
            stale = cached.get("data") if cached else None
            stale_age = (now - cached["ts"]).total_seconds() if cached else None

        stale_usable = (
            stale is not None
            and stale_age is not None
            and stale_age <= cls._STALE_MAX_AGE_SECONDS
        )
        stale_reason = (
            f"KuCoin API unavailable, showing cached data ({int(stale_age)}s old)"
            if stale_usable and stale_age is not None
            else "KuCoin API unavailable"
        )

        try:
            url = f"{cls.BASE_URL}/market/allTickers"
            resp = http_get(url, provider="kucoin", timeout=(2, 6), max_retries=1)
            if resp is None:
                if stale_usable:
                    return {**cls._slice_movers(stale, limit), "available": False, "stale": True, "reason": stale_reason}
                return {"gainers": [], "losers": [], "available": False, "stale": False,
                        "reason": "KuCoin API unavailable"}
            data = resp.json()

            if data.get('code') != '200000':
                if stale_usable:
                    return {**cls._slice_movers(stale, limit), "available": False, "stale": True, "reason": stale_reason}
                return {"gainers": [], "losers": [], "available": False, "stale": False,
                        "reason": "KuCoin API error (non-200 code)"}

            tickers = data['data']['ticker']

            # Filter USDT pairs + minimum volume filter ($50k) to avoid dead coins
            usdt_pairs = []
            for t in tickers:
                symbol = t['symbol']
                if symbol.endswith('-USDT'):
                    try:
                        change_rate = float(t.get('changeRate', 0) or 0)
                        last_price = float(t.get('last', 0) or 0)
                        vol = float(t.get('volValue', 0) or 0)

                        if vol > 50000: # Min $50k volume to avoid spam
                            usdt_pairs.append({
                                'symbol': symbol,
                                'price': last_price,
                                'change_pct': round(change_rate * 100, 2),
                                'volume': vol
                            })
                    except Exception:
                        continue

            # Sort
            usdt_pairs.sort(key=lambda x: x['change_pct'], reverse=True)

            # Store the FULL sorted list (limit-independent): caching the sliced
            # gainers/losers made ?limit=50 serve the ?limit=15 rows for a TTL.
            with cls._cache_lock:
                cls._movers_cache["all"] = {"data": usdt_pairs, "ts": now}

            return {**cls._slice_movers(usdt_pairs, limit), "available": True, "stale": False}

        except Exception as e:
            logger.error(f"KuCoin scan error: {e}")
            if stale_usable:
                return {**cls._slice_movers(stale, limit), "available": False, "stale": True, "reason": stale_reason}
            return {"gainers": [], "losers": [], "available": False, "stale": False,
                    "reason": f"KuCoin scan error: {e}"}



# ==================== YAHOO FINANCE CLIENT (STOCKS) ====================

# Static sector mapping for popular stocks (fallback when Yahoo API fails)
STOCK_SECTORS = {
    # Tech
    "NVDA": "Tech", "AAPL": "Tech", "MSFT": "Tech", "GOOGL": "Tech", "GOOG": "Tech",
    "META": "Tech", "AMZN": "Tech", "TSM": "Tech", "AVGO": "Tech", "ORCL": "Tech",
    "AMD": "Tech", "INTC": "Tech", "QCOM": "Tech", "TXN": "Tech", "MU": "Tech",
    "AMAT": "Tech", "LRCX": "Tech", "KLAC": "Tech", "SNPS": "Tech", "CDNS": "Tech",
    "CRM": "Tech", "NOW": "Tech", "ADBE": "Tech", "INTU": "Tech", "PLTR": "Tech",
    "SNOW": "Tech", "NET": "Tech", "CRWD": "Tech", "ZS": "Tech", "PANW": "Tech",
    "SMCI": "Tech", "DELL": "Tech", "HPE": "Tech", "IBM": "Tech",

    # Financials
    "JPM": "Financials", "BAC": "Financials", "WFC": "Financials", "GS": "Financials",
    "MS": "Financials", "C": "Financials", "SCHW": "Financials", "BLK": "Financials",
    "SPGI": "Financials", "MCO": "Financials", "V": "Financials", "MA": "Financials",
    "AXP": "Financials", "PYPL": "Financials", "SQ": "Financials", "COIN": "Financials",
    "HOOD": "Financials", "MSTR": "Financials", "CLSK": "Financials", "MARA": "Financials",
    "RIOT": "Financials", "HUT": "Financials",

    # Healthcare
    "UNH": "Health", "JNJ": "Health", "LLY": "Health", "PFE": "Health", "ABBV": "Health",
    "MRK": "Health", "TMO": "Health", "ABT": "Health", "DHR": "Health", "BMY": "Health",
    "AMGN": "Health", "GILD": "Health", "VRTX": "Health", "REGN": "Health", "MRNA": "Health",

    # Consumer
    "TSLA": "ConsDisc", "HD": "ConsDisc", "MCD": "ConsDisc", "NKE": "ConsDisc",
    "SBUX": "ConsDisc", "TGT": "ConsDisc", "LOW": "ConsDisc", "BKNG": "ConsDisc",
    "CMG": "ConsDisc", "ABNB": "ConsDisc", "UBER": "ConsDisc", "LYFT": "ConsDisc",
    "WMT": "ConsStp", "COST": "ConsStp", "PG": "ConsStp", "KO": "ConsStp", "PEP": "ConsStp",

    # Energy
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy", "SLB": "Energy", "EOG": "Energy",
    "OXY": "Energy", "PSX": "Energy", "VLO": "Energy", "MPC": "Energy",

    # Communication
    "NFLX": "Comm", "DIS": "Comm", "CMCSA": "Comm", "T": "Comm", "VZ": "Comm",
    "TMUS": "Comm", "SPOT": "Comm", "ROKU": "Comm",

    # Industrials
    "BA": "Indust", "CAT": "Indust", "DE": "Indust", "UPS": "Indust", "FDX": "Indust",
    "HON": "Indust", "GE": "Indust", "RTX": "Indust", "LMT": "Indust", "NOC": "Indust",

    # Materials
    "LIN": "Materials", "APD": "Materials", "SHW": "Materials", "FCX": "Materials",
    "NEM": "Materials", "NUE": "Materials",

    # Real Estate
    "AMT": "RealEst", "PLD": "RealEst", "CCI": "RealEst", "EQIX": "RealEst",
    "SPG": "RealEst", "O": "RealEst",

    # Utilities
    "NEE": "Util", "DUK": "Util", "SO": "Util", "D": "Util", "AEP": "Util",
    # Commodities
    "GC=F": "Commodity", "SI=F": "Commodity", "CL=F": "Commodity",
    "PL=F": "Commodity", "HG=F": "Commodity", "NG=F": "Commodity",
    "PA=F": "Commodity", "HO=F": "Commodity", "RB=F": "Commodity",
}


class YahooClient:
    """Yahoo Finance client for stock data."""
    _cache: Dict[str, dict] = {}
    _cache_lock = threading.Lock()

    @staticmethod
    def _coerce_quote_float(value: Any) -> float:
        try:
            if value is None or value == "":
                return 0.0
            f = float(value)
            return 0.0 if (f != f or f == float("inf") or f == float("-inf")) else f
        except Exception:
            return 0.0

    @classmethod
    def _safe_fast_info_value(cls, fast_info: Any, key: str) -> float:
        if fast_info is None:
            return 0.0
        try:
            return cls._coerce_quote_float(fast_info[key])
        except Exception:
            return 0.0

    @classmethod
    def _fetch_chart_price(cls, ticker: str) -> Dict[str, float]:
        """Fallback that bypasses yfinance's local cache/db quirks."""
        try:
            resp = requests.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/" + ticker,
                params={
                    "range": "5d",
                    "interval": "1d",
                    "includePrePost": "false",
                    "events": "div,splits",
                },
                timeout=6,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code != 200:
                return {"price": 0.0, "prev_close": 0.0}
            payload = resp.json() if resp.content else {}
            result = ((payload.get("chart") or {}).get("result") or [None])[0] or {}
            quote = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
            closes = [cls._coerce_quote_float(v) for v in (quote.get("close") or []) if cls._coerce_quote_float(v) > 0]
            if not closes:
                return {"price": 0.0, "prev_close": 0.0}
            price = closes[-1]
            prev_close = closes[-2] if len(closes) > 1 else price
            return {"price": price, "prev_close": prev_close}
        except Exception:
            return {"price": 0.0, "prev_close": 0.0}

    # ---- quote snapshots (price + the exchange's own last-trade time) ----
    _SPARK_URL = "https://query1.finance.yahoo.com/v7/finance/spark"
    _SPARK_CHUNK = 10
    _quote_cache: Dict[str, dict] = {}

    @staticmethod
    def _epoch_to_iso(value: Any) -> Optional[str]:
        """Provider epoch seconds -> ISO-8601 UTC, or None when absent/invalid."""
        try:
            epoch = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(epoch) or epoch <= 0:
            return None
        try:
            return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    @classmethod
    def parse_spark_quote(cls, response: Any, now_ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Turn one Yahoo spark/chart ``response`` block into a quote snapshot.

        The snapshot carries the exchange's own last-trade time
        (``market_time``) so a Friday close read on a Sunday can never be
        presented as a live price, and ``change_pct`` stays ``None`` when the
        previous close is unknown instead of being invented as 0.00%.
        Returns None when the provider has no usable price for the symbol.
        """
        if not isinstance(response, dict):
            return None
        meta = response.get("meta") if isinstance(response.get("meta"), dict) else {}
        price = cls._coerce_quote_float(meta.get("regularMarketPrice"))
        market_epoch = cls._coerce_quote_float(meta.get("regularMarketTime"))
        session = "regular"

        # Extended-hours bars (includePrePost) are newer than the regular close.
        stamps = response.get("timestamp") or []
        quote = ((response.get("indicators") or {}).get("quote") or [{}])[0] or {}
        closes = quote.get("close") or []
        for stamp, close in zip(reversed(stamps), reversed(closes)):
            bar_price = cls._coerce_quote_float(close)
            bar_epoch = cls._coerce_quote_float(stamp)
            if bar_price <= 0 or bar_epoch <= 0:
                continue
            if price <= 0 or bar_epoch > market_epoch:
                if price > 0:
                    session = "extended"
                price = bar_price
                market_epoch = bar_epoch
            break
        if price <= 0:
            return None

        prev_close = cls._coerce_quote_float(meta.get("previousClose"))
        if prev_close <= 0:
            prev_close = cls._coerce_quote_float(meta.get("chartPreviousClose"))
        change_pct: Optional[float] = None
        if prev_close > 0:
            raw_change = (price - prev_close) / prev_close * 100.0
            if math.isfinite(raw_change):
                change_pct = round(raw_change, 2)

        market_open: Optional[bool] = None
        periods = meta.get("currentTradingPeriod")
        if isinstance(periods, dict):
            bounds = [periods.get(k) or {} for k in ("pre", "regular", "post")]
            starts = [cls._coerce_quote_float(b.get("start")) for b in bounds if isinstance(b, dict)]
            ends = [cls._coerce_quote_float(b.get("end")) for b in bounds if isinstance(b, dict)]
            starts = [v for v in starts if v > 0]
            ends = [v for v in ends if v > 0]
            if starts and ends:
                current = time.time() if now_ts is None else float(now_ts)
                market_open = min(starts) <= current <= max(ends)

        currency = str(meta.get("currency") or "").strip()
        return {
            "price": float(price),
            "prev_close": float(prev_close) if prev_close > 0 else None,
            "change_pct": change_pct,
            "market_time": cls._epoch_to_iso(market_epoch),
            "market_open": market_open,
            "session": session,
            "currency": currency or None,
            "exchange": str(meta.get("fullExchangeName") or meta.get("exchangeName") or "").strip() or None,
            "source": "yahoo",
        }

    @classmethod
    def get_quote_snapshots(cls, tickers: Any, ttl: int = 20) -> Dict[str, Dict[str, Any]]:
        """Batch quotes for many symbols in one small request per 10 symbols.

        Replaces a yfinance ``fast_info`` + ``info`` round trip per symbol
        (two heavy requests each, 8-13 s per watchlist batch under load) with
        Yahoo's multi-symbol spark endpoint. Symbols the provider cannot price
        are simply absent from the result; callers fall back per symbol.
        Never raises.
        """
        wanted: Dict[str, str] = {}
        for raw in tickers or []:
            ticker = str(raw or "").upper().strip()
            if not ticker:
                continue
            y_ticker = normalize_ticker(ticker, provider="yahoo") or ticker
            wanted.setdefault(ticker, str(y_ticker).upper())
        if not wanted:
            return {}

        now_ts = time.time()
        by_symbol: Dict[str, Dict[str, Any]] = {}
        to_fetch: list = []
        with cls._cache_lock:
            for y_ticker in dict.fromkeys(wanted.values()):
                cached = cls._quote_cache.get(y_ticker)
                if cached and (now_ts - float(cached.get("ts", 0.0))) < max(0, int(ttl or 0)):
                    by_symbol[y_ticker] = cached["data"]
                else:
                    to_fetch.append(y_ticker)

        for start in range(0, len(to_fetch), cls._SPARK_CHUNK):
            chunk = to_fetch[start:start + cls._SPARK_CHUNK]
            try:
                resp = http_get(
                    cls._SPARK_URL,
                    provider="yahoo",
                    params={
                        "symbols": ",".join(chunk),
                        "range": "1d",
                        "interval": "1m",
                        "includePrePost": "true",
                    },
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=(3, 6),
                    max_retries=0,
                    priority=True,
                )
                if resp is None or resp.status_code != 200:
                    continue
                payload = resp.json() if resp.content else {}
                results = ((payload.get("spark") or {}).get("result") or []) if isinstance(payload, dict) else []
            except Exception as exc:
                logger.debug(f"Yahoo quote snapshot batch failed for {chunk}: {exc}")
                continue
            for item in results:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol") or "").upper().strip()
                block = (item.get("response") or [None])[0]
                snapshot = cls.parse_spark_quote(block, now_ts=now_ts)
                if not symbol or snapshot is None:
                    continue
                by_symbol[symbol] = snapshot
                with cls._cache_lock:
                    cls._quote_cache[symbol] = {"data": snapshot, "ts": now_ts}

        out: Dict[str, Dict[str, Any]] = {}
        for ticker, y_ticker in wanted.items():
            snapshot = by_symbol.get(y_ticker)
            if snapshot:
                out[ticker] = dict(snapshot)
        return out

    @classmethod
    def get_stock_data(cls, ticker: str, ttl: int = 30) -> Dict:
        """Get stock price and info."""
        now = datetime.now()
        ticker_upper = (ticker or "").upper().strip()
        # Normalize ticker using global utility
        y_ticker = normalize_ticker(ticker_upper, provider="yahoo")

        with cls._cache_lock:
            cached = cls._cache.get(y_ticker)
            if cached and (now - cached["ts"]).total_seconds() < ttl:
                return cached["data"]

        sector = STOCK_SECTORS.get(y_ticker, "Other")

        try:
            stock = yf.Ticker(y_ticker)

            # fast_info first (less likely to fail)
            price = 0.0
            prev_close = 0.0
            prev_close_known = True
            market_epoch = 0.0
            currency: Optional[str] = None
            exchange: Optional[str] = None
            try:
                fi = getattr(stock, "fast_info", None)
                if fi:
                    price = cls._safe_fast_info_value(fi, "lastPrice")
                    if price <= 0:
                        price = cls._safe_fast_info_value(fi, "regularMarketPrice")
                    prev_close = cls._safe_fast_info_value(fi, "previousClose")
                    if prev_close <= 0:
                        prev_close = cls._safe_fast_info_value(fi, "regularMarketPreviousClose")
                    if prev_close <= 0:
                        # No previous close from the provider: the change is
                        # unknown, not 0.00% (delisted / never-traded symbols).
                        prev_close = price
                        prev_close_known = False
            except Exception:
                price = 0.0
                prev_close = 0.0

            # Check live extended hours quote (post-market / pre-market)
            try:
                info = getattr(stock, "info", None)
                if isinstance(info, dict):
                    market_epoch = cls._safe_fast_info_value(info, "regularMarketTime")
                    currency = str(info.get("currency") or "").strip() or None
                    exchange = str(info.get("fullExchangeName") or info.get("exchange") or "").strip() or None
                    post_price = cls._safe_fast_info_value(info, "postMarketPrice")
                    pre_price = cls._safe_fast_info_value(info, "preMarketPrice")
                    if post_price > 0:
                        market_epoch = cls._safe_fast_info_value(info, "postMarketTime") or market_epoch
                    elif pre_price > 0:
                        market_epoch = cls._safe_fast_info_value(info, "preMarketTime") or market_epoch
                    if post_price > 0:
                        price = post_price
                        ref_close = cls._safe_fast_info_value(info, "regularMarketPreviousClose") or cls._safe_fast_info_value(info, "previousClose") or cls._safe_fast_info_value(info, "regularMarketPrice")
                        if ref_close > 0:
                            prev_close = ref_close
                            prev_close_known = True
                    elif pre_price > 0:
                        price = pre_price
                        ref_close = cls._safe_fast_info_value(info, "regularMarketPreviousClose") or cls._safe_fast_info_value(info, "previousClose") or cls._safe_fast_info_value(info, "regularMarketPrice")
                        if ref_close > 0:
                            prev_close = ref_close
                            prev_close_known = True
            except Exception:
                pass

            # The chart metadata yfinance already holds (no extra request)
            # carries the exchange's last-trade time for symbols whose quote
            # summary is empty (delisted, OTC).
            if market_epoch <= 0 or not currency or not exchange:
                try:
                    meta = stock.get_history_metadata()
                    if isinstance(meta, dict):
                        if market_epoch <= 0:
                            market_epoch = cls._coerce_quote_float(meta.get("regularMarketTime"))
                        currency = currency or (str(meta.get("currency") or "").strip() or None)
                        exchange = exchange or (
                            str(meta.get("fullExchangeName") or meta.get("exchangeName") or "").strip() or None
                        )
                except Exception:
                    pass

            # fallback: daily history
            if price == 0:
                try:
                    hist = stock.history(period="2d", interval="1d")
                    if hist is not None and not hist.empty:
                        price = float(hist["Close"].iloc[-1])
                        prev_close = float(hist["Close"].iloc[-2]) if len(hist) > 1 else price
                        prev_close_known = len(hist) > 1
                        try:
                            market_epoch = float(hist.index[-1].timestamp())
                        except Exception:
                            pass
                except Exception:
                    pass

            # yfinance can fail on some Windows hosts due local sqlite/cache issues.
            # Fall back to Yahoo's chart endpoint to still get the latest close.
            if price == 0:
                chart_price = cls._fetch_chart_price(y_ticker)
                if chart_price.get("price", 0) > 0:
                    price = float(chart_price["price"])
                    prev_close = float(chart_price.get("prev_close") or price)

            change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0.0
            if change_pct != change_pct or change_pct == float("inf") or change_pct == float("-inf"):
                change_pct = 0.0

            data = {
                "price": float(price) if price else 0.0,
                "change_pct": round(float(change_pct), 2),
                "sector": sector,
                # Additive honesty fields: change_pct_known=False means the
                # change above is a placeholder (no previous close);
                # market_time is the exchange's last-trade time (None when the
                # provider gave none).
                "change_pct_known": bool(price and prev_close and prev_close_known),
                "market_time": cls._epoch_to_iso(market_epoch),
                "currency": currency,
                "exchange": exchange,
            }

            with cls._cache_lock:
                cls._cache[y_ticker] = {"data": data, "ts": now}

            return data
        except Exception as e:
            logger.warning(f"Yahoo data fetch failed for {ticker}: {e}")
            return {"price": 0.0, "change_pct": 0.0, "sector": sector}

    @classmethod
    def fetch_history(cls, ticker: str, period: str = "5d", interval: str = "1h") -> Optional[pd.DataFrame]:
        """Fetch historical data from Yahoo Finance."""
        try:
            # Normalize for history fetch as well
            y_ticker = normalize_ticker(ticker, provider="yahoo")
            stock = yf.Ticker(y_ticker)
            hist = stock.history(period=period, interval=interval)
            if hist is None or hist.empty:
                return None
            return hist
        except Exception:
            return None


# ==================== TREND + TS HELPERS ====================

def _format_ts_from_df(df: Optional[pd.DataFrame]) -> str:
    if df is None or df.empty:
        return ""
    try:
        ts = df.index[-1]
        # if tz-aware pandas Timestamp (UTC), convert to Bucharest
        if getattr(ts, "tz", None) is not None:
            ts = ts.tz_convert(BUCHAREST_TZ)
        # format
        return ts.strftime("%H:%M:%S")
    except Exception:
        return ""

def _resample_ohlc(df: Optional[pd.DataFrame], rule: str) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None
    try:
        cols = df.columns
        o = "Open" if "Open" in cols else "open"
        h = "High" if "High" in cols else "high"
        l = "Low" if "Low" in cols else "low"
        c = "Close" if "Close" in cols else "close"
        v = "Volume" if "Volume" in cols else "volume"
        resampled = df.resample(rule).agg({
            o: "first",
            h: "max",
            l: "min",
            c: "last",
            v: "sum",
        })
        resampled = resampled.dropna()
        if resampled.empty:
            return None
        # Normalize column names to match expectations
        resampled = resampled.rename(columns={o: "Open", h: "High", l: "Low", c: "Close", v: "Volume"})
        return resampled
    except Exception:
        return None

def calculate_trend(df: Optional[pd.DataFrame]) -> str:
    """Calculate trend from MACD histogram."""
    if df is None or df.empty or len(df) < 26:
        return "N/A"
    try:
        close = df["Close"] if "Close" in df.columns else df["close"]
        _, _, macd_hist = TechnicalAnalysis.calc_macd(close)
        if macd_hist is None or macd_hist.empty:
            return "N/A"
        return TechnicalAnalysis.get_trend_from_macd(macd_hist)
    except Exception:
        return "N/A"

def _optional_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        out = float(value)
    except Exception:
        return None
    if not math.isfinite(out):
        return None
    return out


def calculate_rsi(df: Optional[pd.DataFrame], period: int = 14) -> Optional[float]:
    """Calculate RSI from dataframe."""
    if df is None or df.empty or len(df) < period + 1:
        return None
    try:
        close = df["Close"] if "Close" in df.columns else df["close"]
        rsi_series = TechnicalAnalysis.calc_rsi(close, period)
        if rsi_series is None or rsi_series.empty:
            return None
        return _optional_float(rsi_series.iloc[-1])
    except Exception:
        return None

def calculate_macd_hist(df: Optional[pd.DataFrame]) -> float:
    """Calculate MACD histogram value."""
    if df is None or df.empty or len(df) < 26:
        return 0.0
    try:
        close = df["Close"] if "Close" in df.columns else df["close"]
        _, _, hist = TechnicalAnalysis.calc_macd(close)
        if hist is None or hist.empty:
            return 0.0
        return float(hist.iloc[-1])
    except Exception:
        return 0.0


def _to_yahoo_crypto_ticker(ticker: str) -> str:
    t = (ticker or "").upper().strip()
    if t.endswith("USDT") or t.endswith("USDC"):
        # Stablecoin itself (USDT/USDC with no base asset) has no Yahoo pair.
        # Pre-fix this returned "-USD" (empty base), which Yahoo can't resolve
        # and would yield garbage/empty prices. Return "" so callers skip it
        # (mirror F2/F3 empty-base guard).
        base = t[:-4]
        return f"{base}-USD" if base else ""
    return t


def _remember_last_good_crypto(ticker: str, payload: Dict[str, Any]) -> None:
    try:
        price = float(payload.get("price") or 0.0)
    except Exception:
        price = 0.0
    if price <= 0:
        return

    key = (ticker or "").upper().strip()
    if not key:
        return

    safe_payload = {
        "ticker": key,
        "price": price,
        "change_pct": float(payload.get("change_pct") or 0.0),
        "trend_1d": payload.get("trend_1d", "N/A"),
        "trend_4h": payload.get("trend_4h", "N/A"),
        "trend_1h": payload.get("trend_1h", "N/A"),
        "trend_30m": payload.get("trend_30m", "N/A"),
        "trend_5m": payload.get("trend_5m", "N/A"),
        "ts_1d": payload.get("ts_1d", ""),
        "ts_4h": payload.get("ts_4h", ""),
        "ts_1h": payload.get("ts_1h", ""),
        "ts_30m": payload.get("ts_30m", ""),
        "ts_5m": payload.get("ts_5m", ""),
        "rsi": _optional_float(payload.get("rsi")),
        "macd_hist": float(payload.get("macd_hist") or 0.0),
        "sector": "Crypto",
        "asset_type": "crypto",
    }
    with _CRYPTO_LAST_GOOD_LOCK:
        _CRYPTO_LAST_GOOD[key] = {"ts": time.time(), "data": safe_payload}


def _get_last_good_crypto(ticker: str) -> Optional[Dict[str, Any]]:
    key = (ticker or "").upper().strip()
    if not key:
        return None

    now_ts = time.time()
    with _CRYPTO_LAST_GOOD_LOCK:
        item = _CRYPTO_LAST_GOOD.get(key)
        if not item:
            return None
        age = now_ts - float(item.get("ts", 0.0))
        if age > _CRYPTO_LAST_GOOD_TTL_SECONDS:
            _CRYPTO_LAST_GOOD.pop(key, None)
            return None
        data = item.get("data")
        return dict(data) if isinstance(data, dict) else None


def _build_crypto_payload_from_cache(ticker: str, cached: Dict[str, Any], lightweight: bool) -> Dict[str, Any]:
    if lightweight:
        return {
            "ticker": ticker,
            "price": float(cached.get("price") or 0.0),
            "change_pct": round(float(cached.get("change_pct") or 0.0), 2),
            "trend_1d": "N/A",
            "trend_4h": "N/A",
            "trend_1h": "N/A",
            "trend_30m": "N/A",
            "trend_5m": "N/A",
            "ts_1d": "",
            "ts_4h": "",
            "ts_1h": "",
            "ts_30m": "",
            "ts_5m": "",
            "rsi": 50.0,
            "macd_hist": 0.0,
            "sector": "Crypto",
            "asset_type": "crypto",
        }
    payload = dict(cached)
    payload["ticker"] = ticker
    payload["sector"] = "Crypto"
    payload["asset_type"] = "crypto"
    return payload


# ==================== MAIN DATA FETCHER ====================

def get_ticker_data(ticker: str, lightweight: bool = False) -> Dict:
    """
    Get complete market data for a ticker.
    Uses Binance for crypto (live prices) and Yahoo for stocks.
    :param lightweight: If True, skips history/klines/trends (much faster)
    """
    is_crypto = ticker.endswith("-USD") or ticker.endswith("USDT") or ticker.endswith("-USDC")
    return _get_crypto_data(ticker, lightweight) if is_crypto else _get_stock_data(ticker, lightweight)


def prefers_kucoin_ticker(ticker: str) -> bool:
    """Return whether the symbol explicitly uses KuCoin's dashed quote form.

    Scanner rows sourced from KuCoin retain ``BASE-USDT`` while Binance rows
    use ``BASEUSDT``. Preserving that venue hint prevents a predictable
    Binance invalid-symbol request for every KuCoin-only mover.
    """
    raw = str(ticker or "").upper().strip()
    return raw.endswith("-USDT") or raw.endswith("-USDC")

def _get_crypto_data(ticker: str, lightweight: bool = False) -> Dict:
    """Get crypto data from Binance (real-time)."""
    try:
        ticker = (ticker or "").upper().strip()
        cached_payload = _get_last_good_crypto(ticker)

        price = 0.0
        change_pct = 0.0
        data_source = "unavailable"
        venues = (
            (("kucoin", KuCoinClient), ("binance", BinanceClient))
            if prefers_kucoin_ticker(ticker)
            else (("binance", BinanceClient), ("kucoin", KuCoinClient))
        )
        for venue_name, venue_client in venues:
            venue_price = venue_client.get_last_price(ticker, ttl=2) or 0.0
            if venue_price <= 0:
                continue
            price = venue_price
            change_pct = venue_client.get_24h_change(ticker, ttl=10)
            data_source = venue_name
            break

        if not price or price <= 0:
            try:
                from core.alpaca_client import get_alpaca_client

                alpaca = get_alpaca_client()
                if alpaca.status().get("crypto_fallback"):
                    alpaca_data = alpaca.get_market_data(ticker, crypto=True, ttl=5)
                    if alpaca_data and float(alpaca_data.get("price") or 0.0) > 0:
                        price = float(alpaca_data["price"])
                        change_pct = float(alpaca_data.get("change_pct") or 0.0)
                        data_source = str(alpaca_data.get("source") or "alpaca_crypto")
            except Exception as exc:
                logger.debug("Alpaca crypto fallback failed for %s: %s", ticker, exc)

        if not price or price <= 0:
            yahoo_symbol = _to_yahoo_crypto_ticker(ticker)
            yahoo_data = YahooClient.get_stock_data(yahoo_symbol, ttl=30)
            yahoo_price = float(yahoo_data.get("price") or 0.0)
            if yahoo_price > 0:
                price = yahoo_price
                change_pct = float(yahoo_data.get("change_pct") or 0.0)
                data_source = "yahoo"
                # Log fallback to Yahoo for visibility
                logger.info(f"Crypto ticker {ticker} fallback to YahooFinance: {price}")


        # Lightweight: Return price only, skip klines/trends
        if lightweight:
            payload = {
                "ticker": ticker,
                "price": float(price),
                "change_pct": round(float(change_pct), 2),
                "trend_1d": "N/A", "trend_4h": "N/A",
                "trend_1h": "N/A", "trend_30m": "N/A", "trend_5m": "N/A",
                "ts_1d": "", "ts_4h": "",
                "ts_1h": "", "ts_30m": "", "ts_5m": "",
                "rsi": 50.0,
                "macd_hist": 0.0,
                "sector": "Crypto",
                "asset_type": "crypto",
                "data_source": data_source,
            }
            if float(payload.get("price") or 0.0) > 0:
                _remember_last_good_crypto(ticker, payload)
                return payload
            if cached_payload:
                return _build_crypto_payload_from_cache(ticker, cached_payload, lightweight=True)
            return payload

        ku_interval_map = {
            "1m": "1min",
            "5m": "5min",
            "30m": "30min",
            "1h": "1hour",
            "4h": "4hour",
            "1d": "1day",
        }

        def _fetch_crypto_klines(interval: str, limit: int, ttl: int) -> Optional[pd.DataFrame]:
            ku_interval = ku_interval_map.get(interval)
            kline_venues = (
                ((KuCoinClient, ku_interval), (BinanceClient, interval))
                if data_source == "kucoin" or prefers_kucoin_ticker(ticker)
                else ((BinanceClient, interval), (KuCoinClient, ku_interval))
            )
            for venue_client, venue_interval in kline_venues:
                if not venue_interval:
                    continue
                df = venue_client.fetch_klines(ticker, venue_interval, limit, ttl=ttl)
                if df is not None and not df.empty:
                    return df
            return None

        df_1h = _fetch_crypto_klines("1h", 120, ttl=30)
        df_30m = _fetch_crypto_klines("30m", 120, ttl=30)
        df_5m = _fetch_crypto_klines("5m", 120, ttl=15)
        df_4h = _fetch_crypto_klines("4h", 200, ttl=60)
        df_1d = _fetch_crypto_klines("1d", 240, ttl=120)

        trend_1d = calculate_trend(df_1d)
        trend_4h = calculate_trend(df_4h)
        trend_1h = calculate_trend(df_1h)
        trend_30m = calculate_trend(df_30m)
        trend_5m = calculate_trend(df_5m)

        ts_1d = _format_ts_from_df(df_1d)
        ts_4h = _format_ts_from_df(df_4h)
        ts_1h = _format_ts_from_df(df_1h)
        ts_30m = _format_ts_from_df(df_30m)
        ts_5m = _format_ts_from_df(df_5m)

        rsi = calculate_rsi(df_5m) if df_5m is not None else None
        macd_hist = calculate_macd_hist(df_5m) if df_5m is not None else 0.0

        payload = {
            "ticker": ticker,
            "price": float(price),
            "change_pct": round(float(change_pct), 2),

            "trend_1d": trend_1d,
            "trend_4h": trend_4h,
            "trend_1h": trend_1h,
            "trend_30m": trend_30m,
            "trend_5m": trend_5m,

            # IMPORTANT: timestamps for UI
            "ts_1d": ts_1d,
            "ts_4h": ts_4h,
            "ts_1h": ts_1h,
            "ts_30m": ts_30m,
            "ts_5m": ts_5m,

            "rsi": round(float(rsi), 1),
            "macd_hist": float(macd_hist),

            "sector": "Crypto",
            "asset_type": "crypto",
            "data_source": data_source,
        }
        if float(payload.get("price") or 0.0) > 0:
            _remember_last_good_crypto(ticker, payload)
            return payload
        if cached_payload:
            return _build_crypto_payload_from_cache(ticker, cached_payload, lightweight=False)
        return payload
    except Exception as e:
        logger.error(f"Error getting crypto data for {ticker}: {e}")
        cached_payload = _get_last_good_crypto(ticker)
        if cached_payload:
            return _build_crypto_payload_from_cache(ticker, cached_payload, lightweight=lightweight)
        return _get_empty_data(ticker)

def _get_stock_data(ticker: str, lightweight: bool = False) -> Dict:
    """Get stock data from Yahoo Finance."""
    try:
        price = 0.0
        change_pct = 0.0
        quote = {}
        y_ticker = normalize_ticker(ticker, provider="yahoo")
        prefers_yahoo_futures = _uses_yahoo_futures_price(ticker)
        asset_type = _asset_type_for_ticker(ticker)
        sector = "Commodity" if prefers_yahoo_futures else STOCK_SECTORS.get(y_ticker, "Other")
        data_source = "unavailable"

        # Alpaca IEX is the preferred stock quote source when credentials are
        # configured. Futures remain on Yahoo because Alpaca does not cover them.
        if not prefers_yahoo_futures:
            try:
                from core.alpaca_client import get_alpaca_client

                alpaca = get_alpaca_client()
                if alpaca.status().get("prefer_for_stocks"):
                    alpaca_data = alpaca.get_market_data(ticker, crypto=False, ttl=5)
                    if alpaca_data and float(alpaca_data.get("price") or 0.0) > 0:
                        price = float(alpaca_data["price"])
                        change_pct = float(alpaca_data.get("change_pct") or 0.0)
                        data_source = str(alpaca_data.get("source") or "alpaca_iex")
            except Exception as exc:
                logger.debug("Alpaca stock quote failed for %s: %s", ticker, exc)

        if price <= 0:
            stock_data = YahooClient.get_stock_data(ticker, ttl=30)
            price = float(stock_data.get("price", 0.0) or 0.0)
            change_pct = float(stock_data.get("change_pct", 0.0) or 0.0)
            sector = stock_data.get("sector", sector)
            if price > 0:
                data_source = "yahoo"

        # Lightweight: Return price only, skip history/trends
        if lightweight:
             return {
                "ticker": ticker,
                "price": float(price),
                "change_pct": float(change_pct),
                "trend_1d": "N/A", "trend_4h": "N/A",
                "trend_1h": "N/A", "trend_30m": "N/A", "trend_5m": "N/A",
                "ts_1d": "", "ts_4h": "",
                "ts_1h": "", "ts_30m": "", "ts_5m": "",
                "rsi": None,
                "macd_hist": None,
                "sector": sector,
                "asset_type": asset_type,
                "data_source": data_source,
             }

        df_1d = YahooClient.fetch_history(ticker, "6mo", "1d")
        df_1h = YahooClient.fetch_history(ticker, "5d", "1h")
        df_30m = YahooClient.fetch_history(ticker, "5d", "30m")
        df_5m = YahooClient.fetch_history(ticker, "1d", "5m")

        df_4h = _resample_ohlc(df_1h, "4H")

        trend_1d = calculate_trend(df_1d)
        trend_4h = calculate_trend(df_4h)
        trend_1h = calculate_trend(df_1h)
        trend_30m = calculate_trend(df_30m)
        trend_5m = calculate_trend(df_5m)

        # optional timestamps (harmless for UI; engine will overwrite anyway)
        ts_1d = _format_ts_from_df(df_1d)
        ts_4h = _format_ts_from_df(df_4h)
        ts_1h = _format_ts_from_df(df_1h)
        ts_30m = _format_ts_from_df(df_30m)
        ts_5m = _format_ts_from_df(df_5m)

        has_5m_history = df_5m is not None and not df_5m.empty
        rsi = calculate_rsi(df_5m) if has_5m_history else None
        macd_hist = calculate_macd_hist(df_5m) if has_5m_history and len(df_5m) >= 26 else None

        return {
            "ticker": ticker,
            "price": float(price),
            "change_pct": float(change_pct),

            "trend_1d": trend_1d,
            "trend_4h": trend_4h,
            "trend_1h": trend_1h,
            "trend_30m": trend_30m,
            "trend_5m": trend_5m,

            "ts_1d": ts_1d,
            "ts_4h": ts_4h,
            "ts_1h": ts_1h,
            "ts_30m": ts_30m,
            "ts_5m": ts_5m,

            "rsi": round(float(rsi), 1) if rsi is not None else None,
            "macd_hist": float(macd_hist) if macd_hist is not None else None,

            "sector": sector,
            "asset_type": asset_type,
            "data_source": data_source,
        }
    except Exception as e:
        logger.error(f"Error getting stock data for {ticker}: {e}")
        return _get_empty_data(ticker)

def _get_empty_data(ticker: str) -> Dict:
    """Return empty data structure."""
    is_crypto = ticker.endswith("-USD") or ticker.endswith("USDT") or ticker.endswith("-USDC")
    asset_type = _asset_type_for_ticker(ticker)
    return {
        "ticker": ticker,
        "price": 0.0,
        "change_pct": 0.0,

        "trend_1d": "N/A",
        "trend_4h": "N/A",
        "trend_1h": "N/A",
        "trend_30m": "N/A",
        "trend_5m": "N/A",

        "ts_1d": "",
        "ts_4h": "",
        "ts_1h": "",
        "ts_30m": "",
        "ts_5m": "",

        "rsi": 50.0 if is_crypto else None,
        "macd_hist": 0.0 if is_crypto else None,

        "sector": "Crypto" if is_crypto else "Other",
        "asset_type": asset_type,
    }

def clear_cache() -> None:
    """Clear all caches."""
    with BinanceClient._price_cache_lock:
        BinanceClient._price_cache = {}
    with BinanceClient._change_cache_lock:
        BinanceClient._change_cache = {}
    with BinanceClient._klines_cache_lock:
        BinanceClient._klines_cache = {}
    with BinanceClient._unsupported_symbols_lock:
        BinanceClient._unsupported_symbols = {}
    with KuCoinClient._price_cache_lock:
        KuCoinClient._price_cache = {}
    with KuCoinClient._change_cache_lock:
        KuCoinClient._change_cache = {}
    with KuCoinClient._klines_cache_lock:
        KuCoinClient._klines_cache = {}
    with YahooClient._cache_lock:
        YahooClient._cache = {}
    try:
        from core.alpaca_client import get_alpaca_client

        get_alpaca_client().clear_cache()
    except Exception:
        pass
    with _CRYPTO_LAST_GOOD_LOCK:
        _CRYPTO_LAST_GOOD.clear()

def get_crypto_movers(limit: int = 5) -> Dict[str, list]:
    """Get top crypto gainers/losers from KuCoin."""
    return KuCoinClient.get_top_movers(limit=limit)


# ==================== SYMBOL EXISTENCE CHECK (Add ticker) ====================
# A last trade older than this means the listing is no longer active
# (delisted / suspended); long holiday breaks are far shorter.
SYMBOL_INACTIVE_AFTER_SECONDS = 14 * 86400


def _symbol_check(status: str, ticker: str, source: str, reason: str = "", **extra: Any) -> Dict[str, Any]:
    return {"status": status, "ticker": ticker, "source": source, "reason": reason, **extra}


def _stock_symbol_check(symbol: str, connect_timeout: float, read_timeout: float) -> Dict[str, Any]:
    """Existence of a stock / futures symbol from the Yahoo quote snapshot path."""
    y_ticker = str(normalize_ticker(symbol, provider="yahoo") or symbol).upper()
    try:
        from core.stock_data_client import is_known_missing

        if is_known_missing(y_ticker):
            return _symbol_check("not_found", symbol, "known_missing",
                                 "the data provider already reported this symbol as unknown")
    except Exception:
        pass

    def _from_snapshot(snapshot: Dict[str, Any], source: str) -> Dict[str, Any]:
        market_time = str(snapshot.get("market_time") or "")
        age: Optional[float] = None
        if market_time:
            try:
                parsed = datetime.fromisoformat(market_time.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                age = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
            except (TypeError, ValueError):
                age = None
        if age is not None and age > SYMBOL_INACTIVE_AFTER_SECONDS:
            return _symbol_check("inactive", symbol, source,
                                 f"last trade {market_time[:10]}", last_trade=market_time)
        return _symbol_check("found", symbol, source, last_trade=market_time or None)

    with YahooClient._cache_lock:
        cached = YahooClient._quote_cache.get(y_ticker)
    if cached and (time.time() - float(cached.get("ts", 0.0))) < 300 and isinstance(cached.get("data"), dict):
        return _from_snapshot(cached["data"], "yahoo_quote_cache")

    try:
        resp = http_get(
            YahooClient._SPARK_URL,
            provider="yahoo",
            params={"symbols": y_ticker, "range": "1d", "interval": "1m", "includePrePost": "true"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=(connect_timeout, read_timeout),
            max_retries=0,
            priority=True,
        )
    except Exception as exc:
        return _symbol_check("unverified", symbol, "yahoo", f"lookup failed: {type(exc).__name__}")
    if resp is None:
        return _symbol_check("unverified", symbol, "yahoo", "data provider not reachable")
    status_code = int(getattr(resp, "status_code", 0) or 0)
    try:
        payload = resp.json() if resp.content else {}
    except Exception:
        payload = {}
    spark = payload.get("spark") if isinstance(payload, dict) else None
    spark = spark if isinstance(spark, dict) else {}
    if status_code == 404:
        error = spark.get("error") if isinstance(spark.get("error"), dict) else {}
        if str(error.get("code") or "").strip().lower() == "not found":
            return _symbol_check("not_found", symbol, "yahoo", "the data provider has no such symbol")
        return _symbol_check("unverified", symbol, "yahoo", "data provider answered HTTP 404")
    if status_code != 200:
        return _symbol_check("unverified", symbol, "yahoo", f"data provider answered HTTP {status_code}")
    for item in spark.get("result") or []:
        if not isinstance(item, dict) or str(item.get("symbol") or "").upper().strip() != y_ticker:
            continue
        snapshot = YahooClient.parse_spark_quote((item.get("response") or [None])[0], now_ts=time.time())
        if snapshot is None:
            break
        with YahooClient._cache_lock:
            YahooClient._quote_cache[y_ticker] = {"data": snapshot, "ts": time.time()}
        return _from_snapshot(snapshot, "yahoo")
    return _symbol_check("not_found", symbol, "yahoo", "the data provider returned no quote for this symbol")


def _crypto_symbol_check(symbol: str) -> Dict[str, Any]:
    """Existence of a crypto pair on the venues the app prices crypto from."""
    venues = (
        (("kucoin", KuCoinClient), ("binance", BinanceClient))
        if prefers_kucoin_ticker(symbol)
        else (("binance", BinanceClient), ("kucoin", KuCoinClient))
    )
    unknown_on = 0
    for name, client in venues:
        try:
            price = client.get_last_price(symbol, ttl=30)
        except Exception:
            price = None
        if price is not None and price > 0:
            return _symbol_check("found", symbol, name)
        if name == "binance":
            # Binance answered "Invalid symbol" (remembered by the client).
            unknown = bool(BinanceClient._is_known_unsupported(BinanceClient.to_symbol(symbol)))
        else:
            # KuCoin answers an unknown pair with an empty book: price 0, not None.
            unknown = price is not None and price <= 0
        unknown_on += 1 if unknown else 0
    if unknown_on == len(venues):
        return _symbol_check("not_found", symbol, "binance+kucoin", "neither exchange lists this pair")
    return _symbol_check("unverified", symbol, "binance+kucoin", "exchange not reachable")


def verify_symbol_exists(ticker: str, timeout_seconds: float = 5.0) -> Dict[str, Any]:
    """Can the app get a quote for this ticker? Used by Add ticker. Never raises.

    The "status" key is one of
      found       a provider returned a quote
      inactive    a quote exists but the last trade is weeks old (delisted / suspended)
      not_found   the provider answered definitively that it has no such symbol
      unverified  the lookup could not be completed (network, throttling, timeout):
                  this must not be read as "the symbol does not exist"
    The whole lookup is bounded by timeout_seconds.
    """
    symbol = str(ticker or "").upper().strip()
    if not symbol:
        return _symbol_check("not_found", symbol, "none", "empty symbol")
    deadline = max(1.0, min(float(timeout_seconds or 5.0), 15.0))
    holder: Dict[str, Any] = {}

    def _run() -> None:
        try:
            if is_crypto_ticker(symbol):
                holder["result"] = _crypto_symbol_check(symbol)
            else:
                holder["result"] = _stock_symbol_check(symbol, min(2.0, deadline), deadline)
        except Exception as exc:  # defensive: a lookup bug must not block adding
            logger.warning(f"Symbol check failed for {symbol}: {exc}")
            holder["result"] = _symbol_check("unverified", symbol, "none", f"lookup failed: {type(exc).__name__}")

    worker = threading.Thread(target=_run, name="symbol-verify", daemon=True)
    worker.start()
    worker.join(deadline)
    result = holder.get("result")
    if not isinstance(result, dict):
        return _symbol_check("unverified", symbol, "none", f"lookup timed out after {deadline:.0f}s")
    return result
