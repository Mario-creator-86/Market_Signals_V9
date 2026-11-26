"""The Pre-Market catalyst answer is asked once per question, not once per cycle.

Seen live on 2026-11-24, in pre-market hours: on every 5-minute cycle the agent
asked the model again for its top 10 tickers (bursts of 8-10 Claude calls,
about 100 an hour), whether or not a single headline had changed.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
from datetime import timedelta
from unittest.mock import patch

os.environ.setdefault("TEST_MODE", "1")
os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")

from agents import premarket_scanner_agent as ps  # noqa: E402
from agents.premarket_scanner_agent import PremarketScannerAgent  # noqa: E402

PRE_MARKET = {"data_kind": ps.DATA_KIND_PREMARKET, "now_utc": "2026-11-24T10:00:00+00:00"}
AFTER_HOURS = {"data_kind": ps.DATA_KIND_AFTER_HOURS, "now_utc": "2026-11-24T22:00:00+00:00"}


def _candidate(symbol="INSM", gap=14.2, titles=("Insmed to be acquired", "Analysts react to the deal")):
    return {"symbol": symbol, "gap_pct": gap, "premarket_score_long": 80, "premarket_score_short": 0,
            "news": [{"title": title} for title in titles],
            "earnings": {"eps_surprise_pct": None, "revenue_surprise_pct": None}}


def _agent_and_asks(monkeypatch, *, fail=False):
    """An agent whose model answers are counted: every entry of ``asks`` is one model call."""
    asks = []
    lock = threading.Lock()

    def _generate(prompt, model=None, system="", ui_call=False, timeout_override=None):
        with lock:
            asks.append(re.search(r"Symbol: (\S+)", prompt).group(1))
            number = len(asks)
        if fail:
            return {"error": "Ollama error: 500"}
        return {"response": json.dumps({"catalyst": "M&A", "summary": f"answer {number}", "confidence": 0.9}),
                "_meta": {"model": model}}

    monkeypatch.setattr(ps.ollama, "generate", _generate)
    with patch("agents.premarket_scanner_agent.ConfigManager"):
        agent = PremarketScannerAgent.__new__(PremarketScannerAgent)
    agent._lock = threading.RLock()
    agent._state = {"candidates": [], "status": {}}
    monkeypatch.setattr(agent, "_analyst_model", lambda: "claude-opus-5-5")
    return agent, asks


def _cycle(agent, candidates, session=PRE_MARKET, **settings):
    """One scanner cycle: candidates are rebuilt from scratch, enriched, and become the saved state."""
    rebuilt = [copy.deepcopy(candidate) for candidate in candidates]
    used = agent._apply_ollama(rebuilt, {**PremarketScannerAgent.DEFAULT_CONFIG, **settings}, session=session)
    agent._state = {"candidates": rebuilt, "status": {}}
    return used, rebuilt


def test_a_ticker_with_the_same_headlines_is_not_asked_again(monkeypatch):
    agent, asks = _agent_and_asks(monkeypatch)

    _cycle(agent, [_candidate(gap=14.2)])
    later = dict(PRE_MARKET, now_utc="2026-11-24T10:05:00+00:00")       # the clock moved, the question did not
    used, second = _cycle(agent, [_candidate(gap=14.4)], session=later)

    assert asks == ["INSM"]
    assert used is True, "a kept answer still counts as an AI-enriched cycle"
    assert second[0]["ollama"]["summary"] == "answer 1" and second[0]["ollama"]["catalyst"] == "M&A"
    assert "M&A" in second[0]["catalysts"]


def test_a_new_headline_is_asked_again(monkeypatch):
    agent, asks = _agent_and_asks(monkeypatch)

    _cycle(agent, [_candidate()])
    _, second = _cycle(agent, [_candidate(titles=("Regulator opens a review of the Insmed deal",))])

    assert asks == ["INSM", "INSM"]
    assert second[0]["ollama"]["summary"] == "answer 2"


def test_a_move_of_a_whole_percent_is_asked_again(monkeypatch):
    agent, asks = _agent_and_asks(monkeypatch)

    _cycle(agent, [_candidate(gap=14.2)])
    _cycle(agent, [_candidate(gap=17.9)])

    assert asks == ["INSM", "INSM"]


def test_another_session_is_asked_again(monkeypatch):
    """After-hours 'gap' is a different number than a pre-market gap; the prompt says so."""
    agent, asks = _agent_and_asks(monkeypatch)

    _cycle(agent, [_candidate()], session=PRE_MARKET)
    _cycle(agent, [_candidate()], session=AFTER_HOURS)

    assert asks == ["INSM", "INSM"]


def test_an_answer_older_than_the_reuse_window_is_asked_again(monkeypatch):
    agent, asks = _agent_and_asks(monkeypatch)

    _cycle(agent, [_candidate()])
    agent._state["candidates"][0]["ollama"]["answered_at"] = ps._iso(ps._utc_now() - timedelta(minutes=31))
    _cycle(agent, [_candidate()])
    assert asks == ["INSM", "INSM"]

    _cycle(agent, [_candidate()], ollama_reuse_minutes=0)                # 0 = ask on every cycle
    assert asks == ["INSM", "INSM", "INSM"]


def test_a_failed_answer_is_asked_again(monkeypatch):
    agent, asks = _agent_and_asks(monkeypatch, fail=True)

    used, first = _cycle(agent, [_candidate()])
    _cycle(agent, [_candidate()])

    assert used is False and "ollama" not in first[0]
    assert asks == ["INSM", "INSM"]


def test_only_the_changed_tickers_of_a_cycle_are_asked(monkeypatch):
    agent, asks = _agent_and_asks(monkeypatch)
    book = [_candidate("INSM"), _candidate("KYMR", gap=30.4, titles=("Kymera trial data",)),
            _candidate("SITM", gap=21.5, titles=("SiTime raises guidance",))]

    _cycle(agent, book)
    book[1] = _candidate("KYMR", gap=30.4, titles=("Kymera trial data", "Kymera prices an offering"))
    _, second = _cycle(agent, book)

    assert sorted(asks[:3]) == ["INSM", "KYMR", "SITM"] and asks[3:] == ["KYMR"]
    assert all(isinstance(candidate.get("ollama"), dict) for candidate in second)
