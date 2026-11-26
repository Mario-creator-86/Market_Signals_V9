/*
 * MarketMuse application shell (UI v10).
 *
 * Builds the parts of the frame that are not in index.html:
 *   - groups and icons for the left rail (the existing nav links are kept,
 *     with their data-testid / onclick, so navigation code and tests still work);
 *   - the top bar with the US market-session clock;
 *   - the command palette (Ctrl/Cmd+K or "/");
 *   - theme (dark "desk" / light "paper") and rail width, remembered per browser.
 *
 * No framework, no dependency on app.js load order: everything it needs from
 * the app (ViewRouter, UI.openWatchlistTicker) is looked up when used.
 */
(function () {
    'use strict';

    const root = document.documentElement;
    if (root.getAttribute('data-ui') !== 'v10') return;

    const store = {
        get(key) {
            try { return window.localStorage.getItem(key); } catch (_) { return null; }
        },
        set(key, value) {
            try { window.localStorage.setItem(key, value); } catch (_) { /* private mode */ }
        },
    };

    const esc = (value) => String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
    ));

    const ICONS = {
        menu: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11"/></svg>',
        search: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="7" cy="7" r="4.25"/><path d="M10.25 10.25 13.5 13.5"/></svg>',
        sun: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="8" cy="8" r="3"/><path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1.05 1.05M11.55 11.55l1.05 1.05M3.4 12.6l1.05-1.05M11.55 4.45l1.05-1.05"/></svg>',
        moon: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M13.5 9.6A5.75 5.75 0 0 1 6.4 2.5a5.75 5.75 0 1 0 7.1 7.1Z"/></svg>',
        brand: '<svg viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 13.5 6.5 8l3 3 6-7.5"/><path d="M11.5 3.5h4v4"/></svg>',
    };

    // Order and grouping of the rail. Keys are the nav links' data-testid.
    const NAV = [
        { group: 'Overview' },
        { id: 'nav-home', view: 'home', label: 'Cockpit', icon: 'fa-gauge-high' },
        { group: 'Markets' },
        { id: 'nav-watchlist', view: 'watchlist', label: 'Watchlist', icon: 'fa-list-ul' },
        { id: 'nav-scanner', view: 'scanner', label: 'Scanner', icon: 'fa-chart-simple' },
        { id: 'nav-hotlist', view: 'hotlist', label: 'Hot list', icon: 'fa-fire' },
        { id: 'nav-news', view: 'news', label: 'News', icon: 'fa-newspaper' },
        { id: 'nav-calendar', view: 'calendar', label: 'Calendar', icon: 'fa-calendar-days' },
        { group: 'Setups' },
        { id: 'nav-traderadar', view: 'traderadar', label: 'Trade radar', icon: 'fa-crosshairs' },
        { id: 'nav-premarket', view: 'premarket', label: 'Pre-market', icon: 'fa-sun' },
        { id: 'nav-preearnings', view: 'preearnings', label: 'Pre-earnings', icon: 'fa-wave-square' },
        { group: 'Trading' },
        { id: 'nav-capital', view: 'capital', label: 'Capital', icon: 'fa-wallet' },
        { id: 'nav-agents', view: 'agents', label: 'Agents', icon: 'fa-microchip' },
        { id: 'nav-alerts', view: 'alerts', label: 'Alerts', icon: 'fa-bell' },
        { group: 'Research' },
        { id: 'nav-backtest', view: 'backtest', label: 'Backtest', icon: 'fa-flask' },
    ];
    const NAV_ITEMS = NAV.filter((entry) => entry.id);

    // ── rail ──────────────────────────────────────────────────────────────
    function buildRail() {
        const nav = document.querySelector('.main-nav');
        const header = document.querySelector('.app-header');
        if (!nav || !header || nav.dataset.dsReady === '1') return;
        nav.dataset.dsReady = '1';
        nav.setAttribute('aria-label', 'Main navigation');

        const logo = header.querySelector('.logo');
        if (logo) {
            logo.innerHTML = `<span class="ds-brand-mark" aria-hidden="true">${ICONS.brand}</span>`
                + '<span class="ds-brand-name">MarketMuse <span>v9</span></span>';
        }

        const known = new Map();
        nav.querySelectorAll('.nav-item').forEach((link) => known.set(link.getAttribute('data-testid'), link));
        const fragment = document.createDocumentFragment();
        NAV.forEach((entry) => {
            if (entry.group) {
                const heading = document.createElement('div');
                heading.className = 'ds-nav-group';
                heading.textContent = entry.group;
                fragment.appendChild(heading);
                return;
            }
            const link = known.get(entry.id);
            if (!link) return;
            known.delete(entry.id);
            link.innerHTML = `<i class="fa-solid ${entry.icon}" aria-hidden="true"></i>`
                + `<span class="ds-nav-label">${esc(entry.label)}</span>`;
            link.title = entry.label;
            link.dataset.view = entry.view;
            fragment.appendChild(link);
        });
        // Links this file does not know about stay reachable at the end.
        known.forEach((link) => fragment.appendChild(link));
        nav.replaceChildren(fragment);

        const foot = document.createElement('div');
        foot.className = 'ds-rail-foot';
        foot.id = 'ds-rail-foot';
        foot.setAttribute('role', 'status');
        foot.innerHTML = '<span class="ds-dot" aria-hidden="true"></span><span>Checking engine…</span>';
        header.querySelector('.header-content').appendChild(foot);

        const scrim = document.createElement('div');
        scrim.className = 'ds-rail-scrim';
        scrim.addEventListener('click', () => root.classList.remove('ds-rail-open'));
        document.body.appendChild(scrim);
        nav.addEventListener('click', (event) => {
            if (event.target.closest('.nav-item')) root.classList.remove('ds-rail-open');
        });
    }

    async function refreshEngineState() {
        const foot = document.getElementById('ds-rail-foot');
        if (!foot) return;
        let state = 'bad';
        let text = 'Server not reachable';
        try {
            const response = await fetch('/api/engine/status', { cache: 'no-store' });
            if (response.ok) {
                const data = await response.json();
                if (data && data.running) {
                    state = data.restart_limit_reached ? 'warn' : 'ok';
                    text = data.restart_limit_reached ? 'Engine restarting too often' : 'Engine running';
                } else {
                    state = 'warn';
                    text = 'Engine stopped';
                }
            } else {
                text = `Engine status unavailable (${response.status})`;
            }
        } catch (_) { /* keep "not reachable" */ }
        foot.dataset.state = state;
        foot.lastElementChild.textContent = text;
        foot.title = text;
    }

    // ── market-session clock ──────────────────────────────────────────────
    const SESSION = { pre: [240, 570], regular: [570, 960], after: [960, 1200] };
    const ET_PARTS = new Intl.DateTimeFormat('en-US', {
        timeZone: 'America/New_York', weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
    });
    let serverSession = null;
    let serverSessionAt = 0;

    function easternNow(now) {
        const parts = {};
        ET_PARTS.formatToParts(now).forEach((part) => { parts[part.type] = part.value; });
        const hour = Number(parts.hour) % 24;
        return { weekday: parts.weekday, minutes: hour * 60 + Number(parts.minute) };
    }

    function localSession(now) {
        const et = easternNow(now);
        const weekend = et.weekday === 'Sat' || et.weekday === 'Sun';
        let state = 'closed';
        if (!weekend) {
            if (et.minutes >= SESSION.pre[0] && et.minutes < SESSION.pre[1]) state = 'pre_market';
            else if (et.minutes >= SESSION.regular[0] && et.minutes < SESSION.regular[1]) state = 'regular';
            else if (et.minutes >= SESSION.after[0] && et.minutes < SESSION.after[1]) state = 'after_hours';
        }
        return { state, closedKind: state === 'closed' ? (weekend ? 'weekend' : 'overnight') : null, minutes: et.minutes };
    }

    function duration(ms) {
        const total = Math.max(0, Math.round(ms / 60000));
        const days = Math.floor(total / 1440);
        const hours = Math.floor((total % 1440) / 60);
        const minutes = total % 60;
        if (days > 0) return `${days}d ${hours}h`;
        if (hours > 0) return `${hours}h ${String(minutes).padStart(2, '0')}m`;
        return `${minutes}m`;
    }

    function minutesUntil(target, current) {
        return (target - current) * 60000;
    }

    function describeSession(now) {
        const local = localSession(now);
        const fresh = serverSession && (now.getTime() - serverSessionAt) < 15 * 60000;
        const server = fresh ? serverSession : null;
        const state = server && server.state ? String(server.state).toLowerCase() : local.state;
        const closedKind = server ? server.closed_kind : local.closedKind;
        const nextPre = server && server.next_premarket_open_utc ? new Date(server.next_premarket_open_utc) : null;
        const nextOpen = server && server.next_regular_open_utc ? new Date(server.next_regular_open_utc) : null;
        const until = (date) => (date && !Number.isNaN(date.getTime()) ? duration(date.getTime() - now.getTime()) : null);

        let title = 'US market closed';
        let note = '';
        if (state === 'regular') {
            title = 'US market open';
            note = `Closes in <b>${duration(minutesUntil(SESSION.regular[1], local.minutes))}</b>`
                + (server && server.is_half_day ? ' · early close today' : ' · 16:00 ET');
        } else if (state === 'pre_market') {
            title = 'Pre-market';
            note = `Regular session opens in <b>${until(nextOpen) || duration(minutesUntil(SESSION.regular[0], local.minutes))}</b> · 09:30 ET`;
        } else if (state === 'after_hours') {
            title = 'After hours';
            note = `Extended trading ends in <b>${duration(minutesUntil(SESSION.after[1], local.minutes))}</b> · 20:00 ET`;
        } else {
            const why = closedKind === 'weekend' ? 'Weekend' : closedKind === 'holiday' ? 'Market holiday' : 'Overnight';
            const wait = until(nextPre);
            note = wait
                ? `${why} · pre-market opens in <b>${wait}</b>`
                : `${why} · quotes are from the last session`;
        }
        return { state, title, note, nowPct: (local.minutes / 1440) * 100, open: state !== 'closed' };
    }

    function renderSession() {
        const host = document.getElementById('ds-session');
        if (!host) return;
        const view = describeSession(new Date());
        host.dataset.state = view.state;
        host.dataset.open = String(view.open);
        host.querySelector('.ds-session-state').textContent = view.title;
        host.querySelector('.ds-session-note').innerHTML = view.note;
        host.querySelector('.ds-session-now').style.left = `${view.nowPct.toFixed(2)}%`;
        host.title = `${view.title}. Track shows one trading day in New York time: pre-market 04:00–09:30, regular 09:30–16:00, after hours 16:00–20:00.`;
    }

    async function refreshServerSession() {
        try {
            const response = await fetch('/api/premarket', { cache: 'no-store' });
            if (!response.ok) return;
            const data = await response.json();
            const session = data && (data.session || (data.status && data.status.session));
            if (session && typeof session === 'object' && session.state) {
                serverSession = session;
                serverSessionAt = Date.now();
                renderSession();
            }
        } catch (_) { /* the local clock keeps the display correct on normal days */ }
    }

    // ── top bar ───────────────────────────────────────────────────────────
    function segmentStyle(range) {
        return `left:${(range[0] / 1440) * 100}%;width:${((range[1] - range[0]) / 1440) * 100}%`;
    }

    function buildTopbar() {
        if (document.getElementById('ds-topbar')) return;
        const bar = document.createElement('div');
        bar.className = 'ds-topbar';
        bar.id = 'ds-topbar';
        bar.setAttribute('role', 'banner');
        const mac = /Mac|iPhone|iPad/.test(navigator.platform || '');
        bar.innerHTML = `
            <button type="button" class="ds-icon-btn" id="ds-rail-toggle" aria-label="Collapse or expand navigation">${ICONS.menu}</button>
            <div class="ds-topbar-title" id="ds-topbar-title" aria-live="polite">Cockpit</div>
            <div class="ds-session" id="ds-session" data-state="closed" data-open="false">
                <span class="ds-session-state">US market</span>
                <span class="ds-session-track" aria-hidden="true">
                    <span class="ds-session-seg" data-kind="pre" style="${segmentStyle(SESSION.pre)}"></span>
                    <span class="ds-session-seg" data-kind="regular" style="${segmentStyle(SESSION.regular)}"></span>
                    <span class="ds-session-seg" data-kind="after" style="${segmentStyle(SESSION.after)}"></span>
                    <span class="ds-session-now"></span>
                </span>
                <span class="ds-session-note"></span>
            </div>
            <div class="ds-topbar-actions">
                <button type="button" class="ds-search-btn" id="ds-search-btn" aria-label="Search tickers and pages">
                    ${ICONS.search}<span>Search ticker or page</span><kbd>${mac ? '⌘K' : 'Ctrl K'}</kbd>
                </button>
                <span class="ds-mode" title="Orders are simulated. No real money is traded.">Paper trading</span>
                <button type="button" class="ds-icon-btn" id="ds-theme-toggle" aria-label="Switch between dark and light theme"></button>
            </div>`;
        document.body.insertBefore(bar, document.body.firstChild);

        bar.querySelector('#ds-rail-toggle').addEventListener('click', () => {
            if (window.matchMedia('(max-width: 960px)').matches) {
                root.classList.toggle('ds-rail-open');
                return;
            }
            const collapsed = root.classList.toggle('ds-rail-min');
            store.set('mm-rail', collapsed ? 'min' : 'full');
        });
        bar.querySelector('#ds-search-btn').addEventListener('click', () => openPalette());
        bar.querySelector('#ds-theme-toggle').addEventListener('click', () => {
            applyTheme(root.getAttribute('data-theme') === 'light' ? 'dark' : 'light');
        });
    }

    function applyTheme(theme) {
        const next = theme === 'light' ? 'light' : 'dark';
        if (next === 'light') root.setAttribute('data-theme', 'light');
        else root.removeAttribute('data-theme');
        store.set('mm-theme', next);
        const button = document.getElementById('ds-theme-toggle');
        if (button) {
            button.innerHTML = next === 'light' ? ICONS.moon : ICONS.sun;
            button.title = next === 'light' ? 'Switch to the dark theme' : 'Switch to the light theme';
        }
        document.dispatchEvent(new CustomEvent('mm:theme', { detail: { theme: next } }));
    }

    function syncTitle() {
        const active = document.querySelector('.main-nav .nav-item.active');
        const title = document.getElementById('ds-topbar-title');
        if (!active || !title) return;
        const entry = NAV_ITEMS.find((item) => item.id === active.getAttribute('data-testid'));
        const label = entry ? entry.label : (active.textContent || '').trim();
        if (title.textContent !== label) title.textContent = label;
        document.querySelectorAll('.main-nav .nav-item').forEach((link) => {
            if (link === active) link.setAttribute('aria-current', 'page');
            else link.removeAttribute('aria-current');
        });
    }

    // ── command palette ───────────────────────────────────────────────────
    let paletteItems = [];
    let paletteIndex = 0;
    let watchlistTickers = null;

    function collectTickers(payload, out) {
        if (!payload) return;
        if (typeof payload === 'string') {
            const ticker = payload.trim().toUpperCase();
            if (/^[A-Z0-9.\-=^]{1,15}$/.test(ticker)) out.add(ticker);
        } else if (Array.isArray(payload)) {
            payload.forEach((item) => collectTickers(item && typeof item === 'object' ? (item.ticker || item.symbol) : item, out));
        } else if (typeof payload === 'object') {
            Object.keys(payload).forEach((key) => {
                if (Array.isArray(payload[key])) collectTickers(payload[key], out);
            });
        }
    }

    async function loadTickers() {
        if (watchlistTickers) return watchlistTickers;
        const found = new Set();
        try {
            const response = await fetch('/api/watchlist');
            if (response.ok) collectTickers(await response.json(), found);
        } catch (_) { /* palette still lists pages */ }
        watchlistTickers = Array.from(found).sort();
        return watchlistTickers;
    }

    function go(view) {
        if (typeof ViewRouter !== 'undefined' && ViewRouter && typeof ViewRouter.navigate === 'function') {
            ViewRouter.navigate(view);
        }
    }

    function openTicker(ticker) {
        if (typeof UI !== 'undefined' && UI && typeof UI.openWatchlistTicker === 'function') {
            UI.openWatchlistTicker(ticker);
        } else {
            go('watchlist');
        }
    }

    function buildPalette() {
        if (document.getElementById('ds-palette')) return;
        const backdrop = document.createElement('div');
        backdrop.className = 'ds-palette-backdrop';
        backdrop.id = 'ds-palette';
        backdrop.dataset.open = 'false';
        backdrop.innerHTML = `
            <div class="ds-palette" role="dialog" aria-modal="true" aria-label="Search tickers and pages">
                <input type="text" id="ds-palette-input" placeholder="Type a ticker or a page name" autocomplete="off" spellcheck="false"
                    role="combobox" aria-expanded="true" aria-controls="ds-palette-list">
                <div class="ds-palette-list" id="ds-palette-list" role="listbox"></div>
                <div class="ds-palette-foot"><span><kbd>↑</kbd> <kbd>↓</kbd> move</span><span><kbd>Enter</kbd> open</span><span><kbd>Esc</kbd> close</span></div>
            </div>`;
        document.body.appendChild(backdrop);
        backdrop.addEventListener('mousedown', (event) => { if (event.target === backdrop) closePalette(); });
        const input = backdrop.querySelector('#ds-palette-input');
        input.addEventListener('input', () => renderPalette(input.value));
        input.addEventListener('keydown', (event) => {
            if (event.key === 'ArrowDown') { event.preventDefault(); movePalette(1); }
            else if (event.key === 'ArrowUp') { event.preventDefault(); movePalette(-1); }
            else if (event.key === 'Enter') { event.preventDefault(); runPalette(paletteIndex); }
            else if (event.key === 'Escape') { event.preventDefault(); closePalette(); }
        });
        backdrop.querySelector('#ds-palette-list').addEventListener('click', (event) => {
            const item = event.target.closest('.ds-palette-item');
            if (item) runPalette(Number(item.dataset.index));
        });
    }

    function renderPalette(query) {
        const list = document.getElementById('ds-palette-list');
        if (!list) return;
        const text = String(query || '').trim();
        const upper = text.toUpperCase();
        const lower = text.toLowerCase();
        const pages = NAV_ITEMS.filter((item) => !text || item.label.toLowerCase().includes(lower));
        const tickers = (watchlistTickers || []).filter((ticker) => (text ? ticker.includes(upper) : true)).slice(0, text ? 12 : 6);
        paletteItems = [];
        let html = '';
        // "back" + Enter must open Backtest, not look up a ticker called BACK:
        // when the text is the start of a page name, pages are listed first.
        const pagesFirst = Boolean(text) && pages.some((page) => page.label.toLowerCase().startsWith(lower));
        const pagesHtml = () => {
            if (!pages.length) return;
            html += '<div class="ds-palette-group">Pages</div>';
            pages.forEach((page) => {
                html += `<button type="button" class="ds-palette-item" role="option" data-index="${paletteItems.length}"><span>${esc(page.label)}</span><small>Go to page</small></button>`;
                paletteItems.push({ run: () => go(page.view) });
            });
        };
        if (pagesFirst) pagesHtml();
        if (tickers.length) {
            html += '<div class="ds-palette-group">Watchlist</div>';
            tickers.forEach((ticker) => {
                html += `<button type="button" class="ds-palette-item" role="option" data-index="${paletteItems.length}"><span class="ds-palette-ticker">${esc(ticker)}</span><small>Open in watchlist</small></button>`;
                paletteItems.push({ run: () => openTicker(ticker) });
            });
        }
        if (text && /^[A-Z0-9.\-=^]{1,15}$/.test(upper) && !tickers.includes(upper)) {
            html += '<div class="ds-palette-group">Ticker</div>';
            html += `<button type="button" class="ds-palette-item" role="option" data-index="${paletteItems.length}"><span class="ds-palette-ticker">${esc(upper)}</span><small>Look up in watchlist</small></button>`;
            paletteItems.push({ run: () => openTicker(upper) });
        }
        if (!pagesFirst) pagesHtml();
        list.innerHTML = html || `<div class="ds-palette-empty">Nothing matches “${esc(text)}”. Type a ticker symbol or a page name.</div>`;
        paletteIndex = 0;
        markPalette();
    }

    function markPalette() {
        document.querySelectorAll('#ds-palette-list .ds-palette-item').forEach((item) => {
            const selected = Number(item.dataset.index) === paletteIndex;
            item.setAttribute('aria-selected', String(selected));
            if (selected) item.scrollIntoView({ block: 'nearest' });
        });
    }

    function movePalette(step) {
        if (!paletteItems.length) return;
        paletteIndex = (paletteIndex + step + paletteItems.length) % paletteItems.length;
        markPalette();
    }

    function runPalette(index) {
        const item = paletteItems[index];
        closePalette();
        if (item) item.run();
    }

    let paletteReturnFocus = null;

    function openPalette() {
        buildPalette();
        const backdrop = document.getElementById('ds-palette');
        const input = document.getElementById('ds-palette-input');
        paletteReturnFocus = document.activeElement;
        backdrop.dataset.open = 'true';
        input.value = '';
        renderPalette('');
        input.focus();
        loadTickers().then(() => { if (backdrop.dataset.open === 'true') renderPalette(input.value); });
    }

    function closePalette() {
        const backdrop = document.getElementById('ds-palette');
        if (!backdrop || backdrop.dataset.open !== 'true') return;
        backdrop.dataset.open = 'false';
        if (paletteReturnFocus && typeof paletteReturnFocus.focus === 'function') paletteReturnFocus.focus();
    }

    function isTyping(target) {
        if (!target) return false;
        const tag = (target.tagName || '').toLowerCase();
        return tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable;
    }

    // ── start ─────────────────────────────────────────────────────────────
    function start() {
        if (store.get('mm-rail') === 'min') root.classList.add('ds-rail-min');
        buildRail();
        buildTopbar();
        applyTheme(store.get('mm-theme') === 'light' ? 'light' : 'dark');
        syncTitle();
        renderSession();

        const nav = document.querySelector('.main-nav');
        if (nav && typeof MutationObserver === 'function') {
            new MutationObserver(syncTitle).observe(nav, { subtree: true, attributes: true, attributeFilter: ['class'] });
        }

        document.addEventListener('keydown', (event) => {
            const key = String(event.key || '').toLowerCase();
            if ((event.ctrlKey || event.metaKey) && key === 'k') {
                event.preventDefault();
                openPalette();
            } else if (key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey && !isTyping(event.target)) {
                event.preventDefault();
                openPalette();
            }
        });

        window.setInterval(renderSession, 30000);
        refreshServerSession();
        window.setInterval(refreshServerSession, 5 * 60000);
        refreshEngineState();
        window.setInterval(refreshEngineState, 30000);
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();

    window.MMShell = { openPalette, closePalette, applyTheme, describeSession, localSession };
})();
