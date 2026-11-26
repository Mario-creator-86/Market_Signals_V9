"""Deterministic paper-position simulations for every AI Cockpit entry source."""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agents.execution_agent import ExecutionAgent
from agents.risk_agent import RiskAgent
from core.autonomous_candidates import (
    build_pre_earnings_candidate_signal,
    build_scanner_candidate_signal,
)
from core.capital_entry_guard import evaluate_capital_entry
from core.confidence_gates import ConfidenceGates
from core.strategy_v2.performance_cache import StrategyPerformanceCache


class _SimulatedCapital:
    def __init__(self, balance: float = 10_000.0):
        self.balance = balance
        self.positions: list[dict] = []
        self.open_calls: list[dict] = []

    def get_balance(self):
        return self.balance

    def get_positions(self):
        return deepcopy(self.positions)

    def get_position(self, position_id):
        return next(
            (deepcopy(item) for item in self.positions if item["position_id"] == position_id),
            None,
        )

    def get_equity_curve(self, limit=500):
        return []

    def open_position(self, **kwargs):
        self.open_calls.append(dict(kwargs))
        position = {
            "position_id": f"sim-pos-{len(self.open_calls)}",
            "ticker": kwargs["ticker"],
            "direction": kwargs["direction"],
            "signal_id": kwargs["signal_id"],
            "status": "OPEN",
            "entry_price": 100.0,
            "usd_size": kwargs["size_usd"],
            "decision_context": kwargs["decision_context"],
        }
        self.positions.append(position)
        return deepcopy(position)


class _SimulatedSignals:
    def __init__(self):
        self.open_signals = {}
        self.attached: list[tuple[str, dict]] = []

    def attach_paper_position(self, signal_id, position):
        self.attached.append((signal_id, deepcopy(position)))


class _SimulationConfig:
    def __init__(self):
        execution = {
            "enabled": True,
            "mode": "paper",
            "max_open_trades": 10,
            "balance_usage_pct": 8.0,
            "unprofitable_balance_usage_cap_pct": 8.0,
            "unprofitable_balance_usage_hard_cap_pct": 8.0,
            "profitability_min_sample": 24,
            "profitability_min_profit_factor": 1.05,
            "profitability_min_expectancy": 0.0,
            "force_one_x_until_profitable": False,
            "allow_ai_leverage": True,
            "max_ai_leverage": 2,
            "require_ai_review": True,
            "scanner_min_abs_change_pct": 5.0,
            "scanner_min_volume_24h": 1_000_000.0,
            "scanner_min_dollar_volume": 10_000_000.0,
            "scanner_min_score": 78.0,
            "scanner_entry_mode": "fade",
            "scanner_fade_min_move_pct": 8.0,
            "scanner_fade_max_move_pct": 40.0,
            "scanner_fade_stop_pct": 0.04,
            "scanner_fade_rr_ratio": 1.5,
            "pre_earnings_min_gap_risk": 65.0,
            "pre_earnings_min_abs_bias": 20.0,
            "pre_earnings_min_coverage_score": 50.0,
            "pre_earnings_min_score": 78.0,
            "pre_earnings_rr_ratio": 1.8,
            "capital_entry_gate": {
                "enabled": True,
                "require_ai_review": True,
                "data": {
                    "max_signal_age_seconds": 300,
                    "min_risk_reward_ratio": 1.1,
                    "require_price_integrity": True,
                },
            },
            "ticker_learning": {"enabled": False},
        }
        risk = {
            "enabled": True,
            "max_daily_loss_pct": 10.0,
            "max_daily_loss_trades": 5,
            "max_open_trades": 10,
            "max_exposure_pct": 80.0,
            "max_position_size_pct": 25.0,
            "max_risk_per_trade_pct": 5.0,
            "min_risk_reward_ratio": 1.1,
            "position_size_pct": 8.0,
        }
        self.agents = {"execution": execution, "risk": risk}
        self.bot = {
            "enabled": True,
            "min_edge_score": 64.0,
            "require_strategy_performance": True,
            "allow_unvalidated_autonomous_fallback": False,
            "unvalidated_autonomous_size_factor": 0.25,
            "require_trade_radar_confirmation": True,
            "max_trades_per_day": 0,
            "max_same_strategy_concurrent": 0,
            "consecutive_loss_cooldown": 0,
        }
        self.config = {
            "bot": self.bot,
            "agents": self.agents,
            "autonomy": {
                "mode": "PAPER_AUTO",
                "kill_switch_active": False,
                "live_auto_owner_enabled": False,
            },
            "paper_execution": {"reduce_fraction_pct": 20.0},
            "risk_constitution": {
                "owner_confirmed": True,
                "max_risk_per_trade_pct": 5.0,
                "max_position_size_pct": 25.0,
                "max_total_exposure_pct": 80.0,
                "max_leverage": 2.0,
                "minimum_cash_pct": 10.0,
                "max_sector_exposure_pct": 60.0,
                "correlation_threshold": 0.7,
                "max_correlated_exposure_pct": 60.0,
                "max_positions": 10,
                "max_liquidity_participation_pct": 5.0,
                "max_daily_loss_pct": 10.0,
                "max_daily_loss_trades": 5,
                "max_drawdown_pct": 20.0,
                "max_data_age_seconds": 300,
                "block_data_conflicts": True,
                "require_approved_strategy": False,
            },
        }


def _candidate(source: str, execution_cfg: dict) -> dict:
    if source == "crypto":
        signal = build_scanner_candidate_signal(
            {
                "symbol": "BTCUSDT",
                "price": 50_000.0,
                "change_pct": -10.0,
                "volume": 50_000_000.0,
                "source": "simulation",
            },
            execution_cfg,
        )
    elif source == "stock":
        signal = build_scanner_candidate_signal(
            {
                "symbol": "AAPL",
                "price": 200.0,
                "change_pct": 10.0,
                "volume": 2_000_000.0,
                "source": "simulation",
            },
            execution_cfg,
        )
    else:
        signal = build_pre_earnings_candidate_signal(
            {
                "symbol": "NVDA",
                "price": 100.0,
                "earnings_time_utc": "2026-08-27T00:00:00+00:00",
                "session": "BMO",
                "event_status": "UPCOMING",
                "recommendation_eligible": True,
                "gap_risk": 85,
                "direction_bias_score": 35,
                "direction_label": "LONG",
                "coverage_score": 75,
                "data_confidence": "HIGH",
                "expected_move_pct": 8.0,
                "avg_volume": 20_000_000.0,
                "opportunity_score": 85.0,
            },
            100.0,
            execution_cfg,
        )
    assert signal is not None
    signal.update(
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "data_age_seconds": 1.0,
            "price_verified": True,
            "price_integrity": {"status": "verified", "is_verified": True},
            "has_data_conflict": False,
            "invested_amount": 800.0,
            "usd_size": 800.0,
            "_autonomous_candidate": True,
        }
    )
    return signal


@pytest.mark.parametrize(
    ("source", "expected_ticker", "expected_direction"),
    [
        ("crypto", "BTC-USD", "LONG"),
        ("stock", "AAPL", "SHORT"),
        ("pre_earnings", "NVDA", "LONG"),
    ],
)
def test_autonomous_candidate_reaches_a_filled_paper_position(
    monkeypatch,
    tmp_path,
    source,
    expected_ticker,
    expected_direction,
):
    config = _SimulationConfig()
    capital = _SimulatedCapital()
    signals = _SimulatedSignals()
    execution_cfg = config.agents["execution"]
    signal = _candidate(source, execution_cfg)

    monkeypatch.setattr("core.capital_manager.get_capital_manager", lambda: capital)
    monkeypatch.setattr("agents.execution_agent.get_capital_manager", lambda: capital)
    monkeypatch.setattr("agents.execution_agent.get_signal_manager", lambda: signals)
    monkeypatch.setattr("agents.execution_agent.record_trade_event", lambda **_kwargs: None)
    monkeypatch.setattr("core.feedback_loop.get_recent_trades", lambda *_args, **_kwargs: [])
    monkeypatch.setenv("MS_EXECUTION_DB_PATH", str(tmp_path / f"{source}-execution.db"))

    import core.capital_entry_guard as capital_guard
    import core.execution.runtime as execution_runtime

    monkeypatch.setattr(
        capital_guard,
        "is_pre_earnings_entry_window",
        lambda *_args, **_kwargs: (
            True,
            "pre_earnings_window_open",
            {"status": "window_open", "session": "BMO"},
        ),
    )
    execution_runtime._BROKERS.clear()

    risk = RiskAgent(config).evaluate_signal(signal)
    assert risk["accepted"] is True, risk

    capital_gate = evaluate_capital_entry(signal, [], execution_cfg)
    assert capital_gate["allowed"] is True, capital_gate
    assert capital_gate["paper_capital_experiment"] is True
    signal["paper_capital_experiment"] = True

    confidence = ConfidenceGates(config).evaluate(
        signal,
        perf_cache=StrategyPerformanceCache(tmp_path / f"{source}-performance.json"),
    )
    assert confidence["tier"] == "C", confidence
    assert confidence["adjusted_size_factor"] == pytest.approx(0.25)
    signal["adjusted_size_factor"] = confidence["adjusted_size_factor"]
    signal["ollama_decision"] = {
        "available": True,
        "recommendation": "buy" if signal["action"] == "BUY" else "short",
        "confidence": 0.85,
        "leverage": 1,
    }
    signal["decision_context"] = {
        "strategy_type": signal["strategy_type"],
        "ollama": signal["ollama_decision"],
    }

    opened = ExecutionAgent(config)._process_paper_signal(
        execution_cfg,
        signal["ticker"],
        signal["action"],
        signal["id"],
        signal,
        risk_evaluation=risk,
    )

    assert opened is True
    assert len(capital.positions) == 1
    assert capital.positions[0]["status"] == "OPEN"
    assert capital.positions[0]["ticker"] == expected_ticker
    assert capital.positions[0]["direction"] == expected_direction
    assert capital.open_calls[0]["size_usd"] == pytest.approx(200.0)
    assert signals.attached[0][0] == signal["id"]
