"""Source contracts for Agent Fleet lifecycle rendering and toggle feedback."""

from pathlib import Path


SOURCE = (Path(__file__).parents[2] / "frontend" / "agents.js").read_text(encoding="utf-8")
HTML = (Path(__file__).parents[2] / "frontend" / "index.html").read_text(encoding="utf-8")


def test_running_state_uses_explicit_thread_liveness_not_recent_timestamp():
    assert "if (runtime.running === true)" in SOURCE
    assert "recentSuccess || recentRun" not in SOURCE
    assert "Running / scheduled" in SOURCE
    assert "Running / monitoring only" in SOURCE
    assert "Running / entries enabled" in SOURCE


def test_toggle_waits_for_runtime_confirmation_and_reports_start_stop():
    assert "async function waitForAgentRuntime" in SOURCE
    assert "await waitForAgentRuntime(agentName, true)" in SOURCE
    assert "started' : 'stopped" in SOURCE
    assert "agents-status-starting" in SOURCE


def test_risk_manager_has_schema_driven_owner_editor_and_dedicated_atomic_endpoint():
    assert 'data-testid="risk-settings-open"' in SOURCE
    assert "constitution?.schema" in SOURCE
    assert "constitution?.trade_policy" in SOURCE
    assert "riskFieldHtml(field, value, scope" in SOURCE
    assert 'data-testid="risk-settings-save-draft"' in SOURCE
    assert 'data-testid="risk-settings-save"' in SOURCE
    assert "Actions.saveRiskSettings" in SOURCE
    assert "'/api/agents/risk/constitution'" in SOURCE
    assert "method: 'PATCH'" in SOURCE
    assert "owner_confirmed: Boolean(activate)" in SOURCE
    assert "trade_policy: validation.tradeValues" in SOURCE
    assert "RISK_LIMITS" not in SOURCE


def test_risk_editor_preserves_active_policy_while_paper_draft_is_adjusted():
    assert "No value is inserted automatically" in SOURCE
    assert "Save draft keeps the active policy unchanged" in SOURCE
    assert "Active limits remain unchanged until you confirm this draft" in SOURCE
    assert "Confirm and apply" in SOURCE


def test_risk_editor_validates_cross_field_invariants_before_activation():
    assert "Max risk / trade cannot exceed max daily loss" in SOURCE
    assert "Max position size cannot exceed max total exposure" in SOURCE
    assert "Complete every field before activation" in SOURCE


def test_risk_editor_offers_config_driven_profiles_plus_custom_without_auto_apply():
    assert "constitution?.profiles?.items" in SOURCE
    assert 'data-testid="risk-profile-picker"' in SOURCE
    assert 'data-testid="risk-profile-custom"' in SOURCE
    assert "Actions.selectRiskConstitutionProfile" in SOURCE
    assert "Selecting a profile only fills this form" in SOURCE
    assert "It does not save or activate the Constitution" in SOURCE
    assert "state.riskEditor.profileId = 'custom'" in SOURCE


def test_risk_manager_is_the_only_execution_policy_surface():
    assert 'data-testid="risk-trade-policy"' in SOURCE
    assert "Trade admission, sizing & exits" in SOURCE
    assert "Risk Manager active policy" in SOURCE
    assert "Risk Manager signal monitor" in SOURCE
    assert "Actions.runRiskManagerCycle" in SOURCE
    assert "'/api/agents/risk/run-cycle'" in SOURCE
    assert "'/api/agents/risk/pipeline-status'" in SOURCE
    assert "savePaperExecutionControl" not in SOURCE
    assert "renderExecutionControl" not in SOURCE
    assert "agent-customization-card" not in HTML
    assert "agents-execution-control" not in HTML
    assert ">Autonomous Agent<" not in HTML
    assert "Source observations" in SOURCE
    assert "Scan cap" in SOURCE
    assert "AI → Critic → Risk → Capital → Paper Broker" in SOURCE
    assert "'/api/execution/control'" in SOURCE
    assert "'/api/execution/kill-switch'" in SOURCE
    assert "'/api/execution/reconcile'" in SOURCE
