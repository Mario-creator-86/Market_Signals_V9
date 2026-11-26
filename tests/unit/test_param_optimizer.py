"""Unit tests for ParamOptimizer."""

import pytest
from unittest.mock import MagicMock, patch
from core.param_optimizer import ParamOptimizer


@pytest.fixture
def optimizer():
    cfg = MagicMock()
    return ParamOptimizer(config_manager=cfg)


class TestRunOptimization:
    def test_insufficient_data(self, optimizer):
        with patch("core.feedback_loop.get_recent_trades", return_value=[]):
            result = optimizer.run_optimization()
        assert result["status"] == "insufficient_data"
        assert result["suggestions"] == []

    def test_fewer_than_50_trades(self, optimizer):
        trades = [{"net_pnl": 10, "pnl_pct": 1.0, "strategy_type": "MOM"}] * 30
        with patch("core.feedback_loop.get_recent_trades", return_value=trades):
            result = optimizer.run_optimization()
        assert result["status"] == "insufficient_data"

    def test_enough_trades_no_improvement(self, optimizer):
        # 60 trades with zero PnL — no improvement possible
        trades = [
            {"net_pnl": 0.0, "pnl_pct": 0.0, "strategy_type": "MOMENTUM"}
        ] * 60
        with patch("core.feedback_loop.get_recent_trades", return_value=trades):
            result = optimizer.run_optimization()
        # PF will be 0, no improvement
        assert result["status"] in ("no_improvement", "insufficient_data")


class TestApplySuggestions:
    def test_no_config_manager(self):
        opt = ParamOptimizer(config_manager=None)
        result = opt.apply_suggestions([])
        assert result["status"] == "error"

    def test_skip_below_5pct_improvement(self, optimizer):
        suggestions = [{"strategy": "MOM", "improvement_pct": 3.0, "params": {}}]
        result = optimizer.apply_suggestions(suggestions)
        assert result["strategies"] == []

    def test_apply_valid_suggestion(self, optimizer):
        optimizer.config_manager._config = {}
        suggestions = [{
            "strategy": "MOMENTUM",
            "improvement_pct": 10.0,
            "params": {"atr_sl_multiplier": 1.5, "atr_tp_multiplier": 3.0, "trailing_activation_r": 1.0},
        }]
        result = optimizer.apply_suggestions(suggestions)
        assert "MOMENTUM" in result["strategies"]


class TestComputePF:
    def test_all_winners(self, optimizer):
        trades = [{"net_pnl": 10}, {"net_pnl": 20}]
        pf = optimizer._compute_pf(trades)
        assert pf is None

    def test_all_losers(self, optimizer):
        trades = [{"net_pnl": -10}, {"net_pnl": -20}]
        pf = optimizer._compute_pf(trades)
        assert pf < 1.0

    def test_mixed(self, optimizer):
        trades = [{"net_pnl": 100}, {"net_pnl": -50}]
        pf = optimizer._compute_pf(trades)
        assert pf == 2.0  # 100/50


class TestSimulateExits:
    def test_simulated_trades_count_matches(self, optimizer):
        trades = [
            {"net_pnl": 10, "pnl_pct": 1.0},
            {"net_pnl": -5, "pnl_pct": -0.5},
        ]
        simulated = optimizer._simulate_exits(trades, sl_mult=2.0, tp_mult=3.0, trail_r=1.0)
        assert len(simulated) == 2
