"""No-trade evaluation backfill must not refetch tickers the provider has no data for.

Regression: 18 crypto tickers Yahoo does not list were refetched on every
/api/ledger/no-trade-evaluation call (~3.5s each), so the endpoint took ~65s.
"""
from api import ledger_service as ls


def _budget():
    return {"remaining": ls._EOD_BACKFILL_MAX_TICKERS_PER_CALL, "used": 0}


def test_empty_backfill_is_not_refetched(monkeypatch):
    calls = []
    monkeypatch.setattr(ls, "_eod_backfill_empty_until", {})
    monkeypatch.setattr(ls, "_bars_from_historical", lambda ticker: [])
    monkeypatch.setattr(ls, "_bars_from_eod_backfill", lambda ticker: calls.append(ticker) or [])

    first, second = _budget(), _budget()
    assert ls._bars_for_ticker_with_backfill("ACE-USD", first) == []
    assert ls._bars_for_ticker_with_backfill("ACE-USD", second) == []

    assert calls == ["ACE-USD"]
    assert first["used"] == 1
    assert second["used"] == 0  # skipped ticker does not burn budget


def test_empty_backfill_retried_after_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(ls, "_eod_backfill_empty_until", {"ACE-USD": 0.0})
    monkeypatch.setattr(ls, "_bars_from_historical", lambda ticker: [])
    monkeypatch.setattr(ls, "_bars_from_eod_backfill", lambda ticker: calls.append(ticker) or [])

    ls._bars_for_ticker_with_backfill("ACE-USD", _budget())

    assert calls == ["ACE-USD"]


def test_successful_backfill_is_not_negatively_cached(monkeypatch):
    bars = [{"symbol": "AAPL", "date": f"2026-10-{d:02d}", "close": 100.0 + d} for d in range(1, 12)]
    cache = {}
    monkeypatch.setattr(ls, "_eod_backfill_empty_until", cache)
    monkeypatch.setattr(ls, "_bars_from_historical", lambda ticker: [])
    monkeypatch.setattr(ls, "_bars_from_eod_backfill", lambda ticker: bars)

    assert ls._bars_for_ticker_with_backfill("AAPL", _budget()) == bars
    assert "AAPL" not in cache
