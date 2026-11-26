// Tab audit 2026-11-15 — Calendar + Alerts tabs (frontend/app.js).
// Runs the real functions sliced out of app.js against a minimal fake DOM.
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(require('path').resolve(__dirname, '../../frontend/app.js'), 'utf8');

const slice = (startMarker, endMarker) => {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start + startMarker.length);
    assert(start >= 0 && end > start, `markers not found: ${startMarker} … ${endMarker}`);
    return source.slice(start, end);
};
const escapeHtml = (value) => String(value ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
const pad = (n) => String(n).padStart(2, '0');
const key = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

// ---------------------------------------------------------------- fake DOM
function makeEl(tag) {
    const el = {
        tag, id: '', className: '', innerHTML: '', textContent: '', title: '', children: [], parentElement: null,
        style: { cssText: '', setProperty(name, value, prio) { this[name] = `${value}${prio ? ' !' + prio : ''}`; } },
        classList: {
            add(c) { el.className = `${el.className} ${c}`.trim(); },
            remove(c) { el.className = el.className.split(' ').filter(x => x !== c).join(' '); },
            contains(c) { return el.className.split(' ').includes(c); },
        },
        appendChild(child) { child.parentElement = el; el.children.push(child); return child; },
        insertBefore(child, ref) { child.parentElement = el; el.children.splice(Math.max(0, el.children.indexOf(ref)), 0, child); return child; },
        remove() {},
    };
    // assigning innerHTML drops the children, like a browser does
    let html = '';
    Object.defineProperty(el, 'innerHTML', {
        get() { return html; },
        set(v) { html = String(v); el.children = []; },
    });
    return el;
}
const deepHtml = (el) => el.innerHTML + el.children.map(deepHtml).join('');
const allCells = (el, out = []) => {
    if (String(el.className).includes('calendar-day-cell')) out.push(el);
    el.children.forEach(c => allCells(c, out));
    return out;
};

function calendarContext(fetchImpl) {
    const page = makeEl('div');
    const card = makeEl('div');
    const wrapper = makeEl('div');
    page.appendChild(card); card.appendChild(wrapper);
    const header = makeEl('span');
    const byId = { 'calendar-months-wrapper': wrapper, 'cal-current-month': header };
    const context = {
        Actions: {
            _ensureCalendarCachesLoaded() {}, getWatchlistSignature() { return ''; },
            _persistCalendarCaches() {}, _watchlistSig: '', calendarRangeCache: {},
        },
        UI: { _escapeHtml: escapeHtml },
        IS_DEV: false,
        document: {
            getElementById(id) {
                if (byId[id]) return byId[id];
                const found = page.children.find(c => c.id === id);
                return found || null;
            },
            createElement: makeEl,
            querySelector() { return null; },
        },
        fetch: fetchImpl,
        console: { log() {}, warn() {}, error() {} },
        localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
        window: { location: { origin: 'http://localhost' } },
        URL,
    };
    vm.createContext(context);
    vm.runInContext(slice('Actions.calendarUtil = {', 'Actions.openEventModal = function'), context);
    return { context, wrapper, header, page };
}

(async () => {
    // ------------------------------------------------ pure helper behaviour
    {
        const { context } = calendarContext(async () => ({ ok: true, json: async () => ({}) }));
        const U = context.Actions.calendarUtil;

        // "November - January 2026" was shown for Nov 2026 → Jan 2027
        assert.equal(U.rangeTitle([new Date(2026, 10, 1), new Date(2026, 11, 1), new Date(2027, 0, 1)]),
            'November 2026 – January 2027');
        assert.equal(U.rangeTitle([new Date(2026, 2, 1), new Date(2026, 3, 1), new Date(2026, 4, 1)]), 'March – May 2026');

        // day comes from the provider's own date, not from a UTC shift
        assert.equal(U.dayKey({ date: '2026-12-08T00:00:00+00:00', date_formatted: '2026-12-08' }), '2026-12-08');
        assert.equal(U.dayKey({ date: '2026-11-06T13:30:00+00:00', date_formatted: '2026-11-06 08:30 ET' }), '2026-11-06');
        assert.equal(U.dayKey({}), '');
        assert.equal(U.parseDay('2026-11-05').getDate(), 5);

        // watchlist → high-impact macro → earnings by market cap
        const day = [
            { type: 'earnings', symbol: 'TINY', market_cap: 5e6, impact: 'high' },
            { type: 'economic', title: 'US PPI', impact: 'medium' },
            { type: 'earnings', symbol: 'NOCAP', impact: 'high' },
            { type: 'earnings', symbol: 'MEGA', market_cap: 3e12, impact: 'high' },
            { type: 'economic', title: 'US CPI', impact: 'high' },
            { type: 'earnings', symbol: 'MINE', market_cap: 1e6, impact: 'high', is_watchlist: true },
            { type: 'ipo', title: 'IPO: X' },
        ].sort(U.compareEvents).map(e => e.symbol || e.title);
        assert.deepEqual(day, ['MINE', 'US CPI', 'MEGA', 'TINY', 'NOCAP', 'US PPI', 'IPO: X']);

        // session + how far it can be trusted
        assert.equal(U.sessionText({ when: 'amc' }), 'After market close');
        assert.equal(U.sessionText({ when: 'unknown' }), 'Time not announced');
        assert.equal(U.sessionConfidenceText({ when: 'amc', session_confidence: 'cross_provider' }), 'two providers agree');
        assert.equal(U.sessionConfidenceText({ when: 'bmo', session_confidence: 'provider' }), 'single provider');
        assert.equal(U.sessionConfidenceText({ when: 'bmo', session_confidence: 'inferred' }), 'inferred, not announced');
        assert.equal(U.sessionConfidenceText({ when: 'amc' }), 'unverified');
        assert.equal(U.sessionConfidenceText({ when: 'unknown', session_confidence: 'provider' }), '');
        assert.equal(U.dateConfidenceText({ date_confidence: 'estimated' }), 'Estimated date — not confirmed by the company');
        assert.equal(U.sourceText({ source: 'nasdaq_zacks_estimate' }), 'Nasdaq / Zacks estimate');

        // only published macro times are clock times
        assert.equal(U.hasClockTime({ type: 'economic', date: '2026-11-06T13:30:00+00:00', time_precision: 'published_time' }), true);
        assert.equal(U.hasClockTime({ type: 'economic', date: '2026-12-09T00:00:00', time_precision: 'date_only' }), false);
        assert.equal(U.hasClockTime({ type: 'earnings', date: '2026-11-17T00:00:00+00:00' }), false);
        assert.match(U.timeText({ type: 'economic', date: '2026-11-06T13:30:00+00:00', date_formatted: '2026-11-06 08:30 ET' }), /^\d{2}:\d{2} \(08:30 ET\)$/);
        assert.equal(U.marketCapText(3.2e12), '$3.20T');
        assert.equal(U.marketCapText(null), '');

        // coverage notes: stale list, short earnings window, missing macro year, no IPO feed
        const notes = U.coverageNotes({
            earnings: { fetched_at: '2026-11-15T07:51:18Z', age_seconds: 9000, stale: true, dates_are_estimates: true,
                window_end: '2026-12-18', last_error: 'no earnings provider returned data' },
            macro: { years_missing: [2027] },
            ipo: { available: false },
        }, '2026-11-01', '2027-02-06', 0).map(n => `${n.level}:${n.text}`).join('\n');
        assert.match(notes, /warn:Earnings list is stale — last refreshed 3 h ago/);
        assert.match(notes, /warn:Last earnings refresh failed: no earnings provider returned data/);
        assert.match(notes, /warn:Earnings are only listed through Dec 18, 2026/);
        assert.match(notes, /warn:Not configured: US macro release schedule for 2027/); // wave 3: one wording for a missing source
        assert.match(notes, /info:Not configured: IPO data provider\. IPO listings are unavailable/);
        assert.match(U.coverageNotes(null).map(n => n.text).join(), /unknown/);
        const fresh = U.coverageNotes({ earnings: { fetched_at: 'x', age_seconds: 120, stale: false, window_end: '2027-03-01' },
            macro: { years_missing: [] }, ipo: { available: true } }, '2026-11-01', '2027-02-06', 0);
        assert.equal(fresh.length, 1);
        assert.equal(fresh[0].level, 'info');
    }

    // ----------------------------------------------------- 3-month grid
    {
        const now = new Date();
        const firstOfNext = new Date(now.getFullYear(), now.getMonth() + 1, 1);
        const todayKey = key(now);
        const payload = {
            economic: [{ id: 'macro_x', title: 'US CPI (Consumer Price Index)', impact: 'high',
                date: `${todayKey}T13:30:00+00:00`, date_formatted: `${todayKey} 08:30 ET`, time_precision: 'published_time' }],
            earnings: [
                { id: 'earn_NEXT1', symbol: 'NEXT1', date: `${key(firstOfNext)}T00:00:00`, date_formatted: key(firstOfNext), market_cap: 1e9, when: 'amc' },
                { id: 'earn_small', symbol: 'SMALL', date: `${todayKey}T00:00:00`, date_formatted: todayKey, market_cap: 2e6, when: 'unknown' },
                { id: 'earn_big', symbol: 'BIGCO', date: `${todayKey}T00:00:00`, date_formatted: todayKey, market_cap: 2e12, when: 'bmo' },
                { id: "earn_x');alert(1);//", symbol: '<img src=x onerror=1>', date: `${todayKey}T00:00:00`, date_formatted: todayKey, is_watchlist: true },
                { id: 'earn_nodate', symbol: 'NODATE' },
            ],
            ipo: [],
            watchlist_tickers: [],
            coverage: { generated_at: new Date().toISOString(),
                earnings: { fetched_at: new Date().toISOString(), age_seconds: 60, stale: false, window_end: '2000-01-01' },
                macro: { years_missing: [] }, ipo: { available: false } },
        };
        let calls = 0;
        const { context, wrapper, header, page } = calendarContext(async () => { calls++; return { ok: true, json: async () => payload }; });
        await context.Actions.renderFullCalendar(now);

        const months = [0, 1, 2].map(i => new Date(now.getFullYear(), now.getMonth() + i, 1));
        assert.equal(header.textContent, context.Actions.calendarUtil.rangeTitle(months));
        assert.equal(wrapper.children.length, 3, 'three month sections');

        const cells = allCells(wrapper);
        const html = deepHtml(wrapper);
        // overflow days of a neighbouring month carry no events → nothing is listed twice
        const otherMonth = cells.filter(c => c.className.includes('other-month'));
        assert(otherMonth.length > 0, 'grid has leading/trailing days');
        otherMonth.forEach(c => assert(!c.innerHTML.includes('cal-event-pill'), 'other-month cell must be empty'));
        // (each pill carries exactly one "SYMBOL · session" tooltip)
        assert.equal((html.match(/NEXT1 ·/g) || []).length, 1, 'an event on the 1st of next month is printed once');

        // today is clearly marked, and visible despite the white !important stylesheet rule
        const todayCells = cells.filter(c => c.className.split(' ').includes('today'));
        assert.equal(todayCells.length, 1);
        assert(todayCells[0].innerHTML.includes('TODAY'));
        assert(String(todayCells[0].style.background).includes('!important'));
        if (now.getDate() > 1) {
            assert(cells.some(c => c.className.split(' ').includes('past')), 'past days are marked');
        }

        // order inside the day: watchlist, high-impact macro, then largest market cap
        const todayHtml = todayCells[0].innerHTML;
        const order = ['onerror', 'US CPI', 'BIGCO', 'SMALL'].map(t => todayHtml.indexOf(t));
        assert(order.every(i => i >= 0), 'all four events shown');
        assert.deepEqual([...order].sort((a, b) => a - b), order, 'watchlist → macro → big cap → small cap');

        // provider text is escaped, and cannot break out of the inline onclick
        assert(!html.includes('<img'), 'symbol must be escaped');
        assert(!html.includes("');alert(1)"), 'event id must not terminate the onclick string');
        assert(html.includes('&lt;img src=x onerror=1&gt;'));

        // coverage strip is created and names what is missing
        const note = page.children.find(c => c.id === 'calendar-status-note');
        assert(note, 'status note inserted above the grid');
        assert.match(note.innerHTML, /Earnings are only listed through/);
        assert.match(note.innerHTML, /IPO listings are unavailable/);

        // a second render within the TTL uses the cache; once it expires, a
        // failed refetch keeps the old copy on screen but says so
        await context.Actions.renderFullCalendar(now);
        assert.equal(calls, 1, 'fresh cache entry is reused');
        Object.keys(context.Actions._calendarRangeMemTs).forEach(k => { context.Actions._calendarRangeMemTs[k] = Date.now() - 3600 * 1000; });
        context.fetch = async () => { calls++; return { ok: false, status: 503, json: async () => ({}) }; };
        await context.Actions.renderFullCalendar(now);
        assert.equal(calls, 2, 'expired cache entry triggers a refetch');
        assert.match(page.children.find(c => c.id === 'calendar-status-note').innerHTML, /Could not refresh the calendar \(server answered 503\)/);
        assert(deepHtml(wrapper).includes('BIGCO'), 'older copy stays visible');
    }

    // --------------------------------------------- grid: load failure state
    {
        const { context, wrapper } = calendarContext(async () => ({ ok: false, status: 500, json: async () => ({}) }));
        await context.Actions.renderFullCalendar(new Date());
        assert.match(wrapper.innerHTML, /Calendar could not be loaded/);
        assert.match(wrapper.innerHTML, /server answered 500/);
        assert.match(wrapper.innerHTML, /Retry/);
    }

    // ------------------------------------------- 3-month jump never skips
    {
        const { context } = calendarContext(async () => ({ ok: true, json: async () => ({}) }));
        const A = context.Actions;
        A.initCalendarPage = () => {};
        vm.runInContext(slice('Actions.calendarNextMonth = function', 'Actions.calendarGoToday = function'), context);
        A.calendarState.currentDate = new Date(2026, 7, 31); // 31 Aug + 3 months used to land on 1 Dec
        A.calendarNextMonth();
        assert.equal(A.calendarState.currentDate.getMonth(), 10);
        A.calendarPrevMonth();
        assert.equal(A.calendarState.currentDate.getMonth(), 7);
    }

    // ------------------------------------------------- day list (modal)
    {
        const content = makeEl('div');
        const context = {
            Actions: {}, UI: { _escapeHtml: escapeHtml }, window: {},
            document: { getElementById: (id) => (id === 'calendar-events-list' ? content : null) },
            console: { log() {}, warn() {}, error() {} },
        };
        vm.createContext(context);
        vm.runInContext(slice('Actions.calendarUtil = {', 'Actions.calendarState = {'), context);
        vm.runInContext(slice('Actions.renderCalendar = function', 'UI._globalAiProviders = []'), context);

        const data = {
            economic: [
                { title: 'US Jobs Report <b>', date: '2026-12-04T13:30:00+00:00', date_formatted: '2026-12-04 08:30 ET', impact: 'high', category: 'economic', time_precision: 'published_time' },
                { title: 'Fed Interest Rate Decision (FOMC)', date: '2026-12-04T00:00:00', date_formatted: '2026-12-04', impact: 'high', category: 'fed', time_precision: 'date_only' },
            ],
            earnings: [
                { symbol: 'NVDA', date: '2026-12-04T00:00:00+00:00', date_formatted: '2026-12-04', when: 'amc', session_confidence: 'cross_provider', source: 'yfinance+calendar' },
                { symbol: 'ZZZ', title: 'ZZZ Earnings', company_name: 'Zed <i>Corp</i>', date: '2026-12-04T00:00:00', date_formatted: '2026-12-04', when: 'unknown', eps_estimated: 0, source: 'nasdaq_zacks_estimate', date_confidence: 'estimated', market_cap: 4e9 },
            ],
            ipo: [],
            watchlist_tickers: ['NVDA'],
            coverage: { ipo: { available: false } },
        };
        context.Actions.renderCalendar(data, 'all');
        let html = content.innerHTML;
        assert(!html.includes('00:00'), 'date-only rows must not show a 00:00 time');
        assert(!html.includes('undefined'), 'rows without a title must not print "undefined"');
        assert(html.includes('NVDA Earnings'));
        assert(html.includes('>AMC<') && html.includes('>TBA<') && html.includes('All day'));
        assert(html.includes('08:30 ET'));
        assert(html.includes('(two providers agree)'));
        assert(html.includes('Estimated date') && html.includes('Source: Nasdaq / Zacks estimate'));
        assert(html.includes('EPS Est: <strong>0.00</strong>'), 'an EPS estimate of 0 is a value, not missing');
        assert(html.includes('US Jobs Report &lt;b&gt;') && html.includes('Zed &lt;i&gt;Corp&lt;/i&gt;'));
        assert(html.includes('Friday, December 4, 2026'), 'English date, grouped by the event day');
        assert(html.indexOf('NVDA Earnings') < html.indexOf('US Jobs Report'), 'watchlist earnings first');

        context.Actions.renderCalendar(data, 'ipo');
        assert.match(content.innerHTML, /Not configured: IPO data provider\. IPO listings are unavailable/);

        // 400 reporters on one day: 150 shown, the rest on demand, biggest first
        const many = { economic: [], ipo: [], watchlist_tickers: [], coverage: {}, earnings: Array.from({ length: 400 }, (_, i) => (
            { symbol: `S${i}`, title: `S${i} Earnings`, date: '2026-11-05T00:00:00', date_formatted: '2026-11-05', market_cap: (i + 1) * 1e6 })) };
        context.Actions.renderCalendar(many, 'all');
        html = content.innerHTML;
        assert.equal((html.match(/ Earnings<\/h4>/g) || []).length, 150);
        assert(html.includes('S399 Earnings') && !html.includes('>S0 Earnings<'), 'largest market caps are the ones kept');
        assert(html.includes('Show all 400'));
        context.window._calendarData = many;
        context.Actions.showAllCalendarModalEvents('all');
        assert.equal((content.innerHTML.match(/ Earnings<\/h4>/g) || []).length, 400);
    }

    // ------------------------------------------------------- alerts table
    {
        const container = makeEl('div');
        const rows = [
            { id: 'a1', ticker: 'AAPL', type: 'PRICE_UP', target: 350, active: true },
            { id: 'b2', ticker: 'BTC-USD', type: 'PRICE_DOWN', target: 60000, active: false, triggered_at: '2026-11-14T10:30:00', triggered_price: 59950.5 },
            { id: "c3');alert(1);//", ticker: '<b>X</b>', type: 'VOLATILITY', target: 5, active: false },
        ];
        const context = {
            UI: { _escapeHtml: escapeHtml, formatPrice: (v) => String(v) },
            API: { getAlerts: async () => rows },
            document: { getElementById: (id) => (id === 'alerts-list-container' ? container : null) },
            console: { log() {}, warn() {}, error() {} },
        };
        vm.createContext(context);
        vm.runInContext(slice('UI.renderAlerts = async function', '// --- Telegram Delivery card'), context);
        await context.UI.renderAlerts();
        const html = container.innerHTML;
        assert(html.includes('Price crosses up') && html.includes('Daily move (up or down)'));
        assert(html.includes('>\n                                        Triggered\n'), 'fired alert says Triggered');
        assert(html.includes('@ 59950.5'), 'fired alert shows the trigger price');
        assert(html.includes('paused, or fired before trigger times were recorded'));
        assert(!html.includes('Triggered/Off'));
        assert(html.includes('5%') && html.includes('$350'));
        assert(!html.includes('<b>X</b>') && !html.includes("');alert(1)"));

        context.API.getAlerts = async () => [];
        await context.UI.renderAlerts();
        assert.match(container.innerHTML, /No alerts yet/);
        context.API.getAlerts = async () => { throw new Error('down'); };
        await context.UI.renderAlerts();
        assert.match(container.innerHTML, /Failed to load alerts/);
    }

    // ------------------------------------------- Telegram "Active" badge
    {
        const ids = ['tg-enabled', 'tg-token', 'tg-chat-id', 'tg-route-news', 'tg-route-signals', 'tg-route-capital', 'tg-route-alerts', 'tg-state-badge', 'tg-status'];
        const els = Object.fromEntries(ids.map(id => [id, makeEl('input')]));
        let settings = {};
        const context = {
            UI: {}, API: { getTelegramSettings: async () => settings },
            document: { getElementById: (id) => els[id] || null },
            console: { log() {}, warn() {}, error() {} },
        };
        vm.createContext(context);
        vm.runInContext(slice('UI.loadTelegramDelivery = async function', 'Actions.saveTelegramDelivery = async function'), context);
        const badge = async (s) => { settings = s; await context.UI.loadTelegramDelivery(); return els['tg-state-badge'].textContent; };
        const base = { enabled: true, configured: true, bot_token_masked: '1234…abcd', chat_id: '42', routes: {} };

        // ticked + filled in, but nothing was ever delivered → NOT "Active"
        assert.equal(await badge({ ...base, delivery_state: 'unverified' }), '● Enabled — not verified yet');
        assert.match(await badge({ ...base, delivery_state: 'ok', last_delivery: { ok: true, at: '2026-11-15T08:00:00Z' } }), /^● Active — last delivery OK /);
        assert.match(await badge({ ...base, delivery_state: 'failing', last_delivery: { ok: false, detail: 'http 401: Unauthorized', at: '2026-11-15T08:00:00Z' } }),
            /^● Delivery failing — http 401: Unauthorized$/);
        assert.equal(await badge({ ...base, enabled: false, delivery_state: 'off' }), '● Configured — off');
        assert.equal(await badge({ enabled: false, configured: false, delivery_state: 'not_configured', routes: {} }), '● Not configured');
        // an older server without delivery_state must not be shown as verified
        assert.equal(await badge({ ...base }), '● Enabled — not verified yet');
        // the token is never written back into the field
        assert.equal(els['tg-token'].value, '');
        assert.match(els['tg-token'].placeholder, /leave empty to keep/);
    }

    console.log('Calendar + Alerts tab audit: title, no duplicated overflow weeks, today/past marking, ordering, escaping, coverage notes, cache expiry, day list, alert status, Telegram badge: PASS');
})().catch((err) => { console.error(err); process.exit(1); });
