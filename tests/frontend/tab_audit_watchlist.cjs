// Watchlist tab audit (2026-11-15): the tab must not present a Friday close as
// LIVE, a missing analysis as "Flat +0.00%", a months-old trend as current, a
// EUR quote in dollars, or a failed AI run as a "neutral / WAIT" verdict.
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(require('path').resolve(__dirname, '../../frontend/app.js'), 'utf8');

const slice = (startMarker, endMarker) => {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start + startMarker.length);
    assert(start >= 0 && end > start, `source section not found: ${startMarker}`);
    return source.slice(start, end);
};
const mergeSrc = slice('UI.mergeLivePricesIntoState = function', 'UI.refreshWatchlistLivePrices = async function');
const renderSrc = slice('UI.renderDetailedWatchlist = function', 'UI.toggleActivity = function');
const exportSrc = slice('    exportWatchlistCSV() {', '    // Advanced Watchlist Functions - filterWatchlist');

const container = {
    innerHTML: '',
    querySelector: () => null,
    querySelectorAll: () => [],
};
const errors = [];
let searchValue = '';
let lastBlob = null;
const context = {
    UI: {
        watchlistRemovedTickers: new Set(),
        watchlistSelectedTickers: new Set(),
        watchlistExpandedRows: new Set(),
        watchlistVisibleTickers: [],
        watchlistQuickAiLoading: {},
        watchlistFilters: {},
        watchlistSortBy: null,
        watchlistSortAsc: true,
        watchlistSortMode: 'snapshot',
        watchlistGroupBy: 'none',
        _livePricesCache: {},
        isTickerNew: () => false,
        getWatchlistQuickAi: () => null,
        formatPrice: (v) => String(v),
        _formatLocalDateTime: (v) => String(v).slice(0, 16),
        syncWatchlistSelectionControls() {},
        captureWatchlistSortSnapshot() {},
        scheduleRender() {},
        showToast() {},
    },
    API: { _watchlistCache: null, _warnNetworkOnce() {}, getWatchlistCached: () => Promise.resolve(null) },
    Actions: {},
    ViewRouter: { activeView: 'watchlist' },
    cachedState: null,
    document: {
        getElementById: (id) => {
            if (id === 'detailed-watchlist-content') return container;
            if (id === 'watchlist-search') return { value: searchValue };
            return null;
        },
        createElement: () => ({ click() {}, set href(v) {}, set download(v) {} }),
        body: { appendChild() {}, removeChild() {} },
    },
    Blob: function (parts) { lastBlob = parts.join(''); },
    URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
    console: { error: (...a) => errors.push(a), warn: (...a) => errors.push(a), log() {} },
    Date, Math, Number, String, Object, Array, Set, Map, JSON, Promise,
};
vm.createContext(context);
vm.runInContext(mergeSrc, context);
vm.runInContext(renderSrc, context);
vm.runInContext(`UI.__csv = ({ ${exportSrc} }); UI.exportWatchlistCSV = UI.__csv.exportWatchlistCSV;`, context);

const isoAgo = (seconds) => new Date(Date.now() - seconds * 1000).toISOString();
const HOUR = 3600, DAY = 86400;
const baseRow = (ticker, extra = {}) => ({
    ticker, price: 100, change_pct: 1.5, asset_type: 'stock', sector: 'Technology',
    trend_1d: 'BULL', trend_4h: 'BULL', col_1h: 'BULL 22:56:48', trend_alignment: 'BULL_ALIGN',
    signal_status: 'none', has_open_signal: false, signal_score: 0, earnings_days: -1,
    analysis_available: true, analysis_updated_at: isoAgo(2 * HOUR), rsi: 55, macd_hist: 0.4,
    risk_flags: [], news_pulse: {}, strategy_notes: '', allowed_strategies: [], ...extra,
});
const render = (rows, { live = {}, ui = {} } = {}) => {
    errors.length = 0;
    Object.assign(context.UI, {
        watchlistFilters: {}, watchlistSortBy: null, watchlistSortMode: 'snapshot', watchlistGroupBy: 'none',
        watchlistExpandedRows: new Set(), _livePricesCache: live, getWatchlistQuickAi: () => null,
    }, ui);
    const state = { tickers: rows.map(r => ({ ...r })) };
    context.cachedState = state;
    context.UI.renderDetailedWatchlist(state);
    assert.equal(errors.length, 0, `render must not log errors: ${String(errors)}`);
    assert(!container.innerHTML.includes('Watchlist unavailable.'), 'render must not fall into the error state');
    return container.innerHTML;
};
const rowHtml = (html, ticker) => {
    const start = html.indexOf(`data-ticker="${ticker}"`);
    assert(start >= 0, `row for ${ticker} missing`);
    const next = html.indexOf('data-ticker="', start + 20);
    return html.slice(start, next >= 0 ? next : html.length);
};
const quote = (extra) => ({ price: 356.27, change_pct: 0.39, live: false, source: 'yahoo', timestamp: isoAgo(3),
    age_seconds: 3, freshness: 'CLOSED', market_time: isoAgo(39 * HOUR), market_age_seconds: 39 * HOUR,
    currency: 'USD', exchange: 'NasdaqGS', ...extra });

// 1. Friday close on a Sunday is CLOSED, not LIVE; the tooltip gives the real age.
let html = render([baseRow('AAPL')], { live: { AAPL: quote() } });
let row = rowHtml(html, 'AAPL');
assert(row.includes('>CLOSED</span>'), 'closed-market quote must be badged CLOSED');
assert(!row.includes('>LIVE</span>') && !row.includes('wl-price-live'), 'closed-market quote must not look live');
assert(row.includes('last trade 39h ago'), 'tooltip must state the age of the last trade, not of our fetch');
assert(row.includes('$356.27'), 'USD quote keeps the dollar sign');

// 2. Even if a backend still says LIVE, an old exchange time wins in the browser.
row = rowHtml(render([baseRow('AAPL')], { live: { AAPL: quote({ freshness: 'LIVE', live: true }) } }), 'AAPL');
assert(row.includes('>CLOSED</span>') && !row.includes('>LIVE</span>'), 'old last-trade time must override a LIVE flag');
row = rowHtml(render([baseRow('MSFT')], { live: { MSFT: quote({ freshness: 'LIVE', live: true, market_time: isoAgo(20), market_age_seconds: 20 }) } }), 'MSFT');
assert(row.includes('>LIVE</span>') && row.includes('wl-price-live'), 'a trade from seconds ago stays LIVE');

// 3. Delisted name: months-old last trade is STALE with no day change.
row = rowHtml(render([baseRow('EA', { analysis_available: false, trend_alignment: 'UNKNOWN', trend_1d: 'N/A', trend_4h: 'N/A', col_1h: 'N/A', change_pct: 0, rsi: 50, macd_hist: 0 })],
    { live: { EA: quote({ price: 209.7, change_pct: null, freshness: 'STALE', market_time: isoAgo(103 * DAY), market_age_seconds: 103 * DAY }) } }), 'EA');
assert(row.includes('>STALE</span>'), 'months-old quote must be STALE');
assert(row.includes('last trade 103d ago'), 'stale quote must show its real age');
assert(row.includes('>—</div>') && !row.includes('+0.00%'), 'no day change for a stale quote, never +0.00%');
assert(row.includes('>No data</span>') && !row.includes('>Flat<'), 'a ticker without analysis is "No data", not "Flat"');
assert(!row.includes('RSI 50.0'), 'placeholder RSI 50 must not be explained as a reading');

// 4. A ticker with no analysis and no fetched quote: no fabricated +0.00%.
row = rowHtml(render([baseRow('CPRX', { analysis_available: false, analysis_updated_at: null, trend_alignment: 'UNKNOWN', change_pct: 0, price: 31.49 })]), 'CPRX');
assert(row.includes('>—</div>') && !row.includes('+0.00%'), 'placeholder change must render as a dash');
assert(row.includes('>No data</span>'), 'no analysis => No data');
assert(row.includes('>STALE</span>'), 'a price with no fetched quote is STALE');

// 5. Quote currency: a Paris listing is in euro.
row = rowHtml(render([baseRow('VANTI.PA')], { live: { 'VANTI.PA': quote({ price: 0.084, currency: 'EUR', exchange: 'Paris' }) } }), 'VANTI.PA');
assert(row.includes('€0.084') && !row.includes('$0.084'), 'EUR quote must not be printed with a dollar sign');
assert(row.includes('Paris'), 'tooltip names the venue');

// 6. Engine analysis from 68 days ago is shown as stale, not as today's trend.
row = rowHtml(render([baseRow('NVDA', { analysis_updated_at: isoAgo(68 * DAY), analysis_age_seconds: 68 * DAY })], { live: { NVDA: quote() } }), 'NVDA');
assert(row.includes('>Stale 68d</span>') && !row.includes('>Bull</span>'), 'a 68-day-old trend must be labelled stale');
assert(row.includes('STALE: the engine has not re-analysed'), 'tooltip explains why the trend is unusable');
row = rowHtml(render([baseRow('NVDA')], { live: { NVDA: quote() } }), 'NVDA');
assert(row.includes('>Bull</span>'), 'a recent analysis shows its trend');
// Without a fresh quote the old engine row's change is not today's change.
row = rowHtml(render([baseRow('NVDA', { analysis_updated_at: isoAgo(68 * DAY), change_pct: -2.22 })]), 'NVDA');
assert(!row.includes('-2.22%') && row.includes('>—</div>'), 'a 68-day-old change must not be shown as the day change');
assert(row.includes('68d old'), 'the price age falls back to the analysis age');

// 7. Conflicting timeframes are "Mixed", not "Flat".
row = rowHtml(render([baseRow('TSLA', { trend_alignment: 'MIXED' })], { live: { TSLA: quote() } }), 'TSLA');
assert(row.includes('>Mixed</span>') && !row.includes('>Flat<'), 'MIXED alignment must be labelled Mixed');

// 8. Signal column and summary cards.
html = render([
    baseRow('AAA', { change_pct: 2 }), baseRow('BBB', { change_pct: -1 }),
    baseRow('CCC', { change_pct: null }),
    baseRow('SIG', { signal_status: 'open', has_open_signal: true, signal_direction: 'LONG', signal_score: 82.4, change_pct: 0.5 }),
], { live: { AAA: quote({ change_pct: 2 }), BBB: quote({ change_pct: -1 }), CCC: quote({ change_pct: null }), SIG: quote({ change_pct: 0.5 }) } });
assert(rowHtml(html, 'AAA').includes('>No signal</span>'), 'a ticker without a signal says "No signal"');
assert(rowHtml(html, 'SIG').includes('>OPEN LONG · 82</span>'), 'an open signal shows its score');
assert(html.includes('Avg day change') && !html.includes('Momentum Breadth'), 'the average change card is labelled as what it is');
assert(html.includes('2 up · 1 down · 1 no data'), 'breadth counts up/down and keeps unknowns out of the average');
assert(html.includes('+0.50%'), 'average uses only known changes: (2 - 1 + 0.5) / 3');
assert(html.includes('1 signal scoring 70+'), 'open-setup detail counts signals');
assert(html.includes('Sort order: Fixed') && !html.includes('Sortare'), 'sort toggle label is English');
assert(!html.includes('>Forex<'), 'the Forex filter could never match a row');

// 9. Group + live sort used to throw (TDZ) and blank the whole tab.
html = render([
    baseRow('AAA', { sector: 'Tech' }), baseRow('BBB', { sector: 'Energy' }), baseRow('CCC', { sector: 'Tech' }),
], { ui: { watchlistGroupBy: 'sector', watchlistSortBy: 'ticker', watchlistSortMode: 'live' } });
assert.equal((html.match(/wl-group-header/g) || []).length, 2, 'each group header appears once');
html = render([
    baseRow('AAA', { sector: 'Tech' }), baseRow('BBB', { sector: 'Energy' }), baseRow('CCC', { sector: 'Tech' }),
], { ui: { watchlistGroupBy: 'sector' } });
assert.equal((html.match(/wl-group-header/g) || []).length, 2, 'groups stay contiguous without a sort column too');

// 10. The Trend filter selects on what the Trend column shows.
html = render([
    baseRow('UPUP'), baseRow('DOWN', { trend_1d: 'BULL', trend_alignment: 'BEAR_ALIGN' }),
], { ui: { watchlistFilters: { trend_regime: 'BULL' } } });
assert(html.includes('data-ticker="UPUP"') && !html.includes('data-ticker="DOWN"'), 'Trend: Bull must not list a row whose column says Bear');
html = render([baseRow('UPUP'), baseRow('MIX', { trend_alignment: 'MIXED' })], { ui: { watchlistFilters: { trend_regime: 'NEUTRAL' } } });
assert(html.includes('data-ticker="MIX"') && !html.includes('data-ticker="UPUP"'), 'a stored legacy NEUTRAL filter maps to Mixed');

// 11. Quick AI: a failed run is not a verdict; third-party text is escaped.
const placeholder = { bias: 'neutral', confidence: 0, time_horizon: 'unknown', top_reasons: [], top_risks: [], action: 'WAIT', notes: 'unknown' };
const expand = (quickAi, extra = {}) => rowHtml(render([baseRow('AAPL', { quick_ai: quickAi, ...extra })], {
    live: { AAPL: quote() }, ui: { watchlistExpandedRows: new Set(['AAPL']) },
}), 'AAPL');
row = expand({ enabled: true, available: false, error: 'ollama unavailable', analysis: placeholder, timestamp: isoAgo(5) });
assert(row.includes('AI analysis unavailable: ollama unavailable'), 'failure reason is shown');
assert(!row.includes('Bias: neutral'), 'the neutral placeholder must not be shown as a verdict');
row = expand({ enabled: true, analysis: placeholder, timestamp: isoAgo(5) });
assert(!row.includes('Bias: neutral'), 'a legacy cached placeholder is not a verdict either');
row = expand({ enabled: true, available: true, model: 'm1', timestamp: isoAgo(5),
    analysis: { ...placeholder, bias: 'bullish', confidence: 80, action: 'BUY', notes: '<b>x</b>', top_reasons: ['r'] } });
assert(row.includes('Bias: bullish') && row.includes('80/100'), 'a real answer is shown with its 0-100 confidence');
assert(row.includes('&lt;b&gt;x&lt;/b&gt;') && !row.includes('<b>x</b>'), 'AI notes are escaped');
row = expand(null, { strategy_notes: '<img src=x onerror=alert(1)>', allowed_strategies: ['STRONG/PULLBACK', 'STRONG/BREAKOUT'] });
assert(row.includes('&lt;img src=x') && !row.includes('<img src=x'), 'strategy notes are escaped');
assert(row.includes('No best strategy yet') && !row.includes('>STRONG/PULLBACK<'), 'the first allowed strategy is not presented as the best one');

// 12. CSV export: unknown values are blank, never 0 / range / NEUTRAL.
context.cachedState = { tickers: [
    { ...baseRow('AAPL', { price_source: 'yahoo', price_timestamp: isoAgo(3), price_currency: 'USD', price_freshness: 'CLOSED', price_market_time: '2026-11-13T21:00:01+00:00', change_pct: 0.39 }) },
    { ...baseRow('CPRX', { analysis_available: false, trend_alignment: 'UNKNOWN', change_pct: 0, price: 0, market_regime: 'range', analysis_updated_at: null }) },
    { ...baseRow('=EVIL', {}) },
] };
context.UI.exportWatchlistCSV();
const csv = lastBlob.split('\n').map(line => line.slice(1, -1).split('","'));
const header = csv[0];
const cell = (ticker, column) => csv.find(r => r[0].replace(/^'/, '') === ticker)[header.indexOf(column)];
assert(!header.includes('Volume'), 'the always-zero Volume column is gone');
assert.equal(cell('AAPL', 'Price Status'), 'CLOSED');
assert.equal(cell('AAPL', 'Price As Of'), '2026-11-13T21:00:01+00:00');
assert.equal(cell('AAPL', 'Change %'), '0.39');
assert.equal(cell('AAPL', 'Trend'), 'BULL');
assert.equal(cell('AAPL', 'Signal Score'), '', 'no signal => no score');
assert.equal(cell('AAPL', 'Regime'), '', 'no signal => no regime');
assert.equal(cell('CPRX', 'Price'), '', 'missing price is blank, not 0');
assert.equal(cell('CPRX', 'Change %'), '', 'placeholder change is blank, not 0.00');
assert.equal(cell('CPRX', 'Trend'), 'NO DATA');
assert.equal(cell('CPRX', 'Earnings Days'), '');
assert(csv.some(r => r[0] === "'=EVIL"), 'formula-like text cells are neutralised');

console.log('Watchlist tab audit: honest price badge, currency, trend age, no-data rows, breadth, grouping, filter, Quick AI, CSV: PASS');
