from datetime import datetime, timezone, timedelta

import pytest

from core.config import normalize_ticker

pytestmark = pytest.mark.unit


def _is_iso_utc(value: str) -> bool:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.tzinfo is not None and dt.utcoffset() == timedelta(0)


def test_ticker_normalization_uppercase_and_clean():
    assert normalize_ticker(" aapl ", provider="yahoo") == "AAPL"
    assert normalize_ticker("btcusd", provider="yahoo") == "BTC-USD"
    assert normalize_ticker("eur/usd", provider="yahoo") == "EURUSD=X"


def test_timestamp_iso_utc_invariant():
    ts = datetime.now(timezone.utc).isoformat()
    assert _is_iso_utc(ts)


def test_sorting_desc_score_with_stable_tiebreaker():
    rows = [
        {"ticker": "MSFT", "score": 90},
        {"ticker": "AAPL", "score": 90},
        {"ticker": "NVDA", "score": 99},
    ]
    ordered = sorted(rows, key=lambda r: (-float(r["score"]), str(r["ticker"])))
    assert [x["ticker"] for x in ordered] == ["NVDA", "AAPL", "MSFT"]


def test_duplicates_removed_in_final_list():
    tickers = ["AAPL", "MSFT", "AAPL", "BTC-USD", "BTC-USD"]
    deduped = list(dict.fromkeys(tickers))
    assert deduped == ["AAPL", "MSFT", "BTC-USD"]


def test_earnings_session_missing_defaults_unknown():
    def normalize_session(value):
        raw = str(value or "").strip().upper()
        return raw if raw in {"BMO", "AMC"} else "UNKNOWN"

    assert normalize_session(None) == "UNKNOWN"
    assert normalize_session("") == "UNKNOWN"
    assert normalize_session("bmo") == "BMO"
