"""Unit tests for feedback_loop module."""

import pytest
import json
from unittest.mock import MagicMock, patch
from core.feedback_loop import (
    process_closed_trade,
    compute_portfolio_metrics,
    get_verified_capital_trades,
    get_recent_trades,
    _append_to_journal,
)


class TestComputePortfolioMetrics:
    def test_empty_trades(self):
        result = compute_portfolio_metrics([])
        assert result["total_trades"] == 0
        assert result["win_rate"] == 0.0
        assert result["profit_factor"] is None

    def test_all_wins(self):
        trades = [{"net_pnl": 10}, {"net_pnl": 20}, {"net_pnl": 30}]
        result = compute_portfolio_metrics(trades)
        assert result["total_trades"] == 3
        assert result["win_rate"] == 1.0
        assert result["profit_factor"] is None

    def test_all_losses(self):
        trades = [{"net_pnl": -10}, {"net_pnl": -20}]
        result = compute_portfolio_metrics(trades)
        assert result["win_rate"] == 0.0
        assert result["profit_factor"] < 1.0

    def test_mixed_trades(self):
        trades = [
            {"net_pnl": 100},
            {"net_pnl": -50},
            {"net_pnl": 75},
            {"net_pnl": -25},
        ]
        result = compute_portfolio_metrics(trades)
        assert result["total_trades"] == 4
        assert 0.4 < result["win_rate"] < 0.6
        assert result["profit_factor"] > 1.0
        assert result["expectancy"] > 0


class TestProcessClosedTrade:
    def test_empty_position_ignored(self):
        # Should not raise
        process_closed_trade({})

    def test_no_ticker_ignored(self):
        process_closed_trade({"pnl": 100})

    def test_valid_trade_processed(self, tmp_path):
        with patch("core.feedback_loop._append_to_journal"), \
             patch("core.feedback_loop._update_confidence_adjustments"), \
             patch("core.strategy_v2.context_builder.infer_asset_class", return_value="US_STOCKS"), \
             patch("core.strategy_v2.performance_cache.StrategyPerformanceCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache_cls.return_value = mock_cache
            process_closed_trade({
                "ticker": "AAPL",
                "strategy_type": "MOMENTUM",
                "direction": "LONG",
                "net_pnl": 100.0,
                "pnl_pct_notional": 5.0,
                "entry_price": 150.0,
                "stop_loss_price": 145.0,
            })
            mock_cache.upsert_from_trade_result.assert_called_once()

    def test_research_cohort_trade_does_not_train_performance_cache(self, tmp_path):
        research_path = tmp_path / "research_cohort_journal.json"
        with patch("core.feedback_loop.RESEARCH_COHORT_JOURNAL_PATH", research_path), \
             patch("core.strategy_v2.performance_cache.StrategyPerformanceCache") as mock_cache_cls:
            process_closed_trade({
                "position_id": "pos_research_1",
                "position_type": "research_cohort",
                "research_only": True,
                "ticker": "BTC-USD",
                "strategy_type": "SCANNER_AI_LONG",
                "direction": "LONG",
                "net_pnl": 12.5,
                "pnl_pct_notional": 5.0,
                "entry_price": 100.0,
                "exit_price": 105.0,
                "entry_price_source": "binance_orderbook",
                "exit_price_source": "binance_orderbook",
                "entry_price_quality": {"level": "VERIFIED", "is_valid": True},
                "exit_price_quality": {"level": "VERIFIED", "is_valid": True},
                "closed_at": "2026-07-15T10:00:00+00:00",
            })

        mock_cache_cls.assert_not_called()
        journal = json.loads(research_path.read_text())
        assert journal[0]["position_type"] == "research_cohort"
        assert journal[0]["research_only"] is True
        assert journal[0]["entry_price_source"] == "binance_orderbook"


class TestGetRecentTrades:
    def test_no_journal_file(self, tmp_path):
        with patch("core.feedback_loop.FEEDBACK_JOURNAL_PATH", tmp_path / "nonexistent.json"):
            result = get_recent_trades(50)
            assert result == []

    def test_verified_capital_trades_exclude_research_manual_and_unverified(self):
        verified = {
            "position_id": "verified",
            "position_type": "paper_signal",
            "status": "CLOSED",
            "net_pnl": 10.0,
            "closed_at": "2026-07-15T10:00:00+00:00",
            "entry_price": 100.0,
            "exit_price": 101.0,
            "entry_price_source": "binance_orderbook",
            "exit_price_source": "binance_orderbook",
            "entry_price_quality": {"level": "VERIFIED", "is_valid": True},
            "exit_price_quality": {"level": "VERIFIED", "is_valid": True},
        }
        research = {**verified, "position_id": "research", "position_type": "research_cohort", "research_only": True}
        unverified = {**verified, "position_id": "unverified", "exit_price_quality": {"level": "CACHED", "is_valid": True}}
        manual = {**verified, "position_id": "manual", "position_type": "manual"}

        result = get_verified_capital_trades(50, positions=[research, unverified, manual, verified])

        assert [trade["position_id"] for trade in result] == ["verified"]


class TestAppendToJournal:
    def test_creates_journal_file(self, tmp_path):
        journal_path = tmp_path / "trade_journal.json"
        with patch("core.feedback_loop.FEEDBACK_JOURNAL_PATH", journal_path):
            _append_to_journal({
                "ticker": "AAPL",
                "strategy_type": "MOMENTUM",
                "direction": "LONG",
                "net_pnl": 100.0,
            })
        assert journal_path.exists()
        data = json.loads(journal_path.read_text())
        assert len(data) == 1
        assert data[0]["ticker"] == "AAPL"

    def test_infers_crypto_asset_class_when_position_did_not_persist_it(self, tmp_path):
        journal_path = tmp_path / "trade_journal.json"
        with patch("core.feedback_loop.FEEDBACK_JOURNAL_PATH", journal_path):
            _append_to_journal({
                "ticker": "VET-USD",
                "strategy_type": "SCANNER_AI_SHORT",
                "direction": "SHORT",
                "net_pnl": -1.0,
            })

        data = json.loads(journal_path.read_text())
        assert data[0]["asset_class"] == "CRYPTO_FUTURES"
