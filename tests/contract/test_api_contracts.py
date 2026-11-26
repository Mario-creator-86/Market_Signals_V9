"""
tests/contract/test_api_contracts.py — Stage 5: API Contract Tests

These tests verify that the API payload SHAPES remain stable (no breaking changes).
They do NOT require a running server — they use TestClient from Starlette/FastAPI.

Each test validates:
  - Status code 200
  - Required top-level keys present
  - Types correct on critical fields
  - No breaking removal of existing fields

 Coverage: /api/state, /api/signals, /api/agents, /api/agents/control-plane,
 /api/execution/control,
          /api/health/diagnostics,
          /api/news, /api/earnings, /api/config
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

os.environ["TEST_MODE"] = "1"
os.environ["MS_DISABLE_ENGINE_AUTOSTART"] = "1"
os.environ["MS_STATE_FLUSH_SEC"] = "0.1"

# ── lazy import of FastAPI app (avoids loading engine threads on import) ──
@pytest.fixture(scope="module")
def client():
    try:
        from fastapi.testclient import TestClient
        from api.main import app
        return TestClient(app, raise_server_exceptions=False)
    except Exception as exc:
        pytest.skip(f"Cannot load FastAPI app: {exc}")


# ---------------------------------------------------------------------------
# /api/health (lightweight — always available)
# ---------------------------------------------------------------------------
class TestHealthContract:
    def test_health_200(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200

    def test_health_has_status_key(self, client):
        r = client.get("/api/health")
        data = r.json()
        assert "status" in data

    def test_diagnostics_200(self, client):
        r = client.get("/api/health/diagnostics")
        assert r.status_code == 200

    def test_diagnostics_has_latency(self, client):
        r = client.get("/api/health/diagnostics")
        data = r.json()
        assert "latency" in data, f"Missing 'latency' in diagnostics: {list(data.keys())}"

    def test_diagnostics_has_errors(self, client):
        r = client.get("/api/health/diagnostics")
        data = r.json()
        assert "errors" in data

    def test_diagnostics_has_cache_hit_rates(self, client):
        r = client.get("/api/health/diagnostics")
        data = r.json()
        assert "cache_hit_rates" in data


# ---------------------------------------------------------------------------
# /api/state
# ---------------------------------------------------------------------------
class TestStateContract:
    def test_state_200(self, client):
        r = client.get("/api/state")
        assert r.status_code == 200

    def test_state_has_as_of(self, client):
        r = client.get("/api/state")
        data = r.json()
        assert "as_of" in data or "state_version" in data, (
            f"Missing 'as_of' or 'state_version' in /api/state: {list(data.keys())}"
        )

    def test_state_as_of_is_string(self, client):
        r = client.get("/api/state")
        data = r.json()
        if "as_of" in data:
            assert isinstance(data["as_of"], str)


# ---------------------------------------------------------------------------
# /api/signals
# ---------------------------------------------------------------------------
class TestSignalsContract:
    def test_signals_200(self, client):
        r = client.get("/api/signals")
        assert r.status_code == 200

    def test_signals_has_open_and_closed(self, client):
        r = client.get("/api/signals")
        data = r.json()
        assert "open" in data, f"Missing 'open' in /api/signals: {list(data.keys())}"
        assert "closed" in data, f"Missing 'closed' in /api/signals: {list(data.keys())}"

    def test_signals_open_is_dict(self, client):
        r = client.get("/api/signals")
        data = r.json()
        assert isinstance(data["open"], dict)

    def test_signals_closed_is_list(self, client):
        r = client.get("/api/signals")
        data = r.json()
        assert isinstance(data["closed"], list)

    def test_signals_has_counts(self, client):
        r = client.get("/api/signals")
        data = r.json()
        assert "open_count" in data
        assert "closed_count" in data
        assert isinstance(data["open_count"], int)
        assert isinstance(data["closed_count"], int)


# ---------------------------------------------------------------------------
# /api/agents
# ---------------------------------------------------------------------------
class TestAgentsContract:
    def test_agents_200(self, client):
        r = client.get("/api/agents")
        assert r.status_code == 200

    def test_agents_has_agents_key(self, client):
        r = client.get("/api/agents")
        data = r.json()
        assert "agents" in data

    def test_agents_value_is_dict(self, client):
        r = client.get("/api/agents")
        data = r.json()
        assert isinstance(data["agents"], dict)

    def test_control_plane_is_bounded_and_actionable(self, client):
        response = client.get("/api/agents/control-plane")

        assert response.status_code == 200
        data = response.json()
        assert set(("as_of", "bot", "agents", "config")).issubset(data)
        assert isinstance(data["bot"]["running"], bool)
        assert isinstance(data["bot"]["engine_running"], bool)
        assert set(("execution", "risk")).issubset(data["config"]["agents"])
        assert data["agents"]
        for details in data["agents"].values():
            assert isinstance(details["enabled"], bool)
            assert isinstance(details["running"], bool)
            assert details["lifecycle_state"] in {
                "disabled",
                "stopped",
                "starting",
                "running",
                "error",
            }
            assert details["managed_by"] in {"api", "engine", "unknown"}

    def test_paper_execution_control_exposes_no_preset_owner_reduction(self, client):
        class Store:
            @staticmethod
            def get_reconciliation():
                return {"status": "HEALTHY", "conflicts": [], "checked_at": "now"}

        class Broker:
            store = Store()

        class ExecutionConfig:
            config = {
                "autonomy": {"mode": "ANALYZE", "kill_switch_active": False},
                "paper_execution": {"reduce_fraction_pct": None},
                "agents": {"execution": {"enabled": True, "mode": "paper"}},
            }

        with (
            patch("api.state_service.get_shared_config", return_value=ExecutionConfig()),
            patch("api.execution_service.get_paper_broker", return_value=Broker()),
        ):
            response = client.get("/api/execution/control")

        assert response.status_code == 200
        data = response.json()
        assert set(("mode", "selectable_modes", "kill_switch_active", "readiness", "reconciliation")).issubset(data)
        assert data["reduce_fraction_pct"] is None
        assert data["live_execution_available"] is False
        assert "PAPER_AUTO" in data["selectable_modes"]

    @staticmethod
    def _risk_config():
        class RiskConfig:
            def __init__(self):
                self.agents = {
                    "risk": {
                        "enabled": True,
                        "max_daily_loss_pct": 1.0,
                        "max_risk_per_trade_pct": 0.5,
                        "max_daily_loss_trades": 2,
                    }
                }
                self.bot = {
                    "circuit_breaker_daily_loss_pct": 1.0,
                    "circuit_breaker_drawdown_pct": 10.0,
                }
                self.calls = []

            def update_values(self, updates):
                self.calls.append(list(updates))
                root = {"agents": self.agents, "bot": self.bot}
                for path, value in updates:
                    target = root
                    for part in path[:-1]:
                        target = target[part]
                    target[path[-1]] = value

        return RiskConfig()

    @staticmethod
    def _constitution_payload(**overrides):
        payload = {
            "owner_confirmed": False,
            "max_risk_per_trade_pct": 0.4,
            "max_position_size_pct": 2.0,
            "max_total_exposure_pct": 20.0,
            "max_leverage": 2,
            "minimum_cash_pct": 30.0,
            "max_sector_exposure_pct": 10.0,
            "correlation_threshold": 0.75,
            "max_correlated_exposure_pct": 15.0,
            "max_positions": 6,
            "max_liquidity_participation_pct": 0.5,
            "max_daily_loss_pct": 2.0,
            "max_daily_loss_trades": 2,
            "max_drawdown_pct": 8.0,
            "max_data_age_seconds": 120,
            "block_data_conflicts": True,
            "require_approved_strategy": True,
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def _constitution_config():
        class ConstitutionConfig:
            def __init__(self):
                self.config = {
                    "risk_constitution": {
                        "owner_confirmed": False,
                        **{
                            name: None
                            for name in TestAgentsContract._constitution_payload()
                            if name != "owner_confirmed"
                        },
                    },
                    "agents": {"risk": {}, "execution": {}},
                    "bot": {},
                    "strategy_v2": {},
                }
                self.calls = []

            @property
            def agents(self):
                return self.config["agents"]

            @property
            def bot(self):
                return self.config["bot"]

            @property
            def strategy_v2(self):
                return self.config["strategy_v2"]

            def update_values(self, updates):
                self.calls.append(list(updates))
                for path, value in updates:
                    target = self.config
                    for part in path[:-1]:
                        target = target.setdefault(part, {})
                    target[path[-1]] = value

        return ConstitutionConfig()

    def test_legacy_risk_settings_patch_requires_canonical_policy(self, client):
        config = self._risk_config()
        with patch("api.state_service.get_shared_config", return_value=config):
            response = client.patch(
                "/api/agents/risk/settings",
                json={"max_daily_loss_pct": 10, "max_risk_per_trade_pct": 2},
            )

        assert response.status_code == 422
        assert "complete Risk Manager policy" in response.json()["detail"]
        assert config.calls == []

    @pytest.mark.parametrize(
        "payload",
        [
            {"max_daily_loss_pct": 10.1, "max_risk_per_trade_pct": 0.5},
            {"max_daily_loss_pct": 1, "max_risk_per_trade_pct": 2},
            {"max_daily_loss_pct": "10", "max_risk_per_trade_pct": 0.5},
            {"max_daily_loss_pct": True, "max_risk_per_trade_pct": 0.5},
            {"max_daily_loss_pct": 10, "max_risk_per_trade_pct": 2, "extra": 1},
        ],
    )
    def test_risk_settings_patch_rejects_invalid_payload_without_writes(self, client, payload):
        config = self._risk_config()
        with patch("api.state_service.get_shared_config", return_value=config):
            response = client.patch("/api/agents/risk/settings", json=payload)

        assert response.status_code == 422
        assert config.calls == []

    def test_risk_constitution_draft_is_saved_without_activation(self, client):
        config = self._constitution_config()
        with patch("api.state_service.get_shared_config", return_value=config):
            response = client.patch(
                "/api/agents/risk/constitution",
                json=self._constitution_payload(max_positions=None),
            )

        assert response.status_code == 200
        data = response.json()
        assert data["mode"] == "draft"
        assert data["applied_live"] is False
        assert data["constitution"]["status"] == "draft"
        assert config.config["risk_constitution"]["owner_confirmed"] is False
        assert len(config.calls) == 1
        assert len(config.calls[0]) == 1

    def test_risk_constitution_confirmation_applies_owner_values(self, client):
        config = self._constitution_config()
        with patch("api.state_service.get_shared_config", return_value=config):
            response = client.patch(
                "/api/agents/risk/constitution",
                json=self._constitution_payload(owner_confirmed=True),
            )

        assert response.status_code == 200
        data = response.json()
        assert data["mode"] == "active"
        assert data["applied_live"] is True
        assert data["constitution"]["active"]["max_positions"] == 6
        assert config.agents["execution"]["max_open_trades"] == 6
        assert config.bot["circuit_breaker_daily_loss_pct"] == 2.0
        assert len(config.calls) == 1

    def test_risk_manager_confirmation_applies_nested_trade_policy_in_same_write(self, client):
        from api.agents_service import get_risk_constitution_settings

        config = self._constitution_config()
        policy = get_risk_constitution_settings(config)["trade_policy"]["draft"]
        policy["reduce_fraction_pct"] = 20.0
        payload = self._constitution_payload(owner_confirmed=True)
        payload["trade_policy"] = policy
        with patch("api.state_service.get_shared_config", return_value=config):
            response = client.patch("/api/agents/risk/constitution", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert len(config.calls) == 1
        assert config.config["risk_trade_policy"]["owner_confirmed"] is True
        assert data["constitution"]["trade_policy"]["owner"] == "RISK_MANAGER"
        assert data["constitution"]["trade_policy"]["runtime_drift"] == []

    @pytest.mark.parametrize(
        "overrides",
        [
            {"owner_confirmed": True, "max_positions": None},
            {"owner_confirmed": True, "max_leverage": 6},
            {"owner_confirmed": True, "max_leverage": "2"},
            {"owner_confirmed": True, "block_data_conflicts": "true"},
            {"owner_confirmed": True, "extra": 1},
            {"owner_confirmed": True, "max_drawdown_pct": "__remove__"},
        ],
    )
    def test_risk_constitution_rejects_invalid_payload_without_writes(self, client, overrides):
        config = self._constitution_config()
        payload = self._constitution_payload(**overrides)
        for name, value in list(payload.items()):
            if value == "__remove__":
                payload.pop(name)
        with patch("api.state_service.get_shared_config", return_value=config):
            response = client.patch(
                "/api/agents/risk/constitution",
                json=payload,
            )

        assert response.status_code == 422
        assert config.calls == []


# ---------------------------------------------------------------------------
# /api/news
# ---------------------------------------------------------------------------
class TestNewsContract:
    def test_news_200(self, client):
        r = client.get("/api/news")
        assert r.status_code == 200

    def test_news_has_items(self, client):
        r = client.get("/api/news")
        data = r.json()
        assert "items" in data or "market_news" in data, (
            f"Missing 'items' or 'market_news' in /api/news"
        )

    def test_news_total_items_is_int(self, client):
        r = client.get("/api/news")
        data = r.json()
        if "total_items" in data:
            assert isinstance(data["total_items"], int)


# ---------------------------------------------------------------------------
# /api/sec/{ticker}
# ---------------------------------------------------------------------------
class TestSECContract:
    def test_sec_context_is_bounded_even_when_identity_is_unconfigured(self, client):
        r = client.get("/api/sec/AAPL")
        assert r.status_code == 200
        data = r.json()
        assert data["ticker"] == "AAPL"
        assert data["status"] in {"available", "unavailable"}
        assert isinstance(data["filings"], list)
        assert isinstance(data["total_filings"], int)

    def test_sec_context_rejects_invalid_ticker(self, client):
        r = client.get("/api/sec/INVALID%20TICKER")
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# /api/earnings
# ---------------------------------------------------------------------------
class TestEarningsContract:
    def test_earnings_200(self, client):
        r = client.get("/api/earnings")
        assert r.status_code == 200

    def test_earnings_has_earnings_key(self, client):
        r = client.get("/api/earnings")
        data = r.json()
        assert "earnings" in data

    def test_earnings_list(self, client):
        r = client.get("/api/earnings")
        data = r.json()
        assert isinstance(data["earnings"], list)

    def test_earnings_has_days(self, client):
        r = client.get("/api/earnings")
        data = r.json()
        assert "days" in data
        assert isinstance(data["days"], int)


# ---------------------------------------------------------------------------
# /api/config
# ---------------------------------------------------------------------------
class TestConfigContract:
    def test_config_get_200(self, client):
        r = client.get("/api/config")
        assert r.status_code == 200

    def test_config_is_dict(self, client):
        r = client.get("/api/config")
        data = r.json()
        assert isinstance(data, dict)

    def test_generic_config_patch_cannot_override_risk_manager_policy(self, client):
        response = client.post(
            "/api/config",
            json={"config": {"agents": {"execution": {"balance_usage_pct": 12}}}},
        )
        assert response.status_code == 409
        assert "owned by Risk Manager" in response.json()["detail"]


# ---------------------------------------------------------------------------
# /api/calendar/state
# ---------------------------------------------------------------------------
class TestCalendarStateContract:
    def test_calendar_state_200(self, client):
        r = client.get("/api/calendar/state")
        assert r.status_code == 200

    def test_calendar_state_has_disabled(self, client):
        r = client.get("/api/calendar/state")
        data = r.json()
        assert "disabled" in data


# ---------------------------------------------------------------------------
# /api/scanner/state
# ---------------------------------------------------------------------------
class TestScannerStateContract:
    @staticmethod
    def _dummy_agent():
        class DummyAgent:
            def get_state(self):
                return {
                    "hot_tickers": [],
                    "refreshing": False,
                    "status": {},
                    "flags": {"data_available": False},
                }

            def request_refresh(self):
                return {"status": "accepted"}

        return DummyAgent()

    def test_scanner_state_200(self, client):
        with patch("agents.scanner_agent.get_scanner_agent", return_value=self._dummy_agent()):
            r = client.get("/api/scanner/state")
        assert r.status_code == 200

    def test_scanner_state_has_hot_tickers(self, client):
        with patch("agents.scanner_agent.get_scanner_agent", return_value=self._dummy_agent()):
            r = client.get("/api/scanner/state")
        data = r.json()
        assert "hot_tickers" in data
        assert isinstance(data["hot_tickers"], list)


# ---------------------------------------------------------------------------
# X-Request-ID header propagation
# ---------------------------------------------------------------------------
class TestRequestIdContract:
    def test_request_id_echoed_back(self, client):
        """If client sends X-Request-ID, server should echo it back."""
        sent_id = "test-contract-abc123"
        r = client.get("/api/health", headers={"X-Request-ID": sent_id})
        returned = r.headers.get("x-request-id") or r.headers.get("X-Request-ID")
        assert returned == sent_id, (
            f"Expected X-Request-ID={sent_id!r} echoed back, got {returned!r}"
        )

    def test_server_generates_request_id_when_absent(self, client):
        """Server must always return an X-Request-ID in the response."""
        r = client.get("/api/health")
        returned = r.headers.get("x-request-id") or r.headers.get("X-Request-ID")
        assert returned is not None and len(returned) > 0, (
            "Server did not return X-Request-ID header"
        )


# ---------------------------------------------------------------------------
# /api/performance/quality
# ---------------------------------------------------------------------------
class TestPerformanceQualityContract:
    def test_performance_quality_200(self, client):
        r = client.get("/api/performance/quality")
        assert r.status_code == 200

    def test_performance_quality_has_top_level_keys(self, client):
        r = client.get("/api/performance/quality")
        data = r.json()
        assert "global" in data
        assert "breakdown" in data
        assert "active_alerts" in data
        assert "alert_thresholds" in data

    def test_performance_quality_global_has_windows(self, client):
        r = client.get("/api/performance/quality")
        data = r.json()
        glob = data.get("global", {})
        assert "7d" in glob
        assert "30d" in glob
        assert "all_time" in glob

    def test_performance_quality_metrics_types(self, client):
        r = client.get("/api/performance/quality")
        data = r.json()
        w30 = data.get("global", {}).get("30d", {})
        assert "win_rate" in w30
        assert "profit_factor" in w30
        assert "max_drawdown" in w30
        if w30["win_rate"] is not None:
            assert isinstance(w30["win_rate"], float)
        if w30["profit_factor"] is not None:
            assert isinstance(w30["profit_factor"], float)


# ---------------------------------------------------------------------------
# /api/pre-earnings/track-record — History / Track-Record panel (Task #11)
# ---------------------------------------------------------------------------
class TestPreEarningsTrackRecordContract:
    def test_track_record_200(self, client):
        r = client.get("/api/pre-earnings/track-record")
        assert r.status_code == 200

    def test_track_record_has_required_keys(self, client):
        data = client.get("/api/pre-earnings/track-record").json()
        for k in ("as_of_utc", "sample_size", "overall", "per_verdict",
                  "per_direction", "per_ai_verdict", "per_band", "per_ai_band",
                  "top_predictions", "outcomes_recent", "ai_coverage"):
            assert k in data, f"Missing '{k}' in /api/pre-earnings/track-record"

    def test_track_record_per_verdict_shape(self, client):
        data = client.get("/api/pre-earnings/track-record").json()
        pv = data["per_verdict"]
        assert isinstance(pv, dict)
        for v in ("LONG", "SHORT", "NEUTRAL"):
            assert v in pv, f"Missing verdict bucket '{v}'"
            d = pv[v]
            for k in ("n", "hits", "hit_rate", "avg_realized_move"):
                assert k in d, f"Missing '{k}' in per_verdict.{v}"

    def test_track_record_per_band_is_list(self, client):
        data = client.get("/api/pre-earnings/track-record").json()
        assert isinstance(data["per_band"], list)
        assert len(data["per_band"]) == 4, "expected 4 confidence bands"

    def test_track_record_outcomes_recent_is_list(self, client):
        data = client.get("/api/pre-earnings/track-record").json()
        assert isinstance(data["outcomes_recent"], list)

    def test_track_record_no_raw_nan_in_body(self, client):
        # The endpoint must never emit a literal NaN/Infinity (RFC 8259).
        body = client.get("/api/pre-earnings/track-record").text
        assert "NaN" not in body and "Infinity" not in body
