"""Aggregate Pre-Earnings Pulse AI (Task PE-premium C3).

One cached LLM call over the top gap-risk items produces a session-level
pulse. Cloud-only, non-fatal (None on any error/parse failure/disabled),
TTL-cached on the agent instance (None is NOT served from cache — a failed
call retries next cycle). Mirror of test_premarket_pulse_ai.py.
"""

import sys
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents.pre_earnings_agent import PreEarningsAgent


def _items():
    return [
        {"symbol": "NVDA", "gap_risk_numeric": 88, "direction_label": "LONG",
         "direction_bias_score": 45, "session": "AMC",
         "top_news": [{"title": "NVDA guidance ahead"}]},
        {"symbol": "TSM", "gap_risk_numeric": 74, "direction_label": "SHORT",
         "direction_bias_score": -30, "session": "BMO",
         "top_news": [{"title": "TSM faces downgrades"}]},
    ]


def _agent():
    with patch("agents.pre_earnings_agent.ConfigManager"):
        agent = PreEarningsAgent.__new__(PreEarningsAgent)
        agent._lock = threading.RLock()
        return agent


def _cfg(**over):
    cfg = dict(PreEarningsAgent.DEFAULT_CONFIG)
    cfg.update(over)
    return cfg


VALID_JSON = (
    '{"summary":"Heavy mega-cap earnings slate; risk skews long on AI names.",'
    '"sentiment":"bullish",'
    '"top_long":[{"symbol":"NVDA","reason":"guidance momentum"}],'
    '"top_short":[{"symbol":"TSM","reason":"downgrade pressure"}],'
    '"catalysts":"NVDA/TSM earnings",'
    '"risks":"Thin summer liquidity around prints."}'
)


@pytest.mark.unit
def test_pulse_disabled_returns_none():
    agent = _agent()
    out = agent._compute_pulse(_cfg(pulse_enabled=False), _items())
    assert out is None


@pytest.mark.unit
def test_pulse_local_host_skipped():
    agent = _agent()
    with patch("agents.pre_earnings_agent.ollama.is_cloud_host", return_value=False):
        out = agent._compute_pulse(_cfg(), _items())
    assert out is None


@pytest.mark.unit
def test_pulse_empty_items_returns_none():
    agent = _agent()
    out = agent._compute_pulse(_cfg(), [])
    assert out is None


@pytest.mark.unit
def test_pulse_parses_valid_json():
    agent = _agent()
    with patch("agents.pre_earnings_agent.ollama.is_cloud_host", return_value=True), \
         patch("agents.pre_earnings_agent.ollama.get_best_model", return_value="m1"), \
         patch("agents.pre_earnings_agent.ollama.generate",
               return_value={"response": VALID_JSON, "_meta": {"model": "m1"}}):
        out = agent._compute_pulse(_cfg(), _items())
    assert out is not None
    assert out["sentiment"] == "bullish"
    assert out["top_long"][0]["symbol"] == "NVDA"
    assert out["top_short"][0]["symbol"] == "TSM"
    assert out["model"] == "m1"
    assert out["generated_at"]
    assert "summary" in out and "risks" in out and "catalysts" in out


@pytest.mark.unit
def test_pulse_prompt_excludes_news_confirmed_already_reported_items():
    agent = _agent()
    items = [
        {
            "symbol": "VCYT",
            "gap_risk_numeric": 99,
            "direction_label": "LONG",
            "direction_bias_score": 60,
            "session": "AMC",
            "flags": {"already_reported": True},
            "top_news": [{"title": "VCYT reported results"}],
        },
        {
            "symbol": "FORM",
            "gap_risk_numeric": 98,
            "direction_label": "LONG",
            "direction_bias_score": 55,
            "session": "AMC",
            "flags": {"already_reported": True},
            "top_news": [{"title": "FORM earnings call highlights"}],
        },
        {
            "symbol": "CNO",
            "gap_risk_numeric": 75,
            "direction_label": "LONG",
            "direction_bias_score": 35,
            "session": "BMO",
            "flags": {"already_reported": False},
            "top_news": [{"title": "CNO earnings preview"}],
        },
    ]
    captured = {}

    def generate(prompt, **kwargs):
        captured["prompt"] = prompt
        return {
            "response": (
                '{"summary":"CNO remains upcoming.","sentiment":"bullish",'
                '"top_long":[{"symbol":"CNO","reason":"Upcoming catalyst"}],'
                '"top_short":[],"catalysts":"CNO earnings","risks":"Volatility"}'
            ),
            "_meta": {"model": "m1"},
        }

    with patch("agents.pre_earnings_agent.ollama.is_cloud_host", return_value=True), \
         patch("agents.pre_earnings_agent.ollama.get_best_model", return_value="m1"), \
         patch("agents.pre_earnings_agent.ollama.generate", side_effect=generate):
        out = agent._compute_pulse(_cfg(), items)

    assert out["top_long"] == [{"symbol": "CNO", "reason": "Upcoming catalyst"}]
    assert "- CNO " in captured["prompt"]
    assert "VCYT" not in captured["prompt"]
    assert "FORM" not in captured["prompt"]


@pytest.mark.unit
def test_pulse_skips_llm_when_every_item_is_news_confirmed_reported():
    agent = _agent()
    items = _items()
    for item in items:
        item["flags"] = {"already_reported": True}

    with patch("agents.pre_earnings_agent.ollama.generate") as generate:
        assert agent._compute_pulse(_cfg(), items) is None
    generate.assert_not_called()


@pytest.mark.unit
def test_pulse_bad_json_returns_none():
    agent = _agent()
    with patch("agents.pre_earnings_agent.ollama.is_cloud_host", return_value=True), \
         patch("agents.pre_earnings_agent.ollama.get_best_model", return_value="m1"), \
         patch("agents.pre_earnings_agent.ollama.generate", return_value={"response": "not json at all"}):
        out = agent._compute_pulse(_cfg(), _items())
    assert out is None


@pytest.mark.unit
def test_pulse_generate_exception_non_fatal():
    agent = _agent()
    with patch("agents.pre_earnings_agent.ollama.is_cloud_host", return_value=True), \
         patch("agents.pre_earnings_agent.ollama.get_best_model", return_value="m1"), \
         patch("agents.pre_earnings_agent.ollama.generate", side_effect=RuntimeError("boom")):
        out = agent._compute_pulse(_cfg(), _items())
    assert out is None


@pytest.mark.unit
def test_pulse_error_dict_non_fatal():
    agent = _agent()
    with patch("agents.pre_earnings_agent.ollama.is_cloud_host", return_value=True), \
         patch("agents.pre_earnings_agent.ollama.get_best_model", return_value="m1"), \
         patch("agents.pre_earnings_agent.ollama.generate", return_value={"error": "timeout"}):
        out = agent._compute_pulse(_cfg(), _items())
    assert out is None


@pytest.mark.unit
def test_get_pulse_caches_success():
    agent = _agent()
    call_count = {"n": 0}

    def fake_generate(*a, **kw):
        call_count["n"] += 1
        return {"response": VALID_JSON, "_meta": {"model": "m1"}}

    with patch("agents.pre_earnings_agent.ollama.is_cloud_host", return_value=True), \
         patch("agents.pre_earnings_agent.ollama.get_best_model", return_value="m1"), \
         patch("agents.pre_earnings_agent.ollama.generate", side_effect=fake_generate):
        first = agent.get_pulse(_cfg(pulse_ttl_seconds=300), _items())
        second = agent.get_pulse(_cfg(pulse_ttl_seconds=300), _items())
    assert first == second
    assert call_count["n"] == 1


@pytest.mark.unit
def test_get_pulse_none_not_cached():
    agent = _agent()
    call_count = {"n": 0}

    def fake_generate(*a, **kw):
        call_count["n"] += 1
        return {"error": "timeout"}

    with patch("agents.pre_earnings_agent.ollama.is_cloud_host", return_value=True), \
         patch("agents.pre_earnings_agent.ollama.get_best_model", return_value="m1"), \
         patch("agents.pre_earnings_agent.ollama.generate", side_effect=fake_generate):
        first = agent.get_pulse(_cfg(pulse_ttl_seconds=300), _items())
        second = agent.get_pulse(_cfg(pulse_ttl_seconds=300), _items())
    assert first is None and second is None
    assert call_count["n"] == 2  # failure is NOT cached — retries next cycle


@pytest.mark.unit
def test_pulse_ttl_expiry_recomputes():
    agent = _agent()
    with patch("agents.pre_earnings_agent.ollama.is_cloud_host", return_value=True), \
         patch("agents.pre_earnings_agent.ollama.get_best_model", return_value="m1"), \
         patch("agents.pre_earnings_agent.ollama.generate",
               return_value={"response": VALID_JSON, "_meta": {"model": "m1"}}) as gen:
        first = agent.get_pulse(_cfg(pulse_ttl_seconds=300), _items())
        agent._pre_earnings_pulse_cache = (agent._pre_earnings_pulse_cache[0] - 9999,
                                           agent._pre_earnings_pulse_cache[1])
        second = agent.get_pulse(_cfg(pulse_ttl_seconds=300), _items())
    assert first is not None and second is not None
    assert gen.call_count == 2


@pytest.mark.unit
def test_get_state_normalizes_legacy_snapshot_premium_fields():
    agent = _agent()
    agent._state = {"items": [], "status": {}, "flags": {"news_available": True}}
    agent._state_read_fallback = {}
    agent._get_config = lambda: _cfg()
    agent._is_enabled = lambda: False
    agent._default_coverage_warmup_status = lambda: {"running": False}

    state = agent.get_state()

    assert state["pulse"] is None
    assert state["reported_today"] == []
    assert state["flags"]["pulse_used"] is False
    assert state["flags"]["reported_today_count"] == 0
    assert state["flags"]["news_available"] is True


# ── Audit P0-1: pulse picks must never contradict the row's final label ──────

def _label_items():
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(days=4)).isoformat()
    return [
        {"symbol": "ACMR", "gap_risk_numeric": 88, "direction_label": "LONG",
         "direction_bias_score": 43, "session": "AMC",
         "earnings_time_utc": future,
         "top_news": [{"title": "ACMR beats expectations"}]},
        {"symbol": "TSM", "gap_risk_numeric": 74, "direction_label": "SHORT",
         "direction_bias_score": -30, "session": "BMO",
         "earnings_time_utc": future,
         "top_news": [{"title": "TSM faces downgrades"}]},
        {"symbol": "QBTS", "gap_risk_numeric": 70, "direction_label": "NEUTRAL",
         "direction_bias_score": 0, "session": "BMO",
         "earnings_time_utc": future,
         "top_news": [{"title": "QBTS quantum update"}]},
    ]


CONTRADICT_JSON = (
    '{"summary":"Mixed earnings slate.","sentiment":"neutral",'
    '"top_long":[{"symbol":"TSM","reason":"oversold bounce"}],'
    '"top_short":[{"symbol":"ACMR","reason":"post-run fade"},'
    '{"symbol":"QBTS","reason":"hype unwind"}],'
    '"catalysts":"prints","risks":"thin liquidity"}'
)


@pytest.mark.unit
def test_pulse_drops_picks_contradicting_item_labels():
    """Mocked LLM picks ACMR short while the ACMR row is LONG +43 (live bug)
    and TSM long while the TSM row is SHORT -30. The served pulse must not
    contain either contradiction."""
    agent = _agent()
    with patch("agents.pre_earnings_agent.ollama.is_cloud_host", return_value=True), \
         patch("agents.pre_earnings_agent.ollama.get_best_model", return_value="m1"), \
         patch("agents.pre_earnings_agent.ollama.generate",
               return_value={"response": CONTRADICT_JSON, "_meta": {"model": "m1"}}):
        out = agent._compute_pulse(_cfg(), _label_items())
    assert out is not None
    long_symbols = [p["symbol"] for p in out["top_long"]]
    short_symbols = [p["symbol"] for p in out["top_short"]]
    assert "ACMR" not in short_symbols  # row is LONG +43
    assert "TSM" not in long_symbols    # row is SHORT -30
    # NEUTRAL row (|score| < 15) may still be picked — that is consistent.
    assert "QBTS" in short_symbols


@pytest.mark.unit
def test_pulse_prompt_anchors_label_and_honest_gap_state():
    """The prompt must carry each candidate's current label+score, and a
    gap-unavailable row must read gapN/A — never gap0 (0 reads as low risk)."""
    agent = _agent()
    items = _label_items()
    items[0]["gap_risk_numeric"] = None  # GapRisk unavailable
    captured = {}

    def generate(prompt, **kwargs):
        captured["prompt"] = prompt
        return {"response": CONTRADICT_JSON, "_meta": {"model": "m1"}}

    with patch("agents.pre_earnings_agent.ollama.is_cloud_host", return_value=True), \
         patch("agents.pre_earnings_agent.ollama.get_best_model", return_value="m1"), \
         patch("agents.pre_earnings_agent.ollama.generate", side_effect=generate):
        agent._compute_pulse(_cfg(), items)

    prompt = captured["prompt"]
    assert "TSM BMO gap74 SHORT-30" in prompt
    assert "ACMR AMC gapN/A LONG+43" in prompt


@pytest.mark.unit
def test_merge_pulse_snapshot_drops_contradictory_import():
    """The merge path applies the same guard: an imported snapshot cannot put
    a SHORT pick into the active union while the row label is LONG."""
    agent = _agent()
    agent._ensure_pulse_store_runtime()
    items = _label_items()
    pulse = {
        "summary": "Recovered snapshot.",
        "sentiment": "bearish",
        "top_long": [{"symbol": "ACMR", "reason": "momentum"}],
        "top_short": [{"symbol": "TSM", "reason": "downgrades"}],
        "catalysts": "prints",
        "risks": "liquidity",
        "model": "m1",
        "generated_at": None,
    }
    out = agent._merge_pulse_snapshot_locked(
        pulse, items, _cfg(), source="manual_import", persist=False,
    )
    assert out is not None
    long_symbols = [p["symbol"] for p in out["top_long"]]
    short_symbols = [p["symbol"] for p in out["top_short"]]
    # ACMR row is LONG → long pick stays; TSM row is SHORT → short pick stays.
    assert "ACMR" in long_symbols
    assert "TSM" in short_symbols

    flipped = dict(pulse)
    flipped["top_long"] = [{"symbol": "TSM", "reason": "contradicts row"}]
    out2 = agent._merge_pulse_snapshot_locked(
        flipped, items, _cfg(), source="manual_import", persist=False,
    )
    assert "TSM" not in [p["symbol"] for p in out2["top_long"]]


@pytest.mark.unit
def test_reconcile_archives_active_pick_when_label_flips_against_side():
    """An active short pick whose row has flipped to LONG leaves the active
    union (archived as direction_contradiction) instead of displaying a
    contradiction."""
    agent = _agent()
    agent._ensure_pulse_store_runtime()
    items = _label_items()
    future_utc = items[0]["earnings_time_utc"]
    event_date = future_utc[:10]
    agent._pulse_store["active"] = {
        "top_long": [],
        "top_short": [{
            "symbol": "ACMR",
            "reason": "earlier fade call",
            "event_key": f"ACMR|{event_date}",
            "event_date": event_date,
            "earnings_time_utc": future_utc,
            "session": "AMC",
            "first_seen_at": future_utc,
            "last_seen_at": future_utc,
            "source": "llm",
            "sources": ["llm"],
            "occurrences": 1,
        }],
    }
    changed = agent._reconcile_pulse_active_locked(items, _cfg())
    assert changed
    assert agent._pulse_store["active"]["top_short"] == []
    archive = agent._pulse_store.get("archive") or []
    assert any(
        p.get("symbol") == "ACMR" and p.get("archive_reason") == "direction_contradiction"
        for p in archive
    )


# ── Round-2 P1: a side-pick must be defensible from the row's structured lean ──

def _structured_lean_items():
    """Live re-audit fixtures: QBTS was picked top_short on a NEUTRAL row with
    structured +22; HIMS structured -4 is an honest short-lean (pickable)."""
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(days=4)).isoformat()
    return [
        {"symbol": "QBTS", "gap_risk_numeric": 70, "direction_label": "NEUTRAL",
         "direction_bias_score": 14, "structured_direction_bias_score": 22,
         "session": "BMO", "earnings_time_utc": future,
         "top_news": [{"title": "QBTS quantum update"}]},
        {"symbol": "HIMS", "gap_risk_numeric": 66, "direction_label": "NEUTRAL",
         "direction_bias_score": -3, "structured_direction_bias_score": -4,
         "session": "AMC", "earnings_time_utc": future,
         "top_news": [{"title": "HIMS guidance preview"}]},
        {"symbol": "ARKK", "gap_risk_numeric": 60, "direction_label": "NEUTRAL",
         "direction_bias_score": -6, "structured_direction_bias_score": -18,
         "session": "BMO", "earnings_time_utc": future,
         "top_news": [{"title": "ARKK flows update"}]},
    ]


STRUCTURED_LEAN_JSON = (
    '{"summary":"Mixed quantum slate.","sentiment":"neutral",'
    '"top_long":[{"symbol":"ARKK","reason":"oversold bounce"}],'
    '"top_short":[{"symbol":"QBTS","reason":"hype unwind"},'
    '{"symbol":"HIMS","reason":"guidance fade"}],'
    '"catalysts":"prints","risks":"thin liquidity"}'
)


@pytest.mark.unit
def test_pulse_drops_pick_against_neutral_rows_structured_lean():
    """Generation-time guard: a short-pick on a NEUTRAL row with structured
    +22 is indefensible and dropped; a NEUTRAL -4 row is an honest short-lean
    and stays; a long-pick against structured -18 is dropped too."""
    agent = _agent()
    with patch("agents.pre_earnings_agent.ollama.is_cloud_host", return_value=True), \
         patch("agents.pre_earnings_agent.ollama.get_best_model", return_value="m1"), \
         patch("agents.pre_earnings_agent.ollama.generate",
               return_value={"response": STRUCTURED_LEAN_JSON, "_meta": {"model": "m1"}}):
        out = agent._compute_pulse(_cfg(), _structured_lean_items())
    assert out is not None
    short_symbols = [p["symbol"] for p in out["top_short"]]
    long_symbols = [p["symbol"] for p in out["top_long"]]
    assert "QBTS" not in short_symbols, "structured +22 NEUTRAL row cannot be a top_short"
    assert "HIMS" in short_symbols, "structured -4 NEUTRAL row is an honest short-lean"
    assert "ARKK" not in long_symbols, "structured -18 NEUTRAL row cannot be a top_long"


@pytest.mark.unit
def test_merge_pulse_snapshot_drops_pick_against_structured_lean():
    """Merge-time guard: an imported/recovered snapshot cannot reactivate a
    short-pick whose current NEUTRAL row carries a bullish structured lean."""
    agent = _agent()
    agent._ensure_pulse_store_runtime()
    items = _structured_lean_items()
    pulse = {
        "summary": "Recovered snapshot.",
        "sentiment": "bearish",
        "top_long": [],
        "top_short": [
            {"symbol": "QBTS", "reason": "hype unwind"},
            {"symbol": "HIMS", "reason": "guidance fade"},
        ],
        "catalysts": "prints",
        "risks": "liquidity",
        "model": "m1",
        "generated_at": None,
    }
    out = agent._merge_pulse_snapshot_locked(
        pulse, items, _cfg(), source="manual_import", persist=False,
    )
    assert out is not None
    short_symbols = [p["symbol"] for p in out["top_short"]]
    assert "QBTS" not in short_symbols, "structured +22 NEUTRAL row cannot enter as top_short"
    assert "HIMS" in short_symbols, "structured -4 NEUTRAL row remains a valid short-lean"


@pytest.mark.unit
def test_pulse_prompt_anchors_structured_lean_rule_and_shows_divergence():
    """The prompt anchor states the round-2 eligibility rule (label OR
    structured lean), and a row whose structured score diverges from the
    blended score shows both so the model can honor the rule."""
    agent = _agent()
    captured = {}

    def generate(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["system"] = kwargs.get("system", "")
        return {"response": STRUCTURED_LEAN_JSON, "_meta": {"model": "m1"}}

    with patch("agents.pre_earnings_agent.ollama.is_cloud_host", return_value=True), \
         patch("agents.pre_earnings_agent.ollama.get_best_model", return_value="m1"), \
         patch("agents.pre_earnings_agent.ollama.generate", side_effect=generate):
        agent._compute_pulse(_cfg(), _structured_lean_items())

    prompt = captured["prompt"]
    # QBTS: blended NEUTRAL+14 but structured +22 — both shown.
    assert "QBTS BMO gap70 NEUTRAL+14 (structured+22)" in prompt
    # HIMS: structured -4 == blended -3? No (-4 != -3) -> shown as well.
    assert "HIMS AMC gap66 NEUTRAL-3 (structured-4)" in prompt
    anchor = captured["system"]
    assert "structured lean" in anchor
    assert ">= +15" in anchor and "<= -15" in anchor


@pytest.mark.unit
def test_pulse_structured_score_falls_back_to_final_and_missing_never_blocks():
    """Fallback semantics: items without structured_direction_bias_score use
    the final direction_bias_score; items without any finite score are only
    gated by the label rule (missing data never fabricates a block)."""
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(days=4)).isoformat()
    items = [
        # No structured field: final +20 is the fallback -> short pick drops.
        {"symbol": "FBCK", "gap_risk_numeric": 70, "direction_label": "NEUTRAL",
         "direction_bias_score": 20, "session": "BMO", "earnings_time_utc": future,
         "top_news": [{"title": "FBCK preview"}]},
        # No scores at all and NEUTRAL label: nothing opposes a short lean.
        {"symbol": "NOSC", "gap_risk_numeric": 65, "direction_label": "NEUTRAL",
         "session": "AMC", "earnings_time_utc": future,
         "top_news": [{"title": "NOSC preview"}]},
    ]
    response = (
        '{"summary":"s","sentiment":"neutral","top_long":[],'
        '"top_short":[{"symbol":"FBCK","reason":"r"},'
        '{"symbol":"NOSC","reason":"r"}],'
        '"catalysts":"c","risks":"r"}'
    )
    agent = _agent()
    with patch("agents.pre_earnings_agent.ollama.is_cloud_host", return_value=True), \
         patch("agents.pre_earnings_agent.ollama.get_best_model", return_value="m1"), \
         patch("agents.pre_earnings_agent.ollama.generate",
               return_value={"response": response, "_meta": {"model": "m1"}}):
        out = agent._compute_pulse(_cfg(), items)
    short_symbols = [p["symbol"] for p in out["top_short"]]
    assert "FBCK" not in short_symbols, "fallback final score +20 opposes a short pick"
    assert "NOSC" in short_symbols, "missing scores are non-blocking (label rule only)"
