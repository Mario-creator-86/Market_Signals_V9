import json
from datetime import datetime, timedelta, timezone

import api.cockpit_service as cockpit_service
from api.cockpit_service import ASYNC_STATES, _reconcile_ledger_rows, build_cockpit_payload
from core.strategy_registry import StrategyRegistry


class FakeConfig:
    def __init__(self):
        self.config = {
            "risk_constitution": {"owner_confirmed": False},
            "sec": {"identity_env": "TEST_EDGAR_IDENTITY"},
            "ml": {"registry": []},
        }


def _sources(tmp_path):
    now = datetime(2026, 8, 13, 15, 0, tzinfo=timezone.utc)
    dashboard = {
        "dashboard_regime": {"regime": "risk-on", "sentiment": "bullish", "confidence": 72.0},
        "dashboard_market_narrative": {
            "available": True,
            "tone": "risk-on",
            "risk_regime": "risk-on",
            "generated_at": now.isoformat(),
            "top_catalysts": ["Verified catalyst"],
        },
        "dashboard_risk_flags": [
            {"severity": "high", "type": "NEWS_CRITICAL", "ticker": "AAA", "title": "Risk", "detail": "Verified detail"}
        ],
        "dashboard_news_items": [
            {"id": "n1", "ticker": "AAA", "title": "Major item", "source": "wire", "url": "https://example.com", "published_at": now.isoformat(), "impact": "high", "impact_score": 80, "sentiment": "neutral", "sentiment_quality": {"source": "finbert"}}
        ],
    }
    capital_payload = {
        "capital": {
            "balance": 8_000.0,
            "raw_cash_balance": 8_001.0,
            "positions": [
                {"position_id": "p1", "ticker": "AAA", "direction": "long", "status": "OPEN", "usd_size": 2_000.0, "margin_used": 1_000.0, "unrealized_pnl": 100.0, "unrealized_pnl_pct": 5.0, "entry_price": 10.0, "current_price": 10.5, "stop_loss_price": 9.5, "take_profit_price": 11.0, "strategy_type": "S1"}
            ],
            "history": [
                {"timestamp": now.isoformat(), "type": "CLOSE_POSITION", "amount": 20.0, "metadata": {"net_pnl": 18.0}}
            ],
            "win_rate": 55.0,
            "total_closed_trades": 20,
            "decisions": [
                {"event_id": "d1", "timestamp": now.isoformat(), "event_type": "position_opened", "status": "open", "ticker": "AAA", "position_id": "p1", "strategy_type": "S1", "summary": "Evidence-backed open"}
            ],
            "opportunities": {"signals": []},
        }
    }
    pre_earnings = {
        "items": [
            {
                "symbol": "BBB", "earnings_time_utc": "2026-08-15T12:00:00+00:00", "session": "BMO", "event_status": "UPCOMING",
                "trader_score": 81, "data_confidence": "HIGH", "coverage_score": 90, "expected_move_pct": 6.2,
                "historical_median_move_pct": 5.8, "historical_sample_size": 8, "direction_label": "LONG",
                "final_direction_bias_score": 44, "recommendation_eligible": True, "recommendation_blockers": [],
                "ollama": {"decision": "LONG", "decision_confidence": 0.7}, "flags": {"event_unverified": False, "already_reported": False},
                "why": ["Verified reason"], "what_to_watch": ["Verified watch"], "components": {"options": None},
            },
            {
                "symbol": "CCC", "earnings_time_utc": "2026-08-16T20:00:00+00:00", "session": "AMC", "event_status": "UPCOMING",
                "direction_label": "SHORT", "recommendation_eligible": False, "recommendation_blockers": ["coverage_below_threshold"],
                "flags": {"event_unverified": False, "already_reported": False}, "components": {},
            },
        ],
        "refresh_seconds": 600,
        "status": {
            "last_success_time": now.isoformat(),
            "last_error": None,
            "last_inputs_used": {"news_cache_age_seconds": 2, "earnings_cache_age_seconds": 30},
        },
    }
    agents = {
        "bot": {"engine_running": True},
        "agents": {
            "news_tab": {"enabled": True, "running": True, "ready": True, "last_success": now.isoformat(), "last_error": ""},
            "strategy_research": {"enabled": True, "running": True, "last_success": now.isoformat(), "last_error": ""},
        },
        "config": {"risk_constitution": {"status": "draft", "fingerprint": None}},
    }
    execution = {
        "mode": "ANALYZE", "kill_switch_active": False, "reduce_fraction_pct": 37.0,
        "readiness": {"ready": False, "reasons": ["owner must select PAPER_AUTO"]},
        "reconciliation": {"status": "HEALTHY", "conflicts": [], "checked_at": now.isoformat()},
        "live_execution_available": False,
    }
    providers = [{"name": "ollama", "enabled": True, "type": "ollama", "has_key": False}]
    performance = {
        "entries": [
            {"strategy_id": "S1", "symbol": "AAA", "direction": "LONG", "validation_status": "unverified", "sample_size": 20, "expectancy_R": 0.3, "profit_factor": 1.2, "max_drawdown": 4.0, "last_run": now.isoformat(), "walk_forward": {}}
        ]
    }
    data_health = {
        "streams": {
            "yf_prices": {"level": "VERIFIED", "source": "yahoo", "timestamp": now.isoformat()},
            "ollama_availability": {"level": "VERIFIED", "source": "ollama", "timestamp": now.isoformat(), "value": True},
        }
    }
    database = {"id": "database", "label": "Database", "status": "HEALTHY", "message": "Read check passed.", "checked_at": now.isoformat(), "evidence": {}}
    return {
        "config": FakeConfig(), "dashboard": dashboard, "capital_payload": capital_payload,
        "pre_earnings": pre_earnings, "agents": agents, "execution": execution,
        "providers": providers, "strategy_performance": performance, "data_health": data_health,
        "database": database, "registry": StrategyRegistry(tmp_path / "strategy_registry.json"), "now": now,
    }


def test_cockpit_preserves_owner_values_and_exposes_complete_evidence(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_EDGAR_IDENTITY", raising=False)
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.delenv("RD_AGENT_ENABLED", raising=False)
    payload = build_cockpit_payload(**_sources(tmp_path))

    assert payload["state"] == "SUCCESS"
    assert payload["header"]["reduce_fraction_pct"] == 37.0
    assert payload["header"]["autonomy"]["mode"] == "ANALYZE"
    assert payload["header"]["risk"]["status"] == "DRAFT"
    assert payload["header"]["capital"]["daily_realized_pnl"] == 18.0
    assert payload["header"]["capital"]["notional_exposure_pct"] == round(2_000 / 9_100 * 100, 4)

    earnings = payload["sections"]["upcoming_earnings"]["items"]
    assert set((
        "ticker", "earnings_date", "session", "score", "data_confidence", "expected_move_pct",
        "historical_move_pct", "iv_rv_ratio", "bias", "strategy", "ai_decision", "risk_status",
    )).issubset(earnings[0])
    assert earnings[0]["iv_rv_ratio"] is None
    assert earnings[0]["iv_rv_state"] == "UNAVAILABLE"
    assert earnings[0]["strategy"] == "PRE_EARNINGS_DIRECTIONAL"
    assert earnings[1]["strategy"] is None
    assert earnings[1]["risk_status"] == "BLOCKED"
    assert payload["source_freshness"]["pre_earnings"]["state"] == "FRESH"
    assert payload["sections"]["upcoming_earnings"]["source_freshness"]["calendar_age_seconds"] == 30

    assert payload["sections"]["opportunities"]["items"][0]["ticker"] == "BBB"
    assert "message" not in payload["sections"]["opportunities"]
    assert "message" not in payload["sections"]["risks"]
    for section in payload["sections"].values():
        assert section["state"] in ASYNC_STATES

    services = {item["id"]: item for item in payload["system_health"]["services"]}
    assert services["database"]["status"] == "HEALTHY"
    assert services["options"]["status"] == "UNAVAILABLE"
    assert services["sec"]["status"] == "NOT_CONFIGURED"
    assert services["live_broker"]["status"] == "NOT_CONFIGURED"
    assert services["mlflow"]["status"] == "NOT_CONFIGURED"
    assert services["rd_agent"]["status"] == "NOT_CONFIGURED"

    serialized = json.dumps(payload, allow_nan=False)
    assert "undefined" not in serialized
    assert "[object Object]" not in serialized


def test_empty_sources_remain_empty_or_unavailable_without_fake_numbers(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_EDGAR_IDENTITY", raising=False)
    source = _sources(tmp_path)
    source.update({
        "dashboard": {"dashboard_market_narrative": {"available": False}},
        "capital_payload": {"capital": {"positions": [], "history": [], "decisions": [], "opportunities": {"signals": []}}},
        "pre_earnings": {"items": [], "status": {}},
        "strategy_performance": {"entries": []},
        "data_health": {"streams": {}},
    })
    payload = build_cockpit_payload(**source)

    assert payload["header"]["regime"]["available"] is False
    assert payload["header"]["regime"]["value"] is None
    assert payload["header"]["capital"]["daily_realized_pnl"] is None
    assert payload["header"]["capital"]["notional_exposure_pct"] is None
    assert payload["sections"]["opportunities"]["state"] == "EMPTY"
    assert payload["sections"]["opportunities"]["message"] == (
        "No current setup clears the real Capital or Pre-Earnings eligibility gates."
    )
    assert payload["sections"]["upcoming_earnings"]["state"] == "EMPTY"
    assert payload["sections"]["sentiment_shifts"]["state"] == "EMPTY"


def test_cockpit_reconciles_accepted_ledger_snapshot_with_later_paper_fill():
    rows = [{
        "decision_id": "dec-vet",
        "timestamp": "2026-08-27T15:29:25+00:00",
        "ticker": "VET-USD",
        "status": "accepted",
        "what_happened": "OPEN_SHORT (accepted)",
        "signal_id": "signal-vet",
        "data_existed": {"signal_id": "signal-vet", "entry": 0.007165},
        "capital_allowed": {"invested_amount": 995.0, "usd_size": 0.0},
        "critic": {"available": False, "reason": "critic_not_run"},
        "executed": {},
    }]
    capital = {"positions": [{
        "position_id": "pos-vet",
        "ticker": "VET-USD",
        "direction": "SHORT",
        "status": "OPEN",
        "entry_price": 0.007152,
        "usd_size": 79.59984,
        "margin_used": 79.59984,
        "leverage": 1,
        "opened_at": "2026-08-27T18:29:36+03:00",
        "signal_id": "signal-vet",
        "decision_context": {
            "decision_id": "dec-vet",
            "signal_id": "signal-vet",
            "sizing": {"invested_amount": 79.59984, "usd_size": 79.59984, "leverage": 1},
            "execution": {"order_intent_id": "ord-vet"},
            "ai_critic": {"available": True, "veto": False, "policy_reason": "good_setup"},
        },
    }]}

    [row] = _reconcile_ledger_rows(rows, capital)

    assert row["decision_status"] == "accepted"
    assert row["status"] == "executed"
    assert row["what_happened"] == "OPEN_SHORT (executed)"
    assert row["position_id"] == "pos-vet"
    assert row["executed"]["state"] == "FILLED"
    assert row["executed"]["position_status"] == "OPEN"
    assert row["executed"]["entry_price"] == 0.007152
    assert row["capital_allowed"]["usd_size"] == 79.59984
    assert row["critic"]["available"] is True
    assert row["afterward"]["status"] == "POSITION_OPEN"


def test_cockpit_labels_critic_as_skipped_after_upstream_ai_veto():
    rows = [{
        "decision_id": "dec-policy-veto",
        "timestamp": "2026-08-28T14:51:46+00:00",
        "ticker": "BNTX",
        "status": "rejected",
        "what_happened": "OPEN_LONG (rejected)",
        "validation": {
            "ok": False,
            "stage": "POLICY",
            "reason": "POLICY: risk_constitution_veto",
        },
        "critic": {"available": False, "reason": "critic_not_run"},
    }]

    [row] = _reconcile_ledger_rows(rows, {"positions": [], "history": []})

    assert row["critic"] == {
        "available": False,
        "skipped": True,
        "reason": "skipped_after_ai_validation_veto",
        "upstream_stage": "POLICY",
        "upstream_reason": "POLICY: risk_constitution_veto",
    }


def test_cockpit_normalizes_legacy_no_trade_accepted_snapshot_to_rejected():
    rows = [{
        "decision_id": "dec-no-trade",
        "timestamp": "2026-08-27T16:35:42+00:00",
        "ticker": "VEEV",
        "status": "accepted",
        "what_happened": "NO_TRADE (accepted)",
        "signal_id": "signal-veev",
        "ai_model": {"available": False, "response_fallback_used": "invalid_json"},
        "executed": {},
    }]

    capital = {"positions": [{
        "position_id": "pos-new-veev",
        "ticker": "VEEV",
        "status": "OPEN",
        "signal_id": "signal-veev",
        "decision_context": {
            "decision_id": "dec-new-veev",
            "signal_id": "signal-veev",
        },
    }], "history": []}

    [row] = _reconcile_ledger_rows(rows, capital)

    assert row["decision_status"] == "accepted"
    assert row["status"] == "rejected"
    assert row["event_type"] == "decision_rejected"
    assert row["what_happened"] == "NO_TRADE (rejected)"
    assert not row.get("position_id")


def test_cockpit_does_not_join_reused_signal_id_across_different_decisions():
    rows = [{
        "decision_id": "dec-old",
        "ticker": "VEEV",
        "status": "accepted",
        "what_happened": "OPEN_SHORT (accepted)",
        "signal_id": "hourly-signal-veev",
        "executed": {},
    }]
    capital = {"positions": [{
        "position_id": "pos-new",
        "ticker": "VEEV",
        "direction": "SHORT",
        "status": "OPEN",
        "signal_id": "hourly-signal-veev",
        "decision_context": {
            "decision_id": "dec-new",
            "signal_id": "hourly-signal-veev",
        },
    }], "history": []}

    [row] = _reconcile_ledger_rows(rows, capital)

    assert row["status"] == "accepted"
    assert row["what_happened"] == "OPEN_SHORT (accepted)"
    assert not row.get("position_id")


def test_cockpit_keeps_executed_lifecycle_after_position_closes():
    rows = [{
        "decision_id": "dec-closed",
        "ticker": "VET-USD",
        "status": "accepted",
        "what_happened": "OPEN_SHORT (accepted)",
        "signal_id": "signal-closed",
        "capital_allowed": {"usd_size": 0.0},
        "executed": {},
    }]
    capital = {
        "positions": [],
        "history": [
            {
                "timestamp": "2026-08-27T18:29:36+03:00",
                "type": "OPEN_POSITION",
                "amount": -79.63,
                "metadata": {
                    "position_id": "pos-closed",
                    "ticker": "VET-USD",
                    "signal_id": "signal-closed",
                },
            },
            {
                "timestamp": "2026-08-27T19:00:00+03:00",
                "type": "CLOSE_POSITION",
                "metadata": {
                    "position_id": "pos-closed",
                    "ticker": "VET-USD",
                    "direction": "SHORT",
                    "entry_price": 0.007152,
                    "exit_price": 0.00695,
                    "net_pnl": 2.12,
                    "close_reason": "take_profit",
                },
            },
        ],
    }

    [row] = _reconcile_ledger_rows(rows, capital)

    assert row["status"] == "closed"
    assert row["what_happened"] == "OPEN_SHORT (closed)"
    assert row["executed"]["state"] == "FILLED"
    assert row["executed"]["position_status"] == "CLOSED"
    assert row["executed"]["exit_price"] == 0.00695
    assert row["afterward"]["status"] == "POSITION_CLOSED"
    assert row["afterward"]["realized_pnl"] == 2.12
    assert row["capital_allowed"]["allocated_usd"] == 79.63


def test_source_failures_are_sanitized_as_error_states(tmp_path):
    source = _sources(tmp_path)
    source["source_errors"] = {"pre_earnings": "Pre-Earnings data is unavailable."}
    payload = build_cockpit_payload(**source)

    assert payload["sections"]["upcoming_earnings"]["state"] == "ERROR"
    assert payload["sections"]["upcoming_earnings"]["message"] == "Pre-Earnings data is unavailable."
    assert "Traceback" not in json.dumps(payload)


def test_dao_exposes_exact_paper_auto_blocker_in_cockpit(tmp_path):
    source = _sources(tmp_path)
    source["execution"] = {
        "mode": "PAPER_AUTO",
        "kill_switch_active": False,
        "readiness": {"ready": True, "reasons": []},
        "reconciliation": {"status": "HEALTHY", "conflicts": []},
    }
    source["pre_earnings"]["items"] = [{
        "symbol": "DAO",
        "earnings_time_utc": "2026-08-13T12:00:00+00:00",
        "session": "BMO",
        "event_status": "UPCOMING",
        "event_source": "official_ir",
        "event_source_url": "https://ir.example.test/dao",
        "event_verified_at": "2026-08-13T10:00:00+00:00",
        "recommendation_eligible": True,
        "recommendation_blockers": [],
        "gap_risk": 48,
        "direction_bias_score": -26,
        "direction_label": "SHORT",
        "coverage_score": 77,
        "data_confidence": "MEDIUM",
        "flags": {"event_unverified": False, "already_reported": False},
    }]

    payload = build_cockpit_payload(**source)
    dao = payload["sections"]["upcoming_earnings"]["items"][0]

    assert dao["recommendation_status"] == "ELIGIBLE"
    assert dao["paper_auto_trace"]["status"] == "BLOCKED"
    assert dao["paper_auto_trace"]["first_blocker"] == {
        "code": "gap_risk_below_minimum",
        "message": "Gap-risk 48 is below the autonomous-entry minimum 65.",
        "actual": 48,
        "required": 65,
    }
    assert dao["autonomous_prefilter"]["metrics"]["candidate_score"] == 81.67
    stages = {
        row["name"]: row["status"]
        for row in dao["paper_auto_trace"]["stages"]
    }
    assert stages["AI + independent critic"] == "NOT_REACHED"


def test_cockpit_distinguishes_ranking_eligibility_from_open_entry_window(tmp_path):
    source = _sources(tmp_path)
    source["execution"] = {
        "mode": "PAPER_AUTO",
        "kill_switch_active": False,
        "readiness": {"ready": True, "reasons": []},
        "reconciliation": {"status": "HEALTHY", "conflicts": []},
    }
    source["pre_earnings"]["items"] = [{
        "symbol": "SIMPE",
        "earnings_time_utc": "2026-08-13T00:00:00+00:00",
        "session": "AMC",
        "event_status": "UPCOMING",
        "recommendation_eligible": True,
        "recommendation_blockers": [],
        "gap_risk": 85,
        "direction_bias_score": 35,
        "direction_label": "LONG",
        "coverage_score": 90,
        "data_confidence": "HIGH",
        "flags": {"event_unverified": False, "already_reported": False},
    }]

    source["now"] = datetime(2026, 8, 13, 16, 30, tzinfo=timezone.utc)
    waiting = build_cockpit_payload(**source)["sections"]["upcoming_earnings"]["items"][0]
    assert waiting["ranking_eligible"] is True
    assert waiting["executable_now"] is False
    assert waiting["paper_auto_trace"]["status"] == "WAITING_FOR_ENTRY_WINDOW"
    assert waiting["paper_auto_trace"]["first_blocker"]["code"] == "outside_pre_earnings_window"
    stages = {row["name"]: row["status"] for row in waiting["paper_auto_trace"]["stages"]}
    assert stages["Autonomous prefilter"] == "PASS"
    assert stages["Pre-Earnings entry window"] == "WAITING"
    assert stages["AI + independent critic"] == "NOT_REACHED"

    source["now"] = datetime(2026, 8, 13, 18, 30, tzinfo=timezone.utc)
    ready = build_cockpit_payload(**source)["sections"]["upcoming_earnings"]["items"][0]
    assert ready["executable_now"] is True
    assert ready["paper_auto_trace"]["status"] == "READY_FOR_AI_RISK_CAPITAL"
    stages = {row["name"]: row["status"] for row in ready["paper_auto_trace"]["stages"]}
    assert stages["Pre-Earnings entry window"] == "PASS"
    assert stages["AI + independent critic"] == "PENDING"


def test_in_memory_pre_earnings_snapshot_is_sanitized_after_agent_initialization(monkeypatch):
    now = datetime.now(timezone.utc)
    state = {
        "items": [{
            "symbol": "DAO",
            "earnings_time_utc": now.isoformat(),
            "session": "BMO",
            "event_status": "UPCOMING",
            "flags": {"already_reported": False},
            "top_news": [{
                "ticker": "MARKET",
                "title": "Youdao Reports Second Quarter 2026 Unaudited Financial Results",
                "summary": "Youdao (NYSE: DAO) today announced its financial results.",
                "published_at": (now - timedelta(hours=1)).isoformat(),
                "_pe_relevance": 1.0,
            }],
        }],
        "reported_today": [],
    }

    class InMemoryAgent:
        def get_state(self):
            return state

    class Config:
        config = {"pre_earnings": {"reported_retention_seconds": 86_400}}

    monkeypatch.setattr(
        "agents.pre_earnings_agent.get_pre_earnings_agent_if_ready",
        lambda: InMemoryAgent(),
    )
    monkeypatch.setattr(cockpit_service, "get_shared_config", lambda: Config())

    payload = cockpit_service._read_pre_earnings_cache()

    assert payload["items"] == []
    assert payload["reported_today"][0]["symbol"] == "DAO"
    assert payload["reported_today"][0]["reason"] == "ALREADY_REPORTED_EVIDENCE"


def test_undated_candidates_remain_visible_but_are_not_presented_as_confirmed(tmp_path):
    source = _sources(tmp_path)
    source["pre_earnings"]["items"].append({
        "symbol": "OLD",
        "earnings_time_utc": None,
        "event_status": "UNKNOWN",
        "flags": {"already_reported": False},
    })

    payload = build_cockpit_payload(**source)

    section = payload["sections"]["upcoming_earnings"]
    rows = {row["ticker"]: row for row in section["items"]}

    assert "OLD" in rows
    assert rows["OLD"]["schedule_status"] == "DATE_UNAVAILABLE"
    assert section["schedule_counts"]["date_unavailable"] == 1
    assert section["schedule_counts"]["analyzed_candidates"] == 3
    assert section["schedule_counts"]["calendar_reporters"] == 3


def test_system_health_overall_ignores_data_gaps_but_flags_real_failures(tmp_path, monkeypatch):
    """§98 aggregate semantics: DEGRADED = an ACTIVE component is broken or
    unverifiable. Capability/data gaps (UNAVAILABLE: options evidence, ML
    model) and deliberately NOT_CONFIGURED optional services stay visible
    per-tile and in coverage_gaps — they are not system failures."""
    monkeypatch.delenv("TEST_EDGAR_IDENTITY", raising=False)
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.delenv("RD_AGENT_ENABLED", raising=False)
    payload = build_cockpit_payload(**_sources(tmp_path))
    health = payload["system_health"]
    services = {item["id"]: item for item in health["services"]}

    # Real capability gaps remain honestly per-tile...
    assert services["options"]["status"] == "UNAVAILABLE"
    assert services["ml_model"]["status"] == "UNAVAILABLE"
    # ...and are surfaced as coverage gaps, not system failures.
    gap_ids = {gap["id"] for gap in health["coverage_gaps"]}
    assert {"options", "ml_model"} <= gap_ids
    # Every core component is healthy -> the aggregate is HEALTHY.
    for core in ("backend", "database", "scheduler", "paper_broker", "news", "ai_providers"):
        assert services[core]["status"] == "HEALTHY", core
    assert health["overall"] == "HEALTHY"


def test_system_health_degrades_on_unverifiable_configured_service(tmp_path, monkeypatch):
    """A configured-but-unverifiable service (SEC identity set, no cached
    read) must still degrade the aggregate — UNVERIFIED is a real problem."""
    monkeypatch.setenv("TEST_EDGAR_IDENTITY", "Owner owner@example.com")
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.delenv("RD_AGENT_ENABLED", raising=False)
    payload = build_cockpit_payload(**_sources(tmp_path))
    services = {item["id"]: item for item in payload["system_health"]["services"]}
    assert services["sec"]["status"] == "UNVERIFIED"
    assert payload["system_health"]["overall"] == "DEGRADED"


def test_audit_F1_market_provider_derives_from_shared_freshness(tmp_path, monkeypatch):
    """F1: with NO local quality stream (engine-subprocess deployment), the
    market tile derives evidence from the shared Pre-Earnings freshness —
    never a false UNAVAILABLE while the data path demonstrably delivers."""
    sources = _sources(tmp_path)
    # Simulate the engine-subprocess case: no quality streams registered.
    sources["data_health"] = {"streams": {}}
    monkeypatch.delenv("TEST_EDGAR_IDENTITY", raising=False)
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.delenv("RD_AGENT_ENABLED", raising=False)
    payload = build_cockpit_payload(**sources)
    services = {item["id"]: item for item in payload["system_health"]["services"]}
    assert services["market_provider"]["status"] == "HEALTHY"
    assert services["market_provider"]["evidence"]["quality"].startswith("DERIVED_")


def test_audit_F3_rollback_history_from_promotion_records(tmp_path, monkeypatch):
    """F3: rollback history surfaces Phase 16 ROLLED_BACK promotions."""
    monkeypatch.setenv("MS_PROMOTION_RECORDS_PATH", str(tmp_path / "promo.json"))
    from core.promotion.records import PromotionRecordStore

    store = PromotionRecordStore(tmp_path / "promo.json")
    record = store.append(
        experiment_id="exp_1", strategy_id="ROLLME", previous_version="v1",
        new_version="v2", promotion_evidence={},
        rollback_conditions=[{"kind": "expectancy_r_below", "threshold": 0.0}],
    )
    store.mark_rolled_back(record.promotion_id, reason="breach")
    monkeypatch.delenv("TEST_EDGAR_IDENTITY", raising=False)
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.delenv("RD_AGENT_ENABLED", raising=False)
    payload = build_cockpit_payload(**_sources(tmp_path))
    rollbacks = payload["strategy_lab"]["rollback_history"]
    assert any(r["action"] == "ROLLED_BACK" and r["strategy_id"] == "ROLLME"
               and r["detail"]["restored_version"] == "v1" for r in rollbacks)
    monkeypatch.delenv("MS_PROMOTION_RECORDS_PATH", raising=False)


def test_audit_F12_autonomy_marks_unevaluated_on_execution_failure(tmp_path, monkeypatch):
    """F12: when the execution source fails, readiness is unknown — the header
    must say evaluated=false so the UI never claims '0 blockers'."""
    monkeypatch.delenv("TEST_EDGAR_IDENTITY", raising=False)
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.delenv("RD_AGENT_ENABLED", raising=False)
    sources = _sources(tmp_path)
    sources["execution"] = {}  # simulate _safe_source failure shape
    payload = build_cockpit_payload(**sources)
    assert payload["header"]["autonomy"]["evaluated"] is False


def test_audit_F13_score_zero_is_not_treated_as_missing(tmp_path, monkeypatch):
    """F13: a legitimate score of exactly 0 must survive the or-chain."""
    from api.cockpit_service import _finite

    def first_present(item):
        return _finite(next((item.get(k) for k in ("score", "signal_score") if item.get(k) is not None), None))
    assert first_present({"score": 0}) == 0.0
    assert first_present({"signal_score": 0}) == 0.0
    assert first_present({"score": None, "signal_score": 7}) == 7.0
    assert first_present({}) is None
