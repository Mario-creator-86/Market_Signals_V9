"""Scanner tab audit 2026-11-15 — regression tests for agents/scanner_agent.py.

Defects confirmed on the running app:
* Binance pairs with a non-ASCII base (``牛来USDT``) reached the hot list, but
  the app's canonical ticker collapses them to the quote-only symbol "USDT".
* A failed Binance leg left status "OK" with no error and kept serving the
  previous liquid universe under a fresh scan age.
* On a closed market a stock row's ``change_pct`` is the after-hours drift
  (HTFL -0.64%) while Yahoo ranked it by its regular-session move (-19.13%);
  nothing in the row said which one it was.
* ``GC=F`` (a gold future from Yahoo "trending") was listed as a stock.
* Tokenised stocks (``NVDABUSDT``) were indistinguishable from crypto.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

import agents.scanner_agent as scanner_module
from agents.scanner_agent import ScannerAgent, tokenized_stock_underlying


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _binance_row(symbol, last, change, quote_volume):
    return {
        "symbol": symbol,
        "lastPrice": str(last),
        "priceChangePercent": str(change),
        "quoteVolume": str(quote_volume),
        "volume": "1000",
    }


BINANCE_ROWS = [
    _binance_row("BTCUSDT", 63956.94, -4.82, 2_500_000_000),
    _binance_row("KAVAUSDT", 0.02858, -58.15, 23_000_000),
    _binance_row("牛来USDT", 0.05049, -12.33, 10_000_000),
    _binance_row("币安人生USDT", 0.2, 9.0, 9_000_000),
    _binance_row("NVDABUSDT", 159.65, -1.4, 24_900_000),
    _binance_row("ARBUSDT", 0.11, -6.0, 8_000_000),
    _binance_row("DEADUSDT", 0.0, 0.0, 7_000_000),
    _binance_row("ETHBTC", 0.03, 1.0, 900_000_000),
]

SCREENER_QUOTE = {
    "symbol": "HTFL",
    "quoteType": "EQUITY",
    "shortName": "Heartflow, Inc.",
    "exchange": "NMS",
    "regularMarketPrice": 48.46,
    "regularMarketPreviousClose": 59.92,
    "regularMarketChangePercent": -19.1255,
    "regularMarketTime": 1794603601,
    "regularMarketVolume": 3704634,
    "postMarketPrice": 48.15,
    "postMarketTime": 1794616178,
}


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner_module, "SCANNER_SHARED_STATE_PATH", tmp_path / "scanner_state.json")
    return ScannerAgent(SimpleNamespace(scan={"scan_interval_seconds": 60}))


def _serve(monkeypatch, *, binance=None, screener=None, trending=None):
    def fake_http_get(url, **_kwargs):
        if "binance.com" in url:
            if isinstance(binance, Exception):
                raise binance
            return binance
        if "screener/predefined" in url:
            return screener
        if "trending" in url:
            return trending
        return None

    monkeypatch.setattr(scanner_module, "http_get", fake_http_get)


def test_crypto_scan_drops_pairs_the_app_cannot_address(agent, monkeypatch):
    _serve(monkeypatch, binance=_Resp(BINANCE_ROWS))

    hot = agent._scan_crypto()

    symbols = {row["symbol"] for row in hot}
    liquid = {row["symbol"] for row in agent.cached_liquid_crypto_list}
    assert {"BTCUSDT", "KAVAUSDT", "NVDABUSDT", "ARBUSDT"} <= symbols
    for unusable in ("牛来USDT", "币安人生USDT", "DEADUSDT", "ETHBTC"):
        assert unusable not in symbols
        assert unusable not in liquid
    assert agent._crypto_leg_error is None
    assert agent._crypto_leg_dt is not None


def test_failed_binance_leg_is_reported_not_shown_as_ok(agent, monkeypatch):
    stock_row = {"symbol": "AAPL", "change_pct": 1.0, "price": 200.0, "source": "yahoo_active"}
    monkeypatch.setattr(agent, "_scan_stocks", lambda: [dict(stock_row)])
    monkeypatch.setattr(agent, "_scan_watchlist_stocks", lambda _stocks=None: [])
    _serve(monkeypatch, binance=_Resp([], status_code=451))

    agent.run_scan()
    state = agent.get_state()

    assert [row["symbol"] for row in state["hot_tickers"]] == ["AAPL"]
    assert state["status"]["state"] == "DEGRADED"
    assert "Binance" in state["status"]["last_error"]
    assert "451" in state["legs"]["crypto"]["error"]
    assert state["legs"]["crypto"]["rows"] == 0
    assert state["legs"]["stocks"]["rows"] == 1


def test_stale_liquid_universe_is_dropped_after_the_grace_period(agent, monkeypatch):
    monkeypatch.setattr(agent, "_scan_stocks", lambda: [])
    monkeypatch.setattr(agent, "_scan_watchlist_stocks", lambda _stocks=None: [])
    _serve(monkeypatch, binance=_Resp(BINANCE_ROWS))
    agent.run_scan()
    assert agent.cached_liquid_crypto_list

    _serve(monkeypatch, binance=RuntimeError("timeout"))
    agent.run_scan()
    # One missed cycle is tolerated ...
    assert agent.cached_liquid_crypto_list
    assert "timeout" in agent.get_state()["status"]["last_error"]

    # ... a universe older than the grace period is not served as current.
    agent._crypto_leg_dt = agent._crypto_leg_dt - timedelta(seconds=600)
    agent.run_scan()
    assert agent.cached_liquid_crypto_list == []
    assert agent.get_state()["liquid_crypto_tickers"] == []


def test_scan_that_refreshed_nothing_does_not_reset_the_data_age(agent, monkeypatch):
    old_rows = [{"symbol": "AAPL", "change_pct": 1.0, "price": 200.0, "source": "yahoo_active"}]

    def stale_stock_scan():
        agent._stock_leg_error = "Yahoo screener returned no rows"
        return list(old_rows)

    monkeypatch.setattr(agent, "_scan_stocks", stale_stock_scan)
    monkeypatch.setattr(agent, "_scan_watchlist_stocks", lambda _stocks=None: [])
    _serve(monkeypatch, binance=RuntimeError("down"))
    previous_success = agent._last_success_dt

    agent.run_scan()
    state = agent.get_state()

    assert agent._last_success_dt == previous_success
    assert state["hot_tickers"] == old_rows
    assert "Stock movers not refreshed" in state["status"]["last_error"]
    assert "Binance movers unavailable" in state["status"]["last_error"]


def test_stock_row_says_which_session_its_change_measures(agent, monkeypatch):
    screener = _Resp({"finance": {"result": [{"quotes": [dict(SCREENER_QUOTE)]}]}})
    trending = _Resp({"finance": {"result": [{"quotes": [
        {"symbol": "GC=F"}, {"symbol": "BTC-USD"}, {"symbol": "^GSPC"}, {"symbol": "ANTH.PVT"},
    ]}]}})
    _serve(monkeypatch, screener=screener, trending=trending)

    rows = agent._scan_stocks()

    assert [row["symbol"] for row in rows] == ["HTFL"]  # no future / crypto / index / private
    row = rows[0]
    # Existing contract for other consumers: extended-hours move vs the close.
    assert row["price"] == 48.15
    assert row["change_pct"] == pytest.approx(-0.64, abs=0.01)
    # New: the row states the session and carries the regular-session move.
    assert row["session"] == "post"
    assert row["regular_change_pct"] == -19.13
    assert row["regular_price"] == 48.46
    assert row["quote_time_utc"].startswith("2026-11-1")
    assert agent._stock_leg_error is None


def test_regular_session_row_is_labelled_regular(agent, monkeypatch):
    quote = {k: v for k, v in SCREENER_QUOTE.items() if not k.startswith("postMarket")}
    _serve(monkeypatch, screener=_Resp({"finance": {"result": [{"quotes": [quote]}]}}), trending=None)

    row = agent._scan_stocks()[0]

    assert row["session"] == "regular"
    assert row["change_pct"] == row["regular_change_pct"] == -19.13


def test_empty_stock_scan_is_flagged_and_keeps_serving_the_old_list(agent, monkeypatch):
    agent.cached_stock_list = [{"symbol": "OLD", "price": 10.0, "change_pct": 1.0, "source": "yahoo_gainers"}]
    monkeypatch.setattr(agent, "_get_watchlist_symbols", lambda: [])
    _serve(monkeypatch, screener=None, trending=None)

    rows = agent._scan_stocks()

    assert [row["symbol"] for row in rows] == ["OLD"]
    assert agent._stock_leg_error


def test_tokenized_stock_needs_name_and_price_evidence():
    prices = {"NVDA": 161.01, "AR": 40.0, "BN": 12.0, "MSTR": 109.73}
    assert tokenized_stock_underlying("NVDABUSDT", 159.65, prices) == "NVDA"
    assert tokenized_stock_underlying("MSTRBUSDT", 98.22, prices) == "MSTR"  # weekend discount
    assert tokenized_stock_underlying("ARBUSDT", 0.11, prices) is None      # Arbitrum, not Antero
    assert tokenized_stock_underlying("BNBUSDT", 587.0, prices) is None
    assert tokenized_stock_underlying("CRCLBUSDT", 57.13, prices) is None   # no reference price
    assert tokenized_stock_underlying("NVDAUSDT", 159.65, prices) is None


def test_scan_tags_tokenized_stocks_and_remembers_them(agent, monkeypatch):
    stocks = [{"symbol": "NVDA", "price": 161.01, "change_pct": -1.1, "source": "yahoo_active"}]
    monkeypatch.setattr(agent, "_scan_stocks", lambda: [dict(row) for row in stocks])
    monkeypatch.setattr(agent, "_scan_watchlist_stocks", lambda _stocks=None: [])
    _serve(monkeypatch, binance=_Resp(BINANCE_ROWS))

    agent.run_scan()
    by_symbol = {row["symbol"]: row for row in agent.get_state()["hot_tickers"]}

    assert by_symbol["NVDABUSDT"]["instrument_type"] == "tokenized_stock"
    assert by_symbol["NVDABUSDT"]["underlying"] == "NVDA"
    assert "instrument_type" not in by_symbol["ARBUSDT"]
    assert "instrument_type" not in by_symbol["BTCUSDT"]
    liquid = {row["symbol"]: row for row in agent.get_state()["liquid_crypto_tickers"]}
    assert liquid["NVDABUSDT"]["underlying"] == "NVDA"

    # NVDA leaves today's movers list: the pair is still a tokenised stock.
    stocks.clear()
    agent.run_scan()
    by_symbol = {row["symbol"]: row for row in agent.get_state()["hot_tickers"]}
    assert by_symbol["NVDABUSDT"]["underlying"] == "NVDA"


def test_state_exposes_leg_freshness_and_market_session(agent):
    state = agent.get_state()

    assert set(state["legs"]) == {"stocks", "crypto"}
    assert set(state["legs"]["crypto"]) == {"as_of_utc", "error", "rows"}
    assert state["us_market"]["session"] in {"PRE_MARKET", "REGULAR", "AFTER_HOURS", "CLOSED", "UNKNOWN"}


def test_degraded_snapshot_stays_degraded_for_the_reading_process(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner_module, "SCANNER_SHARED_STATE_PATH", tmp_path / "scanner_state.json")
    cfg = SimpleNamespace(scan={"scan_interval_seconds": 60})
    writer = ScannerAgent(cfg)
    monkeypatch.setattr(
        writer, "_scan_stocks",
        lambda: [{"symbol": "AAPL", "change_pct": 1.0, "price": 200.0, "source": "yahoo_active"}],
    )
    monkeypatch.setattr(writer, "_scan_watchlist_stocks", lambda _stocks=None: [])
    _serve(monkeypatch, binance=RuntimeError("Binance circuit open"))
    writer.run_scan()

    reader = ScannerAgent(cfg)
    monkeypatch.setattr(reader, "run_scan", lambda: None)
    state = reader.get_state()

    assert state["status"]["state"] == "DEGRADED"
    assert "Binance" in state["status"]["last_error"]


# --- Scanner routes in api/main.py ------------------------------------------

def test_movers_route_failure_is_not_an_empty_market(client, monkeypatch):
    """A crash used to answer {"gainers": [], "losers": []}: "nothing moved"."""
    monkeypatch.delenv("MS_DISABLE_SCANNER_AGENT", raising=False)

    def boom(limit=5):
        raise RuntimeError("kucoin exploded")

    monkeypatch.setattr("api.market_data_service.get_crypto_movers", boom)

    data = client.get("/api/crypto/movers?limit=50").json()

    assert data["gainers"] == [] and data["losers"] == []
    assert data["available"] is False
    assert data["reason"]


def test_movers_route_bounds_the_page_size(client, monkeypatch):
    monkeypatch.delenv("MS_DISABLE_SCANNER_AGENT", raising=False)
    seen = {}

    def fake(limit=5):
        seen["limit"] = limit
        return {"gainers": [], "losers": [], "available": True, "stale": False}

    monkeypatch.setattr("api.market_data_service.get_crypto_movers", fake)

    client.get("/api/crypto/movers?limit=100000")

    assert seen["limit"] == 100


def test_cached_scanner_payload_is_dropped_once_a_newer_scan_exists(agent, monkeypatch):
    """"Refresh now" kept getting the pre-refresh list from the 20 s cache."""
    import api.main as main

    monkeypatch.setattr(scanner_module, "_scanner_agent_instance", agent)
    agent._last_success_dt = scanner_module._utc_now()
    current = scanner_module._iso(agent._last_success_dt)

    assert main._scanner_snapshot_superseded({"as_of_utc": current}) is False
    assert main._scanner_snapshot_superseded({"as_of_utc": "2026-11-15T07:00:00+00:00"}) is True

    monkeypatch.setattr(scanner_module, "_scanner_agent_instance", None)
    assert main._scanner_snapshot_superseded({"as_of_utc": "anything"}) is False


def test_scanner_state_route_reports_a_crash(client, monkeypatch):
    import api.main as main

    def boom():
        raise RuntimeError("state exploded")

    monkeypatch.setattr(main, "_api_scanner_state_impl", boom)

    data = client.get("/api/scanner/state").json()

    assert data["hot_tickers"] == []
    assert data["status"]["state"] == "ERROR"
    assert data["status"]["last_error"]


def test_sparkline_route_does_not_refetch_a_symbol_without_candles(client, monkeypatch):
    """Every poll used to re-request an unresolvable symbol from the exchange."""
    import time as _time

    import api.main as main

    calls = []

    def fake_fetch(symbol):
        calls.append(symbol)
        return [1.0, 2.0, 3.0, 4.0] if symbol == "GOODUSDT" else None

    monkeypatch.setattr(main, "_fetch_sparkline_points", fake_fetch)
    for name in ("GOODUSDT", "NOCANDLESUSDT"):
        main._sparkline_cache.pop(name, None)
        main._sparkline_miss.pop(name, None)

    def poll():
        response = client.get("/api/sparklines?symbols=GOODUSDT,NOCANDLESUSDT").json()
        deadline = _time.time() + 2.0
        while _time.time() < deadline and main._sparkline_inflight:
            _time.sleep(0.01)
        return response

    poll()
    deadline = _time.time() + 2.0
    while _time.time() < deadline and "NOCANDLESUSDT" not in main._sparkline_miss:
        _time.sleep(0.01)
    second = poll()
    third = poll()

    assert second["sparklines"] == {"GOODUSDT": [1.0, 2.0, 3.0, 4.0]}
    assert third["sparklines"] == second["sparklines"]
    assert calls.count("NOCANDLESUSDT") == 1
    assert calls.count("GOODUSDT") == 1
    main._sparkline_cache.pop("GOODUSDT", None)
    main._sparkline_miss.pop("NOCANDLESUSDT", None)


def test_untracked_tokenized_stock_is_verified_against_yahoo_once(agent, monkeypatch):
    """CRCLBUSDT is liquid on Binance but CRCL is on no movers list today."""
    lookups = []

    def fake_http_get(url, **_kwargs):
        if "binance.com" in url:
            return _Resp(BINANCE_ROWS + [
                _binance_row("CRCLBUSDT", 57.13, -3.0, 21_800_000),
                _binance_row("BNBUSDT", 587.0, -4.8, 170_000_000),
            ])
        if "/v8/finance/chart/" in url:
            symbol = url.split("/v8/finance/chart/")[1].split("?")[0]
            lookups.append(symbol)
            meta = {
                "CRCL": {"instrumentType": "EQUITY", "regularMarketPrice": 58.4},
                "BN": {"instrumentType": "EQUITY", "regularMarketPrice": 61.0},
            }.get(symbol)
            if meta is None:
                return _Resp({"chart": {"result": None}}, status_code=404)
            return _Resp({"chart": {"result": [{"meta": meta}]}})
        return None

    monkeypatch.setattr(scanner_module, "http_get", fake_http_get)
    monkeypatch.setattr(agent, "_scan_stocks", lambda: [])
    monkeypatch.setattr(agent, "_scan_watchlist_stocks", lambda _stocks=None: [])

    agent.run_scan()
    agent.run_scan()
    liquid = {row["symbol"]: row for row in agent.get_state()["liquid_crypto_tickers"]}

    assert liquid["CRCLBUSDT"]["underlying"] == "CRCL"
    assert "instrument_type" not in liquid["BNBUSDT"]      # BN trades at $61, BNB at $587
    assert "instrument_type" not in liquid["ARBUSDT"]      # sub-$1 token: not even looked up
    assert "AR" not in lookups
    assert lookups.count("CRCL") == 1 and lookups.count("BN") == 1   # remembered, not re-asked
