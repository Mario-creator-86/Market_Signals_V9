"""Static UI contract for the Pre-Earnings premium controls and render path."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "styles_preearnings.css").read_text(encoding="utf-8")
RUN_SERVER = (ROOT / "run_server.py").read_text(encoding="utf-8")


@pytest.mark.unit
def test_pre_earnings_premium_containers_and_controls_are_unique():
    element_ids = [
        "preearnings-top10",
        "preearnings-pulse",
        "pe-reported-strip",
        "pe-reported-chip",
        "pe-reported-list",
        "pe-filter-command-title",
        "pe-filter-count",
        "pe-filter-reset",
        "preearnings-filters",
        "preearnings-filter-range",
        "preearnings-filter-sector",
        "preearnings-filter-cap",
        "preearnings-filter-cap-min",
        "preearnings-filter-cap-max",
        "preearnings-filter-direction",
        "preearnings-filter-session",
        "preearnings-filter-risk",
        "preearnings-filter-gap",
        "preearnings-filter-confidence",
        "preearnings-filter-sort",
        "preearnings-filter-search",
    ]
    for element_id in element_ids:
        assert HTML.count(f'id="{element_id}"') == 1, element_id


@pytest.mark.unit
def test_server_startup_does_not_override_owner_pre_earnings_configuration():
    assert 'setdefault("PRE_EARNINGS_LOOKAHEAD_DAYS"' not in RUN_SERVER
    assert 'setdefault("PRE_EARNINGS_OLLAMA_TIMEOUT_SECONDS"' not in RUN_SERVER
    assert 'setdefault("PRE_EARNINGS_OLLAMA_MAX_TICKERS"' not in RUN_SERVER


@pytest.mark.unit
def test_pre_earnings_premium_render_functions_are_wired():
    required_js = [
        "UI._renderPreEarningsTop10 = function",
        "UI._renderPreEarningsPulse = function",
        "UI._sortPreEarningsItems = function",
        "UI.togglePreEarningsReported = function",
        "UI._renderPreEarningsTop10(data, esc);",
        "UI._renderPreEarningsPulse(data, esc);",
        "const filteredSorted = UI._sortPreEarningsItems(filtered);",
        "set('preearnings-filter-session', 'ALL');",
        "set('preearnings-filter-sort', 'gap');",
        "set('preearnings-filter-search', '');",
    ]
    for snippet in required_js:
        assert snippet in JS, snippet


@pytest.mark.unit
def test_pre_earnings_premium_visual_classes_exist_and_are_used():
    class_names = [
        "pe-top10-card",
        "pe-top10-row",
        "pe-top10-event",
        "pe-top10-stale",
        "pe-pulse-card",
        "pe-reported-list",
        "pe-dp-head",
        "pe-dp-kpis",
        "pe-dp-chip",
        "pe-skeleton-card",
        "pe-skeleton-bar",
    ]
    for class_name in class_names:
        assert f".{class_name}" in CSS, class_name
        assert class_name in JS or class_name in HTML, class_name


@pytest.mark.unit
def test_top10_history_has_dedicated_metrics_rows_and_responsive_table():
    required_js = [
        "Top 10 History",
        "data.top_predictions_history",
        "data.top_predictions_pending",
        "Top 10 LONG",
        "Top 10 SHORT",
        "Realized move",
        "HIT means the post-earnings signed move matched",
        'class="pe-tr-top10-scroll"',
    ]
    for snippet in required_js:
        assert snippet in JS, snippet

    required_css = [
        ".pe-tr-top10-history {",
        ".pe-tr-top10-table {",
        "min-width: 760px;",
        "overflow-x: auto;",
        ".pe-tr-top10-result--hit {",
        ".pe-tr-top10-result--miss {",
    ]
    for snippet in required_css:
        assert snippet in CSS, snippet


@pytest.mark.unit
def test_filter_command_center_keeps_three_balanced_desktop_groups_and_live_handlers():
    assert "Filter Command Center" in HTML
    assert 'class="pe-filter-group pe-filter-group--scope"' in HTML
    assert 'class="pe-filter-group pe-filter-group--signal"' in HTML
    assert 'class="pe-filter-group pe-filter-group--view"' in HTML
    assert HTML.count('class="pe-filter-group pe-filter-group--') == 3
    assert 'id="pe-filter-count" role="status" aria-live="polite"' in HTML
    assert 'onclick="UI.resetPreEarningsFilters()"' in HTML

    live_handlers = [
        'id="preearnings-filter-range" class="bt-select pe-filter-select" onchange="UI.renderPreEarnings()"',
        'id="preearnings-filter-session" class="bt-select pe-filter-select" onchange="UI.renderPreEarnings()"',
        'id="preearnings-filter-search" class="pe-filter-cap-input" type="search"',
        "document.getElementById('preearnings-gap-val').textContent=this.value; UI.schedulePreEarningsRender();",
        "document.getElementById('preearnings-confidence-val').textContent=this.value; UI.schedulePreEarningsRender();",
        "document.getElementById('preearnings-filter-cap').value='ALL'; UI.schedulePreEarningsRender()",
    ]
    for snippet in live_handlers:
        assert snippet in HTML, snippet

    desktop_contract = [
        "Pre-Earnings desktop filter command center",
        "@media (min-width: 901px)",
        ".pe-filter-heading {",
        ".pe-filter-group {",
        ".pe-filter-group-body {",
        ".pe-filter-live-note {",
        "grid-template-columns: minmax(0, 1.32fr) minmax(0, 1.08fr) minmax(300px, 0.8fr);",
    ]
    for snippet in desktop_contract:
        assert snippet in CSS, snippet
    assert 'href="styles_preearnings.css?v=' in HTML
    assert 'src="app.js?v=' in HTML


@pytest.mark.unit
def test_pre_earnings_pulse_uses_aligned_desktop_label_and_content_columns():
    assert 'class="pe-pulse-content pe-pulse-picks"' in JS
    assert 'class="pe-pulse-content pe-pulse-text"' in JS
    assert "grid-template-columns: 92px minmax(0, 1fr);" in CSS
    # Round-2 F6: picks flow compactly 2-3 per line (flex wrap), not one-per-row grid.
    pulse_picks_css = CSS.split(".pe-pulse-picks {", 1)[1].split("}", 1)[0]
    assert "display: flex;" in pulse_picks_css
    assert "flex-wrap: wrap;" in pulse_picks_css
    assert "gap: 8px;" in pulse_picks_css
    assert "grid-template-columns" not in pulse_picks_css
    pulse_pick_css = CSS.split(".pe-pulse-pick {", 1)[1].split("}", 1)[0]
    # User-reported fix: picks are mini-cards (head row + clamped reason block),
    # not single-line pills — long LLM reasons no longer squeeze into an oval.
    assert "flex-direction: column;" in pulse_pick_css
    assert "border-radius: 12px;" in pulse_pick_css
    assert "flex: 1 1 300px;" in pulse_pick_css
    assert "max-width: 460px;" in pulse_pick_css
    assert ".pe-pulse-pick-head {" in CSS
    pulse_reason_css = CSS.split(".pe-pulse-pick-reason {", 1)[1].split("}", 1)[0]
    assert "-webkit-line-clamp: 4;" in pulse_reason_css
    assert ".pe-pulse-pick-reason {" in CSS
    assert "overflow-wrap: anywhere;" in CSS
    assert "grid-template-columns: minmax(0, 1fr) auto;" in CSS
    assert "align-items: baseline;" in CSS
    pulse_card_css = CSS.split(".pe-pulse-card {", 1)[1].split("}", 1)[0]
    assert "rgba(251, 249, 243, 0.97)" in pulse_card_css
    assert "border-left: 4px solid var(--pe-accent, #3b82f6);" in pulse_card_css


@pytest.mark.unit
def test_pre_earnings_pulse_renders_persisted_history_without_replacing_active_picks():
    required_js = [
        "const history = pulse && Array.isArray(pulse.history)",
        "pulse.history_meta && typeof pulse.history_meta === 'object'",
        'class="pe-pulse-history"',
        "Pulse history",
        "saved snapshot",
        "pulse.persisted",
        "Array.isArray(pulse.current_top_long)",
        "pulseLong.map((value) => pick(value, 'long'))",
        "Array.isArray(pulse.current_top_short)",
        "pulseShort.map((value) => pick(value, 'short'))",
    ]
    for snippet in required_js:
        assert snippet in JS, snippet

    required_css = [
        ".pe-pulse-history {",
        ".pe-pulse-history-list {",
        ".pe-pulse-history-item.is-latest {",
        ".pe-pulse-history-symbol--long {",
        ".pe-pulse-history-symbol--short {",
        ".pe-pulse-saved {",
    ]
    for snippet in required_css:
        assert snippet in CSS, snippet


@pytest.mark.unit
def test_pre_earnings_signal_labels_are_semantically_distinct():
    assert "Final Blended Signal" in JS
    assert "Structured vs LLM" in JS
    assert "EPS Beat Rate" in JS
    assert "Data Coverage" in JS
    assert "this is not a probability" in JS
    assert "LLM ${esc(_ai.verdict || 'NEUTRAL')} · ${Math.round(Number(_ai.confidence || 0) * 100)}% conf" in JS


@pytest.mark.unit
def test_pre_earnings_refresh_waits_for_enrichment_rescore():
    assert "startPreEarningsRefreshPoll = function (previousSuccess, waitForCoverage = false)" in JS
    assert "successTs >= warmFinishedTs" in JS
    assert "refreshPreEarningsSymbol(normalized)" in JS
    assert "Calendar, news, EPS history and market context refresh started" in JS


@pytest.mark.unit
def test_pre_earnings_navigation_reads_cached_snapshot_without_starting_refresh():
    assert "if (viewId === 'preearnings') {\n            UI.loadPreEarningsData(false);\n        }" in JS
    load_block = JS.split("UI.loadPreEarningsData = async function (forceNetwork = false, options = {}) {", 1)[1]
    load_block = load_block.split("// ── Pre-Earnings History / Track-Record panel", 1)[0]
    assert "API.getPreEarningsStateCached(30000)" in load_block
    assert "API.refreshPreEarnings(" not in load_block
    assert "API.refreshPreEarningsAi(" not in load_block
    assert "API.warmupPreEarningsCoverage(" not in load_block


@pytest.mark.unit
def test_pre_earnings_pulse_never_promotes_archived_picks_or_summary_to_live():
    pulse_block = JS.split("UI._renderPreEarningsPulse = function", 1)[1]
    pulse_block = pulse_block.split("UI.renderPreEarnings = function", 1)[0]
    assert "? pulse.current_top_long : [];" in pulse_block
    assert "? pulse.current_top_short : [];" in pulse_block
    assert "const hasPulse = !!pulse && (pulseLong.length > 0 || pulseShort.length > 0);" in pulse_block
    assert "String(pulse.summary || '').trim()" not in pulse_block
    assert "No current LONG or SHORT setup clears" in pulse_block


@pytest.mark.unit
def test_pre_earnings_nullable_scores_are_not_coerced_to_zero():
    assert "const rawScore = hasRawScore ? Number(row.direction_bias_score) : NaN;" in JS
    assert "const explicit = hasExplicit ? Number(row.structured_direction_bias_score) : NaN;" in JS
    assert "rawExplicit !== null && rawExplicit !== undefined" in JS


@pytest.mark.unit
def test_pre_earnings_active_statistics_and_accessible_rows_are_pinned():
    assert "const activeUniverse = allItems.filter((item) => !isAlreadyReported(item));" in JS
    assert "Showing ${filtered.length} of ${activeUniverse.length} analyzed candidates" in JS
    assert "calendarUniverseCount" in JS
    assert "calendar_universe" in JS
    assert "calendar reporters" in JS
    in_range_block = JS.split("const inRange = (item) =>", 1)[1].split("const inCapRange", 1)[0]
    assert "return true;" in in_range_block
    assert "const hasVerifiedSchedule = (item) =>" in JS
    assert "!hasVerifiedSchedule(x) || isLowCoverage(x) || isInsufficient(x)" in JS
    assert "Date unavailable &middot; candidate only" in JS
    assert "NEEDS COVERAGE / DATE UNAVAILABLE" in JS
    assert "if (totalEl) totalEl.textContent = activeUniverse.length;" in JS
    assert 'tabindex="0" role="button"' in JS
    assert "event.key==='Enter'||event.key===' '" in JS
    assert "sectorEl.replaceChildren(...options);" in JS
    assert "UI._escapeHtml(ai.model)" in JS


@pytest.mark.unit
def test_pre_earnings_refresh_poll_skips_auxiliary_request_fanout():
    poll = JS.split("UI.startPreEarningsRefreshPoll = function", 1)[1]
    poll = poll.split("UI._preEarningsStructuredScore", 1)[0]
    assert "const payload = await API.getPreEarningsRefreshStatus();" in poll
    assert "await UI.loadPreEarningsData(true, { refreshAuxiliary: false });" in poll
    assert poll.count("UI.loadPreEarningsData(true, { refreshAuxiliary: false })") == 1
    assert "UI.loadPreEarningsTrackRecord(true);" in poll
    assert "if (this._preEarningsCache.inflight) return this._preEarningsCache.inflight;" in JS
    assert "_preEarningsTrackCache: { ts: 0, data: null, inflight: null }" in JS


@pytest.mark.unit
def test_pre_earnings_cache_never_republishes_an_older_refresh_snapshot():
    api_block = JS.split("_preEarningsStateRevision(payload) {", 1)[1]
    api_block = api_block.split("// Watchlist Operations", 1)[0]
    assert "incomingRevision < currentRevision" in api_block
    assert "incomingTime < currentTime" in api_block
    assert "return this._acceptPreEarningsPayload(payload);" in api_block
    assert "if (this._preEarningsCache.inflight) return this._preEarningsCache.inflight;" in api_block
    assert "const req = (async () => {" in api_block
    assert "return await req;" in api_block
    assert "return stale;" in api_block
    stale_branch = api_block.split("async getPreEarningsStateCached", 1)[1]
    stale_branch = stale_branch.split("async refreshPreEarnings", 1)[0]
    assert "req.finally" not in stale_branch
    assert "return stale;\n        }" not in stale_branch


@pytest.mark.unit
def test_detail_opportunity_uses_backend_opportunity_not_confidence():
    assert "const opportunityRaw = Number(item.opportunity_score);" in JS
    assert "const opportunityScore = UI._preEarningsConfidenceScore(item);" not in JS


@pytest.mark.unit
def test_top_news_links_allow_only_absolute_http_urls():
    assert "UI._preEarningsSafeNewsUrl = function (value)" in JS
    assert "const parsed = new URL(raw);" in JS
    assert "parsed.protocol !== 'http:' && parsed.protocol !== 'https:'" in JS
    assert "const safeUrl = UI._preEarningsSafeNewsUrl(n.url);" in JS
    assert "const safeUrl = url.replace(/\"/g, '');" not in JS


@pytest.mark.unit
def test_detail_coverage_prefers_canonical_score_even_when_zero():
    assert "const coverageRaw = item.coverage_score != null" in JS
    assert "? item.coverage_score" in JS
    assert "item.data_quality_score || item.coverage_score" not in JS


@pytest.mark.unit
def test_verification_dossier_is_wired_end_to_end_and_additive():
    """I04: the dossier renders in the detail panel without removing sections."""
    # Backend endpoint exists and is read-only.
    main_py = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    assert '@app.get("/api/pre-earnings/dossier")' in main_py
    assert "get_pre_earnings_agent_if_ready" in main_py.split("/api/pre-earnings/dossier", 1)[1].split("def api_pre_earnings_dossier", 1)[1][:600]

    # Frontend fetch path + render path + stale-selection guard.
    assert "async getPreEarningsDossier(symbol, eventIso)" in JS
    assert "${this.BASE_URL}/pre-earnings/dossier?symbol=" in JS
    assert "UI.loadPreEarningsDossier = async function" in JS
    assert "UI.renderPreEarningsDossier = function" in JS
    assert "data-dossier-symbol" in JS
    assert "Verification dossier unavailable right now." in JS

    # The dossier is an additional section of the detail panel: the existing
    # sections must all still render, and the dossier must come after the
    # news card inside renderPreEarningsDetails.
    details_block = JS.split("UI.renderPreEarningsDetails = function", 1)[1]
    details_block = details_block.split("UI.loadPreEarningsDossier = async function", 1)[0]
    for existing_marker in (
        "How the final signal was blended",
        "Analysis KPIs",
        "Sentiment Breakdown",
        "Earnings History",
        "Rationale",
        "What To Watch",
        "Top Relevant News",
    ):
        assert existing_marker in details_block, existing_marker
    news_idx = details_block.index("Top Relevant News")
    dossier_idx = details_block.index('id="pe-verification-dossier"')
    assert news_idx < dossier_idx

    # The dossier renders opposing evidence, contradictions and gaps by name.
    dossier_block = JS.split("UI.renderPreEarningsDossier = function", 1)[1]
    for section_marker in (
        "Opposing evidence",
        "Contradictions",
        "Missing / thin data",
        "First evaluation (frozen)",
        "Shortlist entry (frozen)",
        "legacy snapshot",
        "not calibrated",
    ):
        assert section_marker in dossier_block, section_marker
