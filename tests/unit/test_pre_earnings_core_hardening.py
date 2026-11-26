import logging
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import yfinance

from agents import pre_earnings_agent as pe
from agents.pre_earnings_agent import PreEarningsAgent
from core.request_context import ExternalCallBudget, get_request_context, request_context


def test_calendar_universe_summary_counts_unique_reporters_and_estimates(monkeypatch):
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent.config_manager = MagicMock()
    agent.config_manager.config = {}
    calendar = MagicMock()
    calendar.get_calendar_events_range.return_value = {
        "earnings": [
            {"symbol": "AMAT", "source": "nasdaq_zacks_estimate"},
            {"symbol": "AAPL", "source": "yfinance"},
            {"symbol": "AAPL", "source": "nasdaq_zacks_estimate"},
        ]
    }
    monkeypatch.setattr(pe, "get_economic_calendar_agent_if_ready", lambda: calendar)

    summary = agent._calendar_universe_summary({"lookahead_days": 10})

    assert summary["reporter_count"] == 2
    assert summary["confirmed_or_provider_count"] == 1
    assert summary["estimated_count"] == 1
    calendar.get_calendar_events_range.assert_called_once()


def test_calendar_universe_summary_does_not_initialize_calendar_singleton(monkeypatch):
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent.config_manager = MagicMock()
    agent.config_manager.config = {}
    constructor = MagicMock(side_effect=AssertionError("must remain cache-only"))
    monkeypatch.setattr(pe, "get_economic_calendar_agent", constructor)
    monkeypatch.setattr(pe, "get_economic_calendar_agent_if_ready", lambda: None)

    summary = agent._calendar_universe_summary({"lookahead_days": 10})

    assert summary["reporter_count"] == 0
    constructor.assert_not_called()


def test_default_provider_budget_does_not_scale_with_result_table(monkeypatch):
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent.config_manager = MagicMock()
    agent.config_manager.config = {"pre_earnings": {"max_results": 200}}
    monkeypatch.delenv("PRE_EARNINGS_EXTERNAL_CALL_BUDGET", raising=False)

    cfg = agent._get_config()

    assert cfg["max_results"] == 200
    assert cfg["external_call_budget"] == 30


class _Index:
    def __init__(self, value):
        self._value = value

    def to_pydatetime(self):
        return self._value


class _Frame:
    def __init__(self, rows):
        self._rows = list(rows)

    def __len__(self):
        return len(self._rows)

    def iterrows(self):
        return iter(self._rows)


def _earnings_frame():
    dt = datetime(2026, 5, 1, 16, 0, tzinfo=timezone.utc)
    return _Frame(
        [
            (
                _Index(dt),
                {"EPS Estimate": 1.0, "Reported EPS": 1.2},
            )
        ]
    )


def _direction_item(symbol="GCT", score=40):
    return {
        "symbol": symbol,
        "earnings_time_utc": pe._iso(datetime.now(timezone.utc) + timedelta(days=5)),
        "structured_direction_bias_score": score,
        "direction_bias_score": score,
        "direction_bias": score / 100.0,
        "direction_label": "LONG",
        "gap_risk": 60,
        "coverage_score": 35,
        "badges": ["LONG_BIAS"],
        "bias_reasons": ["structured"],
        "top_news": [],
    }


def test_persisted_state_revision_is_monotonic_and_survives_reload(
    tmp_path,
    monkeypatch,
):
    output_path = tmp_path / "pre_earnings.json"
    monkeypatch.setattr(pe, "PRE_EARNINGS_OUTPUT_PATH", output_path)
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._state = {"state_revision": 7, "items": [], "status": {}}
    agent._persist_ai_cache_locked = lambda: None

    agent._persist_locked()
    first = json.loads(output_path.read_text(encoding="utf-8"))
    agent._persist_locked()
    second = json.loads(output_path.read_text(encoding="utf-8"))

    assert first["state_revision"] > 7
    assert second["state_revision"] > first["state_revision"]
    assert agent._state["state_revision"] == second["state_revision"]
    assert agent._state_read_fallback["state_revision"] == second["state_revision"]

    # A full scan replaces the payload wholesale. That must not reset the
    # revision just because the newly built dict does not carry the old field.
    agent._state = {"items": [], "status": {}}
    agent._persist_locked()
    replaced = json.loads(output_path.read_text(encoding="utf-8"))
    assert replaced["state_revision"] > second["state_revision"]


def test_get_state_contention_uses_isolated_latest_published_fallback():
    class _BusyLock:
        def acquire(self, timeout=None):
            del timeout
            return False

    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = _BusyLock()
    agent._state_read_fallback = {
        "state_revision": 12,
        "items": [],
        "status": {"last_success_time": None},
        "flags": {},
    }
    agent._get_config = lambda: {"refresh_seconds": 600}
    agent._is_enabled = lambda: False
    agent._attach_daily_ranking = lambda payload, cfg: None

    payload = agent.get_state()
    payload["status"]["caller_mutation"] = True

    assert payload["state_revision"] == 12
    assert "caller_mutation" not in agent._state_read_fallback["status"]


def test_get_state_removes_expired_persisted_rows_from_active_candidates():
    now = datetime.now(timezone.utc)
    past = (now - timedelta(days=1)).isoformat()
    future = (now + timedelta(days=2)).isoformat()
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.Lock()
    agent._state = {
        "items": [
            {
                "symbol": "PAST",
                "earnings_time_utc": past,
                "session": "BMO",
                "event_source_url": "https://example.com/past",
            },
            {
                "symbol": "FUTURE",
                "earnings_time_utc": future,
                "session": "AMC",
                "event_source_url": "https://example.com/future",
            },
        ],
        "reported_today": [],
        "status": {"last_success_time": None},
        "flags": {},
    }
    agent._state_read_fallback = {}
    agent._get_config = lambda: {
        "refresh_seconds": 600,
        "reported_session_filter": True,
    }
    agent._is_enabled = lambda: False
    agent._synchronize_direction_decision = lambda item, cfg: None
    agent._attach_daily_ranking = lambda payload, cfg: None
    agent._calendar_universe_summary = lambda cfg: {}

    payload = agent.get_state()

    assert [item["symbol"] for item in payload["items"]] == ["FUTURE"]
    assert [item["symbol"] for item in payload["reported_today"]] == ["PAST"]


def test_yfinance_history_empty_response_is_not_negative_cached(monkeypatch):
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._yf_earnings_history_cache = {}
    responses = iter([_Frame([]), _earnings_frame()])
    calls = []

    class _Ticker:
        def get_earnings_dates(self, limit):
            calls.append(limit)
            return next(responses)

    monkeypatch.setattr(yfinance, "Ticker", lambda _symbol: _Ticker())

    assert agent._get_yfinance_earnings_history(
        "GCT", allow_remote=True
    ) == []
    assert "GCT" not in agent._yf_earnings_history_cache

    rows = agent._get_yfinance_earnings_history("GCT", allow_remote=True)
    assert len(rows) == 1
    assert rows[0]["epsActual"] == 1.2
    assert len(calls) == 2


def test_explicit_history_retry_bypasses_legacy_negative_but_reuses_success(monkeypatch):
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._yf_earnings_history_cache = {"GCT": (time.time(), [])}
    calls = {"count": 0}

    class _Ticker:
        def get_earnings_dates(self, limit):
            calls["count"] += 1
            return _earnings_frame()

    monkeypatch.setattr(yfinance, "Ticker", lambda _symbol: _Ticker())
    rows = agent._get_yfinance_earnings_history(
        "GCT",
        allow_remote=True,
        force_refresh=True,
    )
    assert len(rows) == 1
    assert calls["count"] == 1

    # A successful 12-hour entry stays cache-first even during explicit warmup.
    again = agent._get_yfinance_earnings_history(
        "GCT",
        allow_remote=True,
        force_refresh=True,
    )
    assert again == rows
    assert calls["count"] == 1


def test_direct_yfinance_calls_obey_budget_and_warn_once(monkeypatch, caplog):
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._yf_earnings_history_cache = {}
    calls = {"count": 0}

    class _Ticker:
        def get_earnings_dates(self, limit):
            calls["count"] += 1
            return _Frame([])

    monkeypatch.setattr(yfinance, "Ticker", lambda _symbol: _Ticker())
    budget = ExternalCallBudget(1)
    caplog.set_level(logging.WARNING, logger="PreEarningsAgent")

    with request_context(
        reason="TEST",
        agent_name="pre_earnings",
        external_budget=budget,
    ):
        for symbol in ("AAA", "BBB", "CCC"):
            assert agent._get_yfinance_earnings_history(
                symbol,
                allow_remote=True,
                force_refresh=True,
            ) == []

    snapshot = budget.snapshot()
    assert calls["count"] == 1
    assert snapshot["used"] == 1
    assert snapshot["blocked"] == 2
    warnings = [
        record
        for record in caplog.records
        if "External-call budget exhausted for agent=pre_earnings"
        in record.getMessage()
    ]
    assert len(warnings) == 1


def test_coverage_worker_creates_real_agent_budget_context():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._get_config = lambda: {"external_call_budget": 3}
    observed = {}

    def _capture(*_args):
        ctx = get_request_context()
        observed["agent"] = ctx.agent_name
        observed["budget"] = ctx.external_budget.snapshot()

    agent._run_coverage_warmup_worker_impl = _capture
    agent._run_coverage_warmup_worker("job", ["GCT"], 1, True)

    assert observed["agent"] == "pre_earnings"
    assert observed["budget"]["limit"] == 3


def test_ai_quota_is_shared_between_early_and_final_pass(monkeypatch):
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    agent._state = {
        "items": [_direction_item("AAPL"), _direction_item("MSFT")],
        "status": {},
    }
    agent._concurrent_item_revision = 0
    agent._concurrent_item_updates = {}
    agent._persist_locked = lambda: None
    calls = []

    monkeypatch.setattr(pe.ollama, "is_cloud_host", lambda: True)
    monkeypatch.setattr(pe.ollama, "get_best_model", lambda: "cloud-model")

    def _enrich(item, _model, _timeout):
        calls.append(item["symbol"])
        return {
            "sentiment": 0.4,
            "decision": "LONG",
            "decision_confidence": 0.7,
            "decision_rationale": "test",
            "catalyst_label": "OTHER",
        }

    agent._ollama_enrich_item = _enrich
    cfg = {
        "use_ollama": True,
        "ollama_max_tickers": 2,
        "ollama_timeout_seconds": 2,
        "ollama_decision_weight": 0.35,
    }
    quota = {"remaining": 2, "attempted": 0}

    agent._early_ai_pass(cfg, cycle_quota=quota)
    final_items = [_direction_item("NVDA"), _direction_item("AMZN")]
    agent._apply_ollama(final_items, cfg, cycle_quota=quota)

    assert calls == ["AAPL", "MSFT"]
    assert quota == {"remaining": 0, "attempted": 2}
    assert not any(item.get("ai_decision") for item in final_items)


def test_targeted_rescore_commits_atomically_and_records_revision(monkeypatch):
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    canonical_decision = {
        "action": "WATCH",
        "recommendation": "avoid",
        "available": False,
    }
    current = _direction_item()
    current["pre_earnings_decision"] = canonical_decision
    agent._state = {
        "items": [current],
        "status": {},
        "calibration": {},
    }
    agent._feedback = {}
    agent._concurrent_item_revision = 0
    agent._concurrent_item_updates = {}
    agent._load_state_rows_map = lambda: {}
    agent._cached_news_rows_for_symbol = lambda _symbol: []
    agent._cached_earnings_rows_for_symbol = (
        lambda _symbol, _cfg, _current: [
            {
                "symbol": "GCT",
                "date": "2026-08-06T12:00:00+00:00",
                "session": "BMO",
            }
        ]
    )
    rebuilt = _direction_item()
    rebuilt["coverage_score"] = 96
    rebuilt["beat_rate_historical"] = 0.875
    agent._build_item_for_symbol = MagicMock(return_value=rebuilt)
    agent._carry_forward_ai_decision = MagicMock(return_value=False)
    persisted = {"count": 0}
    agent._persist_locked = lambda: persisted.__setitem__(
        "count", persisted["count"] + 1
    )

    result = agent._rescore_cached_symbol(
        "GCT",
        {"lookahead_days": 7, "ollama_decision_weight": 0.35},
    )

    assert result["status"] == "ok"
    assert agent._state["items"][0]["coverage_score"] == 96
    assert agent._state["items"][0]["beat_rate_historical"] == 0.875
    assert agent._state["items"][0]["pre_earnings_decision"] == canonical_decision
    assert agent._concurrent_item_revision == 1
    assert persisted["count"] == 1


def test_concurrent_targeted_update_overlays_stale_full_cycle_item():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    agent._concurrent_item_revision = 0
    agent._concurrent_item_updates = {}
    updated = _direction_item()
    updated["coverage_score"] = 96
    updated["ai_decision"] = {
        "used": True,
        "verdict": "SHORT",
        "confidence": 0.8,
        "sentiment": -0.7,
        "weight": 0.35,
        "source": "forced",
    }
    with agent._lock:
        agent._record_concurrent_item_update_locked(
            "GCT",
            updated,
            "forced_ai",
        )

    stale_full_cycle = _direction_item()
    merged = agent._merge_concurrent_item_updates(
        [stale_full_cycle],
        since_revision=0,
        cfg={"ollama_decision_weight": 0.35},
    )

    assert merged[0]["coverage_score"] == 96
    assert merged[0]["ai_decision"]["source"] == "forced"
    assert merged[0]["direction_bias_score"] != stale_full_cycle[
        "direction_bias_score"
    ]


def test_coverage_worker_uses_targeted_rescore_without_full_scan(monkeypatch):
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    agent._state = {
        "items": [_direction_item()],
        "status": {
            "coverage_warmup": {
                "running": True,
                "job_id": "job",
            }
        },
    }
    agent._coverage_pending_symbols = []
    agent._warmup_symbol_coverage = MagicMock(return_value=True)
    agent._rescore_cached_symbol = MagicMock(return_value={"status": "ok"})
    agent._persist_locked = lambda: None
    agent._is_enabled = lambda: True
    agent.request_refresh = MagicMock()
    monkeypatch.setattr(pe.time, "sleep", lambda _seconds: None)

    agent._run_coverage_warmup_worker_impl(
        "job",
        ["GCT"],
        1,
        True,
        {"external_call_budget": 10},
    )

    agent._rescore_cached_symbol.assert_called_once()
    agent.request_refresh.assert_not_called()
    warm = agent._state["status"]["coverage_warmup"]
    assert warm["running"] is False
    assert warm["rescored"] == 1
    assert agent._state["status"]["last_success_time"] == warm["last_finished"]


def test_low_coverage_news_queue_merges_symbols_arriving_inflight():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._low_coverage_news_lock = threading.RLock()
    agent._low_coverage_news_pending = []
    agent._low_coverage_news_thread = None
    agent._low_coverage_news_idle = threading.Event()
    agent._low_coverage_news_idle.set()
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class _NewsAgent:
        def ensure_ticker_news_cached(
            self,
            symbol,
            force_refresh=False,
            allow_remote=True,
        ):
            del force_refresh, allow_remote
            calls.append(symbol)
            if symbol == "HUT":
                entered.set()
                assert release.wait(timeout=2)
            return {"status": "ok", "count": 1}

    news_agent = _NewsAgent()
    first = agent._trigger_low_coverage_news_refresh(
        news_agent,
        ["HUT"],
        {"external_call_budget": 5},
    )
    assert first["status"] == "accepted"
    assert entered.wait(timeout=2)

    second = agent._trigger_low_coverage_news_refresh(
        news_agent,
        ["GCT"],
        {"external_call_budget": 5},
    )
    assert second["status"] == "queued"
    assert second["queued_symbols"] == ["GCT"]
    release.set()
    assert first["event"].wait(timeout=2)
    assert calls == ["HUT", "GCT"]


def _high_coverage_item_with_missing_beat(samples=0):
    item = _direction_item("HUT")
    item.update(
        {
            "coverage_score": 96,
            "coverage_label": "HIGH",
            "badges": ["LONG_BIAS"],
            "beat_rate_historical": None if samples == 0 else 1.0,
            "beat_rate_samples": samples,
            "beat_rate_unavailable_reason": (
                "No completed reports with both estimated and reported EPS."
                if samples == 0
                else None
            ),
            "historical_sample_size": 8,
            "range_proxy_pct": 2.5,
            "expected_move_pct": 2.5,
            "analyst_target_upside_pct": 12.0,
            "analyst_rec_mean": 2.0,
            "analyst_count": 8,
            "market_cap": 2_000_000_000,
            "top_news": [{"title": "HUT earnings preview"}],
        }
    )
    return item


def test_global_enrichment_selects_high_coverage_missing_beat_rate():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    agent._state = {
        "items": [_high_coverage_item_with_missing_beat(samples=0)],
        "status": {},
    }

    targets = agent._resolve_coverage_targets(
        symbols=None,
        only_needs_coverage=True,
    )

    assert targets == ["HUT"]


def test_global_enrichment_selects_insufficient_beat_sample():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    agent._state = {
        "items": [_high_coverage_item_with_missing_beat(samples=2)],
        "status": {},
    }

    assert agent._is_low_coverage_item(agent._state["items"][0]) is True


def test_confirmed_missing_beat_gets_cooldown_but_manual_refresh_bypasses():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    item = _high_coverage_item_with_missing_beat(samples=0)
    agent._mark_enrichment_attempt(item)
    agent._state = {"items": [item], "status": {}}

    assert agent._is_low_coverage_item(item) is False
    assert agent._resolve_coverage_targets(
        symbols=None,
        only_needs_coverage=True,
    ) == []
    assert agent._resolve_coverage_targets(
        symbols=["HUT"],
        only_needs_coverage=False,
    ) == ["HUT"]

    item["enrichment"]["retry_after_utc"]["beat_history"] = (
        "2020-01-01T00:00:00+00:00"
    )
    assert agent._is_low_coverage_item(item) is True


def test_enrichment_cooldown_survives_same_event_full_rebuild():
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    previous = _high_coverage_item_with_missing_beat(samples=0)
    agent._mark_enrichment_attempt(previous)
    rebuilt = _high_coverage_item_with_missing_beat(samples=0)

    agent._carry_forward_enrichment_state(rebuilt, previous)

    assert rebuilt["enrichment"]["retry_after_utc"]["beat_history"]
    assert agent._is_low_coverage_item(rebuilt) is False


def test_coverage_worker_start_is_atomic_under_concurrent_requests(monkeypatch):
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    agent._state = {
        "items": [_high_coverage_item_with_missing_beat(samples=0)],
        "status": {},
    }
    agent._state_read_fallback = agent._state
    agent._coverage_pending_symbols = []
    agent._coverage_job_seq = 0
    agent._coverage_thread = None

    constructor_entered = threading.Event()
    allow_constructor_return = threading.Event()
    second_invoked = threading.Event()
    constructor_count = {"value": 0}
    count_lock = threading.Lock()
    real_thread = threading.Thread

    class _ReservedWorker:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            with count_lock:
                constructor_count["value"] += 1
                ordinal = constructor_count["value"]
            self.ident = None
            self._alive = False
            if ordinal == 1:
                constructor_entered.set()
                assert allow_constructor_return.wait(timeout=2)

        def start(self):
            self.ident = 1
            self._alive = True

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(pe.threading, "Thread", _ReservedWorker)
    results = []

    def _request(second=False):
        if second:
            second_invoked.set()
        results.append(
            agent.request_coverage_warmup(
                symbols=["HUT"],
                batch_size=1,
                only_needs_coverage=False,
                allow_remote_on_miss=True,
            )
        )

    first = real_thread(target=_request)
    first.start()
    assert constructor_entered.wait(timeout=2)
    second = real_thread(target=lambda: _request(second=True))
    second.start()
    assert second_invoked.wait(timeout=2)
    try:
        time.sleep(0.05)
        assert constructor_count["value"] == 1
    finally:
        allow_constructor_return.set()

    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert constructor_count["value"] == 1
    assert {result["status"] for result in results} == {
        "accepted",
        "already_running",
    }


def _rich_item(symbol="GCT", coverage=96, gap_risk=80, etu=None):
    item = _direction_item(symbol=symbol)
    item["coverage_score"] = coverage
    item["gap_risk"] = gap_risk
    # Default: the SAME earnings window as _direction_item (now + 5d) so the
    # richness-overlay contract is exercised on a shared event, not tied to a
    # hardcoded calendar date that eventually drifts out of the 14-day window.
    item["earnings_time_utc"] = etu or pe._iso(datetime.now(timezone.utc) + timedelta(days=5))
    item["top_news"] = [{"title": "rich news", "url": "https://example.com/rich"}]
    return item


def test_rescore_enrichment_survives_later_shallow_cycle():
    """Regression: a cycle starting AFTER a targeted rescore must NOT reset
    the enriched item. The revision-gate (revision <= since_revision) would
    skip it; the richness overlay must preserve the deeper rescore."""
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    agent._concurrent_item_revision = 0
    agent._concurrent_item_updates = {}
    rich = _rich_item(coverage=96)
    with agent._lock:
        agent._record_concurrent_item_update_locked("GCT", rich, "targeted_rescore")
    agent._concurrent_item_revision = 1  # cycle starts after the rescore

    shallow = _direction_item()  # coverage_score=35
    merged = agent._merge_concurrent_item_updates(
        [shallow],
        since_revision=1,
        cfg={"refresh_seconds": 600, "ollama_decision_weight": 0.35},
    )
    assert merged[0]["coverage_score"] == 96  # enriched preserved, not reset


def test_stale_rescore_not_overlaid_after_grace():
    """A rescore older than refresh_seconds must NOT clobber a fresh cycle."""
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    agent._concurrent_item_revision = 0
    agent._concurrent_item_updates = {}
    rich = _rich_item(coverage=96)
    with agent._lock:
        agent._record_concurrent_item_update_locked("GCT", rich, "targeted_rescore")
    # Force the recorded timestamp outside the 600s grace window.
    stale_ts = pe._iso(datetime.now(timezone.utc) - timedelta(seconds=1200))
    agent._concurrent_item_updates["GCT"]["recorded_at"] = stale_ts
    agent._concurrent_item_revision = 1

    shallow = _direction_item()
    merged = agent._merge_concurrent_item_updates(
        [shallow],
        since_revision=1,
        cfg={"refresh_seconds": 600, "ollama_decision_weight": 0.35},
    )
    assert merged[0]["coverage_score"] == 35  # stale rescore not overlaid


def test_richer_overlay_preserves_cycle_ai_when_rescore_lacks_it():
    """On the richness path, a rescore (no AI) must not erase a cycle's
    early-AI-pass verdict."""
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    agent._concurrent_item_revision = 0
    agent._concurrent_item_updates = {}
    rich = _rich_item(coverage=96)
    rich.pop("ai_decision", None)  # rescore does not run AI
    with agent._lock:
        agent._record_concurrent_item_update_locked("GCT", rich, "targeted_rescore")
    agent._concurrent_item_revision = 1

    shallow = _direction_item()
    shallow["ai_decision"] = {
        "used": True,
        "verdict": "SHORT",
        "confidence": 0.8,
        "sentiment": -0.7,
        "weight": 0.35,
        "source": "forced",
    }
    shallow["pre_earnings_decision"] = {
        "action": "WATCH",
        "recommendation": "avoid",
        "available": False,
    }
    merged = agent._merge_concurrent_item_updates(
        [shallow],
        since_revision=1,
        cfg={"refresh_seconds": 600, "ollama_decision_weight": 0.35},
    )
    assert merged[0]["coverage_score"] == 96  # rescore preserved
    assert merged[0]["ai_decision"]["source"] == "forced"  # cycle AI preserved
    assert merged[0]["pre_earnings_decision"]["recommendation"] == "avoid"


def test_richer_overlay_skipped_when_earnings_window_differs():
    """A rescore for a different earnings event (>14d) must not overlay."""
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    agent._concurrent_item_revision = 0
    agent._concurrent_item_updates = {}
    rich = _rich_item(coverage=96, etu="2026-08-06T12:00:00+00:00")
    with agent._lock:
        agent._record_concurrent_item_update_locked("GCT", rich, "targeted_rescore")
    agent._concurrent_item_revision = 1

    shallow = _direction_item()
    shallow["earnings_time_utc"] = "2026-09-10T12:00:00+00:00"  # >14d away
    merged = agent._merge_concurrent_item_updates(
        [shallow],
        since_revision=1,
        cfg={"refresh_seconds": 600, "ollama_decision_weight": 0.35},
    )
    assert merged[0]["coverage_score"] == 35  # cycle wins, different event


def test_equal_coverage_does_not_overlay():
    """When the rescore is not strictly richer (equal coverage), the cycle
    item wins (carry-forward already made them equivalent)."""
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    agent._lock = threading.RLock()
    agent._concurrent_item_revision = 0
    agent._concurrent_item_updates = {}
    rich = _rich_item(coverage=35, gap_risk=99)
    with agent._lock:
        agent._record_concurrent_item_update_locked("GCT", rich, "targeted_rescore")
    agent._concurrent_item_revision = 1

    shallow = _direction_item()  # coverage_score=35, gap_risk=60
    merged = agent._merge_concurrent_item_updates(
        [shallow],
        since_revision=1,
        cfg={"refresh_seconds": 600, "ollama_decision_weight": 0.35},
    )
    assert merged[0]["gap_risk"] == 60  # cycle's, not overlaid
