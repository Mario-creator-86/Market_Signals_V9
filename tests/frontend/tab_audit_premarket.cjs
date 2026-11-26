// Tab audit 2026-11-15 — Premarket tab.
// On a Sunday the tab presented Friday's last prints as a live pre-market:
// "UPDATED 9:50:56 AM" on every card, "next run 300s" forever, an AI card
// titled "Pre-Market Pulse". These checks run the real premarket block of
// frontend/app.js against a closed-market payload and a live pre-market one.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync('frontend/app.js', 'utf8');
const start = source.indexOf('UI._premarketFormatSpan = function');
const end = source.indexOf('UI.preEarningsData = null;', start);
assert.ok(start > 0 && end > start, 'premarket block not found in frontend/app.js');

const elements = {};
const makeEl = () => ({
    innerHTML: '', textContent: '', style: {}, disabled: false,
    classList: { add() {}, remove() {}, toggle() { return false; } },
    querySelector: () => null, querySelectorAll: () => [],
    setAttribute() {}, focus() {},
});
const document = {
    getElementById: (id) => (elements[id] || (elements[id] = makeEl())),
    querySelector: () => null,
    querySelectorAll: () => [],
    activeElement: null,
};
const UI = {
    renderAgentStatusHeader() {},
    capScrollList() {},
    _toDateOrNull: (v) => (v ? new Date(v) : null),
};
const ViewRouter = { activeView: 'premarket' };
vm.runInNewContext(source.slice(start, end), { UI, document, ViewRouter, console, Date, Intl, Number, String, Math, Array });

const SUNDAY = Date.parse('2026-11-15T07:55:56Z');
const candidate = (symbol, direction, gap, long, short, extra = {}) => ({
    symbol, direction, gap_pct: gap, premarket_price: 79.17, prev_close: 75.15,
    premarket_score_long: long, premarket_score_short: short, score: 0, score_long: 0, score_short: 0,
    catalysts: ['NONE'], news: [], earnings: {}, ollama: null, why: `Gap ${gap}%`, freshness: 'warm', inputs_age: {},
    ...extra,
});
const book = [
    candidate('VEON', 'WATCH', 5.35, 43, 23),
    candidate('CIFR', 'WATCH', -3.06, 30, 42),
    candidate('NVDA', 'WATCH', -1.13, 31, 35, { catalysts: ['EARNINGS', 'NONE'] }),
];
const closedPayload = {
    as_of_utc: '2026-11-15T07:55:56Z',
    candidates: book, long_candidates: [], short_candidates: [], watch_only: book,
    reported_today: [],
    flags: { market_open: false, pulse_used: true, news_available: true, earnings_available: true },
    inputs_used: { news_cache_age_seconds: 15, earnings_cache_age_seconds: 279, movers_cache_age_seconds: 49 },
    status: { state: 'OK', last_run_time: '2026-11-15T07:55:57Z', next_run_in: 300, next_refresh_at_utc: '2026-11-15T13:55:56Z' },
    session: {
        state: 'CLOSED', closed_kind: 'weekend', label: 'Market closed (weekend)',
        data_kind: 'LAST_SESSION', data_session_date: '2026-11-13', quote_session_date: '2026-11-13',
        quotes_as_of_utc: '2026-11-14T01:00:00Z',
        next_premarket_open_utc: '2026-11-16T09:00:00Z', next_regular_open_utc: '2026-11-16T14:30:00Z',
    },
    pulse: {
        summary: 'Miners ended Friday lower.', sentiment: 'neutral', top_long: [{ symbol: 'VEON', reason: 'up move' }],
        top_short: [], risks: 'thin', model: 'glm', generated_at: '2026-11-15T07:52:02Z', kind: 'last_session_recap',
    },
    disabled: false,
};

// ── 1. Session view: a closed market is labelled as such ──
const closedView = UI._premarketSessionView(closedPayload, SUNDAY);
assert.equal(closedView.known, true);
assert.equal(closedView.live, false);
assert.equal(closedView.chip, 'Market closed (weekend)');
assert.equal(closedView.priceLabel, 'Last');
assert.equal(closedView.timeLabel, 'Session');
assert.equal(closedView.timeValue, 'Fri, Nov 13');           // exchange date, not shifted to the 12th
assert.equal(closedView.pulseTitle, 'Last session recap');
assert.match(closedView.banner, /US market is closed \(weekend\)/);
assert.match(closedView.banner, /no pre-market session right now/);
assert.match(closedView.banner, /last prints of the Fri, Nov 13 session \(1d 6h old\)/);
assert.match(closedView.banner, /Pre-market opens Mon, Nov 16, 4:00 AM ET/);

const liveView = UI._premarketSessionView({
    as_of_utc: '2026-11-16T10:00:00Z',
    session: { state: 'PRE_MARKET', data_kind: 'PRE_MARKET_LIVE', quotes_as_of_utc: '2026-11-16T09:59:20Z' },
}, Date.parse('2026-11-16T10:00:00Z'));
assert.equal(liveView.live, true);
assert.equal(liveView.priceLabel, 'Premkt');
assert.equal(liveView.timeLabel, 'Quote');
assert.equal(liveView.banner, '');
assert.equal(liveView.pulseTitle, 'Pre-market pulse');

const afterView = UI._premarketSessionView({
    session: { state: 'AFTER_HOURS', data_kind: 'AFTER_HOURS_LIVE', next_premarket_open_utc: '2026-11-17T09:00:00Z' },
}, Date.parse('2026-11-16T22:00:00Z'));
assert.equal(afterView.priceLabel, 'After-hrs');
assert.match(afterView.banner, /not pre-market gaps/);

// Clock already in pre-market but the data is still the closed-market recap.
const pendingView = UI._premarketSessionView({
    session: { state: 'PRE_MARKET', data_kind: 'LAST_SESSION', data_session_date: '2026-11-13' },
}, Date.parse('2026-11-16T09:01:00Z'));
assert.match(pendingView.banner, /first pre-market scan has not completed yet/);

// Payload without a session block (older state file): no invented claims,
// and the time cell is the scan time, labelled "Scanned" — not "Updated".
const legacyView = UI._premarketSessionView({ as_of_utc: '2026-11-15T07:55:56Z' }, SUNDAY);
assert.equal(legacyView.known, false);
assert.equal(legacyView.timeLabel, 'Scanned');
assert.equal(legacyView.banner, '');

// ── 2. Countdown is computed against the clock, not a frozen "300s" ──
assert.equal(UI._premarketNextRunLabel(closedPayload.status, SUNDAY), 'in 6h 00m');
assert.equal(UI._premarketNextRunLabel(closedPayload.status, SUNDAY + 3600 * 1000), 'in 5h 00m');
assert.equal(UI._premarketNextRunLabel(closedPayload.status, Date.parse('2026-11-15T14:00:00Z')), 'due now');
// Legacy state file: derive it from last_run_time + the saved interval.
const legacyStatus = { last_run_time: '2026-11-15T07:55:57Z', next_run_in: 300 };
assert.equal(UI._premarketNextRunLabel(legacyStatus, Date.parse('2026-11-15T07:57:57Z')), 'in 3m 00s');
assert.equal(UI._premarketNextRunLabel(legacyStatus, Date.parse('2026-11-15T08:30:00Z')), 'due now');
assert.equal(UI._premarketNextRunLabel({}, SUNDAY), 'n/a');
assert.equal(UI._premarketFormatSpan(59), '59s');
assert.equal(UI._premarketFormatSpan(90061), '1d 1h');

// ── 3. Direction filter, min-score filter and sorts work ──
UI.premarketFilters = { direction: 'all', sort: 'score', minScore: 0, search: '' };
assert.deepEqual(UI._filterPremarketCandidates(book).map((c) => c.symbol), ['VEON', 'CIFR', 'NVDA']);   // 43, 42, 35
UI.premarketFilters.minScore = 40;
assert.deepEqual(UI._filterPremarketCandidates(book).map((c) => c.symbol), ['VEON', 'CIFR']);
UI.premarketFilters.minScore = 44;
assert.deepEqual(UI._filterPremarketCandidates(book), []);
UI.premarketFilters = { direction: 'LONG', sort: 'score', minScore: 0, search: '' };
assert.deepEqual(UI._filterPremarketCandidates(book), []);                                              // all WATCH
UI.premarketFilters = { direction: 'WATCH', sort: 'gap', minScore: 0, search: '' };
assert.deepEqual(UI._filterPremarketCandidates(book).map((c) => c.symbol), ['VEON', 'CIFR', 'NVDA']);   // |gap| desc
UI.premarketFilters = { direction: 'all', sort: 'symbol', minScore: 0, search: 'v' };
assert.deepEqual(UI._filterPremarketCandidates(book).map((c) => c.symbol), ['NVDA', 'VEON']);
UI.premarketFilters = { direction: 'all', sort: 'score', minScore: 0, search: '' };

// ── 4. Full render of the closed-market payload ──
UI.premarketViewMode = 'split';
UI.premarketData = closedPayload;
UI.renderPremarketScanner(closedPayload);
const meta = elements['premarket-meta'].innerHTML;
const cards = elements['premarket-sections'].innerHTML;
const alertEl = elements['premarket-status-alert'];
const pulseHtml = elements['premarket-pulse'].innerHTML;
const details = elements['premarket-details'].innerHTML;

assert.match(meta, /Market closed \(weekend\)/);
assert.match(meta, /Scanned <b>/);
assert.match(meta, /next refresh <b>(in \d|due now)/);
assert.doesNotMatch(meta, /next run <b>300s/);
assert.match(meta, /Prices <b>last session \(Fri, Nov 13\)/);

assert.equal(alertEl.style.display, 'block');
assert.match(alertEl.innerHTML, /no pre-market session right now/);

// UI v10: the default (split) layout is one table grouped by direction.
assert.match(cards, /<table class="pm-table">/);
assert.doesNotMatch(cards, />Updated</);
assert.doesNotMatch(cards, />Premkt</);
assert.match(cards, /<th[^>]*>Last<\/th>/);
assert.match(cards, /<th[^>]*>Ref close<\/th>/);
assert.match(cards, /<th[^>]*>Session<\/th>/);
assert.match(cards, /<td class="num">79\.1700<\/td>/);
assert.match(cards, /<td class="num pm-col-ref">75\.1500<\/td>/);
assert.match(cards, /<td class="num pm-col-time">Fri, Nov 13<\/td>/);
assert.doesNotMatch(cards, /premarket-catalyst-chip">NONE</);
// Empty groups are one quiet line, not a box; all three groups are listed.
assert.match(cards, /No long candidates above the threshold/);
assert.match(cards, /No short candidates above the threshold/);
assert.match(cards, /pm-group-name">Watch<\/span><span class="pm-group-count">3</);

// Cards layout keeps the same honest labels.
UI.premarketViewMode = 'cards';
UI.renderPremarketScanner(closedPayload);
const cardWall = elements['premarket-sections'].innerHTML;
assert.doesNotMatch(cardWall, /<span>Updated<\/span>/);
assert.doesNotMatch(cardWall, /<span>Premkt<\/span>/);
assert.match(cardWall, /<span>Last<\/span><b>79\.1700<\/b>/);
assert.match(cardWall, /<span>Ref close<\/span><b>75\.1500<\/b>/);
assert.match(cardWall, /<span>Session<\/span><b>Fri, Nov 13<\/b>/);
assert.doesNotMatch(cardWall, /premarket-catalyst-chip">NONE</);
UI.premarketViewMode = 'split';

assert.match(pulseHtml, /Last session recap/);
assert.doesNotMatch(pulseHtml, /Pre-market pulse/);
assert.match(pulseHtml, /written /);
assert.doesNotMatch(pulseHtml, /different session/);

// Details panel: same score as the card (43), not the legacy "+0";
// "NONE" is not shown as a catalyst; price is labelled for what it is.
assert.match(details, /43\/100/);
assert.doesNotMatch(details, />\+0</);
assert.doesNotMatch(details, /premarket-catalyst-chip">NONE</);
assert.match(details, />Last price</);
assert.doesNotMatch(details, />Premarket</);

// A pulse written as a live pre-market read, shown over closed-market data,
// is flagged instead of being presented as current.
UI.renderPremarketScanner({ ...closedPayload, pulse: { ...closedPayload.pulse, kind: undefined } });
assert.match(elements['premarket-pulse'].innerHTML, /Pre-market pulse/);
assert.match(elements['premarket-pulse'].innerHTML, /written as a Pre-market pulse for a different session/);

// Stream view uses the same honest column names.
UI.premarketViewMode = 'stream';
UI.renderPremarketScanner(closedPayload);
const stream = elements['premarket-sections'].innerHTML;
assert.match(stream, /<span>Last<\/span><span>Ref close<\/span>/);
assert.doesNotMatch(stream, /<span>Premkt<\/span>/);
UI.premarketViewMode = 'split';

// ── 5. Live pre-market render keeps the pre-market wording ──
const livePayload = {
    ...closedPayload,
    as_of_utc: '2026-11-16T10:00:00Z',
    status: { state: 'OK', last_run_time: '2026-11-16T10:00:00Z', next_refresh_at_utc: '2026-11-16T10:05:00Z' },
    session: { state: 'PRE_MARKET', data_kind: 'PRE_MARKET_LIVE', quotes_as_of_utc: '2026-11-16T09:59:20Z' },
    pulse: { ...closedPayload.pulse, kind: 'premarket' },
};
UI.premarketData = livePayload;
UI.renderPremarketScanner(livePayload);
assert.match(elements['premarket-sections'].innerHTML, /<th[^>]*>Premkt<\/th>/);
assert.match(elements['premarket-sections'].innerHTML, /<th[^>]*>Quote<\/th>/);
assert.match(elements['premarket-sections'].innerHTML, /<th[^>]*>Gap %<\/th>/);
assert.equal(elements['premarket-status-alert'].style.display, 'none');
assert.match(elements['premarket-pulse'].innerHTML, /Pre-market pulse/);
assert.match(elements['premarket-meta'].innerHTML, /Pre-market session/);

// ── 6. Third-party text is escaped ──
const hostile = {
    ...livePayload,
    watch_only: [candidate('VEON', 'WATCH', 5.35, 43, 23, {
        news: [{ title: '<img src=x onerror=alert(1)>', source: 'feed' }], catalysts: ['<b>X</b>'],
    })],
    pulse: { ...livePayload.pulse, summary: '<script>alert(1)</script>' },
};
hostile.candidates = hostile.watch_only;
UI.premarketData = hostile;
UI.renderPremarketScanner(hostile);
for (const id of ['premarket-sections', 'premarket-pulse', 'premarket-details']) {
    assert.doesNotMatch(elements[id].innerHTML, /<img src=x|<script>|<b>X<\/b>/, `${id} must escape third-party text`);
}

console.log('Premarket tab audit checks passed');
