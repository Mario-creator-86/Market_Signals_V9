// Backtest tab audit (2026-11-15): frontend regression tests.
// Loads the real App object and the two UI helpers the tab uses out of
// frontend/app.js and drives them against a fake DOM and a fake network.
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(path.resolve(__dirname, '../../frontend/app.js'), 'utf8');
const indexHtml = fs.readFileSync(path.resolve(__dirname, '../../frontend/index.html'), 'utf8');

function slice(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    assert(start >= 0 && end > start, `markers not found: ${startMarker}`);
    return source.slice(start, end);
}
const appCode = slice('const App = {', 'window.ViewRouter = ViewRouter;');
const historyCode = slice('UI.renderBacktestHistory = function', 'UI.showHotListDetail = function');
const usageCode = slice('UI.refreshApiUsageWidget = async function', '// Persistence Logic');
const backtestCode = appCode.slice(appCode.indexOf('// ---- Backtest tab helpers'), appCode.indexOf('async deleteSignal('))
    + appCode.slice(appCode.indexOf('async runBacktest(event)'));

const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#039;');

function makeEnv() {
    const elements = {};
    const el = (id, init = {}) => (elements[id] = { id, innerHTML: '', textContent: '', value: '', disabled: false, dataset: {}, style: {}, ...init });
    el('bt-ticker', { value: 'BTC-USD' });
    el('bt-compare-tickers');
    el('bt-days', { value: '30' });
    el('bt-train-days', { value: '20' });
    el('bt-test-days', { value: '10' });
    el('bt-custom-tp');
    el('bt-custom-sl');
    el('bt-risk', { value: 'ALL' });
    el('bt-variant-mode', { value: 'STRICT' });
    el('bt-orb-entry', { value: 'BOTH' });
    el('bt-run-btn', { innerHTML: 'Run' });
    el('bt-wf-btn', { innerHTML: 'WF' });
    el('bt-last-time', { textContent: '-' });
    el('backtest-results', { innerHTML: 'PLACEHOLDER' });
    el('bt-strategies-list', { innerHTML: 'STALE STRATEGIES' });
    el('bt-trades-list', { innerHTML: 'STALE TRADES' });
    el('bt-history-list');
    el('bt-presets');
    el('api-usage-widget');
    const toasts = [];
    const posts = [];
    const downloads = [];
    const env = {
        elements, toasts, posts, downloads,
        respond: () => ({ ok: true, status: 200, body: {} }),
        fetchRespond: () => ({ ok: true, status: 200, body: [] }),
    };
    const context = {
        console: { log() {}, error() {}, warn() {} },
        UI: { _escapeHtml: escapeHtml, showToast: (m, t) => toasts.push({ m, t }), toggleDetails() {} },
        API: {
            BASE_URL: '/api',
            async fetchWithTimeout(url, opts = {}) {
                posts.push({ url, body: opts.body ? JSON.parse(opts.body) : null });
                const r = env.respond(url, opts.body ? JSON.parse(opts.body) : null);
                if (r.throw) throw new Error(r.throw);
                return { ok: r.ok, status: r.status, json: async () => r.body };
            },
        },
        async fetch(url) {
            const r = env.fetchRespond(url);
            if (r.throw) throw new Error(r.throw);
            return { ok: r.ok, status: r.status, json: async () => r.body };
        },
        document: {
            getElementById: (id) => elements[id] || null,
            createElement: () => ({ click() {}, set href(v) {}, set download(v) { downloads.push(v); } }),
        },
        window: {},
        localStorage: { _d: {}, getItem(k) { return this._d[k] ?? null; }, setItem(k, v) { this._d[k] = String(v); } },
        Blob: function (parts) { this.text = parts.join(''); },
        URL: { createObjectURL: (blob) => { env.lastBlob = blob.text; return 'blob:x'; }, revokeObjectURL() {} },
        alert() { throw new Error('alert() must not be used'); },
        prompt: () => null,
        IS_DEV: false,
        cachedBacktestHistory: [],
        toggleOrbOptions() { env.toggled = (env.toggled || 0) + 1; },
        setTimeout, clearTimeout, Date, Number, Array, Object, String, Math, JSON, Set, Promise, Error, RegExp,
    };
    vm.createContext(context);
    vm.runInContext(appCode + '\n;this.App = App;', context);
    vm.runInContext(historyCode, context);
    vm.runInContext(usageCode, context);
    // `let cachedBacktestHistory` lives outside the App slice in app.js.
    env.context = context;
    env.App = context.App;
    env.UI = context.UI;
    return env;
}

const trade = (i, pnl) => ({ type: 'BUY', entry: 100 + i, exit_price: 101 + i, net_pnl: pnl, qty: 1, sl: 99 + i, result: pnl > 0 ? 'WIN' : 'LOSS', entry_time: '2026-11-10T10:00:00+00:00', exit_time: '2026-11-10T11:00:00+00:00' });
const row = (risk, mode, n, wr, exp) => ({
    risk_mode: risk, trade_mode: mode, total_signals: n, wins: Math.round(n * wr / 100), losses: n - Math.round(n * wr / 100),
    win_rate: wr, win_rate_trades: wr, expectancy_r: exp, payoff_ratio: 1.5, profit_factor: 1.2, max_drawdown_r: 3.5,
    avg_win_r: 1.8, avg_loss_r: -1.0, closed_trades: n, open_trades: 0,
    evidence: { sample_size: n, minimum_sample: 30, sufficient: n >= 30, status: n >= 30 ? 'validated_sample_size' : 'insufficient_sample' },
    effective_exit_mode: 'FIXED_PCT', effective_tp_pct: 4, effective_sl_pct: 2,
    trades: Array.from({ length: Math.min(n, 3) }, (_, i) => trade(i, i % 2 ? 5 : -5)),
});

(async () => {
    // ---- 1. Recent simulations: date AND time, per record -------------------
    {
        const env = makeEnv();
        const history = [
            { mode: 'comparison', ticker: 'BTC-USD', days: 90, timestamp: '2026-09-06T12:04:24+03:00', data_coverage: { actual_span_days: 20.8, truncated: true } },
            { mode: 'faithful', ticker: 'BTC-USD', days: 7, timestamp: '2026-09-05T20:34:27+03:00' },
            { mode: 'comparison', ticker: '<img src=x onerror=1>', days: 5, timestamp: '2025-01-02T01:45:18+03:00' },
        ];
        env.UI.renderBacktestHistory(history);
        const html = env.elements['bt-history-list'].innerHTML;
        const whens = [...html.matchAll(/data-testid="bt-history-when">([^<]*)</g)].map(m => m[1]);
        assert.equal(whens.length, 3);
        assert(/Sep/.test(whens[0]) && /6/.test(whens[0]), `first row must carry its date, got "${whens[0]}"`);
        assert(/Sep/.test(whens[1]) && /5/.test(whens[1]), `second row must carry its date, got "${whens[1]}"`);
        assert.notEqual(whens[0], whens[1], 'runs from different days must not look alike');
        assert(/2025/.test(whens[2]), 'a run from another year shows the year');
        assert(html.includes('Faithful'), 'faithful runs are labelled as such, not "Standard"');
        assert(html.includes('90 days requested') && html.includes('20.8 days of data'), 'period says what was requested and what the data covered');
        assert(!html.includes('<img src=x'), 'ticker text is escaped');
    }

    // ---- 2. "Last run" comes from the saved history, and failures are said --
    {
        const env = makeEnv();
        env.fetchRespond = () => ({ ok: true, status: 200, body: [{ mode: 'comparison', ticker: 'AAPL', days: 30, timestamp: '2026-09-06T12:04:24+03:00', results: [] }] });
        await env.App.loadBacktestHistory();
        assert(/Sep/.test(env.elements['bt-last-time'].textContent), `Last run must not stay "-", got "${env.elements['bt-last-time'].textContent}"`);
        env.fetchRespond = () => ({ ok: false, status: 500, body: {} });
        await env.App.loadBacktestHistory();
        assert(env.elements['bt-history-list'].innerHTML.includes('could not be loaded'), 'a failed history load is reported');
    }

    // ---- 3. Error states: shown, escaped, and never leave stale results -----
    {
        const env = makeEnv();
        env.App._btLastData = { mode: 'comparison', ticker: 'OLD', results: [] };
        env.respond = () => ({ ok: false, status: 500, body: { detail: 'Internal server error' } });
        await env.App.runBacktest({ preventDefault() {} });
        const out = env.elements['backtest-results'].innerHTML;
        assert(out.includes('HTTP 500'), `an HTTP failure must be shown, got: ${out}`);
        assert(!out.includes('Configure parameters'), 'no empty placeholder after a failure');
        assert(!env.elements['bt-strategies-list'].innerHTML.includes('STALE'), 'stale strategies are cleared');
        assert(!env.elements['bt-trades-list'].innerHTML.includes('STALE'), 'stale trades are cleared');
        assert.equal(env.App._btLastData, null, 'a failed run must not leave the previous result exportable');
        assert.equal(env.elements['bt-run-btn'].disabled, false, 'the run button is re-enabled');

        env.respond = () => ({ ok: true, status: 200, body: { error: 'No data for <script>alert(1)</script>' } });
        await env.App.runBacktest({ preventDefault() {} });
        assert(env.elements['backtest-results'].innerHTML.includes('&lt;script&gt;'), 'server error text is escaped');
        assert(!env.elements['backtest-results'].innerHTML.includes('<script>'));

        // Standard mode: three parallel requests all failing used to render an empty table.
        env.elements['bt-risk'].value = 'MEDIUM';
        env.respond = () => ({ ok: true, status: 200, body: { mode: 'standard', error: 'Backtest could not run for NOSUCH: no historical price data was returned.' } });
        await env.App.runBacktest({ preventDefault() {} });
        assert(env.elements['backtest-results'].innerHTML.includes('no historical price data was returned'));
        assert(!env.elements['backtest-results'].innerHTML.includes('Top Strategy'));

        env.respond = () => ({ throw: 'Request timed out after 120000ms' });
        await env.App.runBacktest({ preventDefault() {} });
        assert(env.elements['backtest-results'].innerHTML.includes('did not finish within 120 seconds'), 'a timeout is not reported as "Connection error."');
    }

    // ---- 4. Form validation: nothing nonsensical is posted ------------------
    {
        const cases = [
            [{ 'bt-ticker': '' }, 'enter a ticker'],
            [{ 'bt-ticker': 'A B' }, 'not a valid ticker'],
            [{ 'bt-days': '5' }, 'Days must be a whole number between 7 and 365'],
            [{ 'bt-days': '0' }, 'Days must be'],
            [{ 'bt-days': '12.5' }, 'Days must be'],
            [{ 'bt-custom-tp': '0' }, 'Custom TP % must be greater than 0'],
            [{ 'bt-custom-tp': '-2' }, 'Custom TP % must be greater than 0'],
            [{ 'bt-custom-sl': '0' }, 'Custom SL % must be greater than 0'],
            [{ 'bt-custom-sl': '250' }, 'Custom SL %'],
            [{ 'bt-custom-sl': 'abc' }, 'Custom SL %'],
            [{ 'bt-compare-tickers': 'ETH-USD, =cmd|x' }, 'not valid symbols'],
        ];
        for (const [fields, fragment] of cases) {
            const env = makeEnv();
            Object.entries(fields).forEach(([id, v]) => { env.elements[id].value = v; });
            await env.App.runBacktest({ preventDefault() {} });
            assert.equal(env.posts.length, 0, `nothing may be posted for ${JSON.stringify(fields)}`);
            assert(env.elements['backtest-results'].innerHTML.includes(fragment), `expected "${fragment}" for ${JSON.stringify(fields)}, got ${env.elements['backtest-results'].innerHTML}`);
        }
        const parsed = makeEnv().App._btParseTickers(' btc-usd, ETH-USD ;btc-usd  aapl ');
        assert.deepEqual([...parsed.tickers], ['BTC-USD', 'ETH-USD', 'AAPL'], 'compare tickers are trimmed, upper-cased and deduplicated');
    }

    // ---- 5. Small samples are flagged and never crowned ---------------------
    {
        const env = makeEnv();
        env.respond = () => ({ ok: true, status: 200, body: {
            mode: 'comparison', ticker: 'AAPL', days: 30, timestamp: '2026-11-15T10:00:00+02:00',
            results: [row('RELAX', 'BREAKOUT', 1, 100, 2.12), row('MEDIUM', 'PULLBACK', 2, 50, 0.47)],
            warnings: ['Requested 30 days, but the data source returned only 5.0 days (390 bars). Every figure describes that shorter span.'],
            data_coverage: { requested_days: 30, actual_span_days: 5.0, bars: 390, truncated: true },
        } });
        await env.App.runBacktest({ preventDefault() {} });
        const overview = env.elements['backtest-results'].innerHTML;
        const strategies = env.elements['bt-strategies-list'].innerHTML;
        assert(!overview.includes('Top Strategy:'), 'a 1-trade row must not be crowned Top Strategy');
        assert(overview.includes('No strategy has enough trades to be ranked'));
        assert(overview.includes('Not statistically meaningful'));
        assert(overview.includes('MEDIUM'), 'the most active row is the one described');
        assert(overview.includes('Requested 30 days') && overview.includes('5.0 days'), 'truncated data coverage is shown');
        assert(strategies.includes('thin sample'), 'thin rows are badged');
        assert(!strategies.includes('102.12'), 'no score is computed from one trade');
        assert(strategies.includes('same RSI + MACD entry signal'), 'the table says the rows share one entry rule');
        assert(strategies.includes('TP 4.00% / SL 2.00%'), 'the exit profile actually simulated is shown');
        assert(!/\[[ABCD]\]/.test(strategies), 'the risk mode is no longer dressed up as an A-D quality grade');
        assert(/Nov/.test(env.elements['bt-last-time'].textContent), 'Last run carries the date of the run');

        // Re-sorting must not restamp "Last run" and must sort text columns.
        const stamp = env.elements['bt-last-time'].textContent;
        env.App.sortBacktest('trade_mode');
        assert.equal(env.elements['bt-last-time'].textContent, stamp, 'sorting is not a new run');
        const order = () => [...env.elements['bt-strategies-list'].innerHTML.matchAll(/<\/i> (BREAKOUT|PULLBACK)<\/div>/g)].map(m => m[1]);
        const desc = order();
        env.App.sortBacktest('trade_mode');
        assert.notDeepEqual(order(), desc, 'the Strategy header really sorts (it used to compare text with minus)');

        // Enough trades: ranked and named.
        const env2 = makeEnv();
        env2.respond = () => ({ ok: true, status: 200, body: {
            mode: 'comparison', ticker: 'AAPL', days: 90,
            results: [row('RELAX', 'BREAKOUT', 1, 100, 2.12), row('MEDIUM', 'PULLBACK', 40, 55, 0.2), row('STRONG', 'CONTRA', 35, 45, -0.1)],
        } });
        await env2.App.runBacktest({ preventDefault() {} });
        const top = env2.elements['backtest-results'].innerHTML;
        assert(top.includes('Top Strategy: PULLBACK / MEDIUM'), 'the best row WITH a sample is the top strategy');
        assert(top.includes('55.20'), 'score = win rate + expectancy for a sufficient sample');
    }

    // ---- 6. A saved run is shown as saved, not as a fresh one ---------------
    {
        const env = makeEnv();
        env.context.cachedBacktestHistory = [];
        vm.runInContext('cachedBacktestHistory = [' + JSON.stringify({
            mode: 'orb_fvg', ticker: 'AAPL', days: 5, timestamp: '2026-09-05T01:45:18+03:00',
            wins: 1, losses: 1, win_rate: 50, expectancy_r: 0.2, trades: [],
        }) + '];', env.context);
        env.elements['bt-last-time'].textContent = 'KEEP';
        env.App.viewBacktest(0);
        const html = env.elements['backtest-results'].innerHTML;
        assert(html.includes('Saved result from') && html.includes('not a fresh run'));
        assert(html.includes('OPENING_RANGE'), 'an Opening Range record is not titled FAITHFUL');
        assert(!html.includes('FAITHFUL'));
        assert.equal(env.elements['bt-last-time'].textContent, 'KEEP', 'opening a saved run does not change Last run');
        assert(!/Rezultat|Avertismente|vezi tabul/.test(backtestCode), 'no Romanian strings in the Backtest tab');
    }

    // ---- 7. CSV export: escaping, formula injection, real rows --------------
    {
        const env = makeEnv();
        const cell = (v) => env.App._btCsvCell(v);
        assert.equal(cell('=HYPERLINK("http://x","y")'), `"'=HYPERLINK(""http://x"",""y"")"`);
        assert.equal(cell('+cmd'), `"'+cmd"`);
        assert.equal(cell('-cmd'), `"'-cmd"`);
        assert.equal(cell('@SUM(A1)'), `"'@SUM(A1)"`);
        assert.equal(cell(-1.5), '"-1.5"', 'negative numbers stay numbers');
        assert.equal(cell('-1.5'), '"-1.5"');
        assert.equal(cell('say "hi"'), '"say ""hi"""');
        assert.equal(cell(null), '""');

        // Faithful record: fields are exit/net_pct/dir/r_net.
        env.App._btLastData = { mode: 'faithful', ticker: 'BTC-USD', trades: 1, trade_list: [
            { dir: 'LONG', entry: 100, exit: 102, net_pct: 1.75, r_net: 1.2, reason: '=evil()', entry_time: 't1', exit_time: 't2' }] };
        env.App.exportBacktestCSV();
        let lines = env.lastBlob.split('\r\n');
        assert.equal(lines.length, 2, 'one header and one trade row');
        assert(lines[1].includes('"LONG"') && lines[1].includes('"102"') && lines[1].includes('"1.75"') && lines[1].includes('"WIN"') && lines[1].includes('"1.2"'), `faithful trade exported with its values: ${lines[1]}`);
        assert(lines[1].includes(`"'=evil()"`), 'formula text in a trade field is neutralised');

        // Standard record: `signals` are per-ticker summaries, the trades are nested.
        env.App._btLastData = { mode: 'standard', ticker: 'AAPL', signals: [{ ticker: 'AAPL', wins: 1, trades: [trade(0, 5)] }] };
        env.App.exportBacktestCSV();
        lines = env.lastBlob.split('\r\n');
        assert.equal(lines.length, 2);
        assert(lines[1].startsWith('"AAPL","BUY","100","101"'), `standard trades exported, got ${lines[1]}`);

        // Walk-forward: one row per window (was header-only).
        env.App._btLastData = { mode: 'walk_forward', ticker: 'BTC-USD', windows: [{ test_start: 'a', test_end: 'b', best_strategy: { risk_mode: 'MEDIUM', trade_mode: 'PULLBACK', train_score: 10, test_score: -2 }, train_summary: { closed_trades: 12 }, test_summary: { closed_trades: 5, win_rate_trades: 40, expectancy_r: -0.1 } }] };
        env.App.exportBacktestCSV();
        lines = env.lastBlob.split('\r\n');
        assert.equal(lines.length, 2);
        assert(lines[1].includes('"MEDIUM","PULLBACK","10","-2","12","5","40","-0.1"'), `walk-forward window exported, got ${lines[1]}`);
        assert(env.downloads.every(name => /^backtest_[A-Za-z0-9_-]+_\d+\.csv$/.test(name)), 'file names are safe');
    }

    // ---- 8. Compare vs Last: same ticker, same strategy, signed deltas ------
    {
        const env = makeEnv();
        const text = (h) => env.App._btDiffText(h);
        const now = { mode: 'comparison', ticker: 'BTC-USD', timestamp: '2026-11-15T10:00:00+02:00', results: [row('MEDIUM', 'PULLBACK', 40, 55, 0.2), row('RELAX', 'CONTRA', 3, 100, 2)] };
        const other = { mode: 'comparison', ticker: 'AAPL', timestamp: '2026-11-14T10:00:00+02:00', results: [row('MEDIUM', 'PULLBACK', 90, 10, -1)] };
        const before = { mode: 'comparison', ticker: 'BTC-USD', timestamp: '2026-09-06T12:04:00+03:00', results: [row('RELAX', 'CONTRA', 50, 90, 3), row('MEDIUM', 'PULLBACK', 30, 50, -0.3)] };
        const out = text([now, other, before]).text;
        assert(out.includes('BTC-USD') && out.includes('MEDIUM / PULLBACK'), out);
        assert(out.includes('win rate 50.0% → 55.0% (+5.0 pts)'), `win rate delta is latest minus previous for the same strategy: ${out}`);
        assert(out.includes('trades 30 → 40 (+10)'), out);
        assert(out.includes('expectancy -0.30R → 0.20R (+0.50R)'), out);
        assert(/Sep/.test(out) && /Nov/.test(out), 'both runs are dated');
        assert(text([now, other]).error.includes('No earlier BTC-USD run'), 'a different ticker is not compared');
        assert(text([now]).error.includes('no earlier run'));
        // The old code read win_pct/total_wins straight off a comparison record: always 0.
        const faithfulA = { mode: 'faithful', ticker: 'BTC-USD', timestamp: '2026-11-15T10:00:00+02:00', risk_mode: 'FAITHFUL', trade_mode: 'LIVE_REPLAY', win_pct: 33.3, total_wins: 1, total_losses: 2, total_trades: 3, expectancy_r: -0.547 };
        const faithfulB = { ...faithfulA, timestamp: '2026-11-14T10:00:00+02:00', win_pct: 66.7, total_wins: 2, total_losses: 1, expectancy_r: 0.665 };
        const f = text([faithfulA, faithfulB]).text;
        assert(f.includes('win rate 66.7% → 33.3% (-33.4 pts)') && f.includes('expectancy 0.67R → -0.55R (-1.21R)'), f);
    }

    // ---- 9. Walk-forward: risk mode is sent, insufficient data is said ------
    {
        const env = makeEnv();
        env.elements['bt-risk'].value = 'MEDIUM';
        env.elements['bt-days'].value = '60';
        env.respond = () => ({ ok: true, status: 200, body: {
            status: 'insufficient_data', mode: 'walk_forward', ticker: 'BTC-USD', days: 60, train_days: 20, test_days: 10,
            reason: 'Only 20.8 days of price history are available for BTC-USD, but one walk-forward window needs 30 days (20 train + 10 test). Shorten the train/test days.',
            windows: [], summary: { windows: 0, total_trades: 0, win_rate_trades: null, expectancy_r: null }, evidence: { sample_size: 0, minimum_sample: 30, sufficient: false },
        } });
        await env.App.runWalkForward({ preventDefault() {} });
        assert.deepEqual([...env.posts[0].body.risk_modes], ['MEDIUM'], 'the selected risk mode restricts the search');
        const html = env.elements['backtest-results'].innerHTML;
        assert(html.includes('Insufficient data') && html.includes('Only 20.8 days'), html);
        assert(!/win rate/i.test(html) && !html.includes('ds-stat'), 'no KPI tiles of zeros when nothing was tested');
        assert(!env.elements['bt-trades-list'].innerHTML.includes('STALE'));

        env.elements['bt-risk'].value = 'ALL';
        env.respond = () => ({ ok: true, status: 200, body: {
            status: 'ok', mode: 'walk_forward', ticker: 'BTC-USD', days: 60, train_days: 20, test_days: 10,
            windows: [{ test_start: '2026-10-01T00:00:00', test_end: '2026-10-11T00:00:00', best_strategy: { risk_mode: 'MEDIUM', trade_mode: 'PULLBACK', train_score: 12.5, test_score: -3.25 }, train_summary: { closed_trades: 20 }, test_summary: { closed_trades: 6, win_rate_trades: 33.3, expectancy_r: -0.2, profit_factor: 0.6, max_drawdown_pct: 2.5, wins: 2, losses: 4 } }],
            summary: { windows: 1, total_trades: 6, win_rate_trades: 33.33, expectancy_r: -0.2, avg_train_score: 12.5, avg_test_score: -3.25, strategy_counts: { 'MEDIUM|PULLBACK': 1 } },
            evidence: { sample_size: 6, minimum_sample: 30, sufficient: false }, warnings: [],
        } });
        await env.App.runWalkForward({ preventDefault() {} });
        assert.equal(env.posts[1].body.risk_modes, undefined, 'ALL searches every risk mode');
        const ok = env.elements['backtest-results'].innerHTML;
        assert(ok.includes('2026-10-01 to 2026-10-11') && !ok.includes(' ? '), 'the window is not printed with a "?"');
        assert(ok.includes('12.50') && ok.includes('-3.25'), 'train and test score are both shown');
        assert(ok.includes('Not statistically meaningful') && ok.includes('6 closed out-of-sample trades'));

        // Opening Range / Faithful are not walk-forward engines.
        const env3 = makeEnv();
        env3.elements['bt-risk'].value = 'FAITHFUL';
        await env3.App.runWalkForward({ preventDefault() {} });
        assert.equal(env3.posts.length, 0);
        assert(env3.elements['backtest-results'].innerHTML.includes('RSI/MACD engine only'));
    }

    // ---- 10. API usage widget and presets -----------------------------------
    {
        const env = makeEnv();
        env.fetchRespond = () => ({ ok: true, status: 200, body: { current: { fmp_rpm: 0, yahoo_rpm: 8, external_rpm: 19 }, metrics: { external_calls_by_service_last_minute: { yahoo: 8, binance: 3 }, external_calls_last_minute: 19 } } });
        await env.UI.refreshApiUsageWidget();
        const html = env.elements['api-usage-widget'].innerHTML;
        assert(html.includes('Yahoo (stocks)') && html.includes('Binance (crypto)') && html.includes('>3<') && html.includes('>19<'), html);
        assert(!html.includes('FMP'), 'a source the app never calls is not listed');
        env.fetchRespond = () => ({ ok: false, status: 500, body: {} });
        await env.UI.refreshApiUsageWidget();
        assert(env.elements['api-usage-widget'].innerHTML.includes('unavailable'), 'a failed request is not shown as 0 calls');
        env.fetchRespond = () => ({ ok: true, status: 200, body: { current: { yahoo_rpm: 0 }, metrics: {} } });
        await env.UI.refreshApiUsageWidget();
        assert(env.elements['api-usage-widget'].innerHTML.includes('unavailable'), 'the server fallback of zeros is "unknown", not "no traffic"');

        // A preset without a custom TP/SL clears the fields.
        const env2 = makeEnv();
        env2.context.localStorage.setItem('bt_presets', JSON.stringify({ plain: { ticker: 'AAPL', days: 30, risk_mode: 'OPENING_RANGE', variant_mode: 'STRICT', custom_tp_pct: null, custom_sl_pct: null } }));
        env2.elements['bt-presets'].appendChild = () => {};
        env2.context.document.createElement = () => ({});
        env2.elements['bt-custom-tp'].value = '9';
        env2.elements['bt-custom-sl'].value = '3';
        env2.App.loadBacktestPresets();
        env2.elements['bt-presets'].value = 'plain';
        env2.elements['bt-presets'].onchange();
        assert.equal(env2.elements['bt-custom-tp'].value, '', 'the previous custom TP must not survive a preset without one');
        assert.equal(env2.elements['bt-custom-sl'].value, '');
        assert.equal(env2.toggled, 1, 'the Opening Range options follow the loaded risk mode');
    }

    // ---- 10b. Opening Range and Faithful responses render with their notes --
    {
        const env = makeEnv();
        env.elements['bt-ticker'].value = 'AAPL';
        env.elements['bt-risk'].value = 'OPENING_RANGE';
        env.respond = () => ({ ok: true, status: 200, body: {
            mode: 'orb_fvg', ticker: 'AAPL', days: 30, entry_type: 'MIDPOINT', wins: 2, losses: 3, time_exits: 1,
            win_rate: 40, win_rate_trades: 40, avg_r: -0.21, profit_factor: 0.7, max_drawdown_pct: 2.4, max_drawdown_r: 2.4,
            avg_win_r: 1.9, avg_loss_r: -1.05, costs_applied: true, actual_span_days: 29.1,
            data_start: '2026-10-16T09:30:00-04:00', data_end: '2026-11-14T16:00:00-05:00',
            evidence: { sample_size: 5, minimum_sample: 30, sufficient: false },
            warnings: ['Insufficient evidence: 5 closed trades; at least 30 are needed before the metrics mean anything.'],
            trades: [{ direction: 'LONG', entry_type: 'MIDPOINT', entry_price: 190.12, exit_price: 191.5, pnl_pct: 0.71, r_multiple: 1.9, result: 'WIN', exit_reason: 'Take Profit Hit (BUY)', entry_time: '2026-11-10T10:05:00-05:00', exit_time: '2026-11-10T11:20:00-05:00' }],
            timestamp: '2026-11-15T10:00:00+02:00',
        } });
        await env.App.runBacktest({ preventDefault() {} });
        assert.equal(env.posts[0].body.trade_mode, 'ORB_FVG');
        const orb = env.elements['backtest-results'].innerHTML;
        assert(orb.includes('Insufficient evidence: 5 closed trades') && orb.includes('29.1 days'), orb);
        assert(!orb.includes('Top Strategy:'), 'five trades are not a ranking');
        const orbRows = env.elements['bt-strategies-list'].innerHTML;
        assert(orbRows.includes('Max drawdown (R)') && orbRows.includes('2.40R'), 'an R drawdown is labelled in R, not as an account percentage');
        assert(orbRows.includes('after the configured commission and slippage'));
        assert(env.elements['bt-trades-list'].innerHTML.includes('>190.12<'), 'trade prices are shown');

        const env2 = makeEnv();
        env2.elements['bt-risk'].value = 'FAITHFUL';
        env2.elements['bt-variant-mode'].value = 'CUSTOM';
        env2.respond = () => ({ ok: true, status: 200, body: {
            mode: 'faithful', ticker: 'BTC-USD', days: 30, trades: 3, wins: 1, losses: 2, win_rate: 33.3, win_rate_trades: 33.3,
            expectancy_r: -0.547, payoff_ratio: 0.46, profit_factor: 0.23, max_drawdown_pct: 4.26, avg_win_r: 0.49, avg_loss_r: -1.066,
            evidence: { sample_size: 3, minimum_sample: 30, sufficient: false, status: 'insufficient_sample' },
            warnings: ['Insufficient evidence: 3 closed replay trades; need at least 30 before treating metrics as validation.'],
            data_limitations: ['No timestamped historical news archive is available for this replay; historical AI-news gating is not simulated.'],
            data_coverage: { requested_days: 30, actual_span_days: 30.0, bars: 43200, truncated: false },
            variant_mode: 'CUSTOM', variant_applied: true,
            trade_list: [{ dir: 'SHORT', entry: 0.000123, exit: 0.000119, net_pct: 3.1, r_net: 1.4, reason: 'take_profit', entry_time: '2026-11-10T10:05:00+00:00', exit_time: '2026-11-10T11:20:00+00:00' }],
        } });
        await env2.App.runBacktest({ preventDefault() {} });
        assert.equal(env2.posts[0].body.risk_mode, 'FAITHFUL');
        assert.equal(env2.posts[0].body.variant_mode, 'CUSTOM', 'the variant chosen for the replay is sent');
        const rows = env2.elements['bt-strategies-list'].innerHTML;
        assert(rows.includes('historical AI-news gating is not simulated'), 'replay limitations are shown with the row');
        assert(rows.includes('thin sample'));
        const tradesHtml = env2.elements['bt-trades-list'].innerHTML;
        assert(tradesHtml.includes('0.0001230'), `a sub-cent price is not printed as 0.00: ${tradesHtml.slice(0, 400)}`);
        assert(tradesHtml.includes('1.40R'), 'net R of each replay trade is shown');
    }

    // ---- 11. The form says what ALL runs ------------------------------------
    assert(!indexHtml.includes('ALL (Compare 4 Risk Modes)'), 'the ALL option no longer hides that 2 of the 6 other modes are excluded');
    assert(indexHtml.includes('ALL (4 RSI/MACD modes x 3 exit profiles)'));
    assert(indexHtml.includes('Variant Mode applies to FAITHFUL only'));

    console.log('Backtest tab: history dates, error states, validation, evidence flags, saved runs, CSV, compare-vs-last, walk-forward, API usage, presets: PASS');
})().catch((err) => { console.error(err); process.exit(1); });
