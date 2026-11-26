"""Immutable chart evidence captured when a paper Capital position opens."""

from __future__ import annotations

import hashlib
import html
import logging
import math
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from core.config import DATA_DIR, is_crypto_ticker, normalize_market_ticker
from core.supertrend import precompute_supertrend


logger = logging.getLogger("MarketEngine")
ENTRY_CHART_DIR = Path(os.getenv("MS_ENTRY_CHART_DIR") or (DATA_DIR / "entry_charts"))
_SNAPSHOT_ID = re.compile(r"^entry_[0-9a-f]{24}$")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _normalise_frame(frame: Any) -> Optional[pd.DataFrame]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    normalized = frame.rename(columns={column: str(column).lower() for column in frame.columns}).copy()
    required = {"open", "high", "low", "close"}
    if not required.issubset(normalized.columns):
        return None
    if "volume" not in normalized.columns:
        normalized["volume"] = 0.0
    for column in (*required, "volume"):
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized = normalized.dropna(subset=list(required)).tail(96)
    return normalized if len(normalized) >= 20 else None


def _fetch_entry_frame(symbol: str, timeframe: str) -> tuple[Optional[pd.DataFrame], str]:
    """Use the same cache-first market clients as the live chart and scanner."""
    from api.market_data_service import BinanceClient, KuCoinClient, YahooClient

    if is_crypto_ticker(symbol):
        kucoin_interval = {"30m": "30min", "1h": "1hour", "4h": "4hour"}.get(timeframe, "30min")
        for client, interval in ((BinanceClient, timeframe), (KuCoinClient, kucoin_interval)):
            try:
                frame = _normalise_frame(client.fetch_klines(symbol, interval=interval, limit=120, ttl=30))
                if frame is not None:
                    return frame, client.__name__.replace("Client", "").lower()
            except Exception as exc:
                logger.debug("entry chart fetch failed for %s via %s: %s", symbol, client.__name__, exc)
        return None, "unavailable"

    period = {"30m": "1mo", "1h": "3mo", "4h": "6mo"}.get(timeframe, "1mo")
    interval = "1h" if timeframe == "4h" else timeframe
    try:
        frame = _normalise_frame(YahooClient.fetch_history(symbol, period=period, interval=interval))
        if frame is not None:
            if timeframe == "4h" and isinstance(frame.index, pd.DatetimeIndex):
                frame = _normalise_frame(frame.resample("4h").agg({
                    "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
                }).dropna())
            return frame, "yahoo"
    except Exception as exc:
        logger.debug("entry chart fetch failed for %s via Yahoo: %s", symbol, exc)
    return None, "unavailable"


def _fmt_price(value: Any) -> str:
    number = _f(value, 0.0)
    if abs(number) >= 1000:
        return f"{number:,.2f}"
    if abs(number) >= 1:
        return f"{number:.4f}"
    return f"{number:.8f}".rstrip("0").rstrip(".")


def _svg_text(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _timestamp_label(value: Any) -> str:
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value or "")[:19]


def _render_svg(
    frame: pd.DataFrame,
    *,
    symbol: str,
    direction: str,
    timeframe: str,
    entry: float,
    stop: float,
    target: float,
    strategy_evidence: Dict[str, Any],
    captured_at: str,
) -> str:
    width, height = 1440, 820
    left, right, top, bottom = 72.0, 1010.0, 88.0, 640.0
    chart_width, chart_height = right - left, bottom - top
    count = len(frame)
    x_step = chart_width / max(count, 1)

    supertrend = precompute_supertrend(frame, period=10, multiplier=3.0)
    structure = strategy_evidence.get("market_structure") if isinstance(strategy_evidence.get("market_structure"), dict) else {}
    bos_level = _f(structure.get("event_level"), 0.0)
    values = [*_safe_numbers(frame["high"]), *_safe_numbers(frame["low"])]
    values.extend(value for value in (entry, stop, target, bos_level) if value > 0)
    st_values = [_f(value) for value in supertrend["supertrend_line"].tolist() if _f(value) > 0]
    values.extend(st_values)
    price_min, price_max = min(values), max(values)
    padding = max((price_max - price_min) * 0.08, abs(entry) * 0.002, 1e-8)
    price_min -= padding
    price_max += padding

    def x_at(index: int) -> float:
        return left + (index + 0.5) * x_step

    def y_at(price: float) -> float:
        return top + ((price_max - price) / max(price_max - price_min, 1e-12)) * chart_height

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="1440" height="820" fill="#07111f"/>',
        '<rect x="36" y="28" width="1368" height="752" rx="18" fill="#0b1728" stroke="#21344f"/>',
        '<style>text{font-family:Inter,Segoe UI,Arial,sans-serif}.mono{font-family:Consolas,monospace}</style>',
        f'<text x="68" y="60" fill="#f8fafc" font-size="25" font-weight="800">{_svg_text(symbol)} · {_svg_text(direction)} · ENTRY SNAPSHOT</text>',
        f'<text x="1368" y="59" fill="#8da2bf" font-size="14" text-anchor="end">S8 · {_svg_text(timeframe)} trigger · {_svg_text(captured_at[:19])} UTC</text>',
        f'<rect x="{left}" y="{top}" width="{chart_width}" height="{chart_height}" rx="8" fill="#081322" stroke="#1c3049"/>',
    ]

    for grid_index in range(6):
        ratio = grid_index / 5
        y = top + ratio * chart_height
        price = price_max - ratio * (price_max - price_min)
        parts.append(f'<line x1="{left}" y1="{y:.2f}" x2="{right}" y2="{y:.2f}" stroke="#152a42" stroke-width="1"/>')
        parts.append(f'<text class="mono" x="{right - 8}" y="{y - 6:.2f}" fill="#7187a4" font-size="12" text-anchor="end">{_svg_text(_fmt_price(price))}</text>')

    candle_width = max(2.0, min(8.0, x_step * 0.62))
    for index, (_, row) in enumerate(frame.iterrows()):
        open_price, high, low, close = (_f(row[key]) for key in ("open", "high", "low", "close"))
        bullish = close >= open_price
        color = "#22c55e" if bullish else "#ef4444"
        x = x_at(index)
        body_top = min(y_at(open_price), y_at(close))
        body_height = max(1.5, abs(y_at(open_price) - y_at(close)))
        parts.append(f'<line x1="{x:.2f}" y1="{y_at(high):.2f}" x2="{x:.2f}" y2="{y_at(low):.2f}" stroke="{color}" stroke-width="1.2"/>')
        parts.append(f'<rect x="{x - candle_width / 2:.2f}" y="{body_top:.2f}" width="{candle_width:.2f}" height="{body_height:.2f}" fill="{color}" rx="1"/>')

    previous = None
    for index, row in supertrend.reset_index(drop=True).iterrows():
        line = _f(row.get("supertrend_line"), 0.0)
        if line <= 0:
            previous = None
            continue
        point = (x_at(index), y_at(line), str(row.get("supertrend_direction") or ""))
        if previous is not None and previous[2] == point[2]:
            color = "#3ee68b" if point[2] == "BULLISH" else "#ff6577"
            parts.append(f'<line x1="{previous[0]:.2f}" y1="{previous[1]:.2f}" x2="{point[0]:.2f}" y2="{point[1]:.2f}" stroke="{color}" stroke-width="2.4"/>')
        previous = point

    for label, value, color, dash in (
        ("ENTRY", entry, "#38bdf8", ""),
        ("STOP", stop, "#ef4444", "6 5"),
        ("TARGET", target, "#22c55e", "6 5"),
        ("BOS", bos_level, "#fbbf24", "3 4"),
    ):
        if value <= 0:
            continue
        y = y_at(value)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        parts.append(f'<line x1="{left}" y1="{y:.2f}" x2="{right}" y2="{y:.2f}" stroke="{color}" stroke-width="1.6"{dash_attr}/>')
        parts.append(f'<rect x="{right - 130}" y="{y - 18:.2f}" width="122" height="18" rx="4" fill="#07111f" opacity=".92"/>')
        parts.append(f'<text class="mono" x="{right - 13}" y="{y - 5:.2f}" fill="{color}" font-size="12" text-anchor="end">{label} {_svg_text(_fmt_price(value))}</text>')

    for index in (0, count // 2, count - 1):
        parts.append(f'<text x="{x_at(index):.2f}" y="{bottom + 24}" fill="#7187a4" font-size="12" text-anchor="middle">{_svg_text(_timestamp_label(frame.index[index]))}</text>')

    panel_x = 1042
    parts.extend([
        f'<text x="{panel_x}" y="112" fill="#fbbf24" font-size="14" font-weight="800">WHY THE POSITION OPENED</text>',
        f'<text x="{panel_x}" y="143" fill="#f8fafc" font-size="21" font-weight="800">{_svg_text(direction)} @ {_svg_text(_fmt_price(entry))}</text>',
        f'<text x="{panel_x}" y="169" fill="#91a6c1" font-size="13">Risk: SL {_svg_text(_fmt_price(stop))} · TP {_svg_text(_fmt_price(target))}</text>',
    ])
    sequence = strategy_evidence.get("sequence") if isinstance(strategy_evidence.get("sequence"), list) else []
    y_cursor = 210
    for item in sequence[:4]:
        if not isinstance(item, dict):
            continue
        detail = str(item.get("detail") or "")
        wrapped = _wrap(detail, 43)
        parts.append(f'<circle cx="{panel_x + 5}" cy="{y_cursor - 4}" r="5" fill="#fbbf24"/>')
        for line_index, line in enumerate(wrapped[:3]):
            parts.append(f'<text x="{panel_x + 20}" y="{y_cursor + line_index * 19}" fill="#d7e2f0" font-size="13">{_svg_text(line)}</text>')
        y_cursor += max(54, len(wrapped[:3]) * 19 + 18)

    mtf = strategy_evidence.get("multi_timeframe") if isinstance(strategy_evidence.get("multi_timeframe"), dict) else {}
    states = mtf.get("states") if isinstance(mtf.get("states"), dict) else {}
    parts.append(f'<text x="{panel_x}" y="{y_cursor + 8}" fill="#fbbf24" font-size="14" font-weight="800">MULTI-TIMEFRAME CHECK</text>')
    y_cursor += 42
    expected = "BULLISH" if direction == "LONG" else "BEARISH"
    for timeframe in ("30m", "1h", "4h"):
        state = states.get(timeframe) if isinstance(states.get(timeframe), dict) else {}
        state_direction = str(state.get("supertrend_direction") or "UNAVAILABLE").upper()
        available = state.get("available") is True
        aligned = available and state_direction == expected
        color = "#22c55e" if aligned else ("#ef4444" if available else "#7187a4")
        role = "TRIGGER" if timeframe == "30m" else "CONFIRM"
        parts.append(f'<rect x="{panel_x}" y="{y_cursor - 20}" width="326" height="34" rx="7" fill="#101f33" stroke="#213852"/>')
        parts.append(f'<text x="{panel_x + 12}" y="{y_cursor + 2}" fill="#f8fafc" font-size="13" font-weight="800">{timeframe} · {role}</text>')
        parts.append(f'<text x="{panel_x + 312}" y="{y_cursor + 2}" fill="{color}" font-size="12" font-weight="800" text-anchor="end">{_svg_text(state_direction)}</text>')
        y_cursor += 45

    parts.extend([
        f'<text x="68" y="745" fill="#91a6c1" font-size="13">Frozen evidence: only candles available at entry are shown. Green/red line = SuperTrend; gold = BOS; blue/red/green = entry/SL/TP.</text>',
        f'<text x="68" y="768" fill="#617895" font-size="12">Snapshot is audit evidence for simulated paper trading; it is not a promise of profit.</text>',
        '</svg>',
    ])
    return "".join(parts)


def _safe_numbers(series: pd.Series) -> list[float]:
    return [number for value in series.tolist() for number in [_f(value, float("nan"))] if math.isfinite(number)]


def _wrap(text: str, width: int) -> list[str]:
    words = str(text or "").split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join([*current, word])
        if current and len(candidate) > width:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines or [""]


def capture_entry_chart_snapshot(
    *,
    position: Dict[str, Any],
    decision_context: Dict[str, Any],
    timeframe: str = "30m",
    frame: Optional[pd.DataFrame] = None,
    source: str = "",
    output_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    symbol = str(normalize_market_ticker(position.get("ticker")) or "").upper().strip()
    direction = str(position.get("direction") or "").upper().strip()
    strategy_evidence = (
        dict(decision_context.get("strategy_evidence") or {})
        if isinstance(decision_context.get("strategy_evidence"), dict)
        else {}
    )
    captured_at = datetime.now(timezone.utc).isoformat()
    normalized = _normalise_frame(frame)
    frame_source = str(source or "provided") if normalized is not None else ""
    if normalized is None:
        normalized, frame_source = _fetch_entry_frame(symbol, timeframe)
    if normalized is None:
        return {
            "available": False,
            "capture_status": "ohlcv_unavailable",
            "symbol": symbol,
            "timeframe": timeframe,
            "captured_at": captured_at,
            "reason": "30m OHLCV was unavailable immediately after the paper entry; the position remains open and the failure is explicit.",
        }

    snapshot_id = f"entry_{uuid.uuid4().hex[:24]}"
    directory = Path(output_dir) if output_dir is not None else ENTRY_CHART_DIR
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{snapshot_id}.svg"
    temporary = directory / f".{snapshot_id}.tmp"
    svg = _render_svg(
        normalized,
        symbol=symbol,
        direction=direction,
        timeframe=timeframe,
        entry=_f(position.get("entry_price")),
        stop=_f(position.get("stop_loss_price")),
        target=_f(position.get("take_profit_price")),
        strategy_evidence=strategy_evidence,
        captured_at=captured_at,
    )
    temporary.write_text(svg, encoding="utf-8")
    os.replace(temporary, target)
    return {
        "available": True,
        "capture_status": "captured",
        "snapshot_id": snapshot_id,
        "url": f"/api/entry-chart/{snapshot_id}",
        "symbol": symbol,
        "direction": direction,
        "timeframe": timeframe,
        "minimum_timeframe": "30m",
        "captured_at": captured_at,
        "market_data_as_of": _timestamp_label(normalized.index[-1]),
        "bar_count": int(len(normalized)),
        "source": frame_source,
        "sha256": hashlib.sha256(svg.encode("utf-8")).hexdigest(),
        "immutable": True,
        "lookahead_safe": True,
    }


def resolve_entry_chart_path(snapshot_id: str, *, output_dir: Optional[Path] = None) -> Optional[Path]:
    token = str(snapshot_id or "").strip().lower()
    if not _SNAPSHOT_ID.fullmatch(token):
        return None
    directory = Path(output_dir) if output_dir is not None else ENTRY_CHART_DIR
    path = directory / f"{token}.svg"
    return path if path.is_file() else None
