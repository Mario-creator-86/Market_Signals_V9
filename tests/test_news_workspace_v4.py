from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_news_workspace_has_decision_first_controls_and_metrics():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")

    for required_id in (
        'id="news-metric-total"',
        'id="news-metric-moving"',
        'id="news-metric-watchlist"',
        'id="news-metric-sources"',
        'id="news-source-select"',
        'id="news-only-actionable"',
    ):
        assert required_id in html

    for focus in ("top", "all", "watchlist", "breaking", "earnings", "macro", "saved"):
        assert f'data-news-focus="{focus}"' in html

    assert 'data-window="24h"' in html
    assert "Market Newsroom" in html
    assert "EVIDENCE-BASED" in html


def test_news_workspace_logic_defaults_to_current_prioritized_flow():
    app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

    assert "localStorage.getItem('news_time_window') || '24h'" in app
    assert "localStorage.getItem('news_focus_mode') || 'top'" in app
    assert "UI.renderNewsOverview = function" in app
    assert "UI._syncNewsSourceSelector = function" in app
    assert "UI.setNewsFocusMode = function" in app
    assert "Snapshot is ${ageMinutes}m old" in app
    assert "stuck_tickers" in app


def test_news_workspace_visual_layer_is_scoped_and_neutral():
    styles = (ROOT / "frontend" / "styles_premium.css").read_text(encoding="utf-8")

    assert "NEWSROOM WORKSPACE V4" in styles
    assert "#view-news .news-overview-grid" in styles
    assert "#view-news .news-focus-rail" in styles
    assert "#view-news .news-primary-action" in styles
    assert "--nw-blue: #2563eb" in styles


def test_intelligence_briefing_answers_three_essential_questions():
    app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "frontend" / "styles_premium.css").read_text(encoding="utf-8")

    for label in (
        "WHAT IS THE NEWS?",
        "WHAT DOES IT MEAN FOR THE MARKET?",
        "TICKER LINK &amp; LIKELY DIRECTION",
    ):
        assert label in app
    for field in (
        "article_explainer",
        "market_implication",
        "ticker_implication",
        "briefing_version",
    ):
        assert field in app
    assert "UI.ensureEssentialNewsBriefing" in app
    assert "UI._setNewsArticleAnalysis" in app
    assert "#view-news .news-briefing-answer" in styles
    assert "#view-news .news-direction-chip.up" in styles
    assert "#view-news .news-direction-chip.down" in styles


def test_split_news_layout_keeps_briefing_visible_while_page_scrolls():
    styles = (ROOT / "frontend" / "styles_premium.css").read_text(encoding="utf-8")

    assert "#view-news .news-layout-split #news-page-feed-content" in styles
    assert "max-height: none" in styles
    assert "#view-news .news-layout-split #news-insight-section.news-cockpit-insight-pane" in styles
    assert "overscroll-behavior: contain" in styles
    assert "#view-news .news-layout-split #news-insight-section .news-insight-expand-header" in styles
    assert "#view-news #news-insight-section.news-cockpit-insight-pane" in styles
    assert "grid-row: 1" in styles


def test_news_workspace_has_no_legacy_fixed_footer_void():
    news_styles = (ROOT / "frontend" / "styles_news.css").read_text(encoding="utf-8")
    premium_styles = (ROOT / "frontend" / "styles_premium.css").read_text(encoding="utf-8")

    assert "padding-bottom: 200px" not in news_styles
    assert "padding-bottom: 16px !important" in news_styles
    assert "#view-news .news-layout-split #news-page-feed-content" in premium_styles
    assert "padding-bottom: 16px" in premium_styles
