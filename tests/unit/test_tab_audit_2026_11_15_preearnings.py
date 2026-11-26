"""Pre-Earnings tab audit, 2026-11-15 - regression tests.

Each test pins one defect confirmed in the running app that day:

- the shortlist was declared FRESH because a single-symbol rescore bumped
  ``last_success_time`` while the whole-universe snapshot was hours old;
- "0 AI verified" / "32 AI verified" were frozen at the last full cycle and
  contradicted the rows actually served;
- 25 free prefilter verdicts used up the decision quota, so eligible setups
  further down the date-ordered list never received a decision;
- a session guessed from past reporting habit was stamped provider-confirmed;
- a +859% price-data error drove the Top 10 "avg realized move" to 24.7%,
  and a 54.0% hit rate was presented without the always-LONG baseline;
- a HIGH_GAP_RISK badge and a gap-risk band were published on rows whose
  GapRisk is shown as unavailable, and bands carried a hard-coded "68%" level;
- watchlist reporters (reporting tomorrow) waited behind an alphabetical walk
  of the whole earnings day in the continuous scanner and the toolbar warmups;
- "pre-earnings autonomous load failed: 'core.ai'" at every server start.
"""
import os
import subprocess
import sys
import textwrap
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents import pre_earnings_agent as pe
from agents.pre_earnings_agent import (
    PreEarningsAgent,
    guard_pre_earnings_recommendation_freshness,
)
from core.pre_earnings import continuous

ROOT = Path(__file__).resolve().parents[2]


def _bare_agent() -> PreEarningsAgent:
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._get_config = lambda: dict(PreEarningsAgent.DEFAULT_CONFIG)
    agent._lock = threading.RLock()
    agent._feedback = {}
    return agent


# --------------------------------------------------------------------- freshness

@pytest.mark.unit
def test_single_symbol_rescore_does_not_make_an_old_snapshot_fresh():
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    old = pe._iso(now - timedelta(hours=3))
    just_now = pe._iso(now - timedelta(seconds=20))
    payload = {
        "as_of_utc": old,
        "refresh_seconds": 600,
        "status": {
            "last_full_cycle_time": old,
            # bumped by a targeted rescore / coverage warmup
            "last_success_time": just_now,
            "last_targeted_rescore_time": just_now,
        },
        "top_predictions": [{"symbol": "OLD"}],
    }

    guarded = guard_pre_earnings_recommendation_freshness(payload, now=now)

    assert guarded["ranking"]["state"] == "STALE"
    assert guarded["ranking"]["snapshot_age_seconds"] == 3 * 3600
    assert guarded["top_predictions"] == []
    assert guarded["flags"]["recommendations_stale"] is True


@pytest.mark.unit
def test_legacy_snapshot_ignores_a_partial_update_stamp():
    """Snapshots written before last_full_cycle_time existed: a completion time
    equal to the last targeted-rescore / warmup stamp is a partial update."""
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    old = pe._iso(now - timedelta(hours=5))
    just_now = pe._iso(now - timedelta(seconds=30))
    for status in (
        {"last_success_time": just_now, "last_targeted_rescore_time": just_now},
        {"last_success_time": just_now, "coverage_warmup": {"last_finished": just_now}},
    ):
        payload = {"as_of_utc": old, "refresh_seconds": 600, "status": status,
                   "top_predictions": [{"symbol": "OLD"}]}
        guarded = guard_pre_earnings_recommendation_freshness(payload, now=now)
        assert guarded["ranking"]["state"] == "STALE", status
        assert guarded["top_predictions"] == []


@pytest.mark.unit
def test_completed_full_cycle_is_fresh_even_when_build_started_earlier():
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    payload = {
        "as_of_utc": pe._iso(now - timedelta(minutes=9)),
        "refresh_seconds": 600,
        "status": {
            "last_full_cycle_time": pe._iso(now - timedelta(seconds=40)),
            "last_success_time": pe._iso(now - timedelta(seconds=40)),
        },
        "top_predictions": [{"symbol": "NEW"}],
    }
    guarded = guard_pre_earnings_recommendation_freshness(payload, now=now)
    assert guarded["ranking"]["state"] == "FRESH"
    assert guarded["ranking"]["snapshot_age_seconds"] == 40
    assert guarded["top_predictions"] == [{"symbol": "NEW"}]


# ------------------------------------------------------------- ranking counters

def _row(symbol: str, now: datetime, *, ai: bool, eligible: bool) -> dict:
    row = {
        "symbol": symbol,
        "earnings_time_utc": pe._iso(now + timedelta(days=2)),
        "session": "AMC",
        "event_status": "UPCOMING",
        "recommendation_eligible": eligible,
        "news_count": 0,
        "top_news": [],
    }
    if ai:
        row["ai_decision"] = {"used": True, "verdict": "LONG", "confidence": 0.6}
    return row


@pytest.mark.unit
def test_cache_read_recounts_ai_and_eligible_rows_instead_of_freezing_them(monkeypatch):
    agent = _bare_agent()
    monkeypatch.setattr(agent, "_build_top_predictions", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(agent, "_direction_edge_summary", lambda: None, raising=False)
    now = datetime.now(timezone.utc)
    frozen = {
        "coverage_basis": "active_calendar_symbols",
        "universe_event_count": 670,
        "scored_count": 400,
        "screened_out_count": 2,
        "covered_count": 402,
        "eligible_count": 7,       # stale counters from the last full cycle
        "ai_reviewed_count": 32,
    }
    payload = {
        "as_of_utc": pe._iso(now),
        "status": {"last_full_cycle_time": pe._iso(now)},
        "ranking": dict(frozen),
        "items": [
            _row("AAA", now, ai=True, eligible=True),
            _row("BBB", now, ai=True, eligible=False),
            _row("CCC", now, ai=False, eligible=False),
        ],
    }
    agent._attach_daily_ranking(payload, dict(PreEarningsAgent.DEFAULT_CONFIG))
    ranking = payload["ranking"]
    # coverage of the calendar is still the last full cycle's exact figure ...
    assert (ranking["covered_count"], ranking["universe_event_count"]) == (402, 670)
    assert ranking["calendar_coverage_pct"] == 60.0
    # ... but the row-content counters describe the rows actually served.
    assert ranking["ai_reviewed_count"] == 2
    assert ranking["eligible_count"] == 1

    # After a restart every persisted row has reported: nothing is "AI verified".
    payload["items"] = []
    payload["ranking"] = dict(frozen)
    agent._attach_daily_ranking(payload, dict(PreEarningsAgent.DEFAULT_CONFIG))
    assert payload["ranking"]["ai_reviewed_count"] == 0
    assert payload["ranking"]["eligible_count"] == 0


# -------------------------------------------------------------- decision quota

@pytest.mark.unit
def test_free_prefilter_verdicts_do_not_consume_the_decision_quota(monkeypatch):
    from core.ai.decision import Action, Decision
    from core.ai.decision_engine import EvaluationResult
    from core.ai.pipeline import ValidationResult

    evaluated = []

    def _fake_eval(item, *, config, services=None):
        evaluated.append(item["symbol"])
        dec = Decision(action=Action.WATCH, ticker=item["symbol"])
        return EvaluationResult(
            dec, ValidationResult(ok=True, decision=dec, stage="RISK_GATE"), None,
            {"available": True, "recommendation": "watch", "confidence": 0.5,
             "reason": "x", "validation_stage": "", "critic_veto": False}, False)

    monkeypatch.setattr(pe, "evaluate_pre_earnings_decision", _fake_eval)
    agent = object.__new__(PreEarningsAgent)
    blocked = [
        {"symbol": f"B{i}", "recommendation_eligible": False,
         "recommendation_blockers": ["event_date_low_confidence"]}
        for i in range(30)
    ]
    agent._state = {"items": blocked + [{"symbol": "GOOD1"}, {"symbol": "GOOD2"}, {"symbol": "GOOD3"}]}

    agent._attach_pre_earnings_decisions(
        {"pre_earnings": {"decision_enabled": True, "decision_max_tickers": 2},
         "autonomy": {"mode": "PAPER_AUTO"}})

    # the two eligible rows after 30 blocked ones are evaluated (quota = 2) ...
    assert evaluated == ["GOOD1", "GOOD2"]
    # ... and every blocked row still carries its (free) prefilter verdict.
    assert all(
        row["pre_earnings_decision"]["validation_stage"] == "PRE_EARNINGS_PREFILTER"
        for row in agent._state["items"][:30]
    )
    assert "pre_earnings_decision" not in agent._state["items"][-1]


# --------------------------------------------------------------- session label

@pytest.mark.unit
def test_session_inferred_from_past_habit_is_not_labelled_provider(monkeypatch):
    import pandas as pd
    import yfinance as yf

    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    upcoming = (datetime.now(timezone.utc) + timedelta(days=2)).replace(
        hour=0, minute=0, second=0, microsecond=0)   # provider gave a date, no time
    past = [datetime(2026, m, 20, 16, 0, tzinfo=timezone.utc) for m in (8, 5, 2)]
    past.append(datetime(2025, 11, 20, 16, 0, tzinfo=timezone.utc))
    frame = pd.DataFrame(
        {"EPS Estimate": [1.0] * 5, "Reported EPS": [None, 1.0, 1.0, 1.0, 1.0]},
        index=pd.DatetimeIndex([upcoming] + past),
    )

    class MockTicker:
        def __init__(self, sym):
            self.sym = sym

        def get_earnings_dates(self, limit=8):
            return frame

        @property
        def calendar(self):
            return {"Earnings Date": [upcoming.date()]}

    monkeypatch.setattr(yf, "Ticker", MockTicker)
    rows = agent._resolve_yf_earnings_rows(["HABIT"], lookahead_days=5, force_refresh=True)

    assert len(rows) == 1
    row = rows[0]
    assert row["session"] == "AMC"                    # the guess is still shown
    assert row["session_confidence"] == "inferred"    # ... but as a guess
    assert row["confirmed"] is False
    assert row["date_confidence"] == "medium"
    # and a guessed session cannot pass the verified-session gate
    blockers = agent._event_gate_blockers(
        {
            "symbol": "HABIT",
            "earnings_time_utc": row["date"],
            "session": row["session"],
            "event_status": "UPCOMING",
            "event_source": "yfinance",
            "event_source_url": row["source_url"],
            "event_verified_at": row["verified_at"],
            "event_date_confidence": row["date_confidence"],
            "event_session_confidence": row["session_confidence"],
        },
        require_verified_provenance=True,
    )
    assert "verified_event_session_confidence_required" in blockers


# ---------------------------------------------------------------- track record

def _outcome(symbol, bias, move, day, *, top=False):
    row = {
        "symbol": symbol,
        "event_utc": f"2026-09-{day:02d}T00:00:00+00:00",
        "predicted_direction_bias": bias,
        "realized_signed_move_pct": move,
    }
    if top:
        row.update({"was_top_prediction": True,
                    "top_prediction_direction": "LONG" if bias > 0 else "SHORT",
                    "top_prediction_bias": bias})
    return row


@pytest.mark.unit
def test_track_record_excludes_implausible_moves_and_reports_the_baseline():
    agent = _bare_agent()
    outcomes = [
        _outcome("UP1", 40, 5.0, 1, top=True),      # LONG hit
        _outcome("UP2", 30, 3.0, 2),                # LONG hit
        _outcome("UP3", -30, 4.0, 3),               # SHORT miss (stock went up)
        _outcome("DN1", 25, -2.0, 4),               # LONG miss
        _outcome("CLGN", -23, 859.4203, 5, top=True),   # price-data error
    ]
    metrics = agent._compute_track_record_metrics({}, outcomes=outcomes)

    assert metrics["sample_size"] == 5
    assert metrics["excluded_implausible_moves"] == 1
    assert metrics["max_plausible_move_pct"] == 100.0
    overall = metrics["overall"]
    assert (overall["total"], overall["hits"]) == (4, 2)
    assert overall["hit_rate"] == 50.0
    assert overall["avg_realized_move"] == 3.5          # not dragged up by 859%
    # 3 of the 4 scored events simply moved up: always-LONG beats the calls.
    assert overall["always_long_hit_rate"] == 75.0
    assert overall["edge_vs_always_long_pct"] == -25.0
    top = metrics["top_predictions"]
    assert top["settled"] == 2 and top["total"] == 1 and top["unscored"] == 1
    assert top["avg_realized_move"] == 5.0
    assert top["per_side"]["SHORT"]["total"] == 0
    assert metrics["first_event_utc"].startswith("2026-09-01")
    assert metrics["last_event_utc"].startswith("2026-09-05")


@pytest.mark.unit
def test_track_record_caveat_never_calls_a_sub_baseline_hit_rate_an_edge():
    caveat = PreEarningsAgent._track_record_baseline_caveat(
        {"overall": {"total": 2169, "hit_rate": 54.0, "always_long_hit_rate": 56.2}})
    assert caveat.startswith("Not an edge")
    assert "54.0%" in caveat and "56.2%" in caveat and "2169" in caveat

    above = PreEarningsAgent._track_record_baseline_caveat(
        {"overall": {"total": 100, "hit_rate": 61.0, "always_long_hit_rate": 55.0}})
    assert above.startswith("In-sample only")
    assert PreEarningsAgent._track_record_baseline_caveat({"overall": {"total": 0}}) is None


@pytest.mark.unit
def test_track_record_ai_prompt_carries_the_baseline(monkeypatch):
    agent = _bare_agent()
    seen = {}

    def _generate(prompt, model=None, system=None, timeout_override=None):
        seen["prompt"], seen["system"] = prompt, system
        return {"response": '{"summary": "s", "recommendation": "r"}', "_meta": {"model": "m"}}

    monkeypatch.setattr(pe.ollama, "is_cloud_host", lambda: True)
    monkeypatch.setattr(pe.ollama, "get_best_model", lambda: "m")
    monkeypatch.setattr(pe.ollama, "generate", _generate)
    metrics = {
        "sample_size": 10, "ai_coverage": 0,
        "overall": {"total": 10, "hits": 5, "hit_rate": 50.0, "avg_realized_move": 3.0,
                    "always_long_hit_rate": 60.0, "edge_vs_always_long_pct": -10.0},
        "per_verdict": {}, "per_band": [],
    }
    summary = agent._compute_track_record_ai_summary({"use_ollama": True}, metrics)
    assert "always LONG" in seen["prompt"] and "60.0%" in seen["prompt"]
    assert "always-LONG baseline" in seen["system"]
    assert summary["caveat"].startswith("Not an edge")


# ------------------------------------------------- gap-risk honesty on thin rows

def _build_agent():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._resolve_sector = lambda _symbol, _row: None
    agent._resolve_calibration_factor = lambda *_args: (1.0, 0)
    agent._get_track_record_cached = lambda: {"per_symbol": {}, "overall": {}}
    agent._external_budget_exhausted = lambda: False
    agent._reserve_external_call = lambda _provider: False
    agent._get_cached_quote = lambda _symbol: {}
    agent._get_analyst_estimates_cached = lambda _symbol: None
    agent._compute_historical_earnings_gap_stats = lambda _symbol: {
        "median_move_pct": None, "sample_size": 0, "moves": [], "source": "provider_miss"}
    agent._estimate_expected_move_pct = lambda _symbol: None
    agent._estimate_earnings_trend_score = lambda _symbol: None
    agent._compute_recent_earnings_reactions = lambda _symbol, n=3: []
    agent._compute_recent_reaction_tilt = lambda _reactions: None
    agent._enrich_with_verified_data = lambda _symbol: {}
    agent._compute_historical_beat_stats = lambda _symbol: {
        "rate": None, "beats": 0, "samples": 0, "source": None,
        "unavailable_reason": "provider miss"}
    agent.config_manager = SimpleNamespace(watchlist={"stocks": ["NVDA"]})
    return agent


@pytest.mark.unit
def test_row_without_history_has_no_high_gap_badge_and_no_gap_band(monkeypatch):
    monkeypatch.setattr(pe, "compute_gap_risk_score", lambda *a, **k: (88, []))
    agent = _build_agent()
    event_utc = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    item = agent._build_item_for_symbol(
        symbol="NVDA",
        news_rows=[],
        earnings_rows=[{"symbol": "NVDA", "date": event_utc, "session": "AMC"}],
        mismatch_info=None,
        cfg={},
        state_row={},
        as_of_utc=datetime.now(timezone.utc).isoformat(),
    )
    assert item["gap_risk"] is None and item["gap_risk_unavailable"] is True
    assert "HIGH_GAP_RISK" not in item["badges"]
    for key in ("confidence", "confidence_interval"):
        band = item[key]["gap_risk"]
        assert band["low"] is None and band["high"] is None
        assert band["unavailable"] is True
    assert item["is_watchlist"] is True


@pytest.mark.unit
def test_bands_do_not_claim_a_68_percent_level():
    bands = PreEarningsAgent._estimate_confidence_intervals(
        gap_risk=60, direction_bias=20, historical_samples=6, news_count=8,
        implied_available=True, earnings_trend_available=True,
        group_samples=30, global_samples=500)
    for name in ("gap_risk", "direction_bias"):
        assert bands[name]["level"] is None
        assert bands[name]["basis"] == "heuristic_band_not_statistical"
        assert bands[name]["low"] <= bands[name]["high"]


# ------------------------------------------------------------ watchlist first

@pytest.mark.unit
def test_continuous_scanner_takes_watchlist_reporters_before_the_alphabet():
    agent = SimpleNamespace(
        _state={
            "calendar_universe": {"window_start": "2026-11-15", "window_end": "2026-12-15"},
            "items": [
                {"symbol": "ACM", "earnings_time_utc": "2026-11-16T00:00:00+00:00"},
                {"symbol": "AIV", "earnings_time_utc": "2026-11-16T00:00:00+00:00"},
                {"symbol": "GRRR", "earnings_time_utc": "2026-11-16T00:00:00+00:00"},
                {"symbol": "FIRY", "earnings_time_utc": "2026-11-16T00:00:00+00:00"},
                {"symbol": "NVDA", "earnings_time_utc": "2026-11-17T00:00:00+00:00"},
                {"symbol": "ZZZ", "earnings_time_utc": "2026-11-16T00:00:00+00:00"},
            ],
        },
        _get_watchlist_symbols=lambda: ["NVDA", "FIRY", "GRRR", "AAPL"],
    )
    assert continuous._eligible_symbols(agent) == ["FIRY", "GRRR", "NVDA", "ACM", "AIV", "ZZZ"]

    # no watchlist available -> unchanged nearest-first order
    agent_no_wl = SimpleNamespace(_state=agent._state)
    assert continuous._eligible_symbols(agent_no_wl) == ["ACM", "AIV", "FIRY", "GRRR", "ZZZ", "NVDA"]


def _low_row(symbol: str, days: int) -> dict:
    return {
        "symbol": symbol,
        "earnings_time_utc": pe._iso(datetime.now(timezone.utc) + timedelta(days=days)),
        "session": "AMC",
        "event_status": "UPCOMING",
        "coverage_score": 20,
        "coverage_label": "LOW",
        "badges": ["LOW_COVERAGE"],
    }


@pytest.mark.unit
def test_toolbar_warmup_and_cycle_warmup_put_watchlist_first():
    agent = _bare_agent()
    agent.config_manager = SimpleNamespace(watchlist={"stocks": ["NVDA", "GRRR", "FIRY"]})
    agent._state = {
        "status": {},
        "items": [_low_row(s, 2) for s in ("AAA", "ABB", "ACC", "ADD", "AEE", "AFF")]
        + [_low_row("NVDA", 3), _low_row("GRRR", 2), _low_row("FIRY", 2)],
    }

    targets = agent._resolve_coverage_targets(symbols=None, only_needs_coverage=True)
    assert sorted(targets[:3]) == ["FIRY", "GRRR", "NVDA"]
    assert targets[2] == "NVDA"          # later event comes after the two imminent ones

    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return {"status": "accepted"}

    agent.request_coverage_warmup = _capture
    result = agent._queue_watchlist_priority_warmup()
    assert result == {"status": "accepted"}
    assert sorted(captured["symbols"]) == ["FIRY", "GRRR", "NVDA"]
    assert captured["symbols"][-1] == "NVDA"     # nearest event first
    assert captured["only_needs_coverage"] is True

    agent.config_manager = SimpleNamespace(watchlist={})
    assert agent._queue_watchlist_priority_warmup() == {"status": "no_watchlist"}


# --------------------------------------------------- "'core.ai'" at server start

_RACE = textwrap.dedent(
    """
    import importlib, sys, threading, time
    errors = []
    gate = threading.Barrier(2)

    def load(name, delay):
        gate.wait()
        time.sleep(delay)
        try:
            importlib.import_module(name)
        except BaseException as exc:
            errors.append(name + ": " + repr(exc))

    threads = [
        threading.Thread(target=load, args=("core.pre_earnings.decision", 0.0)),
        threading.Thread(target=load, args=(sys.argv[1], float(sys.argv[2]))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print("ERRORS=" + "|".join(errors))
    """
)


@pytest.mark.unit
@pytest.mark.parametrize("other,delay", [("core.ai.decision_engine", "0"), ("core.ai.debate", "0.002")])
def test_pre_earnings_decision_import_survives_a_concurrent_core_ai_import(other, delay, tmp_path):
    """First import of core.pre_earnings.decision racing another thread's first
    import of a core.ai submodule (AgentManager monitor vs API startup)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["MS_DATA_DIR"] = str(tmp_path)
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", _RACE, other, delay],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=150,
    )
    marker = [line for line in proc.stdout.splitlines() if line.startswith("ERRORS=")]
    assert marker, proc.stderr[-2000:]
    failures = [e for e in marker[-1][len("ERRORS="):].split("|") if e]
    assert not [e for e in failures if e.startswith("core.pre_earnings.decision")], failures


# ------------------------------------------------ honest "where the bias is from"

@pytest.mark.unit
def test_direction_explanation_names_the_real_inputs_when_there_is_no_news():
    """GRRR: zero news, yet the row said 'DirectionBias=32.67 from cached news
    sentiment and catalyst flags' and 'DirectionBias from news'. The +33 came
    from the eToro crowd skew alone."""
    from core.pre_earnings.score import compute_data_confidence

    agent = _bare_agent()
    why = agent._build_why_bullets(
        symbol="GRRR", gap_risk=0, direction_bias=33, news_count=0, severity_label="",
        mismatch=False, event={}, contributors=[],
        gap_risk_unavailable_reason="No historical quarters",
        direction_components={
            "historical": None, "analyst_revision": None, "options": None,
            "news_ai": 50.0, "post_earnings_history": None, "retail_positioning": 99.0,
        },
    )
    assert why[1] == "DirectionBias=33 from eToro retail positioning, no news (sentiment held neutral)."

    with_news = agent._build_why_bullets(
        symbol="NVDA", gap_risk=70, direction_bias=26, news_count=84, severity_label="",
        mismatch=False, event={}, contributors=[],
        direction_components={"analyst_revision": 71.0, "news_ai": 44.8},
    )
    assert with_news[1] == "DirectionBias=26 from analyst consensus, news sentiment."

    _level, message, insufficient = compute_data_confidence(
        historical_samples=0, coverage_score=20, implied_available=False, news_count=0)
    assert insufficient is True
    assert "DirectionBias from news" not in message
    assert "No news either" in message
    _level, message, _ = compute_data_confidence(
        historical_samples=0, coverage_score=50, implied_available=False, news_count=84)
    assert message.endswith("DirectionBias from news.")
