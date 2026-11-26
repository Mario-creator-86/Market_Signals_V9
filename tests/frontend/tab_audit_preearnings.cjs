// Tab audit 2026-11-15 - Pre-Earnings tab.
// Runs the real Pre-Earnings renderers of frontend/app.js against the payload
// shapes seen in the running app that day:
//   - "Staged calendar coverage: 409/670 reporters (10%), nearest first through
//     28 Oct" (two different snapshots mixed in one sentence, date in the past);
//   - a 24-day-old snapshot shown as a dead "paused" tab while a refresh cycle
//     was in fact running;
//   - an October Pulse narrative with nothing saying how old it was;
//   - a 54.0% hit rate with no naive baseline next to it, a +859% price-data
//     error scored as a MISS, and an LLM summary calling it "a positive edge".
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync('frontend/app.js', 'utf8');
const slice = (startMarker, endMarker) => {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start + 1);
    assert.ok(start > 0 && end > start, `block not found: ${startMarker}`);
    return source.slice(start, end);
};

const NOW = Date.parse('2026-11-15T07:54:00Z');
class FixedDate extends Date {
    constructor(...args) { if (args.length) super(...args); else super(NOW); }
    static now() { return NOW; }
}

const elements = {};
const makeEl = (id) => ({
    id, innerHTML: '', textContent: '', value: '', style: {}, disabled: false, scrollTop: 0, className: '',
    classList: { add() {}, remove() {}, toggle() { return false; }, contains() { return false; } },
    querySelector: () => null, querySelectorAll: () => [],
    setAttribute() {}, getAttribute() { return null; }, addEventListener() {},
});
const document = {
    getElementById: (id) => (elements[id] || (elements[id] = makeEl(id))),
    querySelector: () => null,
    querySelectorAll: () => [],
};
const escapeHtml = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
const UI = {
    _escapeHtml: escapeHtml,
    preEarningsData: { items: [] },
    capScrollList() {},
    selectPreEarningsRow() {},
    _preEarningsLearningReport: null,
};
const API = { _warnNetworkOnce() {}, getPreEarningsLearningReport: async () => null };
const context = {
    UI, API, document, console, Date: FixedDate, Intl, Number, String, Math, Array, Object, Set, Map, JSON,
    Actions: {}, ViewRouter: { activeView: 'preearnings' }, setTimeout, clearTimeout,
};
vm.createContext(context);
vm.runInContext(slice('function _peHitRateBadge(rate) {', 'UI.togglePreEarningsHistoryDays = function'), context);
vm.runInContext(slice('UI._preEarningsBiasScore = function', 'UI.startPreEarningsRefreshPoll = function'), context);
vm.runInContext(slice('UI._preEarningsStructuredScore = function', 'UI.applyCapPreset = function'), context);
vm.runInContext(slice('UI._renderPreEarningsTop10 = function', 'UI.renderPreEarnings = function'), context);

const esc = escapeHtml;
const top10 = () => elements['preearnings-top10'].innerHTML;

// ---------------------------------------------------------------- Top 10 board
// 1. Stale snapshot after a restart, refresh cycle in flight.
const staleRanking = {
    state: 'STALE', snapshot_age_seconds: 572.2 * 3600,
    universe_event_count: 4090, scored_count: 400, screened_out_count: 9, covered_count: 409,
    calendar_coverage_pct: 10.0, coverage_frontier_event_date: '2026-10-28',
    news_articles_scanned: 0, ai_reviewed_count: 0, min_trader_score: 60,
};
UI._renderPreEarningsTop10({
    ranking: staleRanking,
    flags: { recommendations_stale: true },
    calendar_universe: { reporter_count: 670 },
    top_predictions: [],
    status: { cycle_running: true, cycle_started_at: '2026-11-15T07:51:10Z' },
}, esc);
let html = top10();
assert.ok(!html.includes('409/670'), 'stale covered count must not be divided by the live calendar count');
assert.ok(html.includes('409/4090 reporters (10%)'), 'covered / universe / percentage come from one snapshot');
assert.ok(html.includes('Last completed snapshot (572.2h old)'), 'a stale coverage line is labelled as the last snapshot');
assert.ok(!html.includes('Staged calendar coverage'), 'stale figures are not presented as current coverage');
assert.ok(html.includes('Refreshing') && html.includes('running for 3 min'), 'a cycle in flight is reported with its age');
assert.ok(!html.includes('Recommendations paused'), 'a refreshing tab is not shown as paused');
assert.ok(html.includes('deep scored · last snapshot'), 'stale counters are tagged as last-snapshot values');

// 2. Stale and nothing running: paused, with the failure reason when there is one.
UI._renderPreEarningsTop10({
    ranking: staleRanking, flags: { recommendations_stale: true }, calendar_universe: { reporter_count: 670 },
    top_predictions: [], status: { cycle_running: false, last_error: 'calendar <b>down</b>' },
}, esc);
html = top10();
assert.ok(html.includes('Recommendations paused — snapshot is stale (572.2h).'));
assert.ok(html.includes('The last cycle failed: calendar &lt;b&gt;down&lt;/b&gt;'), 'the error text is shown, escaped');

// 3. A "running" marker left behind by a dead process (older than 30 min) is ignored.
UI._renderPreEarningsTop10({
    ranking: staleRanking, flags: { recommendations_stale: true }, calendar_universe: { reporter_count: 670 },
    top_predictions: [], status: { cycle_running: true, cycle_started_at: '2026-11-12T07:02:00Z' },
}, esc);
assert.ok(top10().includes('Recommendations paused'), 'a three-day-old running marker is not a running cycle');

// 4. Fresh snapshot: the arithmetic is the one printed.
UI._renderPreEarningsTop10({
    ranking: {
        state: 'FRESH', snapshot_age_seconds: 64, universe_event_count: 670, scored_count: 400,
        screened_out_count: 2, covered_count: 402, calendar_coverage_pct: 60.0,
        coverage_frontier_event_date: '2026-12-01', news_articles_scanned: 135, ai_reviewed_count: 6,
    },
    flags: { recommendations_stale: false }, calendar_universe: { reporter_count: 670 }, top_predictions: [], status: {},
}, esc);
html = top10();
assert.ok(html.includes('Staged calendar coverage: 402/670 reporters (60%), nearest first through 1 Dec.'));
assert.ok(!html.includes('last snapshot') && !html.includes('has passed'));
assert.ok(html.includes('<strong>6</strong><span>AI reviewed</span>'));

// ----------------------------------------------------------------------- Pulse
const oldPulse = {
    summary: 'Single-name slate: only AAL reports BMO tomorrow.', sentiment: 'bullish',
    catalysts: 'AAL pre-open print.', risks: 'One airline.', model: 'glm', persisted: true,
    generated_at: '2026-10-22T11:21:18Z',
    current_top_long: [{ symbol: 'NVDA', reason: 'still upcoming' }], current_top_short: [],
    history: [{ snapshot_id: 1094, generated_at: '2026-10-22T11:21:18Z', sentiment: 'bullish', summary: 'old', top_long: [], top_short: [] }],
    history_meta: { snapshot_count: 1094 },
};
UI._renderPreEarningsPulse({ pulse: oldPulse }, esc);
html = elements['preearnings-pulse'].innerHTML;
assert.ok(html.includes('generated 24 days ago'), 'the live Pulse card states its age');
assert.ok(!html.includes('only AAL reports BMO tomorrow'), 'a 24-day-old narrative is not shown as the current read');
assert.ok(!html.includes('AAL pre-open print.') && !html.includes('One airline.'), 'old catalysts/risk are withheld too');
assert.ok(html.includes('not shown as current'));
assert.ok(html.includes('Latest saved · 24 days ago'), 'the newest history entry carries its age');
assert.ok(html.includes('NVDA'), 'validated current picks stay visible');

const freshPulse = { ...oldPulse, generated_at: '2026-11-15T07:20:00Z' };
UI._renderPreEarningsPulse({ pulse: freshPulse }, esc);
html = elements['preearnings-pulse'].innerHTML;
assert.ok(html.includes('only AAL reports BMO tomorrow') && html.includes('generated 34 min ago'));

UI._renderPreEarningsPulse({ pulse: { ...oldPulse, current_top_long: [] } }, esc);
html = elements['preearnings-pulse'].innerHTML;
assert.ok(html.includes('last one generated 24 days ago'), 'the empty state says how old the saved Pulse is');

// ---------------------------------------------------------------- Track record
const band = (label) => ({ band: label, n: 0, hits: 0, hit_rate: null, avg_realized_move: null });
const trackRecord = {
    sample_size: 3622, ai_coverage: 1455,
    first_event_utc: '2026-02-26T09:00:00+00:00', last_event_utc: '2026-10-22T00:00:00+00:00',
    excluded_implausible_moves: 1, max_plausible_move_pct: 100,
    overall: { total: 2169, hits: 1171, hit_rate: 54.0, avg_realized_move: 4.05, always_long_hit_rate: 56.2, edge_vs_always_long_pct: -2.2 },
    per_direction: {
        LONG: { n: 1747, hits: 974, hit_rate: 55.8, avg_realized_move: 3.7, directional: true },
        SHORT: { n: 422, hits: 197, hit_rate: 46.7, avg_realized_move: 5.5, directional: true },
        NEUTRAL: { n: 1332, hits: 0, hit_rate: null, avg_realized_move: 5.05, directional: false },
    },
    per_ai_verdict: {}, per_ai_band: ['0-50', '50-70', '70-85', '85-100'].map(band),
    top_predictions: { total: 47, settled: 48, unscored: 1, hits: 25, hit_rate: 53.2, avg_realized_move: 6.9, pending: 0, per_side: {} },
    top_predictions_history: [
        { symbol: 'CLGN', event_day: '2026-09-04', direction: 'SHORT', trader_score: 61, realized_signed_move_pct: 859.4203, hit: null, move_implausible: true, status: 'SETTLED' },
        { symbol: 'SNOW', event_day: '2026-09-02', direction: 'LONG', trader_score: 70, realized_signed_move_pct: 23.2786, hit: true, move_implausible: false, status: 'SETTLED' },
    ],
    top_predictions_pending: [],
    ai_summary: {
        summary: 'Directional calls show a modest positive edge <img src=x onerror=1>.', recommendation: 'Tighten SHORTs.', model: 'glm',
        caveat: 'Not an edge: the 54.0% directional hit rate does not beat always-LONG (56.2% of these 2169 settled events moved up).',
    },
    outcomes_recent: [
        { symbol: 'CLGN', event_utc: '2026-09-04T00:00:00+00:00', event_day: '2026-09-04', predicted_direction_bias: -23, label: 'SHORT', realized_signed_move_pct: 859.4203, hit: null, move_implausible: true },
    ],
    pulse: { overall: {}, per_side: {}, recent: [] },
};
UI.renderPreEarningsTrackRecord(trackRecord);
html = elements['pe-trackrecord-body'].innerHTML;
const sampleText = elements['pe-tr-samples'].textContent;
assert.ok(sampleText.includes('3622 settled · 1455 with AI verdict'));
assert.ok(sampleText.includes('events 26 Feb – 22 Oct'), 'the header says which events the sample covers');
assert.ok(html.includes('always-LONG baseline 56.2% · not beaten'), 'the overall hit rate is shown next to its naive baseline');
assert.ok(html.includes('1 settled outcome with a one-day move beyond ±100% is treated as price-data error'));
assert.ok(html.includes('DATA ERROR · excluded'), 'an implausible Top 10 move is not scored as a miss');
assert.ok(html.includes('Reality check (computed, not AI)') && html.includes('Not an edge: the 54.0%'));
assert.ok(!html.includes('<img src=x'), 'LLM text is escaped');

console.log('Pre-Earnings tab audit: Top 10 coverage/stale/refreshing, Pulse age, track-record honesty: PASS');
