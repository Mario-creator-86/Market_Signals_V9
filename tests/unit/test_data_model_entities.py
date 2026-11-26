from datetime import datetime, timezone
from core.data_model.entities import (
    AssetClass, Currency, TradingSession, Exchange, Symbol, EXCHANGES, get_exchange,
    OHLCVBar, CorporateAction, Earnings, AnalystEstimate, Fundamentals,
    OptionContract, OptionsChain, NewsArticle, SECFiling, Signal,
)
from core.data_model.pit import PointInTime, UNAVAILABLE, as_of

_NYSE = EXCHANGES["XNYS"]


def _sym(t="AAPL"):
    return Symbol(t, EXCHANGES["XNAS"], AssetClass.US_STOCKS, Currency.USD)


def test_asset_class_values():
    assert AssetClass.US_STOCKS.value == "US_STOCKS"
    assert AssetClass.CRYPTO_FUTURES.value == "CRYPTO_FUTURES"
    assert AssetClass.FOREX_MAJORS.value == "FOREX_MAJORS"
    assert AssetClass.COMMODITIES.value == "COMMODITIES"


def test_trading_sessions_present():
    for s in ("PRE_MARKET", "REGULAR", "AFTER_HOURS", "BMO", "AMC", "CLOSED", "CRYPTO_CONTINUOUS"):
        assert TradingSession[s] is not None


def test_exchanges_registry_populated():
    assert EXCHANGES["XNYS"].tz == "America/New_York"
    assert EXCHANGES["XNYS"].currency is Currency.USD
    assert EXCHANGES["XNAS"].asset_class is AssetClass.US_STOCKS
    assert EXCHANGES["CRYPTO"].tz == "UTC"


def test_get_exchange_case_insensitive():
    assert get_exchange("xnys") is EXCHANGES["XNYS"]
    assert get_exchange("") is None
    assert get_exchange("XLON") is None  # stubbed for later phase


def test_symbol_frozen():
    sym = Symbol("AAPL", EXCHANGES["XNAS"], AssetClass.US_STOCKS, Currency.USD)
    assert sym.ticker == "AAPL"
    assert sym.exchange.mic == "XNAS"
    assert sym.isin is None
    import dataclasses
    try:
        sym.ticker = "MSFT"  # frozen -> must raise
        assert False, "Symbol must be frozen"
    except dataclasses.FrozenInstanceError:
        pass


def test_ohlcvbar_auto_pit_and_utc_enforced():
    ts = datetime(2026, 8, 11, 20, 0, 0, tzinfo=timezone.utc)
    bar = OHLCVBar(_sym(), ts, 100.0, 101.0, 99.0, 100.5, 1000.0)
    assert bar.pit is not None
    assert bar.pit.event_time == ts
    try:
        OHLCVBar(_sym(), datetime(2026, 8, 11), 1, 1, 1, 1, 1)  # naive ts
        assert False, "naive timestamp must raise"
    except ValueError:
        pass


def test_earnings_session_field():
    e = Earnings(_sym(), "Q2 2026", datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc), TradingSession.BMO)
    assert e.session is TradingSession.BMO
    assert e.eps_actual is None


def test_news_article_pit_as_of():
    a = NewsArticle(id="1", title="t", url="u", source="s",
                    published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    pit=PointInTime(available_from=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
                                    fetched_at=datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc)))
    b = NewsArticle(id="2", title="t2", url="u2", source="s",
                    published_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
                    pit=PointInTime(available_from=datetime(2026, 3, 1, tzinfo=timezone.utc),
                                    fetched_at=datetime(2026, 3, 1, tzinfo=timezone.utc)))
    got = as_of([a, b], datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert got is a
    got2 = as_of([a, b], datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert got2 is UNAVAILABLE


def test_signal_is_mutable_view():
    s = Signal(id="s1", ticker="AAPL", action="BUY", entry=100.0, entry_price=100.0,
              created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    s.meta["variant"] = "v2"  # mutable
    assert s.meta["variant"] == "v2"


def test_all_15_entity_types_importable():
    # The 15 §27 entities: value types (Symbol/Exchange/Currency/Timezone-via-Exchange.tz/
    # TradingSession) + data entities. Confirm all are present.
    for t in (OHLCVBar, CorporateAction, Earnings, AnalystEstimate, Fundamentals,
              OptionContract, OptionsChain, NewsArticle, SECFiling, Signal):
        assert t is not None