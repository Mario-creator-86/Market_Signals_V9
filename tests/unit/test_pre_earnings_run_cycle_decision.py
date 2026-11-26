import agents.pre_earnings_agent as pe


def test_attach_pre_earnings_decisions_is_method_on_agent():
    assert hasattr(pe.PreEarningsAgent, "_attach_pre_earnings_decisions")


def test_attach_is_noop_when_disabled(monkeypatch):
    # decision_enabled default False -> no item is touched, no engine call.
    called = {"n": 0}

    def _fake_eval(item, *, config, services=None):
        called["n"] += 1
        return None

    monkeypatch.setattr(pe, "evaluate_pre_earnings_decision", _fake_eval)
    agent = object.__new__(pe.PreEarningsAgent)
    agent._state = {"items": [{"symbol": "NVDA"}, {"symbol": "AAPL"}]}
    agent._attach_pre_earnings_decisions({"pre_earnings": {"decision_enabled": False, "decision_max_tickers": 25}})
    assert called["n"] == 0
    assert "pre_earnings_decision" not in agent._state["items"][0]


def test_attach_adds_decision_summary_when_enabled(monkeypatch):
    from core.ai.decision import Action, Decision
    from core.ai.decision_engine import EvaluationResult
    from core.ai.pipeline import ValidationResult

    def _fake_eval(item, *, config, services=None):
        dec = Decision(action=Action.OPEN_LONG, ticker=item["symbol"], confidence=0.8, thesis="t")
        return EvaluationResult(dec, ValidationResult(ok=True, decision=dec, stage="RISK_GATE"), None,
                                {"available": True, "recommendation": "approve", "confidence": 0.8,
                                 "reason": "t", "validation_stage": "RISK_GATE", "critic_veto": False}, True)

    monkeypatch.setattr(pe, "evaluate_pre_earnings_decision", _fake_eval)
    agent = object.__new__(pe.PreEarningsAgent)
    agent._state = {"items": [{"symbol": "NVDA", "price": 100}]}
    agent._attach_pre_earnings_decisions({"pre_earnings": {"decision_enabled": True, "decision_max_tickers": 25},
                                          "autonomy": {"mode": "PAPER_AUTO"}})
    d = agent._state["items"][0]["pre_earnings_decision"]
    assert d["action"] == "OPEN_LONG" and d["recommendation"] == "approve" and d["available"] is True


def test_attach_accepts_runtime_subsection_and_passes_full_config(monkeypatch):
    from core.ai.decision import Action, Decision
    from core.ai.decision_engine import EvaluationResult
    from core.ai.pipeline import ValidationResult

    full_config = {
        "pre_earnings": {"decision_enabled": True, "decision_max_tickers": 25},
        "autonomy": {"mode": "PAPER_AUTO"},
        "ai": {"decision": {"critic_enabled": True}},
    }

    def _fake_eval(item, *, config, services=None):
        assert config is full_config
        dec = Decision(action=Action.OPEN_LONG, ticker=item["symbol"], confidence=0.8, thesis="t")
        return EvaluationResult(
            dec,
            ValidationResult(ok=True, decision=dec, stage="RISK_GATE"),
            None,
            {
                "available": True,
                "recommendation": "approve",
                "confidence": 0.8,
                "reason": "t",
                "validation_stage": "RISK_GATE",
                "critic_veto": False,
            },
            True,
        )

    monkeypatch.setattr(pe, "evaluate_pre_earnings_decision", _fake_eval)
    agent = object.__new__(pe.PreEarningsAgent)
    agent.config_manager = type("Config", (), {"config": full_config})()
    agent._state = {"items": [{"symbol": "NVDA", "price": 100}]}

    agent._attach_pre_earnings_decisions(full_config["pre_earnings"])

    assert agent._state["items"][0]["pre_earnings_decision"]["action"] == "OPEN_LONG"


def test_attach_does_not_spend_ai_calls_on_ineligible_recommendations(monkeypatch):
    called = {"n": 0}

    def _fake_eval(item, *, config, services=None):
        called["n"] += 1
        raise AssertionError("ineligible recommendation must not reach AI")

    monkeypatch.setattr(pe, "evaluate_pre_earnings_decision", _fake_eval)
    agent = object.__new__(pe.PreEarningsAgent)
    agent._state = {
        "items": [
            {
                "symbol": "BLOCKED",
                "recommendation_eligible": False,
                "recommendation_blockers": ["event_session_unverified"],
            }
        ]
    }

    agent._attach_pre_earnings_decisions(
        {"pre_earnings": {"decision_enabled": True, "decision_max_tickers": 25}}
    )

    decision = agent._state["items"][0]["pre_earnings_decision"]
    assert called["n"] == 0
    assert decision["action"] == "WATCH"
    assert decision["available"] is False
    assert "event_session_unverified" in decision["reason"]


def test_attach_can_enrich_detached_items_without_mutating_live_state(monkeypatch):
    from core.ai.decision import Action, Decision
    from core.ai.decision_engine import EvaluationResult
    from core.ai.pipeline import ValidationResult

    def _fake_eval(item, *, config, services=None):
        dec = Decision(action=Action.OPEN_SHORT, ticker=item["symbol"], confidence=0.7, thesis="t")
        return EvaluationResult(
            dec,
            ValidationResult(ok=True, decision=dec, stage="RISK_GATE"),
            None,
            {
                "available": True,
                "recommendation": "approve",
                "confidence": 0.7,
                "reason": "t",
                "validation_stage": "RISK_GATE",
                "critic_veto": False,
            },
            True,
        )

    monkeypatch.setattr(pe, "evaluate_pre_earnings_decision", _fake_eval)
    agent = object.__new__(pe.PreEarningsAgent)
    agent._state = {"items": [{"symbol": "LIVE"}]}
    detached = [{"symbol": "COPY"}]

    result = agent._attach_pre_earnings_decisions(
        {"pre_earnings": {"decision_enabled": True, "decision_max_tickers": 25}},
        items=detached,
    )

    assert result is detached
    assert detached[0]["pre_earnings_decision"]["action"] == "OPEN_SHORT"
    assert "pre_earnings_decision" not in agent._state["items"][0]


def test_attach_bounded_by_max_tickers(monkeypatch):
    from core.ai.decision import Action, Decision
    from core.ai.decision_engine import EvaluationResult
    from core.ai.pipeline import ValidationResult

    def _fake_eval(item, *, config, services=None):
        dec = Decision(action=Action.WATCH, ticker=item["symbol"])
        return EvaluationResult(dec, ValidationResult(ok=True, decision=dec, stage="RISK_GATE"), None,
                                {"available": False, "recommendation": "watch", "confidence": None,
                                 "reason": "x", "validation_stage": "", "critic_veto": False}, False)

    monkeypatch.setattr(pe, "evaluate_pre_earnings_decision", _fake_eval)
    agent = object.__new__(pe.PreEarningsAgent)
    agent._state = {"items": [{"symbol": f"S{i}"} for i in range(5)]}
    agent._attach_pre_earnings_decisions({"pre_earnings": {"decision_enabled": True, "decision_max_tickers": 2},
                                          "autonomy": {"mode": "PAPER_AUTO"}})
    with_decision = [it for it in agent._state["items"] if "pre_earnings_decision" in it]
    assert len(with_decision) == 2  # bounded by decision_max_tickers
