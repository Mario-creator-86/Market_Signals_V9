from playwright.sync_api import Page, expect
import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.smoke
def test_agent_fleet_uses_live_worker_state_while_between_cycles(page: Page, base_url: str):
    agent_names = [
        "execution",
        "risk",
        "scanner",
        "sentiment",
        "news_tab",
        "economic_calendar",
        "trade_radar",
        "strategy_research",
        "premarket_scanner",
        "pre_earnings",
    ]
    config_agents = {name: {"enabled": name in {"execution", "trade_radar"}} for name in agent_names}
    runtime_agents = {
        name: {
            "enabled": config_agents[name]["enabled"],
            "running": False,
            "initializing": False,
            "ready": False,
            "lifecycle_state": "disabled" if not config_agents[name]["enabled"] else "stopped",
            "managed_by": "engine" if name in {"execution", "risk", "scanner", "sentiment"} else "api",
        }
        for name in agent_names
    }
    runtime_agents["execution"].update(running=True, ready=True, lifecycle_state="running")
    runtime_agents["trade_radar"].update(
        running=True,
        ready=True,
        lifecycle_state="running",
        next_run_in=300,
    )

    page.route(
        "**/api/agents/control-plane",
        lambda route: route.fulfill(
            json={
                "as_of": "2026-08-09T12:00:00Z",
                "bot": {"running": False, "engine_running": True, "mode": "paper"},
                "agents": runtime_agents,
                "config": {
                    "agents": config_agents,
                    "bot": {"mode": "paper"},
                    "news": {"news_refresh_minutes": 5},
                    "premarket": {},
                    "pre_earnings": {},
                    "strategy_v2": {"research_agent": {}},
                },
            }
        ),
    )
    page.goto(base_url, wait_until="domcontentloaded")
    page.get_by_test_id("nav-agents").click()

    expect(
        page.locator('[data-agent-key="execution"] .agents-status-pill')
    ).to_have_text("Running / bot gated")
    expect(
        page.locator('[data-agent-key="trade_radar"] .agents-status-pill')
    ).to_have_text("Running / scheduled")


@pytest.mark.smoke
def test_risk_manager_limits_save_and_rehydrate_from_server(page: Page, base_url: str):
    agent_names = [
        "execution",
        "risk",
        "scanner",
        "sentiment",
        "news_tab",
        "economic_calendar",
        "trade_radar",
        "strategy_research",
        "premarket_scanner",
        "pre_earnings",
    ]
    settings = {"max_daily_loss_pct": 1.0, "max_risk_per_trade_pct": 0.5}
    requests = []

    def constitution():
        schema = [
            {
                "name": "max_daily_loss_pct", "label": "Max daily loss", "kind": "number",
                "minimum": 0.1, "maximum": 100.0, "step": 0.1, "unit": "%",
                "section": "Loss limits", "help": "Daily loss circuit breaker.",
            },
            {
                "name": "max_risk_per_trade_pct", "label": "Max risk per trade", "kind": "number",
                "minimum": 0.1, "maximum": 100.0, "step": 0.1, "unit": "%",
                "section": "Loss limits", "help": "Maximum risk accepted for one entry.",
            },
        ]
        return {
            "status": "active",
            "active": dict(settings),
            "draft": dict(settings),
            "schema": schema,
            "fingerprint": "e2e-risk-policy",
            "profiles": {"status": "available", "source": "e2e", "items": []},
        }

    def control_plane(route):
        config_agents = {
            name: {"enabled": True}
            for name in agent_names
        }
        config_agents["risk"].update(settings)
        runtime_agents = {
            name: {
                "enabled": True,
                "running": True,
                "initializing": False,
                "ready": True,
                "lifecycle_state": "running",
                "managed_by": "engine" if name in {"execution", "risk", "scanner", "sentiment"} else "api",
            }
            for name in agent_names
        }
        route.fulfill(
            json={
                "as_of": "2026-08-09T12:00:00Z",
                "bot": {"running": False, "engine_running": True, "mode": "paper"},
                "agents": runtime_agents,
                "config": {
                    "agents": config_agents,
                    "bot": {"mode": "paper"},
                    "news": {"news_refresh_minutes": 5},
                    "premarket": {},
                    "pre_earnings": {},
                    "strategy_v2": {"research_agent": {}},
                    "risk_constitution": constitution(),
                },
            }
        )

    def save_settings(route):
        payload = route.request.post_data_json
        requests.append(payload)
        settings.update({key: payload[key] for key in settings})
        route.fulfill(
            json={
                "status": "updated",
                "activated": bool(payload.get("owner_confirmed")),
                "constitution": constitution(),
            }
        )

    page.route("**/api/agents/control-plane", control_plane)
    page.route("**/api/agents/risk/constitution", save_settings)
    page.goto(base_url, wait_until="domcontentloaded")
    page.get_by_test_id("nav-agents").click()

    page.get_by_test_id("risk-settings-open").click()
    expect(page.get_by_test_id("risk-settings-form")).to_be_visible()
    page.get_by_test_id("risk-daily-loss-input").fill("10")
    page.get_by_test_id("risk-per-trade-input").fill("2")
    page.get_by_test_id("risk-settings-save").click()

    expect(page.get_by_test_id("risk-settings-form")).to_have_count(0)
    assert requests == [{"owner_confirmed": True, "max_daily_loss_pct": 10, "max_risk_per_trade_pct": 2}]
    risk_card = page.locator('[data-agent-key="risk"]')
    expect(risk_card.locator(".agents-card-metric", has_text="Active daily limit").locator("strong")).to_have_text("10%")
    expect(risk_card.locator(".agents-card-metric", has_text="Active risk / trade").locator("strong")).to_have_text("2%")

    page.get_by_test_id("risk-settings-open").click()
    expect(page.get_by_test_id("risk-daily-loss-input")).to_have_value("10")
    expect(page.get_by_test_id("risk-per-trade-input")).to_have_value("2")
    page.set_viewport_size({"width": 390, "height": 844})
    assert risk_card.evaluate("node => node.scrollWidth <= node.clientWidth") is True

@pytest.mark.smoke
def test_quality_tabs_navigation_and_console_clean(page: Page, base_url: str):
    errors = []

    def _on_console(msg):
        if msg.type == "error":
            errors.append(msg.text)

    page.on("console", _on_console)
    page.goto(base_url, wait_until="domcontentloaded")

    tabs = [
        ("nav-news", "#view-news", "#news-page-feed-content"),
        ("nav-calendar", "#view-calendar", "#calendar-months-wrapper"),
        ("nav-agents", "#view-agents", ".agents-grid"),
        ("nav-premarket", "#view-premarket", "#premarket-sections"),
        ("nav-preearnings", "#view-preearnings", "#preearnings-sections"),
        ("nav-home", "#view-home", ".home-scanner-top"),
        ("nav-watchlist", "#view-watchlist", "#detailed-watchlist-content"),
        ("nav-scanner", "#view-scanner", "#full-scanner-grid"),
        ("nav-traderadar", "#view-traderadar", "#traderadar-list"),
    ]

    for nav_id, view_id, content_selector in tabs:
        page.get_by_test_id(nav_id).click()
        expect(page.locator(view_id)).to_be_visible(timeout=15000)
        if page.locator(content_selector).count() > 0:
            expect(page.locator(content_selector)).to_be_visible(timeout=15000)

    assert not errors, f"Console errors found: {errors[:5]}"


@pytest.mark.smoke
def test_refresh_buttons_follow_agent_disabled_state(page: Page, base_url: str):
    page.goto(base_url, wait_until="domcontentloaded")

    premarket_state = page.evaluate("async () => (await fetch('/api/premarket')).json()")
    page.get_by_test_id("nav-premarket").click()
    premarket_btn = page.locator("#premarket-refresh-btn")
    expect(premarket_btn).to_be_visible()
    expected_premarket_disabled = bool(premarket_state.get("disabled"))
    page.wait_for_function(
        "(args) => { const [sel, expected] = args; const el = document.querySelector(sel); return !!el && el.disabled === expected; }",
        arg=["#premarket-refresh-btn", expected_premarket_disabled],
    )
    assert premarket_btn.is_disabled() == expected_premarket_disabled

    preearn_state = page.evaluate("async () => (await fetch('/api/pre-earnings')).json()")
    page.get_by_test_id("nav-preearnings").click()
    preearn_btn = page.locator("#preearnings-refresh-btn")
    expect(preearn_btn).to_be_visible()
    expected_preearn_disabled = bool(preearn_state.get("disabled"))
    page.wait_for_function(
        "(args) => { const [sel, expected] = args; const el = document.querySelector(sel); return !!el && el.disabled === expected; }",
        arg=["#preearnings-refresh-btn", expected_preearn_disabled],
    )
    assert preearn_btn.is_disabled() == expected_preearn_disabled


@pytest.mark.smoke
def test_pre_earnings_cache_rejects_out_of_order_snapshots(page: Page, base_url: str):
    page.goto(base_url, wait_until="domcontentloaded")

    result = page.evaluate(
        """async () => {
            const originalGet = API.getPreEarningsState;
            const originalFetch = API.fetchWithTimeout;
            try {
                API._preEarningsCache = {
                    ts: 0,
                    data: {
                        state_revision: 10,
                        items: [{ symbol: 'VTAK', coverage_score: 26 }],
                        status: { last_success_time: '2026-08-09T09:00:00Z' },
                    },
                    inflight: null,
                };

                let resolveFresh;
                API.getPreEarningsState = () => new Promise(resolve => {
                    resolveFresh = resolve;
                });
                const pending = API.getPreEarningsStateCached(0);
                const early = await Promise.race([
                    pending.then(() => 'resolved'),
                    new Promise(resolve => setTimeout(() => resolve('pending'), 30)),
                ]);
                resolveFresh({
                    state_revision: 11,
                    items: [{ symbol: 'VTAK', coverage_score: 56 }],
                    status: { last_success_time: '2026-08-09T09:01:00Z' },
                });
                const fresh = await pending;

                API._preEarningsCache = { ts: 0, data: null, inflight: null };
                const resolvers = [];
                API.fetchWithTimeout = () => new Promise(resolve => resolvers.push(resolve));
                API.getPreEarningsState = originalGet;
                const olderRequest = API.getPreEarningsState();
                const newerRequest = API.getPreEarningsState();
                await Promise.resolve();
                const resolverCount = resolvers.length;
                resolvers[0]({
                    ok: true,
                    json: async () => ({
                        state_revision: 13,
                        items: [{ symbol: 'VTAK', coverage_score: 60 }],
                        status: { last_success_time: '2026-08-09T09:03:00Z' },
                    }),
                });
                const [older, newer] = await Promise.all([olderRequest, newerRequest]);
                const olderLate = API._acceptPreEarningsPayload({
                    state_revision: 12,
                    items: [{ symbol: 'VTAK', coverage_score: 26 }],
                    status: { last_success_time: '2026-08-09T09:02:00Z' },
                });
                return {
                    early,
                    freshCoverage: fresh.items[0].coverage_score,
                    resolverCount,
                    firstRevision: older.state_revision,
                    newerRevision: newer.state_revision,
                    olderLateRevision: olderLate.state_revision,
                    cachedRevision: API._preEarningsCache.data.state_revision,
                    cachedCoverage: API._preEarningsCache.data.items[0].coverage_score,
                };
            } finally {
                API.getPreEarningsState = originalGet;
                API.fetchWithTimeout = originalFetch;
                API._preEarningsCache = { ts: 0, data: null, inflight: null };
            }
        }"""
    )

    assert result == {
        "early": "pending",
        "freshCoverage": 56,
        "resolverCount": 1,
        "firstRevision": 13,
        "newerRevision": 13,
        "olderLateRevision": 13,
        "cachedRevision": 13,
        "cachedCoverage": 60,
    }


@pytest.mark.smoke
def test_watchlist_row_removal_requires_confirmation_without_mutating_live_data(
    page: Page,
    base_url: str,
):
    ticker = "E2E-REMOVE"
    delete_payloads = []

    def intercept_ticker_delete(route):
        if route.request.method != "DELETE":
            route.continue_()
            return
        payload = route.request.post_data_json
        delete_payloads.append(payload)
        route.fulfill(
            status=200,
            json={
                "status": "ok",
                "removed": payload.get("tickers", []),
                "watchlist": {
                    "stocks": [],
                    "crypto": [],
                    "hot_list": [],
                    "order": [],
                    "settings": {},
                    "scanner_exclusions": payload.get("tickers", []),
                    "schema_version": 2,
                },
            },
        )

    page.route("**/api/tickers", intercept_ticker_delete)
    page.goto(base_url, wait_until="domcontentloaded")
    page.get_by_test_id("nav-watchlist").click()
    expect(page.locator("#view-watchlist")).to_be_visible()

    render_and_click_remove = """ticker => {
        UI.renderDetailedWatchlist({
            tickers: [{
                ticker,
                asset_type: 'stock',
                price: 42.5,
                change_pct: 1.25,
                signal_score: 0,
                trend_1d: 'NEUTRAL',
                is_hot: false,
            }],
            watchlist: {
                stocks: [ticker],
                crypto: [],
                hot_list: [],
                order: [ticker],
                settings: {},
            },
        });
        const button = document.querySelector(`[data-testid="watchlist-remove-${ticker}"]`);
        if (!button) return false;
        button.click();
        return true;
    }"""

    modal = page.get_by_test_id("delete-confirm-modal")
    assert page.evaluate(render_and_click_remove, ticker) is True
    expect(modal).to_be_visible()
    expect(page.locator("#delete-confirm-message")).to_contain_text(ticker)
    page.get_by_test_id("delete-confirm-cancel").click()
    expect(modal).to_be_hidden()
    page.wait_for_timeout(100)
    assert delete_payloads == []

    assert page.evaluate(render_and_click_remove, ticker) is True
    expect(modal).to_be_visible()
    with page.expect_request(
        lambda request: request.method == "DELETE" and request.url.endswith("/api/tickers")
    ) as delete_request:
        page.get_by_test_id("delete-confirm-confirm").click()

    assert delete_request.value.post_data_json == {
        "tickers": [ticker],
        "return_state": False,
    }
    assert delete_payloads == [{"tickers": [ticker], "return_state": False}]
    expect(modal).to_be_hidden()


def test_basic_table_alignment_watchlist_and_traderadar(page: Page, base_url: str):
    page.goto(base_url, wait_until="domcontentloaded")

    page.get_by_test_id("nav-watchlist").click()
    expect(page.locator("#view-watchlist")).to_be_visible()
    headers = page.locator("#view-watchlist .wl-table-header > div")
    first_row = page.locator("#view-watchlist .wl-list .wl-row").first.locator("> div")
    if headers.count() > 0 and first_row.count() > 0:
        assert abs(headers.count() - first_row.count()) <= 1

    page.get_by_test_id("nav-traderadar").click()
    expect(page.locator("#view-traderadar")).to_be_visible()
    tr_headers = page.locator("#view-traderadar .wl-table-header > div")
    tr_row = page.locator("#view-traderadar .wl-list .wl-row").first.locator("> div")
    if tr_headers.count() > 0 and tr_row.count() > 0:
        assert abs(tr_headers.count() - tr_row.count()) <= 1
