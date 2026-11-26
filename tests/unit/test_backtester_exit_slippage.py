"""tests/unit/test_backtester_exit_slippage.py
==============================================
Regression guard for medium-finding (M5): the backtester must apply exit
slippage symmetrically with entry slippage.

THE BUG (pre-fix)
-----------------
``core/strategy_backtester.py`` filled entries with 0.1% slippage
(``slip = entry * 0.001``) but filled exits at the EXACT tp/sl/trail/close
level -> asymmetric. Live closes always incur adverse slippage (sell-to-close
long fills lower, buy-to-cover short fills higher), so the backtest inflated
avg_win, shrank avg_loss magnitude, and overstated profit factor / net PnL
(backtest reported better-than-live performance, contra "axează-te pe backtest
... profitabili").

THE FIX
-------
Apply the same 0.001 factor at exit, against the close direction:
  long  -> exit_price -= exit_price * 0.001   (sell fills lower)
  short -> exit_price += exit_price * 0.001   (buy-to-cover fills higher)

THIS TEST
---------
Patches compute_signal_score to a controlled always-valid LONG scorer with
tp=0/sl=0 so the backtester uses the cfg tp_pct/sl_pct overrides, runs the real
backtest() on a smooth synthetic uptrend (TP hits before any retracement), and
asserts the recorded TP exit price equals tp * (1 - 0.001), i.e. slippage was
applied. Pre-fix exit == tp exactly.
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


def _synthetic_ohlc_df(n: int = 240):
    import pandas as pd
    idx = pd.date_range("2025-01-01 09:30", periods=n, freq="1min", tz="UTC", name="dt")
    t = pd.Series(range(n))
    close = 100.0 + 0.06 * t  # smooth uptrend -> TP (+5%) hits well within window
    df = pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close * 1.0015,
            "low": close * 0.9985,
            "close": close,
            "volume": pd.Series([10_000.0] * n),
        }
    )
    df.index = idx
    return df


def _always_valid_long(ctx, mode, profile, scoring_cfg, **kw):
    """Always-valid LONG with tp=0/sl=0 -> backtester uses pct overrides
    (fill*(1+tp_pct) / fill*(1-sl_pct)). Same score for every mode/profile so
    the mode/profile loops don't matter; the exit-fill math is what's under test."""
    return SimpleNamespace(
        is_valid=True, total_score=82.0, direction="LONG",
        mode=mode, profile=profile, tp=0.0, sl=0.0,
    )


def test_long_tp_exit_applies_adverse_slippage():
    import core.strategy_backtester as sb
    from core.strategy_backtester import backtest
    from core.config import ConfigManager

    cfg = ConfigManager().signal
    tp_pct = float(cfg.get("profit_target_pct") or 5.0) / 100.0

    df = _synthetic_ohlc_df(240)
    with patch.object(sb, "compute_signal_score", side_effect=_always_valid_long), \
         patch.object(sb, "allowed_modes_for_regime",
                      lambda regime, cfg_: ["PULLBACK", "BREAKOUT", "CONTRA"]), \
         patch.object(sb, "row_to_context",
                      lambda row: SimpleNamespace(market_regime="MIXED")), \
         patch.object(sb, "detect_market_regime",
                      lambda ctx, cfg_: ("MIXED", {})):
        res = backtest("SLIP-USD", days=2, warmup=5, df=df)
    assert "error" not in res, f"backtest errored: {res}"
    trades = res.get("trade_list") or res.get("trades") or []
    assert trades, "synthetic uptrend must yield >=1 TP trade under the patched scorer"

    tp_trades = [t for t in trades if t["reason"] == "take_profit"]
    assert tp_trades, (
        f"expected at least one take_profit exit; reasons seen: "
        f"{[t['reason'] for t in trades]}"
    )
    tr = tp_trades[0]
    fill = tr["entry"]
    # tp level (pre-slippage) = fill * (1 + tp_pct). Post-fix exit = tp * (1 - 0.001).
    expected_exit = fill * (1.0 + tp_pct) * (1.0 - 0.001)
    assert tr["exit"] == pytest.approx(expected_exit, rel=1e-3), (
        f"TP exit must apply 0.1% adverse slippage: expected {expected_exit:.6f} "
        f"(= fill*(1+tp_pct)*0.999), got {tr['exit']:.6f}. "
        "Pre-fix exit == fill*(1+tp_pct) exactly (no slippage)."
    )
    # And it must NOT equal the un-slipped tp (the pre-fix value).
    unslipped_tp = fill * (1.0 + tp_pct)
    assert tr["exit"] != pytest.approx(unslipped_tp, rel=1e-4)


def test_short_tp_exit_applies_adverse_slippage():
    """Symmetric: a short TP (buy-to-cover) fills HIGHER than the tp level."""
    import core.strategy_backtester as sb
    from core.strategy_backtester import backtest
    from core.config import ConfigManager

    cfg = ConfigManager().signal
    tp_pct = float(cfg.get("profit_target_pct") or 5.0) / 100.0

    def _always_valid_short(ctx, mode, profile, scoring_cfg, **kw):
        return SimpleNamespace(
            is_valid=True, total_score=82.0, direction="SHORT",
            mode=mode, profile=profile, tp=0.0, sl=0.0,
        )

    # Smooth DOWNTREND so a short TP (-5%) hits.
    import pandas as pd
    n = 240
    idx = pd.date_range("2025-01-01 09:30", periods=n, freq="1min", tz="UTC", name="dt")
    t = pd.Series(range(n))
    close = 100.0 - 0.06 * t
    df = pd.DataFrame(
        {"open": close.shift(1).fillna(close.iloc[0]), "high": close * 1.0015,
         "low": close * 0.9985, "close": close, "volume": pd.Series([10_000.0] * n)},
    )
    df.index = idx

    with patch.object(sb, "compute_signal_score", side_effect=_always_valid_short), \
         patch.object(sb, "allowed_modes_for_regime",
                      lambda regime, cfg_: ["PULLBACK", "BREAKOUT", "CONTRA"]), \
         patch.object(sb, "row_to_context",
                      lambda row: SimpleNamespace(market_regime="MIXED")), \
         patch.object(sb, "detect_market_regime",
                      lambda ctx, cfg_: ("MIXED", {})):
        res = backtest("SLIPS-USD", days=2, warmup=5, df=df)
    trades = res.get("trade_list") or res.get("trades") or []
    tp_trades = [t for t in trades if t["reason"] == "take_profit"]
    assert tp_trades, f"expected a short take_profit; reasons: {[t['reason'] for t in trades]}"
    tr = tp_trades[0]
    fill = tr["entry"]
    # short tp = fill*(1 - tp_pct); post-fix exit = tp * (1 + 0.001) (buy higher).
    expected_exit = fill * (1.0 - tp_pct) * (1.0 + 0.001)
    assert tr["exit"] == pytest.approx(expected_exit, rel=1e-3), (
        f"short TP exit must apply 0.1% adverse slippage: expected {expected_exit:.6f}, "
        f"got {tr['exit']:.6f}"
    )