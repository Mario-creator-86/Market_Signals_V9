"""
tests/unit/test_pre_earnings_sector_resolution.py
==================================================
History sector resolution (owner requirement, 2026-08-04):

"The history has too many UNKNOWNs. For UNKNOWN the AI must search the news
and assign it to a sector. If there is no news, it stays UNKNOWN but must have
a sector-refresh button."

Pins:
1. Verified chain order: estimates payload sector wins; AI news classification
   only runs when estimates has nothing; "none" when both fail.
2. AI classification: GICS answer mapped onto the yfinance vocabulary
   (Financials -> Financial Services, Information Technology -> Technology),
   unparseable/UNKNOWN answers -> None, NO news -> None (honest, refresh
   button in UI), config kill-switch respected.
3. Persistence: a resolved sector is written onto every UNKNOWN row for the
   symbol (outcomes + open predictions), peer grouping re-derived, store
   persisted once.
4. Per-cycle backfill: small batch, newest first, estimates-only (never AI in
   a cycle), limit from config.
5. API: POST /api/pre-earnings/sector-refresh resolves + persists, and returns
   an honest null when nothing resolves.

All network/LLM calls mocked — fully deterministic.
"""

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents import pre_earnings_agent as pe
from agents.pre_earnings_agent import PreEarningsAgent, _iso


def _light_agent():
    with patch("agents.pre_earnings_agent.ConfigManager"):
        agent = PreEarningsAgent.__new__(PreEarningsAgent)
        agent._lock = threading.RLock()
        agent._analyst_ttl_cache = {}
        agent._feedback = {"open_predictions": {}, "outcomes": []}
        agent._ai_cache = {}
        agent._persist_feedback_store = lambda: None
        return agent


# ---------------------------------------------------------------------------
# 1. Chain order
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_estimates_sector_wins_and_ai_not_called(monkeypatch):
    agent = _light_agent()
    monkeypatch.setattr(agent, "_get_analyst_estimates_cached", lambda s: {"sector": "Healthcare"})
    monkeypatch.setattr(
        agent,
        "_classify_sector_from_news",
        lambda *a, **kw: pytest.fail("AI must not run when estimates resolved"),
    )
    sector, source = agent.resolve_history_sector("GENK", {})
    assert sector == "Healthcare"
    assert source == "estimates"


@pytest.mark.unit
def test_estimates_blank_and_unknown_fall_through_to_ai(monkeypatch):
    agent = _light_agent()
    monkeypatch.setattr(agent, "_get_analyst_estimates_cached", lambda s: {"sector": "UNKNOWN"})
    monkeypatch.setattr(agent, "_classify_sector_from_news", lambda s, cfg=None: "Technology")
    sector, source = agent.resolve_history_sector("ABCX", {})
    assert sector == "Technology"
    assert source == "ai_news"


@pytest.mark.unit
def test_nothing_resolves_returns_none(monkeypatch):
    agent = _light_agent()
    monkeypatch.setattr(agent, "_get_analyst_estimates_cached", lambda s: None)
    monkeypatch.setattr(agent, "_classify_sector_from_news", lambda s, cfg=None: None)
    sector, source = agent.resolve_history_sector("ZZZQ", {})
    assert sector is None
    assert source == "none"


# ---------------------------------------------------------------------------
# 2. AI classification from news
# ---------------------------------------------------------------------------


class _FakeNewsAgent:
    def __init__(self, articles):
        self._articles = articles

    def get_ticker_news(self, sym, max_items=8):
        return self._articles[:max_items]


def _patch_news(monkeypatch, articles):
    monkeypatch.setattr(pe, "get_news_tab_agent_if_ready", lambda: _FakeNewsAgent(articles))


@pytest.mark.unit
def test_ai_classifies_from_headlines_and_maps_vocabulary(monkeypatch):
    agent = _light_agent()
    _patch_news(monkeypatch, [{"title": "QuantumChip unveils new cloud AI processor", "summary": ""}])

    captured = {}

    def fake_generate(prompt, model=None, system=None, timeout_override=None):
        captured["prompt"] = prompt
        captured["system"] = system
        return {"response": "Information Technology"}

    monkeypatch.setattr(pe.ollama, "generate", fake_generate)
    sector = agent._classify_sector_from_news("QUBT", {"sector_ai_enabled": True})
    assert sector == "Technology"  # GICS -> yfinance vocabulary
    assert "QUBT" in captured["prompt"]
    assert "QuantumChip" in captured["prompt"]
    assert "UNKNOWN" in captured["system"]  # honest escape hatch in the prompt


@pytest.mark.unit
def test_ai_unsure_or_garbage_answer_returns_none(monkeypatch):
    agent = _light_agent()
    _patch_news(monkeypatch, [{"title": "Holding company files 8-K", "summary": ""}])
    monkeypatch.setattr(pe.ollama, "generate", lambda *a, **kw: {"response": "UNKNOWN"})
    assert agent._classify_sector_from_news("ZZZQ", {}) is None
    monkeypatch.setattr(pe.ollama, "generate", lambda *a, **kw: {"response": "I think maybe tech?"})
    assert agent._classify_sector_from_news("ZZZQ", {}) is None


@pytest.mark.unit
def test_ai_no_news_stays_unknown_without_llm_call(monkeypatch):
    """Owner rule: no news -> honestly UNKNOWN; the LLM must NOT be called."""
    agent = _light_agent()
    _patch_news(monkeypatch, [])
    monkeypatch.setattr(
        pe.ollama, "generate", lambda *a, **kw: pytest.fail("LLM must not run without headlines")
    )
    assert agent._classify_sector_from_news("ZZZQ", {}) is None


@pytest.mark.unit
def test_ai_classification_kill_switch(monkeypatch):
    agent = _light_agent()
    _patch_news(monkeypatch, [{"title": "Some headline", "summary": ""}])
    monkeypatch.setattr(
        pe.ollama, "generate", lambda *a, **kw: pytest.fail("LLM must not run when disabled")
    )
    assert agent._classify_sector_from_news("ZZZQ", {"sector_ai_enabled": False}) is None


# ---------------------------------------------------------------------------
# 3. Persistence
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_persist_sector_resolution_updates_outcomes_and_open_predictions(monkeypatch):
    agent = _light_agent()
    now = datetime.now(timezone.utc)
    agent._feedback = {
        "version": 1,
        "updated_utc": _iso(now),
        "open_predictions": {
            "GENK|2026-08-03T12:00:00+00:00": {
                "symbol": "GENK",
                "event_utc": "2026-08-03T12:00:00+00:00",
                "session": "BMO",
                "sector": "UNKNOWN",
                "peer_key": "UNKNOWN",
                "peer_key_label": None,
            }
        },
        "outcomes": [
            {"symbol": "GENK", "sector": "UNKNOWN", "peer_key": "UNKNOWN", "peer_key_label": None,
             "event_utc": _iso(now - timedelta(days=2)), "evaluation_utc": _iso(now - timedelta(hours=10))},
            {"symbol": "GENK", "sector": "Healthcare", "peer_key": "HEALTHCARE",
             "event_utc": _iso(now - timedelta(days=60)), "evaluation_utc": _iso(now - timedelta(days=58))},
            {"symbol": "OTHER", "sector": "UNKNOWN",
             "event_utc": _iso(now - timedelta(days=1)), "evaluation_utc": _iso(now - timedelta(hours=5))},
        ],
        "weights": {},
    }
    persisted = {"n": 0}
    agent._persist_feedback_store = lambda: persisted.__setitem__("n", persisted["n"] + 1)
    # industry resolution must not hit the network: theme-less symbol, so the
    # peer chain falls back to the freshly persisted sector.
    monkeypatch.setattr(agent, "_resolve_industry", lambda sym, _=None: None)

    touched = agent._persist_sector_resolution("GENK", "Consumer Cyclical", {})
    assert touched == 2  # one outcome + one open prediction; known/other rows untouched
    outcome = agent._feedback["outcomes"][0]
    assert outcome["sector"] == "Consumer Cyclical"
    assert outcome["peer_key"] == "CONSUMER_CYCLICAL"
    assert outcome["peer_key_label"] == "Consumer Cyclical"
    pred = agent._feedback["open_predictions"]["GENK|2026-08-03T12:00:00+00:00"]
    assert pred["sector"] == "Consumer Cyclical"
    assert agent._feedback["outcomes"][1]["sector"] == "Healthcare"  # untouched
    assert agent._feedback["outcomes"][2]["sector"] == "UNKNOWN"  # other symbol
    assert persisted["n"] == 1


@pytest.mark.unit
def test_persist_sector_resolution_theme_override_beats_sector(monkeypatch):
    """A BTC miner resolved through estimates keeps the curated theme group,
    not the raw sector group (CIFR Technology -> BTC Miners, not Technology)."""
    agent = _light_agent()
    now = datetime.now(timezone.utc)
    agent._feedback = {
        "version": 1,
        "updated_utc": _iso(now),
        "open_predictions": {},
        "outcomes": [
            {"symbol": "CIFR", "sector": "UNKNOWN", "peer_key": "UNKNOWN", "peer_key_label": None,
             "event_utc": _iso(now - timedelta(days=1)), "evaluation_utc": _iso(now - timedelta(hours=3))},
        ],
        "weights": {},
    }
    agent._persist_feedback_store = lambda: None
    touched = agent._persist_sector_resolution("CIFR", "Technology", {})
    assert touched == 1
    outcome = agent._feedback["outcomes"][0]
    assert outcome["sector"] == "Technology"
    assert outcome["peer_key"] == "BTC_MINERS"
    assert outcome["peer_key_label"] == "BTC Miners"


# ---------------------------------------------------------------------------
# 4. Per-cycle backfill
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_backfill_resolves_small_batch_newest_first_estimates_only(monkeypatch):
    agent = _light_agent()
    now = datetime.now(timezone.utc)
    outcomes = []
    for i in range(12):
        outcomes.append({
            "symbol": f"SYM{i:02d}",
            "sector": "UNKNOWN",
            "event_utc": _iso(now - timedelta(days=2)),
            "evaluation_utc": _iso(now - timedelta(hours=i)),  # SYM00 newest
        })
    agent._feedback = {"version": 1, "updated_utc": _iso(now),
                       "open_predictions": {}, "outcomes": outcomes, "weights": {}}
    agent._persist_feedback_store = lambda: None
    monkeypatch.setattr(agent, "_resolve_industry", lambda sym, _=None: None)

    ai_called = {"n": 0}
    monkeypatch.setattr(agent, "_classify_sector_from_news",
                        lambda *a, **kw: ai_called.__setitem__("n", ai_called["n"] + 1))
    monkeypatch.setattr(agent, "_get_analyst_estimates_cached",
                        lambda s: {"sector": "Industrials"})

    resolved = agent._backfill_history_sectors({"sector_backfill_per_cycle": 3})
    assert resolved == 3
    assert ai_called["n"] == 0, "cycle backfill must never burn LLM budget"
    done = [o["symbol"] for o in outcomes if o["sector"] == "Industrials"]
    assert done == ["SYM00", "SYM01", "SYM02"], "newest rows resolve first"


# ---------------------------------------------------------------------------
# 5. API endpoint
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sector_refresh_endpoint_resolves_and_persists():
    from fastapi.testclient import TestClient
    from api.main import app

    class _Agent:
        def _get_config(self):
            return {}

        def resolve_history_sector(self, symbol, cfg, allow_ai=True):
            assert symbol == "GENK" and allow_ai is True
            return "Consumer Cyclical", "estimates"

        def _persist_sector_resolution(self, symbol, sector, cfg):
            return 3

    with patch("agents.pre_earnings_agent.get_pre_earnings_agent", return_value=_Agent()), patch(
        "api.main._agent_enabled", return_value=True
    ):
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post("/api/pre-earnings/sector-refresh", json={"symbol": "genk"})
    assert r.status_code == 200
    data = r.json()
    assert data["sector"] == "Consumer Cyclical"
    assert data["source"] == "estimates"
    assert data["updated_rows"] == 3


@pytest.mark.unit
def test_sector_refresh_endpoint_honest_null_when_nothing_resolves():
    from fastapi.testclient import TestClient
    from api.main import app

    class _Agent:
        def _get_config(self):
            return {}

        def resolve_history_sector(self, symbol, cfg, allow_ai=True):
            return None, "none"

    with patch("agents.pre_earnings_agent.get_pre_earnings_agent", return_value=_Agent()), patch(
        "api.main._agent_enabled", return_value=True
    ):
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post("/api/pre-earnings/sector-refresh", json={"symbol": "ZZZQ"})
    assert r.status_code == 200
    data = r.json()
    assert data["sector"] is None
    assert data["source"] == "none"
    assert data["updated_rows"] == 0


@pytest.mark.unit
def test_sector_refresh_endpoint_rejects_malformed_symbol():
    from fastapi.testclient import TestClient
    import api.main as api_main

    with patch.object(api_main, "_agent_enabled", return_value=True):
        client = TestClient(api_main.app, raise_server_exceptions=False)
        response = client.post(
            "/api/pre-earnings/sector-refresh",
            json={"symbol": "BAD<script>"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid symbol"
