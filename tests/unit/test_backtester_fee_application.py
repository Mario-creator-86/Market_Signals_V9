"""tests/unit/test_backtester_fee_application.py
==============================================
Regression guards for the Standard-View backtester (``core/backtester.py``):

* F3 (dead fee parameters): exit slippage was never applied -- exits filled at
  the raw SL/TP level while the entry paid slippage, so avg_win was overstated
  and avg_loss magnitude understated. The exit-slippage application path must
  be LIVE (a user setting a fee sees it reflected) even though the defaults
  keep behavior honest.
* F1 (default-path parity): the gap-through exit fill lived only in
  ``strategy_backtester.py``; ``core/backtester.py`` filled every exit at the
  raw SL/TP level, diverging from the faithful path on any bar that gaps past
  the level. The default path now mirrors ``min(sl, open)`` / ``max(tp, open)``
  for longs (and the symmetric shorts) -- look-ahead-safe (uses the bar's open,
  known at open time).
* F2 (NaN-ATR fallback): a NaN/None/0 ATR must NOT propagate into SL/TP/PnL;
  the deterministic ``price * 0.02`` proxy keeps the ATR exit branch live.

These tests drive ``Backtester.run_backtest`` directly on synthetic dataframes
(no network). The synthetic warmups are constructed to actually trigger the
backtester's real signal rule (``rsi < oversold AND macd > signal AND hist > 0``
for BUY, symmetric for SHORT): a smooth uptrend saturates RSI at 99.9 (BUY gate
``rsi < 99`` fails -> zero signals), so a gentle rise with regular pullbacks is
used instead, scaled so the signal bar closes at a known price. The legacy
backtester has no inter-trade cooldown, so a signal-triggering warmup can yield
multiple trades; we assert on the deterministic first signal (bar 50), which is
the trade the scenario is built around.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import os
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")


# ---- shared fixtures ------------------------------------------------------- #

def _make_backtester(monkeypatch, signal_cfg_overrides=None):
    """Build a Backtester with a ConfigManager whose .signal dict is fully
    controlled by the test (so ATR multipliers / exit_mode / pcts are
    deterministic and never read the real config.json)."""
    import core.backtester as bt_mod

    base_signal = {
        "rsi_period": 14, "rsi_oversold": 99, "rsi_overbought": 1,
        "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
        "atr_period": 14,
        "profit_target_pct": 5.0, "stop_loss_pct": 2.0,
        "exit_mode": "FIXED_PCT",
        "atr_multiplier_tp": 2.0, "atr_multiplier_sl": 1.5,
        # Lenient variant thresholds: any hist >= -1.0 -> BUY variant C; any
        # hist <= 1.0 -> SHORT variant C. With oversold=99 / overbought=1 the
        # RSI gate is always open, so the signal rule reduces to MACD sign.
        "variant_a_macd_hist_min_pos": -1.0,
        "variant_b_macd_hist_min_pos": -1.0,
        "variant_c_macd_hist_min_pos": -1.0,
        "variant_a_macd_hist_min_neg": 1.0,
        "variant_b_macd_hist_min_neg": 1.0,
        "variant_c_macd_hist_min_neg": 1.0,
    }
    if signal_cfg_overrides:
        base_signal.update(signal_cfg_overrides)

    class _FakeCM:
        signal = base_signal
        signal_variants = {}
        backtest = {"max_tickers": 10}
        watchlist = {"yahoo": [], "binance": []}
        strategy_v2 = {
            "costs_by_asset": {
                "CRYPTO_FUTURES": {
                    "commission_bps": 25.0,
                    "entry_slippage_bps": 10.0,
                    "slippage_sl_bps": 10.0,
                    "slippage_tp_bps": 10.0,
                },
            },
        }

    return bt_mod, bt_mod.Backtester(_FakeCM())


def _up_dip_series(n=120, rise=0.3, dip=0.6, every=4, signal_bar=50, target=100.0):
    """Gentle rise with regular pullbacks -> BUY fires at ``signal_bar``; scaled
    so close[signal_bar] == target. RSI stays well under 99 (pullbacks keep
    losses in the rolling window) and MACD hist > 0 (rising)."""
    i = np.arange(n, dtype=float)
    close = target * ((1.0 + rise / 100.0) ** (i - signal_bar))
    close = close * (1 - dip / 100.0 * (np.mod(i, every) == 0))
    # rescale to hit target exactly at signal_bar
    close = close * (target / close[signal_bar])
    return close


def _down_dip_series(n=120, fall=0.3, rally=0.6, every=4, signal_bar=50, target=100.0):
    """Gentle fall with regular rallies -> SHORT fires at ``signal_bar``; scaled
    so close[signal_bar] == target."""
    i = np.arange(n, dtype=float)
    close = target * ((1.0 - fall / 100.0) ** (i - signal_bar))
    close = close * (1 + rally / 100.0 * (np.mod(i, every) == 0))
    close = close * (target / close[signal_bar])
    return close


def _df_from_close(close, atr=None):
    n = len(close)
    idx = pd.date_range("2025-01-01 09:30", periods=n, freq="1min", tz="UTC",
                        name="Datetime")
    s = pd.Series(close, index=idx)
    df = pd.DataFrame({
        "open": s.shift(1).fillna(s.iloc[0]),
        "high": s, "low": s, "close": s, "volume": 10000.0,
    }, index=idx)
    if atr is not None:
        df["atr"] = atr
    return df, idx


# --------------------------------------------------------------------------- #
# F3: exit slippage is applied (the dead fee path is now live)
# --------------------------------------------------------------------------- #
def test_backtester_applies_exit_slippage_on_long_tp(monkeypatch):
    """F3: a long TP exit must fill at tp * (1 - 0.001), not the raw tp level.
    Pre-fix the exit filled at the raw tp while entry paid slippage -> asymmetric
    and overstated avg_win. The 0.1% exit slippage matches strategy_backtester.py
    for default-path parity (F1)."""
    bt_mod, bt = _make_backtester(monkeypatch)
    df, _ = _df_from_close(_up_dip_series(120))
    res = bt.run_backtest("SLIP-USD", days=7, is_crypto=True, df=df)
    trades = res.get("signals", [])
    assert trades, "up_dip warmup must yield >=1 BUY trade"
    # The first signal (bar 50) is the deterministic one; it TP-exits as the
    # uptrend continues. Find the first WIN (TP) trade.
    win_trades = [t for t in trades if t.get("result") == "WIN"]
    assert win_trades, f"expected a WIN (TP) trade; results={[(t.get('result'), t.get('exit_price')) for t in trades]}"
    tr = win_trades[0]
    entry = tr["entry"]
    # exit_mode=FIXED_PCT, tp_pct=5% -> raw tp = entry*(1+0.05); no gap so
    # exit = max(tp, open) = tp, then -0.1% adverse slippage (long close = sell).
    raw_tp = entry * 1.05
    expected_exit = raw_tp * (1.0 - 0.001)
    assert tr["exit_price"] == pytest.approx(expected_exit, rel=1e-4), (
        f"long TP exit must apply 0.1% adverse slippage: expected {expected_exit:.6f} "
        f"(= entry*1.05*0.999), got {tr['exit_price']}. Pre-fix exit == raw tp."
    )
    assert tr["exit_price"] != pytest.approx(raw_tp, rel=1e-4)


def test_backtester_applies_exit_slippage_on_long_sl(monkeypatch):
    """F3 mirror on the SL side: a SHORT SL exit (price rises above entry)
    fills at sl * (1 + 0.001). The stop-loss exit also pays exit slippage
    (buy-to-cover fills higher)."""
    bt_mod, bt = _make_backtester(monkeypatch, {"profit_target_pct": 50.0,
                                                 "stop_loss_pct": 1.0})
    # down_dip warmup -> SHORT fires at bar 50 (entry ~= 100 + slippage). Then
    # bar 51 rises so the SHORT's SL (entry*1.01) is hit intrabar (open < sl,
    # high >= sl) -> fills at the SL level (no gap), +0.1% slippage.
    close = _down_dip_series(120)
    df, idx = _df_from_close(close)
    df.loc[idx[51], "open"] = 100.5
    df.loc[idx[51], "high"] = 102.0
    df.loc[idx[51], "low"] = 100.0
    df.loc[idx[51], "close"] = 101.5
    # bars 52+ smooth uptrend from 101.5 (dead zone: RSI saturates -> no further
    # same-direction signals interfere with the SL exit being the first LOSS).
    for k in range(52, 120):
        df.loc[idx[k], "close"] = 101.5 * (1.005 ** (k - 51))
        df.loc[idx[k], "high"] = df.loc[idx[k], "close"]
        df.loc[idx[k], "low"] = df.loc[idx[k], "close"]
        df.loc[idx[k], "open"] = df.loc[idx[k - 1], "close"]
    res = bt.run_backtest("SLIPS-USD", days=7, is_crypto=True, df=df)
    trades = res.get("signals", [])
    assert trades, "down_dip warmup must yield >=1 SHORT trade"
    loss_trades = [t for t in trades if t.get("result") == "LOSS"]
    assert loss_trades, f"expected a LOSS (SL) trade; results={[(t.get('result'), t.get('exit_price')) for t in trades]}"
    tr = loss_trades[0]
    entry = tr["entry"]
    raw_sl = entry * (1.0 + 0.01)  # short sl_pct=1% above entry
    expected_exit = raw_sl * (1.0 + 0.001)  # short close = buy-to-cover -> higher
    assert tr["exit_price"] == pytest.approx(expected_exit, rel=1e-4), (
        f"short SL exit must apply 0.1% adverse slippage: expected {expected_exit:.6f} "
        f"(= entry*1.01*1.001), got {tr['exit_price']}"
    )


# --------------------------------------------------------------------------- #
# F1: gap-through exit fill at the open (parity with strategy_backtester M2)
# --------------------------------------------------------------------------- #
def test_backtester_gap_down_below_sl_fills_at_open(monkeypatch):
    """F1: a bar that OPENS below the SL fills the stop at the (worse) open,
    not the SL level. Pre-fix every stop filled at `sl` (optimistic on gap-downs
    -> smaller losses -> inflated win-rate vs the live gap). Parity with
    strategy_backtester.py M2."""
    bt_mod, bt = _make_backtester(monkeypatch, {"profit_target_pct": 20.0,
                                                 "stop_loss_pct": 2.0})
    close = _up_dip_series(120)  # BUY fires at bar 50, entry ~= 100 + slippage
    df, idx = _df_from_close(close)
    gap_open = 90.0
    df.loc[idx[51], "open"] = gap_open
    df.loc[idx[51], "high"] = gap_open + 0.5
    df.loc[idx[51], "low"] = gap_open - 0.5
    df.loc[idx[51], "close"] = gap_open
    # bars 52+ smooth uptrend from the gap price (dead zone). The legacy
    # backtester has no cooldown so extra trades may follow; the FIRST signal
    # (bar 50 BUY) is the deterministic gap scenario -- assert on it.
    for k in range(52, 120):
        df.loc[idx[k], "close"] = gap_open * (1.005 ** (k - 51))
        df.loc[idx[k], "high"] = df.loc[idx[k], "close"]
        df.loc[idx[k], "low"] = df.loc[idx[k], "close"]
        df.loc[idx[k], "open"] = df.loc[idx[k - 1], "close"]
    res = bt.run_backtest("GAP-USD", days=7, is_crypto=False, df=df)
    trades = res.get("signals", [])
    assert trades, "up_dip warmup must yield the bar-50 BUY before the gap"
    tr = trades[0]  # deterministic first signal = the gap-down trade
    assert tr["result"] == "LOSS", f"gap-down must be a LOSS; got {tr['result']}"
    entry = tr["entry"]
    sl_level = entry * (1.0 - 0.02)
    # F1: exit = min(sl, open) = 90 (open below sl), then F3 long exit slippage.
    expected_exit = min(sl_level, gap_open) * (1.0 - 0.001)
    pre_fix_exit = sl_level * (1.0 - 0.001)
    assert tr["exit_price"] == pytest.approx(expected_exit, rel=1e-4), (
        f"gap-down SL must fill at the open ({expected_exit:.4f}), not the SL "
        f"level ({pre_fix_exit:.4f}); got {tr['exit_price']}"
    )
    assert tr["exit_price"] < pre_fix_exit, (
        "gap-down fill must be WORSE than the SL level (live gap realism)"
    )


def test_backtester_gap_up_above_tp_fills_at_open(monkeypatch):
    """F1 mirror: a bar that OPENS above the TP fills the TP at the (better)
    open, not the TP level. Symmetric max(tp, open) for long TP."""
    bt_mod, bt = _make_backtester(monkeypatch, {"profit_target_pct": 5.0,
                                                 "stop_loss_pct": 20.0})
    close = _up_dip_series(120)
    df, idx = _df_from_close(close)
    gap_open = 108.0
    df.loc[idx[51], "open"] = gap_open
    df.loc[idx[51], "high"] = gap_open + 1.0
    df.loc[idx[51], "low"] = gap_open - 0.5
    df.loc[idx[51], "close"] = gap_open + 0.5
    for k in range(52, 120):
        df.loc[idx[k], "close"] = (gap_open + 0.5) * (1.005 ** (k - 51))
        df.loc[idx[k], "high"] = df.loc[idx[k], "close"]
        df.loc[idx[k], "low"] = df.loc[idx[k], "close"]
        df.loc[idx[k], "open"] = df.loc[idx[k - 1], "close"]
    res = bt.run_backtest("GAPU-USD", days=7, is_crypto=False, df=df)
    trades = res.get("signals", [])
    assert trades, "up_dip warmup must yield the bar-50 BUY before the gap"
    tr = trades[0]
    assert tr["result"] == "WIN", f"gap-up TP must be a WIN; got {tr['result']}"
    entry = tr["entry"]
    tp_level = entry * (1.0 + 0.05)
    expected_exit = max(tp_level, gap_open) * (1.0 - 0.001)  # long TP adverse slip
    pre_fix_exit = tp_level * (1.0 - 0.001)
    assert tr["exit_price"] == pytest.approx(expected_exit, rel=1e-4), (
        f"gap-up TP must fill at the open ({expected_exit:.4f}), not the TP "
        f"level ({pre_fix_exit:.4f}); got {tr['exit_price']}"
    )
    assert tr["exit_price"] > pre_fix_exit, "gap-up TP fill must be BETTER than the TP level"


# --------------------------------------------------------------------------- #
# F2: NaN/0 ATR falls back to a deterministic proxy (never propagates to SL/TP)
# --------------------------------------------------------------------------- #
# ``core/backtester.py`` recomputes df["atr"] via calculate_atr(df) at line 136,
# overwriting any atr column we set on the input df. The F2 fallback
# (`if not (atr_val > 0): atr_val = price * 0.02`) only fires when the recomputed
# ATR is NaN/0 at the signal bar -- which never happens on volatile (signal-
# triggering) data. To exercise the fallback we monkeypatch calculate_atr to
# return an all-NaN / all-0 series, keeping the OHLC that triggers the signal.
def test_backtester_nan_atr_falls_back_to_proxy_not_nan_sl_tp(monkeypatch):
    """F2: when ATR is NaN (forced via monkeypatch), the old ``if atr_val <= 0``
    check did NOT catch NaN (``NaN <= 0`` is False), so NaN propagated into SL/TP
    -> broken exits / NaN PnL. Post-fix the ATR branch uses a deterministic
    ``price * 0.02`` proxy so SL/TP are finite. exit_mode=ATR forces the branch.
    The proxy is derived from the signal bar's RAW close (price, before entry
    slippage) -- not from entry -- so we assert the proxy-distances from entry."""
    bt_mod, bt = _make_backtester(monkeypatch, {"exit_mode": "ATR"})
    close = _up_dip_series(120, target=100.0)  # close[50] == 100.0 exactly
    df, idx = _df_from_close(close)
    nan_atr = pd.Series(np.nan, index=idx)
    monkeypatch.setattr(bt_mod, "calculate_atr", lambda d, period=14: nan_atr)
    res = bt.run_backtest("NANATR-USD", days=7, is_crypto=False, df=df)
    trades = res.get("signals", [])
    assert trades, "NaN ATR must not suppress trades (proxy keeps the ATR branch live)"
    tr = trades[0]
    # SL/TP must be finite -- NaN propagation would make these NaN.
    assert tr["sl"] == tr["sl"], f"SL is NaN: {tr}"
    assert tr["tp"] == tr["tp"], f"TP is NaN: {tr}"
    assert tr["exit_price"] == tr["exit_price"], f"exit_price is NaN: {tr}"
    assert tr["net_pnl"] == tr["net_pnl"], f"net_pnl is NaN: {tr}"
    # proxy_atr = signal_bar_close * 0.02 = 100.0 * 0.02 = 2.0 (raw price, pre-
    # slippage). sl_mult=1.5, tp_mult=2.0. Distances from entry are the proxy
    # distances (independent of the entry slippage).
    proxy_atr = 100.0 * 0.02
    assert tr["entry"] - tr["sl"] == pytest.approx(proxy_atr * 1.5, rel=1e-3), (
        f"SL must use the price*0.02 proxy: expected entry-sl={proxy_atr*1.5:.6f}, "
        f"got {tr['entry'] - tr['sl']}"
    )
    assert tr["tp"] - tr["entry"] == pytest.approx(proxy_atr * 2.0, rel=1e-3), (
        f"TP must use the price*0.02 proxy: expected tp-entry={proxy_atr*2.0:.6f}, "
        f"got {tr['tp'] - tr['entry']}"
    )


def test_backtester_zero_atr_falls_back_to_proxy(monkeypatch):
    """F2: a 0 ATR (flat market) is caught by the same `not (x > 0)` guard and
    falls back to the proxy, so the ATR branch never divides by / sizes on 0."""
    bt_mod, bt = _make_backtester(monkeypatch, {"exit_mode": "ATR"})
    close = _up_dip_series(120, target=100.0)
    df, idx = _df_from_close(close)
    zero_atr = pd.Series(0.0, index=idx)
    monkeypatch.setattr(bt_mod, "calculate_atr", lambda d, period=14: zero_atr)
    res = bt.run_backtest("ZEROATR-USD", days=7, is_crypto=False, df=df)
    trades = res.get("signals", [])
    assert trades, "0 ATR must not suppress trades"
    tr = trades[0]
    assert tr["sl"] > 0 and tr["tp"] > 0, f"SL/TP must be positive: {tr}"
    assert tr["net_pnl"] == tr["net_pnl"], f"net_pnl is NaN: {tr}"
    proxy_atr = 100.0 * 0.02
    assert tr["entry"] - tr["sl"] == pytest.approx(proxy_atr * 1.5, rel=1e-3)
    assert tr["tp"] - tr["entry"] == pytest.approx(proxy_atr * 2.0, rel=1e-3)


def test_backtester_fixed_pct_tolerates_null_owner_fields(monkeypatch):
    bt_mod, bt = _make_backtester(monkeypatch, {
        "exit_mode": "FIXED_PCT",
        "profit_target_pct": None,
        "stop_loss_pct": None,
    })
    df, _ = _df_from_close(_up_dip_series(120))

    result = bt.run_backtest("NULLCFG-USD", days=7, is_crypto=True, df=df)

    assert "error" not in result
    assert result["signals"]
