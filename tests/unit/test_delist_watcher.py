"""Paper experiment: short a token for four hours after Binance announces its spot delisting.

The watcher reads Binance's public announcement list, keeps what it has seen,
and offers a SHORT candidate for each named token that has a live USDT
perpetual, inside a time window after the notice. The AI reviewer keeps its veto:
the candidate goes through the normal autonomous pipeline.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from core.delist_watcher import (
    DELIST_ENTRY_STRATEGY,
    DELIST_STRATEGY_TYPE,
    DelistWatcher,
    parse_delist_title,
)

MIN = 60_000
T0 = 1_795_420_800_000  # publication time of the notice used below


class FakeBinance:
    def __init__(self):
        self.now = T0
        self.calls = []
        self.fail_cms = False
        self.notices = [
            {"id": 9001, "code": "abc123", "title": "Binance Will Delist VIC, HFT and USDP on 2026-12-01", "releaseDate": T0},
            {"id": 9000, "code": "old456", "title": "Notice of Removal of Spot Trading Pairs - 2026-11-20", "releaseDate": T0 - 5 * 86_400_000},
        ]
        self.perps = {"VICUSDT", "BTCUSDT"}            # HFT has no perpetual
        self.spot = {"VICUSDT": ("0.2000", "3500000"), "HFTUSDT": ("0.0500", "900000")}

    def __call__(self, url):
        self.calls.append(url)
        parsed = urlparse(url)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if "cms/article/list/query" in parsed.path:
            if self.fail_cms:
                return 0, "URLError: timed out"
            return 200, {"data": {"catalogs": [{"total": len(self.notices), "articles": list(self.notices)}]}}
        if parsed.path == "/fapi/v1/exchangeInfo":
            return 200, {"symbols": [{"symbol": s, "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"}
                                     for s in sorted(self.perps)]}
        if parsed.path == "/api/v3/ticker/24hr":
            price, volume = self.spot.get(query["symbol"], (None, None))
            if price is None:
                return 400, {"code": -1121, "msg": "Invalid symbol."}
            return 200, {"symbol": query["symbol"], "lastPrice": price, "quoteVolume": volume, "priceChangePercent": "-21.5"}
        raise AssertionError(f"unexpected url {url}")


def _watcher(tmp_path, api):
    return DelistWatcher(tmp_path / "delist_watcher.json", fetch=api, now_ms=lambda: api.now)


CFG = {"delist_short": {"enabled": True}}


def test_title_parser_keeps_tokens_and_drops_stablecoins_and_other_notices():
    assert parse_delist_title("Binance Will Delist VIC, HFT and USDP on 2026-12-01") == ["VIC", "HFT"]
    assert parse_delist_title("Binance Will Delist ALPACA, PDA, VIB, WING on 2025-05-02") == ["ALPACA", "PDA", "VIB", "WING"]
    assert parse_delist_title("Binance Will Delist CVP, EPX, FOR, LOOM, REEF, VGX on 2024-08-26") == ["CVP", "EPX", "FOR", "LOOM", "REEF", "VGX"]
    assert parse_delist_title("Notice of Removal of Spot Trading Pairs - 2026-11-20") == []
    assert parse_delist_title("Binance Futures Will Delist PTBUSDT and MITOUSDT USDⓈ-M Perpetual Contracts") == []
    assert parse_delist_title("Binance Will Delist BTC on 2030-01-01") == []


def test_a_fresh_notice_becomes_a_short_candidate_inside_the_entry_window(tmp_path):
    api = FakeBinance()
    watcher = _watcher(tmp_path, api)

    api.now = T0 + 3 * MIN
    assert watcher.candidates(CFG) == []          # too early: the first minutes overshoot and bounce

    api.now = T0 + 20 * MIN
    signals = watcher.candidates(CFG)
    assert [s["ticker"] for s in signals] == ["VIC-USD"]   # HFT: no perpetual; USDP: stablecoin
    s = signals[0]
    assert s["action"] == "SHORT" and s["type"] == "SHORT"
    assert s["strategy_type"] == DELIST_STRATEGY_TYPE and s["entry_strategy"] == DELIST_ENTRY_STRATEGY
    assert s["autonomous_source"] == "delist_notice" and s["asset_class"] == "CRYPTO_FUTURES"
    assert s["entry"] == pytest.approx(0.2)
    assert s["sl"] == pytest.approx(0.2 * 1.15)             # 15% stop above the entry
    assert s["tp"] < s["entry"]
    assert s["time_stop_minutes"] == 240
    assert s["regime_evidence"]["kind"] == "backtest"
    assert s["strategy_status"] == "RESEARCH"
    assert "Binance Will Delist VIC" in s["news_headlines"][0]
    assert s["average_daily_volume_usd"] == pytest.approx(3_500_000)
    assert s["scanner_context"]["event"]["perpetual"] == "VICUSDT"
    assert 19 <= s["scanner_context"]["event"]["age_minutes"] <= 21

    api.now = T0 + 95 * MIN
    assert watcher.candidates(CFG) == []          # the window is over


def test_old_notices_seen_at_first_start_never_trade(tmp_path):
    api = FakeBinance()
    api.now = T0 + 3 * 86_400_000
    assert _watcher(tmp_path, api).candidates(CFG) == []


def test_disabled_makes_no_request(tmp_path):
    api = FakeBinance()
    assert _watcher(tmp_path, api).candidates({"delist_short": {"enabled": False}}) == []
    assert api.calls == []


def test_the_announcement_list_is_polled_at_most_once_per_interval(tmp_path):
    api = FakeBinance()
    watcher = _watcher(tmp_path, api)
    api.now = T0 + 20 * MIN
    watcher.candidates(CFG)
    api.now += 20_000
    watcher.candidates(CFG)
    assert sum("cms/article" in c for c in api.calls) == 1

    api.now += 60_000
    watcher.candidates(CFG)
    assert sum("cms/article" in c for c in api.calls) == 2


def test_a_failed_poll_is_reported_and_the_known_notice_still_trades(tmp_path):
    api = FakeBinance()
    watcher = _watcher(tmp_path, api)
    api.now = T0 + 12 * MIN
    assert len(watcher.candidates(CFG)) == 1

    api.fail_cms = True
    api.now += 2 * MIN
    assert len(watcher.candidates(CFG)) == 1      # the notice was already saved
    assert "timed out" in watcher.status()["last_error"]

    api.fail_cms = False
    api.now += 2 * MIN
    watcher.candidates(CFG)
    assert watcher.status()["last_error"] == ""


def test_notices_survive_a_restart(tmp_path):
    api = FakeBinance()
    api.now = T0 + 12 * MIN
    _watcher(tmp_path, api).candidates(CFG)

    api.fail_cms = True                            # the page is unreachable after the restart
    api.now += 5 * MIN
    again = _watcher(tmp_path, api)
    assert [s["ticker"] for s in again.candidates(CFG)] == ["VIC-USD"]
    assert again.status()["notices_known"] >= 1


def test_agent_manager_collects_delist_candidates_and_reports_the_source(monkeypatch):
    import core.agent_manager as am

    class StubWatcher:
        def candidates(self, cfg):
            return [{"ticker": "VIC-USD", "action": "SHORT", "autonomous_source": "delist_notice"}]

        def status(self):
            return {"last_error": "", "notices_known": 1, "last_poll_at": "now"}

    monkeypatch.setattr(am, "get_delist_watcher", lambda: StubWatcher())
    manager = am.AgentManager.__new__(am.AgentManager)
    manager._autonomous_source_stats = {}

    found = manager._delist_notice_candidates({"delist_short": {"enabled": True}})

    assert [c["ticker"] for c in found] == ["VIC-USD"]
    stats = manager._autonomous_source_stats["delist_notice"]
    assert stats["enabled"] is True and stats["candidate_count"] == 1


def test_the_real_fetch_is_switched_off_inside_the_test_suite():
    from core.delist_watcher import _http_fetch

    status, detail = _http_fetch("https://www.binance.com/")
    assert status == 0 and "MS_ENV=test" in detail


def _verified_candidate(tmp_path):
    api = FakeBinance()
    api.now = T0 + 20 * MIN
    signal = _watcher(tmp_path, api).candidates(CFG)[0]
    signal.update({"_autonomous_candidate": True, "price_verified": True,
                   "price_integrity": {"status": "verified", "is_verified": True}})
    return signal


def test_capital_admits_the_delist_candidate_only_as_a_paper_experiment(tmp_path):
    from core.capital_entry_guard import evaluate_capital_entry

    signal = _verified_candidate(tmp_path)

    paper = evaluate_capital_entry(signal, [], {"mode": "paper"})
    assert paper["allowed"] is True
    assert paper["check"] == "paper_autonomous_experiment" and paper["paper_capital_experiment"] is True
    assert paper["checks"]["paper_experiment_budget"]["allowed"] is True   # a fresh budget for this rule

    live = evaluate_capital_entry(signal, [], {"mode": "live"})
    assert live["allowed"] is False                                     # never admitted with real money


def test_capital_still_refuses_a_delist_candidate_without_a_verified_price(tmp_path):
    from core.capital_entry_guard import evaluate_capital_entry

    signal = _verified_candidate(tmp_path)
    signal["price_verified"] = False
    signal["price_integrity"] = {"status": "unavailable", "is_verified": False}

    result = evaluate_capital_entry(signal, [], {"mode": "paper"})
    assert result["allowed"] is False and result["check"] == "data"
