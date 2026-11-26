"""Wave 3 — macro-calendar-2027 zone.

The Calendar loads one bundled file of US macro release dates per year
(``data/us_macro_calendar_<year>.json``). These tests pin:

* the 2027 file has the same structure as the 2026 file and every date in it
  is a real 2027 working day (never a weekend or a federal holiday, never a
  duplicate) with a recorded official source;
* 08:30 ET is converted to UTC correctly in winter (EST) and summer (EDT);
* the coverage block reports a partly published schedule per indicator instead
  of looking complete (synthetic file in a temp dir, not the real one);
* the 2027 FOMC rows match the Federal Reserve calendar as read on 2026-11-15.
"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import api.main as main
from agents.economic_calendar_agent import EconomicCalendarAgent

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
INDICATORS = ["jobs", "cpi", "pce", "retail", "ppi", "gdp"]


def _load(year):
    return json.loads((DATA_DIR / f"us_macro_calendar_{year}.json").read_text(encoding="utf-8"))


def _nth_weekday(year, month, weekday, n):
    first = date(year, month, 1)
    return first + timedelta((weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    last = date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta((last.weekday() - weekday) % 7)


def _observed(day):
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _federal_holidays(year):
    return {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),      # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),      # Washington's Birthday
        _last_weekday(year, 5, 0),        # Memorial Day
        _observed(date(year, 6, 19)),     # Juneteenth
        _observed(date(year, 7, 4)),      # Independence Day
        _nth_weekday(year, 9, 0, 1),      # Labor Day
        _nth_weekday(year, 10, 0, 2),     # Columbus Day
        _observed(date(year, 11, 11)),    # Veterans Day
        _nth_weekday(year, 11, 3, 4),     # Thanksgiving
        _observed(date(year, 12, 25)),    # Christmas
        _observed(date(year + 1, 1, 1)),  # New Year's Day observed on Dec 31
    }


# ------------------------------------------------------------ the real file
def test_2027_file_has_the_same_structure_as_the_2026_file():
    old, new = _load(2026), _load(2027)

    assert set(new) == set(old) == {"year", "source", "note", "time_et", "events", "source_url"}
    assert new["year"] == 2027 and old["year"] == 2026
    assert new["time_et"] == old["time_et"] == "08:30"
    assert isinstance(new["source"], str) and new["source"]
    assert isinstance(new["note"], str) and new["note"]
    assert new["source_url"].startswith("https://")

    assert [e["indicator"] for e in new["events"]] == [e["indicator"] for e in old["events"]] == INDICATORS
    for old_entry, new_entry in zip(old["events"], new["events"]):
        assert new_entry["title"] == old_entry["title"]
        assert new_entry["impact"] == old_entry["impact"]
        assert set(old_entry) <= set(new_entry)
        assert isinstance(new_entry["dates"], list)
        assert all(isinstance(ds, str) for ds in new_entry["dates"])


def test_2027_file_records_an_official_source_per_indicator():
    official_hosts = ("https://www.bls.gov/", "https://www.bea.gov/", "https://www.census.gov/")
    for entry in _load(2027)["events"]:
        assert entry["source_url"].startswith(official_hosts), entry["indicator"]
        retrieved = datetime.strptime(entry["retrieved"], "%Y-%m-%d").date()
        assert date(2026, 11, 15) <= retrieved <= date(2027, 12, 31), entry["indicator"]
        if entry.get("complete", True) is not True:
            # a partly published schedule must say through which month
            assert str(entry.get("note") or "").strip(), entry["indicator"]
        if not entry["dates"]:
            assert entry.get("complete") is False, entry["indicator"]


def test_2027_dates_are_valid_working_days_without_duplicates():
    holidays = _federal_holidays(2027)
    assert date(2027, 7, 5) in holidays and date(2027, 12, 24) in holidays  # observed days
    for entry in _load(2027)["events"]:
        days = [datetime.strptime(ds, "%Y-%m-%d").date() for ds in entry["dates"]]
        assert all(ds == day.isoformat() for ds, day in zip(entry["dates"], days))
        assert len(set(days)) == len(days), f"duplicate date for {entry['indicator']}"
        assert days == sorted(days), f"dates out of order for {entry['indicator']}"
        months = [day.month for day in days]
        assert len(set(months)) == len(months), f"two {entry['indicator']} releases in one month"
        for day in days:
            assert day.year == 2027, (entry["indicator"], day)
            assert day.weekday() < 5, f"{entry['indicator']} {day} is a weekend"
            assert day not in holidays, f"{entry['indicator']} {day} is a federal holiday"


def test_2027_is_not_a_copy_of_2026_shifted_by_a_year():
    old = {e["indicator"]: [ds[5:] for ds in e["dates"]] for e in _load(2026)["events"]}
    for entry in _load(2027)["events"]:
        if entry["dates"]:
            assert [ds[5:] for ds in entry["dates"]] != old[entry["indicator"]], entry["indicator"]


def test_2027_release_time_is_converted_from_eastern_time_winter_and_summer():
    events = []
    main._merge_macro_calendar_events(datetime(2027, 1, 1), datetime(2027, 12, 31), events)
    assert events, "the 2027 schedule produced no calendar events"
    assert all(e["id"].startswith("macro_") and e["date_formatted"].endswith("08:30 ET") for e in events)
    assert all(e["source_url"].startswith("https://") for e in events)
    assert all(e["time_precision"] == "published_time" for e in events)

    by_month = {}
    for event in events:
        by_month.setdefault(int(event["date_formatted"][5:7]), []).append(event)
    # 08:30 ET is 13:30 UTC on standard time (January) and 12:30 UTC on
    # daylight time (July)
    assert by_month[1] and all(e["date"].endswith("T13:30:00+00:00") for e in by_month[1])
    assert by_month[7] and all(e["date"].endswith("T12:30:00+00:00") for e in by_month[7])
    for event in events:
        assert event["date"][:10] == event["date_formatted"][:10]

    ids = [e["id"] for e in events]
    assert len(ids) == len(set(ids))
    assert len(events) == sum(len(e["dates"]) for e in _load(2027)["events"])


def test_real_schedule_coverage_is_consistent_with_the_files():
    coverage = main._macro_calendar_coverage(datetime(2026, 11, 1), datetime(2027, 2, 6))
    assert coverage["years_available"] == [2026, 2027]
    assert coverage["years_missing"] == []
    assert coverage["complete"] == (not coverage["years_partial"])
    assert len(coverage["partial_notes"]) == len(coverage["years_partial"])

    by_year = {s["year"]: s for s in coverage["schedules"]}
    # the 2026 file carries no flags at all, so it is a complete year
    assert by_year[2026]["complete"] is True
    assert [i["indicator"] for i in by_year[2027]["indicators"]] == INDICATORS
    for item, entry in zip(by_year[2027]["indicators"], _load(2027)["events"]):
        assert item["dates_count"] == len(entry["dates"])
        assert item["published_through"] == (max(entry["dates"]) if entry["dates"] else None)
        assert item["complete"] == (bool(entry["dates"]) and entry.get("complete", True) is True)
        assert item["retrieved"] == entry["retrieved"]


# ---------------------------------------------- synthetic partial schedules
def _monthly(year, day, months):
    return [f"{year}-{month:02d}-{day:02d}" for month in months]


def _synthetic(year=2027):
    full, half = range(1, 13), range(1, 7)
    return {
        "year": year,
        "source": "synthetic test schedule",
        "note": "",
        "time_et": "08:30",
        "source_url": "https://example.test/fallback",
        "events": [
            {"indicator": "jobs", "title": "US Jobs Report (Nonfarm Payrolls)", "impact": "high",
             "dates": _monthly(year, 5, full), "complete": True,
             "source_url": "https://example.test/jobs", "retrieved": "2026-11-15"},
            {"indicator": "cpi", "title": "US CPI (Consumer Price Index)", "impact": "high",
             "dates": _monthly(year, 12, full)},
            {"indicator": "pce", "title": "US PCE (Personal Income & Outlays)", "impact": "high",
             "dates": _monthly(year, 26, half), "complete": False,
             "note": "Published through June only."},
            {"indicator": "retail", "title": "US Retail Sales (Advance)", "impact": "high",
             "dates": [], "complete": False, "note": "Nothing published yet."},
            {"indicator": "ppi", "title": "US PPI (Producer Price Index)", "impact": "medium",
             "dates": _monthly(year, 13, full)},
            {"indicator": "gdp", "title": "US GDP", "impact": "medium",
             "dates": _monthly(year, 26, half), "complete": False,
             "note": "Published through June only."},
        ],
    }


@pytest.fixture
def schedule_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_macro_calendar_path",
                        lambda year: str(tmp_path / f"us_macro_calendar_{year}.json"))

    def write(year, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (tmp_path / f"us_macro_calendar_{year}.json").write_text(text, encoding="utf-8")

    return write


def test_coverage_reports_a_partly_published_schedule_per_indicator(schedule_dir):
    schedule_dir(2027, _synthetic())

    coverage = main._macro_calendar_coverage(datetime(2027, 1, 1), datetime(2027, 12, 31))

    assert coverage["years_available"] == [2027]
    assert coverage["years_missing"] == []
    assert coverage["years_partial"] == [2027]
    assert coverage["complete"] is False

    schedule = coverage["schedules"][0]
    assert schedule["year"] == 2027 and schedule["complete"] is False
    assert schedule["summary"] == (
        "US macro schedule for 2027: Jobs, CPI and PPI published through December; "
        "PCE and GDP through June; Retail Sales not published yet."
    )
    assert coverage["partial_notes"] == [
        schedule["summary"] + " Later dates are not published by the agencies yet — "
        "an empty month there does not mean nothing is scheduled."
    ]

    items = {i["indicator"]: i for i in schedule["indicators"]}
    assert list(items) == INDICATORS
    assert items["jobs"]["complete"] is True and items["jobs"]["published_through"] == "2027-12-05"
    assert items["cpi"]["complete"] is True          # no flag = complete, as in the 2026 file
    assert items["pce"]["complete"] is False and items["pce"]["published_through"] == "2027-06-26"
    assert items["pce"]["note"] == "Published through June only."
    assert items["gdp"]["complete"] is False and items["gdp"]["dates_count"] == 6
    assert items["retail"]["complete"] is False and items["retail"]["published_through"] is None
    assert items["retail"]["note"] == "Nothing published yet."
    assert items["jobs"]["source_url"] == "https://example.test/jobs"
    assert items["jobs"]["retrieved"] == "2026-11-15"
    assert items["cpi"]["source_url"] == "https://example.test/fallback"


def test_partial_schedule_shows_only_published_dates_nothing_extrapolated(schedule_dir):
    schedule_dir(2027, _synthetic())

    events = []
    main._merge_macro_calendar_events(datetime(2027, 1, 1), datetime(2027, 12, 31), events)

    by_indicator = {}
    for event in events:
        by_indicator.setdefault(event["id"].split("_")[1], []).append(event["date_formatted"][:10])
    assert "retail" not in by_indicator
    assert by_indicator["pce"] == _monthly(2027, 26, range(1, 7))
    assert by_indicator["gdp"] == _monthly(2027, 26, range(1, 7))
    assert len(by_indicator["jobs"]) == len(by_indicator["cpi"]) == len(by_indicator["ppi"]) == 12


def test_coverage_of_a_fully_published_year_is_complete(schedule_dir):
    payload = _synthetic(2026)
    for entry in payload["events"]:
        entry["dates"] = _monthly(2026, 10, range(1, 13))
        entry.pop("complete", None)
        entry.pop("note", None)
    schedule_dir(2026, payload)

    coverage = main._macro_calendar_coverage(datetime(2026, 3, 1), datetime(2026, 5, 31))

    assert coverage["complete"] is True
    assert coverage["years_partial"] == [] and coverage["partial_notes"] == []
    assert coverage["schedules"][0]["summary"] == (
        "US macro schedule for 2026: Jobs, CPI, PCE, Retail Sales, PPI and GDP published through December."
    )


def test_an_explicit_false_flag_wins_even_when_all_months_are_listed(schedule_dir):
    payload = _synthetic()
    payload["events"][0]["complete"] = False
    payload["events"][0]["note"] = "December date still marked tentative by the agency."
    schedule_dir(2027, payload)

    coverage = main._macro_calendar_coverage(datetime(2027, 1, 1), datetime(2027, 1, 31))

    jobs = coverage["schedules"][0]["indicators"][0]
    assert jobs["complete"] is False
    assert jobs["note"] == "December date still marked tentative by the agency."
    assert coverage["complete"] is False


def test_coverage_counts_missing_and_unreadable_years_as_missing(schedule_dir):
    schedule_dir(2026, _synthetic(2026))
    schedule_dir(2027, "{ this is not json")

    coverage = main._macro_calendar_coverage(datetime(2026, 12, 1), datetime(2028, 1, 31))

    assert coverage["years_available"] == [2026]
    assert coverage["years_missing"] == [2027, 2028]
    assert coverage["years_unreadable"] == [2027]
    assert coverage["complete"] is False
    assert [s["year"] for s in coverage["schedules"]] == [2026]


def test_calendar_range_endpoint_passes_the_macro_schedule_status_through(client):
    response = client.get("/api/calendar/range", params={"start": "2026-12-01", "end": "2027-02-27"})
    assert response.status_code == 200
    macro = response.json()["coverage"]["macro"]
    assert {"years_available", "years_missing", "years_partial", "complete",
            "schedules", "partial_notes"} <= set(macro)
    assert 2027 in macro["years_available"]
    january = [e for e in response.json()["economic"]
               if str(e.get("id", "")).startswith("macro_") and e["date_formatted"].startswith("2027-01")]
    assert january, "January 2027 shows no macro releases"


# ------------------------------------------------------------------- FOMC
def test_fomc_2027_rows_match_the_federal_reserve_calendar_read_2026_11_15(tmp_path, monkeypatch):
    """federalreserve.gov/monetarypolicy/fomccalendars.htm, "Last Update:
    November 12, 2026": January 26-27, March 16-17, April 27-28, June 8-9,
    July 27-28, September 14-15, October 26-27 (tagged "Monetary Policy
    Strategy Meeting"), November 16-17, and no December meeting."""
    monkeypatch.chdir(tmp_path)
    agent = EconomicCalendarAgent()
    agent._cache_path = str(tmp_path / "calendar_events_cache.json")
    agent._events_cache = {"economic": {}, "earnings": {}, "ipo": {}, "earnings_meta": {}}

    assert [d.strftime("%Y-%m-%d") for d in agent.FOMC_DATES if d.year == 2027] == [
        "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
        "2027-07-28", "2027-09-15", "2027-10-27", "2027-11-17",
    ]
    assert all(d.weekday() == 2 for d in agent.FOMC_DATES)  # decision day is the Wednesday

    events = agent._get_events_for_date_range(datetime(2027, 9, 1), datetime(2027, 12, 31), allow_remote=False)
    fomc = {e.id: e.to_dict() for e in events if e.id.startswith("fomc_")}
    assert list(fomc) == ["fomc_20270915", "fomc_20271027", "fomc_20271117"]
    assert "Monetary Policy Strategy Meeting" in fomc["fomc_20271027"]["description"]
    assert "November 16–17, 2027" in fomc["fomc_20271117"]["description"]
    assert all(item["time_precision"] == "date_only" for item in fomc.values())
    fomc_coverage = agent.get_calendar_coverage()["fomc"]
    assert fomc_coverage["schedule_through"] == "2027-11-17"
    assert "2026-11-15" in fomc_coverage["source_name"]
