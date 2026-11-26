const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(path.resolve(__dirname, '../../frontend/app.js'), 'utf8');
const start = source.indexOf('UI.loadPreEarningsEvidenceLab = async function');
const end = source.indexOf('UI.renderPreEarningsTrackRecord = function', start);
assert(start >= 0 && end > start);
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const context = {UI: {_escapeHtml: escapeHtml}, API: {_warnNetworkOnce() {}}, document: {getElementById: () => null}, Number, Array, Object, String};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);

assert.equal(context.UI._pePaperPerformanceHtml(null), '', 'No data renders nothing');

const losing = {
    basis: 'closed paper positions', min_trades_for_verdict: 30, profitable_strategies: [],
    headline: 'Paper trading is not yet profitable: 276 closed trades, net -86.79 USD after estimated costs.',
    overall: {strategy: 'ALL_STRATEGIES', trades: 276, net_pnl_usd: -86.79, mean_pnl_usd: -0.31, mean_pnl_ci95_usd: [-1.05, 0.44], win_rate_pct: 43.5, breakeven_win_rate_pct: 46.9, profit_factor: 0.89, verdict: 'UNPROVEN'},
    strategies: [{strategy: 'PRE_EARNINGS_AI_LONG', trades: 13, net_pnl_usd: -37.43, mean_pnl_usd: -2.88, mean_pnl_ci95_usd: [-5.37, 0.1], win_rate_pct: 15.4, profit_factor: 0.28, verdict: 'INSUFFICIENT_EVIDENCE'}],
};
let html = context.UI._pePaperPerformanceHtml(losing);
assert(html.includes('not yet profitable'), 'Losing headline shown verbatim');
assert(html.includes('-86.79') && html.includes('All strategies') && html.includes('NOT ENOUGH TRADES'));
assert(!html.includes('>PROFITABLE<'), 'Nothing may be labelled profitable');
assert(html.includes('Needed to break even') && html.includes('46.9%'), 'Break-even win rate shown next to the actual win rate');

const hostile = JSON.parse(JSON.stringify(losing));
hostile.strategies[0].strategy = '<img src=x onerror=1>';
hostile.headline = '<script>x</script>';
html = context.UI._pePaperPerformanceHtml(hostile);
assert(!html.includes('<img src=x') && !html.includes('<script>'), 'Strategy names and headline are escaped');

// The lab panel embeds the paper section when data is present.
context.UI._paperPerformance = losing;
const lab = context.UI._peEvidenceLabHtml({headline: 'h', dataset: {}, direction_claims: [], gap_size_claims: []});
assert(lab.includes('Realized paper trading'), 'Evidence Lab must include realized paper trading');
console.log('Paper performance: empty, losing ledger, escaping and lab embedding: PASS');
