"""
tests/unit/test_ollama_budget_exempt.py
========================================
Regression for the Pre-Earnings "AI does not analyze" failure (2026-08):

The pre_earnings agent runs its cycle under a per-run ExternalCallBudget
(default 30). Data-ingestion calls (earnings calendar, yfinance quotes, market
caps) consume that budget; by the time `_apply_ollama` runs the LLM enrichment,
the budget is exhausted and `ollama.generate` (which goes through
`http_post` -> the shared budget gate) is blocked — so NO item ever receives an
`ai_decision` block and the AI the user pays for never runs.

Contract: Ollama AI calls are an intentional, separately-capped premium
feature (`ollama_max_tickers` already bounds the count). They MUST be exempt
from the per-run ingestion budget so data fetches can never starve the AI.
"""

import pytest

import core.http_utils as h
from core.request_context import ExternalCallBudget, request_context


class _FakeResp:
    def __init__(self):
        self.status_code = 200
        self.text = ""

    def json(self):
        return {}


@pytest.mark.unit
def test_ollama_call_passes_when_budget_exhausted(monkeypatch):
    """With the per-run budget fully consumed by data calls, an Ollama AI
    call must still go through (not be blocked)."""
    budget = ExternalCallBudget(limit=1)
    # Consume the only slot with a data-ingestion provider.
    assert budget.reserve("yahoo") is True

    called = {"n": 0}

    def fake_request(method, url, **kwargs):
        called["n"] += 1
        return _FakeResp()

    monkeypatch.setattr(h.requests, "request", fake_request)

    with request_context(reason="test", agent_name="pre_earnings", external_budget=budget):
        # A normal data call must be blocked (budget exhausted) -> http_get returns None.
        r_yahoo = h.http_get("https://finance.yahoo.com/q", provider="yahoo")
        assert r_yahoo is None, "data call should be blocked by exhausted budget"
        assert called["n"] == 0, "no network call should have happened for the blocked data call"

        # The Ollama AI call must be EXEMPT and reach the network.
        r_ollama = h.http_post(
            "https://ollama.com/api/generate", provider="ollama", json={"prompt": "x"}
        )
        assert r_ollama is not None, "ollama call must not be blocked by the ingestion budget"
        assert r_ollama.status_code == 200
        assert called["n"] == 1, "exactly one network call (the ollama one) should have fired"


@pytest.mark.unit
def test_non_ollama_call_still_budgeted(monkeypatch):
    """Sanity: the exemption is ollama-only. A non-ollama provider is still
    subject to the budget (no blanket hole)."""
    budget = ExternalCallBudget(limit=1)
    assert budget.reserve("yahoo") is True

    called = {"n": 0}

    def fake_request(method, url, **kwargs):
        called["n"] += 1
        return _FakeResp()

    monkeypatch.setattr(h.requests, "request", fake_request)

    with request_context(reason="test", agent_name="pre_earnings", external_budget=budget):
        # A second yahoo call must be blocked.
        assert h.http_get("https://finance.yahoo.com/q2", provider="yahoo") is None
        # A finnhub call must also be blocked (same exhausted budget, different provider).
        assert h.http_get("https://finnhub.io/api", provider="finnhub") is None
        assert called["n"] == 0