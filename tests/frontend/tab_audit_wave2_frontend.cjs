// Tab audit 2026-11-15, wave 2 — "frontend-consistency".
// Runs the real code from frontend/*.js in a vm with minimal fake DOMs.
//   node tests/frontend/tab_audit_wave2_frontend.cjs
const fs = require('fs'), vm = require('vm'), path = require('path'), assert = require('assert/strict');
const read = (name) => fs.readFileSync(path.resolve(__dirname, '../../frontend', name), 'utf8');
const source = read('app.js');
const cockpitSource = read('cockpit.js');
const skeletonSource = read('skeleton_utils.js');
const indexHtml = read('index.html');

function slice(startMarker, endMarker, text = source) {
    const start = text.indexOf(startMarker);
    const end = text.indexOf(endMarker, start + startMarker.length);
    assert(start >= 0 && end > start, `markers not found: ${startMarker} .. ${endMarker}`);
    return text.slice(start, end);
}
const escapeHtml = (value) => String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
const count = (text, needle) => text.split(needle).length - 1;

// ======================================================= 7. one date helper
const dateHelperSrc = slice('    _fmtDateTime(value', '    initWatchlistQuickAiCache() {');
{
    const ctx = {};
    vm.runInNewContext(`var UI = { ${dateHelperSrc} };`, ctx);
    const U = ctx.UI;
    const morning = new Date(2026, 10, 15, 9, 53, 7);
    assert.equal(U._fmtDateTime(morning), 'Nov 15, 2026, 09:53', 'en-US short month, 24h clock');
    assert.equal(U._fmtDateTime(morning, 'date'), 'Nov 15, 2026');
    assert.equal(U._fmtDateTime(morning, 'time'), '09:53');
    assert.equal(U._fmtDateTime(morning, 'datetime', { seconds: true }), 'Nov 15, 2026, 09:53:07');
    assert.equal(U._fmtDateTime(new Date(2026, 10, 15, 0, 5)), 'Nov 15, 2026, 00:05', 'midnight is 00, not 24');
    assert.equal(U._fmtDateTime(morning.toISOString()), 'Nov 15, 2026, 09:53', 'ISO strings are shown in local time');
    assert.equal(U._fmtDateTime('not a date'), '');
    assert.equal(U._fmtDateTime(null), '');
    assert.equal(U._formatLocalDateTime(morning), 'Nov 15, 2026, 09:53', 'the watchlist helper uses the same notation');
    assert.equal(U._formatLocalDateTime(''), 'unknown');
    assert.equal(U._formatLocalDateTime('garbage'), 'garbage');
    assert(!/\d{2}\.\d{2}\.\d{4}/.test(U._formatLocalDateTime(morning)), 'no dd.mm.yyyy');

    // No call site formats a date with the Romanian locale any more.
    for (const [name, text] of [['app.js', source], ['skeleton_utils.js', skeletonSource], ['cockpit.js', cockpitSource]]) {
        assert(!/toLocale(?:Date|Time)?String\(\s*'ro-RO'/.test(text), `${name} must not hard-code the ro-RO locale`);
    }
    // Shared freshness badge: English tooltip, same notation (with and without app.js loaded).
    const badgeCtx = { window: {}, document: { getElementById: () => null }, Date, setInterval: () => 1, clearInterval() {} };
    vm.createContext(badgeCtx);
    vm.runInContext(`${skeletonSource}\n;globalThis.__badge = FreshnessBadge;`, badgeCtx);
    const ts = new Date(Date.now() - 5000);
    let badge = badgeCtx.__badge.render(ts.toISOString());
    assert(badge.includes('title="Updated ') && !badge.includes('Actualizat'), badge);
    assert(/title="Updated [A-Z][a-z]{2} \d{1,2}, \d{4}, \d{2}:\d{2}:\d{2}"/.test(badge), badge);
    vm.runInContext(`var UI = { ${dateHelperSrc} };`, badgeCtx);
    badge = badgeCtx.__badge.render(ts.toISOString());
    assert(badge.includes(`title="Updated ${U._fmtDateTime(ts, 'datetime', { seconds: true })}"`), 'uses UI._fmtDateTime when app.js is loaded');
}

// ================================================================= 1. NEWS
const newsCode = [
    slice('// ============ NEWS PAGE FUNCTIONS ============', 'UI._capitalExplainabilitySelection = '),
    slice('UI._collectNewsTickerOptions = function', 'UI._syncHomeNewsFilterButtons = function'),
    `;Object.assign(UI, { ${dateHelperSrc} });`,
    ';globalThis.__setNewsCache = (v) => { cachedNewsData = v; };',
    'globalThis.__getNewsCache = () => cachedNewsData;',
    'globalThis.__NewsUIState = NewsUIState;',
].join('\n');

function makeEl(id) {
    const classes = new Set();
    return {
        id, innerHTML: '', textContent: '', value: '', checked: false, hidden: false, title: '',
        dataset: {}, attrs: {}, style: {}, options: [], children: [],
        classList: {
            add: (...c) => c.forEach(x => classes.add(x)),
            remove: (...c) => c.forEach(x => classes.delete(x)),
            toggle: (c, force) => { const on = force === undefined ? !classes.has(c) : !!force; if (on) classes.add(c); else classes.delete(c); return on; },
            contains: c => classes.has(c),
        },
        setAttribute(k, v) { this.attrs[k] = String(v); },
        getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
        removeAttribute(k) { delete this.attrs[k]; },
        querySelectorAll() { return this._query || []; },
        appendChild(child) { this.children.push(child); this.options.push(child); },
        focus() {}, scrollIntoView() {}, remove() {},
    };
}
const els = new Map();
const getEl = (id) => { if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); };
const impactBoxes = ['critical', 'high', 'medium', 'low'].map(level => {
    const box = makeEl(`impact-${level}`); box.checked = true; box.attrs['data-impact'] = level; return box;
});
const store = new Map();
const toasts = [];
let fetchImpl = async () => ({ ok: true, status: 200, json: async () => ({}) });
const newsCtx = {
    console: { log() {}, warn() {}, error() {} },
    IS_DEV: false,
    localStorage: {
        getItem: k => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => { store.set(k, String(v)); },
        removeItem: k => { store.delete(k); },
    },
    document: {
        activeElement: null,
        getElementById: getEl,
        createElement: () => makeEl('created'),
        addEventListener() {},
        querySelectorAll(selector) {
            if (selector === '#view-news input[data-impact]') return impactBoxes;
            if (selector === '#view-news input[data-impact]:not(:checked)') return impactBoxes.filter(b => !b.checked);
            return [];
        },
    },
    ViewRouter: { activeView: 'news' },
    API: {
        getNewsState: async () => null, getNewsTicker: async () => ({ news: [] }),
        getWatchlistCached: async () => ({}), getState: async () => ({ tickers: [] }),
        fetchWithTimeout: async (...args) => fetchImpl(...args),
    },
    Actions: {}, HomeUIState: {},
    fetch: (...args) => fetchImpl(...args),
    setInterval: () => 1, clearInterval() {},
    setTimeout: () => 1, clearTimeout() {},
    UI: {
        showToast: (message, type) => toasts.push({ message: String(message), type }),
        scheduleRender: (key, fn) => fn(),
        formatTimeAgo: () => 'LEGACY-TIME',
        renderNews() {}, mergeStatePreserveDetails: (a) => a, _composeHomeState: () => ({}),
        loadDashboardSnapshot: () => ({}), updateActivityLog() {},
    },
};
newsCtx.window = { innerWidth: 1400, setTimeout: newsCtx.setTimeout, open: () => null };
vm.createContext(newsCtx);
vm.runInContext(newsCode, newsCtx, { filename: 'app.js#news' });
const N = newsCtx.UI, NewsActions = newsCtx.Actions, State = newsCtx.__NewsUIState, setCache = newsCtx.__setNewsCache;

const NOW = Date.now();
const ago = (minutes) => new Date(NOW - minutes * 60000).toISOString();
const art = (id, over = {}) => ({
    id, title: `Headline ${id}`, summary: `Body text for ${id} that says something different from the headline.`,
    url: `https://example.com/${id}`, source: 'yahoo_rss', published_at: ago(30), ticker: 'AAPL',
    category: 'general', sentiment: 'neutral', impact: 'medium', impact_score: 40, action_score: 80,
    is_duplicate: false, is_breaking: false, ai_micro_summary: null, ai_deep_analysis: null,
    content_type: 'news', source_group: 'yahoo_rss', ...over,
});
const social = (id, author, over = {}) => art(id, {
    source: author, content_type: 'social', source_group: 'eToro community', title: `Opinion ${id}`, ...over,
});
function resetNews() {
    els.clear(); store.clear(); toasts.length = 0;
    impactBoxes.forEach(b => { b.checked = true; });
    getEl('news-hide-duplicates').checked = true;
    Object.assign(State, {
        selectedArticleId: null, viewMode: 'split', timeWindow: '24h', focusMode: 'all', selectedSource: 'ALL',
        categories: new Set(['ALL']), selectedTicker: 'ALL', actionabilityThreshold: null, sentimentFilter: 'all',
        briefingActiveId: null, selectionPinned: false, settingsDefaultsApplied: false, deepAnalysisActiveId: null,
        hideSocial: false,
        watchlistSet: new Set(['AAPL']), watchlistOrder: ['AAPL'], watchlistStocks: ['AAPL'], watchlistCrypto: [],
    });
    State.briefingAttempted.clear(); State.briefingFailures.clear(); State.briefingFailureReasons.clear();
    State.briefingUnavailable.clear(); State.briefingCache.clear(); State.extraArticles.clear();
    N._newsStoredIdCache = {};
    if (N._newsTitlePartsCache) N._newsTitlePartsCache.clear();
}
const text = (id) => getEl(id).textContent;
const html = (id) => getEl(id).innerHTML;

// ---- 1a. social marker + "Hide social" toggle (persisted)
resetNews();
{
    assert.equal(N._isNewsSocial({ content_type: 'social', source: '@alice' }), true);
    assert.equal(N._isNewsSocial({ content_type: 'news', source: 'eToro (@x)' }), false, 'the backend field wins over the name rule');
    assert.equal(N._isNewsSocial({ source: 'eToro (@x)' }), true, 'payload without content_type: name fallback');
    assert.equal(N._isNewsSocial({ source: 'yahoo_rss' }), false);

    const data = {
        last_update: ago(1), settings: { news_refresh_minutes: 5 }, breaking_news: [],
        market_news: [social('s1', '@alice', { ticker: null }), social('s2', '@bob', { ticker: null, impact: 'high' })],
        ticker_news: { AAPL: [art('n1'), art('n2', { source: 'marketwatch', source_group: 'marketwatch' }), social('s3', '@carol')] },
    };
    setCache(data);
    N.renderNewsCockpit(data);
    let feed = html('news-page-feed-content');
    assert.equal(count(feed, 'nc2-social-pill'), 3, 'every social post carries the Social marker');
    assert.equal(count(feed, ' news-social'), 3, 'and the secondary card style');
    assert(!N.renderNewsCard(art('plain')).includes('nc2-social-pill'), 'a publisher article is not marked Social');
    assert.equal(text('news-metric-total'), '5', 'social posts are shown by default');
    assert.equal(text('news-social-count'), '(3)');
    assert.equal(getEl('news-hide-social').checked, false);
    assert(html('news-breaking-list').includes('nc2-social-pill'), 'the urgent rail marks a social post too');

    // Sources card / filter follow the backend source_group (authors are not "eToro (...)" here).
    assert.equal(N._newsSourceGroup(data.market_news[0]), 'eToro community');
    assert.equal(N._newsSourceGroup({ source: 'eToro (@x)' }), 'eToro community', 'fallback without source_group');
    assert.equal(N._newsSourceGroup('yahoo_rss'), 'Yahoo Finance', 'a bare source name still works');
    assert.equal(text('news-metric-sources'), '3', 'Yahoo + MarketWatch + one community source');
    assert.equal(text('news-metric-source-note'), 'Most active: eToro community (3)');
    assert(getEl('news-source-select').options.some(o => o.value === 'eToro community'), 'the group is selectable as a filter');
    getEl('news-source-select').value = 'eToro community';
    assert.equal(N._articlePassesFilters(data.market_news[0], data), true);
    assert.equal(N._articlePassesFilters(data.ticker_news.AAPL[0], data), false);
    getEl('news-source-select').value = 'ALL';

    // One toggle hides them everywhere, and the choice is stored.
    N.setNewsHideSocial(true);
    assert.equal(store.get('news_hide_social'), '1');
    feed = html('news-page-feed-content');
    assert.equal(count(feed, 'nc2-social-pill'), 0);
    assert(feed.includes('Headline n1') && !feed.includes('Opinion s1'));
    assert.equal(text('news-metric-total'), '2', 'header counts use the same base as the feed');
    assert.equal(text('news-metric-range'), 'last 24 hours · 3 social hidden');
    assert.equal(text('news-metric-sources'), '2');
    assert.equal(getEl('news-hide-social').checked, true);
    assert.equal(getEl('news-filters-count').textContent, '1', 'counts as an active filter');
    assert(getEl('news-breaking-section').classList.contains('hidden') || !html('news-breaking-list').includes('Opinion s2'),
        'hidden in the urgent rail as well (the rail closes when it was the only urgent item)');
    State.focusMode = 'saved';
    N._newsStoredIdCache = {};
    store.set('saved_news', JSON.stringify(['s1']));
    assert.equal(N._articlePassesFilters(data.market_news[0], data), true, 'a bookmarked post stays in Saved');
    State.focusMode = 'all';
    N.resetNewsFilters();
    assert.equal(State.hideSocial, false);
    assert.equal(store.get('news_hide_social'), '0');
    assert.equal(text('news-metric-total'), '5');
}

// ---- 1b. per-source health and refresh-cycle progress
resetNews();
{
    const base = { last_update: ago(1), settings: { news_refresh_minutes: 5 }, breaking_news: [], market_news: [art('m1', { ticker: null })], ticker_news: {} };
    N.renderNewsCockpit(base);
    assert.equal(getEl('news-source-health').hidden, true, 'no source data in the payload: nothing is invented');
    assert(html('news-agent-status').includes('agent-badge--ok'));
    assert(/Synced <b>[A-Z][a-z]{2} \d{1,2}, \d{4}, \d{2}:\d{2}<\/b>/.test(html('news-agent-status')), 'Synced uses the shared date helper');

    const sources = [
        { name: 'yahoo_markets', label: 'Yahoo Finance (market headlines RSS)', kind: 'news', status: 'ok', last_items: 12, last_success: ago(3) },
        { name: 'marketwatch', label: 'MarketWatch (top stories RSS)', kind: 'news', status: 'failing', last_error: 'HTTP 403 <b>Forbidden</b>', consecutive_failures: 4, last_success: null },
        { name: 'finnhub', label: 'Finnhub (company news API)', kind: 'news', status: 'disabled', disabled_reason: 'finnhub_api_key is not configured; per-ticker company news comes from Yahoo RSS only' },
        { name: 'etoro_market', label: 'eToro feed (social posts, market)', kind: 'social', status: 'disabled', disabled_reason: 'eToro API is not configured' },
        { name: 'etoro_ticker', label: 'eToro feed (social posts, per ticker)', kind: 'social', status: 'disabled', disabled_reason: 'eToro API is not configured' },
        { name: 'coindesk', label: 'CoinDesk (RSS)', kind: 'crypto', status: 'not_run_yet' },
    ];
    const withHealth = {
        ...base, sources,
        source_summary: { level: 'degraded', ok: 1, failing: 1, disabled: 3, not_run_yet: 1, total: 6, message: 'Failing: MarketWatch (top stories RSS). Not configured: Finnhub (company news API).' },
        refresh_coverage: {
            tickers_total: 52, tickers_fresh: 34, tickers_stale: ['MSFT', 'NVDA'], tickers_never_fetched: ['BMNR', 'ETOR', 'IREN'],
            ticker_ttl_seconds: 1800, full_cycle_seconds_estimate: 2100, market_refresh_age_seconds: 240,
            market_refresh_ttl_seconds: 900, last_refresh_error: '',
        },
    };
    const model = N._newsSourceHealthModel(withHealth);
    assert.equal(model.level, 'degraded');
    assert.equal(model.failing.length, 1);
    assert.equal(model.disabled.length, 2, 'both eToro feeds share one "not configured" entry');
    assert.equal(model.cycle.pct, 65);

    N.renderNewsCockpit(withHealth);
    const health = html('news-source-health');
    assert.equal(getEl('news-source-health').hidden, false);
    assert(health.includes('News sources: degraded'));
    assert(health.includes('1 of 3 configured responding'));
    assert(health.includes('<b>Failing:</b> MarketWatch (top stories RSS) — HTTP 403 &lt;b&gt;Forbidden&lt;/b&gt; (4 in a row) · last success never since the server started'),
        'a failing source is named with its last error, escaped');
    assert(!health.includes('<b>Forbidden</b>'));
    assert(health.includes('<b>Not configured:</b> Finnhub (company news API) — finnhub_api_key is not configured'));
    assert(health.includes('<b>Not configured:</b> eToro feed (social posts, market), eToro feed (social posts, per ticker) — eToro API is not configured'));
    assert(health.includes('<b>Not polled yet:</b> CoinDesk (RSS)'));
    assert(health.includes('<b>Refresh cycle:</b> 34/52 tickers fresh (65%) · 3 never fetched · 2 due · full cycle ≈ 35 min · market feeds refreshed 4 min ago'));
    assert(health.includes('Never fetched: BMNR, ETOR, IREN'));
    // ... and the tab badge no longer says OK above it.
    assert(html('news-agent-status').includes('agent-badge--degraded'), 'a failing source degrades the status badge');
    assert(html('news-agent-status').includes('Failing: MarketWatch'));
    assert.equal(getEl('news-last-updated').classList.contains('is-live'), false);

    // Only "not configured" sources: listed, but the tab stays OK (backend level "ok").
    N.renderNewsCockpit({ ...base, sources: [sources[0], sources[2]], source_summary: { level: 'ok', message: '1 of 1 configured sources are responding. Not configured: Finnhub (company news API).' } });
    assert(html('news-agent-status').includes('agent-badge--ok'));
    assert(html('news-source-health').includes('<b>Not configured:</b> Finnhub'), 'an unconfigured source is still visible');
    // Market feeds never refreshed / last cycle error are flagged.
    N.renderNewsSourceHealth({ refresh_coverage: { tickers_total: 0, tickers_fresh: 0, market_refresh_age_seconds: null, last_refresh_error: 'rss <timeout>' } });
    assert(html('news-source-health').includes('market feeds not refreshed yet'));
    assert(html('news-source-health').includes('<b>Last refresh error:</b> rss &lt;timeout&gt;'));
}

// ---- 1c. regime banner: both tones on a conflict, evidence, availability
resetNews();
{
    const note = 'AI read the top headlines as risk-on, but headline sentiment is 15% bullish / 55% bearish across 40 recent market articles; reported as mixed.';
    N.renderMarketNarrative({ settings: {}, market_narrative: {
        tone: 'mixed', risk_regime: 'mixed', ai_tone: 'risk-on', tone_conflict: true, tone_note: note,
        sentiment_mood: { article_count: 40, bullish_pct: 15, bearish_pct: 55, neutral_pct: 30, label: 'bearish tilt' },
        evidence_count: 40, evidence_window_hours: 24, available: true, source: 'ai',
        what_matters_today: ['Fed speakers in focus'], generated_at: ago(4),
    } });
    assert.equal(text('news-narrative-status'), 'mixed · AI risk-on vs headlines bearish tilt', 'both tones, not a flat RISK-ON');
    assert.equal(getEl('news-narrative-status').title, note);
    assert(!String(getEl('news-narrative-status').className).includes('news-regime-risk-on'));
    assert.equal(text('news-narrative-note'), `${note} Evidence: 40 market headlines from the last 24h · AI narrative.`);
    assert.equal(getEl('news-narrative-note').hidden, false);
    assert.equal(getEl('news-narrative-note').classList.contains('is-conflict'), true);
    assert.equal(text('news-market-narrative-content'), 'Fed speakers in focus');

    // No conflict: plain regime plus what it was measured against.
    N.renderMarketNarrative({ settings: {}, market_narrative: {
        tone: 'risk-on', risk_regime: 'risk-on', tone_conflict: false,
        sentiment_mood: { article_count: 20, bullish_pct: 40, bearish_pct: 35, label: 'balanced' },
        evidence_count: 1, evidence_window_hours: 24, available: true, source: 'ai', what_matters_today: ['x'], generated_at: ago(4),
    } });
    assert.equal(text('news-narrative-status'), 'risk-on');
    assert.equal(text('news-narrative-note'), 'Evidence: 1 market headline from the last 24h · AI narrative · headline sentiment 40% bullish / 35% bearish (balanced).');
    assert.equal(getEl('news-narrative-note').classList.contains('is-conflict'), false);
    assert.equal(text('news-narrative-updated'), 'AI narrative · generated 4m ago');

    // Headline list without an AI reading is not presented as an AI regime.
    N.renderMarketNarrative({ settings: {}, market_narrative: {
        tone: 'mixed', risk_regime: 'mixed', evidence_count: 6, evidence_window_hours: 24, available: true, source: 'headline_list',
        what_matters_today: ['MARKET: Stocks slip'], generated_at: ago(2),
    } });
    assert.equal(text('news-narrative-status'), 'no AI reading');
    assert.equal(text('news-narrative-updated'), 'Headline list (no AI) · built 2m ago');
    assert(text('news-narrative-note').includes('headline list only, no AI reading'));

    // No evidence at all: unavailable, with the backend's reason; nothing "generated Just now".
    N.renderMarketNarrative({ settings: {}, market_narrative: {
        tone: 'unknown', risk_regime: 'unknown', evidence_count: 0, evidence_window_hours: 24, available: false, source: 'none',
        note: 'No market-wide headlines in the last 24 hours; no narrative can be derived.',
        what_matters_today: ['No major catalyst detected.'], generated_at: ago(0),
    } });
    assert.equal(text('news-narrative-status'), 'no narrative');
    assert.equal(text('news-market-narrative-content'), 'No market-wide headlines in the last 24 hours; no narrative can be derived.');
    assert.equal(text('news-narrative-note'), 'Narrative unavailable: No market-wide headlines in the last 24 hours; no narrative can be derived.');
    assert.equal(getEl('news-narrative-note').classList.contains('is-unavailable'), true);
    assert.equal(text('news-narrative-updated'), '');

    // A payload without the provenance fields renders as before, with no note line.
    N.renderMarketNarrative({ settings: {}, market_narrative: { risk_regime: 'risk-off', what_matters_today: ['y'], generated_at: ago(4) } });
    assert.equal(text('news-narrative-status'), 'risk-off');
    assert.equal(getEl('news-narrative-note').hidden, true);
}

// ---- 1d. briefing: "AI analysis unavailable" is not an answer; HTTP 502 is a failure with its reason
const newsBriefingTests = async () => {
    resetNews();
    const ESSENTIAL = {
        briefing_version: 3, tldr: 'x', article_explainer: 'The company cut guidance.',
        market_implication: 'Negative read-through.', market_direction: 'risk-off', evidence_scope: 'full-article',
        ticker_implication: { ticker: 'AAPL', has_direct_link: true, relationship: 'Direct.', direction: 'down', rationale: 'Orders.', confidence: 62 },
    };
    assert.equal(N._hasEssentialNewsBriefing(ESSENTIAL), true);
    assert.equal(N._hasEssentialNewsBriefing({ ...ESSENTIAL, ai_status: 'ok' }), true);
    assert.equal(N._hasEssentialNewsBriefing({ ...ESSENTIAL, ai_status: 'unavailable' }), false);
    assert.equal(N._hasEssentialNewsBriefing({ ...ESSENTIAL, ai_status: 'invalid_output' }), false);

    const data = { breaking_news: [], market_news: [], ticker_news: { AAPL: [art('b1', { summary: 'Local summary of the story with its own words.' })] } };
    setCache(data);
    State.selectedArticleId = 'b1';
    const find = () => N._flattenNewsArticles(newsCtx.__getNewsCache()).find(a => a.id === 'b1');
    const realAnalyze = NewsActions.analyzeNews;
    const excerpt = `RAW <b>post</b> text from the source. ${'More of the original post. '.repeat(60)}`;
    NewsActions.analyzeNews = async () => ({
        fallback: true, model: 'pre_computed_metadata',
        analysis: {
            ...ESSENTIAL, ai_status: 'unavailable', ai_unavailable_reason: 'AI model is not reachable',
            source_excerpt: excerpt, source: 'pre_computed_metadata',
            article_explainer: 'AI briefing unavailable: the AI model is not reachable, so this item has not been analysed.',
        },
    });
    await N.ensureEssentialNewsBriefing(find());
    let panel = html('news-insight-panel');
    assert(panel.includes('AI analysis unavailable: AI model is not reachable.'), 'stated as unavailable');
    assert(panel.includes('Source excerpt · AI analysis unavailable'));
    assert(panel.includes('RAW post text from the source.'), 'the backend excerpt is shown, as plain text');
    assert(!panel.includes('<b>post</b>'));
    assert(!panel.includes('Essential answer'), 'never presented as an answer');
    assert(!panel.includes('AI briefing failed'), 'unavailable is not worded as a failed request');
    assert(!panel.includes('Likely down') && panel.includes('Not analysed'), 'no direction from the fallback metadata');
    assert(panel.includes('Source text only (AI analysis unavailable)'));
    const shown = panel.split('<h5>WHAT IS THE NEWS?</h5>')[1].split('<p>')[1].split('</p>')[0];
    assert(shown.length <= 520, `the excerpt is bounded (${shown.length} chars)`);
    assert(State.briefingFailures.has('b1'), 'still retryable');

    // Retry -> the model call fails with HTTP 502: a failure, with the server's reason and detail.
    NewsActions.analyzeNews = realAnalyze;
    fetchImpl = async () => ({ ok: false, status: 502, json: async () => ({
        error: 'AI briefing unavailable: the AI model did not answer.', detail: 'read timeout after 90s', ai_status: 'unavailable',
    }) });
    N.selectNewsArticle('b1');
    await N.ensureEssentialNewsBriefing(find());
    panel = html('news-insight-panel');
    assert.equal(NewsActions._lastNewsAnalysisError, 'the AI model did not answer. (read timeout after 90s)');
    assert(panel.includes('AI briefing failed: the AI model did not answer. (read timeout after 90s)'), panel.slice(0, 400));
    assert(!panel.includes('AI analysis unavailable:'), 'a failed call is not the "unavailable" state');
    assert(panel.includes('Local summary of the story'), 'falls back to the local source excerpt');

    // 502 "invalid output" keeps its own message.
    fetchImpl = async () => ({ ok: false, status: 502, json: async () => ({ error: 'AI briefing unavailable: the model returned no usable analysis. Try again.', ai_status: 'invalid_output' }) });
    N.selectNewsArticle('b1');
    await N.ensureEssentialNewsBriefing(find());
    assert(html('news-insight-panel').includes('AI briefing failed: the model returned no usable analysis. Try again.'));
    fetchImpl = async () => ({ ok: true, status: 200, json: async () => ({}) });
};

// ============================================================== 2. COCKPIT
{
    const fnSrc = slice('const decisionHeadline = (item) => {', 'const decisionStatusLabel', cockpitSource);
    const headline = vm.runInNewContext(
        `const titleWords = (v) => String(v || '').toLowerCase().replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());\n${fnSrc}; decisionHeadline`
    );
    assert.equal(headline({ status: 'rejected', action: 'WATCH', what_happened: 'WATCH (rejected)', event_type: 'decision_rejected' }),
        'Entry declined · AI verdict: WATCH');
    assert.equal(headline({ status: 'rejected', what_happened: 'WATCH (rejected)' }), 'Entry declined · AI verdict: WATCH', 'action parsed from what_happened');
    assert.equal(headline({ status: 'rejected', action: 'NO_TRADE' }), 'Entry declined · AI verdict: NO TRADE');
    assert.equal(headline({ status: 'recorded', action: 'WATCH' }), 'No entry · AI verdict: WATCH');
    assert.equal(headline({ status: 'executed', action: 'OPEN_LONG' }), 'Opened long · position open', 'entries keep their wording');
    assert.equal(headline({ status: 'rejected', action: 'OPEN_SHORT' }), 'Short entry rejected');
    assert(!headline({ status: 'rejected', action: 'WATCH' }).includes('(rejected)'));
}

// ===================================================== 3. PRE-EARNINGS TOP 10
{
    const NOW_PE = Date.parse('2026-11-15T07:54:00Z');
    class FixedDate extends Date {
        constructor(...args) { if (args.length) super(...args); else super(NOW_PE); }
        static now() { return NOW_PE; }
    }
    const peEls = {};
    const peEl = (id) => ({
        id, innerHTML: '', textContent: '', value: '', style: {}, className: '',
        classList: { add() {}, remove() {}, toggle() { return false; }, contains() { return false; } },
        querySelector: () => null, querySelectorAll: () => [], setAttribute() {}, getAttribute() { return null; }, addEventListener() {},
    });
    const ctx = {
        UI: { _escapeHtml: escapeHtml, preEarningsData: { items: [] }, capScrollList() {}, selectPreEarningsRow() {}, _preEarningsLearningReport: null },
        API: { _warnNetworkOnce() {}, getPreEarningsLearningReport: async () => null },
        document: { getElementById: (id) => (peEls[id] || (peEls[id] = peEl(id))), querySelector: () => null, querySelectorAll: () => [] },
        console, Date: FixedDate, Intl, Actions: {}, ViewRouter: { activeView: 'preearnings' }, setTimeout, clearTimeout,
    };
    vm.createContext(ctx);
    vm.runInContext(slice('function _peHitRateBadge(rate) {', 'UI.togglePreEarningsHistoryDays = function'), ctx);
    vm.runInContext(slice('UI._preEarningsBiasScore = function', 'UI.startPreEarningsRefreshPoll = function'), ctx);
    vm.runInContext(slice('UI._preEarningsStructuredScore = function', 'UI.applyCapPreset = function'), ctx);
    vm.runInContext(slice('UI._renderPreEarningsTop10 = function', 'UI.renderPreEarnings = function'), ctx);
    const board = (calendarCount) => {
        ctx.UI._renderPreEarningsTop10({
            ranking: { state: 'FRESH', snapshot_age_seconds: 64, universe_event_count: 670, scored_count: 400, screened_out_count: 8,
                covered_count: 408, coverage_frontier_event_date: '2026-12-01', news_articles_scanned: 135, ai_reviewed_count: 6 },
            flags: { recommendations_stale: false }, calendar_universe: { reporter_count: calendarCount }, top_predictions: [], status: {},
        }, escapeHtml);
        return peEls['preearnings-top10'].innerHTML;
    };
    // Live observation: tile "658 CALENDAR REPORTERS" above "408/670 reporters (61%)".
    let top = board(658);
    assert(top.includes('Staged calendar coverage: 408/670 reporters (61%)'));
    assert(/<strong[^>]*>670<\/strong><span>calendar reporters<\/span>/.test(top), 'the tile shows the denominator of the sentence');
    assert(!/<strong[^>]*>658<\/strong>/.test(top), 'the other total is not shown as a bare number');
    assert(top.includes('The earnings calendar lists 658 upcoming reporters right now; 670 is the number this ranking run was built on.'),
        'the second total is named, not left unexplained');
    // Same count in both sources: one number, no note.
    top = board(670);
    assert(/<strong[^>]*>670<\/strong><span>calendar reporters<\/span>/.test(top));
    assert(!top.includes('upcoming reporters right now'));
}

// ============================================================ 4. WATCHLIST
{
    const SUNDAY = Date.parse('2026-11-15T09:00:00Z');        // Sunday, 04:00 New York
    const TUESDAY_OPEN = Date.parse('2026-11-17T15:00:00Z');  // Tuesday, 10:00 New York
    const TUESDAY_NIGHT = Date.parse('2026-11-18T03:00:00Z'); // Tuesday, 22:00 New York
    let clock = SUNDAY;
    class ClockDate extends Date {
        constructor(...args) { if (args.length) super(...args); else super(clock); }
        static now() { return clock; }
    }
    const container = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
    const errors = [];
    const ctx = {
        UI: {
            watchlistRemovedTickers: new Set(), watchlistSelectedTickers: new Set(), watchlistExpandedRows: new Set(),
            watchlistVisibleTickers: [], watchlistQuickAiLoading: {}, watchlistFilters: {}, watchlistSortBy: null,
            watchlistSortAsc: true, watchlistSortMode: 'snapshot', watchlistGroupBy: 'none', _livePricesCache: {},
            isTickerNew: () => false, getWatchlistQuickAi: () => null, formatPrice: (v) => String(v),
            _formatLocalDateTime: (v) => String(v).slice(0, 16), syncWatchlistSelectionControls() {},
            captureWatchlistSortSnapshot() {}, scheduleRender() {}, showToast() {},
        },
        API: { _watchlistCache: null, _warnNetworkOnce() {}, getWatchlistCached: () => Promise.resolve(null) },
        Actions: {}, ViewRouter: { activeView: 'watchlist' }, cachedState: null,
        document: { getElementById: (id) => (id === 'detailed-watchlist-content' ? container : (id === 'watchlist-search' ? { value: '' } : null)) },
        console: { error: (...a) => errors.push(a), warn: (...a) => errors.push(a), log() {} },
        Date: ClockDate, Intl,
    };
    vm.createContext(ctx);
    vm.runInContext(slice('UI.mergeLivePricesIntoState = function', 'UI.refreshWatchlistLivePrices = async function'), ctx);
    vm.runInContext(slice('UI.renderDetailedWatchlist = function', 'UI.toggleActivity = function'), ctx);
    const isoAgo = (seconds) => new Date(clock - seconds * 1000).toISOString();
    const baseRow = (ticker, extra = {}) => ({
        ticker, price: 100, change_pct: null, asset_type: 'stock', sector: 'Technology',
        trend_1d: 'BULL', trend_4h: 'BULL', col_1h: 'BULL 22:56:48', trend_alignment: 'BULL_ALIGN',
        signal_status: 'none', has_open_signal: false, signal_score: 0, earnings_days: -1,
        analysis_available: true, analysis_updated_at: isoAgo(7200), rsi: 55, macd_hist: 0.4,
        risk_flags: [], news_pulse: {}, strategy_notes: '', allowed_strategies: [], ...extra,
    });
    const rowOf = (rows, live, ticker) => {
        errors.length = 0;
        Object.assign(ctx.UI, { watchlistFilters: {}, watchlistSortBy: null, watchlistSortMode: 'snapshot', watchlistGroupBy: 'none',
            watchlistExpandedRows: new Set(), _livePricesCache: live });
        const state = { tickers: rows.map(r => ({ ...r })) };
        ctx.cachedState = state;
        ctx.UI.renderDetailedWatchlist(state);
        assert.equal(errors.length, 0, `render must not log errors: ${String(errors)}`);
        const all = container.innerHTML;
        const start = all.indexOf(`data-ticker="${ticker}"`);
        assert(start >= 0, `row for ${ticker} missing`);
        const next = all.indexOf('data-ticker="', start + 20);
        return all.slice(start, next >= 0 ? next : all.length);
    };
    const etoroQuote = (extra = {}) => ({ price: 17.56, change_pct: null, live: true, source: 'etoro', timestamp: isoAgo(3),
        age_seconds: 3, freshness: 'LIVE', market_time: null, market_age_seconds: null, ...extra });

    // Sunday: "$17.56 LIVE —" between rows badged CLOSED.
    let row = rowOf([baseRow('BMNR')], { BMNR: etoroQuote() }, 'BMNR');
    assert(row.includes('>LAST</span>'), 'eToro quote on a closed market is LAST');
    assert(!row.includes('>LIVE</span>') && !row.includes('wl-price-live'), 'and never LIVE');
    assert(row.includes('exchange trade time not reported'), 'the tooltip says why');
    assert(row.includes('the US market is closed'));
    // Known change but still no exchange trade time, closed market: LAST as well.
    row = rowOf([baseRow('ETOR')], { ETOR: etoroQuote({ change_pct: 1.2 }) }, 'ETOR');
    assert(row.includes('>LAST</span>') && !row.includes('>LIVE</span>'));
    // A venue that really traded seconds ago, but with no day change, is not LIVE outside the US session either.
    row = rowOf([baseRow('NOCHG')], { NOCHG: etoroQuote({ source: 'yahoo', market_time: isoAgo(20), market_age_seconds: 20 }) }, 'NOCHG');
    assert(row.includes('>LAST</span>'));
    // ... with a day change it is a real live quote (non-US venue): unchanged rule.
    row = rowOf([baseRow('SAP.DE')], { 'SAP.DE': etoroQuote({ source: 'yahoo', change_pct: 0.4, market_time: isoAgo(20), market_age_seconds: 20 }) }, 'SAP.DE');
    assert(row.includes('>LIVE</span>'));
    // Crypto trades around the clock.
    row = rowOf([baseRow('BTC-USD', { asset_type: 'crypto' })], { 'BTC-USD': etoroQuote({ source: 'binance' }) }, 'BTC-USD');
    assert(row.includes('>LIVE</span>'), 'crypto is not downgraded on a Sunday');
    // Friday close fetched on Sunday keeps the existing CLOSED badge.
    row = rowOf([baseRow('AAPL')], { AAPL: etoroQuote({ source: 'yahoo', change_pct: 0.39, live: false, freshness: 'CLOSED', market_time: isoAgo(39 * 3600), market_age_seconds: 39 * 3600 }) }, 'AAPL');
    assert(row.includes('>CLOSED</span>'));

    // Inside the US session the same eToro quote is live.
    clock = TUESDAY_OPEN;
    row = rowOf([baseRow('BMNR')], { BMNR: etoroQuote() }, 'BMNR');
    assert(row.includes('>LIVE</span>') && !row.includes('>LAST</span>'), 'during the US session the quote stays LIVE');
    // After 20:00 New York on a weekday: LAST again.
    clock = TUESDAY_NIGHT;
    row = rowOf([baseRow('BMNR')], { BMNR: etoroQuote() }, 'BMNR');
    assert(row.includes('>LAST</span>'));
}

// ============================================================= 5. HOT LIST
{
    const ctx = { UI: {} };
    vm.createContext(ctx);
    vm.runInContext(slice('UI.mergeHotListFastIntoDetails = function', 'UI._stateHasNews = function'), ctx);
    const oldStamp = new Date(Date.now() - 68 * 86400000).toISOString();
    const newStamp = new Date().toISOString();
    const cached = [{
        ticker: 'BMNR', _details_loaded: true, price: 17.1, trend_1d: 'BEAR', trend_4h: 'BEAR',
        technicals_stale: true, technicals_as_of: oldStamp, earnings_risk_flags: ['technicals_stale', 'price_stale'],
        data_gap_flags: ['technicals_stale'], price_stale: true, price_as_of: oldStamp, historical_gap_avg: 4.2,
    }];
    const fresh = [{
        ticker: 'BMNR', _details_loaded: false, price: 17.56, trend_1d: 'BULL', trend_4h: 'BULL',
        technicals_stale: false, technicals_as_of: newStamp, earnings_risk_flags: [], data_gap_flags: [],
        price_stale: false, price_as_of: newStamp,
    }];
    const [merged] = ctx.UI.mergeHotListFastIntoDetails(cached, fresh);
    assert.equal(merged.trend_1d, 'BULL');
    assert.equal(merged.technicals_stale, false, 'fresh trends must not keep the cached "stale" flag');
    assert.equal(merged.technicals_as_of, newStamp, 'nor the cached 68-day-old timestamp');
    assert.equal(merged.price_stale, false);
    assert.deepEqual(Array.from(merged.earnings_risk_flags), []);
    assert.deepEqual(Array.from(merged.data_gap_flags), []);
    assert.equal(merged.historical_gap_avg, 4.2, 'detail-only metrics are still preserved');
    assert.equal(merged._details_loaded, true);
    // A fresh item that says nothing about staleness leaves the cached value alone.
    const [partial] = ctx.UI.mergeHotListFastIntoDetails(cached, [{ ticker: 'BMNR', trend_1d: 'BULL' }]);
    assert.equal(partial.technicals_stale, true);
    // The card prints the label from the item it is given.
    assert(source.includes('const techStale = Boolean(item.technicals_stale);'));
}

// ========================================================== 6. TRADE RADAR
{
    const radarEls = {
        'traderadar-list': { innerHTML: '' }, 'traderadar-meta': { innerHTML: '' },
        'traderadar-summary-strip': { innerHTML: '' }, 'traderadar-symbol-filter': { innerHTML: '', value: 'ALL' },
    };
    const ctx = {
        UI: { tradeRadarExpanded: new Set(), scheduleRender() {}, showToast() {} },
        API: {}, Actions: {}, ViewRouter: { activeView: 'traderadar' },
        document: { getElementById: id => radarEls[id] || null }, window: {}, console,
    };
    vm.createContext(ctx);
    vm.runInContext(slice('UI.formatRadarPrice = function', 'UI.startTradeRadarAutoRefresh = function'), ctx);
    const stamp = new Date().toISOString();
    const candidate = (symbol, backtest, extra = {}) => ({
        symbol, source: 'binance', change_pct: 18.4, price: 2.5, last_seen: stamp, last_processed: stamp, analysis_pending: false,
        analysis: {
            verdict: 'LONG', confidence: 66, time_horizon: 'intraday', strength: 66, strength_estimated: false,
            strategy: { risk_mode: 'MEDIUM', trade_mode: 'BREAKOUT', trade_mode_source: 'strategy', setup_label: 'BREAKOUT' },
            entry: 2.5, tp: 2.6, sl: 2.42, levels_basis: 'atr', indicator_timeframe: '1h',
            key_indicators: { rsi: 61, adx: 28, macd_hist: 0.01, atr: 0.05 },
            confidence_breakdown: { base_strength: 66, news_alignment_bonus: 0, calendar_risk_penalty: 0, backtest_bonus: 0, final_confidence: 66, formula: 'base + news - calendar + backtest' },
        },
        news: { available: true, checked: true, bullish_count: 0, bearish_count: 0, top: [] },
        calendar: { available: true, checked: true, macro_checked: true, earnings: {}, macro_next_72h: [] },
        backtest,
        reasons: [`${symbol} moved +18.40% (threshold 15.00%).`, 'Backtest-lite 21d: WR 25.0% PF 0.61 over 8 trades.'],
        red_flags: [],
        readiness: { status: 'READY', can_open: true, distance_to_open: 0, entry_confidence_threshold: 60, blockers: [], note: '' },
        data_freshness: { scanner_ts: stamp, ohlc_ts: stamp, news_ts: stamp },
        ...extra,
    });
    const cards = (list) => {
        ctx.UI.tradeRadarExpanded = new Set();
        ctx.UI.renderTradeRadar({ as_of: stamp, config: {}, suppressed: {}, meta: { processed: list.length, queued: 0 }, total_candidates: list.length, candidates: list, disabled: false });
        return radarEls['traderadar-list'].innerHTML;
    };
    const cardOf = (all, symbol) => {
        const at = all.indexOf(`<div class="traderadar-symbol">${symbol}`);
        assert(at >= 0, `card for ${symbol} missing`);
        const next = all.indexOf('<article', at);
        return all.slice(at, next === -1 ? undefined : next);
    };
    const all = cards([
        candidate('LOSEUSDT', { available: true, days: 21, win_rate: 25, profit_factor: 0.61, closed_trades: 8, max_drawdown: 3 }),
        candidate('WINUSDT', { available: true, days: 21, win_rate: 62, profit_factor: 1.8, closed_trades: 13, max_drawdown: 1 }),
        candidate('FEWUSDT', { available: true, days: 21, win_rate: 0, profit_factor: 0.2, closed_trades: 4, max_drawdown: 2 }),
        candidate('NOPFUSDT', { available: true, days: 21, win_rate: 100, profit_factor: null, closed_trades: 6, max_drawdown: 0 }),
        candidate('NOBTUSDT', { available: false, unavailable_reason: 'no_price_history', days: 21, win_rate: 0, profit_factor: 0, closed_trades: 0 }),
        candidate('EVENUSDT', { available: true, days: 21, win_rate: 50, profit_factor: 1.0, closed_trades: 10, max_drawdown: 1 }),
    ]);
    const losing = cardOf(all, 'LOSEUSDT');
    assert(losing.includes('>READY</span>') && losing.includes('entry gate · 60'), 'the gate itself is unchanged');
    assert(losing.includes('Backtest negative (PF 0.61, WR 25%, 8 trades)'), 'READY is never shown without the losing backtest');
    assert(losing.includes('traderadar-backtest-warning'));
    assert(losing.indexOf('entry gate · 60') < losing.indexOf('Backtest negative') && losing.indexOf('Backtest negative') < losing.indexOf('traderadar-levels-panel'),
        'the warning sits in the readiness panel, next to the badge');
    for (const symbol of ['WINUSDT', 'FEWUSDT', 'NOPFUSDT', 'NOBTUSDT', 'EVENUSDT']) {
        assert(!cardOf(all, symbol).includes('Backtest negative'), `${symbol}: no warning without a losing backtest of at least 5 trades`);
    }
}

// ============================================================ 8. CALENDAR
const calendarTests = async () => {
    const pad = (n) => String(n).padStart(2, '0');
    const key = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    function calEl(tag) {
        const el = {
            tag, id: '', className: '', textContent: '', title: '', children: [], parentElement: null, attrs: {},
            style: { cssText: '', setProperty(name, value, prio) { this[name] = `${value}${prio ? ' !' + prio : ''}`; } },
            classList: {
                add(c) { el.className = `${el.className} ${c}`.trim(); },
                remove(c) { el.className = el.className.split(' ').filter(x => x !== c).join(' '); },
                contains(c) { return el.className.split(' ').includes(c); },
            },
            setAttribute(k, v) { el.attrs[k] = String(v); },
            getAttribute(k) { return Object.prototype.hasOwnProperty.call(el.attrs, k) ? el.attrs[k] : null; },
            appendChild(child) { child.parentElement = el; el.children.push(child); return child; },
            insertBefore(child, ref) { child.parentElement = el; el.children.splice(Math.max(0, el.children.indexOf(ref)), 0, child); return child; },
            remove() {},
        };
        let inner = '';
        Object.defineProperty(el, 'innerHTML', { get() { return inner; }, set(v) { inner = String(v); el.children = []; } });
        return el;
    }
    const allCells = (el, out = []) => {
        if (String(el.className).includes('calendar-day-cell')) out.push(el);
        el.children.forEach(c => allCells(c, out));
        return out;
    };
    const page = calEl('div'), card = calEl('div'), wrapper = calEl('div'), header = calEl('span');
    page.appendChild(card); card.appendChild(wrapper);
    const byId = { 'calendar-months-wrapper': wrapper, 'cal-current-month': header };
    const now = new Date();
    const payload = {
        economic: [], earnings: [{ id: 'earn_big', symbol: 'BIGCO', date: `${key(now)}T00:00:00`, date_formatted: key(now), market_cap: 2e12, when: 'bmo' }],
        ipo: [], watchlist_tickers: [],
        coverage: { generated_at: now.toISOString(), earnings: { fetched_at: now.toISOString(), age_seconds: 60, stale: false, window_end: '2000-01-01' }, macro: { years_missing: [] }, ipo: { available: false } },
    };
    const ctx = {
        Actions: { _ensureCalendarCachesLoaded() {}, getWatchlistSignature() { return ''; }, _persistCalendarCaches() {}, _watchlistSig: '', calendarRangeCache: {} },
        UI: { _escapeHtml: escapeHtml }, IS_DEV: false,
        document: {
            getElementById(id) { return byId[id] || page.children.find(c => c.id === id) || null; },
            createElement: calEl, querySelector() { return null; },
        },
        fetch: async () => ({ ok: true, json: async () => payload }),
        console: { log() {}, warn() {}, error() {} },
        localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
        window: { location: { origin: 'http://localhost' } }, URL,
    };
    vm.createContext(ctx);
    vm.runInContext(slice('Actions.calendarUtil = {', 'Actions.openEventModal = function'), ctx);
    await ctx.Actions.renderFullCalendar(now);
    const cells = allCells(wrapper);
    const today = cells.filter(c => c.className.split(' ').includes('today'));
    assert.equal(today.length, 1);
    assert.equal(today[0].getAttribute('aria-current'), 'date', 'today is exposed to assistive technology');
    assert.equal(cells.filter(c => c.getAttribute('aria-current')).length, 1, 'only today carries aria-current');
    const head = today[0].innerHTML.split('<div class="cal-daily-events"')[0];
    const numberAt = head.indexOf(`<span>${now.getDate()}</span>`);
    const tagAt = head.indexOf('TODAY');
    assert(numberAt >= 0 && tagAt > numberAt, 'the TODAY tag follows the day number inside the same cell');
    assert.equal(head.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim(), `${now.getDate()} TODAY`, 'text order reads "15 TODAY"');
    assert(head.includes('flex-direction:row-reverse'), 'visual order (tag left, number right) is kept by CSS only');
    const other = cells.find(c => !c.className.includes('today') && !c.className.includes('other-month') && c.innerHTML.includes('day-number'));
    assert(other && !other.innerHTML.includes('TODAY'));
};

// ====================================================== 9. markup / versions
{
    for (const id of ['news-source-health', 'news-narrative-note', 'news-hide-social', 'news-social-count']) {
        assert(indexHtml.includes(`id="${id}"`), `index.html is missing #${id}`);
    }
    assert(indexHtml.includes('onchange="UI.setNewsHideSocial(this.checked)"'));
    // Versions only ever go up; pin a floor, not an exact value.
    const assetVersion = (file) => {
        const marker = `${file}?v=`;
        const start = indexHtml.indexOf(marker);
        if (start < 0) return null;
        const match = /^[0-9.]+/.exec(indexHtml.slice(start + marker.length));
        return match ? match[0].split('.').map(Number) : null;
    };
    const atLeast = (file, floor) => {
        const have = assetVersion(file);
        if (!have) return false;
        for (let i = 0; i < Math.max(have.length, floor.length); i++) {
            const a = have[i] || 0;
            const b = floor[i] || 0;
            if (a !== b) return a > b;
        }
        return true;
    };
    assert(atLeast('app.js', [38, 37]), 'app.js cache version bumped');
    assert(atLeast('cockpit.js', [1, 20]) && atLeast('skeleton_utils.js', [1, 2]) && atLeast('styles_premium.css', [4, 12]));
    const css = read('styles_premium.css');
    for (const rule of ['.news-source-health', '.news-narrative-note', '.nc2-social-pill', '.traderadar-backtest-warning']) {
        assert(css.includes(rule), `styles_premium.css is missing ${rule}`);
    }
}

(async () => {
    await newsBriefingTests();
    await calendarTests();
    console.log('Tab audit wave 2 (news fields, cockpit wording, reporter total, LAST badge, hot-list staleness, radar backtest warning, date helper, calendar today): PASS');
})().catch(error => { console.error(error); process.exit(1); });
