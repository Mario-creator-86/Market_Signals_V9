import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from agents.ui_inspector_agent import (
    UIInspectorAgent,
    get_ui_inspector_agent,
    get_ui_inspector_agent_if_ready,
)
from core.development_factory.watchdog import RepairCaseStore


def _iso_ago(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


def _healthy_other_tabs(url, timeout=10.0):
    if "/api/cockpit" in url:
        return {"strategy_lab": {"records": []}, "decision_ledger": {"items": []}}
    if "/api/news-tab/state" in url:
        return {"total_articles": 12, "last_update": _iso_ago(seconds=30)}
    if "/api/scanner/state" in url:
        return {
            "hot_tickers": [{"symbol": "AAPL"}],
            "as_of_utc": _iso_ago(seconds=10),
            "refresh_seconds": 60,
        }
    if "/api/traderadar/state" in url:
        return {"candidates": [{"ticker": "NVDA"}], "as_of": _iso_ago(seconds=30)}
    if "/api/premarket" in url:
        return {"candidates": [{"symbol": "MSTR"}], "as_of_utc": _iso_ago(seconds=60), "refresh_seconds": 300}
    if "/api/watchlist" in url:
        # 7d liveness probe: populated registry, no EMPTY_FEED anomaly
        return {"stocks": ["PYPL"], "crypto": [], "hot_list": []}
    if "/api/agents/control-plane" in url:
        return {"agents": {"ui_inspector": {"enabled": True, "running": True}}}
    return {}


class TestUIInspectorAgent(unittest.TestCase):
    def setUp(self):
        self.agent = UIInspectorAgent(config={"agents": {"ui_inspector": {"enabled": True}}})

    def test_singleton_helpers(self):
        agent1 = get_ui_inspector_agent()
        agent2 = get_ui_inspector_agent_if_ready()
        self.assertIsNotNone(agent1)
        self.assertIs(agent1, agent2)

    @patch.object(UIInspectorAgent, "_wait_for_server_readiness")
    @patch.object(UIInspectorAgent, "run_inspection_cycle")
    def test_start_and_stop_lifecycle(self, mock_cycle, mock_wait):
        self.assertFalse(self.agent.is_running)
        self.agent.start()
        self.assertTrue(self.agent.is_running)
        self.agent.stop()
        self.assertFalse(self.agent.is_running)

    @patch.object(UIInspectorAgent, "_fetch_json")
    @patch.object(UIInspectorAgent, "_escalate_to_coding_agent", return_value=1)
    def test_run_inspection_detects_strategy_lab_stall(self, mock_escalate, mock_fetch):
        def side_effect(url, timeout=10.0):
            if "/api/cockpit" in url:
                return {
                    "strategy_lab": {
                        "records": [
                            {"strategy_id": "S1", "oos_verdict": None, "last_validated": None},
                            {"strategy_id": "S2", "oos_verdict": "UNAVAILABLE", "last_validated": "—"},
                        ]
                    }
                }
            if "/api/news-tab/state" in url:
                return {"count": 10, "articles": [{"title": "Market up"}]}
            if "/api/scanner/state" in url:
                return {"hotlist": ["AAPL"], "crypto_pairs": ["BTC-USD"]}
            if "/api/traderadar/state" in url:
                return {"items": [{"ticker": "NVDA"}]}
            if "/api/pre-earnings" in url:
                return {"items": [{"ticker": "MSFT"}]}
            if "/api/watchlist" in url:
                return {"stocks": ["PYPL"], "crypto": [], "hot_list": []}
            return {}

        mock_fetch.side_effect = side_effect

        report = self.agent.run_inspection_cycle()
        self.assertEqual(report["status"], "ok")
        self.assertGreaterEqual(report["anomalies_detected"], 1)
        self.assertEqual(report["tab_health"]["strategy_lab"]["status"], "DEGRADED")
        self.assertEqual(report["tab_health"]["news_tab"]["status"], "HEALTHY")
        mock_escalate.assert_called_once()

    @patch.object(UIInspectorAgent, "_fetch_json")
    def test_run_inspection_all_healthy(self, mock_fetch):
        def side_effect(url, timeout=10.0):
            if "/api/cockpit" in url:
                return {
                    "strategy_lab": {
                        "records": [
                            {"strategy_id": "S1", "oos_verdict": "VALIDATED", "last_validated": "2026-08-25"},
                            {"strategy_id": "S2", "oos_verdict": "RESEARCH_ONLY", "last_validated": "2026-08-25"},
                        ]
                    }
                }
            if "/api/news-tab/state" in url:
                return {"count": 25, "articles": [{"title": "News 1"}]}
            if "/api/scanner/state" in url:
                return {"hotlist": ["AAPL"], "crypto_pairs": ["BTC-USD"]}
            if "/api/traderadar/state" in url:
                return {"items": [{"ticker": "NVDA"}]}
            if "/api/pre-earnings" in url:
                return {"items": [{"ticker": "MSFT"}]}
            if "/api/watchlist" in url:
                return {"stocks": ["PYPL"], "crypto": [], "hot_list": []}
            return {}

        mock_fetch.side_effect = side_effect

        report = self.agent.run_inspection_cycle()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["anomalies_detected"], 0)
        self.assertEqual(report["tab_health"]["strategy_lab"]["status"], "HEALTHY")
        self.assertEqual(report["tab_health"]["news_tab"]["status"], "HEALTHY")
        self.assertEqual(report["tab_health"]["scanner"]["status"], "HEALTHY")

    @patch.object(UIInspectorAgent, "_fetch_json")
    def test_run_inspection_understands_live_api_contracts(self, mock_fetch):
        def side_effect(url, timeout=10.0):
            if "/api/cockpit" in url:
                return {"strategy_lab": {"records": []}}
            if "/api/news-tab/state" in url:
                return {"total_articles": 17, "market_news": [{"title": "Market"}]}
            if "/api/scanner/state" in url:
                return {"hot_tickers": [{"symbol": "AAPL"}], "liquid_crypto_tickers": [{"symbol": "BTC-USD"}]}
            if "/api/traderadar/state" in url:
                return {"total_candidates": 1, "candidates": [{"ticker": "NVDA"}]}
            if "/api/pre-earnings" in url:
                return {"items": [{"symbol": "MSFT"}]}
            if "/api/watchlist" in url:
                return {"stocks": ["PYPL"], "crypto": [], "hot_list": []}
            return {}

        mock_fetch.side_effect = side_effect

        report = self.agent.run_inspection_cycle()

        self.assertEqual(report["anomalies_detected"], 0)
        self.assertEqual(report["tab_health"]["news_tab"]["articles"], 17)
        self.assertEqual(report["tab_health"]["scanner"]["stocks"], 1)
        self.assertEqual(report["tab_health"]["trade_radar"]["candidates"], 1)

    @patch.object(UIInspectorAgent, "_escalate_to_coding_agent")
    @patch.object(UIInspectorAgent, "_post_json")
    @patch.object(UIInspectorAgent, "_fetch_json")
    def test_enabled_stopped_agent_is_self_healed_before_escalation(
        self, mock_fetch, mock_post, mock_escalate
    ):
        def side_effect(url, timeout=10.0):
            if "/api/cockpit" in url:
                return {"strategy_lab": {"records": []}, "decision_ledger": {"items": []}}
            if "/api/news-tab/state" in url:
                return {"total_articles": 1}
            if "/api/scanner/state" in url:
                return {"hot_tickers": [{"symbol": "AAPL"}]}
            if "/api/traderadar/state" in url:
                return {"candidates": [{"ticker": "AAPL"}]}
            if "/api/pre-earnings" in url:
                return {"items": [{"symbol": "MSFT"}]}
            if "/api/agents/control-plane" in url:
                return {"agents": {
                    "research_factory": {
                        "enabled": True,
                        "running": False,
                        "initializing": False,
                        "lifecycle_state": "stopped",
                    },
                    "ui_inspector": {"enabled": True, "running": True},
                }}
            if "/api/watchlist" in url:
                return {"stocks": ["PYPL"], "crypto": [], "hot_list": []}
            return {}

        mock_fetch.side_effect = side_effect
        mock_post.return_value = {"runtime": {"running": True, "lifecycle_state": "running"}}

        report = self.agent.run_inspection_cycle()

        self.assertEqual(report["self_healed_count"], 1)
        self.assertEqual(report["unresolved_anomalies"], 0)
        self.assertEqual(report["tab_health"]["agents"]["status"], "RECOVERED")
        mock_post.assert_called_once()
        mock_escalate.assert_not_called()

    @patch.object(UIInspectorAgent, "_escalate_to_coding_agent", return_value=1)
    @patch.object(UIInspectorAgent, "_fetch_json")
    def test_executed_ai_decision_without_critic_is_escalated(self, mock_fetch, mock_escalate):
        def side_effect(url, timeout=10.0):
            if "/api/cockpit" in url:
                return {
                    "strategy_lab": {"records": []},
                    "decision_ledger": {"items": [{
                        "decision_id": "dec-1",
                        "signal_id": "sig-1",
                        "ticker": "OKTA",
                        "status": "executed",
                        "action": "OPEN_SHORT",
                        "ai_model": {"consulted": True},
                        "critic": {"available": False},
                    }]},
                }
            if "/api/news-tab/state" in url:
                return {"total_articles": 1}
            if "/api/scanner/state" in url:
                return {"hot_tickers": [{"symbol": "OKTA"}]}
            if "/api/traderadar/state" in url:
                return {"candidates": [{"ticker": "OKTA"}]}
            if "/api/pre-earnings" in url:
                return {"items": [{"symbol": "OKTA"}]}
            if "/api/agents/control-plane" in url:
                return {"agents": {"ui_inspector": {"enabled": True, "running": True}}}
            if "/api/watchlist" in url:
                # 7d liveness probe: populated registry, no EMPTY_FEED anomaly
                return {"stocks": ["PYPL"], "crypto": [], "hot_list": []}
            return {}

        mock_fetch.side_effect = side_effect

        report = self.agent.run_inspection_cycle()

        self.assertEqual(report["tab_health"]["decision_ledger"]["status"], "DEGRADED")
        self.assertEqual(report["tab_health"]["decision_ledger"]["missing_critic"], 1)
        self.assertEqual(report["unresolved_anomalies"], 1)
        mock_escalate.assert_called_once()

    @patch.object(UIInspectorAgent, "_escalate_to_coding_agent")
    @patch.object(UIInspectorAgent, "_fetch_json")
    def test_historical_execution_after_ai_veto_is_reported_as_recovered(self, mock_fetch, mock_escalate):
        def side_effect(url, timeout=10.0):
            if "/api/cockpit" in url:
                return {
                    "strategy_lab": {"records": []},
                    "decision_ledger": {"items": [{
                        "decision_id": "dec-policy-old",
                        "timestamp": "2020-01-01T00:00:00+00:00",
                        "ticker": "BNTX",
                        "status": "executed",
                        "what_happened": "OPEN_LONG (executed)",
                        "ai_model": {"consulted": True},
                        "validation": {"ok": False, "stage": "POLICY"},
                        "critic": {
                            "available": False,
                            "skipped": True,
                            "reason": "skipped_after_ai_validation_veto",
                        },
                    }]},
                }
            if "/api/news-tab/state" in url:
                return {"total_articles": 1}
            if "/api/scanner/state" in url:
                return {"hot_tickers": [{"symbol": "BNTX"}]}
            if "/api/traderadar/state" in url:
                return {"candidates": [{"ticker": "BNTX"}]}
            if "/api/pre-earnings" in url:
                return {"items": [{"symbol": "BNTX"}]}
            if "/api/agents/control-plane" in url:
                return {"agents": {"ui_inspector": {"enabled": True, "running": True}}}
            if "/api/watchlist" in url:
                # 7d liveness probe: populated registry, no EMPTY_FEED anomaly
                return {"stocks": ["PYPL"], "crypto": [], "hot_list": []}
            return {}

        mock_fetch.side_effect = side_effect

        report = self.agent.run_inspection_cycle()

        ledger = report["tab_health"]["decision_ledger"]
        self.assertEqual(ledger["status"], "RECOVERED")
        self.assertEqual(ledger["missing_critic"], 0)
        self.assertEqual(ledger["active_policy_violations"], 0)
        self.assertEqual(ledger["historical_policy_violations"], 1)
        self.assertEqual(report["unresolved_anomalies"], 0)
        mock_escalate.assert_not_called()

    @patch.object(UIInspectorAgent, "_escalate_to_coding_agent", return_value=1)
    @patch.object(UIInspectorAgent, "_fetch_json")
    def test_current_execution_after_ai_veto_is_escalated(self, mock_fetch, mock_escalate):
        def side_effect(url, timeout=10.0):
            if "/api/cockpit" in url:
                return {
                    "strategy_lab": {"records": []},
                    "decision_ledger": {"items": [{
                        "decision_id": "dec-policy-current",
                        "ticker": "PYPL",
                        "status": "executed",
                        "what_happened": "OPEN_LONG (executed)",
                        "ai_model": {"consulted": True},
                        "validation": {"ok": False, "stage": "POLICY"},
                        "critic": {"available": False},
                    }]},
                }
            if "/api/news-tab/state" in url:
                return {"total_articles": 1}
            if "/api/scanner/state" in url:
                return {"hot_tickers": [{"symbol": "PYPL"}]}
            if "/api/traderadar/state" in url:
                return {"candidates": [{"ticker": "PYPL"}]}
            if "/api/pre-earnings" in url:
                return {"items": [{"symbol": "PYPL"}]}
            if "/api/watchlist" in url:
                # 7d watchlist liveness: a populated registry must not trip the
                # EMPTY_FEED anomaly this scenario doesn't assert about
                return {"stocks": ["PYPL"], "crypto": [], "hot_list": []}
            if "/api/agents/control-plane" in url:
                return {"agents": {"ui_inspector": {"enabled": True, "running": True}}}
            if "/api/watchlist" in url:
                # 7d liveness probe: populated registry, no EMPTY_FEED anomaly
                return {"stocks": ["PYPL"], "crypto": [], "hot_list": []}
            return {}

        mock_fetch.side_effect = side_effect

        report = self.agent.run_inspection_cycle()

        ledger = report["tab_health"]["decision_ledger"]
        self.assertEqual(ledger["status"], "DEGRADED")
        self.assertEqual(ledger["missing_critic"], 0)
        self.assertEqual(ledger["active_policy_violations"], 1)
        self.assertEqual(report["unresolved_anomalies"], 1)
        self.assertEqual(report["anomalies"][0]["kind"], "EXECUTED_AFTER_AI_VALIDATION_VETO")
        mock_escalate.assert_called_once()

    @patch.object(UIInspectorAgent, "_escalate_to_coding_agent", return_value=1)
    @patch.object(UIInspectorAgent, "_fetch_json")
    def test_running_agent_with_failed_last_cycle_is_degraded(self, mock_fetch, mock_escalate):
        def side_effect(url, timeout=10.0):
            if "/api/cockpit" in url:
                return {"strategy_lab": {"records": []}, "decision_ledger": {"items": []}}
            if "/api/news-tab/state" in url:
                return {"total_articles": 1}
            if "/api/scanner/state" in url:
                return {"hot_tickers": [{"symbol": "AAPL"}]}
            if "/api/traderadar/state" in url:
                return {"candidates": [{"ticker": "AAPL"}]}
            if "/api/pre-earnings" in url:
                return {"items": [{"symbol": "MSFT"}]}
            if "/api/agents/control-plane" in url:
                return {"agents": {
                    "research_factory": {
                        "enabled": True,
                        "running": True,
                        "initializing": False,
                        "last_error": "UnicodeEncodeError: cp1252",
                    },
                }}
            if "/api/watchlist" in url:
                return {"stocks": ["PYPL"], "crypto": [], "hot_list": []}
            return {}

        mock_fetch.side_effect = side_effect

        report = self.agent.run_inspection_cycle()

        self.assertEqual(report["tab_health"]["agents"]["status"], "DEGRADED")
        self.assertEqual(report["unresolved_anomalies"], 1)
        self.assertEqual(report["self_healed_count"], 0)
        incident = report["anomalies"][0]
        self.assertEqual(incident["context"]["file"], "core/research_factory/scheduler.py")
        self.assertIn("core/research_factory/tracking.py", incident["context"]["allowed_files"])
        mock_escalate.assert_called_once()

    @patch.object(UIInspectorAgent, "_escalate_to_coding_agent")
    @patch.object(UIInspectorAgent, "_post_json")
    @patch.object(UIInspectorAgent, "_fetch_json")
    def test_engine_managed_agents_paused_with_engine_are_not_restarted(
        self, mock_fetch, mock_post, mock_escalate
    ):
        def side_effect(url, timeout=10.0):
            if "/api/cockpit" in url:
                return {"strategy_lab": {"records": []}, "decision_ledger": {"items": []}}
            if "/api/news-tab/state" in url:
                return {"total_articles": 1}
            if "/api/scanner/state" in url:
                return {"hot_tickers": [{"symbol": "AAPL"}]}
            if "/api/traderadar/state" in url:
                return {"candidates": [{"ticker": "AAPL"}]}
            if "/api/pre-earnings" in url:
                return {"items": [{"symbol": "MSFT"}]}
            if "/api/agents/control-plane" in url:
                return {
                    "bot": {"engine_running": False},
                    "agents": {
                        "scanner": {"enabled": True, "running": False, "initializing": False},
                        "ui_inspector": {"enabled": True, "running": True},
                    },
                }
            if "/api/watchlist" in url:
                return {"stocks": ["PYPL"], "crypto": [], "hot_list": []}
            return {}

        mock_fetch.side_effect = side_effect

        report = self.agent.run_inspection_cycle()

        self.assertEqual(report["tab_health"]["agents"]["status"], "HEALTHY")
        self.assertEqual(report["tab_health"]["agents"]["paused_with_engine"], ["scanner"])
        mock_post.assert_not_called()
        mock_escalate.assert_not_called()

    @patch.object(UIInspectorAgent, "_escalate_to_coding_agent", return_value=4)
    @patch.object(UIInspectorAgent, "_fetch_json")
    def test_pre_earnings_starvation_signature_is_detected_and_escalated(
        self, mock_fetch, mock_escalate
    ):
        """The exact live defect: 200 items served, frozen as_of/last_run,
        last_success kept fresh by targeted rescorses, Pulse card empty while
        the durable registry holds active picks. The old check read HEALTHY."""
        starved_payload = {
            "items": [{"symbol": "MU"}, {"symbol": "SNOW"}],
            "refresh_seconds": 600,
            "as_of_utc": _iso_ago(hours=4),
            "status": {
                "last_run_time": _iso_ago(hours=4),
                "last_success_time": _iso_ago(seconds=40),
            },
            "pulse": {
                "current_top_long": [],
                "current_top_short": [],
                "top_long": [{"symbol": "MU"}, {"symbol": "AOUT"}, {"symbol": "CRDO"}],
                "top_short": [{"symbol": "DXLG"}, {"symbol": "CURV"}, {"symbol": "HAIN"}],
                "active_history": {
                    "top_long": [{"symbol": "MU"}],
                    "top_short": [{"symbol": "DXLG"}],
                },
            },
        }

        def side_effect(url, timeout=10.0):
            if "/api/pre-earnings" in url:
                return starved_payload
            return _healthy_other_tabs(url, timeout)

        mock_fetch.side_effect = side_effect

        report = self.agent.run_inspection_cycle()

        self.assertEqual(report["tab_health"]["pre_earnings"]["status"], "DEGRADED")
        kinds = {a["kind"] for a in report["anomalies"]}
        self.assertIn("DATA_STALE", kinds)
        self.assertIn("CYCLE_STALLED", kinds)
        self.assertIn("PARTIAL_UPDATE_MASKING", kinds)
        self.assertIn("PULSE_EMPTY_WITH_ACTIVE_PICKS", kinds)
        pulse_anomaly = next(a for a in report["anomalies"] if a["kind"] == "PULSE_EMPTY_WITH_ACTIVE_PICKS")
        self.assertEqual(pulse_anomaly["context"]["active_long"], ["MU", "AOUT", "CRDO"])
        self.assertEqual(pulse_anomaly["context"]["file"], "agents/pre_earnings_agent.py")
        mock_escalate.assert_called_once()

    @patch.object(UIInspectorAgent, "_escalate_to_coding_agent")
    @patch.object(UIInspectorAgent, "_fetch_json")
    def test_pre_earnings_fresh_with_current_picks_is_healthy(self, mock_fetch, mock_escalate):
        def side_effect(url, timeout=10.0):
            if "/api/pre-earnings" in url:
                return {
                    "items": [{"symbol": "MU"}],
                    "refresh_seconds": 600,
                    "as_of_utc": _iso_ago(seconds=45),
                    "status": {
                        "last_run_time": _iso_ago(seconds=95),
                        "last_success_time": _iso_ago(seconds=10),
                    },
                    "pulse": {
                        "current_top_long": [{"symbol": "MU"}],
                        "current_top_short": [],
                        "top_long": [{"symbol": "MU"}],
                        "top_short": [],
                    },
                }
            return _healthy_other_tabs(url, timeout)

        mock_fetch.side_effect = side_effect

        report = self.agent.run_inspection_cycle()

        self.assertEqual(report["tab_health"]["pre_earnings"]["status"], "HEALTHY")
        self.assertEqual(report["tab_health"]["pre_earnings"]["pulse_current_picks"], 1)
        self.assertEqual(report["anomalies_detected"], 0)
        mock_escalate.assert_not_called()

    @patch.object(UIInspectorAgent, "_escalate_to_coding_agent", return_value=1)
    @patch.object(UIInspectorAgent, "_fetch_json")
    def test_news_feed_frozen_beyond_budget_is_stale(self, mock_fetch, mock_escalate):
        def side_effect(url, timeout=10.0):
            if "/api/news-tab/state" in url:
                return {"total_articles": 40, "last_update": _iso_ago(hours=2)}
            return _healthy_other_tabs(url, timeout)

        mock_fetch.side_effect = side_effect

        report = self.agent.run_inspection_cycle()

        self.assertEqual(report["tab_health"]["news_tab"]["status"], "DEGRADED")
        kinds = {a["kind"] for a in report["anomalies"]}
        self.assertIn("DATA_STALE", kinds)
        news_anomaly = next(a for a in report["anomalies"] if a["tab"] == "News Tab" and a["kind"] == "DATA_STALE")
        self.assertEqual(news_anomaly["context"]["file"], "agents/news_tab_agent.py")
        mock_escalate.assert_called_once()

    @patch.object(UIInspectorAgent, "_escalate_to_coding_agent", return_value=1)
    @patch.object(UIInspectorAgent, "_fetch_json")
    def test_scanner_and_radar_frozen_payloads_are_stale(self, mock_fetch, mock_escalate):
        def side_effect(url, timeout=10.0):
            if "/api/scanner/state" in url:
                return {
                    "hot_tickers": [{"symbol": "AAPL"}],
                    "as_of_utc": _iso_ago(minutes=30),
                    "refresh_seconds": 60,
                }
            if "/api/traderadar/state" in url:
                return {"candidates": [{"ticker": "NVDA"}], "as_of": _iso_ago(hours=1)}
            return _healthy_other_tabs(url, timeout)

        mock_fetch.side_effect = side_effect

        report = self.agent.run_inspection_cycle()

        self.assertEqual(report["tab_health"]["scanner"]["status"], "DEGRADED")
        self.assertEqual(report["tab_health"]["trade_radar"]["status"], "DEGRADED")
        stale_tabs = {a["tab"] for a in report["anomalies"] if a["kind"] == "DATA_STALE"}
        self.assertEqual(stale_tabs, {"Market Scanner", "Trade Radar"})
        mock_escalate.assert_called_once()

    @patch.object(UIInspectorAgent, "_escalate_to_coding_agent", return_value=1)
    @patch.object(UIInspectorAgent, "_fetch_json")
    def test_premarket_stale_snapshot_is_flagged_but_disabled_is_not(self, mock_fetch, mock_escalate):
        def side_effect(url, timeout=10.0):
            if "/api/premarket" in url:
                return {
                    "candidates": [{"symbol": "MSTR"}],
                    "as_of_utc": _iso_ago(hours=1),
                    "refresh_seconds": 300,
                }
            return _healthy_other_tabs(url, timeout)

        mock_fetch.side_effect = side_effect

        report = self.agent.run_inspection_cycle()

        self.assertEqual(report["tab_health"]["premarket"]["status"], "DEGRADED")
        self.assertEqual(len(report["anomalies"]), 1)
        self.assertEqual(report["anomalies"][0]["tab"], "Premarket")
        mock_escalate.assert_called_once()

        disabled_state = {"disabled": True}
        mock_fetch.side_effect = lambda url, timeout=10.0: (
            disabled_state if "/api/premarket" in url else _healthy_other_tabs(url, timeout)
        )
        report = self.agent.run_inspection_cycle()
        self.assertEqual(report["tab_health"]["premarket"]["status"], "WATCH")
        self.assertEqual(report["anomalies_detected"], 0)
        mock_escalate.assert_called_once()

    @patch.object(UIInspectorAgent, "_escalate_to_coding_agent")
    @patch.object(UIInspectorAgent, "_fetch_json")
    def test_missing_freshness_fields_never_flag(self, mock_fetch, mock_escalate):
        def side_effect(url, timeout=10.0):
            if "/api/cockpit" in url:
                return {"strategy_lab": {"records": []}, "decision_ledger": {"items": []}}
            if "/api/news-tab/state" in url:
                return {"total_articles": 5}
            if "/api/scanner/state" in url:
                return {"hot_tickers": [{"symbol": "AAPL"}]}
            if "/api/traderadar/state" in url:
                return {"candidates": [{"ticker": "NVDA"}]}
            if "/api/pre-earnings" in url:
                return {"items": [{"symbol": "MSFT"}]}
            if "/api/premarket" in url:
                return {"candidates": [{"symbol": "MSTR"}]}
            if "/api/agents/control-plane" in url:
                return {"agents": {"ui_inspector": {"enabled": True, "running": True}}}
            if "/api/watchlist" in url:
                # 7d liveness probe: populated registry, no EMPTY_FEED anomaly
                return {"stocks": ["PYPL"], "crypto": [], "hot_list": []}
            return {}

        mock_fetch.side_effect = side_effect

        report = self.agent.run_inspection_cycle()

        self.assertEqual(report["anomalies_detected"], 0)
        self.assertEqual(report["tab_health"]["pre_earnings"]["status"], "HEALTHY")
        mock_escalate.assert_not_called()


def _pm_row(symbol, *, vol=None, news=0, earnings=False, ai=True):
    row = {
        "symbol": symbol,
        "direction": "LONG",
        "premarket_vol_rel": vol,
        "news": [{"title": f"n{i}"} for i in range(news)],
        "earnings": {"date": "2026-09-10", "eps_estimated": 1.0} if earnings else {},
        "ollama": {"summary": "AI read"} if ai else {},
    }
    return row


class TestPremarketFieldCoverage(unittest.TestCase):
    def setUp(self):
        self.agent = UIInspectorAgent(config={"agents": {"ui_inspector": {"enabled": True}}})

    def _run(self, mock_fetch, premarket_payload):
        def side_effect(url, timeout=10.0):
            if "/api/cockpit" in url:
                return {"strategy_lab": {"records": []}, "decision_ledger": {"items": []}}
            if "/api/news-tab/state" in url:
                return {"total_articles": 12, "last_update": _iso_ago(seconds=30)}
            if "/api/scanner/state" in url:
                return {"hot_tickers": [{"symbol": "AAPL"}], "as_of_utc": _iso_ago(seconds=10), "refresh_seconds": 60}
            if "/api/traderadar/state" in url:
                return {"candidates": [{"ticker": "NVDA"}], "as_of": _iso_ago(seconds=30)}
            if "/api/pre-earnings" in url:
                return {"items": [{"symbol": "MSFT"}], "as_of_utc": _iso_ago(seconds=45)}
            if "/api/premarket" in url:
                return premarket_payload
            if "/api/agents/control-plane" in url:
                return {"agents": {"ui_inspector": {"enabled": True, "running": True}}}
            if "/api/watchlist" in url:
                # 7d liveness probe: populated registry, no EMPTY_FEED anomaly
                return {"stocks": ["PYPL"], "crypto": [], "hot_list": []}
            return {}

        mock_fetch.side_effect = side_effect
        return self.agent.run_inspection_cycle()

    @patch.object(UIInspectorAgent, "_escalate_to_coding_agent", return_value=1)
    @patch.object(UIInspectorAgent, "_fetch_json")
    def test_vol_rel_gap_detected_and_escalated(self, mock_fetch, mock_escalate):
        """Live defect: 0/15 candidates expose premarket_vol_rel while inputs
        are healthy — the details panel always shows 'Vol Rel: n/a'."""
        payload = {
            "as_of_utc": _iso_ago(seconds=30),
            "refresh_seconds": 300,
            "flags": {"news_available": True, "news_stale": False, "earnings_available": True},
            "candidates": [
                _pm_row("A", vol=1.2, news=2),
                _pm_row("B", vol=0.8, news=1),
                _pm_row("C", vol=None, news=1),
                _pm_row("D", vol=None, news=2),
                _pm_row("E", vol=None, news=1),
                _pm_row("F", vol=None, news=1),
            ],
        }
        report = self._run(mock_fetch, payload)

        kinds = {a["kind"] for a in report["anomalies"]}
        self.assertNotIn("DATA_INCOMPLETE", kinds)
        self.assertEqual(report["tab_health"]["premarket"]["status"], "HEALTHY")
        mock_escalate.assert_not_called()

        payload["candidates"] = [_pm_row(s, vol=None, news=2) for s in "ABCDEF"]
        report = self._run(mock_fetch, payload)
        gaps = [a for a in report["anomalies"] if a["kind"] == "DATA_INCOMPLETE" and "relative volume" in a["title"]]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["context"]["file"], "agents/premarket_scanner_agent.py")
        self.assertEqual(report["tab_health"]["premarket"]["status"], "DEGRADED")
        self.assertEqual(report["tab_health"]["premarket"]["field_coverage"]["vol_rel"], "0/6")
        mock_escalate.assert_called_once()

    @patch.object(UIInspectorAgent, "_escalate_to_coding_agent", return_value=1)
    @patch.object(UIInspectorAgent, "_fetch_json")
    def test_news_coverage_gap_detected(self, mock_fetch, mock_escalate):
        """Live defect: 4/15 candidates carry news while the news input reports
        available+fresh — 'News Count: 0' for the rest."""
        payload = {
            "as_of_utc": _iso_ago(seconds=30),
            "refresh_seconds": 300,
            "flags": {"news_available": True, "news_stale": False},
            "candidates": [
                _pm_row("A", vol=1.0, news=0),
                _pm_row("B", vol=1.0, news=0),
                _pm_row("C", vol=1.0, news=1),
                _pm_row("D", vol=1.0, news=0),
                _pm_row("E", vol=1.0, news=0),
                _pm_row("F", vol=1.0, news=0),
            ],
        }
        report = self._run(mock_fetch, payload)

        gaps = [a for a in report["anomalies"] if a["kind"] == "DATA_INCOMPLETE" and "news coverage" in a["title"]]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["context"]["file"], "agents/news_tab_agent.py")
        self.assertEqual(report["tab_health"]["premarket"]["field_coverage"]["news_items"], "1/6")
        mock_escalate.assert_called_once()

    @patch.object(UIInspectorAgent, "_escalate_to_coding_agent")
    @patch.object(UIInspectorAgent, "_fetch_json")
    def test_news_gap_not_flagged_when_input_unavailable(self, mock_fetch, mock_escalate):
        """When the news input itself is down, the coverage gap is a symptom —
        the input-level signal governs, no duplicate coverage anomaly."""
        payload = {
            "as_of_utc": _iso_ago(seconds=30),
            "refresh_seconds": 300,
            "flags": {"news_available": False, "news_stale": True},
            "candidates": [_pm_row(s, vol=1.0, news=0) for s in "ABCDEF"],
        }
        report = self._run(mock_fetch, payload)

        kinds = {a["kind"] for a in report["anomalies"]}
        self.assertNotIn("DATA_INCOMPLETE", kinds)
        mock_escalate.assert_not_called()

    @patch.object(UIInspectorAgent, "_escalate_to_coding_agent")
    @patch.object(UIInspectorAgent, "_fetch_json")
    def test_small_slate_skips_coverage_checks(self, mock_fetch, mock_escalate):
        """2 candidates cannot distinguish systemic gaps from honest absence
        (a name simply has no upcoming earnings most days)."""
        payload = {
            "as_of_utc": _iso_ago(seconds=30),
            "refresh_seconds": 300,
            "flags": {"news_available": True, "news_stale": False},
            "candidates": [_pm_row("A", vol=None, news=0), _pm_row("B", vol=None, news=0)],
        }
        report = self._run(mock_fetch, payload)

        self.assertEqual(report["anomalies_detected"], 0)
        self.assertNotIn("field_coverage", report["tab_health"]["premarket"])
        mock_escalate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
