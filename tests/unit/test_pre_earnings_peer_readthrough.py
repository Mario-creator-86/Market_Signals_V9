"""
tests/unit/test_pre_earnings_peer_readthrough.py
=================================================
Peer read-through & AI re-analysis (owner requirement, 2026-11):

"Yesterday's events should populate history grouped by sector and by day.
Build a database of information so the AI can analyze and decide from history.
EXAMPLE: if HUT and CIFR report below expectations and the decision was LONG,
then for MARA, CLSK and RIOT it must RE-ANALYZE!"

Pins:
1. peer_key chain: curated theme override wins (CIFR "Technology" and MARA
   "Financial Services" share BTC_MINERS), industry fallback, sector fallback,
   UNKNOWN -> None (a name never joins a fake "UNKNOWN" group).
2. peer_readthrough computation: bearish/mixed/bullish verdict bands at the
   pct boundaries, self excluded, 96h window respected, cap at 5 peers,
   honest null when no fresh settled peers exist.
3. Verdict staleness: a saved verdict older than the freshest settled peer
   evaluation is stale -> re-enters the normal per-cycle enrichment pool
   (refresh_reason="peer_readthrough"); a fresh verdict is reused; LLM-down
   keeps serving the previous verdict (staleness unlocks, never deletes).
4. Prompt: the peer line is present with a read-through and absent when null.
5. Settlement: peer_key/label persisted; legacy UNKNOWN prediction re-resolved
   through the theme->industry->sector chain.

All ollama calls are mocked — fully deterministic, no network.
"""

import copy
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents import pre_earnings_agent as pe
from agents.pre_earnings_agent import PreEarningsAgent, _iso, direction_label_from_score


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _light_agent():
    """Agent without __init__ (no disk I/O) for the pure grouping helpers."""
    with patch("agents.pre_earnings_agent.ConfigManager"):
        agent = PreEarningsAgent.__new__(PreEarningsAgent)
        agent._lock = threading.RLock()
        agent._analyst_ttl_cache = {}
        agent._feedback = {"open_predictions": {}, "outcomes": []}
        agent._ai_cache = {}
        return agent


def _full_agent():
    """Real agent (established test pattern) with an isolated feedback store."""
    agent = PreEarningsAgent()
    agent._feedback = {
        "version": 1,
        "updated_utc": "2026-07-15T00:00:00+00:00",
        "open_predictions": {},
        "outcomes": [],
        "weights": {},
        "last_recalibration_quarter": None,
        "calibration_model": {},
    }
    agent._ai_cache = {}
    return agent


def _outcome(symbol, move, hours_ago, verdict=None, sector=None, peer_key=None):
    now = datetime.now(timezone.utc)
    row = {
        "symbol": symbol,
        "event_utc": _iso(now - timedelta(hours=hours_ago + 36)),
        "evaluation_utc": _iso(now - timedelta(hours=hours_ago)),
        "realized_signed_move_pct": move,
        "predicted_ai_verdict": verdict,
    }
    if sector is not None:
        row["sector"] = sector
    if peer_key is not None:
        row["peer_key"] = peer_key
    return row


def _readthrough(peers_moves, verdict, hours=96):
    peers = []
    now = datetime.now(timezone.utc)
    for i, (sym, mv) in enumerate(peers_moves):
        peers.append({
            "symbol": sym,
            "event_utc": _iso(now - timedelta(hours=50 + i)),
            "evaluation_utc": _iso(now - timedelta(hours=10 + i)),
            "signed_move_pct": mv,
            "ai_verdict": "LONG",
        })
    positives = sum(1 for _, mv in peers_moves if mv > 0)
    return {
        "peers": peers,
        "count": len(peers_moves),
        "avg_signed_move_pct": round(sum(mv for _, mv in peers_moves) / max(1, len(peers_moves)), 4),
        "pct_positive": round(positives / max(1, len(peers_moves)), 4),
        "verdict": verdict,
        "latest_evaluation_utc": peers[0]["evaluation_utc"] if peers else None,
        "window_hours": hours,
    }


def _item(symbol="MARA", score=10, gap=70):
    return {
        "symbol": symbol,
        "earnings_time_utc": _iso(datetime.now(timezone.utc) + timedelta(days=2)),
        "gap_risk": gap,
        "direction_bias_score": score,
        "direction_label": direction_label_from_score(score),
        "top_news": [{"title": "reports next week", "source": "Reuters", "url": "x"}],
        "bias_reasons": ["structured reason"],
        "why": ["structured why"],
        "what_to_watch": ["gap direction"],
    }


def _ollama_response(payload: dict):
    return {
        "response": json.dumps(payload),
        "model": "gpt-oss:120b",
        "done": True,
        "_meta": {"model": "gpt-oss:120b"},
    }


def _patch_cloud(monkeypatch):
    monkeypatch.setattr(pe.ollama, "is_cloud_host", lambda: True)
    monkeypatch.setattr(pe.ollama, "get_best_model", lambda: "gpt-oss:120b")


_LONG_PAYLOAD = {
    "sentiment": -0.6,
    "confidence": 0.8,
    "decision": "SHORT",
    "decision_confidence": 0.75,
    "decision_rationale": "Peer miners gapped down hard after their reports.",
    "catalyst_label": "EARNINGS_VOLATILITY",
    "why_bullets": ["Peers sold off"],
    "what_to_watch": ["Peer guidance"],
}


# ---------------------------------------------------------------------------
# 1. peer_key / peer_key_label resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_peer_key_theme_override_wins_across_different_sectors():
    """The owner's example: CIFR is 'Technology', MARA is 'Financial Services'
    — a pure sector join would split them, the theme override keeps the BTC
    miner cluster together."""
    agent = _light_agent()
    assert agent.peer_key("CIFR", sector="Technology") == "BTC_MINERS"
    assert agent.peer_key("MARA", sector="Financial Services") == "BTC_MINERS"
    assert agent.peer_key("HUT") == agent.peer_key("RIOT") == "BTC_MINERS"
    assert agent.peer_key_label("CIFR", sector="Technology") == "BTC Miners"
    assert agent.peer_key_label("MARA", sector="Financial Services") == "BTC Miners"


@pytest.mark.unit
def test_peer_key_industry_fallback():
    agent = _light_agent()
    agent._analyst_ttl_cache = {
        "AAPL": (time.time(), {"industry": "Consumer Electronics", "sector": "Technology"}),
    }
    assert agent.peer_key("AAPL") == "CONSUMER_ELECTRONICS"
    assert agent.peer_key_label("AAPL") == "Consumer Electronics"


@pytest.mark.unit
def test_peer_key_sector_fallback_when_no_industry():
    agent = _light_agent()
    with patch.object(agent, "_get_analyst_estimates_cached", return_value=None):
        assert agent.peer_key("ZZZQ", sector="Financial Services") == "FINANCIAL_SERVICES"
        assert agent.peer_key_label("ZZZQ", sector="Financial Services") == "Financial Services"


@pytest.mark.unit
def test_peer_key_unknown_everywhere_returns_none():
    """A fully unresolved name groups with NOTHING — never with a synthetic
    'UNKNOWN' peer pool (honest null)."""
    agent = _light_agent()
    with patch.object(agent, "_get_analyst_estimates_cached", return_value=None):
        assert agent.peer_key("ZZZQ") is None
        assert agent.peer_key("ZZZQ", sector="UNKNOWN") is None
        assert agent.peer_key("ZZZQ", sector="") is None
        assert agent.peer_key_label("ZZZQ", sector="UNKNOWN") is None
        assert agent.peer_key("") is None


@pytest.mark.unit
def test_peer_theme_overrides_config_merges_over_defaults():
    agent = _light_agent()
    cfg = {"peer_theme_overrides": {"NVDA": "AI_HARDWARE_QUANTUM", "HUT": "CUSTOM_MINERS"}}
    assert agent.peer_key("NVDA", cfg=cfg) == "AI_HARDWARE_QUANTUM"
    assert agent.peer_key("NVDA", cfg=cfg) == agent.peer_key("SMCI", cfg=cfg)
    # Operator override wins over the curated default.
    assert agent.peer_key("HUT", cfg=cfg) == "CUSTOM_MINERS"


# ---------------------------------------------------------------------------
# 2. peer_readthrough computation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_readthrough_bearish_owner_example():
    """HUT -12.4% and CIFR -8.1% settled within 96h -> MARA sees a bearish
    read-through (0% positive), exactly the owner's scenario."""
    agent = _light_agent()
    agent._feedback["outcomes"] = [
        _outcome("HUT", -12.4, 24, verdict="LONG"),
        _outcome("CIFR", -8.1, 30, verdict="LONG"),
    ]
    key, label, rt = agent._build_peer_readthrough(
        "MARA", sector="Financial Services", industry=None, cfg={}
    )
    assert key == "BTC_MINERS"
    assert label == "BTC Miners"
    assert rt is not None
    assert rt["count"] == 2
    assert rt["pct_positive"] == 0.0
    assert rt["avg_signed_move_pct"] == pytest.approx(-10.25, abs=1e-3)
    assert rt["verdict"] == "bearish"
    assert [p["symbol"] for p in rt["peers"]] == ["HUT", "CIFR"]  # newest first
    assert rt["peers"][0]["ai_verdict"] == "LONG"
    assert rt["latest_evaluation_utc"] == rt["peers"][0]["evaluation_utc"]


@pytest.mark.unit
def test_readthrough_mixed_and_bullish_verdicts():
    agent = _light_agent()
    agent._feedback["outcomes"] = [
        _outcome("HUT", -12.4, 24),
        _outcome("CIFR", 8.1, 30),
    ]
    _, _, rt = agent._build_peer_readthrough("RIOT", sector="Financial Services", cfg={})
    assert rt["verdict"] == "mixed"  # 1/2 positive = 50%

    agent._feedback["outcomes"] = [
        _outcome("HUT", 12.4, 24),
        _outcome("CIFR", 8.1, 30),
    ]
    _, _, rt = agent._build_peer_readthrough("RIOT", sector="Financial Services", cfg={})
    assert rt["verdict"] == "bullish"  # 2/2 positive = 100%


@pytest.mark.unit
def test_readthrough_verdict_pct_boundaries():
    """1/3 positive (33%) reads bearish, 2/3 (67%) reads bullish, a single
    peer never tips the verdict (mixed)."""
    agent = _light_agent()
    agent._feedback["outcomes"] = [
        _outcome("HUT", 5.0, 10),
        _outcome("CIFR", -3.0, 20),
        _outcome("RIOT", -2.0, 30),
    ]
    _, _, rt = agent._build_peer_readthrough("MARA", sector="Financial Services", cfg={})
    assert rt["count"] == 3
    assert rt["pct_positive"] == pytest.approx(1 / 3, abs=1e-3)
    assert rt["verdict"] == "bearish"

    agent._feedback["outcomes"] = [
        _outcome("HUT", 5.0, 10),
        _outcome("CIFR", 3.0, 20),
        _outcome("RIOT", -2.0, 30),
    ]
    _, _, rt = agent._build_peer_readthrough("MARA", sector="Financial Services", cfg={})
    assert rt["pct_positive"] == pytest.approx(2 / 3, abs=1e-3)
    assert rt["verdict"] == "bullish"

    agent._feedback["outcomes"] = [_outcome("HUT", -9.0, 10)]
    _, _, rt = agent._build_peer_readthrough("MARA", sector="Financial Services", cfg={})
    assert rt["count"] == 1
    assert rt["verdict"] == "mixed"  # count < 2 -> no directional read


@pytest.mark.unit
def test_readthrough_excludes_self_and_other_groups():
    agent = _light_agent()
    agent._feedback["outcomes"] = [
        _outcome("MARA", -20.0, 10),                      # self — excluded
        _outcome("AAPL", 15.0, 10, sector="Technology"),  # different group
        _outcome("HUT", -5.0, 20),
    ]
    _, _, rt = agent._build_peer_readthrough("MARA", sector="Financial Services", cfg={})
    assert rt["count"] == 1
    assert rt["peers"][0]["symbol"] == "HUT"


@pytest.mark.unit
def test_readthrough_window_respected_and_configurable():
    agent = _light_agent()
    agent._feedback["outcomes"] = [
        _outcome("HUT", -5.0, 200),   # outside the default 96h window
        _outcome("CIFR", -7.0, 20),
    ]
    _, _, rt = agent._build_peer_readthrough("MARA", sector="Financial Services", cfg={})
    assert [p["symbol"] for p in rt["peers"]] == ["CIFR"]
    # Widening the window via config admits the older peer.
    _, _, rt = agent._build_peer_readthrough(
        "MARA", sector="Financial Services", cfg={"peer_readthrough_hours": 240}
    )
    assert rt["count"] == 2
    assert rt["window_hours"] == 240


@pytest.mark.unit
def test_readthrough_caps_at_five_newest_peers():
    agent = _light_agent()
    agent._feedback["outcomes"] = [
        _outcome(sym, -float(i + 1), 10 + i * 10)
        for i, sym in enumerate(["HUT", "CIFR", "RIOT", "CLSK", "WULF", "IREN", "BITF"])
    ]
    _, _, rt = agent._build_peer_readthrough("MARA", sector="Financial Services", cfg={})
    assert rt["count"] == 5
    kept = [p["symbol"] for p in rt["peers"]]
    assert kept == ["HUT", "CIFR", "RIOT", "CLSK", "WULF"]  # newest 5
    assert "BITF" not in kept and "IREN" not in kept


@pytest.mark.unit
def test_readthrough_null_when_no_fresh_peers_or_no_group():
    agent = _light_agent()
    agent._feedback["outcomes"] = [_outcome("AAPL", 5.0, 10, sector="Technology")]
    # No peer in MARA's group -> honest null (not an empty-object fake).
    key, label, rt = agent._build_peer_readthrough("MARA", sector="Financial Services", cfg={})
    assert key == "BTC_MINERS"
    assert rt is None
    # Name with no resolvable group -> key/label/readthrough all None.
    with patch.object(agent, "_get_analyst_estimates_cached", return_value=None):
        key, label, rt = agent._build_peer_readthrough("ZZZQ", sector="UNKNOWN", cfg={})
    assert key is None and label is None and rt is None


# ---------------------------------------------------------------------------
# 3. Verdict staleness -> re-analysis
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_staleness_helper_compares_peer_eval_vs_generated_at():
    agent = _light_agent()
    now = datetime.now(timezone.utc)
    item = _item("MARA")
    item["peer_readthrough"] = {
        "peers": [{"symbol": "HUT", "signed_move_pct": -12.4}],
        "latest_evaluation_utc": _iso(now - timedelta(hours=6)),
    }
    old_entry = {"ai_decision": {"used": True, "generated_at": _iso(now - timedelta(hours=20))}}
    new_entry = {"ai_decision": {"used": True, "generated_at": _iso(now - timedelta(hours=1))}}
    assert agent._ai_verdict_stale_by_peer_evidence(item, old_entry) is True
    assert agent._ai_verdict_stale_by_peer_evidence(item, new_entry) is False
    # Unprovable ordering -> honestly "not stale" (no per-cycle budget burn).
    assert agent._ai_verdict_stale_by_peer_evidence(
        item, {"ai_decision": {"used": True}}  # no generated_at
    ) is False
    assert agent._ai_verdict_stale_by_peer_evidence(_item("MARA"), old_entry) is False  # no readthrough
    assert agent._ai_verdict_stale_by_peer_evidence(item, None) is False


@pytest.mark.unit
def test_apply_ollama_reenriches_stale_verdict_with_refresh_reason(monkeypatch):
    """The owner's 'ar trebui reanalizat!': a LONG verdict predating the HUT/
    CIFR settlements must be re-analyzed under the normal quota and tagged."""
    _patch_cloud(monkeypatch)
    agent = _full_agent()
    cfg = agent._get_config()
    now = datetime.now(timezone.utc)
    item = _item("MARA", score=10, gap=80)
    item["peer_readthrough"] = _readthrough([("HUT", -12.4), ("CIFR", -8.1)], "bearish")
    item["ai_decision"] = {
        "used": True,
        "verdict": "LONG",
        "confidence": 0.7,
        "event_utc": item["earnings_time_utc"],
        "generated_at": _iso(now - timedelta(hours=30)),  # BEFORE the peer settlements
        "source": "fresh",
    }
    with patch.object(pe.ollama, "generate", return_value=_ollama_response(_LONG_PAYLOAD)):
        used = agent._apply_ollama([item], cfg)
    assert used
    assert item["ai_decision"]["verdict"] == "SHORT"  # re-analyzed
    assert item["ai_decision"]["refresh_reason"] == "peer_readthrough"
    assert item["ai_decision"]["source"] == "fresh"


@pytest.mark.unit
def test_apply_ollama_reuses_fresh_verdict_no_llm_call(monkeypatch):
    _patch_cloud(monkeypatch)
    agent = _full_agent()
    cfg = agent._get_config()
    now = datetime.now(timezone.utc)
    item = _item("MARA", score=10, gap=80)
    item["peer_readthrough"] = _readthrough([("HUT", -12.4), ("CIFR", -8.1)], "bearish")
    item["ai_decision"] = {
        "used": True,
        "verdict": "LONG",
        "confidence": 0.7,
        "event_utc": item["earnings_time_utc"],
        "generated_at": _iso(now + timedelta(hours=1)),  # AFTER the peer settlements
    }
    generate_calls = {"n": 0}
    monkeypatch.setattr(
        pe.ollama, "generate",
        lambda *a, **kw: generate_calls.__setitem__("n", generate_calls["n"] + 1),
    )
    used = agent._apply_ollama([item], cfg)
    assert used is False
    assert generate_calls["n"] == 0, "fresh verdict must be reused, not re-run"
    assert item["ai_decision"]["verdict"] == "LONG"
    assert "refresh_reason" not in item["ai_decision"]


@pytest.mark.unit
def test_apply_ollama_llm_down_keeps_stale_verdict(monkeypatch):
    """Staleness only UNLOCKS re-enrichment — when the LLM is unavailable the
    previous verdict keeps being served (never dropped to null)."""
    monkeypatch.setattr(pe.ollama, "is_cloud_host", lambda: False)
    agent = _full_agent()
    cfg = agent._get_config()
    now = datetime.now(timezone.utc)
    item = _item("MARA", score=10, gap=80)
    item["peer_readthrough"] = _readthrough([("HUT", -12.4), ("CIFR", -8.1)], "bearish")
    item["ai_decision"] = {
        "used": True,
        "verdict": "LONG",
        "confidence": 0.7,
        "generated_at": _iso(now - timedelta(hours=30)),
    }
    used = agent._apply_ollama([item], cfg)
    assert used is False
    assert item["ai_decision"]["verdict"] == "LONG"  # still served
    assert item["ai_decision"]["used"] is True


@pytest.mark.unit
def test_carry_forward_marks_stale_reuse_but_keeps_verdict():
    agent = _full_agent()
    cfg = agent._get_config()
    now = datetime.now(timezone.utc)
    previous = _item("MARA")
    previous["ai_decision"] = {
        "used": True,
        "verdict": "LONG",
        "confidence": 0.7,
        "event_utc": previous["earnings_time_utc"],
        "generated_at": _iso(now - timedelta(hours=30)),
    }
    rebuilt = _item("MARA")
    rebuilt["peer_readthrough"] = _readthrough([("HUT", -12.4), ("CIFR", -8.1)], "bearish")
    carried = agent._carry_forward_ai_decision(rebuilt, previous, cfg)
    assert carried is True
    assert rebuilt["ai_decision"]["verdict"] == "LONG"  # kept, not deleted
    assert rebuilt["ai_decision"]["stale_by_peer_evidence"] is True

    # Fresh verdict -> reused clean, no staleness marker.
    previous["ai_decision"]["generated_at"] = _iso(now + timedelta(hours=1))
    rebuilt2 = _item("MARA")
    rebuilt2["peer_readthrough"] = rebuilt["peer_readthrough"]
    assert agent._carry_forward_ai_decision(rebuilt2, previous, cfg) is True
    assert "stale_by_peer_evidence" not in rebuilt2["ai_decision"]


@pytest.mark.unit
def test_early_pass_reruns_when_only_stale_verdicts_exist(monkeypatch):
    """The early pass's 'already enriched' short-circuit must not fire when a
    verdict is stale by peer evidence — the name re-enters the shared quota."""
    _patch_cloud(monkeypatch)
    agent = _full_agent()
    now = datetime.now(timezone.utc)
    stale = _item("MARA", gap=80)
    stale["peer_readthrough"] = _readthrough([("HUT", -12.4), ("CIFR", -8.1)], "bearish")
    stale["ai_decision"] = {
        "used": True,
        "verdict": "LONG",
        "confidence": 0.7,
        "event_utc": stale["earnings_time_utc"],
        "generated_at": _iso(now - timedelta(hours=30)),
    }
    agent._state = {"items": [stale], "status": {}}
    monkeypatch.setattr(agent, "_persist_locked", lambda: None)
    with patch.object(pe.ollama, "generate", return_value=_ollama_response(_LONG_PAYLOAD)) as gen:
        agent._early_ai_pass(agent._get_config())
    assert gen.call_count >= 1, "stale verdict must be re-analyzed in the early pass"
    enriched = agent._state["items"][0]["ai_decision"]
    assert enriched["verdict"] == "SHORT"
    assert enriched["refresh_reason"] == "peer_readthrough"


@pytest.mark.unit
def test_early_pass_short_circuit_holds_for_fresh_verdicts(monkeypatch):
    _patch_cloud(monkeypatch)
    agent = _full_agent()
    now = datetime.now(timezone.utc)
    fresh = _item("MARA", gap=80)
    fresh["peer_readthrough"] = _readthrough([("HUT", -12.4), ("CIFR", -8.1)], "bearish")
    fresh["ai_decision"] = {
        "used": True,
        "verdict": "LONG",
        "confidence": 0.7,
        "generated_at": _iso(now + timedelta(hours=1)),
    }
    agent._state = {"items": [fresh], "status": {}}
    generate_calls = {"n": 0}
    monkeypatch.setattr(
        pe.ollama, "generate",
        lambda *a, **kw: generate_calls.__setitem__("n", generate_calls["n"] + 1),
    )
    agent._early_ai_pass(agent._get_config())
    assert generate_calls["n"] == 0


# ---------------------------------------------------------------------------
# 4. Prompt injection
# ---------------------------------------------------------------------------


def _capture_prompt(item):
    agent = _full_agent()
    captured = {}

    def fake_generate(prompt, model=None, system=None, timeout_override=None):
        captured["prompt"] = prompt
        captured["system"] = system
        return _ollama_response(_LONG_PAYLOAD)

    with patch.object(pe.ollama, "generate", fake_generate):
        agent._ollama_enrich_item(item, "gpt-oss:120b", 6)
    return captured


@pytest.mark.unit
def test_prompt_includes_peer_readthrough_line():
    item = _item("MARA")
    item["peer_readthrough"] = _readthrough([("HUT", -12.4), ("CIFR", -8.1)], "bearish")
    captured = _capture_prompt(item)
    prompt = captured["prompt"]
    assert "Peer read-through (peer reports in last 96h): HUT -12.4%, CIFR -8.1%" in prompt
    assert "(2 peers, 0% positive)" in prompt
    assert "bearish read-through; weigh this fresh evidence carefully" in prompt
    # System prompt carries the peer-evidence conviction clause.
    assert "freshest" in captured["system"]
    assert "read-through" in captured["system"]


@pytest.mark.unit
def test_prompt_omits_peer_line_when_no_readthrough():
    item = _item("MARA")  # no peer_readthrough key at all
    captured = _capture_prompt(item)
    assert "Peer read-through" not in captured["prompt"]

    item["peer_readthrough"] = None  # honest null -> still no line
    captured = _capture_prompt(item)
    assert "Peer read-through" not in captured["prompt"]


# ---------------------------------------------------------------------------
# 5. Registration + settlement persistence
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_register_open_predictions_persists_peer_key():
    agent = _full_agent()
    cfg = agent._get_config()
    item = _item("MARA")
    item["sector"] = "Financial Services"
    item["peer_key"] = "BTC_MINERS"
    item["peer_key_label"] = "BTC Miners"
    changed = agent._register_open_predictions([item], cfg)
    assert changed
    pred = next(v for v in agent._feedback["open_predictions"].values() if v["symbol"] == "MARA")
    assert pred["peer_key"] == "BTC_MINERS"
    assert pred["peer_key_label"] == "BTC Miners"

    legacy = _item("ZZZQ")
    legacy.pop("peer_key", None)
    legacy.pop("peer_key_label", None)
    agent._register_open_predictions([legacy], cfg)
    pred = next(v for v in agent._feedback["open_predictions"].values() if v["symbol"] == "ZZZQ")
    assert pred["peer_key"] == "UNKNOWN"
    assert pred["peer_key_label"] is None


@pytest.mark.unit
def test_settlement_persists_peer_key_and_resolves_legacy_unknown(monkeypatch):
    """Legacy prediction (sector-only, peer_key UNKNOWN) settles: the outcome
    re-resolves through the theme override so the history row joins the BTC
    miner cluster."""
    agent = _full_agent()
    cfg = agent._get_config()
    now = datetime.now(timezone.utc)
    event_utc = _iso(now - timedelta(hours=40))  # matured (>36h)
    agent._feedback["open_predictions"] = {
        f"CIFR|{event_utc}": {
            "symbol": "CIFR",
            "event_utc": event_utc,
            "session": "AMC",
            "sector": "Technology",      # legacy row — sector split from miners
            "cap_bucket": "MID",
            "predicted_gap_risk": 70,
            "predicted_direction_bias": 40,
            "predicted_prob_high_gap": 0.7,
            "contributors": {},
            "high_gap_threshold_pct": 5.0,
            "created_utc": _iso(now - timedelta(hours=60)),
            "updated_utc": _iso(now - timedelta(hours=60)),
        }
    }
    monkeypatch.setattr(agent, "_compute_event_gap_move_pct", lambda *a, **kw: -8.1)
    with patch.object(agent, "_get_analyst_estimates_cached", return_value=None):
        changed = agent._evaluate_matured_feedback_predictions(cfg)
    assert changed
    assert agent._feedback["open_predictions"] == {}
    outcome = agent._feedback["outcomes"][0]
    assert outcome["symbol"] == "CIFR"
    assert outcome["peer_key"] == "BTC_MINERS"          # theme re-resolved
    assert outcome["peer_key_label"] == "BTC Miners"
    assert outcome["realized_signed_move_pct"] == pytest.approx(-8.1, abs=1e-4)


@pytest.mark.unit
def test_settlement_reuses_prediction_peer_key_when_present(monkeypatch):
    agent = _full_agent()
    cfg = agent._get_config()
    now = datetime.now(timezone.utc)
    event_utc = _iso(now - timedelta(hours=40))
    agent._feedback["open_predictions"] = {
        f"MARA|{event_utc}": {
            "symbol": "MARA",
            "event_utc": event_utc,
            "session": "BMO",
            "sector": "Financial Services",
            "peer_key": "BTC_MINERS",
            "peer_key_label": "BTC Miners",
            "cap_bucket": "MID",
            "predicted_gap_risk": 70,
            "predicted_direction_bias": 40,
            "predicted_prob_high_gap": 0.7,
            "contributors": {},
            "high_gap_threshold_pct": 5.0,
            "created_utc": _iso(now - timedelta(hours=60)),
            "updated_utc": _iso(now - timedelta(hours=60)),
        }
    }
    monkeypatch.setattr(agent, "_compute_event_gap_move_pct", lambda *a, **kw: 6.3)
    agent._evaluate_matured_feedback_predictions(cfg)
    outcome = agent._feedback["outcomes"][0]
    assert outcome["peer_key"] == "BTC_MINERS"
    assert outcome["peer_key_label"] == "BTC Miners"


@pytest.mark.unit
def test_settlement_peer_key_unresolvable_stays_unknown(monkeypatch):
    """Honest UNKNOWN: a name with no theme/industry/sector keeps UNKNOWN on
    the outcome instead of being forced into a fake group."""
    agent = _full_agent()
    cfg = agent._get_config()
    now = datetime.now(timezone.utc)
    event_utc = _iso(now - timedelta(hours=40))
    agent._feedback["open_predictions"] = {
        f"ZZZQ|{event_utc}": {
            "symbol": "ZZZQ",
            "event_utc": event_utc,
            "session": "BMO",
            "sector": "UNKNOWN",
            "cap_bucket": "UNKNOWN",
            "predicted_gap_risk": 50,
            "predicted_direction_bias": 10,
            "predicted_prob_high_gap": 0.5,
            "contributors": {},
            "high_gap_threshold_pct": 5.0,
            "created_utc": _iso(now - timedelta(hours=60)),
            "updated_utc": _iso(now - timedelta(hours=60)),
        }
    }
    monkeypatch.setattr(agent, "_compute_event_gap_move_pct", lambda *a, **kw: 1.2)
    with patch.object(agent, "_get_analyst_estimates_cached", return_value=None):
        agent._evaluate_matured_feedback_predictions(cfg)
    outcome = agent._feedback["outcomes"][0]
    assert outcome["peer_key"] == "UNKNOWN"
    assert outcome["peer_key_label"] is None


# ---------------------------------------------------------------------------
# Early day-1 peer reactions (owner requirement, 2026-08-04)
#
# "HUT and CIFR reported ALREADY" — settled outcomes land ~36h after the
# report, but cluster peers (MARA/CLSK/RIOT) report TOMORROW. The read-through
# must therefore include day-1 reactions computed from live OHLC bars for
# peers that already reported but have not settled yet.
# ---------------------------------------------------------------------------


def _open_pred(symbol, event_utc, session="BMO", sector="Financial Services", verdict="LONG"):
    return {
        "symbol": symbol,
        "event_utc": event_utc,
        "session": session,
        "sector": sector,
        "cap_bucket": "UNKNOWN",
        "predicted_gap_risk": 50,
        "predicted_direction_bias": 40,
        "predicted_prob_high_gap": 0.5,
        "predicted_ai_verdict": verdict,
        "contributors": {},
        "high_gap_threshold_pct": 5.0,
        "created_utc": _iso(datetime.now(timezone.utc) - timedelta(days=3)),
        "updated_utc": _iso(datetime.now(timezone.utc) - timedelta(days=1)),
    }


@pytest.mark.unit
def test_early_peer_reaction_included_when_reported_not_settled(monkeypatch):
    """HUT reported 3h ago (BMO) and its gap is computable NOW -> MARA's
    read-through must include it as an early (day-1, not final) peer, even
    though the settled outcome only exists ~36h later."""
    agent = _light_agent()
    now = datetime.now(timezone.utc)
    event_utc = _iso(now - timedelta(hours=3))
    agent._feedback["open_predictions"] = {
        f"HUT|{event_utc}": _open_pred("HUT", event_utc, session="BMO"),
    }
    monkeypatch.setattr(agent, "_compute_event_gap_move_pct", lambda *a, **kw: -8.2)
    key, label, rt = agent._build_peer_readthrough("MARA", sector="Financial Services")
    assert key == "BTC_MINERS"
    assert rt is not None
    assert rt["count"] == 1
    peer = rt["peers"][0]
    assert peer["symbol"] == "HUT"
    assert peer["early"] is True
    assert peer["evaluation_utc"] is None
    assert peer["signed_move_pct"] == pytest.approx(-8.2)
    assert peer["ai_verdict"] == "LONG"
    assert rt["early_count"] == 1
    # single-peer evidence -> mixed (verdict bands need count>=2)
    assert rt["verdict"] == "mixed"


@pytest.mark.unit
def test_early_two_negative_peers_flip_cluster_bearish(monkeypatch):
    """The owner's exact scenario: HUT and CIFR both gapped down today ->
    MARA/CLSK/RIOT read-through is bearish BEFORE their reports tomorrow."""
    agent = _light_agent()
    now = datetime.now(timezone.utc)
    hut_ev = _iso(now - timedelta(hours=4))
    cifr_ev = _iso(now - timedelta(hours=3))
    agent._feedback["open_predictions"] = {
        f"HUT|{hut_ev}": _open_pred("HUT", hut_ev, session="BMO"),
        f"CIFR|{cifr_ev}": _open_pred("CIFR", cifr_ev, session="BMO", sector="Technology"),
    }
    moves = {"HUT": -8.2, "CIFR": -5.1}
    monkeypatch.setattr(
        agent,
        "_compute_event_gap_move_pct",
        lambda sym, *a, **kw: moves.get(sym),
    )
    for sym in ("MARA", "CLSK", "RIOT"):
        _, _, rt = agent._build_peer_readthrough(sym, sector="Financial Services")
        assert rt is not None, sym
        assert rt["count"] == 2
        assert rt["verdict"] == "bearish"
        assert rt["pct_positive"] == 0.0
        assert rt["avg_signed_move_pct"] == pytest.approx((-8.2 - 5.1) / 2)
        assert rt["early_count"] == 2


@pytest.mark.unit
def test_early_never_reads_through_to_yourself(monkeypatch):
    agent = _light_agent()
    now = datetime.now(timezone.utc)
    event_utc = _iso(now - timedelta(hours=2))
    agent._feedback["open_predictions"] = {
        f"HUT|{event_utc}": _open_pred("HUT", event_utc),
    }
    monkeypatch.setattr(agent, "_compute_event_gap_move_pct", lambda *a, **kw: -8.2)
    _, _, rt = agent._build_peer_readthrough("HUT", sector="Financial Services")
    assert rt is None


@pytest.mark.unit
def test_early_dedup_same_report_t00_and_t12_keys(monkeypatch):
    """Calendar refreshes registered CIFR twice for the same day (T00 AMC
    placeholder + refined T12 BMO) — the read-through counts the report ONCE,
    preferring the session-resolved (BMO) entry."""
    agent = _light_agent()
    now = datetime.now(timezone.utc)
    # Use a fully elapsed UTC day so the T12 refinement is never in the future
    # when this test runs around 00:00 UTC.
    day = (now - timedelta(days=1)).date().isoformat()
    agent._feedback["open_predictions"] = {
        f"CIFR|{day}T00:00:00+00:00": _open_pred(
            "CIFR", f"{day}T00:00:00+00:00", session="AMC", sector="Technology"
        ),
        f"CIFR|{day}T12:00:00+00:00": _open_pred(
            "CIFR", f"{day}T12:00:00+00:00", session="BMO", sector="Technology"
        ),
    }
    seen_sessions = []

    def _fake_gap(sym, event_dt, session, **kw):
        seen_sessions.append(session)
        return -5.1

    monkeypatch.setattr(agent, "_compute_event_gap_move_pct", _fake_gap)
    _, _, rt = agent._build_peer_readthrough("MARA", sector="Financial Services")
    assert rt is not None
    assert rt["count"] == 1
    assert rt["peers"][0]["symbol"] == "CIFR"
    assert seen_sessions == ["BMO"]


@pytest.mark.unit
def test_early_skipped_when_gap_not_computable(monkeypatch):
    """AMC reporter whose next-open bar does not exist yet (or budget
    exhausted) -> honest skip, no fake 0% peer."""
    agent = _light_agent()
    now = datetime.now(timezone.utc)
    event_utc = _iso(now - timedelta(hours=1))
    agent._feedback["open_predictions"] = {
        f"CIFR|{event_utc}": _open_pred("CIFR", event_utc, session="AMC", sector="Technology"),
    }
    monkeypatch.setattr(agent, "_compute_event_gap_move_pct", lambda *a, **kw: None)
    _, _, rt = agent._build_peer_readthrough("MARA", sector="Financial Services")
    assert rt is None


@pytest.mark.unit
def test_early_excludes_event_already_settled(monkeypatch):
    """The same report must not appear twice: once an outcome exists for
    (symbol, event day), the open prediction leaves the early layer."""
    agent = _light_agent()
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    agent._feedback["open_predictions"] = {
        f"HUT|{day}T12:00:00+00:00": _open_pred("HUT", f"{day}T12:00:00+00:00"),
    }
    agent._feedback["outcomes"] = [
        _outcome("HUT", -8.2, 40, sector="Financial Services", peer_key="BTC_MINERS"),
    ]
    # Force the settled outcome's event onto the same UTC day as the open pred.
    agent._feedback["outcomes"][0]["event_utc"] = f"{day}T12:00:00+00:00"
    monkeypatch.setattr(agent, "_compute_event_gap_move_pct", lambda *a, **kw: -8.2)
    _, _, rt = agent._build_peer_readthrough("MARA", sector="Financial Services")
    # The settled outcome is 40h old -> outside the 96h? no, inside: it IS the
    # settled layer (early=False), and the open pred adds nothing.
    assert rt is not None
    assert rt["count"] == 1
    assert rt["peers"][0]["early"] is False


@pytest.mark.unit
def test_early_sets_latest_evaluation_so_old_verdict_is_stale(monkeypatch):
    """Re-analysis rule with early evidence: a MARA verdict generated BEFORE
    HUT's report is stale (HUT's move did not exist yet); a verdict generated
    after is not re-burned every cycle (idempotent)."""
    agent = _light_agent()
    now = datetime.now(timezone.utc)
    event_dt = now - timedelta(hours=2)
    agent._feedback["open_predictions"] = {
        f"HUT|{_iso(event_dt)}": _open_pred("HUT", _iso(event_dt)),
    }
    monkeypatch.setattr(agent, "_compute_event_gap_move_pct", lambda *a, **kw: -8.2)
    _, _, rt = agent._build_peer_readthrough("MARA", sector="Financial Services")
    assert rt is not None
    assert rt["latest_evaluation_utc"] == _iso(event_dt)

    old_item = {"peer_readthrough": rt}
    stale_cache = {
        "ai_decision": {
            "used": True,
            "generated_at": _iso(event_dt - timedelta(hours=5)),
        }
    }
    fresh_cache = {
        "ai_decision": {
            "used": True,
            "generated_at": _iso(event_dt + timedelta(minutes=30)),
        }
    }
    assert agent._ai_verdict_stale_by_peer_evidence(old_item, stale_cache) is True
    assert agent._ai_verdict_stale_by_peer_evidence(old_item, fresh_cache) is False


@pytest.mark.unit
def test_early_combined_with_settled_in_verdict(monkeypatch):
    """Mixed evidence layers: 1 settled positive peer (3d ago) + 1 early
    negative peer (today) -> 1/2 positive -> mixed; both layers in peers."""
    agent = _light_agent()
    now = datetime.now(timezone.utc)
    event_utc = _iso(now - timedelta(hours=3))
    agent._feedback["outcomes"] = [
        _outcome("WULF", 4.0, 70, sector="Financial Services", peer_key="BTC_MINERS"),
    ]
    agent._feedback["open_predictions"] = {
        f"HUT|{event_utc}": _open_pred("HUT", event_utc),
    }
    monkeypatch.setattr(agent, "_compute_event_gap_move_pct", lambda *a, **kw: -8.2)
    _, _, rt = agent._build_peer_readthrough("MARA", sector="Financial Services")
    assert rt is not None
    assert rt["count"] == 2
    assert rt["early_count"] == 1
    assert rt["verdict"] == "mixed"
    # Newest first: the early peer (3h ago) outranks the settled one (70h).
    assert rt["peers"][0]["symbol"] == "HUT"
    assert rt["peers"][0]["early"] is True
    assert rt["peers"][1]["symbol"] == "WULF"
    assert rt["peers"][1]["early"] is False


@pytest.mark.unit
def test_prompt_peer_line_marks_early_reactions(monkeypatch):
    """The AI prompt must say which peer moves are day-1 (not final) so the
    model weighs freshness vs maturity correctly."""
    item = _item("MARA")
    rt = _readthrough([("HUT", -8.2), ("WULF", 4.0)], "mixed")
    rt["peers"][0]["early"] = True
    rt["peers"][0]["evaluation_utc"] = None
    rt["peers"][1]["early"] = False
    rt["early_count"] = 1
    item["peer_readthrough"] = rt
    captured = _capture_prompt(item)
    prompt = captured["prompt"]
    assert "Peer read-through (peer reports in last 96h):" in prompt
    assert "HUT -8.2% (day-1 reaction, not final)" in prompt
    assert "WULF +4.0%" in prompt
    assert "WULF +4.0% (day-1" not in prompt
