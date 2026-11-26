// Scanner tab audit 2026-11-15 — regression tests for the Scanner view in frontend/app.js.
// Run: node tests/frontend/tab_audit_scanner.cjs
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(require('path').resolve(__dirname, '../../frontend/app.js'), 'utf8');

const start = source.indexOf('    _tapeSpark(points, changePct) {');
const end = source.indexOf('    // Prefer our internal OHLC workspace.', start);
assert(start >= 0 && end > start, 'scanner section markers not found');
const escStart = source.indexOf('UI._escapeHtml = function');
const escEnd = source.indexOf('};', escStart) + 2;
assert(escStart >= 0, '_escapeHtml not found');

// ---- minimal DOM -----------------------------------------------------------
const elements = {};
const makeEl = (id) => ({
    id, innerHTML: '', textContent: '', style: {}, attrs: {},
    classList: { toggle() {}, add() {}, remove() {} },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k]; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    appendChild() {},
    closest() { return { querySelector: () => (elements[`${id}::sub`] = elements[`${id}::sub`] || makeEl(`${id}::sub`)) }; },
});
const IDS = ['scanner-summary-strip', 'full-scanner-grid', 'crypto-marquee', 'stocks-marquee', 'kucoin-marquee',
    'crypto-count', 'stocks-count', 'kucoin-count', 'scanner-agent-status', 'scanner-status-alert',
    'tab-crypto', 'tab-stocks', 'tab-kucoin', 'tape-spark-defs'];
IDS.forEach(id => { elements[id] = makeEl(id); });

const timers = [];
const fetchLog = [];
let fetchImpl = async () => ({ ok: true, json: async () => ({ sparklines: {} }) });
let moversImpl = async () => ({ ok: true, json: async () => ({ gainers: [], losers: [], available: true, stale: false }) });
const toasts = [];

const context = {
    UI: {
        formatPrice: v => String(v),
        renderAgentStatusHeader() {},
        showToast: (msg, type) => toasts.push([msg, type]),
        scheduleRender: (_k, fn) => fn(),
    },
    API: {
        _scannerCache: { data: null, ts: 0 },
        fetchWithTimeout: (url) => { fetchLog.push(url); return moversImpl(url); },
        getScannerState: async () => ({ hot_tickers: [] }),
    },
    cachedState: {},
    document: {
        getElementById: id => elements[id] || null,
        querySelectorAll: () => [],
        createElement: () => makeEl('new'),
        createElementNS: () => makeEl('svg'),
        body: { appendChild() {} },
    },
    window: {},
    localStorage: { getItem: () => null, setItem() {} },
    logRateLimited() {},
    fetch: (url) => { fetchLog.push(url); return fetchImpl(url); },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    requestAnimationFrame: fn => fn(),
    console, Intl, JSON, Math, Date, Promise, Number, String, Array, Object, Set, Error, encodeURIComponent,
};
context.window = context;
vm.createContext(context);
vm.runInContext(source.slice(escStart, escEnd), context);
vm.runInContext(`Object.assign(UI, {\n${source.slice(start, end)}\n});`, context);
const UI = context.UI;
const flushTimers = () => { const due = timers.splice(0); due.forEach(t => t.fn()); };
const tick = () => new Promise(resolve => setImmediate(resolve));
const count = (text, needle) => text.split(needle).length - 1;

(async () => {
    // 1. Boards are static ranked lists (UI v10): no marquee, so no second copy of the rows.
    //    Every mover is in the DOM once, and every add button stays in the tab order.
    const rows20 = Array.from({ length: 20 }, (_, i) => UI._scannerTapeCard(
        { symbol: `T${i}USDT`, change_pct: -i, price: 1, source: 'binance' }, i));
    let html = UI._tapeCascade(rows20, 'sig');
    assert.equal(count(html, 'class="tape-row"'), 20, 'each mover is rendered once');
    assert(!html.includes('tape-clone'), 'the marquee copy is gone');
    assert(!/animation|tape-cascade run/.test(html), 'nothing scrolls on its own');
    assert.equal(count(html, 'tabindex="-1"'), 0, 'add buttons stay reachable by keyboard');
    assert(html.includes('data-sig="sig"') && html.includes('data-rows="20"'), 'in-place patching still has its signature');
    html = UI._tapeCascade(rows20.slice(0, 3), 'sig');
    assert.equal(count(html, 'class="tape-row"'), 3);
    assert(!/stroke="#|fill="url\(/.test(html), 'sparkline colour comes from CSS classes, no gradient fill');

    // 2. Third-party symbols are escaped in HTML and in inline handlers.
    const hostile = `X'");alert(1)//<img src=x onerror=alert(2)>USDT`;
    html = UI._scannerTapeCard({ symbol: hostile, change_pct: 1, price: 1, source: 'binance' }, 0);
    assert(!html.includes('<img'), 'markup in a symbol must not reach the DOM');
    assert(!html.includes(`('${hostile}'`), 'symbol must not be pasted into a JS string');
    assert(html.includes('UI.openTickerChart(&quot;X&#39;\\&quot;);alert(1)'), 'handler argument is a JSON string, HTML-escaped');
    html = UI._scannerTapeCard({ symbol: '牛来USDT', change_pct: -12.33, price: 0.05, source: 'binance' }, 0);
    assert(html.includes('牛来USDT'), 'non-ASCII symbols still render');

    // 3. Summary cards: truthful labels, one value, one date format, flat is not "positive".
    const scannerState = {
        as_of_utc: '2026-11-15T07:53:00+00:00',
        liquid_crypto_tickers: [{ change_pct: 1 }, { change_pct: -2 }, { change_pct: -3 }],
        legs: { crypto: { as_of_utc: '2026-11-15T07:53:00+00:00' } },
    };
    UI.renderScannerSummary(scannerState, [
        { symbol: 'KAVAUSDT', change_pct: -58.41 }, { symbol: 'INITUSDT', change_pct: 18.11 },
        { symbol: 'FLATUSDT', change_pct: 0 },
    ], 'crypto');
    let summary = elements['scanner-summary-strip'].innerHTML;
    for (const gone of ['>Leader<', '>Universe<', '>Breadth<', 'visible rows']) {
        assert(!summary.includes(gone), `misleading label still present: ${gone}`);
    }
    assert(summary.includes('Biggest move'), 'the max-|change| card is named for what it is');
    assert(summary.includes('▼ 58.41%</span>') && summary.includes('24h change · updated'),
        'same arrow + absolute value as the list row, basis stated next to it');
    assert(summary.includes('scanner-stat-chg is-neg'), 'the biggest move is the one coloured number, by class');
    assert(!summary.includes('-58.41'), 'no second, differently formatted value');
    assert(summary.includes('>1 / 1<') && summary.includes('1 flat'), 'a 0.00% row is neither up nor down');
    assert(summary.includes('all 3 liquid pairs: 1 up / 2 down'), 'whole-universe breadth is stated separately');
    assert(summary.includes('biggest 24h movers among the 3 most-traded Binance USDT pairs'));
    assert(/updated Nov 15, 2026, \d{2}:\d{2}/.test(summary), 'en-US date like the rest of the app');
    assert(!/\d{2}\.\d{2}\.\d{4}/.test(summary), 'no ro-RO dd.mm.yyyy date');

    // 4. Stock rows: which number is shown depends on the US session.
    const htfl = { symbol: 'HTFL', source: 'yahoo_losers', change_pct: -0.64, regular_change_pct: -19.13, session: 'post' };
    let row = UI._scannerDisplayRow(htfl, 'CLOSED');
    assert.equal(row.change_pct, -19.13, 'closed market: last regular session move');
    assert.equal(row._note, 'after-hours -0.64%');
    row = UI._scannerDisplayRow(htfl, 'AFTER_HOURS');
    assert.equal(row.change_pct, -0.64, 'live after-hours: the extended move');
    assert.equal(row._note, 'day -19.13%');
    row = UI._scannerDisplayRow(htfl, undefined);
    assert.equal(row.change_pct, -0.64, 'old payload without us_market: unchanged');
    row = UI._scannerDisplayRow({ symbol: 'BTCUSDT', source: 'binance', change_pct: -4.8 }, 'CLOSED');
    assert.equal(row.change_pct, -4.8);
    assert.equal(row._basis, '24h');
    assert.equal(UI._scannerStockBasis({ us_market: { session: 'CLOSED' } }).header, 'Change (last session)');

    // 5. KuCoin movers: one request for tape + table, shared while in flight, limit=50.
    let resolveMovers;
    moversImpl = () => new Promise(resolve => { resolveMovers = resolve; });
    fetchLog.length = 0;
    const p1 = UI._loadKuCoinMovers();
    const p2 = UI._loadKuCoinMovers();
    const moversPayload = {
        gainers: [{ symbol: 'EVAA-USDT', price: 1.8, change_pct: 71.02, volume: 6_700_000 },
            { symbol: 'DUP-USDT', price: 1, change_pct: 0.5, volume: 90_000_000 }],
        losers: [{ symbol: 'KAVA-USDT', price: 0.03, change_pct: -58.3, volume: 2_000_000 },
            { symbol: 'DUP-USDT', price: 1, change_pct: 0.5, volume: 90_000_000 }],
        available: true, stale: false,
    };
    resolveMovers({ ok: true, json: async () => moversPayload });
    await Promise.all([p1, p2]);
    await UI._loadKuCoinMovers();
    assert.deepEqual(fetchLog, ['/api/crypto/movers?limit=50'], 'exactly one movers request');
    assert.equal(UI._kucoinMovers(moversPayload).length, 3, 'overlap between gainers and losers is de-duplicated');
    assert.equal(UI._kucoinNotice(moversPayload), '');

    // 5b. KuCoin table: spot labels, English headers, "Most Traded" really sorts by volume.
    context.cachedState = { scanner: { hot_tickers: [] } };
    UI.currentScannerTab = 'kucoin';
    UI.scannerSortMode = 'active';
    await UI.renderFullScanner();
    let grid = elements['full-scanner-grid'].innerHTML;
    assert(!/perpetual|Modificare|Vezi Grafic/.test(grid), 'no false "perpetual" label, no Romanian strings');
    assert(grid.includes('KuCoin spot') && grid.includes('Change (24h)') && grid.includes('View chart'));
    assert(grid.indexOf('DUP-USDT') < grid.indexOf('EVAA-USDT') && grid.indexOf('EVAA-USDT') < grid.indexOf('KAVA-USDT'),
        'Most Traded orders by 24h volume');
    assert.equal(elements['kucoin-count'].textContent, 3);

    // 5c. "KuCoin unavailable" is not "no pair moved", and never replaces a good list silently.
    moversImpl = async () => ({ ok: true, json: async () => ({ gainers: [], losers: [], available: false, reason: 'KuCoin API unavailable' }) });
    const staleData = await UI._loadKuCoinMovers(true);
    assert.equal(staleData.stale, true);
    assert.equal(UI._kucoinMovers(staleData).length, 3, 'previous list is kept');
    assert(UI._kucoinNotice(staleData).includes('KuCoin feed unavailable'), 'and labelled as stale');
    context.window.cachedKuCoinData = null;
    UI._kucoinRetryAfter = 0;
    await assert.rejects(UI._loadKuCoinMovers(true), /KuCoin API unavailable/);
    UI._renderKuCoinTape(null);
    assert(elements['kucoin-marquee'].innerHTML.includes('KuCoin movers unavailable'));

    // 6. Sparklines: the three columns share requests, and a re-render does not re-ask.
    flushTimers(); await tick(); await tick(); await tick();   // drain what the renders above queued
    fetchLog.length = 0;
    timers.length = 0;
    UI._refreshScannerSparklines(Array.from({ length: 20 }, (_, i) => `B${i}USDT`));
    UI._refreshScannerSparklines(Array.from({ length: 20 }, (_, i) => `S${i}`));
    UI._refreshScannerSparklines(Array.from({ length: 8 }, (_, i) => `K${i}-USDT`));
    assert.equal(fetchLog.length, 0, 'nothing is sent before the coalescing window closes');
    flushTimers();
    assert.equal(fetchLog.length, 2, '48 symbols -> two batches of 24, not one request per column');
    await tick(); await tick(); await tick();
    const afterFirst = fetchLog.length;
    UI._refreshScannerSparklines(Array.from({ length: 20 }, (_, i) => `B${i}USDT`));   // poll re-render
    UI._refreshScannerSparklines(Array.from({ length: 20 }, (_, i) => `S${i}`));
    assert.equal(timers.filter(t => t.ms === 150).length, 0, 're-render inside the retry window queues nothing');
    assert.equal(fetchLog.length, afterFirst);
    assert.equal(timers.filter(t => t.ms > 6000).length, 1, 'one retry timer for the whole flush');

    // 7. Board, cards and table come from ONE snapshot; error state is read from `status`.
    context.window.cachedKuCoinData = moversPayload;
    context.window.lastKuCoinFetch = Date.now();
    context.cachedState = { scanner: {
        as_of_utc: '2026-11-15T07:53:00+00:00',
        us_market: { session: 'CLOSED', reason: 'weekend' },
        status: { state: 'OK', last_error: null },
        hot_tickers: [
            { symbol: 'KAVAUSDT', change_pct: -58.41, price: 0.028, volume: 23_000_000, source: 'binance' },
            { symbol: 'NVDABUSDT', change_pct: -1.4, price: 159.65, volume: 24_900_000, source: 'binance',
                instrument_type: 'tokenized_stock', underlying: 'NVDA' },
            { ...htfl, price: 48.15, name: 'Heartflow <b>Inc</b>', volume: 3_704_634, quote_time_utc: '2026-11-13T23:49:38+00:00' },
            { symbol: 'KAVAUSDT', change_pct: -58.35, price: 0.028, source: 'binance' },   // duplicate row
        ],
    } };
    UI.currentScannerTab = 'crypto';
    UI.scannerSortMode = 'volatility';
    await UI.renderFullScanner();
    grid = elements['full-scanner-grid'].innerHTML;
    summary = elements['scanner-summary-strip'].innerHTML;
    const tape = elements['crypto-marquee'].innerHTML;
    assert(tape.includes('▼ 58.41%') && summary.includes('▼ 58.41%') && grid.includes('58.41%'),
        'board, summary card and table show the same value');
    assert(!tape.includes('58.35') && !grid.includes('58.35'));
    assert.equal(elements['crypto-count'].textContent, 2, 'tab count = rows of the tab, not the board\'s top-20');
    assert.equal(elements['stocks-count'].textContent, 1);
    assert(grid.includes('>Tokenised stock<') && grid.includes('Tokenised NVDA stock on Binance'));
    assert(!/Modificare|Vezi Grafic/.test(grid));

    UI.currentScannerTab = 'stocks';
    await UI.renderFullScanner();
    grid = elements['full-scanner-grid'].innerHTML;
    assert(grid.includes('US market closed (weekend)'), 'closed market is stated on the Stocks tab');
    assert(grid.includes('Change (last session)') && grid.includes('19.13%') && grid.includes('after-hours -0.64%'));
    assert(grid.includes('Heartflow &lt;b&gt;Inc&lt;/b&gt;'), 'company name is escaped');
    assert(elements['stocks-marquee'].innerHTML.includes('19.13%'), 'board uses the same stock number');

    context.cachedState = { scanner: { hot_tickers: [], status: { state: 'ERROR', last_error: 'Binance movers unavailable (HTTP 451)' } } };
    UI.currentScannerTab = 'crypto';
    await UI.renderFullScanner();
    assert(elements['full-scanner-grid'].innerHTML.includes('Failed to load scanner data'),
        'a failed scan is not presented as "No movers detected"');

    console.log('Scanner tab: static boards (no marquee copy), escaping, truthful summary cards, session-aware stock change, single KuCoin fetch, coalesced sparklines, one snapshot for board/cards/table, error state: PASS');
})().catch(err => { console.error(err); process.exit(1); });
