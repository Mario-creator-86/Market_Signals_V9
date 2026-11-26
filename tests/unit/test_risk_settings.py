import json

import pytest

from api.agents_service import RiskConstitutionSettingsError, set_risk_settings
from core.config import ConfigManager
from core.risk_limits import RiskLimitValidationError, validate_risk_limits


class _FakeConfig:
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
        self.atomic_calls = []

    def update_values(self, updates):
        self.atomic_calls.append(list(updates))
        root = {"agents": self.agents, "bot": self.bot}
        for path, value in updates:
            target = root
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value


@pytest.mark.parametrize(
    ("daily", "per_trade"),
    [(0.5, 0.05), (1, 0.5), (10, 2), (10.0, 5.0)],
)
def test_operator_risk_limits_accept_documented_numeric_range(daily, per_trade):
    assert validate_risk_limits(daily, per_trade) == (float(daily), float(per_trade))


@pytest.mark.parametrize(
    ("daily", "per_trade"),
    [
        (0.49, 0.05),
        (10.01, 0.5),
        (1, 0.04),
        (10, 5.01),
        (1, 2),
        (True, 0.5),
        (1, False),
        ("10", 0.5),
        (10, "2"),
        (float("nan"), 0.5),
        (1, float("inf")),
    ],
)
def test_operator_risk_limits_reject_coercion_nonfinite_and_unsafe_values(daily, per_trade):
    with pytest.raises(RiskLimitValidationError):
        validate_risk_limits(daily, per_trade)


def test_legacy_two_field_writer_cannot_create_a_second_risk_policy():
    config = _FakeConfig()

    with pytest.raises(RiskConstitutionSettingsError, match="complete Risk Manager policy"):
        set_risk_settings(
            config=config,
            max_daily_loss_pct=10,
            max_risk_per_trade_pct=2,
        )

    assert config.atomic_calls == []


def test_config_manager_multi_update_preserves_unrelated_values(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.get_sqlite_store", lambda: None)
    path = tmp_path / "config.json"
    manager = ConfigManager(path)
    manager.update_value(["ui", "theme"], "dark")

    manager.update_values(
        [
            (["agents", "risk", "max_daily_loss_pct"], 10.0),
            (["agents", "risk", "max_risk_per_trade_pct"], 2.0),
            (["bot", "circuit_breaker_daily_loss_pct"], 10.0),
        ]
    )

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["ui"]["theme"] == "dark"
    assert persisted["agents"]["risk"]["max_daily_loss_pct"] == 10.0
    assert persisted["agents"]["risk"]["max_risk_per_trade_pct"] == 2.0
    assert persisted["bot"]["circuit_breaker_daily_loss_pct"] == 10.0
