from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "frontend" / "cockpit.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "styles_cockpit.css").read_text(encoding="utf-8")


@pytest.mark.unit
def test_ai_cockpit_is_central_and_assets_are_wired_once():
    assert HTML.count('<section id="ai-trading-cockpit"') == 1
    assert 'data-testid="nav-home"' in HTML
    assert 'AI Cockpit</a>' in HTML
    assert HTML.count('href="styles_cockpit.css?v=') == 1
    assert HTML.count('src="cockpit.js?v=') == 1
    assert 'data-testid="cockpit-refresh"' in HTML
    assert 'data-testid="cockpit-kill-switch"' in HTML


@pytest.mark.unit
def test_all_async_states_have_explicit_render_paths():
    assert "const ASYNC_STATES = new Set(['LOADING', 'SUCCESS', 'EMPTY', 'ERROR'])" in JS
    assert "state === 'ERROR'" in JS
    assert "state === 'EMPTY'" in JS
    assert 'id="cockpit-loading"' in HTML
    assert 'id="cockpit-error"' in HTML
    assert 'id="cockpit-status"' in HTML
    assert "Cockpit returned an invalid state." in JS


@pytest.mark.unit
def test_pre_earnings_matrix_has_every_phase12_column_and_drilldown():
    columns = [
        "Ticker", "Earnings date", "BMO / AMC", "Score", "Data confidence",
        "Historical move", "IV / RV", "Bias", "Strategy",
        "AI decision", "Recommendation / Paper Auto",
    ]
    for column in columns:
        assert f"<th>{column}</th>" in JS
    assert '>Range proxy</th>' in JS
    assert 'not a forecast of the earnings move' in JS
    assert '<th>Expected move</th>' not in JS
    assert 'data-testid="cockpit-earnings-table"' in JS
    assert 'data-cockpit-earnings=' in JS
    assert "UI.selectPreEarningsRow" in JS
    assert "IV/RV is N/A when options evidence is absent" in JS
    assert "Eligible means recommendation-eligible only" in JS
    assert "data-cockpit-gates=" in JS
    assert "Verify gates" in JS
    assert "Gap-risk" in JS


@pytest.mark.unit
def test_strategy_ledger_research_and_health_workspaces_are_not_dead_tabs():
    for tab in ("overview", "strategy", "ledger", "research", "health", "repair"):
        assert f'data-cockpit-tab="{tab}"' in HTML
        assert f'data-cockpit-panel="{tab}"' in HTML
    assert "renderStrategy(payload)" in JS
    assert "renderLedger(payload)" in JS
    assert "renderResearch(payload)" in JS
    assert "renderHealth(payload)" in JS
    assert "renderRepairAgent" in JS
    assert "loadRepairAgent" in JS
    assert "data-cockpit-repair-approve" in JS
    assert "What happened?" in JS
    assert "What happened afterward?" in JS
    assert "Why positions are not opening" in JS
    assert "Current governance cannot admit these strategies" in JS
    assert "No order was sent and no position was created." in JS
    assert "MODEL VETO OVERRIDDEN BY SCORE POLICY" in JS
    assert "critic.model_veto === true && !veto" in JS
    assert "hasExecution ? 'Position status' : 'Why no position?'" in JS
    assert "evidenceText(item[key])" not in JS
    assert "data-cockpit-open-risk" in JS
    assert "risk-settings-open" in JS
    assert "Deterministic metrics" in JS
    assert "AI Quant commentary" in JS
    assert "no synthetic green fallback" in JS


@pytest.mark.unit
def test_coding_agent_history_has_evidence_based_expandable_journeys():
    assert 'data-cockpit-repair-details=' in JS
    assert 'data-cockpit-repair-detail-row=' in JS
    assert 'aria-expanded=' in JS
    assert "Outcome diagnosis" in JS
    assert "1. Detection" in JS
    assert "2. Lifecycle" in JS
    assert "3. Coding attempts" in JS
    assert "4. Independent review" in JS
    assert "5. Apply & post-check" in JS
    assert "Detailed stage events were not captured for this legacy attempt" in JS
    assert ".cockpit-repair-detail-panel" in CSS
    assert ".cockpit-repair-events" in CSS


@pytest.mark.unit
def test_refresh_navigation_and_kill_switch_have_real_handlers():
    assert "byId('cockpit-refresh')?.addEventListener('click', refreshSources)" in JS
    assert "requestJson('/api/pre-earnings/refresh'" in JS
    assert "byId('cockpit-error-retry')?.addEventListener('click', () => load({ force: true }))" in JS
    assert "byId('cockpit-kill-switch')?.addEventListener('click', toggleKillSwitch)" in JS
    assert "requestJson('/api/execution/kill-switch'" in JS
    # §74: the confirm click must derive its target from LIVE server state, not a
    # possibly-stale snapshot (a poll between arm and confirm must not flip intent).
    assert "requestJson('/api/execution/control'" in JS
    assert "JSON.stringify({ active: targetActive })" in JS
    assert "data-cockpit-route" in JS
    assert "window.ViewRouter.navigate" in JS
    assert "data-cockpit-tab-open" in JS


@pytest.mark.unit
def test_cockpit_surfaces_real_source_and_ai_freshness():
    assert "source_freshness" in JS
    assert "Pre-Earnings source:" in JS
    assert "ai_generated_at" in JS
    assert "calendar evidence" in JS
    assert "Date unavailable" in JS
    assert "calendar reporters light-scanned" in JS
    assert "deep-scored" in JS
    assert "Not every calendar reporter invokes AI" in JS
    assert "calendar reporters" in JS
    assert "estimated or missing dates" in JS
    assert "Pre-Earnings decision matrix" in JS


@pytest.mark.unit
def test_cockpit_does_not_prefill_or_mutate_owner_trading_values():
    assert "reduce_fraction_pct" not in JS
    # The control endpoint appears ONLY as the kill-switch safety pre-read —
    # no PATCH of owner trading values exists in the cockpit.
    assert "PATCH" not in JS
    assert "requestJson('/api/execution/control'" in JS  # GET pre-read (kill-switch safety)
    assert "PAPER_AUTO" not in JS
    assert "value=\"50\"" not in HTML
    assert "value=\"25\"" not in HTML


@pytest.mark.unit
def test_cockpit_visual_states_and_responsive_contract_exist():
    required = [
        ".cockpit-shell {", ".cockpit-kpi-grid {", ".cockpit-overview-grid {",
        ".cockpit-state-pill.is-success", ".cockpit-state-pill.is-error",
        ".cockpit-empty", ".cockpit-section-error", ".cockpit-health-grid {",
        "@media (max-width: 760px)", "@media (prefers-reduced-motion: reduce)",
    ]
    for snippet in required:
        assert snippet in CSS
