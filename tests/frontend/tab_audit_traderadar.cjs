// Trade Radar tab audit 2026-11-15 — render regressions.
// Runs the real UI.renderTradeRadar from frontend/app.js against a fake DOM.
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(require('path').resolve(__dirname, '../../frontend/app.js'), 'utf8');
const start = source.indexOf('UI.formatRadarPrice = function');
const end = source.indexOf('UI.startTradeRadarAutoRefresh = function', start);
assert(start >= 0 && end > start, 'could not locate the Trade Radar render block');

const els = {
    'traderadar-list': { innerHTML: '' },
    'traderadar-meta': { innerHTML: '' },
    'traderadar-summary-strip': { innerHTML: '' },
    'traderadar-symbol-filter': { innerHTML: '', value: 'ALL' },
};
const context = {
    UI: { tradeRadarExpanded: new Set(), scheduleRender() {}, showToast() {} },
    API: {}, Actions: {}, ViewRouter: { activeView: 'traderadar' },
    document: { getElementById: id => els[id] || null },
    window: {}, console,
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
const UI = context.UI;

// ── price precision ───────────────────────────────────────────────────────
assert.equal(UI.formatRadarPrice(0.004831), '0.004831');
assert.equal(UI.formatRadarPrice(0.00463776), '0.004638');
assert.equal(UI.formatRadarPrice(0.00497593), '0.004976');
assert.equal(UI.formatRadarPrice(0.02841), '0.02841');
assert.equal(UI.formatRadarPrice(0.5273), '0.5273');
assert.equal(UI.formatRadarPrice(79.17), '79.17');
assert.equal(UI.formatRadarPrice(0.00000073), '0.0000007300');
assert.equal(UI.formatRadarPrice(0), '—');
// the on-screen distance must match the real one (was 4.2% / 4.2% for a real 4.0% / 3.0%)
const shown = (a, b) => (Number(UI.formatRadarPrice(a)) - Number(UI.formatRadarPrice(b))) / Number(UI.formatRadarPrice(b)) * 100;
assert(Math.abs(shown(0.00463776, 0.004831) - (-4.0)) < 0.05, 'target distance distorted by rounding');
assert(Math.abs(shown(0.00497593, 0.004831) - 3.0) < 0.05, 'stop distance distorted by rounding');

const now = new Date().toISOString();
const scored = (symbol, extra = {}) => ({
    symbol, source: 'binance', change_pct: -58.36, price: 0.02839, last_seen: now, last_processed: now,
    analysis_pending: false,
    analysis: {
        verdict: 'SHORT', confidence: 59, time_horizon: 'intraday', strength: 70, strength_estimated: true,
        strategy: { risk_mode: 'MEDIUM', trade_mode: 'PULLBACK', trade_mode_source: 'default', setup_label: 'MOMENTUM' },
        entry: 0.02841, tp: 0.0272736, sl: 0.0292623, levels_basis: 'default_pct', indicator_timeframe: null,
        key_indicators: { rsi: null, adx: null, macd_hist: null, atr: null },
        confidence_breakdown: {
            base_strength: 70, news_alignment_bonus: 0, calendar_risk_penalty: 0, backtest_bonus: 0,
            final_confidence: 59, evidence_cap: 59, formula: 'base + news - calendar + backtest (capped: strength estimated)',
            base_estimated: true, news_assessed: false, calendar_assessed: false, backtest_assessed: false,
        },
    },
    news: { available: false, checked: false, bullish_count: 0, bearish_count: 0, top: [] },
    calendar: { available: false, checked: true, macro_checked: false, earnings: {}, macro_next_72h: [] },
    backtest: { available: false, unavailable_reason: 'no_price_history', days: 30, win_rate: 0, profit_factor: 0, closed_trades: 0 },
    reasons: [
        `${symbol} moved -58.36% (threshold 15.00%).`,
        'Direction SHORT follows the move - no strategy signal for this symbol; strength 70/100 is estimated from the move size.',
        'Confidence math: base 70.0 + news 0.0 - calendar 0.0 + backtest 0.0 = 70.0.',
        'Evidence cap 59: strength estimated — confidence capped below the entry gate until a strategy signal confirms it.',
    ],
    red_flags: ['analysis_strength_estimated', 'missing_ohlc', 'macro_calendar_unavailable'],
    readiness: { status: 'BLOCKED', can_open: false, distance_to_open: 1, entry_confidence_threshold: 60,
        blockers: ['analysis_strength_estimated', 'missing_ohlc'], note: 'Insufficient validated data for entry.' },
    data_freshness: { scanner_ts: now, ohlc_ts: null, news_ts: null },
    ...extra,
});
const queued = (symbol) => ({
    symbol, source: 'binance', change_pct: -16.56, price: 0.0917, last_seen: now, last_processed: null,
    analysis_pending: true,
    analysis: { verdict: 'NEUTRAL', confidence: 0, time_horizon: 'intraday', strategy: { risk_mode: 'MEDIUM', trade_mode: 'PULLBACK' }, entry: 0, tp: 0, sl: 0, strength: 0 },
    news: {}, calendar: {}, backtest: {}, reasons: [], red_flags: [],
    readiness: { status: 'PENDING', can_open: false, blockers: [], note: 'Queued for analysis — not scored yet.' },
    data_freshness: {},
});
const render = (candidates, extra = {}) => {
    UI.tradeRadarExpanded = new Set(candidates.map(c => c.symbol));
    UI.renderTradeRadar({ as_of: now, config: {}, suppressed: {}, meta: { processed: 4, queued: 2 }, total_candidates: candidates.length, candidates, disabled: false, ...extra });
    return { list: els['traderadar-list'].innerHTML, meta: els['traderadar-meta'].innerHTML, summary: els['traderadar-summary-strip'].innerHTML };
};
const cardOf = (html, symbol) => {
    const at = html.indexOf(`<div class="traderadar-symbol">${symbol}`);
    assert(at >= 0, `card for ${symbol} missing`);
    const next = html.indexOf('<article', at);
    return html.slice(at, next === -1 ? undefined : next);
};

// ── pending candidates are not "scored 0 / BLOCKED / PULLBACK" ─────────────
let out = render([scored('KAVAUSDT'), scored('SHELLUSDT', { analysis: { ...scored('X').analysis, confidence: 53 } }), queued('BERAUSDT')]);
const pendingCard = cardOf(out.list, 'BERAUSDT');
assert(pendingCard.includes('PENDING'), 'un-scored candidate must read PENDING');
assert(pendingCard.includes('Analysis pending'), 'pending explanation missing');
assert(!pendingCard.includes('BLOCKED'), 'un-scored candidate must not read BLOCKED');
assert(!pendingCard.includes('PULLBACK'), 'un-scored candidate must not carry a setup label');
assert(!pendingCard.includes('NEUTRAL'), 'un-scored candidate must not carry a verdict');
// summary: (59 + 53) / 2 = 56, the pending zero is not averaged in (was 37)
assert(/Avg confidence<\/div>\s*<div class="traderadar-summary-value">56</.test(out.summary), out.summary);
assert(out.summary.includes('1 awaiting analysis'), 'pending count missing from the summary');
assert(out.summary.includes('>0L<') && out.summary.includes('>2S<'), 'direction split must count analysed cards only');

out = render([queued('BERAUSDT')]);
assert(out.summary.includes('no analysed candidates'), 'average of nothing must not print 0');

// ── truthful setup label, gate line, levels, unassessed components ─────────
out = render([scored('KAVAUSDT')]);
let card = cardOf(out.list, 'KAVAUSDT');
assert(card.includes('MOMENTUM · unconfirmed'), 'setup label must come from setup_label and be marked unconfirmed');
assert(!/traderadar-panel-subline[^>]*>PULLBACK/.test(card), 'placeholder PULLBACK must not be shown as the setup');
// UI v10: the Blocked chip sits beside the line, so the line is the first blocker in words + a "+N more" control.
assert(/traderadar-gate-line[^>]*>No strategy signal \(strength estimated\) <button[^>]*>\+1 more<\/button>/.test(card), 'gate line must name the real blocker');
assert(!card.includes('entry gate · 60'), 'a data-blocked card must not read as one point short of the gate');
assert(card.includes('$0.02841') && card.includes('$0.02727') && card.includes('$0.02926'), 'sub-$1 levels lost precision');
assert(card.includes('no ATR · flat %'), 'flat-percentage levels must be labelled');
assert(card.includes('Est. strength 70/100'), 'estimated strength must not be presented as "Tech"');
assert(!card.includes('Tech 70/100'));
assert(card.includes('Capped at 59: strength estimated'), 'cap chip must name the cause without being cut mid-word');
assert(card.includes('Base (est.)'));
assert((card.match(/>n\/a<\/span>/g) || []).length >= 3, 'news/calendar/backtest components must read n/a when not assessed');
assert(card.includes('Backtest-lite: <b>unavailable</b> (no price history)'), 'backtest must be shown as unavailable, not 0 trades');
assert(!card.includes('Variant:'), 'meaningless variant label must be gone');
assert(card.includes('Macro calendar unavailable — event risk not assessed.'));
assert(card.includes('News unavailable for this symbol.'));
// UI v10: unavailable values read "n/a" everywhere (3 unassessed components + 4 null indicators).
assert((card.match(/>n\/a<\/span>/g) || []).length >= 7, 'null indicators must read n/a, never a number');

// ── measured data is shown as measured ────────────────────────────────────
const measured = scored('VEON', {
    change_pct: 5.35, price: 79.17,
    news: { available: true, checked: true, bullish_count: 2, bearish_count: 1,
        top: [{ title: '<img src=x onerror=alert(1)> VEON beats', sentiment: 'bullish', published_at: now }] },
    calendar: { available: true, checked: true, macro_checked: true, earnings: { date: '2026-11-16', when: 'bmo' },
        macro_next_72h: [{ title: 'CPI <b>y/y</b>', date: now, impact: 'high' }] },
    backtest: { available: true, days: 30, win_rate: 33.3, profit_factor: null, max_drawdown: 4.15, closed_trades: 3 },
});
measured.analysis = { ...measured.analysis, verdict: 'LONG', strength_estimated: false, confidence: 71,
    strategy: { risk_mode: 'MEDIUM', trade_mode: 'BREAKOUT', trade_mode_source: 'watchlist_strategy', setup_label: 'BREAKOUT' },
    entry: 79.17, tp: 79.49, sl: 78.93, levels_basis: 'atr', indicator_timeframe: '5m',
    key_indicators: { rsi: 61.234, adx: 27.5, macd_hist: 0.0123, atr: 0.16 },
    confidence_breakdown: { base_strength: 61, news_alignment_bonus: 3, calendar_risk_penalty: 5, backtest_bonus: 0,
        final_confidence: 71, formula: 'base + news - calendar + backtest', base_estimated: false,
        news_assessed: true, calendar_assessed: true, backtest_assessed: true } };
measured.red_flags = [];
measured.readiness = { status: 'READY', can_open: true, distance_to_open: 0, entry_confidence_threshold: 60, blockers: [], note: 'ok' };
measured.reasons = ['VEON moved +5.35% (threshold 5.00%).', 'Technical verdict LONG via BREAKOUT, strength 61/100.',
    'Backtest-lite 12d: WR 33.3%, PF n/a (no losing trades) (3 trades) [RADAR_QUICK].'];
out = render([measured]);
card = cardOf(out.list, 'VEON');
assert(card.includes('>BREAKOUT<'), 'strategy mode must be shown as-is');
assert(!card.includes('unconfirmed'));
assert(card.includes('entry gate · 60'));
assert(card.includes('ATR 5m based'));
assert(card.includes('Tech 61/100'));
assert(card.includes('BT 12d: WR 33% · PF n/a'), 'backtest chip must use the real window and a null PF');
assert(card.includes('RSI <b>61.2</b>') && card.includes('ADX <b>27.5</b>'));
assert(card.includes('profit factor <b>n/a (no losing trades)</b>'));
assert(card.includes('small sample'));
assert(card.includes('+3.0') && card.includes('-5.0'), 'assessed components keep their numbers');
// third-party text is escaped
assert(card.includes('&lt;img src=x onerror=alert(1)&gt; VEON beats'), 'news title must be escaped');
assert(!card.includes('<img src=x'), 'raw news HTML leaked into the card');
assert(card.includes('CPI &lt;b&gt;y/y&lt;/b&gt;'), 'calendar title must be escaped');
assert(out.summary.includes('>1<span class="traderadar-summary-sub">entry-ready'));

// ── stale movers get an age chip well before two hours ────────────────────
const aged = scored('PUMPUSDT', { last_seen: new Date(Date.now() - 40 * 60000).toISOString() });
card = cardOf(render([aged]).list, 'PUMPUSDT');
assert(card.includes('40m old'), 'a mover last confirmed 40 minutes ago must carry its age');
card = cardOf(render([scored('KAVAUSDT')]).list, 'KAVAUSDT');
assert(!card.includes('traderadar-age-chip'), 'a mover confirmed this cycle carries no age chip');

// ── suppressed symbols: listed with an Undo, expired ones ignored ─────────
out = render([scored('KAVAUSDT')], { suppressed: {
    SHELLUSDT: new Date(Date.now() + 3600000).toISOString(),
    OLDUSDT: new Date(Date.now() - 60000).toISOString(),
} });
assert(out.meta.includes('Hidden by you (1)'), 'active suppression must be listed');
assert(out.meta.includes("Actions.unsuppressTradeRadarSymbol('SHELLUSDT')"), 'Undo control missing');
assert(!out.meta.includes('OLDUSDT'), 'expired suppression must not be listed');
assert(!render([scored('KAVAUSDT')]).meta.includes('Hidden by you'));

// ── wiring ────────────────────────────────────────────────────────────────
assert(source.includes('/traderadar/unsuppress'), 'un-suppress API call missing');
assert(source.includes('Actions.unsuppressTradeRadarSymbol = async function'), 'un-suppress action missing');
assert(/mode === 'noop'/.test(source), 'a rate-limited Refresh must not claim a refresh started');

console.log('Trade Radar: precision, pending state, setup label, unassessed components, escaping, age chip, suppress/undo: PASS');
