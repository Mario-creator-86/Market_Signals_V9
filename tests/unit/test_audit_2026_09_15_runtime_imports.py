"""Regression tests for latent NameErrors found by the 2026-09-15 audit.

Each bug lived inside a broad ``except`` so it failed silently in production.
"""
import inspect
import logging

import pandas as pd


def test_freshness_guard_handles_naive_utc_index():
    # StockDataClient strips timezones (converted to UTC), so the engine sees
    # naive timestamps. A missing pytz import made every such check error out
    # and report stale data as fresh.
    from core.engine import MarketEngine

    engine = object.__new__(MarketEngine)
    stamp = pd.Timestamp.now(tz="UTC").tz_convert(None) - pd.Timedelta(seconds=900)
    df = pd.DataFrame({"close": [1.0, 2.0]}, index=[stamp - pd.Timedelta(minutes=1), stamp])
    fresh, age = engine._validate_data_freshness(df, "1m", "AAPL")
    assert fresh is False
    assert 800 < age < 1000


def test_engine_has_stock_data_client_for_batch_quotes_and_hybrid_macd():
    import core.engine as engine_mod

    assert hasattr(engine_mod.StockDataClient, "get_batch_quote")
    assert hasattr(engine_mod.StockDataClient, "get_hybrid_macd")


def test_broker_module_defines_logger():
    import core.execution.broker as broker

    assert isinstance(broker.logger, logging.Logger)


def test_etoro_discussion_with_unparseable_date_falls_back_to_now():
    import core.etoro_client as etoro

    cls = next(
        c for c in vars(etoro).values()
        if inspect.isclass(c) and hasattr(c, "_parse_discussion_to_article")
    )
    client = object.__new__(cls)
    disc = {"post": {"id": "p1", "owner": {"username": "u"},
                     "message": {"text": "$AAPL looks strong"}, "created": "not-a-date"}}
    article = client._parse_discussion_to_article(disc, default_ticker="AAPL")
    assert article is not None
