import api.state_service as state_service
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from core.request_context import request_context
from api.state_service import (
    _build_capital_opportunities,
    _build_capital_payload,
    _extract_watchlist_signal_score,
    normalize_signal_payload_scores,
)


def test_extract_watchlist_signal_score_prefers_signal_score_0_100():
    payload = {"signal_score": 87, "probability_score": 0.91, "quality_score": 6}
    assert _extract_watchlist_signal_score(payload) == 87.0


def test_extract_watchlist_signal_score_scales_probability_0_1():
    payload = {"probability_score": 0.86}
    assert _extract_watchlist_signal_score(payload) == 86.0


def test_extract_watchlist_signal_score_scales_quality_0_10():
    payload = {"quality_score": 7}
    assert _extract_watchlist_signal_score(payload) == 70.0


def test_extract_watchlist_signal_score_clamps_bounds():
    assert _extract_watchlist_signal_score({"signal_score": 130}) == 100.0
    assert _extract_watchlist_signal_score({"signal_score": -5}) == 0.0


def test_extract_watchlist_signal_score_falls_back_from_zero_signal_score_to_raw_score():
    payload = {"signal_score": 0, "score": 0, "raw_score": 91}
    assert _extract_watchlist_signal_score(payload) == 91.0


def test_normalize_signal_payload_scores_backfills_public_score_fields():
    payload = {
        "open": {
            "sig-1": {"ticker": "BTC-USD", "signal_score": 0, "score": 0, "raw_score": 88}
        },
        "closed": [],
    }

    normalized = normalize_signal_payload_scores(payload)

    assert normalized["open"]["sig-1"]["signal_score"] == 88.0
    assert normalized["open"]["sig-1"]["score"] == 88.0


def test_capital_opportunities_expose_paper_position_metadata(monkeypatch):
    monkeypatch.setattr(
        state_service,
        "summarize_ticker_news_signals",
        lambda ticker: {"sentiment_trend": "bullish", "news_catalyst_score": 8, "top_headlines": {"7d": ["Catalyst"]}},
    )
    payload = {
        "open": {
            "sig-1": {
                "ticker": "AAPL",
                "action": "BUY",
                "signal_score": 0,
                "score": 0,
                "raw_score": 91,
                "paper_position_id": "pos-1",
                "paper_status": "OPEN",
                "paper_opened_at": "2026-04-26T10:00:00",
            }
        }
    }

    opportunities = _build_capital_opportunities([], payload)

    row = opportunities["signals"][0]
    assert row["signal_id"] == "sig-1"
    assert row["paper_position_id"] == "pos-1"
    assert row["paper_status"] == "OPEN"
    assert row["decision_status"] == "open_capital"
    assert row["execution_outcome"] == "capital_position_open"
    assert row["score"] == 91.0
    assert row["news_sentiment"] == "bullish"


def test_capital_opportunities_label_open_research_separately(monkeypatch):
    monkeypatch.setattr(state_service, "summarize_ticker_news_signals", lambda ticker: {})
    payload = {
        "open": {
            "sig-research": {
                "ticker": "SOXLB-USD",
                "action": "SHORT",
                "signal_score": 95,
                "paper_position_id": "research-pos",
                "paper_status": "OPEN",
                "research_only": True,
                "capital_execution_eligible": False,
                "capital_execution_block_reason": "Faithful V2 validation is missing",
            }
        }
    }

    opportunities = _build_capital_opportunities([], payload)

    row = opportunities["signals"][0]
    assert row["decision_status"] == "open_research"
    assert row["execution_outcome"] == "research_position_open"
    assert row["qualified"] is False
    assert row["reason"] == "Faithful V2 validation is missing"
    assert opportunities["signal_summary"]["open_capital"] == 0
    assert opportunities["signal_summary"]["open_research"] == 1
    assert opportunities["signal_summary"]["open_paper"] == 1


def test_capital_opportunities_expose_evidence_funnel_and_position_scopes(monkeypatch):
    monkeypatch.setattr(state_service, "summarize_ticker_news_signals", lambda ticker: {})
    payload = {
        "open": {
            "sig-research": {
                "ticker": "GIGGLE-USD",
                "action": "SHORT",
                "signal_score": 95,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "paper_position_id": "research-pos",
                "paper_status": "OPEN",
                "research_only": True,
                "capital_execution_eligible": False,
                "capital_entry_gate": {
                    "allowed": False,
                    "check": "backtest",
                    "status": "unfaithful_strategy_source",
                    "reason": "Automatic Capital execution requires a faithful V2 strategy validation",
                    "checks": {
                        "data": {"allowed": True, "status": "valid"},
                        "backtest": {
                            "allowed": False,
                            "status": "unfaithful_strategy_source",
                        },
                    },
                },
            }
        }
    }
    positions = [
        {
            "position_id": "research-pos",
            "ticker": "GIGGLE-USD",
            "status": "OPEN",
            "position_type": "research_cohort",
            "research_only": True,
        },
        {
            "position_id": "paper-v2",
            "ticker": "BTC-USD",
            "status": "OPEN",
            "position_type": "autonomous",
            "research_only": False,
            "paper_capital_experiment": True,
        },
    ]

    opportunities = _build_capital_opportunities([], payload, positions)

    assert opportunities["position_summary"] == {
        "total_open": 2,
        "capital_open": 1,
        "validated_capital_open": 0,
        "paper_experiment_open": 1,
        "research_open": 1,
    }
    assert opportunities["gate_funnel"]["current_signals"] == 1
    assert opportunities["gate_funnel"]["price_data_passed"] == 1
    assert opportunities["gate_funnel"]["faithful_oos_passed"] == 0
    assert opportunities["gate_funnel"]["research_positions_open"] == 1
    assert opportunities["blocker_summary"][0]["check"] == "backtest"
    assert opportunities["blocker_summary"][0]["count"] == 1
    assert opportunities["readiness"]["status"] == "paper_experiment_open"


def test_capital_opportunities_do_not_treat_score_as_gate_decision(monkeypatch):
    monkeypatch.setattr(state_service, "summarize_ticker_news_signals", lambda ticker: {})
    payload = {
        "open": {
            "sig-1": {
                "ticker": "BTC-USD",
                "action": "BUY",
                "signal_score": 99,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "research_only": True,
                "capital_execution_eligible": False,
                "capital_execution_block_reason": "Faithful OOS validation is missing",
            }
        }
    }

    row = _build_capital_opportunities([], payload)["signals"][0]

    assert row["qualified"] is False
    assert row["decision_status"] == "research_only"
    assert row["reason"] == "Faithful OOS validation is missing"


def test_explicit_capital_gate_failure_is_blocked_even_for_research_signal(monkeypatch):
    monkeypatch.setattr(state_service, "summarize_ticker_news_signals", lambda ticker: {})
    gate_reason = "Live and technical prices are not concordant"
    payload = {
        "open": {
            "sig-1": {
                "ticker": "UTK-USD",
                "action": "BUY",
                "signal_score": 95,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "research_only": True,
                "capital_execution_eligible": False,
                "capital_entry_gate": {
                    "allowed": False,
                    "check": "data",
                    "status": "unverified_price_integrity",
                    "reason": gate_reason,
                },
            }
        }
    }

    row = _build_capital_opportunities([], payload)["signals"][0]

    assert row["decision_status"] == "blocked"
    assert row["reason"] == gate_reason


def test_capital_opportunities_hide_expired_signals(monkeypatch):
    monkeypatch.setattr(state_service, "summarize_ticker_news_signals", lambda ticker: {})
    stale_time = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    payload = {
        "open": {
            "long": {"ticker": "UTK-USD", "action": "BUY", "signal_score": 95, "created_at": stale_time},
            "short": {"ticker": "UTK-USD", "action": "SHORT", "signal_score": 93, "created_at": stale_time},
        }
    }

    opportunities = _build_capital_opportunities([], payload)

    assert opportunities["signals"] == []
    assert opportunities["signal_summary"]["expired_hidden"] == 2


def test_capital_opportunities_collapse_current_direction_conflict(monkeypatch):
    monkeypatch.setattr(state_service, "summarize_ticker_news_signals", lambda ticker: {})
    created_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "open": {
            "long": {"ticker": "D-USD", "action": "BUY", "signal_score": 90, "created_at": created_at},
            "short": {"ticker": "D-USD", "action": "SHORT", "signal_score": 91, "created_at": created_at},
        }
    }

    opportunities = _build_capital_opportunities([], payload)

    assert len(opportunities["signals"]) == 1
    assert opportunities["signals"][0]["decision_status"] == "conflict"
    assert opportunities["signals"][0]["qualified"] is False
    assert opportunities["signal_summary"]["conflicts"] == 1


def test_capital_opportunities_require_recorded_allowed_gate(monkeypatch):
    monkeypatch.setattr(state_service, "summarize_ticker_news_signals", lambda ticker: {})
    payload = {
        "open": {
            "s7": {
                "ticker": "ETH-USD",
                "action": "BUY",
                "signal_score": 80,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "strategy_source": "V2_FORWARD_RESEARCH",
                "strategy_type": "S7",
                "research_only": False,
                "capital_execution_eligible": True,
                "capital_entry_gate": {
                    "allowed": True,
                    "check": "paper_s7_experiment",
                    "status": "paper_only",
                    "reason": "S7 paper checks passed",
                },
            }
        }
    }

    row = _build_capital_opportunities([], payload)["signals"][0]

    assert row["decision_status"] == "eligible"
    assert row["qualified"] is True
    assert row["reason"] == "S7 paper checks passed"


def test_capital_autonomous_status_requires_master_and_execution_pipeline(monkeypatch):
    def build(bot_enabled, execution_enabled, candidates_enabled):
        config = SimpleNamespace(
            agents={
                "execution": {
                    "enabled": execution_enabled,
                    "autonomous_candidates_enabled": candidates_enabled,
                },
                "scanner": {},
            },
            bot={"enabled": bot_enabled},
            config={},
        )
        monkeypatch.setattr(state_service, "get_shared_config", lambda: config)
        return _build_capital_opportunities([], {})

    assert build(True, True, True)["autonomous_enabled"] is True
    assert build(False, True, True)["autonomous_enabled"] is False
    assert build(True, False, True)["autonomous_enabled"] is False
    assert build(True, True, False)["autonomous_enabled"] is False


def test_capital_payload_enriches_open_positions_with_uncached_live_price(monkeypatch):
    calls = []

    class FakeCapitalManager:
        def get_positions(self):
            return [
                {
                    "position_id": "pos-1",
                    "ticker": "BTC-USD",
                    "direction": "LONG",
                    "entry_price": 100.0,
                    "qty": 2.0,
                    "status": "OPEN",
                }
            ]

        def _get_execution_price(self, ticker, side, **kwargs):
            calls.append((ticker, side, kwargs))
            return {
                "execution_price": 110.0,
                "execution_price_quality": {
                    "value": 110.0,
                    "level": "VERIFIED",
                    "source": "Binance_orderbook",
                    "is_valid": True,
                },
                "price_timestamp": "2026-04-26T12:00:00",
                "source": "Binance_orderbook",
            }

        def get_balance(self):
            return 1000.0

        def get_history(self):
            return []

        def get_equity_curve(self, limit=30):
            return []

        def get_ledger(self, limit=20):
            return [
                {
                    "timestamp": "2026-04-26T11:00:00+00:00",
                    "type": "OPEN_POSITION",
                    "amount": -100.0,
                    "balance": 900.0,
                    "raw_balance": 900.0,
                    "account_scope": "capital",
                    "affects_deployable_capital": True,
                },
                {
                    "timestamp": "2026-04-26T11:01:00+00:00",
                    "type": "OPEN_POSITION",
                    "amount": -25.0,
                    "balance": 875.0,
                    "raw_balance": 875.0,
                    "account_scope": "research",
                    "affects_deployable_capital": False,
                },
            ][-limit:]

    monkeypatch.setattr(state_service, "get_capital_manager", lambda: FakeCapitalManager())
    monkeypatch.setattr(state_service, "get_trade_audit", lambda limit=20: [])
    monkeypatch.setattr(
        state_service,
        "_build_capital_opportunities",
        lambda tickers, signals, positions=None: {},
    )

    with request_context(reason="test", path="/api/state", cache_only=True):
        payload = _build_capital_payload([], {}, fast_only=False)
    position = payload["positions"][0]

    assert calls == [("BTC-USD", "SELL", {"use_cache": False})]
    assert position["current_price"] == 110.0
    assert position["current_price_live"] is True
    assert position["current_price_source"] == "Binance_orderbook"
    assert position["current_price_side"] == "SELL"
    assert position["current_pnl"] == 20.0
    assert position["current_pnl_pct"] == 10.0
    assert len(payload["ledger"]) == 2
    assert len(payload["history"]) == 1
    assert payload["history"][0]["account_scope"] == "capital"
    assert payload["accounting"]["recent_capital_ledger_entries"] == 1
    assert payload["accounting"]["recent_research_ledger_entries"] == 1
    assert payload["accounting"]["recent_unclassified_ledger_entries"] == 0
    assert payload["accounting"]["ledger_reconciliation_status"] == "reconciled"


def test_fast_capital_payload_uses_cross_process_verified_quote(monkeypatch):
    class FakeCapitalManager:
        def get_positions(self):
            return [
                {
                    "position_id": "pos-cache",
                    "ticker": "BTC-USD",
                    "direction": "LONG",
                    "entry_price": 100.0,
                    "qty": 2.0,
                    "status": "OPEN",
                    "commission_rate": 0.001,
                    "commission_usd": 0.2,
                }
            ]

        def get_balance(self):
            return 1000.0

        def get_equity_curve(self, limit=90):
            return []

        def get_ledger(self, limit=100):
            return []

    monkeypatch.setattr(state_service, "get_capital_manager", lambda: FakeCapitalManager())
    monkeypatch.setattr(state_service, "get_trade_audit", lambda limit=20: [])
    monkeypatch.setattr(
        state_service,
        "_build_capital_opportunities",
        lambda tickers, signals, positions=None: {},
    )
    monkeypatch.setattr(
        "core.live_price_cache.get_live_prices",
        lambda symbols: {
            "BTC-USD": {
                "price": 110.0,
                "source": "Binance_orderbook_mid",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        },
    )

    payload = _build_capital_payload(
        [],
        {},
        fast_only=True,
        include_opportunities=False,
        include_decisions=False,
    )
    position = payload["positions"][0]

    assert position["current_price"] == 110.0
    assert position["current_price_live"] is True
    assert position["current_price_source"] == "Binance_orderbook_mid"
    assert position["current_pnl"] == 20.0
    assert position["unrealized_pnl"] == 19.58
