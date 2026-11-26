"""Source contracts for safe, accessible Watchlist management controls."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
INDEX_SOURCE = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    """Return a named source section and fail clearly when its contract disappears."""
    start_index = source.find(start)
    assert start_index >= 0, f"Missing source marker: {start}"
    end_index = source.find(end, start_index + len(start))
    assert end_index >= 0, f"Missing source marker after {start}: {end}"
    return source[start_index:end_index]


def test_each_watchlist_row_exposes_an_accessible_remove_control_without_expanding_row():
    button_match = re.search(
        r'<button\s+type="button"\s+class="wl-remove-row-btn".*?</button>',
        APP_SOURCE,
        flags=re.DOTALL,
    )
    assert button_match, "The compact Watchlist row must render a remove button"
    button = button_match.group(0)

    assert 'data-testid="watchlist-remove-${esc(d.ticker)}"' in button
    assert 'title="Remove ${esc(d.ticker)} from Watchlist"' in button
    assert 'aria-label="Remove ${esc(d.ticker)} from Watchlist"' in button
    assert "Actions.removeTicker('${esc(d.ticker)}', this)" in button
    assert (
        '<div class="wl-actions-cell" data-label="Actions" '
        'onclick="event.stopPropagation()">'
    ) in APP_SOURCE


def test_bulk_removal_normalizes_symbols_and_uses_one_delete_request():
    api_remove = _between(
        APP_SOURCE,
        "async removeTickers(tickers)",
        "async removeTicker(ticker)",
    )

    assert "new Set" in api_remove
    assert ".toUpperCase().trim()" in api_remove
    assert "`${this.BASE_URL}/tickers`" in api_remove
    assert "method: 'DELETE'" in api_remove
    assert "JSON.stringify({ tickers: normalized, return_state: false })" in api_remove
    assert "if (!res.ok)" in api_remove
    assert "payload.detail" in api_remove

    removal_flow = _between(
        APP_SOURCE,
        "async executeWatchlistRemoval(tickers)",
        "async reorderWatchlist(newOrder)",
    )
    assert "await API.removeTickers(symbols)" in removal_flow
    assert "watchlistRemovalInFlight" in removal_flow


def test_bulk_selection_is_explicit_and_limited_to_currently_visible_tickers():
    assert "watchlistSelectedTickers: new Set()" in APP_SOURCE
    assert "watchlistVisibleTickers: []" in APP_SOURCE

    select_visible = _between(
        APP_SOURCE,
        "toggleWatchlistSelectVisible(selected)",
        "clearWatchlistSelection()",
    )
    assert "(this.watchlistVisibleTickers || []).forEach" in select_visible
    assert "this.watchlistSelectedTickers.add(symbol)" in select_visible
    assert "this.watchlistSelectedTickers.delete(symbol)" in select_visible

    assert 'data-testid="watchlist-select-visible"' in APP_SOURCE
    assert 'aria-label="Select all visible watchlist tickers"' in APP_SOURCE
    assert 'data-testid="watchlist-select-${esc(d.ticker)}"' in APP_SOURCE
    assert 'aria-label="Select ${esc(d.ticker)}"' in APP_SOURCE
    assert 'data-testid="watchlist-remove-selected"' in APP_SOURCE
    assert "Actions.removeSelectedTickers(this)" in APP_SOURCE
    assert "selectedCount === 0 || this.watchlistRemovalInFlight" in APP_SOURCE


def test_detailed_table_filters_state_rows_through_authoritative_watchlist_membership():
    renderer = _between(
        APP_SOURCE,
        "UI.renderDetailedWatchlist = function (providedState)",
        "UI.toggleActivity = function",
    )

    assert "const authoritativeWatchlist" in renderer
    assert "const watchlistKeys = ['order', 'stocks', 'crypto', 'yahoo', 'binance', 'cryptos']" in renderer
    assert "const allowedSymbols = new Set()" in renderer
    assert "if (!symbol || UI.watchlistRemovedTickers.has(symbol)) return false" in renderer
    assert "return !hasAuthoritativeWatchlist || allowedSymbols.has(symbol)" in renderer
    assert "if (!knownSymbols.has(symbol)) UI.watchlistSelectedTickers.delete(symbol)" in renderer


def test_removal_failure_keeps_the_page_alive_and_restores_controls():
    removal_flow = _between(
        APP_SOURCE,
        "async executeWatchlistRemoval(tickers)",
        "async reorderWatchlist(newOrder)",
    )

    request_index = removal_flow.index("await API.removeTickers(symbols)")
    state_mutation_index = removal_flow.index("cachedState.tickers = cachedState.tickers.filter")
    assert request_index < state_mutation_index, "Rows must not be removed before the server succeeds"
    assert "Could not remove from Watchlist:" in removal_flow
    assert "finally" in removal_flow
    assert "UI.watchlistRemovalInFlight = false" in removal_flow
    assert "classList.remove('is-removing')" in removal_flow
    assert "window.location.reload" not in removal_flow


def test_watchlist_confirmation_is_an_accessible_dialog_with_cancel_focus_and_escape():
    modal_match = re.search(
        r'<div\s+id="delete-confirm-modal".*?</div>\s*</div>\s*</div>',
        INDEX_SOURCE,
        flags=re.DOTALL,
    )
    assert modal_match, "A confirmation modal must exist"
    modal = modal_match.group(0)

    assert 'role="alertdialog"' in modal
    assert 'aria-modal="true"' in modal
    assert 'aria-labelledby="delete-confirm-modal-title"' in modal
    assert 'aria-describedby="delete-confirm-message"' in modal
    assert 'data-testid="delete-confirm-cancel"' in modal
    assert 'data-testid="delete-confirm-confirm"' in modal

    confirm_flow = _between(
        APP_SOURCE,
        "showWatchlistDeleteConfirm(tickers, returnFocus = null)",
        "handleStrategyLogic(scope)",
    )
    assert "pendingDeleteContext = { type: 'watchlist', tickers: symbols }" in confirm_flow
    assert "HOT membership and ticker-specific settings will also be removed" in confirm_flow
    assert "document.getElementById('delete-confirm-cancel')?.focus()" in confirm_flow
    assert "returnFocus.focus()" in confirm_flow
    assert "Actions.executeWatchlistRemoval(context.tickers)" in confirm_flow

    escape_handler = _between(
        APP_SOURCE,
        "document.addEventListener('keydown', (event) =>",
        "UI.renderDetailedWatchlist = function (providedState)",
    )
    assert "event.key !== 'Escape'" in escape_handler
    assert "UI.closeDeleteConfirm(true)" in escape_handler

