"""
tests/unit/test_backtest_agent_summarize_c2.py
==============================================
Regression guard for C2 (Runda-5): ``BacktestAgent._summarize_directional``
must derive the R-multiple from fields the backtester ACTUALLY produces
(``entry``/``sl``/``qty``/``net_pnl``/``result``), not from ``r_multiple``/
``pnl_pct`` keys that ``core/backtester.py`` never emits.

THE BUG (pre-fix)
-----------------
``_summarize_directional`` read ``trade.get("r_multiple")`` (always None) then
fell back to ``pnl_pct/2.0`` using a key the backtester never sets -> 0.0 ->
every trade got r=0.0 -> ``r >= 0`` True for every trade -> ``win_rate=1.0``,
``profit_factor=0.0``, ``expectancy_R=0.0`` for EVERY V2 strategy. The research
agent (same cache key as feedback_loop) then overwrote live-accumulated
performance with these bogus metrics every ~12h -> validation_score=60 for
every mature strategy, disconnected from real performance, and live feedback
destroyed.

THE FIX
-------
Derive ``r_mult = net_pnl / (|entry - sl| * qty)`` (mirror
``QuickBacktester._compute_trade_kpis``) and classify win/loss via the
backtester's own ``result`` field ("WIN"/"LOSS"), falling back to ``r >= 0``
only when ``result`` is absent.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
__import__("os").environ.setdefault("TEST_MODE", "1")
__import__("os").environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from agents.backtest_agent import BacktestAgent  # noqa: E402


def _trade(entry: float, sl: float, qty: float, net_pnl: float, result: str, sig_type: str = "BUY"):
    """Build a signal dict matching core/backtester.py's emitted shape (entry,
    sl, qty, net_pnl, result, type) -- NO r_multiple, NO pnl_pct."""
    return {
        "type": sig_type,
        "entry": entry,
        "sl": sl,
        "tp": entry + (entry - sl) * 2,
        "qty": qty,
        "net_pnl": net_pnl,
        "gross_pnl": net_pnl,
        "commission": 0.0,
        "result": result,
    }


class TestSummarizeDirectionalDerivesRFromBacktesterFields:
    """C2: R must be derived from entry/sl/qty/net_pnl, not from nonexistent
    r_multiple/pnl_pct keys."""

    def test_win_rate_not_inflated_to_one(self):
        # 2 wins (1R, 2R) + 1 loss (-1R): wr=2/3, pf=3.0, expectancy=0.667R.
        result = {
            "signals": [
                _trade(entry=100.0, sl=98.0, qty=10.0, net_pnl=+20.0, result="WIN"),  # risk=20, r=1.0
                _trade(entry=100.0, sl=98.0, qty=10.0, net_pnl=-20.0, result="LOSS"),  # r=-1.0
                _trade(entry=100.0, sl=98.0, qty=10.0, net_pnl=+40.0, result="WIN"),  # r=2.0
            ]
        }
        summary = BacktestAgent._summarize_directional(result, direction="LONG")
        assert summary["sample_size"] == 3
        assert summary["wins"] == 2
        assert summary["losses"] == 1
        assert summary["win_rate"] == pytest.approx(2 / 3, abs=1e-6), (
            "win_rate must be 2/3, not 1.0 (pre-fix C2 bug: every trade counted as r=0 -> win)"
        )
        assert summary["profit_factor"] == pytest.approx(3.0, abs=1e-6), (
            "profit_factor must be 3.0 (gp=3.0/gl=1.0), not 0.0 (pre-fix C2)"
        )
        assert summary["expectancy_R"] == pytest.approx((1.0 - 1.0 + 2.0) / 3, abs=1e-6), (
            "expectancy_R must be ~0.667, not 0.0 (pre-fix C2)"
        )
        assert summary["avg_R"] == pytest.approx(summary["expectancy_R"], abs=1e-6)

    def test_all_losses_win_rate_zero(self):
        result = {
            "signals": [
                _trade(entry=100.0, sl=98.0, qty=10.0, net_pnl=-20.0, result="LOSS"),
                _trade(entry=100.0, sl=98.0, qty=10.0, net_pnl=-15.0, result="LOSS"),
            ]
        }
        summary = BacktestAgent._summarize_directional(result, direction="LONG")
        assert summary["win_rate"] == 0.0
        assert summary["losses"] == 2
        assert summary["wins"] == 0
        assert summary["profit_factor"] == 0.0  # gp=0
        assert summary["expectancy_R"] < 0.0

    def test_empty_signals_returns_zeros(self):
        summary = BacktestAgent._summarize_directional({"signals": []}, direction="LONG")
        assert summary["sample_size"] == 0
        assert summary["win_rate"] == 0.0
        assert summary["profit_factor"] == 0.0
        assert summary["expectancy_R"] == 0.0

    def test_direction_filter_respected(self):
        # SHORT trades must be filtered out when direction=LONG.
        result = {
            "signals": [
                _trade(entry=100.0, sl=98.0, qty=10.0, net_pnl=+20.0, result="WIN", sig_type="BUY"),
                _trade(entry=100.0, sl=102.0, qty=10.0, net_pnl=-20.0, result="LOSS", sig_type="SELL"),
            ]
        }
        summary = BacktestAgent._summarize_directional(result, direction="LONG")
        assert summary["sample_size"] == 1
        assert summary["wins"] == 1

    def test_explicit_r_multiple_still_honored(self):
        """If a trade dict DOES carry r_multiple (external/legacy), it is used
        directly -- backward compatible."""
        result = {
            "signals": [
                {"type": "BUY", "r_multiple": 1.5, "result": "WIN"},
                {"type": "BUY", "r_multiple": -1.0, "result": "LOSS"},
            ]
        }
        summary = BacktestAgent._summarize_directional(result, direction="LONG")
        assert summary["wins"] == 1
        assert summary["losses"] == 1
        assert summary["win_rate"] == 0.5
        assert summary["expectancy_R"] == pytest.approx(0.25, abs=1e-6)