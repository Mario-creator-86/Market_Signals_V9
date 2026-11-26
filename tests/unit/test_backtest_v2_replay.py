"""Regression: V2 replay + BOTH_AUTO_BEST selection in strategy_backtester (Pasul 3).

The faithful backtester now drives the live ``StrategySelector`` per bar (look-ahead-
safe context) and, under BOTH_AUTO_BEST, lets a V2 candidate that clears
``min_edge_score`` override the V1 ``best`` -- mirroring the live engine's
``_apply_v2_override`` / BOTH_AUTO_BEST path. These tests pin both branches:

- high V2 total_score (>= min_edge_score) -> trade recorded with
  ``strategy_source == "V2"`` and ``mode`` prefixed ``V2:``.
- low V2 total_score (< min_edge_score) -> defers to V1, ``strategy_source == "V1"``.

The unit under test here is the V2 *override decision*, not V1 signal generation
(that the V1 scorer is the live one is pinned in test_backtest_parity.py). So
``compute_signal_score`` is stubbed to a valid V1 candidate so the override block
always runs, and ``StrategySelector`` is mocked (no real evaluator/perf_cache).
``ConfigManager`` is faked to enable BOTH_AUTO_BEST.
"""
import math
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _synthetic_ohlcdf(n: int = 200) -> pd.DataFrame:
    """Minimal valid OHLCV df (no need to produce V1 signals; compute is stubbed).
    Only needs finite open/high/low/close/volume for the exit simulation + a 'dt'
    index so entry_time/exit_time populate."""
    idx = pd.date_range("2025-01-01 09:30", periods=n, freq="1min", tz="UTC", name="dt")
    t = pd.Series(range(n))
    close = 100.0 + 0.05 * t + 3.0 * pd.Series([math.sin(i / 19.0) for i in range(n)])
    open_ = close.shift(1).fillna(close.iloc[0])
    df = pd.DataFrame({"open": open_, "high": close * 1.0015, "low": close * 0.9985,
                       "close": close, "volume": 10_000.0})
    df.index = idx
    # row_to_context reads these; provide finite defaults so the stubbed scorer's
    # ctx construction (detect_market_regime) doesn't crash.
    df["rsi"] = 55.0
    df["macd"] = 0.05; df["macd_signal"] = 0.03; df["macd_hist"] = 0.02
    df["ema_20"] = close
    df["atr"] = 0.4
    df["adx"] = 22.0
    df["vol_sma"] = 10_000.0
    df["vwap"] = close
    df["trend_1m"] = "BULL"
    df["dist_vwap_atr"] = 0.0
    df["dist_ema_atr"] = 0.0
    df["cooldown_active"] = False
    df["cooldown_bars_remaining"] = 0
    df["trend_1d"] = "BULL"; df["trend_4h"] = "BULL"; df["trend_1h"] = "BULL"
    df["trend_30m"] = "BULL"; df["trend_15m"] = "BULL"; df["trend_5m"] = "BULL"
    df["adx_5m"] = 25.0; df["bb_width_5m"] = 0.02; df["atr_percentile_5m"] = 50.0
    return df


def _v1_score(direction="LONG", total_score=70):
    """A valid V1 ScoreResult so the backtest loop picks a non-None `best` and the
    V2 override block runs every eligible bar."""
    from core.scoring_system import ScoreResult
    return ScoreResult(
        total_score=total_score, direction=direction, mode="PULLBACK",
        profile="MEDIUM", is_valid=True, probability_score=0.7,
        raw_logit_value=0.8, mtf_score=80, rsi_score=60, vwap_score=70,
        ema_score=70, macd_score=65, price_action_score=60,
    )


class _FakeCM:
    """ConfigManager stand-in: real signal/scoring cfg, BOTH_AUTO_BEST enabled."""
    def __init__(self, real_cm):
        self.signal = real_cm.signal
        self.bot = {**(real_cm.bot or {}), "min_edge_score": 75}
        self.strategy_v2 = {
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


def _patched_selector(monkeypatch, total_score, direction="LONG", tp1=102.0, sl=99.0):
    inst = MagicMock()
    inst.select.return_value = {
        "best_candidate": {
            "strategy_id": "pullback_breakout",
            "direction": direction,
            "total_score": total_score,
            "tp1": tp1, "sl": sl, "rr": 3.0,
        }
    }
    cls = MagicMock(return_value=inst)
    monkeypatch.setattr("core.strategy_v2.selector.StrategySelector", cls)
    return inst


def _patched_research_selector(monkeypatch, total_score=85, direction="LONG", tp1=102.0, sl=99.0):
    inst = MagicMock()
    inst.select.return_value = {
        "best_candidate": None,
        "research_candidates": [{
            "strategy_id": "S7_LIQUIDITY_SWEEP_REVERSAL",
            "direction": direction,
            "total_score": total_score,
            "tp1": tp1,
            "sl": sl,
            "rr": 3.0,
            "research_only": True,
        }],
    }
    cls = MagicMock(return_value=inst)
    monkeypatch.setattr("core.strategy_v2.selector.StrategySelector", cls)
    return inst


@pytest.fixture
def fake_config(monkeypatch):
    from core.config import ConfigManager
    real = ConfigManager()
    # strategy_backtester imports ConfigManager lazily inside backtest()
    # (`from core.config import ConfigManager`), so patching the source module
    # is enough -- the lazy import rebinds at call time.
    monkeypatch.setattr("core.config.ConfigManager", lambda: _FakeCM(real))
    # Stub the V1 scorer so `best` is always a valid V1 candidate, exercising the
    # V2 override decision (the actual unit under test).
    monkeypatch.setattr("core.strategy_backtester.compute_signal_score",
                        lambda ctx, mode, profile, scoring_cfg: _v1_score())
    return real


def test_v2_high_score_overrides_v1(fake_config, monkeypatch):
    _patched_selector(monkeypatch, total_score=85)  # >= min_edge_score 75
    from core.strategy_backtester import backtest
    df = _synthetic_ohlcdf()
    res = backtest("SYNTH-USD", days=2, df=df)
    assert "error" not in res
    trades = res.get("trade_list", [])
    assert trades, "stubbed V1 scorer should yield trades so the V2 override runs"
    v2_trades = [t for t in trades if t.get("strategy_source") == "V2"]
    assert v2_trades, "V2 high-score candidate (85>=75) should override V1"
    assert v2_trades[0]["mode"].startswith("V2:")
    assert v2_trades[0]["profile"] == "V2"


def test_v2_low_score_defers_to_v1(fake_config, monkeypatch):
    _patched_selector(monkeypatch, total_score=70)  # < min_edge_score 75
    from core.strategy_backtester import backtest
    df = _synthetic_ohlcdf()
    res = backtest("SYNTH-USD", days=2, df=df)
    assert "error" not in res
    trades = res.get("trade_list", [])
    assert trades, "stubbed V1 scorer should yield trades"
    assert all(t.get("strategy_source") == "V1" for t in trades), (
        f"low V2 score (70<75) should defer to V1; got sources: {[t.get('strategy_source') for t in trades]}")


def test_v2_candidate_can_replay_without_a_v1_candidate(fake_config, monkeypatch):
    """A validated V2 setup must not depend on an unrelated V1 setup."""
    _patched_selector(monkeypatch, total_score=85)

    def no_v1_candidate(*_args, **_kwargs):
        score = _v1_score()
        score.is_valid = False
        return score

    monkeypatch.setattr("core.strategy_backtester.compute_signal_score", no_v1_candidate)
    from core.strategy_backtester import backtest

    result = backtest("SYNTH-USD", days=2, df=_synthetic_ohlcdf())
    trades = result.get("trade_list", [])
    assert trades
    assert all(trade.get("strategy_source") == "V2" for trade in trades)
    assert all(trade.get("strategy_id") == "pullback_breakout" for trade in trades)


def test_v2_levels_are_rebased_to_execution_fill_like_live(fake_config, monkeypatch):
    """Live execution converts V2 levels to percentages around the signal
    reference, then applies them to the verified fill. Replay must do the same."""
    frame = _synthetic_ohlcdf()
    signal_index = 60
    signal_reference = float(frame.iloc[signal_index]["close"])
    _patched_selector(
        monkeypatch,
        total_score=85,
        tp1=signal_reference * 1.02,
        sl=signal_reference * 0.99,
    )

    from core.strategy_backtester import backtest

    result = backtest("SYNTH-USD", days=2, df=frame)
    trade = next(item for item in result["trade_list"] if item["strategy_source"] == "V2")

    assert trade["signal_reference_price"] == pytest.approx(signal_reference)
    assert trade["stop_loss_price"] == pytest.approx(trade["entry"] * 0.99)
    assert trade["take_profit_price"] == pytest.approx(trade["entry"] * 1.02)


def test_commission_uses_actual_exit_notional(fake_config, monkeypatch):
    _patched_selector(monkeypatch, total_score=85)
    from core.strategy_backtester import backtest

    result = backtest("SYNTH-USD", days=2, df=_synthetic_ohlcdf())
    trade = next(item for item in result["trade_list"] if item["strategy_source"] == "V2")
    commission_fraction = 25.0 / 10_000.0
    expected_cost_pct = (
        (trade["entry"] + trade["exit"]) * commission_fraction / trade["entry"]
    ) * 100.0

    assert trade["commission_cost_pct"] == pytest.approx(expected_cost_pct, abs=1e-6)


def test_research_candidate_is_replayed_only_when_explicitly_requested(fake_config, monkeypatch):
    """S7 can seed private OOS training evidence without changing live replay."""
    _patched_research_selector(monkeypatch)

    def no_v1_candidate(*_args, **_kwargs):
        score = _v1_score()
        score.is_valid = False
        return score

    monkeypatch.setattr("core.strategy_backtester.compute_signal_score", no_v1_candidate)
    from core.strategy_backtester import backtest

    default_result = backtest("SYNTH-USD", days=2, df=_synthetic_ohlcdf(), v2_only=True)
    assert default_result.get("trade_list", []) == []

    research_result = backtest(
        "SYNTH-USD",
        days=2,
        df=_synthetic_ohlcdf(),
        v2_only=True,
        allow_research_only_candidates=True,
    )
    trades = research_result.get("trade_list", [])
    assert trades
    assert all(trade.get("strategy_source") == "V2" for trade in trades)
    assert all(trade.get("strategy_id") == "S7_LIQUIDITY_SWEEP_REVERSAL" for trade in trades)
    assert all(trade.get("research_only") is True for trade in trades)


def test_v2_disabled_by_default_signal_mode(monkeypatch):
    """Default signal_mode V1 must NOT instantiate the V2 selector (V1-only replay)."""
    from core.config import ConfigManager
    real = ConfigManager()

    class _V1CM:
        signal = real.signal
        bot = {**(real.bot or {}), "min_edge_score": 75}
        strategy_v2 = {
            "signal_mode": "V1",
            "costs_by_asset": {
                "CRYPTO_FUTURES": {
                    "commission_bps": 25,
                    "entry_slippage_bps": 10,
                    "slippage_sl_bps": 10,
                    "slippage_tp_bps": 10,
                }
            },
        }

    monkeypatch.setattr("core.config.ConfigManager", lambda: _V1CM())
    # Sentinel: if V2 is (incorrectly) enabled, StrategySelector would be
    # instantiated -> _boom raises -> backtest raises -> test fails loudly.
    def _boom(*a, **k):
        raise AssertionError("StrategySelector must not be instantiated when signal_mode=V1")
    monkeypatch.setattr("core.strategy_v2.selector.StrategySelector", _boom)
    # Stub V1 scorer so trades exist to observe strategy_source.
    monkeypatch.setattr("core.strategy_backtester.compute_signal_score",
                        lambda ctx, mode, profile, scoring_cfg: _v1_score())

    from core.strategy_backtester import backtest
    df = _synthetic_ohlcdf()
    res = backtest("SYNTH-USD", days=2, df=df)
    assert "error" not in res
    assert all(t.get("strategy_source") == "V1" for t in res.get("trade_list", []))
