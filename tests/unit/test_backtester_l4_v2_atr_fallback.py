"""tests/unit/test_backtester_l4_v2_atr_fallback.py
====================================================
Regression guard for L4 (Runda-5): when a V2 candidate is active (mode
"V2:S1".."V2:S6") but supplies no tp/sl (tp1=0/sl=0), the backtester's ATR
exit branch must fall back to the PULLBACK per-mode ATR multipliers, NOT the
global atr_multiplier_tp/sl defaults.

THE BUG (pre-fix)
-----------------
`atr_mult_by_mode.get(str(best.mode).upper(), {})` keyed on the V1 mode names
(PULLBACK/BREAKOUT/CONTRA). A V2 candidate's `best.mode` is "V2:S1" (built at
strategy_backtester.py:332), which never matches -> `mc = {}` -> the global
atr_mult_tp/sl defaults were used. V2 S1 TrendPullback IS a pullback; when the
config overrides PULLBACK's ATR multipliers away from the globals, V2 trades
silently used the WRONG (non-PULLBACK) ATR distances, diverging from intent.

THE FIX
-------
Map a "V2:*" mode that misses in atr_mult_by_mode to the PULLBACK base (the
conservative default). Win-rate-neutral when PULLBACK isn't separately
configured (mc={} -> global defaults, unchanged).
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")


def _synth_df(n=80, atr_5m=2.0):
    import pandas as pd
    idx = pd.date_range("2025-01-01 09:30", periods=n, freq="1min", tz="UTC", name="dt")
    t = pd.Series(range(n), dtype=float)
    close = 100.0 * (1.005 ** t)  # ~0.5%/bar uptrend -> TP reached, SL not
    df = pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close, "low": close, "close": close,
        "volume": pd.Series([10_000.0] * n),
        "atr_5m": float(atr_5m),
    })
    df.index = idx
    return df


def _always_valid_long(ctx, mode, profile, scoring_cfg, **kw):
    return SimpleNamespace(is_valid=True, total_score=82.0, direction="LONG",
                           mode=mode, profile=profile, tp=0.0, sl=0.0)


class _FakeSelector:
    """Returns a V2 S1 candidate with tp1=0/sl=0 so the backtester's ATR
    exit branch runs (the L4 path). total_score=85 >= min_edge_score=75 so the
    V2 override fires and best.mode becomes "V2:S1"."""
    def __init__(self, *a, **k):
        pass

    def select(self, ticker, asset_class, ctx):
        return {"best_candidate": {
            "total_score": 85, "direction": "LONG",
            "strategy_id": "S1", "tp1": 0.0, "sl": 0.0,
        }}


def _fake_cm_v2():
    base_signal = {
        "risk_mode": "MEDIUM", "regime": {}, "scoring": None,
        "min_candles_between_signals": 10_000,  # single trade
        "profit_target_pct": 5.0, "stop_loss_pct": 2.0,
        "enable_regime_gating": True, "signal_cooldown_minutes": 0,
        "exit_mode": "ATR",
        "atr_multiplier_tp": 2.0, "atr_multiplier_sl": 1.5,  # globals
        # PULLBACK override DISTINCT from globals -> exposes the L4 bug
        "atr_multipliers_by_mode": {"PULLBACK": {"tp": 3.0, "sl": 1.5}},
        "min_tp_pct": 0.0,
    }

    class _FakeCM:
        signal = base_signal
        bot = {"trailing_enabled": False, "trailing_activation_r": 1.0,
               "trailing_factor": 0.5, "min_edge_score": 75}
        strategy_v2 = {
            "signal_mode": "BOTH_AUTO_BEST",
            "costs_by_asset": {
                "CRYPTO_FUTURES": {
                    "commission_bps": 25,
                    "entry_slippage_bps": 10,
                    "slippage_sl_bps": 10,
                    "slippage_tp_bps": 10,
                }
            },
        }
    return _FakeCM


def test_l4_v2_atr_fallback_uses_pullback_mults_not_globals():
    import core.strategy_backtester as sb
    from core.strategy_backtester import backtest
    df = _synth_df(n=80, atr_5m=2.0)
    with patch("core.config.ConfigManager", _fake_cm_v2), \
         patch.object(sb, "compute_signal_score", side_effect=_always_valid_long), \
         patch.object(sb, "allowed_modes_for_regime",
                      lambda r, c: ["PULLBACK", "BREAKOUT", "CONTRA"]), \
         patch.object(sb, "row_to_context",
                      lambda row: SimpleNamespace(market_regime="MIXED")), \
         patch.object(sb, "detect_market_regime", lambda ctx, c: ("MIXED", {})), \
         patch("core.strategy_v2.selector.StrategySelector", _FakeSelector):
        res = backtest("V2L4-USD", days=2, warmup=5, df=df)
    assert "error" not in res, f"backtest errored: {res}"
    trades = res.get("trade_list") or res.get("trades") or []
    assert trades, "expected >=1 trade"
    tp_trades = [t for t in trades if t["reason"] == "take_profit"]
    assert tp_trades, (
        f"expected a take_profit exit; reasons={[t['reason'] for t in trades]}"
    )
    tr = tp_trades[0]
    # Prove the V2 override fired (the L4 path is only reachable for V2 candidates).
    assert tr.get("strategy_source") == "V2", (
        f"expected strategy_source=V2; got {tr.get('strategy_source')!r}"
    )
    fill = tr["entry"]
    atr_5m = 2.0
    # L4 fix: V2:S1 -> PULLBACK mults (tp=3.0).
    expected_tp_level = fill + atr_5m * 3.0
    expected_exit = expected_tp_level * (1.0 - 0.001)  # long TP adverse slippage
    # Pre-fix bug: global default tp=2.0.
    pre_fix_exit = (fill + atr_5m * 2.0) * (1.0 - 0.001)
    assert tr["exit"] == pytest.approx(expected_exit, rel=1e-3), (
        f"L4: V2 ATR fallback must use PULLBACK mults (tp=3.0 -> {expected_exit:.4f}), "
        f"not the global default (tp=2.0 -> {pre_fix_exit:.4f}); got {tr['exit']:.6f}"
    )
    assert tr["exit"] != pytest.approx(pre_fix_exit, abs=1e-3), (
        "L4: V2 ATR fallback collapsed to the global default (the pre-fix bug)"
    )


def test_l4_v2_atr_fallback_global_default_when_pullback_not_configured():
    """When PULLBACK is NOT in atr_multipliers_by_mode, the V2->PULLBACK map
    finds nothing and falls through to the global defaults -- win-rate-neutral
    vs pre-fix (the only behavior pre-fix ever had). Guards against over-eager
    mapping inventing a multiplier that isn't configured."""
    import core.strategy_backtester as sb
    from core.strategy_backtester import backtest
    df = _synth_df(n=80, atr_5m=2.0)

    class _FakeCMNoPullback:
        signal = {
            "risk_mode": "MEDIUM", "regime": {}, "scoring": None,
            "min_candles_between_signals": 10_000, "profit_target_pct": 5.0,
            "stop_loss_pct": 2.0, "enable_regime_gating": True,
            "signal_cooldown_minutes": 0, "exit_mode": "ATR",
            "atr_multiplier_tp": 2.5, "atr_multiplier_sl": 1.5,
            "atr_multipliers_by_mode": {},  # NO PULLBACK key
            "min_tp_pct": 0.0,
        }
        bot = {"trailing_enabled": False, "trailing_activation_r": 1.0,
               "trailing_factor": 0.5, "min_edge_score": 75}
        strategy_v2 = {
            "signal_mode": "BOTH_AUTO_BEST",
            "costs_by_asset": {
                "CRYPTO_FUTURES": {
                    "commission_bps": 25,
                    "entry_slippage_bps": 10,
                    "slippage_sl_bps": 10,
                    "slippage_tp_bps": 10,
                }
            },
        }

    with patch("core.config.ConfigManager", _FakeCMNoPullback), \
         patch.object(sb, "compute_signal_score", side_effect=_always_valid_long), \
         patch.object(sb, "allowed_modes_for_regime",
                      lambda r, c: ["PULLBACK", "BREAKOUT", "CONTRA"]), \
         patch.object(sb, "row_to_context",
                      lambda row: SimpleNamespace(market_regime="MIXED")), \
         patch.object(sb, "detect_market_regime", lambda ctx, c: ("MIXED", {})), \
         patch("core.strategy_v2.selector.StrategySelector", _FakeSelector):
        res = backtest("V2L4b-USD", days=2, warmup=5, df=df)
    trades = res.get("trade_list") or res.get("trades") or []
    tp_trades = [t for t in trades if t["reason"] == "take_profit"]
    assert tp_trades
    tr = tp_trades[0]
    fill = tr["entry"]
    # No PULLBACK override -> global default tp=2.5 applies (unchanged from pre-fix).
    expected_exit = (fill + 2.0 * 2.5) * (1.0 - 0.001)
    assert tr["exit"] == pytest.approx(expected_exit, rel=1e-3), (
        f"L4 neutral: with no PULLBACK override, V2 falls through to global "
        f"defaults ({expected_exit:.4f}); got {tr['exit']:.6f}"
    )
