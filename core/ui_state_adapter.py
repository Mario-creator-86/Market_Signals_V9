# ui_state_adapter.py - adapter for state.json / signals_state.json -> UI models

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from core.config import STATE_PATH, SIGNALS_PATH, PRESETS_PATH
from core.sectors import get_sector
from core.state_persistence import load_state_payload, load_signals_payload

logger = logging.getLogger(__name__)


@dataclass
class TreeRowVM:
    """View model for a ticker Treeview row."""
    sector: str
    ticker_label: str
    price_str: str
    col_1h: str
    col_30m: str
    col_5m: str
    sig_str: str
    tag: str  # "", "bull", "bear", "signal"


# ========== State / signals I/O ==========

def load_sector_mapping() -> Dict[str, str]:
    """Load ticker -> sector mapping from presets.json."""
    sector_map = {}
    if PRESETS_PATH.exists():
        try:
            with open(PRESETS_PATH, "r", encoding="utf-8") as f:
                presets_data = json.load(f)
                ticker_meta = presets_data.get("ticker_meta", {})
                for ticker, meta in ticker_meta.items():
                    sector_map[ticker] = meta.get("sector", "Unknown")
        except Exception as e:
            logger.error(f"Failed to load sector mapping: {e}")
    return sector_map


def load_state_and_signals() -> Tuple[Optional[dict], Optional[dict]]:
    """Load state.json and signals_state.json (or (None, None) on error)."""
    try:
        state = load_state_payload(None)
    except Exception as e:
        logger.error(f"Failed to load state from {STATE_PATH}: {e}")
        state = None

    try:
        signals = load_signals_payload(None)
    except Exception as e:
        logger.error(f"Failed to load signals from {SIGNALS_PATH}: {e}")
        signals = None

    return state, signals


# ========== Internal helpers ==========

def _format_price(price: Optional[float], asset_type: str) -> str:
    if price is None:
        return "-"
    if asset_type == "crypto" and price < 1:
        return f"${price:.4f}"
    return f"${price:,.2f}" if price >= 1000 else f"${price:.2f}"


def _build_signal_map(signals: Optional[dict]) -> Dict[str, dict]:
    """Map ticker -> last signal (open or closed) for that ticker."""
    sig_map: Dict[str, dict] = {}
    if not signals:
        return sig_map

    open_sigs = (signals.get("open") or {}) if isinstance(signals, dict) else {}
    closed_sigs = (signals.get("closed") or []) if isinstance(signals, dict) else []

    for s in open_sigs.values():
        t = s.get("ticker")
        if t:
            sig_map[t] = s

    for s in closed_sigs[-20:]:
        t = s.get("ticker")
        if t and t not in sig_map:
            sig_map[t] = s

    return sig_map


# ========== Build Treeview rows ==========

def build_tree_rows(
    state: Optional[dict],
    signals: Optional[dict],
) -> List[TreeRowVM]:
    """Build Treeview row list from state + signals."""
    rows: List[TreeRowVM] = []

    if not state or "tickers" not in state:
        return rows

    sig_map = _build_signal_map(signals)

    for t, info in state["tickers"].items():
        # FIX: Use get_sector() from core.sectors instead of load_sector_mapping()
        sector = get_sector(t)
        
        # Defensive None checking before float conversion
        change_pct_value = info.get("change_pct", 0)
        if change_pct_value is None:
            pct = 0.0
        else:
            try:
                pct = float(change_pct_value)
            except (TypeError, ValueError):
                pct = 0.0

        sign = "+" if pct >= 0 else ""
        ticker_label = f"{t} {sign}{pct:.1f}%"

        price = info.get("price")
        asset = info.get("asset_type", "stock")
        last_upd = info.get("last_update", "")
        time_part = last_upd[11:19] if isinstance(last_upd, str) and len(last_upd) >= 19 else ""
        price_str = f"{_format_price(price, asset)} ({time_part})"

        tr_1h = info.get("trend_1h", "N/A")
        ts_1h = info.get("ts_1h", "")
        tr_30m = info.get("trend_30m", "N/A")
        ts_30m = info.get("ts_30m", "")
        tr_5m = info.get("trend_5m", "N/A")
        ts_5m = info.get("ts_5m", "")

        col_1h = f"{tr_1h} ({ts_1h})" if ts_1h else tr_1h
        col_30m = f"{tr_30m} ({ts_30m})" if ts_30m else tr_30m
        col_5m = f"{tr_5m} ({ts_5m})" if ts_5m else tr_5m

        sig = sig_map.get(t)
        sig_str = "-"
        if sig and sig.get("status") == "Pending":
            action = sig.get("action", sig.get("type", ""))
            variant = sig.get("variant", "C")
            variant_mode = str(sig.get("variant_mode", "STRICT")).upper()
            sig_time = sig.get("created_at_time", "")
            if not sig_time:
                ca = sig.get("created_at", "")
                sig_time = ca[11:19] if isinstance(ca, str) and len(ca) >= 19 else ""
            tp, sl = sig.get("tp", 0.0), sig.get("sl", 0.0)
            bt_stats = sig.get("backtest_stats", {}) or {}
            bt_str = ""
            if bt_stats and bt_stats.get("total", 0) > 0:
                bt_str = f" | BT:{bt_stats.get('win_pct', 0)}%W"
            backed = " *S" if sig.get("strict_backed") else ""
            replaced = " *R" if sig.get("strict_replaced") else ""
            sig_str = f"[{variant}|{variant_mode}]{backed}{replaced} {action} ({sig_time}) TP:{tp:.2f} SL:{sl:.2f}{bt_str}"

        tag = ""
        if pct > 0.5:
            tag = "bull"
        elif pct < -0.5:
            tag = "bear"

        if sig and sig.get("status") == "Pending":
            tag = "signal"

        rows.append(
            TreeRowVM(
                sector=sector,
                ticker_label=ticker_label,
                price_str=price_str,
                col_1h=col_1h,
                col_30m=col_30m,
                col_5m=col_5m,
                sig_str=sig_str,
                tag=tag,
            )
        )

    return rows
