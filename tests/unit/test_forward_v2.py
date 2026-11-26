from datetime import datetime, timezone

import pandas as pd

import core.forward_v2 as forward_v2


def _frame(age_seconds: int = 0) -> pd.DataFrame:
    timestamp = pd.Timestamp(datetime.now(timezone.utc) - pd.Timedelta(seconds=age_seconds))
    frame = pd.DataFrame(
        {
            "open": [100.0] * 240,
            "high": [101.0] * 240,
            "low": [99.0] * 240,
            "close": [100.0] * 239 + [101.0],
            "volume": [10.0] * 240,
        },
        index=pd.date_range(end=timestamp, periods=240, freq="min", tz="UTC"),
    )
    frame.attrs["source"] = "binance_public_ohlcv"
    frame.attrs["source_truncated"] = False
    return frame


def _execution_config(**overrides):
    config = {
        "v2_forward": {
            "enabled": True,
            "asset_classes": ["CRYPTO_FUTURES"],
            "max_ohlcv_age_seconds": 120,
            "min_quote_volume_24h": 10_000_000,
            "min_setup_score": 80.0,
            "min_risk_score": 70.0,
            "min_forward_quality_score": 85.0,
        }
    }
    config["v2_forward"].update(overrides)
    return config


def _selector_result():
    return {
        "best_candidate": {
            "direction": "LONG",
            "strategy_id": "S2_BREAKOUT_RETEST",
            "sl": 98.0,
            "tp1": 107.0,
            "rr": 3.0,
            "setup_score": 92.0,
            "risk_score": 90.0,
            "catalyst_score": 80.0,
        }
    }


def test_live_v2_forward_candidate_is_explicitly_unvalidated(monkeypatch):
    monkeypatch.setattr(forward_v2, "_forward_ohlcv_dataframe", lambda *_args, **_kwargs: _frame())
    monkeypatch.setattr(forward_v2, "build_context", lambda **_kwargs: {"price": 101.0})

    class Selector:
        def __init__(self, config):
            assert config["require_profitable_performance"] is False
            assert config["min_validation_score"] == 0.0

        def select(self, *_args):
            return _selector_result()

    monkeypatch.setattr(forward_v2, "StrategySelector", Selector)
    signal = forward_v2.build_forward_v2_candidate(
        {"symbol": "SOLUSDT", "quoteVolume": 50_000_000, "source": "binance"},
        strategy_v2_config={"min_rr": 2.0},
        execution_config=_execution_config(),
    )

    assert signal is not None
    assert signal["strategy_source"] == "V2_FORWARD_RESEARCH"
    assert signal["autonomous_source"] == "v2_forward"
    assert signal["forward_validation"]["status"] == "forward_unvalidated"
    assert signal["sl"] < signal["entry"] < signal["tp"]
    assert signal["signal_score"] == 91.44


def test_forward_price_quality_does_not_use_optional_news_catalyst():
    score = forward_v2.forward_price_quality_score(
        90.0,
        80.0,
        {"forward_quality_setup_weight": 0.65, "forward_quality_risk_weight": 0.25},
    )

    assert score == 87.22


def test_stale_ohlcv_never_creates_forward_v2_candidate(monkeypatch):
    monkeypatch.setattr(
        forward_v2,
        "_forward_ohlcv_dataframe",
        lambda *_args, **_kwargs: _frame(age_seconds=121),
    )
    diagnostic = {}
    signal = forward_v2.build_forward_v2_candidate(
        {"symbol": "SOLUSDT", "quoteVolume": 50_000_000, "source": "binance"},
        strategy_v2_config={},
        execution_config=_execution_config(),
        diagnostic=diagnostic,
    )

    assert signal is None
    assert diagnostic["status"] == "stale_ohlcv"


def test_crypto_forward_fetch_uses_single_raw_ohlcv_interval(monkeypatch):
    calls = []

    def fake_fetch(ticker, *, days, interval):
        calls.append((ticker, days, interval))
        return _frame()

    monkeypatch.setattr(
        "core.crypto_research_strategies.fetch_research_ohlcv",
        fake_fetch,
    )

    result = forward_v2._forward_ohlcv_dataframe(
        "BTC-USD",
        "CRYPTO_FUTURES",
        {"history_days": 2, "crypto_history_interval": "1m"},
    )

    assert result is not None
    assert calls == [("BTC-USD", 2, "1m")]


def test_stock_forward_candidate_uses_multi_asset_ohlcv(monkeypatch):
    monkeypatch.setattr(forward_v2, "_forward_ohlcv_dataframe", lambda *_args, **_kwargs: _frame())
    monkeypatch.setattr(forward_v2, "build_context", lambda **_kwargs: {"price": 101.0})

    class Selector:
        def __init__(self, config):
            self.config = config

        def select(self, *_args):
            return _selector_result()

    monkeypatch.setattr(forward_v2, "StrategySelector", Selector)
    signal = forward_v2.build_forward_v2_candidate(
        {"symbol": "AAPL", "source": "watchlist_v2_forward"},
        strategy_v2_config={"min_rr": 2.0},
        execution_config=_execution_config(
            asset_classes=["US_STOCKS"],
            max_ohlcv_age_seconds_by_asset={"US_STOCKS": 900},
        ),
    )

    assert signal is not None
    assert signal["ticker"] == "AAPL"
    assert signal["asset_class"] == "US_STOCKS"
    assert signal["strategy_source"] == "V2_FORWARD_RESEARCH"


def test_forward_universe_rotates_across_liquid_symbols_without_using_daily_move_as_primary_rank():
    config = {"max_symbols_per_cycle": 2, "universe_size": 4, "rotation_interval_seconds": 120}
    items = [
        {"symbol": "BTCUSDT", "quoteVolume": 100.0, "change_pct": 1.0},
        {"symbol": "ETHUSDT", "quoteVolume": 90.0, "change_pct": 99.0},
        {"symbol": "SOLUSDT", "quoteVolume": 80.0, "change_pct": 50.0},
        {"symbol": "BNBUSDT", "quoteVolume": 70.0, "change_pct": 40.0},
    ]

    first = forward_v2.select_forward_universe(items, config=config, rotate=True, now_seconds=0)
    second = forward_v2.select_forward_universe(items, config=config, rotate=True, now_seconds=120)

    assert [item["symbol"] for item in first] == ["BTCUSDT", "ETHUSDT"]
    assert [item["symbol"] for item in second] == ["SOLUSDT", "BNBUSDT"]


def test_forward_universe_rejects_stablecoin_base_pairs():
    config = forward_v2.forward_v2_config(
        {
            "v2_forward": {
                "excluded_base_assets": ["USDC", "USD1"],
                "min_quote_volume_24h": 50.0,
            }
        }
    )

    assert forward_v2.is_forward_universe_item({"symbol": "BTCUSDT", "quoteVolume": 100.0}, config) is True
    assert forward_v2.is_forward_universe_item({"symbol": "USDCUSDT", "quoteVolume": 100.0}, config) is False
    assert forward_v2.is_forward_universe_item({"symbol": "USD1USDT", "quoteVolume": 100.0}, config) is False
    assert forward_v2.is_forward_universe_item({"symbol": "EURUSDT", "quoteVolume": 100.0}, config) is False
    assert forward_v2.is_forward_universe_item({"symbol": "SOLUSDT", "quoteVolume": 49.0}, config) is False


def test_forward_universe_accepts_enabled_stocks_and_commodities():
    config = forward_v2.forward_v2_config(
        {
            "v2_forward": {
                "asset_classes": ["US_STOCKS", "COMMODITIES"],
                "min_quote_volume_24h": 1_000_000_000,
            }
        }
    )

    assert forward_v2.is_forward_universe_item({"symbol": "AAPL"}, config) is True
    assert forward_v2.is_forward_universe_item({"symbol": "GC=F"}, config) is True
    assert forward_v2.is_forward_universe_item({"symbol": "BTCUSDT", "quoteVolume": 9_000_000_000}, config) is False
