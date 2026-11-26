"""Cache-first Alpaca market-data adapter.

Credentials are resolved only from environment variables and are sent as
headers, so secrets never enter URLs, persisted config, or logs.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Iterable, Optional

from core.http_utils import http_get

logger = logging.getLogger("AlpacaClient")


class AlpacaClient:
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._explicit_config = config
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def set_config(self, config: Optional[Dict[str, Any]]) -> None:
        with self._lock:
            self._explicit_config = config
            self._cache.clear()

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def _settings(self) -> Dict[str, Any]:
        if isinstance(self._explicit_config, dict):
            root = self._explicit_config
        else:
            try:
                from core.config import ConfigManager

                root = ConfigManager().data_sources
            except Exception as exc:
                logger.debug("Could not load Alpaca settings: %s", exc)
                root = {}
        settings = (root.get("alpaca") or {}) if isinstance(root, dict) else {}
        return settings if isinstance(settings, dict) else {}

    @staticmethod
    def _credentials() -> tuple[str, str]:
        key = (
            os.getenv("ALPACA_API_KEY", "")
            or os.getenv("APCA_API_KEY_ID", "")
        ).strip()
        secret = (
            os.getenv("ALPACA_SECRET_KEY", "")
            or os.getenv("APCA_API_SECRET_KEY", "")
        ).strip()
        return key, secret

    def is_available(self) -> bool:
        key, secret = self._credentials()
        return bool(self._settings().get("enabled", True) and key and secret)

    def status(self) -> Dict[str, Any]:
        settings = self._settings()
        key, secret = self._credentials()
        return {
            "enabled": bool(settings.get("enabled", True)),
            "configured": bool(key and secret),
            "available": self.is_available(),
            "stock_feed": str(settings.get("stock_feed") or "iex"),
            "crypto_feed": str(settings.get("crypto_feed") or "us"),
            "currency": str(settings.get("currency") or "USD"),
            "prefer_for_stocks": bool(settings.get("prefer_for_stocks", True)),
            "crypto_fallback": bool(settings.get("crypto_fallback", True)),
        }

    def _headers(self) -> Dict[str, str]:
        key, secret = self._credentials()
        return {
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json",
        }

    @staticmethod
    def _stock_symbol(ticker: str) -> str:
        return str(ticker or "").upper().strip()

    @staticmethod
    def _crypto_symbol(ticker: str) -> str:
        value = str(ticker or "").upper().strip().replace("_", "/")
        if "/" in value:
            return value
        if "-" in value:
            base, quote = value.split("-", 1)
            return f"{base}/{quote}"
        for quote in ("USDT", "USDC", "USD"):
            if value.endswith(quote) and len(value) > len(quote):
                normalized_quote = "USD" if quote in {"USDT", "USDC"} else quote
                return f"{value[:-len(quote)]}/{normalized_quote}"
        return value

    def _cached_get(
        self,
        cache_key: str,
        url: str,
        params: Dict[str, Any],
        ttl: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        settings = self._settings()
        try:
            ttl_seconds = max(1, int(ttl or settings.get("snapshot_ttl_seconds", 5) or 5))
        except (TypeError, ValueError):
            ttl_seconds = 5
        now = time.time()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and now - float(cached.get("ts", 0.0)) < ttl_seconds:
                data = cached.get("data")
                return dict(data) if isinstance(data, dict) else None

        if not self.is_available():
            return None
        response = http_get(
            url,
            provider="alpaca",
            params=params,
            headers=self._headers(),
            timeout=(2, 7),
            max_retries=1,
        )
        if response is None or response.status_code != 200:
            if response is not None:
                logger.warning("Alpaca request failed: HTTP %s", response.status_code)
            return dict(cached["data"]) if cached and isinstance(cached.get("data"), dict) else None
        try:
            payload = response.json() or {}
        except Exception as exc:
            logger.warning("Alpaca response decode failed: %s", exc)
            return dict(cached["data"]) if cached and isinstance(cached.get("data"), dict) else None
        if not isinstance(payload, dict):
            return None
        with self._lock:
            self._cache[cache_key] = {"ts": now, "data": payload}
        return dict(payload)

    def get_stock_snapshots(self, symbols: Iterable[str], ttl: Optional[int] = None) -> Dict[str, Any]:
        normalized = list(dict.fromkeys(self._stock_symbol(s) for s in symbols if self._stock_symbol(s)))
        if not normalized:
            return {}
        settings = self._settings()
        base = str(settings.get("data_base_url") or "https://data.alpaca.markets").rstrip("/")
        feed = str(settings.get("stock_feed") or "iex").lower()
        currency = str(settings.get("currency") or "USD").upper()
        payload = self._cached_get(
            f"stock:{feed}:{currency}:{','.join(normalized)}",
            f"{base}/v2/stocks/snapshots",
            {"symbols": ",".join(normalized), "feed": feed, "currency": currency},
            ttl=ttl,
        )
        return payload or {}

    def get_crypto_snapshots(self, symbols: Iterable[str], ttl: Optional[int] = None) -> Dict[str, Any]:
        normalized = list(dict.fromkeys(self._crypto_symbol(s) for s in symbols if self._crypto_symbol(s)))
        if not normalized:
            return {}
        settings = self._settings()
        base = str(settings.get("data_base_url") or "https://data.alpaca.markets").rstrip("/")
        feed = str(settings.get("crypto_feed") or "us").lower()
        payload = self._cached_get(
            f"crypto:{feed}:{','.join(normalized)}",
            f"{base}/v1beta3/crypto/{feed}/snapshots",
            {"symbols": ",".join(normalized)},
            ttl=ttl,
        )
        return payload or {}

    def get_clock(self, ttl: int = 15) -> Optional[Dict[str, Any]]:
        settings = self._settings()
        base = str(settings.get("trading_base_url") or "https://paper-api.alpaca.markets").rstrip("/")
        return self._cached_get("clock", f"{base}/v2/clock", {}, ttl=ttl)

    @staticmethod
    def _snapshot_row(payload: Dict[str, Any], symbol: str) -> Dict[str, Any]:
        snapshots = payload.get("snapshots") if isinstance(payload, dict) else {}
        if not isinstance(snapshots, dict):
            snapshots = payload
        row = snapshots.get(symbol) if isinstance(snapshots, dict) else None
        return row if isinstance(row, dict) else {}

    @staticmethod
    def _number(mapping: Any, *keys: str) -> float:
        if not isinstance(mapping, dict):
            return 0.0
        for key in keys:
            try:
                value = float(mapping.get(key) or 0.0)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                continue
        return 0.0

    def _market_data_from_payload(
        self,
        ticker: str,
        symbol: str,
        payload: Dict[str, Any],
        *,
        crypto: bool,
    ) -> Optional[Dict[str, Any]]:
        row = self._snapshot_row(payload, symbol)
        if not row:
            return None
        latest_trade = row.get("latestTrade") or row.get("latest_trade") or {}
        latest_quote = row.get("latestQuote") or row.get("latest_quote") or {}
        minute_bar = row.get("minuteBar") or row.get("minute_bar") or {}
        daily_bar = row.get("dailyBar") or row.get("daily_bar") or {}
        previous_bar = row.get("prevDailyBar") or row.get("previous_daily_bar") or {}
        price = self._number(latest_trade, "p", "price")
        if price <= 0:
            price = self._number(minute_bar, "c", "close")
        if price <= 0:
            price = self._number(daily_bar, "c", "close")
        previous_close = self._number(previous_bar, "c", "close")
        if price <= 0:
            return None
        change_pct = ((price - previous_close) / previous_close * 100.0) if previous_close > 0 else 0.0
        return {
            "ticker": str(ticker or "").upper().strip(),
            "alpaca_symbol": symbol,
            "price": float(price),
            "change_pct": round(float(change_pct), 4),
            "previous_close": previous_close or None,
            "bid": self._number(latest_quote, "bp", "bid_price") or None,
            "ask": self._number(latest_quote, "ap", "ask_price") or None,
            "timestamp": (
                latest_trade.get("t")
                or latest_trade.get("timestamp")
                or minute_bar.get("t")
                or minute_bar.get("timestamp")
            ),
            "source": "alpaca_crypto" if crypto else "alpaca_iex",
            "feed": self.status()["crypto_feed" if crypto else "stock_feed"],
        }

    def get_market_data(self, ticker: str, *, crypto: bool = False, ttl: Optional[int] = None) -> Optional[Dict[str, Any]]:
        symbol = self._crypto_symbol(ticker) if crypto else self._stock_symbol(ticker)
        payload = (
            self.get_crypto_snapshots([symbol], ttl=ttl)
            if crypto
            else self.get_stock_snapshots([symbol], ttl=ttl)
        )
        return self._market_data_from_payload(ticker, symbol, payload, crypto=crypto)

    def get_market_data_batch(
        self,
        tickers: Iterable[str],
        *,
        crypto: bool = False,
        ttl: Optional[int] = None,
    ) -> Dict[str, Dict[str, Any]]:
        normalized_tickers = list(
            dict.fromkeys(str(t or "").upper().strip() for t in tickers if str(t or "").strip())
        )
        symbols = [
            self._crypto_symbol(ticker) if crypto else self._stock_symbol(ticker)
            for ticker in normalized_tickers
        ]
        payload = (
            self.get_crypto_snapshots(symbols, ttl=ttl)
            if crypto
            else self.get_stock_snapshots(symbols, ttl=ttl)
        )
        result: Dict[str, Dict[str, Any]] = {}
        for ticker, symbol in zip(normalized_tickers, symbols):
            row = self._market_data_from_payload(ticker, symbol, payload, crypto=crypto)
            if row:
                result[ticker] = row
        return result


_client: Optional[AlpacaClient] = None
_client_lock = threading.Lock()


def get_alpaca_client() -> AlpacaClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = AlpacaClient()
    return _client
