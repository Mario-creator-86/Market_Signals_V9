import pytest

from core.scoring_system import RowContext, detect_market_regime, allowed_modes_for_regime, compute_signal_score
from core.engine import MarketEngine


def _base_row(**overrides):
    base = dict(
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=1000.0,
        rsi=50.0,
        macd=0.0,
        macd_signal=0.0,
        macd_hist=0.0,
        ema20=100.0,
        vwap=100.0,
        atr=1.0,
        adx=20.0,
        vol_sma=1000.0,
        adx_5m=20.0,
        bb_width_5m=0.03,
        atr_percentile_5m=50.0,
        market_regime="MIXED",
        trend_1d="N/A",
        trend_4h="N/A",
        trend_1h="N/A",
        trend_30m="N/A",
        trend_15m="N/A",
        trend_5m="N/A",
        trend_1m="N/A",
        dist_vwap_atr=0.5,
        dist_ema_atr=0.5,
        cooldown_active=False,
        cooldown_bars_remaining=0,
    )
    base.update(overrides)
    return RowContext(**base)


def test_atr_exit_levels_live():
    entry = 100.0
    atr = 2.0
    tp_mult = 2.0
    sl_mult = 1.5

    tp_long, sl_long = MarketEngine._calculate_exit_levels(
        None, entry, "LONG", "ATR", atr, tp_mult, sl_mult, 0.04, 0.02
    )
    assert tp_long == pytest.approx(104.0, abs=1e-6)
    assert sl_long == pytest.approx(97.0, abs=1e-6)

    tp_short, sl_short = MarketEngine._calculate_exit_levels(
        None, entry, "SHORT", "ATR", atr, tp_mult, sl_mult, 0.04, 0.02
    )
    assert tp_short == pytest.approx(96.0, abs=1e-6)
    assert sl_short == pytest.approx(103.0, abs=1e-6)


def test_regime_detection_and_gating():
    row_trend = _base_row(
        adx_5m=25.0,
        bb_width_5m=0.03,
        atr_percentile_5m=50.0,
        trend_1d="BULL",
        trend_4h="BULL",
        trend_1h="BULL",
        trend_30m="BULL",
        trend_5m="BULL",
        trend_1m="BULL",
    )
    regime, _ = detect_market_regime(row_trend, {})
    assert regime == "TREND"
    assert allowed_modes_for_regime(regime) == ["CONTRA"]

    row_range = _base_row(
        adx_5m=10.0,
        bb_width_5m=0.02,
        atr_percentile_5m=50.0,
    )
    regime, _ = detect_market_regime(row_range, {})
    assert regime == "RANGE"
    assert allowed_modes_for_regime(regime) == ["CONTRA"]

    row_low_vol = _base_row(
        atr_percentile_5m=10.0,
    )
    regime, _ = detect_market_regime(row_low_vol, {})
    assert regime == "LOW_VOL"
    assert allowed_modes_for_regime(regime) == []


def test_scoring_mtf_hard_gate():
    row = _base_row(
        trend_1d="N/A",
        trend_4h="N/A",
        trend_1h="N/A",
        trend_30m="N/A",
        trend_5m="N/A",
        trend_1m="N/A",
    )
    result = compute_signal_score(row, "PULLBACK", "MEDIUM", {"mtf_alignment_min": 4})
    assert result.is_valid is False
    assert result.total_score == 0
    assert result.probability_score == 0.0


def test_signal_serialization_new_fields(tmp_path):
    from core.signals import SignalManager

    signal_id = "sig_v8_test"
    path = tmp_path / "signals_state.json"

    sm = SignalManager(path=path)
    sm.open_signals = {
        signal_id: {
            "id": signal_id,
            "ticker": "AAPL",
            "action": "BUY",
            "status": "Pending",
            "entry": 100.0,
            "tp": 104.0,
            "sl": 97.0,
            "exit_mode": "ATR",
            "atr_period": 14,
            "atr_timeframe": "5m",
            "atr_exit_timeframe": "5m",
            "atr_exit": 2.0,
            "tp_atr_mult": 2.0,
            "sl_atr_mult": 1.5,
            "probability_score": 0.62,
            "market_regime": "TREND",
            "variant_mode": "CUSTOM",
            "strict_backed": True,
            "strict_variant": "A",
            "strict_signal_score": 92,
            "size_factor": 0.8,
            "base_risk_pct": 2.0,
            "position_size_pct": 1.6,
            "kelly_fraction": 0.15,
            "expectancy_r_used": 0.3,
            "win_rate_used": 55.0,
            "payoff_ratio_used": 1.4,
        }
    }
    sm.closed_signals = []
    sm.save()

    sm2 = SignalManager(path=path)
    loaded = sm2.open_signals[signal_id]
    assert loaded["exit_mode"] == "ATR"
    assert loaded["atr_period"] == 14
    assert loaded["atr_timeframe"] == "5m"
    assert loaded["atr_exit"] == 2.0
    assert loaded["tp_atr_mult"] == 2.0
    assert loaded["sl_atr_mult"] == 1.5
    assert loaded["probability_score"] == 0.62
    assert loaded["market_regime"] == "TREND"
    assert loaded["variant_mode"] == "CUSTOM"
    assert loaded["strict_backed"] is True
    assert loaded["strict_variant"] == "A"
    assert loaded["strict_signal_score"] == 92
    assert loaded["size_factor"] == 0.8
    assert loaded["position_size_pct"] == 1.6


def test_back_signal_with_strict_updates_open_signal(tmp_path):
    from core.signals import SignalManager

    path = tmp_path / "signals_state.json"
    sm = SignalManager(path=path)
    sm.open_signals = {
        "sig1": {
            "id": "sig1",
            "ticker": "AAPL",
            "action": "BUY",
            "status": "Pending",
            "variant": "C",
            "variant_mode": "CUSTOM",
            "signal_score": 70,
        }
    }
    sm.closed_signals = []
    sm.save()

    strict_signal = {
        "variant": "A",
        "risk_mode": "STRONG",
        "signal_mode": "PULLBACK",
        "signal_score": 95,
        "probability_score": 0.93,
        "mtf_alignment": "6/6",
    }
    updated = sm.back_signal_with_strict("AAPL", strict_signal)
    assert updated is True

    sm2 = SignalManager(path=path)
    loaded = sm2.open_signals["sig1"]
    assert loaded["strict_backed"] is True
    assert loaded["strict_variant"] == "A"
    assert loaded["strict_signal_score"] == 95
