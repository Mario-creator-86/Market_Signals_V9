import copy
import json
from pathlib import Path

import pytest

from api.agents_service import (
    RISK_CONSTITUTION_FIELD_NAMES,
    RISK_TRADE_POLICY_FIELD_NAMES,
    RiskConstitutionSettingsError,
    assert_no_risk_manager_owned_updates,
    get_risk_constitution_settings,
    get_risk_constitution_profiles,
    set_risk_constitution,
)


def _values(**overrides):
    values = {
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
    values.update(overrides)
    return values


class _Config:
    def __init__(self):
        self.config = {
            "risk_constitution": {
                "owner_confirmed": False,
                **{name: None for name in RISK_CONSTITUTION_FIELD_NAMES},
            },
            "agents": {"risk": {}, "execution": {}},
            "bot": {},
            "strategy_v2": {},
            "untouched": {"keep": True},
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
        self.calls.append(copy.deepcopy(list(updates)))
        for path, value in updates:
            target = self.config
            for part in path[:-1]:
                target = target.setdefault(part, {})
            target[path[-1]] = copy.deepcopy(value)


def _trade_values(**overrides):
    values = get_risk_constitution_settings(_Config())["trade_policy"]["draft"]
    values["reduce_fraction_pct"] = 20.0
    values.update(overrides)
    assert set(values) == set(RISK_TRADE_POLICY_FIELD_NAMES)
    return values


def test_unconfigured_contract_exposes_schema_without_inventing_choices():
    snapshot = get_risk_constitution_settings(_Config())

    assert snapshot["status"] == "unconfigured"
    assert snapshot["active"] is None
    assert snapshot["can_activate"] is False
    assert set(snapshot["draft"]) == set(RISK_CONSTITUTION_FIELD_NAMES)
    assert all(value is None for value in snapshot["draft"].values())
    assert [field["name"] for field in snapshot["schema"]] == list(
        RISK_CONSTITUTION_FIELD_NAMES
    )


def test_save_draft_does_not_replace_or_activate_current_policy():
    config = _Config()

    result = set_risk_constitution(
        config=config,
        values=_values(),
        owner_confirmed=False,
    )

    assert result["mode"] == "draft"
    assert result["applied_live"] is False
    assert result["synced_legacy_guards"] == []
    assert config.config["risk_constitution"]["owner_confirmed"] is False
    assert config.config["risk_constitution_draft"] == {
        "owner_confirmed": False,
        **_values(),
    }
    assert config.config["untouched"] == {"keep": True}
    assert len(config.calls) == 1
    assert len(config.calls[0]) == 1


def test_confirmed_values_activate_and_align_overlapping_live_guards_atomically():
    config = _Config()

    result = set_risk_constitution(
        config=config,
        values=_values(),
        owner_confirmed=True,
    )

    assert result["mode"] == "active"
    assert result["applied_live"] is True
    assert result["constitution"]["status"] == "active"
    assert len(result["constitution"]["fingerprint"]) == 64
    assert config.config["risk_constitution"] == {
        "owner_confirmed": True,
        **_values(),
    }
    assert config.agents["risk"]["max_daily_loss_pct"] == 2.0
    assert config.agents["risk"]["max_exposure_pct"] == 20.0
    assert config.agents["execution"]["max_open_trades"] == 6
    assert config.agents["execution"]["max_ai_leverage"] == 2
    assert config.bot["circuit_breaker_drawdown_pct"] == 8.0
    assert config.strategy_v2["decision_max_leverage"] == 2
    assert len(config.calls) == 1


def test_confirmed_risk_manager_policy_atomically_owns_trade_admission_sizing_and_exits(monkeypatch):
    config = _Config()
    # The config fixture must represent an entry-ready environment: activating
    # PAPER_AUTO is refused (fail-closed) when the execution agent is off or
    # reconciliation has not passed.
    config.config["agents"]["execution"] = {"enabled": True, "mode": "paper"}

    class _Store:
        def get_reconciliation(self):
            return {"status": "HEALTHY", "conflicts": [], "checked_at": "now"}

    class _Broker:
        store = _Store()

    monkeypatch.setattr("core.execution.runtime.get_paper_broker", lambda **kwargs: _Broker())

    policy = _trade_values(
        execution_mode="PAPER_AUTO",
        base_position_capital_pct=1.0,
        unprofitable_position_cap_pct=0.5,
        scanner_time_stop_minutes=0,
        trailing_stop_enabled=False,
        pre_earnings_min_gap_risk=37.0,
        pre_earnings_min_abs_bias=20.0,
        pre_earnings_require_canonical_decision=False,
    )

    result = set_risk_constitution(
        config=config,
        values=_values(),
        trade_policy_values=policy,
        owner_confirmed=True,
    )

    assert len(config.calls) == 1
    assert config.config["risk_trade_policy"] == {"owner_confirmed": True, **policy}
    assert config.config["autonomy"]["mode"] == "PAPER_AUTO"
    assert config.config["paper_execution"]["reduce_fraction_pct"] == 20.0
    assert config.config["agents"]["execution"]["balance_usage_pct"] == 1.0
    assert config.config["agents"]["execution"]["max_open_trades"] == 6
    assert config.config["agents"]["execution"]["max_ai_leverage"] == 2
    assert config.config["bot"]["trailing_enabled"] is False
    assert config.config["bot"]["scanner_time_stop_minutes"] == 0
    assert result["constitution"]["trade_policy"]["status"] == "active"
    assert result["constitution"]["trade_policy"]["runtime_drift"] == []


def test_trade_sizing_cannot_exceed_constitution_position_cap():
    config = _Config()
    with pytest.raises(RiskConstitutionSettingsError, match="cannot exceed"):
        set_risk_constitution(
            config=config,
            values=_values(max_position_size_pct=2.0),
            trade_policy_values=_trade_values(base_position_capital_pct=3.0),
            owner_confirmed=True,
        )
    assert config.calls == []


def test_generic_config_writer_cannot_bypass_risk_manager_owned_paths():
    with pytest.raises(RiskConstitutionSettingsError, match="owned by Risk Manager"):
        assert_no_risk_manager_owned_updates({
            "agents": {"execution": {"pre_earnings_min_gap_risk": 10}},
        })
    with pytest.raises(RiskConstitutionSettingsError, match="agents.execution"):
        assert_no_risk_manager_owned_updates({"agents": {"execution": None}})
    assert_no_risk_manager_owned_updates({"agents": {"execution": {"enabled": True}}})


def test_new_draft_preserves_an_already_active_policy():
    config = _Config()
    set_risk_constitution(config=config, values=_values(), owner_confirmed=True)
    active = copy.deepcopy(config.config["risk_constitution"])

    result = set_risk_constitution(
        config=config,
        values=_values(max_positions=4),
        owner_confirmed=False,
    )

    assert config.config["risk_constitution"] == active
    assert result["constitution"]["status"] == "draft_changes"
    assert result["constitution"]["active"]["max_positions"] == 6
    assert result["constitution"]["draft"]["max_positions"] == 4


@pytest.mark.parametrize(
    "values",
    [
        _values(max_positions=None),
        _values(max_leverage=6),
        _values(max_risk_per_trade_pct=3.0, max_daily_loss_pct=2.0),
        _values(max_position_size_pct=25.0, max_total_exposure_pct=20.0),
        _values(block_data_conflicts="true"),
    ],
)
def test_invalid_activation_is_rejected_without_writes(values):
    config = _Config()

    with pytest.raises(RiskConstitutionSettingsError):
        set_risk_constitution(
            config=config,
            values=values,
            owner_confirmed=True,
        )

    assert config.calls == []


def test_incomplete_values_are_valid_as_draft():
    config = _Config()
    values = {name: None for name in RISK_CONSTITUTION_FIELD_NAMES}
    values["max_positions"] = 3

    result = set_risk_constitution(
        config=config,
        values=values,
        owner_confirmed=False,
    )

    assert result["constitution"]["status"] == "draft"
    assert result["constitution"]["can_activate"] is False


def test_shipped_paper_profiles_are_external_complete_and_valid():
    profiles = get_risk_constitution_profiles()

    assert profiles["status"] == "ready"
    assert profiles["source"] == "risk_constitution_profiles.json"
    assert [item["id"] for item in profiles["items"]] == [
        "conservative",
        "balanced",
        "dynamic-paper",
    ]
    assert all(set(item["values"]) == set(RISK_CONSTITUTION_FIELD_NAMES) for item in profiles["items"])
    assert all(all(value is not None for value in item["values"].values()) for item in profiles["items"])


def test_invalid_profile_file_fails_visible_without_manufacturing_fallback(tmp_path: Path):
    path = tmp_path / "profiles.json"
    path.write_text('{"schema_version": 1, "profiles": []}', encoding="utf-8")

    profiles = get_risk_constitution_profiles(path)

    assert profiles["status"] == "error"
    assert profiles["items"] == []
    assert "must contain profiles" in profiles["error"]


def test_profile_file_hot_reloads_when_operator_edits_it(tmp_path: Path):
    source = Path(__file__).parents[2] / "risk_constitution_profiles.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    first = get_risk_constitution_profiles(path)

    document["profiles"][0]["label"] = "Low variance"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    second = get_risk_constitution_profiles(path)

    assert first["items"][0]["label"] == "Conservative"
    assert second["items"][0]["label"] == "Low variance"
