// The chart must open a stock on the exchange it is listed on (seen live: NASDAQ:DV "doesn't exist").
// Run: node tests/frontend/chart_listing_exchange.cjs
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(require('path').resolve(__dirname, '../../frontend/chart_workspace.js'), 'utf8');
const start = source.indexOf('    /* ---- Symbol helpers ---- */');
const end = source.indexOf('    function tvInterval(tf) {', start);
assert(start >= 0 && end > start, 'symbol helper markers not found');

const calls = [];
let reply = async (url) => ({ ok: true, json: async () => ({ tradingview: url.includes('DV') ? 'NYSE:DV' : '' }) });
const ctx = {
    TV_SYMBOL_MAP: { 'BTC-USD': 'BINANCE:BTCUSDT', 'GC=F': 'COMEX:GC1!' },
    AbortSignal, encodeURIComponent,
    fetch: (url) => { calls.push(url); return reply(url); },
};
vm.createContext(ctx);
vm.runInContext(source.slice(start, end), ctx);

(async () => {
    // Crypto, futures and mapped symbols never ask the server.
    assert.equal(await ctx.resolveTvSymbol('BTC-USD'), 'BINANCE:BTCUSDT');
    assert.equal(await ctx.resolveTvSymbol('PUMP-USD'), 'BINANCE:PUMPUSDT');
    assert.equal(await ctx.resolveTvSymbol('GC=F'), 'COMEX:GC1!');
    assert.equal(calls.length, 0);

    // A stock is opened on its own exchange, asked once.
    assert.equal(await ctx.resolveTvSymbol('DV'), 'NYSE:DV');
    assert.equal(await ctx.resolveTvSymbol('DV'), 'NYSE:DV');
    assert.deepEqual(calls, ['/api/market/listing?ticker=DV']);

    // Unknown to the directory: no forced exchange, TradingView searches; a share class uses a dot.
    assert.equal(await ctx.resolveTvSymbol('NOSUCH'), 'NOSUCH');
    assert.equal(ctx.tvSymbol('BRK-B'), 'BRK.B');
    assert(!String(ctx.tvSymbol('DV')).startsWith('NASDAQ:'), 'the exchange is never guessed');

    // A failed lookup falls back for now and is asked again next time.
    reply = async () => { throw new Error('offline'); };
    assert.equal(await ctx.resolveTvSymbol('AES'), 'AES');
    reply = async () => ({ ok: true, json: async () => ({ tradingview: 'NYSE:AES' }) });
    assert.equal(await ctx.resolveTvSymbol('AES'), 'NYSE:AES');

    // A newer request makes an older one obsolete: only the last symbol is mounted.
    assert(source.includes('if (request === state.tvRequest) mountTvWidget(container, symbol, tf, tvSym);'));
    console.log('chart listing exchange: all checks passed');
})().catch((error) => { console.error(error); process.exit(1); });
