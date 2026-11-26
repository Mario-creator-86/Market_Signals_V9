"""The AI reviewer sees how broad today's move is, with the measured base rates, as context only."""
from __future__ import annotations

from core.autonomous_candidates import MOVER_REPLAY_EVIDENCE, market_context
from core.ollama_decision import _compact_signal, build_trade_review_prompt


def _state(stock_changes, coin_changes, btc=4.4):
    return {
        "as_of_utc": "2026-11-23T14:35:00+00:00",
        "hot_tickers": [{"symbol": f"S{i}", "change_pct": c} for i, c in enumerate(stock_changes)]
                       + [{"symbol": "DOGEUSDT", "change_pct": 30.0}],          # a coin in the hot list is not a stock
        "watchlist_stock_tickers": [{"symbol": "WATCH", "change_pct": 1.0}],
        "liquid_crypto_tickers": [{"symbol": "BTCUSDT", "change_pct": btc}]
                                 + [{"symbol": f"C{i}USDT", "change_pct": c} for i, c in enumerate(coin_changes)],
    }


def test_a_green_day_reads_as_broad_up_for_stocks_and_crypto():
    context = market_context(_state([6.0] * 9 + [-7.0] * 2, [3.0] * 12 + [-1.0] * 2))

    assert context["stocks"]["count"] == 12 and context["stocks"]["reading"] == "broad up"
    assert context["stocks"]["up_5pct_or_more"] == 9 and context["stocks"]["down_5pct_or_more"] == 2
    assert "not the whole market" in context["stocks"]["sample"]
    assert context["crypto"]["reading"] == "broad up" and context["crypto"]["btc_change_24h_pct"] == 4.4
    assert context["replay_evidence"] == MOVER_REPLAY_EVIDENCE and context["replay_evidence"]["kind"] == "backtest"
    assert context["as_of"] == "2026-11-23T14:35:00+00:00"


def test_red_and_mixed_days_and_too_little_data():
    assert market_context(_state([-6.0] * 10 + [2.0], [-3.0] * 14))["stocks"]["reading"] == "broad down"
    assert market_context(_state([6.0] * 6 + [-6.0] * 6, [1.0] * 7 + [-1.0] * 7))["crypto"]["reading"] == "mixed"
    thin = market_context(_state([5.0] * 3, [1.0] * 3))
    assert thin is None                                                    # a handful of names is not a reading
    assert market_context(None) is None and market_context({"hot_tickers": "broken"}) is None
    bad_numbers = market_context(_state([float("nan"), "x", None] + [1.0] * 10, []))
    assert bad_numbers["stocks"]["count"] == 11 and "crypto" not in bad_numbers


def test_the_reviewer_receives_it_as_context_with_the_measured_numbers():
    signal = {"ticker": "NVDA", "action": "SHORT", "market_context": market_context(_state([6.0] * 12, [3.0] * 14))}

    assert _compact_signal(signal)["market_context"]["stocks"]["reading"] == "broad up"
    prompt = build_trade_review_prompt(signal, {"accepted": True})
    assert "When market_context is present" in prompt and "never a signal by itself" in prompt
    assert '"buy_a_stock_that_is_up"' in prompt                             # the numbers travel with the context
