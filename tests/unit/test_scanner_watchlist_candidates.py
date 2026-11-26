from types import SimpleNamespace

import pandas as pd

import agents.scanner_agent as scanner_module
from agents.scanner_agent import ScannerAgent
from core.agent_manager import AgentManager


class _WatchlistConfig:
    def __init__(self, watchlist):
        self.watchlist = watchlist
        self.config = {}
        self.agents = {
            "scanner": {
                "watchlist_market_scan_enabled": True,
                "watchlist_market_scan_interval_seconds": 300,
                "watchlist_market_scan_max_symbols": 100,
            }
        }
        self.reload_count = 0

    def reload_watchlist_if_changed(self):
        self.reload_count += 1


def _scanner(watchlist):
    agent = object.__new__(ScannerAgent)
    agent.config_manager = _WatchlistConfig(watchlist)
    agent.cached_watchlist_stock_list = []
    agent.last_watchlist_stock_scan_time = 0.0
    return agent


def test_watchlist_symbol_selection_uses_persisted_owner_list_and_respects_controls():
    agent = _scanner(
        {
            "yahoo": ["AAPL", "disabled", "BTC-USD", "BLOCKED", "AAPL"],
            "settings": {"DISABLED": {"signals_enabled": False}},
            "scanner_exclusions": ["BLOCKED"],
        }
    )

    assert agent._get_watchlist_symbols() == ["AAPL"]
    assert agent.config_manager.reload_count == 1


def test_watchlist_crypto_selection_uses_matching_bucket_and_reuses_binance_snapshot():
    agent = _scanner(
        {
            "binance": ["BTC-USD", "disabled-usd", "BTC-USD"],
            "settings": {"DISABLED-USD": {"signals_enabled": False}},
            "scanner_exclusions": [],
        }
    )
    agent.cached_watchlist_crypto_list = []
    rows = [
        {
            "symbol": "BTCUSDT",
            "lastPrice": "100.0",
            "priceChangePercent": "9.0",
            "quoteVolume": "500000000",
            "volume": "1000000",
        },
        {
            "symbol": "ETHUSDT",
            "lastPrice": "3000.0",
            "priceChangePercent": "6.0",
            "quoteVolume": "400000000",
            "volume": "200000",
        },
    ]

    observations = agent._capture_watchlist_crypto_observations(rows)

    assert [item["symbol"] for item in observations] == ["BTCUSDT"]
    assert observations[0]["source"] == "binance_watchlist"
    assert observations[0]["watchlist_member"] is True


def test_watchlist_market_scan_builds_real_price_move_and_volume(monkeypatch):
    agent = _scanner({"yahoo": ["AAPL"], "settings": {}, "scanner_exclusions": []})
    history = pd.DataFrame(
        {
            "Close": [100.0, 110.0],
            "Volume": [1_500_000.0, 2_000_000.0],
        },
        index=pd.to_datetime(["2026-08-26", "2026-08-27"]),
    )
    monkeypatch.setattr(
        scanner_module,
        "yf",
        SimpleNamespace(download=lambda *args, **kwargs: history),
    )

    rows = agent._scan_watchlist_stocks([])

    assert rows == [
        {
            "symbol": "AAPL",
            "change_pct": 10.0,
            "gap_pct": 10.0,
            "price": 110.0,
            "prev_close": 100.0,
            "name": "AAPL",
                "exchange": "",
                "volume": 2_000_000.0,
                "avg_volume": 1_750_000.0,
                "source": "yahoo_watchlist",
            "watchlist_member": True,
            "candidate_origin": "watchlist",
        }
    ]


def test_agent_manager_merges_watchlist_into_stocks_candidate_pool():
    scanner_state = {
        "data_age_seconds": 1,
        "hot_tickers": [
            {
                "symbol": "BTCUSDT",
                "price": 100.0,
                "change_pct": 10.0,
                "quoteVolume": 500_000_000.0,
                "source": "binance",
            }
        ],
        "watchlist_stock_tickers": [
            {
                "symbol": "AAPL",
                "price": 110.0,
                "change_pct": 10.0,
                "volume": 2_000_000.0,
                "source": "yahoo_watchlist",
                "watchlist_member": True,
            }
        ],
        "watchlist_crypto_tickers": [
            {
                "symbol": "SOLUSDT",
                "price": 150.0,
                "change_pct": -9.0,
                "quoteVolume": 300_000_000.0,
                "source": "binance_watchlist",
                "watchlist_member": True,
            }
        ],
    }
    manager = AgentManager.__new__(AgentManager)
    manager.agents = {"scanner": SimpleNamespace(get_state=lambda: scanner_state)}
    manager._autonomous_source_stats = {}
    cfg = {
        "autonomous_sources": {"scanner": True, "watchlist": True},
        "autonomous_scanner_max_data_age_seconds": 120,
        "autonomous_scanner_max_symbols_per_bucket": 25,
        "autonomous_watchlist_max_symbols": 100,
        "scanner_entry_mode": "fade",
        "scanner_min_abs_change_pct": 5.0,
        "scanner_fade_min_move_pct": 6.0,
        "scanner_fade_max_move_pct": 40.0,
        "scanner_min_volume_24h": 1_000_000.0,
        "scanner_min_dollar_volume": 10_000_000.0,
        "scanner_min_score": 78.0,
    }

    candidates = manager._scanner_candidates(cfg)

    assert {candidate["ticker"] for candidate in candidates} == {"AAPL", "BTC-USD", "SOL-USD"}
    aapl = next(candidate for candidate in candidates if candidate["ticker"] == "AAPL")
    assert aapl["autonomous_candidate_origin"] == "watchlist"
    assert aapl["scanner_context"]["watchlist_member"] is True
    assert manager._autonomous_source_stats["watchlist"] == {
        "enabled": True,
        "observation_count": 2,
        "stock_observation_count": 1,
        "crypto_observation_count": 1,
        "candidate_count": 2,
        "allocation_buckets": ["stocks", "crypto"],
    }


def test_scanner_cycle_can_observe_fifty_symbols_per_market_bucket():
    hot_tickers = [
        {
            "symbol": f"STK{index}",
            "price": 10.0,
            "change_pct": 10.0,
            "volume": 2_000_000.0,
            "dollar_volume": 20_000_000.0,
            "source": "yahoo",
        }
        for index in range(60)
    ] + [
        {
            "symbol": f"COIN{index}USDT",
            "price": 1.0,
            "change_pct": 10.0,
            "quoteVolume": 20_000_000.0,
            "source": "binance",
        }
        for index in range(60)
    ]
    manager = AgentManager.__new__(AgentManager)
    manager.agents = {
        "scanner": SimpleNamespace(
            get_state=lambda: {
                "data_age_seconds": 1,
                "hot_tickers": hot_tickers,
                "watchlist_stock_tickers": [],
                "watchlist_crypto_tickers": [],
            }
        )
    }
    manager._autonomous_source_stats = {}
    cfg = {
        "autonomous_sources": {"watchlist": True},
        "autonomous_scanner_max_data_age_seconds": 120,
        "autonomous_scanner_max_symbols_per_bucket": 50,
        "autonomous_watchlist_max_symbols": 100,
        "scanner_entry_mode": "fade",
        "scanner_min_abs_change_pct": 5.0,
        "scanner_fade_min_move_pct": 6.0,
        "scanner_fade_max_move_pct": 40.0,
        "scanner_min_volume_24h": 1_000_000.0,
        "scanner_min_dollar_volume": 10_000_000.0,
        "scanner_min_score": 78.0,
    }

    candidates = manager._scanner_candidates(cfg)

    assert len(candidates) == 100
    assert manager._autonomous_source_stats["scanner"]["observation_count"] == 100
