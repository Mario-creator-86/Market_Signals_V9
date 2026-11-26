from datetime import datetime, timezone

from core.data_model.survivorship import SurvivorshipRegistry


def _iso(s):
    return datetime.fromisoformat(s)


def test_is_listed_true_after_register_listed(tmp_path):
    reg = SurvivorshipRegistry(path=tmp_path / "t.db")
    reg.register_listed("AAPL", event_time=_iso("2020-01-01T00:00:00+00:00"))
    assert reg.is_listed("AAPL", _iso("2026-01-01T00:00:00+00:00")) is True


def test_is_listed_false_before_listing(tmp_path):
    reg = SurvivorshipRegistry(path=tmp_path / "t.db")
    reg.register_listed("AAPL", event_time=_iso("2020-01-01T00:00:00+00:00"))
    assert reg.is_listed("AAPL", _iso("2019-01-01T00:00:00+00:00")) is False


def test_is_listed_false_after_delisting(tmp_path):
    reg = SurvivorshipRegistry(path=tmp_path / "t.db")
    reg.register_listed("ENRN", event_time=_iso("1999-01-01T00:00:00+00:00"))
    reg.register_delisted("ENRN", event_time=_iso("2001-12-01T00:00:00+00:00"))
    assert reg.is_listed("ENRN", _iso("2000-06-01T00:00:00+00:00")) is True
    assert reg.is_listed("ENRN", _iso("2002-06-01T00:00:00+00:00")) is False


def test_is_listed_unknown_symbol_is_false(tmp_path):
    reg = SurvivorshipRegistry(path=tmp_path / "t.db")
    assert reg.is_listed("NOPE", _iso("2026-01-01T00:00:00+00:00")) is False  # §32: unknown -> not listed


def test_ticker_change_old_false_new_true(tmp_path):
    reg = SurvivorshipRegistry(path=tmp_path / "t.db")
    reg.register_listed("FB", event_time=_iso("2012-05-01T00:00:00+00:00"))
    reg.register_ticker_change("FB", "META", event_time=_iso("2022-06-09T00:00:00+00:00"))
    assert reg.is_listed("FB", _iso("2021-01-01T00:00:00+00:00")) is True   # before change
    assert reg.is_listed("FB", _iso("2023-01-01T00:00:00+00:00")) is False    # after change (old ticker gone)
    assert reg.is_listed("META", _iso("2023-01-01T00:00:00+00:00")) is True    # new ticker listed


def test_universe_at_filters_delisted(tmp_path):
    reg = SurvivorshipRegistry(path=tmp_path / "t.db")
    reg.register_listed("AAPL", event_time=_iso("2020-01-01T00:00:00+00:00"))
    reg.register_listed("ENRN", event_time=_iso("1999-01-01T00:00:00+00:00"))
    reg.register_delisted("ENRN", event_time=_iso("2001-12-01T00:00:00+00:00"))
    base = {"AAPL", "ENRN", "MSFT"}
    res = reg.universe_at(_iso("2026-01-01T00:00:00+00:00"), base)
    assert res == {"AAPL", "MSFT"}  # ENRN delisted, MSFT unknown -> excluded (§32)


def test_universe_at_returns_all_when_none_delisted(tmp_path):
    reg = SurvivorshipRegistry(path=tmp_path / "t.db")
    for s in ["AAPL", "MSFT", "GOOG"]:
        reg.register_listed(s, event_time=_iso("2020-01-01T00:00:00+00:00"))
    base = {"AAPL", "MSFT", "GOOG"}
    assert reg.universe_at(_iso("2026-01-01T00:00:00+00:00"), base) == base