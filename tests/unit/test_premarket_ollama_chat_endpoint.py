"""
tests/unit/test_premarket_ollama_chat_endpoint.py
=================================================
Regression for the premarket AI catalyst classifier breaking on the CLOUD host
(2026-08): `_ollama_enrich_candidate` POSTed directly to `{OLLAMA_BASE_URL}/api/generate`,
but ollama.com's cloud API does NOT expose /api/generate (HTTP 404) — only /api/chat.
The same bug class already fixed for the Pre-Earnings agent's `ollama.generate`.

Contract for the fix:
- `_ollama_enrich_candidate` MUST go through `ollama.generate` (which uses /api/chat
  and normalizes `message.content` -> `response`), NOT `http_post` to /api/generate.
- A cloud-shaped chat response (text under `response`) with strict-JSON catalyst
  payload must yield `{"catalyst", "summary", "confidence"}`.
- A generation error (circuit open / HTTP error) must surface as `{"_error": ...}`,
  preserving the existing fail-fast behavior the caller relies on.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from agents import premarket_scanner_agent as ps
from agents.premarket_scanner_agent import PremarketScannerAgent


def _agent():
    with patch("agents.premarket_scanner_agent.ConfigManager"):
        agent = PremarketScannerAgent.__new__(PremarketScannerAgent)
        agent._lock = __import__("threading").RLock()
        agent._state = {"items": [], "status": {}}
        agent._last_run_time = 0.0
        return agent


def _candidate(symbol="AAPL", gap=4.2):
    return {
        "symbol": symbol,
        "gap_pct": gap,
        "news": [{"title": "raises full-year guidance"}, {"title": "analyst upgrade"}],
        "earnings": {"eps_surprise_pct": 12.5, "revenue_surprise_pct": 8.0},
    }


@pytest.mark.unit
def test_enrich_uses_ollama_generate_not_generate_endpoint(monkeypatch):
    """Must call ollama.generate (chat), never http_post to /api/generate."""
    agent = _agent()

    posted = {"n": 0, "url": None}

    def fake_http_post(url, *a, **kw):
        posted["n"] += 1
        posted["url"] = url
        raise AssertionError("must NOT POST to /api/generate directly; use ollama.generate")

    payload = {"catalyst": "GUIDANCE", "summary": "Raised full-year outlook.", "confidence": 0.82}

    def fake_generate(prompt, model=None, system="", ui_call=False, timeout_override=None):
        # ollama.generate normalizes chat `message.content` -> `response`.
        return {"response": json.dumps(payload), "model": model or "gpt-oss:120b",
                "done": True, "_meta": {"model": model or "gpt-oss:120b"}}

    # Install a sentinel even though production no longer imports http_post.
    # Any regression that reintroduces a direct call still fails this test.
    monkeypatch.setattr(ps, "http_post", fake_http_post, raising=False)
    monkeypatch.setattr(ps.ollama, "generate", fake_generate)

    out = agent._ollama_enrich_candidate(_candidate(), "gpt-oss:120b", 6)

    assert posted["n"] == 0, "must not POST to /api/generate; route through ollama.generate"
    assert isinstance(out, dict)
    assert out.get("catalyst") == "GUIDANCE"
    assert "Raised full-year outlook" in out.get("summary", "")
    assert out.get("confidence") == pytest.approx(0.82, abs=1e-3)


@pytest.mark.unit
def test_enrich_surfaces_generation_error_as_error_code(monkeypatch):
    """A cloud outage / circuit-open must map to {"_error": ...}, not raise."""
    agent = _agent()

    def fake_generate(prompt, model=None, system="", ui_call=False, timeout_override=None):
        return {"error": "Ollama error: 404"}

    monkeypatch.setattr(ps.ollama, "generate", fake_generate)
    out = agent._ollama_enrich_candidate(_candidate(), "gpt-oss:120b", 6)
    assert isinstance(out, dict)
    assert str(out.get("_error") or "").strip(), "generation error must surface as _error"


@pytest.mark.unit
def test_apply_ollama_enriches_concurrently_not_sequentially(monkeypatch):
    """REGRESSION (audit #12): per-ticker AI enrichment must run concurrently,
    not sequentially — a sequential loop of up to 10 tickers (6s timeout each)
    blocked the agent loop for ~60s. Verify max in-flight LLM calls > 1."""
    import json as _json
    import threading as _th
    import time as _time

    agent = _agent()
    cands = []
    for i in range(6):
        c = _candidate(f"T{i}")
        c["score_long"] = 10 + i
        c["score_short"] = 0
        cands.append(c)

    active = {"cur": 0, "max": 0}
    alock = _th.Lock()

    def fake_generate(prompt, model=None, system="", ui_call=False, timeout_override=None):
        with alock:
            active["cur"] += 1
            active["max"] = max(active["max"], active["cur"])
        _time.sleep(0.1)  # simulate LLM latency so concurrency is observable
        with alock:
            active["cur"] -= 1
        return {"response": _json.dumps({"catalyst": "X", "summary": "y", "confidence": 0.5}),
                "_meta": {"model": "m"}}

    monkeypatch.setattr(ps.ollama, "get_best_model", lambda *a, **k: "m")
    monkeypatch.setattr(ps.ollama, "generate", fake_generate)

    cfg = dict(PremarketScannerAgent.DEFAULT_CONFIG)
    cfg["ollama_max_tickers"] = 6
    cfg["ollama_workers"] = 4
    used = agent._apply_ollama(cands, cfg)

    assert used is True
    assert active["max"] > 1, "enrichment must be concurrent, not sequential"
    assert all(isinstance(c.get("ollama"), dict) for c in cands), "all candidates enriched"


@pytest.mark.unit
def test_apply_ollama_selects_by_premium_score_not_legacy_divergence(monkeypatch):
    """REGRESSION (audit O2): per-ticker AI enrichment must select targets by the
    premium composite score (the same metric the UI sorts by and the Pulse
    summarizes), NOT by the vestigial legacy int divergence. A high-premium
    ticker must take the enrichment slot even when its legacy divergence is low."""
    import json as _json

    agent = _agent()
    # AAA: high premium score, LOW legacy divergence -> must be enriched.
    a = _candidate("AAA")
    a["premarket_score_long"] = 90
    a["premarket_score_short"] = 10
    a["score_long"] = 3
    a["score_short"] = 2  # legacy divergence = 1 (low)
    # BBB: low premium score, HIGH legacy divergence -> must NOT take the slot.
    b = _candidate("BBB")
    b["premarket_score_long"] = 30
    b["premarket_score_short"] = 5
    b["score_long"] = 20
    b["score_short"] = 0  # legacy divergence = 20 (high)
    cands = [b, a]  # input order must not matter

    def fake_generate(prompt, model=None, system="", ui_call=False, timeout_override=None):
        return {"response": _json.dumps({"catalyst": "X", "summary": "y", "confidence": 0.5}),
                "_meta": {"model": "m"}}

    monkeypatch.setattr(ps.ollama, "get_best_model", lambda *a, **k: "m")
    monkeypatch.setattr(ps.ollama, "generate", fake_generate)

    cfg = dict(PremarketScannerAgent.DEFAULT_CONFIG)
    cfg["ollama_max_tickers"] = 1  # only the top-1 by the selection key is enriched
    agent._apply_ollama(cands, cfg)

    assert isinstance(a.get("ollama"), dict), "high-premium AAA must be enriched"
    assert b.get("ollama") is None, "low-premium BBB must not take the slot over AAA"
