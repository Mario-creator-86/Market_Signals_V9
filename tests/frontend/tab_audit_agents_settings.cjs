// Regression tests for the 2026-11-15 audit of the Agents tab (frontend/agents.js)
// and the Global Settings AI handlers (frontend/app.js).
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');

const agentsSource = fs.readFileSync(path.resolve(__dirname, '../../frontend/agents.js'), 'utf8');
const appSource = fs.readFileSync(path.resolve(__dirname, '../../frontend/app.js'), 'utf8');

// ── minimal DOM ──────────────────────────────────────────────────────────────
function makeElement(id) {
    return {
        id,
        innerHTML: '',
        textContent: '',
        className: '',
        disabled: false,
        value: '',
        children: [],
        dataset: {},
        attributes: {},
        classList: { toggle() {}, add() {}, remove() {} },
        setAttribute(name, value) { this.attributes[name] = value; },
        querySelectorAll() { return []; },
        querySelector() { return null; },
        insertAdjacentHTML() {},
        remove() {},
    };
}

function makeAgentsPage({ control, execution, pipeline, pipelineFails = false }) {
    const elements = {};
    const ids = [
        'agents-master-panel', 'agents-kpi-grid', 'agents-readiness-panel', 'agents-opportunity-monitor',
        'agents-grid', 'agents-summary', 'agents-control-plane', 'agents-refresh-btn', 'agents-last-updated',
        'agents-empty', 'agents-live-region',
    ];
    ids.forEach((id) => { elements[id] = makeElement(id); });
    const toasts = [];
    const json = (body, ok = true) => ({ ok, status: ok ? 200 : 500, json: async () => body });
    const context = {
        UI: { showToast: (message, kind) => toasts.push({ message, kind }) },
        Actions: {},
        API: {
            fetchWithTimeout: async (url) => {
                if (url === '/api/agents/control-plane') return json(control);
                if (url === '/api/execution/control') return json(execution);
                if (url === '/api/agents/risk/pipeline-status') {
                    return pipelineFails ? json({ detail: 'Signal monitor store unreadable' }, false) : json(pipeline);
                }
                throw new Error(`unexpected request ${url}`);
            },
            isNetworkFailure: () => false,
            _warnNetworkOnce() {},
        },
        document: {
            getElementById: (id) => elements[id] || null,
            querySelectorAll: () => [],
            addEventListener() {},
            body: { classList: { add() {}, remove() {}, toggle() {} } },
        },
        console: { warn() {}, error() {}, log() {} },
        Intl, Date, Math, Number, String, Array, Object, JSON, Promise, Set, Map,
    };
    context.window = { setTimeout: () => 0, confirm: () => true };
    vm.createContext(context);
    vm.runInContext(agentsSource, context);
    return { context, elements, toasts };
}

const iso = (secondsAgo) => new Date(Date.now() - secondsAgo * 1000).toISOString();

function basePayload({ gateOpen }) {
    const blocker = {
        key: 'broker_reconciliation',
        label: 'Paper broker reconciliation',
        ready: false,
        detail: 'EXECUTION_STATE_CONFLICT: 1 order-ledger conflict(s) with Capital positions: filled order position missing (ord_1, pos_1)',
        action: 'Open Risk Manager and press Reconcile broker.',
    };
    const readyGate = (key, label) => ({ key, label, ready: true, detail: 'ok', action: '' });
    const gates = [
        readyGate('market_engine', 'Market engine'),
        readyGate('master_switch', 'Bot master switch'),
        readyGate('execution_agent', 'Execution Agent'),
        gateOpen ? readyGate('broker_reconciliation', 'Paper broker reconciliation') : blocker,
    ];
    const blockers = gateOpen ? [] : [blocker];
    const masterBound = gateOpen
        ? { accepting_new_entries: true, operating_mode: 'entry_enabled' }
        : { accepting_new_entries: false, operating_mode: 'monitoring_only', entry_block_reason: 'Paper broker reconciliation' };
    const engine = { enabled: true, running: true, managed_by: 'engine', lifecycle_state: 'running', health: 'ok', overdue: false };
    return {
        bot: { running: true, controller_running: true, mode: 'paper', engine_running: true, circuit_breaker_active: false, min_edge_score: 90 },
        entry_gate: {
            open: gateOpen, status: gateOpen ? 'open' : 'blocked',
            ready_count: gates.length - blockers.length, total: gates.length, gates, blockers,
        },
        agents: {
            execution: { ...engine, ...masterBound, last_heartbeat: iso(4), last_activity_at: iso(4), note: 'event_driven_keepalive' },
            risk: { ...engine, ...masterBound, last_heartbeat: iso(20), last_activity_at: iso(20) },
            scanner: { ...engine, last_heartbeat: iso(3), last_activity_at: iso(3), last_run_duration_ms: 16778.28 },
            sentiment: { ...engine, last_heartbeat: iso(200), last_activity_at: iso(200) },
            trade_radar: {
                enabled: true, running: true, lifecycle_state: 'running', managed_by: 'api',
                last_run: iso(4000), last_success: iso(4000), last_activity_at: iso(4000), next_run_in: 300,
                schedule_interval_seconds: 300, overdue: true, health: 'attention',
                health_reason: 'no completed cycle for 66m (scheduled every 5m)',
            },
            news_tab: {
                enabled: true, running: false, lifecycle_state: 'error', managed_by: 'api',
                last_error: '<img src=x onerror=alert(1)> feed down', health: 'error',
            },
        },
        config: {
            agents: {
                execution: { enabled: true, mode: 'paper', max_open_trades: 40, autonomous_candidates_enabled: true, autonomous_monitor_interval_seconds: 120 },
                risk: { enabled: true },
                scanner: { enabled: true, interval_sec: 60, min_volume_24h: 1000000 },
                sentiment: { enabled: true, provider: 'marketaux', min_sentiment_score: 0.2 },
                trade_radar: { enabled: true },
                news_tab: { enabled: true },
            },
            bot: { circuit_breaker_daily_loss_pct: 10, circuit_breaker_drawdown_pct: 25 },
            risk_constitution: { status: 'active', active: { max_daily_loss_pct: 10, max_risk_per_trade_pct: 4 } },
        },
    };
}

const executionBlocked = {
    readiness: { ready: false, reasons: ['EXECUTION_STATE_CONFLICT must be resolved'] },
    autonomous_allocation: { buckets_pct: { stocks: 50, crypto: 30, pre_earnings: 20 } },
    autonomous_cycle_limits: { candidate_reviews_per_cycle: 0, trade_submissions_per_cycle: 1, scanner_symbols_per_bucket: 50, watchlist_symbols: 100 },
};
const executionReady = { ...executionBlocked, readiness: { ready: true, reasons: [] } };
const pipeline = {
    updated_at: iso(60),
    trigger: 'scheduled',
    result: { cycle: {
        status: 'reviewed', candidate_count: 17, reviewed: 1, submitted: 1,
        sources: {
            scanner: { enabled: true, observation_count: 100, candidate_count: 17 },
            watchlist: { enabled: true, observation_count: 36, stock_observation_count: 36, crypto_observation_count: 0, candidate_count: 1 },
            pre_earnings: { enabled: true, status: 'no_eligible_candidates', evaluated: 25, candidate_count: 0 },
            cross_sectional_momentum: { enabled: false, candidate_count: 0 },
        },
    } },
};

function cardHtml(grid, key) {
    const start = grid.indexOf(`data-agent-key="${key}"`);
    assert(start >= 0, `card ${key} rendered`);
    const end = grid.indexOf('</article>', start);
    return grid.slice(start, end);
}

(async () => {
    // 1. Bot on, broker refuses new risk: every surface says "blocked" and names the gate.
    let page = makeAgentsPage({ control: basePayload({ gateOpen: false }), execution: executionBlocked, pipeline });
    await page.context.UI.renderAgents();
    let master = page.elements['agents-master-panel'].innerHTML;
    assert(master.includes('Bot running · entries blocked'), 'master pill states entries are blocked');
    assert(master.includes('Blocked by Paper broker reconciliation'), 'master card names the blocking gate');
    assert(master.includes('Reconcile broker'), 'master card names the action that clears it');
    assert(!master.includes('entries enabled'), 'master card must not also claim entries are enabled');
    let grid = page.elements['agents-grid'].innerHTML;
    for (const key of ['execution', 'risk']) {
        const card = cardHtml(grid, key);
        assert(card.includes('Running / entries blocked'), `${key} card says entries blocked`);
        assert(!card.includes('Running / entries enabled'), `${key} card must not say entries enabled`);
        assert(card.includes('New entries blocked by Paper broker reconciliation'), `${key} card repeats the gate`);
    }
    let readiness = page.elements['agents-readiness-panel'].innerHTML;
    assert(readiness.includes('3/4 ready'), 'readiness score counts the served gates');
    assert(readiness.includes('data-gate="broker_reconciliation"') && readiness.includes('EXECUTION_STATE_CONFLICT'),
        'the failing gate is listed with its evidence');
    assert(readiness.includes('Open Risk Manager and press Reconcile broker.'), 'the failing gate lists its action');

    // 2. Engine cards show the heartbeat / duration the engine published.
    const scanner = cardHtml(grid, 'scanner');
    assert(scanner.includes('Heartbeat') && scanner.includes('17s'), 'scanner card shows heartbeat age and run duration');
    assert(!grid.includes('Engine managed'), 'no card falls back to the empty "Engine managed" label');
    assert(!scanner.includes('Not measured'), 'a measured run is not shown as "Not measured"');

    // 3. A worker that stopped completing cycles and one in error both need attention.
    const radar = cardHtml(grid, 'trade_radar');
    assert(radar.includes('Running / overdue') && radar.includes('no completed cycle for 66m'), 'overdue worker is flagged with the reason');
    assert(radar.includes('Every 5m'), 'the schedule is shown as an interval, not as a "next run" countdown');
    assert(!grid.includes('Next run'), 'the interval is no longer mislabelled "Next run"');
    const summary = page.elements['agents-summary'].textContent;
    assert(/ 2 need attention$/.test(summary), `summary counts unhealthy workers, got "${summary}"`);
    assert(page.elements['agents-kpi-grid'].innerHTML.includes('need attention'), 'worker-fleet tile reflects unhealthy workers');

    // 4. Sentiment card describes what the worker does, not an unused provider key.
    const sentiment = cardHtml(grid, 'sentiment');
    assert(sentiment.includes('News Flow labels') && !sentiment.includes('marketaux'), 'sentiment card no longer shows the unused provider');

    // 5. Third-party text is escaped.
    assert(!grid.includes('<img src=x'), 'worker error text is escaped');
    assert(grid.includes('&lt;img src=x'), 'escaped error text is still shown');

    // 6. Signal monitor: disabled source, real weights, and why the button is disabled.
    let monitor = page.elements['agents-opportunity-monitor'].innerHTML;
    assert(monitor.includes('Source disabled') && !monitor.includes('Liquidity threshold unavailable'),
        'a switched-off research source is not presented as "awaiting cycle"');
    assert(monitor.includes('Unavailable — Paper broker reconciliation'), 'the disabled Run button explains itself');
    assert(/data-testid="agents-run-v2-cycle"/.test(monitor) && /disabled\s+data-testid="agents-run-v2-cycle"/.test(monitor),
        'Run signal cycle stays disabled while a gate is blocked');
    assert(monitor.includes('>161<'), 'observation total is the sum of the cycle sources');

    // 7. Circuit breaker tile: armed with the enforced limits.
    let kpis = page.elements['agents-kpi-grid'].innerHTML;
    assert(kpis.includes('Armed') && kpis.includes('10% daily loss') && kpis.includes('25% drawdown'), 'breaker tile shows the enforced limits');

    // 8. All gates passed: enabled everywhere, no blocker note.
    page = makeAgentsPage({ control: basePayload({ gateOpen: true }), execution: executionReady, pipeline });
    await page.context.UI.renderAgents();
    master = page.elements['agents-master-panel'].innerHTML;
    grid = page.elements['agents-grid'].innerHTML;
    assert(master.includes('Bot running · entries enabled') && !master.includes('Blocked by'), 'open gate renders as enabled');
    assert(cardHtml(grid, 'execution').includes('Running / entries enabled'), 'execution card agrees with the master card');
    assert(page.elements['agents-readiness-panel'].innerHTML.includes('4/4 ready'), 'all gates ready');

    // 9. Bot stopped: the breaker is not watching, so it is not "Ready".
    const stopped = basePayload({ gateOpen: true });
    stopped.bot.running = false;
    stopped.bot.controller_running = false;
    page = makeAgentsPage({ control: stopped, execution: executionReady, pipeline });
    await page.context.UI.renderAgents();
    kpis = page.elements['agents-kpi-grid'].innerHTML;
    assert(kpis.includes('Standby') && !kpis.includes('>Ready<'), 'breaker tile says standby while the bot is stopped');

    // 10. Older server (no entry_gate): the same panel is derived from readiness.
    const legacy = basePayload({ gateOpen: false });
    delete legacy.entry_gate;
    page = makeAgentsPage({ control: legacy, execution: executionBlocked, pipeline });
    await page.context.UI.renderAgents();
    readiness = page.elements['agents-readiness-panel'].innerHTML;
    assert(readiness.includes('Risk Manager active policy') && readiness.includes('EXECUTION_STATE_CONFLICT must be resolved'),
        'fallback readiness still names the blocking reason');
    assert(page.elements['agents-master-panel'].innerHTML.includes('Blocked by Risk Manager active policy'), 'fallback master card names the gate');

    // 11. Monitor status request failed: no board of zeros.
    page = makeAgentsPage({ control: basePayload({ gateOpen: true }), execution: executionReady, pipeline, pipelineFails: true });
    await page.context.UI.renderAgents();
    monitor = page.elements['agents-opportunity-monitor'].innerHTML;
    assert(monitor.includes('Status unavailable') && monitor.includes('Signal monitor store unreadable'), 'monitor failure is shown as unavailable');
    assert(!monitor.includes('Source observations'), 'no KPI board is drawn from a failed request');

    // 12. No cycle recorded yet: dashes, not zeros.
    page = makeAgentsPage({ control: basePayload({ gateOpen: true }), execution: executionReady, pipeline: {} });
    await page.context.UI.renderAgents();
    monitor = page.elements['agents-opportunity-monitor'].innerHTML;
    assert(monitor.includes('No cycle result recorded yet') && monitor.includes('<strong>—</strong>'), 'missing cycle is not rendered as 0 observations');
    assert(monitor.includes('50/30/20 weights'), 'allocation weights come from the configuration, not a hardcoded 33/33/33');

    // 13. UI Inspector run: a failed request is an error, not "all 0 tabs healthy".
    page = makeAgentsPage({ control: basePayload({ gateOpen: true }), execution: executionReady, pipeline });
    const realFetch = page.context.API.fetchWithTimeout;
    page.context.API.fetchWithTimeout = async (url, options) => (url === '/api/ui-inspector/run'
        ? { ok: false, status: 500, json: async () => ({ detail: 'Inspector crashed' }) }
        : realFetch(url, options));
    await page.context.Actions.runUiInspector(null);
    assert(page.toasts.some((toast) => toast.kind === 'error' && toast.message.includes('Inspector crashed')), 'inspector failure is reported');
    assert(!page.toasts.some((toast) => /tabs healthy/.test(toast.message)), 'a failed inspection never claims healthy tabs');
    page.toasts.length = 0;
    page.context.API.fetchWithTimeout = async (url, options) => (url === '/api/ui-inspector/run'
        ? { ok: true, status: 200, json: async () => ({ tabs_checked: 10, anomalies_detected: 1, escalations_sent: 0, self_healed_count: 0, tab_health: { pre_earnings: { status: 'DEGRADED' } } }) }
        : realFetch(url, options));
    await page.context.Actions.runUiInspector(null);
    assert(page.toasts.some((toast) => toast.kind === 'warning' && toast.message.includes('0 escalated') && toast.message.includes('pre earnings degraded')),
        'anomaly toast reports the real escalation count and the degraded tab');

    // 14. Start bot asks for confirmation; declining sends nothing.
    page = makeAgentsPage({ control: stopped, execution: executionReady, pipeline });
    await page.context.UI.renderAgents();
    const posted = [];
    const baseFetch = page.context.API.fetchWithTimeout;
    page.context.API.fetchWithTimeout = async (url, options = {}) => {
        if (options.method === 'POST') { posted.push(url); return { ok: true, status: 200, json: async () => ({ status: 'started' }) }; }
        return baseFetch(url, options);
    };
    page.context.window.confirm = () => false;
    await page.context.Actions.setBotRunning(true);
    assert.deepEqual(posted, [], 'declined confirmation does not start the bot');
    page.context.window.confirm = () => true;
    await page.context.Actions.setBotRunning(true);
    assert.deepEqual(posted, ['/api/bot/start'], 'confirmed start posts once');

    // ── Global Settings AI handlers (app.js) ────────────────────────────────
    const slice = (startMarker, endMarker) => {
        const start = appSource.indexOf(startMarker);
        const end = appSource.indexOf(endMarker, start);
        assert(start >= 0 && end > start, `source slice ${startMarker}`);
        return appSource.slice(start, end);
    };
    const escapeHtml = (value) => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    const fields = {
        'global-ollama-model': { value: 'gpt-x-large' },
        'global-ollama-timeout': { value: '120' },
        'agent-ai-assignments-container': makeElement('agent-ai-assignments-container'),
        'agent-ai-model-coding_agent': { value: 'kimi-k3:cloud' },
    };
    const requests = [];
    let provider = { name: 'openai', type: 'openai' };
    let assignment = {};
    const settings = {
        UI: { _escapeHtml: escapeHtml, showToast() {}, _selectedGlobalAiProvider: () => provider },
        API: { BASE_URL: '/api' },
        document: { getElementById: (id) => fields[id] || null },
        fetch: async (url, options = {}) => {
            requests.push({ url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null });
            if (url === '/api/ai/assignment' && !options.method) return { ok: true, json: async () => assignment };
            return { ok: true, status: 200, json: async () => ({ models: [] }) };
        },
        window: { GlobalAISelection: { publish() {} } },
        console: { warn() {}, error() {} },
        Number, String, Array, Object, JSON, parseInt,
    };
    vm.createContext(settings);
    vm.runInContext(`const Actions = {${slice('    async saveOllamaModelSelection(options = {}) {', '    async previewGlobalSettings() {')}}; this.Actions = Actions;`, settings);
    vm.runInContext(slice('const AGENT_ROLE_METAS = [', 'UI.testAgentAiModel = async function'), settings);
    vm.runInContext(slice('UI.saveAgentAiAssignments = async function', '// Expose App and Actions globally'), settings);

    // 15. A cloud provider's model must never be written as the Ollama model.
    let saved = await settings.Actions.saveOllamaModelSelection({ silent: true });
    assert.equal(saved.skipped, 'non_ollama_provider');
    assert(!requests.some((request) => request.url === '/api/ollama/set-model'), 'no Ollama write while a cloud provider is selected');
    await settings.UI.saveAgentAiAssignments({ silent: true });
    let sent = requests.find((request) => request.url === '/api/ai/assignment' && request.method === 'POST');
    assert(sent && !('all' in sent.body.assignment), 'cloud model is not sent as the global "all" (which rewrites Ollama)');
    assert.equal(sent.body.assignment.coding_agent, 'kimi-k3:cloud');

    requests.length = 0;
    provider = { name: 'ollama', type: 'ollama' };
    fields['global-ollama-model'].value = 'glm-5.3-flash:cloud';
    saved = await settings.Actions.saveOllamaModelSelection({ silent: true });
    assert(requests.some((request) => request.url === '/api/ollama/set-model' && request.body.model === 'glm-5.3-flash:cloud' && request.body.timeout === 120),
        'the Ollama model is saved while Ollama is selected');
    fields['global-ollama-timeout'].value = 'abc';
    requests.length = 0;
    saved = await settings.Actions.saveOllamaModelSelection({ silent: true });
    assert.equal(saved, null, 'a non-numeric timeout is rejected');
    assert.equal(requests.length, 0, 'nothing is posted with an invalid timeout');
    await settings.UI.saveAgentAiAssignments({ silent: true });
    sent = requests.find((request) => request.url === '/api/ai/assignment' && request.method === 'POST');
    assert.equal(sent.body.assignment.all, 'glm-5.3-flash:cloud', 'Ollama selection is still sent as the global model');

    // 16. Per-agent rows keep a saved model that left the catalog, and escape model ids.
    assignment = { coding_agent: 'kimi-k3:cloud', news_analyst: 'safe-model' };
    await settings.UI.renderAgentAiAssignments(['safe-model', '"><img src=x onerror=1>']);
    const rows = fields['agent-ai-assignments-container'].innerHTML;
    assert(rows.includes('value="kimi-k3:cloud" selected') && rows.includes('not in current catalog'),
        'a saved model missing from the catalog stays selected instead of silently becoming "Default"');
    assert(!rows.includes('<img src=x'), 'model ids are escaped');
    assert(rows.includes('value="safe-model" selected'), 'catalog models still preselect');

    console.log('Agents tab + Global Settings AI handlers: entry gate, health, monitor, inspector, confirmations, AI saves: PASS');
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
