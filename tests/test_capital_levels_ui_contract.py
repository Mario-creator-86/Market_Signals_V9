from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_active_position_cards_expose_level_editor():
    markup = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    script = (PROJECT_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

    for element_id in (
        "capital-risk-modal",
        "capital-risk-stop",
        "capital-risk-target",
        "capital-risk-reason",
        "capital-risk-validation",
        "capital-risk-save",
    ):
        assert f'id="{element_id}"' in markup

    assert 'data-testid="capital-edit-levels"' in script
    assert "Actions.openCapitalRiskEditor" in script
    assert "Actions.previewCapitalRiskLevels" in script
    assert "Actions.saveCapitalRiskLevels" in script
    assert "API.updateCapitalPositionLevels" in script
    assert "/capital/update-position-levels" in script


def test_level_editor_exposes_safe_manual_override_copy_and_responsive_styles():
    markup = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    styles = (PROJECT_ROOT / "frontend" / "styles_capital.css").read_text(encoding="utf-8")

    assert "disables trailing or AI exit overrides" in markup
    assert "verified live price" in markup
    assert "Break-even SL" in markup
    assert "capital-risk-overlay" in styles
    assert "@media (max-width: 640px)" in styles
    assert ".btn-edit-risk" in styles


def test_position_badges_cannot_overlap_numeric_columns():
    styles = (PROJECT_ROOT / "frontend" / "styles_capital.css").read_text(encoding="utf-8")

    assert "grid-template-columns: 230px minmax(0, 1fr) 105px auto" in styles
    assert "flex-wrap: wrap" in styles
    assert ".cap-pos-ident" in styles
    assert "overflow: hidden" in styles


def test_capital_exposes_paper_bot_and_signal_cycle_controls():
    markup = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    script = (PROJECT_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

    for test_id in ("capital-bot-start", "capital-bot-stop", "capital-cycle-run"):
        assert f'data-testid="{test_id}"' in markup
    assert "Actions.setCapitalBotRunning" in script
    assert "Actions.runCapitalAutonomousCycle" in script
    assert "/agents/autonomous/run-once" in script
    assert "Last signal cycle:" in script
    assert "PAPER AUTO" in script
    assert "V2 PAPER" not in script


def test_capital_kpis_and_counts_exclude_research_positions():
    markup = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    script = (PROJECT_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

    assert 'id="capital-research-count"' in markup
    assert "Active Capital Positions" in markup
    assert "const researchPositions = openPositions.filter(isResearchPosition)" in script
    assert "const capitalPositions = openPositions.filter" in script
    assert "capitalPositions.forEach(pos =>" in script
    assert "open_capital: ['POSITION OPEN'" in script
    assert "open_research: ['RESEARCH OPEN'" in script


def test_capital_has_one_unified_history_below_portfolio_performance():
    markup = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    script = (PROJECT_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    styles = (PROJECT_ROOT / "frontend" / "styles_capital.css").read_text(encoding="utf-8")

    assert "Execution Ledger (all scopes)" not in markup
    assert markup.count("Capital Transaction History") == 1
    assert markup.count('id="capital-history-body"') == 1
    assert markup.index('id="capital-equity-curve"') < markup.index('id="capital-history-section"')
    assert markup.index('id="capital-history-section"') < markup.index('class="capital-terminal glass-effect"')
    assert "Research transactions never alter deployable Capital" in markup
    assert "<th style=\"text-align:right;\">Raw Cash</th>" in markup
    assert "txn.account_scope" in script
    assert "txn.raw_balance ?? txn.balance" in script
    assert "Array.isArray(capitalData.ledger) ? capitalData.ledger : history" in script
    assert "'AccountScope', 'AffectsDeployableCapital'" in script
    assert "'MetadataJSON'" in script
    assert "if (/^[=+\\-@]/.test(text))" in script
    assert ".capital-transaction-scope.is-capital" in styles
    assert ".capital-transaction-scope.is-research" in styles
    assert ".capital-table-scroll.capital-transaction-scroll" in styles
    assert "max-height: 640px" in styles
    assert ".capital-transaction-section.has-open-transaction" in styles
    assert "max-height: none" in styles
    assert "overflow: visible" in styles


def test_every_transaction_has_accessible_complete_auditable_dropdown():
    script = (PROJECT_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    styles = (PROJECT_ROOT / "frontend" / "styles_capital.css").read_text(encoding="utf-8")

    assert 'data-testid="capital-transaction-toggle"' in script
    assert "UI.toggleCapitalHistoryDetail" in script
    assert "UI._renderCapitalClosedPositionDetails" in script
    assert "UI._renderCapitalTransactionDetails" in script
    assert "UI._flattenCapitalTransactionRecord" in script
    assert "UI._capitalTransactionRecords = new Map()" in script
    assert "data-capital-detail-rendered" in script
    assert "content.innerHTML = transaction" in script
    assert "section.classList.toggle('has-open-transaction', opening)" in script
    assert "detail.scrollIntoView({ behavior: 'smooth', block: 'start' })" in script
    assert "Every recorded field" in script
    assert 'aria-expanded="${isDetailOpen ? \'true\' : \'false\'}"' in script
    for label in (
        "Execution & outcome",
        "Signal & sizing",
        "AI & Critic",
        "Final size factor",
        "Close reason",
        "Critic policy",
    ):
        assert label in script
    assert ".capital-history-toggle" in styles
    assert ".capital-close-detail" in styles
    assert ".capital-close-columns" in styles
    assert ".capital-transaction-fields" in styles


def test_capital_state_polls_use_cross_process_cache_without_remote_fanout():
    script = (PROJECT_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

    assert "livePrices: capitalOnly ? false : null" in script
    assert "livePrices: true" not in script
    assert script.count("livePrices: false") >= 4


def test_capital_router_exposes_selected_tab_to_assistive_technology():
    script = (PROJECT_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    capital_branch = script.split("if (viewName === 'capital')", 1)[1].split(
        "if (viewName === 'alerts')", 1
    )[0]

    assert "navItem.classList.add('active')" in capital_branch
    assert "navItem.setAttribute('aria-selected', 'true')" in capital_branch


def test_equity_curve_uses_modern_interactive_accessible_component():
    markup = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    script = (PROJECT_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    styles = (PROJECT_ROOT / "frontend" / "styles_capital.css").read_text(encoding="utf-8")

    assert 'class="capital-section capital-equity-card-shell"' in markup
    assert 'data-testid="capital-equity-plot"' in script
    assert 'role="img" tabindex="0" aria-label="Equity moved' in script
    assert "capital-equity-tooltip" in script
    assert "UI._buildCapitalEquityScale" in script
    assert "const minEq = Math.min(...equities)" in script
    assert "const maxEq = Math.max(...equities)" in script
    assert "useTimeScale" in script
    assert "Straight sample-to-sample segments" in script
    assert "Deployable cash + open Capital margin + mark-to-market P&amp;L" in script
    assert "tooltip.classList.toggle('is-below'" in script
    assert "plot?.addEventListener('pointermove'" in script
    assert "event.key !== 'ArrowLeft'" in script
    assert ".capital-equity-kpis" in styles
    assert ".capital-equity-area" in styles
    assert ".capital-equity-tooltip.is-below" in styles
    assert "@media (max-width: 900px)" in styles


def test_premarket_pulse_copy_covers_transient_ai_failures():
    script = (PROJECT_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

    assert "AI may be disabled, unavailable, or temporarily timed out" in script
