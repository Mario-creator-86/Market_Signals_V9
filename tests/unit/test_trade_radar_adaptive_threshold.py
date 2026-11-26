"""tests/unit/test_trade_radar_adaptive_threshold.py
====================================================
Regression guards for the asset-class-adaptive Trade Radar move threshold
(display-only, win-rate-neutral). A flat ``threshold_pct`` over-reports
routine crypto pumps and under-reports meaningful stock moves; the
effective threshold is now per-symbol with config overrides.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
__import__("os").environ.setdefault("TEST_MODE", "1")
__import__("os").environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from agents.trade_radar_agent import TradeRadarAgent  # noqa: E402


def _eff(symbol: str, cfg: dict, base: float = 10.0) -> float:
    return TradeRadarAgent._effective_threshold(symbol, cfg, base)


# ─────────────────────────────────────────────────────────────────────────────
# Default behaviour (no overrides): crypto floor 15%, stock ceiling 5%,
# with the user's base as the bound.
# ─────────────────────────────────────────────────────────────────────────────

def test_crypto_default_is_15_when_base_lower():
    """Crypto with base=10 (the legacy default) -> 15% (filters routine
    noise pumps)."""
    assert _eff("ETH-USD", {}, base=10.0) == 15.0


def test_crypto_base_above_15_respected():
    """If the user raised threshold_pct to 20, crypto uses 20 (base is a
    floor for crypto, never lowered)."""
    assert _eff("ETH-USD", {}, base=20.0) == 20.0


def test_stock_default_is_5_when_base_higher():
    """Stock with base=10 -> 5% (a 5% stock move is meaningful; a flat 10%
    hid most stock setups)."""
    assert _eff("AAPL", {}, base=10.0) == 5.0


def test_stock_base_below_5_respected():
    """If the user lowered threshold_pct to 3, stock uses 3 (base is a
    ceiling for stocks, never raised)."""
    assert _eff("AAPL", {}, base=3.0) == 3.0


# ─────────────────────────────────────────────────────────────────────────────
# Config overrides take precedence and restore legacy flat behaviour
# ─────────────────────────────────────────────────────────────────────────────

def test_crypto_override_takes_precedence():
    assert _eff("ETH-USD", {"threshold_pct_crypto": 12.0}, base=10.0) == 12.0


def test_stock_override_takes_precedence():
    assert _eff("AAPL", {"threshold_pct_stock": 8.0}, base=10.0) == 8.0


def test_both_overrides_equal_base_restores_legacy_flat():
    """Setting both overrides equal to base restores the legacy flat
    threshold for both asset classes (backward-compat kill-switch)."""
    base = 10.0
    assert _eff("ETH-USD", {"threshold_pct_crypto": base, "threshold_pct_stock": base}, base=base) == base
    assert _eff("AAPL", {"threshold_pct_crypto": base, "threshold_pct_stock": base}, base=base) == base


def test_invalid_override_falls_back_to_adaptive_default():
    """A non-numeric / zero / negative override is ignored -> adaptive
    default applies (defensive, no crash)."""
    assert _eff("ETH-USD", {"threshold_pct_crypto": 0.0}, base=10.0) == 15.0
    assert _eff("ETH-USD", {"threshold_pct_crypto": "oops"}, base=10.0) == 15.0
    assert _eff("AAPL", {"threshold_pct_stock": -1.0}, base=10.0) == 5.0


def test_crypto_ticker_variants_detected():
    """The adaptive path relies on is_crypto_ticker; sanity-check a few
    ticker shapes that the scanner emits."""
    for sym in ("ETH-USD", "BTCUSDT", "SOL-USD", "DOGE-USD"):
        assert _eff(sym, {}, base=10.0) == 15.0, f"{sym} should be crypto-adaptive"
    for sym in ("AAPL", "MSFT", "TSLA"):
        assert _eff(sym, {}, base=10.0) == 5.0, f"{sym} should be stock-adaptive"