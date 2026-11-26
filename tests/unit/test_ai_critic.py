"""§44 AI critic — independent challenge of an important decision."""
import pytest

from core.ai.critic import CRITIC_CATEGORIES, CriticVerdict, run_critic
from core.ai.decision import Action, Decision
from core.ai.provider import AIResponse


def _dec(action=Action.OPEN_LONG, conf=0.8):
    return Decision(action=action, ticker="NVDA", confidence=conf,
                    thesis="momentum", supporting_evidence=["s1"], opposing_evidence=["o1"])


class _FakeProvider:
    name = "fake"

    def __init__(self, text):
        self._text = text

    def generate(self, prompt, *, model=None, system="", **opts):
        return AIResponse(text=self._text, model=model or "critic-m")


def test_critic_categories_are_the_ten_spec_44_issues():
    assert CRITIC_CATEGORIES == (
        "contradictory_evidence", "stale_information", "provider_conflict",
        "weak_backtest", "fragile_parameters", "poor_sample_size",
        "excessive_volatility", "concentration", "catalyst_risk", "regime_mismatch",
    )


def test_critic_verdict_is_frozen():
    v = CriticVerdict(challenges=("x",), categories=("concentration",), severity=0.4, veto=True)
    with pytest.raises(Exception):
        v.veto = False  # frozen dataclass


def test_run_critic_critical_issue_returns_veto_verdict_even_for_good_setup():
    text = ('{"challenges":["concentration too high"],"categories":["concentration"],'
            '"severity":0.96,"veto":true,"rationale":"too concentrated"}')
    v = run_critic(_dec(), {"ticker": "NVDA", "signal_score": 86}, config={},
                   services={"critic_provider": _FakeProvider(text), "critic_model": "critic-m"})
    assert v.veto is True
    assert v.model_veto is True
    assert "concentration" in v.categories
    assert v.severity == 0.96
    assert v.quality_score == 86


def test_run_critic_good_setup_downgrades_moderate_model_veto_to_size_reduction():
    text = ('{"challenges":["ordinary event uncertainty"],"categories":["catalyst_risk"],'
            '"severity":0.78,"veto":true,"rationale":"size down"}')
    # Wave 3 (2026-11-15): the score band may override a model veto only when
    # the score is MEASURED (ai.decision.veto_override_requires_validated_score,
    # default on). The setup therefore carries a validation score here; the
    # rule-score-only case is pinned in tests/unit/test_wave3_decision_path.py.
    v = run_critic(
        _dec(),
        {"ticker": "NVDA", "signal_score": 84, "validation_score": 84},
        config={},
        services={"critic_provider": _FakeProvider(text)},
    )

    assert v.model_veto is True
    assert v.veto is False
    assert v.veto_threshold == 0.90
    assert 0.5 <= v.size_factor < 1.0
    assert v.policy_reason.startswith("model_veto_downgraded")


def test_run_critic_good_score_with_low_ai_confidence_vetoes_serious_model_finding():
    text = ('{"challenges":["evidence does not support the direction"],'
            '"categories":["contradictory_evidence"],'
            '"severity":0.75,"veto":true,"rationale":"low conviction"}')
    v = run_critic(
        _dec(conf=0.45),
        {"ticker": "NVDA", "signal_score": 84},
        config={},
        services={"critic_provider": _FakeProvider(text)},
    )

    assert v.veto is True
    assert v.veto_threshold == 0.75
    assert v.policy_reason.startswith("veto:low_confidence")


def test_run_critic_good_score_vetoes_multiple_structural_conflicts():
    text = ('{"challenges":["providers conflict and evidence is contradictory"],'
            '"categories":["contradictory_evidence","provider_conflict"],'
            '"severity":0.82,"veto":true,"rationale":"structural conflict"}')
    v = run_critic(
        _dec(conf=0.70),
        {"ticker": "NVDA", "signal_score": 86},
        config={},
        services={"critic_provider": _FakeProvider(text)},
    )

    assert v.veto is True
    assert v.veto_threshold == 0.80
    assert v.policy_reason.startswith("veto:structural_conflict")


def test_run_critic_good_setup_keeps_single_catalyst_concern_permissive():
    text = ('{"challenges":["event uncertainty"],"categories":["catalyst_risk"],'
            '"severity":0.82,"veto":true,"rationale":"reduce exposure"}')
    # Measured validation score supplied on purpose (wave 3: a rule score
    # alone no longer overrides a model veto).
    v = run_critic(
        _dec(conf=0.80),
        {"ticker": "NVDA", "signal_score": 84, "validation_score": 84},
        config={},
        services={"critic_provider": _FakeProvider(text)},
    )

    assert v.veto is False
    assert v.veto_threshold == 0.90
    assert 0.5 <= v.size_factor < 1.0


def test_run_critic_poor_setup_vetoes_concrete_severe_issue_without_model_veto():
    text = ('{"challenges":["weak evidence and unstable parameters"],'
            '"categories":["weak_backtest","fragile_parameters"],'
            '"severity":0.70,"veto":false,"rationale":"poor setup"}')
    v = run_critic(
        _dec(conf=0.6),
        {"ticker": "NVDA", "signal_score": 60},
        config={},
        services={"critic_provider": _FakeProvider(text)},
    )

    assert v.model_veto is False
    assert v.veto is True
    assert v.veto_threshold == 0.65
    assert v.policy_reason.startswith("veto:poor_setup")


def test_run_critic_veto_false_keeps_decision_action():
    text = '{"challenges":[],"categories":[],"severity":0.1,"veto":false,"rationale":"ok"}'
    v = run_critic(_dec(), {"ticker": "NVDA"}, config={},
                   services={"critic_provider": _FakeProvider(text)})
    assert v.veto is False


def test_run_critic_normalizes_fractional_signal_score():
    text = '{"challenges":[],"categories":[],"severity":0.1,"veto":false,"rationale":"ok"}'
    v = run_critic(_dec(), {"ticker": "NVDA", "signal_score": 0.82}, config={},
                   services={"critic_provider": _FakeProvider(text)})
    assert v.quality_score == 82.0


def test_run_critic_unparseable_forces_no_trade_when_configured():
    v = run_critic(_dec(), {"ticker": "NVDA"},
                   config={"ai": {"decision": {"critic_force_no_trade_on_fail": True}}},
                   services={"critic_provider": _FakeProvider("not json")})
    assert v.veto is True
    assert "unparseable" in v.rationale


def test_run_critic_unparseable_does_not_veto_when_not_configured():
    v = run_critic(_dec(), {"ticker": "NVDA"},
                   config={"ai": {"decision": {"critic_force_no_trade_on_fail": False}}},
                   services={"critic_provider": _FakeProvider("not json")})
    assert v.veto is False


def test_run_critic_unparseable_good_paper_setup_vetoes_by_default():
    v = run_critic(
        _dec(),
        {"ticker": "NVDA", "signal_score": 84},
        config={"bot": {"mode": "paper"}},
        services={"critic_provider": _FakeProvider("not json")},
    )

    assert v.veto is True


def test_run_critic_unparseable_good_setup_in_paper_mode_reduces_instead_of_vetoing():
    # Measured validation score supplied on purpose (wave 3: the opt-in
    # fail-open shortcut needs a measured score as well).
    v = run_critic(
        _dec(),
        {"ticker": "NVDA", "signal_score": 84, "validation_score": 84},
        config={
            "bot": {"mode": "paper"},
            "ai": {"decision": {
                "critic_force_no_trade_on_fail": True,
                "critic_fail_open_good_paper_setup": True,
            }},
        },
        services={"critic_provider": _FakeProvider("not json")},
    )

    assert v.veto is False
    assert v.quality_score == 84.0
    assert v.size_factor == 0.5
    assert v.policy_reason.startswith("critic_failure_good_paper_setup_reduced")


def test_run_critic_unparseable_poor_setup_in_paper_mode_still_vetoes():
    v = run_critic(
        _dec(conf=0.55),
        {"ticker": "NVDA", "signal_score": 55},
        config={
            "bot": {"mode": "paper"},
            "ai": {"decision": {"critic_force_no_trade_on_fail": True}},
        },
        services={"critic_provider": _FakeProvider("not json")},
    )

    assert v.veto is True
    assert v.quality_score == 55.0
    assert v.policy_reason.startswith("critic_failure_veto")


def test_run_critic_unparseable_good_score_but_low_confidence_still_vetoes():
    v = run_critic(
        _dec(conf=0.45),
        {"ticker": "NVDA", "signal_score": 84},
        config={
            "bot": {"mode": "paper"},
            "ai": {"decision": {"critic_force_no_trade_on_fail": True}},
        },
        services={"critic_provider": _FakeProvider("not json")},
    )

    assert v.veto is True
    assert v.policy_reason.startswith("critic_failure_veto")


def test_run_critic_no_provider_forces_no_trade_when_configured():
    # critic_provider explicitly None and empty config -> nothing resolvable -> critic fail -> veto
    v = run_critic(_dec(), {"ticker": "NVDA"}, config={},
                   services={"critic_provider": None})
    assert v.veto is True  # default critic_force_no_trade_on_fail True


def test_run_critic_drops_unknown_categories():
    text = '{"challenges":[],"categories":["concentration","bogus"],"severity":0.2,"veto":false,"rationale":""}'
    v = run_critic(_dec(), {"ticker": "NVDA"}, config={},
                   services={"critic_provider": _FakeProvider(text)})
    assert v.categories == ("concentration",)


def test_configured_risk_critic_model_is_routed_and_recorded(monkeypatch):
    provider = _FakeProvider(
        '{"challenges":[],"categories":[],"severity":0.1,"veto":false,"rationale":"ok"}'
    )
    seen = {}

    def fake_build(_config, name):
        seen["provider"] = name
        return provider

    monkeypatch.setattr("api.ai_config_service.build_provider", fake_build)
    config = {
        "ai": {
            "providers": {"ollama": {"enabled": True}},
            "assignment": {"risk_critic": "independent-risk-model"},
        }
    }

    verdict = run_critic(_dec(), {"ticker": "NVDA"}, config=config)

    assert seen["provider"] == "ollama"
    assert verdict.provider == "fake"
    assert verdict.model == "independent-risk-model"
    assert verdict.prompt_version == "risk-critic-v1"
