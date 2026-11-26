
import pytest
from playwright.sync_api import Page, expect

def test_home_page_loads(page: Page, base_url: str):
    """
    Smoke test: Verify the home page loads and title is correct.
    """
    page.goto(base_url, wait_until="domcontentloaded")
    
    # check title
    expect(page).to_have_title("MarketMuse Premium | AI Signals for Smart Trading")
    
    # check header exists
    expect(page.locator(".logo")).to_be_visible()
    
    # check navigation exists
    expect(page.get_by_test_id("nav-home")).to_be_visible()

def test_dashboard_cards_visible(page: Page, base_url: str):
    """
    Smoke test: Verify main dashboard cards are present.
    """
    page.goto(base_url, wait_until="domcontentloaded")
    
    # Check for Market Scanner section
    expect(page.locator(".home-scanner-top")).to_be_visible()
    
    # Check for Signals section
    expect(page.locator(".home-signals-card")).to_be_visible()
    
    # Check for News card
    expect(page.locator("#card-news")).to_be_visible()

def test_responsiveness_mobile(page: Page, base_url: str):
    """
    Verify layout adapts (basic check) on mobile viewport.
    """
    # Set viewport to mobile size
    page.set_viewport_size({"width": 375, "height": 667})
    page.goto(base_url, wait_until="domcontentloaded")
    
    # Logo should still be visible
    expect(page.locator(".logo")).to_be_visible()
    
    # Check if cards are stacked (visually inspecting width or just existence)
    # This is a basic check that it renders without error
    expect(page.locator(".home-scanner-top")).to_be_visible()
