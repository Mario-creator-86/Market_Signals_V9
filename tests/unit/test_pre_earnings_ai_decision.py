"""
tests/unit/test_pre_earnings_ai_decision.py
============================================
Tests that the LLM is a first-class participant in the Pre-Earnings
long/short DECISION and the direction SCORE (punctaj) — mandatory per the
audit follow-up.

Pins:
1. `_ollama_enrich_item` parses an explicit `decision` (LONG/SHORT/NEUTRAL),
   `decision_confidence` (0..1) and `decision_rationale` from the LLM JSON.
2. `_apply_ollama` writes an `ai_decision` block onto each enriched item
   (verdict, confidence, sentiment, rationale, catalyst_label, model).
3. The AI is blended into `direction_bias_score` with a CONFIGURABLE weight
   (default 0.35) — AI is a real contributor but no longer the 0.6 override
   that could flip a data-driven call on headlines alone.
4. A confident AI verdict breaks a NEUTRAL tie (the explicit long/short
   decision participates, not just the scalar sentiment).
5. Graceful skip when no Ollama cloud key is configured (honest `used:False`,
   no fake AI signal).

All ollama calls are mocked — fully deterministic, no network.
"""

import json
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents import pre_earnings_agent as pe
from agents.pre_earnings_agent import PreEarningsAgent, direction_label_from_score


def _item(symbol="AAPL", score=10, gap=70):
    return {
        "symbol": symbol,
        "gap_risk": gap,
        "direction_bias_score": score,
        "direction_label": direction_label_from_score(score),
        "top_news": [{"title": "AAPL raises guidance", "source": "Reuters", "url": "x"}],
        "bias_reasons": ["structured reason"],
        "why": ["structured why"],
        "what_to_watch": ["gap direction"],
    }


def _ollama_response(payload: dict):
    """Shape returned by OllamaClient.generate on success."""
    return {
        "response": json.dumps(payload),
        "model": "gpt-oss:120b",
        "done": True,
        "_meta": {"model": "gpt-oss:120b"},
    }


def _hut_conflict_item():
    """HUT-style row: strong structured LONG, then a bearish LLM read."""
    item = _item(symbol="HUT", score=62, gap=85)
    item.update({
        "direction_bias": 0.62,
        "structured_direction_bias_score": 62,
        "structured_direction_label": "LONG",
        "final_direction_bias_score": 62,
        "coverage_score": 96,
        "opportunity_score": 74,
        "badges": ["HIGH_GAP_RISK", "LONG_BIAS"],
        "confidence": {
            "gap_risk": {"low": 80, "high": 90, "width": 10.0, "score": 0.75},
            "direction_bias": {"low": 48, "high": 76, "width": 28.0, "score": 0.65},
        },
        "confidence_interval": {
            "gap_risk": {"low": 80, "high": 90, "width": 10.0, "score": 0.75},
            "direction_bias": {"low": 48, "high": 76, "width": 28.0, "score": 0.65},
        },
        "bias_reasons": [
            "AI verdict SHORT (conf 0.75, sentiment -0.7) blended into direction at 35% weight.",
            "Composite bias score = 62 (range -100..100).",
            "Structured evidence remains available.",
        ],
    })
    return item


def _hut_short_payload():
    return {
        "sentiment": -0.70,
        "confidence": 0.80,
        "decision": "SHORT",
        "decision_confidence": 0.75,
        "decision_rationale": "Zoning delays create material execution risk.",
        "catalyst_label": "OTHER",
        "why_bullets": ["A large data-center project is delayed."],
        "what_to_watch": ["A rescheduled zoning vote."],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 1. _ollama_enrich_item parses the explicit decision fields
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_enrich_parses_explicit_decision_fields():
    agent = PreEarningsAgent()
    payload = {
        "why_bullets": ["Bullish analyst consensus"],
        "what_to_watch": ["Opening gap"],
        "catalyst_label": "GUIDANCE",
        "sentiment": 0.6,
        "confidence": 0.7,
        "decision": "LONG",
        "decision_confidence": 0.8,
        "decision_rationale": "Raised guidance + positive sentiment into earnings.",
    }
    with patch.object(pe.ollama, "generate", return_value=_ollama_response(payload)):
        enriched = agent._ollama_enrich_item(_item(), "gpt-oss:120b", 6)
    assert isinstance(enriched, dict)
    assert enriched["decision"] == "LONG"
    assert enriched["decision_confidence"] == pytest.approx(0.8)
    assert "Raised guidance" in enriched["decision_rationale"]
    assert enriched["sentiment"] == pytest.approx(0.6)


@pytest.mark.unit
def test_enrich_normalizes_invalid_decision_to_neutral():
    agent = PreEarningsAgent()
    payload = {
        "sentiment": 0.0,
        "confidence": 0.1,
        "decision": "MAYBE",  # not a valid verdict
        "decision_confidence": 0.1,
        "decision_rationale": "Unclear.",
    }
    with patch.object(pe.ollama, "generate", return_value=_ollama_response(payload)):
        enriched = agent._ollama_enrich_item(_item(), "gpt-oss:120b", 6)
    assert enriched["decision"] == "NEUTRAL"
    assert enriched["decision_confidence"] == pytest.approx(0.1)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. _apply_ollama writes the ai_decision block
# ═══════════════════════════════════════════════════════════════════════════════

def _patch_cloud(monkeypatch):
    monkeypatch.setattr(pe.ollama, "is_cloud_host", lambda: True)
    monkeypatch.setattr(pe.ollama, "get_best_model", lambda: "gpt-oss:120b")


@pytest.mark.unit
def test_apply_ollama_sets_ai_decision_block(monkeypatch):
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    payload = {
        "sentiment": 0.6,
        "confidence": 0.7,
        "decision": "LONG",
        "decision_confidence": 0.8,
        "decision_rationale": "Bullish setup.",
        "catalyst_label": "GUIDANCE",
        "why_bullets": ["w1"],
        "what_to_watch": ["watch1"],
    }
    item = _item(score=10)
    with patch.object(pe.ollama, "generate", return_value=_ollama_response(payload)):
        used = agent._apply_ollama([item], agent._get_config())
    assert used is True
    ai = item["ai_decision"]
    assert ai["verdict"] == "LONG"
    assert ai["confidence"] == pytest.approx(0.8)
    assert ai["sentiment"] == pytest.approx(0.6)
    assert ai["rationale"] == "Bullish setup."
    assert ai["catalyst_label"] == "GUIDANCE"
    assert ai["model"] == "gpt-oss:120b"
    assert ai["used"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# 3. AI blends into the score with a CONFIGURABLE weight (not the 0.6 override)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_apply_ollama_blends_with_configured_weight(monkeypatch):
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    cfg = agent._get_config()
    w = float(cfg["ollama_decision_weight"])
    assert 0.0 < w < 0.6, "AI weight must be a contributor, not an override"

    ai_sent = 0.9
    dec_conf = 0.9
    payload = {"sentiment": ai_sent, "confidence": 0.9, "decision": "LONG",
               "decision_confidence": dec_conf, "decision_rationale": "x"}
    item = _item(score=10)  # structured = 10
    with patch.object(pe.ollama, "generate", return_value=_ollama_response(payload)):
        agent._apply_ollama([item], cfg)

    structured = 10
    # AI component = sentiment*100 + verdict nudge (±10 * decision_confidence).
    ai_component = max(-100.0, min(100.0, ai_sent * 100.0 + 10.0 * dec_conf))
    expected = max(-100, min(100, round((1.0 - w) * structured + w * ai_component)))
    assert item["direction_bias_score"] == expected
    # The old behavior was 0.4*old + 0.6*(sent*100) → 0.4*10+0.6*90 = 58. The
    # new contributor-weight blend must differ from the old override.
    assert item["direction_bias_score"] != round(0.4 * structured + 0.6 * (ai_sent * 100))


@pytest.mark.unit
def test_apply_ollama_respects_env_weight_override(monkeypatch):
    _patch_cloud(monkeypatch)
    monkeypatch.setenv("PRE_EARNINGS_OLLAMA_DECISION_WEIGHT", "0.5")
    agent = PreEarningsAgent()
    cfg = agent._get_config()
    assert cfg["ollama_decision_weight"] == pytest.approx(0.5)

    ai_sent = 0.8
    dec_conf = 0.5
    payload = {"sentiment": ai_sent, "confidence": 0.5, "decision": "LONG",
               "decision_confidence": dec_conf, "decision_rationale": "x"}
    item = _item(score=20)
    with patch.object(pe.ollama, "generate", return_value=_ollama_response(payload)):
        agent._apply_ollama([item], cfg)
    ai_component = max(-100.0, min(100.0, ai_sent * 100.0 + 10.0 * dec_conf))  # 80 + 5 = 85
    expected = max(-100, min(100, round(0.5 * 20 + 0.5 * ai_component)))
    assert item["direction_bias_score"] == expected


@pytest.mark.unit
def test_hut_style_conflict_synchronizes_every_direction_derivative(monkeypatch):
    """Structured +62 and LLM -77.5 must produce one coherent +13/NEUTRAL row."""
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    cfg = agent._get_config()
    cfg["ollama_decision_weight"] = 0.35
    item = _hut_conflict_item()

    with patch.object(
        pe.ollama,
        "generate",
        return_value=_ollama_response(_hut_short_payload()),
    ):
        used = agent._apply_ollama([item], cfg)

    assert used is True
    assert item["structured_direction_bias_score"] == 62
    assert item["structured_direction_label"] == "LONG"
    assert item["llm_direction_score"] == -78
    assert item["ai_decision"]["direction_score"] == -78
    assert item["ai_decision"]["weight"] == pytest.approx(0.35)

    assert item["final_direction_bias_score"] == 13
    assert item["direction_bias_score"] == 13
    assert item["direction_bias"] == pytest.approx(0.13)
    assert item["direction_label"] == "NEUTRAL"
    assert item["decision_alignment"] == "CONFLICT"

    assert "HIGH_GAP_RISK" in item["badges"]
    assert "AI_CONFLICT" in item["badges"]
    assert "LONG_BIAS" not in item["badges"]
    assert "SHORT_BIAS" not in item["badges"]
    assert item["opportunity_score"] == agent._compute_opportunity_score(
        gap_risk=85,
        direction_bias=13,
        coverage_score=96,
    )

    for container_key in ("confidence", "confidence_interval"):
        direction_ci = item[container_key]["direction_bias"]
        assert direction_ci["width"] == pytest.approx(28.0)
        assert direction_ci["low"] == -1
        assert direction_ci["high"] == 27

    assert item["bias_reasons"][:3] == [
        "Structured direction = LONG +62.",
        "LLM direction = SHORT -78 (75% confidence, 35% blend weight).",
        "Final blended direction = NEUTRAL +13.",
    ]
    assert not any(reason.startswith("AI verdict ") for reason in item["bias_reasons"])


# ═══════════════════════════════════════════════════════════════════════════════
# 4. A confident AI verdict breaks a NEUTRAL tie
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_ai_verdict_breaks_neutral_tie_long(monkeypatch):
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    # Structured score 5 → NEUTRAL. Strong bullish AI should push to LONG.
    payload = {"sentiment": 0.6, "confidence": 0.8, "decision": "LONG",
               "decision_confidence": 0.85, "decision_rationale": "Bullish."}
    item = _item(score=5)
    assert item["direction_label"] == "NEUTRAL"
    with patch.object(pe.ollama, "generate", return_value=_ollama_response(payload)):
        agent._apply_ollama([item], agent._get_config())
    assert item["direction_label"] == "LONG"
    assert item["ai_decision"]["verdict"] == "LONG"


@pytest.mark.unit
def test_ai_verdict_breaks_neutral_tie_short(monkeypatch):
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    payload = {"sentiment": -0.6, "confidence": 0.8, "decision": "SHORT",
               "decision_confidence": 0.85, "decision_rationale": "Bearish."}
    item = _item(score=-5)
    assert item["direction_label"] == "NEUTRAL"
    with patch.object(pe.ollama, "generate", return_value=_ollama_response(payload)):
        agent._apply_ollama([item], agent._get_config())
    assert item["direction_label"] == "SHORT"
    assert item["ai_decision"]["verdict"] == "SHORT"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Graceful skip when no cloud key — honest, no fake AI signal
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_apply_ollama_skips_honestly_when_no_cloud(monkeypatch):
    monkeypatch.setattr(pe.ollama, "is_cloud_host", lambda: False)
    agent = PreEarningsAgent()
    item = _item(score=24)
    original_score = item["direction_bias_score"]
    with patch.object(pe.ollama, "generate", side_effect=AssertionError("must not call ollama without cloud")):
        used = agent._apply_ollama([item], agent._get_config())
    assert used is False
    # Structured score untouched, no fake ai_decision.
    assert item["direction_bias_score"] == original_score
    assert "ai_decision" not in item or item["ai_decision"].get("used") is False


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Persistence + accumulating coverage — AI verdicts are SAVED (never silently
#    re-run), coverage accumulates to all tickers over cycles, and transient
#    provider failures no longer cascade-exclude eligible tickers. Only an
#    explicit circuit-open hard-stops the batch. (User requirement: "daca face
#    analiza AI sa o salveze sa nu se reseteze la fiecare restart sau refresh.
#    Doar daca o solicita userul.")
# ═══════════════════════════════════════════════════════════════════════════════

def _enriched_ai(verdict="LONG", rationale="prior"):
    return {"used": True, "verdict": verdict, "confidence": 0.5,
            "sentiment": 0.3, "rationale": rationale, "catalyst_label": "OTHER",
            "model": "gpt-oss:120b"}


@pytest.mark.unit
def test_apply_ollama_skips_already_enriched_item(monkeypatch):
    """An item that already has ai_decision.used=True must NOT be re-run — its
    persisted verdict is preserved (no reset on refresh/restart)."""
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    item = _item(score=10)
    item["ai_decision"] = _enriched_ai(verdict="SHORT", rationale="prior")
    with patch.object(pe.ollama, "generate", side_effect=AssertionError("must not re-run an enriched item")) as gen:
        used = agent._apply_ollama([item], agent._get_config())
    assert gen.call_count == 0, "enriched item must be skipped, no LLM call"
    assert used is False
    # Existing verdict preserved verbatim — not overwritten.
    assert item["ai_decision"]["verdict"] == "SHORT"
    assert item["ai_decision"]["rationale"] == "prior"


@pytest.mark.unit
def test_apply_ollama_force_reruns_enriched_item(monkeypatch):
    """With force=True (the user-requested Refresh-AI path) an enriched item IS
    re-run and its verdict overwritten."""
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    item = _item(score=10)
    item["ai_decision"] = _enriched_ai(verdict="SHORT", rationale="prior")
    payload = {"sentiment": 0.7, "confidence": 0.8, "decision": "LONG",
               "decision_confidence": 0.9, "decision_rationale": "fresh take"}
    with patch.object(pe.ollama, "generate", return_value=_ollama_response(payload)) as gen:
        used = agent._apply_ollama([item], agent._get_config(), force=True)
    assert gen.call_count == 1, "force must re-run the enriched item"
    assert used is True
    assert item["ai_decision"]["verdict"] == "LONG"
    assert item["ai_decision"]["rationale"] == "fresh take"


@pytest.mark.unit
def test_apply_ollama_selects_unenriched_next_batch(monkeypatch):
    """Coverage accumulates: with 30 items where the top-25 (by gap_risk) are
    already enriched, the next cycle selects the 5 NOT-yet-enriched — not the
    enriched top-25. ollama.generate is called exactly 5 times."""
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    items = []
    for i in range(30, 0, -1):  # gap_risk 30..1
        it = _item(symbol=f"T{i}", score=10, gap=i)
        if i > 5:  # top-25 (gap 30..6) already enriched
            it["ai_decision"] = _enriched_ai(verdict="NEUTRAL", rationale="old")
        items.append(it)
    payload = {"sentiment": 0.5, "confidence": 0.7, "decision": "LONG",
               "decision_confidence": 0.7, "decision_rationale": "new"}
    with patch.object(pe.ollama, "generate", return_value=_ollama_response(payload)) as gen:
        used = agent._apply_ollama(items, agent._get_config())
    assert gen.call_count == 5, "only the 5 unenriched tickers get an LLM call"
    assert used is True
    fresh = [it for it in items if it.get("ai_decision", {}).get("rationale") == "new"]
    assert len(fresh) == 5
    assert {it["symbol"] for it in fresh} == {f"T{i}" for i in range(5, 0, -1)}
    # The 25 already-enriched keep their old verdict (not overwritten).
    old = [it for it in items if it.get("ai_decision", {}).get("rationale") == "old"]
    assert len(old) == 25


@pytest.mark.unit
def test_apply_ollama_prioritizes_imminent_stale_news_over_higher_score(monkeypatch):
    """A score penalty caused by pending AI refresh must not starve that refresh."""
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    now = pe._utc_now()
    stale = _item(symbol="HPE", score=49, gap=56)
    stale.update({
        "trader_score": 56,
        "opportunity_score": 60,
        "coverage_score": 99,
        "earnings_time_utc": pe._iso(now + timedelta(hours=10)),
        "session": "AMC",
        "event_status": "UPCOMING",
        "news_update_pending": True,
        "news_digest": {"fingerprint": "new", "article_count": 2, "unique_sources": 1},
        "top_news": [{
            "title": "New material HPE evidence",
            "source": "Reuters",
            "url": "https://example.com/hpe-new",
            "published_utc": pe._iso(now - timedelta(minutes=10)),
        }],
        "ai_decision": {
            **_enriched_ai(verdict="LONG", rationale="old"),
            "generated_at": pe._iso(now - timedelta(hours=2)),
            "news_fingerprint": "old",
            "news_evidence_ids": [],
        },
    })
    future = _item(symbol="FUTURE", score=70, gap=95)
    future.update({
        "trader_score": 99,
        "opportunity_score": 99,
        "coverage_score": 100,
        "earnings_time_utc": pe._iso(now + timedelta(days=10)),
        "session": "AMC",
        "event_status": "UPCOMING",
    })
    cfg = agent._get_config()
    cfg["ollama_max_tickers"] = 1
    cfg["news_change_reanalysis"] = True
    payload = {
        "sentiment": 0.7,
        "confidence": 0.8,
        "decision": "LONG",
        "decision_confidence": 0.8,
        "decision_rationale": "new HPE evidence reviewed",
    }

    with patch.object(pe.ollama, "generate", return_value=_ollama_response(payload)) as gen:
        used = agent._apply_ollama([future, stale], cfg)

    assert used is True
    assert gen.call_count == 1
    assert stale["ai_decision"]["rationale"] == "new HPE evidence reviewed"
    assert stale["ai_decision"]["refresh_reason"] == "news_changed"
    assert "ai_decision" not in future


@pytest.mark.unit
def test_apply_ollama_transient_failures_do_not_break_loop(monkeypatch):
    """Transient provider failures (timeout/error/parse) must NOT halt the batch
    — every eligible item is attempted. Here the first 8 of 10 fail transiently
    and the last 2 succeed: all 10 are attempted, 2 get AI."""
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    items = [_item(symbol=f"S{i}", score=10, gap=100 - i) for i in range(10)]
    success = _ollama_response({"sentiment": 0.5, "confidence": 0.7, "decision": "LONG",
                                "decision_confidence": 0.7, "decision_rationale": "ok"})

    def _gen(*a, **kw):
        idx = _gen._i
        _gen._i += 1
        if idx < 8:
            raise TimeoutError("simulated transient timeout")
        return success
    _gen._i = 0

    with patch.object(pe.ollama, "generate", side_effect=_gen) as gen:
        used = agent._apply_ollama(items, agent._get_config())
    assert gen.call_count == 10, "transient failures must not break the loop"
    assert used is True
    enriched = [it for it in items if it.get("ai_decision", {}).get("used") is True]
    assert len(enriched) == 2


@pytest.mark.unit
def test_apply_ollama_breaks_only_on_circuit_open(monkeypatch):
    """A provider circuit-open is the ONE hard stop: the batch halts immediately
    (one call) and nothing is marked used. This pins that the no-break-on-transient
    change must NOT remove the circuit breaker."""
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    items = [_item(symbol=f"S{i}", score=10, gap=100 - i) for i in range(10)]
    with patch.object(pe.ollama, "generate", return_value={"error": "circuit open"}) as gen:
        used = agent._apply_ollama(items, agent._get_config())
    assert gen.call_count == 1, "circuit-open must break immediately"
    assert used is False
    assert all(it.get("ai_decision", {}).get("used") is not True for it in items)


@pytest.mark.unit
def test_force_ai_for_symbol_reruns_enriched(monkeypatch):
    """The user 'Refresh AI' button (force_ai_for_symbol) must re-run even an
    already-enriched ticker and overwrite the verdict. Regression guard for the
    skip-if-enriched change: force_ai_for_symbol must pass force=True."""
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    old = _enriched_ai(verdict="LONG", rationale="old")
    agent._state = {"items": [_item("AAPL", score=10, gap=70)], "status": {}}
    agent._state["items"][0]["ai_decision"] = old
    monkeypatch.setattr(agent, "_persist_locked", lambda: None)
    with patch.object(pe.ollama, "generate",
                      return_value=_ollama_response({"sentiment": -0.6, "confidence": 0.8,
                                                     "decision": "SHORT", "decision_confidence": 0.9,
                                                     "decision_rationale": "fresh bearish"})):
        result = agent.force_ai_for_symbol("AAPL")
    assert result["status"] == "ok"
    assert result["ai_decision"]["verdict"] == "SHORT", "force must overwrite the old LONG"
    aapl = next(it for it in agent._state["items"] if it["symbol"] == "AAPL")
    assert aapl["ai_decision"]["verdict"] == "SHORT"


@pytest.mark.unit
def test_force_ai_refresh_is_idempotent_and_never_blends_a_blend(monkeypatch):
    """Two identical forced HUT analyses must both finish at +13, not drift to SHORT."""
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    cfg = agent._get_config()
    cfg["ollama_decision_weight"] = 0.35
    monkeypatch.setattr(agent, "_get_config", lambda: dict(cfg))
    monkeypatch.setattr(agent, "_persist_locked", lambda: None)
    agent._state = {"items": [_hut_conflict_item()], "status": {}}

    response = _ollama_response(_hut_short_payload())
    with patch.object(pe.ollama, "generate", return_value=response) as generate:
        first = agent.force_ai_for_symbol("HUT")
        first_item = next(item for item in agent._state["items"] if item["symbol"] == "HUT")
        first_snapshot = {
            "structured": first_item["structured_direction_bias_score"],
            "llm": first_item["llm_direction_score"],
            "final": first_item["final_direction_bias_score"],
            "normalized": first_item["direction_bias"],
            "label": first_item["direction_label"],
            "alignment": first_item["decision_alignment"],
            "badges": list(first_item["badges"]),
            "opportunity": first_item["opportunity_score"],
            "ci": dict(first_item["confidence"]["direction_bias"]),
            "reasons": list(first_item["bias_reasons"]),
        }

        second = agent.force_ai_for_symbol("HUT")
        second_item = next(item for item in agent._state["items"] if item["symbol"] == "HUT")
        second_snapshot = {
            "structured": second_item["structured_direction_bias_score"],
            "llm": second_item["llm_direction_score"],
            "final": second_item["final_direction_bias_score"],
            "normalized": second_item["direction_bias"],
            "label": second_item["direction_label"],
            "alignment": second_item["decision_alignment"],
            "badges": list(second_item["badges"]),
            "opportunity": second_item["opportunity_score"],
            "ci": dict(second_item["confidence"]["direction_bias"]),
            "reasons": list(second_item["bias_reasons"]),
        }

    assert generate.call_count == 2
    assert first["status"] == second["status"] == "ok"
    assert first_snapshot == second_snapshot
    assert second_snapshot["structured"] == 62
    assert second_snapshot["llm"] == -78
    assert second_snapshot["final"] == 13
    assert second_snapshot["normalized"] == pytest.approx(0.13)
    assert second_snapshot["label"] == "NEUTRAL"


# ═══════════════════════════════════════════════════════════════════════════════
# Already-reported tickers: AI is skipped in the normal cycle (no forward verdict
# on a completed earnings event) but still runs on explicit user force-refresh.
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_apply_ollama_skips_already_reported_item(monkeypatch):
    """A ticker the news says already reported must NOT get a forward AI verdict in
    the normal cycle — no LLM call, no ai_decision written."""
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    item = _item(symbol="CORT", score=40, gap=88)
    item["flags"] = {"already_reported": True}
    mock_gen = MagicMock(return_value=_ollama_response(
        {"sentiment": 0.6, "confidence": 0.7, "decision": "LONG",
         "decision_confidence": 0.8, "decision_rationale": "should not happen"}))
    with patch.object(pe.ollama, "generate", mock_gen):
        used = agent._apply_ollama([item], agent._get_config())
    assert mock_gen.call_count == 0
    assert "ai_decision" not in item or not item.get("ai_decision", {}).get("used")
    # used stays False — nothing enriched.
    assert used is False


@pytest.mark.unit
def test_apply_ollama_force_still_enriches_already_reported(monkeypatch):
    """Explicit user Refresh-AI on an already-reported ticker still runs the LLM so
    the user can get an on-demand confirmation (the honest prompt makes the model
    return NEUTRAL + 'already reported' rationale)."""
    _patch_cloud(monkeypatch)
    agent = PreEarningsAgent()
    item = _item(symbol="CORT", score=40, gap=88)
    item["flags"] = {"already_reported": True}
    payload = {"sentiment": 0.0, "confidence": 0.1, "decision": "NEUTRAL",
               "decision_confidence": 0.1,
               "decision_rationale": "Already reported; no longer qualifies."}
    mock_gen = MagicMock(return_value=_ollama_response(payload))
    with patch.object(pe.ollama, "generate", mock_gen):
        used = agent._apply_ollama([item], agent._get_config(), force=True)
    assert mock_gen.call_count == 1
    assert used is True
    assert item["ai_decision"]["verdict"] == "NEUTRAL"
    assert "already reported" in item["ai_decision"]["rationale"].lower()
