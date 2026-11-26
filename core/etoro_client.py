"""etoro_client.py — thin shared adapter for the eToro Public API.

Pattern mirrors finnhub_client.py: this is a URL / auth / cache adapter.
Provides:
  - Live rates (bid, ask, last, spread)
  - Historical OHLCV candles (OneMinute, FiveMinutes, OneHour, OneDay, etc.)
  - Symbol lookup & rich metadata (P/E, ESG, retail crowd positioning & popularity)
  - Social feeds (community posts & sentiment)
  - User watchlists

Key resolution order:
  1. config["etoro"]["private_key"] / config["etoro"]["public_key"]
  2. env ETORO_PRIVATE_KEY / env ETORO_PUBLIC_KEY
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

import requests

logger = logging.getLogger("EToroClient")

_BASE = "https://public-api.etoro.com/api/v1"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_TTL_RATES = 30
_TTL_SEARCH = 300
_TTL_FEED = 600
_TTL_WATCHLIST = 300
_TTL_CANDLES = 60

_INTERVAL_MAP = {
    "1m": "OneMinute", "1min": "OneMinute", "minute": "OneMinute", "oneminute": "OneMinute",
    "5m": "FiveMinutes", "5min": "FiveMinutes", "fiveminutes": "FiveMinutes",
    "10m": "TenMinutes", "10min": "TenMinutes", "tenminutes": "TenMinutes",
    "15m": "FifteenMinutes", "15min": "FifteenMinutes", "fifteenminutes": "FifteenMinutes",
    "30m": "ThirtyMinutes", "30min": "ThirtyMinutes", "thirtyminutes": "ThirtyMinutes",
    "1h": "OneHour", "1hour": "OneHour", "60m": "OneHour", "onehour": "OneHour",
    "4h": "FourHours", "4hour": "FourHours", "fourhours": "FourHours",
    "1d": "OneDay", "1day": "OneDay", "day": "OneDay", "d": "OneDay", "oneday": "OneDay",
    "1w": "OneWeek", "1week": "OneWeek", "week": "OneWeek", "w": "OneWeek", "oneweek": "OneWeek",
}


def _safe_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f else None
    except Exception:
        return None


def _symbol_key(value: Any) -> str:
    """Comparison key for instrument symbols (BRK-B == BRK.B == brk.b)."""
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


# eToro suffixes that name the SAME US listing (PSN.US, BSX.RTH). Any other
# suffix is a different listing on another exchange, in another currency
# (BBVA.MC, TEL.OL, SF.ST): never the requested US ticker.
_SAME_LISTING_SUFFIXES = (".US", ".RTH")


def symbols_match(requested: Any, instrument_symbol: Any) -> bool:
    """True only when an eToro instrument really is the requested symbol.

    Accepting "the first search hit" silently mapped an unknown ticker onto an
    unrelated instrument (NVD -> NVDA, TEL -> Telenor's TEL.OL, crypto SHELL ->
    Shell plc SHELL.NV), whose rates were then shown (and traded on paper) as
    the requested ticker's price.
    """
    wanted = _symbol_key(requested)
    if not wanted:
        return False
    candidate = str(instrument_symbol or "").upper().strip()
    if wanted == _symbol_key(candidate):
        return True
    for suffix in _SAME_LISTING_SUFFIXES:
        if candidate.endswith(suffix) and wanted == _symbol_key(candidate[: -len(suffix)]):
            return True
    # Small-unit crypto is quoted per million / per thousand units (PEPExM,
    # SHIBxM): the same asset, scaled by ``unit_multiplier``.
    for unit_suffix in ("XM", "XK"):
        if candidate.endswith(unit_suffix) and wanted == _symbol_key(candidate[: -len(unit_suffix)]):
            return True
    return False


# A last execution that sits outside the current bid/ask band is, by
# definition, older than the book: the market has moved since that trade.
_LAST_EXECUTION_BAND_TOLERANCE = 0.01


def resolve_rate_price(rate: Optional[Dict[str, Any]]) -> tuple:
    """Return ``(price, basis)`` for one eToro rate row.

    ``lastExecution`` is the last trade, which can be hours or days old for a
    thin instrument or a closed market. When a two-sided book exists and the
    last execution is outside it, the mid of the live book is the honest
    current price; otherwise the last execution is kept.
    """
    row = rate if isinstance(rate, dict) else {}
    bid = _safe_float(row.get("bid"))
    ask = _safe_float(row.get("ask"))
    last = _safe_float(row.get("last"))
    has_book = bool(bid and ask and bid > 0 and ask > 0 and ask >= bid)
    if last and last > 0:
        if has_book:
            low = bid * (1.0 - _LAST_EXECUTION_BAND_TOLERANCE)
            high = ask * (1.0 + _LAST_EXECUTION_BAND_TOLERANCE)
            if not (low <= last <= high):
                return round((bid + ask) / 2.0, 6), "mid_last_execution_stale"
        return last, "last_execution"
    if has_book:
        return round((bid + ask) / 2.0, 6), "mid"
    if bid and bid > 0:
        return bid, "bid"
    if ask and ask > 0:
        return ask, "ask"
    return None, "unavailable"


def rate_age_seconds(rate: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> Optional[float]:
    """Age of a rate row from eToro's own quote time; None when unknown."""
    raw = str((rate or {}).get("timestamp") or "").strip()
    if not raw:
        return None
    try:
        text = raw.replace("Z", "+00:00")
        # eToro emits 7 fractional digits; fromisoformat accepts at most 6.
        if "." in text:
            head, _, tail = text.partition(".")
            digits = ""
            for ch in tail:
                if not ch.isdigit():
                    break
                digits += ch
            zone = tail[len(digits):]
            text = f"{head}.{digits[:6]}{zone}"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return max(0.0, (current - parsed.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return None


class EToroClient:
    """Thin eToro Public API adapter. Singleton via get_etoro_client()."""

    def __init__(self, config: Optional[Dict] = None):
        self._explicit_config = config
        self._lock = threading.RLock()
        self._rates_cache: Dict[str, tuple] = {}
        self._search_cache: Dict[str, tuple] = {}
        self._feed_cache: Dict[str, tuple] = {}
        self._candles_cache: Dict[str, tuple] = {}
        self._watchlists_cache: Optional[tuple] = None
        self._public_key: Optional[str] = None
        self._private_key: Optional[str] = None
        self._keys_resolved = False
        self._session: Optional[requests.Session] = None
        from core.config import data_path  # configured data folder (MS_DATA_DIR)
        self._map_file = Path(data_path("etoro_instruments_map.json"))
        self._load_disk_map()

    def _load_disk_map(self) -> None:
        dropped: List[str] = []
        try:
            if self._map_file.exists():
                data = json.loads(self._map_file.read_text(encoding="utf-8"))
                now = time.monotonic()
                for sym, item in (data or {}).items():
                    if isinstance(item, dict):
                        clean_key = str(sym or "").upper().strip().lstrip("$")
                        if clean_key.endswith(("-USD", "-USDT", "-USDC")):
                            clean_key = clean_key.rsplit("-", 1)[0]
                        if item.get("symbol") and not symbols_match(clean_key, item.get("symbol")):
                            # Legacy fuzzy match persisted a different
                            # instrument under this ticker: drop it so the
                            # symbol is re-resolved with exact matching.
                            dropped.append(f"{sym}->{item.get('symbol')}")
                            continue
                        full_sym = str(item.get("symbol") or sym or "").upper()
                        disp_name = str(item.get("name") or "").lower()
                        unit_mult = 1.0
                        if full_sym.endswith("XM") or "(in millions)" in disp_name:
                            unit_mult = 1_000_000.0
                        elif full_sym.endswith("XK") or "(in thousands)" in disp_name:
                            unit_mult = 1_000.0
                        item["unit_multiplier"] = unit_mult
                    # 30-day memory cache for resolved instrument IDs
                    self._search_cache[str(sym).upper().strip()] = (now + 86400 * 30, item)
        except Exception as e:
            logger.debug("Failed to load etoro instruments map: %s", e)
        if dropped:
            logger.warning(
                "Ignored %d eToro instrument mapping(s) that point at a different instrument (%s%s)",
                len(dropped),
                ", ".join(dropped[:8]),
                ", ..." if len(dropped) > 8 else "",
            )

    def _save_disk_map(self) -> None:
        try:
            self._map_file.parent.mkdir(parents=True, exist_ok=True)
            export = {}
            for sym, val in self._search_cache.items():
                if isinstance(val, tuple) and len(val) == 2:
                    export[sym] = val[1]
            self._map_file.write_text(json.dumps(export, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug("Failed to save etoro instruments map: %s", e)

    def set_config(self, config: Optional[Dict]) -> None:
        with self._lock:
            self._explicit_config = config
            self._public_key = None
            self._private_key = None
            self._keys_resolved = False
            self._session = None

    def _resolve_keys(self) -> tuple[str, str]:
        if self._keys_resolved:
            return self._public_key or "", self._private_key or ""
        cfg: Dict[str, Any] = {}
        if isinstance(self._explicit_config, dict):
            cfg = self._explicit_config
        else:
            try:
                from core.config import ConfigManager
                cfg = ConfigManager().config or {}
            except Exception as exc:
                logger.debug("ConfigManager unavailable for etoro keys: %s", exc)
        etoro_cfg = cfg.get("etoro") or (cfg.get("data_sources") or {}).get("etoro") or {}
        pub = (etoro_cfg.get("public_key", "") or os.getenv("ETORO_PUBLIC_KEY", "")).strip()
        priv = (etoro_cfg.get("private_key", "") or os.getenv("ETORO_PRIVATE_KEY", "")).strip()
        with self._lock:
            self._public_key = pub
            self._private_key = priv
            self._keys_resolved = True
        if pub and priv:
            logger.debug("eToro API keys resolved")
        return pub, priv

    def is_available(self) -> bool:
        pub, priv = self._resolve_keys()
        return bool(pub and priv)

    def _cfg_value(self, key: str, default: Any) -> Any:
        cfg: Dict[str, Any] = {}
        if isinstance(self._explicit_config, dict):
            cfg = self._explicit_config
        else:
            try:
                from core.config import ConfigManager
                cfg = ConfigManager().config or {}
            except Exception:
                pass
        etoro_cfg = cfg.get("etoro") or (cfg.get("data_sources") or {}).get("etoro") or {}
        return etoro_cfg.get(key, default)

    def _get_session(self) -> requests.Session:
        pub, priv = self._resolve_keys()
        with self._lock:
            if self._session is None:
                s = requests.Session()
                s.headers.update({
                    "x-api-key": pub,
                    "x-user-key": priv,
                    "Accept": "application/json",
                    # Pin the content locale: eToro translates feed posts to the
                    # caller's language, which served Spanish/French/German/CJK
                    # posts into the (English-only) news pool.
                    "Accept-Language": "en-US,en;q=0.9",
                    "User-Agent": _USER_AGENT,
                })
                self._session = s
        return self._session

    def _get(self, path: str, params: Optional[Dict] = None, timeout: int = 10) -> Optional[Any]:
        if not self.is_available():
            return None
        session = self._get_session()
        try:
            session.headers["x-request-id"] = str(uuid.uuid4())
            r = session.get(f"{_BASE}{path}", params=params, timeout=timeout)
            if not r.content:
                return {}
            if "json" in r.headers.get("content-type", ""):
                data = r.json()
            else:
                return None
            if r.status_code == 200:
                return data
            err_code = data.get("errorCode", "") if isinstance(data, dict) else ""
            logger.debug("eToro %s -> HTTP %s (%s)", path, r.status_code, err_code)
            return None
        except Exception as exc:
            logger.warning("eToro request %s failed: %s", path, exc)
            with self._lock:
                self._session = None
            return None

    # ---- Live rates ----

    def get_live_rates(
        self,
        instrument_ids: List[int],
        *,
        timeout: float = 10,
    ) -> Dict[int, Dict[str, Any]]:
        """Fetch live bid/ask/last for eToro instrument IDs.
        Returns {instrumentID -> {bid, ask, last, spread, timestamp}}.

        ``timeout`` lets latency-sensitive read paths (UI state builds) bound
        the wait instead of blocking a request for the default 10 s.
        """
        if not instrument_ids:
            return {}
        ttl = int(self._cfg_value("rates_ttl_seconds", _TTL_RATES))
        cache_key = ",".join(str(i) for i in sorted(instrument_ids))
        with self._lock:
            cached = self._rates_cache.get(cache_key)
            if cached:
                ts, data = cached
                if time.monotonic() - ts < ttl:
                    return data
        params = {"instrumentIds": ",".join(str(i) for i in instrument_ids)}
        raw = self._get("/market-data/instruments/rates", params=params, timeout=timeout)
        if not isinstance(raw, dict):
            return {}
        result: Dict[int, Dict[str, Any]] = {}
        for r in raw.get("rates") or []:
            iid = r.get("instrumentID")
            if iid is None:
                continue
            bid = _safe_float(r.get("bid"))
            ask = _safe_float(r.get("ask"))
            last = _safe_float(r.get("lastExecution"))
            spread = round(ask - bid, 6) if (bid is not None and ask is not None) else None
            result[int(iid)] = {
                "bid": bid,
                "ask": ask,
                "last": last,
                "spread": spread,
                "timestamp": r.get("date", ""),
            }
        with self._lock:
            self._rates_cache[cache_key] = (time.monotonic(), result)
        return result

    # ---- Instrument search & rich metadata ----

    def search_instrument(self, symbol: str, *, cache_only: bool = False) -> Optional[Dict[str, Any]]:
        """Search for an instrument by ticker symbol using eToro's indexed fields.
        Returns metadata dict or None if not found.

        ``cache_only=True`` answers from the resolved-instrument cache and never
        performs a network lookup (for UI read paths).
        """
        raw_sym = str(symbol or "").upper().strip()
        if not raw_sym:
            return None

        # Clean symbol: "BTC-USD" -> "BTC", "$AAPL" -> "AAPL"
        clean_sym = raw_sym.lstrip("$")
        if clean_sym.endswith(("-USD", "-USDT", "-USDC")):
            clean_sym = clean_sym.rsplit("-", 1)[0]

        ttl = int(self._cfg_value("search_ttl_seconds", _TTL_SEARCH))
        with self._lock:
            cached = self._search_cache.get(clean_sym)
            if cached:
                ts, data = cached
                if time.monotonic() - ts < ttl:
                    return data
        if cache_only:
            return None

        fields = (
            "instrumentId,internalSymbolFull,internalInstrumentDisplayName,"
            "popularityUniques,peRatio-TTM,arabesqueESGTotal,holdingPct,"
            "buyHoldingPct,sellHoldingPct,marketCapInUSD,nextEarningDate,"
            "internalAssetClassName,internalExchangeName"
        )
        params = {"internalSymbolFull": clean_sym, "fields": fields}
        raw = self._get("/market-data/search", params=params)

        best: Optional[Dict] = None
        if isinstance(raw, dict) and raw.get("items"):
            for item in raw.get("items", []):
                if str(item.get("internalSymbolFull", "")).upper() == clean_sym:
                    best = item
                    break
            if not best:
                # Exact match only (punctuation-insensitive, BRK-B == BRK.B).
                # The previous "take the first hit" fallback bound unknown
                # tickers to whatever instrument the search happened to rank
                # first.
                for item in raw.get("items", []):
                    if symbols_match(clean_sym, item.get("internalSymbolFull")):
                        best = item
                        break

        # Fallback: if not found, try searchTerm
        if not best:
            raw = self._get("/market-data/search", params={"searchTerm": clean_sym, "limit": 10})
            if isinstance(raw, dict):
                for item in raw.get("items", []):
                    sym_val = str(item.get("internalSymbolFull") or item.get("symbolName") or "").upper()
                    if sym_val == clean_sym and (item.get("instrumentId") or 0) > 0:
                        best = item
                        break

        if not best or not best.get("instrumentId") or int(best.get("instrumentId", 0)) <= 0:
            with self._lock:
                self._search_cache[clean_sym] = (time.monotonic() + 86400 * 7, None)
                if raw_sym != clean_sym:
                    self._search_cache[raw_sym] = (time.monotonic() + 86400 * 7, None)
                self._save_disk_map()
            return None

        iid = int(best["instrumentId"])
        pop_rank = best.get("popularityUniques")
        holding = _safe_float(best.get("holdingPct")) or 0.0
        buy_h = _safe_float(best.get("buyHoldingPct"))
        sell_h = _safe_float(best.get("sellHoldingPct"))

        full_sym = str(best.get("internalSymbolFull") or clean_sym).upper()
        disp_name = str(best.get("internalInstrumentDisplayName") or "").lower()
        unit_mult = 1.0
        if full_sym.endswith("XM") or "(in millions)" in disp_name:
            unit_mult = 1_000_000.0
        elif full_sym.endswith("XK") or "(in thousands)" in disp_name:
            unit_mult = 1_000.0

        result = {
            "instrument_id": iid,
            "symbol": best.get("internalSymbolFull") or clean_sym,
            "name": best.get("internalInstrumentDisplayName") or "",
            "asset_class": best.get("internalAssetClassName") or "",
            "exchange": best.get("internalExchangeName") or "",
            "pe_ratio": _safe_float(best.get("peRatio-TTM")),
            "esg_score": _safe_float(best.get("arabesqueESGTotal")),
            "popularity_rank": int(pop_rank) if pop_rank is not None else None,
            "holders": int(pop_rank) if pop_rank is not None else None,
            "holding_pct": holding,
            "buy_pct": buy_h,
            "sell_pct": sell_h if sell_h is not None else (round(100.0 - buy_h, 1) if buy_h is not None else None),
            "market_cap": _safe_float(best.get("marketCapInUSD")),
            "next_earning_date": best.get("nextEarningDate"),
            "unit_multiplier": unit_mult,
        }

        with self._lock:
            self._search_cache[clean_sym] = (time.monotonic() + 86400 * 30, result)
            if raw_sym != clean_sym:
                self._search_cache[raw_sym] = (time.monotonic() + 86400 * 30, result)
            self._save_disk_map()

        return result

    # ---- Historical OHLCV candles ----

    def get_candles(
        self,
        symbol_or_id: Union[str, int],
        interval: str = "OneDay",
        count: int = 100,
        direction: str = "asc",
    ) -> List[Dict[str, Any]]:
        """Fetch OHLCV candles for an instrument symbol or ID.
        direction: "asc" (oldest first) or "desc" (newest first).
        interval: e.g. "1m", "5m", "15m", "1h", "1d", or "OneDay", "FiveMinutes".
        Returns list of {date, open, high, low, close, volume}.
        """
        if isinstance(symbol_or_id, int):
            iid = symbol_or_id
        else:
            meta = self.search_instrument(str(symbol_or_id))
            if not meta or not meta.get("instrument_id"):
                return []
            iid = meta["instrument_id"]

        e_interval = _INTERVAL_MAP.get(interval.lower(), interval)
        dir_clean = "desc" if str(direction).lower() == "desc" else "asc"
        count_clean = max(1, min(1000, int(count)))

        ttl = int(self._cfg_value("candles_ttl_seconds", _TTL_CANDLES))
        cache_key = f"{iid}_{e_interval}_{count_clean}_{dir_clean}"
        with self._lock:
            cached = self._candles_cache.get(cache_key)
            if cached:
                ts, data = cached
                if time.monotonic() - ts < ttl:
                    return data

        url = f"/market-data/instruments/{iid}/history/candles/{dir_clean}/{e_interval}/{count_clean}"
        raw = self._get(url)
        if not isinstance(raw, dict):
            return []

        candles_wrap = (raw.get("candles") or [{}])[0]
        raw_candles = candles_wrap.get("candles") or []

        records: List[Dict[str, Any]] = []
        for c in raw_candles:
            records.append({
                "date": c.get("fromDate"),
                "open": _safe_float(c.get("open")),
                "high": _safe_float(c.get("high")),
                "low": _safe_float(c.get("low")),
                "close": _safe_float(c.get("close")),
                "volume": _safe_float(c.get("volume")) or 0.0,
            })

        with self._lock:
            self._candles_cache[cache_key] = (time.monotonic(), records)
        return records

    def get_candles_df(
        self,
        symbol_or_id: Union[str, int],
        interval: str = "5m",
        count: int = 100,
    ) -> Optional[Any]:
        """Return historical candles as a pandas DataFrame with datetime index and
        lowercase columns (open, high, low, close, volume) matching StockDataClient.
        """
        records = self.get_candles(symbol_or_id, interval=interval, count=count, direction="asc")
        if not records:
            return None
        try:
            import pandas as pd
            df = pd.DataFrame(records)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date")
            return df
        except Exception as exc:
            logger.warning("Error converting eToro candles to DataFrame: %s", exc)
            return None

    # ---- Fast quote ----

    def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch quote matching StockDataClient._fetch_yahoo_chart_quote return format:
        {symbol, price, changesPercentage, change, previousClose, exchange, volume, dayHigh, dayLow, bid, ask}
        """
        meta = self.search_instrument(symbol)
        if not meta or not meta.get("instrument_id"):
            return None
        iid = meta["instrument_id"]

        rates = self.get_live_rates([iid])
        rate_info = rates.get(iid) or {}
        bid = rate_info.get("bid")
        ask = rate_info.get("ask")
        price, _price_basis = resolve_rate_price(rate_info)

        candles = self.get_candles(iid, interval="OneDay", count=2, direction="desc")
        prev_close = None
        day_high = None
        day_low = None
        vol = 0
        if candles:
            curr_c = candles[0]
            day_high = curr_c.get("high")
            day_low = curr_c.get("low")
            vol = int(curr_c.get("volume") or 0)
            if price is None:
                price = curr_c.get("close")
            if len(candles) > 1:
                prev_close = candles[1].get("close")

        change = round(price - prev_close, 4) if (price and prev_close) else 0.0
        change_pct = round((change / prev_close) * 100.0, 4) if (prev_close and prev_close > 0) else 0.0

        unit_mult = float(meta.get("unit_multiplier") or 1.0)
        spread_val = rate_info.get("spread")
        if unit_mult > 1.0:
            if price is not None:
                price = price / unit_mult
            if bid is not None:
                bid = bid / unit_mult
            if ask is not None:
                ask = ask / unit_mult
            if spread_val is not None:
                spread_val = float(spread_val) / unit_mult
            if prev_close is not None:
                prev_close = prev_close / unit_mult
            if day_high is not None:
                day_high = day_high / unit_mult
            if day_low is not None:
                day_low = day_low / unit_mult

        return {
            "symbol": symbol.upper(),
            "price": price or 0.0,
            "bid": bid,
            "ask": ask,
            "spread": spread_val,
            "changesPercentage": change_pct,
            "change": change,
            "previousClose": prev_close,
            "dayHigh": day_high,
            "dayLow": day_low,
            "unit_multiplier": unit_mult,
            "volume": vol,
            "exchange": meta.get("exchange", ""),
            "source": "etoro",
        }

    # ---- Social feed ----

    # ---- Social feed & News ----

    def _parse_discussion_to_article(
        self, disc: Dict[str, Any], default_ticker: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Parse an eToro discussion into a standard news article dict."""
        if not isinstance(disc, dict):
            return None
        post = disc.get("post") or {}
        post_id = str(post.get("id") or "")
        if not post_id:
            return None
        owner = post.get("owner") or {}
        username = str(owner.get("username") or "eToro")
        msg_raw = post.get("message") or post.get("text") or ""
        if isinstance(msg_raw, dict):
            text = str(msg_raw.get("text") or msg_raw.get("content") or "").strip()
        else:
            text = str(msg_raw).strip()
        if not text or len(text) < 10:
            return None

        lines = [l.strip() for l in text.split("\n") if l.strip()]
        first_line = lines[0] if lines else text[:120]
        # Clean title
        title = first_line[:140] if len(first_line) > 140 else first_line
        if len(lines) > 1 and len(title) < 50:
            title = f"{title}: {lines[1][:80]}"

        tags = post.get("tags") or []
        symbols: List[str] = []
        for t in tags:
            if isinstance(t, dict):
                market = t.get("market") or {}
                sym = market.get("symbolName") or t.get("tag") or ""
                if sym and len(sym) <= 10 and sym.replace(".", "").isalnum():
                    symbols.append(sym.upper())
        if default_ticker and default_ticker.upper() not in symbols:
            symbols.insert(0, default_ticker.upper())

        created = post.get("created") or ""
        try:
            from dateutil import parser as dt_parser
            pub_dt = dt_parser.parse(created)
        except Exception:
            pub_dt = datetime.now(timezone.utc)

        emotions = disc.get("emotionsData") or {}
        likes = (emotions.get("like") or {}).get("paging", {}).get("totalCount", 0)
        comments = (disc.get("summary") or {}).get("totalCommentsAndReplies", 0)

        return {
            "id": f"etoro_{post_id}",
            "title": title,
            "summary": text,
            "url": f"https://www.etoro.com/posts/{post_id}",
            "source": f"eToro (@{username})",
            "source_provider": "etoro",
            "published_at": pub_dt,
            "symbols": symbols,
            "primary_ticker": symbols[0] if symbols else (default_ticker.upper() if default_ticker else None),
            "likes": likes,
            "comments": comments,
        }

    def get_feed_news(self, take: int = 30) -> List[Dict[str, Any]]:
        """Fetch curated news discussions from eToro."""
        ttl = int(self._cfg_value("feed_ttl_seconds", _TTL_FEED))
        cache_key = f"feed_news_{take}"
        with self._lock:
            cached = self._feed_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < ttl:
                return cached[1]
        raw = self._get(f"/feeds/news?take={take}")
        articles: List[Dict[str, Any]] = []
        for disc in (raw or {}).get("discussions") or []:
            art = self._parse_discussion_to_article(disc)
            if art:
                articles.append(art)
        with self._lock:
            self._feed_cache[cache_key] = (time.monotonic(), articles)
        return articles

    def get_for_you_feed(self, take: int = 20) -> List[Dict[str, Any]]:
        """Fetch personalized / popular social discussions from eToro."""
        ttl = int(self._cfg_value("feed_ttl_seconds", _TTL_FEED))
        cache_key = f"for_you_{take}"
        with self._lock:
            cached = self._feed_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < ttl:
                return cached[1]
        raw = self._get(f"/feeds/for-you?take={take}")
        articles: List[Dict[str, Any]] = []
        for disc in (raw or {}).get("discussions") or []:
            art = self._parse_discussion_to_article(disc)
            if art:
                articles.append(art)
        with self._lock:
            self._feed_cache[cache_key] = (time.monotonic(), articles)
        return articles

    def get_ticker_discussions(self, ticker: str, take: int = 15) -> List[Dict[str, Any]]:
        """Fetch instrument-specific discussions and sentiment posts."""
        sym = ticker.upper().strip()
        ttl = int(self._cfg_value("feed_ttl_seconds", _TTL_FEED))
        cache_key = f"ticker_disc_{sym}_{take}"
        with self._lock:
            cached = self._feed_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < ttl:
                return cached[1]

        articles: List[Dict[str, Any]] = []
        iid = None
        # Try finding instrument ID
        inst = self.search_instrument(sym)
        if inst and inst.get("instrument_id"):
            iid = inst["instrument_id"]

        if iid:
            raw = self._get(f"/feeds/instrument/{iid}?take={take}")
            for disc in (raw or {}).get("discussions") or []:
                art = self._parse_discussion_to_article(disc, default_ticker=sym)
                if art:
                    articles.append(art)

        # If instrument feed returned few items, also search /feeds/news for mentions
        if len(articles) < 3:
            news_items = self.get_feed_news(take=30)
            for item in news_items:
                if sym in item.get("symbols", []) or sym in item.get("title", "").upper():
                    if item["id"] not in {a["id"] for a in articles}:
                        item_copy = dict(item)
                        item_copy["primary_ticker"] = sym
                        articles.append(item_copy)

        with self._lock:
            self._feed_cache[cache_key] = (time.monotonic(), articles)
        return articles

    def get_feed_for_instrument(self, instrument_id: int, limit: int = 3) -> List[Dict[str, Any]]:
        """Get recent social posts for an instrument (backwards compatibility)."""
        iid = int(instrument_id)
        ttl = int(self._cfg_value("feed_ttl_seconds", _TTL_FEED))
        cache_key = f"inst_feed_{iid}"
        with self._lock:
            cached = self._feed_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < ttl:
                return cached[1]
        raw = self._get(f"/feeds/instrument/{iid}?take={max(limit, 5)}")
        if not isinstance(raw, dict) or not (raw.get("discussions") or []):
            raw = self._get(f"/feeds/news?take={max(limit, 5)}")
        posts: List[Dict[str, Any]] = []
        for disc in (raw or {}).get("discussions") or []:
            if not isinstance(disc, dict):
                continue
            post = disc.get("post") or {}
            owner = post.get("owner") or {}
            msg_raw = post.get("message") or post.get("text") or ""
            if isinstance(msg_raw, dict):
                msg = str(msg_raw.get("text") or msg_raw.get("content") or "")[:200]
            else:
                msg = str(msg_raw)[:200]
            tags = post.get("tags") or []
            instruments_mentioned = []
            for t in tags:
                if isinstance(t, dict):
                    market = t.get("market") or {}
                    sym_tag = market.get("symbolName") or t.get("tag") or ""
                    if sym_tag:
                        instruments_mentioned.append(sym_tag)
            emotions = disc.get("emotionsData") or {}
            like_count = (emotions.get("like") or {}).get("paging", {}).get("totalCount", 0)
            summary = disc.get("summary") or {}
            comment_count = summary.get("totalCommentsAndReplies", 0)
            posts.append({
                "author": owner.get("username", ""),
                "text": msg,
                "created": str(post.get("created", ""))[:19],
                "likes": like_count,
                "comments": comment_count,
                "instruments_mentioned": instruments_mentioned,
            })
            if len(posts) >= limit:
                break
        with self._lock:
            self._feed_cache[cache_key] = (time.monotonic(), posts)
        return posts

    # ---- Watchlists ----

    def get_watchlists(self) -> List[Dict[str, Any]]:
        ttl = int(self._cfg_value("watchlist_ttl_seconds", _TTL_WATCHLIST))
        with self._lock:
            if self._watchlists_cache:
                ts, data = self._watchlists_cache
                if time.monotonic() - ts < ttl:
                    return data
        raw = self._get("/watchlists")
        if not isinstance(raw, dict):
            return []
        result = []
        for wl in raw.get("watchlists") or []:
            if not isinstance(wl, dict):
                continue
            result.append({
                "watchlist_id": wl.get("watchlistId"),
                "name": wl.get("name", ""),
                "type": wl.get("watchlistType", ""),
                "total_items": wl.get("totalItems", 0),
                "items": [
                    {"item_id": i.get("itemId"), "item_type": i.get("itemType"), "rank": i.get("itemRank", 0)}
                    for i in (wl.get("items") or [])
                    if isinstance(i, dict)
                ],
            })
        with self._lock:
            self._watchlists_cache = (time.monotonic(), result)
        return result

    def get_watchlist_detail(self, watchlist_id: str) -> List[Dict[str, Any]]:
        raw = self._get(f"/watchlists/{watchlist_id}")
        if not isinstance(raw, dict):
            return []
        detail_wls = raw.get("watchlists") or []
        if not detail_wls:
            return []
        return [
            {"item_id": i.get("itemId"), "item_type": i.get("itemType"), "rank": i.get("itemRank", 0)}
            for i in (detail_wls[0].get("items") or [])
            if isinstance(i, dict) and i.get("itemType") == "Instrument"
        ]

    def resolve_instrument_ids(self, instrument_ids: List[int]) -> List[Dict[str, Any]]:
        """Resolve instrument IDs in bulk to symbolFull, name, and type."""
        if not instrument_ids:
            return []
        params = {"instrumentIds": ",".join(str(i) for i in instrument_ids)}
        raw = self._get("/market-data/instruments", params=params)
        if not isinstance(raw, dict):
            return []
        items = raw.get("instrumentDisplayDatas") or []
        out = []
        for d in items:
            out.append({
                "instrument_id": d.get("instrumentID"),
                "symbol": d.get("symbolFull"),
                "name": d.get("instrumentDisplayName"),
                "type_id": d.get("instrumentTypeID"),
                "exchange_id": d.get("exchangeID"),
            })
        return out

    def invalidate_cache(self) -> None:
        with self._lock:
            self._rates_cache.clear()
            self._search_cache.clear()
            self._feed_cache.clear()
            self._candles_cache.clear()
            self._watchlists_cache = None
            self._session = None
            self._keys_resolved = False


_client: Optional[EToroClient] = None
_client_lock = threading.Lock()


def get_etoro_client() -> EToroClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = EToroClient()
    return _client
