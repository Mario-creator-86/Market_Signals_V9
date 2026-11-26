"""Contract: FAITHFUL backtest branch wiring (Pasul 2c).

``run_backtest`` gained a FAITHFUL branch (crypto-only) that delegates to
``core.strategy_backtester`` (live replay) and adapts its summary to the
comparison-row contract ``App.renderBacktestResults`` reads. This test patches
the network-dependent ``build_dataframe``/``backtest`` and asserts:
- risk_mode='FAITHFUL' triggers the branch (raw value preserved before the
  risk-mode normalization that would otherwise collapse it to MEDIUM);
- crypto gate rejects a US stock;
- the returned dict carries mode='faithful' + the adapter keys
  (win_rate_trades, expectancy_r, payoff_ratio, wins, losses, trades).
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _fake_backtest_summary(ticker, days, **kw):
    return {
        "ticker": ticker, "days": days, "trades": 3,
        "wins": 2, "losses": 1, "win_rate": 66.7, "win_rate_trades": 66.7,
        "avg_R": 0.42, "expectancy_r": 0.42, "avg_win_r": 1.1, "avg_loss_r": -0.6,
        "expectancy_pct": 1.3, "rr": 1.8, "payoff_ratio": 1.8, "profit_factor": 2.4,
        "max_drawdown_pct": 4.2, "total_net_pct": 3.9, "exit_mix": {"take_profit": 2, "stop_loss": 1},
        "trade_list": [
            {"dir": "LONG", "entry": 100.0, "exit": 102.0, "entry_time": "2025-01-01T09:31:00+00:00",
             "exit_time": "2025-01-01T10:15:00+00:00", "net_pct": 1.75, "r": 1.2, "reason": "take_profit",
             "score": 82.0, "mode": "PULLBACK", "profile": "MEDIUM"},
        ],
    }


def test_faithful_branch_crypto_returns_adapted_contract(monkeypatch):
    from api import actions_service as svc

    monkeypatch.setattr(svc, "build_dataframe", lambda *a, **k: "fake_df", raising=False)
    # Patch the backtest used inside the branch (imported lazily from core.strategy_backtester).
    import core.strategy_backtester as sb
    monkeypatch.setattr(sb, "build_dataframe", lambda *a, **k: "fake_df")
    monkeypatch.setattr(sb, "backtest", _fake_backtest_summary)
    # Avoid touching the audit/state store on disk.
    monkeypatch.setattr(svc, "save_backtest_result", lambda r: r)

    res = svc.run_backtest(days=7, risk_mode="FAITHFUL", trade_mode="PULLBACK",
                           tickers=["BTC-USD"], compare_all=False)
    assert res["mode"] == "faithful"
    assert res["ticker"] == "BTC-USD"
    # Adapter keys present for App.renderBacktestResults comparison row.
    for key in ("win_rate_trades", "expectancy_r", "payoff_ratio",
                "profit_factor", "max_drawdown_pct", "avg_win_r", "avg_loss_r",
                "wins", "losses", "trades", "trade_list", "timestamp"):
        assert key in res, f"missing adapter key: {key}"


def test_faithful_branch_rejects_us_stock():
    from api import actions_service as svc

    res = svc.run_backtest(days=7, risk_mode="FAITHFUL", trade_mode="PULLBACK",
                           tickers=["AAPL"], compare_all=False)
    assert res["mode"] == "faithful"
    assert "error" in res
    assert res["wins"] == 0 and res["losses"] == 0


def test_faithful_triggered_by_trade_mode_too(monkeypatch):
    """trade_mode='FAITHFUL' also triggers (independent of risk_mode normalization)."""
    from api import actions_service as svc
    import core.strategy_backtester as sb

    monkeypatch.setattr(sb, "build_dataframe", lambda *a, **k: "fake_df")
    monkeypatch.setattr(sb, "backtest", _fake_backtest_summary)
    monkeypatch.setattr(svc, "save_backtest_result", lambda r: r)

    res = svc.run_backtest(days=7, risk_mode="MEDIUM", trade_mode="FAITHFUL",
                           tickers=["ETH-USD"], compare_all=False)
    assert res["mode"] == "faithful"


def test_faithful_summary_does_not_invent_ratio_without_a_loss():
    from core.strategy_backtester import _summarize

    summary = _summarize(
        "BTC-USD",
        7,
        [{"net_pct": 1.2, "r": 1.0, "r_net": 0.98, "reason": "take_profit"}],
        min_trades_for_validation=30,
    )

    assert summary["profit_factor"] is None
    assert summary["payoff_ratio"] is None
    assert summary["evidence"] == {
        "sample_size": 1,
        "minimum_sample": 30,
        "sufficient": False,
        "status": "insufficient_sample",
    }
    assert any("Insufficient evidence" in warning for warning in summary["warnings"])
