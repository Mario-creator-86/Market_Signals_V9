from datetime import datetime
import threading
from unittest.mock import MagicMock

from agents.economic_calendar_agent import EconomicCalendarAgent, EarningsEvent


def _new_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = EconomicCalendarAgent()
    agent._events_cache = {
        "economic": {},
        "earnings": {},
        "ipo": {},
        "earnings_meta": {},
    }
    return agent


def test_get_cached_earnings_range_prefers_richer_duplicate(tmp_path, monkeypatch):
    agent = _new_agent(tmp_path, monkeypatch)
    agent._events_cache["earnings"]["ODD"] = [
        {
            "id": "earn_ODD_20260225",
            "symbol": "ODD",
            "date": "2026-02-25T09:00:00",
            "eps_estimated": 0.14,
            "eps_actual": None,
            "revenue_estimated": 151082730,
            "revenue_actual": None,
            "when": "bmo",
            "is_watchlist": False,
        },
        {
            "id": "earn_ODD_20260225",
            "symbol": "ODD",
            "date": "2026-02-25T00:00:00",
            "eps_estimated": 0.14,
            "eps_actual": 0.2,
            "revenue_estimated": 303795600,
            "revenue_actual": 152726000,
            "when": "bmo",
            "is_watchlist": True,
        },
    ]

    out = agent._get_cached_earnings_range(
        datetime(2026, 2, 24),
        datetime(2026, 2, 26),
        watchlist_tickers=["ODD"],
    )

    assert len(out) == 1
    assert out[0]["symbol"] == "ODD"
    assert out[0]["eps_actual"] == 0.2
    assert out[0]["revenue_actual"] == 152726000


def test_update_earnings_cache_merges_same_id_with_quality(tmp_path, monkeypatch):
    agent = _new_agent(tmp_path, monkeypatch)
    agent._events_cache["earnings"]["ODD"] = [
        {
            "id": "earn_ODD_20260225",
            "symbol": "ODD",
            "date": "2026-02-25T09:00:00",
            "eps_estimated": 0.14,
            "eps_actual": None,
            "revenue_estimated": 151082730,
            "revenue_actual": None,
            "when": "bmo",
            "is_watchlist": False,
        }
    ]

    agent._update_earnings_cache_from_events(
        [
            EarningsEvent(
                symbol="ODD",
                date=datetime(2026, 2, 25, 0, 0, 0),
                eps_estimated=0.14,
                eps_actual=0.2,
                revenue_estimated=303795600,
                revenue_actual=152726000,
                when="bmo",
                is_watchlist=True,
            )
        ],
        watchlist_tickers=["ODD"],
    )

    rows = agent._events_cache["earnings"]["ODD"]
    assert len(rows) == 1
    assert rows[0]["eps_actual"] == 0.2
    assert rows[0]["revenue_actual"] == 152726000


def test_nonblocking_ticker_read_does_not_wait_for_calendar_writer(tmp_path, monkeypatch):
    agent = _new_agent(tmp_path, monkeypatch)
    agent._events_cache["earnings"]["AAPL"] = [
        {"symbol": "AAPL", "date": "2026-08-28", "when": "amc"}
    ]
    acquired = threading.Event()
    release = threading.Event()

    def hold_cache_lock():
        with agent._cache_lock:
            acquired.set()
            release.wait(timeout=2)

    worker = threading.Thread(target=hold_cache_lock)
    worker.start()
    assert acquired.wait(timeout=1)
    try:
        assert agent.try_get_earnings_for_ticker("AAPL") is None
    finally:
        release.set()
        worker.join(timeout=2)

    assert agent.get_earnings_for_ticker("AAPL")["date"] == "2026-08-28"


def test_fomc_calendar_uses_official_2026_dates_with_source_and_no_invented_time(tmp_path, monkeypatch):
    agent = _new_agent(tmp_path, monkeypatch)

    events = agent._get_events_for_date_range(
        datetime(2026, 9, 1), datetime(2026, 12, 31), allow_remote=False
    )
    payloads = [event.to_dict() for event in events if event.id.startswith("fomc_")]

    assert [item["id"] for item in payloads] == ["fomc_20260916", "fomc_20261028", "fomc_20261209"]
    assert all(item["time_precision"] == "date_only" for item in payloads)
    assert all(item["source_url"].startswith("https://www.federalreserve.gov/") for item in payloads)
    assert all("not inferred" in item["description"] for item in payloads)


def test_fomc_calendar_carries_official_2027_schedule_when_navigating_forward(tmp_path, monkeypatch):
    agent = _new_agent(tmp_path, monkeypatch)

    events = agent._get_events_for_date_range(
        datetime(2027, 1, 1), datetime(2027, 3, 31), allow_remote=False
    )

    assert [event.id for event in events if event.id.startswith("fomc_")] == [
        "fomc_20270127", "fomc_20270317"
    ]


def _stale_bulk_agent(tmp_path, monkeypatch, warming):
    """Agent with a stale all-reporters bulk cache and a mocked FinnHub client.
    `warming` simulates a background bulk warm already in progress."""
    agent = _new_agent(tmp_path, monkeypatch)
    agent._earnings_bulk_fetched_at = None  # stale -> _earnings_bulk_is_stale() True
    agent._earnings_warming = warming
    agent._fh = MagicMock()
    agent._fh.is_available.return_value = True
    fetch_calls = []

    def _fake_fetch(start, end, watchlist_tickers=None, restrict_to_watchlist=True):
        fetch_calls.append((start, end))
        return []

    monkeypatch.setattr(agent, "_fetch_finnhub_earnings_calendar", _fake_fetch)
    monkeypatch.setattr(agent, "_fetch_nasdaq_earnings_calendar", lambda *_args: [])
    monkeypatch.setattr(agent, "_save_cache_to_disk", lambda: None)
    return agent, fetch_calls


def test_inline_refresh_skips_when_background_warm_in_progress(tmp_path, monkeypatch):
    """REGRESSION (audit #6): when a background bulk warm is already running,
    an inline (allow_remote=True) refresh must NOT fire a second concurrent
    FinnHub fetch for the same range — the background warm will populate the
    cache and the next cycle sees fresh data."""
    agent, fetch_calls = _stale_bulk_agent(tmp_path, monkeypatch, warming=True)
    agent.get_calendar_events_range(
        datetime(2026, 8, 9), datetime(2026, 8, 11), allow_remote=True
    )
    assert fetch_calls == []  # no redundant inline fetch while background warming


def test_inline_refresh_fetches_when_no_background_warm(tmp_path, monkeypatch):
    """Control: with no background warm in progress, an inline refresh DOES
    fetch (the normal explicit-refresh path). Guards against over-fixing #6."""
    agent, fetch_calls = _stale_bulk_agent(tmp_path, monkeypatch, warming=False)
    agent.get_calendar_events_range(
        datetime(2026, 8, 9), datetime(2026, 8, 11), allow_remote=True
    )
    assert len(fetch_calls) == 1  # inline fetch fires normally


def test_cache_only_read_never_schedules_or_performs_remote_refresh(tmp_path, monkeypatch):
    """A cache-only API contract must remain side-effect free when stale."""
    agent, fetch_calls = _stale_bulk_agent(tmp_path, monkeypatch, warming=False)

    agent.get_calendar_events_range(
        datetime(2026, 8, 9), datetime(2026, 8, 11), allow_remote=False
    )

    assert fetch_calls == []
    assert agent._earnings_warming is False


def test_nasdaq_bulk_fallback_parses_full_public_calendar_rows(tmp_path, monkeypatch):
    agent = _new_agent(tmp_path, monkeypatch)

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "data": {
                    "rows": [
                        {
                            "symbol": "AMAT",
                            "name": "Applied Materials, Inc.",
                            "time": "time-after-hours",
                            "marketCap": "$167,294,400,000",
                            "fiscalQuarterEnding": "Jul/2026",
                            "epsForecast": "$2.11",
                            "noOfEsts": "18",
                        },
                        {
                            "symbol": "NU",
                            "name": "Nu Holdings Ltd.",
                            "time": "time-not-supplied",
                            "marketCap": "$74,000,000,000",
                            "epsForecast": "N/A",
                            "noOfEsts": "",
                        },
                    ]
                }
            }

    calls = []

    def _fake_http_get(url, **kwargs):
        calls.append((url, kwargs))
        return _Response()

    monkeypatch.setattr("agents.economic_calendar_agent.http_get", _fake_http_get)
    rows = agent._fetch_nasdaq_earnings_calendar(
        datetime(2026, 8, 13), datetime(2026, 8, 13)
    )

    assert len(calls) == 1
    assert calls[0][1]["params"] == {"date": "2026-08-13"}
    assert [row.symbol for row in rows] == ["AMAT", "NU"]
    assert rows[0].when == "amc"
    assert rows[0].eps_estimated == 2.11
    assert rows[0].market_cap == 167_294_400_000
    assert rows[0].eps_estimate_count == 18
    assert rows[0].confirmed is False
    assert rows[0].date_confidence == "estimated"
    assert rows[1].eps_estimated is None
    assert agent._nasdaq_last_fetch_succeeded is True


def test_bulk_refresh_uses_nasdaq_when_finnhub_is_not_configured(tmp_path, monkeypatch):
    agent = _new_agent(tmp_path, monkeypatch)
    agent._fh = MagicMock()
    agent._fh.is_available.return_value = False
    event = EarningsEvent(
        symbol="AMAT",
        date=datetime(2026, 8, 13),
        source="nasdaq_zacks_estimate",
        date_confidence="estimated",
    )

    def _nasdaq(*_args):
        agent._nasdaq_last_fetch_succeeded = True
        return [event]

    monkeypatch.setattr(agent, "_fetch_nasdaq_earnings_calendar", _nasdaq)
    saved = []
    monkeypatch.setattr(agent, "_save_cache_to_disk", lambda force=False: saved.append(force))

    agent._do_bulk_earnings_fetch(datetime(2026, 8, 13), datetime(2026, 8, 23))

    assert "AMAT" in agent._events_cache["earnings"]
    assert agent._earnings_bulk_fetched_at is not None
    assert saved == [True]
