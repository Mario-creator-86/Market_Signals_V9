"""Paper experiment: weekly cross-sectional momentum basket on liquid crypto pairs.

Once a week the liquid Binance USDT pairs are ranked by their 14-day return;
the strongest fifth becomes LONG candidates, the weakest fifth SHORT candidates.
The candidates go through the normal autonomous pipeline; for this basket the
AI's rejection is recorded but not obeyed (paper only).
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from core.xs_momentum_basket import (
    SOURCE_NAME,
    XS_ENTRY_STRATEGY,
    XS_LONG_STRATEGY_TYPE,
    XS_SHORT_STRATEGY_TYPE,
    XsMomentumBasket,
    week_start_ms,
    xs_momentum_ai_is_advisory,
)

DAY = 86_400_000
HOUR = 3_600_000
MONDAY = int(datetime(2026, 11, 23, tzinfo=timezone.utc).timestamp() * 1000)   # a Monday, 00:00 UTC
CFG = {SOURCE_NAME: {"enabled": True}}


class FakeBinance:
    """20 coins C00..C19; coin Cnn gained nn percent over the last 14 completed days."""

    def __init__(self):
        self.now = MONDAY + 2 * HOUR
        self.calls = []
        self.fail = set()
        self.returns = {f"C{i:02d}USDT": float(i) for i in range(20)}
        self.volume = {symbol: 50_000_000.0 for symbol in self.returns}
        self.extra = ["USDCUSDT", "XAUTUSDT", "BTCUPUSDT", "NOPERPUSDT", "THINUSDT", "NEWUSDT", "C05BTC"]
        self.perps = set(self.returns) | {"THINUSDT", "NEWUSDT", "USDCUSDT", "XAUTUSDT"}
        self.price = {symbol: 10.0 for symbol in self.returns}

    def _bars(self, symbol, limit):
        today = self.now // DAY
        if symbol == "NEWUSDT":
            limit = 5                                           # listed five days ago
        volume = 1_000_000.0 if symbol == "THINUSDT" else self.volume.get(symbol, 50_000_000.0)
        gain = self.returns.get(symbol, 3.0) / 100.0
        bars = []
        for day in range(today - limit + 1, today + 1):
            close = 100.0 * (1 + gain) if day >= today - 1 else 100.0
            if day == today:
                close = 500.0                                   # the running day must not be used
            bars.append([day * DAY, "0", "0", "0", str(close), "0", (day + 1) * DAY - 1, str(volume)])
        return bars

    def __call__(self, url):
        self.calls.append(url)
        parsed = urlparse(url)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if parsed.path in self.fail:
            return 0, "URLError: timed out"
        if parsed.path == "/api/v3/ticker/24hr":
            return 200, [{"symbol": s, "quoteVolume": str(self.volume.get(s, 60_000_000.0))}
                         for s in list(self.returns) + self.extra]
        if parsed.path == "/fapi/v1/exchangeInfo":
            return 200, {"symbols": [{"symbol": s, "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"}
                                     for s in sorted(self.perps)]}
        if parsed.path == "/api/v3/klines":
            return 200, self._bars(query["symbol"], int(query["limit"]))
        if parsed.path == "/api/v3/ticker/price":
            return 200, [{"symbol": s, "price": str(p)} for s, p in self.price.items()]
        raise AssertionError(f"unexpected url {url}")

    def count(self, path):
        return sum(1 for url in self.calls if urlparse(url).path == path)


def _basket(tmp_path, api):
    return XsMomentumBasket(tmp_path / "xs_momentum_basket.json", fetch=api, now_ms=lambda: api.now)


def _by_side(signals):
    longs = sorted(s["scanner_context"]["symbol"] for s in signals if s["action"] == "BUY")
    shorts = sorted(s["scanner_context"]["symbol"] for s in signals if s["action"] == "SHORT")
    return longs, shorts


def test_week_starts_on_the_configured_weekday():
    assert week_start_ms(MONDAY) == MONDAY
    assert week_start_ms(MONDAY + 6 * DAY + 23 * HOUR) == MONDAY
    assert week_start_ms(MONDAY - 1) == MONDAY - 7 * DAY
    assert week_start_ms(MONDAY + 3 * DAY, rebalance_weekday=2) == MONDAY + 2 * DAY


def test_strongest_fifth_is_long_and_weakest_fifth_is_short(tmp_path):
    api = FakeBinance()
    signals = _basket(tmp_path, api).candidates(CFG)

    longs, shorts = _by_side(signals)
    assert longs == ["C16USDT", "C17USDT", "C18USDT", "C19USDT"]
    assert shorts == ["C00USDT", "C01USDT", "C02USDT", "C03USDT"]
    top = next(s for s in signals if s["scanner_context"]["symbol"] == "C19USDT")
    assert top["ticker"] == "C19-USD" and top["strategy_type"] == XS_LONG_STRATEGY_TYPE
    assert top["entry_strategy"] == XS_ENTRY_STRATEGY and top["autonomous_source"] == SOURCE_NAME
    assert top["asset_class"] == "CRYPTO_FUTURES" and top["strategy_status"] == "RESEARCH"
    assert top["scanner_context"]["factor"]["return_pct"] == 19.0           # the running day was not used
    assert top["entry"] == 10.0 and top["sl"] == 7.5                        # 25% protective stop
    assert top["regime_evidence"]["kind"] == "backtest"
    bottom = next(s for s in signals if s["scanner_context"]["symbol"] == "C00USDT")
    assert bottom["strategy_type"] == XS_SHORT_STRATEGY_TYPE and bottom["sl"] == 12.5
    # Everything closes an hour before the next ranking: 7 days - 2 hours elapsed - 1 hour.
    assert top["time_stop_minutes"] == 7 * 24 * 60 - 120 - 60


def test_the_two_legs_alternate_in_priority(tmp_path):
    api = FakeBinance()
    signals = sorted(_basket(tmp_path, api).candidates(CFG), key=lambda s: -s["signal_score"])

    assert [s["action"] for s in signals[:4]] == ["BUY", "SHORT", "BUY", "SHORT"]
    assert signals[0]["scanner_context"]["symbol"] == "C19USDT"
    assert signals[1]["scanner_context"]["symbol"] == "C00USDT"
    # Every member must clear the bot's minimum edge score of 90, not only the first one.
    assert min(s["signal_score"] for s in signals) >= 92.5


def test_stablecoins_leveraged_tokens_thin_new_and_unshortable_pairs_are_left_out(tmp_path):
    api = FakeBinance()
    api.returns["C19USDT"] = 19.0
    basket = _basket(tmp_path, api)
    basket.candidates(CFG)

    asked = {parse_qs(urlparse(u).query)["symbol"][0] for u in api.calls if urlparse(u).path == "/api/v3/klines"}
    assert "USDCUSDT" not in asked and "BTCUPUSDT" not in asked and "C05BTC" not in asked
    assert "XAUTUSDT" not in asked                                        # gold token
    assert "NOPERPUSDT" not in asked                                      # no perpetual: cannot be shorted
    assert basket.status(CFG)["universe"] == 20                           # THIN (volume) and NEW (history) dropped


def test_the_caller_can_exclude_instruments_before_the_ranking(tmp_path):
    api = FakeBinance()
    signals = _basket(tmp_path, api).candidates(CFG, exclude=lambda symbol: symbol in {"C19USDT", "C00USDT"})

    longs, shorts = _by_side(signals)
    assert "C19USDT" not in longs and "C00USDT" not in shorts
    assert len(longs) == len(shorts) == 3                                  # 18 pairs -> three a side


def test_no_basket_when_too_few_liquid_pairs(tmp_path):
    api = FakeBinance()
    for symbol in list(api.returns)[10:]:
        api.volume[symbol] = 2_000_000.0
    basket = _basket(tmp_path, api)

    assert basket.candidates(CFG) == []
    status = basket.status(CFG)
    assert status["phase"] == "no_basket" and "only 10 liquid pairs" in status["last_error"]


def test_the_ranking_is_done_once_a_week_and_survives_a_restart(tmp_path):
    api = FakeBinance()
    basket = _basket(tmp_path, api)
    basket.candidates(CFG)
    klines = api.count("/api/v3/klines")
    assert klines == 22                                                    # 20 coins + THIN + NEW

    api.now += 2 * HOUR
    basket.candidates(CFG)
    again = _basket(tmp_path, api)
    longs, _shorts = _by_side(again.candidates(CFG))
    assert api.count("/api/v3/klines") == klines                           # no second ranking
    assert longs == ["C16USDT", "C17USDT", "C18USDT", "C19USDT"]

    api.now = MONDAY + 7 * DAY + HOUR                                       # next Monday
    api.returns["C00USDT"] = 40.0
    longs, _shorts = _by_side(again.candidates(CFG))
    assert api.count("/api/v3/klines") == 2 * klines
    assert "C00USDT" in longs


def test_no_candidates_after_the_entry_window(tmp_path):
    api = FakeBinance()
    basket = _basket(tmp_path, api)
    assert len(basket.candidates(CFG)) == 8
    assert basket.status(CFG)["phase"] == "entry_window"

    api.now = MONDAY + 37 * HOUR
    assert basket.candidates(CFG) == []
    status = basket.status(CFG)
    assert status["phase"] == "holding" and len(status["long"]) == 4 and len(status["short"]) == 4


def test_a_basket_first_ranked_after_the_window_does_not_trade_that_week(tmp_path):
    api = FakeBinance()
    api.now = MONDAY + 3 * DAY
    basket = _basket(tmp_path, api)

    assert basket.candidates(CFG) == []
    assert basket.status(CFG)["phase"] == "holding"


def test_a_failed_ranking_is_reported_and_retried_later(tmp_path):
    api = FakeBinance()
    api.fail.add("/api/v3/ticker/24hr")
    basket = _basket(tmp_path, api)

    assert basket.candidates(CFG) == []
    assert "ticker list unavailable" in basket.status(CFG)["last_error"]
    api.fail.clear()
    api.now += 5 * 60_000
    assert basket.candidates(CFG) == []                                    # not hammered: retry after 10 minutes
    api.now += 6 * 60_000
    assert len(basket.candidates(CFG)) == 8
    assert basket.status(CFG)["last_error"] == ""


def test_without_the_perpetual_list_there_is_no_basket(tmp_path):
    api = FakeBinance()
    api.fail.add("/fapi/v1/exchangeInfo")
    basket = _basket(tmp_path, api)

    assert basket.candidates(CFG) == []
    assert "perpetual list unavailable" in basket.status(CFG)["last_error"]


def test_a_member_is_not_offered_again_after_three_reviews(tmp_path):
    api = FakeBinance()
    basket = _basket(tmp_path, api)
    first = basket.candidates(CFG)
    target = next(s for s in first if s["scanner_context"]["symbol"] == "C19USDT")

    for _ in range(3):
        basket.note_reviewed(target["id"])
    basket.note_reviewed("auto_xsmom_UNKNOWN_long_20261123")              # ignored

    later = _basket(tmp_path, api).candidates(CFG)                         # the count survives a restart
    assert len(later) == 7 and all(s["id"] != target["id"] for s in later)


def test_a_gold_token_already_in_a_saved_basket_is_not_offered(tmp_path):
    api = FakeBinance()
    basket = _basket(tmp_path, api)
    basket.candidates(CFG)
    basket._basket["members"][0]["symbol"] = "XAUTUSDT"                    # ranked before gold tokens were excluded
    api.price["XAUTUSDT"] = 4000.0

    symbols = {s["scanner_context"]["symbol"] for s in basket.candidates(CFG)}
    assert "XAUTUSDT" not in symbols and len(symbols) == 7


def test_a_member_without_a_price_is_skipped(tmp_path):
    api = FakeBinance()
    del api.price["C19USDT"]
    longs, _shorts = _by_side(_basket(tmp_path, api).candidates(CFG))
    assert longs == ["C16USDT", "C17USDT", "C18USDT"]


def test_disabled_makes_no_request(tmp_path):
    api = FakeBinance()
    assert _basket(tmp_path, api).candidates({SOURCE_NAME: {"enabled": False}}) == []
    assert api.calls == []


def test_ai_rejection_is_advisory_only_for_this_rule_and_only_in_paper():
    signal = {"autonomous_source": SOURCE_NAME, "strategy_type": XS_LONG_STRATEGY_TYPE}
    assert xs_momentum_ai_is_advisory(signal, {"mode": "paper"}) is True
    assert xs_momentum_ai_is_advisory(signal, {}) is True                  # paper is the default mode
    assert xs_momentum_ai_is_advisory(signal, {"mode": "live"}) is False
    assert xs_momentum_ai_is_advisory(signal, {"mode": "paper", SOURCE_NAME: {"ai_advisory": False}}) is False
    scanner = {"autonomous_source": "scanner", "strategy_type": "SCANNER_AI_LONG"}
    assert xs_momentum_ai_is_advisory(scanner, {"mode": "paper"}) is False
    delist = {"autonomous_source": "delist_notice", "strategy_type": "DELIST_NOTICE_SHORT"}
    assert xs_momentum_ai_is_advisory(delist, {"mode": "paper"}) is False


def test_agent_manager_collects_basket_candidates_and_reports_the_source(monkeypatch):
    import core.agent_manager as am

    class StubBasket:
        def __init__(self):
            self.reviewed = []

        def candidates(self, cfg, exclude=None):
            assert exclude("SOXLBUSDT") is True and exclude("ETHUSDT") is False
            return [{"id": "auto_xsmom_ETHUSDT_long_20261123", "ticker": "ETH-USD", "action": "BUY",
                     "autonomous_source": SOURCE_NAME}]

        def status(self, cfg=None):
            return {"last_error": "", "phase": "entry_window", "long": ["ETHUSDT"], "short": []}

        def note_reviewed(self, signal_id):
            self.reviewed.append(signal_id)

    stub = StubBasket()
    monkeypatch.setattr(am, "get_xs_momentum_basket", lambda: stub)
    monkeypatch.setattr(
        am, "autonomous_instrument_exclusion",
        lambda candidate, policy=None, tags=None: {"reason": "tokenized_stock"} if candidate["ticker"].startswith("SOXL") else None,
    )
    manager = am.AgentManager.__new__(am.AgentManager)
    manager._autonomous_source_stats = {}
    monkeypatch.setattr(manager, "_instrument_exclusion_inputs", lambda: ({"exclude_tokenized_stocks": True}, {}))

    found = manager._xs_momentum_basket_candidates({SOURCE_NAME: {"enabled": True}})

    assert [c["ticker"] for c in found] == ["ETH-USD"]
    stats = manager._autonomous_source_stats[SOURCE_NAME]
    assert stats["enabled"] is True and stats["candidate_count"] == 1 and stats["phase"] == "entry_window"

    manager._note_autonomous_review(found[0])
    manager._note_autonomous_review({"id": "x", "autonomous_source": "scanner"})
    assert stub.reviewed == ["auto_xsmom_ETHUSDT_long_20261123"]


def test_ai_block_becomes_advisory_for_the_basket_in_paper_mode():
    import core.agent_manager as am

    signal = {"autonomous_source": SOURCE_NAME, "strategy_type": XS_SHORT_STRATEGY_TYPE}
    assert am._ai_block_is_advisory(signal, {"mode": "paper"}) is True
    assert am._ai_block_is_advisory(signal, {"mode": "live"}) is False
    assert am._ai_block_is_advisory({"autonomous_source": "scanner", "strategy_type": "SCANNER_AI_DIP_LONG"}, {"mode": "paper"}) is False


def _verified_candidate(tmp_path):
    api = FakeBinance()
    signal = _basket(tmp_path, api).candidates(CFG)[0]
    signal.update({"_autonomous_candidate": True, "price_verified": True,
                   "price_integrity": {"status": "verified", "is_verified": True}})
    return signal


def test_capital_admits_the_basket_candidate_only_as_a_paper_experiment(tmp_path):
    from core.capital_entry_guard import evaluate_capital_entry

    signal = _verified_candidate(tmp_path)

    paper = evaluate_capital_entry(signal, [], {"mode": "paper"})
    assert paper["allowed"] is True
    assert paper["check"] == "paper_autonomous_experiment" and paper["paper_capital_experiment"] is True
    assert "Factor rule" in paper["reason"]
    # No per-leg budget for a two-legged basket: it would stop one leg and leave a one-sided basket.
    assert "paper_experiment_budget" not in paper["checks"]

    live = evaluate_capital_entry(signal, [], {"mode": "live"})
    assert live["allowed"] is False                                     # never admitted with real money


def test_capital_still_refuses_a_basket_candidate_without_a_verified_price(tmp_path):
    from core.capital_entry_guard import evaluate_capital_entry

    signal = _verified_candidate(tmp_path)
    signal["price_verified"] = False
    signal["price_integrity"] = {"status": "unavailable", "is_verified": False}

    result = evaluate_capital_entry(signal, [], {"mode": "paper"})
    assert result["allowed"] is False and result["check"] == "data"


def test_a_coins_own_streak_does_not_resize_or_block_a_basket_leg():
    from agents.execution_agent import basket_ticker_adjustment

    streak = {"action": "REDUCE", "size_mult": 0.5, "min_score_required": 90.0, "reason": "3x LOSS SHORT"}
    basket = {"autonomous_source": SOURCE_NAME, "strategy_type": XS_SHORT_STRATEGY_TYPE}
    assert basket_ticker_adjustment(basket, streak) == (None, streak)       # kept on record, not applied
    assert basket_ticker_adjustment(basket, None) == (None, None)

    scanner = {"autonomous_source": "scanner", "strategy_type": "SCANNER_AI_SHORT"}
    assert basket_ticker_adjustment(scanner, streak) == (streak, None)      # every other signal: unchanged
