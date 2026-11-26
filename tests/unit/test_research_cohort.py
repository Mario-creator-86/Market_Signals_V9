from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from core.agent_manager import AgentManager
from core.research_cohort import evaluate_research_cohort_entry, research_cohort_notional


def _signal(**overrides):
    payload = {
        "id": "cohort_signal_1",
        "ticker": "BTC-USD",
        "action": "BUY",
        "direction": "LONG",
        "_autonomous_candidate": True,
        "autonomous_source": "scanner",
        "strategy_type": "SCANNER_AI_LONG",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "signal_score": 91.0,
        "entry": 100.0,
        "sl": 98.0,
        "tp": 104.0,
    }
    payload.update(overrides)
    return payload


def _config(**overrides):
    cohort = {
        "enabled": True,
        "paper_only": True,
        "allowed_sources": ["scanner"],
        "max_open_positions": 1,
        "max_signal_age_seconds": 180,
        "min_signal_score": 85.0,
        "min_risk_reward_ratio": 1.5,
        "max_entry_deviation_pct": 3.0,
        "notional_pct_of_balance": 0.25,
        "min_notional_usd": 25.0,
        "max_notional_usd": 250.0,
    }
    cohort.update(overrides)
    return {"mode": "paper", "research_cohort": cohort}


def _news_allowed():
    return {
        "allowed": True,
        "status": "allowed",
        "reason": "fresh source-backed AI News evidence",
        "context": {"article_count_24h": 1, "source_backed_ai_analyzed_count_24h": 1},
    }


def test_research_cohort_requires_strict_forward_paper_evidence():
    result = evaluate_research_cohort_entry(
        _signal(),
        _config(),
        [],
        news_decision=_news_allowed(),
    )

    assert result["allowed"] is True
    assert result["position_type"] == "research_cohort"
    assert result["research_only"] is True
    assert result["metrics"]["risk_reward_ratio"] == 2.0
    assert result["metrics"]["stop_loss_pct"] == 2.0
    assert result["metrics"]["take_profit_pct"] == 4.0


def test_research_cohort_accepts_risk_reward_exactly_at_threshold():
    result = evaluate_research_cohort_entry(
        _signal(tp=103.0),
        _config(min_risk_reward_ratio=1.5),
        [],
        news_decision=_news_allowed(),
    )

    assert result["allowed"] is True
    assert result["metrics"]["risk_reward_ratio"] == 1.5


def test_research_cohort_rejects_stale_signal_before_opening():
    result = evaluate_research_cohort_entry(
        _signal(created_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()),
        _config(),
        [],
        news_decision=_news_allowed(),
    )

    assert result["allowed"] is False
    assert result["status"] == "stale_signal"
    assert "exceeds research limit" in result["reason"]


def test_research_cohort_rejects_duplicate_or_missing_news_evidence():
    duplicate = {
        "ticker": "BTC-USD",
        "direction": "LONG",
        "status": "OPEN",
        "position_type": "research_cohort",
        "research_only": True,
    }
    duplicate_result = evaluate_research_cohort_entry(
        _signal(), _config(), [duplicate], news_decision=_news_allowed()
    )
    assert duplicate_result["allowed"] is False
    assert duplicate_result["status"] == "duplicate_open"

    news_result = evaluate_research_cohort_entry(
        _signal(),
        _config(),
        [],
        news_decision={"allowed": False, "status": "unverifiable_sources", "reason": "No source-backed AI News"},
    )
    assert news_result["allowed"] is False
    assert news_result["status"] == "news_gate"


def test_price_first_research_allows_missing_news_but_not_a_verified_conflict():
    config = _config(news_policy={"allow_missing_context": True})
    missing = evaluate_research_cohort_entry(
        _signal(),
        config,
        [],
        news_decision={"allowed": False, "status": "insufficient_evidence", "reason": "No recent article"},
    )
    assert missing["allowed"] is True
    assert missing["news_gate"]["status"] == "missing_context_price_first"
    assert missing["news_gate"]["research_news_policy"]["capital_policy_changed"] is False

    conflict = evaluate_research_cohort_entry(
        _signal(),
        config,
        [],
        news_decision={"allowed": False, "status": "direction_conflict", "reason": "Bearish AI news"},
    )
    assert conflict["allowed"] is False
    assert conflict["status"] == "news_gate"


def test_price_first_research_allows_missing_news_for_major_crypto_when_contextual():
    result = evaluate_research_cohort_entry(
        _signal(),
        _config(news_policy={"allow_missing_context": True, "require_context_for_major_crypto": False}),
        [],
        news_decision={
            "allowed": False,
            "status": "insufficient_evidence",
            "reason": "No recent article",
            "crypto_policy": {"is_major_crypto": True},
        },
    )
    assert result["allowed"] is True
    assert result["news_gate"]["status"] == "missing_context_price_first"


def test_research_cohort_can_require_an_explicit_v2_forward_source():
    config = _config(
        allowed_sources=["v2_forward"],
        allowed_strategy_sources=["V2_FORWARD_RESEARCH"],
    )
    rejected = evaluate_research_cohort_entry(
        _signal(autonomous_source="v2_forward", strategy_source="V1"),
        config,
        [],
        news_decision=_news_allowed(),
    )
    accepted = evaluate_research_cohort_entry(
        _signal(autonomous_source="v2_forward", strategy_source="V2_FORWARD_RESEARCH"),
        config,
        [],
        news_decision=_news_allowed(),
    )

    assert rejected["status"] == "strategy_source_not_allowed"
    assert accepted["allowed"] is True


def test_research_cohort_accepts_engine_s7_as_isolated_v2_research():
    result = evaluate_research_cohort_entry(
        _signal(
            ticker="GLD",
            asset_class="COMMODITIES",
            autonomous_source="v2_engine_research",
            strategy_source="V2",
            strategy_type="S7_LIQUIDITY_SWEEP_REVERSAL",
            signal_score=67.2,
            tp=102.4,
        ),
        _config(
            allowed_sources=["v2_engine_research"],
            allowed_strategy_sources=["V2"],
            min_signal_score=67.2,
            min_risk_reward_ratio=1.2,
        ),
        [],
        news_decision=_news_allowed(),
    )

    assert result["allowed"] is True
    assert result["research_only"] is True
    assert result["metrics"]["risk_reward_ratio"] == 1.2


def test_research_notional_is_small_and_bounded():
    assert research_cohort_notional(100_000.0, _config()) == 250.0
    assert research_cohort_notional(10_000.0, _config()) == 25.0


def test_agent_manager_opens_only_an_isolated_research_position():
    signal = _signal()
    fake_capital = MagicMock()
    fake_capital.get_balance.return_value = 10_000.0
    fake_capital.open_position.return_value = {
        "position_id": "pos_research_1",
        "ticker": "BTC-USD",
        "direction": "LONG",
        "position_type": "research_cohort",
        "research_only": True,
        "entry_price": 100.0,
    }
    fake_signals = MagicMock()

    class ManagerStub:
        config_manager = MagicMock(config={"autonomy": {"mode": "PAPER_AUTO"}})
        _autonomy_order_gate = AgentManager._autonomy_order_gate

        def _ensure_autonomous_signal_registered(self, candidate):
            return True

    with patch("core.research_cohort.evaluate_autonomous_news", return_value=_news_allowed()), \
         patch("core.capital_manager.get_capital_manager", return_value=fake_capital), \
         patch("core.signals.get_signal_manager", return_value=fake_signals), \
         patch("core.agent_manager.record_autonomous_news_evidence"), \
         patch("core.agent_manager.record_trade_event"):
        result = AgentManager._attempt_research_cohort_entry(
            ManagerStub(),
            signal,
            {"accepted": True, "reason": "accepted", "checks": [], "metrics": {}},
            _config(),
            [],
            {"allowed": False, "reason": "Missing V2 evidence"},
        )

    assert result["opened"] is True
    kwargs = fake_capital.open_position.call_args.kwargs
    assert kwargs["position_type"] == "research_cohort"
    assert kwargs["research_only"] is True
    assert kwargs["leverage"] == 1
    assert kwargs["size_usd"] == 25.0
    assert kwargs["stop_loss_pct"] == 0.02
    assert kwargs["take_profit_pct"] == 0.04
    fake_signals.attach_paper_position.assert_called_once_with("cohort_signal_1", fake_capital.open_position.return_value)


def test_research_cohort_data_gate_stops_before_news_ai_and_position_open():
    signal = _signal()
    gate_reason = "Live and technical prices are not concordant"

    with patch("core.research_cohort.evaluate_research_cohort_entry") as evaluate_entry, \
         patch("core.agent_manager.evaluate_trade_decision_with_ollama") as ai_review, \
         patch("core.agent_manager.record_trade_event") as record_event:
        result = AgentManager._attempt_research_cohort_entry(
            object(),
            signal,
            {"accepted": True},
            _config(),
            [],
            {
                "allowed": False,
                "check": "data",
                "status": "unverified_price_integrity",
                "reason": gate_reason,
            },
        )

    assert result["opened"] is False
    assert result["status"] == "data_gate"
    assert gate_reason in result["reason"]
    evaluate_entry.assert_not_called()
    ai_review.assert_not_called()
    assert record_event.call_args.kwargs["status"] == "data_gate"


def test_research_position_persists_ai_review_when_enabled():
    signal = _signal(
        autonomous_source="v2_engine_research",
        strategy_source="V2",
        strategy_type="S7_LIQUIDITY_SWEEP_REVERSAL",
    )
    fake_capital = MagicMock()
    fake_capital.get_balance.return_value = 10_000.0
    fake_capital.open_position.return_value = {
        "position_id": "pos_research_ai_1",
        "ticker": "BTC-USD",
        "direction": "LONG",
        "position_type": "research_cohort",
        "research_only": True,
        "entry_price": 100.0,
    }
    review = {
        "available": True,
        "provider": "ollama_local",
        "recommendation": "approve",
        "confidence": 0.81,
        "reason": "Sweep reclaim and CHOCH support the paper thesis.",
        "risks": ["Forward-only observation"],
    }

    class ManagerStub:
        config_manager = MagicMock(config={"ollama": {"decision_veto_enabled": True},
                                           "autonomy": {"mode": "PAPER_AUTO"}})
        _autonomy_order_gate = AgentManager._autonomy_order_gate

        def _ensure_autonomous_signal_registered(self, candidate):
            return True

    with patch("core.research_cohort.evaluate_autonomous_news", return_value=_news_allowed()), \
         patch("core.agent_manager.evaluate_trade_decision_with_ollama", return_value=review), \
         patch("core.capital_manager.get_capital_manager", return_value=fake_capital), \
         patch("core.signals.get_signal_manager", return_value=MagicMock()), \
         patch("core.agent_manager.record_autonomous_news_evidence"), \
         patch("core.agent_manager.record_trade_event"):
        result = AgentManager._attempt_research_cohort_entry(
            ManagerStub(),
            signal,
            {"accepted": True, "reason": "accepted", "checks": [], "metrics": {}},
            _config(
                allowed_sources=["v2_engine_research"],
                allowed_strategy_sources=["V2"],
                ai_review_enabled=True,
                block_ai_veto=True,
            ),
            [],
            {"allowed": False, "reason": "Faithful OOS validation is pending"},
        )

    assert result["opened"] is True
    decision_context = fake_capital.open_position.call_args.kwargs["decision_context"]
    assert decision_context["ollama"]["recommendation"] == "approve"
    assert decision_context["execution"]["analysis_provider"] == "ollama_local"
