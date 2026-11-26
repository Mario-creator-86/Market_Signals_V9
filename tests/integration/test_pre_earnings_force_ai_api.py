"""
tests/integration/test_pre_earnings_force_ai_api.py
====================================================
HTTP-level coverage for POST /api/pre-earnings/refresh-ai — the per-ticker
'Refresh AI' button endpoint. Pins: 400 on empty symbol, 409 when the agent is
disabled, graceful 200 bodies for not_found / ai_unavailable, and that an 'ok'
response BUSTS the 60s `pre_earnings_state` endpoint cache while a failure does
NOT (state unchanged).
"""

import pytest

pytestmark = pytest.mark.integration


class _StubAgent:
    """Stand-in exposing only the surface the handler touches."""
    def __init__(self, force_result):
        self._force_result = force_result
        self.calls = []

    def force_ai_for_symbol(self, symbol):
        self.calls.append(str(symbol).upper().strip())
        return dict(self._force_result)


def _clear_endpoint_cache():
    from api.main import _endpoint_payload_clear
    _endpoint_payload_clear("pre_earnings_state")


def _patch_agent(monkeypatch, agent):
    monkeypatch.setattr(
        "agents.pre_earnings_agent.get_pre_earnings_agent",
        lambda *a, **kw: agent,
    )


def _enable_agent(monkeypatch):
    # The refresh endpoints gate on _agent_enabled("pre_earnings", default=False).
    import api.main as m
    monkeypatch.setattr(m, "_agent_enabled", lambda name, default=False: True)


def test_refresh_ai_ok_busts_state_cache(client, monkeypatch):
    _clear_endpoint_cache()
    _enable_agent(monkeypatch)
    from api.main import _endpoint_payload_set, _endpoint_payload_get
    _patch_agent(monkeypatch, _StubAgent({"status": "ok", "symbol": "AAPL",
                                          "ai_decision": {"used": True, "verdict": "LONG"}}))
    # Pre-seed the GET cache so we can observe the bust.
    _endpoint_payload_set("pre_earnings_state", b'{"cached": true}')

    r = client.post("/api/pre-earnings/refresh-ai?symbol=aapl")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["result"]["status"] == "ok"
    assert body["result"]["ai_decision"]["verdict"] == "LONG"
    # Cache was busted by the successful mutation.
    assert _endpoint_payload_get("pre_earnings_state", 60) is None


def test_refresh_ai_empty_symbol_returns_400(client, monkeypatch):
    _clear_endpoint_cache()
    _enable_agent(monkeypatch)
    agent = _StubAgent({"status": "ok"})
    _patch_agent(monkeypatch, agent)

    r = client.post("/api/pre-earnings/refresh-ai?symbol=")
    assert r.status_code == 400
    assert agent.calls == [], "must not call the agent for an empty symbol"


@pytest.mark.parametrize("symbol", ["AAPL<script>", "TOO-LONG-SYMBOL", "BRK..B"])
def test_refresh_ai_rejects_malformed_symbols(client, monkeypatch, symbol):
    _enable_agent(monkeypatch)
    agent = _StubAgent({"status": "ok"})
    _patch_agent(monkeypatch, agent)

    response = client.post("/api/pre-earnings/refresh-ai", params={"symbol": symbol})

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid symbol"
    assert agent.calls == []


def test_refresh_ai_ai_unavailable_does_not_clear_cache(client, monkeypatch):
    _clear_endpoint_cache()
    _enable_agent(monkeypatch)
    from api.main import _endpoint_payload_set, _endpoint_payload_get
    _patch_agent(monkeypatch, _StubAgent({"status": "ai_unavailable", "symbol": "AAPL"}))
    _endpoint_payload_set("pre_earnings_state", b'{"cached": true}')

    r = client.post("/api/pre-earnings/refresh-ai?symbol=AAPL")
    assert r.status_code == 200
    assert r.json()["status"] == "ai_unavailable"
    # State unchanged → cache must still be present.
    assert _endpoint_payload_get("pre_earnings_state", 60) == b'{"cached": true}'


def test_refresh_ai_not_found_returns_graceful_200(client, monkeypatch):
    _clear_endpoint_cache()
    _enable_agent(monkeypatch)
    _patch_agent(monkeypatch, _StubAgent({"status": "not_found", "symbol": "NOPE"}))

    r = client.post("/api/pre-earnings/refresh-ai?symbol=NOPE")
    assert r.status_code == 200
    assert r.json()["status"] == "not_found"
