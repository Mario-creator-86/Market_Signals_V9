from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from core.sec.edgar_adapter import EdgarToolsAdapter, SECProviderUnavailable


class _Filings(list):
    def head(self, limit):
        return _Filings(self[:limit])


class _Company:
    name = "Example Corp"

    def __init__(self, ticker, filings):
        self.ticker = ticker
        self._filings = filings
        self.requested_forms = None

    def get_filings(self, form=None):
        self.requested_forms = form
        return self._filings


def _filing(**changes):
    values = {
        "form": "10-K",
        "filing_date": date(2026, 8, 1),
        "accession_no": "0001-26-000001",
        "homepage_url": "https://www.sec.gov/Archives/edgar/data/1/filing-index.html",
        "primary_document": "form10k.htm",
        "description": "Annual report",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_adapter_requires_owner_sec_identity(monkeypatch):
    monkeypatch.delenv("EDGAR_IDENTITY", raising=False)
    adapter = EdgarToolsAdapter(company_factory=lambda ticker: None, set_identity=lambda value: None)
    with pytest.raises(SECProviderUnavailable, match="EDGAR_IDENTITY"):
        adapter.fetch_filings("AAPL", forms=["10-K"], limit=5, as_of=datetime.now(timezone.utc))


def test_adapter_sets_identity_once_and_maps_only_allowed_point_in_time_filings(monkeypatch):
    monkeypatch.setenv("EDGAR_IDENTITY", "Market Scanner owner@example.com")
    identities = []
    company = _Company("AAPL", _Filings([
        _filing(),
        _filing(form="8-K", accession_no="0001-26-000002", filing_date=date(2026, 8, 20)),
        _filing(form="S-1", accession_no="0001-26-000003"),
    ]))
    adapter = EdgarToolsAdapter(
        company_factory=lambda ticker: company,
        set_identity=identities.append,
        now=lambda: datetime(2026, 8, 13, 12, tzinfo=timezone.utc),
    )
    cutoff = datetime(2026, 8, 13, tzinfo=timezone.utc)
    first = adapter.fetch_filings("aapl", forms=["10-K", "8-K"], limit=5, as_of=cutoff)
    second = adapter.fetch_filings("AAPL", forms=["10-K"], limit=5, as_of=cutoff)
    assert identities == ["Market Scanner owner@example.com"]
    assert len(first) == len(second) == 1
    assert first[0].ticker == "AAPL"
    assert first[0].company_name == "Example Corp"
    assert first[0].pit.available_from == datetime(2026, 8, 13, 12, tzinfo=timezone.utc)


def test_adapter_rejects_non_sec_urls_and_invalid_symbols(monkeypatch):
    monkeypatch.setenv("EDGAR_IDENTITY", "Owner owner@example.com")
    company = _Company("AAPL", _Filings([_filing(homepage_url="https://evil.example/steal")]))
    adapter = EdgarToolsAdapter(company_factory=lambda ticker: company, set_identity=lambda value: None)
    assert adapter.fetch_filings("AAPL", forms=["10-K"], limit=5, as_of=datetime.now(timezone.utc)) == ()
    with pytest.raises(ValueError, match="ticker"):
        adapter.fetch_filings("../AAPL", forms=["10-K"], limit=5, as_of=datetime.now(timezone.utc))


def test_adapter_rejects_invalid_form_configuration(monkeypatch):
    monkeypatch.setenv("EDGAR_IDENTITY", "Owner owner@example.com")
    adapter = EdgarToolsAdapter(company_factory=lambda ticker: None, set_identity=lambda value: None)
    with pytest.raises(ValueError, match="form"):
        adapter.fetch_filings("AAPL", forms=["<script>"], limit=5, as_of=datetime.now(timezone.utc))
