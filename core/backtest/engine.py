"""Stable backtest engine protocol and hard no-look-ahead guard."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol, runtime_checkable

from .types import BacktestConfig, BacktestResult, Trade


class NoLookAheadError(ValueError):
    """Raised when the supplied execution evidence permits a look-ahead fill."""


def assert_no_look_ahead(trades: Sequence[Trade], signal_bars: Iterable[int]) -> None:
    """Require every entry to occur strictly after an observed signal bar."""

    signals = sorted({int(bar) for bar in signal_bars})
    for trade in trades:
        if trade.entry_bar in signals:
            raise NoLookAheadError(
                f"same-bar fill at bar {trade.entry_bar}; entry must follow the signal"
            )
        if any(bar < trade.entry_bar for bar in signals):
            continue
        if signals:
            raise NoLookAheadError(
                f"trade entry at bar {trade.entry_bar} precedes its earliest signal at bar {signals[0]}"
            )
        raise NoLookAheadError(
            f"trade entry at bar {trade.entry_bar} has no signal evidence"
        )


@runtime_checkable
class BacktestEngine(Protocol):
    """Section 57 domain interface implemented structurally by adapters."""

    def run(self, config: BacktestConfig) -> BacktestResult:
        """Run an auditable, next-bar-open backtest."""
        ...
