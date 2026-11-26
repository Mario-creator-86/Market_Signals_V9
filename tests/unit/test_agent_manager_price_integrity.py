import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.agent_manager import AgentManager


def _manager():
    manager = object.__new__(AgentManager)
    manager.config_manager = SimpleNamespace(
        signal={"price_integrity": {"max_deviation_pct": {"crypto": 2.5, "stock": 3.0}}}
    )
    return manager


def _signal(**overrides):
    payload = {
        "ticker": "DODO-USD",
        "asset_class": "CRYPTO_FUTURES",
        "action": "SHORT",
        "entry": 0.02720,
    }
    payload.update(overrides)
    return payload


def test_selected_autonomous_candidate_carries_verified_price_concordance():
    capital = MagicMock()
    capital._get_execution_price.return_value = {
        "execution_price": 0.02724,
        "source": "Binance_orderbook",
        "price_timestamp": "2026-07-16T10:00:00+00:00",
        "execution_price_quality": {"level": "VERIFIED", "is_valid": True},
    }
    capital._is_verified_execution_payload.return_value = True

    with patch("core.capital_manager.get_capital_manager", return_value=capital):
        result = _manager()._attach_candidate_price_integrity(_signal())

    assert result["price_verified"] is True
    assert result["price_integrity"]["status"] == "verified"
    assert result["has_data_conflict"] is False
    assert result["data_conflict_evidence"]["has_conflict"] is False
    assert result["price_evidence"]["live_source"] == "Binance_orderbook"
    capital._get_execution_price.assert_called_once_with("DODO-USD", "SELL", use_cache=False)


def test_selected_autonomous_candidate_fails_closed_without_verified_execution_quote():
    capital = MagicMock()
    capital._get_execution_price.return_value = {
        "execution_price": 0.02724,
        "source": "fallback",
        "execution_price_quality": {"level": "ESTIMATED", "is_valid": True},
    }
    capital._is_verified_execution_payload.return_value = False

    with patch("core.capital_manager.get_capital_manager", return_value=capital):
        result = _manager()._attach_candidate_price_integrity(_signal())

    assert result["price_verified"] is False
    assert result["price_integrity"]["status"] == "single_source"
    assert result["has_data_conflict"] is False
    assert result["price_evidence"]["execution_price_verified"] is False


def test_selected_autonomous_candidate_marks_real_price_disagreement_as_conflict():
    capital = MagicMock()
    capital._get_execution_price.return_value = {
        "execution_price": 0.03050,
        "source": "Binance_orderbook",
        "price_timestamp": "2026-07-16T10:00:00+00:00",
        "execution_price_quality": {"level": "VERIFIED", "is_valid": True},
    }
    capital._is_verified_execution_payload.return_value = True

    with patch("core.capital_manager.get_capital_manager", return_value=capital):
        result = _manager()._attach_candidate_price_integrity(_signal())

    assert result["price_integrity"]["status"] == "conflict"
    assert result["price_verified"] is False
    assert result["has_data_conflict"] is True
    assert result["data_conflict_evidence"]["has_conflict"] is True


def test_manual_autonomous_cycle_does_not_overlap_background_cycle():
    manager = object.__new__(AgentManager)
    manager._autonomous_cycle_lock = threading.Lock()
    calls = []
    manager._autonomous_monitor_once = lambda: calls.append("ran") or {"status": "no_candidates"}

    result = manager.run_autonomous_cycle_now()

    assert result == {
        "status": "completed",
        "accepted": True,
        "cycle": {"status": "no_candidates"},
    }
    assert calls == ["ran"]

    manager._autonomous_cycle_lock.acquire()
    try:
        concurrent = manager.run_autonomous_cycle_now()
    finally:
        manager._autonomous_cycle_lock.release()
    assert concurrent == {"status": "already_running", "accepted": False}


def test_stale_scanner_snapshot_does_not_create_autonomous_candidates():
    manager = object.__new__(AgentManager)
    manager.agents = {
        "scanner": SimpleNamespace(
            get_state=lambda: {
                "data_age_seconds": 121,
                "hot_tickers": [
                    {
                        "symbol": "SOLUSDT",
                        "price": 150.0,
                        "change_pct": 10.0,
                        "quoteVolume": 50_000_000,
                        "source": "binance",
                    }
                ],
            }
        )
    }

    result = manager._scanner_candidates(
        {
            "autonomous_scanner_max_data_age_seconds": 120,
            "scanner_entry_mode": "momentum",
            "scanner_min_abs_change_pct": 5.0,
            "scanner_min_score": 78.0,
        }
    )

    assert result == []


def test_forward_v2_candidates_filter_non_crypto_before_the_evaluation_cap(monkeypatch):
    manager = object.__new__(AgentManager)
    manager._autonomous_source_stats = {}
    manager.config_manager = SimpleNamespace(strategy_v2={})
    manager.agents = {
        "scanner": SimpleNamespace(
            get_state=lambda: {
                "data_age_seconds": 5,
                "hot_tickers": [
                    {"symbol": "AAL", "volume": 1_000_000_000},
                    {"symbol": "NU", "volume": 900_000_000},
                    {"symbol": "ETHUSDT", "quoteVolume": 100_000_000},
                    {"symbol": "SOLUSDT", "quoteVolume": 90_000_000},
                ],
            }
        )
    }
    seen = []

    def fake_build(item, **_kwargs):
        seen.append(item["symbol"])
        return None

    monkeypatch.setattr("core.forward_v2.build_forward_v2_candidate", fake_build)
    result = manager._forward_v2_candidates(
        {
            "v2_forward": {
                "enabled": True,
                "asset_classes": ["CRYPTO_FUTURES"],
                "max_symbols_per_cycle": 2,
                "max_scanner_data_age_seconds": 120,
            }
        }
    )

    assert result == []
    assert set(seen) == {"ETHUSDT", "SOLUSDT"}
    assert manager._autonomous_source_stats["v2_forward"]["evaluated_symbols"] == ["ETHUSDT", "SOLUSDT"]


def test_forward_v2_candidates_use_liquid_crypto_universe_before_hot_movers(monkeypatch):
    manager = object.__new__(AgentManager)
    manager._autonomous_source_stats = {}
    manager.config_manager = SimpleNamespace(strategy_v2={})
    manager.agents = {
        "scanner": SimpleNamespace(
            get_state=lambda: {
                "data_age_seconds": 5,
                "hot_tickers": [{"symbol": "MEMEUSDT", "quoteVolume": 1_000_000_000}],
                "liquid_crypto_tickers": [
                    {"symbol": "BTCUSDT", "quoteVolume": 900_000_000},
                    {"symbol": "ETHUSDT", "quoteVolume": 800_000_000},
                ],
            }
        )
    }
    seen = []

    def fake_build(item, **_kwargs):
        seen.append(item["symbol"])
        return None

    monkeypatch.setattr("core.forward_v2.build_forward_v2_candidate", fake_build)
    result = manager._forward_v2_candidates(
        {
            "v2_forward": {
                "enabled": True,
                "asset_classes": ["CRYPTO_FUTURES"],
                "max_symbols_per_cycle": 2,
                "universe_size": 2,
                "max_scanner_data_age_seconds": 120,
            }
        }
    )

    assert result == []
    assert set(seen) == {"BTCUSDT", "ETHUSDT"}
    assert manager._autonomous_source_stats["v2_forward"]["evaluated_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert manager._autonomous_source_stats["v2_forward"]["universe_source"] == "binance_liquid"


def test_forward_v2_candidates_cover_crypto_stocks_and_commodities(monkeypatch):
    manager = object.__new__(AgentManager)
    manager._autonomous_source_stats = {}
    manager.config_manager = SimpleNamespace(strategy_v2={}, watchlist={"yahoo": ["AAPL"]})
    manager.agents = {
        "scanner": SimpleNamespace(
            get_state=lambda: {
                "data_age_seconds": 5,
                "hot_tickers": [{"symbol": "AAPL", "volume": 5_000_000}],
                "liquid_crypto_tickers": [{"symbol": "BTCUSDT", "quoteVolume": 900_000_000}],
            }
        )
    }
    seen = []

    def fake_build(item, **_kwargs):
        seen.append(item["symbol"])
        return None

    monkeypatch.setattr("core.forward_v2.build_forward_v2_candidate", fake_build)
    result = manager._forward_v2_candidates(
        {
            "v2_forward": {
                "enabled": True,
                "asset_classes": ["CRYPTO_FUTURES", "US_STOCKS", "COMMODITIES"],
                "max_symbols_per_asset_class_per_cycle": 1,
                "universe_size": 5,
                "min_quote_volume_24h": 1,
                "universe_symbols_by_asset_class": {"COMMODITIES": ["GC=F"]},
            }
        }
    )

    assert result == []
    assert set(seen) == {"BTCUSDT", "AAPL", "GC=F"}
    assert manager._autonomous_source_stats["v2_forward"]["universe_by_asset_class"] == {
        "CRYPTO_FUTURES": 1,
        "US_STOCKS": 1,
        "COMMODITIES": 1,
    }
