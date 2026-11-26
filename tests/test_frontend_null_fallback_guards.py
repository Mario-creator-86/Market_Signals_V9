from pathlib import Path


def _frontend_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "frontend" / "app.js").read_text(encoding="utf-8")


def test_preearnings_component_breakdown_has_no_fake_neutral_defaults():
    source = _frontend_source()
    forbidden = [
        "Math.round(comps.analyst_revision || 50)",
        "width:${comps.analyst_revision || 50}%",
        "Math.round(comps.options || 50)",
        "width:${comps.options || 50}%",
        "Math.round(comps.news_ai || 50)",
        "width:${comps.news_ai || 50}%",
    ]
    for pattern in forbidden:
        assert pattern not in source


def test_signal_rsi_logic_has_no_forced_neutral_default():
    source = _frontend_source()
    assert "const rsi = sig.rsi || 50;" not in source


def test_watchlist_momentum_logic_keeps_null_indicators_as_na():
    source = _frontend_source()
    forbidden = [
        "const rsiVal = Number(d.rsi);",
        "const macdVal = toNum(d.macd_hist, 0);",
        "Momentum RSI ${rsiDisplay} | MACD ${toNum(d.macd_hist, 0).toFixed(4)}",
    ]
    for pattern in forbidden:
        assert pattern not in source


def test_preearnings_detail_reads_backend_field_names_not_stale_aliases():
    """The Pre-Earnings detail panel must read the field names the backend
    actually emits (pre_earnings_agent.py:2103-2106: `why`, `what_to_watch`,
    `top_news`), not the stale aliases `rationales`/`watch_factors`/hardcoded
    empty news that left the Rationale / What To Watch / Top News cards dead.
    Regression for audit F1/F2/F3."""
    source = _frontend_source()
    # Must read the real backend list fields as the primary path.
    assert "Array.isArray(item.why)" in source
    assert "Array.isArray(item.what_to_watch)" in source
    assert "Array.isArray(item.top_news)" in source
    # Must NOT hardcode the news section empty.
    assert 'const newsCards = "";' not in source
    # The old broken alias-first lookups must be gone.
    assert "Array.isArray(item.rationales)" not in source


def test_preearnings_list_rows_have_data_label_for_mobile_cards():
    """The responsive CSS (styles_preearnings.css .pe-table td::before {
    content: attr(data-label) }) turns table rows into stacked label/value
    cards below 899px. Without a `data-label` on every <td> the mobile cards
    showed the value with an EMPTY left side — the user couldn't tell which
    field was which. Regression for audit F4."""
    source = _frontend_source()
    # Every pre-earnings list-view <td> must carry a data-label attribute.
    required_labels = [
        'data-label="Ticker"',
        'data-label="Final signal"',
        'data-label="Structured vs LLM"',
        'data-label="EPS Beat Rate"',
        'data-label="Analyst"',
        'data-label="Data Coverage"',
    ]
    for label in required_labels:
        assert label in source, f"pre-earnings list <td> missing {label}"


def _frontend_html() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "frontend" / "index.html").read_text(encoding="utf-8")


def test_preearnings_tab_has_ai_model_selector():
    """The Pre-Earnings tab must surface the selected AI (Ollama) model and let
    the user change it from the available models list (user requirement)."""
    html = _frontend_html()
    assert 'id="preearnings-ai-model"' in html, "AI model <select> missing from Pre-Earnings tab"
    assert 'preearnings-ai-status-dot' in html
    js = _frontend_source()
    assert "UI.loadPreEarningsAiModel" in js
    assert "UI.savePreEarningsAiModel" in js
    # The selector must be loaded when the tab loads data.
    assert "UI.loadPreEarningsAiModel()" in js
    # It must drive the real ollama endpoints.
    assert "/ollama/status" in js
    assert "/ollama/set-model" in js
    assert "window.GlobalAISelection" in js
    assert "marketsignals:ai-model-changed" in js
    assert "Global AI model" in html


def test_preearnings_tab_has_history_track_record_panel():
    """Task #11: the Pre-Earnings tab must surface a History / Track-Record
    panel (settled-outcome accuracy per verdict + per confidence band, recent
    verified predictions, and a separate-prompt AI summary + recommendations)."""
    html = _frontend_html()
    assert 'id="preearnings-trackrecord"' in html, "track-record panel container missing"
    assert 'id="pe-trackrecord-body"' in html
    assert 'id="pe-tr-samples"' in html
    js = _frontend_source()
    assert "UI.loadPreEarningsTrackRecord" in js
    assert "UI.renderPreEarningsTrackRecord" in js
    assert "API.getPreEarningsTrackRecord" in js
    assert "/pre-earnings/track-record" in js
    # The panel must be loaded when the tab loads data (wired into loadPreEarningsData).
    assert "UI.loadPreEarningsTrackRecord()" in js
    # It must render per-verdict accuracy and confidence bands.
    assert "per_verdict" in js
    assert "per_band" in js
    assert "ai_summary" in js
    assert "_preEarningsTrackRecordRetryCount" in js
    assert "UI.loadPreEarningsTrackRecord(true)" in js
