// Hot List tab audit 2026-11-15 — frontend regressions.
// Run: node tests/frontend/tab_audit_hotlist.cjs
const fs = require('fs'), vm = require('vm'), path = require('path'), assert = require('assert/strict');

/* ------------------------------------------------------------------ cards */
const appSource = fs.readFileSync(path.resolve(__dirname, '../../frontend/app.js'), 'utf8');
const start = appSource.indexOf('UI.renderHotList = function');
const end = appSource.indexOf('UI.renderBacktestHistory = function', start);
assert(start >= 0 && end > start, 'renderHotList slice not found');

const grid = { innerHTML: '' };
const detailsBtn = { innerHTML: '', disabled: false, title: '', dataset: {} };
const errors = [];
const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const cardContext = {
    UI: { loadHotlistHeat() {}, formatPrice: (v) => Number(v).toFixed(2), _escapeHtml: esc, _livePricesCache: {} },
    document: { getElementById: (id) => (id === 'hotlist-grid' ? grid : (id === 'hotlist-load-details-btn' ? detailsBtn : null)) },
    window: {}, IS_DEV: false, Date, Math, Number, Object, Array, String, Boolean, JSON,
    console: { warn: (...a) => errors.push(a), error: (...a) => errors.push(a), log() {} },
};
vm.createContext(cardContext);
vm.runInContext(appSource.slice(start, end), cardContext);
const renderCard = (item, livePrices = {}) => {
    errors.length = 0;
    cardContext.UI._livePricesCache = livePrices;
    cardContext.UI.renderHotList({ hot_list: [{ ticker: 'NVDA', ...item }] });
    assert.equal(errors.length, 0, errors.map(String).join('\n'));
    assert(grid.innerHTML.includes('NVDA'), 'card must render');
    return grid.innerHTML;
};
const isoAgo = (seconds) => new Date(Date.now() - seconds * 1000).toISOString();

// 1) The live NVDA card: stale engine price, no bar metrics, verdict was AVOID / RISK HEAT 95%.
const staleInsufficient = {
    price: 225.25, change_pct: -2.22, price_stale: true, price_as_of: isoAgo(68 * 86400), price_source: 'yahoo',
    trend_1d: 'BULL', trend_4h: 'BULL', technicals_stale: true, technicals_as_of: isoAgo(68 * 86400),
    earnings_days: 2, earnings_date: '2026-11-17', earnings_when: 'AMC',
    earnings_edge_score: 49, earnings_setup: 'N/A', earnings_play: 'INSUFFICIENT_DATA', verdict_status: 'insufficient_data',
    risk_level: 'high', overall_score: 43.2, priority_score: 81.6, confidence_score: 12, data_coverage_pct: 29,
    earnings_risk_flags: ['momentum_unknown', 'adx_unknown', 'earnings_history_unknown', 'risk_events_present', 'price_stale', 'technicals_stale'],
    risk_factor_flags: ['risk_events_present'],
    missing_inputs: ['price', 'trend', 'momentum', 'adx', 'earnings_history'],
    top_risk: 'risk_events_present',
    _data_quality: { has_price: true, has_trends: true, has_fundamental_details: false, fundamentals_provider: false, source: 'fast' },
};
let html = renderCard(staleInsufficient);
assert(html.includes('STALE · 68d ago'), 'a 68-day-old quote must be labelled stale with its age');
assert(html.includes('INSUFFICIENT DATA'), 'missing inputs must show INSUFFICIENT DATA');
assert(!html.includes('>AVOID<'), 'no confident AVOID verdict on missing inputs');
assert(html.includes('<span class="risk-heat-value">n/a</span>'), 'risk heat is not a number without inputs');
assert(!html.includes('95%'), 'data gaps must not print a 95% risk heat');
assert(/data-k="edge"><span class="hl-na">n\/a<\/span>/.test(html), 'no edge grade without inputs'); // UI v10: edge is a labelled value, not an "EDGE N/A" badge
assert(/hot-score-main"[^>]*>—</.test(html), 'overall score is hidden when core inputs are missing');
assert(html.includes('Missing: price, trend, momentum, adx, earnings history'), 'confidence tooltip names the gaps');
assert(html.includes('Trend 1D/4H <span') && html.includes('STALE · 68d ago</span></span>'), 'stale trend snapshot is labelled');
assert(html.includes('>n/a</span>'), 'insider shows n/a when no provider is connected');
assert(!html.includes('>unknown</span>\n                        <span class="text-muted" style="font-weight: 600;">unknown'), 'no double unknown line');

// 2) Same card once the browser holds the Watchlist's live quote -> same price as the Watchlist.
html = renderCard(staleInsufficient, { NVDA: { price: 161.01, change_pct: -3.12, timestamp: isoAgo(30), source: 'yahoo', freshness: 'CACHED' } });
assert(html.includes('$161.01'), 'hot list must show the live quote the Watchlist shows');
assert(html.includes('-3.12%'), 'change comes from the same live quote');
assert(!html.includes('$225.25'), 'the stale engine price must be gone');
assert(!html.includes('hot-card-quote-age'), 'a fresh quote carries no stale chip');

// 3) Missing change is "N/A", not +0.00% (BMNR eToro mid quote has no change).
html = renderCard({ price: 17.56, change_pct: null, price_stale: false, price_as_of: isoAgo(600) });
assert(html.includes('<span class="hot-card-change text-muted">n/a</span>'), 'null change must render n/a'); // UI v10: missing values read "n/a"
assert(!html.includes('+0.00%'), 'null change must not be printed as +0.00%');
assert(html.includes('10m ago'), 'quote age is shown');

// 4) Zero / missing price is N/A, never "$0.00".
html = renderCard({ price: 0, change_pct: null });
assert(html.includes('<span class="hot-card-price">n/a</span>'), 'zero price renders n/a');

// 5) Risk heat counts real risk factors only (older payload without risk_factor_flags).
html = renderCard({
    price: 100, change_pct: 1, risk_level: 'med', earnings_play: 'WAIT', earnings_setup: 'C',
    earnings_risk_flags: ['momentum_unknown', 'adx_unknown', 'trend_conflict'],
});
assert(html.includes('<span class="risk-heat-value">46%</span>'), '20 + 14*1 real flag + 12 (med) = 46%');
assert(html.includes('Data gaps (momentum_unknown, adx_unknown) are not counted as risk'), 'tooltip explains the gaps');

// 6) Priority is floored at 0 on screen; a past-earnings card has no earnings play.
html = renderCard({
    price: 6.83, change_pct: -5.13, priority_score: -1.2, overall_score: 37.5, earnings_days: -12, earnings_date: '2026-11-03',
    earnings_play: 'N/A', earnings_setup: 'N/A', verdict_status: 'no_upcoming_earnings', missing_inputs: [], risk_factor_flags: [],
});
assert(/data-k="priority">0\.0</.test(html), 'negative priority must not be displayed'); // UI v10: priority is a labelled value in the Scores column
assert(!/data-k="priority">-/.test(html), 'no negative priority');
assert(html.includes('NO EARNINGS PLAY'), 'past earnings -> no earnings play');
assert(html.includes('Last earnings 12d ago'), 'past date is labelled as the last report'); // UI v10: sentence case

// 7) Top Risk can be a news headline (third-party text): it must be escaped.
html = renderCard({ price: 100, change_pct: 1, top_risk: '<img src=x onerror=alert(1)> lawsuit', risk_factor_flags: [], missing_inputs: [] });
assert(html.includes('&lt;img src=x onerror=alert(1)&gt; lawsuit'), 'top risk headline is escaped');
assert(!html.includes('<img src=x'), 'no raw HTML from a headline');

// 8) Earnings history shows its real sample size.
html = renderCard({ price: 100, change_pct: 1, earnings_history_summary: { sample_size: 1, last_8: { up_count: 1, down_count: 0, avg_gap_pct: 6.3 } } });
assert(html.includes('U1/D0 (n=1)'), 'a single sampled quarter is not presented as 8 quarters');

// 9) "Load Details" reflects the daily-bar metrics, not the retired FMP flag.
renderCard({ price: 100, change_pct: 1, _data_quality: { has_bar_metrics: true } });
assert(detailsBtn.innerHTML.includes('Details loaded'), 'button shows loaded when every card has bar metrics');
assert.equal(detailsBtn.disabled, false, 'button stays usable as a refresh');
renderCard({ price: 100, change_pct: 1, _data_quality: { has_bar_metrics: false } });
assert(detailsBtn.innerHTML.includes('Load details'), 'button offers loading while metrics are missing'); // UI v10: sentence case

/* ------------------------------------------------------- heat strip + dossier */
const dossierSource = fs.readFileSync(path.resolve(__dirname, '../../frontend/hotlist_dossier.js'), 'utf8');
const makeDossierContext = (fetchImpl) => {
    const nodes = {
        'hotlist-heat-strip': { innerHTML: '' },
        'hl-dossier-head': { innerHTML: '' },
        'hl-dossier-body': { innerHTML: '' },
        'hl-dossier-overlay': { classList: { remove() {}, add() {} }, addEventListener() {} },
    };
    const toasts = [];
    const ctx = {
        UI: { formatPrice: (v) => Number(v).toFixed(2), showToast: (msg, type) => toasts.push([msg, type]) },
        API: { calls: 0, fetchWithTimeout: async function (url, opts) { this.calls += 1; return fetchImpl(url, opts); } },
        document: { getElementById: (id) => nodes[id] || null, body: { classList: { add() {}, remove() {} }, appendChild() {} }, createElement: () => ({ addEventListener() {}, classList: { remove() {}, add() {} } }) },
        console: { warn() {}, error() {}, log() {} }, Date, Math, Number, String, Promise, encodeURIComponent,
    };
    vm.createContext(ctx);
    vm.runInContext(dossierSource, ctx);
    return { ctx, nodes, toasts };
};

(async () => {
    // 10) A failed heat load ends in an "unavailable" state and is NOT reset to the spinner by the next poll.
    let env = makeDossierContext(async () => { throw new Error('Request timed out after 45000ms'); });
    await env.ctx.UI.loadHotlistHeat();
    assert(env.nodes['hotlist-heat-strip'].innerHTML.includes('Market heat unavailable'), 'failure is shown');
    assert(env.nodes['hotlist-heat-strip'].innerHTML.includes('Request timed out after 45000ms'), 'with the reason');
    const callsAfterFailure = env.ctx.API.calls;
    await env.ctx.UI.loadHotlistHeat(); // what every state poll does
    assert.equal(env.ctx.API.calls, callsAfterFailure, 'no immediate re-fetch after a failure');
    assert(!env.nodes['hotlist-heat-strip'].innerHTML.includes('Loading market heat'), 'the spinner must not come back');
    await env.ctx.UI.loadHotlistHeat({ force: true }); // "Retry now" / Load Details
    assert.equal(env.ctx.API.calls, callsAfterFailure + 1, 'a forced retry does fetch');

    // 11) A failed server build (200 + error) is not rendered as "No heat right now".
    env = makeDossierContext(async () => ({ ok: true, status: 200, json: async () => ({ curated: [], heat: [], scanner_available: false, as_of: null, error: 'heat_unavailable' }) }));
    await env.ctx.UI.loadHotlistHeat();
    assert(env.nodes['hotlist-heat-strip'].innerHTML.includes('Market heat unavailable (heat_unavailable)'), 'server error is surfaced');
    assert(!env.nodes['hotlist-heat-strip'].innerHTML.includes('No heat right now'), 'not shown as an empty market');

    // 12) Success renders the chips and returns the data (Load Details awaits it).
    env = makeDossierContext(async () => ({ ok: true, status: 200, json: async () => ({ curated: [{ ticker: 'NVDA' }], heat: [{ ticker: 'MARA', change_pct: -2.28, categories: ['most_active'] }], scanner_available: true, as_of: new Date().toISOString() }) }));
    const heat = await env.ctx.UI.loadHotlistHeat();
    assert(heat && heat.heat.length === 1, 'loadHotlistHeat resolves with the payload');
    assert(env.nodes['hotlist-heat-strip'].innerHTML.includes('MARA'), 'chips rendered');

    // 13) Dossier: a past earnings date is the LAST report, never "in -12 days"; stale quote is labelled.
    env = makeDossierContext(async () => ({ ok: true, status: 200, json: async () => ({}) }));
    env.ctx.UI.renderTickerDossier({
        ticker: 'MARA', as_of: new Date().toISOString(), in_curated_hotlist: true,
        quote: { available: true, price: 11.81, change_pct: 4.42, price_stale: true, price_as_of: isoAgo(68 * 86400), trend_1d: 'BULL', technicals_stale: true, technicals_as_of: isoAgo(68 * 86400) },
        news: { available: false }, trades: { available: true, traded: false }, decisions: { available: false },
        deep_analysis: { available: false, error: 'bull_argument_unavailable', generated_at: '2026-09-04T22:37:28+00:00' },
        pre_earnings: { available: true, source: 'watchlist_state', earnings_date: '2026-11-03', earnings_when: '', days_until: -12, is_past: true },
    });
    const body = env.nodes['hl-dossier-body'].innerHTML;
    const head = env.nodes['hl-dossier-head'].innerHTML;
    assert(body.includes('Last earnings: <strong>2026-11-03</strong>') && body.includes('<strong>12 days ago</strong>'), 'past date labelled as last earnings');
    assert(!body.includes('in <strong>-12'), 'no negative countdown');
    assert(!body.includes('Next earnings'), 'not presented as the next earnings');
    assert(head.includes('STALE · 68d old'), 'dossier header labels the stale quote');
    assert(body.includes('trend snapshot is stale'), 'stale trend is called out');
    assert(body.includes('The last deep analysis did not complete') && body.includes('bull argument unavailable'), 'failed debate shows its reason');
    assert(!body.includes('No deep analysis yet'), 'a failed run is not shown as "never run"');

    // 14) Future date still reads as next earnings.
    env.ctx.UI.renderTickerDossier({
        ticker: 'NVDA', quote: { available: true, price: 161.01, change_pct: -3.12, price_stale: false, price_as_of: isoAgo(120) },
        news: {}, trades: {}, decisions: {}, deep_analysis: null,
        pre_earnings: { available: true, source: 'watchlist_state', earnings_date: '2026-11-17', earnings_when: 'AMC', days_until: 2, is_past: false },
    });
    assert(env.nodes['hl-dossier-body'].innerHTML.includes('Next earnings: <strong>2026-11-17</strong>'), 'future date is the next earnings');
    assert(env.nodes['hl-dossier-body'].innerHTML.includes('in <strong>2 days</strong>'), 'with a positive countdown');
    assert(!env.nodes['hl-dossier-head'].innerHTML.includes('STALE'), 'fresh quote has no stale chip');

    // 15) A failed dossier build is an error, not an "empty but verified" dossier.
    env = makeDossierContext(async () => ({ ok: true, status: 200, json: async () => ({ available: false, error: 'dossier_failed', ticker: 'MARA' }) }));
    await env.ctx.UI.openTickerDossier('MARA');
    assert(env.nodes['hl-dossier-body'].innerHTML.includes('Dossier unavailable: dossier failed'), 'error is shown');
    assert(!env.nodes['hl-dossier-body'].innerHTML.includes('We never traded this ticker'), 'no false "never traded" claim');

    // 16) A deep-analysis run that fails tells the user.
    env = makeDossierContext(async (url, opts) => (opts && opts.method === 'POST'
        ? { ok: true, status: 200, json: async () => ({ available: false, error: 'no_provider' }) }
        : { ok: true, status: 200, json: async () => ({ ticker: 'MARA', quote: {}, news: {}, trades: {}, decisions: {}, pre_earnings: {} }) }));
    await env.ctx.UI.runHotlistDeepAnalysis('MARA');
    assert(env.toasts.some(([msg, type]) => type === 'error' && msg.includes('did not complete') && msg.includes('no provider')), 'failed run raises a toast');

    console.log('Hot List tab audit: stale quote, live overlay, insufficient data, risk heat, priority floor, escaping, heat failure state, dossier earnings/quote/debate: PASS');
})().catch((e) => { console.error(e); process.exit(1); });
