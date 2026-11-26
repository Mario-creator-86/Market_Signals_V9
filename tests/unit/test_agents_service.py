"""Pure-service contracts for the Agents control plane."""

from types import SimpleNamespace

import pytest

from api.agents_service import (
    RequiredAgentRunningError,
    UnknownAgentError,
    build_control_plane_payload,
    set_agent_enabled,
)


def _config():
    config = SimpleNamespace(
        agents={"execution": {"enabled": True}, "news_tab": {"enabled": False}},
        config={"premarket": {"enabled": False}, "pre_earnings": {"enabled": True}},
        news={"news_refresh_minutes": 10},
        strategy_v2={"research_agent": {"enabled": True}},
        bot={"enabled": False},
    )
    config.updates = []
    config.update_value = lambda path, value: config.updates.append((path, value))
    return config


def test_control_plane_is_bounded_and_contains_engine_state():
    payload = build_control_plane_payload(
        config=_config(),
        bot_status={"running": False},
        agents_status={"execution": {"running": True}},
        engine_running=True,
        as_of="2026-07-15T12:00:00+00:00",
    )

    assert payload["as_of"] == "2026-07-15T12:00:00+00:00"
    assert payload["bot"]["engine_running"] is True
    assert payload["bot"]["autonomous_execution_active"] is False
    assert payload["agents"]["execution"]["running"] is True
    assert payload["agents"]["execution"]["accepting_new_entries"] is False
    assert payload["agents"]["execution"]["operating_mode"] == "monitoring_only"
    assert payload["config"]["agents"]["execution"] == {"enabled": True}
    constitution = payload["config"]["risk_constitution"]
    assert constitution["status"] == "unconfigured"
    assert constitution["active"] is None
    assert constitution["schema"]
    assert constitution["profiles"]["status"] == "ready"
    assert len(constitution["profiles"]["items"]) == 3
    assert all(value is None for value in constitution["draft"].values())
    assert "unrelated_config" not in payload["config"]


def test_control_plane_distinguishes_worker_liveness_from_entry_permission():
    payload = build_control_plane_payload(
        config=_config(),
        bot_status={"running": True, "circuit_breaker_active": False},
        agents_status={
            "execution": {"running": True},
            "risk": {"running": True},
            "scanner": {"running": True},
        },
        engine_running=True,
    )

    assert payload["bot"]["autonomous_execution_active"] is True
    assert payload["agents"]["execution"]["accepting_new_entries"] is True
    assert payload["agents"]["risk"]["operating_mode"] == "entry_enabled"
    assert "accepting_new_entries" not in payload["agents"]["scanner"]


def test_disabling_required_agent_is_rejected_while_bot_runs():
    with pytest.raises(RequiredAgentRunningError):
        set_agent_enabled(
            config=_config(),
            agent_name="risk",
            enabled=False,
            bot_running=True,
            status_updater=lambda **_kwargs: None,
        )


def test_strategy_research_updates_both_config_locations_and_runtime_status():
    config = _config()
    updates = []

    result = set_agent_enabled(
        config=config,
        agent_name="strategy_research",
        enabled=False,
        bot_running=False,
        status_updater=lambda name, **kwargs: updates.append((name, kwargs)),
    )

    assert result == {"status": "updated", "agent": "strategy_research", "enabled": False}
    assert config.updates == [
        (["agents", "strategy_research", "enabled"], False),
        (["strategy_v2", "research_agent", "enabled"], False),
    ]
    assert updates == [("strategy_research", {"enabled": False})]


def test_unknown_agent_is_not_written():
    config = _config()

    with pytest.raises(UnknownAgentError):
        set_agent_enabled(
            config=config,
            agent_name="arbitrary_worker",
            enabled=True,
            bot_running=False,
            status_updater=lambda **_kwargs: None,
        )

    assert config.updates == []


def test_enabling_agent_reconciles_runtime_and_returns_live_state():
    config = _config()
    runtime_calls = []
    status_updates = []

    result = set_agent_enabled(
        config=config,
        agent_name="pre_earnings",
        enabled=True,
        bot_running=False,
        status_updater=lambda name, **kwargs: status_updates.append((name, kwargs)),
        runtime_switcher=lambda name, enabled: (
            runtime_calls.append((name, enabled))
            or {"enabled": True, "running": True, "lifecycle_state": "running", "managed_by": "api"}
        ),
    )

    assert runtime_calls == [("pre_earnings", True)]
    assert result["runtime"]["running"] is True
    assert result["runtime"]["lifecycle_state"] == "running"
    assert status_updates == [
        (
            "pre_earnings",
            {"enabled": True, "running": True, "lifecycle_state": "running", "managed_by": "api"},
        )
    ]


def test_runtime_failure_rolls_back_agent_and_strategy_alias():
    config = _config()

    def fail_start(_name, _enabled):
        raise RuntimeError("worker failed")

    with pytest.raises(RuntimeError, match="worker failed"):
        set_agent_enabled(
            config=config,
            agent_name="strategy_research",
            enabled=True,
            bot_running=False,
            status_updater=lambda *_args, **_kwargs: None,
            runtime_switcher=fail_start,
        )

    assert config.updates == [
        (["agents", "strategy_research", "enabled"], True),
        (["strategy_v2", "research_agent", "enabled"], True),
        (["agents", "strategy_research", "enabled"], False),
        (["strategy_v2", "research_agent", "enabled"], True),
    ]
