"""Regression tests for the crypto execution-price fallback chain.

Root cause (2026-09-06): BULLA-USD trades on Binance *futures* only and on
KuCoin-style venues, but `_get_execution_price` only consulted the Binance
*spot* orderbook, so manual paper opens failed with HTTP 409
"verified live entry price unavailable for <ticker>" while the chart (fed by
the futures/perp stream) kept showing a live price.

Chain under test: Binance spot -> Binance futures (fapi) -> KuCoin level1.
"""
import pytest

import core.capital_manager as capital_mod
import core.http_utils as http_utils
import core.etoro_client as etoro_mod
from core.capital_manager import CapitalManager

pytestmark = pytest.mark.integration


@pytest.fixture
def capital_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("MS_DISABLE_CAPITAL_THREADS", "1")
    monkeypatch.setattr(capital_mod, "CAPITAL_STATE_PATH", tmp_path / "capital_fallback.json")
    monkeypatch.setattr(capital_mod, "get_sqlite_store", lambda: None)
    CapitalManager._instance = None
    CapitalManager._price_cache = {}
    CapitalManager._atr_cache = {}
    yield CapitalManager()
    CapitalManager._instance = None
    CapitalManager._price_cache = {}
    CapitalManager._atr_cache = {}


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _binance_depth(bid: str, ask: str) -> _FakeResponse:
    return _FakeResponse(200, {"bids": [[bid, "10"]], "asks": [[ask, "10"]]})


def _kucoin_level1(bid=None, ask=None, price=None, code="200000") -> _FakeResponse:
    data = {}
    if bid is not None:
        data["bestBid"] = bid
    if ask is not None:
        data["bestAsk"] = ask
    if price is not None:
        data["price"] = price
    return _FakeResponse(200, {"code": code, "data": data})


def _patch_quotes(monkeypatch, responses_by_host):
    """Map https host -> response (or exception) for the fake http_get."""

    def fake_http_get(url, provider=None, params=None, **kwargs):
        host = url.split("/")[2]
        outcome = responses_by_host[host]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(http_utils, "http_get", fake_http_get)

    class _UnavailableEtoro:
        def is_available(self):
            return False

    monkeypatch.setattr(etoro_mod, "get_etoro_client", lambda: _UnavailableEtoro())


def test_crypto_falls_back_to_binance_futures_when_spot_lacks_symbol(capital_manager, monkeypatch):
    """BULLAUSDT is perp-only: spot 400 -> fapi depth must fill as verified."""
    _patch_quotes(
        monkeypatch,
        {
            "api.binance.com": _FakeResponse(400, {"code": -1121, "msg": "Invalid symbol."}),
            "fapi.binance.com": _binance_depth("0.06924", "0.06929"),
        },
    )

    quote = capital_manager._get_execution_price("BULLA-USD", "SELL", use_cache=False)

    assert quote["source"] == "Binance_futures_orderbook"
    # SELL crosses the book at the best bid
    assert quote["execution_price"] == pytest.approx(0.06924)
    assert quote["execution_price_quality"]["level"] == "VERIFIED"
    assert CapitalManager._is_verified_execution_payload(quote)


def test_crypto_falls_back_to_kucoin_when_absent_from_binance(capital_manager, monkeypatch):
    """Tokens listed only on KuCoin fill from the public level1 top of book."""
    _patch_quotes(
        monkeypatch,
        {
            "api.binance.com": _FakeResponse(400, {"code": -1121, "msg": "Invalid symbol."}),
            "fapi.binance.com": _FakeResponse(400, {"code": -1121, "msg": "Invalid symbol."}),
            "api.kucoin.com": _kucoin_level1(bid="1.2340", ask="1.2350"),
        },
    )

    quote = capital_manager._get_execution_price("SOMETOKEN-USD", "BUY", use_cache=False)

    assert quote["source"] == "KuCoin_orderbook"
    # BUY crosses the book at the best ask
    assert quote["execution_price"] == pytest.approx(1.2350)
    assert quote["execution_price_quality"]["level"] == "VERIFIED"
    assert CapitalManager._is_verified_execution_payload(quote)


def test_kucoin_level1_price_only_is_still_verified(capital_manager, monkeypatch):
    _patch_quotes(
        monkeypatch,
        {
            "api.binance.com": _FakeResponse(400, {"code": -1121, "msg": "Invalid symbol."}),
            "fapi.binance.com": _FakeResponse(400, {"code": -1121, "msg": "Invalid symbol."}),
            "api.kucoin.com": _kucoin_level1(price="0.500"),
        },
    )

    quote = capital_manager._get_execution_price("SOMETOKEN-USD", "BUY", use_cache=False)

    assert quote["source"] == "KuCoin_orderbook"
    assert quote["execution_price"] == pytest.approx(0.500)
    assert CapitalManager._is_verified_execution_payload(quote)


def test_crypto_unavailable_when_no_venue_lists_symbol(capital_manager, monkeypatch):
    """No venue -> Unavailable estimate must stay rejected for order entry."""
    _patch_quotes(
        monkeypatch,
        {
            "api.binance.com": _FakeResponse(400, {"code": -1121, "msg": "Invalid symbol."}),
            "fapi.binance.com": _FakeResponse(400, {"code": -1121, "msg": "Invalid symbol."}),
            "api.kucoin.com": _FakeResponse(200, {"code": "400600", "data": None}),
        },
    )

    quote = capital_manager._get_execution_price("GHOSTCOIN-USD", "BUY", use_cache=False)

    assert quote["execution_price"] == 0.0
    assert quote["source"] == "Unavailable"
    assert quote["execution_price_quality"]["level"] == "UNAVAILABLE"
    assert not CapitalManager._is_verified_execution_payload(quote)


# ---------------------------------------------------------------------------
# Equity chain: eToro live book -> Yahoo.
#
# Root cause (2026-11-12): the eToro step ran ahead of Yahoo but its sources
# were never graded VERIFIED, so every stock price was "estimated", the P&L
# tracker skipped the position, and stop/target/time exits never fired. Eleven
# stock positions opened 2026-09-02..08 were still OPEN two months later.
# ---------------------------------------------------------------------------
class _FakeEtoro:
    def __init__(self, quote):
        self._quote = quote

    def is_available(self):
        return True

    def get_quote(self, symbol):
        return self._quote


class _FakeYahooTicker:
    def __init__(self, last_price):
        self.fast_info = type("FastInfo", (), {"last_price": last_price})()
        self.info = {}


def _patch_equity_sources(monkeypatch, capital_manager, *, etoro_quote, yahoo_last):
    import yfinance

    monkeypatch.setattr(etoro_mod, "get_etoro_client", lambda: _FakeEtoro(etoro_quote))
    monkeypatch.setattr(yfinance, "Ticker", lambda symbol: _FakeYahooTicker(yahoo_last))
    monkeypatch.setattr(capital_manager, "_get_atr_for_symbol", lambda symbol, last_price: 0.0)


def test_equity_tight_etoro_book_is_a_verified_fill_price(capital_manager, monkeypatch):
    _patch_equity_sources(
        monkeypatch, capital_manager,
        etoro_quote={"price": 100.0, "bid": 99.95, "ask": 100.05}, yahoo_last=0.0,
    )

    result = capital_manager._get_execution_price("PANW", "SELL", use_cache=False)

    assert result["source"] == "eToro_bid_ask"
    assert result["execution_price"] == pytest.approx(99.95)
    assert CapitalManager._is_verified_execution_payload(result)


def test_equity_wide_etoro_book_falls_through_to_yahoo(capital_manager, monkeypatch):
    # Off-hours eToro books are indicative (here 5% wide): not fillable.
    _patch_equity_sources(
        monkeypatch, capital_manager,
        etoro_quote={"price": 100.0, "bid": 97.5, "ask": 102.5}, yahoo_last=101.0,
    )

    result = capital_manager._get_execution_price("PANW", "SELL", use_cache=False)

    assert result["source"] == "Yahoo_quote"
    assert CapitalManager._is_verified_execution_payload(result)


def test_equity_etoro_last_only_does_not_preempt_yahoo(capital_manager, monkeypatch):
    _patch_equity_sources(
        monkeypatch, capital_manager,
        etoro_quote={"price": 100.0, "bid": None, "ask": None}, yahoo_last=101.0,
    )

    result = capital_manager._get_execution_price("PANW", "BUY", use_cache=False)

    assert result["source"] == "Yahoo_quote"
    assert CapitalManager._is_verified_execution_payload(result)


def test_equity_indicative_etoro_price_is_never_fillable(capital_manager, monkeypatch):
    # Yahoo down: the indicative eToro last is shown but stays ESTIMATED.
    _patch_equity_sources(
        monkeypatch, capital_manager,
        etoro_quote={"price": 100.0, "bid": None, "ask": None}, yahoo_last=0.0,
    )

    result = capital_manager._get_execution_price("PANW", "BUY", use_cache=False)

    assert result["source"] == "eToro_quote"
    assert result["execution_price"] == pytest.approx(100.0)
    assert not CapitalManager._is_verified_execution_payload(result)


# ---------------------------------------------------------------------------
# Yahoo bid/ask far from the last trade.
#
# Seen live on 2026-11-24: two learning-mode shorts were stopped out at prices
# that never traded - SITM at 539.09 while it traded 383.5-386.6 (loss -41 $),
# MTSI at 224.29 while it traded 202-205 (-12 $). Yahoo's bid/ask was checked
# for a tight spread (1 $ on 385 passes) but never against the last trade.
# ---------------------------------------------------------------------------
class _FakeYahooBook:
    def __init__(self, last_price, bid, ask):
        self.fast_info = type("FastInfo", (), {"last_price": last_price})()
        self.info = {"bid": bid, "ask": ask}


def test_a_yahoo_book_far_from_the_last_trade_is_not_a_fill_price(capital_manager, monkeypatch):
    import yfinance

    monkeypatch.setattr(etoro_mod, "get_etoro_client", lambda: _FakeEtoro({"price": 0, "bid": None, "ask": None}))
    monkeypatch.setattr(capital_manager, "_get_atr_for_symbol", lambda symbol, last_price: 0.0)
    monkeypatch.setattr(yfinance, "Ticker", lambda symbol: _FakeYahooBook(385.0, 538.5, 539.09))

    result = capital_manager._get_execution_price("SITM", "BUY", use_cache=False)

    assert result["source"] == "Yahoo_quote"
    assert result["execution_price"] == pytest.approx(385.0, rel=0.01)    # never 539
    assert CapitalManager._is_verified_execution_payload(result)


def test_a_yahoo_book_next_to_the_last_trade_is_still_used(capital_manager, monkeypatch):
    import yfinance

    monkeypatch.setattr(etoro_mod, "get_etoro_client", lambda: _FakeEtoro({"price": 0, "bid": None, "ask": None}))
    monkeypatch.setattr(capital_manager, "_get_atr_for_symbol", lambda symbol, last_price: 0.0)
    monkeypatch.setattr(yfinance, "Ticker", lambda symbol: _FakeYahooBook(385.0, 384.9, 385.2))

    result = capital_manager._get_execution_price("SITM", "BUY", use_cache=False)

    assert result["source"] == "Yahoo_bid_ask" and result["execution_price"] == pytest.approx(385.2)
