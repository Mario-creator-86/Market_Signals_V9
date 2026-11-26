"""tests/unit/test_backtester_spike_cooldown_parity.py
========================================================
Regression guard for round-5 finding #1 (HIGH/ACTIVE): the replay backtester
must NOT apply the ``add_cooldown_columns`` spike-cooldown gate, because live
does not.

THE BUG (pre-fix)
-----------------
``core/strategy_backtester.build_dataframe`` calls ``prepare_1m_dataframe``
(``strategy_backtester.py:125``) which runs ``add_cooldown_columns``
(``scoring_system.py:1371``). That flags the spike bar + the next 15 bars
``cooldown_active=True`` whenever ``rsi > 78`` (or ``< 22``) AND
``dist_vwap_atr > 1.8``. ``compute_signal_score`` then early-returns
``is_valid=False`` on every cooldown-True bar (``scoring_system.py:995``) -- so
the backtest silently dropped post-spike candidates.

Live NEVER applies this gate: ``core/engine.py:2454`` constructs
``RowContext(..., cooldown_active=False, cooldown_bars_remaining=0)`` hardcoded,
and the live indicator path (``engine.py:1770-1794``) never calls
``prepare_1m_dataframe`` / ``add_cooldown_columns``. So live admits the post-spike
bars the backtest dropped -> the backtest's signal admission diverged from live,
biasing win-rate / expectancy / trade-count and every metric seeded from them.

The OTHER cooldown -- the per-ticker ``signal_cooldown_minutes`` lockout applied
post-close (``strategy_backtester.py:~420``, parity with live
``signals.can_open_new_signal_for_ticker``) -- is a SEPARATE mechanism and stays
in place. This test is only about the spike-cooldown column.

THE FIX
-------
``build_dataframe`` forces ``cooldown_active=False`` /
``cooldown_bars_remaining=0`` immediately after ``prepare_1m_dataframe`` (mirroring
live's hardcoded False and the golden parity test's own manual neutralization at
``test_backtest_parity.py:72-73``). ``dist_vwap_atr``/``dist_ema_atr`` (also added
by ``prepare_1m_dataframe``) are KEPT -- live consumes them
(``engine.py:2452-2453``).

Win-rate (CLAUDE.md §4): the fix ADDS back the post-spike candidates live already
takes -> backtest admission now matches live. Membership on the non-spike bars is
unchanged. Effect is honesty (backtest reports what live actually does), neutral
to positive on profitability (post-spike entries are evaluated on their real merit
instead of being dropped).
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import os
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")


def _ramping_base_df(n: int = 400):
    """Strong 1%/bar ramp -> RSI saturates >78 and close runs far above VWAP/EMA,
    so dist_vwap_atr >> 1.8 -> add_cooldown_columns WILL flag cooldown on many bars.
    Returns the df with the 1m indicators build_dataframe computes BEFORE
    prepare_1m_dataframe (rsi/macd/ema_20/atr/adx/vol_sma/vwap/trend_1m)."""
    from core.config import ConfigManager
    from core.ta import TechnicalAnalysis
    from core.signal_generation import calculate_vwap
    from core.scoring_system import calculate_atr

    cfg = ConfigManager().signal
    idx = pd.date_range("2025-01-01 09:30", periods=n, freq="1min", tz="UTC", name="dt")
    t = pd.Series(range(n), dtype=float)
    close = 100.0 * (1.01 ** t)  # 1%/bar -> RSI>78, dist_vwap_atr large
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
    df["rsi"] = TechnicalAnalysis.calc_rsi(df["close"], cfg["rsi_period"])
    macd, macd_sig, macd_hist = TechnicalAnalysis.calc_macd(
        df["close"], cfg["macd_fast"], cfg["macd_slow"], cfg["macd_signal"])
    df["macd"], df["macd_signal"], df["macd_hist"] = macd, macd_sig, macd_hist
    df["ema_20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["atr"] = calculate_atr(df, int(cfg.get("atr_period", 14)))
    df["adx"] = TechnicalAnalysis.calc_adx(df["high"], df["low"], df["close"], 14)
    df["vol_sma"] = TechnicalAnalysis.calc_sma(df["volume"], 20)
    df["vwap"] = calculate_vwap(df)
    df["trend_1m"] = df["macd_hist"].apply(lambda v: "BULL" if v > 0 else "BEAR")
    return df


def test_spike_cooldown_would_fire_on_ramping_synth_confirming_fix_is_meaningful():
    """Control: prepare_1m_dataframe (which runs add_cooldown_columns) on the
    ramping synth MUST flag some bars cooldown_active=True. This proves the
    build_dataframe neutralization is meaningful (not a no-op) -- without it the
    backtest WOULD drop post-spike candidates."""
    from core.config import ConfigManager
    from core.scoring_system import prepare_1m_dataframe

    cfg = ConfigManager().signal
    df = _ramping_base_df(400)
    prepared = prepare_1m_dataframe(df, atr_period=int(cfg.get("atr_period", 14)))
    assert "cooldown_active" in prepared.columns, "prepare_1m_dataframe must add cooldown_active"
    assert prepared["cooldown_active"].any(), (
        "ramping synth must trigger add_cooldown_columns (RSI>78 + dist_vwap_atr>1.8) -- "
        "if it does not, the build_dataframe neutralization is a no-op and the test is "
        "meaningless. Adjust the ramp steepness."
    )


def test_build_dataframe_neutralizes_spike_cooldown_for_live_parity(monkeypatch):
    """P4-round5: build_dataframe must force cooldown_active=False (mirror live
    engine.py:2454) so the backtest admits the post-spike candidates live takes.
    Patches _fetch_klines to return the ramping synth for 1m (None for HTF) and
    asserts the built df has cooldown_active all-False even though the raw synth
    triggers the spike-cooldown (proven by the control test above)."""
    import core.strategy_backtester as sb
    from core.strategy_backtester import build_dataframe

    base = _ramping_base_df(400)

    def fake_fetch(sym, interval, start_ms, end_ms):
        return base.copy() if interval == "1m" else None

    monkeypatch.setattr(sb, "_fetch_klines", fake_fetch)
    out = build_dataframe("SPIKE-USD", days=2)
    assert out is not None, "build_dataframe must return a df (synth has 400 bars > 300 min)"
    assert "cooldown_active" in out.columns
    assert not out["cooldown_active"].any(), (
        "build_dataframe must neutralize the spike-cooldown (mirror live): "
        "post-spike candidates must not be dropped in backtest. "
        "Pre-fix the ramping synth triggered add_cooldown_columns -> backtest "
        "dropped those bars while live (cooldown_active hardcoded False) admitted them."
    )
    # dist_vwap_atr/dist_ema_atr are KEPT (live consumes them) -- neutralization only
    # touches the cooldown columns, not the distance features.
    assert "dist_vwap_atr" in out.columns
    assert "dist_ema_atr" in out.columns
    # The distance features must carry real values (not zeroed by neutralization).
    assert out["dist_vwap_atr"].abs().sum() > 0