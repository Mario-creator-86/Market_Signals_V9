"""Regression: probability-score scaling in the leverage-boost gate (M1).

Bug (M1): ``CapitalManager.open_position`` computed

    _prob = probability_score / (100.0 if _score > 1 else 1.0)

where ``_score`` is the 0..100 ``signal_score`` (always > 1). ``probability_score``
is a sigmoid in [0,1] (``scoring_system.SignalScore``), so a 0.85 probability was
divided by 100 -> 0.0085, and the prob-gated leverage boost
(``_prob >= 0.75`` / ``_prob >= 0.85``) could NEVER fire. The boost was therefore
dead whenever a signal carried a probability_score (which all logistic signals do).

Fix: normalize by scale — a value <= 1.0 is already a probability; > 1.0 is on the
0..100 score scale (divided by 100). The boost is still gated behind
``_strategy_is_profitable`` (>=30 trades, PF>=1.2, expectancy>0), so re-activating
the prob path only amplifies a PROVEN profitable edge.
"""
from dataclasses import dataclass
from typing import Dict, List, Tuple

import pytest

import core.capital_manager as capital_mod
from core.capital_manager import CapitalManager


def _quote(price: float, commission_rate: float = 0.0025) -> dict:
    return {
        "execution_price": float(price),
        "execution_price_quality": {"quality": "verified", "source": "unit_test"},
        "spread_pct": 0.0,
        "slippage_pct": 0.0,
        "commission_rate": float(commission_rate),
        "total_cost_pct": float(commission_rate) * 100.0,
        "price_timestamp": "2026-07-02T10:00:00+00:00",
        "source": "unit_test",
    }


@pytest.fixture
def capital_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MS_DISABLE_CAPITAL_THREADS", "1")
    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", tmp_path / "capital.json")
    monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: None)
    try:
        import core.trade_audit as trade_audit

        monkeypatch.setattr(trade_audit, "record_trade_event", lambda *a, **k: None)
    except Exception:
        pass
    try:
        import core.feedback_loop as feedback_loop

        monkeypatch.setattr(feedback_loop, "process_closed_trade", lambda *a, **k: None)
    except Exception:
        pass

    CapitalManager._instance = None
    CapitalManager._price_cache = {}
    CapitalManager._atr_cache = {}
    manager = CapitalManager()
    manager.set_balance(10_000.0, reason="test", clear_positions=True)
    yield manager
    CapitalManager._instance = None
    CapitalManager._price_cache = {}
    CapitalManager._atr_cache = {}


def test_prob_gated_boost_fires_for_high_probability_when_profitable(capital_env, monkeypatch):
    """probability_score=0.85 with signal_score=80 (below the 85 score gate) must still
    trigger the 2x boost when the strategy is profitable — the prob path was dead pre-fix."""
    manager = capital_env
    monkeypatch.setattr(manager, "_strategy_is_profitable", lambda: True)
    monkeypatch.setattr(manager, "_get_execution_price", lambda ticker, side, **kw: _quote(100.0))

    opened = manager.open_position(
        ticker="AAPL",
        direction="LONG",
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="sig_prob_high",
        leverage=1,
        decision_context={"scoring": {"signal_score": 80, "probability_score": 0.85}},
    )
    # prob path (0.85 >= 0.75) fires -> boosted from 1 to 2
    assert opened["leverage"] == 2


def test_prob_gated_boost_does_not_fire_when_not_profitable(capital_env, monkeypatch):
    """Even with a high probability_score, the boost must NOT fire when the strategy is
    not yet profitable — the profit-gate is the safety guard and stays intact."""
    manager = capital_env
    monkeypatch.setattr(manager, "_strategy_is_profitable", lambda: False)
    monkeypatch.setattr(manager, "_get_execution_price", lambda ticker, side, **kw: _quote(100.0))

    opened = manager.open_position(
        ticker="AAPL",
        direction="LONG",
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="sig_prob_unprofitable",
        leverage=1,
        decision_context={"scoring": {"signal_score": 80, "probability_score": 0.85}},
    )
    assert opened["leverage"] == 1


def test_mis_scaled_probability_on_0_100_scale_is_normalized(capital_env, monkeypatch):
    """If a stale payload stored probability_score on the 0..100 scale (>1), it is
    normalized to [0,1] so the gate still works (defensive, mirrors trade_decision._fmt_score)."""
    manager = capital_env
    monkeypatch.setattr(manager, "_strategy_is_profitable", lambda: True)
    monkeypatch.setattr(manager, "_get_execution_price", lambda ticker, side, **kw: _quote(100.0))

    opened = manager.open_position(
        ticker="AAPL",
        direction="LONG",
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="sig_prob_mis_scaled",
        leverage=1,
        # probability_score=85 (0..100 scale) -> normalized to 0.85 -> boost fires
        decision_context={"scoring": {"signal_score": 80, "probability_score": 85.0}},
    )
    assert opened["leverage"] == 2


def test_low_probability_does_not_boost_even_when_profitable(capital_env, monkeypatch):
    """A mediocre probability (0.6) below the 0.75 gate and a score below 85 must NOT
    boost — guards against the fix over-triggering."""
    manager = capital_env
    monkeypatch.setattr(manager, "_strategy_is_profitable", lambda: True)
    monkeypatch.setattr(manager, "_get_execution_price", lambda ticker, side, **kw: _quote(100.0))

    opened = manager.open_position(
        ticker="AAPL",
        direction="LONG",
        size_usd=1000.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        signal_id="sig_prob_low",
        leverage=1,
        decision_context={"scoring": {"signal_score": 70, "probability_score": 0.6}},
    )
    assert opened["leverage"] == 1