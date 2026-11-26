// In Capital, a click on a ticker opens its chart (owner request, 2026-11-23).
// Run: node tests/frontend/capital_ticker_chart.cjs
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(require('path').resolve(__dirname, '../../frontend/app.js'), 'utf8');

const cut = (startMarker, endMarker) => {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    assert(start >= 0 && end > start, `markers not found: ${startMarker}`);
    return source.slice(start, end);
};
const escStart = source.indexOf('UI._escapeHtml = function');
const escEnd = source.indexOf('};', escStart) + 2;
assert(escStart >= 0, '_escapeHtml not found');

const opened = [];
const context = { UI: {}, window: { ChartWorkspace: { open: (symbol) => opened.push(symbol) } } };
vm.createContext(context);
vm.runInContext(source.slice(escStart, escEnd), context);
vm.runInContext(`Object.assign(UI, {
${cut('    _scannerJsArg(value) {', '    // One date format for the Scanner tab')}
${cut('    openTickerChart(ticker, source) {', '    // Add scanner ticker to watchlist')}
});`, context);
const { UI } = context;

// 1. A ticker becomes a button that opens the chart and keeps the click to itself.
const html = UI._capitalTickerButton('PUMP-USD', 'cap-pos-ticker');
assert(html.startsWith('<button type="button" class="cap-pos-ticker cap-ticker-link"'), 'a real button, keyboard reachable');
assert(html.includes('event.stopPropagation(); UI.openTickerChart(&quot;PUMP-USD&quot;)'), 'opens the chart without selecting the row');
assert(html.includes('aria-label="Open the PUMP-USD chart"') && html.endsWith('>PUMP-USD</button>'));

// 2. The handler it writes really opens the internal chart for that ticker.
const handler = html.match(/onclick="([^"]+)"/)[1].replace(/&quot;/g, '"');
let stopped = 0;
vm.runInContext(`(function (event) { ${handler} })`, context)({ stopPropagation: () => { stopped += 1; } });
assert.deepEqual(opened, ['PUMP-USD']);
assert.equal(stopped, 1, 'the row behind the ticker is not selected by the same click');

// 3. A hostile ticker cannot break out of the attribute or the handler.
const hostile = UI._capitalTickerButton(`X'");alert(1)//<b>`, 'ticker-badge');
assert(!hostile.includes('<b>') && !hostile.includes(`'");alert(1)`), 'markup and quotes are escaped');
assert(hostile.includes('class="ticker-badge cap-ticker-link"'));

// 4. A placeholder is not a link.
assert.equal(UI._capitalTickerButton('-', 'cap-pos-ticker'), '<span class="cap-pos-ticker">-</span>');
assert.equal(UI._capitalTickerButton(null, 'cap-pos-ticker'), '<span class="cap-pos-ticker">-</span>');

// 5. Every ticker the Capital view prints goes through the helper.
for (const call of [
    "UI._capitalTickerButton(pos.ticker, 'cap-pos-ticker')",
    "UI._capitalTickerButton(row.ticker, 'cap-pos-ticker')",
    "UI._capitalTickerButton(row.ticker || row.raw_symbol || '-', 'cap-pos-ticker')",
    "UI._capitalTickerButton(ticker, 'ticker-badge')",
]) assert(source.includes(call), `missing: ${call}`);
assert(!source.includes('<span class="cap-pos-ticker">${esc('), 'no plain-text ticker is left in Capital');

console.log('capital ticker chart: all checks passed');
