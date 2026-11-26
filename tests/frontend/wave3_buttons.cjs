// Wave 3 — "button-verification": defects found by operating the app like a user on a
// clean-install instance (own data folder, port 8004), 2026-11-15.
// Runs the real code from frontend/*.js in a vm with minimal fake DOMs.
//   node tests/frontend/wave3_buttons.cjs
const fs = require('fs'), vm = require('vm'), path = require('path'), assert = require('assert/strict');
const read = (name) => fs.readFileSync(path.resolve(__dirname, '../../frontend', name), 'utf8');
const source = read('app.js');
const agentsSource = read('agents.js');

function slice(startMarker, endMarker, text = source) {
    const start = text.indexOf(startMarker);
    const end = text.indexOf(endMarker, start + startMarker.length);
    assert(start >= 0 && end > start, `markers not found: ${startMarker} .. ${endMarker}`);
    return text.slice(start, end);
}

// ============================================================ 1. API base URL
// A page opened at http://localhost:<port> rewrote every API call to
// http://127.0.0.1:<port>: cross-origin. On any port but 8003 the reads were
// blocked by CORS (News / Trade Radar / Premarket "could not be loaded"); on
// 8003 every write was refused with 403 "Cross-site requests cannot change state".
{
    const apiHead = slice('const API = {', '_stateCache:');
    assert(/BASE_URL: '\/api',/.test(apiHead), 'API.BASE_URL must be the same-origin /api');
    assert(!/BASE_URL:[^\n]*127\.0\.0\.1/.test(apiHead), 'no rewrite of localhost to 127.0.0.1');
    assert(!/\/\/127\.0\.0\.1:\$\{/.test(source), 'no other place builds a 127.0.0.1 API URL');
}

// ================================================= 2. status badge on a cache miss
// Premarket / Pre-earnings showed a green "CACHE_ONLY_MISS" chip on a first run.
{
    const code = slice('UI._agentBadgeClass = function', 'UI.initNewsUIStateFromSettings = function');
    const nodes = {};
    const ctx = {
        UI: { _toDateOrNull: () => null, _fmtDateTime: () => '' },
        document: { getElementById: (id) => (nodes[id] = nodes[id] || { innerHTML: '' }) },
    };
    vm.createContext(ctx);
    vm.runInContext(code, ctx);
    const U = ctx.UI;
    assert.equal(U._agentBadgeClass('CACHE_ONLY_MISS'), 'agent-badge--paused');
    assert.equal(U._agentBadgeClass('OK'), 'agent-badge--ok');
    assert.equal(U._agentBadgeClass('ERROR'), 'agent-badge--error');

    U.renderAgentStatusHeader('pm', { status: { state: 'CACHE_ONLY_MISS' }, disabled: true });
    assert(nodes.pm.innerHTML.includes('>Agent off<'), nodes.pm.innerHTML);
    assert(!nodes.pm.innerHTML.includes('CACHE_ONLY_MISS'), 'the internal code is not shown');
    assert(!nodes.pm.innerHTML.includes('agent-badge--ok'), 'no data is not a healthy state');

    U.renderAgentStatusHeader('pe', { status: { state: 'CACHE_ONLY_MISS' }, disabled: false });
    assert(nodes.pe.innerHTML.includes('>No data yet<'), nodes.pe.innerHTML);

    U.renderAgentStatusHeader('ok', { status: { state: 'OK' } });
    assert(nodes.ok.innerHTML.includes('agent-badge--ok') && nodes.ok.innerHTML.includes('>OK<'));
}

// ====================================================== 3. toast states its type
// The skin matched the type on the inline colour ("#ef4444"); the browser stores it
// as "rgb(239, 68, 68)", so an error toast looked exactly like a success toast.
{
    const code = slice("    showToast(message, type = 'info', options = null) {", '    renderStats(state = {}) {');
    const made = [];
    const ctx = {
        setTimeout: () => 0,
        document: {
            getElementById: () => null,
            createElement: () => {
                const node = {
                    style: {}, attrs: {}, children: [],
                    setAttribute(k, v) { this.attrs[k] = v; },
                    appendChild(c) { this.children.push(c); },
                    remove() {},
                };
                made.push(node);
                return node;
            },
            body: { appendChild() {} },
        },
    };
    vm.createContext(ctx);
    vm.runInContext(`var UI = { ${code} };`, ctx);
    const toastOf = (type) => { made.length = 0; ctx.UI.showToast('message', type); return made[0]; };

    const error = toastOf('error');
    assert.equal(error.attrs['data-type'], 'error');
    assert.equal(error.attrs.role, 'alert');
    assert.equal(error.style.borderLeftColor, 'var(--ds-neg, #ef4444)');
    assert.equal(error.textContent, 'message');

    const success = toastOf('success');
    assert.equal(success.attrs.role, 'status');
    assert.equal(success.style.borderLeftColor, 'var(--ds-pos, #10b981)');
    assert.notEqual(success.style.borderLeftColor, error.style.borderLeftColor);

    assert.equal(toastOf('warning').attrs.role, 'alert');
    assert.equal(toastOf('nonsense').attrs['data-type'], 'info');
    assert.equal(toastOf(undefined).attrs['data-type'], 'info');
}

// ============================================== 4. Alerts opened by link or reload
// The list waited for the cached state and stayed on "Loading alerts…" for good.
{
    const branch = slice('// Explicit handling for ALERTS view', '    if (originalNavigate) {');
    assert(/\n\s+UI\.renderAlerts\(cachedState\);/.test(branch), 'the alerts list is always loaded');
    assert(!/if \(cachedState\) UI\.renderAlerts/.test(branch), 'no cached-state guard before loading the list');
}

// ======================================================= 5. per-ticker settings
// The dialog existed with no control that opens it; its save used alert().
{
    const actions = slice('<div class="wl-expand-actions">', '</div>');
    assert(actions.includes('Actions.openTickerSettings('), 'expanded row opens the ticker settings');
    assert(actions.includes('data-testid="wl-ticker-settings"'));
    const save = slice('    async saveTickerSettings(e) {', '    // --- Global Watchlist Settings ---');
    assert(!save.includes('alert('), 'failures are reported in a toast, not alert()');
    assert(save.includes('res.ok && data.status ==='), 'an HTTP error is not a save');
    assert(/were not saved/.test(save));
}

// ============================================ 6. Escape closes the topmost dialog
{
    const code = slice('UI._topOpenDialog = function () {', 'UI.renderDetailedWatchlist = function');
    const listeners = [];
    const mk = (id, z, { hidden = false, display = '', rects = 1, close = true } = {}) => {
        const node = {
            id, z, clicked: 0,
            classList: { contains: (c) => c === 'hidden' && hidden },
            style: { display },
            getClientRects: () => new Array(rects),
            querySelector: () => (close ? { click() { node.clicked += 1; } } : null),
        };
        return node;
    };
    let nodes = [];
    const ctx = {
        UI: {},
        window: { getComputedStyle: (node) => ({ zIndex: String(node.z) }) },
        document: {
            querySelectorAll: () => nodes,
            addEventListener: (type, fn) => listeners.push(fn),
        },
    };
    vm.createContext(ctx);
    vm.runInContext(code, ctx);
    const press = (key = 'Escape', defaultPrevented = false) => {
        const event = { key, defaultPrevented, prevented: false, preventDefault() { this.prevented = true; } };
        listeners.forEach((fn) => fn(event));
        return event;
    };

    const settings = mk('global-settings-modal', 1000);
    const day = mk('calendar-modal', 1000);
    const eventModal = mk('cal-event-modal', 1100);
    nodes = [mk('add-ticker-modal', 1000, { hidden: true }), settings];
    assert.equal(ctx.UI._topOpenDialog(), settings);
    assert.equal(press().prevented, true);
    assert.equal(settings.clicked, 1);

    // stacked dialogs: only the top one closes
    nodes = [day, eventModal];
    press();
    assert.equal(eventModal.clicked, 1);
    assert.equal(day.clicked, 0);

    // dialogs with their own Escape handler are not touched
    const workspace = mk('chart-workspace-modal', 2000);
    const confirmModal = mk('delete-confirm-modal', 2000);
    nodes = [workspace, confirmModal];
    assert.equal(ctx.UI._topOpenDialog(), null);
    assert.equal(press().prevented, false);
    assert.equal(workspace.clicked + confirmModal.clicked, 0);

    // not open: hidden class, display none, or not rendered
    nodes = [mk('a', 1, { hidden: true }), mk('b', 1, { display: 'none' }), mk('c', 1, { rects: 0 })];
    assert.equal(ctx.UI._topOpenDialog(), null);

    // another handler already used this Escape (command palette), or another key
    nodes = [settings];
    press('Escape', true);
    press('Enter');
    assert.equal(settings.clicked, 1);
}

// ================================= 7. Run signal cycle with the candidate worker off
// The button answered "cycle triggered" while nothing reads the queued request.
{
    const monitor = slice('const cycleGate = resolveEntryGate(', '// A cycle result only exists once the engine recorded one.', agentsSource);
    assert(monitor.includes("gate.key === 'candidate_worker'"));
    assert(/\|\| workerOff;/.test(monitor), 'the button is disabled while the worker is off');
    assert(monitor.includes('(workerOff ? workerGate : null)'), 'and the reason names the worker');
    assert(agentsSource.includes('There is no switch for this in the app: set agents.execution.autonomous_candidates_enabled to true in config.json.'));
    assert(!agentsSource.includes('Enable autonomous candidates in the execution configuration.'));
}

console.log('wave3_buttons: all checks passed');
