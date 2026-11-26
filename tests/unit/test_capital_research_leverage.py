from __future__ import annotations

import core.capital_manager as capital_mod
from core.capital_manager import CapitalManager


def test_research_cohort_never_receives_profitability_leverage_boost(monkeypatch, tmp_path):
    monkeypatch.setenv("MS_DISABLE_CAPITAL_THREADS", "1")
    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", tmp_path / "capital.json")
    monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: None)
    CapitalManager._instance = None
    manager = CapitalManager()
    manager.set_balance(1_000.0, reason="test", clear_positions=True)
    monkeypatch.setattr(manager, "_strategy_is_profitable", lambda: True)
    monkeypatch.setattr(
        manager,
        "_get_execution_price",
        lambda *_args, **_kwargs: {
            "execution_price": 100.0,
            "commission_rate": 0.0002,
            "source": "test_quote",
            "price_timestamp": "2026-07-16T00:00:00+00:00",
            "execution_price_quality": {"quality": "verified", "source": "test_quote"},
            "cost_model": {},
        },
    )

    opened = manager.open_position(
        ticker="AAPL",
        direction="LONG",
        size_usd=25.0,
        stop_loss_pct=0.04,
        take_profit_pct=0.072,
        leverage=1,
        position_type="research_cohort",
        research_only=True,
        decision_context={"scoring": {"signal_score": 95.0, "probability_score": 0.95}},
    )

    assert opened["research_only"] is True
    assert opened["leverage"] == 1
    CapitalManager._instance = None


def test_paper_capital_experiment_never_receives_profitability_leverage_boost(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("MS_DISABLE_CAPITAL_THREADS", "1")
    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", tmp_path / "capital.json")
    monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: None)
    CapitalManager._instance = None
    manager = CapitalManager()
    manager.set_balance(1_000.0, reason="test", clear_positions=True)
    monkeypatch.setattr(manager, "_strategy_is_profitable", lambda: True)
    monkeypatch.setattr(
        manager,
        "_get_execution_price",
        lambda *_args, **_kwargs: {
            "execution_price": 100.0,
            "commission_rate": 0.0002,
            "source": "test_quote",
            "price_timestamp": "2026-08-26T00:00:00+00:00",
            "execution_price_quality": {"quality": "verified", "source": "test_quote"},
            "cost_model": {},
        },
    )

    opened = manager.open_position(
        ticker="BTC-USD",
        direction="LONG",
        size_usd=25.0,
        stop_loss_pct=0.04,
        take_profit_pct=0.06,
        leverage=1,
        decision_context={
            "paper_capital_experiment": True,
            "scoring": {"signal_score": 95.0, "probability_score": 0.95},
        },
    )

    assert opened["paper_capital_experiment"] is True
    assert opened["leverage"] == 1
    CapitalManager._instance = None
