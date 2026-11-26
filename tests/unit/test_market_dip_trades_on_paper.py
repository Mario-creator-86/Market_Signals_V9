"""The crypto market-dip rule trades on paper; the AI's verdict on it is recorded, not obeyed.

Owner, 2026-11-24, after every dip candidate of a BTC -5% day was turned down
(first by a risk/reward mismatch, then by the AI because the two-year backtest
is break-even): "Trebuie totuși să avem și tranzacții ... dacă este paper
trading, de ce să nu profităm?" The rule is a labelled paper experiment: it
trades like the weekly basket, its own budget stops it after 100 closed trades
without an edge, and the risk and Capital checks stay binding.
"""
from __future__ import annotations

import core.agent_manager as am
from core.autonomous_candidates import build_scanner_candidate_signal
from core.config import DEFAULT_CONFIG

PAPER, LIVE = {"mode": "paper"}, {"mode": "live"}


def _dip_signal():
    execution = dict(DEFAULT_CONFIG["agents"]["execution"], scanner_crypto_entry_mode="market_dip")
    item = {"symbol": "ZROUSDT", "price": 1.027, "change_pct": -16.9, "volume": 40_000_000,
            "source": "binance", "market_btc_change_pct": -5.1}
    signal = build_scanner_candidate_signal(item, execution)
    assert signal is not None and signal["strategy_type"] == "SCANNER_AI_DIP_LONG"
    return signal


def test_an_ai_opinion_on_a_dip_candidate_does_not_stop_it_on_paper():
    signal = _dip_signal()
    for opinion in ("ai_no_trade", "ai_watch", "critic_veto", "ai_low_entry_confidence", "legacy_ai_veto"):
        assert am._advisory_override_applies(True, opinion, signal, PAPER) is True


def test_the_risk_agent_and_the_capital_gate_still_stop_a_dip_candidate():
    signal = _dip_signal()
    for hard in ("ai_validation_policy", "ai_validation_authoritative_data_check"):
        assert am._advisory_override_applies(True, hard, signal, PAPER) is False


def test_with_real_money_the_ai_keeps_its_veto():
    assert am._advisory_override_applies(True, "critic_veto", _dip_signal(), LIVE) is False


def test_other_scanner_candidates_keep_the_ai_veto():
    stock_fade = {"autonomous_source": "scanner", "strategy_type": "SCANNER_AI_LONG", "ticker": "SMTC",
                  "entry_strategy": "fade"}
    labelled_only = {"autonomous_source": "watchlist", "strategy_type": "SCANNER_AI_DIP_LONG", "ticker": "AAPL",
                     "entry_strategy": "fade"}
    assert am._advisory_override_applies(True, "ai_watch", stock_fade, PAPER) is False
    assert am._advisory_override_applies(True, "ai_watch", labelled_only, PAPER) is False   # not the dip rule


def test_the_setting_can_give_the_ai_its_veto_back():
    signal = _dip_signal()
    assert am._advisory_override_applies(True, "critic_veto", signal, {"mode": "paper", "scanner_dip_ai_advisory": False}) is False
