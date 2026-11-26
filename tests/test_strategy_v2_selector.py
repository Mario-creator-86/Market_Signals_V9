from core.strategy_v2.selector import StrategySelector


def _base_cfg():
    return {
        "min_rr": 2.0,
        "target_win_rate": 0.60,
        "event_block_hours": 6,
        "min_sample_size": 30,
        "min_validation_score": 60,
        "allow_low_sample_fallback": True,
        "allow_low_validation_fallback": True,
        "enabled_strategies_by_asset_class": {
            "US_STOCKS": ["S2_BREAKOUT_RETEST"]
        },
        "quality_thresholds": {
            "US_STOCKS": {"min_volume": 1000, "max_spread_bps": 40, "min_atr_pct": 0.001}
        },
    }


def test_selector_picks_long_breakout_candidate():
    selector = StrategySelector(config=_base_cfg())
    ctx = {
        "symbol": "AAPL",
        "asset_class": "US_STOCKS",
        "price": 110.0,
        "resistance": 108.0,
        "support": 102.0,
        "retest_hold_long": True,
        "retest_hold_short": False,
        "volume": 1_000_000,
        "volume_avg": 700_000,
        "adx": 28.0,
        "atr": 1.5,
        "atr_pct": 0.013,
        "spread_bps": 8.0,
        "news_sentiment": "bullish",
        "news_pulse_score": 72,
        "event_risk": False,
        "hours_to_event": 999,
        "ema_slope": 0.002,
        "macd_hist": 0.03,
        "bb_width": 0.03,
        "swing_low": 106.0,
        "swing_high": 111.0,
    }
    out = selector.select(symbol="AAPL", asset_class="US_STOCKS", context=ctx)
    best = out["best_candidate"]
    assert best is not None
    assert best["direction"] == "LONG"
    assert best["rr"] >= 2.0


def test_selector_can_reverse_to_short_when_short_scores_higher():
    selector = StrategySelector(config=_base_cfg())
    ctx = {
        "symbol": "AAPL",
        "asset_class": "US_STOCKS",
        "price": 100.0,
        "resistance": 108.0,
        "support": 102.0,
        "retest_hold_long": False,
        "retest_hold_short": True,
        "volume": 1_200_000,
        "volume_avg": 600_000,
        "adx": 31.0,
        "atr": 1.8,
        "atr_pct": 0.018,
        "spread_bps": 8.0,
        "news_sentiment": "bearish",
        "news_pulse_score": 80,
        "event_risk": False,
        "hours_to_event": 999,
        "ema_slope": -0.002,
        "macd_hist": -0.04,
        "bb_width": 0.03,
        "swing_low": 99.0,
        "swing_high": 105.0,
    }
    out = selector.select(symbol="AAPL", asset_class="US_STOCKS", context=ctx)
    best = out["best_candidate"]
    assert best is not None
    assert best["direction"] == "SHORT"


def test_selector_surfaces_unvalidated_s7_as_non_executable_research_only():
    config = {
        "min_rr": 2.0,
        "target_win_rate": 0.60,
        "event_block_hours": 6,
        "min_sample_size": 30,
        "min_validation_score": 60,
        "allow_low_sample_fallback": False,
        "allow_low_validation_fallback": False,
        "require_profitable_performance": True,
        "emit_unvalidated_research_candidates": True,
        "research_only_strategy_ids": ["S7_LIQUIDITY_SWEEP_REVERSAL"],
        "enabled_strategies_by_asset_class": {
            "CRYPTO_FUTURES": ["S7_LIQUIDITY_SWEEP_REVERSAL"],
        },
        "quality_thresholds": {
            "CRYPTO_FUTURES": {"min_volume": 1000, "max_spread_bps": 40, "min_atr_pct": 0.001},
        },
    }
    selector = StrategySelector(config=config)
    selector.regime_engine.detect = lambda _ctx: {"regime": "RANGE"}
    context = {
        "symbol": "BTCUSDT",
        "asset_class": "CRYPTO_FUTURES",
        "price": 100.0,
        "atr": 2.0,
        "atr_pct": 0.02,
        "volume": 2_000_000.0,
        "volume_avg": 1_000_000.0,
        "spread_bps": 2.0,
        "news_sentiment": "neutral",
        "news_pulse_score": 0.0,
        "liquidity_last_event": "SWEEP_RECLAIM",
        "liquidity_last_event_direction": "BULLISH",
        "liquidity_last_event_level": 96.0,
        "liquidity_event_age_bars": 1,
        "structure_event": "CHOCH",
        "structure_event_direction": "BULLISH",
        "structural_support": 95.0,
        "structural_resistance": 105.0,
    }

    result = selector.select("BTCUSDT", "CRYPTO_FUTURES", context)

    assert result["best_candidate"] is None
    assert result["research_candidates_considered"] == 1
    research = result["research_candidates"][0]
    assert research["strategy_id"] == "S7_LIQUIDITY_SWEEP_REVERSAL"
    assert research["meta"]["research_only"] is True
    assert research["meta"]["capital_execution_eligible"] is False
