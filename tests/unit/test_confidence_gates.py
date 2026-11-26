"""Unit tests for ConfidenceGates."""

import pytest
from unittest.mock import MagicMock
from core.confidence_gates import ConfidenceGates


@pytest.fixture
def gates():
    cfg = MagicMock()
    cfg.bot = {
        "min_edge_score": 75,
        "max_trades_per_day": 5,
        "max_same_strategy_concurrent": 2,
        "consecutive_loss_cooldown": 1,
        "require_strategy_performance": False,
    }
    return ConfidenceGates(config_manager=cfg)


def _make_signal(score=80, validation=60, strategy_type="MOMENTUM", ticker="AAPL", signal_mode="BUY"):
    return {
        "signal_score": score,
        "validation_score": validation,
        "strategy_type": strategy_type,
        "ticker": ticker,
        "type": signal_mode,
        "strategy_source": "V2",
        "asset_class": "US_STOCKS",
        "trade_radar_opportunity": 75,
        "sentiment_score": 0.0,
        "gap_risk": 0,
    }


class TestTierAssignment:
    def test_tier_a_full_size(self, gates):
        sig = _make_signal(score=90, validation=75)
        result = gates.evaluate(sig)
        assert result["tier"] == "A"
        assert result["adjusted_size_factor"] == 1.0

    def test_tier_b_half_size(self, gates):
        sig = _make_signal(score=78, validation=55)
        result = gates.evaluate(sig)
        assert result["tier"] == "B"
        assert result["adjusted_size_factor"] == 0.5

    def test_tier_c_quarter_size(self, gates):
        sig = _make_signal(score=76, validation=20)
        result = gates.evaluate(sig)
        assert result["tier"] == "C"
        assert result["adjusted_size_factor"] == 0.25

    def test_skip_below_min_edge(self, gates):
        sig = _make_signal(score=50, validation=80)
        result = gates.evaluate(sig)
        assert result["tier"] == "SKIP"
        assert result["adjusted_size_factor"] == 0.0

    def test_short_allows_negative_sentiment(self, gates):
        sig = _make_signal(score=82, validation=60, signal_mode="SHORT")
        sig["sentiment_score"] = -0.8
        result = gates.evaluate(sig)
        assert result["tier"] != "SKIP"

    def test_short_rejects_strong_positive_sentiment(self, gates):
        sig = _make_signal(score=82, validation=60, signal_mode="SHORT")
        sig["sentiment_score"] = 0.8
        result = gates.evaluate(sig)
        assert result["tier"] == "SKIP"
        assert any("contradicts SHORT" in r for r in result["reasons"])

    def test_pre_earnings_gap_risk_is_not_auto_skip(self, gates):
        sig = _make_signal(score=84, validation=60, strategy_type="PRE_EARNINGS_AI_SHORT", signal_mode="SHORT")
        sig["autonomous_source"] = "pre_earnings"
        sig["gap_risk"] = 82
        result = gates.evaluate(sig)
        assert result["tier"] != "SKIP"
        assert any("Pre-earnings gap risk" in r for r in result["reasons"])

    def test_non_event_gap_risk_still_skips(self, gates):
        sig = _make_signal(score=84, validation=60, strategy_type="MOMENTUM", signal_mode="BUY")
        sig["gap_risk"] = 82
        result = gates.evaluate(sig)
        assert result["tier"] == "SKIP"
        assert any("Gap risk" in r for r in result["reasons"])


class TestDailyTradeLimit:
    def test_daily_limit_reached(self, gates):
        # Use different strategies to avoid same-strategy limit
        for i in range(5):
            sig = _make_signal(score=90, validation=75, strategy_type=f"STRAT_{i}")
            gates.evaluate(sig)
        # 6th trade hits daily limit
        sig = _make_signal(score=90, validation=75, strategy_type="STRAT_6")
        result = gates.evaluate(sig)
        assert result["tier"] == "SKIP"
        assert any("Daily" in r and "limit" in r.lower() for r in result["reasons"])


class TestConsecutiveLossCooldown:
    def test_cooldown_after_losses(self, gates):
        gates._consecutive_losses = 2
        sig = _make_signal(score=90, validation=75)
        result = gates.evaluate(sig)
        assert result["tier"] == "SKIP"
        assert any("Cooldown" in r for r in result["reasons"])

    def test_no_cooldown_after_win(self, gates):
        gates._consecutive_losses = 1
        gates.record_trade_outcome(True)  # win resets
        assert gates._consecutive_losses == 0


class TestSameStrategyConcurrent:
    def test_max_concurrent_reached(self, gates):
        sig = _make_signal(score=90, validation=75, strategy_type="MOMENTUM")
        gates.evaluate(sig)  # 1
        gates.evaluate(sig)  # 2
        result = gates.evaluate(sig)  # 3 — should be blocked
        assert result["tier"] == "SKIP"
        assert any("Max concurrent" in r for r in result["reasons"])


class TestPerfCacheIntegration:
    def test_suspended_strategy_skipped(self, gates):
        sig = _make_signal(score=90, validation=75, strategy_type="MOMENTUM")
        perf_cache = MagicMock()
        perf_cache.get_entry.return_value = {
            "suspended": True,
            "degraded": False,
            "sample_size": 50,
            "win_rate": 0.3,
            "profit_factor": 0.5,
        }
        result = gates.evaluate(sig, perf_cache=perf_cache)
        assert result["tier"] == "SKIP"

    def test_degraded_strategy_downgraded(self, gates):
        sig = _make_signal(score=90, validation=75, strategy_type="MOMENTUM")
        perf_cache = MagicMock()
        perf_cache.get_entry.return_value = {
            "suspended": False,
            "degraded": True,
            "sample_size": 50,
            "win_rate": 0.38,
            "profit_factor": 1.3,
            "expectancy_R": 0.2,
            "max_drawdown": 8.0,
        }
        result = gates.evaluate(sig, perf_cache=perf_cache)
        # Tier A -> B due to degraded
        assert result["tier"] == "B"
        assert result["adjusted_size_factor"] == 0.5

    def test_low_profit_factor_strategy_skipped(self, gates):
        gates.config_manager.bot["require_strategy_performance"] = True
        sig = _make_signal(score=90, validation=75, strategy_type="MOMENTUM")
        perf_cache = MagicMock()
        perf_cache.get_entry.return_value = {
            "suspended": False,
            "degraded": False,
            "sample_size": 50,
            "win_rate": 0.62,
            "profit_factor": 0.9,
            "expectancy_R": 0.2,
            "max_drawdown": 8.0,
        }
        result = gates.evaluate(sig, perf_cache=perf_cache)
        assert result["tier"] == "SKIP"
        assert any("Profit factor" in r for r in result["reasons"])

    def test_insufficient_sample_strategy_skipped(self, gates):
        gates.config_manager.bot["require_strategy_performance"] = True
        sig = _make_signal(score=90, validation=75, strategy_type="MOMENTUM")
        perf_cache = MagicMock()
        perf_cache.get_entry.return_value = {
            "suspended": False,
            "degraded": False,
            "sample_size": 12,
            "win_rate": 0.70,
            "profit_factor": 1.8,
            "expectancy_R": 0.4,
            "max_drawdown": 5.0,
        }
        result = gates.evaluate(sig, perf_cache=perf_cache)
        assert result["tier"] == "SKIP"
        assert any("Sample size" in r for r in result["reasons"])

    def test_trade_radar_unavailable_skips_by_default(self, gates):
        sig = _make_signal(score=90, validation=75, strategy_type="MOMENTUM")
        sig["trade_radar_opportunity"] = 0.0
        sig["agent_reasons"] = ["trade_radar_agent_not_ready"]
        result = gates.evaluate(sig)
        assert result["tier"] == "SKIP"
        assert any("Trade radar unavailable" in r for r in result["reasons"])

    def test_autonomous_missing_performance_uses_reduced_fallback(self, gates):
        gates.config_manager.bot["require_strategy_performance"] = True
        sig = _make_signal(score=90, validation=55, strategy_type="SCANNER_AI_LONG", ticker="WOLF")
        sig["autonomous_source"] = "scanner"
        perf_cache = MagicMock()
        perf_cache.get_entry.return_value = {}

        result = gates.evaluate(sig, perf_cache=perf_cache)

        assert result["tier"] == "C"
        assert result["adjusted_size_factor"] == 0.25
        assert any("No validated performance" in r for r in result["reasons"])

    def test_paper_s7_experiment_uses_reduced_unvalidated_size(self, gates):
        gates.config_manager.bot["require_strategy_performance"] = True
        gates.config_manager.bot["allow_unvalidated_autonomous_fallback"] = False
        sig = _make_signal(
            score=90,
            validation=0,
            strategy_type="S7_LIQUIDITY_SWEEP_REVERSAL",
            ticker="BTC-USD",
        )
        sig.update(
            {
                "strategy_source": "V2_FORWARD_RESEARCH",
                "autonomous_source": "v2_forward",
                "paper_capital_experiment": True,
                "asset_class": "CRYPTO_FUTURES",
            }
        )
        perf_cache = MagicMock()
        perf_cache.get_entry.return_value = {}

        result = gates.evaluate(sig, perf_cache=perf_cache)

        assert result["tier"] == "C"
        assert result["adjusted_size_factor"] == 0.25
        assert any("Paper S7 Capital experiment" in reason for reason in result["reasons"])

    def test_autonomous_trade_radar_unavailable_uses_reduced_fallback(self, gates):
        sig = _make_signal(score=90, validation=75, strategy_type="SCANNER_AI_LONG", ticker="WOLF")
        sig["autonomous_source"] = "scanner"
        sig["trade_radar_opportunity"] = 0.0
        sig["agent_reasons"] = ["trade_radar_agent_not_ready"]

        result = gates.evaluate(sig)

        assert result["tier"] == "C"
        assert result["adjusted_size_factor"] == 0.25
        assert any("Trade radar unavailable" in r for r in result["reasons"])

    def test_missing_performance_cache_skips_non_exempt_strategy(self, gates):
        gates.config_manager.bot["require_strategy_performance"] = True
        sig = _make_signal(score=90, validation=75, strategy_type="MOMENTUM")

        result = gates.evaluate(sig, perf_cache=None)

        assert result["tier"] == "SKIP"
        assert any("performance cache unavailable" in r for r in result["reasons"])

    def test_autonomous_scanner_uses_execution_score_floor(self):
        cfg = MagicMock()
        cfg.bot = {
            "min_edge_score": 80,
            "max_trades_per_day": 5,
            "max_same_strategy_concurrent": 2,
            "consecutive_loss_cooldown": 1,
            "require_strategy_performance": False,
            "require_trade_radar_confirmation": False,
        }
        cfg.agents = {"execution": {"scanner_min_score": 78.0}}
        gates = ConfidenceGates(config_manager=cfg)
        sig = _make_signal(score=78, validation=55, strategy_type="SCANNER_AI_LONG", ticker="SOL-USD")
        sig["autonomous_source"] = "scanner"
        sig["asset_class"] = "CRYPTO_FUTURES"

        result = gates.evaluate(sig)

        assert result["tier"] != "SKIP"

    def test_same_strategy_limit_is_partitioned_by_asset_class(self, gates):
        stock = _make_signal(score=90, validation=75, strategy_type="SCANNER_AI_SHORT", signal_mode="SHORT")
        stock["asset_class"] = "US_STOCKS"
        crypto = _make_signal(score=90, validation=75, strategy_type="SCANNER_AI_SHORT", ticker="BTC-USD", signal_mode="SHORT")
        crypto["asset_class"] = "CRYPTO_FUTURES"

        gates.evaluate(stock)
        gates.evaluate(stock)
        result = gates.evaluate(crypto)

        assert result["tier"] != "SKIP"
