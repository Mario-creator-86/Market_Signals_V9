"""
tests/unit/test_backtester_mode_loop_max.py
===========================================
Regression guard for high-finding (d): the backtester mode loop must evaluate
ALL allowed modes (PULLBACK/BREAKOUT/CONTRA) and pick the global max-total_score
candidate, not the first mode that yields any valid signal.

THE BUG (pre-fix)
-----------------
``core/strategy_backtester.py`` had an early break in the outer mode loop::

    best = None
    for mode in modes:
        for profile in profiles:
            res = compute_signal_score(ctx, mode, profile, scoring_cfg)
            if res.is_valid and (best is None or res.total_score > best.total_score):
                best = res
        if best is not None:
            break   # <-- dropped BREAKOUT/CONTRA even if they'd score higher

Because ``best`` is set inside the inner profile loop, the first mode that yields
ANY valid signal (PULLBACK, the iteration head) makes ``best`` non-None, so the
outer loop broke before scoring BREAKOUT/CONTRA. Selection was
first-by-iteration-order, not max-across-modes — biasing backtest to PULLBACK
and understating achievable performance (contra "axează-te foarte mult pe
backtest … profitabili").

THE FIX
-------
Delete the early break; the existing ``> best.total_score`` check then selects
the true global max across all modes and profiles.

THIS TEST
---------
Patches ``compute_signal_score`` so PULLBACK scores 70, BREAKOUT scores 85,
CONTRA is invalid — then runs the real ``backtest()``. Post-fix every trade is
BREAKOUT (85 > 70, the global max); pre-fix every trade was PULLBACK (first
mode, break). Asserts the recorded mode/score reflect the max, not the order.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os_env = __import__("os").environ
os_env.setdefault("TEST_MODE", "1")
os_env.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")


def _synthetic_ohlc_df(n: int = 220) -> pd.DataFrame:
    """Minimal OHLCV df (smooth uptrend) with a 'dt'-named datetime index so
    backtest's reset_index produces the 'dt' column entry_time expects. No
    indicator columns needed — row_to_context / detect_market_regime are
    patched in the test."""
    idx = pd.date_range("2025-01-01 09:30", periods=n, freq="1min", tz="UTC", name="dt")
    t = pd.Series(range(n))
    close = 100.0 + 0.05 * t  # smooth uptrend -> LONG TP hits reliably
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


def _fake_score(ctx, mode, profile, scoring_cfg, **kw):
    """Controlled per-mode scorer: PULLBACK=70, BREAKOUT=85 (the global max),
    CONTRA invalid. Same result for every profile so the inner profile loop
    only re-confirms best; the OUTER mode loop is what's under test."""
    if mode == "CONTRA":
        return SimpleNamespace(is_valid=False, total_score=0, direction="LONG",
                               mode=mode, profile=profile, tp=0.0, sl=0.0)
    score = 70.0 if mode == "PULLBACK" else 85.0
    return SimpleNamespace(
        is_valid=True, total_score=score, direction="LONG",
        mode=mode, profile=profile, tp=0.0, sl=0.0,
    )


@pytest.fixture
def _patched_backtest():
    import core.strategy_backtester as sb
    df = _synthetic_ohlc_df()
    with patch.object(sb, "compute_signal_score", side_effect=_fake_score), \
         patch.object(sb, "allowed_modes_for_regime",
                      lambda regime, cfg: ["PULLBACK", "BREAKOUT", "CONTRA"]), \
         patch.object(sb, "row_to_context",
                      lambda row: SimpleNamespace(market_regime="MIXED")), \
         patch.object(sb, "detect_market_regime",
                      lambda ctx, cfg: ("MIXED", {})):
        yield df


class TestBacktesterModeLoopSelectsGlobalMax:
    """High-finding (d): the outer mode loop must NOT early-break — it must
    evaluate every allowed mode and pick the max-total_score candidate."""

    def test_breakout_chosen_when_it_outscores_pullback(self, _patched_backtest):
        from core.strategy_backtester import backtest
        df = _patched_backtest
        res = backtest("TEST-USD", days=2, warmup=5, df=df)
        assert "error" not in res, f"backtest errored: {res}"
        trades = res.get("trade_list") or res.get("trades") or []
        # Determine the trade-list key the summary actually uses.
        if not trades:
            # Some summaries nest trades under a different key; fall back to
            # the raw list on the returned dict.
            trades = res.get("trade_list", [])
        assert trades, "synthetic uptrend must yield >=1 trade under the patched scorer"
        # BREAKOUT (85) > PULLBACK (70) -> every recorded trade must be BREAKOUT.
        # Pre-fix (early break after PULLBACK) every trade was PULLBACK.
        modes = [t["mode"] for t in trades]
        assert all(m == "BREAKOUT" for m in modes), (
            f"Mode loop did not reach BREAKOUT (the global max); recorded modes: {modes}. "
            "Pre-fix the outer `if best is not None: break` exited after PULLBACK."
        )
        assert trades[0]["score"] == pytest.approx(85.0, abs=0.1)

    def test_global_max_score_recorded_not_first_mode_score(self, _patched_backtest):
        from core.strategy_backtester import backtest
        df = _patched_backtest
        res = backtest("TEST-USD", days=2, warmup=5, df=df)
        trades = res.get("trade_list") or res.get("trades") or []
        assert trades
        # The recorded score is the max (85), never the first-mode score (70).
        for t in trades:
            assert t["score"] >= 85.0 - 0.1
            assert t["score"] != pytest.approx(70.0, abs=0.1)