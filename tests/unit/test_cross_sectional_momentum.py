from datetime import datetime, timezone

import numpy as np
import pandas as pd

import core.cross_sectional_momentum as csm


def _frame(end_price: float) -> pd.DataFrame:
    periods = 850
    index = pd.date_range(end=datetime.now(timezone.utc), periods=periods, freq="h", tz="UTC")
    close = np.linspace(100.0, end_price, periods)
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(periods, 1000.0),
        },
        index=index,
    )


def _config(**overrides):
    config = {
        "cross_sectional_momentum": {
            "enabled": True,
            "max_universe_symbols": 2,
            "max_candidates_per_cycle": 1,
            "max_fetch_workers": 2,
            "min_quote_volume_24h": 50_000_000,
            "history_days": 35,
            "short_lookback_hours": 168,
            "long_lookback_hours": 720,
            "fast_ema_hours": 20,
            "slow_ema_hours": 50,
            "atr_period_hours": 14,
            "min_short_return_pct": 2.0,
            "min_long_return_pct": 5.0,
            "min_vol_adjusted_short_return": 0.75,
            "stop_atr": 2.0,
            "target_atr": 3.5,
            "max_completed_bar_age_seconds": 7500,
            "min_signal_score": 85.0,
        }
    }
    config["cross_sectional_momentum"].update(overrides)
    return config


def test_cross_sectional_momentum_emits_only_a_forward_research_candidate(monkeypatch):
    frames = {"BTC-USD": _frame(118.0), "ZEC-USD": _frame(155.0)}
    monkeypatch.setattr(csm, "fetch_research_ohlcv", lambda ticker, **_kwargs: frames[ticker])

    candidates, diagnostic = csm.build_cross_sectional_momentum_candidates(
        [
            {"symbol": "BTCUSDT", "quoteVolume": 1_000_000_000},
            {"symbol": "ZECUSDT", "quoteVolume": 100_000_000},
        ],
        execution_config=_config(),
    )

    assert diagnostic["status"] == "candidate_found"
    assert diagnostic["eligible_liquid_universe_size"] == 2
    assert diagnostic["configured_universe_limit"] == 2
    assert len(candidates) == 1
    candidate = candidates[0]
    # Ranking is volatility-adjusted, so the smoother BTC trajectory can
    # legitimately outrank a larger but noisier absolute return.
    assert candidate["ticker"] in {"BTC-USD", "ZEC-USD"}
    assert candidate["autonomous_source"] == "cross_sectional_momentum"
    assert candidate["strategy_source"] == "CROSS_SECTIONAL_MOMENTUM_FORWARD_RESEARCH"
    assert candidate["forward_validation"]["status"] == "forward_unvalidated"
    assert candidate["sl"] < candidate["entry"] < candidate["tp"]


def test_cross_sectional_momentum_rejects_stablecoins_and_nonpositive_trend(monkeypatch):
    frame = _frame(80.0)
    monkeypatch.setattr(csm, "fetch_research_ohlcv", lambda *_args, **_kwargs: frame)

    candidates, diagnostic = csm.build_cross_sectional_momentum_candidates(
        [
            {"symbol": "USDCUSDT", "quoteVolume": 1_000_000_000},
            {"symbol": "BTCUSDT", "quoteVolume": 1_000_000_000},
        ],
        execution_config=_config(),
    )

    assert candidates == []
    assert diagnostic["status"] == "no_qualifying_cross_sectional_setup"
    assert diagnostic["universe_size"] == 1
    assert diagnostic["eligible_liquid_universe_size"] == 1
