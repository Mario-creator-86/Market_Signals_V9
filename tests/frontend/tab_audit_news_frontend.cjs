// Tab audit 2026-11-15 — News tab ("Market Newsroom") frontend regressions.
// Runs the real news code from frontend/app.js in a vm with a minimal fake DOM.
//   node tests/frontend/tab_audit_news_frontend.cjs
const fs = require('fs'), vm = require('vm'), path = require('path'), assert = require('assert/strict');
const source = fs.readFileSync(path.resolve(__dirname, '../../frontend/app.js'), 'utf8');

function slice(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    assert(start >= 0 && end > start, `markers not found: ${startMarker} .. ${endMarker}`);
    return source.slice(start, end);
}
const code = [
    slice('// ============ NEWS PAGE FUNCTIONS ============', 'UI._capitalExplainabilitySelection = '),
    slice('UI._collectNewsTickerOptions = function', 'UI._syncHomeNewsFilterButtons = function'),
    ';globalThis.__setNewsCache = (v) => { cachedNewsData = v; };',
    'globalThis.__getNewsCache = () => cachedNewsData;',
    'globalThis.__NewsUIState = NewsUIState;',
].join('\n');

// ---------------------------------------------------------------- fake DOM
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
const exportBoxes = ['critical', 'high', 'medium', 'low'].map(level => {
    const box = makeEl(`export-${level}`); box.checked = true; box.value = level; return box;
});
const store = new Map();
const toasts = [];
const opened = [];
const timers = [];
let fetchImpl = async () => ({ ok: true, status: 200, json: async () => ({}) });

const consoleErrors = [];
const context = {
    console: { log() {}, warn() {}, error: (...args) => consoleErrors.push(args.map(String).join(' ')) },
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
            if (selector === '.export-impact') return exportBoxes;
            if (selector === '.export-impact:checked') return exportBoxes.filter(b => b.checked);
            return [];
        },
    },
    ViewRouter: { activeView: 'news' },
    API: {
        getNewsState: async () => null,
        getNewsTicker: async () => ({ news: [] }),
        getWatchlistCached: async () => ({}),
        getState: async () => ({ tickers: [] }),
        fetchWithTimeout: async (...args) => fetchImpl(...args),
    },
    Actions: {},
    HomeUIState: {},
    fetch: (...args) => fetchImpl(...args),
    setInterval: () => 1, clearInterval() {},
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    clearTimeout() {},
    UI: {
        showToast: (message, type) => toasts.push({ message: String(message), type }),
        scheduleRender: (key, fn) => fn(),
        formatTimeAgo: () => 'LEGACY-TIME',
        renderNews() {}, mergeStatePreserveDetails: (a) => a, _composeHomeState: () => ({}),
        loadDashboardSnapshot: () => ({}), updateActivityLog() {},
    },
};
context.window = {
    innerWidth: 1400,
    setTimeout: context.setTimeout,
    open: (...args) => { opened.push(args); return null; },
};
vm.createContext(context);
vm.runInContext(code, context, { filename: 'app.js#news' });
const { UI, Actions } = context;
const State = context.__NewsUIState;
const setCache = context.__setNewsCache;

// ------------------------------------------------------------------ fixtures
const NOW = Date.now();
const ago = (minutes) => new Date(NOW - minutes * 60000).toISOString();
const LONG_POST = 'Good morning to all investors and followers.\n\nOn Saturday, November 14, at Starbase, Texas, a Starship returning from orbit was caught by the tower. '
    + '**This is huge** for the launch cadence and for every supplier in the chain. '.repeat(12);
const art = (id, over = {}) => ({
    id, title: `Headline ${id}`, summary: `Body text for ${id} that says something different from the headline.`,
    url: `https://example.com/${id}`, source: 'yahoo_rss', published_at: ago(30), ticker: 'AAPL',
    category: 'general', sentiment: 'neutral', impact: 'medium', impact_score: 40, action_score: 80,
    is_duplicate: false, is_breaking: false, ai_micro_summary: null, ai_deep_analysis: null,
    published_at_formatted: 'Just now', ...over,
});
const ESSENTIAL = {
    briefing_version: 3, tldr: 'Supplier guidance cut hits the chain', article_explainer: 'The company cut guidance.',
    market_implication: 'Negative read-through for suppliers.', market_direction: 'risk-off', evidence_scope: 'full-article',
    ticker_implication: { ticker: 'AAPL', has_direct_link: true, relationship: 'Direct supplier.', direction: 'down', rationale: 'Lower orders.', confidence: 62 },
    key_takeaways: ['One'], time_horizon: '1-7d',
};
function resetUi() {
    els.clear(); store.clear(); toasts.length = 0; opened.length = 0; timers.length = 0;
    impactBoxes.forEach(b => { b.checked = true; });
    exportBoxes.forEach(b => { b.checked = true; });
    getEl('news-hide-duplicates').checked = true;
    Object.assign(State, {
        selectedArticleId: null, viewMode: 'split', timeWindow: '24h', focusMode: 'top', selectedSource: 'ALL',
        categories: new Set(['ALL']), selectedTicker: 'ALL', actionabilityThreshold: null, sentimentFilter: 'all',
        briefingActiveId: null, selectionPinned: false, settingsDefaultsApplied: false, tickerSelectorSynced: false,
        deepAnalysisActiveId: null,
        watchlistSet: new Set(['AAPL', 'NVDA', 'MSFT']), watchlistOrder: ['AAPL', 'NVDA', 'MSFT'],
        watchlistStocks: ['AAPL', 'NVDA', 'MSFT'], watchlistCrypto: [],
    });
    State.briefingAttempted.clear(); State.briefingFailures.clear(); State.briefingFailureReasons.clear();
    State.briefingCache.clear(); State.extraArticles.clear();
    UI._newsStoredIdCache = {};
    if (UI._newsTitlePartsCache) UI._newsTitlePartsCache.clear();
    context.document.activeElement = null;
}
const count = (html, needle) => html.split(needle).length - 1;
const text = (id) => getEl(id).textContent;
const html = (id) => getEl(id).innerHTML;

// ============================================================ 1. timestamps
resetUi();
assert.equal(UI._newsParseTs('2026-11-15T08:00:00'), Date.UTC(2026, 10, 15, 8, 0, 0), 'zone-less ISO is UTC, not browser-local');
assert.equal(UI._newsParseTs('2026-11-15 08:00:00'), Date.UTC(2026, 10, 15, 8, 0, 0));
assert.equal(UI._newsParseTs('2026-11-15T10:00:00+02:00'), Date.UTC(2026, 10, 15, 8, 0, 0), 'explicit offsets are honoured');
assert(Number.isNaN(UI._newsParseTs('not a date')));
assert.equal(UI._newsTimeAgo('not a date'), 'time unknown', 'an unreadable timestamp is never "Just now"');
assert.equal(UI._newsTimeAgo(ago(14)), '14m ago');
assert.equal(UI._newsTimeAgo(ago(0)), 'Just now');
assert.equal(UI._newsTimeAgo(ago(5 * 60 + 10)), '5h ago');
assert.equal(UI._newsTimeAgo(ago(3 * 24 * 60)), '3d ago');
{
    // The relative-time ticker re-computes ages from the real timestamp.
    const span = makeEl('t'); span.attrs['data-news-ts'] = ago(61); span.textContent = 'Just now';
    const synced = makeEl('s'); synced.attrs['data-news-ts'] = ago(7); synced.attrs['data-news-ts-prefix'] = 'Synced '; synced.textContent = 'Synced Just now';
    getEl('view-news')._query = [span, synced];
    assert.equal(UI._refreshNewsRelativeTimes(), 2);
    assert.equal(span.textContent, '1h ago');
    assert.equal(synced.textContent, 'Synced 7m ago');
}

// ============================================ 2. one story, several tickers
resetUi();
{
    const shared = art('multi', { title: 'Weekly update', ticker: 'AAPL', impact: 'high' });
    const data = { breaking_news: [], market_news: [shared], ticker_news: { AAPL: [shared], NVDA: [shared], TSLA: [art('tsla', { ticker: 'TSLA' })] } };
    const flat = UI._flattenNewsArticles(data);
    assert.equal(flat.length, 2, 'deduplicated by id');
    const rec = flat.find(a => a.id === 'multi');
    assert.deepEqual(Array.from(rec._tickers), ['MARKET', 'AAPL', 'NVDA']);
    assert.equal(rec.ticker, 'AAPL', 'display ticker is the first real ticker');
    State.focusMode = 'all';
    for (const tk of ['NVDA', 'AAPL', 'MARKET']) {
        getEl('news-follow-ticker').value = tk;
        assert.equal(UI._articlePassesFilters(rec, data), true, `ticker filter ${tk} must find a story delivered in that bucket`);
    }
    getEl('news-follow-ticker').value = 'TSLA';
    assert.equal(UI._articlePassesFilters(rec, data), false);
}

// ================== 3. header cards, chips, rail, feed and tags agree
resetUi();
{
    const market = [
        art('m1', { ticker: null, impact: 'high', source: 'eToro (@Jeffearner)' }),
        art('m2', { ticker: null, impact: 'high', source: 'eToro (@fdlemail)' }),
    ];
    const aapl = [
        art('a1', { impact: 'high', source: 'yahoo_rss' }),
        art('a2', { impact: 'high', source: 'eToro (@wantonietti)' }),
        art('a3', { impact: 'high', source: 'eToro (@pino428)' }),
        art('a4', { impact: 'medium', action_score: 95, source: 'eToro (@Dafne17)' }),
        art('a5', { impact: 'high', is_duplicate: true, source: 'eToro (@andrei9868)' }),
        art('old', { impact: 'critical', is_breaking: true, published_at: ago(40 * 60) }),
    ];
    const data = { breaking_news: [], market_news: market, ticker_news: { AAPL: aapl }, settings: { actionability_threshold: 60 } };
    setCache(data);
    UI.renderNewsOverview(data);
    UI.renderNewsFeed(data);
    const feed = html('news-page-feed-content');

    assert.equal(text('news-metric-total'), '6', 'Coverage counts what the feed can show (duplicate hidden)');
    assert.equal(text('news-metric-range'), 'last 24 hours · 1 duplicate hidden');
    assert.equal(text('news-feed-count'), '6 top stories', 'feed count equals Coverage when nothing else filters');
    assert.equal(count(feed, 'data-article-id='), 6);
    assert.match(html('news-ticker-chips'), />ALL<span class="ntc-n">6<\/span>/);

    assert.equal(text('news-metric-moving'), '5', 'Market-moving = high/critical impact or flagged breaking');
    assert.equal(text('news-breaking-count'), '5', 'the "Market-moving now" rail uses the same rule');
    assert.equal(text('news-metric-moving-note'), '5 high/critical impact · 0 breaking');
    assert.equal(count(feed, 'nc2-breaking-tag'), 0, 'no BREAKING tag while the header says 0 breaking');

    assert.equal(text('news-metric-watchlist'), '4');
    assert.equal(text('news-metric-watchlist-note'), 'stories · 1 of 3 watchlist symbols');
    assert.match(getEl('news-metric-watchlist-note').title, /4 stories in the last 24 hours tagged with a watchlist symbol; 1 of your 3 watchlist symbols have at least one/);
    assert.equal(text('news-metric-sources'), '2', 'eToro authors are one community source, not one publisher each');
    assert.equal(text('news-metric-source-note'), 'Most active: eToro community (5)');

    // Breaking: the tag, the header count and the Breaking focus use one rule.
    State.timeWindow = '3d';
    UI.renderNewsOverview(data);
    UI.renderNewsFeed(data);
    assert.equal(text('news-metric-moving-note'), '6 high/critical impact · 1 breaking');
    assert.equal(count(html('news-page-feed-content'), 'nc2-breaking-tag'), 1);
    State.focusMode = 'breaking';
    UI.renderNewsFeed(data);
    assert.equal(text('news-feed-count'), '1 breaking story of 7 in window');
    assert.equal(count(html('news-page-feed-content'), 'data-article-id='), 1);

    // Empty watchlist wording.
    State.watchlistSet = new Set();
    UI.renderNewsOverview(data);
    assert.equal(text('news-metric-watchlist-note'), 'watchlist is empty');
}

// The rail says how many of the total it shows.
resetUi();
{
    const many = Array.from({ length: 9 }, (_, i) => art(`h${i}`, { impact: 'high', published_at: ago(10 + i) }));
    const data = { breaking_news: [], market_news: [], ticker_news: { AAPL: many } };
    setCache(data);
    UI.renderNewsFeed(data);
    assert.equal(text('news-breaking-count'), '6 newest of 9');
    assert.equal(count(html('news-breaking-list'), 'nb3-card'), 6);
}

// ===================================== 4. takeaway vs excerpt, cut titles
resetUi();
{
    const post = art('post', {
        title: 'Good morning to all investors and followers.: On Saturday, November 14, at Starbase, Texas, a Starship returni',
        summary: LONG_POST, source: 'eToro (@fdlemail)',
    });
    const card = UI.renderNewsCard(post);
    assert(!card.includes('Takeaway:'), 'the start of the same post is not a takeaway');
    assert(card.includes('Source excerpt:'), 'it is labelled as a source excerpt');
    assert(!card.includes('returni<') && !/returni\s*…/.test(card), 'title is not cut in the middle of a word');
    assert(card.includes('a Starship…'), 'cut title ends at a word boundary with an ellipsis');
    assert(!card.includes('followers.:'), 'the ".: " join artefact is removed');
    const excerpt = card.split('Source excerpt:</b> ')[1].split('</span>')[0];
    assert(excerpt.startsWith('…returning from orbit'), `excerpt continues after the headline, got: ${excerpt.slice(0, 40)}`);
    assert(!excerpt.includes('**'), 'markdown markers are stripped');
    assert(excerpt.length <= 182, 'excerpt is bounded');

    // A title cut inside a number ("7" of "7,561.67") drops the fragment too.
    const num = UI._newsTitleParts(art('num', {
        title: 'BERKSHIRE HIGHS, BITCOIN LOWS: Friday closed a positive week with a divided market. The $SPX500 fell 0.40% to 7',
        summary: 'BERKSHIRE HIGHS, BITCOIN LOWS\n\nFriday closed a positive week with a divided market. The $SPX500 fell 0.40% to 7,561.67 on Friday but gained 2.51% over the week.',
    }));
    assert(num.title.endsWith('fell 0.40% to…'), num.title);
    assert(num.rest.startsWith('7,561.67 on Friday'), num.rest);
    // A complete headline followed by more text is left alone.
    const whole = UI._newsTitleParts(art('whole', { title: 'Three tests for the market this week.', summary: 'Three tests for the market this week.\n\n$NVDA reports Tuesday.' }));
    assert.equal(whole.title, 'Three tests for the market this week.');
    assert.equal(whole.rest, '$NVDA reports Tuesday.');

    const echo = UI.renderNewsCard(art('echo', { title: 'Apple beats estimates', summary: 'Apple beats estimates' }));
    assert(!echo.includes('nc2-ai-takeaway'), 'a summary identical to the headline shows nothing');

    const ai = UI.renderNewsCard(art('ai', { ai_micro_summary: { tldr: 'Guidance cut drags suppliers' } }));
    assert(ai.includes('<b>Takeaway:</b> Guidance cut drags suppliers'), 'a real AI summary keeps the Takeaway label');
    const aiEcho = UI.renderNewsCard(art('ai2', { title: 'Guidance cut drags suppliers', ai_micro_summary: { tldr: 'Guidance cut drags suppliers.' } }));
    assert(!aiEcho.includes('Takeaway:'), 'an AI line that only repeats the headline is not a takeaway');

    const styled = UI.renderNewsCard(art('styled', { title: '📊 𝙁𝙄𝙉𝘼𝙉𝘾𝙄𝘼𝙇 𝗠𝗔𝗥𝗞𝗘𝗧 update' }));
    assert(styled.includes('📊 FINANCIAL MARKET update'), 'styled unicode letters are normalised');

    // Relative time comes from published_at, not from the frozen server string.
    const timed = UI.renderNewsCard(art('timed', { published_at: ago(14), published_at_formatted: 'Just now' }));
    assert(timed.includes('>14m ago</span>') && !timed.includes('>Just now<'));
    assert(timed.includes('data-news-ts="'), 'time carries its timestamp for the live ticker');
}

// ============================================================== 5. escaping
resetUi();
{
    const evil = art('x\'"><img src=x onerror=alert(1)>', {
        title: '<img src=x onerror=alert(1)> **bold** "quoted"',
        summary: '<script>alert(2)</script> &lt;b&gt;text&lt;/b&gt; body of the story is here',
        source: 'eToro (@<b>bad</b>)', category: '<i>cat</i>', impact: 'high"><b>', ticker: 'AA<b>PL',
    });
    const data = { breaking_news: [], market_news: [], ticker_news: { 'AA<b>PL': [evil] } };
    setCache(data);
    const card = UI.renderNewsCard(UI._flattenNewsArticles(data)[0]);
    for (const bad of ['<img', '<script', '<b>bad', '<i>cat', '<b>PL', 'high"><b>', '<b>text']) {
        assert(!card.toLowerCase().includes(bad.toLowerCase()), `unescaped third-party text in card: ${bad}`);
    }
    assert(card.includes('UI.selectNewsArticle(&quot;x&#39;\\&quot;&gt;&lt;img'), 'ids are passed as escaped JSON string literals');
    State.focusMode = 'all';
    impactBoxes.forEach(b => { b.checked = true; });
    UI.renderNewsFeed(data);
    for (const id of ['news-page-feed-content', 'news-breaking-list', 'news-ticker-chips']) {
        for (const bad of ['<img', '<script', '<b>bad', '<b>pl']) assert(!html(id).toLowerCase().includes(bad), `${id}: ${bad}`);
    }
    State.selectedArticleId = evil.id;
    UI.renderInsightPanel(data, { force: true, skipAutoBriefing: true });
    for (const bad of ['<img', '<script', '<b>bad']) assert(!html('news-insight-panel').toLowerCase().includes(bad), `panel: ${bad}`);
}

// ================================================== 6. briefing panel states
resetUi();
{
    const post = art('p1', { title: 'Three tests for the market this week', summary: LONG_POST, sentiment: 'bearish', impact: 'high' });
    const data = { breaking_news: [], market_news: [], ticker_news: { AAPL: [post] } };
    setCache(data);
    State.selectedArticleId = 'p1';
    const explainer = () => html('news-insight-panel').split('WHAT IS THE NEWS?</h5>')[1].split('<p>')[1].split('</p>')[0];

    // loading
    State.briefingActiveId = 'p1';
    UI.renderInsightPanel(data, { force: true, skipAutoBriefing: true });
    let panel = html('news-insight-panel');
    assert(panel.includes('Building the three-point briefing'));
    assert(panel.includes('Source excerpt · not analysed'), 'the unanalysed source is not presented as the essential answer');
    assert(!panel.includes('Essential answer'));
    assert(!panel.includes('**'), 'raw markdown is not dumped');
    assert(explainer().length <= 421, `source text is bounded, got ${explainer().length}`);
    assert(panel.includes('Analysis in progress…'));
    assert(!panel.includes('Likely down') && !panel.includes('Risk-off'), 'no direction is derived from headline sentiment');
    assert.equal(count(panel, '</i>Not analysed'), 2);

    // queued behind another story
    State.briefingActiveId = 'someone-else';
    UI.renderInsightPanel(data, { force: true, skipAutoBriefing: true });
    assert(html('news-insight-panel').includes('Queued: the briefing for another story'));

    // failed, with the reason, escaped
    State.briefingActiveId = null;
    State.briefingFailures.add('p1');
    State.briefingFailureReasons.set('p1', 'Ollama is <b>not</b> reachable.');
    UI.renderInsightPanel(data, { force: true, skipAutoBriefing: true });
    panel = html('news-insight-panel');
    assert(panel.includes('AI briefing failed: Ollama is &lt;b&gt;not&lt;/b&gt; reachable.'));
    assert(panel.includes('UI.retryEssentialNewsBriefing()'));
    assert(!panel.includes('fa-spin'), 'no spinner once the request has failed');

    // ready
    State.briefingFailures.clear();
    post.ai_micro_summary = { ...ESSENTIAL, article_explainer: `**Bold** claim. ${'word '.repeat(400)}` };
    UI.renderInsightPanel(data, { force: true, skipAutoBriefing: true });
    panel = html('news-insight-panel');
    assert(panel.includes('Essential answer') && panel.includes('Likely down') && panel.includes('62% confidence'));
    assert(!panel.includes('**') && explainer().length <= 701, 'AI text is cleaned and bounded too');

    // the backend "AI unavailable" fallback is metadata, not a briefing
    post.ai_micro_summary = { ...ESSENTIAL, source: 'pre_computed_metadata', article_explainer: LONG_POST.slice(0, 500) };
    assert.equal(UI._hasEssentialNewsBriefing(post.ai_micro_summary), false);
    UI.renderInsightPanel(data, { force: true, skipAutoBriefing: true });
    assert(html('news-insight-panel').includes('Source excerpt · not analysed'));
    post.ai_micro_summary = null;
}

// ============================================================== 7. filters
resetUi();
{
    const set = [
        art('t1', { published_at: ago(60) }),
        art('t2', { published_at: ago(2 * 24 * 60), source: 'coindesk', category: 'crypto' }),
        art('t3', { published_at: ago(5 * 24 * 60) }),
        art('t4', { published_at: ago(20 * 24 * 60) }),
        art('t5', { published_at: ago(45 * 24 * 60) }),
        art('bad', { published_at: 'garbage' }),
    ];
    const data = { breaking_news: [], market_news: [], ticker_news: { AAPL: set } };
    setCache(data);
    State.focusMode = 'all';
    const shown = () => count(html('news-page-feed-content'), 'data-article-id=');
    const expected = { '24h': 1, '3d': 2, '7d': 3, '30d': 4 };
    for (const [key, n] of Object.entries(expected)) {
        UI.setNewsTimeWindow(key);
        assert.equal(shown(), n, `time window ${key}`);
        assert.equal(text('news-metric-total'), String(n), `Coverage follows the ${key} window`);
        assert.equal(store.get('news_time_window'), key, 'window is remembered');
    }
    // search also matches the source and the category shown on the card
    getEl('news-filter-search').value = 'coindesk';
    UI.applyNewsFilters();
    assert.equal(shown(), 1);
    assert.equal(text('news-feed-count'), '1 article of 4 in window');
    getEl('news-filter-search').value = '';
    // every impact level unticked -> nothing, not everything
    impactBoxes.forEach(b => { b.checked = false; });
    UI.applyNewsFilters();
    assert.equal(shown(), 0);
    assert.match(html('news-page-feed-content'), /No articles in the last 30 days match the current filters/);
    assert.equal(getEl('news-filters-count').textContent, '1');
    impactBoxes.forEach(b => { b.checked = true; });

    // sentiment chip goes through the full filter pass (badge + count)
    UI.setNewsSentimentFilter('bearish');
    assert.equal(getEl('news-filters-count').textContent, '1');
    assert.equal(getEl('news-filters-count').hidden, false);
    UI.setNewsSentimentFilter('bearish');
    assert.equal(getEl('news-filters-count').hidden, true);

    // Dedup default from the server is applied once, then the user's choice wins
    UI.initNewsUIStateFromSettings({ settings: { hide_duplicates_default: true } });
    getEl('news-hide-duplicates').checked = false;
    UI.initNewsUIStateFromSettings({ settings: { hide_duplicates_default: true } });
    assert.equal(getEl('news-hide-duplicates').checked, false, 'revisiting the tab must not undo the Dedup choice');
}

// ticker filter: restored from storage, kept across refreshes
resetUi();
{
    const data = { breaking_news: [], market_news: [], ticker_news: { SPCX: [art('s1', { ticker: 'SPCX' })], AAPL: [art('s2')] } };
    setCache(data);
    State.selectedTicker = 'NVDA';                 // value read from localStorage at start-up
    getEl('news-follow-ticker').value = 'ALL';     // the static <select> default
    UI._syncNewsTickerSelector(data);
    assert.equal(State.selectedTicker, 'NVDA', 'the stored ticker filter is restored on first render');
    UI.setNewsTickerChip('SPCX');                  // SPCX is not on the watchlist
    UI._syncNewsTickerSelector(data);              // what every background refresh does
    assert.equal(getEl('news-follow-ticker').value, 'SPCX', 'a chip filter outside the watchlist survives a refresh');
    assert.equal(State.selectedTicker, 'SPCX');
    assert.equal(getEl('news-filters-count').textContent, '1', 'the chip shows up in the Filters badge');
    UI._syncNewsTickerSelector({ breaking_news: [], market_news: [], ticker_news: { AAPL: [art('s2')] } });
    assert.equal(State.selectedTicker, 'ALL', 'a ticker with no stories left falls back to ALL');
}

// selection: a story opened on purpose is not replaced by a background refresh
resetUi();
{
    const data = { breaking_news: [], market_news: [], ticker_news: { AAPL: [art('top1', { impact: 'high' }), art('low1', { impact: 'low', action_score: 10 })] } };
    setCache(data);
    UI.renderNewsFeed(data);
    assert.equal(State.selectedArticleId, 'top1');
    UI.selectNewsArticle('low1');                  // not part of "Top stories"
    UI.renderNewsFeed(data);                       // background refresh
    assert.equal(State.selectedArticleId, 'low1');
    UI.applyNewsFilters();                         // the user changes a filter
    assert.equal(State.selectedArticleId, 'top1');
}

// ============================================= 8. saved stories, bad storage
resetUi();
{
    const old = art('keep', { published_at: ago(3 * 24 * 60), title: 'Saved three days ago' });
    let data = { breaking_news: [], market_news: [], ticker_news: { AAPL: [old, art('fresh')] } };
    setCache(data);
    UI.toggleSaveNews(null, 'keep');
    assert.deepEqual(JSON.parse(store.get('saved_news')), ['keep']);
    assert.equal(toasts.at(-1).message, 'News saved');
    State.focusMode = 'saved';
    UI.renderNewsFeed(data);
    assert(html('news-page-feed-content').includes('Saved three days ago'), 'Saved is not cut by the 24h window');
    assert.equal(text('news-feed-count'), '1 saved story');
    data = { breaking_news: [], market_news: [], ticker_news: { AAPL: [art('fresh')] } };   // aged out of the payload
    setCache(data);
    UI.renderNewsFeed(data);
    assert(html('news-page-feed-content').includes('Saved three days ago'), 'a bookmark survives the story leaving the feed');
    assert(html('news-page-feed-content').includes('Saved copy'));
    UI.toggleSaveNews(null, 'keep');
    assert.deepEqual(JSON.parse(store.get('saved_news')), []);
    assert.equal(store.get('saved_news_items'), '{}');
    UI.renderNewsFeed(data);
    assert.match(html('news-page-feed-content'), /No saved stories yet/);

    // corrupt storage must not take the feed down
    store.set('saved_news', '{not json'); store.set('read_news', 'null');
    State.focusMode = 'all';
    UI.renderNewsFeed(data);
    assert.equal(count(html('news-page-feed-content'), 'data-article-id='), 1);
    // a failed write is not reported as "saved"
    const realSet = context.localStorage.setItem;
    context.localStorage.setItem = () => { throw new Error('QuotaExceededError'); };
    toasts.length = 0;
    UI.toggleSaveNews(null, 'fresh');
    assert.equal(toasts.at(-1).type, 'error');
    context.localStorage.setItem = realSet;
}

// ========================================= 9. mood bar vs. narrative regime
resetUi();
{
    UI._renderNewsMoodBar([art('1', { sentiment: 'bullish' }), ...['2', '3', '4'].map(i => art(i, { sentiment: 'bearish' })), art('5'), art('6')]);
    const mood = html('news-mood-bar');
    assert(mood.includes('Bearish tilt') && mood.includes('Headline mood') && mood.includes('· 6 stories'));
    assert(!/Risk-o(n|ff) lean/.test(mood), 'the headline mix is not worded as a market regime');
    assert(mood.includes('▲ 17%') && mood.includes('▼ 50%'));

    UI.renderMarketNarrative({ last_update: ago(0), settings: { narrative_ttl_seconds: 900 },
        market_narrative: { risk_regime: 'risk-on', what_matters_today: ['**Rates** matter'], generated_at: ago(185) } });
    assert.equal(text('news-narrative-status'), 'risk-on');
    assert.equal(text('news-narrative-updated'), 'AI narrative · stale, generated 3h ago', 'age of the narrative, not of the news sync');
    assert.equal(text('news-market-narrative-content'), 'Rates matter');
    UI.renderMarketNarrative({ last_update: ago(0), market_narrative: { risk_regime: 'risk-on', what_matters_today: ['x'], generated_at: ago(4) } });
    assert.equal(text('news-narrative-updated'), 'AI narrative · generated 4m ago');
    UI.renderMarketNarrative({ last_update: ago(0), market_narrative: null });
    assert.equal(text('news-narrative-status'), 'no narrative', 'no invented "mixed" regime');
    assert.equal(text('news-narrative-updated'), '');
}

// ================================================= 10. export + source links
resetUi();
{
    getEl('export-days').value = '9999';
    getEl('export-tickers').value = ' aapl, btc-usd ;<script> ';
    UI.downloadNewsExport();
    assert.equal(opened.length, 1);
    assert.equal(opened[0][0], '/api/news-tab/export?days=365&tickers=AAPL%2CBTC-USD%2CSCRIPT&impact=critical%2Chigh%2Cmedium%2Clow');
    assert.equal(opened[0][2], 'noopener');
    opened.length = 0;
    exportBoxes.forEach(b => { b.checked = false; });
    UI.downloadNewsExport();
    assert.equal(opened.length, 0, 'no impact level ticked must not export everything');
    assert.equal(toasts.at(-1).type, 'warning');

    const data = { breaking_news: [], market_news: [], ticker_news: { AAPL: [art('js', { url: 'javascript:alert(1)' }), art('ok', { url: 'https://example.com/a' })] } };
    setCache(data);
    opened.length = 0;
    State.selectedArticleId = 'js';
    UI.openSelectedNewsSource();
    assert.equal(opened.length, 0, 'only http(s) links are opened');
    State.selectedArticleId = 'ok';
    UI.openSelectedNewsSource();
    assert.deepEqual(Array.from(opened[0]), ['https://example.com/a', '_blank', 'noopener,noreferrer']);
    UI.renderInsightPanel(data, { force: true, skipAutoBriefing: true });
    assert(!html('news-insight-panel').includes('javascript:'));
}

// ========================================================= 11. source names
assert.equal(UI.formatNewsSourceName('eToro (@Jeffearner)'), 'eToro (@Jeffearner)', 'not "EToro"');
assert.equal(UI.formatNewsSourceName('yahoo_rss'), 'Yahoo Finance');
assert.equal(UI.formatNewsSourceName('coindesk'), 'CoinDesk');
assert.equal(UI.formatNewsSourceName('some_new_feed'), 'Some New Feed');
assert.equal(UI.formatNewsSourceName(''), 'Unknown source');

// ===== 11b. full cockpit render, source groups, keyboard access, markup
resetUi();
{
    const data = {
        breaking_news: [], market_news: [], last_update: ago(1), settings: { news_refresh_minutes: 5 },
        ticker_news: { AAPL: [art('e1', { source: 'eToro (@a)' }), art('e2', { source: 'eToro (@b)' }), art('y1')] },
    };
    setCache(data);
    State.focusMode = 'all';
    State.selectedTicker = 'AAPL';                 // ticker filter restored from localStorage
    consoleErrors.length = 0;
    UI.renderNewsCockpit(data);
    assert.deepEqual(consoleErrors, [], 'the cockpit renders without errors');
    assert.equal(getEl('news-filters-count').textContent, '1', 'a restored filter shows in the badge on the first render');
    assert.equal(getEl('news-filters-count').hidden, false);
    assert(html('news-last-updated').includes('data-news-ts="') && html('news-last-updated').includes('Synced 1m ago'));
    assert.equal(getEl('news-page-feed-content').attrs['aria-busy'], 'false');

    const sourceOptions = getEl('news-source-select').options.map(o => o.textContent);
    assert(sourceOptions.includes('eToro community') && sourceOptions.includes('eToro (@a)'));
    assert.equal(sourceOptions[0], 'ALL SOURCES');
    UI.onNewsSourceSelectionChange('eToro community');
    assert.equal(count(html('news-page-feed-content'), 'data-article-id='), 2, 'the publisher group filters all its authors');
    assert.equal(store.get('news_source_filter'), 'eToro community');
    UI.onNewsSourceSelectionChange('ALL');

    // keyboard: cards are focusable and open with Enter / Space
    const card = UI.renderNewsCard(art('y1'));
    assert(card.includes('role="button" tabindex="0"') && card.includes('onkeydown="UI._onNewsCardKey(event, &quot;y1&quot;)"'));
    assert(card.includes('aria-pressed="false"') && card.includes('aria-label="Save story"'));
    const node = {};
    let prevented = 0;
    UI._onNewsCardKey({ key: 'a', target: node, currentTarget: node, preventDefault() { prevented += 1; } }, 'e2');
    assert.notEqual(State.selectedArticleId, 'e2');
    UI._onNewsCardKey({ key: 'Enter', target: {}, currentTarget: node, preventDefault() { prevented += 1; } }, 'e2');
    assert.notEqual(State.selectedArticleId, 'e2', 'a key pressed on the bookmark button does not open the story');
    UI._onNewsCardKey({ key: ' ', target: node, currentTarget: node, preventDefault() { prevented += 1; } }, 'e2');
    assert.equal(State.selectedArticleId, 'e2');
    assert.equal(prevented, 1);

    // the briefing panel button toggles the Hot List and says so
    UI.renderInsightPanel(data, { force: true, skipAutoBriefing: true });
    assert(html('news-insight-panel').includes('Hot list') && !/watchlist\s*<\/button>/i.test(html('news-insight-panel')));
}
{
    const page = fs.readFileSync(path.resolve(__dirname, '../../frontend/index.html'), 'utf8');
    const start = page.indexOf('id="view-news"');
    const newsView = page.slice(start, page.indexOf('id="news-legacy-elements"', start));
    for (const category of ['EARNINGS', 'FED', 'FDA', 'MERGER', 'ANALYST', 'MACRO', 'SECTOR', 'COMMODITY', 'CRYPTO', 'BLOCKCHAIN', 'IPO', 'DIVIDEND', 'GENERAL']) {
        assert(newsView.includes(`<option value="${category}">`), `category filter offers ${category} (a backend category)`);
    }
    assert(!/<a\s[^>]*target="_blank"(?![^>]*rel="[^"]*noopener)/i.test(newsView), 'no target=_blank link without rel=noopener');
}

// ============ 12. async flows: briefing request, refresh, ticker panel
(async () => {
    resetUi();
    const post = art('b1', { summary: LONG_POST });
    let data = { breaking_news: [], market_news: [], ticker_news: { AAPL: [post] } };
    setCache(data);
    State.selectedArticleId = 'b1';
    const find = () => UI._flattenNewsArticles(context.__getNewsCache()).find(a => a.id === 'b1');
    const realAnalyze = Actions.analyzeNews;

    // Every outcome that is not a real briefing is a visible, retryable failure.
    Actions.analyzeNews = async () => { Actions._lastNewsAnalysisError = 'the analysis timed out after 3 minutes'; return null; };
    await UI.ensureEssentialNewsBriefing(find());
    assert(State.briefingFailures.has('b1'));
    assert.equal(State.briefingFailureReasons.get('b1'), 'the analysis timed out after 3 minutes');
    assert.equal(State.briefingActiveId, null, 'the spinner state always ends');
    assert(html('news-insight-panel').includes('AI briefing failed: the analysis timed out after 3 minutes'));

    UI.selectNewsArticle('b1');            // re-selecting clears the failure and allows a retry
    assert(!State.briefingFailures.has('b1') && !State.briefingAttempted.has('b1'));
    Actions.analyzeNews = async () => ({ analysis: { ...ESSENTIAL, source: 'pre_computed_metadata' }, fallback: true });
    await UI.ensureEssentialNewsBriefing(find());
    assert.match(State.briefingFailureReasons.get('b1'), /AI model is not reachable/);

    UI.selectNewsArticle('b1');
    Actions.analyzeNews = async () => ({ analysis: { briefing_version: 3, tldr: 'x' } });
    await UI.ensureEssentialNewsBriefing(find());
    assert.match(State.briefingFailureReasons.get('b1'), /did not contain a usable briefing/);

    // A real briefing survives the next state poll.
    UI.selectNewsArticle('b1');
    Actions.analyzeNews = async () => ({ analysis: ESSENTIAL });
    await UI.ensureEssentialNewsBriefing(find());
    assert(!State.briefingFailures.has('b1'));
    assert(html('news-insight-panel').includes('Essential answer'));
    data = { breaking_news: [], market_news: [], ticker_news: { AAPL: [art('b1', { summary: LONG_POST })] } };
    setCache(data);                        // the server sends the article without the briefing
    UI.renderInsightPanel(data, { force: true, skipAutoBriefing: true });
    assert(html('news-insight-panel').includes('Essential answer'), 'a generated briefing must survive the next state poll');
    assert(UI.renderNewsCard(find()).includes('<b>Takeaway:</b> Supplier guidance cut hits the chain'));

    // Actions.analyzeNews keeps the server's reason.
    Actions.analyzeNews = realAnalyze;
    fetchImpl = async () => ({ ok: false, status: 503, json: async () => ({ error: 'Ollama is not reachable.' }) });
    assert.equal(await Actions.analyzeNews('https://example.com/a', null, false, 'summary', 'AAPL', 'b1'), null);
    assert.equal(Actions._lastNewsAnalysisError, 'Ollama is not reachable.');
    fetchImpl = async () => { throw new Error('Request timed out after 180000ms'); };
    assert.equal(await Actions.analyzeNews('https://example.com/a', null, false, 'summary', 'AAPL', 'b1'), null);
    assert.equal(Actions._lastNewsAnalysisError, 'the analysis timed out after 3 minutes');

    // Deep analysis: a failure is reported, not silent.
    toasts.length = 0;
    await UI.runDeepAnalysisSelected(true);
    assert.equal(toasts.at(-1).type, 'error');
    assert.match(toasts.at(-1).message, /Deep analysis failed: the analysis timed out/);
    assert.equal(State.deepAnalysisActiveId, null);

    // Refresh: no false success.
    resetUi();
    setCache({ breaking_news: [], market_news: [], ticker_news: {}, last_update: ago(3) });
    fetchImpl = async () => ({ ok: false, status: 500, json: async () => ({ detail: 'Internal error' }) });
    await UI.refreshNewsPage({ target: null });
    assert.deepEqual(toasts.map(t => t.message), ['Failed to refresh news'], 'an HTTP error is an error, not "refresh started"');
    toasts.length = 0;
    fetchImpl = async () => ({ ok: true, status: 200, json: async () => ({ status: 'disabled', disabled: true }) });
    await UI.refreshNewsPage({ target: null });
    assert.equal(toasts[0].type, 'warning');
    assert.match(toasts[0].message, /disabled/);

    // Ticker panel: the clicked story is the one shown.
    resetUi();
    const feedData = { breaking_news: [], market_news: [], ticker_news: { AAPL: [art('feed1', { impact: 'high' })] } };
    setCache(feedData);
    State.selectedArticleId = 'feed1';
    context.API.getNewsTicker = async () => ({ news: [art('deep1', { title: 'Only in the ticker endpoint', ticker: 'NVDA' })] });
    await UI.showTickerNewsPanel('NVDA');
    assert(html('news-insight-panel').includes('Only in the ticker endpoint'));
    UI.selectNewsArticle('deep1');
    assert(html('news-insight-panel').includes('Only in the ticker endpoint'), 'not the first feed article');
    assert(!html('news-insight-panel').includes('Headline feed1'));
    // a failed request is reported as a failure, not as "no news"
    State.extraArticles.clear();
    setCache({ breaking_news: [], market_news: [], ticker_news: {} });
    State.selectedArticleId = null;
    context.API.getNewsTicker = async () => { throw new Error('boom'); };
    await UI.showTickerNewsPanel('<b>ZZ');
    assert(html('news-insight-panel').includes('Could not load &lt;B&gt;ZZ news'));
    assert(!html('news-insight-panel').includes('<B>ZZ') && !html('news-insight-panel').includes('No news found'));

    console.log('News tab frontend audit (counts, labels, filters, briefing states, escaping, persistence): PASS');
})().catch(error => { console.error(error); process.exit(1); });
