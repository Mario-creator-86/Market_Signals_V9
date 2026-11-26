// 2026-11-15 tab audit — AI Trading Cockpit (Home). Runs the real cockpit.js
// against a fake DOM and checks what the owner would read.
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(path.resolve(__dirname, '../../frontend/cockpit.js'), 'utf8');

function makeElement(id) {
    const classes = new Set();
    return {
        id, innerHTML: '', textContent: '', disabled: false, hidden: false, dataset: {}, style: {}, children: [],
        classList: {
            add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
            toggle: (c, on) => { const want = on === undefined ? !classes.has(c) : Boolean(on); if (want) classes.add(c); else classes.delete(c); return want; },
        },
        addEventListener() {}, setAttribute() {}, getAttribute() { return null; }, scrollIntoView() {},
        querySelectorAll() { return []; }, querySelector() { return null; }, closest() { return null; },
    };
}

function boot(responses) {
    const elements = new Map();
    const toasts = [];
    const posts = [];
    const document = {
        readyState: 'complete', hidden: false,
        getElementById(id) { if (!elements.has(id)) elements.set(id, makeElement(id)); return elements.get(id); },
        querySelectorAll() { return []; }, querySelector() { return null; }, addEventListener() {},
    };
    const fetch = async (url, options = {}) => {
        const key = Object.keys(responses).find((prefix) => String(url).startsWith(prefix));
        if ((options.method || 'GET') === 'POST') posts.push(String(url));
        const entry = key ? responses[key] : { status: 404, body: { detail: 'not found' } };
        const status = entry.status || 200;
        return { ok: status >= 200 && status < 300, status, json: async () => entry.body };
    };
    const window = {
        location: { hash: '', href: 'http://127.0.0.1:8003/' }, scrollY: 0, scrollTo() {},
        setInterval: () => 1, clearInterval() {},
        UI: { showToast: (message, kind) => toasts.push([message, kind]) },
    };
    const context = {
        window, document, fetch, console, URL, URLSearchParams, AbortController,
        setTimeout: () => 0, clearTimeout() {}, setInterval: () => 1, clearInterval() {},
        Date, Math, Number, String, Array, Object, Set, Map, JSON, Promise, Error, Boolean, Intl,
    };
    vm.createContext(context);
    vm.runInContext(source, context);
    return { context, elements, toasts, posts, html: (id) => (elements.get(id) || { innerHTML: '' }).innerHTML };
}

const hoursAgo = (hours) => new Date(Date.now() - hours * 3600 * 1000).toISOString();
const section = (items, extra = {}) => ({ state: items.length ? 'SUCCESS' : 'EMPTY', items, count: items.length, ...extra });

function payload() {
    const failing = [{ id: 'paper_broker', label: 'Paper Broker', status: 'UNHEALTHY', message: 'Paper order ord_1 is recorded as FILLED, but its position (pos_1) does not exist in the Capital ledger. New Paper Auto entries are blocked.', action: 'Open Agents → Risk Manager and press “Reconcile broker”.' }];
    const services = [
        { id: 'backend', label: 'Backend', status: 'HEALTHY', message: 'Cockpit API request completed.', checked_at: hoursAgo(0), evidence: {} },
        { id: 'database', label: 'Database', status: 'HEALTHY', message: 'ok', checked_at: hoursAgo(0), evidence: {} },
        { ...failing[0], checked_at: hoursAgo(0.1), evidence: { conflict_count: 1 } },
    ];
    return {
        schema_version: 1, state: 'SUCCESS', generated_at: hoursAgo(0), cockpit_state_version: 'v1',
        header: {
            market: { is_open: false, session: 'CLOSED', reason: 'weekend', exchange: 'XNYS' },
            autonomy: { mode: 'PAPER_AUTO', ready: false, reasons: ['x'], evaluated: true },
            ai: { status: 'HEALTHY', global_model: 'm' },
            regime: { available: true, value: 'risk-on', sentiment: 'bullish', confidence: 40, generated_at: hoursAgo(72), freshness: 'STALE' },
            capital: { deployable_cash: 8000, estimated_equity: 9900, notional_exposure_pct: 15, notional_exposure: 1500, open_positions: 1,
                daily_realized_pnl: null, daily_closed_trades: 0, daily_pnl_basis: 'Realized net P/L of positions closed today (Europe/Bucharest day, same boundary as the Risk daily-loss limit)', unrealized_pnl: 12 },
            risk: { status: 'ACTIVE', readiness: false }, kill_switch_active: false,
        },
        sections: {
            opportunities: section([], { message: 'No current setup clears the real Capital or Pre-Earnings eligibility gates.' }),
            risks: section([
                { severity: 'CRITICAL', type: 'EXECUTION_RECONCILIATION', title: 'Paper order ledger conflict (1)', detail: 'Paper order ord_1 is recorded as FILLED, but its position (pos_1) does not exist in the Capital ledger.', action: 'Open Agents → Risk Manager and press “Reconcile broker”.' },
                { severity: 'HIGH', type: 'EARNINGS_SOON', ticker: 'GRRR', title: 'Earnings in 1d', detail: 'After Hours' },
            ]),
            active_positions: section([{ ticker: 'PANW', direction: 'LONG', strategy: 'S', entry_price: 1, current_price: 2, unrealized_pnl: 12, unrealized_pnl_pct: 1 }]),
            upcoming_earnings: section([], {
                message: 'The Pre-Earnings scan is out of date: its last successful refresh was 24 days ago (expected every 10 minutes), and every event it held has since passed.',
                source_freshness: { state: 'STALE', age_seconds: 2060224, refresh_seconds: 600 }, stale: true,
                watchlist_earnings: [{ ticker: 'GRRR', days: 1, session: 'After Hours', in_scan: false }],
            }),
            sentiment_shifts: section([{ current_tone: 'risk-on', risk_regime: 'risk-on', generated_at: hoursAgo(72), freshness: 'STALE', stale_after_seconds: 10800, top_catalysts: ['Rate decision'], shift_state: 'UNAVAILABLE' }]),
            major_news: section([
                { id: 'n1', ticker: 'NVDA', title: '<img src=x onerror=alert(1)> Chip guidance', source: 'wire<b>', url: 'javascript:alert(1)', published_at: hoursAgo(5), impact: 'CRITICAL', sentiment: 'bearish' },
                { id: 'n2', ticker: 'AAPL', title: 'Second item', source: 'wire', url: 'https://example.com/a', published_at: hoursAgo(30), impact: 'HIGH', sentiment: 'neutral' },
            ], { window_hours: 72, dropped_stale: 9, dropped_duplicates: 1 }),
            ai_decisions: section([
                { ticker: 'PEPE-USD', timestamp: hoursAgo(72), event_type: 'decision_rejected', status: 'rejected', what_happened: 'WATCH (rejected)' },
                { ticker: 'SOXLB-USD', timestamp: hoursAgo(500), event_type: 'decision_accepted', status: 'accepted', what_happened: 'OPEN_LONG (accepted)' },
                { ticker: 'IOST-USD', timestamp: hoursAgo(501), event_type: 'decision_accepted', status: 'closed', what_happened: 'OPEN_LONG (closed)' },
            ], { total_entries: 2699 }),
            strategy_degradation: section([], { message: 'No Strategy Registry record is currently marked DEGRADED.' }),
            research_activity: { state: 'SUCCESS', activity: [{ hypothesis: 'H1', experiment: 'e', status: 'PROPOSED', last_run: hoursAgo(1) }], capabilities: [] },
        },
        strategy_lab: { state: 'SUCCESS', leaderboard: [{ strategy_id: 'S1', validation_status: 'unverified', sample_size: 1, sample_sufficient: false, expectancy_r: 11.46 }], leaderboard_min_sample: 10, records: [], ai_quant_commentary: { state: 'EMPTY', message: 'none' } },
        decision_ledger: section([
            { decision_id: 'd1', ticker: 'PUMP-USD', timestamp: hoursAgo(1), status: 'rejected', what_happened: 'NO_TRADE (rejected)', ai_proposal: { action: 'NO_TRADE' }, critic: { available: true, veto: true }, risk: { accepted: true, checks: [] } },
            { decision_id: 'd2', ticker: 'AAA', timestamp: hoursAgo(2), status: 'rejected', what_happened: 'WATCH (rejected)', ai_proposal: { action: 'WATCH' }, risk: { accepted: true, checks: [] } },
        ], { coverage: 'PERSISTENT_LEDGER', total_entries: 2699 }),
        decision_replay: section([]), model_lab: section([]),
        research_factory: { state: 'SUCCESS', activity: [], capabilities: [], auto_cycle: { enabled: true } },
        system_health: { state: 'SUCCESS', overall: 'UNHEALTHY', summary: '1 of 3 checks need attention: Paper Broker.', failing, services, counts: {}, coverage_gaps: [] },
        source_errors: {}, deferred: {},
    };
}

// init() starts the first load itself; let its promise chain finish.
const settle = async () => { for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve)); };

(async () => {
    const ok = boot({
        '/api/cockpit': { body: payload() },
        '/api/development/repair-agent': { body: { activity: {}, watchdog: {}, summary: { owner_actions: 1 }, cases: [], proposals: [],
            owner_actions: [{ case_id: 'c1', title: 'SEC is NOT_CONFIGURED — owner action needed', problem: 'Set EDGAR_IDENTITY in the .env file for news_tab_agent.py' }] } },
    });
    await settle();
    const overview = ok.html('cockpit-panel-overview');
    const kpis = ok.html('cockpit-kpis');
    const health = ok.html('cockpit-panel-health');
    const everything = overview + kpis + health + ok.html('cockpit-panel-strategy') + ok.html('cockpit-panel-ledger') + ok.html('cockpit-panel-research');
    if (process.env.COCKPIT_DUMP) {
        fs.writeFileSync(process.env.COCKPIT_DUMP, [overview, kpis, health, (ok.elements.get('cockpit-error-message') || {}).textContent].join('\n=====\n'));
    }

    // Internal transport states are never printed on a chip.
    assert(!/SUCCESS\s*·/.test(everything), 'no "SUCCESS · n" chip');
    assert(!/>(SUCCESS|EMPTY|LOADING)</.test(everything), 'no raw state word on a chip');
    assert(overview.includes('>2 risks<'), 'risk chip counts risks');
    assert(overview.includes('is-success is-attention'), 'risk chip is not green');
    assert(overview.includes('>None right now<'), 'empty opportunities chip is plain language');

    // Risks tell the owner what to do.
    assert(overview.includes('Paper order ledger conflict (1)') && overview.includes('Reconcile broker'), 'risk row carries the action');

    // Unhealthy system: the failing check is listed (not "source unavailable").
    assert(!overview.includes('This verified source is unavailable'), 'health card must not collapse into a source error');
    assert(overview.includes('1 of 3 need attention'), 'health chip names how many checks fail');
    assert(overview.indexOf('Paper Broker') < overview.indexOf('Backend'), 'failing check is listed first');
    assert(kpis.includes('1 of 3 checks need attention: Paper Broker'), 'health KPI names the failing check');
    assert(!kpis.includes('real checks'), 'old KPI wording removed');
    assert(health.includes('cockpit-health-failing') && health.includes('Reconcile broker'), 'health tab explains failure and action');

    // Stale narrative is not "current".
    assert(overview.includes('Last recorded tone') && !overview.includes('Current tone'), 'stale narrative is labelled as last recorded');
    assert(overview.includes('3d ago'), 'stale narrative shows its age');
    assert(kpis.includes('Market regime (last known)') && kpis.includes('STALE'), 'regime KPI is marked stale');

    // Stale, empty Pre-Earnings scan tells the truth.
    assert(overview.includes('out of date') && !overview.includes('current verified snapshot'), 'pre-earnings empty state is honest');
    assert(overview.includes('>Scan out of date<'), 'pre-earnings chip says the scan is out of date');

    // News: window note, ages, escaping, unsafe URL dropped.
    assert(overview.includes('published in the last 72 hours') && overview.includes('9 older item(s) withheld'), 'news window is stated');
    assert(overview.includes('5h ago'), 'news shows its age');
    assert(!overview.includes('<img src=x') && overview.includes('&lt;img src=x'), 'news title is escaped');
    assert(!overview.includes('javascript:alert'), 'unsafe news URL is not rendered');
    assert(overview.includes('href="https://example.com/a"'), 'safe news URL is linked');

    // Decisions in plain words, with their age.
    assert(overview.includes('PEPE-USD · Entry declined · AI verdict: WATCH'), 'WATCH decision wording (status = fate of the entry, action = AI verdict)');
    assert(overview.includes('SOXLB-USD · Long entry approved · no position recorded'), 'accepted-without-position wording');
    assert(overview.includes('IOST-USD · Opened long · position since closed'), 'closed position wording');
    assert(!overview.includes('decision rejected'), 'raw event type is not shown');
    assert(overview.includes('latest 3 of 2,699') || overview.includes('latest 3 of 2.699') || overview.includes('latest 3 of 2699'), 'decision chip shows window vs total');

    // Daily P/L names the day boundary.
    assert(kpis.includes('Realized P/L today') && kpis.includes('Europe/Bucharest') && !kpis.includes('UTC realized'), 'daily P/L basis');

    // Strategy Lab: a one-trade row is flagged, not presented as a ranked edge.
    assert(ok.html('cockpit-panel-strategy').includes('too few trades'), 'single-trade leaderboard row is flagged');

    // Ledger: the diagnosis names AI/critic stops and the current readiness block.
    const ledger = ok.html('cockpit-panel-ledger');
    assert(ledger.includes('AI review recommended No trade / Watch') && ledger.includes('2 / 2'), 'ledger counts AI declines');
    assert(ledger.includes('Independent critic vetoed the entry'), 'ledger counts critic vetoes');
    assert(ledger.includes('Right now no new Paper Auto position can open'), 'ledger states the current readiness block');
    assert(!ledger.includes('No failed Risk checks appear in the visible window'), 'no false "nothing blocks" line');

    // Coding Agent: owner instructions are shown verbatim (identifiers intact).
    const repair = ok.html('cockpit-panel-repair');
    assert(repair.includes('Set EDGAR_IDENTITY in the .env file for news_tab_agent.py'), 'owner instruction keeps identifiers');
    assert(!repair.includes('set edgar identity'), 'owner instruction is not lower-cased');

    // A refused action is reported, not swallowed.
    const refused = boot({
        '/api/cockpit': { body: payload() },
        '/api/development/repair-agent': { body: { activity: {}, watchdog: {}, summary: {}, cases: [], proposals: [] } },
        '/api/research/auto-cycle/stop': { status: 403, body: { detail: 'Owner confirmation required' } },
    });
    await settle();
    assert(source.includes('postAction(url)') && source.includes('reportActionError'), 'toggle uses the checked POST helper');
    assert(!/fetch\(url, \{ method: 'POST'[^)]*\)\s*\.catch\(\(\) => null\)/.test(source), 'toggle errors are no longer swallowed');

    console.log('Cockpit: chips, health, staleness, news, decisions, daily P/L and action errors: PASS');
})().catch((error) => { console.error(error); process.exit(1); });
