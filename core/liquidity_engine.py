"""Deterministic liquidity-pool and sweep detection on confirmed pivots."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

import pandas as pd

from core.market_structure import MarketStructureEngine, Pivot


@dataclass(frozen=True)
class LiquidityPool:
    id: str
    side: str
    level: float
    lower: float
    upper: float
    touches: int
    first_pivot_index: int
    last_pivot_index: int
    confirmed_index: int = 0


@dataclass(frozen=True)
class LiquidityEvent:
    index: int
    kind: str
    direction: str
    pool_id: str
    level: float
    timestamp: Optional[str] = None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if pd.notna(result) else default
    except (TypeError, ValueError):
        return default


def build_liquidity_pools(
    pivots: list[Pivot],
    atr: float,
    tolerance_bps: float = 12.0,
    atr_tolerance: float = 0.15,
    min_touches: int = 2,
) -> list[LiquidityPool]:
    """Cluster confirmed pivot prices into equal-high/equal-low liquidity zones."""
    pools: list[LiquidityPool] = []
    for kind, side in (("HIGH", "BUY_SIDE"), ("LOW", "SELL_SIDE")):
        clusters: list[list[Pivot]] = []
        for pivot in sorted((p for p in pivots if p.kind == kind), key=lambda p: p.index):
            tolerance = max(abs(pivot.price) * tolerance_bps / 10_000.0, max(atr, 0.0) * atr_tolerance)
            match = next(
                (
                    cluster
                    for cluster in clusters
                    if abs(pivot.price - sum(item.price for item in cluster) / len(cluster)) <= tolerance
                ),
                None,
            )
            if match is None:
                clusters.append([pivot])
            else:
                match.append(pivot)
        for cluster in clusters:
            if len(cluster) < min_touches:
                continue
            level = sum(item.price for item in cluster) / len(cluster)
            tolerance = max(abs(level) * tolerance_bps / 10_000.0, max(atr, 0.0) * atr_tolerance)
            pools.append(
                LiquidityPool(
                    id=f"{side}:{cluster[0].index}:{cluster[-1].index}",
                    side=side,
                    level=level,
                    lower=level - tolerance,
                    upper=level + tolerance,
                    touches=len(cluster),
                    first_pivot_index=cluster[0].index,
                    last_pivot_index=cluster[-1].index,
                    confirmed_index=max(item.confirmed_index for item in cluster),
                )
            )
    return sorted(pools, key=lambda item: (item.last_pivot_index, item.level))


class LiquidityEngine:
    def __init__(
        self,
        left_bars: int = 3,
        right_bars: int = 3,
        tolerance_bps: float = 12.0,
        atr_tolerance: float = 0.15,
    ) -> None:
        self.structure = MarketStructureEngine(left_bars, right_bars)
        self.tolerance_bps = float(tolerance_bps)
        self.atr_tolerance = float(atr_tolerance)
        self.events: list[LiquidityEvent] = []
        self.pools: list[LiquidityPool] = []
        self._swept_pool_ids: set[str] = set()
        self._previous_close: Optional[float] = None

    def update(
        self,
        high: Any,
        low: Any,
        close: Any,
        atr: Any = 0.0,
        timestamp: Any = None,
    ) -> dict[str, Any]:
        index = len(self.structure._closes)
        high_f, low_f, close_f = _number(high), _number(low), _number(close)
        self.structure.update(high_f, low_f, close_f, timestamp=timestamp)
        self.pools = build_liquidity_pools(
            self.structure.pivots,
            _number(atr),
            tolerance_bps=self.tolerance_bps,
            atr_tolerance=self.atr_tolerance,
        )
        event = self._detect_sweep(index, high_f, low_f, close_f, timestamp)
        self._previous_close = close_f
        return self.snapshot(close_f, event)

    def _detect_sweep(
        self, index: int, high: float, low: float, close: float, timestamp: Any
    ) -> Optional[LiquidityEvent]:
        # Only pools confirmed before the current candle are eligible.
        eligible = [p for p in self.pools if p.id not in self._swept_pool_ids and p.last_pivot_index < index]
        buy_sweeps = [p for p in eligible if p.side == "BUY_SIDE" and high > p.upper and close < p.level]
        sell_sweeps = [p for p in eligible if p.side == "SELL_SIDE" and low < p.lower and close > p.level]
        pool: Optional[LiquidityPool] = None
        direction = ""
        if buy_sweeps:
            pool = max(buy_sweeps, key=lambda item: item.level)
            direction = "BEARISH"
        elif sell_sweeps:
            pool = min(sell_sweeps, key=lambda item: item.level)
            direction = "BULLISH"
        if pool is None:
            return None
        value = timestamp.isoformat() if hasattr(timestamp, "isoformat") else (str(timestamp) if timestamp is not None else str(index))
        event = LiquidityEvent(index, "SWEEP_RECLAIM", direction, pool.id, pool.level, value)
        self.events.append(event)
        self._swept_pool_ids.add(pool.id)
        return event

    def snapshot(self, price: float, event: Optional[LiquidityEvent] = None) -> dict[str, Any]:
        buy_levels = [pool.level for pool in self.pools if pool.side == "BUY_SIDE" and pool.level >= price]
        sell_levels = [pool.level for pool in self.pools if pool.side == "SELL_SIDE" and pool.level <= price]
        last_event = self.events[-1] if self.events else None
        event_age = (len(self.structure._closes) - 1 - last_event.index) if last_event else None
        return {
            "buy_side_liquidity": min(buy_levels) if buy_levels else None,
            "sell_side_liquidity": max(sell_levels) if sell_levels else None,
            "buy_side_pool_count": sum(pool.side == "BUY_SIDE" for pool in self.pools),
            "sell_side_pool_count": sum(pool.side == "SELL_SIDE" for pool in self.pools),
            "liquidity_event": event.kind if event else None,
            "liquidity_event_direction": event.direction if event else None,
            "liquidity_event_level": event.level if event else None,
            "liquidity_last_event": last_event.kind if last_event else None,
            "liquidity_last_event_direction": last_event.direction if last_event else None,
            "liquidity_event_age_bars": event_age,
        }

    def annotations(self, limit: int = 100) -> dict[str, list[dict[str, Any]]]:
        return {
            "liquidity_pools": [asdict(item) for item in self.pools[-limit:]],
            "liquidity_events": [asdict(item) for item in self.events[-limit:]],
        }


def precompute_liquidity(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Missing OHLC columns: {sorted(missing)}")
    atr = pd.to_numeric(frame.get("atr", pd.Series(0.0, index=frame.index)), errors="coerce").fillna(0.0)
    engine = LiquidityEngine()
    snapshots = [
        engine.update(row.high, row.low, row.close, atr.iloc[position], timestamp=index)
        for position, (index, row) in enumerate(frame[["high", "low", "close"]].iterrows())
    ]
    return pd.DataFrame(snapshots, index=frame.index)


def analyze_liquidity(frame: pd.DataFrame) -> dict[str, Any]:
    required = {"high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Missing OHLC columns: {sorted(missing)}")
    engine = LiquidityEngine()
    atr = pd.to_numeric(frame.get("atr", pd.Series(0.0, index=frame.index)), errors="coerce").fillna(0.0)
    latest: dict[str, Any] = {}
    for position, (index, row) in enumerate(frame[["high", "low", "close"]].iterrows()):
        latest = engine.update(row.high, row.low, row.close, atr.iloc[position], timestamp=index)
    return {**latest, **engine.annotations()}
