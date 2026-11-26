"""Unit tests for BotController."""

import pytest
from unittest.mock import MagicMock, patch
from core.bot_controller import BotController


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset BotController singleton between tests."""
    BotController._instance = None
    yield
    BotController._instance = None


@pytest.fixture
def mock_config():
    cfg = MagicMock()
    cfg.bot = {
        "mode": "paper",
        "min_edge_score": 75,
        "circuit_breaker_drawdown_pct": 10.0,
        "circuit_breaker_daily_loss_pct": 5.0,
    }
    cfg._config = {}
    cfg.save = MagicMock()
    return cfg


@pytest.fixture
def mock_agent_manager():
    am = MagicMock()
    am.set_bot_mode = MagicMock()
    return am


class TestBotControllerStartStop:
    def test_start_paper_mode(self, mock_config, mock_agent_manager):
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)
        result = bc.start()
        assert result["status"] == "started"
        assert result["mode"] == "paper"
        assert bc.is_running

    def test_start_writes_enabled_to_config(self, mock_config, mock_agent_manager):
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)
        bc.start()
        # _set_bot_config("enabled", True) should have been called
        assert mock_config._config.get("bot", {}).get("enabled") is True
        mock_config.save.assert_called()

    def test_start_calls_agent_manager(self, mock_config, mock_agent_manager):
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)
        bc.start()
        mock_agent_manager.set_bot_mode.assert_called_once_with(True)

    def test_start_already_running(self, mock_config, mock_agent_manager):
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)
        bc.start()
        result = bc.start()
        assert result["status"] == "already_running"

    def test_stop(self, mock_config, mock_agent_manager):
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)
        bc.start()
        result = bc.stop(reason="manual")
        assert result["status"] == "stopped"
        assert not bc.is_running
        mock_agent_manager.set_bot_mode.assert_called_with(False)

    def test_stop_writes_disabled_to_config(self, mock_config, mock_agent_manager):
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)
        bc.start()
        bc.stop(reason="manual")
        assert mock_config._config.get("bot", {}).get("enabled") is False

    def test_stop_not_running(self, mock_config, mock_agent_manager):
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)
        result = bc.stop()
        assert result["status"] == "already_stopped"
        assert mock_config._config.get("bot", {}).get("enabled") is False

    def test_stop_clears_persisted_switch_after_controller_restart(self, mock_config, mock_agent_manager):
        mock_config.bot["enabled"] = True
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)

        result = bc.stop(reason="api_request")

        assert result["status"] == "stopped"
        assert mock_config._config["bot"]["enabled"] is False


class TestBotControllerStatus:
    def test_status_when_stopped(self, mock_config, mock_agent_manager):
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)
        status = bc.get_status()
        assert status["running"] is False
        assert status["mode"] == "paper"
        assert status["circuit_breaker_active"] is False

    def test_status_when_running(self, mock_config, mock_agent_manager):
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)
        bc.start()
        status = bc.get_status()
        assert status["running"] is True
        assert status["min_edge_score"] == 75

    def test_status_uses_persisted_master_switch(self, mock_config, mock_agent_manager):
        mock_config.bot["enabled"] = True
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)

        status = bc.get_status()

        assert status["running"] is True
        assert status["configured_enabled"] is True
        assert status["controller_running"] is False

    def test_refreshing_dependencies_preserves_circuit_breaker(self, mock_config, mock_agent_manager):
        bc = BotController.get_instance(config_manager=mock_config, agent_manager=mock_agent_manager)
        bc._circuit_breaker_active = True

        BotController.get_instance(config_manager=MagicMock())

        assert bc._circuit_breaker_active is True

    def test_capital_reset_rebases_daily_pnl_and_clears_artificial_breaker(self, mock_config, mock_agent_manager):
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)
        bc._day_start_balance = 14509.23
        bc._day_start_date = "2026-08-27"
        bc._circuit_breaker_active = True

        assert BotController.notify_capital_reset(10000.0) is True
        assert bc._day_start_balance == 10000.0
        assert bc._circuit_breaker_active is False

        cm = MagicMock()
        cm.get_deployable_balance.return_value = 10000.0
        cm.get_positions.return_value = []
        with patch("core.capital_manager.get_capital_manager", return_value=cm):
            status = bc.get_status()

        assert status["daily_pnl"] == 0.0
        assert status["drawdown_pct"] == 0.0

    def test_reserved_margin_is_not_reported_as_daily_loss(self, mock_config, mock_agent_manager):
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)
        bc._day_start_balance = 1000.0
        bc._day_start_date = "2026-07-31"
        cm = MagicMock()
        cm.get_balance.return_value = 899.60
        cm.get_positions.return_value = [{
            "status": "OPEN",
            "direction": "LONG",
            "margin_used": 100.0,
            "entry_price": 50.0,
            "current_price": 50.0,
            "qty": 2.0,
        }]

        with patch("core.capital_manager.get_capital_manager", return_value=cm):
            status = bc.get_status()

        assert status["balance"] == 899.60
        assert status["equity"] == 999.60
        assert status["daily_pnl"] == -0.40
        assert status["drawdown_pct"] == 0.04

    def test_research_position_is_excluded_from_capital_status(self, mock_config, mock_agent_manager):
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)
        bc._day_start_balance = 1000.0
        cm = MagicMock()
        cm.get_deployable_balance.return_value = 1000.0
        cm.get_positions.return_value = [{
            "status": "OPEN",
            "position_type": "research_cohort",
            "research_only": True,
            "margin_used": 100.0,
            "unrealized_pnl": -20.0,
        }]

        with patch("core.capital_manager.get_capital_manager", return_value=cm):
            status = bc.get_status()

        assert status["balance"] == 1000.0
        assert status["equity"] == 1000.0
        assert status["daily_pnl"] == 0.0
        assert status["positions_open"] == 0
        assert status["research_positions_open"] == 1


class TestBotControllerCanGoLive:
    def test_not_eligible_with_no_trades(self, mock_config, mock_agent_manager):
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)
        with patch("core.feedback_loop.get_recent_trades", return_value=[]):
            result = bc.can_go_live()
        assert result["eligible"] is False
        assert any("100" in reason for reason in result["reasons"])

    def test_not_eligible_low_win_rate(self, mock_config, mock_agent_manager):
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)
        with patch("core.feedback_loop.compute_portfolio_metrics") as mock_metrics:
            mock_metrics.return_value = {
                "win_rate": 0.45,
                "profit_factor": 0.8,
                "max_drawdown_pct": 20.0,
                "expectancy": -2.5,
            }
            with patch("core.feedback_loop.get_recent_trades", return_value=[{"net_pnl": -10}] * 100):
                result = bc.can_go_live()
        assert result["eligible"] is False


class TestBotControllerSelfAssessment:
    @staticmethod
    def _metrics(win_rate=0.55, profit_factor=2.0, expectancy=5.0, drawdown=3.0):
        return {
            "total_trades": 100,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "expectancy": expectancy,
            "max_drawdown_pct": drawdown,
        }

    def test_does_not_lower_edge_on_profit_factor_alone(self, mock_config, mock_agent_manager):
        mock_config.bot.update({"enabled": False, "min_edge_score": 80, "self_assessment_sample_size": 100})
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)
        with patch("core.feedback_loop.get_verified_capital_trades", return_value=[{}] * 100), patch(
            "core.feedback_loop.compute_portfolio_metrics",
            return_value=self._metrics(win_rate=0.45, profit_factor=2.2),
        ):
            result = bc._run_self_assessment_once()

        assert result["status"] == "unchanged"
        assert result["min_edge_score"] == 80

    def test_lowers_edge_only_when_all_quality_gates_pass(self, mock_config, mock_agent_manager):
        mock_config.bot.update({"enabled": False, "min_edge_score": 80, "self_assessment_sample_size": 100})
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)
        with patch("core.feedback_loop.get_verified_capital_trades", return_value=[{}] * 100), patch(
            "core.feedback_loop.compute_portfolio_metrics",
            return_value=self._metrics(win_rate=0.55, profit_factor=2.2, expectancy=4.0, drawdown=4.0),
        ):
            result = bc._run_self_assessment_once()

        assert result["status"] == "updated"
        assert result["min_edge_score"] == 75

    def test_raises_edge_for_negative_expectancy(self, mock_config, mock_agent_manager):
        mock_config.bot.update({"enabled": False, "min_edge_score": 80, "self_assessment_sample_size": 100})
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)
        with patch("core.feedback_loop.get_verified_capital_trades", return_value=[{}] * 100), patch(
            "core.feedback_loop.compute_portfolio_metrics",
            return_value=self._metrics(win_rate=0.60, profit_factor=1.2, expectancy=-1.0),
        ):
            result = bc._run_self_assessment_once()

        assert result["status"] == "updated"
        assert result["min_edge_score"] == 85


class TestBotControllerInvalidMode:
    def test_invalid_mode_rejected(self, mock_config, mock_agent_manager):
        mock_config.bot = {"mode": "invalid_mode", "min_edge_score": 75}
        bc = BotController(config_manager=mock_config, agent_manager=mock_agent_manager)
        result = bc.start()
        assert result["status"] == "error"
