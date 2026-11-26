/* Chart Workspace — TradingView Advanced Chart + Paper Order Ticket. */
window.ChartWorkspace = (() => {
    'use strict';

    const PREFERENCES_STORAGE_KEY = 'market-signals.chart-workspace.preferences.v2';

    /* Timeframe mapping: internal label -> TradingView interval string */
    const TV_INTERVALS = {
        '1m': '1', '5m': '5', '15m': '15', '30m': '30',
        '1h': '60', '4h': '240', '1d': 'D',
    };

    /* Symbol mapping: internal canonical -> TradingView exchange:symbol */
    const TV_SYMBOL_MAP = {
        'BTC-USD':  'BINANCE:BTCUSDT',
        'ETH-USD':  'BINANCE:ETHUSDT',
        'SOL-USD':  'BINANCE:SOLUSDT',
        'BNB-USD':  'BINANCE:BNBUSDT',
        'XRP-USD':  'BINANCE:XRPUSDT',
        'ADA-USD':  'BINANCE:ADAUSDT',
        'DOGE-USD': 'BINANCE:DOGEUSDT',
        'AVAX-USD': 'BINANCE:AVAXUSDT',
        'LINK-USD': 'BINANCE:LINKUSDT',
        'DOT-USD':  'BINANCE:DOTUSDT',
        'LTC-USD':  'BINANCE:LTCUSDT',
        'GC=F':     'COMEX:GC1!',
        'CL=F':     'NYMEX:CL1!',
        'SI=F':     'COMEX:SI1!',
        'NG=F':     'NYMEX:NG1!',
        'ES=F':     'CME_MINI:ES1!',
        'NQ=F':     'CME_MINI:NQ1!',
    };

    /* ---- State ---- */
    const state = {
        symbol: 'BTC-USD',
        timeframe: '5m',
        direction: 'long',
        capital: null,
        submitting: false,
        tvLastPrice: null,   // price received from our price-poll API
        tvWidget: null,      // current <iframe> element
        tvPriceTimer: null,  // setInterval handle for price polling
    };

    const get = id => document.getElementById(id);

    /* ---- Utility ---- */
    function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        })[c]);
    }

    function money(value) {
        const n = Number(value);
        return Number.isFinite(n)
            ? n.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 })
            : '--';
    }

    function format(value) {
        const n = Number(value);
        if (!Number.isFinite(n)) return '--';
        if (Math.abs(n) >= 1000) return n.toLocaleString('en-US', { maximumFractionDigits: 2 });
        if (Math.abs(n) >= 1)    return n.toFixed(4);
        return n.toFixed(6);
    }

    /* ---- Symbol helpers ---- */
    function canonicalSymbol(value) {
        let sym = String(value || '').trim().toUpperCase().replace(/\s+/g, '');
        const aliases = { GOLD: 'GC=F', XAU: 'GC=F', OIL: 'CL=F', WTI: 'CL=F', CRUDE: 'CL=F' };
        if (aliases[sym]) return aliases[sym];
        sym = sym.replace('/', '-').replace('_', '-');
        const providerQuote = sym.match(/^([A-Z0-9]+)-?(USDT|USDC|BUSD)$/);
        if (providerQuote) return `${providerQuote[1]}-USD`;
        const compactUsd = sym.match(/^(BTC|ETH|SOL|BNB|XRP|ADA|DOGE|AVAX|LINK|DOT|LTC)USD$/);
        if (compactUsd) return `${compactUsd[1]}-USD`;
        return sym;
    }

    function tvSymbol(internalSym) {
        if (TV_SYMBOL_MAP[internalSym]) return TV_SYMBOL_MAP[internalSym];
        const cryptoMatch = internalSym.match(/^([A-Z0-9]+)-USD$/);
        if (cryptoMatch) return `BINANCE:${cryptoMatch[1]}USDT`;
        if (internalSym.includes('=F')) return internalSym;  // futures fallback
        // A stock whose exchange is not known: no prefix, so that TradingView searches for it.
        // (A forced "NASDAQ:" made every NYSE stock read "This symbol doesn't exist".)
        return internalSym.replace('-', '.');
    }

    /* Listing exchange of a US stock, from the app's copy of the US symbol directory. */
    const listingCache = new Map();          // internal symbol -> TradingView symbol ('' = not known)

    function needsListing(internalSym) {
        return !TV_SYMBOL_MAP[internalSym] && !/-USD$/.test(internalSym) && !internalSym.includes('=F');
    }

    async function resolveTvSymbol(internalSym) {
        if (!needsListing(internalSym)) return tvSymbol(internalSym);
        if (!listingCache.has(internalSym)) {
            let resolved = '';
            try {
                const response = await fetch(`/api/market/listing?ticker=${encodeURIComponent(internalSym)}`,
                    { signal: AbortSignal.timeout(4000) });
                if (response.ok) {
                    const data = await response.json();
                    resolved = String((data && data.tradingview) || '');
                }
            } catch (_) {
                return tvSymbol(internalSym);     // not cached: the next open asks again
            }
            listingCache.set(internalSym, resolved);
        }
        return listingCache.get(internalSym) || tvSymbol(internalSym);
    }

    function tvInterval(tf) {
        return TV_INTERVALS[tf] || '5';
    }

    function assetClass() {
        if (state.symbol.endsWith('-USD')) return 'CRYPTO';
        if (state.symbol.endsWith('=F'))  return 'COMMODITY';
        return 'STOCK';
    }

    /* ---- Preferences ---- */
    function safeStorageRead(key, fallback) {
        try { return JSON.parse(window.localStorage.getItem(key) || 'null') ?? fallback; }
        catch (_) { return fallback; }
    }

    function safeStorageWrite(key, value) {
        try { window.localStorage.setItem(key, JSON.stringify(value)); } catch (_) {}
    }

    function restorePreferences() {
        const saved = safeStorageRead(PREFERENCES_STORAGE_KEY, {});
        if (!saved || typeof saved !== 'object') return;
        if (typeof saved.timeframe === 'string' && TV_INTERVALS[saved.timeframe]) state.timeframe = saved.timeframe;
        if (saved.direction === 'long' || saved.direction === 'short') state.direction = saved.direction;
        const fields = (saved.orderFields && typeof saved.orderFields === 'object') ? saved.orderFields : {};
        [
            ['chart-order-size',     fields.size],
            ['chart-order-leverage', fields.leverage],
            ['chart-order-stop',     fields.stop],
            ['chart-order-target',   fields.target],
            ['chart-order-reason',   fields.reason],
        ].forEach(([id, value]) => {
            const el = get(id);
            if (el && value !== undefined && value !== null) el.value = String(value);
        });
    }

    function persistPreferences() {
        safeStorageWrite(PREFERENCES_STORAGE_KEY, {
            timeframe: state.timeframe,
            direction: state.direction,
            orderFields: {
                size:     get('chart-order-size')?.value,
                leverage: get('chart-order-leverage')?.value,
                stop:     get('chart-order-stop')?.value,
                target:   get('chart-order-target')?.value,
                reason:   get('chart-order-reason')?.value,
            },
        });
    }

    /* ---- TradingView Widget lifecycle ---- */

    function destroyTvWidget() {
        if (state.tvWidget) { state.tvWidget.remove(); state.tvWidget = null; }
        clearInterval(state.tvPriceTimer);
        state.tvPriceTimer = null;
    }

    function setTvLoading(visible) {
        const loader = get('tv-chart-loading');
        if (loader) loader.style.display = visible ? 'flex' : 'none';
    }

    // Chart colours come from the design tokens at render time, so the embed
    // matches whichever theme is active when it is created.
    function chartTheme() {
        const root = document.documentElement;
        const name = root.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
        let toolbar = '';
        try { toolbar = window.getComputedStyle(root).getPropertyValue('--ds-surface-1').trim(); } catch (_) { toolbar = ''; }
        if (!/^#[0-9a-fA-F]{6}$/.test(toolbar)) toolbar = name === 'light' ? '#ffffff' : '#121a28';
        return { name, toolbar };
    }

    function createTvWidget(symbol, tf) {
        destroyTvWidget();
        const container = get('tv-chart-container');
        if (!container) return;

        setTvLoading(true);
        state.tvLastPrice = null;
        renderOrderPreview();

        // The exchange of a stock is looked up first; a newer request (another symbol or
        // timeframe chosen meanwhile) makes this one obsolete.
        const request = (state.tvRequest = (state.tvRequest || 0) + 1);
        resolveTvSymbol(symbol).then((tvSym) => {
            if (request === state.tvRequest) mountTvWidget(container, symbol, tf, tvSym);
        });
    }

    function mountTvWidget(container, symbol, tf, tvSym) {
        const interval = tvInterval(tf);

        // The embed follows the app theme: dark is the default, light only when
        // the shell set data-theme="light". The toolbar takes the panel surface
        // token so the chart frame and our chrome read as one surface.
        const appTheme = chartTheme();
        const params = new URLSearchParams({
            symbol:              tvSym,
            interval:            interval,
            timezone:            'Etc/UTC',
            theme:               appTheme.name,
            style:               '1',         // candlestick
            locale:              'en',
            toolbar_bg:          appTheme.toolbar,
            enable_publishing:   'false',
            hide_side_toolbar:   'false',
            allow_symbol_change: 'false',     // symbol is controlled by our header
            save_image:          'false',
            withdateranges:      '1',
            hide_volume:         '0',
            support_host:        'https://www.tradingview.com',
        });

        const iframe = document.createElement('iframe');
        iframe.src = `https://www.tradingview.com/widgetembed/?${params.toString()}`;
        iframe.style.cssText = 'width:100%;height:100%;border:none;display:block;';
        iframe.setAttribute('allow', 'clipboard-write');
        iframe.setAttribute('title', `TradingView chart for ${tvSym}`);
        iframe.setAttribute('aria-label', `TradingView chart for ${symbol}`);

        iframe.addEventListener('load', () => {
            setTvLoading(false);
            const legend = get('chart-workspace-legend');
            if (legend) {
                legend.innerHTML =
                    `<span class="chart-legend-symbol">${escapeHtml(tvSym)}</span>`
                    + `<span>${escapeHtml(tf)} \u00b7 TradingView live feed</span>`;
            }
            startPricePoll(symbol);
        });

        container.appendChild(iframe);
        state.tvWidget = iframe;

        const extBtn = get('chart-tv-external');
        if (extBtn) {
            extBtn.href = `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(tvSym)}&interval=${encodeURIComponent(interval)}`;
        }

        setChartStatus(`${symbol} \u00b7 ${tf.toUpperCase()} \u00b7 TradingView live feed`);
    }

    /* ---- Price polling (15 s) ---- */
    // TradingView widgetembed is cross-origin; the Charting Library postMessage
    // quoteUpdate event is only available on paid plans. We fall back to polling
    // our own /api/chart/bars endpoint to keep the order ticket price current.

    function startPricePoll(symbol) {
        clearInterval(state.tvPriceTimer);
        fetchLivePrice(symbol);  // immediate first fetch
        state.tvPriceTimer = setInterval(() => {
            if (!state.tvWidget) return;
            fetchLivePrice(symbol);
        }, 15000);
    }

    function applyOrderTicketPrice(data, symbol, requestId) {
        if (symbol !== state.symbol || requestId !== state.priceRequestId) return;
        const bars = Array.isArray(data?.bars) ? data.bars : [];
        const bar = bars[bars.length - 1];
        const rawTime = bar?.time ?? bar?.timestamp ?? bar?.date;
        const timestamp = typeof rawTime === 'number' ? (rawTime < 1e12 ? rawTime * 1000 : rawTime) : Date.parse(rawTime);
        const price = Number(bar?.close);
        // The endpoint contains five-minute bars. A fresh cache alone is not
        // evidence of a current quote; closed-session prices cannot size orders.
        const age = Date.now() - timestamp;
        state.tvLastPrice = data?.cache !== 'stale' && Number.isFinite(price) && price > 0
            && Number.isFinite(age) && age >= -60000 && age <= 10 * 60000 ? price : null;
        renderOrderPreview();
    }

    async function fetchLivePrice(symbol) {
        const requestId = state.priceRequestId = (state.priceRequestId || 0) + 1;
        const current = () => symbol === state.symbol && requestId === state.priceRequestId;
        try {
            const url = `/api/chart/${encodeURIComponent(symbol)}?timeframe=5m`;
            let response = await fetch(url, {signal: AbortSignal.timeout(8000)});
            if (!response.ok) throw new Error('Quote unavailable');
            let data = await response.json();
            if (!current()) return;
            if (data?.cache === 'stale') {
                state.tvLastPrice = null;
                renderOrderPreview();
                response = await fetch(url + '&refresh=true', {signal: AbortSignal.timeout(12000)});
                if (!response.ok) throw new Error('Quote refresh unavailable');
                data = await response.json();
            }
            applyOrderTicketPrice(data, symbol, requestId);
        } catch (_) {
            if (current()) {
                state.tvLastPrice = null;
                renderOrderPreview();
            }
        }
    }

    // Public TradingView embeds do not provide authenticated quote messages.
    // Only the validated API quote above can update the paper order draft.

    /* ---- Chart status bar ---- */

    function setChartStatus(text, kind) {
        const status = get('chart-workspace-status');
        if (!status) return;
        status.className = `chart-workspace__status${kind ? ` is-${kind}` : ''}`;
        status.innerHTML = `<span class="chart-status-pulse"></span><span>${escapeHtml(text)}</span>`;
    }

    /* ---- Capital ---- */

    function currentCapital() {
        /* eslint-disable no-undef */
        if (typeof cachedState !== 'undefined' && cachedState?.capital) return cachedState.capital;
        /* eslint-enable no-undef */
        return state.capital || {};
    }

    async function refreshCapital(remote) {
        try {
            if (remote) {
                const fresh = await API.getState({
                    includeNews: false, includeHotlist: false, hotlistDetails: false,
                    lean: false, include: 'capital', unchanged: false, livePrices: false,
                });
                if (fresh?.capital) {
                    state.capital = fresh.capital;
                    /* eslint-disable no-undef */
                    if (typeof cachedState !== 'undefined') {
                        cachedState = UI.mergeStatePreserveDetails(cachedState || {}, fresh);
                    }
                    /* eslint-enable no-undef */
                }
            } else {
                state.capital = currentCapital();
            }
        } catch (error) {
            console.warn('Chart terminal capital refresh failed:', error);
        }
        renderOrderPreview();
    }

    /* ---- Order preview ---- */

    function lastChartPrice() { return state.tvLastPrice; }

    function orderDraft() {
        const price     = lastChartPrice();
        const notional  = Number(get('chart-order-size')?.value     || 0);
        const leverage  = Number(get('chart-order-leverage')?.value || 1);
        const stopPct   = Number(get('chart-order-stop')?.value     || 0);
        const targetPct = Number(get('chart-order-target')?.value   || 0);
        const balance   = Number(currentCapital()?.balance);
        const margin    = notional / Math.max(leverage, 1);
        const valid = (
            Number.isFinite(price) && price > 0
            && Number.isFinite(notional)  && notional  >= 10
            && Number.isFinite(leverage)  && leverage  >= 1 && leverage <= 5
            && Number.isFinite(stopPct)   && stopPct   > 0
            && Number.isFinite(targetPct) && targetPct > 0
            && (!Number.isFinite(balance) || margin <= balance)
        );
        return { price, notional, leverage, stopPct, targetPct, balance, margin, valid };
    }

    function renderOrderPreview() {
        const draft     = orderDraft();
        const dirFactor = state.direction === 'long' ? 1 : -1;
        const stopLoss  = Number.isFinite(draft.price) ? draft.price * (1 - dirFactor * draft.stopPct   / 100) : null;
        const takeProfit= Number.isFinite(draft.price) ? draft.price * (1 + dirFactor * draft.targetPct / 100) : null;
        const rr        = draft.stopPct > 0 ? draft.targetPct / draft.stopPct : null;

        if (get('chart-order-price'))           get('chart-order-price').textContent           = Number.isFinite(draft.price) ? format(draft.price) : '--';
        if (get('chart-order-balance'))         get('chart-order-balance').textContent         = money(draft.balance);
        if (get('chart-order-asset'))           get('chart-order-asset').textContent           = assetClass();
        if (get('chart-order-leverage-label'))  get('chart-order-leverage-label').textContent  = `${draft.leverage}x`;
        if (get('chart-order-margin'))          get('chart-order-margin').textContent          = money(draft.margin);
        if (get('chart-order-risk'))            get('chart-order-risk').textContent            = money(draft.notional * draft.stopPct   / 100);
        if (get('chart-order-reward'))          get('chart-order-reward').textContent          = money(draft.notional * draft.targetPct / 100);
        if (get('chart-order-rr'))              get('chart-order-rr').textContent              = Number.isFinite(rr) ? `1 : ${rr.toFixed(2)}` : '--';
        if (get('chart-order-levels')) {
            get('chart-order-levels').textContent = (Number.isFinite(stopLoss) && Number.isFinite(takeProfit))
                ? `${format(stopLoss)} / ${format(takeProfit)}` : '--';
        }

        document.querySelectorAll('[data-paper-direction]').forEach(btn =>
            btn.classList.toggle('is-active', btn.dataset.paperDirection === state.direction));
        document.querySelectorAll('[data-paper-leverage]').forEach(btn =>
            btn.classList.toggle('is-active', Number(btn.dataset.paperLeverage) === draft.leverage));
        document.querySelectorAll('[data-paper-size]').forEach(btn =>
            btn.classList.toggle('is-active', Number(btn.dataset.paperSize) === draft.notional));
        document.querySelectorAll('[data-risk-stop]').forEach(btn =>
            btn.classList.toggle('is-active',
                Number(btn.dataset.riskStop) === draft.stopPct && Number(btn.dataset.riskTarget) === draft.targetPct));
        document.querySelectorAll('[data-chart-symbol]').forEach(btn =>
            btn.classList.toggle('is-active', canonicalSymbol(btn.dataset.chartSymbol) === state.symbol));

        const submit = get('chart-order-submit');
        if (submit) {
            submit.classList.toggle('is-long',  state.direction === 'long');
            submit.classList.toggle('is-short', state.direction === 'short');
            submit.textContent = state.submitting
                ? 'Submitting paper order...'
                : `Open ${state.direction === 'long' ? 'long' : 'short'} paper position`;
            submit.disabled = state.submitting || !draft.valid;
            submit.title = draft.valid ? '' : 'Load price data and complete a valid order within free capital.';
        }
        renderSymbolPositions();
    }

    /* ---- Positions ---- */

    function symbolPositions() {
        const positions = Array.isArray(currentCapital()?.positions) ? currentCapital().positions : [];
        return positions.filter(p =>
            String(p?.status || '').toUpperCase() === 'OPEN'
            && canonicalSymbol(p?.ticker) === state.symbol);
    }

    function setOrderStatus(kind, text) {
        const status = get('chart-order-status');
        if (!status) return;
        status.className = `paper-order-status${kind ? ` is-${kind}` : ''}`;
        const icon = kind === 'success' ? 'circle-check' : kind === 'error' ? 'triangle-exclamation' : 'shield-halved';
        status.innerHTML = `<i class="fa-solid fa-${icon}"></i><span>${escapeHtml(text)}</span>`;
    }

    function renderSymbolPositions() {
        const container = get('chart-symbol-positions');
        if (!container) return;
        const positions = symbolPositions();
        if (!positions.length) {
            container.innerHTML = '<div class="paper-position-empty">No open positions for this symbol.</div>';
            return;
        }
        container.innerHTML = positions.map(position => {
            const direction = String(position.direction || 'long').toUpperCase();
            const leverage  = Math.max(1, Number(position.leverage || 1));
            const notional  = Number(position.usd_size || 0);
            const entry     = Number(position.entry_price || 0);
            return `<div class="paper-position-row" data-position-id="${escapeHtml(position.position_id)}">
                <div>
                    <strong>${escapeHtml(direction)} ${leverage}x \u00b7 ${money(notional)}</strong>
                    <small>Entry ${format(entry)} \u00b7 margin ${money(position.margin_used)}</small>
                </div>
                <button type="button" data-close-paper-position="${escapeHtml(position.position_id)}" aria-label="Close this ${escapeHtml(direction.toLowerCase())} position at market">Close</button>
            </div>`;
        }).join('');

        container.querySelectorAll('[data-close-paper-position]').forEach(btn => {
            btn.addEventListener('click', async () => {
                if (btn.disabled) return;
                btn.disabled = true;
                setOrderStatus('', 'Closing with a verified live execution price...');
                const data = await Actions.closeCapitalPosition(btn.dataset.closePaperPosition);
                if (data) {
                    state.capital = data;
                    setOrderStatus('success', 'Paper position closed successfully.');
                    renderOrderPreview();
                } else {
                    setOrderStatus('error', 'Position could not be closed. Check the notification for the exact reason.');
                    btn.disabled = false;
                }
            });
        });
    }

    /* ---- Direction ---- */

    function setDirection(direction) {
        state.direction = direction === 'short' ? 'short' : 'long';
        persistPreferences();
        renderOrderPreview();
    }

    /* ---- Submit order ---- */

    async function submitOrder() {
        if (state.submitting) return;
        const draft = orderDraft();
        if (!draft.valid) {
            setOrderStatus('error', 'Load a chart and complete a valid order before opening a position.');
            return;
        }
        state.submitting = true;
        setOrderStatus('', 'Checking market hours and requesting a verified live fill...');
        renderOrderPreview();
        const data = await Actions.submitCapitalPaperOrder({
            ticker:          state.symbol,
            direction:       state.direction,
            leverage:        draft.leverage,
            usd_size:        draft.notional,
            stop_loss_pct:   draft.stopPct,
            take_profit_pct: draft.targetPct,
            reason: String(get('chart-order-reason')?.value || '').trim()
                || `Manual ${state.direction.toUpperCase()} from ${state.timeframe} chart`,
            strategy_type: 'MANUAL_CHART',
        });
        state.submitting = false;
        if (data?.position) {
            state.capital = data;
            const p = data.position;
            setOrderStatus('success',
                `Opened at ${format(p.entry_price)} \u00b7 margin ${money(p.margin_used)} \u00b7 SL ${format(p.stop_loss_price)} \u00b7 TP ${format(p.take_profit_price)}.`);
            if (get('chart-order-reason')) get('chart-order-reason').value = '';
        } else {
            setOrderStatus('error', 'Order was not opened. Check the notification for the validation or market-hours reason.');
        }
        renderOrderPreview();
    }

    /* ---- Symbol / Timeframe change ---- */

    async function setSymbol(value) {
        const symbol = canonicalSymbol(value);
        if (!symbol) { setChartStatus('Enter a valid market symbol.', 'error'); return; }
        state.symbol       = symbol;
        state.tvLastPrice  = null;
        if (get('chart-symbol-input'))    get('chart-symbol-input').value = symbol;
        if (get('chart-workspace-title')) get('chart-workspace-title').textContent = `${symbol} \u00b7 chart trading`;
        renderOrderPreview();
        createTvWidget(symbol, state.timeframe);
        document.querySelectorAll('[data-chart-symbol]').forEach(btn =>
            btn.classList.toggle('is-active', canonicalSymbol(btn.dataset.chartSymbol) === symbol));
    }

    function setTimeframe(tf) {
        if (!TV_INTERVALS[tf]) return;
        state.timeframe = tf;
        persistPreferences();
        createTvWidget(state.symbol, tf);
        get('chart-timeframes')?.querySelectorAll('button').forEach(btn =>
            btn.classList.toggle('is-active', btn.dataset.timeframe === tf));
    }

    /* ---- Event bindings (once) ---- */

    let bindingsDone = false;

    function bindTerminal() {
        if (bindingsDone) return;
        bindingsDone = true;

        // Symbol search
        get('chart-symbol-load')?.addEventListener('click', () => {
            const input = get('chart-symbol-input');
            if (input) setSymbol(input.value.trim());
        });
        get('chart-symbol-input')?.addEventListener('keydown', event => {
            if (event.key === 'Enter') { event.preventDefault(); setSymbol(event.target.value.trim()); }
        });
        document.querySelectorAll('[data-chart-symbol]').forEach(btn =>
            btn.addEventListener('click', () => setSymbol(btn.dataset.chartSymbol)));

        // Timeframes
        get('chart-timeframes')?.querySelectorAll('button').forEach(btn =>
            btn.addEventListener('click', () => setTimeframe(btn.dataset.timeframe)));

        // Focus / expand
        get('chart-focus-toggle')?.addEventListener('click', () => {
            get('chart-workspace-modal')?.querySelector('.chart-workspace')?.classList.toggle('is-focus');
        });

        // Manual refresh
        get('chart-refresh')?.addEventListener('click', () => {
            createTvWidget(state.symbol, state.timeframe);
            refreshCapital(true);
        });

        // Direction
        document.querySelectorAll('[data-paper-direction]').forEach(btn =>
            btn.addEventListener('click', () => setDirection(btn.dataset.paperDirection)));

        // Leverage slider + presets
        get('chart-order-leverage')?.addEventListener('input', renderOrderPreview);
        document.querySelectorAll('[data-paper-leverage]').forEach(btn => btn.addEventListener('click', () => {
            const lev = get('chart-order-leverage');
            if (lev) { lev.value = btn.dataset.paperLeverage; renderOrderPreview(); }
        }));

        // Size input + presets
        get('chart-order-size')?.addEventListener('input', renderOrderPreview);
        document.querySelectorAll('[data-paper-size]').forEach(btn => btn.addEventListener('click', () => {
            const sz = get('chart-order-size');
            if (sz) { sz.value = btn.dataset.paperSize; renderOrderPreview(); }
        }));
        document.querySelectorAll('[data-paper-size-percent]').forEach(btn => btn.addEventListener('click', () => {
            const pct     = Number(btn.dataset.paperSizePercent) / 100;
            const balance = Number(currentCapital()?.balance);
            const sz      = get('chart-order-size');
            if (sz && Number.isFinite(balance) && balance > 0) {
                sz.value = Math.max(10, Math.floor(balance * pct / 10) * 10);
                renderOrderPreview();
            }
        }));

        // Risk presets + inputs
        get('chart-order-stop')?.addEventListener('input', renderOrderPreview);
        get('chart-order-target')?.addEventListener('input', renderOrderPreview);
        document.querySelectorAll('[data-risk-stop]').forEach(btn => btn.addEventListener('click', () => {
            const stop   = get('chart-order-stop');
            const target = get('chart-order-target');
            if (stop)   stop.value   = btn.dataset.riskStop;
            if (target) target.value = btn.dataset.riskTarget;
            renderOrderPreview();
        }));

        // Submit + positions refresh
        get('chart-order-submit')?.addEventListener('click', submitOrder);
        get('chart-positions-refresh')?.addEventListener('click', () => refreshCapital(true));

        // Persist preferences on form change
        ['chart-order-size', 'chart-order-leverage', 'chart-order-stop', 'chart-order-target', 'chart-order-reason']
            .forEach(id => get(id)?.addEventListener('change', persistPreferences));
    }

    /* ---- Open / Close ---- */

    function open(symbol) {
        state.symbol      = canonicalSymbol(symbol || 'BTC-USD') || 'BTC-USD';
        state.tvLastPrice = null;

        restorePreferences();
        bindTerminal();

        get('chart-workspace-modal')?.classList.remove('hidden');
        document.body.classList.add('chart-workspace-open');

        if (get('chart-workspace-title')) get('chart-workspace-title').textContent = `${state.symbol} \u00b7 chart trading`;
        if (get('chart-symbol-input'))    get('chart-symbol-input').value = state.symbol;

        // Sync active timeframe button
        get('chart-timeframes')?.querySelectorAll('button').forEach(btn =>
            btn.classList.toggle('is-active', btn.dataset.timeframe === state.timeframe));

        renderOrderPreview();

        window.requestAnimationFrame(() => {
            createTvWidget(state.symbol, state.timeframe);
            refreshCapital(true);
        });
    }

    function close() {
        persistPreferences();
        destroyTvWidget();
        get('chart-workspace-modal')?.classList.add('hidden');
        document.body.classList.remove('chart-workspace-open');
        state.tvLastPrice = null;
    }

    // Theme switch while the workspace is open: rebuild the embed in the new theme.
    document.addEventListener('mm:theme', () => {
        const modal = get('chart-workspace-modal');
        if (modal && !modal.classList.contains('hidden')) createTvWidget(state.symbol, state.timeframe);
    });

    // ESC closes the workspace
    window.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
            const modal = get('chart-workspace-modal');
            if (modal && !modal.classList.contains('hidden')) close();
        }
    });

    /* ---- Public API ---- */
    return {
        open,
        close,
        refresh:     () => { createTvWidget(state.symbol, state.timeframe); refreshCapital(true); },
        setSymbol,
        submitOrder,
    };
})();
