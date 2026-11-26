"""Phase 13 unit tests — §88 Model Lab: identical inputs, per-role isolation."""
import json

import pytest

from core.ai.provider import AIResponse
from core.decision_ledger.model_lab import MODEL_LAB_ROLES, ModelLab, validate_role
from core.decision_ledger.store import DecisionLedgerStore


class ScriptedProvider:
    name = "stub"

    def __init__(self, action="OPEN_LONG", model="model-a"):
        self.action = action
        self.model = model
        self.prompts = []

    def generate(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return AIResponse(text=json.dumps({
            "action": self.action, "ticker": "MSFT", "horizon": "SWING",
            "confidence": 0.6, "strategy_id": "MOM", "thesis": "ok",
            "supporting_evidence": ["e"], "opposing_evidence": [], "risk_flags": [],
            "data_quality": 0.9, "requires_more_data": False,
        }), model=self.model)


_NO_CRITIC_CONFIG = {"ai": {"decision": {"critic_enabled": False}}}


def _permissive_services():
    from core.pre_earnings.decision import _pe_default_services

    return _pe_default_services()


def _signal():
    return {"ticker": "MSFT", "entry": 300.0, "tp": 315.0, "sl": 292.5,
            "signal_score": 0.7, "horizon": "SWING", "strategy_type": "MOMENTUM"}


def test_all_models_receive_identical_inputs(tmp_path):
    lab = ModelLab(DecisionLedgerStore(tmp_path / "ledger.db"))
    provider_a = ScriptedProvider(model="model-a")
    provider_b = ScriptedProvider(action="NO_TRADE", model="model-b")
    run = lab.run(
        role="quant", signal=_signal(), config=_NO_CRITIC_CONFIG,
        candidates=[{"provider": provider_a, "model": "model-a"},
                    {"provider": provider_b, "model": "model-b"}],
        services=_permissive_services(),
    )
    assert run["inputs_sha256"]
    assert provider_a.prompts and provider_a.prompts == provider_b.prompts  # identical prompt
    actions = {result["action"] for result in run["results"]}
    assert len(actions) == 2  # models genuinely disagreed on the same input
    assert run["inter_model_agreement"] == 0.5


def test_role_validation_rejects_unknown_role(tmp_path):
    lab = ModelLab(DecisionLedgerStore(tmp_path / "ledger.db"))
    with pytest.raises(ValueError):
        lab.run(role="global_score", signal=_signal(), config=_NO_CRITIC_CONFIG,
                candidates=[{"provider": ScriptedProvider(), "model": "m"}],
                services=_permissive_services())
    assert "global_score" not in MODEL_LAB_ROLES
    assert validate_role("Risk_Critic") == "risk_critic"


def test_missing_provider_recorded_as_unavailable_never_simulated(tmp_path):
    lab = ModelLab(DecisionLedgerStore(tmp_path / "ledger.db"))
    run = lab.run(
        role="news_analyst", signal=_signal(), config=_NO_CRITIC_CONFIG,
        candidates=[{"name": "ghost", "model": "ghost-1"}],
        services=_permissive_services(),
    )
    assert run["results"][0]["ai_available"] is False
    assert run["results"][0]["error"] == "provider_not_injected"


def test_role_summary_is_per_role_only(tmp_path):
    store = DecisionLedgerStore(tmp_path / "ledger.db")
    lab = ModelLab(store)
    for role in ("quant", "risk_critic"):
        lab.run(
            role=role, signal=_signal(), config=_NO_CRITIC_CONFIG,
            candidates=[{"provider": ScriptedProvider(), "model": "m"}],
            services=_permissive_services(),
        )
    summary = lab.role_summary()
    assert set(summary["roles"].keys()) == {"quant", "risk_critic"}
    assert all("runs" in bucket and "actions" in bucket for bucket in summary["roles"].values())
    # There is deliberately no cross-role aggregate key.
    assert not any(key in summary for key in ("global_score", "overall_score", "ai_score"))


def test_run_is_idempotent_on_run_id(tmp_path):
    store = DecisionLedgerStore(tmp_path / "ledger.db")
    lab = ModelLab(store)
    run = lab.run(
        role="review", signal=_signal(), config=_NO_CRITIC_CONFIG,
        candidates=[{"provider": ScriptedProvider(), "model": "m"}],
        services=_permissive_services(),
    )
    assert store.append_model_lab_run(
        {"run_id": run["run_id"], "role": "review", "inputs_sha256": run["inputs_sha256"]},
        run["results"],
    ) is False
