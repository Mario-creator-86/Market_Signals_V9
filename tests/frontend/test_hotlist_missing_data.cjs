const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(require('path').resolve(__dirname, '../../frontend/app.js'), 'utf8');
const start = source.indexOf('UI.renderHotList = function');
const end = source.indexOf('UI.renderBacktestHistory = function', start);
assert(start >= 0 && end > start);
const grid = {innerHTML: ''};
const errors = [];
const context = {
    UI: {loadHotlistHeat() {}, formatPrice: value => String(value)},
    document: {getElementById: id => id === 'hotlist-grid' ? grid : null},
    window: {}, IS_DEV: false,
    console: {warn: (...args) => errors.push(args), error: (...args) => errors.push(args)},
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
const render = extra => {
    errors.length = 0;
    context.UI.renderHotList({hot_list: [{ticker: 'TEST', price: 100, ...extra}]});
    assert.equal(errors.length, 0, String(errors));
    assert(grid.innerHTML.includes('TEST'), 'Card must remain visible');
    return grid.innerHTML;
};
let html = render({expected_move_vs_avg: {expected_move_pct: null, avg_move_8q: null, edge_pct: null}});
assert(html.includes('Expected N/A | Avg 8Q N/A | Edge N/A'), 'Missing values must not become zero');
html = render({expected_move_vs_avg: {expected_move_pct: 0, avg_move_8q: 0, edge_pct: 0}});
assert(html.includes('Expected 0.00% | Avg 8Q 0.00% | Edge 0.00%'), 'Measured zero remains valid');
html = render({expected_move_vs_avg: {expected_move_pct: 8, avg_move_8q: 5, edge_pct: 3}});
const comparison = html.slice(html.indexOf('<span>Expected vs 8Q move</span>'));
assert(comparison.slice(0, 200).includes('<span>5.00%</span>'), 'Second value must be historical average, not edge');
render({earnings_history_summary: {sample_size: 2}});
html = render({earnings_history_summary: {sample_size: 2, last_8: {up_count: 1, down_count: 1, avg_gap_pct: null}}});
assert(html.includes('avg gap=N/A'), 'Incomplete sampled history must remain visible without a invented gap');
console.log('Hot List: missing metrics, measured zero, historical average, missing summary and missing gap: PASS');
