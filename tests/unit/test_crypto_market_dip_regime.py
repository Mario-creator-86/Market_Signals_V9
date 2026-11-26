"""Crypto entry regime "market_dip": buy liquid coins that fell hard while the
whole market sold off, with a fixed stop/target and a mandatory time stop.

Background (2026-11-22 study, Binance USDT pairs, hourly bars, costs included):
on the most recent 150 days this rule was profitable in both halves, but only
with an exit after about 24 hours. Over two years it is break-even (profit
factor 0.99), so it is an unproven paper experiment that is off unless
configured, and the evidence handed to the AI says so.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.autonomous_candidates import (
    MARKET_DIP_ENTRY_STRATEGY,
    build_scanner_candidate_signal,
    market_btc_change_pct,
    position_time_stop_minutes,
)
from core.exit_manager import ExitManager

CFG = {
    "scanner_entry_mode": "fade",
    "scanner_crypto_entry_mode": "market_dip",
    "scanner_min_abs_change_pct": 5.0,
    "scanner_min_dollar_volume": 10_000_000,
    "scanner_fade_min_move_pct": 8.0,
    "scanner_fade_max_move_pct": 40.0,
    "scanner_fade_stop_pct": 0.04,
    "scanner_fade_rr_ratio": 1.5,
    "scanner_min_score": 78.0,
}


def _coin(change_pct, *, btc=-3.0, volume=50_000_000, symbol="SOLUSDT", price=100.0):
    item = {"symbol": symbol, "price": price, "change_pct": change_pct, "volume": volume, "source": "binance"}
    if btc is not None:
        item["market_btc_change_pct"] = btc
    return item


def test_loser_during_a_market_selloff_becomes_a_long_candidate():
    signal = build_scanner_candidate_signal(_coin(-12.0, btc=-3.0), CFG)

    assert signal is not None
    assert signal["action"] == "BUY" and signal["ticker"] == "SOL-USD"
    assert signal["strategy_type"] == "SCANNER_AI_DIP_LONG"
    assert signal["entry_strategy"] == MARKET_DIP_ENTRY_STRATEGY == "market_dip"
    assert signal["sl"] == pytest.approx(94.0) and signal["tp"] == pytest.approx(107.5)
    assert signal["time_stop_minutes"] == 1440
    assert signal["scanner_context"]["btc_change_pct"] == -3.0
    assert signal["score"] >= CFG["scanner_min_score"]
    evidence = signal["regime_evidence"]
    assert evidence["kind"] == "backtest" and "not live results" in evidence["note"]
    # the evidence shown to the AI is the full picture, not only the favourable window
    assert evidence["verdict"] == "no demonstrated edge"
    assert evidence["two_years"]["profit_factor"] <= 1.05          # break-even over two years
    assert any("BTC" in reason for reason in signal["reasons"])


@pytest.mark.parametrize("change_pct, btc", [
    (-12.0, -1.0),    # the market is not selling off
    (-12.0, 0.5),
    (-12.0, None),    # no market reading: fail closed
    (12.0, -3.0),     # gainers are not shorted in this regime
    (-30.0, -3.0),    # beyond the tested band
    (-5.0, -3.0),     # too small a drop
])
def test_everything_outside_the_tested_rule_is_rejected(change_pct, btc):
    assert build_scanner_candidate_signal(_coin(change_pct, btc=btc), CFG) is None


def test_thin_coins_are_rejected():
    assert build_scanner_candidate_signal(_coin(-12.0, volume=2_000_000), CFG) is None


def test_thresholds_come_from_config():
    cfg = dict(CFG, scanner_dip_min_drop_pct=10.0, scanner_dip_max_drop_pct=30.0,
               scanner_dip_btc_max_change_pct=-3.0, scanner_dip_stop_pct=0.05,
               scanner_dip_rr_ratio=1.5, scanner_dip_time_stop_minutes=720)

    assert build_scanner_candidate_signal(_coin(-9.0, btc=-4.0), cfg) is None
    assert build_scanner_candidate_signal(_coin(-12.0, btc=-2.5), cfg) is None
    signal = build_scanner_candidate_signal(_coin(-28.0, btc=-4.0), cfg)
    assert signal["sl"] == pytest.approx(95.0) and signal["tp"] == pytest.approx(107.5)
    assert signal["time_stop_minutes"] == 720


def test_stocks_keep_the_general_entry_mode():
    stock = {"symbol": "AAPL", "price": 200.0, "change_pct": 10.0, "volume": 5_000_000, "source": "yahoo"}

    signal = build_scanner_candidate_signal(stock, CFG)

    assert signal is not None and signal["action"] == "SHORT"
    assert signal["strategy_type"] == "SCANNER_AI_SHORT" and signal["entry_strategy"] == "fade"
    assert "time_stop_minutes" not in signal


def test_crypto_follows_the_general_mode_when_no_crypto_mode_is_set():
    cfg = {k: v for k, v in CFG.items() if k != "scanner_crypto_entry_mode"}

    signal = build_scanner_candidate_signal(_coin(-12.0, btc=0.5), cfg)

    assert signal is not None and signal["entry_strategy"] == "fade"
    assert signal["strategy_type"] == "SCANNER_AI_LONG"


def test_market_btc_change_is_read_from_the_liquid_universe():
    state = {"liquid_crypto_tickers": [
        {"symbol": "ETHUSDT", "change_pct": -4.0},
        {"symbol": "BTCUSDT", "change_pct": -2.75},
    ]}
    assert market_btc_change_pct(state) == -2.75
    assert market_btc_change_pct({"liquid_crypto_tickers": [{"symbol": "ETHUSDT", "change_pct": -4}]}) is None
    assert market_btc_change_pct({"liquid_crypto_tickers": [{"symbol": "BTCUSDT", "change_pct": None}]}) is None
    assert market_btc_change_pct(None) is None


def test_ai_prompt_keeps_the_regime_fields():
    from core.ollama_decision import _compact_signal

    signal = build_scanner_candidate_signal(_coin(-12.0, btc=-3.0), CFG)
    compact = _compact_signal(signal)

    assert compact["regime_evidence"]["kind"] == "backtest"
    assert compact["entry_strategy"] == "market_dip" and compact["time_stop_minutes"] == 1440


# --- the time stop travels with the position --------------------------------

def _position(hours_open, *, context=None, top_level=None):
    opened = datetime.now(timezone.utc) - timedelta(hours=hours_open)
    position = {
        "position_id": "p1", "ticker": "SOL-USD", "direction": "LONG", "status": "OPEN",
        "strategy_type": "SCANNER_AI_DIP_LONG", "entry_price": 100.0,
        "stop_loss_price": 94.0, "take_profit_price": 106.0, "opened_at": opened.isoformat(),
    }
    if context is not None:
        position["decision_context"] = context
    if top_level is not None:
        position["time_stop_minutes"] = top_level
    return position


def _manager():
    return ExitManager({"trailing_enabled": False, "time_exit_enabled": False,
                        "max_holding_minutes": 0, "scanner_time_stop_minutes": 0})


def test_position_time_stop_minutes_reads_every_place_it_is_stored():
    assert position_time_stop_minutes(_position(1, top_level=1440)) == 1440
    assert position_time_stop_minutes(_position(1, context={"time_stop_minutes": 720})) == 720
    assert position_time_stop_minutes(
        _position(1, context={"execution_context": {"time_stop_minutes": 60}})) == 60
    assert position_time_stop_minutes(_position(1)) == 0
    assert position_time_stop_minutes(_position(1, context={"time_stop_minutes": "junk"})) == 0


def test_position_is_closed_once_its_time_stop_has_passed():
    result = _manager().evaluate_position(_position(25, context={"time_stop_minutes": 1440}), 101.0)

    assert result == {"action": "close", "reason": "regime_time_stop", "new_sl": 0.0}


def test_position_is_held_before_its_time_stop():
    assert _manager().evaluate_position(_position(23, context={"time_stop_minutes": 1440}), 101.0) is None


def test_positions_without_a_time_stop_are_not_closed_by_time():
    assert _manager().evaluate_position(_position(500), 101.0) is None


def test_stop_loss_still_wins_over_the_time_stop():
    result = _manager().evaluate_position(_position(25, context={"time_stop_minutes": 1440}), 93.0)

    assert result["action"] == "close" and result["reason"] != "regime_time_stop"


def test_evidence_rules_treat_the_regime_time_stop_as_a_mechanical_exit():
    from core.portfolio.evidence import exit_condition_kind, exit_due_since

    assert exit_condition_kind("regime_time_stop") == "time_exit"
    position = _position(30, context={"time_stop_minutes": 1440})
    due, _basis = exit_due_since(position)
    opened = datetime.fromisoformat(position["opened_at"])
    assert due == opened + timedelta(minutes=1440)
    assert exit_due_since(_position(30)) is None


@pytest.mark.parametrize("price", [100.0, 0.002522, 0.1974, 761.01])
def test_a_dip_candidate_can_pass_the_risk_checks(price):
    """Seen live on 2026-11-24 (BTC -5%): all 35 dip candidates of the day were turned down with
    "Risk/reward 1.00 < minimum 1.20". The rule aimed for a 6% target on a 6% stop, so it could
    never trade. With the app's own settings the levels must clear every risk/reward minimum,
    computed the way the gates compute it: from the rounded prices.
    """
    from core.config import DEFAULT_CONFIG

    execution = dict(DEFAULT_CONFIG["agents"]["execution"])
    execution["scanner_crypto_entry_mode"] = "market_dip"
    risk_minimum = float(DEFAULT_CONFIG["agents"]["risk"].get("min_risk_reward_ratio", 1.2) or 1.2)
    capital_minimum = max(1.1, float(execution["capital_entry_gate"]["data"]["min_risk_reward_ratio"]))
    cohort_minimum = float(execution["research_cohort"]["min_risk_reward_ratio"])

    signal = build_scanner_candidate_signal(_coin(-12.0, btc=-3.0, price=price), execution)

    assert signal is not None and signal["strategy_type"] == "SCANNER_AI_DIP_LONG"
    reward = signal["tp"] - signal["entry"]
    risk = signal["entry"] - signal["sl"]
    assert risk > 0
    assert reward / risk >= max(risk_minimum, capital_minimum, cohort_minimum)
    assert signal["sl"] == pytest.approx(price * 0.94)      # the tested 6% stop is kept


def test_the_evidence_describes_the_levels_the_rule_trades():
    from core.autonomous_candidates import MARKET_DIP_BACKTEST

    assert "stop 6%, target 7.5%" in MARKET_DIP_BACKTEST["rule"]
    assert MARKET_DIP_BACKTEST["two_years"]["profit_factor"] == pytest.approx(1.01)
