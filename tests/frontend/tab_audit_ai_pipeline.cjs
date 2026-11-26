// AI pipeline tab audit 2026-11-15 — regression tests for the per-ticker
// "Analyze" modal in frontend/app.js (Watchlist / Hot List / Trade Radar).
// Run: node tests/frontend/tab_audit_ai_pipeline.cjs
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(require('path').resolve(__dirname, '../../frontend/app.js'), 'utf8');

const start = source.indexOf('Actions.analyzeTicker = async function');
const end = source.indexOf('Actions.switchAnalysisTab = function', start);
assert(start >= 0 && end > start, 'analysis modal section markers not found');
const escStart = source.indexOf('UI._escapeHtml = function');
const escEnd = source.indexOf('};', escStart) + 2;
assert(escStart >= 0, '_escapeHtml not found');

// ---- minimal DOM -----------------------------------------------------------
const makeEl = (id) => {
    const classes = new Set();
    return {
        id, innerHTML: '', style: {}, disabled: false,
        classList: {
            add: (c) => classes.add(c), remove: (c) => classes.delete(c),
            contains: (c) => classes.has(c), toggle() {},
        },
        appendChild() {},
    };
};
const elements = {};
['analysis-modal', 'analysis-modal-title', 'analysis-refresh-btn', 'analysis-tabs', 'analysis-modal-body']
    .forEach(id => { elements[id] = makeEl(id); });

const toasts = [];
let fetchImpl = null;
const fetchLog = [];
// UI helpers the technical/fundamental tabs call are not under test here:
// anything not defined explicitly renders as an empty string.
const uiBase = {
    showToast: (msg, type) => toasts.push([msg, type]),
    formatPrice: (v) => Number(v).toFixed(2),
};
const UI = new Proxy(uiBase, {
    get: (target, prop) => (prop in target ? target[prop] : (() => '')),
});
const Actions = new Proxy({}, {
    get: (target, prop) => (prop in target ? target[prop] : (() => '')),
});

const context = {
    UI, Actions,
    API: { fetchWithTimeout: (url, opts) => { fetchLog.push({ url, opts }); return fetchImpl(url, opts); } },
    cachedState: {},
    document: {
        getElementById: (id) => elements[id] || null,
        querySelectorAll: () => [],
        createElement: () => makeEl('new'),
        body: { appendChild() {} },
    },
    window: {},
    localStorage: { getItem: () => null, setItem() {} },
    console: { error() {}, warn() {}, log() {} },
    AbortController, setTimeout, clearTimeout, encodeURIComponent, Date, JSON, Math, Number, String, Array, Object,
};
vm.createContext(context);
vm.runInContext(source.slice(escStart, escEnd), context, { filename: 'app.js#escape' });
vm.runInContext(source.slice(start, end), context, { filename: 'app.js#analysis-modal' });

const body = () => elements['analysis-modal-body'].innerHTML;
const title = () => elements['analysis-modal-title'].innerHTML;
const jsonResponse = (payload, ok = true, status = 200) => ({ ok, status, json: async () => payload });
const deferred = () => { let resolve, reject; const promise = new Promise((res, rej) => { resolve = res; reject = rej; }); return { promise, resolve, reject }; };
const abortable = (signal, pending) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => { const e = new Error('The operation was aborted'); e.name = 'AbortError'; reject(e); });
    pending.promise.then(resolve, reject);
});

const GOOD = {
    ticker: 'NVDA', price: 123.45, change_pct: 1.2, rsi: 55, macd_hist: 0.5,
    trend_1d: 'BULL', mode_scores: {}, timestamp: new Date(Date.now() - 37 * 60000).toISOString(),
    ai_analysis: null, model: 'Technical',
};

(async () => {
    // 1. A 200 response that carries `error` and placeholder zeros is a FAILED analysis.
    //    It used to be rendered as "Current Price $0.00 / RSI 50.0 Neutral / MACD Bearish".
    fetchImpl = async () => jsonResponse({
        ticker: 'ZZZT', error: 'Could not fetch data for ZZZT', price: 0, change_pct: 0, rsi: 50, macd_hist: 0,
        mode_scores: {}, ai_analysis: null,
    });
    await Actions.analyzeTicker('ZZZT');
    assert.match(body(), /Analysis failed/);
    assert.match(body(), /Could not fetch data for ZZZT/);
    assert.doesNotMatch(body(), /Current Price/);
    assert.equal(Actions._analysisAbort, null);
    assert.equal(Actions.analysisFailure(GOOD), '');
    assert.equal(Actions.analysisFailure(null), 'The server returned no analysis.');
    // an `error` next to a real price is a partial result, not a failure
    assert.equal(Actions.analysisFailure({ error: 'AI offline', price: 10 }), '');

    // 2. HTTP failure shows the failed state with the status, escaped.
    fetchImpl = async () => jsonResponse({}, false, 502);
    await Actions.analyzeTicker('NVDA');
    assert.match(body(), /Analysis failed \(HTTP 502\)/);

    // 3. Starting ticker B while A is still running: A's abort must not paint
    //    "A — Analysis failed (cancelled)" over B's spinner, and must not clear
    //    B's abort handle.
    const pendingA = deferred(), pendingB = deferred();
    fetchImpl = (url, opts) => abortable(opts.signal, JSON.parse(opts.body).ticker === 'AAA' ? pendingA : pendingB);
    const runA = Actions.analyzeTicker('AAA');
    await Promise.resolve();
    const runB = Actions.analyzeTicker('BBB');
    await runA;                                   // A was aborted by B
    assert.match(body(), /Analysing BBB/, 'superseded request overwrote the modal');
    assert.doesNotMatch(body(), /cancelled/);
    assert.notEqual(Actions._analysisAbort, null, 'superseded request cleared the live abort handle');
    assert.equal(Actions._currentAnalysisTicker, 'BBB');
    pendingB.resolve(jsonResponse({ ...GOOD, ticker: 'BBB' }));
    await runB;
    assert.match(title(), /BBB - Deep Analysis/);
    assert.equal(Actions._analysisAbort, null);

    // 4. Cancel replaces the spinner with the cancelled state (the aborted
    //    request no longer owns the modal, so it cannot do it itself).
    const pendingC = deferred();
    fetchImpl = (url, opts) => abortable(opts.signal, pendingC);
    const runC = Actions.analyzeTicker('CCC');
    await Promise.resolve();
    assert.match(body(), /Analysing CCC/);
    Actions.cancelAnalysis();
    await runC;
    assert.match(body(), /Analysis was cancelled by the user/);
    assert.doesNotMatch(body(), /fa-spinner/);

    // 5. A cached result says how old it is and at which price it was made.
    assert.equal(Actions.analysisAgeLabel(new Date(1_000_000_000_000).toISOString(), 1_000_000_000_000 + 30_000), 'just now');
    assert.equal(Actions.analysisAgeLabel(new Date(1_000_000_000_000).toISOString(), 1_000_000_000_000 + 37 * 60000), '37 min ago');
    assert.equal(Actions.analysisAgeLabel(new Date(1_000_000_000_000).toISOString(), 1_000_000_000_000 + 125 * 60000), '2 h 05 min ago');
    assert.equal(Actions.analysisAgeLabel(new Date(1_000_000_000_000).toISOString(), 1_000_000_000_000 + 3 * 1440 * 60000), '3 d ago');
    assert.equal(Actions.analysisAgeLabel('', 0), '');
    assert.equal(Actions.analysisAgeLabel('not a date', 0), '');
    Actions.showAnalysisModal('NVDA', { ...GOOD, cached: true });
    assert.match(title(), /CACHED/);
    assert.match(title(), /analysed 37 min ago at \$123\.45/);
    Actions.showAnalysisModal('NVDA', { ...GOOD, cached: true, timestamp: undefined });
    assert.match(title(), /analysis time unknown/);

    // 6. The loading text states the real cache lifetime, in English.
    Actions.showAnalysisModal('NVDA', { _loading: true });
    assert.match(body(), /cached for up to 1 hour/);
    assert.doesNotMatch(body(), /2 hours|Se proceseaza|Poate dura/);

    // 7. Error text and ticker are escaped.
    Actions.showAnalysisModal('<img src=x>', { _error: true, _errorMsg: '<script>alert(1)</script>' });
    assert.doesNotMatch(body(), /<script>/);
    assert.match(body(), /&lt;script&gt;/);
    assert.doesNotMatch(title(), /<img/);

    // 8. AI tab: model output is escaped and confidence is labelled as self-reported.
    const hostile = '<img src=x onerror=alert(1)>';
    Actions.showAnalysisModal('NVDA', {
        ...GOOD, model: 'glm-5.3-flash:cloud',
        ai_analysis: {
            source: 'ai', sentiment: 'bullish', earnings_play: 'WAIT', confidence: 0.73,
            key_drivers: [hostile], risks: [hostile],
            news_summary: { last_7d_positive: [hostile], last_7d_negative: [], last_30d_theme: hostile, major_risk_events: [hostile] },
            scenario: { bull_case: hostile, base_case: 'b', bear_case: 'c' },
            trade_plan: { direction: 'LONG', entry_condition: hostile, invalidations: 'i', tp_logic: 'ATR', notes: hostile },
        },
    });
    const aiTab = body().slice(body().indexOf('id="tab-ai-ollama"'));
    assert(aiTab.length > 0, 'AI tab not rendered');
    assert.doesNotMatch(aiTab, /<img src=x/, 'model output reached the page unescaped');
    assert.match(aiTab, /&lt;img src=x onerror=alert\(1\)&gt;/);
    assert.match(aiTab, /AI self-rated confidence 73% \(uncalibrated, not a probability\)/);
    assert.doesNotMatch(aiTab, />Confidence 73%</);
    assert.match(aiTab, /glm-5\.3-flash:cloud/);
    assert.doesNotMatch(aiTab, /analysis-ai-fallback-notice/);
    assert.match(aiTab, /Written from the data of this snapshot \(analysed 37 min ago at \$123\.45\)/);

    // 9. The rule-based stand-in is never presented as the model's answer.
    Actions.showAnalysisModal('NVDA', {
        ...GOOD, model: 'glm-5.3-flash:cloud',
        ai_analysis: {
            source: 'deterministic_fallback', sentiment: 'bullish', earnings_play: 'WAIT', confidence: 0.6,
            fallback_reason: 'The AI reply was empty or not valid JSON; this summary was computed by fixed rules.',
            missing_inputs: ['rsi', 'macd_hist'],
            key_drivers: ['Trend context 1D/4H: BULL/UNKNOWN.'], risks: [],
            news_summary: {}, scenario: {}, trade_plan: { direction: 'LONG' },
        },
    });
    const fallbackTab = body().slice(body().indexOf('id="tab-ai-ollama"'));
    assert.match(fallbackTab, /analysis-ai-fallback-notice/);
    assert.match(fallbackTab, /Not an AI answer/);
    assert.match(fallbackTab, /Rule-based summary \(no AI answer\)/);
    assert.match(fallbackTab, /Rule score 60% \(not a probability\)/);
    assert.match(fallbackTab, /Inputs unavailable: rsi, macd_hist/);
    assert.doesNotMatch(fallbackTab, /glm-5\.3-flash:cloud/, 'fallback was attributed to the model');

    // 10. No AI answer at all: say so, do not show a model badge with "real-time analysis".
    Actions.showAnalysisModal('NVDA', { ...GOOD, ai_analysis: null });
    const noneTab = body().slice(body().indexOf('id="tab-ai-ollama"'));
    assert.match(noneTab, /AI unavailable/);
    assert.doesNotMatch(noneTab, /Real-Time Analysis/);

    // 11. A missing MACD is "N/A", not "Bearish".
    Actions.showAnalysisModal('NVDA', { ...GOOD, macd_hist: 0 });
    const macdCard = body().slice(body().indexOf('MACD Hist'), body().indexOf('MACD Hist') + 420);
    assert.doesNotMatch(macdCard, /Bearish/);
    Actions.showAnalysisModal('NVDA', { ...GOOD, macd_hist: -0.4 });
    assert.match(body().slice(body().indexOf('MACD Hist'), body().indexOf('MACD Hist') + 420), /Bearish/);

    dataNoticeChecks();
    console.log('tab_audit_ai_pipeline: all assertions passed');
})().catch((err) => { console.error(err); process.exit(1); });

// 12. Figures the model wrote that are not in its input data, and inputs that
//     were missing, are disclosed in the AI tab (server-side §43 check).
function dataNoticeChecks() {
    Actions.showAnalysisModal('NVDA', {
        ...GOOD, model: 'glm-5.3-flash:cloud',
        ai_analysis: {
            source: 'ai', sentiment: 'bullish', earnings_play: 'WAIT', confidence: 0.7,
            numbers_checked: true, ungrounded_numbers: ['$150', '8.5%'], missing_inputs: ['technical.adx', 'earnings.days'],
            key_drivers: ['Target $150'], risks: [], news_summary: {}, scenario: {}, trade_plan: {},
        },
    });
    const tab = elements['analysis-modal-body'].innerHTML;
    assert.match(tab, /analysis-ai-data-notice/);
    assert.match(tab, /Figures not found in the data given to the model:<\/strong> \$150, 8\.5%/);
    assert.match(tab, /Inputs that were unavailable when the model was asked:<\/strong> technical\.adx, earnings\.days/);
    Actions.showAnalysisModal('NVDA', {
        ...GOOD, ai_analysis: { source: 'ai', confidence: 0.7, numbers_checked: true, ungrounded_numbers: [], missing_inputs: [],
            key_drivers: ['d'], risks: [], news_summary: {}, scenario: {}, trade_plan: {} },
    });
    assert.doesNotMatch(elements['analysis-modal-body'].innerHTML, /analysis-ai-data-notice/);
}
