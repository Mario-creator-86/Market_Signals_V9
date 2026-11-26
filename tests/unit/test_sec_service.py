from datetime import datetime, timedelta, timezone

from core.data_model.entities import SECFiling
from core.data_model.pit import PointInTime
from core.sec.edgar_adapter import SECProviderUnavailable
from core.sec.service import SECFilingCache, SECFilingService


def _record(*, filed_at, available_from, accession="0001"):
    return SECFiling(
        symbol=None,
        form="10-K",
        filed_at=filed_at,
        accession_no=accession,
        url="https://www.sec.gov/Archives/example",
        pit=PointInTime(
            event_time=filed_at,
            received_at=available_from,
            available_from=available_from,
            fetched_at=available_from,
            version=0,
        ),
        ticker="AAPL",
        company_name="Apple Inc.",
    )


class _Provider:
    def __init__(self, records=(), error=None):
        self.records = tuple(records)
        self.error = error
        self.calls = 0

    def fetch_filings(self, *args, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.records


def _service(tmp_path, provider, now, ttl=3600):
    return SECFilingService(
        provider,
        SECFilingCache(tmp_path / "sec.json"),
        forms=["10-K", "10-Q", "8-K", "4"],
        cache_ttl_seconds=ttl,
        max_filings=20,
        now=lambda: now,
    )


def test_cache_first_service_does_not_redownload_unchanged_filings(tmp_path):
    now = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    provider = _Provider([_record(filed_at=now - timedelta(days=3), available_from=now)])
    service = _service(tmp_path, provider, now)
    first = service.get_context("AAPL")
    second = service.get_context("AAPL")
    assert provider.calls == 1
    assert first["source"] == "edgartools"
    assert second["source"] == "cache"
    assert second["total_filings"] == 1


def test_service_filters_cache_by_point_in_time_availability(tmp_path):
    fetch_time = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    record = _record(filed_at=fetch_time - timedelta(days=1), available_from=fetch_time)
    provider = _Provider([record])
    service = _service(tmp_path, provider, fetch_time)
    service.get_context("AAPL")
    before_observation = service.get_context("AAPL", as_of=fetch_time - timedelta(hours=1))
    assert before_observation["filings"] == []


def test_historical_as_of_never_fetches_current_sec_data(tmp_path):
    cutoff = datetime(2020, 1, 1, tzinfo=timezone.utc)
    provider = _Provider([_record(filed_at=cutoff - timedelta(days=1), available_from=cutoff)])
    service = _service(tmp_path, provider, datetime(2026, 8, 13, tzinfo=timezone.utc))
    payload = service.get_context("AAPL", as_of=cutoff)
    assert provider.calls == 0
    assert payload["status"] == "unavailable"
    assert payload["error"] == "point-in-time SEC cache unavailable"


def test_stale_cache_is_returned_with_explicit_error_when_provider_fails(tmp_path):
    initial = datetime(2026, 8, 13, 10, tzinfo=timezone.utc)
    cache = SECFilingCache(tmp_path / "sec.json")
    record = _record(filed_at=initial - timedelta(days=1), available_from=initial)
    cache.put("AAPL|10-K|20", fetched_at=initial, filings=[record])
    service = SECFilingService(
        _Provider(error=SECProviderUnavailable("SEC offline")), cache,
        forms=["10-K"], cache_ttl_seconds=1, max_filings=20,
        now=lambda: initial + timedelta(hours=2),
    )
    payload = service.get_context("AAPL")
    assert payload["source"] == "stale_cache"
    assert payload["stale"] is True
    assert "SEC offline" in payload["error"]


def test_missing_provider_and_cache_is_honestly_unavailable(tmp_path):
    now = datetime(2026, 8, 13, tzinfo=timezone.utc)
    service = _service(tmp_path, _Provider(error=SECProviderUnavailable("identity missing")), now)
    payload = service.get_context("AAPL")
    assert payload["status"] == "unavailable"
    assert payload["filings"] == []
    assert "identity missing" in payload["error"]


def test_cache_refresh_preserves_first_observed_time_for_pit_replay(tmp_path):
    first_seen = datetime(2026, 8, 13, 10, tzinfo=timezone.utc)
    later_seen = first_seen + timedelta(hours=2)
    cache = SECFilingCache(tmp_path / "sec.json")
    cache.put(
        "AAPL|10-K|20",
        fetched_at=first_seen,
        filings=[_record(filed_at=first_seen - timedelta(days=1), available_from=first_seen)],
    )
    cache.put(
        "AAPL|10-K|20",
        fetched_at=later_seen,
        filings=[_record(filed_at=first_seen - timedelta(days=1), available_from=later_seen)],
    )
    row = cache.get("AAPL|10-K|20")["filings"][0]
    assert row["available_from"] == first_seen.isoformat()
