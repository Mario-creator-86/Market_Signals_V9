"""tests/unit/test_backtester_atr_exit.py
==========================================
Regression guard for P4: the replay backtester must mirror the LIVE V1 exit-level
logic (core/engine.py:1806-1838 ``_calculate_exit_levels`` +
``_resolve_atr_multipliers`` 1798-1804), branching on ``exit_mode``:

  * ``FIXED_PCT`` -> flat ``entry*(1 +/- pct)`` (production config.json setting).
  * ``ATR``       -> ``tp = entry +/- max(atr*tp_mult, min_tp_dist)``,
                    ``sl = entry -/+ atr*sl_mult`` with per-mode multipliers from
                    ``atr_multipliers_by_mode`` and a ``min_tp_pct`` floor on TP only.

THE BUG (pre-fix, latent)
-------------------------
``core/strategy_backtester.py`` hardcoded V1 exits to flat ``fill*(1+/-pct)``
regardless of ``exit_mode``. Production config.json sets ``exit_mode=FIXED_PCT``,
so live == backtester == flat -> NO active divergence in production (latent). But
if ``exit_mode`` were flipped to ``ATR`` (the DEFAULT_CONFIG value), the backtester
would stay flat while live computed ATR levels -> replay would diverge from live
and ATR-mode could never be backtested. Perf_cache/validation_score is seeded by
``core/quick_backtester.py`` (already ATR-based) -- NOT by this backtester -- so the
fix does NOT cascade into validation_score (no re-seed needed).

THE FIX
-------
Read ``exit_mode`` + ATR multiplier keys from cfg and mirror ``_calculate_exit_levels``
exactly (FIXED_PCT branch + ATR branch with a safe flat fallback when ``atr <= 0``,
preserving the pre-fix behavior on data lacking ATR). ``build_dataframe`` now
persists the 5m ATR as ``atr_5m`` (mirroring the existing ``adx_5m``/``bb_width_5m``/
``atr_percentile_5m`` 5m metrics) so the ATR branch has its input.

Win-rate (CLAUDE.md §4): in production (FIXED_PCT) the branch is byte-identical to
pre-fix -> zero win-rate change. In ATR mode the fix ALIGNS backtest with live
(positive: ATR regime becomes backtestable / honest). No membership change on the
FIXED_PCT path; ATR path now reports real ATR-exit performance instead of flat-pct
fantasy.

THIS TEST
---------
Patches compute_signal_score to an always-valid LONG scorer with tp=0/sl=0 (so the
backtester computes exit levels from cfg, not from the candidate) and runs the real
``backtest()`` on a smooth, noise-free synthetic uptrend. Because the trade record
stores ``entry`` (post-slippage fill) and ``exit`` (post-slippage), each test recovers
the pre-slippage TP level as ``exit/0.999`` (long TP slippage = ``tp*(1-0.001)``) and
asserts it equals the expected ATR/floor/flat math -- proving the branch taken.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import os
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")


def _synthetic_uptrend_df(n: int = 80, atr_5m=None):
    """Noise-free compounding uptrend (~0.5%/bar). high=low=close so TP/SL only
    trigger at a bar's close (fully deterministic). When ``atr_5m`` is given the
    column is populated; otherwise it is absent (drives the flat-fallback path)."""
    import pandas as pd
    idx = pd.date_range("2025-01-01 09:30", periods=n, freq="1min", tz="UTC", name="dt")
    t = pd.Series(range(n), dtype=float)
    close = 100.0 * (1.005 ** t)  # ~0.5%/bar compounding
    df = pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close,
            "low": close,
            "close": close,
            "volume": pd.Series([10_000.0] * n),
        }
    )
    df.index = idx
    if atr_5m is not None:
        df["atr_5m"] = float(atr_5m)
    return df


def _always_valid_long(ctx, mode, profile, scoring_cfg, **kw):
    """Always-valid LONG with tp=0/sl=0 -> backtester computes exit levels from cfg.
    Same total_score for every mode/profile so PULLBACK (first iterated) wins and
    ``best.mode == "PULLBACK"`` -> atr_multipliers_by_mode["PULLBACK"] is the active
    per-mode multiplier set."""
    return SimpleNamespace(
        is_valid=True, total_score=82.0, direction="LONG",
        mode=mode, profile=profile, tp=0.0, sl=0.0,
    )


def _fake_cm(**signal_overrides):
    """Build a _FakeCM whose .signal carries the P4 exit keys. ``signal_overrides``
    lets each test set exit_mode / atr_multipliers_by_mode / min_tp_pct / etc."""
    base_signal = {
        "risk_mode": "MEDIUM", "regime": {}, "scoring": None,
        "min_candles_between_signals": 10_000,  # cap to a single trade
        "profit_target_pct": 5.0, "stop_loss_pct": 2.0,
        "enable_regime_gating": True,
        "signal_cooldown_minutes": 0,
        "exit_mode": "ATR",
        "atr_multiplier_tp": 2.0,
        "atr_multiplier_sl": 1.5,
        "atr_multipliers_by_mode": {"PULLBACK": {"tp": 3.0, "sl": 1.5}},
        "min_tp_pct": 0.0,
    }
    base_signal.update(signal_overrides)

    class _FakeCM:
        signal = base_signal
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


def _run_backtest(df, signal_overrides):
    import core.strategy_backtester as sb
    from core.strategy_backtester import backtest

    with patch("core.config.ConfigManager", _fake_cm(**signal_overrides)), \
         patch.object(sb, "compute_signal_score", side_effect=_always_valid_long), \
         patch.object(sb, "allowed_modes_for_regime",
                      lambda regime, cfg_: ["PULLBACK", "BREAKOUT", "CONTRA"]), \
         patch.object(sb, "row_to_context",
                      lambda row: SimpleNamespace(market_regime="MIXED")), \
         patch.object(sb, "detect_market_regime",
                      lambda ctx, cfg_: ("MIXED", {})):
        res = backtest("ATR-USD", days=2, warmup=5, df=df)
    assert "error" not in res, f"backtest errored: {res}"
    trades = res.get("trade_list") or res.get("trades") or []
    assert trades, "synthetic uptrend must yield >=1 trade under the patched scorer"
    return trades


def _first_tp_trade(trades):
    tp_trades = [t for t in trades if t["reason"] == "take_profit"]
    assert tp_trades, (
        f"expected a take_profit exit to read the TP level; reasons seen: "
        f"{[t['reason'] for t in trades]}"
    )
    return tp_trades[0]


# --------------------------------------------------------------------------- #
# 1. ATR branch: tp/sl = fill +/- atr_5m * per-mode multiplier (NOT flat pct)
# --------------------------------------------------------------------------- #
def test_backtester_v1_atr_exit_when_exit_mode_atr_and_atr_5m_present():
    """P4: exit_mode=ATR + a populated atr_5m column -> the TP level must be
    ``fill + atr_5m*tp_mult`` (per-mode), NOT the flat ``fill*(1+tp_pct)``.
    Pre-fix the backtester ignored exit_mode and always used flat pct."""
    atr_5m = 2.0
    df = _synthetic_uptrend_df(n=80, atr_5m=atr_5m)
    trades = _run_backtest(df, {"exit_mode": "ATR",
                                "atr_multipliers_by_mode": {"PULLBACK": {"tp": 3.0, "sl": 1.5}}})
    tr = _first_tp_trade(trades)
    fill = tr["entry"]
    m_tp = 3.0  # PULLBACK tp multiplier
    expected_tp_level = fill + atr_5m * m_tp  # min_tp_pct=0 -> floor = 0
    expected_exit = expected_tp_level * (1.0 - 0.001)  # long TP adverse slippage
    assert tr["exit"] == pytest.approx(expected_exit, rel=1e-3), (
        f"ATR exit must be fill+atr_5m*tp_mult: expected {expected_exit:.6f} "
        f"(= (fill+{atr_5m}*{m_tp})*0.999), got {tr['exit']:.6f}. "
        "Pre-fix exit == fill*(1+tp_pct)*0.999 (flat pct, ignoring ATR)."
    )
    # Must differ from the flat-pct level (proves the ATR branch was taken).
    flat_exit = fill * (1.0 + 0.05) * (1.0 - 0.001)
    assert tr["exit"] != pytest.approx(flat_exit, abs=1e-3), (
        f"ATR exit collapsed to flat pct: got {tr['exit']:.6f} ~ flat {flat_exit:.6f}"
    )


# --------------------------------------------------------------------------- #
# 2. ATR unavailable -> safe flat fallback (backward-compat, no crash/div-zero)
# --------------------------------------------------------------------------- #
def test_backtester_atr_unavailable_falls_back_to_flat_pct():
    """P4: exit_mode=ATR but no atr_5m (and no atr) column -> atr_val=0 -> the safe
    flat fallback must produce the flat ``fill*(1+tp_pct)`` level (the pre-fix
    behavior), without crashing or dividing by zero."""
    df = _synthetic_uptrend_df(n=80, atr_5m=None)  # NO atr_5m column
    trades = _run_backtest(df, {"exit_mode": "ATR"})
    tr = _first_tp_trade(trades)
    fill = tr["entry"]
    expected_exit = fill * (1.0 + 0.05) * (1.0 - 0.001)  # flat tp_pct=5%
    assert tr["exit"] == pytest.approx(expected_exit, rel=1e-3), (
        f"ATR-unavailable fallback must equal flat pct: expected {expected_exit:.6f}, "
        f"got {tr['exit']:.6f}. The fallback preserves pre-fix behavior on ATR-less data."
    )


# --------------------------------------------------------------------------- #
# 3. FIXED_PCT mode -> flat overrides (backward-compat with production)
# --------------------------------------------------------------------------- #
def test_backtester_fixed_pct_mode_uses_flat_overrides():
    """P4: exit_mode=FIXED_PCT -> flat ``fill*(1+tp_pct)`` EVEN WHEN atr_5m is
    populated (the ATR data must be ignored). This is the production path."""
    df = _synthetic_uptrend_df(n=80, atr_5m=2.0)  # atr_5m present but must be ignored
    trades = _run_backtest(df, {"exit_mode": "FIXED_PCT"})
    tr = _first_tp_trade(trades)
    fill = tr["entry"]
    expected_exit = fill * (1.0 + 0.05) * (1.0 - 0.001)  # flat, ATR ignored
    assert tr["exit"] == pytest.approx(expected_exit, rel=1e-3), (
        f"FIXED_PCT must use flat pct and ignore atr_5m: expected {expected_exit:.6f}, "
        f"got {tr['exit']:.6f}"
    )
    # And it must NOT equal the ATR level (proves ATR data was ignored).
    atr_exit = (fill + 2.0 * 3.0) * (1.0 - 0.001)
    assert tr["exit"] != pytest.approx(atr_exit, abs=1e-3), (
        f"FIXED_PCT leaked into ATR math: got {tr['exit']:.6f} ~ ATR {atr_exit:.6f}"
    )


# --------------------------------------------------------------------------- #
# 4. min_tp_pct floor: TP respects the minimum distance when ATR*mult is tiny
# --------------------------------------------------------------------------- #
def test_backtester_atr_respects_min_tp_pct_floor():
    """P4: when atr_5m*tp_mult < fill*min_tp_pct/100, the TP floor must win
    (mirror engine.py:1833-1835: ``tp = fill + max(tp_dist, min_tp_dist)``).
    With atr_5m=0.01, m_tp=3.0 -> tp_dist=0.03; min_tp_pct=0.5 -> min_tp_dist~0.5;
    so tp = fill + ~0.5 (the floor), NOT fill+0.03 (the raw ATR distance)."""
    df = _synthetic_uptrend_df(n=80, atr_5m=0.01)  # tiny ATR -> floor dominates TP
    trades = _run_backtest(df, {"exit_mode": "ATR",
                                "atr_multipliers_by_mode": {"PULLBACK": {"tp": 3.0, "sl": 1.5}},
                                "min_tp_pct": 0.5})
    tr = _first_tp_trade(trades)
    fill = tr["entry"]
    m_tp = 3.0
    atr_5m = 0.01
    tp_dist = atr_5m * m_tp  # 0.03
    min_tp_dist = fill * (0.5 / 100.0)  # ~0.5
    expected_tp_level = fill + max(tp_dist, min_tp_dist)  # floor wins -> fill + ~0.5
    expected_exit = expected_tp_level * (1.0 - 0.001)
    assert tr["exit"] == pytest.approx(expected_exit, rel=1e-3), (
        f"min_tp_pct floor must dominate when ATR distance is tiny: expected "
        f"{expected_exit:.6f} (= (fill+max({tp_dist},{min_tp_dist:.4f}))*0.999), "
        f"got {tr['exit']:.6f}. Pre-fix/unfloored would be (fill+{tp_dist})*0.999."
    )
    # TP level >= fill + fill*min_tp_pct/100 (the floor is a lower bound on TP).
    implied_tp = tr["exit"] / (1.0 - 0.001)
    assert implied_tp >= fill + fill * (0.5 / 100.0) - 1e-6, (
        f"TP must respect the min_tp_pct floor: implied_tp={implied_tp:.6f} < "
        f"fill+floor={fill + fill * 0.005:.6f}"
    )
    # And it must NOT equal the raw (unfloored) ATR distance (proves the floor applied).
    unfloored_exit = (fill + tp_dist) * (1.0 - 0.001)
    assert tr["exit"] != pytest.approx(unfloored_exit, abs=1e-3), (
        f"min_tp_pct floor not applied: got {tr['exit']:.6f} ~ unfloored {unfloored_exit:.6f}"
    )


# --------------------------------------------------------------------------- #
# L3 (Runda-5): NaN ATR must fall back to flat, NOT corrupt tp/sl -> time_exit
# --------------------------------------------------------------------------- #
def test_backtester_l3_nan_atr_falls_back_to_flat_not_time_exit():
    """L3: when atr_5m=0.0 (falsy) and the 1m `atr` is NaN (calculate_atr warmup,
    or any NaN), `atr_5m or atr or 0.0` returns NaN (NaN is truthy in Python).
    Pre-fix `if atr_val <= 0` did NOT catch NaN (NaN<=0 is False) -> the ATR
    branch ran with NaN -> tp/sl became NaN -> no exit level ever triggered
    (NaN comparisons are False) -> every such trade silently fell to time_exit
    at the last bar, understating the strategy. Post-fix `not (atr_val > 0)`
    catches NaN -> safe flat fallback -> normal take_profit."""
    import pandas as pd
    n = 80
    idx = pd.date_range("2025-01-01 09:30", periods=n, freq="1min", tz="UTC", name="dt")
    t = pd.Series(range(n), dtype=float)
    close = 100.0 * (1.005 ** t)
    df = pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close, "low": close, "close": close,
        "volume": pd.Series([10_000.0] * n),
        "atr_5m": 0.0,        # falsy -> `0.0 or atr` -> falls through to atr
        "atr": float("nan"),  # NaN -> atr_val = NaN (NaN is truthy)
    })
    df.index = idx
    trades = _run_backtest(df, {"exit_mode": "ATR"})
    reasons = [t["reason"] for t in trades]
    assert "time_exit" not in reasons, (
        f"L3: NaN atr corrupted tp/sl -> trade fell to time_exit (no level "
        f"triggered); reasons={reasons}. Expected flat-fallback take_profit."
    )
    assert any(r == "take_profit" for r in reasons), (
        f"flat fallback should produce a take_profit exit; reasons={reasons}"
    )
    for tr in trades:
        # exit must be finite (NaN-corrupted exits would be NaN or the last-bar close).
        assert tr["exit"] == tr["exit"], f"exit is NaN: {tr}"  # NaN != NaN -> falsy
        assert tr["exit"] > 0


# --------------------------------------------------------------------------- #
# M2 (Runda-5): gap-through exit fills at the OPEN, not the level
# --------------------------------------------------------------------------- #
def test_backtester_m2_gap_down_below_sl_fills_at_open_not_sl():
    """M2: a bar that OPENS below the SL (gap-down) must fill the stop at the
    (worse) open, not at the SL level. Pre-fix every stop filled at `sl`,
    which was optimistic on gap-downs (smaller losses -> inflated win-rate vs
    the 30% live vs 60% backtest gap, the profitability root cause). Live fills
    a gap-down open below the stop at the open; the backtest must mirror that.
    FIXED_PCT mode for deterministic levels; single trade via min_candles."""
    import pandas as pd
    n = 20
    idx = pd.date_range("2025-01-01 09:30", periods=n, freq="1min", tz="UTC", name="dt")
    # bars 0-5: flat at 100 (warmup=5 -> signal at bar 5, entry at bar 6 open=100)
    # bar 6: entry, intraday high/low do NOT trigger (high<tp, low>sl)
    # bar 7: gap-down open=90 (below SL=98.098) -> stop filled at open
    open_ = [100.0]*6 + [100.0, 90.0] + [90.0]*(n-8)
    high_ = [100.0]*5 + [100.0, 101.0, 90.5] + [90.5]*(n-8)
    low_ = [100.0]*5 + [100.0, 99.5, 89.5] + [89.5]*(n-8)
    close_ = [100.0]*5 + [100.0, 100.5, 90.0] + [90.0]*(n-8)
    df = pd.DataFrame({
        "open": open_, "high": high_, "low": low_, "close": close_,
        "volume": pd.Series([10_000.0] * n),
    })
    df.index = idx
    trades = _run_backtest(df, {"exit_mode": "FIXED_PCT",
                                "profit_target_pct": 5.0, "stop_loss_pct": 2.0,
                                "atr_multipliers_by_mode": {}})
    assert len(trades) == 1, f"expected a single trade, got {len(trades)}"
    tr = trades[0]
    assert tr["reason"] == "stop_loss", (
        f"gap-down bar must trigger a stop_loss; got reason={tr['reason']}"
    )
    fill = tr["entry"]  # 100 + 0.001 slippage = 100.1
    sl_level = fill * (1.0 - 0.02)  # 98.098
    gap_open = 90.0
    # Post-fix: exit = min(sl, open) = 90, then long exit slippage: 90 - 90*0.001.
    expected_exit = min(sl_level, gap_open) * (1.0 - 0.001)
    pre_fix_exit = sl_level * (1.0 - 0.001)  # what the bug used to produce
    assert tr["exit"] == pytest.approx(expected_exit, rel=1e-3), (
        f"M2: gap-down stop must fill at the open ({expected_exit:.4f}), not the SL "
        f"level ({pre_fix_exit:.4f}); got {tr['exit']:.6f}"
    )
    assert tr["exit"] < pre_fix_exit, (
        "gap-down fill must be WORSE than the SL level (live gap realism)"
    )


def test_backtester_m2_gap_up_above_tp_fills_at_open_not_tp():
    """M2 mirror: a bar that OPENS above the TP (gap-up) fills the TP at the
    (better) open, not at the TP level. Pre-fix filled at `tp` (pessimistic on
    gap-ups). Confirms the symmetric max(tp, open) for long TP."""
    import pandas as pd
    n = 20
    idx = pd.date_range("2025-01-01 09:30", periods=n, freq="1min", tz="UTC", name="dt")
    # entry at bar 6 open=100; bar 7 gap-UP open=112 (above TP=105.105)
    open_ = [100.0]*6 + [100.0, 112.0] + [112.0]*(n-8)
    high_ = [100.0]*5 + [100.0, 101.0, 113.0] + [113.0]*(n-8)
    low_ = [100.0]*5 + [100.0, 99.5, 111.5] + [111.5]*(n-8)
    close_ = [100.0]*5 + [100.0, 100.5, 112.5] + [112.5]*(n-8)
    df = pd.DataFrame({
        "open": open_, "high": high_, "low": low_, "close": close_,
        "volume": pd.Series([10_000.0] * n),
    })
    df.index = idx
    trades = _run_backtest(df, {"exit_mode": "FIXED_PCT",
                                "profit_target_pct": 5.0, "stop_loss_pct": 2.0,
                                "atr_multipliers_by_mode": {}})
    assert len(trades) == 1
    tr = trades[0]
    assert tr["reason"] == "take_profit", f"gap-up bar must trigger TP; got {tr['reason']}"
    fill = tr["entry"]  # 100.1
    tp_level = fill * (1.0 + 0.05)  # 105.105
    gap_open = 112.0
    expected_exit = max(tp_level, gap_open) * (1.0 - 0.001)  # long TP adverse slip
    pre_fix_exit = tp_level * (1.0 - 0.001)
    assert tr["exit"] == pytest.approx(expected_exit, rel=1e-3), (
        f"M2: gap-up TP must fill at the open ({expected_exit:.4f}), not the TP "
        f"level ({pre_fix_exit:.4f}); got {tr['exit']:.6f}"
    )
    assert tr["exit"] > pre_fix_exit, "gap-up TP fill must be BETTER than the TP level"
