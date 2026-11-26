"""E2E regression: execution policy exists only inside Risk Manager."""

from playwright.sync_api import Page, expect


def _open_risk_manager(page: Page, base_url: str):
    page.goto(base_url)
    page.get_by_test_id("nav-agents").click()
    expect(page.get_by_test_id("risk-settings-open")).to_be_visible()
    page.get_by_test_id("risk-settings-open").click()
    expect(page.get_by_test_id("risk-settings-form")).to_be_visible()
    expect(page.get_by_test_id("risk-trade-policy")).to_be_visible()


def test_duplicate_autonomous_surfaces_are_removed(page: Page, base_url: str):
    page.goto(base_url)
    page.get_by_test_id("nav-agents").click()
    expect(page.get_by_test_id("agent-customization-card")).to_have_count(0)
    expect(page.get_by_test_id("paper-execution-control")).to_have_count(0)


def test_risk_manager_hydrates_sizing_entry_and_exit_policy(page: Page, base_url: str):
    _open_risk_manager(page, base_url)
    expect(page.get_by_test_id("risk-trade-field-execution_mode")).not_to_have_value("")
    expect(page.get_by_test_id("risk-trade-field-base_position_capital_pct")).not_to_have_value("")
    expect(page.get_by_test_id("risk-trade-field-pre_earnings_min_gap_risk")).not_to_have_value("")
    expect(page.get_by_test_id("risk-trade-field-pre_earnings_require_directional_edge")).not_to_have_value("")
    expect(page.get_by_test_id("risk-trade-field-pre_earnings_post_event_ai_review")).not_to_have_value("")


def test_risk_manager_submit_contains_one_nested_trade_policy(page: Page, base_url: str):
    captured = {}

    def capture(route, request):
        captured["payload"] = request.post_data_json
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"status":"updated","mode":"active","applied_live":true,"constitution":{}}',
        )

    page.route("**/api/agents/risk/constitution", capture)
    _open_risk_manager(page, base_url)
    # The isolated E2E runtime deliberately starts without an owner
    # Constitution or REDUCE value. Select an explicit profile and complete
    # the remaining owner choice before exercising activation.
    page.get_by_test_id("risk-profile-balanced").click()
    reduce_input = page.get_by_test_id("risk-trade-field-reduce_fraction_pct")
    if reduce_input.input_value() == "":
        reduce_input.fill("20")
    expect(page.get_by_test_id("risk-settings-save")).to_be_enabled()
    page.get_by_test_id("risk-settings-save").click()
    page.wait_for_timeout(300)

    payload = captured["payload"]
    assert payload["owner_confirmed"] is True
    assert "trade_policy" in payload
    assert payload["trade_policy"]["execution_mode"]
    assert "pre_earnings_min_gap_risk" in payload["trade_policy"]
    assert "max_positions" in payload
    assert "mode" not in payload
