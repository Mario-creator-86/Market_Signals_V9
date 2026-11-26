from pathlib import Path
from uuid import uuid4

import pandas as pd

from core import paper_btc_strategy
from core.paper_btc_strategy import generate_paper_btc_signal
from core.signals import SignalManager


def _build_ohlcv(close_values, freq: str) -> pd.DataFrame:
    close = [float(value) for value in close_values]
    return pd.DataFrame(
        {
            "Open": close,
            "High": [value + 0.8 for value in close],
            "Low": [value - 0.8 for value in close],
            "Close": close,
            "Volume": [1000.0] * len(close),
        },
        index=pd.date_range("2026-01-01", periods=len(close), freq=freq, tz="UTC"),
    )


def test_generate_paper_btc_signal_prefers_bollinger_short(monkeypatch) -> None:
    bollinger_signal = {"strategy_id": "bollinger_rsi_short_4h", "type": "SHORT", "entry": 100.0, "tp": 95.0, "sl": 103.0}
    donchian_signal = {"strategy_id": "donchian_short_1h", "type": "SHORT", "entry": 99.0, "tp": 94.0, "sl": 102.0}
    monkeypatch.setattr(paper_btc_strategy, "_evaluate_bollinger_short", lambda *args, **kwargs: bollinger_signal)
    monkeypatch.setattr(paper_btc_strategy, "_evaluate_donchian_short", lambda *args, **kwargs: donchian_signal)

    signal = generate_paper_btc_signal(
        "BTC-USD",
        _build_ohlcv([100, 99, 98], "1h"),
        _build_ohlcv([100, 99, 98], "4h"),
        config={
            "tickers": ["BTC-USD"],
            "prefer": ["bollinger_rsi_short_4h", "donchian_short_1h"],
            "validation": {"enabled": False},
        },
    )

    assert signal["strategy_id"] == "bollinger_rsi_short_4h"
    assert signal["entry"] == 100.0
    assert signal["tp"] == 95.0
    assert signal["sl"] == 103.0
    assert signal["validation_passed"] is True


def test_generate_paper_btc_signal_falls_back_to_donchian_breakout(monkeypatch) -> None:
    donchian_signal = {"strategy_id": "donchian_short_1h", "type": "SHORT", "entry": 99.0, "tp": 94.0, "sl": 102.0}
    monkeypatch.setattr(paper_btc_strategy, "_evaluate_bollinger_short", lambda *args, **kwargs: None)
    monkeypatch.setattr(paper_btc_strategy, "_evaluate_donchian_short", lambda *args, **kwargs: donchian_signal)

    signal = generate_paper_btc_signal(
        "BTC-USD",
        _build_ohlcv([100, 99, 98], "1h"),
        _build_ohlcv([100, 99, 98], "4h"),
        config={
            "tickers": ["BTC-USD"],
            "prefer": ["bollinger_rsi_short_4h", "donchian_short_1h"],
            "validation": {"enabled": False},
        },
    )

    assert signal["strategy_id"] == "donchian_short_1h"
    assert signal["entry"] == 99.0
    assert signal["tp"] == 94.0
    assert signal["sl"] == 102.0
    assert signal["validation_passed"] is True


def test_generate_paper_btc_signal_skips_unprofitable_preferred_strategy(monkeypatch) -> None:
    bollinger_signal = {"strategy_id": "bollinger_rsi_short_4h", "type": "SHORT", "entry": 100.0, "tp": 95.0, "sl": 103.0}
    donchian_signal = {"strategy_id": "donchian_short_1h", "type": "SHORT", "entry": 99.0, "tp": 94.0, "sl": 102.0}
    monkeypatch.setattr(paper_btc_strategy, "_evaluate_bollinger_short", lambda *args, **kwargs: bollinger_signal)
    monkeypatch.setattr(paper_btc_strategy, "_evaluate_donchian_short", lambda *args, **kwargs: donchian_signal)

    def fake_validate(strategy_id, candidate, *args, **kwargs):
        if strategy_id == "bollinger_rsi_short_4h":
            return None
        enriched = dict(candidate)
        enriched.update({"validation_passed": True, "backtest_trade_count": 12, "profit_factor_30d": 2.1})
        return enriched

    monkeypatch.setattr(paper_btc_strategy, "_validate_candidate", fake_validate)

    signal = generate_paper_btc_signal(
        "BTC-USD",
        _build_ohlcv([100, 99, 98], "1h"),
        _build_ohlcv([100, 99, 98], "4h"),
        config={"tickers": ["BTC-USD"], "prefer": ["bollinger_rsi_short_4h", "donchian_short_1h"]},
    )

    assert signal["strategy_id"] == "donchian_short_1h"
    assert signal["validation_passed"] is True
    assert signal["backtest_trade_count"] == 12


def test_paper_validation_excludes_position_open_at_end_of_sample() -> None:
    df = pd.DataFrame(
        {"close": [100.0, 90.0, 80.0]},
        index=pd.date_range("2026-01-01", periods=3, freq="4h", tz="UTC"),
    )
    positions = pd.Series([-1.0, -1.0, -1.0], index=df.index)

    stats = paper_btc_strategy._backtest_position_series(
        df,
        positions,
        leverage=1.0,
        cost_bps_per_side=4.0,
    )

    assert stats["trade_count"] == 0
    assert stats["total_return_pct"] == 0.0
    assert stats["profit_factor"] is None


def test_1h_filter_uses_only_preceding_completed_4h_candle() -> None:
    df_1h = _build_ohlcv([100.0] * 12, "1h")
    df_4h = _build_ohlcv([100.0, 90.0, 80.0], "4h")
    df_1h.columns = [str(column).lower() for column in df_1h.columns]
    df_4h.columns = [str(column).lower() for column in df_4h.columns]

    bearish = paper_btc_strategy._build_4h_bearish_filter_for_1h(df_1h, df_4h)

    assert bool(bearish.iloc[4]) is False
    assert bool(bearish.iloc[8]) is True


def test_signal_manager_syncs_closed_paper_position(monkeypatch) -> None:
    class FakeCapitalManager:
        def get_position(self, position_id: str):
            if position_id != "pos_test":
                return None
            return {
                "position_id": "pos_test",
                "status": "CLOSED",
                "ticker": "BTC-USD",
                "entry_price": 100.0,
                "exit_price": 92.0,
                "net_pnl": 8.0,
                "pnl_pct_notional": 8.0,
                "usd_size": 100.0,
                "closed_at": "2026-03-12T12:00:00+02:00",
                "close_reason": "take_profit",
            }

    monkeypatch.setattr("core.signals.get_capital_manager", lambda: FakeCapitalManager())
    test_path = Path("output") / f"signals_test_{uuid4().hex}.json"
    sm = SignalManager(path=test_path)
    sm.open_signals = {
        "sig_test": {
            "id": "sig_test",
            "ticker": "BTC-USD",
            "action": "SHORT",
            "type": "SHORT",
            "entry": 100.0,
            "tp": 94.0,
            "sl": 103.0,
            "status": "Open",
            "paper_position_id": "pos_test",
            "executed_entry_price": 100.0,
        }
    }

    outcomes = sm.sync_paper_positions()

    assert outcomes == {"BTC-USD": "WIN"}
    assert "sig_test" not in sm.open_signals
    assert sm.closed_signals[0]["paper_position_status"] == "CLOSED"
    assert sm.closed_signals[0]["pnl_amount"] == 8.0
    if test_path.exists():
        test_path.unlink()


def test_signal_manager_clears_missing_paper_position(monkeypatch) -> None:
    class FakeCapitalManager:
        def get_position(self, position_id: str):
            return None

    monkeypatch.setattr("core.signals.get_capital_manager", lambda: FakeCapitalManager())
    test_path = Path("output") / f"signals_test_{uuid4().hex}.json"
    sm = SignalManager(path=test_path)
    sm.open_signals = {
        "sig_test": {
            "id": "sig_test",
            "ticker": "NVDA",
            "action": "BUY",
            "entry": 100.0,
            "status": "Open",
            "paper_position_id": "pos_missing",
        }
    }

    outcomes = sm.sync_paper_positions()

    signal = sm.open_signals["sig_test"]
    assert outcomes == {}
    assert signal["stale_paper_position_id"] == "pos_missing"
    assert "paper_position_id" not in signal
    assert signal["paper_status"] == "MISSING"
    if test_path.exists():
        test_path.unlink()


def test_signal_manager_syncs_closed_position_pushed_by_capital_manager() -> None:
    test_path = Path("output") / f"signals_test_{uuid4().hex}.json"
    sm = SignalManager(path=test_path)
    sm.open_signals = {
        "sig_test": {
            "id": "sig_test",
            "ticker": "AAPL",
            "action": "BUY",
            "entry": 100.0,
            "status": "Open",
            "paper_position_id": "pos_test",
            "executed_entry_price": 100.0,
        }
    }

    result = sm.sync_closed_paper_position(
        {
            "position_id": "pos_test",
            "signal_id": "sig_test",
            "status": "CLOSED",
            "ticker": "AAPL",
            "entry_price": 100.0,
            "exit_price": 104.0,
            "net_pnl": 4.0,
            "pnl_pct_notional": 4.0,
            "usd_size": 100.0,
            "closed_at": "2026-04-26T10:00:00+00:00",
            "close_reason": "take_profit",
        }
    )

    assert result == "WIN"
    assert "sig_test" not in sm.open_signals
    assert sm.closed_signals[0]["status"] == "WIN"
    assert sm.closed_signals[0]["paper_position_status"] == "CLOSED"
    if test_path.exists():
        test_path.unlink()


def test_signal_manager_save_backfills_zero_public_scores() -> None:
    test_path = Path("output") / f"signals_test_{uuid4().hex}.json"
    sm = SignalManager(path=test_path)
    sm.open_signals = {
        "sig_score": {
            "id": "sig_score",
            "ticker": "MSFT",
            "signal_score": 0,
            "score": 0,
            "raw_score": 97,
            "status": "Open",
        }
    }

    sm.save()

    assert sm.open_signals["sig_score"]["signal_score"] == 97.0
    assert sm.open_signals["sig_score"]["score"] == 97.0
    if test_path.exists():
        test_path.unlink()
