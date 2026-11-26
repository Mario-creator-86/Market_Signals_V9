import json
from pathlib import Path

import pytest


FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "quality_gate"
pytestmark = pytest.mark.golden


def _load(name: str):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_premarket_golden_output(client, monkeypatch):
    expected = _load("expected_premarket.json")

    class _FakePremarket:
        def get_state(self):
            return dict(expected)

    monkeypatch.setattr("agents.premarket_scanner_agent.get_premarket_scanner_agent", lambda: _FakePremarket())
    monkeypatch.setattr("api.main._agent_enabled", lambda name, config=None, default=True: True)

    response = client.get("/api/premarket")
    assert response.status_code == 200
    payload = response.json()

    assert payload["as_of_utc"] == expected["as_of_utc"]
    assert payload["status"]["last_error"] is None
    assert payload["candidates"] == expected["candidates"]


def test_pre_earnings_golden_output(client, monkeypatch):
    expected = _load("expected_pre_earnings.json")

    class _FakePreEarnings:
        def get_state(self):
            return dict(expected)

    monkeypatch.setattr("agents.pre_earnings_agent.get_pre_earnings_agent", lambda: _FakePreEarnings())
    monkeypatch.setattr("api.main._agent_enabled", lambda name, config=None, default=True: True)

    response = client.get("/api/pre-earnings")
    assert response.status_code == 200
    payload = response.json()

    assert payload["as_of_utc"] == expected["as_of_utc"]
    assert payload["items"] == expected["items"]
    assert payload["status"]["last_error"] is None
