"""Strategy Lab data: daily price panels for crypto and US stocks, cached on disk.

Crypto comes from Binance's public API (the liquid USDT pairs that also have a
perpetual, so that a short is possible). Stocks come from Yahoo daily bars, in
small slow batches: a fast bulk pull once got this machine rate-limited and the
live app lost its quotes for minutes.

Only completed days are kept. A panel is a plain ``.npz`` file (no pickle).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np

from core.delist_watcher import FUTURES_INFO_URL, _http_fetch
from core.strategy_lab.engine import Panel

logger = logging.getLogger(__name__)

DAY_MS = 86_400_000
NY = ZoneInfo("America/New_York")
TICKERS_URL = "https://api.binance.com/api/v3/ticker/24hr"
KLINES_URL = "https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1d&limit={limit}"
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&startTime={start}&limit=1000"
FUNDING_FILE = "crypto_funding.npz"
SYMBOL_DIRECTORY_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt"
STOCK_META_FILE = "stocks_meta.json"
# The close as it really traded on the day. Yahoo restates past prices for later splits, so a stock that
# traded for cents and then did reverse splits looks like a stock of thousands of dollars in the adjusted history.
TRADED_CLOSE_FILE = "stocks_traded_close.npz"
STOCK_META_MAX_AGE_DAYS = 7
# Listing exchange code of the US symbol directory -> the prefix TradingView uses for that exchange.
LISTING_EXCHANGES = {"N": "NYSE", "Q": "NASDAQ", "A": "AMEX", "P": "AMEX", "Z": "CBOE"}
# Not ordinary shares: the backtest's cost model does not hold for them.
_NOT_SHARES = (" warrant", " unit", " right", " etn", " notes due", "% ", " preferred", " depositary shares")
NOT_COINS = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "EURI", "AEUR", "USD1", "XUSD", "BFUSD", "USDE",
    "PYUSD", "RLUSD", "USDS", "FRAX", "GBP", "TRY", "BRL", "WBTC", "WBETH", "BNSOL", "PAXG", "XAUT",
}

MARKETS: Dict[str, Dict[str, Any]] = {
    # cost = per unit of turnover (one side)
    # entry_lag: closes between the ranking and the entry. Crypto is entered right after the daily close;
    # stocks are ranked after the close and can only be entered in the next session.
    "crypto": {"cost": 0.00065, "min_dollar_volume": 10e6, "min_price": 0.0, "benchmark": "BTCUSDT",
               "max_names": 5, "history_days": 800, "min_history_days": 90, "entry_lag": 0, "min_hold": 1,
               "file": "crypto_daily.npz"},
    # short_borrow_annual: the fee assumed for borrowing a stock to short it. The rules tend to short the most
    # volatile names, which are expensive to borrow, so the assumption is deliberately high.
    "stocks": {"cost": 0.00035, "min_dollar_volume": 20e6, "min_price": 5.0, "benchmark": "SPY",
               "max_names": 6, "history_days": 1300, "min_history_days": 120, "entry_lag": 1, "min_hold": 3,
               "short_borrow_annual": 0.20, "rank_benchmark": False, "file": "stocks_daily.npz"},
}

Fetch = Callable[[str], Tuple[int, Any]]
Download = Callable[[List[str], str], Dict[str, List[Tuple[int, float, float]]]]


def lab_dir() -> Path:
    from core.config import data_path

    path = Path(data_path("research", "strategy_lab"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return number


# ------------------------------------------------------------------ storage
def save_table(path: Path, table: Dict[str, Dict[int, Tuple[float, float]]]) -> None:
    """Write {symbol: {day: (close, dollar volume)}} as aligned matrices."""
    symbols = sorted(s for s, rows in table.items() if rows)
    days = sorted({day for s in symbols for day in table[s]})
    index = {day: i for i, day in enumerate(days)}
    close = np.full((len(days), len(symbols)), np.nan)
    volume = np.full((len(days), len(symbols)), np.nan)
    for j, symbol in enumerate(symbols):
        for day, (c, v) in table[symbol].items():
            close[index[day], j], volume[index[day], j] = c, v
    tmp = path.with_name(path.name + ".tmp.npz")
    np.savez_compressed(tmp, days=np.array(days, dtype=np.int64), symbols=np.array(symbols, dtype=str),
                        close=close, dollar_volume=volume)
    os.replace(tmp, path)


def load_table(path: Path) -> Dict[str, Dict[int, Tuple[float, float]]]:
    if not Path(path).exists():
        return {}
    try:
        with np.load(path, allow_pickle=False) as data:
            days, symbols = data["days"], [str(s) for s in data["symbols"]]
            close, volume = data["close"], data["dollar_volume"]
    except Exception as exc:  # noqa: BLE001 - truncated or damaged archive (BadZipFile, EOFError, ...): start again
        logger.warning("Strategy lab: %s unreadable (%s: %s)", path, exc.__class__.__name__, exc)
        return {}
    table: Dict[str, Dict[int, Tuple[float, float]]] = {}
    for j, symbol in enumerate(symbols):
        rows = {}
        for i in np.flatnonzero(np.isfinite(close[:, j])):
            rows[int(days[i])] = (float(close[i, j]), float(volume[i, j]) if np.isfinite(volume[i, j]) else 0.0)
        table[symbol] = rows
    return table


def panel_from_table(market: str, table: Dict[str, Dict[int, Tuple[float, float]]], last_day: Optional[int] = None,
                     carry_table: Optional[Dict[str, Dict[int, Tuple[float, float]]]] = None,
                     floor_table: Optional[Dict[str, Dict[int, Tuple[float, float]]]] = None) -> Optional[Panel]:
    """A Panel of the completed days up to ``last_day``; None when there is too little data.

    ``floor_table`` holds the close as it really traded; the price floor is applied to it.
    """
    cfg = MARKETS[market]
    symbols = sorted(s for s, rows in table.items() if len(rows) >= 60)
    days = sorted({day for s in symbols for day in table[s] if last_day is None or day <= last_day})
    days = days[-int(cfg["history_days"]):]
    if len(days) < 250 or len(symbols) < 20:
        return None
    index = {day: i for i, day in enumerate(days)}
    close = np.full((len(days), len(symbols)), np.nan)
    volume = np.full((len(days), len(symbols)), np.nan)
    for j, symbol in enumerate(symbols):
        for day, (c, v) in table[symbol].items():
            i = index.get(day)
            if i is not None:
                close[i, j], volume[i, j] = c, v
    carry = None
    if carry_table:
        carry = np.zeros((len(days), len(symbols)))
        for j, symbol in enumerate(symbols):
            for day, (rate, _count) in (carry_table.get(symbol) or {}).items():
                i = index.get(day)
                if i is not None:
                    carry[i, j] = rate
    floor = None
    if floor_table:
        floor = np.full((len(days), len(symbols)), np.nan)
        for j, symbol in enumerate(symbols):
            for day, (traded, _unused) in (floor_table.get(symbol) or {}).items():
                i = index.get(day)
                if i is not None:
                    floor[i, j] = traded

    def build(cols: List[int]) -> Panel:
        return Panel(market, np.array(days, dtype=np.int64), [symbols[j] for j in cols], close[:, cols], volume[:, cols],
                     cfg["benchmark"], cfg["cost"], cfg["min_dollar_volume"], cfg["min_price"],
                     int(cfg.get("min_history_days", 0)), None if carry is None else carry[:, cols],
                     int(cfg.get("entry_lag", 0)), float(cfg.get("short_borrow_annual", 0.0)) / 252.0,
                     None if floor is None else floor[:, cols], bool(cfg.get("rank_benchmark", True)))

    panel = build(list(range(len(symbols))))
    # Instruments that could never be picked only slow the search down.
    keep = [j for j in range(len(symbols)) if panel.eligible[:, j].any() or symbols[j] == cfg["benchmark"]]
    return panel if len(keep) == len(symbols) else build(keep)


def _http_text(url: str) -> Tuple[int, str]:
    """GET a public text file. Trouble comes back as (0, reason)."""
    if os.environ.get("MS_ENV") == "test":
        return 0, "network disabled under MS_ENV=test"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "market-research-script"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - DNS, timeout, HTTP error
        return 0, f"{exc.__class__.__name__}: {exc}"


def parse_symbol_directory(text: str) -> Dict[str, Dict[str, Any]]:
    """{yahoo symbol: {"etf": bool, "rankable": bool, "name": str}} from the US symbol directory."""
    lines = [line for line in str(text or "").splitlines() if "|" in line]
    if not lines or "Symbol" not in lines[0]:
        return {}
    header = lines[0].split("|")
    col = {name: header.index(name) for name in ("Symbol", "Security Name", "ETF", "Test Issue") if name in header}
    if len(col) < 4:
        return {}
    exchange_col = header.index("Listing Exchange") if "Listing Exchange" in header else -1
    out: Dict[str, Dict[str, Any]] = {}
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) < len(header) or line.startswith("File Creation Time"):
            continue
        symbol = parts[col["Symbol"]].strip().upper().replace(".", "-")
        name = parts[col["Security Name"]].strip()
        etf, test = parts[col["ETF"]].strip().upper() == "Y", parts[col["Test Issue"]].strip().upper() == "Y"
        lowered = f" {name.lower()} "
        ordinary = not any(marker in lowered for marker in _NOT_SHARES)
        if symbol:
            out[symbol] = {"etf": etf, "rankable": not etf and not test and ordinary, "name": name[:80]}
            exchange = LISTING_EXCHANGES.get(parts[exchange_col].strip().upper()) if exchange_col >= 0 else None
            if exchange:
                out[symbol]["exchange"] = exchange
    return out


def load_stock_meta() -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads((lab_dir() / STOCK_META_FILE).read_text(encoding="utf-8"))
        symbols = data.get("symbols") if isinstance(data, dict) else None
        return symbols if isinstance(symbols, dict) else {}
    except (OSError, ValueError):
        return {}


_LISTING_CACHE: Dict[str, Any] = {"mtime": None, "symbols": {}}


def stock_listing(ticker: Any) -> Dict[str, Any]:
    """Where a US stock or ETF is listed, from the stored symbol directory.

    The chart needs it: TradingView has no "NASDAQ:DV" (the stock is on the NYSE)
    and shows "this symbol doesn't exist". ``tradingview`` is "" when the
    directory does not know the symbol; the caller then lets TradingView search.
    """
    symbol = str(ticker or "").strip().upper().replace(".", "-")
    try:
        mtime = (lab_dir() / STOCK_META_FILE).stat().st_mtime
    except OSError:
        mtime = None
    if mtime != _LISTING_CACHE["mtime"]:
        _LISTING_CACHE.update({"mtime": mtime, "symbols": load_stock_meta() if mtime is not None else {}})
    entry = _LISTING_CACHE["symbols"].get(symbol) if symbol else None
    exchange = str((entry or {}).get("exchange") or "")
    return {
        "ticker": symbol, "known": bool(entry), "exchange": exchange, "name": str((entry or {}).get("name") or ""),
        # TradingView writes a share class with a dot (BRK.B), Yahoo with a dash (BRK-B).
        "tradingview": f"{exchange}:{symbol.replace('-', '.')}" if exchange else "",
    }


def refresh_stock_meta(fetch_text: Optional[Callable[[str], Tuple[int, str]]] = None, now_ms: Optional[int] = None,
                       *, force: bool = False) -> Dict[str, Any]:
    """Download the US symbol directory (one public file) when the stored copy is older than a week."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    path = lab_dir() / STOCK_META_FILE
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    age_days = (now_ms - int(stored.get("fetched_ms") or 0)) / DAY_MS if isinstance(stored, dict) else 1e9
    if not force and stored.get("symbols") and age_days < STOCK_META_MAX_AGE_DAYS:
        return {"ok": True, "symbols": len(stored["symbols"]), "refreshed": False}
    status, text = (fetch_text or _http_text)(SYMBOL_DIRECTORY_URL)
    symbols = parse_symbol_directory(text) if status == 200 else {}
    if len(symbols) < 1000:
        return {"ok": False, "reason": f"symbol directory unavailable (status {status})",
                "symbols": len((stored or {}).get("symbols") or {}), "refreshed": False}
    # A stock that left the directory (taken over, bankrupt) keeps its entry: erasing it would remove
    # its history from every past ranking and change old results without any new price.
    previous = stored.get("symbols") if isinstance(stored, dict) and isinstance(stored.get("symbols"), dict) else {}
    for symbol, entry in previous.items():
        if symbol not in symbols and isinstance(entry, dict):
            symbols[symbol] = {**entry, "listed": False}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"fetched_ms": now_ms, "symbols": symbols}), encoding="utf-8")
    os.replace(tmp, path)
    return {"ok": True, "symbols": len(symbols), "refreshed": True}


def rankable_stocks(table: Dict[str, Dict[int, Tuple[float, float]]], meta: Dict[str, Dict[str, Any]],
                    benchmark: str) -> Dict[str, Dict[int, Tuple[float, float]]]:
    """Ordinary shares only: no ETFs, leveraged products, warrants, units or test issues.

    A symbol no directory ever listed is left out too (not a US listing, or gone
    before the first download); one that left later keeps its history. Without a
    directory nothing can be told apart, so nothing is ranked.
    """
    return {symbol: rows for symbol, rows in table.items()
            if symbol == benchmark or bool((meta.get(symbol) or {}).get("rankable"))}


def load_panel(market: str, now_ms: Optional[int] = None) -> Optional[Panel]:
    table = load_table(lab_dir() / MARKETS[market]["file"])
    if market == "stocks":
        table = rankable_stocks(table, load_stock_meta(), MARKETS[market]["benchmark"])
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    carry_table = load_table(lab_dir() / FUNDING_FILE) if market == "crypto" else None
    floor_table = load_table(lab_dir() / TRADED_CLOSE_FILE) if market == "stocks" else None
    return panel_from_table(market, table, last_complete_day(market, now_ms), carry_table, floor_table)


def last_complete_day(market: str, now_ms: int) -> int:
    """The last day whose bar is final (days since 1970-01-01)."""
    if market == "crypto":
        return now_ms // DAY_MS - 1                        # the UTC day that ended at the last midnight
    local = dt.datetime.fromtimestamp(now_ms / 1000, NY)
    date = local.date() if (local.hour, local.minute) >= (16, 20) else local.date() - dt.timedelta(days=1)
    while not is_stock_session(date):                       # weekend or holiday: the last session was earlier
        date -= dt.timedelta(days=1)
    return (date - dt.date(1970, 1, 1)).days


def is_stock_session(date: dt.date) -> bool:
    """True on a US trading day (weekends and exchange holidays are not)."""
    try:
        from core.market_hours import is_us_equity_trading_day

        return bool(is_us_equity_trading_day(date))
    except Exception:  # noqa: BLE001 - fall back to weekdays
        return date.weekday() < 5


def stock_exit_time(date: dt.date) -> dt.time:
    """When to be out on the last day of a holding period: a quarter of an hour before the close."""
    try:
        from core.market_hours import _us_equity_early_close_date

        if _us_equity_early_close_date(date):
            return dt.time(12, 45)                              # the session ends at 13:00
    except Exception:  # noqa: BLE001
        pass
    return dt.time(15, 45)


def session_after(date: dt.date, sessions: int) -> dt.date:
    """The ``sessions``-th US trading day after ``date``."""
    for _ in range(max(1, int(sessions))):
        date += dt.timedelta(days=1)
        while not is_stock_session(date):
            date += dt.timedelta(days=1)
    return date


# ------------------------------------------------------------------- crypto
def crypto_universe(fetch: Fetch, max_symbols: int = 150) -> Optional[List[str]]:
    status, data = fetch(TICKERS_URL)
    if status != 200 or not isinstance(data, list):
        logger.warning("Strategy lab: Binance ticker list unavailable (status %s)", status)
        return None
    status, info = fetch(FUTURES_INFO_URL)
    if status != 200 or not isinstance(info, dict):
        logger.warning("Strategy lab: Binance perpetual list unavailable (status %s)", status)
        return None
    perps = {str(item.get("symbol")) for item in info.get("symbols") or []
             if item.get("contractType") == "PERPETUAL" and item.get("quoteAsset") == "USDT"
             and item.get("status") == "TRADING"}
    rows = []
    for item in data:
        symbol = str(item.get("symbol") or "")
        if not symbol.endswith("USDT") or not (symbol.isascii() and symbol.isalnum()):
            continue
        base = symbol[:-4]
        if base in NOT_COINS or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
            continue
        if symbol not in perps and f"1000{symbol}" not in perps:
            continue
        rows.append((_number(item.get("quoteVolume")), symbol))
    rows.sort(reverse=True)
    return [symbol for _volume, symbol in rows[:max_symbols]]


def refresh_crypto(fetch: Optional[Fetch] = None, now_ms: Optional[int] = None, *, exclude: Iterable[str] = ()) -> Dict[str, Any]:
    """Download the daily bars of the liquid USDT pairs and store the completed days."""
    fetch = fetch or _http_fetch
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    cfg = MARKETS["crypto"]
    path = lab_dir() / cfg["file"]
    symbols = crypto_universe(fetch)
    if symbols is None:
        return {"ok": False, "reason": "Binance lists unavailable", "symbols": 0}
    skip = {str(s).upper() for s in exclude}
    symbols = [s for s in symbols if s not in skip]
    limit = min(1000, int(cfg["history_days"]) + 5)

    def one(symbol: str) -> Tuple[str, Dict[int, Tuple[float, float]]]:
        status, bars = fetch(KLINES_URL.format(symbol=symbol, limit=limit))
        rows: Dict[int, Tuple[float, float]] = {}
        if status == 200 and isinstance(bars, list):
            for bar in bars:
                # [open time, open, high, low, close, volume, close time, quote volume, ...]
                if isinstance(bar, list) and len(bar) >= 8 and int(_number(bar[6])) < now_ms:
                    close, quote = _number(bar[4]), _number(bar[7])
                    if close > 0:
                        rows[int(bar[0]) // DAY_MS] = (close, quote if quote == quote else 0.0)
        return symbol, rows

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="lab-crypto") as pool:
        fetched = dict(pool.map(one, symbols))
    got = {s: rows for s, rows in fetched.items() if rows}
    # A partial answer would leave a last day with only some of the pairs: keep the previous history instead.
    if len(got) < 20 or len(got) < 0.9 * len(symbols):
        return {"ok": False, "reason": f"only {len(got)} of {len(symbols)} pairs returned bars; history left as it was",
                "symbols": len(got)}
    table = load_table(path)
    for symbol, rows in got.items():
        table[symbol] = rows                                  # Binance history does not change: replace
    save_table(path, table)
    return {"ok": True, "symbols": len(got), "last_day": max(max(rows) for rows in got.values())}


def refresh_funding(fetch: Optional[Fetch] = None, now_ms: Optional[int] = None, *, max_calls: int = 500,
                    pause_seconds: float = 0.7, sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Store the daily sum of funding rates of every cached pair's perpetual (a long pays a positive rate).

    Incremental: each call continues after the last stored day. Paced, because
    Binance limits this endpoint to a few hundred calls per five minutes.
    """
    fetch = fetch or _http_fetch
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    prices = load_table(lab_dir() / MARKETS["crypto"]["file"])
    if not prices:
        return {"ok": False, "reason": "no crypto price history yet", "symbols": 0}
    status, info = fetch(FUTURES_INFO_URL)
    if status != 200 or not isinstance(info, dict):
        return {"ok": False, "reason": f"perpetual list unavailable (status {status})", "symbols": 0}
    perps = {str(item.get("symbol")) for item in info.get("symbols") or []
             if item.get("contractType") == "PERPETUAL" and item.get("quoteAsset") == "USDT"}
    path = lab_dir() / FUNDING_FILE
    table = load_table(path)
    first_day = now_ms // DAY_MS - int(MARKETS["crypto"]["history_days"]) - 5
    calls = done = 0
    for symbol in sorted(prices):
        perp = symbol if symbol in perps else (f"1000{symbol}" if f"1000{symbol}" in perps else "")
        if not perp:
            continue
        rows = table.setdefault(symbol, {})
        if rows and max(rows) >= now_ms // DAY_MS:
            done += 1
            continue                                          # fetched today already: yesterday is complete
        # The last stored day may be partial: fetch it again.
        start_day = max(first_day, max(rows) if rows else first_day)
        while calls < max_calls:
            status, data = fetch(FUNDING_URL.format(symbol=perp, start=start_day * DAY_MS))
            calls += 1
            if calls < max_calls:
                sleep(pause_seconds)
            if status != 200 or not isinstance(data, list) or not data:
                break
            sums: Dict[int, Tuple[float, float]] = {}
            for item in data:
                day = int(_number(item.get("fundingTime"))) // DAY_MS
                rate = _number(item.get("fundingRate"))
                if rate == rate:
                    total, count = sums.get(day, (0.0, 0.0))
                    sums[day] = (total + rate, count + 1)
            rows.update(sums)
            if len(data) < 1000:
                break
            start_day = max(sums)
        done += 1
        if calls >= max_calls:
            break
    save_table(path, table)
    return {"ok": True, "symbols": done, "calls": calls, "complete": calls < max_calls}


# ------------------------------------------------------------------- stocks
def traded_closes(closes: List[float], splits: List[float]) -> List[float]:
    """The closes as they traded, from closes restated for splits and the split ratios (0 = no split that day).

    A split on day k restates every earlier close: a 1-for-10 reverse split (ratio 0.1) makes a $1 close of
    the past read $10. Multiplying by the ratios of all later splits gives the price back.
    """
    out = [0.0] * len(closes)
    factor = 1.0
    for i in range(len(closes) - 1, -1, -1):
        out[i] = closes[i] * factor
        ratio = splits[i] if i < len(splits) else 0.0
        if ratio == ratio and ratio > 0:
            factor *= ratio
    return out


def _yahoo_download(symbols: List[str], period: str) -> Dict[str, List[Tuple[int, float, float, float]]]:
    """Daily (day, adjusted close, dollar volume, close as traded) per symbol from Yahoo. Empty dict on failure."""
    if os.environ.get("MS_ENV") == "test":
        return {}                                               # the test suite never loads a public website
    import yfinance as yf

    try:
        frame = yf.download(symbols, period=period, interval="1d", auto_adjust=True, actions=True, progress=False,
                            threads=False, group_by="ticker")
    except Exception as exc:  # noqa: BLE001 - network, rate limit, parsing
        logger.warning("Strategy lab: Yahoo batch failed (%s)", exc.__class__.__name__)
        return {}
    out: Dict[str, List[Tuple[int, float, float, float]]] = {}
    for symbol in symbols:
        try:
            try:
                part = frame[symbol]
            except KeyError:
                if len(symbols) != 1 or "Close" not in frame.columns:
                    continue
                part = frame
            part = part.dropna(subset=["Close"])
            closes = [_number(value) for value in part["Close"].to_numpy()]
            volumes = [_number(value) for value in part["Volume"].to_numpy()]
            splits = [_number(value) for value in part["Stock Splits"].to_numpy()] if "Stock Splits" in part.columns else []
        except Exception:  # noqa: BLE001 - symbol missing from the answer
            continue
        traded = traded_closes(closes, splits)
        rows = []
        for stamp, close, volume, real in zip(part.index, closes, volumes, traded):
            if close == close and close > 0:
                day = (stamp.date() - dt.date(1970, 1, 1)).days
                rows.append((day, close, close * (volume if volume == volume else 0.0), real if real == real else close))
        if rows:
            out[symbol] = rows
    return out


def refresh_stocks(add: Iterable[str] = (), *, download: Optional[Download] = None, now_ms: Optional[int] = None,
                   batch: int = 8, pause_seconds: float = 4.0, max_new: int = 120, full: bool = False,
                   sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Update the cached stock history: recent days for known symbols, full history for new ones.

    Small batches with a pause, no threads, and it stops at the first batch that
    returns nothing (the sign of a rate limit). ``full`` downloads the whole
    history of every known symbol again (needed once, to learn the closes as traded).
    """
    download = download or _yahoo_download
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    cfg = MARKETS["stocks"]
    path = lab_dir() / cfg["file"]
    traded_path = lab_dir() / TRADED_CLOSE_FILE
    table = load_table(path)
    traded_table = load_table(traded_path)
    last_ok = last_complete_day("stocks", now_ms)
    known = sorted(table)
    wanted = [s for s in dict.fromkeys(str(x).upper().strip() for x in add) if s and s not in table]
    if cfg["benchmark"] not in table and cfg["benchmark"] not in wanted:
        wanted.insert(0, cfg["benchmark"])
    stale = [s for s in known if max(table[s]) < last_ok]
    refetch: List[str] = []
    updated = added = 0
    stopped = ""

    def run(symbols: List[str], period: str, replace: bool) -> bool:
        nonlocal updated, added, stopped
        for start in range(0, len(symbols), batch):
            chunk = symbols[start:start + batch]
            got = download(chunk, period)
            if not got:
                stopped = f"empty answer for {chunk[0]}.. ({period}); stopped to stay under Yahoo's limits"
                return False
            for symbol, rows in got.items():
                fresh = {row[0]: (row[1], row[2]) for row in rows if row[0] <= last_ok}
                # The close as traded; a source that does not give it (old caches, tests) falls back to the close.
                fresh_traded = {row[0]: (row[3] if len(row) > 3 else row[1], 0.0) for row in rows if row[0] <= last_ok}
                if not fresh:
                    continue
                old = table.get(symbol) or {}
                if replace or not old:
                    table[symbol] = fresh
                    traded_table[symbol] = fresh_traded
                    added += 0 if old else 1
                else:
                    overlap = sorted(set(old) & set(fresh))
                    # A split or dividend re-bases Yahoo's adjusted history: the old rows no longer match.
                    if overlap and abs(fresh[overlap[0]][0] / old[overlap[0]][0] - 1) > 0.005:
                        refetch.append(symbol)
                        continue
                    old.update(fresh)
                    # Closes as traded never change afterwards: the new days are simply added.
                    traded_table.setdefault(symbol, {}).update(fresh_traded)
                updated += 1
            if start + batch < len(symbols):
                sleep(pause_seconds)
        return True

    if full:
        # Resumable: symbols whose closes as traded are already stored are skipped.
        missing = [s for s in known if s not in traded_table]
        ok = run(missing, "5y", True)
    else:
        ok = run(stale, "1mo", False)
    if ok and refetch:
        ok = run(sorted(set(refetch))[:max_new], "5y", True)
    if ok and wanted:
        run(wanted[:max_new], "5y", True)
    if updated or added:
        save_table(path, table)
        save_table(traded_path, traded_table)
    without_traded = sum(1 for s in table if s not in traded_table)
    return {"ok": not stopped, "reason": stopped, "symbols": len(table), "updated": updated, "added": added,
            "pending_new": max(0, len(wanted) - max_new), "last_day": last_ok,
            "without_traded_close": without_traded}


def bootstrap_stocks_from_frames(frames: Dict[str, Any], min_median_dollar_volume: float = 5e6) -> int:
    """Seed the stock cache from already downloaded daily frames (columns Close, Volume)."""
    path = lab_dir() / MARKETS["stocks"]["file"]
    table = load_table(path)
    count = 0
    for symbol, frame in frames.items():
        try:
            part = frame.dropna(subset=["Close"])
            dollars = (part["Close"] * part["Volume"]).to_numpy()
            if len(part) < 250 or float(np.nanmedian(dollars[-250:])) < min_median_dollar_volume:
                continue
            rows = {(stamp.date() - dt.date(1970, 1, 1)).days: (float(close), float(value) if value == value else 0.0)
                    for stamp, close, value in zip(part.index, part["Close"].to_numpy(), dollars) if close > 0}
        except Exception:  # noqa: BLE001 - one malformed frame must not stop the seed
            continue
        if rows and str(symbol).upper() not in table:
            table[str(symbol).upper()] = rows
            count += 1
    if count:
        save_table(path, table)
    return count
