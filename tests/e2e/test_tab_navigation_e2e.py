import os

import pytest
from playwright.sync_api import Page, expect


pytestmark = pytest.mark.e2e

if str(os.getenv("RUN_E2E", "0")).strip().lower() not in {"1", "true", "yes", "on"}:
    pytestmark = [pytest.mark.e2e, pytest.mark.skip(reason="E2E disabled (set RUN_E2E=1).")]


@pytest.mark.smoke
def test_tab_navigation_contract_e2e(page: Page, base_url: str):
    page.goto(base_url, wait_until="domcontentloaded")
    tabs = [
        ("nav-news", "#view-news"),
        ("nav-calendar", "#view-calendar"),
        ("nav-agents", "#view-agents"),
        ("nav-premarket", "#view-premarket"),
        ("nav-preearnings", "#view-preearnings"),
        ("nav-home", "#view-home"),
    ]
    for nav_id, view_id in tabs:
        page.get_by_test_id(nav_id).click()
        expect(page.locator(view_id)).to_be_visible(timeout=12000)
