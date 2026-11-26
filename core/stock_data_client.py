# Stock Data Client - Yahoo Finance wrapper for stock market data
# Replaces the old FMPClient (which was already a yfinance wrapper)
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import requests
import yfinance as yf

from core.data_quality import get_health_dashboard, QualifiedValue, QualityLevel
from core.http_utils import http_get

logger = logging.getLogger("MarketEngine")

_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}


# ---------------------------------------------------------------------------
# Symbol routing + negative cache
# ---------------------------------------------------------------------------
# Yahoo has no exchange-pair symbols (KAVAUSDT, BTC-USDT, BTCUSDC). Callers
# that pass them used to trigger one 404 / "possibly delisted" round trip per
# symbol per cycle. Price requests are routed to Yahoo's BASE-USD listing;
# stock-only requests (fundamentals, earnings, insiders, analysts) are refused
# for every crypto symbol.
_EXCHANGE_PAIR_SUFFIXES = ("-USDT", "-USDC", "USDT", "USDC")

# A symbol Yahoo reported as unknown is not asked again for a while. Two
# strikes are required before blocking (one empty answer can be transient) and
# the block doubles on every further miss, so a delisted watchlist ticker
# costs a handful of requests per day instead of one per scan.
_NOT_FOUND_BASE_TTL_SECONDS = max(
    60.0, float(os.getenv("MS_YAHOO_NOT_FOUND_TTL_SECONDS", "900") or 900)
)
_NOT_FOUND_MAX_TTL_SECONDS = 6 * 3600.0
_NOT_FOUND_STRIKES_TO_BLOCK = 2
_NOT_FOUND_STRIKE_WINDOW_SECONDS = 3600.0
_NOT_FOUND_LOCK = threading.Lock()
# yahoo symbol -> {"strikes", "last", "blocked_until", "reason"} (monotonic clock)
_NOT_FOUND: Dict[str, Dict[str, Any]] = {}

# A "current" quote whose last trade is older than this is a delisted or
# suspended listing, never a live price (longest regular closure is 4 days).
_STALE_LISTING_MAX_AGE_SECONDS = 7 * 86400.0

# History spans over which an existing listing always has at least one bar; an
# empty answer for a shorter span (1d on a weekend) proves nothing.
_DEFINITIVE_EMPTY_PERIODS = {"5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}


def _plain_symbol(symbol: Any) -> str:
    return str(symbol or "").strip().lstrip("$").upper()


def is_exchange_crypto_pair(symbol: Any) -> bool:
    """True for exchange pair notation (KAVAUSDT, BTC-USDT, BTCUSDC)."""
    token = _plain_symbol(symbol)
    return any(token.endswith(suffix) and len(token) > len(suffix) for suffix in _EXCHANGE_PAIR_SUFFIXES)


def is_crypto_symbol(symbol: Any) -> bool:
    """True for any crypto symbol, including Yahoo's BASE-USD form."""
    token = _plain_symbol(symbol)
    if not token:
        return False
    if is_exchange_crypto_pair(token):
        return True
    try:
        from core.config import is_crypto_ticker

        return bool(is_crypto_ticker(token))
    except Exception:
        return token.endswith("-USD")


def yahoo_symbol_for(symbol: Any) -> Optional[str]:
    """Yahoo symbol to request for ``symbol``, or None when Yahoo cannot have it.

    * ``$AAPL``    -> ``AAPL``
    * ``KAVAUSDT`` -> ``KAVA-USD`` (exchange pair -> Yahoo crypto listing)
    * non-ASCII / empty symbols -> None (no request is made)
    """
    token = _plain_symbol(symbol)
    if not token or not token.isascii() or any(ch.isspace() for ch in token):
        return None
    for suffix in _EXCHANGE_PAIR_SUFFIXES:
        if token.endswith(suffix) and len(token) > len(suffix):
            base = token[: -len(suffix)].rstrip("-")
            return f"{base}-USD" if base else None
    return token


def _not_found_key(symbol: Any) -> str:
    return yahoo_symbol_for(symbol) or _plain_symbol(symbol)


def is_known_missing(symbol: Any) -> bool:
    """True while ``symbol`` is negatively cached as unknown to Yahoo."""
    key = _not_found_key(symbol)
    if not key:
        return True
    now = time.monotonic()
    with _NOT_FOUND_LOCK:
        entry = _NOT_FOUND.get(key)
        return bool(entry and float(entry.get("blocked_until") or 0.0) > now)


def mark_missing(symbol: Any, reason: str = "not_found", *, exact: bool = False) -> bool:
    """Record one definitive "unknown symbol" answer. Returns True once blocked.

    ``exact=True`` records the symbol exactly as the provider named it, without
    routing: "KAVAUSDT is unknown" says nothing about the KAVA-USD listing.
    """
    key = _plain_symbol(symbol) if exact else _not_found_key(symbol)
    if not key:
        return False
    now = time.monotonic()
    with _NOT_FOUND_LOCK:
        entry = _NOT_FOUND.get(key)
        if entry is None or (now - float(entry.get("last") or 0.0)) > max(
            _NOT_FOUND_STRIKE_WINDOW_SECONDS, _NOT_FOUND_MAX_TTL_SECONDS * 2
        ):
            entry = {"strikes": 0, "last": now, "blocked_until": 0.0, "reason": reason}
            _NOT_FOUND[key] = entry
        if float(entry.get("blocked_until") or 0.0) > now:
            return True
        entry["strikes"] = int(entry.get("strikes") or 0) + 1
        entry["last"] = now
        entry["reason"] = str(reason or "not_found")[:120]
        if entry["strikes"] < _NOT_FOUND_STRIKES_TO_BLOCK:
            return False
        ttl = min(
            _NOT_FOUND_MAX_TTL_SECONDS,
            _NOT_FOUND_BASE_TTL_SECONDS * (2 ** (entry["strikes"] - _NOT_FOUND_STRIKES_TO_BLOCK)),
        )
        entry["blocked_until"] = now + ttl
        first_block = entry["strikes"] == _NOT_FOUND_STRIKES_TO_BLOCK
    if first_block:
        logger.warning(
            "Yahoo has no data for %s (%s); not asking again for %.0f min",
            key,
            reason,
            ttl / 60.0,
        )
    return True


def clear_missing(symbol: Any = None) -> None:
    """Forget a negative-cache entry (after a successful answer) or all of them."""
    with _NOT_FOUND_LOCK:
        if symbol is None:
            _NOT_FOUND.clear()
            return
        _NOT_FOUND.pop(_not_found_key(symbol), None)


def missing_symbols_snapshot() -> Dict[str, Dict[str, Any]]:
    """Currently blocked symbols with the seconds left, for diagnostics."""
    now = time.monotonic()
    with _NOT_FOUND_LOCK:
        return {
            key: {
                "reason": entry.get("reason"),
                "strikes": int(entry.get("strikes") or 0),
                "retry_in_seconds": round(float(entry.get("blocked_until") or 0.0) - now, 1),
            }
            for key, entry in _NOT_FOUND.items()
            if float(entry.get("blocked_until") or 0.0) > now
        }


def _requestable_yahoo_symbol(symbol: Any) -> Optional[str]:
    """Yahoo symbol to request now, or None (unroutable or negatively cached)."""
    routed = yahoo_symbol_for(symbol)
    if routed is None or is_known_missing(routed):
        return None
    return routed


_YF_NOT_FOUND_PATTERNS = (
    re.compile(r"^\$?(?P<sym>[^:\s]+): possibly delisted; no price data found\s*\(period=(?P<period>\w+)\)"),
    re.compile(r"Quote not found for symbol: (?P<sym>[A-Za-z0-9.\-=^]+)"),
)
_YF_LOG_REPEAT_SECONDS = 900.0


class _YFinanceNoiseFilter(logging.Filter):
    """Learn "unknown symbol" answers from yfinance and stop repeating them.

    yfinance reports an unknown symbol only through its own logger. Every
    module that calls yfinance directly therefore produced the same ERROR line
    on every cycle. The filter feeds those answers into the negative cache and
    lets an identical line through at most once per 15 minutes.
    """

    def __init__(self) -> None:
        super().__init__("yfinance-noise")
        self._seen: Dict[str, float] = {}
        self._seen_lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        text = str(message or "").strip()
        if not text:
            # yfinance emits bare blank ERROR lines around its failure summary.
            return record.levelno < logging.ERROR
        for pattern in _YF_NOT_FOUND_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            period = (match.groupdict().get("period") or "").lower()
            if not period or period in _DEFINITIVE_EMPTY_PERIODS:
                mark_missing(match.group("sym"), "yfinance_not_found", exact=True)
            break
        now = time.monotonic()
        key = text[:200]
        with self._seen_lock:
            last = self._seen.get(key)
            if last is not None and (now - last) < _YF_LOG_REPEAT_SECONDS:
                return False
            if len(self._seen) > 2000:
                self._seen.clear()
            self._seen[key] = now
        return True


def _install_yfinance_noise_filter() -> None:
    yf_logger = logging.getLogger("yfinance")
    if not any(isinstance(item, _YFinanceNoiseFilter) for item in yf_logger.filters):
        yf_logger.addFilter(_YFinanceNoiseFilter())


_install_yfinance_noise_filter()

# Market-cap TTL cache (Faza 3, 2026-07-07). symbol -> (fetched_at, market_cap).
# 6h TTL — market cap changes slowly and yfinance .info is rate-limited hard,
# so we avoid re-fetching on every Pre-Earnings refresh. Fallback to FinnHub
# /stock/profile2 (marketCapitalization in millions) when yfinance lacks it.
_MARKET_CAP_TTL_SECONDS = 6 * 3600
_MARKET_CAP_CACHE: Dict[str, tuple] = {}
_MARKET_CAP_LOCK = __import__("threading").Lock()


def _to_float(value) -> Optional[float]:
    """Safe float cast (None for non-numeric / empty)."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf guard
        return None
    return f


def get_stock_data_client():
    """Create and return a singleton StockDataClient instance."""
    return StockDataClient()


class StockDataClient:
    """Yahoo Finance-based stock data client."""

    _legacy_cache: Dict[str, tuple] = {}

    @staticmethod
    def _clean_symbol(symbol: str) -> str:
        """Strip common prefixes/suffixes that break yfinance lookups."""
        if not isinstance(symbol, str):
            return str(symbol)
        # Remove leading '$' used by some data sources (e.g., $BUGDF)
        return symbol.lstrip("$")

    def __init__(self, api_key=None):
        # api_key parameter kept for backward compatibility but not used
        self.api_key = api_key

    # Shared routing / negative-cache helpers, exposed for modules that call
    # yfinance directly.
    yahoo_symbol_for = staticmethod(yahoo_symbol_for)
    is_known_missing = staticmethod(is_known_missing)
    mark_missing = staticmethod(mark_missing)
    clear_missing = staticmethod(clear_missing)
    is_exchange_crypto_pair = staticmethod(is_exchange_crypto_pair)

    @staticmethod
    def _stock_only_symbol(symbol) -> Optional[str]:
        """Symbol for stock-only data, or None for crypto / unknown symbols."""
        if is_crypto_symbol(symbol):
            return None
        return _requestable_yahoo_symbol(symbol)

    @staticmethod
    def _strip_tz(df):
        """Strip timezone from DatetimeIndex and any datetime columns to prevent
        'Cannot compare tz-naive and tz-aware timestamps' errors downstream."""
        import pandas as pd
        if df is None or df.empty:
            return df
        # Strip timezone from DatetimeIndex
        if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None:
            df.index = df.index.tz_convert(None)
        # Strip timezone from any datetime columns
        for col in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                if hasattr(df[col].dt, 'tz') and df[col].dt.tz is not None:
                    df[col] = df[col].dt.tz_convert(None)
        return df

    def get_historical(self, symbol, period="1d", interval="5m"):
        routed = yahoo_symbol_for(symbol)
        if routed is None:
            # Not a symbol any provider here can resolve: no request at all.
            return None
        result = None
        fetch_failed = False
        if not is_known_missing(routed):
            try:
                ticker = yf.Ticker(routed)
                df = ticker.history(period=period, interval=interval)
                time.sleep(0.2)
                result = self._strip_tz(df)
                if result is not None and not result.empty:
                    clear_missing(routed)
                elif str(period or "").lower() in _DEFINITIVE_EMPTY_PERIODS:
                    mark_missing(routed, f"empty_history_{period}")
            except Exception as e:
                logger.error(f"Error getting historical for {symbol}: {e}")
                result = None
                fetch_failed = True

        if result is None or result.empty:
            try:
                from core.etoro_client import get_etoro_client
                e_client = get_etoro_client()
                if e_client.is_available():
                    e_df = e_client.get_candles_df(symbol, interval=interval, count=100)
                    if e_df is not None and not e_df.empty:
                        logger.info(f"eToro historical fallback provided {len(e_df)} candles for {symbol}")
                        get_health_dashboard().register(
                            "yf_historical", QualifiedValue.verified({"rows": len(e_df)}, "etoro_candles")
                        )
                        return e_df
            except Exception as e_exc:
                logger.debug(f"eToro historical fallback failed for {symbol}: {e_exc}")

        # The health stream describes the provider, so it stores a row count
        # (never the DataFrame) and only a failed request marks it down: an
        # unknown symbol or a closed-market empty window is not an outage.
        if result is not None and not result.empty:
            get_health_dashboard().register(
                "yf_historical", QualifiedValue.verified({"rows": len(result)}, "yfinance_history")
            )
        elif fetch_failed:
            get_health_dashboard().register(
                "yf_historical", QualifiedValue.unavailable("yfinance_history", "History request failed")
            )
        return result

    @staticmethod
    def get_historical_chart(symbol, timeframe=None, limit=100, interval=None, as_dataframe=False):
        """Get historical chart data. Static method for backward compat.

        Args:
            symbol: Ticker symbol
            timeframe: Chart timeframe (1min, 5min, 15min, 30min, 1hour, 4hour, 1day)
            limit: Max number of bars to return
            interval: Alias for timeframe (backward compat)
            as_dataframe: If True, return pandas DataFrame; if False (default), return list of dicts

        Returns:
            list[dict] by default, or pd.DataFrame if as_dataframe=True. None on error.
        """
        try:
            import pandas as pd

            timeframe = timeframe or interval or "1hour"

            tf_map = {
                "1min": ("1d", "1m"),
                "5min": ("1d", "5m"),
                "15min": ("5d", "15m"),
                "30min": ("5d", "30m"),
                "1hour": ("1mo", "1h"),
                "4hour": ("3mo", "1h"),
                "1day": ("1y", "1d")
            }

            period, yf_interval = tf_map.get(timeframe, ("1mo", "1h"))

            routed = yahoo_symbol_for(symbol)
            if routed is None:
                return None
            df = None
            if not is_known_missing(routed):
                ticker = yf.Ticker(routed)
                df = ticker.history(period=period, interval=yf_interval)
                time.sleep(0.2)
                if df is not None and len(df) > 0:
                    clear_missing(routed)
                elif period in _DEFINITIVE_EMPTY_PERIODS:
                    mark_missing(routed, f"empty_history_{period}")

            if df is None or len(df) == 0:
                try:
                    from core.etoro_client import get_etoro_client
                    e_client = get_etoro_client()
                    if e_client.is_available():
                        e_candles = e_client.get_candles(symbol, interval=timeframe, count=limit, direction="asc")
                        if e_candles:
                            if as_dataframe:
                                df_e = pd.DataFrame(e_candles)
                                if "date" in df_e.columns:
                                    df_e["date"] = pd.to_datetime(df_e["date"])
                                    df_e = df_e.set_index("date")
                                get_health_dashboard().register(
                                    "yf_chart", QualifiedValue.verified({"rows": len(df_e)}, "etoro_chart")
                                )
                                return df_e
                            get_health_dashboard().register(
                                "yf_chart", QualifiedValue.verified({"rows": len(e_candles)}, "etoro_chart")
                            )
                            return e_candles
                except Exception as e_exc:
                    logger.debug(f"eToro chart fallback failed for {symbol}: {e_exc}")
                # An empty window for one symbol (unknown ticker, closed
                # market) is not a provider outage: leave the stream as is.
                return None

            if timeframe == "4hour":
                # Yahoo supplies hourly bars. Aggregate inside each exchange
                # session before dropping its timezone; never join overnight.
                sessions = []
                for _, session in df.groupby(df.index.date):
                    candles = session.resample("4h", origin=session.index[0]).agg({
                        "Open": "first", "High": "max", "Low": "min",
                        "Close": "last", "Volume": "sum",
                    }).dropna(subset=["Open", "Close"])
                    sessions.append(candles)
                df = pd.concat(sessions).sort_index()

            # Strip timezone from yfinance data to prevent tz comparison errors
            df = StockDataClient._strip_tz(df)

            if len(df) > limit:
                df = df.tail(limit)

            if as_dataframe:
                # Return DataFrame with standard lowercase column names
                df = df.reset_index()
                df.columns = [c.lower() for c in df.columns]
                if 'datetime' in df.columns and 'date' not in df.columns:
                    df = df.rename(columns={'datetime': 'date'})
                get_health_dashboard().register(
                    "yf_chart", QualifiedValue.verified({"rows": len(df)}, "yfinance_chart")
                )
                return df

            records = []
            for idx, row in df.iterrows():
                records.append({
                    'date': idx.strftime('%Y-%m-%d %H:%M:%S') if hasattr(idx, 'strftime') else str(idx),
                    'open': float(row['Open']),
                    'high': float(row['High']),
                    'low': float(row['Low']),
                    'close': float(row['Close']),
                    'volume': int(row['Volume'])
                })

            get_health_dashboard().register(
                "yf_chart", QualifiedValue.verified({"rows": len(records)}, "yfinance_chart")
            )
            return records

        except Exception as e:
            logger.error(f"Error getting historical chart for {symbol}: {e}")
            get_health_dashboard().register("yf_chart", QualifiedValue.unavailable("yfinance_chart", str(e)))
            return None

    @staticmethod
    def _fetch_yahoo_chart_quote(symbol: str) -> dict:
        """Fast price fetch via Yahoo chart API (~200ms vs 2-3s for yf.Ticker.info)."""
        routed = _requestable_yahoo_symbol(symbol)
        if routed is None:
            # Unroutable (non-ASCII / empty) or negatively cached: no request.
            return {"symbol": symbol, "price": 0, "changesPercentage": 0, "unavailable_reason": "symbol_not_on_yahoo"}
        try:
            resp = http_get(
                "https://query1.finance.yahoo.com/v8/finance/chart/" + routed,
                provider="yahoo",
                params={"range": "5d", "interval": "1d", "includePrePost": "false"},
                timeout=(3, 6),
                headers=_YAHOO_HEADERS,
            )
            if resp is not None and resp.status_code == 404:
                mark_missing(routed, "chart_404")
            if resp is None or resp.status_code != 200:
                try:
                    from core.etoro_client import get_etoro_client
                    e_client = get_etoro_client()
                    if e_client.is_available():
                        eq = e_client.get_quote(symbol)
                        if eq and eq.get("price", 0) > 0:
                            get_health_dashboard().register("yf_prices", QualifiedValue.verified(eq, "etoro_quote"))
                            return eq
                except Exception:
                    pass
                return {"symbol": symbol, "price": 0, "changesPercentage": 0}
            payload = resp.json() if resp.content else {}
            result = ((payload.get("chart") or {}).get("result") or [None])[0] or {}
            meta = result.get("meta") or {}
            price = float(meta.get("regularMarketPrice", 0) or 0)
            prev_close = float(meta.get("chartPreviousClose", 0)
                               or meta.get("previousClose", 0) or 0)
            change_pct = 0.0
            if prev_close > 0 and price > 0:
                change_pct = round(((price - prev_close) / prev_close) * 100, 4)
            # ``timestamp`` is when the quote was fetched; ``market_time`` is
            # when the instrument last traded. They differ by days on a
            # weekend and by months for a delisted listing.
            market_time = int(_to_float(meta.get("regularMarketTime")) or 0)
            market_age = (time.time() - market_time) if market_time > 0 else None
            result = {
                "symbol": symbol,
                "price": price,
                "changesPercentage": change_pct,
                "change": round(price - prev_close, 4) if prev_close > 0 else 0,
                "previousClose": prev_close,
                "exchange": meta.get("exchangeName", ""),
                "volume": int(meta.get("regularMarketVolume", 0) or 0),
                "dayHigh": float(meta.get("regularMarketDayHigh", 0) or 0) or None,
                "dayLow": float(meta.get("regularMarketDayLow", 0) or 0) or None,
                "timestamp": int(time.time()),
                "market_time": market_time or None,
                "market_time_age_seconds": round(market_age, 1) if market_age is not None else None,
            }
            if price > 0 and market_age is not None and market_age > _STALE_LISTING_MAX_AGE_SECONDS:
                # Yahoo keeps answering for a delisted / suspended listing
                # with its final trade (EA: last trade 2026-08-04 still served
                # as regularMarketPrice in November). That is not a quote.
                mark_missing(routed, "listing_inactive")
                result.update({
                    "price": 0,
                    "changesPercentage": 0,
                    "change": 0,
                    "last_trade_price": price,
                    "listing_inactive": True,
                    "unavailable_reason": "no_trade_for_over_7_days",
                })
                return result
            if price > 0:
                clear_missing(routed)
                get_health_dashboard().register(
                    "yf_prices",
                    QualifiedValue.verified({"symbol": symbol, "price": price}, "yahoo_chart_api"),
                )
            else:
                try:
                    from core.etoro_client import get_etoro_client
                    e_client = get_etoro_client()
                    if e_client.is_available():
                        eq = e_client.get_quote(symbol)
                        if eq and eq.get("price", 0) > 0:
                            get_health_dashboard().register("yf_prices", QualifiedValue.verified(eq, "etoro_quote"))
                            return eq
                except Exception:
                    pass
                get_health_dashboard().register("yf_prices", QualifiedValue.estimated(result, "yahoo_chart_api", "Price returned as zero"))
            return result
        except Exception as e:
            logger.debug(f"Yahoo chart quote failed for {symbol}: {e}")
            try:
                from core.etoro_client import get_etoro_client
                e_client = get_etoro_client()
                if e_client.is_available():
                    eq = e_client.get_quote(symbol)
                    if eq and eq.get("price", 0) > 0:
                        get_health_dashboard().register("yf_prices", QualifiedValue.verified(eq, "etoro_quote"))
                        return eq
            except Exception:
                pass
            get_health_dashboard().register("yf_prices", QualifiedValue.unavailable("yahoo_chart_api", str(e)))
            return {"symbol": symbol, "price": 0, "changesPercentage": 0}

    @staticmethod
    def _cache_key(prefix: str, symbols) -> str:
        normalized = [str(symbol or "").upper().strip() for symbol in (symbols or []) if str(symbol or "").strip()]
        return f"{prefix}_{','.join(normalized)}"

    @classmethod
    def _get_cached(cls, key, ttl=60):
        cached = cls._legacy_cache.get(str(key or ""))
        if not cached:
            return None
        payload, ts = cached
        if ttl is not None and ttl >= 0 and (time.time() - ts) > float(ttl):
            return None
        return payload

    @classmethod
    def _set_cache(cls, key, data, permanent=False, ttl_seconds=300):
        cls._legacy_cache[str(key or "")] = (data, time.time())
        return data

    @classmethod
    def get_batch_quote(cls, symbols, ttl=None):
        """Get batch quotes for multiple symbols using fast Yahoo chart API.

        Uses direct Yahoo chart endpoint (~200ms/ticker) instead of slow
        yf.Ticker.info (~2-3s/ticker). Fetches in parallel for speed.
        """
        if not symbols:
            return []
        normalized = []
        seen = set()
        for symbol in symbols:
            sym = str(symbol or "").upper().strip()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            normalized.append(sym)

        cache_key = cls._cache_key("batch_quote", normalized)
        cached = cls._get_cached(cache_key, ttl=ttl or 60)
        if cached is not None:
            return list(cached)

        results = []
        with ThreadPoolExecutor(max_workers=min(len(normalized), 8)) as pool:
            futures = {pool.submit(cls.get_quote, symbol, ttl=ttl): symbol for symbol in normalized}
            for future in as_completed(futures):
                try:
                    result = future.result(timeout=8)
                    if isinstance(result, dict) and result.get("price", 0) > 0:
                        results.append(result)
                except Exception:
                    pass

        ordered = []
        by_symbol = {str(item.get("symbol") or "").upper().strip(): item for item in results if isinstance(item, dict)}
        for sym in normalized:
            item = by_symbol.get(sym)
            if item:
                ordered.append(item)
        cls._set_cache(cache_key, ordered, ttl_seconds=ttl or 300)
        return ordered

    # ---- Backward-compatible aliases for old FMPClient method names ----
    # These methods were called by ticker_analyzer.py and other modules.
    # They delegate to yfinance directly.

    def get_price_and_change(self, symbol):
        """Get current price and change for a symbol."""
        routed = _requestable_yahoo_symbol(symbol)
        if routed is None:
            return None
        try:
            ticker = yf.Ticker(routed)
            info = ticker.info or {}
            time.sleep(0.2)
            post_price = _to_float(info.get('postMarketPrice'))
            pre_price = _to_float(info.get('preMarketPrice'))
            reg_price = _to_float(info.get('regularMarketPrice')) or 0.0
            prev_close = _to_float(info.get('regularMarketPreviousClose') or info.get('previousClose')) or reg_price

            if post_price and post_price > 0:
                price = post_price
                change = price - prev_close if prev_close else _to_float(info.get('postMarketChange')) or 0.0
                change_pct = ((price - prev_close) / prev_close * 100.0) if prev_close else _to_float(info.get('postMarketChangePercent')) or 0.0
            elif pre_price and pre_price > 0:
                price = pre_price
                change = price - prev_close if prev_close else _to_float(info.get('preMarketChange')) or 0.0
                change_pct = ((price - prev_close) / prev_close * 100.0) if prev_close else _to_float(info.get('preMarketChangePercent')) or 0.0
            else:
                price = reg_price
                change = _to_float(info.get('regularMarketChange')) or 0.0
                change_pct = _to_float(info.get('regularMarketChangePercent')) or 0.0

            result = {'price': price, 'change': change, 'changePercent': change_pct}
            if price > 0:
                get_health_dashboard().register("yf_price_change", QualifiedValue.verified(result, "yfinance_info"))
            else:
                get_health_dashboard().register("yf_price_change", QualifiedValue.estimated(result, "yfinance_info", "Price is zero"))
            return result
        except Exception as e:
            logger.error(f"Error getting price for {symbol}: {e}")
            get_health_dashboard().register("yf_price_change", QualifiedValue.unavailable("yfinance_info", str(e)))
            return None

    # Earnings cache: symbol -> (items, timestamp)
    _earnings_cache: Dict[str, tuple] = {}
    _earnings_cache_ttl = 3600  # 1 hour

    def get_calendar_earnings(self, symbol):
        """Get earnings calendar for a symbol. Returns list of dicts with 'date' key.

        Uses in-memory cache (1h TTL) to avoid slow yfinance calls blocking the API.
        """
        now = time.time()
        cached = self._earnings_cache.get(symbol)
        if cached and (now - cached[1]) < self._earnings_cache_ttl:
            return cached[0]

        routed = self._stock_only_symbol(symbol)
        if routed is None:
            # Crypto has no earnings; unknown symbols are not asked again.
            return []
        try:
            ticker = yf.Ticker(routed)
            cal = ticker.calendar
            if cal is None:
                return []
            items = []
            if isinstance(cal, dict):
                earnings_date = cal.get("Earnings Date")
                if earnings_date is not None:
                    import pandas as pd
                    if isinstance(earnings_date, pd.Timestamp):
                        items.append({"date": earnings_date.strftime("%Y-%m-%d"), "when": ""})
                    elif isinstance(earnings_date, (list, pd.Series)):
                        for d in earnings_date:
                            if hasattr(d, "strftime"):
                                items.append({"date": d.strftime("%Y-%m-%d"), "when": ""})
                            else:
                                items.append({"date": str(d)[:10], "when": ""})
                    elif hasattr(earnings_date, "strftime"):
                        items.append({"date": earnings_date.strftime("%Y-%m-%d"), "when": ""})
                    else:
                        date_str = str(earnings_date)[:10]
                        if date_str and date_str != "NaT":
                            items.append({"date": date_str, "when": ""})
                if not items:
                    for key, val in cal.items():
                        if "date" in key.lower() or "earnings" in key.lower():
                            if hasattr(val, "strftime"):
                                items.append({"date": val.strftime("%Y-%m-%d"), "when": ""})
                            elif isinstance(val, str):
                                items.append({"date": val[:10], "when": ""})
            elif hasattr(cal, "iterrows"):
                for _, row in cal.iterrows():
                    date_val = row.get("Earnings Date", row.iloc[0] if len(row) > 0 else None)
                    if hasattr(date_val, "strftime"):
                        items.append({"date": date_val.strftime("%Y-%m-%d"), "when": ""})
                    else:
                        items.append({"date": str(date_val)[:10], "when": ""})
            self._earnings_cache[symbol] = (items, now)
            get_health_dashboard().register("yf_earnings", QualifiedValue.verified(items, "yfinance_calendar"))
            return items
        except Exception as e:
            logger.error(f"Error getting earnings calendar for {symbol}: {e}")
            get_health_dashboard().register("yf_earnings", QualifiedValue.unavailable("yfinance_calendar", str(e)))
            return []

    def get_reporting_dates(self, symbol):
        """Get earnings reporting dates."""
        routed = self._stock_only_symbol(symbol)
        if routed is None:
            return None
        try:
            ticker = yf.Ticker(routed)
            dates = ticker.earnings_dates
            time.sleep(0.2)
            return self._strip_tz(dates) if dates is not None else None
        except Exception as e:
            logger.error(f"Error getting reporting dates for {symbol}: {e}")
            return None

    def get_balance_sheet(self, symbol):
        """Get balance sheet data (FMP-era list-of-records shape)."""
        routed = self._stock_only_symbol(symbol)
        if routed is None:
            return None
        try:
            ticker = yf.Ticker(routed)
            data = self._strip_tz(ticker.balance_sheet)
            time.sleep(0.2)
            return self._statement_records(data)
        except Exception as e:
            logger.error(f"Error getting balance sheet for {symbol}: {e}")
            return None

    # 2026-09-03 audit: yfinance statements are line-item-indexed DataFrames,
    # but consumers (Fundamentals tab, ticker_analyzer) expect FMP-era
    # list-of-row-dicts with camelCase keys. Normalize once, here.
    _STATEMENT_KEY_MAP = {
        "cash and cash equivalents": "cashAndCashEquivalents",
        "total debt": "totalDebt",
        "stockholders equity": "totalStockholderEquity",
        "free cash flow": "freeCashFlow",
        "operating cash flow": "operatingCashFlow",
        "capital expenditure": "capitalExpenditure",
        "total revenue": "revenue",
        "gross profit": "grossProfit",
        "operating income": "operatingIncome",
        "net income": "netIncome",
        "ebitda": "ebitda",
        "basic eps": "eps",
        "diluted eps": "epsdiluted",
        "research and development": "researchAndDevelopmentExpenses",
    }

    def _statement_records(self, df) -> List[Dict[str, Any]]:
        import pandas as pd

        if df is None or getattr(df, "empty", True):
            return []
        try:
            records: List[Dict[str, Any]] = []
            transposed = df.T  # rows = reporting dates, columns = line items
            for _, row in transposed.iterrows():
                rec: Dict[str, Any] = {}
                for label, value in row.items():
                    key = self._STATEMENT_KEY_MAP.get(str(label).strip().lower())
                    if key is None:
                        continue
                    try:
                        fv = float(value)
                        rec[key] = fv if fv == fv else None  # NaN guard
                    except (TypeError, ValueError):
                        rec[key] = None
                if rec:
                    records.append(rec)
            return records
        except Exception as e:
            logger.error(f"Error converting statement to records: {e}")
            return []

    def get_cash_flow(self, symbol):
        """Get cash flow data."""
        routed = self._stock_only_symbol(symbol)
        if routed is None:
            return None
        try:
            ticker = yf.Ticker(routed)
            data = self._strip_tz(ticker.cashflow)
            time.sleep(0.2)
            return self._statement_records(data)
        except Exception as e:
            logger.error(f"Error getting cash flow for {symbol}: {e}")
            return None

    def get_income_statement(self, symbol):
        """Get income statement data."""
        routed = self._stock_only_symbol(symbol)
        if routed is None:
            return None
        try:
            ticker = yf.Ticker(routed)
            data = self._strip_tz(ticker.income_stmt)
            time.sleep(0.2)
            return self._statement_records(data)
        except Exception as e:
            logger.error(f"Error getting income statement for {symbol}: {e}")
            return None

    def get_analyst_estimates(self, symbol):
        """Get analyst price targets."""
        routed = self._stock_only_symbol(symbol)
        if routed is None:
            return None
        try:
            ticker = yf.Ticker(routed)
            info = ticker.info
            time.sleep(0.2)
            return {
                'targetHighPrice': info.get('targetHighPrice', 0),
                'targetLowPrice': info.get('targetLowPrice', 0),
                'targetMeanPrice': info.get('targetMeanPrice', 0),
                'targetMedianPrice': info.get('targetMedianPrice', 0),
                'numberOfAnalystOpinions': info.get('numberOfAnalystOpinions', 0),
                'recommendationKey': info.get('recommendationKey', ''),
                # 2026-09-03 audit: the Fundamentals tab renders these FMP-era
                # keys (estimatedEpsAvg etc.); yfinance .info carries the same
                # facts under forwardEps/epsCurrentYear.
                'estimatedEpsAvg': info.get('forwardEps'),
                'estimatedEps': info.get('forwardEps'),
                'estimatedEpsLow': info.get('epsCurrentYear') if info.get('epsCurrentYear') and info.get('forwardEps') and info.get('epsCurrentYear') < info.get('forwardEps') else None,
                'estimatedEpsHigh': info.get('epsCurrentYear') if info.get('epsCurrentYear') and info.get('forwardEps') and info.get('epsCurrentYear') >= info.get('forwardEps') else None,
                'epsEstimatedAvg': info.get('forwardEps'),
                'estimatedRevenueAvg': info.get('totalRevenue'),
                'period': 'current',
                'date': None,
            }
        except Exception as e:
            logger.error(f"Error getting analyst estimates for {symbol}: {e}")
            return None

    def get_enterprise_values(self, symbol):
        """Get enterprise value data."""
        routed = self._stock_only_symbol(symbol)
        if routed is None:
            return None
        try:
            ticker = yf.Ticker(routed)
            info = ticker.info
            time.sleep(0.2)
            return {
                'enterpriseValue': info.get('enterpriseValue', 0),
                'enterpriseToRevenue': info.get('enterpriseToRevenue', 0),
                'enterpriseToEbitda': info.get('enterpriseToEbitda', 0),
            }
        except Exception as e:
            logger.error(f"Error getting enterprise value for {symbol}: {e}")
            return None

    def get_stock_price_change(self, symbol):
        """Get stock price change data over various periods."""
        routed = _requestable_yahoo_symbol(symbol)
        if routed is None:
            return None
        try:
            ticker = yf.Ticker(routed)
            info = ticker.info
            time.sleep(0.2)
            return {
                '5D': info.get('5DayPriceChangePercent', 0),
                '1M': info.get('1MonthPriceChangePercent', 0),
                '3M': info.get('3MonthPriceChangePercent', 0),
                '1Y': info.get('52WeekChange', 0),
                'YTD': info.get('ytdReturn', 0),
            }
        except Exception as e:
            logger.error(f"Error getting price change for {symbol}: {e}")
            return None

    def get_hot_list_data(self, symbol):
        """Get hot list data (insider, ratings, sector, etc.)."""
        routed = self._stock_only_symbol(symbol)
        if routed is None:
            return None
        try:
            ticker = yf.Ticker(routed)
            info = ticker.info
            recs = ticker.recommendations
            time.sleep(0.2)
            result = {
                'sector': info.get('sector', ''),
                'industry': info.get('industry', ''),
                'ratings': [],
                'insiderTrading': [],
                'adx': None,
            }
            if recs is not None and hasattr(recs, 'to_dict'):
                try:
                    recent = recs.tail(5)
                    for _, row in recent.iterrows():
                        result['ratings'].append({
                            'firm': row.get('Firm', ''),
                            'grade': row.get('To Grade', ''),
                            'action': row.get('Action', ''),
                        })
                except Exception:
                    pass
            return result
        except Exception as e:
            logger.error(f"Error getting hot list data for {symbol}: {e}")
            return None

    def get_hybrid_macd(self, symbol, timeframe="4hour"):
        """Get MACD data computed from OHLCV."""
        try:
            chart_data = self.get_historical_chart(symbol, timeframe=timeframe, limit=100)
            if not chart_data:
                return None
            import pandas as pd
            df = pd.DataFrame(chart_data)
            if 'close' in df.columns:
                close = df['close']
                ema_12 = close.ewm(span=12).mean()
                ema_26 = close.ewm(span=26).mean()
                macd = ema_12 - ema_26
                signal = macd.ewm(span=9).mean()
                hist = macd - signal
                return {
                    'macd': float(macd.iloc[-1]) if len(macd) > 0 else 0,
                    'signal': float(signal.iloc[-1]) if len(signal) > 0 else 0,
                    'histogram': float(hist.iloc[-1]) if len(hist) > 0 else 0,
                }
            return None
        except Exception as e:
            logger.error(f"Error getting hybrid MACD for {symbol}: {e}")
            return None

    def get_historical_eod_light(self, symbol):
        """Get light EOD historical data."""
        routed = _requestable_yahoo_symbol(symbol)
        if routed is None:
            return None
        try:
            ticker = yf.Ticker(routed)
            df = ticker.history(period="1y", interval="1d")
            time.sleep(0.2)
            if df is None or df.empty:
                mark_missing(routed, "empty_history_1y")
            else:
                clear_missing(routed)
            return self._strip_tz(df)
        except Exception as e:
            logger.error(f"Error getting EOD light for {symbol}: {e}")
            return None

    def get_historical_eod_full(self, symbol):
        """Get full EOD historical data."""
        routed = _requestable_yahoo_symbol(symbol)
        if routed is None:
            return None
        try:
            ticker = yf.Ticker(routed)
            df = ticker.history(period="max", interval="1d")
            time.sleep(0.2)
            if df is None or df.empty:
                mark_missing(routed, "empty_history_max")
            else:
                clear_missing(routed)
            return self._strip_tz(df)
        except Exception as e:
            logger.error(f"Error getting EOD full for {symbol}: {e}")
            return None

    def get_financial_growth(self, symbol):
        """Get financial growth metrics."""
        routed = self._stock_only_symbol(symbol)
        if routed is None:
            return None
        try:
            ticker = yf.Ticker(routed)
            info = ticker.info
            time.sleep(0.2)
            return {
                'revenueGrowth': info.get('revenueGrowth', 0),
                'earningsGrowth': info.get('earningsGrowth', 0),
                'earningsQuarterlyGrowth': info.get('earningsQuarterlyGrowth', 0),
            }
        except Exception as e:
            logger.error(f"Error getting financial growth for {symbol}: {e}")
            return None

    def get_owner_earnings(self, symbol):
        """Get owner earnings (computed from cash flow)."""
        return None  # Not directly available from yfinance

    def get_cash_flow_statement_growth(self, symbol):
        """Get cash flow growth."""
        return self.get_financial_growth(symbol)

    def get_balance_sheet_growth(self, symbol):
        """Get balance sheet growth."""
        return self.get_financial_growth(symbol)

    def get_balance_sheet_as_reported(self, symbol):
        """Get as-reported balance sheet."""
        return self.get_balance_sheet(symbol)

    def get_cash_flow_statement_as_reported(self, symbol):
        """Get as-reported cash flow."""
        return self.get_cash_flow(symbol)

    def get_latest_financial_statements(self, symbol):
        """Get latest financial statements."""
        routed = self._stock_only_symbol(symbol)
        if routed is None:
            return None
        try:
            ticker = yf.Ticker(routed)
            time.sleep(0.2)
            result = {
                'income_statement': self._strip_tz(ticker.income_stmt),
                'balance_sheet': self._strip_tz(ticker.balance_sheet),
                'cash_flow': self._strip_tz(ticker.cashflow),
            }
            get_health_dashboard().register(
                "yf_fundamentals",
                QualifiedValue.verified({"symbol": str(symbol), "statements": 3}, "yfinance_statements"),
            )
            return result
        except Exception as e:
            logger.error(f"Error getting financial statements for {symbol}: {e}")
            get_health_dashboard().register("yf_fundamentals", QualifiedValue.unavailable("yfinance_statements", str(e)))
            return None

    def get_latest_cash_flow_statement(self, symbol) -> Optional[Dict[str, Any]]:
        """Most recent cash-flow statement as a row dict (audit 2026-09-03:
        ticker_analyzer called this method but it never existed)."""
        records = self.get_cash_flow(symbol)
        if isinstance(records, list) and records:
            return records[0]
        return None

    def get_latest_balance_sheet_statement(self, symbol) -> Optional[Dict[str, Any]]:
        """Most recent balance-sheet statement as a row dict (same audit)."""
        records = self.get_balance_sheet(symbol)
        if isinstance(records, list) and records:
            return records[0]
        return None

    def get_financial_reports_json(self, symbol):
        """Get financial reports as JSON."""
        return self.get_latest_financial_statements(symbol)

    # ---- Additional methods used by agents (pre_earnings, scanner, etc.) ----

    @staticmethod
    def save_cache(min_interval=60.0):
        """No-op: yfinance client doesn't have a cache to save.
        Kept for backward compatibility with old FMPClient calls."""
        pass

    _quote_info_cache: Dict[str, Any] = {}

    @staticmethod
    def get_quote(symbol, ttl=None):
        """Get a single stock quote. Uses fast Yahoo chart API, enriched with
        the .info fundamentals the Fundamentals tab renders (marketCap/pe/eps/
        52w range). The .info lookup is TTL-cached 30 min per symbol."""
        import time as _time

        result = StockDataClient._fetch_yahoo_chart_quote(symbol)
        if not result or result.get("price", 0) <= 0:
            return None
        sym = str(symbol or "").upper().strip()
        cached = StockDataClient._quote_info_cache.get(sym)
        now = _time.time()
        if cached and now - cached[1] < 1800:
            result.update(cached[0])
            return result
        info_fields: Dict[str, Any] = {}
        try:
            info_symbol = StockDataClient._stock_only_symbol(symbol)
            # Fundamentals exist for equities only; a crypto quote keeps its
            # price fields without a pointless .info round trip.
            info = (yf.Ticker(info_symbol).info or {}) if info_symbol else {}
            info_fields = {
                "marketCap": info.get("marketCap"),
                "pe": info.get("trailingPE"),
                "forwardPe": info.get("forwardPE"),
                "eps": info.get("trailingEps"),
                "yearLow": info.get("fiftyTwoWeekLow"),
                "yearHigh": info.get("fiftyTwoWeekHigh"),
                "averageVolume": info.get("averageVolume"),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "dividendYield": info.get("dividendYield"),
                "beta": info.get("beta"),
            }
            info_fields = {k: v for k, v in info_fields.items() if v is not None}
        except Exception as e:
            logger.debug(f"quote info enrichment unavailable for {symbol}: {e}")
        StockDataClient._quote_info_cache[sym] = (info_fields, now)
        result.update(info_fields)
        return result

    @staticmethod
    def get_estimates(symbol):
        """Get analyst estimates (EPS, revenue)."""
        routed = StockDataClient._stock_only_symbol(symbol)
        if routed is None:
            return None
        try:
            ticker = yf.Ticker(routed)
            info = ticker.info
            time.sleep(0.2)
            result = {
                'estimatedEPS': info.get('forwardEps', None),
                'estimatedRevenue': None,  # Not directly available from yfinance
                'numberAnalysts': info.get('numberOfAnalystOpinions', None),
                'analystCount': info.get('numberOfAnalystOpinions', None),
                'numberOfAnalysts': info.get('numberOfAnalystOpinions', None),
                'targetMeanPrice': info.get('targetMeanPrice', None),
                'targetHighPrice': info.get('targetHighPrice', None),
                'targetLowPrice': info.get('targetLowPrice', None),
                # Real analyst consensus (same info payload — no extra request)
                'recommendationKey': info.get('recommendationKey', None),
                'recommendationMean': info.get('recommendationMean', None),
                'currentPrice': info.get('currentPrice') or info.get('regularMarketPrice') or None,
                # Market cap from the same info payload — no extra request.
                # Populates the Pre-Earnings market-cap filter (Faza 3, 2026-07-07).
                'marketCap': info.get('marketCap', None),
                # Sector/industry from the same info payload — no extra request.
                # Activates Pre-Earnings sector filter + sector×cap calibration
                # (Task PE-premium C2).
                'sector': info.get('sector', None),
                'industry': info.get('industry', None),
            }
            get_health_dashboard().register(
                "yf_estimates", QualifiedValue.verified({"symbol": str(symbol)}, "yfinance_info")
            )
            return result
        except Exception as e:
            logger.error(f"Error getting estimates for {symbol}: {e}")
            get_health_dashboard().register("yf_estimates", QualifiedValue.unavailable("yfinance_info", str(e)))
            return None

    @staticmethod
    def get_market_cap(symbol) -> Optional[float]:
        """Market cap in USD, TTL-cached (6h). Primary: yfinance .info
        (same payload get_estimates already fetches). Fallback: FinnHub
        /stock/profile2 marketCapitalization (reported in millions -> *1e6).
        Returns None if neither source has it. Populates the Pre-Earnings
        market-cap filter (Faza 3, 2026-07-07).
        """
        sym = str(symbol or "").upper().strip()
        if not sym:
            return None
        now = time.time()
        with _MARKET_CAP_LOCK:
            hit = _MARKET_CAP_CACHE.get(sym)
            if hit and (now - hit[0]) < _MARKET_CAP_TTL_SECONDS:
                return hit[1]
        # Try yfinance .info marketCap
        cap = None
        routed = _requestable_yahoo_symbol(sym)
        if routed is None or is_exchange_crypto_pair(sym):
            # Unknown / unroutable symbol or an exchange pair: no provider
            # here has a market cap for it. Remember the miss for the TTL.
            with _MARKET_CAP_LOCK:
                _MARKET_CAP_CACHE[sym] = (now, None)
            return None
        try:
            info = yf.Ticker(routed).info or {}
            cap = _to_float(info.get("marketCap"))
        except Exception as e:
            logger.debug(f"get_market_cap yfinance failed for {sym}: {e}")
        # Fallback: FinnHub profile2 (marketCapitalization is in millions)
        if cap is None:
            try:
                from core.finnhub_client import get_finnhub_client
                fh = get_finnhub_client()
                if fh.is_available():
                    prof = fh.get_profile2(sym)
                    if isinstance(prof, dict):
                        mc = _to_float(prof.get("marketCapitalization"))
                        if mc is not None:
                            cap = mc * 1_000_000.0
            except Exception as e:
                logger.debug(f"get_market_cap finnhub fallback failed for {sym}: {e}")
        # Fallback 2: eToro search_instrument
        if cap is None:
            try:
                from core.etoro_client import get_etoro_client
                et = get_etoro_client()
                if et.is_available():
                    meta = et.search_instrument(sym)
                    if meta and meta.get("market_cap"):
                        cap = _to_float(meta["market_cap"])
            except Exception as e:
                logger.debug(f"get_market_cap eToro fallback failed for {sym}: {e}")
        # Cache only non-None values; cache None too but short-lived? Cache None
        # to avoid re-hitting rate limits on tickers with no cap data — 6h TTL.
        with _MARKET_CAP_LOCK:
            _MARKET_CAP_CACHE[sym] = (now, cap)
        return cap

    @staticmethod
    def get_insider_trading(symbol, ttl=None):
        """Get insider trading data."""
        routed = StockDataClient._stock_only_symbol(symbol)
        if routed is None:
            return None
        try:
            ticker = yf.Ticker(routed)
            purchases = ticker.insider_purchases
            transactions = ticker.insider_transactions
            time.sleep(0.2)
            result = []
            for df in [purchases, transactions]:
                if df is not None and hasattr(df, 'to_dict'):
                    try:
                        records = df.to_dict('records')
                        result.extend(records[:10])
                    except Exception:
                        pass
            return result if result else None
        except Exception as e:
            logger.error(f"Error getting insider trading for {symbol}: {e}")
            return None

    @staticmethod
    def get_income_statement_growth(symbol, limit=4, ttl=None):
        """Get income statement growth metrics."""
        routed = StockDataClient._stock_only_symbol(symbol)
        if routed is None:
            return None
        try:
            ticker = yf.Ticker(routed)
            info = ticker.info
            time.sleep(0.2)
            return {
                'revenueGrowth': info.get('revenueGrowth', None),
                'earningsGrowth': info.get('earningsGrowth', None),
                'earningsQuarterlyGrowth': info.get('earningsQuarterlyGrowth', None),
            }
        except Exception as e:
            logger.error(f"Error getting income growth for {symbol}: {e}")
            return None

    @staticmethod
    def get_requests_per_minute():
        """No-op: yfinance doesn't track RPM. Kept for backward compat."""
        return 0


# Module-level singleton
stock_data_client = StockDataClient()

# Backward compatibility aliases removed as part of FMP elimination.
