
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

class TestAPI:
    def test_get_state(self, client: TestClient):
        response = client.get("/api/state")
        assert response.status_code == 200
        data = response.json()
        assert "tickers" in data
        assert "signals" in data

    def test_capital_state_includes_signals_decisions_and_opportunities(self, client: TestClient):
        expected = {
            "refresh_interval_ms": 5000,
            "capital": {
                "positions": [{"position_id": "pos-1", "status": "OPEN"}],
                "decisions": [{"event_id": "decision-1"}],
                "opportunities": {"signals": [{"ticker": "BTC-USD"}]},
            },
        }
        with patch("api.main._endpoint_payload_get", return_value=None), \
             patch("api.main._endpoint_payload_set"), \
             patch("api.main.get_capital_state", return_value=expected) as capital_state:
            response = client.get("/api/state?include=capital&live_prices=false")

        assert response.status_code == 200
        assert response.json()["capital"] == expected["capital"]
        capital_state.assert_called_once_with(
            live_prices=False,
            include_opportunities=True,
            include_decisions=True,
        )

    def test_capital_state_remote_quote_fanout_is_opt_in(self, client: TestClient, monkeypatch):
        monkeypatch.delenv("MS_CAPITAL_STATE_REMOTE_QUOTES", raising=False)
        with patch("api.main._endpoint_payload_get", return_value=None), \
             patch("api.main._endpoint_payload_set"), \
             patch("api.main.get_capital_state", return_value={"capital": {}}) as capital_state:
            response = client.get("/api/state?include=capital&live_prices=true")

        assert response.status_code == 200
        capital_state.assert_called_once_with(
            live_prices=False,
            include_opportunities=True,
            include_decisions=True,
        )

    def test_capital_equity_curve_uses_gross_mark_to_market_and_excludes_research(self, client: TestClient):
        class DummyCapitalManager:
            def get_equity_curve(self, limit=90):
                return []

            def get_deployable_balance(self):
                return 900.0

            def get_positions(self):
                return [
                    {
                        "status": "OPEN",
                        "margin_used": 100.0,
                        "unrealized_pnl_gross": 15.0,
                        "unrealized_pnl": 12.0,
                    },
                    {
                        "status": "OPEN",
                        "research_only": True,
                        "margin_used": 500.0,
                        "unrealized_pnl_gross": 250.0,
                    },
                ]

        with patch(
            "core.capital_manager.get_capital_manager",
            return_value=DummyCapitalManager(),
        ):
            response = client.get("/api/capital/equity-curve")

        assert response.status_code == 200
        current = response.json()["current"]
        assert current["total_equity"] == 1015.0
        assert current["deployable_cash"] == 900.0
        assert current["open_margin"] == 100.0
        assert current["mark_to_market_pnl"] == 15.0
        assert current["pnl_basis"] == "gross_mark_to_market_before_future_exit_costs"
        assert current["research_excluded"] is True

    def test_get_config(self, client: TestClient):
        response = client.get("/api/config")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)

    def test_add_ticker_validation(self, client: TestClient):
        # Empty ticker
        response = client.post("/api/tickers", json={"ticker": ""})
        assert response.status_code == 400
        
        # Valid ticker (mocked engine)
        with patch("api.main.add_ticker") as mock_add, \
             patch("api.main.threading.Thread") as mock_thread:
            mock_add.return_value = {"status": "ok", "ticker": "AAPL"}
            mock_thread.return_value.start.return_value = None
            response = client.post("/api/tickers", json={"ticker": "AAPL"})
            assert response.status_code == 200
            assert response.json()["ticker"] == "AAPL"

    def test_engine_status(self, client: TestClient):
        response = client.get("/api/engine/status")
        assert response.status_code == 200
        assert "running" in response.json()

    def test_news_tab_breaking_cold_start_returns_fast_placeholder(self, client: TestClient):
        with patch("api.main._agent_enabled", return_value=True), \
             patch("api.main._endpoint_payload_get", return_value=None), \
             patch("agents.news_tab_agent.get_news_tab_agent_if_ready", return_value=None), \
             patch("agents.news_tab_agent.kickoff_news_tab_agent_init") as mock_kickoff:
            response = client.get("/api/news-tab/breaking")

        assert response.status_code == 200
        data = response.json()
        assert data["breaking"] == []
        assert data["disabled"] is False
        assert data["initializing"] is True
        mock_kickoff.assert_called_once()

    def test_news_tab_state_cache_only_miss_returns_live_state(self, client: TestClient):
        class DummyAgent:
            def get_news_tab_state(self, active_tickers=None):
                return {"market_news": [{"id": "x"}], "total_articles": 1}

        with patch("api.main._agent_enabled", return_value=True), \
             patch("api.main._get_news_tab_state_stale_payload", return_value=None), \
             patch("agents.news_tab_agent.get_news_tab_agent_if_ready", return_value=DummyAgent()), \
             patch("agents.news_tab_agent.kickoff_news_tab_agent_init"):
            response = client.get("/api/news-tab/state")

        assert response.status_code == 200
        data = response.json()
        assert data["loading"] is False
        assert data["disabled"] is False
        assert data["market_news"] == [{"id": "x"}]

    def test_news_tab_state_cache_only_does_not_pin_an_expired_snapshot(self, client: TestClient):
        class DummyAgent:
            def get_news_tab_state(self, active_tickers=None):
                return {"market_news": [{"id": "fresh"}], "total_articles": 1}

        with patch("api.main._agent_enabled", return_value=True), \
             patch("api.main._get_news_tab_state_stale_payload", return_value=b'{"market_news":[{"id":"stale"}]}'), \
             patch("agents.news_tab_agent.get_news_tab_agent_if_ready", return_value=DummyAgent()), \
             patch("agents.news_tab_agent.kickoff_news_tab_agent_init"), \
             patch("api.main._build_news_tab_state_cached", wraps=__import__("api.main", fromlist=["_build_news_tab_state_cached"])._build_news_tab_state_cached) as cached_builder:
            from api.main import _clear_news_tab_state_cache
            _clear_news_tab_state_cache()
            response = client.get("/api/news-tab/state")

        assert response.status_code == 200
        assert response.json()["market_news"] == [{"id": "fresh"}]
        cached_builder.assert_called_once()

    def test_scanner_state_cache_only_empty_payload_does_not_trigger_refresh(self, client: TestClient):
        class DummyAgent:
            def __init__(self):
                self.refresh_calls = 0

            def get_state(self):
                return {"hot_tickers": [], "refreshing": False, "status": {}}

            def request_refresh(self):
                self.refresh_calls += 1
                return {"status": "accepted"}

        agent = DummyAgent()
        with patch("api.main._endpoint_payload_get", return_value=None), \
             patch("api.main._endpoint_payload_set") as mock_cache_set, \
             patch("api.main._maybe_hint_trade_radar_refresh") as mock_hint, \
             patch("agents.scanner_agent.get_scanner_agent", return_value=agent):
            response = client.get("/api/scanner/state")

        assert response.status_code == 200
        data = response.json()
        assert data["hot_tickers"] == []
        assert data["status"]["state"] == "WARMING_UP"
        assert agent.refresh_calls == 0
        mock_cache_set.assert_not_called()
        mock_hint.assert_not_called()

    def test_scanner_state_ignores_cached_empty_payload(self, client: TestClient):
        class DummyAgent:
            def __init__(self):
                self.refresh_calls = 0

            def get_state(self):
                return {"hot_tickers": [], "refreshing": False, "status": {"state": "LIVE"}}

            def request_refresh(self):
                self.refresh_calls += 1
                return {"status": "accepted"}

        cached_empty = b'{"hot_tickers":[],"refreshing":false,"status":{"state":"STALE_EMPTY"}}'
        agent = DummyAgent()
        with patch("api.main._endpoint_payload_get", return_value=cached_empty), \
             patch("api.main._endpoint_payload_set") as mock_cache_set, \
             patch("api.main._maybe_hint_trade_radar_refresh"), \
             patch("agents.scanner_agent.get_scanner_agent", return_value=agent):
            response = client.get("/api/scanner/state")

        assert response.status_code == 200
        data = response.json()
        assert data["status"]["state"] == "WARMING_UP"
        assert agent.refresh_calls == 0
        mock_cache_set.assert_not_called()

    def test_pre_earnings_cached_payload_serializes_nan_as_null(self, client: TestClient):
        cached_nan = b'{"calibration":{"avg_realized_move_pct":NaN},"items":[]}'
        with patch("api.main._endpoint_payload_get", return_value=cached_nan):
            response = client.get("/api/pre-earnings")

        assert response.status_code == 200
        assert b"NaN" not in response.content
        data = response.json()
        assert data["calibration"]["avg_realized_move_pct"] is None

    def test_not_found(self, client: TestClient):
        response = client.get("/api/non-existent-endpoint")
        assert response.status_code == 404

    def test_run_backtest_params(self, client: TestClient):
        with patch("api.main.run_backtest") as mock_backtest:
            mock_backtest.return_value = {"status": "ok"}
            
            payload = {
                "days": 10,
                "risk_mode": "HIGH", 
                "trade_mode": "BREAKOUT",
                "tickers": ["BTC-USD"]
            }
            response = client.post("/api/backtest/run", json=payload)
            assert response.status_code == 200
            mock_backtest.assert_called_with(
                days=10,
                risk_mode="HIGH",
                trade_mode="BREAKOUT",
                tickers=["BTC-USD"],
                compare_all=False,
                orb_entry_type="MIDPOINT",
                variant_mode="STRICT",
            )

    def test_latest_oos_validation_endpoint_is_read_only_and_fail_closed(self, client: TestClient):
        expected = {
            "status": "available",
            "reports": [{"ticker": "BTC-USD", "status": "negative_edge"}],
            "autonomous_execution_eligible": False,
            "reason": "Price-only OOS report",
        }
        with patch(
            "core.oos_validation_reports.get_latest_oos_validation_reports",
            return_value=expected,
        ):
            response = client.get("/api/backtest/oos-validation/latest")

        assert response.status_code == 200
        assert response.json() == expected

    def test_capital_decisions_include_bounded_news_evidence(self, client: TestClient):
        audit_record = {"event_id": "audit-1", "timestamp": "2026-07-14T10:00:00+00:00", "event_type": "position_opened"}
        news_record = {"event_id": "news-1", "timestamp": "2026-07-14T10:01:00+00:00", "event_type": "autonomous_news_evaluated"}
        with patch("api.main._endpoint_payload_get", return_value=None), \
             patch("api.main._endpoint_payload_set"), \
             patch("core.trade_audit.get_trade_audit", return_value=[audit_record]), \
             patch("core.execution_archive.read_recent_executions", return_value=[news_record]):
            response = client.get("/api/capital/decisions?limit=5")

        assert response.status_code == 200
        payload = response.json()
        assert payload["archive_backed_news_evidence_count"] == 1
        assert [item["event_id"] for item in payload["records"]] == ["news-1", "audit-1"]
