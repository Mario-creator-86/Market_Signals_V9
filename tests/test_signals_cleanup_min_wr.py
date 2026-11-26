from pathlib import Path

from core.signals import SignalManager


class _Cfg:
    def __init__(self, min_wr: float):
        self._min_wr = float(min_wr)

    def get_effective_ticker_settings(self, ticker: str):
        return {
            "signals_enabled": True,
            "allowed_strategies": [],
            "min_win_rate": self._min_wr,
        }


def _make_manager(tmp_path: Path) -> SignalManager:
    p = tmp_path / "signals_test.json"
    return SignalManager(path=p)


def test_cleanup_invalid_signals_removes_open_signal_below_min_wr(tmp_path):
    mgr = _make_manager(tmp_path)
    mgr.open_signals = {
        "S1": {
            "id": "S1",
            "ticker": "AAPL",
            "status": "Open",
            "entry": 100.0,
            "action": "BUY",
            "risk_mode": "STRONG",
            "trade_mode": "PULLBACK",
            "quick_backtest_30d": {"total": 30, "win_rate": 45.0},
        }
    }
    cfg = _Cfg(min_wr=60.0)

    removed = mgr.cleanup_invalid_signals(cfg, tickers_data={"AAPL": {"price": 101.0}})

    assert removed == 1
    assert "S1" not in mgr.open_signals
    assert len(mgr.closed_signals) == 1
    assert mgr.closed_signals[0]["close_reason"] == "Win rate 45.0% < min 60.0%"


def test_cleanup_invalid_signals_removes_open_signal_without_bt_history_when_min_wr_enabled(tmp_path):
    mgr = _make_manager(tmp_path)
    mgr.open_signals = {
        "S2": {
            "id": "S2",
            "ticker": "NVDA",
            "status": "Open",
            "entry": 200.0,
            "action": "BUY",
            "risk_mode": "STRONG",
            "trade_mode": "PULLBACK",
            "quick_backtest_30d": {"total": 0, "win_rate": 0.0},
        }
    }
    cfg = _Cfg(min_wr=60.0)

    removed = mgr.cleanup_invalid_signals(cfg, tickers_data={"NVDA": {"price": 199.0}})

    assert removed == 1
    assert "S2" not in mgr.open_signals
    assert len(mgr.closed_signals) == 1
    assert mgr.closed_signals[0]["close_reason"] == "No 30d backtest history for min WR 60.0%"
