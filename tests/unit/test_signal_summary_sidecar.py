"""Contracts for the latency-safe signal summary sidecar."""

import core.state_persistence as persistence
from api import engine_service, main
from api.state_service import build_stats


def _payload():
    return {
        "open": {
            "open-1": {"id": "open-1", "ticker": "BTC-USD", "status": "Open", "entry": 100},
        },
        "closed": [
            {"id": "loss-1", "ticker": "ETH-USD", "status": "LOST", "strategy_source": "V2", "outcome_data_valid": True, "closed_at": "2026-07-15T12:00:00+00:00"},
            {"id": "win-1", "ticker": "SOL-USD", "result": "WIN", "strategy_source": "V2", "outcome_data_valid": True, "closed_at": "2026-07-15T13:00:00+00:00"},
        ],
        "updated_at": "2026-07-15T13:00:00+00:00",
    }


def test_lean_signal_load_uses_sidecar_without_persistent_history(tmp_path, monkeypatch):
    signals_path = tmp_path / "signals_state.json"
    monkeypatch.setattr(persistence, "SIGNALS_PATH", signals_path)
    persistence._write_signals_summary(_payload())
    monkeypatch.setattr(
        persistence,
        "load_persistent_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("full history must not be read")),
    )

    data = persistence.load_signals_payload({"open": {}, "closed": []}, include_closed=False)

    assert data["open"]["open-1"]["ticker"] == "BTC-USD"
    assert data["open"]["open-1"]["research_only"] is True
    assert len(data["closed"]) == 2
    assert data["_summary"] == {
        "open_count": 1,
        "closed_count": 2,
        "wins": 1,
        "losses": 1,
        "verified_outcomes": 2,
        "unverified_outcomes": 0,
        "performance_basis": "verified_price_provenance",
        "latest_closed_at": "2026-07-15T13:00:00+00:00",
        "updated_at": "2026-07-15T13:00:00+00:00",
    }


def test_full_signal_load_reuses_revision_cache(tmp_path, monkeypatch):
    signals_path = tmp_path / "signals_state.json"
    monkeypatch.setattr(persistence, "SIGNALS_PATH", signals_path)
    persistence._signals_payload_cache.update({"marker": None, "data": None})
    calls = []

    def _load(*_args, **_kwargs):
        calls.append(True)
        return _payload()

    monkeypatch.setattr(persistence, "load_persistent_json", _load)

    first = persistence.load_signals_payload()
    second = persistence.load_signals_payload()

    assert len(calls) == 1
    assert first == second
    assert first is not second


def test_subprocess_engine_status_reads_summary_only(monkeypatch):
    monkeypatch.setattr(engine_service, "_ENGINE_MODE", "subprocess")
    monkeypatch.setattr(engine_service, "is_engine_running", lambda: True)
    monkeypatch.setattr(
        engine_service,
        "load_signals_summary",
        lambda default: {"open_count": 7, "open": {"legacy": {}}},
    )

    status = engine_service.get_engine_status()

    assert status["running"] is True
    assert status["signals_count"] == 7


def test_signals_api_defaults_to_bounded_summary_window(monkeypatch):
    calls = []

    def _load(_default, *, include_closed):
        calls.append(include_closed)
        return {
            "open": {},
            "closed": [
                {"id": "recent", "status": "WIN", "closed_at": "2026-07-15T13:00:00+00:00"},
            ],
            "_summary": {"closed_count": 500},
        }

    monkeypatch.setattr(main, "load_signals_payload", _load)

    response = main.api_signals(limit=999)

    assert calls == [False]
    assert response["limit"] == 250
    assert response["closed_count"] == 500
    assert response["history_complete"] is False
    assert response["next_offset"] is None


def test_api_stats_exclude_legacy_outcomes_without_price_provenance():
    stats = build_stats({
        "open": {},
        "closed": [
            {"id": "legacy-win", "status": "WIN"},
            {"id": "verified-loss", "status": "LOST", "outcome_data_valid": True},
        ],
    })

    assert stats["wins"] == 0
    assert stats["losses"] == 1
    assert stats["verified_outcomes"] == 1
    assert stats["unverified_outcomes"] == 1
    assert stats["performance_basis"] == "verified_price_provenance"


def test_sidecar_excludes_research_only_outcomes_and_migrates_old_open_rows(tmp_path, monkeypatch):
    signals_path = tmp_path / "signals_state.json"
    monkeypatch.setattr(persistence, "SIGNALS_PATH", signals_path)
    persistence._write_signals_summary({
        "open": {
            "legacy-open": {"id": "legacy-open", "ticker": "BTC-USD", "status": "Open"},
            "v2-open": {"id": "v2-open", "ticker": "ETH-USD", "status": "Open", "strategy_source": "V2"},
        },
        "closed": [
            {"id": "v1-win", "status": "WIN", "outcome_data_valid": True},
            {"id": "v2-loss", "status": "LOST", "outcome_data_valid": True, "strategy_source": "V2"},
        ],
    })

    data = persistence.load_signals_payload(include_closed=False)

    assert data["open"]["legacy-open"]["research_only"] is True
    assert "research_only" not in data["open"]["v2-open"]
    assert data["_summary"]["wins"] == 0
    assert data["_summary"]["losses"] == 1
    assert data["_summary"]["verified_outcomes"] == 1
    assert data["_summary"]["unverified_outcomes"] == 1
