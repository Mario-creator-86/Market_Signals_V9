/**
 * skeleton_utils.js — Stage 6 UI Premium Polish
 *
 * Utilities for:
 *  1. Skeleton placeholder generation
 *  2. Empty state rendering
 *  3. Freshness timestamp badges
 *  3b. Data quality badges
 *
 * All functions return HTML strings or DOM elements — they never
 * mutate global state or call APIs.
 */

// ────────────────────────────────────────────────────────────
// 1.  SKELETON BUILDER
// ────────────────────────────────────────────────────────────

const SkeletonUI = {

  /**
   * Generate N skeleton text lines.
   * widths: array of CSS width strings, e.g. ['100%', '75%', '60%']
   */
  lines(widths = ['100%', '75%', '50%']) {
    return widths
      .map(w => `<span class="skeleton-line" style="width:${w}"></span>`)
      .join('');
  },

  /**
   * One skeleton row (circle avatar + two text lines).
   */
  row() {
    return `
      <div class="skeleton-row">
        <span class="skeleton-circle"></span>
        <div style="flex:1; display:flex; flex-direction:column; gap:6px;">
          ${this.lines(['60%', '40%'])}
        </div>
        <span class="skeleton-badge"></span>
      </div>`;
  },

  /**
   * N skeleton table rows.
   */
  rows(n = 5) {
    return Array.from({ length: n }, () => this.row()).join('');
  },

  /**
   * Hotlist-style skeleton card.
   */
  hotlistCard() {
    return `
      <div class="skeleton-hotlist-card">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <span class="skeleton-line h-lg w-40"></span>
          <span class="skeleton-badge"></span>
        </div>
        ${this.lines(['100%', '75%'])}
        <div style="display:flex; gap:8px; margin-top:4px;">
          <span class="skeleton-badge"></span>
          <span class="skeleton-badge"></span>
        </div>
      </div>`;
  },

  /**
   * N hotlist cards in a grid wrapper.
   */
  hotlistGrid(n = 6) {
    return `<div class="skeleton-grid">
      ${Array.from({ length: n }, () => this.hotlistCard()).join('')}
    </div>`;
  },

  /**
   * News card skeleton.
   */
  newsCard() {
    return `
      <div class="skeleton-news-card">
        <span class="skeleton-circle" style="width:32px;height:32px;"></span>
        <div style="flex:1; display:flex; flex-direction:column; gap:6px;">
          ${this.lines(['90%', '60%', '40%'])}
        </div>
      </div>`;
  },

  /**
   * N news cards.
   */
  newsCards(n = 5) {
    return Array.from({ length: n }, () => this.newsCard()).join('');
  },

  /**
   * Scanner row skeleton.
   */
  scannerRow() {
    return `
      <div class="skeleton-scanner-row">
        <span class="skeleton-line h-lg w-75"></span>
        <span class="skeleton-line h-sm"></span>
        <span class="skeleton-line h-sm"></span>
        <span class="skeleton-line h-sm"></span>
        <span class="skeleton-line h-sm"></span>
        <span class="skeleton-badge"></span>
        <span class="skeleton-line h-sm w-60"></span>
      </div>`;
  },

  /**
   * Inject skeleton into an element by ID.
   * @param {string} elementId
   * @param {string} html
   */
  inject(elementId, html) {
    const el = document.getElementById(elementId);
    if (el) el.innerHTML = html;
  },

  /**
   * Clear skeleton (replace with empty string).
   */
  clear(elementId) {
    const el = document.getElementById(elementId);
    if (el) el.innerHTML = '';
  },
};


// ────────────────────────────────────────────────────────────
// 2.  EMPTY STATE BUILDER
// ────────────────────────────────────────────────────────────

const EmptyStateUI = {
  /**
   * Render a full empty state.
   * @param {object} opts
   * @param {string} opts.icon         FA class, e.g. 'fa-solid fa-fire'
   * @param {string} opts.title        Main message
   * @param {string} [opts.subtitle]   Secondary message
   * @param {string} [opts.action]     HTML for action button
   * @param {boolean} [opts.compact]   Use compact layout
   */
  render({ icon, title, subtitle = '', action = '', compact = false }) {
    const cls = compact ? 'empty-state empty-state--compact' : 'empty-state';
    return `
      <div class="${cls}">
        <div class="empty-state__icon"><i class="${icon}"></i></div>
        <p class="empty-state__title">${title}</p>
        ${subtitle ? `<p class="empty-state__subtitle">${subtitle}</p>` : ''}
        ${action ? `<div class="empty-state__action">${action}</div>` : ''}
      </div>`;
  },

  /**
   * Preset: no data yet (spinner inside).
   */
  loading(message = 'Loading data...') {
    return `
      <div class="empty-state">
        <div class="empty-state__icon">
          <i class="fa-solid fa-circle-notch" style="animation: spin 1s linear infinite; color:var(--accent-color, #3b82f6);"></i>
        </div>
        <p class="empty-state__title">${message}</p>
      </div>`;
  },

  // ── Presets per tab ──
  hotlist() {
    return this.render({
      icon: 'fa-solid fa-fire',
      title: 'Hot List is empty',
      subtitle: 'Add tickers from the Watchlist table to track them here.',
      action: `<button class="btn-mini" onclick="ViewRouter.navigate('watchlist')">
                 <i class="fa-solid fa-arrow-right"></i> Go to Watchlist
               </button>`,
    });
  },

  news() {
    return this.render({
      icon: 'fa-solid fa-newspaper',
      title: 'No news available',
      subtitle: 'The news agent is initializing. Check again in a few moments.',
      action: `<button class="btn-mini btn-mini--accent" onclick="Actions.forceRefreshNews()">
                 <i class="fa-solid fa-rotate"></i> Refresh
               </button>`,
    });
  },

  premarket() {
    return this.render({
      icon: 'fa-solid fa-sun',
      title: 'No Premarket candidates',
      subtitle: 'The Premarket scanner is waiting or the market is not open yet.',
    });
  },

  preearnings() {
    return this.render({
      icon: 'fa-solid fa-wave-square',
      title: 'No Pre-Earnings tickers',
      subtitle: 'No earnings scheduled in the current window or the agent is initializing.',
    });
  },

  scanner() {
    return this.render({
      icon: 'fa-solid fa-radar',
      title: 'No movers detected',
      subtitle: 'The scanner found no significant moves. The market may be at low volume.',
    });
  },

  signals() {
    return this.render({
      icon: 'fa-solid fa-signal',
      title: 'No active signals',
      subtitle: 'The engine is monitoring the market. Signals will appear automatically.',
    });
  },

  watchlist() {
    return this.render({
      icon: 'fa-solid fa-bookmark',
      title: 'Watchlist is empty',
      subtitle: 'Add tickers to track prices and signals.',
      action: `<button class="btn-auth" onclick="Actions.addTicker()">
                 <i class="fa-solid fa-plus"></i> Add Ticker
               </button>`,
    });
  },

  calendar() {
    return this.render({
      icon: 'fa-solid fa-calendar-days',
      title: 'No calendar events',
      subtitle: 'No economic events or earnings in the selected period.',
    });
  },

  traderadar() {
    return this.render({
      icon: 'fa-solid fa-crosshairs',
      title: 'Trade Radar has no candidates',
      subtitle: 'The agent is monitoring the market for high-conviction trade setups.',
    });
  },

  agents() {
    return this.render({
      icon: 'fa-solid fa-robot',
      title: 'Loading agents...',
      subtitle: 'Initializing the autonomous agent system.',
    });
  },

  /**
   * Inject empty state into an element by ID.
   */
  inject(elementId, html) {
    const el = document.getElementById(elementId);
    if (el) el.innerHTML = html;
  },
};


// ────────────────────────────────────────────────────────────
// 3.  FRESHNESS BADGE
// ────────────────────────────────────────────────────────────

const FreshnessBadge = {
  /**
   * Returns a freshness-badge HTML string.
   * @param {string|null} isoTs  ISO 8601 timestamp or null
   * @param {object} opts
   * @param {number} [opts.warnMinutes=2]   Warn after N minutes
   * @param {number} [opts.staleMinutes=10] Stale after N minutes
   */
  render(isoTs, { warnMinutes = 2, staleMinutes = 10 } = {}) {
    if (!isoTs) {
      return `<span class="freshness-badge freshness-badge--unknown">Unknown</span>`;
    }
    const ts = new Date(isoTs);
    if (isNaN(ts.getTime())) {
      return `<span class="freshness-badge freshness-badge--unknown">&minus;</span>`;
    }
    const ageMs = Date.now() - ts.getTime();
    const ageSec = Math.floor(ageMs / 1000);
    const ageMin = Math.floor(ageSec / 60);

    let cls, label;
    if (ageSec < warnMinutes * 60) {
      cls = 'freshness-badge--good';
      label = ageSec < 10 ? 'Now' : `${ageSec}s`;
    } else if (ageSec < staleMinutes * 60) {
      cls = 'freshness-badge--warn';
      label = `${ageMin}m`;
    } else {
      cls = 'freshness-badge--stale';
      const h = Math.floor(ageMin / 60);
      label = h > 0 ? `${h}h ${ageMin % 60}m` : `${ageMin}m`;
    }

    // Same notation as the rest of the UI (UI._fmtDateTime in app.js, en-US,
    // 24-hour clock); this file loads before app.js, hence the guarded lookup.
    const fullTs = (typeof UI !== 'undefined' && UI && typeof UI._fmtDateTime === 'function')
      ? UI._fmtDateTime(ts, 'datetime', { seconds: true })
      : ts.toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
    return `<span class="freshness-badge ${cls}" title="Updated ${fullTs}">${label}</span>`;
  },

  /**
   * Inject a freshness badge into an element.
   * @param {string} elementId
   * @param {string|null} isoTs
   * @param {object} opts
   */
  inject(elementId, isoTs, opts = {}) {
    const el = document.getElementById(elementId);
    if (el) el.innerHTML = this.render(isoTs, opts);
  },

  /**
   * Start auto-updating a freshness badge every 30 seconds.
   * Returns a clearInterval handle.
   */
  autoUpdate(elementId, getTimestamp, opts = {}, intervalMs = 30000) {
    const update = () => this.inject(elementId, getTimestamp(), opts);
    update();
    return setInterval(update, intervalMs);
  },
};


// ────────────────────────────────────────────────────────────
// 3b. DATA QUALITY BADGE
// ────────────────────────────────────────────────────────────

const DataQualityBadge = {
  /**
   * Returns a data-quality badge HTML string.
   * @param {string} level  One of: "VERIFIED", "ESTIMATED", "STALE", "UNAVAILABLE", or "" (unknown)
   * @param {object} [opts]
   * @param {boolean} [opts.compact=false]  If true, show short label only
   */
  render(level, { compact = false } = {}) {
    const lvl = (level || '').toUpperCase();
    const map = {
      VERIFIED:    { cls: 'data-quality-badge--verified',    label: compact ? 'V' : 'Verified',   title: 'Data verified from primary source' },
      ESTIMATED:   { cls: 'data-quality-badge--estimated',   label: compact ? 'E' : 'Estimated',  title: 'Data from fallback or interpolated source' },
      STALE:       { cls: 'data-quality-badge--stale',       label: compact ? 'S' : 'Stale',      title: 'Data is older than allowed threshold' },
      UNAVAILABLE: { cls: 'data-quality-badge--unavailable', label: compact ? 'U' : 'Unavailable', title: 'Data source unavailable or error' },
    };
    const info = map[lvl] || { cls: 'data-quality-badge--unknown', label: compact ? '?' : 'Unverified', title: 'Data quality unknown' };
    return `<span class="data-quality-badge ${info.cls}" title="${info.title}">${info.label}</span>`;
  },

  /**
   * Derive a per-ticker quality level from the ticker data dict.
   * Falls back to the global data_health summary if per-ticker level is absent.
   * @param {object} tickerData   Single ticker entry from /api/state
   * @param {object} [dataHealth] data_health object from /api/state or /api/health/diagnostics
   * @returns {string} Quality level string
   */
  levelForTicker(tickerData, dataHealth) {
    const src = (tickerData || {}).data_source || '';
    const q   = (tickerData || {}).data_quality || '';
    if (q) return q.toUpperCase();

    if (src === 'yahoo' || src === 'yfinance') return 'VERIFIED';
    if (src === 'binance') return 'VERIFIED';
    if (src === 'finnhub') return 'ESTIMATED';

    if (dataHealth && dataHealth.streams) {
      const isCrypto = (tickerData || {}).asset_type === 'crypto';
      const key = isCrypto ? 'binance_crypto' : 'yahoo_stocks';
      const stream = dataHealth.streams[key];
      if (stream) return stream.level || 'UNKNOWN';
    }
    return '';
  },

  /**
   * Render a summary bar of all data-health streams.
   * @param {object} dataHealth  data_health object from /api/state or diagnostics
   */
  renderSummary(dataHealth) {
    if (!dataHealth || !dataHealth.streams) return '';
    const entries = Object.entries(dataHealth.streams);
    if (!entries.length) return '';
    return entries.map(([key, val]) => {
      const lvl = (val.level || '').toUpperCase();
      return this.render(lvl);
    }).join(' ');
  }
};


// ────────────────────────────────────────────────────────────
// 4.  EXPORTS (global namespace)
// ────────────────────────────────────────────────────────────

window.SkeletonUI = SkeletonUI;
window.EmptyStateUI = EmptyStateUI;
window.FreshnessBadge = FreshnessBadge;
window.DataQualityBadge = DataQualityBadge;