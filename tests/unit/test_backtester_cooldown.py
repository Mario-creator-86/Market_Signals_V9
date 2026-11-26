"""tests/unit/test_backtester_cooldown.py
=========================================
Regression guard for round-2 finding (P2): the replay backtester must apply the
post-close per-ticker cooldown (parity with live
``signals.can_open_new_signal_for_ticker``, default ``signal_cooldown_minutes=15``).

THE BUG (pre-fix)
-----------------
After a trade closed, ``core/strategy_backtester.py`` set ``i = exit_idx + 1`` --
re-entering on the VERY NEXT bar. Live trading blocks re-entry on a ticker for N
real minutes after a close (the per-ticker cooldown lockout). So replay
overstated trade count / total_net_pct with quick re-entries that live filters
out -> backtest reported more (and faster) trades than live would ever take,
contra "axează-te foarte mult pe backtest ... profitabili".

THE FIX
-------
``i = exit_idx + 1 + cooldown_bars`` where ``cooldown_bars =
max(1, signal_cooldown_minutes // bar_minutes)`` (derived from the bar dt
spacing), mirroring the live lockout. ``cooldown_minutes=0`` preserves the old
``exit_idx + 1`` behavior.

Win-rate (CLAUDE.md §4): membership (WIN/LOSS) and per-trade R are unchanged --
only the COUNT and SPACING of re-entries changes. Pre-fix overstated trade
count and total_net_pct; post-fix matches live's lockout. Neutral-to-positive
on win-rate (same setups, fewer churn re-entries), positive on realism.

THIS TEST
---------
Patches compute_signal_score to an always-valid LONG scorer with tp=0/sl=0 (so
the backtester uses the cfg tp_pct/sl_pct overrides), disables trailing, and
sets ``min_candles_between_signals=1`` so the ONLY re-entry spacing is the
post-close cooldown. Runs the same smooth synthetic uptrend (1-min bars, ~0.5%/bar
so TP hits repeatedly within the window) twice -- with ``signal_cooldown_minutes=0``
(pre-fix behavior) and ``=15`` (post-fix lockout) -- and asserts:

  1. both runs yield >=2 TP trades,
  2. cooldown=15 yields STRICTLY FEWER trades than cooldown=0,
  3. with cooldown=15 every consecutive exit->next-entry gap is >= 15 min,
  4. with cooldown=0 at least one consecutive exit->next-entry gap is < 15 min
     (proving the pre-fix re-entered-next-bar behavior the fix replaces).
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import os
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")


def _synthetic_uptrend_df(n: int = 240):
    """1-min bars, ~0.5%/bar smooth uptrend -> a long +5% TP hits every ~10 bars,
    producing many re-entries within the window so cooldown's count effect shows."""
    idx = pd.date_range("2025-01-01 09:30", periods=n, freq="1min", tz="UTC", name="dt")
    t = pd.Series(range(n), dtype=float)
    close = 100.0 * (1.005 ** t)  # ~0.5%/bar compounding
    df = pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close * 1.002,
            "low": close * 0.998,
            "close": close,
            "volume": pd.Series([10_000.0] * n),
        }
    )
    df.index = idx
    return df


def _always_valid_long(ctx, mode, profile, scoring_cfg, **kw):
    return SimpleNamespace(
        is_valid=True, total_score=82.0, direction="LONG",
        mode=mode, profile=profile, tp=0.0, sl=0.0,
    )


def _fake_cm(cooldown_minutes: int):
    class _FakeCM:
        signal = {
            "risk_mode": "MEDIUM", "regime": {}, "scoring": None,
            "min_candles_between_signals": 1,  # isolate cooldown as the only spacing
            "profit_target_pct": 5.0, "stop_loss_pct": 2.0,
            "enable_regime_gating": True,
            "signal_cooldown_minutes": cooldown_minutes,
        }
        bot = {"trailing_enabled": False, "trailing_activation_r": 1.0,
               "trailing_factor": 0.5}
        strategy_v2 = {
            "signal_mode": "V1",
            "costs_by_asset": {
                "CRYPTO_FUTURES": {
                    "commission_bps": 25.0,
                    "entry_slippage_bps": 10.0,
                    "slippage_sl_bps": 10.0,
                    "slippage_tp_bps": 10.0,
                },
            },
        }
    return _FakeCM


def _run_backtest(cooldown_minutes: int):
    import core.strategy_backtester as sb
    from core.strategy_backtester import backtest

    df = _synthetic_uptrend_df(240)
    with patch("core.config.ConfigManager", _fake_cm(cooldown_minutes)), \
         patch.object(sb, "compute_signal_score", side_effect=_always_valid_long), \
         patch.object(sb, "allowed_modes_for_regime",
                      lambda regime, cfg_: ["PULLBACK", "BREAKOUT", "CONTRA"]), \
         patch.object(sb, "row_to_context",
                      lambda row: SimpleNamespace(market_regime="MIXED")), \
         patch.object(sb, "detect_market_regime",
                      lambda ctx, cfg_: ("MIXED", {})):
        res = backtest("CD-USD", days=2, warmup=5, df=df)
    assert "error" not in res, f"backtest errored: {res}"
    return res.get("trade_list") or res.get("trades") or []


def _exit_to_entry_gap_minutes(prev_trade, next_trade):
    """Gap between one trade's exit bar and the next trade's entry bar (minutes)."""
    exit_t = pd.Timestamp(prev_trade["exit_time"])
    entry_t = pd.Timestamp(next_trade["entry_time"])
    return (entry_t - exit_t).total_seconds() / 60.0


def test_backtest_cooldown_reduces_reentry_count_and_enforces_lockout():
    trades_no_cd = _run_backtest(cooldown_minutes=0)
    trades_cd = _run_backtest(cooldown_minutes=15)

    assert len(trades_no_cd) >= 2, (
        f"cooldown=0 run must yield >=2 re-entry trades to demonstrate the effect; "
        f"got {len(trades_no_cd)}"
    )
    # (2) cooldown=15 yields strictly fewer trades than cooldown=0.
    assert len(trades_cd) < len(trades_no_cd), (
        f"cooldown=15 must reduce re-entry count vs cooldown=0: "
        f"got cd15={len(trades_cd)} vs cd0={len(trades_no_cd)} (pre-fix replay "
        f"re-entered next bar, overstating trade count vs live's lockout)."
    )
    # (3) with cooldown=15, every consecutive exit->next-entry gap >= 15 min.
    for prev, nxt in zip(trades_cd, trades_cd[1:]):
        gap = _exit_to_entry_gap_minutes(prev, nxt)
        assert gap >= 15.0, (
            f"cooldown=15 violated: exit->next-entry gap={gap:.1f}min < 15min "
            f"(pre-fix re-entered on the next bar)."
        )
    # (4) with cooldown=0, at least one consecutive gap is < 15 min (the pre-fix
    # next-bar re-entry behavior the cooldown replaces).
    no_cd_gaps = [_exit_to_entry_gap_minutes(p, n)
                  for p, n in zip(trades_no_cd, trades_no_cd[1:])]
    assert any(g < 15.0 for g in no_cd_gaps), (
        f"cooldown=0 must show at least one <15min re-entry gap (pre-fix behavior); "
        f"gaps={no_cd_gaps}"
    )


def test_backtest_cooldown_zero_preserves_next_bar_reentry():
    """cooldown=0 keeps the legacy ``exit_idx + 1`` jump (no lockout) so the
    fix is backward-compatible when the cooldown is explicitly disabled."""
    trades = _run_backtest(cooldown_minutes=0)
    assert len(trades) >= 2
    # Every consecutive gap is the small next-bar jump (~2 min), none >= 15.
    for prev, nxt in zip(trades, trades[1:]):
        gap = _exit_to_entry_gap_minutes(prev, nxt)
        assert gap < 15.0, (
            f"cooldown=0 should not enforce a 15min lockout, but gap={gap:.1f}min"
        )
