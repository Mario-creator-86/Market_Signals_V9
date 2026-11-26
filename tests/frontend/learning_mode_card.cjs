// Learning mode card in the cockpit: progress toward 10,000 paper trades Opus chose (owner, 2026-11-24).
// Run: node tests/frontend/learning_mode_card.cjs
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(require('path').resolve(__dirname, '../../frontend/cockpit.js'), 'utf8');
const start = source.indexOf('    function renderLearning(state) {');
const end = source.indexOf('    async function loadLearning(', start);
assert(start >= 0 && end > start, 'renderLearning / loadLearning markers not found');

const ctx = {};
vm.createContext(ctx);
vm.runInContext(`const esc = (value) => String(value == null ? '' : value).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
${source.slice(start, end)}; this.renderLearning = renderLearning;`, ctx);
const { renderLearning } = ctx;

// 1. Nothing yet: it says so, and loading is not shown as a result.
assert(renderLearning(null).includes('Loading'));
assert(renderLearning({ status: 'not_started', reason: 'not yet' }).includes('not yet'));

// 2. Progress, today, results by source, side and confidence, the latest trades.
const html = renderLearning({
    status: 'ran', stale: false, opened_today: 7, daily_cap: 100, paused_reason: '',
    progress: {
        target: 10000, closed: 1234, open: 9, net_pnl: -12.5, win_rate_pct: 48.2, checkpoint: 1, checkpoint_every: 1000,
        pending_ideas: 2,
        by_source: { mover: { closed: 1000, net_pnl: -20, win_rate_pct: 47 }, news: { closed: 234, net_pnl: 7.5, win_rate_pct: 52 } },
        by_side: { LONG: { closed: 700, net_pnl: 3, win_rate_pct: 50 }, SHORT: { closed: 534, net_pnl: -15.5, win_rate_pct: 46 } },
        by_confidence: { '0.55-0.65': { closed: 800, net_pnl: -30, win_rate_pct: 45 }, '0.65-0.75': { closed: 300, net_pnl: 10, win_rate_pct: 51 }, '0.75+': { closed: 134, net_pnl: 7.5, win_rate_pct: 55 } },
        recent: [{ ticker: '<b>GLD</b>', side: 'LONG', source: 'news', confidence: 0.7, net_pnl: 4, close_reason: 'take_profit', closed_at: '2026-11-24T19:00:00+00:00' }],
    },
});
assert(html.includes('1,234') && html.includes('10,000'), 'closed of the target');
assert(html.includes('7 of 100'), "today's trades against the daily cap");
assert(html.includes('48.2%') && html.includes('-12.50'), 'win rate and net result');
assert(html.includes('Movers') && html.includes('News') && html.includes('0.75+'), 'by source and confidence band');
assert(html.includes('&lt;b&gt;GLD&lt;/b&gt;') && !html.includes('<b>GLD</b>'), 'ticker text is escaped');
assert(html.includes('checkpoint 1'), 'checkpoint every 1,000');

// 3. A pause or a silent worker is visible.
assert(renderLearning({ status: 'paused', paused_reason: 'daily cap reached', progress: { target: 10000, closed: 0 } }).includes('daily cap reached'));
assert(renderLearning({ status: 'ran', stale: true, progress: { target: 10000, closed: 0 } }).includes('has not reported'));

// 4. It is loaded with the lab card.
assert(source.includes("fetch('/api/learning/state')"));
assert(source.includes('id="cockpit-learning"'));
console.log('learning mode card: all checks passed');
