from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_capital_page_exposes_multi_asset_chart_trading_terminal():
    markup = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")

    required_ids = {
        "open-paper-chart-terminal",
        "chart-workspace-modal",
        "chart-symbol-input",
        "tv-chart-container",
        "tv-chart-loading",
        "chart-focus-toggle",
        "chart-order-size",
        "chart-order-leverage",
        "chart-order-stop",
        "chart-order-target",
        "chart-order-rr",
        "chart-paper-order-submit",
        "chart-symbol-positions",
    }
    for element_id in required_ids:
        assert element_id in markup

    for symbol in ("BTC-USD", "ETH-USD", "AAPL", "NVDA", "GC=F", "CL=F"):
        assert f'data-chart-symbol="{symbol}"' in markup
    assert 'data-timeframe="30m"' in markup


def test_chart_terminal_reuses_verified_capital_order_flow():
    chart_script = (PROJECT_ROOT / "frontend" / "chart_workspace.js").read_text(encoding="utf-8")
    app_script = (PROJECT_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    markup = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")

    assert "Actions.submitCapitalPaperOrder" in chart_script
    assert "Actions.closeCapitalPosition" in chart_script
    assert "strategy_type: 'MANUAL_CHART'" in chart_script
    assert "/api/chart/" in chart_script
    assert "TV_SYMBOL_MAP" in chart_script
    assert "createTvWidget" in chart_script
    assert "tvSymbol" in chart_script
    assert 'data-risk-stop="0.8"' in markup
    assert 'data-paper-size-percent="10"' in markup
    assert "API.openCapitalPosition" in app_script
    assert "capital:position-opened" in app_script
    assert "capital:position-closed" in app_script
    assert "/^MANUAL(?:_|$)/i.test(strategyType)" in app_script
    assert 'title="Entry strategy"' in app_script
    assert "Chart at Entry · Frozen AI Evidence" in app_script
    assert "entry_chart_snapshot" in app_script
    assert "/api/entry-chart/" in app_script


def test_chart_terminal_exposes_tradingview_widget():
    chart_script = (PROJECT_ROOT / "frontend" / "chart_workspace.js").read_text(encoding="utf-8")
    markup = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")

    assert "tradingview.com/widgetembed" in chart_script
    assert "tv-chart-container" in markup
    assert "document.body.classList.add('chart-workspace-open')" in chart_script


def test_chart_preferences_are_persisted():
    chart_script = (PROJECT_ROOT / "frontend" / "chart_workspace.js").read_text(encoding="utf-8")

    assert "window.localStorage.setItem" in chart_script
    assert "PREFERENCES_STORAGE_KEY" in chart_script
    assert "persistPreferences()" in chart_script
    assert "restorePreferences()" in chart_script


def test_chart_terminal_has_responsive_ticket_and_mobile_controls():
    styles = (PROJECT_ROOT / "frontend" / "styles_chart_workspace.css").read_text(encoding="utf-8")

    assert "@media (max-width: 900px)" in styles
    assert 'grid-template-areas: "header" "picker" "stage" "ticket"' in styles
    assert "@media (max-width: 640px)" in styles
    assert ".paper-order-ticket" in styles
    assert "overflow: auto" in styles
