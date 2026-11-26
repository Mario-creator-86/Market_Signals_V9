"""Regression tests for the 2026-11-15 AI Trading Cockpit (Home tab) audit.

Each test pins one defect that was visible in the running app:
old news shown as current, a stale narrative shown as "Current tone", an
out-of-date Pre-Earnings snapshot described as "the current verified
snapshot", an Unhealthy system status that did not say which check failed,
a UTC day boundary for "today", and a 1 MB payload that was re-parsed and
re-encoded on every poll.
"""

import contextvars
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import api.cockpit_service as cockpit_service
import api.dashboard_service as dashboard_service
from api.cockpit_service import build_cockpit_payload
from core.strategy_registry import StrategyRegistry


NOW = datetime(2026, 11, 15, 7, 56, tzinfo=timezone.utc)


class _Config:
    def __init__(self):
        self.config = {"sec": {"identity_env": "TEST_EDGAR_IDENTITY"}, "ml": {"registry": []}}


def _sources(tmp_path, **overrides):
    sources = {
        "config": _Config(),
        "dashboard": {
            "dashboard_regime": {"regime": "risk-on", "sentiment": "bullish", "confidence": 40.0},
            "dashboard_market_narrative": {
                "available": True, "tone": "risk-on", "risk_regime": "risk-on",
                "generated_at": NOW.isoformat(), "top_catalysts": ["Catalyst"],
            },
            "dashboard_risk_flags": [],
            "dashboard_news_items": [],
            "dashboard_earnings_7d": [],
        },
        "capital_payload": {"capital": {
            "balance": 8000.0, "raw_cash_balance": 8000.0, "positions": [], "history": [],
            "decisions": [], "opportunities": {"signals": []},
        }},
        "pre_earnings": {
            "items": [], "refresh_seconds": 600,
            "status": {"last_success_time": NOW.isoformat(), "last_error": None,
                       "last_inputs_used": {"news_cache_age_seconds": 2, "earnings_cache_age_seconds": 44}},
        },
        "agents": {
            "bot": {"engine_running": True},
            "agents": {
                "news_tab": {"enabled": True, "running": True, "ready": True, "last_error": ""},
                "strategy_research": {"enabled": True, "running": True, "last_error": ""},
            },
            "config": {"risk_constitution": {"status": "active", "fingerprint": "f"}},
        },
        "execution": {
            "mode": "PAPER_AUTO", "kill_switch_active": False,
            "readiness": {"ready": True, "reasons": []},
            "reconciliation": {"status": "HEALTHY", "conflicts": [], "checked_at": NOW.isoformat()},
        },
        "providers": [{"name": "ollama", "enabled": True, "type": "ollama", "has_key": False}],
        "strategy_performance": {"entries": []},
        "data_health": {"streams": {
            "yf_prices": {"level": "VERIFIED", "source": "yahoo", "timestamp": NOW.isoformat()},
            "ollama_availability": {"level": "VERIFIED", "source": "ollama", "timestamp": NOW.isoformat(), "value": True},
        }},
        "database": {"id": "database", "label": "Database", "status": "HEALTHY", "message": "ok",
                     "checked_at": NOW.isoformat(), "evidence": {}},
        "registry": StrategyRegistry(tmp_path / "strategy_registry.json"),
        "now": NOW,
    }
    sources.update(overrides)
    return sources


def _news(item_id, title, published, impact="critical", score=100, **extra):
    row = {"id": item_id, "ticker": "MARKET", "title": title, "source": "wire", "url": "https://example.com/x",
           "published_at": published.isoformat(), "impact": impact, "impact_score": score, "sentiment": "neutral"}
    row.update(extra)
    return row


def _quiet_env(monkeypatch):
    for name in ("TEST_EDGAR_IDENTITY", "MLFLOW_TRACKING_URI", "RD_AGENT_ENABLED"):
        monkeypatch.delenv(name, raising=False)


# --- Major news: old items and repeats are not "current major news" ---------

def test_major_news_drops_two_month_old_items_and_repeated_headlines(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    sources = _sources(tmp_path)
    sources["dashboard"]["dashboard_news_items"] = [
        _news("old1", "Rate hike: what it means", NOW - timedelta(days=57)),
        _news("dup1", "12 STOCKS THAT COULD BENEFIT FROM A RATE HIKE", NOW - timedelta(hours=5)),
        _news("dup2", "12 stocks that could benefit from a rate hike!", NOW - timedelta(hours=5, minutes=2)),
        _news("new1", "Chip maker guidance cut", NOW - timedelta(hours=30), impact="high", score=65),
        _news("undated", "No timestamp item", NOW, published_at=None),
        _news("low", "Minor item", NOW - timedelta(hours=1), impact="low", score=10),
    ]
    sources["dashboard"]["dashboard_news_items"][4]["published_at"] = None
    section = build_cockpit_payload(**sources)["sections"]["major_news"]

    assert [row["id"] for row in section["items"]] == ["dup1", "new1"]
    assert section["window_hours"] == 72
    assert section["dropped_stale"] == 1
    assert section["dropped_duplicates"] == 1
    assert section["dropped_undated"] == 1


def test_major_news_empty_state_says_older_items_were_withheld(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    sources = _sources(tmp_path)
    sources["dashboard"]["dashboard_news_items"] = [_news("old", "Old critical", NOW - timedelta(days=58))]
    section = build_cockpit_payload(**sources)["sections"]["major_news"]
    assert section["state"] == "EMPTY"
    assert "last 72 hours" in section["message"]
    assert "1 older item(s) are not shown as current" in section["message"]


def test_dashboard_news_selection_keeps_ranking_but_only_current_unique_items():
    pool = [
        _news("a", "Old critical", NOW - timedelta(days=57)),
        _news("b", "Fresh story", NOW - timedelta(hours=2)),
        _news("c", "Fresh  STORY", NOW - timedelta(hours=3)),
        _news("d", "Flagged duplicate", NOW - timedelta(hours=1), is_duplicate=True),
        _news("e", "Second fresh story", NOW - timedelta(hours=40)),
    ]
    kept, meta = dashboard_service._select_current_news(pool, now=NOW, limit=12)
    assert [row["id"] for row in kept] == ["b", "e"]
    assert meta == {"window_hours": 72, "dropped_stale": 1, "dropped_undated": 0, "dropped_duplicates": 2}


def test_dashboard_news_read_is_bounded_when_the_news_agent_is_busy(monkeypatch):
    """A news agent that holds its cache lock must not stall the dashboard build."""
    release = threading.Event()

    class BusyAgent:
        def get_dashboard_news_items(self, active_tickers=None, limit=12):
            release.wait(5)
            return [{"id": "late"}]

        def get_market_narrative(self):
            return {"tone": "mixed"}

    monkeypatch.setattr(dashboard_service, "_news_read_state",
                        {"inflight": None, "ts": 100.0, "items": [{"id": "previous"}], "narrative": {"tone": "risk-on"}})
    started = time.perf_counter()
    items, narrative, live = dashboard_service._read_news_bounded(BusyAgent(), ["AAA"], timeout=0.2)
    elapsed = time.perf_counter() - started
    release.set()

    assert elapsed < 2.0
    assert live is False
    assert items == [{"id": "previous"}]
    assert narrative == {"tone": "risk-on"}


# --- Sentiment / regime: an old narrative is not "current" ------------------

def test_three_day_old_narrative_is_flagged_stale_in_sentiment_and_regime(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    sources = _sources(tmp_path)
    sources["dashboard"]["dashboard_market_narrative"]["generated_at"] = (NOW - timedelta(days=3)).isoformat()
    payload = build_cockpit_payload(**sources)
    sentiment = payload["sections"]["sentiment_shifts"]
    assert sentiment["freshness"] == "STALE"
    assert sentiment["items"][0]["freshness"] == "STALE"
    assert payload["header"]["regime"]["freshness"] == "STALE"

    sources["dashboard"]["dashboard_market_narrative"]["generated_at"] = (NOW - timedelta(minutes=6)).isoformat()
    fresh = build_cockpit_payload(**sources)
    assert fresh["sections"]["sentiment_shifts"]["items"][0]["freshness"] == "FRESH"
    assert fresh["header"]["regime"]["freshness"] == "FRESH"


# --- Pre-Earnings: stale snapshot is described as stale, consistently -------

def _stale_pre_earnings():
    return {
        "items": [], "refresh_seconds": 600,
        "status": {"last_success_time": (NOW - timedelta(days=24)).isoformat(), "last_error": None,
                   "last_inputs_used": {"news_cache_age_seconds": 2, "earnings_cache_age_seconds": 44}},
    }


def test_stale_empty_pre_earnings_snapshot_is_not_called_current_and_names_watchlist_events(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    sources = _sources(tmp_path, pre_earnings=_stale_pre_earnings())
    sources["dashboard"]["dashboard_earnings_7d"] = [
        {"ticker": "NVDA", "earnings_days": 2, "earnings_date": "2026-11-17", "earnings_when_desc": "After Hours"},
        {"ticker": "GRRR", "earnings_days": 1, "earnings_date": "2026-11-16", "earnings_when_desc": "After Hours"},
    ]
    sources["data_health"] = {"streams": {}}
    payload = build_cockpit_payload(**sources)
    section = payload["sections"]["upcoming_earnings"]

    assert section["state"] == "EMPTY"
    assert "current verified snapshot" not in section["message"]
    assert "out of date" in section["message"] and "24 days" in section["message"]
    assert "GRRR (1d)" in section["message"] and "NVDA (2d)" in section["message"]
    assert section["stale"] is True
    assert [row["ticker"] for row in section["watchlist_earnings"]] == ["GRRR", "NVDA"]
    assert all(row["in_scan"] is False for row in section["watchlist_earnings"])

    services = {item["id"]: item for item in payload["system_health"]["services"]}
    # A 24-day-old snapshot exists: that is a stale source, not "no snapshot".
    assert services["earnings"]["status"] == "DEGRADED"
    assert "stale" in services["earnings"]["message"] and "24 days" in services["earnings"]["message"]
    assert services["earnings"]["action"]
    # ...and its "news 2s old" stamp must not prove a live market-data path today.
    assert services["market_provider"]["status"] == "UNAVAILABLE"


def test_watchlist_earnings_tickers_are_listed_first_in_the_matrix(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    items = [
        {"symbol": f"T{index:02d}", "earnings_time_utc": "2026-11-16T00:00:00+00:00", "event_status": "UPCOMING",
         "trader_score": 90 - index, "flags": {}}
        for index in range(30)
    ]
    items.append({"symbol": "NVDA", "earnings_time_utc": "2026-11-17T00:00:00+00:00", "event_status": "UPCOMING",
                  "trader_score": 10, "flags": {}})
    pre = {"items": items, "refresh_seconds": 600,
           "status": {"last_success_time": NOW.isoformat(), "last_inputs_used": {}}}
    sources = _sources(tmp_path, pre_earnings=pre)
    sources["dashboard"]["dashboard_earnings_7d"] = [{"ticker": "NVDA", "earnings_days": 2}]

    calls = []
    real = cockpit_service.assess_pre_earnings_candidate

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(cockpit_service, "assess_pre_earnings_candidate", counting)
    section = build_cockpit_payload(**sources)["sections"]["upcoming_earnings"]

    assert section["items"][0]["ticker"] == "NVDA"
    assert section["watchlist_earnings"][0]["in_scan"] is True
    assert section["count"] == 31 and len(section["items"]) == 20
    # The autonomous prefilter runs for the displayed rows only.
    assert len(calls) == 20
    assert all(row["autonomous_prefilter"] for row in section["items"])
    assert all("paper_auto_trace" in row for row in section["items"])


# --- System health / risks: say which check fails and what to do ------------

def test_reconciliation_conflict_is_explained_with_an_action_everywhere(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    sources = _sources(tmp_path)
    sources["execution"] = {
        "mode": "PAPER_AUTO", "kill_switch_active": False,
        "readiness": {"ready": False, "reasons": ["EXECUTION_STATE_CONFLICT must be resolved"]},
        "reconciliation": {
            "status": "EXECUTION_STATE_CONFLICT", "checked_at": NOW.isoformat(),
            "conflicts": [{"code": "filled_order_position_missing",
                           "order_intent_id": "ord_1629", "position_id": "pos_ccd4"}],
        },
    }
    payload = build_cockpit_payload(**sources)

    risks = payload["sections"]["risks"]["items"]
    ledger_risk = next(row for row in risks if row["type"] == "EXECUTION_RECONCILIATION")
    assert ledger_risk["title"] == "Paper order ledger conflict (1)"
    assert "ord_1629" in ledger_risk["detail"] and "pos_ccd4" in ledger_risk["detail"]
    assert ledger_risk["detail"] != "EXECUTION_STATE_CONFLICT"
    assert "Reconcile broker" in ledger_risk["action"]
    readiness = next(row for row in risks if row["type"] == "PAPER_READINESS")
    assert "EXECUTION_STATE_CONFLICT" not in readiness["detail"]

    health = payload["system_health"]
    assert health["overall"] == "UNHEALTHY"
    assert [row["id"] for row in health["failing"]] == ["paper_broker"]
    assert health["failing"][0]["action"]
    assert health["summary"].startswith("1 of 15 checks need attention: Paper Broker")
    broker = next(item for item in health["services"] if item["id"] == "paper_broker")
    assert "ord_1629" in broker["message"] and "blocked" in broker["message"]
    assert broker["evidence"]["conflict_count"] == 1


def test_healthy_system_reports_no_failing_check_and_header_ai_matches_health_tile(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    payload = build_cockpit_payload(**_sources(tmp_path))
    health = payload["system_health"]
    assert health["failing"] == []
    assert "active checks pass" in health["summary"]
    tile = next(item for item in health["services"] if item["id"] == "ai_providers")
    assert payload["header"]["ai"]["status"] == tile["status"]
    assert all("action" not in item for item in health["services"] if item["status"] == "HEALTHY")


# --- Daily P/L: same day boundary as the Risk Agent's daily-loss limit ------

def test_daily_realized_pnl_uses_the_app_trading_day_not_utc(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    sources = _sources(tmp_path)
    sources["capital_payload"]["capital"]["history"] = [
        # 00:30 Europe/Bucharest on Nov 15 (22:30 UTC on Nov 14): today for the Risk Agent.
        {"timestamp": "2026-11-14T22:30:00+00:00", "type": "CLOSE_POSITION", "amount": 5.0, "metadata": {"net_pnl": 4.0}},
        # 23:30 Europe/Bucharest on Nov 14: yesterday.
        {"timestamp": "2026-11-14T21:30:00+00:00", "type": "CLOSE_POSITION", "amount": 9.0, "metadata": {"net_pnl": 9.0}},
        {"timestamp": "2026-11-15T06:00:00+00:00", "type": "OPEN_POSITION", "amount": -100.0, "metadata": {}},
    ]
    capital = build_cockpit_payload(**sources)["header"]["capital"]
    assert capital["daily_realized_pnl"] == 4.0
    assert capital["daily_closed_trades"] == 1
    assert capital["daily_pnl_day"] == "2026-11-15"
    assert capital["daily_pnl_timezone"] == "Europe/Bucharest"
    assert "UTC" not in capital["daily_pnl_basis"]
    assert capital["daily_pnl_window_truncated"] is False


# --- Payload size / speed ---------------------------------------------------

def test_overview_decisions_are_compact_and_full_rows_live_once_in_the_ledger():
    full = {
        "state": "SUCCESS", "count": 2, "coverage": "PERSISTENT_LEDGER", "total_entries": 2699,
        "items": [
            {"decision_id": "d1", "ticker": "PEPE-USD", "timestamp": NOW.isoformat(), "event_type": "decision_rejected",
             "status": "rejected", "what_happened": "WATCH (rejected)", "validation": {"big": "x" * 5000},
             "risk": {"checks": []}, "prompt": {"version": "v1"}},
            {"decision_id": "d2", "ticker": "AAA", "timestamp": NOW.isoformat(), "status": "accepted",
             "what_happened": "OPEN_LONG (accepted)", "critic": {"rationale": "y" * 5000}},
        ],
    }
    compact = cockpit_service._compact_decisions_section(full)
    assert compact["total_entries"] == 2699 and compact["count"] == 2
    assert [row["decision_id"] for row in compact["items"]] == ["d1", "d2"]
    assert all(not ({"validation", "risk", "prompt", "critic"} & set(row)) for row in compact["items"])
    assert len(json.dumps(compact)) < 1000 < len(json.dumps(full))


def test_cockpit_build_pins_cache_only_context_in_background_threads(monkeypatch):
    """Rebuild threads carry no request context; the dashboard builder would then
    default to allow_remote=True and refresh the watchlist from the provider."""
    from core.request_context import get_request_context

    seen = {}

    def fake_dashboard():
        ctx = get_request_context()
        seen["cache_only"] = bool(ctx and ctx.cache_only)
        seen["thread"] = threading.current_thread().name
        return {}

    def boom():
        raise RuntimeError("source down")

    monkeypatch.setattr(cockpit_service, "get_dashboard_payload", fake_dashboard)
    monkeypatch.setattr(cockpit_service, "get_capital_state", lambda **_kwargs: boom())
    monkeypatch.setattr(cockpit_service, "_read_pre_earnings_cache", lambda: {})
    monkeypatch.setattr(cockpit_service, "get_execution_control", lambda _config: {})
    monkeypatch.setattr(cockpit_service, "list_ai_providers", lambda _root: [])
    monkeypatch.setattr(cockpit_service, "get_strategy_performance", lambda: {"entries": []})
    monkeypatch.setattr(cockpit_service, "build_control_plane_payload", lambda **_kwargs: {})
    monkeypatch.setattr(cockpit_service, "snapshot_agent_runtimes", lambda **_kwargs: {})
    monkeypatch.setattr(cockpit_service, "is_engine_running", lambda: False)
    captured = {}
    monkeypatch.setattr(cockpit_service, "build_cockpit_payload", lambda **kwargs: captured.update(kwargs) or {"state": "SUCCESS"})

    result = {}
    worker = threading.Thread(
        target=lambda: result.update(payload=contextvars.Context().run(cockpit_service.get_cockpit_payload)),
    )
    worker.start()
    worker.join(30)

    assert result["payload"] == {"state": "SUCCESS"}
    assert seen["cache_only"] is True
    assert seen["thread"].startswith("cockpit-src")
    # One failing source is isolated and reported; the others still load.
    assert captured["source_errors"] == {"capital": "Capital data is unavailable."} or \
        "capital" in captured["source_errors"]
    assert captured["capital_payload"] == {}


def _api_payload():
    return {
        "schema_version": 1, "state": "SUCCESS", "generated_at": "2026-11-15T07:56:00+00:00",
        "header": {}, "sections": {}, "strategy_lab": {}, "decision_ledger": {},
        "research_factory": {}, "system_health": {}, "source_errors": {}, "deferred": {},
    }


def test_cached_cockpit_snapshot_is_served_as_bytes_with_embedded_version(client):
    import api.main as main

    snapshot = main._cockpit_encode_snapshot(_api_payload())
    assert snapshot.startswith(main._COCKPIT_VERSION_PREFIX)
    version = main._cockpit_snapshot_version(snapshot)
    assert version == main._cockpit_stable_version(_api_payload())

    with patch("api.main._endpoint_payload_get", return_value=snapshot), \
         patch("api.main.get_cockpit_payload") as build, \
         patch("api.main._cockpit_stable_version") as rehash:
        full = client.get("/api/cockpit")
        unchanged = client.get(f"/api/cockpit?if_version={version}")

    assert full.status_code == 200
    assert full.content == snapshot
    assert full.json()["cockpit_state_version"] == version
    assert unchanged.json() == {"unchanged": True, "version": version}
    build.assert_not_called()
    rehash.assert_not_called()  # no 1 MB decode + hash per poll


def test_cold_cockpit_request_joins_the_running_build_instead_of_duplicating_it():
    import api.main as main

    built = main._cockpit_encode_snapshot(_api_payload())
    with patch("api.main._endpoint_payload_get_stale", return_value=(built, 1.0)), \
         patch("api.main.get_cockpit_payload") as build:
        # Cold request (not_before=0): the warm-up finished while it waited for the lock.
        assert main._cockpit_build_snapshot(not_before=0.0) == built
        build.assert_not_called()

    with patch("api.main._endpoint_payload_get_stale", return_value=(built, 1.0)), \
         patch("api.main._endpoint_payload_set") as cache_set, \
         patch("api.main.get_cockpit_payload", return_value=_api_payload()) as build:
        # fresh=true only accepts a build that started after the request arrived.
        fresh = main._cockpit_build_snapshot(not_before=time.time() + 5)
        build.assert_called_once_with()
        cache_set.assert_called_once()
    assert json.loads(fresh)["state"] == "SUCCESS"


# --- Home dashboard strip: profit factor arithmetic --------------------------

def test_dashboard_profit_factor_uses_percent_returns_not_raw_price_differences():
    """Live evidence: five closed signals on coins priced from 0.0009 to 72 USD
    produced "profit factor 1551.49" because per-unit price moves were summed."""
    closed_at = datetime.now(timezone.utc).isoformat()
    closed = [
        {"ticker": "XTZ-USD", "action": "SHORT", "status": "WIN", "entry": 0.3376, "exit_price": 0.3049, "sl": 0.351104, "pnl_pct": 9.0665},
        {"ticker": "DASH-USD", "action": "SHORT", "status": "WIN", "entry": 72.47, "exit_price": 51.33, "sl": 75.3688, "pnl_pct": 28.7677},
        {"ticker": "SAGA-USD", "action": "BUY", "status": "LOST", "entry": 0.03272, "exit_price": 0.01926, "sl": 0.0314112, "pnl_pct": -41.1729},
        {"ticker": "MARSCOIN-USD", "action": "SHORT", "status": "WIN", "entry": 0.1391, "exit_price": 0.0765, "sl": 0.144664, "pnl_pct": 45.4351},
        {"ticker": "IOST-USD", "action": "BUY", "status": "LOST", "entry": 0.000882, "exit_price": 0.000655, "sl": 0.00084672},
    ]
    for row in closed:
        row.update({"closed_at": closed_at, "created_at": closed_at, "outcome_data_valid": True})
    health = dashboard_service._compute_dashboard_strategy_health({"open": {}, "closed": closed})

    winners = 9.0665 + 28.7677 + 45.4351
    losers = 41.1729 + (0.000882 - 0.000655) / 0.000882 * 100.0
    assert health["profit_factor_30d"] == round(winners / losers, 2)
    assert health["profit_factor_30d"] < 2
    assert health["closed_signals_30d"] == 5
    assert health["win_rate_30d"] == 60.0


# --- Coding Agent tab: counters must match what is listed -------------------

def test_repair_view_lists_every_waiting_proposal_and_counts_owner_todos_once(tmp_path):
    from core.development_factory.watchdog import CaseDraft, RepairCaseStore, repair_agent_view

    store = RepairCaseStore(tmp_path / "cases.json")
    proposal = store.enqueue(CaseDraft(
        fingerprint="fp-proposal", kind="IMPROVEMENT", source_agent="scout",
        title="Old proposal", problem="Bounded improvement", context={},
    ))
    store.update(proposal["case_id"], status="PROPOSED")
    for index in range(3):
        todo = store.enqueue(CaseDraft(
            fingerprint=f"fp-owner-{index}", kind="OWNER_ACTION", source_agent="runtime_monitor",
            title="SEC is NOT_CONFIGURED — owner action needed", problem="Set EDGAR_IDENTITY",
            context={"tile": "SEC"},
        ))
        store.update(todo["case_id"], status="OWNER_ACTION")
    for index in range(25):  # push the proposal out of the 20-case display window
        store.enqueue(CaseDraft(
            fingerprint=f"fp-code-{index}", kind="CODE", source_agent="code_quality_monitor",
            title=f"Static issue {index}", problem="p", context={},
        ))

    view = repair_agent_view(store, limit=20)
    assert len(view["cases"]) == 20
    assert all(case["title"] != "Old proposal" for case in view["cases"])
    assert [row["title"] for row in view["proposals"]] == ["Old proposal"]
    assert view["summary"]["proposals_waiting"] == 1
    assert len(view["owner_actions"]) == 1
    assert view["summary"]["owner_actions"] == 1
    assert view["summary"]["owner_action_cases"] == 3


# --- Process-wide slowdown: tracemalloc must be opt-in -----------------------

def test_tracemalloc_is_opt_in_and_memory_gauge_still_reports_rss():
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    script = (
        "import tracemalloc, json, core.observability as o; "
        "print(json.dumps({'tracing': tracemalloc.is_tracing(), 'memory': o.memory_snapshot()}))"
    )
    env = {key: value for key, value in os.environ.items() if key not in {"MS_TRACEMALLOC", "PYTHONTRACEMALLOC"}}
    default = json.loads(subprocess.run(
        [sys.executable, "-c", script], cwd=root, env=env, capture_output=True, text=True, timeout=60, check=True,
    ).stdout.strip().splitlines()[-1])
    assert default["tracing"] is False
    assert default["memory"]["tracemalloc_enabled"] is False
    assert default["memory"]["rss_bytes"] > 1_000_000
    assert "tracemalloc_current_bytes" not in default["memory"]

    opted_in = json.loads(subprocess.run(
        [sys.executable, "-c", script], cwd=root, env={**env, "MS_TRACEMALLOC": "1"},
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout.strip().splitlines()[-1])
    assert opted_in["tracing"] is True
    assert opted_in["memory"]["tracemalloc_current_bytes"] >= 0


# --- Strategy Lab: one lucky trade must not be rank 1 ------------------------

def test_leaderboard_ranks_sufficient_samples_before_single_trade_rows(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    sources = _sources(tmp_path)
    sources["strategy_performance"] = {"entries": [
        {"strategy_id": "MANUAL_CHART", "symbol": "AREN", "direction": "SHORT", "sample_size": 1, "expectancy_R": 11.46},
        {"strategy_id": "S_REAL", "symbol": "AAA", "direction": "LONG", "sample_size": 40, "expectancy_R": 0.3},
        {"strategy_id": "S_FLAT", "symbol": "BBB", "direction": "LONG", "sample_size": 25, "expectancy_R": 0.0},
        {"strategy_id": "S_NEG", "symbol": "CCC", "direction": "LONG", "sample_size": 30, "expectancy_R": -0.2},
    ]}
    lab = build_cockpit_payload(**sources)["strategy_lab"]
    assert [row["strategy_id"] for row in lab["leaderboard"]] == ["S_REAL", "S_FLAT", "S_NEG", "MANUAL_CHART"]
    assert lab["leaderboard"][-1]["sample_sufficient"] is False
    assert lab["leaderboard_min_sample"] == 10
