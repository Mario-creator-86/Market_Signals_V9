// Wave 3 — "frontend-states": data states the backend sends that the UI did not render,
// bulk/partial results, the add-anyway flow, scoped filter chips, calendar coverage notes,
// wording, and the leftover unstyled pieces of the redesign.
// Runs the real code from frontend/*.js in a vm with minimal fake DOMs.
//   node tests/frontend/wave3_frontend_states.cjs
const fs = require('fs'), vm = require('vm'), path = require('path'), assert = require('assert/strict');
const read = (name) => fs.readFileSync(path.resolve(__dirname, '../../frontend', name), 'utf8');
const source = read('app.js');
const agentsSource = read('agents.js');
const cockpitSource = read('cockpit.js');
const indexHtml = read('index.html');
const sharedCss = read('ds_view_shared.css');

function slice(startMarker, endMarker, text = source) {
    const start = text.indexOf(startMarker);
    const end = text.indexOf(endMarker, start + startMarker.length);
    assert(start >= 0 && end > start, `markers not found: ${startMarker} .. ${endMarker}`);
    return text.slice(start, end);
}
const escapeHtml = (value) => String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
const count = (text, needle) => text.split(needle).length - 1;

(async () => {
    // =============================================== 1a. no data / delisted price
    {
        const helpers = slice('    _rowPriceState(row) {', '    // "Show all" on a board:');
        const formatPrice = slice('    formatPrice(price) {', '    _activateScannerMarquee(');
        const ctx = { cachedState: null };
        vm.createContext(ctx);
        vm.runInContext(`var UI = { ${formatPrice} ${helpers} };`, ctx);
        ctx.UI._escapeHtml = escapeHtml;
        const U = ctx.UI;

        // engine rows (core/engine.py _no_data_row): price 0 + price_available false
        const noData = U._rowPriceState({ ticker: 'CPRX', price: 0, price_available: false, data_quality: 'no_data', change_pct: 0,
            price_integrity: { status: 'no_data', reason: 'No price data is available for CPRX from any provider' } });
        assert.equal(noData.kind, 'no_data');
        assert.equal(noData.label, 'No data');
        assert.match(noData.title, /CPRX from any provider/);
        // last price of a dropped listing, with the date of that price
        const inactive = U._rowPriceState({ ticker: 'SLP', price: 4.27, data_quality: 'listing_inactive', session: 'inactive', price_as_of: '2026-09-08' });
        assert.equal(inactive.kind, 'inactive');
        assert.equal(inactive.label, 'Delisted or inactive · last traded 2026-09-08');
        assert.equal(U._rowPriceState({ price: 4.27, session: 'inactive', price_as_of: '2026-09-08T20:00:00+00:00' }).asOf, '2026-09-08');
        assert.equal(U._rowPriceState({ price: 4.27, session: 'inactive' }).label, 'Delisted or inactive · last trade date unknown');
        // normal rows, and a closed session, are not a state
        assert.equal(U._rowPriceState({ price: 356.27, data_quality: 'verified', session: 'closed' }), null);
        assert.equal(U._rowPriceState({ price: 356.27, data_quality: 'live' }), null);
        assert.equal(U._rowPriceState(null), null);
        // the API's own verdict decides when it is sent
        assert.equal(U._rowPriceState({ price: 10, price_display_state: 'last_close', session: 'closed' }), null);
        assert.equal(U._rowPriceState({ price: 10, price_display_state: 'ok', data_quality: 'no_data' }), null);
        assert.equal(U._rowPriceState({ price: 0, price_display_state: 'no_data' }).kind, 'no_data');
        assert.equal(U._rowPriceState({ price: 0, price_display_state: 'unavailable' }).label, 'No data');
        assert.equal(U._rowPriceState({ price: 3, price_display_state: 'listing_inactive', price_as_of: '2026-10-05' }).label,
            'Delisted or inactive · last traded 2026-10-05');

        // markup: text only, escaped, the last price in the tooltip and never in the cell
        const html = U._priceStateHtml(inactive, 4.27);
        assert(html.includes('class="ms-price-state ms-price-state--inactive"'), html);
        assert(html.includes('>Delisted or inactive · last traded 2026-09-08<'), html);
        assert(html.includes('title="Last price $4.27. '), html);
        assert(!/>\s*\$4\.27/.test(html), 'the last price must not be shown as a quote');
        assert(!/text-green|text-red|ds-pos|ds-neg/.test(html), 'a state is never coloured as a move');
        assert.equal(U._priceStateHtml(null), '');
        const hostile = U._priceStateHtml(U._rowPriceState({ price: 0, price_available: false, price_integrity: { reason: '"><img src=x>' } }));
        assert(!hostile.includes('<img'), 'provider text is escaped');
        assert(!U._priceStateHtml(noData, 0).includes('Last price'), 'no_data has no last price');

        // a view whose row lacks the marking falls back to the engine row
        ctx.cachedState = { tickers: [{ ticker: 'SLP', price: 4.27, data_quality: 'listing_inactive', price_as_of: '2026-09-08' }, { ticker: 'AAPL', price: 356 }] };
        assert.equal(U._tickerPriceState('slp', { ticker: 'SLP', price: 4.27 }).kind, 'inactive');
        assert.equal(U._tickerPriceState('AAPL', { ticker: 'AAPL' }), null);
        assert.equal(U._tickerPriceState('', null), null);
        ctx.cachedState = null;
        assert.equal(U._tickerPriceState('SLP', {}), null);

        // --------------------------------------------- 1b. instrument chips
        assert.match(U._instrumentChip({ instrument_type: 'tokenized_stock', underlying: 'nvda' }), />Tokenised NVDA</);
        assert.match(U._instrumentChip({ instrument_type: 'tokenized_stock' }), />Tokenised stock</);
        assert.match(U._instrumentChip({ instrument_type: 'stablecoin' }), />Stablecoin</);
        assert.match(U._instrumentChip({ is_stablecoin: true }), />Stablecoin</);
        assert.equal(U._instrumentChip({ instrument_type: 'crypto' }), '');
        assert.equal(U._instrumentChip(null), '');
        assert(!/ds-chip--(pos|neg|warn|ai|brand)/.test(U._instrumentChip({ instrument_type: 'stablecoin' })), 'neutral chip');
        assert(!U._instrumentChip({ instrument_type: 'tokenized_stock', underlying: '<b>x' }).includes('<b>'));
    }
    // every view that shows a price uses the state instead of the price and the change
    assert(source.includes("priceStateV2 ? UI._priceStateHtml(priceStateV2, d.price, pricePrefix)"), 'Watchlist row');
    assert(source.includes('const chg = priceStateV2 ? null : rowChange(d);'), 'Watchlist: a state row has no change');
    assert(source.includes('${hotPriceState ? UI._priceStateHtml(hotPriceState, quote.price)'), 'Hot List card');
    assert(source.includes('${detailPriceState ? UI._priceStateHtml(detailPriceState, item.price)'), 'Hot List detail');
    assert(source.includes('${anPriceState ? `<div class="ds-stat-note an-price-state">${UI._priceStateHtml(anPriceState, a.price)}</div>`'), 'Analyze modal price tile');
    assert(cockpitSource.includes("priceStateNote(state, 'cockpit-pos-state')"), 'Cockpit positions');
    assert(cockpitSource.includes('${statusPill(item.direction || item.status)}${priceStateNote(priceState(item))}'), 'Cockpit opportunities');
    assert(cockpitSource.includes("const pnlTone = (state || finite(item.unrealized_pnl) === null) ? 'flat'"), 'Cockpit: no P/L colour on a state row');
    assert(source.includes("['LIVE', 'CACHED', 'CLOSED', 'LAST', 'DELAYED', 'STALE'].includes(priceFreshness)"), 'live-prices freshness LAST is accepted');
    for (const call of ["${typeof UI._instrumentChip === 'function' ? UI._instrumentChip(item) : ''}${ageChip}", "UI._instrumentChip(row) : ''}\r\n                        <span class=\"capital-direction-chip", "UI._instrumentChip(row) : ''}\r\n                            <span class=\"capital-opportunity-score\">"]) {
        assert(source.includes(call) || source.includes(call.replace(/\r\n/g, '\n')), `instrument chip missing: ${call.slice(0, 50)}`);
    }

    // ======================================================== 1c. agents
    {
        const fn = slice('    function computeAgentState(key, enabled, runtime, bot) {', '    // Single source of truth for "can a new paper entry open right now"', agentsSource);
        const ctx = {};
        vm.runInNewContext(`const AGENTS = { news: {}, risk: { masterBound: true } }; const BENIGN_PAUSES = new Set(['market_closed']);\n${fn}\nthis.compute = computeAgentState;`, ctx);
        const running = { running: true, next_run_in: 60 };
        assert.equal(ctx.compute('news', true, running, {}).group, 'running');
        const failed = ctx.compute('news', true, { ...running, last_run_ok: false, last_run_error: 'HTTP 500', consecutive_failures: 3 }, {});
        assert.equal(failed.group, 'attention', 'a failed last run needs attention even while the worker thread is alive');
        assert.equal(failed.label, 'Last run failed');
        assert.equal(failed.key, 'error');
        assert.equal(ctx.compute('news', true, { ...running, last_run_ok: null }, {}).group, 'running', 'null = not recorded yet');
        assert.equal(ctx.compute('news', true, { ...running, last_run_ok: true }, {}).group, 'running');
        assert.equal(ctx.compute('news', false, { last_run_ok: false }, {}).group, 'disabled');
        // the row says when and why, escaped, and does not repeat the same error twice
        assert(agentsSource.includes('`Last run failed${runFailedAt ? ` ${relativeTime(runFailedAt).toLowerCase()}` : \'\'}${runError ? `: ${runError}` : \'\'}${runFailures > 1 ? ` (${runFailures} runs in a row)` : \'\'}`'));
        assert(agentsSource.includes('data-testid="agent-last-run-failed"') && agentsSource.includes('${escapeHtml(runFailedNote)}</p>'));
        assert(agentsSource.includes('error && !benignError && !errorRepeatsRun'));
        assert(agentsSource.includes("const attentionCount = cards.filter((card) => card.status.group === 'attention').length;"), '"need attention" counts the status group');
    }

    // ======================================================= 1d. capital
    {
        const helpers = slice('// --- Capital tab audit helpers (begin) ---', '// --- Capital tab audit helpers (end) ---');
        const dateHelper = slice('    _fmtDateTime(value', '    initWatchlistQuickAiCache() {');
        const ctx = { Number, Array, Object, String, Date, JSON, Math };
        vm.createContext(ctx);
        vm.runInContext(`var UI = { ${slice('    formatPrice(price) {', '    _activateScannerMarquee(')} ${dateHelper} };`, ctx);
        ctx.UI._escapeHtml = escapeHtml;
        vm.runInContext(`${helpers}\n${slice('UI._formatCapitalMetricNumber = function', 'UI._formatCapitalMetricPercent = function')}`, ctx);
        const U = ctx.UI;

        // validation "unavailable" / null score is n/a, never 0.0
        assert.equal(U._capitalValidationScoreText({ validation_status: 'unavailable', validation_score: null }), 'n/a');
        assert.equal(U._capitalValidationScoreText({ validation_score_status: 'unavailable', validation_score: 0 }), 'n/a');
        assert.equal(U._capitalValidationScoreText({ validation_score: null }), 'n/a');
        assert.equal(U._capitalValidationScoreText({}), 'n/a');
        assert.equal(U._capitalValidationScoreText({ validation_status: 'validated', validation_score: 71.26 }), '71.3');
        assert.equal(U._capitalValidationScoreText({ validation_score: 0 }), '0.0', 'a measured zero stays a zero');
        assert(source.includes('${esc(UI._capitalValidationScoreText(scoring))}</strong>'));

        // trades excluded from the evidence
        const excluded = { type: 'CLOSE_POSITION', metadata: { position_snapshot: { evidence_excluded: true, evidence_excluded_reason: 'exit filled 3 days after the stop was hit "late"' } } };
        assert.equal(U._capitalEvidenceExclusion(excluded).label, 'Excluded from evidence · exit delayed');
        const chip = U._capitalEvidenceExclusionChip(excluded);
        assert(chip.includes('class="ds-chip ms-evidence-excluded"') && chip.includes('>Excluded from evidence · exit delayed<'), chip);
        assert(chip.includes('title="exit filled 3 days after the stop was hit &quot;late&quot;"'), 'the reason is the tooltip, escaped');
        assert(!/ds-chip--/.test(chip), 'neutral chip');
        assert.equal(U._capitalEvidenceExclusionChip({ type: 'CLOSE_POSITION', metadata: {} }), '');
        assert.equal(U._capitalEvidenceExclusion({ evidence_excluded: true }).label, 'Excluded from evidence · exit delayed');
        assert.equal(U._capitalEvidenceExclusion({ metadata: { evidence_excluded: false } }), null);
        assert(count(source, '${UI._capitalEvidenceExclusionChip(') === 2, 'ledger row and closed-position audit render the chip');

        // win-rate context line
        assert.equal(U._capitalExcludedTrades({ excluded_trades: 3, excluded_net_pnl: -41.5 }).text, '3 trades excluded (-$41.50)');
        assert.equal(U._capitalExcludedTrades({ excluded_trades: 1, excluded_pnl: 12 }).text, '1 trade excluded (+$12.00)');
        assert.equal(U._capitalExcludedTrades({ excluded_trades: { count: 2, net_pnl: 0 } }).text, '2 trades excluded ($0.00)');
        assert.equal(U._capitalExcludedTrades({ excluded_trades: [{ net_pnl: 5 }, { net_pnl: -7.25 }] }).text, '2 trades excluded (-$2.25)');
        assert.equal(U._capitalExcludedTrades({ excluded_trades: 4 }).text, '4 trades excluded', 'no amount is invented');
        assert.equal(U._capitalExcludedTrades({ excluded_trades: 0 }), null);
        assert.equal(U._capitalExcludedTrades({}), null);
        const view = U._capitalWinRateView({ win_rate_summary: { trades: 297, win_rate_pct: 39.5, breakeven_win_rate_pct: 46.7, excluded_trades: 3, excluded_net_pnl: -41.5 } });
        assert.equal(view.sub, '297 closed trades · needs 46.7% to break even · 3 trades excluded (-$41.50)');
        assert.match(view.title, /3 closed trades are left out of this win rate/);
        assert.equal(U._capitalWinRateView({ win_rate_summary: { trades: 2, win_rate_pct: 100, breakeven_win_rate_pct: null } }).sub, '2 closed trades');

        // exit condition met since
        const since = '2026-11-14T21:05:00Z';
        const waiting = { market_state: { market_hours_enforced: true, market_open: false }, exit_condition_since: since };
        const reached = U._capitalLevelDistanceHtml('target', -3.2, waiting);
        assert(reached.includes('target reached · closes at market open'), reached);
        assert(reached.includes(`<span class="ms-exit-since"`) && reached.includes(`since ${U._fmtDateTime(Date.parse(since))}`), reached);
        assert.equal(U._capitalLevelDistanceHtml('target', 3.21, {}), '<span class="text-green">3.2% away</span>', 'unchanged without the field');
        assert(!U._capitalLevelDistanceHtml('stop', -1, { exit_condition_since: since, exit_condition: 'take_profit' }).includes('ms-exit-since'),
            'the time belongs to the level that was met');
        assert(U._capitalLevelDistanceHtml('target', 0.4, { exit_condition_since: since, exit_condition: 'take_profit' }).includes('met since'),
            'a condition met earlier is still stated when the price moved back');
        assert(!U._capitalLevelDistanceHtml('target', -1, { exit_condition_since: 'garbage' }).includes('ms-exit-since'));

        // equity curve: what each stretch of samples is
        const daily = (date) => ({ date, total_equity: 1, resolution: 'daily' });
        const fine = (date) => ({ date, total_equity: 1, resolution: '5m' });
        const coverage = U._capitalEquityCoverage([daily('2026-10-01T00:00:00Z'), daily('2026-10-20T00:00:00Z'), fine('2026-11-15T10:00:00Z'), fine('2026-11-15T10:05:00Z')], null);
        assert.match(coverage, /^Daily samples Oct 1 – Oct 20 \(2 samples\) · 5-minute samples Nov 15 \(2 samples\)$/);
        assert.equal(U._capitalEquityCoverage([{ date: '2026-11-15T10:00:00Z', total_equity: 1 }], null), '', 'nothing is claimed without the field');
        assert.match(U._capitalEquityCoverage([], { segments: [{ resolution: 'hourly', start: '2026-11-01T00:00:00Z', end: '2026-11-15T00:00:00Z', samples: 300 }] }),
            /^Hourly samples Nov 1 – Nov 15 \(300 samples\)$/);
        assert(source.includes('data-testid="capital-equity-coverage"'));
    }

    // =================================================== 1e. trade radar
    {
        const start = source.indexOf('UI.formatRadarPrice = function');
        const end = source.indexOf('UI.startTradeRadarAutoRefresh = function', start);
        const els = { 'traderadar-list': { innerHTML: '' }, 'traderadar-meta': { innerHTML: '' }, 'traderadar-summary-strip': { innerHTML: '' }, 'traderadar-symbol-filter': { innerHTML: '', value: 'ALL' } };
        const ctx = {
            UI: { tradeRadarExpanded: new Set(), scheduleRender() {}, showToast() {} },
            API: {}, Actions: {}, ViewRouter: { activeView: 'traderadar' },
            document: { getElementById: id => els[id] || null }, window: {}, console,
        };
        vm.createContext(ctx);
        vm.runInContext(source.slice(start, end), ctx);
        const now = new Date().toISOString();
        const candidate = (symbol, readiness, backtest, extra = {}) => ({
            symbol, source: 'binance', change_pct: 12.4, price: 1.25, last_seen: now, last_processed: now, analysis_pending: false,
            analysis: { verdict: 'LONG', confidence: 71, time_horizon: 'intraday', strength: 70, strategy: { risk_mode: 'MEDIUM', trade_mode: 'BREAKOUT', setup_label: 'BREAKOUT' },
                entry: 1.25, tp: 1.3, sl: 1.21, levels_basis: 'atr', key_indicators: {}, confidence_breakdown: { final_confidence: 71 } },
            news: {}, calendar: {}, backtest, reasons: [], red_flags: [], readiness, data_freshness: {}, ...extra,
        });
        const render = (items) => {
            ctx.UI.tradeRadarExpanded = new Set(items.map(c => c.symbol));
            ctx.UI.renderTradeRadar({ as_of: now, config: {}, suppressed: {}, meta: {}, total_candidates: items.length, candidates: items, disabled: false });
            return els['traderadar-list'].innerHTML;
        };
        const losing = { available: true, days: 30, win_rate: 33.3, profit_factor: 0.64, max_drawdown: 4.1, closed_trades: 27 };

        // backend blocker: the blocking reason in words, with its numbers
        let html = render([candidate('AAAUSDT', { status: 'BLOCKED', entry_confidence_threshold: 60, blockers: ['backtest_negative'] }, losing)]);
        assert(/traderadar-gate-line[^>]*>Backtest negative: PF 0\.64 over 27 trades</.test(html), 'gate line names the backtest blocker');
        assert(html.includes('<span class="traderadar-blocker-chip">Backtest negative: PF 0.64 over 27 trades</span>'));
        assert(!html.includes('class="traderadar-backtest-warning"'), 'the frontend-only note (which says "not a blocker") is not shown next to the blocker');
        // an object blocker carries its own detail
        html = render([candidate('BBBUSDT', { status: 'BLOCKED', blockers: [{ code: 'backtest_negative', detail: 'PF 0.41 over 12 trades' }] }, losing)]);
        assert(html.includes('>Backtest negative: PF 0.41 over 12 trades<'));
        // fallback: no backend blocker, the existing warning stays
        html = render([candidate('CCCUSDT', { status: 'READY', entry_confidence_threshold: 60, blockers: [] }, losing)]);
        assert(html.includes('class="traderadar-backtest-warning"'), 'frontend-only warning is the fallback');
        // backend verdict fields: backtest.negative decides the fallback note, non_blocking_notes are never blockers
        html = render([candidate('EEEUSDT', { status: 'READY', entry_confidence_threshold: 60, blockers: ['backtest_thin_sample'], non_blocking_notes: ['backtest_thin_sample'] },
            { available: true, days: 30, win_rate: 30, profit_factor: 0.5, max_drawdown: 2, closed_trades: 8, negative: false, thin_sample: true, negative_min_trades: 20 })]);
        assert(!html.includes('class="traderadar-backtest-warning"'), 'backtest.negative:false is not a negative backtest (8 trades < 20)');
        assert(html.includes('Backtest sample is thin: 8 trades (needs 20 to count)'));
        assert(!html.includes('traderadar-blocker-chip'));
        html = render([candidate('FFFUSDT', { status: 'READY', entry_confidence_threshold: 60, blockers: [] },
            { available: true, days: 30, win_rate: 30, profit_factor: 0.5, max_drawdown: 2, closed_trades: 25, negative: true, thin_sample: false, negative_min_trades: 20 })]);
        assert(html.includes('class="traderadar-backtest-warning"'));
        // thin sample: a quiet note, never a blocking reason
        html = render([candidate('DDDUSDT', { status: 'READY', entry_confidence_threshold: 60, blockers: ['backtest_thin_sample'] },
            { available: true, days: 30, win_rate: 50, profit_factor: 1.2, max_drawdown: 1, closed_trades: 3 })]);
        assert(html.includes('class="traderadar-panel-micro traderadar-backtest-thin"') && html.includes('Backtest sample is thin: 3 trades'), 'thin sample note');
        assert(!html.includes('traderadar-blocker-chip'), 'thin sample is not listed as a blocker');
        assert(/traderadar-gate-line[^>]*>entry gate · 60</.test(html), 'gate line is not replaced by the thin-sample code');
    }

    // ========================================================= 1f. news
    assert(source.includes('Array.isArray(selected.impact_reasons) ? selected.impact_reasons : []'));
    assert(source.includes('<span class="news-impact-reasons-label">Why this impact</span><ul>${impactReasons.map(reason => `<li>${esc(reason)}</li>`).join(\'\')}</ul>'),
        'impact reasons are listed under the impact chip, escaped');
    assert(source.includes('if (result.attached === false) NewsUIState.briefingDetached.add(article.id);'), 'attached:false is remembered');
    assert(source.includes('The server could not attach this briefing to the story'));

    // ========================================================= 2. wording
    for (const gone of ['Probability and quality', 'Score probability', 'win rate probability', '"Probability":', 'higher probability of', 'weakens the probability']) {
        assert(!source.includes(gone), `heuristic score still called a probability: ${gone}`);
    }
    assert(source.includes("label: 'Setup score and quality'") && source.includes('"Setup score": "Setup score: uncalibrated'));
    assert(source.includes("label: 'Setup score (uncalibrated, not a probability)'"));
    // the percentage of capital a position starts with is a size, not "risk"
    assert(!source.includes('Risk used') && !/Risk per trade/i.test(source), 'position size is not called risk');
    assert(source.includes('`Position size ${UI._formatCapitalMetricNumber(Number(sizing.risk_pct_used || 0), 2)}% of capital, size factor'));
    assert(indexHtml.includes('<label for="global-setting-risk-pct">Base position size (% of capital)</label>'));
    assert(indexHtml.includes('Starting size of a new position; capped by Max position size. Loss at the stop is limited by Max risk / trade.'));
    assert(source.includes("settingsProblem.startsWith('Base position size') ? 'global-setting-risk-pct'"), 'the field error still lands on its field');
    assert(!/value: `P \$\{/.test(source), 'no "P 62.0%" for a heuristic score');

    // ================================================== 3. bulk / add ticker
    {
        const helpers = slice('UI._reasonCodeText = function', '// Consolidated render function');
        const ctx = { UI: { _escapeHtml: escapeHtml } };
        vm.createContext(ctx);
        vm.runInContext(helpers, ctx);
        const U = ctx.UI;
        // reason codes of candidate / decision traces, in words
        assert.equal(U._reasonCodeText('tokenized_stock_excluded'), 'Tokenised stock: excluded from autonomous entries');
        assert.equal(U._reasonCodeText('stablecoin_excluded'), 'Stablecoin: excluded, its moves are not a trend');
        assert.equal(U._reasonCodeText('ai_low_entry_confidence'), 'AI entry confidence is below the required level');
        assert.equal(U._reasonCodeText('veto:model_veto_upheld_no_validated_score'), 'AI veto upheld: no validated score exists that could override it');
        assert.equal(U._reasonCodeText('trade_radar_blocked=backtest_negative,missing_ohlc'), 'Trade Radar blocks the entry: Backtest negative, missing ohlc');
        assert.equal(U._reasonCodeText('veto:critic_rejected'), 'AI veto: critic rejected');
        assert.equal(U._reasonCodeText('min_signal_score'), 'min signal score');
        assert.equal(U._reasonCodeText('Score 61 is below the 65 gate (min_signal_score).'), 'Score 61 is below the 65 gate (min_signal_score).', 'a sentence is left alone');
        assert.equal(U._reasonCodeText(null), '');
        assert(source.includes("UI._reasonCodeText(reasonRaw) : reasonRaw)}</div>"), 'capital pipeline row');
        assert(source.includes("tokenized_stock_excluded: 'No trade: tokenised stock, excluded from autonomous entries'"), 'capital blocked reason');
        assert(cockpitSource.includes("typeof UI._reasonCodeText === 'function') ? UI._reasonCodeText(value) : value;"), 'cockpit decision ledger');
        assert.equal(U._bulkFailureSummary({ status: 'ok', failed: [] }), null);
        assert.equal(U._bulkFailureSummary(null), null);
        const partial = U._bulkFailureSummary({ status: 'partial', failed: [{ ticker: 'slp', error: 'not_in_watchlist' }, { ticker: 'CPRX', error: 'PermissionError' }] });
        assert.equal(partial.text, 'Failed: SLP (not in watchlist), CPRX (PermissionError).');
        assert.deepEqual(Array.from(partial.tickers), ['SLP', 'CPRX']);
        assert.equal(U._bulkFailureSummary({ failed: ['AAA'] }).text, 'Failed: AAA.');
        assert.match(U._bulkFailureSummary({ status: 'partial', detail: 'Removed 1 of 2 tickers.' }).text, /Removed 1 of 2/);
        assert.match(U._bulkFailureSummary({ failed: Array.from({ length: 11 }, (_, i) => ({ ticker: `T${i}`, error: 'x' })) }).text, /and 3 more\.$/);

        // HTTP 422 {code: "no_quote", requires_force: true}
        const refused = Object.assign(new Error('No quote found for ZZZT — check the symbol'), { noQuote: true });
        assert.equal(U._addTickerOutcome(null, refused).needsForce, true);
        assert.equal(U._addTickerOutcome(null, new Error('Invalid ticker symbol')).needsForce, false);
        assert.equal(U._addTickerOutcome({ added: false, code: 'no_quote', requires_force: true, detail: 'No quote found for ZZZT — check the symbol' }, null).needsForce, true);
        assert.equal(U._addTickerOutcome({ added: false, ticker: 'AAPL' }, null).already, true);
        const unverified = U._addTickerOutcome({ added: true, verified: false, verify_status: 'unverified' }, null);
        assert.deepEqual({ ...U._addTickerToast('XYZ', unverified) }, { text: 'Added, but the symbol could not be verified (no connection)', type: 'warning' });
        assert.equal(U._addTickerToast('XYZ', U._addTickerOutcome({ added: true, verified: false, verify_status: 'forced' }, null)).text, 'XYZ added without a quote check.');
        assert.equal(U._addTickerToast('XYZ', U._addTickerOutcome({ added: true, verified: true, verify_status: 'found' }, null)).type, 'success');
    }
    {
        // API.addTicker marks the "no quote" refusal; the dialog offers Add anyway, which resends with force:true
        const apiAdd = slice('    async addTicker(ticker, options = {}) {', '    async removeTickers(tickers) {');
        const dialog = slice('// ============ ADD TICKER MODAL ============', 'Actions.saveAiIdeaToCarnet = async function');
        const helpers = slice('UI._bulkFailureSummary = function', '// Consolidated render function');
        const bodies = [];
        const toasts = [];
        const el = (id) => ({ id, value: '', hidden: true, innerHTML: '', style: {}, classList: { add() {}, remove() {} }, focus() {} });
        const dom = { 'add-ticker-modal': el('add-ticker-modal'), 'add-ticker-input': el('add-ticker-input'), 'add-ticker-message': el('add-ticker-message') };
        let answer = () => ({ ok: false, status: 422, json: async () => ({ status: 'error', ok: false, detail: 'No quote found for ZZZT — check the symbol', code: 'no_quote', added: false, requires_force: true }) });
        const ctx = {
            console: { log() {}, warn() {}, error() {} },
            fetch: async (url, init) => { bodies.push(JSON.parse(init.body)); return answer(); },
            document: { getElementById: (id) => dom[id] || null },
            UI: { _escapeHtml: escapeHtml, showToast: (text, type, options) => toasts.push({ text, type, options }), watchlistRemovedTickers: new Set(), watchlistSelectedTickers: new Set(),
                markTickerAsNew() {}, mergeStatePreserveDetails: (a, b) => b, renderWatchlist() {} },
            Actions: {}, cachedState: null,
        };
        vm.createContext(ctx);
        vm.runInContext(`var API = { BASE_URL: '/api', _resetStateCache() {}, getWatchlistCached: async () => ({ order: [] }), getState: async () => ({ watchlist: {} }), ${apiAdd} };\n${helpers}\n${dialog}`, ctx);
        dom['add-ticker-input'].value = 'zzzt';
        await ctx.Actions.confirmAddTicker();
        assert.deepEqual(bodies[0], { ticker: 'ZZZT', return_state: false }, 'first request carries no force');
        assert.equal(dom['add-ticker-message'].hidden, false);
        assert(dom['add-ticker-message'].innerHTML.includes('No quote found for ZZZT — check the symbol'), 'the server message is shown');
        assert(dom['add-ticker-message'].innerHTML.includes('onclick="Actions.confirmAddTicker({ force: true })">Add anyway</button>'), 'with an Add anyway button');
        assert.equal(toasts.length, 0, 'no false success, no generic error');
        answer = () => ({ ok: true, status: 200, json: async () => ({ ticker: 'ZZZT', added: true, verified: false, verify_status: 'forced' }) });
        await ctx.Actions.confirmAddTicker({ force: true });
        assert.deepEqual(bodies[1], { ticker: 'ZZZT', return_state: false, force: true }, 'Add anyway repeats the request with force:true');
        assert.equal(toasts[0].text, 'ZZZT added without a quote check.');
        assert.equal(dom['add-ticker-message'].hidden, true, 'the message is cleared');
        // verified:false after a lookup that could not be completed
        answer = () => ({ ok: true, status: 200, json: async () => ({ ticker: 'NEWT', added: true, verified: false, verify_status: 'unverified' }) });
        dom['add-ticker-input'].value = 'NEWT';
        await ctx.Actions.confirmAddTicker();
        assert.equal(toasts[1].text, 'Added, but the symbol could not be verified (no connection)');
        assert.equal(toasts[1].type, 'warning');
        // another refusal stays an error
        answer = () => ({ ok: false, status: 400, json: async () => ({ detail: 'Invalid ticker symbol.' }) });
        dom['add-ticker-input'].value = 'BAD';
        await ctx.Actions.confirmAddTicker();
        assert.equal(toasts[2].type, 'error');
        assert.equal(dom['add-ticker-message'].hidden, true);
    }
    assert(indexHtml.includes('<div id="add-ticker-message" class="ms-dialog-message" role="alert" hidden></div>'));
    // bulk remove and global settings name the failures instead of a generic success
    assert(source.includes('const removalFailure = UI._bulkFailureSummary(result);'));
    assert(source.includes('ticker${symbols.length === 1 ? \'\' : \'s\'} removed from Watchlist. ${removalFailure.text}`'));
    assert(source.includes('const tickerFailure = UI._bulkFailureSummary(data);'));
    assert(!source.includes("alert('Error saving global settings"), 'no alert() for a failed bulk save');
    {
        // toast with an action button
        const showToast = slice('    showToast(message, type = \'info\', options = null) {', '    renderStats(state = {}) {');
        const made = [];
        const make = (tag) => { const node = { tag, style: {}, children: [], textContent: '', appendChild(c) { this.children.push(c); }, remove() { node.removed = true; } }; made.push(node); return node; };
        const body = make('body');
        const timers = [];
        const ctx = { document: { getElementById: () => null, createElement: make, body }, setTimeout: (fn, ms) => { timers.push(ms); return 1; } };
        vm.createContext(ctx);
        vm.runInContext(`var UI = { ${showToast.replace(/,\s*$/, '')} };`, ctx);
        let clicked = 0;
        ctx.UI.showToast('ZZZT: No quote found', 'warning', { action: { label: 'Add anyway', onClick: () => { clicked += 1; } } });
        const toast = body.children[0];
        const button = toast.children[0];
        assert.equal(button.tag, 'button');
        assert.equal(button.textContent, 'Add anyway');
        assert.equal(button.className, 'ms-toast-action');
        assert(timers[0] >= 10000, 'a toast with an action stays long enough to be used');
        button.onclick();
        assert.equal(clicked, 1);
        ctx.UI.showToast('Saved', 'success');
        assert.equal(timers[1], 3000, 'plain toasts are unchanged');
        assert.equal(body.children[1].children.length, 0);
    }

    // ============================================= 4. filter chips are scoped
    {
        const setFilter = slice('    setFilter(filterName, chip) {', '    // --- Ticker Settings ---');
        const chip = (text, active) => {
            const classes = new Set(active ? ['active'] : []);
            return { textContent: text, classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) } };
        };
        const watchlistChips = [chip('All', true), chip('Crypto', false)];
        const calendarChips = [chip('All events', true), chip('Crypto', false)];
        const group = (chips) => ({ querySelectorAll: (sel) => (sel === '.filter-chip' ? chips : []) });
        const watchlistGroup = group(watchlistChips);
        watchlistChips.forEach(c => { c.closest = () => watchlistGroup; c.parentElement = watchlistGroup; });
        let documentWide = 0;
        const ctx = {
            UI: { filter: 'All', renderWatchlist() {} }, API: { getWatchlist: () => ({ then() {} }) },
            document: { querySelectorAll: () => { documentWide += 1; return [...watchlistChips, ...calendarChips]; }, getElementById: (id) => (id === 'view-watchlist' ? watchlistGroup : null) },
        };
        vm.createContext(ctx);
        vm.runInContext(`var Actions = { ${setFilter.replace(/,\s*$/, '')} };`, ctx);
        ctx.Actions.setFilter('Crypto', watchlistChips[1]);
        assert.equal(ctx.UI.filter, 'Crypto');
        assert.equal(watchlistChips[1].classList.contains('active'), true);
        assert.equal(watchlistChips[0].classList.contains('active'), false);
        assert.equal(calendarChips[0].classList.contains('active'), true, "another view's selected chip must stay selected");
        assert.equal(calendarChips[1].classList.contains('active'), false, "another view's chip with the same text must not be activated");
        assert.equal(documentWide, 0, 'no document-wide chip sweep');
        ctx.Actions.setFilter('All');   // no chip given: the watchlist view only
        assert.equal(watchlistChips[0].classList.contains('active'), true);
        assert.equal(calendarChips[0].classList.contains('active'), true);
        // the load-time binding no longer overwrites a chip that has its own handler
        assert(source.includes("if (btn.onclick || btn.hasAttribute('onclick')) return;"));
        assert(source.includes('btn.onclick = () => Actions.setFilter(btn.textContent, btn);'));
    }

    // ======================================= calendar coverage (macro schedule)
    {
        const ctx = { Actions: {}, Date, Number, Array, String, Math };
        vm.createContext(ctx);
        vm.runInContext(slice('Actions.calendarUtil = {', 'Actions.calendarState = {'), ctx);
        const notesOf = (macro) => ctx.Actions.calendarUtil.coverageNotes({
            earnings: { fetched_at: 'x', age_seconds: 60, stale: false, window_end: '2027-03-01' }, macro, ipo: { available: true },
        }, '2026-11-01', '2027-02-06', 0).map(n => `${n.level}:${n.text}`);
        let notes = notesOf({ years_missing: [], years_partial: [2027], partial_notes: ['US macro schedule for 2027 is published only through June: CPI and Jobs dates after that are not shown.'] });
        assert(notes.includes('warn:US macro schedule for 2027 is published only through June: CPI and Jobs dates after that are not shown.'), 'partial notes are warn-level notes');
        assert.equal(notes.filter(n => n.startsWith('warn:')).length, 1);
        notes = notesOf({ years_missing: [2028], years_unreadable: [2027, 2028] });
        assert.equal(notes.filter(n => /Not configured: US macro release schedule for 2028, 2027 /.test(n)).length, 1, 'an unreadable year is treated like a missing one, listed once');
        notes = notesOf({ years_missing: [], years_unreadable: [], partial_notes: [] });
        assert.equal(notes.filter(n => n.startsWith('warn:')).length, 0);
        assert.equal(notesOf({ partial_notes: [null, '  '] }).filter(n => n.startsWith('warn:')).length, 0, 'empty notes are dropped');
        // rendered through the calendar's escaper like every other note
        assert(source.includes('<span>${escCal(n.text)}</span>'));
    }

    // ============================== 6. a missing source is named, one wording
    assert(count(source, 'Not configured: IPO data provider. IPO listings are unavailable until one is set up.') === 3);
    assert(!source.includes('no IPO data provider is configured'));
    assert(source.includes('Not configured: insider-data provider.'));
    assert(source.includes('Not configured: fundamentals data provider (FMP was retired and no replacement is set up).'));
    assert(source.includes('Not configured: US macro release schedule for '));
    assert(cockpitSource.includes('Not configured: options/IV data source.'));
    assert(source.includes('<b>Not configured:</b> ${esc(group.labels.join'), 'news sources (Finnhub key) keep the same wording');

    // ============================== 5. leftover unstyled pieces of the redesign
    const literalColour = /style="[^"]*(#[0-9a-fA-F]{3,8}\b|rgba?\()/;
    assert.equal(source.split('\n').filter(line => literalColour.test(line)).length, 0, 'app.js: no hard-coded colour in an inline style');
    assert.equal(indexHtml.split('\n').filter(line => literalColour.test(line)).length, 0, 'index.html: no hard-coded colour in an inline style');
    assert(!/background:\s*white/.test(source), 'no "background: white" in templates');
    const modalOf = (id, endMarker) => slice(`<div id="${id}"`, endMarker, indexHtml);
    const tickerSettings = modalOf('ticker-settings-modal', '<!-- Strategy Notes Modal');
    const deleteConfirm = modalOf('delete-confirm-modal', '<!-- Add Ticker Modal -->');
    assert(!/style="/.test(tickerSettings), 'ticker settings: no inline styles');
    assert(!/style="/.test(deleteConfirm), 'delete confirm: no inline styles');
    for (const id of ['setting-ticker-display', 'setting-ticker-id', 'setting-signals-enabled', 'setting-min-wr', 'setting-tp', 'setting-sl', 'effective-settings-box', 'ticker-settings-form']) {
        assert(tickerSettings.includes(`id="${id}"`), `ticker settings keeps #${id}`);
    }
    for (const value of ['PRO', 'STRONG', 'MEDIUM', 'RELAX', 'OPENING_RANGE']) assert(tickerSettings.includes(`class="setting-risk${value === 'OPENING_RANGE' ? ' special-strat' : ''}" value="${value}"`));
    for (const value of ['PULLBACK', 'BREAKOUT', 'CONTRA']) assert(tickerSettings.includes(`class="setting-trade" value="${value}"`));
    assert(tickerSettings.includes('>Ticker settings · <') && tickerSettings.includes('>Allowed strategies<'), 'sentence case');
    for (const id of ['delete-confirm-message', 'delete-confirm-cancel', 'delete-confirm-btn']) assert(deleteConfirm.includes(`id="${id}"`));
    assert(deleteConfirm.includes('data-testid="delete-confirm-confirm"') && deleteConfirm.includes('class="ds-btn ds-btn--danger"'));
    // activity dropdown: classes, escaped text
    assert(source.includes('<div class="ms-activity-item">') && source.includes('${UI._escapeHtml(desc)}') && source.includes('${UI._escapeHtml(typeLabel)}'));
    assert(!source.includes('onmouseover="this.style.background'));
    // fundamentals tab: classes, sentence case, no "$N/A"
    const fundTab = slice('        fundamentalHtml = `', 'No fundamentals data available.');
    assert(fundTab.includes('Market snapshot') && fundTab.includes('Extended fundamentals'), 'the slice is the fundamentals tab');
    assert.equal(count(fundTab, 'style="'), 0, 'fundamentals tab: no inline styles');
    const labels = Array.from(fundTab.matchAll(/<div class="f-label">([^<]+)<\/div>/g), m => m[1]);
    assert(labels.length >= 35, `f-label count ${labels.length}`);
    const titleCase = labels.filter(text => /[a-z] [A-Z][a-z]/.test(text.replace(/\((YoY|avg)\)/g, '')));
    assert.deepEqual(titleCase, [], 'f-label texts are sentence case');
    assert(!fundTab.includes('$${fmtM(') && !fundTab.includes('$${fmtShort('), 'a missing amount is "N/A", not "$N/A"');
    // shared stylesheet: tokens only
    const cssBody = sharedCss.replace(/\/\*[\s\S]*?\*\//g, '');
    assert(!/#[0-9a-fA-F]{3,8}\b/.test(cssBody), 'ds_view_shared.css: no hex colours');
    assert(!/rgba?\(/.test(cssBody), 'ds_view_shared.css: no rgb literals');
    for (const selector of ['.ms-price-state', '.ms-instrument-chip', '.ms-evidence-excluded', '#ticker-settings-modal .modal-content', '.ms-activity-item', '.ms-dialog-message', '.ms-toast-action', '.news-impact-reasons']) {
        assert(sharedCss.includes(selector), `shared CSS styles ${selector}`);
    }
    assert(cssBody.split('\n').filter(line => /^[^\s@}/][^{]*\{/.test(line)).every(line => line.startsWith('html[data-ui="v10"]')), 'every rule is scoped to the v10 UI');

    // ====================================================== 7. keyboard focus
    assert(/:is\(a, button, summary, \[tabindex\], \[role="button"\], \[role="tab"\]\):focus-visible \{[^}]*box-shadow: var\(--ds-focus\) !important;/.test(sharedCss),
        'component recipes cannot remove the focus ring');
    assert(source.includes('<article class="hotlist-card" tabindex="0"') && source.includes("event.target === this) { event.preventDefault(); UI.showHotListDetail("),
        'the hot list card opens its detail from the keyboard too');

    // ============================================================ versions
    const version = (file) => Number((indexHtml.match(new RegExp(`${file.replace('.', '\\.')}\\?v=([0-9.]+)`)) || [])[1]);
    assert(version('app.js') > 39.0, 'app.js cache version bumped');
    assert(version('cockpit.js') > 1.21, 'cockpit.js cache version bumped');
    assert(version('agents.js') > 2.9, 'agents.js cache version bumped');
    assert(version('ds_view_shared.css') > 10.1, 'ds_view_shared.css cache version bumped');

    console.log('wave3_frontend_states: all assertions passed');
})().catch((error) => { console.error(error); process.exit(1); });
