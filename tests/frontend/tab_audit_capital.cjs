// Capital tab audit 2026-11-15: pure rendering helpers of the Capital tab.
// Run: node tests/frontend/tab_audit_capital.cjs
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(path.resolve(__dirname, '../../frontend/app.js'), 'utf8');
const html = fs.readFileSync(path.resolve(__dirname, '../../frontend/index.html'), 'utf8');

const slice = (startMarker, endMarker) => {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    assert(start >= 0 && end > start, `source block not found: ${startMarker}`);
    return source.slice(start, end);
};
const helpers = slice('// --- Capital tab audit helpers (begin) ---', '// --- Capital tab audit helpers (end) ---');
const signedMoney = slice('UI._formatCapitalSignedMoney = function', 'UI._formatCapitalTxnDetails = function');
const resetImpact = slice('UI._capitalResetImpactText = function', 'Actions.openCapitalResetModal = function');
const formatPriceBody = slice('    formatPrice(price) {', '    _activateScannerMarquee(');

const escapeHtml = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
const context = { UI: { _escapeHtml: escapeHtml }, Number, Array, Object, String, Date, JSON, Math };
vm.createContext(context);
vm.runInContext(`Object.assign(UI, {\n${formatPriceBody.replace(/,\s*$/, '')}\n});`, context);
vm.runInContext(`${helpers}\n${signedMoney}\n${resetImpact}`, context);
const UI = context.UI;

// 1. Transaction order: the live ledger wrote RELEASE_MARGIN then CLOSE_POSITION
//    113 microseconds apart; Date.parse sees one millisecond. Evidence row:
//    "RELEASE_MARGIN IOST-USD +$157.75 -> $8061.37" was listed ABOVE
//    "CLOSE_POSITION IOST-USD -$40.51 -> $8020.86".
const ledger = [
    { timestamp: '2026-10-22T13:57:47.243884+03:00', type: 'OPEN_POSITION', amount: -157.81, raw_balance: 7729.55 },
    { timestamp: '2026-11-12T08:47:32.341726+02:00', type: 'RELEASE_MARGIN', amount: 160.5525, raw_balance: 7890.10 },
    { timestamp: '2026-11-12T08:47:32.341850+02:00', type: 'CLOSE_POSITION', amount: 13.5228, raw_balance: 7903.62 },
    { timestamp: '2026-11-12T09:04:29.799253+02:00', type: 'RELEASE_MARGIN', amount: 157.74715, raw_balance: 8061.37 },
    { timestamp: '2026-11-12T09:04:29.799366+02:00', type: 'CLOSE_POSITION', amount: -40.5133, raw_balance: 8020.86 },
];
const sorted = UI._sortCapitalTransactions(ledger);
assert.deepEqual(
    Array.from(sorted, (row) => row.type),
    ['CLOSE_POSITION', 'RELEASE_MARGIN', 'CLOSE_POSITION', 'RELEASE_MARGIN', 'OPEN_POSITION'],
    'Newest first, and the later row of a same-millisecond pair is listed above the earlier one'
);
// Raw Cash now reads as one running balance from bottom to top.
for (let index = 0; index < sorted.length - 1; index += 1) {
    const expected = sorted[index + 1].raw_balance + sorted[index].amount;
    assert(Math.abs(expected - sorted[index].raw_balance) < 0.02, `running balance broken at row ${index}`);
}
assert.equal(sorted[0].raw_balance, 8020.86, 'The top row carries the current cash balance');
assert.deepEqual(ledger.map((row) => row.type)[0], 'OPEN_POSITION', 'Input is not mutated');
assert.equal(UI._sortCapitalTransactions(null).length, 0);
assert(source.includes('const sortedHistory = UI._sortCapitalTransactions(transactions);'), 'History table uses the stable sort');

// 2. Dates: rows and equity samples span weeks, a bare clock time is not enough.
const now = new Date('2026-11-15T10:00:00Z');
const stamp = UI._formatCapitalDateTime('2026-11-12T09:04:29.799366+02:00', now);
assert(/Nov 1[12]/.test(stamp), `date missing from "${stamp}"`);
assert(/\d{2}:\d{2}/.test(stamp) && !/AM|PM/.test(stamp), `24h time expected in "${stamp}"`);
assert(!stamp.includes('2026'), 'Current year is not repeated');
assert(UI._formatCapitalDateTime('2025-12-31T12:00:00Z', now).includes('2025'), 'Another year is shown');
assert.equal(UI._formatCapitalDateTime('', now), '');
assert.equal(UI._formatCapitalDateTime('not-a-date', now), 'not-a-date');
assert(source.includes('${esc(UI._formatCapitalDateTime(txn.timestamp))}'), 'History rows show date and time');
assert(!source.includes('<span>${UI.formatTime(txn.timestamp)}</span>'), 'Time-only history cell is gone');
assert(!/UI\.formatTime\(points\[/.test(source), 'Equity curve caption/axis no longer time-only');
assert(source.includes('tooltipDate.textContent = point.date ? UI._formatCapitalDateTime(point.date)'), 'Equity tooltip shows the date');

// 3. Amount sign: "$-40.51" -> "-$40.51".
assert.equal(UI._formatCapitalSignedMoney(-40.5133), '-$40.51');
assert.equal(UI._formatCapitalSignedMoney(157.74715), '+$157.75');
assert(!source.includes('${sign}$${UI.formatPrice(amountNum)}'), 'History amount no longer renders "$-40.51"');

// 4. Win rate: sample, period and break-even bar.
const live = UI._capitalWinRateView({
    win_rate: 37.3, total_closed_trades: 300,
    win_rate_summary: {
        trades: 300, win_rate_pct: 37.3, breakeven_win_rate_pct: 46.7, window_is_capped: true, retention_limit: 300,
        period_start: '2026-08-28T17:00:21+00:00', period_end: '2026-11-12T07:04:29+00:00',
    },
});
assert.equal(live.value, '37.3%');
assert.equal(live.sub, 'last 300 closed trades · needs 46.7% to break even');
assert.equal(live.belowBreakeven, true);
assert(live.title.includes('Aug 28, 2026') && live.title.includes('Nov 12, 2026'), 'Period is in the tooltip');
assert(live.title.includes('Only the latest 300 closed trades'), 'The cap on the sample is disclosed');
const none = UI._capitalWinRateView({ win_rate: 0, total_closed_trades: 0, history: [] });
assert.equal(none.value, 'n/a', 'No closed trades is "n/a", not a 0% win rate');
assert.equal(none.sub, 'no closed trades yet');
const noBreakeven = UI._capitalWinRateView({ win_rate_summary: { trades: 2, win_rate_pct: 100, breakeven_win_rate_pct: null } });
assert.equal(noBreakeven.sub, '2 closed trades');
assert.equal(noBreakeven.belowBreakeven, false);
const legacy = UI._capitalWinRateView({ history: [{ type: 'CLOSE_POSITION', amount: 5 }, { type: 'CLOSE_POSITION', amount: -5 }, { type: 'RELEASE_MARGIN', amount: 100 }] });
assert.equal(legacy.value, '50%');
assert.equal(legacy.sub, '2 closed trades');
assert(html.includes('id="capital-win-rate-sub"'), 'Win-rate tile has a sample line');

// 5. Price chip: a stock quoted on a weekend is the LAST quote, not LIVE.
const weekendStock = {
    current_price_live: true, current_price_source: 'eToro_live_rates',
    market_state: { market_hours_enforced: true, market_open: false, market_reason: 'weekend', exit_trigger: 'take_profit', exit_note: 'Target already crossed on the last quote.' },
};
const chip = UI._capitalPriceChip(weekendStock);
assert.equal(chip.label, 'LAST');
assert.equal(chip.cls, 'is-cache');
assert(chip.title.includes('US market closed (weekend)') && chip.title.includes('not a tradable price'));
assert.equal(UI._capitalPriceChip({ current_price_live: true, current_price_source: 'Binance_orderbook', market_state: { market_hours_enforced: false, market_open: true } }).label, 'LIVE');
assert.equal(UI._capitalPriceChip({ current_price_live: false, current_price_source: 'state_cache' }).label, 'CACHE');
assert.equal(UI._capitalPriceChip({ current_price_live: false, current_price_source: 'entry_price_fallback' }).label, 'NO QUOTE');
assert.equal(UI._capitalPriceChip(null).label, 'CACHE');

// 6. Stop/target distance: a crossed level is not "-81.6% away".
assert.equal(UI._capitalLevelDistanceHtml('target', 3.21, {}), '<span class="text-green">3.2% away</span>');
assert.equal(UI._capitalLevelDistanceHtml('stop', 1.04, {}), '<span class="text-red">1.0% away</span>');
const reached = UI._capitalLevelDistanceHtml('target', -81.6, weekendStock);
assert(reached.includes('target reached · closes at market open') && !reached.includes('away'));
assert(reached.includes('title="Target already crossed on the last quote."'));
assert(UI._capitalLevelDistanceHtml('stop', -2, { market_state: { market_hours_enforced: false, market_open: true } }).includes('stop hit · exit pending'));
assert.equal(UI._capitalLevelDistanceHtml('target', null, {}), '');
assert(!UI._capitalLevelDistanceHtml('target', -1, { market_state: { exit_note: '"><img src=x>' } }).includes('<img'), 'Exit note is escaped');

// 7. CSV: numbers stay numeric, text is quoted, formulas are neutralised.
const csv = UI._capitalHistoryCsv([
    { timestamp: '2026-11-12T09:04:29.799366+02:00', type: 'CLOSE_POSITION', account_scope: 'capital', affects_deployable_capital: true,
      details: 'IOST-USD LONG close (stop_loss), "quoted"', amount: -40.51326396273365, raw_balance: 8020.856371085945, balance_kind: 'raw_shared_cash',
      metadata: { position_id: 'pos_9e9bba88c4ea' } },
    { timestamp: '2026-11-12T09:05:00+02:00', type: '=HYPERLINK("http://x")', details: '-starts with minus', amount: 0, balance: 5 },
]);
const lines = csv.split('\r\n');
assert.equal(lines.length, 3);
assert.equal(lines[0], '"Timestamp","Type","AccountScope","AffectsDeployableCapital","Details","Amount","RawCash","BalanceKind","MetadataJSON"');
assert(lines[1].includes(',-40.51326396273365,8020.856371085945,'), 'Negative amounts are numbers, not \'-40.51 text');
assert(lines[1].includes('"2026-11-12T09:04:29.799366+02:00"'), 'Full ISO timestamp with date and offset');
assert(lines[1].includes('"IOST-USD LONG close (stop_loss), ""quoted"""'), 'Commas and quotes are escaped');
assert(lines[1].includes(',true,'), 'Booleans are plain');
assert(lines[1].includes('"{""position_id"":""pos_9e9bba88c4ea""}"'));
assert(lines[2].includes('"\'=HYPERLINK(""http://x"")"') && lines[2].includes('"\'-starts with minus"'), 'Formula-like text is neutralised');
assert(lines[2].includes(',0,5,'), 'Zero and fallback balance stay numeric');
assert(source.includes('const csv = UI._capitalHistoryCsv(history);'), 'Export uses the tested builder');

// 8. Reconciliation banner: says what conflicts and how to resolve it; escapes text.
assert.equal(UI._capitalReconciliationHtml({ status: 'HEALTHY', conflicts: [] }), '');
assert.equal(UI._capitalReconciliationHtml(null), '');
const banner = UI._capitalReconciliationHtml({
    status: 'EXECUTION_STATE_CONFLICT', checked_at: '2026-11-15T07:50:45.483911+00:00',
    conflict_details: [{
        code: 'filled_order_position_missing', order_intent_id: 'ord_1629dab4a036c5ca33efe478', position_id: 'pos_ccd4f1e6c35b',
        message: 'Order ledger records OPEN_LONG SOXLB-USD (ord_1629dab4a036c5ca33efe478) as FILLED <b>x</b>',
        resolution: 'Record an orphaned-fill rollback for this order.', repair_action: 'ROLLBACK_ORPHANED_FILL',
    }],
});
// UI v10: the status is shown in sentence case ("execution state conflict"), not capitals.
assert(/execution state conflict/i.test(banner) && banner.includes('SOXLB-USD'));
assert(banner.includes('How to resolve: Record an orphaned-fill rollback'));
assert(banner.includes("Actions.rollbackOrphanedFill('ord_1629dab4a036c5ca33efe478')"));
assert(!banner.includes('<b>x</b>'), 'Conflict text is escaped');
const hostile = UI._capitalReconciliationHtml({ status: 'EXECUTION_STATE_CONFLICT', conflict_details: [{ order_intent_id: "x');alert(1);('", repair_action: 'ROLLBACK_ORPHANED_FILL', message: 'm' }] });
assert(!hostile.includes('rollbackOrphanedFill'), 'A malformed order id never reaches an inline handler');
assert(UI._capitalReconciliationHtml({ status: 'EXECUTION_STATE_CONFLICT', conflict_details: [] }).includes('disagree'));
assert(html.includes('id="capital-reconciliation-banner"'));

// 9. Reset dialog states what is destroyed, with the real counts.
const impact = UI._capitalResetImpactText({
    positions: [{ status: 'OPEN' }, { status: 'OPEN' }, { status: 'CLOSED' }],
    win_rate_summary: { trades: 300 },
});
assert(impact.includes('2 open positions') && impact.includes('300 closed trades') && impact.includes('cannot be undone'));
assert(UI._capitalResetImpactText(null).includes('no open positions'));
assert(html.includes('id="capital-reset-impact"') && html.includes('onclick="Actions.openCapitalResetModal()"'));

// 10. "Sync with Server" only reads: no write call in the sync path.
const sync = slice('Actions.syncCapital = async function', 'Actions.setCapitalBotRunning = async function');
assert(!/method:\s*'(POST|PUT|PATCH|DELETE)'/.test(sync), 'Sync must not write to the server');
assert(sync.includes('UI.refreshCapitalState({ showToast: true })'));

console.log('Capital tab audit: ordering, dates, signs, win rate, price chip, levels, CSV, reconciliation, reset, sync: PASS');
