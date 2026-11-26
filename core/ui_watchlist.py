# ui_watchlist.py - preset and watchlist management

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import List, Tuple, Optional

from core.config import PRESETS_PATH, WATCHLIST_PATH, _config_write_lock


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
        temp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        import time
        for attempt in range(5):
            try:
                os.replace(str(temp_path), str(path))
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1)
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass


def _load_watchlist() -> Tuple[List[str], List[str]]:
    """Load watchlist (stocks + cryptos)."""
    stocks: list[str] = []
    cryptos: list[str] = []
    if WATCHLIST_PATH.exists():
        try:
            with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            stocks = list(data.get("stocks", []) or [])
            cryptos = list(data.get("crypto", data.get("cryptos", [])) or [])
        except Exception:
            pass
    return stocks, cryptos


def _save_watchlist(stocks: List[str], cryptos: List[str]) -> None:
    with _config_write_lock(WATCHLIST_PATH):
        try:
            data = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8")) if WATCHLIST_PATH.exists() else {}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        data.update(stocks=stocks, crypto=cryptos, cryptos=cryptos)
        _write_json_atomic(WATCHLIST_PATH, data)


# ========== PRESETS ==========

def load_presets_for_ui() -> Tuple[List[str], Optional[str]]:
    """
    Returns (preset_name_list, current_preset_or_None)
    for populating the UI combobox.
    """
    names: List[str] = []
    current: Optional[str] = None

    if PRESETS_PATH.exists():
        try:
            with open(PRESETS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            names = list(data.get("presets", {}).keys())
            current = data.get("current_preset") or data.get("default_preset")
        except Exception:
            pass

    return names, current


def apply_preset(preset_name: str) -> str:
    """
    Apply preset: write tickers to watchlist.json.
    Returns a log message for the UI.
    """
    if not preset_name:
        return "⚠️ No preset selected"

    try:
        if not PRESETS_PATH.exists():
            return f"⚠️ presets.json does not exist"

        with open(PRESETS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        preset_data = data.get("presets", {}).get(preset_name)
        if not preset_data:
            return f"⚠️ Preset '{preset_name}' does not exist"
        tickers = preset_data.get("tickers", []) or []
        if not tickers:
            return f"⚠️ Preset '{preset_name}' has no tickers"

        stocks, cryptos = _load_watchlist()

        for t in tickers:
            t = t.strip().upper()
            if not t:
                continue
            if t.endswith("-USD") or t.endswith("-USDC") or t.endswith("USDT"):
                if t not in cryptos:
                    cryptos.append(t)
            else:
                if t not in stocks:
                    stocks.append(t)

        _save_watchlist(stocks, cryptos)
        return f"✅ Preset '{preset_name}' applied"
    except Exception as e:
        return f"❌ {e}"


# ========== WATCHLIST (add/remove) ==========

def add_ticker_to_watchlist(ticker: str) -> str:
    """Add a ticker to watchlist.json and return log message."""
    ticker = ticker.strip().upper()
    if not ticker:
        return "⚠️ Empty ticker"

    try:
        stocks, cryptos = _load_watchlist()

        # FIX: Use set for O(1) lookup instead of O(n) linear search
        if ticker.endswith("-USD") or ticker.endswith("-USDC") or ticker.endswith("USDT"):
            cryptos_set = set(cryptos)
            if ticker in cryptos_set:
                return f"ℹ️ {ticker} already in watchlist"
            cryptos.append(ticker)
        else:
            stocks_set = set(stocks)
            if ticker in stocks_set:
                return f"ℹ️ {ticker} already in watchlist"
            stocks.append(ticker)

        _save_watchlist(stocks, cryptos)
        return f"✅ {ticker} added"
    except Exception as e:
        return f"❌ {e}"


def remove_tickers_from_watchlist(tickers: List[str]) -> str:
    """Remove a list of tickers from watchlist.json and return log message."""
    if not tickers:
        return "⚠️ Nothing to remove"

    to_remove = {t.strip().upper() for t in tickers if t.strip()}
    try:
        stocks, cryptos = _load_watchlist()
        # Ensure consistent uppercase handling - normalize stored values to uppercase
        stocks = [t.upper() for t in stocks if t.upper() not in to_remove]
        cryptos = [t.upper() for t in cryptos if t.upper() not in to_remove]
        _save_watchlist(stocks, cryptos)
        return f"🗑️ Removed: {', '.join(sorted(to_remove))}"
    except Exception as e:
        return f"❌ {e}"


def sync_watchlist_from_etoro(watchlist_name: str = "My Watchlist") -> str:
    """Sync symbols from an eToro watchlist into local watchlist.json (merge-only).
    Does not delete existing symbols; only appends newly discovered symbols.
    """
    try:
        from core.etoro_client import get_etoro_client
        client = get_etoro_client()
        if not client.is_available():
            return "⚠️ eToro client is not configured or unavailable"

        wls = client.get_watchlists()
        target_wl = None
        for wl in wls:
            if str(wl.get("name") or "").strip().lower() == watchlist_name.strip().lower():
                target_wl = wl
                break
        if not target_wl and wls:
            target_wl = wls[0]

        if not target_wl:
            return f"⚠️ Watchlist '{watchlist_name}' not found on eToro"

        items = target_wl.get("items", []) or []
        iids = [i["item_id"] for i in items if i.get("item_type") == "Instrument" and i.get("item_id")]
        if not iids:
            return f"ℹ️ eToro watchlist '{target_wl.get('name')}' has no instruments"

        resolved = client.resolve_instrument_ids(iids)
        stocks, cryptos = _load_watchlist()
        added_count = 0

        for r in resolved:
            sym = str(r.get("symbol") or "").strip().upper()
            if not sym:
                continue
            is_crypto = (r.get("type_id") == 10) or sym.endswith(("-USD", "-USDT", "-USDC"))
            if is_crypto:
                c_sym = sym if sym.endswith(("-USD", "-USDT", "-USDC")) else f"{sym}-USD"
                if c_sym not in cryptos:
                    cryptos.append(c_sym)
                    added_count += 1
            else:
                stock_sym = sym[:-3] if sym.endswith(".US") else sym
                if stock_sym not in stocks:
                    stocks.append(stock_sym)
                    added_count += 1

        if added_count > 0:
            _save_watchlist(stocks, cryptos)
            return f"✅ Synced from eToro '{target_wl.get('name')}': added {added_count} new symbols"
        else:
            return f"ℹ️ eToro '{target_wl.get('name')}': all {len(resolved)} symbols already present in watchlist"
    except Exception as exc:
        return f"❌ eToro sync failed: {exc}"

