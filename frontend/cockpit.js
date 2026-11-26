/* Phase 12 — central AI Trading Cockpit. No trading values are selected here. */
(function () {
    'use strict';

    const ASYNC_STATES = new Set(['LOADING', 'SUCCESS', 'EMPTY', 'ERROR']);
    const model = {
        payload: null,
        version: '',
        activeTab: 'overview',
        loading: false,
        abortController: null,
        interval: null,
        sourceRefreshTimer: null,
        stopArmedUntil: 0,
        stopArmTimer: null,
        autoLab: null,
        autoLabLoadedAt: 0,
        learning: null,
        learningLoadedAt: 0,
        expandedEarningsGates: new Set(),
        expandedRepairCases: new Set(),
        ledgerEverHadOpen: false,
        actionError: null,
    };

    const byId = (id) => document.getElementById(id);
    const esc = (value) => String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    const finite = (value) => {
        if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
        const number = Number(value);
        return Number.isFinite(number) ? number : null;
    };
    const fmtNumber = (value, digits = 1) => {
        const number = finite(value);
        return number === null ? '—' : number.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
    };
    const fmtMoney = (value, digits = 2) => {
        const number = finite(value);
        return number === null ? '—' : number.toLocaleString(undefined, { style: 'currency', currency: 'USD', minimumFractionDigits: digits, maximumFractionDigits: digits });
    };
    const fmtPct = (value, digits = 1, signed = false) => {
        const number = finite(value);
        if (number === null) return '—';
        const sign = signed && number > 0 ? '+' : '';
        return `${sign}${number.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}%`;
    };
    const fmtDate = (value, withTime = false) => {
        if (!value) return '—';
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) return '—';
        // Same notation as the rest of the app (UI._fmtDateTime in app.js:
        // "Nov 15, 2026, 10:50"). The browser locale used here printed
        // "15 nov. 2026, 09:53" next to en-US dates on other tabs.
        if (typeof UI !== 'undefined' && UI && typeof UI._fmtDateTime === 'function') {
            return UI._fmtDateTime(parsed, withTime ? 'datetime' : 'date') || '—';
        }
        return parsed.toLocaleString('en-US', withTime
            ? { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }
            : { year: 'numeric', month: 'short', day: 'numeric' });
    };
    const fmtAge = (seconds) => {
        const value = finite(seconds);
        if (value === null) return 'age unavailable';
        if (value < 60) return `${Math.max(0, Math.round(value))}s ago`;
        if (value < 3600) return `${Math.round(value / 60)}m ago`;
        if (value < 86400) return `${Math.round(value / 3600)}h ago`;
        return `${Math.round(value / 86400)}d ago`;
    };
    const fmtDuration = (seconds) => {
        const value = finite(seconds);
        if (value === null) return 'interval unavailable';
        if (value < 60) return `${Math.max(0, Math.round(value))}s`;
        if (value < 3600) return `${Math.round(value / 60)}m`;
        return `${Math.round(value / 3600)}h`;
    };
    const safeState = (value) => {
        const normalized = String(value || 'EMPTY').toUpperCase();
        return ASYNC_STATES.has(normalized) ? normalized : 'ERROR';
    };
    const words = (value) => String(value || 'unavailable').replaceAll('_', ' ').toLowerCase();
    // `words()` is for enum codes (CLOSED_NO_DEFECT -> "closed no defect").
    // Free text - titles, instructions, errors, file names - is shown verbatim:
    // lower-casing it turned "Set EDGAR_IDENTITY in .env" into "set edgar
    // identity in .env" and "news_tab_agent.py" into "news tab agent.py".
    const plain = (value) => String(value == null ? '' : value);
    const titleWords = (value) => words(value).replace(/\b\w/g, (letter) => letter.toUpperCase());
    const safeHttpUrl = (value) => {
        try {
            const parsed = new URL(String(value || ''), window.location.href);
            return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : '';
        } catch (_) {
            return '';
        }
    };
    const toneForStatus = (status) => {
        const normalized = String(status || '').toUpperCase();
        if (['HEALTHY', 'ACTIVE', 'SUCCESS', 'ELIGIBLE', 'OPEN', 'REGULAR', 'PASS', 'POSITION_OPEN'].includes(normalized)) return 'good';
        if (['UNHEALTHY', 'ERROR', 'BLOCKED', 'CRITICAL', 'CONFLICT'].includes(normalized)) return 'bad';
        if (['DEGRADED', 'UNVERIFIED', 'DRAFT', 'WATCH', 'HIGH', 'PRE_MARKET', 'AFTER_HOURS'].includes(normalized)) return 'warn';
        return 'muted';
    };
    const sentenceWords = (value) => {
        const text = words(value);
        return text.charAt(0).toUpperCase() + text.slice(1);
    };
    // Display tone of a status chip. Rare, actionable states carry colour; the
    // everyday ones (rejected, blocked, closed) stay neutral so they do not
    // shout from every row of a list.
    const CHIP_TONES = {
        pos: ['HEALTHY', 'ACTIVE', 'SUCCESS', 'ELIGIBLE', 'OPEN', 'REGULAR', 'PASS', 'PASSED', 'POSITION_OPEN', 'APPROVED', 'APPLIED', 'LONG', 'BULLISH', 'FRESH', 'RUNNING', 'CHAMPION', 'KEEP', 'STABLE', 'CLOSED_NO_DEFECT', 'MEASURED', 'VERIFIED', 'READY'],
        neg: ['UNHEALTHY', 'ERROR', 'CRITICAL', 'CONFLICT', 'FAILED', 'SHORT', 'BEARISH', 'ROLLED_BACK', 'GENERATION_FAILED', 'WF ERROR'],
        warn: ['DEGRADED', 'DRAFT', 'WATCH', 'HIGH', 'PRE_MARKET', 'AFTER_HOURS', 'STALE', 'WARNING'],
        info: ['MEDIUM', 'INFO', 'PROPOSED', 'DETECTED', 'IN_PROGRESS', 'CHALLENGER', 'SHADOW'],
    };
    const chipTone = (normalized) => Object.keys(CHIP_TONES).find((tone) => CHIP_TONES[tone].includes(normalized)) || '';
    const statusPill = (status) => {
        const normalized = String(status || 'UNAVAILABLE').toUpperCase();
        const tone = chipTone(normalized);
        return `<span class="cockpit-status-pill ds-chip${tone ? ` ds-chip--${tone}` : ''} is-${esc(normalized.toLowerCase())}">${esc(sentenceWords(normalized))}</span>`;
    };
    // Anything a model wrote (verdict, narrative, confidence) carries this chip.
    const aiChip = '<span class="ds-chip ds-chip--ai cockpit-ai-chip" title="Produced by an AI model, not measured">AI</span>';
    const actionLine = (text) => `<span class="cockpit-item-action"><b>Next step</b>${esc(text)}</span>`;
    const fmtSignedMoney = (value, digits = 2) => {
        const number = finite(value);
        return number === null ? '—' : `${number > 0 ? '+' : ''}${fmtMoney(number, digits)}`;
    };
    // Card chips speak the owner's language. The transport state
    // (SUCCESS / EMPTY / ERROR / LOADING) is never printed: "Current risks —
    // SUCCESS · 5" read as if having five risks were a success.
    //   labels.text  full replacement label for a loaded section
    //   labels.one / labels.many  noun for the count ("risk" / "risks")
    //   labels.empty  label when nothing is present
    //   labels.tone  'attention' | 'bad' | 'muted' colour override
    const statePill = (section, labels = {}) => {
        const state = safeState(section && section.state);
        const count = finite(section && section.count);
        let label;
        if (state === 'ERROR') label = 'Unavailable';
        else if (state === 'LOADING') label = 'Loading';
        else if (state === 'EMPTY') label = labels.empty || 'None';
        else if (labels.text) label = labels.text;
        else if (count !== null) label = `${count.toLocaleString()} ${count === 1 ? (labels.one || 'item') : (labels.many || 'items')}`;
        else label = labels.ok || 'Loaded';
        const tone = state === 'SUCCESS' && labels.tone ? ` is-${labels.tone}` : '';
        return `<span class="cockpit-state-pill is-${state.toLowerCase()}${esc(tone)}">${esc(label)}</span>`;
    };
    const ageSeconds = (value) => {
        if (!value) return null;
        const time = new Date(value).getTime();
        return Number.isNaN(time) ? null : Math.max(0, (Date.now() - time) / 1000);
    };
    const fmtAgeOf = (value) => (ageSeconds(value) === null ? 'time unknown' : fmtAge(ageSeconds(value)));
    const formatValue = (value, depth = 0) => {
        if (value === null || value === undefined || value === '') return '';
        if (Array.isArray(value)) {
            const items = value.map((item) => formatValue(item, depth + 1)).filter(Boolean);
            return items.length ? items.join(', ') : '';
        }
        if (typeof value === 'object') {
            const entries = Object.entries(value).filter(([, val]) => formatValue(val, depth + 1) !== '');
            if (!entries.length) return '';
            const indent = '  '.repeat(depth);
            return entries.map(([key, val]) => {
                const text = formatValue(val, depth + 1);
                return `${indent}${words(key)}: ${typeof val === 'object' && val !== null ? `\n${text}` : text}`;
            }).join('\n');
        }
        if (typeof value === 'boolean') return value ? 'yes' : 'no';
        return String(value);
    };
    const evidenceText = (value) => {
        const text = formatValue(value);
        return text && text.trim() ? text : null;
    };
    const routeButton = (label, route, icon = 'fa-arrow-right') => (
        `<button type="button" class="cockpit-link-btn" data-cockpit-route="${esc(route)}">${esc(label)} <i class="fa-solid ${esc(icon)}"></i></button>`
    );

    function sectionBody(section, html) {
        const state = safeState(section && section.state);
        if (state === 'ERROR') {
            return `<div class="cockpit-section-error"><i class="fa-solid fa-triangle-exclamation"></i><span>${esc(section.message || 'This verified source is unavailable.')}</span></div>`;
        }
        if (state === 'EMPTY') {
            return `<div class="cockpit-empty"><span>${esc(section.message || 'No evidence is available for this section.')}</span></div>`;
        }
        return html || `<div class="cockpit-empty"><span>No displayable evidence was returned.</span></div>`;
    }

    function card(title, section, html, action = '', labels = {}, options = {}) {
        return `<article class="cockpit-card">
            <div class="cockpit-card-head">
                <div class="cockpit-card-title">${esc(title)}${options.ai ? aiChip : ''}</div>
                ${statePill(section, labels)}
            </div>
            <div class="cockpit-card-body">${sectionBody(section, html)}</div>
            ${action ? `<div class="cockpit-card-actions">${action}</div>` : ''}
        </article>`;
    }

    function renderKpis(payload) {
        const header = payload.header || {};
        const market = header.market || {};
        const autonomy = header.autonomy || {};
        const ai = header.ai || {};
        const regime = header.regime || {};
        const capital = header.capital || {};
        const risk = header.risk || {};
        const health = payload.system_health || {};
        const marketLabel = market.is_open === true ? 'Open' : titleWords(market.session || 'Closed');
        const dailyPnl = finite(capital.daily_realized_pnl);
        const positions = capital.open_positions;
        const positionsLabel = positions === null || positions === undefined ? '—' : `${positions} position${positions === 1 ? '' : 's'}`;
        // Execution source failure means readiness was never evaluated — say
        // "unknown", never "0 blockers" (which implies a verified pass).
        const autonomyDetail = autonomy.ready === true ? 'Paper entry ready'
            : (autonomy.evaluated === false ? 'Readiness unknown (execution source error)'
               : `${(autonomy.reasons || []).length} readiness blocker${(autonomy.reasons || []).length === 1 ? '' : 's'}`);
        const autonomyTone = autonomy.ready === true ? 'good' : (autonomy.evaluated === false ? '' : 'warn');
        // A regime derived from an old narrative is the LAST KNOWN regime: show
        // its age and drop the "healthy" colour instead of presenting it as live.
        const regimeStale = regime.available && String(regime.freshness || '').toUpperCase() === 'STALE';
        const regimeDetail = !regime.available ? 'No source-backed regime'
            : `${titleWords(regime.sentiment)} · confidence ${fmtPct(regime.confidence, 0)}${regime.generated_at ? ` · ${regimeStale ? 'STALE, ' : ''}narrative ${fmtAgeOf(regime.generated_at)}` : ''}`;
        const dailyBasis = `${capital.daily_pnl_basis || 'Realized ledger basis unavailable'}${capital.daily_pnl_window_truncated ? ' · partial: the ledger window does not cover the whole day' : ''}`;
        const dailyCloses = finite(capital.daily_closed_trades);
        const dailyValue = dailyPnl === null ? 'No closes today'
            : `${fmtSignedMoney(dailyPnl)}${dailyCloses ? ` · ${dailyCloses} close${dailyCloses === 1 ? '' : 's'}` : ''}`;
        // Say WHICH checks fail; "Unhealthy · 15 real checks" told the owner nothing.
        const failingChecks = Array.isArray(health.failing) ? health.failing : [];
        const serviceCount = (health.services || []).length;
        const healthDetail = failingChecks.length
            ? `${failingChecks.length} of ${serviceCount} checks need attention: ${failingChecks.slice(0, 3).map((item) => item.label).join(', ')}${failingChecks.length > 3 ? '…' : ''}`
            : (health.summary || `${serviceCount} checks`);
        // Status colour is reserved for the two states the owner must act on
        // (autonomy readiness, system health); every other value is plain.
        const healthTone = toneForStatus(health.overall);
        const kpis = [
            ['Market status', marketLabel, `${market.exchange || 'XNYS'} · ${words(market.reason)}`, ''],
            ['Autonomy mode', autonomy.mode ? titleWords(autonomy.mode) : '—', autonomyDetail, autonomyTone],
            ['AI status', titleWords(ai.status), ai.global_model ? `Global · ${ai.global_model}` : ((ai.configured_providers || []).join(', ') || 'No configured provider'), ''],
            [regimeStale ? 'Market regime (last known)' : 'Market regime', regime.available ? titleWords(regime.value) : 'Unavailable', regimeDetail, ''],
            ['Deployable capital', fmtMoney(capital.deployable_cash), `Estimated equity ${fmtMoney(capital.estimated_equity)}`, ''],
            ['Portfolio exposure', fmtPct(capital.notional_exposure_pct), `Notional ${fmtMoney(capital.notional_exposure)} · ${positionsLabel}`, ''],
            ['Realized P/L today', dailyValue, dailyBasis, ''],
            ['Risk status', titleWords(risk.status), risk.readiness ? 'Paper readiness passed' : 'Paper readiness blocked', ''],
            ['Active positions', positions === null || positions === undefined ? '—' : String(positions), `Unrealized ${fmtSignedMoney(capital.unrealized_pnl)}`, ''],
            ['System health', titleWords(health.overall), healthDetail, healthTone === 'muted' ? '' : healthTone],
        ];
        const target = byId('cockpit-kpis');
        if (!target) return;
        target.innerHTML = kpis.map(([label, value, detail, tone]) => `<div class="cockpit-kpi ds-stat">
            <span class="cockpit-kpi-label ds-label">${esc(label)}</span>
            <strong class="cockpit-kpi-value ds-stat-value${tone ? ` cockpit-tone--${tone}` : ''}" title="${esc(value)}">${esc(value)}</strong>
            <small class="cockpit-kpi-detail ds-stat-note" title="${esc(detail)}">${esc(detail)}</small>
        </div>`).join('');
    }

    // A ticker without a usable price ("No data" / "Delisted or inactive · last traded
    // <date>"): from the item's own marking, else from the engine row app.js holds.
    const priceState = (item) => ((typeof UI !== 'undefined' && UI && typeof UI._tickerPriceState === 'function')
        ? UI._tickerPriceState(item && item.ticker, item)
        : null);
    const priceStateNote = (state, extraClass = '') => (state
        ? `<span class="ms-price-state ms-price-state--${esc(state.kind)}${extraClass ? ` ${extraClass}` : ''}" title="${esc(state.title)}">${esc(state.label)}</span>`
        : '');

    function renderOpportunityList(section) {
        return (section.items || []).map((item) => {
            const context = `${item.strategy ? sentenceWords(item.strategy) : 'Strategy not recorded'} · ${item.reason || 'No additional rationale'}`;
            return `<button type="button" class="cockpit-row-action" data-cockpit-ticker="${esc(item.ticker)}" data-cockpit-source="${esc(item.source)}">
            <span class="cockpit-item-main">
                <span class="cockpit-item-title"><span class="cockpit-ticker">${esc(item.ticker || '—')}</span>${statusPill(item.direction || item.status)}${priceStateNote(priceState(item))}</span>
                <span class="cockpit-item-sub" title="${esc(context)}">${esc(context)}</span>
            </span>
            <span class="cockpit-item-metric" title="Setup score">${finite(item.score) === null ? 'Unscored' : fmtNumber(item.score, 0)}</span>
        </button>`;
        }).join('');
    }

    function renderRisks(section) {
        return `<div class="cockpit-list" data-cockpit-cap="15" data-cockpit-cap-id="risks">${(section.items || []).map((item) => `<div class="cockpit-list-item cockpit-list-item--lead">
            <span class="cockpit-item-lead">${statusPill(item.severity)}</span>
            <span class="cockpit-item-main">
                <span class="cockpit-item-title">${esc(item.ticker ? `${item.ticker} · ${item.title}` : item.title || item.type)}</span>
                <span class="cockpit-item-sub cockpit-item-sub--full">${esc(item.detail || 'No additional detail')}</span>
                ${item.action ? actionLine(item.action) : ''}
            </span>
        </div>`).join('')}</div>`;
    }

    function renderEarnings(section) {
        const freshness = section.source_freshness || {};
        const schedule = section.schedule_counts || {};
        const refreshPolicy = section.refresh_policy || {};
        const na = (text, title = '') => `<span class="cockpit-data-na"${title ? ` title="${esc(title)}"` : ''}>${esc(text)}</span>`;
        const rows = (section.items || []).map((item, index) => {
            const trace = item.paper_auto_trace || {};
            const prefilter = item.autonomous_prefilter || {};
            const metrics = prefilter.metrics || {};
            const thresholds = prefilter.thresholds || {};
            const policies = prefilter.policies || {};
            const executionBlockers = trace.blockers || [];
            const detailId = `cockpit-earnings-gates-${index}`;
            const expanded = model.expandedEarningsGates.has(String(item.ticker || '').toUpperCase());
            const sourceUrl = safeHttpUrl(item.event_source_url);
            const stages = (trace.stages || []).map((stage) => `<li><span>${esc(stage.name || 'Unknown stage')}</span>${statusPill(stage.status)}</li>`).join('');
            const blockerRows = executionBlockers.length
                ? `<ul class="cockpit-gate-blockers">${executionBlockers.map((blocker) => `<li><code>${esc(blocker.code || 'blocked')}</code><span>${esc(blocker.message || 'Blocked without detail')}</span></li>`).join('')}</ul>`
                : '<p class="cockpit-gate-clear">No visible deterministic/session/readiness blocker. AI, Risk and Capital still run before an order.</p>';
            const session = String(item.session || 'UNKNOWN').toUpperCase();
            const strategyText = item.strategy ? sentenceWords(item.strategy) : '';
            const rangeProxy = item.range_proxy_pct ?? item.expected_move_pct;
            const aiConfidence = finite(item.ai_confidence) === null ? null : finite(item.ai_confidence) * 100;
            const aiMeta = `confidence ${fmtPct(aiConfidence, 0)}${item.ai_model ? ` · ${item.ai_model}` : ''}${item.ai_generated_at ? ` · ${fmtAge((Date.now() - new Date(item.ai_generated_at).getTime()) / 1000)}` : ''}`;
            return `<tr>
            <td><button type="button" class="cockpit-table-link" data-cockpit-earnings="${esc(item.ticker)}">${esc(item.ticker || '—')}</button></td>
            <td>${item.earnings_date ? `<span class="num">${esc(fmtDate(item.earnings_date))}</span>` : na('Date unavailable')}<small>${esc(words(item.schedule_status || 'UNVERIFIED'))}</small></td>
            <td>${session === 'UNKNOWN' ? na('Unknown') : esc(item.session)}</td>
            <td class="num">${finite(item.score) === null ? na('N/A') : `${esc(fmtNumber(item.score, 0))}<small>${esc(words(item.score_kind))}</small>`}</td>
            <td>${item.data_confidence ? esc(sentenceWords(item.data_confidence)) : na('N/A')}<small>coverage ${esc(fmtPct(item.coverage_score, 0))}</small></td>
            <td class="num" title="Observed price-range proxy; not a forecast of the earnings move.">${finite(rangeProxy) === null ? na('N/A') : esc(fmtPct(rangeProxy, 1))}</td>
            <td class="num">${finite(item.historical_move_pct) === null ? na('N/A') : `${esc(fmtPct(item.historical_move_pct, 1))}<small>n=${esc(item.historical_sample_size ?? '—')}</small>`}</td>
            <td class="num">${finite(item.iv_rv_ratio) === null ? na('N/A', 'Not configured: options/IV data source. No implied-volatility evidence is in the current snapshot.') : esc(fmtNumber(item.iv_rv_ratio, 2))}</td>
            <td>${statusPill(item.bias)}<small class="num">${finite(item.bias_score) === null ? 'unscored' : esc(fmtNumber(item.bias_score, 0))}</small></td>
            <td class="cockpit-cell-text" title="${esc(strategyText || 'No eligible strategy')}">${strategyText ? esc(strategyText) : na('No eligible strategy')}</td>
            <td class="cockpit-cell-text">${item.ai_decision ? `<span class="ds-chip ds-chip--ai" title="AI verdict, not a measurement">${esc(sentenceWords(item.ai_decision))}</span><small title="${esc(aiMeta)}">${esc(aiMeta)}</small>` : na('Not reviewed')}</td>
            <td><span class="cockpit-chip-row">${statusPill(item.recommendation_status || item.risk_status)}${statusPill(trace.status || 'UNVERIFIED')}</span><button type="button" class="cockpit-gate-toggle" data-cockpit-gates="${esc(detailId)}" data-cockpit-gate-ticker="${esc(item.ticker)}" aria-expanded="${expanded ? 'true' : 'false'}" aria-controls="${esc(detailId)}">Verify gates${executionBlockers.length ? ` (${esc(executionBlockers.length)})` : ''}</button></td>
        </tr>
        <tr id="${esc(detailId)}" class="cockpit-gate-detail"${expanded ? '' : ' hidden'}><td colspan="12">
            <div class="cockpit-gate-panel">
                <div class="cockpit-gate-summary"><strong>${esc(item.ticker || 'Ticker')} · ${esc(trace.why_no_position || 'An open Paper position exists for this ticker.')}</strong><span>RiskManager thresholds are owner-controlled. Canonical AI WATCH is ${policies.canonical_ai_open_required ? 'a required veto because the owner enabled AI OPEN approval' : 'advisory and does not block entry'}.</span></div>
                <dl class="cockpit-gate-metrics">
                    <div><dt>Gap-risk</dt><dd>${esc(fmtNumber(metrics.gap_risk, 0))} / min ${esc(fmtNumber(thresholds.gap_risk, 0))}</dd></div>
                    <div><dt>|Bias|</dt><dd>${esc(fmtNumber(metrics.directional_bias_abs, 0))} / min ${esc(fmtNumber(thresholds.directional_bias_abs, 0))}</dd></div>
                    <div><dt>Coverage</dt><dd>${esc(fmtNumber(metrics.coverage_score, 0))} / min ${esc(fmtNumber(thresholds.coverage_score, 0))}</dd></div>
                    <div><dt>Execution score</dt><dd>${esc(fmtNumber(metrics.candidate_score, 2))} / min ${esc(fmtNumber(thresholds.candidate_score, 2))}</dd></div>
                </dl>
                <div class="cockpit-gate-columns"><section><h4>Blocking evidence</h4>${blockerRows}</section><section><h4>Position trace</h4><ol class="cockpit-gate-stages">${stages}</ol></section></div>
                <p class="cockpit-gate-source">Event evidence: ${esc(item.event_source || 'source unavailable')} · verified ${esc(fmtDate(item.event_verified_at, true))}${sourceUrl ? ` · <a href="${esc(sourceUrl)}" target="_blank" rel="noopener noreferrer">open source</a>` : ''}</p>
            </div>
        </td></tr>`;
        }).join('');
        const freshnessState = String(freshness.state || 'UNKNOWN').toUpperCase();
        const staleBanner = freshnessState === 'FRESH' ? '' : `<div class="cockpit-stale-note"><span>${freshnessState === 'STALE'
            ? `This Pre-Earnings scan is out of date: last successful refresh ${esc(fmtAge(freshness.age_seconds))} (expected every ${esc(fmtDuration(freshness.refresh_seconds))}). Rows below are from that scan, not from now. Use Refresh to start a new one.`
            : 'The age of this Pre-Earnings scan is unknown, so the rows below may be out of date.'}</span></div>`;
        const watchlistRows = Array.isArray(section.watchlist_earnings) ? section.watchlist_earnings : [];
        const watchlistNote = watchlistRows.length
            ? `<p class="cockpit-card-note">Watchlist earnings within 7 days (listed first, same events as Current risks): ${watchlistRows.map((row) => `<strong>${esc(row.ticker)}</strong> in ${esc(row.days)}d${row.session ? ` (${esc(row.session)})` : ''}${row.in_scan === false ? ' — not in this scan' : ''}`).join(' · ')}.</p>`
            : '';
        return `${staleBanner}${watchlistNote}<p class="cockpit-card-note"><span class="cockpit-metric-origin ds-chip">Deterministic evidence</span> and <span class="cockpit-metric-origin ds-chip ds-chip--ai is-ai">AI review</span> remain visibly separate. <strong>Eligible means recommendation-eligible only; it is not an order approval.</strong> IV/RV is N/A when options evidence is absent.</p>
            <p class="cockpit-card-note">Pre-Earnings source: ${esc(titleWords(freshness.state || 'UNKNOWN'))} · updated ${esc(fmtDate(freshness.updated_at, true))} (${esc(fmtAge(freshness.age_seconds))}) · calendar evidence ${esc(fmtAge(freshness.calendar_age_seconds))} · news evidence ${esc(fmtAge(freshness.news_age_seconds))}.</p>
            <div class="cockpit-table-wrap cockpit-table-wrap--tall"><table class="cockpit-table cockpit-matrix" data-testid="cockpit-earnings-table">
                <thead><tr><th>Ticker</th><th>Earnings date</th><th>BMO / AMC</th><th>Score</th><th>Data confidence</th><th title="Observed price-range proxy; not a forecast of the earnings move.">Range proxy</th><th>Historical move</th><th>IV / RV</th><th>Bias</th><th>Strategy</th><th>AI decision</th><th>Recommendation / Paper Auto</th></tr></thead>
                <tbody>${rows}</tbody>
            </table></div>
            <details class="cockpit-method"><summary>Scan coverage and refresh policy</summary>
                <p class="cockpit-card-note">${esc(schedule.light_scanned ?? schedule.calendar_reporters ?? 0)} calendar reporters light-scanned · ${esc(schedule.deep_scored ?? schedule.analyzed_candidates ?? 0)} deep-scored · ${esc(schedule.ai_reviewed ?? 0)} carry current AI evidence · ${esc(schedule.reported_today ?? 0)} moved to reported history. The table shows the first ${esc(schedule.displayed_candidates ?? section.count ?? 0)} active candidates; estimated or missing dates are never presented as confirmed reports.</p>
                <p class="cockpit-card-note">Refresh policy: full evidence cycle every ${esc(fmtDuration(refreshPolicy.full_refresh_seconds))}${refreshPolicy.continuous_scan_enabled ? ` · targeted scan every ${esc(fmtDuration(refreshPolicy.continuous_scan_seconds))}` : ' · targeted scan disabled'} · AI is bounded to ${esc(refreshPolicy.ai_max_tickers_per_cycle ?? 'configured')} ticker(s) per full cycle${refreshPolicy.news_change_reanalysis ? ' and is re-run when the news fingerprint changes' : ''}. Not every calendar reporter invokes AI on every targeted scan.</p>
            </details>`;
    }

    // The narrative is "current" only while its own timestamp is recent. An old
    // one is labelled as the last recorded tone with its real age.
    const sentimentIsStale = (item) => {
        const flagged = String((item && item.freshness) || '').toUpperCase();
        if (flagged === 'STALE') return true;
        const age = ageSeconds(item && item.generated_at);
        const limit = finite(item && item.stale_after_seconds) || 10800;
        return age === null ? flagged !== 'FRESH' : age > limit;
    };

    function renderSentiment(section) {
        const item = (section.items || [])[0];
        if (!item) return '';
        const stale = sentimentIsStale(item);
        const shiftNote = item.shift && item.prior_tone
            ? `Shifted from ${titleWords(item.prior_tone)}`
            : 'No earlier snapshot to compare, so no shift is reported';
        const catalysts = (item.top_catalysts || []).slice(0, 4);
        return `${stale ? `<div class="cockpit-stale-note"><span>This narrative was generated ${esc(fmtAgeOf(item.generated_at))} and has not been refreshed since. It is the last recorded tone, not today's.</span></div>` : ''}
        <div class="cockpit-list">
            <div class="cockpit-list-item"><span class="cockpit-item-main"><span class="cockpit-item-title">${stale ? 'Last recorded tone' : 'Current tone'} · ${esc(titleWords(item.current_tone))}</span><span class="cockpit-item-sub cockpit-item-sub--full">Risk regime ${esc(titleWords(item.risk_regime))} · generated ${esc(fmtDate(item.generated_at, true))} (${esc(fmtAgeOf(item.generated_at))}) · ${esc(shiftNote)}</span></span>${statusPill(stale ? 'STALE' : 'FRESH')}</div>
        </div>
        ${catalysts.length ? `<div class="cockpit-sublabel ds-label">${stale ? 'Catalysts at that time' : 'Catalysts'}</div><ul class="cockpit-bullets">${catalysts.map((value) => `<li>${esc(value)}</li>`).join('')}</ul>` : ''}`;
    }

    function renderNews(section) {
        const windowHours = finite(section.window_hours);
        const droppedStale = finite(section.dropped_stale) || 0;
        const droppedDup = finite(section.dropped_duplicates) || 0;
        const note = windowHours === null ? '' : `<p class="cockpit-card-note">High and critical impact items published in the last ${esc(windowHours)} hours${droppedStale ? ` · ${esc(droppedStale)} older item(s) withheld` : ''}${droppedDup ? ` · ${esc(droppedDup)} repeat(s) removed` : ''}.</p>`;
        return `${note}<div class="cockpit-list" data-cockpit-cap="6" data-cockpit-cap-id="news">${(section.items || []).map((item) => {
            const url = safeHttpUrl(item.url);
            const title = esc(item.title || 'Untitled');
            return `<div class="cockpit-list-item cockpit-list-item--lead">
            <span class="cockpit-item-lead">${statusPill(item.impact)}</span>
            <span class="cockpit-item-main">
                <span class="cockpit-item-title"><span class="cockpit-ticker">${esc(item.ticker || 'Market')}</span>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${title}</a>` : `<span>${title}</span>`}</span>
                <span class="cockpit-item-sub">${esc(item.source || 'Unknown source')} · ${esc(fmtAgeOf(item.published_at))} (${esc(fmtDate(item.published_at, true))}) · sentiment ${esc(item.sentiment || 'not measured')}</span>
            </span>
        </div>`;
        }).join('')}</div>`;
    }

    // "PEPE-USD · decision rejected — WATCH (rejected)" → plain operator wording.
    const decisionHeadline = (item) => {
        const action = String(item.action || String(item.what_happened || '').split(' (', 1)[0] || '').toUpperCase();
        const status = String(item.status || '').toLowerCase();
        const side = action.includes('SHORT') || action === 'SELL' ? 'short' : 'long';
        // Two different facts: status = what happened to the entry, action =
        // the AI's verdict. "WATCH (rejected)" read as if the WATCH had been
        // rejected; it means the entry was declined and the AI said WATCH.
        if (action === 'NO_TRADE' || action === 'WATCH') {
            const fate = status === 'rejected' ? 'Entry declined' : 'No entry';
            return `${fate} · AI verdict: ${action.replace(/_/g, ' ')}`;
        }
        if (action.startsWith('OPEN_') || ['BUY', 'SELL', 'LONG', 'SHORT'].includes(action)) {
            if (status === 'executed') return `Opened ${side} · position open`;
            if (status === 'closed') return `Opened ${side} · position since closed`;
            if (status === 'accepted') return `${titleWords(side)} entry approved · no position recorded`;
            return `${titleWords(side)} entry rejected`;
        }
        return titleWords(action || item.event_type || 'decision');
    };
    const decisionStatusLabel = (status) => {
        const normalized = String(status || '').toLowerCase();
        if (normalized === 'executed') return 'OPEN';
        if (normalized === 'accepted') return 'APPROVED';
        return String(status || 'UNAVAILABLE').toUpperCase();
    };

    function renderDecisionsCompact(section) {
        return `<div class="cockpit-list">${(section.items || []).slice(0, 6).map((item) => `<button type="button" class="cockpit-row-action" data-cockpit-tab-open="ledger">
            <span class="cockpit-item-main"><span class="cockpit-item-title">${esc(item.ticker || 'SYSTEM')} · ${esc(decisionHeadline(item))}</span><span class="cockpit-item-sub">${esc(fmtAgeOf(item.timestamp))} · ${esc(fmtDate(item.timestamp, true))}</span></span>${statusPill(decisionStatusLabel(item.status))}
        </button>`).join('')}</div>`;
    }

    function renderPositions(section) {
        const sortedItems = [...(section.items || [])].sort((a, b) => {
            const pnlA = Number.isFinite(Number(a.unrealized_pnl)) ? Number(a.unrealized_pnl) : -Infinity;
            const pnlB = Number.isFinite(Number(b.unrealized_pnl)) ? Number(b.unrealized_pnl) : -Infinity;
            if (pnlB !== pnlA) return pnlB - pnlA;
            const pctA = Number.isFinite(Number(a.unrealized_pnl_pct)) ? Number(a.unrealized_pnl_pct) : -Infinity;
            const pctB = Number.isFinite(Number(b.unrealized_pnl_pct)) ? Number(b.unrealized_pnl_pct) : -Infinity;
            return pctB - pctA;
        });
        // The header is the first child of the capped list (it stays pinned
        // while the rows scroll), so the cap is 15 rows + 1 header.
        return `<div class="cockpit-list cockpit-pos-table" data-cockpit-cap="16" data-cockpit-cap-id="positions">
            <div class="cockpit-pos-row cockpit-pos-head"><span>Ticker</span><span>Side</span><span class="num">Entry</span><span class="num">Current</span><span class="num">P/L $</span><span class="num">P/L %</span></div>${sortedItems.map((item) => {
            // No data / delisted: no current price, and the P/L is not a move.
            const state = priceState(item);
            const pnlTone = (state || finite(item.unrealized_pnl) === null) ? 'flat' : (item.unrealized_pnl >= 0 ? 'up' : 'down');
            const noPrice = state && state.kind === 'no_data';
            return `<button type="button" class="cockpit-row-action cockpit-pos-row${state ? ' has-price-state' : ''}" data-cockpit-route="capital" title="${esc(item.strategy || 'Strategy unavailable')} · open in Capital">
                <span class="cockpit-ticker">${esc(item.ticker || '—')}</span>
                <span>${statusPill(item.direction)}</span>
                <span class="num">${esc(fmtNumber(item.entry_price, 2))}</span>
                <span class="num">${noPrice ? '—' : esc(fmtNumber(item.current_price, 2))}</span>
                <span class="num cockpit-pnl is-${pnlTone}">${noPrice ? '—' : esc(fmtSignedMoney(item.unrealized_pnl))}</span>
                <span class="num cockpit-pnl is-${pnlTone}">${noPrice ? '—' : esc(fmtPct(item.unrealized_pnl_pct, 2, true))}</span>
                ${priceStateNote(state, 'cockpit-pos-state')}
            </button>`;
        }).join('')}</div>`;
    }

    function renderOverview(payload) {
        const sections = payload.sections || {};
        // The health checks loaded fine even when their RESULT is "unhealthy":
        // treating that as a section ERROR replaced the list with "This verified
        // source is unavailable" exactly when the owner needed to see what failed.
        const healthData = payload.system_health || {};
        const healthServices = healthData.services || [];
        const healthFailing = Array.isArray(healthData.failing) ? healthData.failing : [];
        const healthSection = {
            state: healthData.overall ? 'SUCCESS' : 'EMPTY',
            count: healthServices.length,
            items: healthServices,
            message: 'No health check result is available.',
        };
        const healthLabels = healthFailing.length
            ? { text: `${healthFailing.length} of ${healthServices.length} need attention`, tone: healthData.overall === 'UNHEALTHY' ? 'bad' : 'attention' }
            : { text: `${healthServices.length} checks pass or are optional` };
        // Failing checks first (with what to do), then the rest.
        const failingIds = new Set(healthFailing.map((item) => item.id));
        const healthRows = [
            ...healthServices.filter((item) => failingIds.has(item.id)),
            ...healthServices.filter((item) => !failingIds.has(item.id)),
        ].slice(0, Math.max(5, healthFailing.length));
        const healthPreview = `<div class="cockpit-list">${healthRows.map((item) => `<button type="button" class="cockpit-row-action" data-cockpit-tab-open="health"><span class="cockpit-item-main"><span class="cockpit-item-title">${esc(item.label)}</span><span class="cockpit-item-sub">${esc(item.message)}</span>${item.action ? actionLine(item.action) : ''}</span>${statusPill(item.status)}</button>`).join('')}</div>`;
        const research = sections.research_activity || { state: 'EMPTY', activity: [] };
        const researchHtml = `<div class="cockpit-list">${(research.activity || []).slice(0, 5).map((item) => `<button type="button" class="cockpit-row-action" data-cockpit-tab-open="research"><span class="cockpit-item-main"><span class="cockpit-item-title">${esc(item.description || item.hypothesis || 'Unlabelled observation')}</span><span class="cockpit-item-sub">${esc(item.strategy || item.experiment || 'Observation')} · ${esc(fmtAgeOf(item.last_run))} · ${esc(fmtDate(item.last_run, true))}</span></span>${statusPill(item.status)}</button>`).join('')}</div>`;
        const degradation = sections.strategy_degradation || { state: 'EMPTY', items: [] };
        const degradationHtml = `<div class="cockpit-list">${(degradation.items || []).map((item) => `<button type="button" class="cockpit-row-action" data-cockpit-tab-open="strategy"><span class="cockpit-item-main"><span class="cockpit-item-title">${esc(item.strategy_id)}</span><span class="cockpit-item-sub">${esc(item.description || 'No description')}</span></span>${statusPill(item.status)}</button>`).join('')}</div>`;
        const earnings = sections.upcoming_earnings || { state: 'EMPTY', items: [] };
        const earningsShown = (earnings.items || []).length;
        const earningsTotal = finite(earnings.count) ?? earningsShown;
        const earningsStale = String((earnings.source_freshness || {}).state || '').toUpperCase() !== 'FRESH';
        const earningsLabels = {
            text: `${earningsShown} of ${earningsTotal.toLocaleString()} shown${earningsStale ? ' · stale scan' : ''}`,
            empty: earningsStale ? 'Scan out of date' : 'No upcoming events',
            tone: earningsStale ? 'attention' : '',
        };
        const sentimentItem = ((sections.sentiment_shifts || {}).items || [])[0];
        const sentimentStale = sentimentItem ? sentimentIsStale(sentimentItem) : false;
        const sentimentLabels = { text: sentimentStale ? 'Stale' : 'Current', empty: 'Not available', tone: sentimentStale ? 'attention' : '' };
        const decisions = sections.ai_decisions || {};
        const decisionsShown = Math.min(6, (decisions.items || []).length);
        const decisionsTotal = finite(decisions.total_entries) ?? finite(decisions.count) ?? decisionsShown;
        const decisionLabels = { text: `latest ${decisionsShown} of ${decisionsTotal.toLocaleString()}`, empty: 'None recorded' };
        return `<div class="cockpit-overview-grid">
            ${card('Best current opportunities', sections.opportunities || {}, `<div class="cockpit-list" data-cockpit-cap="15" data-cockpit-cap-id="opportunities">${renderOpportunityList(sections.opportunities || {})}</div>`, routeButton('Open Trade Radar', 'traderadar'), { one: 'setup', many: 'setups', empty: 'None right now' })}
            ${card('Current risks', sections.risks || {}, renderRisks(sections.risks || {}), routeButton('Open Risk controls', 'agents'), { one: 'risk', many: 'risks', empty: 'None reported', tone: 'attention' })}
            ${card('Active positions', sections.active_positions || {}, renderPositions(sections.active_positions || {}), routeButton('Open Capital', 'capital'), { one: 'open position', many: 'open positions', empty: 'None open' })}
            <article class="cockpit-card cockpit-card--full">
                <div class="cockpit-card-head"><div class="cockpit-card-title">Pre-Earnings decision matrix</div>${statePill(earnings, earningsLabels)}</div>
                <div class="cockpit-card-body">${sectionBody(earnings, renderEarnings(earnings))}</div>
                <div class="cockpit-card-actions">${routeButton('Open complete Pre-Earnings evidence', 'preearnings')}</div>
            </article>
            ${card('Sentiment shifts', sections.sentiment_shifts || {}, renderSentiment(sections.sentiment_shifts || {}), routeButton('Open News', 'news'), sentimentLabels, { ai: true })}
            ${card('Major news', sections.major_news || {}, renderNews(sections.major_news || {}), routeButton('Open News Cockpit', 'news'), { one: 'current item', many: 'current items', empty: 'None in window' })}
            ${card('AI decisions', sections.ai_decisions || {}, renderDecisionsCompact(sections.ai_decisions || {}), '<button type="button" class="cockpit-link-btn" data-cockpit-tab-open="ledger">Open ledger <i class="fa-solid fa-arrow-right"></i></button>', decisionLabels, { ai: true })}
            ${card('Strategy degradation', degradation, degradationHtml, '<button type="button" class="cockpit-link-btn" data-cockpit-tab-open="strategy">Open Strategy Lab <i class="fa-solid fa-arrow-right"></i></button>', { one: 'degraded strategy', many: 'degraded strategies', empty: 'None degraded', tone: 'attention' })}
            ${card('Research activity', research, researchHtml, '<button type="button" class="cockpit-link-btn" data-cockpit-tab-open="research">Open Research Factory <i class="fa-solid fa-arrow-right"></i></button>', { text: `${(research.activity || []).length} recent`, empty: 'No activity' })}
            ${card('Provider / system health', healthSection, healthPreview, '<button type="button" class="cockpit-link-btn" data-cockpit-tab-open="health">Open all checks <i class="fa-solid fa-arrow-right"></i></button>', healthLabels)}
        </div>`;
    }

    // ---- Autonomous Strategy Lab (core/strategy_lab): search -> backtest -> research lead -> paper -> retire.
    const LAB_STATUS_LABEL = { paper: 'TRADING ON PAPER', explore: 'EXPLORATION', watch: 'WATCHING', rejected: 'REJECTED', retired: 'RETIRED' };
    const labStat = (part) => (part && part.ok
        ? `Sharpe ${esc(fmtNumber(part.sharpe, 2))} · α-t ${esc(fmtNumber(part.alpha_t, 2))}`
        : '—');
    function renderAutoLab(state) {
        if (!state) {
            return `<article class="cockpit-card cockpit-card--full"><div class="cockpit-card-head"><div class="cockpit-card-title">Autonomous lab</div></div><div class="cockpit-card-body"><div class="cockpit-empty"><span>Loading the lab's state…</span></div></div></article>`;
        }
        if (state.error) {
            return `<article class="cockpit-card cockpit-card--full"><div class="cockpit-card-head"><div class="cockpit-card-title">Autonomous lab</div>${statusPill('UNAVAILABLE')}</div><div class="cockpit-card-body"><p class="cockpit-card-note">${esc(state.error)}</p></div></article>`;
        }
        const runner = state.runner || {};
        const running = Boolean(runner.run_in_progress);
        const lastResult = runner.last_result || {};
        const runnerLine = running
            ? 'A run is in progress (it takes several minutes).'
            : `Last run ${state.last_run_at ? esc(fmtDate(state.last_run_at, true)) : 'never'} · next daily run after ${esc(runner.daily_run_after_utc || '00:15')} UTC${lastResult.ok === false ? ` · last attempt failed: ${esc(String(lastResult.detail || '').slice(0, 160))}` : ''}`;
        // The newest run record of each market: a failed refresh, a failed market or a missing review must be visible.
        const latestRun = {};
        (state.runs || []).forEach((run) => { if (run && run.market && !latestRun[run.market]) latestRun[run.market] = run; });
        const runProblems = (market) => {
            const run = latestRun[market] || {};
            const problems = [];
            if (run.error) problems.push(`the run failed: ${run.error}`);
            const refresh = run.data_refresh || null;
            if (refresh && refresh.ok === false) problems.push(`the price data did not arrive whole: ${refresh.reason || 'no reason given'}`);
            if (run.incomplete_data) problems.push('the newest day had bars for only part of the instruments; no new basket was ranked');
            const review = run.review || null;
            if (review && review.available === false && review.error) problems.push(`the research lead did not answer: ${review.error}`);
            if (run.passed_today_only) problems.push(`${run.passed_today_only} rule(s) passed only on the latest data and were not treated as findings`);
            return problems;
        };
        const blockedByRule = {};
        ((state.live && state.live.trading_rules) || []).forEach((rule) => { blockedByRule[rule.id] = rule.not_entered || {}; });
        const marketCards = Object.entries(state.markets || {}).map(([market, board]) => {
            const rows = (board.leaderboard || []).slice(0, 8).map((item, index) => `<tr>
                <td class="num">${index + 1}</td><td>${esc(String(item.description || '').replace(/^\w+: /, ''))}</td>
                <td>${item.passes ? statusPill('PASSED') : statusPill('BELOW THE BAR')}</td>
                <td>${labStat(item.train)}</td><td>${labStat(item.holdout)}</td>
                <td class="num">${esc(fmtPct(item.full && item.full.total_pct, 1, true))}</td>
                <td class="num">${esc(fmtPct(item.full && item.full.max_drawdown_pct, 1))}</td>
                <td>${esc(item.origin || 'search')}</td></tr>`).join('');
            const table = rows
                ? `<div class="cockpit-table-wrap"><table class="cockpit-table"><thead><tr><th class="num">#</th><th>Rule</th><th>Bar</th><th>First 60% of history</th><th>Last 40%</th><th class="num">Whole history</th><th class="num">Worst drawdown</th><th>Proposed by</th></tr></thead><tbody>${rows}</tbody></table></div>`
                : '<div class="cockpit-empty"><span>No run has finished for this market yet.</span></div>';
            const commentary = board.commentary
                ? `<p class="cockpit-card-note"><strong>Research lead${board.review_model ? ` (${esc(board.review_model)})` : ''}:</strong> ${esc(board.commentary)}</p>`
                : `<p class="cockpit-card-note">No commentary from the research lead yet${board.review_error ? `: ${esc(String(board.review_error).slice(0, 200))}` : '.'}</p>`;
            return `<article class="cockpit-card cockpit-card--full"><div class="cockpit-card-head"><div class="cockpit-card-title">${esc(titleWords(market))} · ${esc(board.rules_tested ?? 0)} rules tested, ${esc(board.passed ?? 0)} passed the bar</div>${statusPill(board.trading ? `${board.trading} TRADING` : 'NONE TRADING')}</div><div class="cockpit-card-body">
                <p class="cockpit-card-note">${esc(board.data || '')}${board.proposals_for_next_run ? ` · ${esc(board.proposals_for_next_run)} rule(s) proposed by the research lead wait for the next run` : ''}</p>
                ${runProblems(market).map((problem) => `<p class="cockpit-card-note"><i class="fa-solid fa-triangle-exclamation cockpit-tone--bad"></i> ${esc(problem)}</p>`).join('')}
                ${commentary}${table}</div></article>`;
        }).join('');
        const strategyRows = (state.strategies || []).map((item) => {
            const review = item.review || {};
            const live = item.live || {};
            const results = item.paper_results || null;
            const blocked = Object.entries(blockedByRule[item.id] || {}).map(([symbol, why]) => `${symbol} (${why})`);
            const members = live.not_entered
                ? `<small class="cockpit-data-na">not entered: ${esc(live.not_entered)}</small>`
                : (live.long || live.short)
                    ? `<small class="cockpit-data-na">long ${esc((live.long || []).join(', ') || '—')} · short ${esc((live.short || []).join(', ') || '—')} · out by ${esc(live.exit_at ? fmtDate(live.exit_at, true) : '—')}${blocked.length ? ` · could not be entered: ${esc(blocked.join(', '))}` : ''}</small>`
                    : '';
            const paper = results
                ? `${esc(results.closed)} closed${results.closed ? `, ${esc(fmtPct(results.mean_pct, 2, true))} avg, ${esc(fmtNumber(results.win_rate_pct, 0))}% wins` : ''} · ${esc(results.open)} open`
                : 'no trades yet';
            return `<tr><td>${statusPill(LAB_STATUS_LABEL[item.status] || item.status)}</td><td>${esc(titleWords(item.market || ''))}</td>
                <td>${esc(String(item.description || '').replace(/^\w+: /, ''))}${members}</td>
                <td>${labStat(item.stats && item.stats.train)}<small class="cockpit-data-na">last 40%: ${labStat(item.stats && item.stats.holdout)}</small></td>
                <td>${review.verdict ? `${esc(titleWords(review.verdict))}${review.confidence != null ? ` (${esc(fmtNumber(Number(review.confidence) * 100, 0))}%)` : ''}` : '—'}<small class="cockpit-data-na">${esc(String(review.reason || '').slice(0, 220))}</small></td>
                <td>${esc(item.decision || '')}</td><td>${paper}</td></tr>`;
        }).join('');
        const strategies = strategyRows
            ? `<div class="cockpit-table-wrap"><table class="cockpit-table"><thead><tr><th>Status</th><th>Market</th><th>Rule</th><th>Evidence</th><th>Research lead</th><th>Decision</th><th>Paper results</th></tr></thead><tbody>${strategyRows}</tbody></table></div>`
            : '<div class="cockpit-empty"><span>No rule has reached the research lead yet: nothing passed the statistical bar and no near miss was put forward.</span></div>';
        return `<article class="cockpit-card cockpit-card--full"><div class="cockpit-card-head"><div class="cockpit-card-title">Autonomous lab · search, test, review, paper-trade, retire</div><button type="button" class="ds-btn ds-btn--ghost" data-cockpit-lab-run${running ? ' disabled' : ''}><i class="fa-solid fa-flask"></i> ${running ? 'Running…' : 'Run now'}</button></div><div class="cockpit-card-body">
                <p class="cockpit-card-note">${esc(state.how_it_decides || '')}</p>
                <p class="cockpit-card-note">${runnerLine}</p>${strategies}</div></article>${marketCards}`;
    }

    function renderLearning(state) {
        const title = 'Learning mode · Opus trades movers and news on paper';
        const card = (body, pill = '') => `<article class="cockpit-card cockpit-card--full"><div class="cockpit-card-head"><div class="cockpit-card-title">${title}</div>${pill}</div><div class="cockpit-card-body">${body}</div></article>`;
        if (!state) return card('<div class="cockpit-empty"><span>Loading learning mode…</span></div>');
        if (state.status === 'not_started' || state.status === 'unreadable' || state.error) {
            return card(`<div class="cockpit-empty"><span>${esc(state.reason || state.error || 'No progress yet.')}</span></div>`);
        }
        const p = state.progress || {};
        const count = (value) => Number(value || 0).toLocaleString('en-US');
        const money = (value) => (value == null ? '—' : `${Number(value) >= 0 ? '+' : ''}${Number(value).toFixed(2)} $`);
        const rate = (value) => (value == null ? '—' : `${Number(value).toFixed(1)}%`);
        const target = Number(p.target || 10000);
        const share = Math.min(100, (100 * Number(p.closed || 0)) / (target || 1));
        const notes = [];
        if (state.paused_reason) notes.push(`<p class="cockpit-card-note">Paused: ${esc(state.paused_reason)}</p>`);
        if (state.stale) notes.push('<p class="cockpit-card-note">The worker has not reported for more than 10 minutes.</p>');
        const groupRows = (groups, labels) => Object.keys(labels).map((key) => {
            const g = (groups || {})[key] || {};
            return `<tr><td>${esc(labels[key])}</td><td class="num">${count(g.closed)}</td><td class="num">${esc(money(g.net_pnl))}</td><td class="num">${esc(rate(g.win_rate_pct))}</td></tr>`;
        }).join('');
        const table = (head, rows) => `<div class="cockpit-table-wrap"><table class="cockpit-table"><thead><tr><th>${esc(head)}</th><th class="num">Closed</th><th class="num">Net</th><th class="num">Win rate</th></tr></thead><tbody>${rows}</tbody></table></div>`;
        const recent = (p.recent || []).slice(0, 10).map((r) => `<tr><td>${esc(r.ticker)}</td><td>${esc(r.side)}</td><td>${esc(r.source)}</td><td class="num">${esc(r.confidence == null ? '—' : Number(r.confidence).toFixed(2))}</td><td class="num">${esc(money(r.net_pnl))}</td><td>${esc(r.close_reason || '')}</td><td class="cockpit-cell-date">${esc(String(r.closed_at || '').slice(0, 16).replace('T', ' '))}</td></tr>`).join('');
        const recentTable = recent
            ? `<div class="cockpit-table-wrap"><table class="cockpit-table"><thead><tr><th>Ticker</th><th>Side</th><th>From</th><th class="num">Confidence</th><th class="num">Net</th><th>Exit</th><th>Closed</th></tr></thead><tbody>${recent}</tbody></table></div>`
            : '<div class="cockpit-empty"><span>No learning trade has closed yet.</span></div>';
        const body = `${notes.join('')}
            <div class="cockpit-summary-grid ds-stats">
                <div class="cockpit-summary-box ds-stat"><span class="ds-label">Closed</span><strong>${count(p.closed)} of ${count(target)}</strong><small>${share.toFixed(1)}% · checkpoint ${esc(p.checkpoint || 0)} (every ${count(p.checkpoint_every || 1000)})</small></div>
                <div class="cockpit-summary-box ds-stat"><span class="ds-label">Today</span><strong>${count(state.opened_today)} of ${count(state.daily_cap)}</strong><small>${count(p.open)} open · ${count(p.pending_ideas)} waiting</small></div>
                <div class="cockpit-summary-box ds-stat"><span class="ds-label">Net result</span><strong>${esc(money(p.net_pnl))}</strong><small>win rate ${esc(rate(p.win_rate_pct))}</small></div>
            </div>
            <p class="cockpit-card-note">Opus chooses the side of scanner movers and trades fresh high-impact news; every trade is 1% of equity and leaves by its stop, its target or its hold time. These are paper trades to learn from, not a proven edge.</p>
            ${table('Source', groupRows(p.by_source, { mover: 'Movers', news: 'News' }))}
            ${table('Side', groupRows(p.by_side, { LONG: 'Long', SHORT: 'Short' }))}
            ${table('Opus confidence', groupRows(p.by_confidence, { '0.55-0.65': '0.55-0.65', '0.65-0.75': '0.65-0.75', '0.75+': '0.75+' }))}
            ${recentTable}`;
        return card(body);
    }

    async function loadLearning(force = false) {
        const now = Date.now();
        if (!force && model.learningLoadedAt && now - model.learningLoadedAt < 60000) return;
        model.learningLoadedAt = now;
        try {
            const response = await fetch('/api/learning/state');
            model.learning = response.ok ? await response.json() : { error: `Learning mode could not be read (${response.status}).` };
        } catch (_) {
            model.learning = { error: 'Learning mode could not be read.' };
        }
        const host = byId('cockpit-learning');
        if (host) host.innerHTML = renderLearning(model.learning);
    }

    async function loadAutoLab(force = false) {
        const now = Date.now();
        if (!force && model.autoLabLoadedAt && now - model.autoLabLoadedAt < 60000) return;
        model.autoLabLoadedAt = now;
        try {
            const response = await fetch('/api/strategy-lab/state');
            model.autoLab = response.ok ? await response.json() : { error: `The lab's state could not be read (${response.status}).` };
        } catch (_) {
            model.autoLab = { error: "The lab's state could not be read." };
        }
        const host = byId('cockpit-autolab');
        if (host) host.innerHTML = renderAutoLab(model.autoLab);
    }

    function renderStrategy(payload) {
        const lab = payload.strategy_lab || {};
        const lifecycleCounts = [
            ['Champion', (lab.champions || []).length],
            ['Challengers', (lab.challengers || []).length],
            ['Shadow', (lab.shadow || []).length],
            ['Degraded', (lab.degraded || []).length],
        ];
        const rows = (lab.leaderboard || []).map((item, index) => `<tr>
            <td class="num">${index + 1}</td><td>${esc(item.strategy_id || '—')}</td><td>${esc(item.symbol || '—')}</td><td>${item.direction ? statusPill(item.direction) : '—'}</td>
            <td>${statusPill(item.validation_status)}</td><td class="num">${esc(item.sample_size ?? '—')}${item.sample_sufficient === false ? '<small class="cockpit-data-na">too few trades</small>' : ''}</td><td class="num">${esc(fmtNumber(item.expectancy_r, 3))}</td>
            <td class="num">${esc(fmtNumber(item.profit_factor, 2))}</td><td class="num">${esc(fmtPct(item.max_drawdown_pct, 2))}</td><td class="cockpit-cell-date">${esc(fmtDate(item.last_run, true))}</td>
        </tr>`).join('');
        const leaderboard = rows ? `<div class="cockpit-table-wrap"><table class="cockpit-table"><thead><tr><th class="num">Rank</th><th>Strategy</th><th>Symbol</th><th>Direction</th><th>OOS / validation</th><th class="num">Sample</th><th class="num">Expectancy R</th><th class="num">Profit factor</th><th class="num">Drawdown</th><th>Last validated/run</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="cockpit-empty"><span>No strategy performance evidence is available.</span></div>';
        const history = (lab.promotion_history || []).length
            ? `<div class="cockpit-list">${lab.promotion_history.map((item) => `<div class="cockpit-list-item"><span class="cockpit-item-main"><span class="cockpit-item-title">${esc(item.strategy_id)} · ${esc(item.action)}</span><span class="cockpit-item-sub">${esc(fmtDate(item.occurred_at, true))}</span></span>${statusPill(item.to_status)}</div>`).join('')}</div>`
            : '<div class="cockpit-empty"><span>No promotion history exists.</span></div>';
        const rollbackRows = (lab.rollback_history || []).map((item) => `<div class="cockpit-list-item"><span class="cockpit-item-main"><span class="cockpit-item-title">${esc(item.strategy_id || '—')} v${esc(item.version || '—')} · ${esc(words(item.action))}</span><span class="cockpit-item-sub">${esc(fmtDate(item.at, true))}${item.detail && item.detail.restored_version ? ` · restored v${esc(item.detail.restored_version)}` : ''}${item.detail && item.detail.reason ? ` · ${esc(String(item.detail.reason).slice(0, 90))}` : ''}</span></span>${statusPill('ROLLED_BACK')}</div>`).join('');
        const rollbacks = rollbackRows
            ? `<div class="cockpit-list">${rollbackRows}</div>`
            : '<div class="cockpit-empty"><span>No rollback has been recorded.</span></div>';
        // `robustness` carries the whole walk-forward payload object — render a
        // status label, not String(object) ("[OBJECT OBJECT]" pill).
        const robustnessLabel = (rob) => {
            if (!rob) return 'UNAVAILABLE';
            if (typeof rob !== 'object') return String(rob);
            if (String(rob.status || '').toLowerCase() === 'error') return 'WF ERROR';
            return 'MEASURED';
        };
        const lifecycleRows = (lab.records || []).map((record) => `<tr>
            <td>${esc(record.strategy_id || '—')}</td><td>${esc(record.version || '—')}</td>
            <td>${statusPill(record.status)}</td>
            <td>${statusPill(record.oos_verdict || 'UNAVAILABLE')}</td>
            <td>${statusPill(robustnessLabel(record.robustness))}</td>
            <td class="cockpit-cell-date">${esc(record.last_validated ? fmtDate(record.last_validated, true) : '—')}</td>
            <td>${statusPill(record.drift || 'NOT_MEASURED')}</td>
        </tr>`).join('');
        const lifecycleTable = lifecycleRows
            ? `<div class="cockpit-table-wrap"><table class="cockpit-table"><thead><tr><th>Strategy</th><th>Version</th><th>Lifecycle</th><th>OOS verdict</th><th>Robustness</th><th>Last validated</th><th>Drift</th></tr></thead><tbody>${lifecycleRows}</tbody></table></div>`
            : '<div class="cockpit-empty"><span>No registry lifecycle records exist.</span></div>';
        const aiCommentary = lab.ai_quant_commentary || { state: 'EMPTY' };
        return `<div class="cockpit-workspace-head"><div><h2>Strategy Lab</h2><p>Registry lifecycle and deterministic performance evidence. Unverified rows are never presented as a promoted strategy.</p></div>${routeButton('Open Backtest', 'backtest', 'fa-arrow-up-right-from-square')}</div>
            <div class="cockpit-overview-grid" id="cockpit-learning">${renderLearning(model.learning)}</div>
            <div class="cockpit-overview-grid" id="cockpit-autolab">${renderAutoLab(model.autoLab)}</div>
            <div class="cockpit-workspace-head"><div><h2>Legacy registry</h2><p>The older S1–S8 registry and its validation caches. None of these has produced a validated strategy.</p></div></div>
            <div class="cockpit-summary-grid ds-stats">${lifecycleCounts.map(([label, count]) => `<div class="cockpit-summary-box ds-stat"><span class="ds-label">${esc(label)}</span><strong>${count}</strong></div>`).join('')}</div>
            <div class="cockpit-overview-grid">
                <article class="cockpit-card cockpit-card--full"><div class="cockpit-card-head"><div class="cockpit-card-title">Strategy leaderboard</div>${statePill({ state: lab.state, count: (lab.leaderboard || []).length }, { one: 'ranked row', many: 'ranked rows', empty: 'No evidence' })}</div><div class="cockpit-card-body"><p class="cockpit-card-note"><span class="cockpit-metric-origin ds-chip">Deterministic metrics</span> Rankings retain their recorded validation label; sample size, OOS, walk-forward, robustness and drawdown are not inferred. Rows with fewer than ${esc(lab.leaderboard_min_sample ?? 10)} closed trades are listed after the others and marked “too few trades”: one lucky trade is not evidence of an edge.</p>${leaderboard}</div></article>
                <article class="cockpit-card cockpit-card--full"><div class="cockpit-card-head"><div class="cockpit-card-title">Lifecycle · OOS · robustness · drift</div></div><div class="cockpit-card-body">${lifecycleTable}</div></article>
                <article class="cockpit-card"><div class="cockpit-card-head"><div class="cockpit-card-title">Promotion history</div></div><div class="cockpit-card-body">${history}</div></article>
                <article class="cockpit-card"><div class="cockpit-card-head"><div class="cockpit-card-title">Rollback history</div></div><div class="cockpit-card-body">${rollbacks}</div></article>
                <article class="cockpit-card"><div class="cockpit-card-head"><div class="cockpit-card-title">AI Quant commentary${aiChip}</div>${statePill(aiCommentary, { ok: 'Available', empty: 'Not produced' })}</div><div class="cockpit-card-body">${sectionBody(aiCommentary, `<p class="cockpit-card-note">${esc(aiCommentary.text || '')}</p>`)}</div></article>
            </div>`;
    }

    const LEDGER_CHECK_LABELS = {
        risk_constitution_evidence_error: 'Required Risk evidence was missing',
        strategy_evidence: 'Strategy lifecycle approval',
        data_conflict: 'Conflicting provider data',
        data_freshness: 'Market-data freshness',
        liquidity: 'Order liquidity / ADV',
        minimum_cash: 'Minimum cash reserve',
        max_position: 'Maximum position size',
        max_total_exposure: 'Maximum portfolio exposure',
        max_sector_exposure: 'Maximum sector exposure',
        correlation_data: 'Portfolio correlation evidence',
        max_correlated_exposure: 'Maximum correlated exposure',
        risk_per_trade: 'Maximum risk per trade',
        daily_loss: 'Daily loss limit',
        drawdown: 'Drawdown limit',
        max_leverage: 'Maximum leverage',
        max_positions: 'Maximum open positions',
        kill_switch: 'Kill Switch',
        live_autonomy: 'Live autonomy permission',
    };

    const ledgerCheckLabel = (check) => LEDGER_CHECK_LABELS[String(check || '').toLowerCase()] || titleWords(check || 'Risk rule');
    const ledgerFailedChecks = (item) => ((item && item.risk && item.risk.checks) || [])
        .filter((check) => check && check.passed === false);
    const ledgerDetailText = (detail) => {
        const value = String(detail || '').trim();
        if (!value) return '';
        if (value.includes('missing signal data-conflict evidence')) return 'The source-conflict assessment did not reach Risk.';
        if (value.includes('missing signal strategy lifecycle status')) return 'The strategy lifecycle did not reach Risk.';
        if (value.includes('ADV unavailable')) return 'Average daily dollar volume was unavailable, so liquidity headroom could not be proven.';
        if (value.includes('missing signal invested_amount')) return 'The proposed capital amount did not reach Risk.';
        if (value.includes('missing signal sector')) return 'The proposal did not include a sector or asset-class classification.';
        // Reason codes of the decision path (tokenized_stock_excluded, ai_low_entry_confidence,
        // veto:..., trade_radar_blocked=...) in words; app.js holds the one mapping.
        const coded = (typeof UI !== 'undefined' && UI && typeof UI._reasonCodeText === 'function') ? UI._reasonCodeText(value) : value;
        return coded.replaceAll('_', ' ');
    };
    const ledgerCheckExplanation = (check) => {
        const name = String(check && check.check || '').toLowerCase();
        const actual = check ? check.actual : null;
        const limit = check ? check.limit : null;
        const detail = ledgerDetailText(check && check.detail);
        if (name === 'minimum_cash' && finite(actual) !== null && finite(limit) !== null) {
            return `Would leave ${fmtMoney(actual)}; at least ${fmtMoney(limit)} must remain.`;
        }
        if (name === 'strategy_evidence') {
            return `Current lifecycle: ${titleWords(actual || 'unavailable')}; required: Approved or Champion.`;
        }
        if (name === 'liquidity' && finite(actual) !== null && finite(limit) !== null) {
            return `Requested participation ${fmtPct(actual, 3)}; maximum ${fmtPct(limit, 3)} of ADV.`;
        }
        if (detail) return detail;
        if (actual !== null && actual !== undefined && limit !== null && limit !== undefined) {
            return `Observed ${String(actual).replaceAll('_', ' ')}; limit ${String(limit).replaceAll('_', ' ')}.`;
        }
        return 'This mandatory Risk rule did not pass.';
    };
    const ledgerRiskHtml = (item) => {
        const risk = item.risk || {};
        const failed = ledgerFailedChecks(item);
        if (failed.length) {
            return `<ul class="cockpit-ledger-checks">${failed.map((check) => `<li><span><strong>${esc(ledgerCheckLabel(check.check))}</strong>${esc(ledgerCheckExplanation(check))}</span></li>`).join('')}</ul>`;
        }
        if (risk.accepted === true) return '<span class="cockpit-ledger-pass">Risk accepted the proposal.</span>';
        return 'No structured Risk result was captured.';
    };
    const ledgerDataText = (item) => {
        const data = item.data_existed || {};
        const facts = [];
        if (data.autonomous_source) facts.push(`Source: ${titleWords(data.autonomous_source)}`);
        if (finite(data.entry) !== null) facts.push(`Entry: ${fmtMoney(data.entry, Math.abs(Number(data.entry)) < 1 ? 6 : 2)}`);
        if (finite(data.signal_score) !== null) facts.push(`Signal score: ${fmtNumber(data.signal_score, 2)}`);
        if (data.price_integrity_status) facts.push(`Price check: ${titleWords(data.price_integrity_status)}`);
        if (data.price_verified === true) facts.push('Price independently verified');
        if (data.strategy_status) facts.push(`Lifecycle: ${titleWords(data.strategy_status)}`);
        if (finite(data.average_daily_volume_usd) !== null) facts.push(`Dollar volume: ${fmtMoney(data.average_daily_volume_usd, 0)}`);
        if (data.captured_at) facts.push(`Captured ${fmtDate(data.captured_at, true)}`);
        return facts.join(' · ') || 'No point-in-time market facts were captured.';
    };
    const ledgerAiText = (item) => {
        const ai = item.ai_model || {};
        if (ai.consulted === false) return 'AI was not consulted because Risk stopped the proposal first.';
        if (ai.available === false) return 'AI was consulted but no usable model response was available.';
        const identity = [ai.provider, ai.model].filter(Boolean).join(' / ');
        return identity ? `${identity} reviewed this proposal.` : 'AI review evidence is unavailable.';
    };
    const ledgerCapitalText = (item) => {
        const capital = item.capital_allowed || {};
        const finalSize = finite(capital.usd_size || capital.allocated_usd || capital.size);
        const requested = finite(capital.invested_amount || capital.requested_usd);
        const leverage = finite(capital.leverage) || 1;
        if (finalSize !== null && finalSize > 0) {
            return `Capital allowed ${fmtMoney(finalSize)} at ${fmtNumber(leverage, 0)}× leverage.`;
        }
        if (finalSize === 0) {
            return `No capital was allocated${requested !== null ? `; preliminary request was ${fmtMoney(requested)}` : ''}.`;
        }
        return requested !== null
            ? `Preliminary allocation request was ${fmtMoney(requested)} at ${fmtNumber(leverage, 0)}× leverage.`
            : 'Capital was not reached because an earlier gate stopped the proposal.';
    };
    const ledgerExecutionText = (item) => {
        const executed = item.executed || {};
        const positionId = executed.position_id || item.position_id;
        const orderId = executed.order_id || executed.order_intent_id;
        const data = item.data_existed || {};
        const entry = finite(executed.entry_price) ?? finite(data.entry);
        if (positionId || orderId) {
            const positionStatus = String(executed.position_status || 'OPEN').toUpperCase();
            let details = `Paper position opened${orderId ? ` · Order #${orderId}` : ''}${positionId ? ` · Position ID ${positionId}` : ''}`;
            if (entry !== null) details += ` · Entry: ${fmtMoney(entry, Math.abs(entry) < 1 ? 6 : 2)}`;
            if (positionStatus === 'CLOSED') details = `Paper position closed${positionId ? `; Position ID ${positionId}` : ''}${entry !== null ? `; Entry: ${fmtMoney(entry, Math.abs(entry) < 1 ? 6 : 2)}` : ''}`;
            return details;
        }
        if (String(item.status || '').toLowerCase() === 'accepted') {
            return `Position accepted and recorded in paper portfolio${entry !== null ? ` · Entry: ${fmtMoney(entry, Math.abs(entry) < 1 ? 6 : 2)}` : ''}.`;
        }
        return 'No order was sent and no position was created.';
    };

    function renderLedger(payload) {
        const ledger = payload.decision_ledger || {};
        const items = ledger.items || [];
        const capital = (payload.header || {}).capital || {};
        const governance = ledger.governance || {};
        const statusCounts = ledger.status_counts || {};
        const recentRejected = items.filter((item) => String(item.status || '').toLowerCase() === 'rejected').length;
        const blockerCounts = new Map();
        items.forEach((item) => ledgerFailedChecks(item).forEach((check) => {
            const code = String(check.check || 'risk_rule');
            blockerCounts.set(code, (blockerCounts.get(code) || 0) + 1);
        }));
        const blockers = [...blockerCounts.entries()].sort((left, right) => right[1] - left[1]);
        const allRejected = finite(statusCounts.rejected);
        const totalEntries = finite(ledger.total_entries) ?? items.length;
        const approved = finite(governance.approved_strategies);
        const strategyTotal = finite(governance.strategy_total);
        const policyBlocksAll = governance.require_approved_strategy === true && approved === 0;
        // Risk rules are only one of the stages that can stop a trade. Count the
        // others too, otherwise a window of AI "No trade" verdicts reads as
        // "nothing is blocking" while nothing opens.
        const proposalAction = (item) => String((item.ai_proposal || {}).action || (item.ai_proposal || {}).recommendation || String(item.what_happened || '').split(' (', 1)[0] || '').toUpperCase();
        const notOpened = items.filter((item) => String(item.status || '').toLowerCase() === 'rejected');
        const stageCounts = [
            ['AI review recommended No trade / Watch', notOpened.filter((item) => !ledgerFailedChecks(item).length && ['NO_TRADE', 'WATCH', 'AVOID'].includes(proposalAction(item))).length],
            ['Independent critic vetoed the entry', items.filter((item) => item.critic && item.critic.veto === true).length],
            ['AI answer failed validation', items.filter((item) => item.validation && item.validation.ok === false).length],
        ].filter(([, count]) => count > 0);
        const autonomy = (payload.header || {}).autonomy || {};
        const readinessBlock = autonomy.evaluated !== false && autonomy.ready === false && (autonomy.reasons || []).length
            ? `<p class="cockpit-ledger-now"><strong>Right now no new Paper Auto position can open:</strong> ${esc((autonomy.reasons || []).map((reason) => (String(reason).toUpperCase().includes('EXECUTION_STATE_CONFLICT') ? 'the paper order ledger has an unresolved conflict (see Current risks)' : plain(reason))).join('; '))}.</p>`
            : '';
        const blockerRows = [
            ...blockers.map(([code, count]) => [`Risk rule: ${ledgerCheckLabel(code)}`, count]),
            ...stageCounts,
        ];
        const blockerSummary = blockerRows.length
            ? `<ul class="cockpit-ledger-blockers">${blockerRows.map(([label, count]) => `<li><span>${esc(label)}</span><strong>${count} / ${items.length}</strong></li>`).join('')}</ul>`
            : '<p class="cockpit-card-note">No failed Risk checks appear in the visible window.</p>';
        const policyWarning = policyBlocksAll
            ? `<div class="cockpit-ledger-policy"><div><strong>Current governance cannot admit these strategies</strong><p>Risk requires an Approved or Champion strategy, while Strategy Lab has 0 approved strategies out of ${strategyTotal ?? 0}. Research and Challenger candidates remain blocked until you change that owner policy or a strategy earns promotion.</p></div></div>`
            : '';
        const entries = items.map((item, index) => {
            const failed = ledgerFailedChecks(item);
            const hasExecution = Boolean((item.executed || {}).position_id || (item.executed || {}).order_id || (item.executed || {}).order_intent_id || item.position_id);
            const happened = failed.length ? `No trade — stopped by ${ledgerCheckLabel(failed[0].check)}` : decisionHeadline(item);
            const aiProposalHtml = (() => {
                if (item.ai_model && item.ai_model.consulted === false) {
                    return 'No AI proposal — Risk stopped the flow first.';
                }
                const prop = item.ai_proposal || {};
                const action = prop.action || prop.recommendation || (item.ai_model ? item.ai_model.action : null);
                if (!action) return 'Unavailable';
                const conf = finite(prop.confidence);
                const thesis = prop.thesis || prop.reason || '';
                const parts = [`<strong>${esc(sentenceWords(action))}</strong>`];
                if (conf !== null && conf > 0) {
                    parts.push(`<span>(Confidence: ${Math.round(conf <= 1 ? conf * 100 : conf)}%)</span>`);
                }
                if (thesis) {
                    parts.push(`<div class="cockpit-ledger-quote">${esc(thesis)}</div>`);
                }
                return parts.join(' ');
            })();
            const criticHtml = (() => {
                const critic = item.critic || {};
                if (item.ai_model && item.ai_model.consulted === false) {
                    return 'The independent critic did not run because Risk stopped the proposal first.';
                }
                if (critic.available === true) {
                    const veto = critic.veto === true;
                    const modelVetoOverridden = critic.model_veto === true && !veto;
                    const model = critic.model ? ` [${esc(critic.model)}]` : '';
                    const rationale = critic.rationale || critic.challenges || '';
                    // The override label keeps its recorded wording; the skin
                    // prints it in sentence case like the other chips.
                    const badge = veto
                        ? '<span class="ds-chip ds-chip--neg">Vetoed</span>'
                        : modelVetoOverridden
                            ? '<span class="ds-chip ds-chip--warn cockpit-chip-sentence">MODEL VETO OVERRIDDEN BY SCORE POLICY</span>'
                            : '<span class="ds-chip ds-chip--pos">Approved</span>';
                    const policy = modelVetoOverridden
                        ? `<div class="cockpit-ledger-quote cockpit-tone--warn">Effective policy: ${esc(critic.policy_reason || 'score-calibrated override')}${finite(critic.size_factor) !== null ? ` · size factor ${finite(critic.size_factor).toFixed(2)}` : ''}</div>`
                        : '';
                    return `${badge}${model}${policy}${rationale ? `<div class="cockpit-ledger-quote">${esc(rationale)}</div>` : ''}`;
                }
                if (critic.skipped === true || critic.reason === 'skipped_after_ai_validation_veto') {
                    const stage = critic.upstream_stage ? ` (${esc(titleWords(critic.upstream_stage))})` : '';
                    const reason = critic.upstream_reason
                        ? `<div class="cockpit-ledger-quote">${esc(critic.upstream_reason)}</div>`
                        : '';
                    return `<span class="ds-chip ds-chip--warn">Skipped — AI/policy already blocked${stage}</span>${reason}`;
                }
                const aiAction = String((item.ai_proposal || {}).action || (item.ai_proposal || {}).recommendation || '').toLowerCase();
                if (['no_trade', 'avoid', 'watch'].includes(aiAction)) {
                    return 'Not needed — AI already recommended No Trade / Watch (Critic only reviews active trade proposals).';
                }
                return esc(titleWords(critic.verdict || critic.reason || 'Skipped'));
            })();
            const debateHtml = (() => {
                const debate = item.debate;
                if (!debate) return 'No debate ran (scope: pre-earnings entries only, after the AI reviewer approves).';
                if (debate.available !== true) {
                    const reason = debate.error ? ` (${esc(String(debate.error))})` : '';
                    return `<span class="ds-chip ds-chip--warn">Unavailable${reason}</span> — the reviewer verdict stands.`;
                }
                const verdict = debate.verdict || {};
                const models = debate.models || {};
                const rec = String(verdict.recommendation || '').toUpperCase();
                const recTone = { APPROVE: 'pos', REDUCE: 'warn', WATCH: 'warn', AVOID: 'neg' }[rec] || '';
                const conf = finite(verdict.confidence);
                const block = (label, text, tone) => `
                    <div class="cockpit-ledger-arg">
                        <span class="ds-label cockpit-tone--${tone}">${esc(label)}</span>
                        <div class="cockpit-ledger-quote">${esc(text || '—')}</div>
                    </div>`;
                return `
                    <div><span class="ds-chip${recTone ? ` ds-chip--${recTone}` : ''}">${esc(rec ? sentenceWords(rec) : '—')}</span>
                    ${conf !== null ? `<span class="cockpit-ledger-meta"> Confidence: ${Math.round(conf <= 1 ? conf * 100 : conf)}%</span>` : ''}
                    <span class="cockpit-ledger-meta"> · bull ${finite(verdict.bull_score) ?? '—'}/10 vs bear ${finite(verdict.bear_score) ?? '—'}/10${models.manager ? ` · judge ${esc(models.manager)}` : ''}</span></div>
                    ${verdict.rationale ? `<div class="cockpit-ledger-quote">${esc(verdict.rationale)}</div>` : ''}
                    ${block('Bull argument', debate.bull, 'good')}
                    ${block('Bear argument', debate.bear, 'bad')}`;
            })();
            const afterwardHtml = (() => {
                if (item.reflection && item.reflection.lesson) {
                    const base = item.afterward ? esc(titleWords(item.afterward.outcome || item.afterward.status || 'Outcome recorded')) : 'No afterward evaluation is available yet.';
                    const pnl = finite(item.reflection.pnl_pct);
                    return `${base}<div class="cockpit-ledger-quote">${aiChip} <strong>AI lesson:</strong> ${esc(item.reflection.lesson)}${pnl !== null ? ` <span class="cockpit-ledger-meta num">(${pnl > 0 ? '+' : ''}${pnl.toFixed(2)}%)</span>` : ''}</div>`;
                }
                return item.afterward ? esc(titleWords(item.afterward.outcome || item.afterward.status || 'Outcome recorded')) : 'No afterward evaluation is available yet.';
            })();
            const fields = [
                ['What happened?', `<strong>${esc(happened)}</strong>`],
                [hasExecution ? 'Position status' : 'Why no position?', failed.length ? ledgerRiskHtml(item) : esc(ledgerExecutionText(item))],
                ['What data existed?', esc(ledgerDataText(item))],
                ['Which strategy?', `${esc(String(item.strategy || 'Unavailable').replaceAll('_', ' '))}${item.data_existed && item.data_existed.strategy_status ? ` · ${esc(titleWords(item.data_existed.strategy_status))}` : ''}`],
                ['Which AI model?', esc(ledgerAiText(item))],
                ['Which prompt?', item.ai_model && item.ai_model.consulted === false ? 'No prompt was created because AI was not reached.' : (item.prompt && item.prompt.version ? `Prompt ${esc(item.prompt.version)} was used.` : 'Prompt metadata is unavailable.')],
                ['What did AI propose?', aiProposalHtml, true],
                ['What did critic say?', criticHtml, true],
                ['Did the AI debate itself?', debateHtml, true],
                ['What did risk say?', ledgerRiskHtml(item)],
                ['What did capital allow?', esc(ledgerCapitalText(item))],
                ['What was executed?', esc(ledgerExecutionText(item))],
                ['What happened afterward?', afterwardHtml],
            ];
            return `<details class="cockpit-ledger-entry" data-ledger-id="${esc(item.decision_id || item.event_id || index)}">
                <summary><span class="cockpit-item-main"><span class="cockpit-item-title">${esc(item.ticker || 'SYSTEM')} · ${esc(happened)}</span><span class="cockpit-item-sub">${esc(fmtDate(item.timestamp, true))}${failed.length ? ` · ${failed.length} failed Risk rule${failed.length === 1 ? '' : 's'}` : ''}</span></span>${statusPill(decisionStatusLabel(item.status))}</summary>
                <div class="cockpit-ledger-fields">${fields.map(([label, html, ai]) => `<div class="cockpit-ledger-field${ai ? ' is-ai' : ''}"><span>${esc(label)}${ai ? aiChip : ''}</span><div class="cockpit-ledger-answer">${html}</div></div>`).join('')}</div>
            </details>`;
        }).join('');
        const replay = payload.decision_replay || {};
        const replayRows = (replay.items || []).map((item) => `<tr><td>${esc(item.ticker || '—')}</td><td>${esc(item.action || '—')}</td><td>${esc(item.model || '—')}</td><td>${esc(item.replay_of || '—')}</td><td class="cockpit-cell-date">${esc(fmtDate(item.created_at, true))}</td></tr>`).join('');
        const lab = payload.model_lab || {};
        const labRows = (lab.items || []).map((run) => `<tr><td>${esc(run.role || '—')}</td><td class="cockpit-cell-date">${esc(run.created_at ? fmtDate(run.created_at, true) : '—')}</td><td>${(run.results || []).map((result) => `${esc(result.provider || '?')}/${esc(result.model || '?')}:${esc(result.action || '—')}`).join(' · ') || '—'}</td></tr>`).join('');
        return `<div class="cockpit-workspace-head"><div><h2>Decision Ledger</h2><p>Every decision — executed, rejected or NO TRADE — explained in operator language with point-in-time evidence.</p></div><div class="cockpit-head-actions"><button type="button" class="cockpit-link-btn" data-cockpit-open-risk><span>Open Risk controls</span> <i class="fa-solid fa-sliders"></i></button>${routeButton('Open Capital audit', 'capital', 'fa-arrow-up-right-from-square')}</div></div>
            <div class="cockpit-summary-grid ds-stats">
                <div class="cockpit-summary-box ds-stat"><span class="ds-label">Persistent decisions</span><strong>${totalEntries.toLocaleString()}</strong></div>
                <div class="cockpit-summary-box ds-stat"><span class="ds-label">All ledger rejects</span><strong>${allRejected === null ? '—' : allRejected.toLocaleString()}</strong></div>
                <div class="cockpit-summary-box ds-stat"><span class="ds-label">Recent rejected</span><strong>${recentRejected} / ${items.length}</strong></div>
                <div class="cockpit-summary-box ds-stat"><span class="ds-label">Historical closed trades</span><strong>${finite(capital.closed_trades) ?? '—'}</strong></div>
            </div>
            <article class="cockpit-card cockpit-ledger-diagnosis"><div class="cockpit-card-head"><div class="cockpit-card-title">Why positions are not opening</div></div><div class="cockpit-card-body"><p class="cockpit-card-note">What stopped the last ${items.length} decisions, by stage. AI is intentionally not called after an earlier Risk rejection.</p>${readinessBlock}${blockerSummary}</div></article>
            ${policyWarning}
            ${ledger.coverage ? `<p class="cockpit-card-note">Showing the latest ${items.length} of ${totalEntries} persistent ledger decisions. Historical closed trades are reported separately by Capital.</p>` : ''}
            ${sectionBody(ledger, `<div class="cockpit-ledger-list">${entries}</div>`)}
            <div class="cockpit-overview-grid">
                <article class="cockpit-card cockpit-card--full"><div class="cockpit-card-head"><div class="cockpit-card-title">Point-in-time replays</div>${statePill(replay, { one: 'replay', many: 'replays', empty: 'None recorded' })}</div><div class="cockpit-card-body">${sectionBody(replay, `<div class="cockpit-table-wrap"><table class="cockpit-table"><thead><tr><th>Ticker</th><th>Replay action</th><th>Model</th><th>Replay of</th><th>Recorded</th></tr></thead><tbody>${replayRows}</tbody></table></div>`)}</div></article>
                <article class="cockpit-card cockpit-card--full"><div class="cockpit-card-head"><div class="cockpit-card-title">Model Lab (per role)</div>${statePill(lab, { one: 'run', many: 'runs', empty: 'None recorded' })}</div><div class="cockpit-card-body">${sectionBody(lab, `<div class="cockpit-table-wrap"><table class="cockpit-table"><thead><tr><th>Role</th><th>Run</th><th>Candidates</th></tr></thead><tbody>${labRows}</tbody></table></div>`)}</div></article>
            </div>`;
    }

    function renderResearch(payload) {
        const research = payload.research_factory || {};
        const worker = research.worker || {};
        const activity = (research.activity || []).length
            ? `<div class="cockpit-table-wrap"><table class="cockpit-table"><thead><tr><th>Hypothesis</th><th>Experiment</th><th>Status</th><th>Tests / sample</th><th>Validation</th><th>Last run</th></tr></thead><tbody>${research.activity.map((item) => `<tr><td>${item.description ? `${esc(plain(item.description))}<br><small>${esc([item.strategy, item.hypothesis].filter(Boolean).join(' · '))}</small>` : esc(item.hypothesis || '—')}</td><td>${esc(plain(item.experiment || '—'))}</td><td>${statusPill(item.status)}</td><td>${esc(item.tests ?? '—')}</td><td>${esc(item.validation ? sentenceWords(item.validation) : 'Not validated')}</td><td class="cockpit-cell-date">${esc(fmtDate(item.last_run, true))}</td></tr>`).join('')}</tbody></table></div>`
            : '<div class="cockpit-empty"><span>No observed research activity exists.</span></div>';
        const autoCycleOn = !!(research.auto_cycle && research.auto_cycle.enabled);
        // Karpathy autoresearch adoption: KEEP/DISCARD ledger + current best.
        const ar = research.auto_cycle && research.auto_cycle.autoresearch ? research.auto_cycle.autoresearch : null;
        const autoresearchNote = ar
            ? `<p class="cockpit-card-note">Autoresearch ledger: <strong>${Number(ar.keep ?? 0)} kept</strong> / <strong>${Number(ar.discard ?? 0)} discarded</strong>${Number(ar.open ?? 0) ? ` · ${Number(ar.open)} open` : ''}${ar.current_best ? ` · current best: <strong>${esc(String(ar.current_best.strategy_id || ar.current_best.experiment_id || '—'))}</strong> — ${esc(String(ar.current_best.description || ''))}` : ' · no kept experiment yet'}</p>`
            : '';
        // Freshness note: experiments only appear when a weakness is detected, so
        // the list can legitimately show old timestamps while the cycle still runs.
        const cyc = research.auto_cycle || {};
        const cycleNote = autoCycleOn && cyc.last_run
            ? `<p class="cockpit-card-note">Auto-cycle last checked ${esc(fmtDate(cyc.last_run, true))} — ${Number(cyc.last_weaknesses ?? 0)} weakness(es) detected, ${Number(cyc.last_experiments ?? 0)} new experiment(s). No new experiment is created when nothing is weak; rows below keep their own last-updated time.</p>`
            : '';
        return `<div class="cockpit-workspace-head"><div><h2>Research Factory</h2><p>Honest readiness view: observed research is real; future autonomous capabilities are labelled by phase and are not clickable placeholders.</p></div>${routeButton('Coding Agent', 'repair', 'fa-screwdriver-wrench')}</div>
            <div class="cockpit-summary-grid ds-stats">
                <div class="cockpit-summary-box ds-stat"><span class="ds-label">Worker</span><strong>${esc(worker.running === true ? 'Running' : (worker.enabled === false ? 'Disabled' : 'Unavailable'))}</strong></div>
                <div class="cockpit-summary-box ds-stat"><span class="ds-label">Observations</span><strong>${(research.activity || []).length}</strong></div>
                <div class="cockpit-summary-box ds-stat"><span class="ds-label">Last success</span><strong class="is-text">${esc(fmtDate(worker.last_success, true))}</strong></div>
                <div class="cockpit-summary-box ds-stat"><span class="ds-label">Auto-cycle</span><strong>${autoCycleOn ? 'On' : 'Off'}</strong><button type="button" class="cockpit-btn cockpit-btn--secondary cockpit-summary-toggle" data-cockpit-toggle-autocycle="${autoCycleOn ? 'stop' : 'start'}">${autoCycleOn ? 'Turn off' : 'Turn on'}</button></div>
            </div>
            <div class="cockpit-overview-grid">
                <article class="cockpit-card cockpit-card--full"><div class="cockpit-card-head"><div class="cockpit-card-title">Hypotheses and experiments</div>${statePill({ state: research.state, count: (research.activity || []).length }, { one: 'recent row', many: 'recent rows', empty: 'No activity' })}</div><div class="cockpit-card-body">${autoresearchNote}${cycleNote}${activity}</div></article>
                <article class="cockpit-card cockpit-card--full"><div class="cockpit-card-head"><div class="cockpit-card-title">Factory capability map</div></div><div class="cockpit-card-body"><div class="cockpit-capability-grid">${(research.capabilities || []).map((item) => `<div class="cockpit-capability"><div class="cockpit-health-head"><strong>${esc(item.name)}</strong>${statusPill(item.status)}</div><p>${esc(words(item.phase))}</p></div>`).join('')}</div></div></article>
            </div>`;
    }

    const agentVerdictTone = (verdict) => String(verdict || '').toUpperCase() === 'PASSED' ? 'cockpit-tone--good'
        : (String(verdict || '').toUpperCase() === 'FAILED' ? 'cockpit-tone--bad' : 'cockpit-tone--muted');
    const repairDur = (seconds) => (seconds === null || seconds === undefined ? '—' : fmtDuration(seconds));
    let repairAgentCache = null;

    async function loadRepairAgent() {
        const panel = byId('cockpit-panel-repair');
        if (!panel) return;
        try {
            const data = await requestJson('/api/development/repair-agent');
            repairAgentCache = data;
        } catch (err) {
            if (!repairAgentCache) {
                panel.innerHTML = '<div class="cockpit-empty"><span>The Coding Agent view is unavailable right now.</span></div>';
                return;
            }
        }
        panel.innerHTML = renderRepairAgent(repairAgentCache);
    }

    function renderRepairAgent(data) {
        const act = data.activity || {};
        const wd = data.watchdog || {};
        const sum = data.summary || {};
        const cases = data.cases || [];
        const proposals = data.proposals || [];
        const working = act.state === 'WORKING';
        const stateLine = working
            ? `<div class="cockpit-tone--good">${esc(act.stage || 'working')}</div><div>${esc(plain(act.detail))}</div><div class="cockpit-tone--muted cockpit-fine">on: ${esc(plain(act.title))} · for ${repairDur(act.in_state_seconds)}</div>`
            : (act.state === 'WAITING_OWNER'
                ? `<div class="cockpit-tone--warn">${sum.proposals_waiting || proposals.length} reviewed proposal(s) wait for your approval below.</div>`
                : `<div class="cockpit-tone--muted">Nothing to do${act.idle_seconds !== null && act.idle_seconds !== undefined ? ` since ${repairDur(act.idle_seconds)}` : ''} — detectors keep watching.</div><div class="cockpit-tone--muted cockpit-fine">Last outcome: ${esc(words(act.last_outcome) || 'none yet')}${act.last_finished_at ? ` · ${esc(fmtDate(act.last_finished_at, true))}` : ''}</div>`);
        const evidenceLines = (obj) => Object.entries(obj || {})
            .filter(([, v]) => v !== null && v !== undefined && v !== '')
            .map(([k, v]) => `<div><span class="cockpit-tone--muted">${esc(k)}:</span> ${esc(String(v).slice(0, 80))}</div>`).join('');
        const chainBadges = (chain) => (chain || []).length
            ? (chain || []).map((v) => `<div class="cockpit-verdict"><div class="cockpit-verdict-head">${statusPill(v.verdict)}<strong>${esc(v.agent)}</strong></div>${v.rationale ? `<p>${esc(plain(v.rationale))}</p>` : ''}</div>`).join('')
            : '<span class="cockpit-tone--muted">not reached</span>';
        const attemptsBlock = (item) => {
            const attempts = item.attempts || [];
            if (!attempts.length) return '<span class="cockpit-tone--muted">not started</span>';
            const attemptRows = attempts.map((a, idx) => `<div><strong>Attempt ${idx + 1}</strong> · ${esc(a.status || '')}${a.error ? `<div class="cockpit-tone--muted cockpit-fine">${esc(plain(a.error))}</div>` : ''}</div>`).join('');
            const reasoning = (item.agent_reasoning || []).length
                ? (item.agent_reasoning || []).map((r) => `<div class="cockpit-tone--muted cockpit-fine">learned: ${esc(plain(r))}</div>`).join('')
                : '';
            return attemptRows + reasoning;
        };
        const repairStageLabel = (value) => sentenceWords(String(value || 'not reached'));
        const repairEvent = (event, index) => `<li class="cockpit-repair-event">
            <span class="cockpit-repair-event-index">${index + 1}</span>
            <div><div class="cockpit-repair-event-head"><strong>${esc(repairStageLabel(event.stage))}</strong>${statusPill(event.status || 'INFO')}<time>${esc(fmtDate(event.at, true))}</time></div>
            ${event.detail ? `<p>${esc(String(event.detail))}</p>` : ''}
            ${event.evidence && Object.keys(event.evidence).length ? `<details class="cockpit-repair-raw"><summary>Stage evidence</summary><pre>${esc(JSON.stringify(event.evidence, null, 2))}</pre></details>` : ''}</div>
        </li>`;
        const repairAttempt = (attempt, index) => {
            const events = attempt.events || [];
            const stages = Object.entries(attempt.stages || {});
            const provider = [attempt.provider, attempt.model].filter(Boolean).join(' · ');
            return `<article class="cockpit-repair-attempt">
                <div class="cockpit-repair-attempt-head"><div><span>Attempt ${esc(attempt.attempt || index + 1)}</span><strong>${esc(provider || 'Provider/model not recorded')}</strong></div>${statusPill(attempt.status || 'UNKNOWN')}</div>
                <div class="cockpit-repair-meta">
                    <span><b>Started</b>${esc(fmtDate(attempt.started_at, true))}</span>
                    <span><b>Finished</b>${esc(fmtDate(attempt.finished_at, true))}</span>
                    <span><b>Submission</b>${esc(attempt.submission_id || 'not created')}</span>
                    <span><b>Sandbox</b>${attempt.worktree_removed ? 'cleaned' : 'not recorded / retained'}</span>
                </div>
                ${attempt.error ? `<div class="cockpit-repair-error"><i class="fa-solid fa-triangle-exclamation"></i><div><strong>Recorded error</strong><p>${esc(String(attempt.error))}</p></div></div>` : ''}
                ${stages.length ? `<div class="cockpit-repair-stage-strip">${stages.map(([stage, verdict]) => `<span class="${agentVerdictTone(verdict)}"><b>${esc(repairStageLabel(stage))}</b> ${esc(words(verdict))}</span>`).join('')}</div>` : ''}
                ${events.length ? `<ol class="cockpit-repair-events">${events.map(repairEvent).join('')}</ol>` : '<p class="cockpit-repair-legacy">Detailed stage events were not captured for this legacy attempt; the persisted status and error above are the complete available evidence.</p>'}
            </article>`;
        };
        const repairReview = (review) => {
            const findings = [...(review.findings || []), ...(review.failures || [])];
            return `<article class="cockpit-repair-review">
                <div class="cockpit-repair-review-head"><div><strong>${esc(repairStageLabel(review.stage))}</strong><span>${esc(review.agent || 'reviewer not recorded')}${review.model ? ` · ${esc(review.model)}` : ''}</span></div>${statusPill(review.verdict || 'NOT_PERFORMED')}</div>
                ${review.rationale ? `<p>${esc(String(review.rationale))}</p>` : ''}
                ${findings.length ? `<ul>${findings.map((finding) => `<li>${esc(String(finding))}</li>`).join('')}</ul>` : ''}
                ${review.command ? `<div class="cockpit-repair-command"><b>Check:</b> ${esc(review.command)}${(review.files_checked || []).length ? ` · ${esc(review.files_checked.join(', '))}` : ''}</div>` : ''}
                <time>${esc(fmtDate(review.reviewed_at, true))}</time>
            </article>`;
        };
        const repairCaseDetail = (item) => {
            const diagnostic = item.diagnostic || {};
            const history = item.history || [];
            const attempts = item.attempts || [];
            const reviews = item.review_chain || [];
            const assignments = Object.entries(item.role_assignments || {}).filter(([, assignment]) => assignment && (assignment.provider || assignment.model));
            const postCheck = item.post_check || null;
            return `<div class="cockpit-repair-detail-panel">
                <section class="cockpit-repair-diagnosis ${String(item.status || '').toUpperCase() === 'FAILED' ? 'is-failed' : ''}">
                    <div class="cockpit-repair-section-title"><div><span>Outcome diagnosis</span><strong>${esc(repairStageLabel(diagnostic.failed_stage || item.status))}</strong></div></div>
                    <div><b>What happened</b><p>${esc(String(diagnostic.reason || item.error || item.outcome || item.status || 'No diagnostic was captured.'))}</p></div>
                    <div><b>What happens next</b><p>${esc(String(diagnostic.next_action || 'No next action was recorded.'))}</p></div>
                    ${diagnostic.evidence_complete === false ? `<div class="cockpit-repair-missing">Missing from this record: ${esc((diagnostic.missing_evidence || []).join(', ') || 'unspecified evidence')}.</div>` : ''}
                </section>
                <div class="cockpit-repair-detail-grid">
                    <section class="cockpit-repair-section"><div class="cockpit-repair-section-title"><div><span>1. Detection</span><strong>${esc(item.recommended_by || 'unknown detector')} · ${esc(fmtDate(item.detected_at, true))}</strong></div></div>
                        <p class="cockpit-repair-problem">${esc(String(item.problem || 'Problem statement was not recorded.'))}</p>
                        ${(item.target_files || []).length ? `<div class="cockpit-repair-files"><b>Target files</b>${item.target_files.map((file) => `<code>${esc(file)}</code>`).join('')}</div>` : '<p class="cockpit-repair-legacy">No target file was recorded.</p>'}
                        ${assignments.length ? `<div class="cockpit-repair-assignments"><b>Assigned models</b>${assignments.map(([role, assignment]) => `<span>${esc(repairStageLabel(role))}: ${esc([assignment.provider, assignment.model].filter(Boolean).join(' · '))}</span>`).join('')}</div>` : ''}
                    </section>
                    <section class="cockpit-repair-section"><div class="cockpit-repair-section-title"><div><span>2. Lifecycle</span><strong>${history.length} recorded transition(s)</strong></div></div>
                        ${history.length ? `<ol class="cockpit-repair-events">${history.map((event, index) => repairEvent({ at: event.at, stage: `${event.from || 'new'} → ${event.to || 'unknown'}`, status: event.to || 'INFO', detail: event.note }, index)).join('')}</ol>` : '<p class="cockpit-repair-legacy">No lifecycle history was captured for this case.</p>'}
                    </section>
                    <section class="cockpit-repair-section cockpit-repair-section--full"><div class="cockpit-repair-section-title"><div><span>3. Coding attempts</span><strong>${attempts.length} attempt(s)</strong></div></div>
                        ${attempts.length ? `<div class="cockpit-repair-attempts">${attempts.map(repairAttempt).join('')}</div>` : '<p class="cockpit-repair-legacy">Coding Agent did not start; inspect the lifecycle and diagnosis above.</p>'}
                    </section>
                    <section class="cockpit-repair-section"><div class="cockpit-repair-section-title"><div><span>4. Independent review</span><strong>${reviews.length ? `${reviews.length} verdict(s)` : 'not reached'}</strong></div></div>
                        ${reviews.length ? `<div class="cockpit-repair-reviews">${reviews.map(repairReview).join('')}</div>` : '<p class="cockpit-repair-legacy">No reviewer verdict exists because the chain stopped before independent review.</p>'}
                    </section>
                    <section class="cockpit-repair-section"><div class="cockpit-repair-section-title"><div><span>5. Apply & post-check</span><strong>${postCheck ? (postCheck.passed ? 'verified' : 'failed') : 'not reached'}</strong></div></div>
                        ${postCheck ? `<div class="cockpit-repair-postcheck ${postCheck.passed ? 'is-passed' : 'is-failed'}"><b>${postCheck.passed ? 'Post-check passed' : 'Post-check failed'}</b><p>${esc((postCheck.files || []).join(', ') || 'No files recorded')}</p>${(postCheck.failures || []).length ? `<ul>${postCheck.failures.map((failure) => `<li>${esc(String(failure))}</li>`).join('')}</ul>` : ''}</div>` : '<p class="cockpit-repair-legacy">No change reached production apply, so no post-check was run.</p>'}
                        ${(item.applied_files || []).length ? `<div class="cockpit-repair-files"><b>Applied files</b>${item.applied_files.map((file) => `<code>${esc(file)}</code>`).join('')}</div>` : ''}
                    </section>
                </div>
            </div>`;
        };
        const proposalCards = proposals.length
            ? proposals.map((item) => `<article class="cockpit-card"><div class="cockpit-card-head"><div class="cockpit-card-title">${esc(plain(item.title))}</div>${statusPill('PROPOSED')}</div>
                <div class="cockpit-card-body">
                    <div class="cockpit-tone--muted cockpit-fine">proposed by ${esc(item.recommended_by || '')} · ${esc(fmtDate(item.detected_at, true))}</div>
                    <div>${chainBadges(item.review_chain)}</div>
                    ${(item.diff_files || []).length ? `<div class="cockpit-tone--muted cockpit-fine">files: ${esc(item.diff_files.join(', '))}</div>` : ''}
                    <details><summary>Full recommendation and diff preview</summary><pre class="cockpit-pre">${esc(item.problem || '')}</pre><pre class="cockpit-pre">${esc(item.diff_preview || '')}</pre></details>
                </div>
                <div class="cockpit-card-actions"><button type="button" class="cockpit-btn cockpit-btn--secondary" data-cockpit-repair-approve="${esc(item.case_id)}">Approve and apply</button></div>
            </article>`).join('')
            : '<div class="cockpit-empty"><span>No proposal is waiting. The scout proposes one bounded improvement when the system stays healthy.</span></div>';
        const latest = cases[0];
        const journeyCard = latest ? `<article class="cockpit-card cockpit-card--full"><div class="cockpit-card-head"><div class="cockpit-card-title">Latest case — the complete journey</div>${statusPill(latest.status)}</div><div class="cockpit-card-body">
            <p class="cockpit-card-note cockpit-step">Step 1 — Recommended by <strong>${esc(latest.recommended_by || '—')}</strong> (${esc(fmtDate(latest.detected_at, true))}): ${esc(plain(latest.title))}</p>
            ${evidenceLines(latest.evidence) ? `<div class="cockpit-evidence">${evidenceLines(latest.evidence)}</div>` : ''}
            <details><summary>Full recommendation sent to the coding agent</summary><pre class="cockpit-pre">${esc(latest.problem || '')}</pre></details>
            <p class="cockpit-card-note cockpit-step">Step 2 — Coding agent (${(latest.attempts || []).length} attempt(s))</p>
            ${attemptsBlock(latest)}
            <p class="cockpit-card-note cockpit-step">Step 3 — Independent reviews</p>
            ${chainBadges(latest.review_chain)}
            <p class="cockpit-card-note cockpit-step">Step 4 — Outcome</p>
            <div>${latest.status === 'APPLIED'
                ? `<span class="cockpit-tone--good">applied ${esc((latest.applied_files || []).join(', '))} · verified ${latest.post_check && latest.post_check.passed ? 'yes' : 'no'}</span>`
                : esc(latest.error ? plain(latest.error) : words(latest.outcome || latest.status))}</div>
            ${(latest.applied_files || []).length ? `<details><summary>Diff preview</summary><pre class="cockpit-pre">${esc(latest.diff_preview || '')}</pre></details>` : ''}
        </div></article>` : '';
        const history = cases.length
            ? `<div class="cockpit-table-wrap"><table class="cockpit-table cockpit-repair-table"><thead><tr><th>Case</th><th>Recommended by</th><th>Kind</th><th>Attempts</th><th>Chain</th><th>Status</th><th>Updated</th><th>Action</th></tr></thead><tbody>${cases.map((item) => {
                const caseId = String(item.case_id || '');
                const detailId = `cockpit-repair-detail-${caseId.replace(/[^a-zA-Z0-9_-]/g, '-')}`;
                const expanded = model.expandedRepairCases.has(caseId);
                const retryable = !item.diagnostic || item.diagnostic.retryable !== false;
                return `<tr class="cockpit-repair-row${expanded ? ' is-expanded' : ''}">
                    <td><button type="button" class="cockpit-repair-toggle" data-cockpit-repair-details="${esc(caseId)}" aria-expanded="${expanded}" aria-controls="${esc(detailId)}"><i class="fa-solid fa-chevron-right"></i><span><strong>${esc(String(item.title || 'Untitled case'))}</strong><small>${esc(caseId)}${item.diagnostic?.failed_stage ? ` · stopped at ${esc(repairStageLabel(item.diagnostic.failed_stage))}` : ''}</small></span></button></td>
                    <td>${esc(item.recommended_by || '—')}</td>
                    <td>${esc(item.kind ? sentenceWords(item.kind) : '')}</td>
                    <td>${esc((item.attempts || []).length)}</td>
                    <td>${(item.review_chain || []).map((v) => `<span class="${agentVerdictTone(v.verdict)} cockpit-repair-chain-chip">${esc((v.agent || 'reviewer').replace('_agent', '').replace('_review', ' rev'))}</span>`).join('') || '<span class="cockpit-tone--muted">not reached</span>'}</td>
                    <td>${statusPill(item.status)}</td>
                    <td class="cockpit-cell-date">${esc(fmtDate(item.updated_at, true))}</td>
                    <td><button type="button" class="cockpit-btn cockpit-btn--secondary cockpit-repair-run" data-cockpit-repair-run-case="${esc(caseId)}" title="Run this case through Coding Agent" ${retryable ? '' : 'disabled'}><i class="fa-solid fa-play"></i> ${String(item.status || '').toUpperCase() === 'FAILED' ? 'Retry' : 'Run'}</button></td>
                </tr><tr id="${esc(detailId)}" class="cockpit-repair-detail-row" data-cockpit-repair-detail-row="${esc(caseId)}" ${expanded ? '' : 'hidden'}><td colspan="8">${repairCaseDetail(item)}</td></tr>`;
            }).join('')}</tbody></table></div>`
            : '<div class="cockpit-empty"><span>No case has ever been opened.</span></div>';
        return `<div class="cockpit-workspace-head"><div><h2>Coding Agent</h2><p>The complete journey of the autonomous repair chain — who recommended what, what the agent did and thought at every step, and every reviewer verdict. Repairs auto-apply after independent reviews; improvements wait for your approval.</p></div>
            <div class="cockpit-head-actions">
                <button type="button" class="cockpit-btn cockpit-btn--secondary" data-cockpit-repair-run>Detect and repair now</button>
                <button type="button" class="cockpit-btn cockpit-btn--secondary" data-cockpit-toggle-watchdog="${wd.running ? 'stop' : 'start'}">Watchdog ${wd.running ? 'on' : 'off'}</button>
            </div></div>
        <div class="cockpit-summary-grid ds-stats">
            <div class="cockpit-summary-box ds-stat"><span class="ds-label">Agent state</span><strong class="is-text">${esc(act.state_label || '—')}</strong></div>
            <div class="cockpit-summary-box ds-stat"><span class="ds-label">${working ? 'Working for' : 'Idle for'}</span><strong>${fmtDuration(working ? act.in_state_seconds : act.idle_seconds)}</strong></div>
            <div class="cockpit-summary-box ds-stat"><span class="ds-label">Applied</span><strong>${sum.applied || 0}</strong></div>
            <div class="cockpit-summary-box ds-stat"><span class="ds-label">No code cause</span><strong>${sum.no_defect || 0}</strong></div>
            <div class="cockpit-summary-box ds-stat"><span class="ds-label">Failed / rolled back</span><strong>${(sum.failed || 0) + (sum.rolled_back || 0)}</strong></div>
            <div class="cockpit-summary-box ds-stat"><span class="ds-label">Proposals waiting</span><strong>${sum.proposals_waiting || 0}</strong></div>
            <div class="cockpit-summary-box ds-stat"><span class="ds-label">Owner actions</span><strong>${sum.owner_actions || 0}</strong></div>
        </div>
        <div class="cockpit-overview-grid">
            <article class="cockpit-card cockpit-card--full"><div class="cockpit-card-head"><div class="cockpit-card-title">Right now</div>${statusPill(working ? 'RUNNING' : (act.state === 'WAITING_OWNER' ? 'DRAFT' : 'STABLE'))}</div><div class="cockpit-card-body">${stateLine}
                <p class="cockpit-card-note">Watchdog ${wd.running ? `running · every ${Math.round((wd.interval_seconds || 900) / 60)}min · ${wd.cycles_completed || 0} cycles` : 'stopped'}${wd.last_error ? ` · last error: ${esc(plain(wd.last_error))}` : ''} · improvements scout ${wd.improvements_enabled ? 'on' : 'off'} · max ${wd.max_attempts || 3} attempt(s) per case.</p>
            </div></article>
            ${(data.owner_actions || []).length ? `<article class="cockpit-card cockpit-card--full"><div class="cockpit-card-head"><div class="cockpit-card-title">Owner actions needed — only you can configure these</div>${statePill({ state: 'SUCCESS', count: (data.owner_actions || []).length }, { one: 'to do', many: 'to do', tone: 'attention' })}</div><div class="cockpit-card-body">${(data.owner_actions || []).map((item) => `<div class="cockpit-owner-action"><strong>${esc(plain(item.title))}</strong><div class="cockpit-tone--muted cockpit-fine">${esc(plain(item.problem))}</div></div>`).join('')}</div></article>` : ''}
            <article class="cockpit-card cockpit-card--full"><div class="cockpit-card-head"><div class="cockpit-card-title">Proposals — your approval needed</div>${statePill({ state: sum.proposals_waiting ? 'SUCCESS' : 'EMPTY', count: sum.proposals_waiting || 0 }, { one: 'waiting', many: 'waiting', empty: 'None waiting', tone: 'attention' })}</div><div class="cockpit-card-body">${proposalCards}</div></article>
            ${journeyCard}
            <article class="cockpit-card cockpit-card--full"><div class="cockpit-card-head"><div class="cockpit-card-title">History — every case</div>${statePill({ state: cases.length ? 'SUCCESS' : 'EMPTY', count: cases.length }, { one: 'case', many: 'cases', empty: 'No cases' })}</div><div class="cockpit-card-body">${history}
                <p class="cockpit-card-note">${esc(data.policies || '')}</p>
            </div></article>
        </div>`;
    }

    function renderHealth(payload) {
        const health = payload.system_health || {};
        const services = health.services || [];
        const gaps = (health.coverage_gaps || []).map((gap) => gap.name || gap.id).filter(Boolean);
        const gapsNote = gaps.length
            ? `<p class="cockpit-card-note">Coverage gaps (visible per tile, not system failures): ${esc(gaps.join(', '))}.</p>`
            : '';
        const failing = Array.isArray(health.failing) ? health.failing : [];
        const failingBlock = failing.length
            ? `<div class="cockpit-health-failing"><strong>${esc(health.summary || `${failing.length} checks need attention`)}</strong><ul>${failing.map((item) => `<li>${statusPill(item.status)}<span><b>${esc(item.label)}</b> — ${esc(item.message || 'No detail')}${item.action ? `<em><b>Next step</b>${esc(item.action)}</em>` : ''}</span></li>`).join('')}</ul></div>`
            : `<p class="cockpit-card-note">${esc(health.summary || 'No check needs attention.')}</p>`;
        return `<div class="cockpit-workspace-head"><div><h2>System Health</h2><p>Every tile reports a real check, last-observed evidence, or an explicit unavailable/not-configured state. There is no synthetic green fallback. The overall status reflects active components only.</p></div>${statusPill(health.overall)}</div>
            ${failingBlock}
            ${gapsNote}
            <div class="cockpit-health-grid">${services.map((item) => {
                const evidence = evidenceText(item.evidence);
                const isMl = item.id === 'ml_model' || item.label === 'ML model';
                const mlBtn = isMl
                    ? `<div class="cockpit-health-tools"><button type="button" class="cockpit-btn cockpit-btn--secondary cockpit-btn--sm" onclick="window.CockpitUI && window.CockpitUI.trainMlModel && window.CockpitUI.trainMlModel(event)">${item.status === 'HEALTHY' ? 'Retrain ML model' : 'Train ML model'}</button></div>`
                    : '';
                return `<article class="cockpit-health-item"><div class="cockpit-health-head"><strong>${esc(item.label)}</strong>${statusPill(item.status)}</div><p>${esc(item.message || 'No check message')}</p>${item.action ? `<p class="cockpit-health-action"><b>Next step</b>${esc(item.action)}</p>` : ''}${evidence ? `<div class="cockpit-health-evidence">${esc(evidence).replace(/\n/g, '<br>')}</div>` : ''}${mlBtn}<time>${item.checked_at ? `checked ${esc(fmtDate(item.checked_at, true))} (${esc(fmtAgeOf(item.checked_at))})` : 'never checked'}</time></article>`;
            }).join('')}</div>`;
    }

    // Overview lists grow with live data (30+ positions); cap the visible height
    // at N rows (data-cockpit-cap) and let the rest scroll. Height is measured
    // from real row positions so the cap tracks row density, not a fixed px guess.
    function capOverviewLists(panel) {
        if (!panel) return;
        panel.querySelectorAll('[data-cockpit-cap]').forEach((el) => {
            const cap = Number(el.dataset.cockpitCap) || 15;
            const rows = Array.from(el.children);
            if (rows.length <= cap) {
                el.classList.remove('cockpit-scroll-capped');
                el.style.maxHeight = '';
                return;
            }
            const height = rows[cap].getBoundingClientRect().top - rows[0].getBoundingClientRect().top;
            if (!Number.isFinite(height) || height <= 0) return;
            el.classList.add('cockpit-scroll-capped');
            el.style.maxHeight = `${Math.ceil(height)}px`;
        });
    }

    function render(payload) {
        model.payload = payload;
        renderKpis(payload);
        const overview = byId('cockpit-panel-overview');
        const strategy = byId('cockpit-panel-strategy');
        const ledger = byId('cockpit-panel-ledger');
        const research = byId('cockpit-panel-research');
        const health = byId('cockpit-panel-health');
        // Preserve the owner's open evidence disclosures across the 30s re-render.
        const openLedger = new Set(
            Array.from(document.querySelectorAll('#cockpit-panel-ledger details[open]'))
                .map((node) => node.dataset.ledgerId)
                .filter(Boolean)
        );
        // Track whether any entry has ever been open (to distinguish first load from user-closed-all).
        const hadAnyOpen = openLedger.size > 0 || model.ledgerEverHadOpen;
        if (openLedger.size > 0) model.ledgerEverHadOpen = true;
        // Capture scroll position of the page before re-render (panels use window scroll).
        const savedScrollY = window.scrollY;
        // Capture in-list scroll of capped overview lists; innerHTML reset would lose it.
        const savedListScrolls = {};
        document.querySelectorAll('#cockpit-panel-overview [data-cockpit-cap-id]').forEach((el) => {
            savedListScrolls[el.dataset.cockpitCapId] = el.scrollTop;
        });
        if (overview) overview.innerHTML = renderOverview(payload);
        if (strategy) strategy.innerHTML = renderStrategy(payload);
        loadAutoLab();
        loadLearning();
        if (ledger) ledger.innerHTML = renderLedger(payload);
        if (research) research.innerHTML = renderResearch(payload);
        if (health) health.innerHTML = renderHealth(payload);
        capOverviewLists(overview);
        document.querySelectorAll('#cockpit-panel-overview [data-cockpit-cap-id]').forEach((el) => {
            const top = savedListScrolls[el.dataset.cockpitCapId];
            if (top) el.scrollTop = top;
        });
        loadRepairAgent();
        if (openLedger.size) {
            // Restore exactly which entries were open before re-render.
            document.querySelectorAll('#cockpit-panel-ledger details[data-ledger-id]').forEach((node) => {
                if (openLedger.has(node.dataset.ledgerId)) node.setAttribute('open', '');
            });
        } else if (!hadAnyOpen) {
            // First load only: open the first entry so the page isn't blank.
            const first = document.querySelector('#cockpit-panel-ledger details[data-ledger-id]');
            if (first) first.setAttribute('open', '');
        }
        // Restore the scroll position after re-render.
        window.scrollTo({ top: savedScrollY, behavior: 'instant' });

        const updated = byId('cockpit-updated-at');
        const snapshotAge = ageSeconds(payload.generated_at);
        const snapshotOld = snapshotAge !== null && snapshotAge > SNAPSHOT_OLD_SECONDS;
        if (updated) updated.textContent = `Snapshot ${fmtDate(payload.generated_at, true)}${snapshotOld ? ` (${fmtAge(snapshotAge)})` : ''}`;
        const status = byId('cockpit-status');
        const recentActionError = model.actionError && (Date.now() - model.actionError.at) < 20000 ? model.actionError : null;
        if (status && recentActionError) {
            status.innerHTML = `<i class="fa-solid fa-triangle-exclamation cockpit-tone--bad"></i>${esc(`Action failed: ${recentActionError.message}`)}`;
        } else if (status && snapshotOld) {
            status.innerHTML = `<i class="fa-solid fa-clock cockpit-tone--warn"></i>Showing the snapshot from ${esc(fmtAge(snapshotAge))}; a newer one is being built and will replace it shortly.`;
        } else if (status) {
            const errors = Object.keys(payload.source_errors || {});
            status.innerHTML = errors.length
                ? `<i class="fa-solid fa-triangle-exclamation cockpit-tone--warn"></i>${errors.length} source(s) unavailable; unaffected sections remain usable.`
                : '';
        }
        renderKillSwitch();
        selectTab(model.activeTab);
    }

    function renderKillSwitch() {
        const button = byId('cockpit-kill-switch');
        if (!button) return;
        const active = Boolean(model.payload && model.payload.header && model.payload.header.kill_switch_active);
        const armed = Date.now() < model.stopArmedUntil;
        button.classList.toggle('is-active', active);
        button.classList.toggle('is-armed', armed);
        if (armed) {
            button.innerHTML = `<i class="fa-solid fa-triangle-exclamation"></i> Confirm ${active ? 'release' : 'stop'}`;
        } else if (active) {
            button.innerHTML = '<i class="fa-solid fa-lock"></i> Kill switch active · Release';
        } else {
            button.innerHTML = '<i class="fa-solid fa-hand"></i> Stop autonomous trading';
        }
        button.setAttribute('aria-label', active ? 'Release autonomous trading Kill Switch' : 'Activate autonomous trading Kill Switch');
    }

    function selectTab(tab) {
        const valid = new Set(['overview', 'strategy', 'ledger', 'research', 'health', 'repair']);
        const selected = valid.has(tab) ? tab : 'overview';
        model.activeTab = selected;
        document.querySelectorAll('[data-cockpit-tab]').forEach((button) => {
            const active = button.dataset.cockpitTab === selected;
            button.classList.toggle('is-active', active);
            button.setAttribute('aria-selected', String(active));
        });
        document.querySelectorAll('[data-cockpit-panel]').forEach((panel) => {
            panel.classList.toggle('hidden', panel.dataset.cockpitPanel !== selected);
        });
        // Cap lists only once their panel is visible — hidden panels measure 0-height
        // rows, and the first render() paints before selectTab reveals the panel.
        if (selected === 'overview') capOverviewLists(byId('cockpit-panel-overview'));
    }

    async function requestJson(url, options = {}, timeoutMs = 30000) {
        const controller = new AbortController();
        const shared = options.mutable !== true;  // polls share; owner actions stay private
        if (shared) model.abortController = controller;
        const timeout = setTimeout(() => controller.abort(), timeoutMs);
        try {
            const response = await fetch(url, { ...options, signal: controller.signal, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
            let data = null;
            try { data = await response.json(); } catch (_) { data = null; }
            if (!response.ok) {
                const detail = data && typeof data.detail === 'string' ? data.detail : `Request failed (${response.status})`;
                throw new Error(detail);
            }
            return data;
        } finally {
            clearTimeout(timeout);
            if (shared && model.abortController === controller) model.abortController = null;
        }
    }

    function setLoading(active, preserve = false) {
        model.loading = active;
        const loading = byId('cockpit-loading');
        const content = byId('cockpit-content');
        const error = byId('cockpit-error');
        if (loading) loading.classList.toggle('hidden', !active || preserve);
        if (content) content.classList.toggle('hidden', active && !preserve);
        if (error && active) error.classList.add('hidden');
        const refresh = byId('cockpit-refresh');
        if (refresh) {
            refresh.disabled = active;
            refresh.innerHTML = active ? '<i class="fa-solid fa-spinner fa-spin"></i> Refreshing' : '<i class="fa-solid fa-rotate"></i> Refresh';
        }
    }

    function showError(message) {
        const loading = byId('cockpit-loading');
        const content = byId('cockpit-content');
        const error = byId('cockpit-error');
        const text = byId('cockpit-error-message');
        if (loading) loading.classList.add('hidden');
        if (content && !model.payload) content.classList.add('hidden');
        if (error) error.classList.remove('hidden');
        if (text) text.textContent = String(message || 'The verified snapshot could not be loaded.');
    }

    const SNAPSHOT_OLD_SECONDS = 150;

    // An old snapshot means the server is rebuilding in the background: ask
    // again in a few seconds (bounded) rather than waiting for the 30s tick.
    function scheduleFreshRetry(payload) {
        clearTimeout(model.freshRetryTimer);
        const age = ageSeconds(payload && payload.generated_at);
        if (age === null || age <= SNAPSHOT_OLD_SECONDS) {
            model.freshRetries = 0;
            return;
        }
        if ((model.freshRetries || 0) >= 6) return;
        model.freshRetries = (model.freshRetries || 0) + 1;
        model.freshRetryTimer = setTimeout(() => {
            const activeView = window.ViewRouter && window.ViewRouter.activeView;
            if (!document.hidden && (!activeView || activeView === 'home')) load();
        }, 5000);
    }

    async function load(options = {}) {
        const force = options.force === true;
        if (model.loading) return;
        setLoading(true, Boolean(model.payload));
        const params = new URLSearchParams({ unchanged: 'true' });
        if (model.version && !force) params.set('if_version', model.version);
        if (force) params.set('fresh', 'true');
        try {
            const payload = await requestJson(`/api/cockpit?${params.toString()}`, {}, 60000);
            if (payload && payload.unchanged && model.payload) {
                const updated = byId('cockpit-updated-at');
                if (updated) updated.textContent = `Snapshot ${fmtDate(model.payload.generated_at, true)} · checked ${new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' })}`;
                scheduleFreshRetry(model.payload);
            } else if (payload && payload.state === 'SUCCESS') {
                model.version = String(payload.cockpit_state_version || '');
                // Fresh server data invalidates a pending kill-switch
                // confirmation: the armed intent referred to a state that may
                // no longer match (§74 — never act on a stale intent).
                if (model.stopArmedUntil) {
                    model.stopArmedUntil = 0;
                    clearTimeout(model.stopArmTimer);
                }
                render(payload);
                const content = byId('cockpit-content');
                if (content) content.classList.remove('hidden');
                // Re-apply list caps now that the content wrapper is visible —
                // render() measured while it was still hidden (0-height rows).
                capOverviewLists(byId('cockpit-panel-overview'));
                const error = byId('cockpit-error');
                if (error) error.classList.add('hidden');
                scheduleFreshRetry(payload);
            } else {
                throw new Error('Cockpit returned an invalid state.');
            }
        } catch (error) {
            if (error && error.name === 'AbortError') showError('Cockpit request timed out. Retry when the local services are responsive.');
            else showError(error && error.message ? error.message : 'Cockpit request failed.');
        } finally {
            setLoading(false, Boolean(model.payload));
        }
    }

    async function refreshSources() {
        if (model.loading) return;
        setLoading(true, Boolean(model.payload));
        const status = byId('cockpit-status');
        if (status) status.innerHTML = '<i class="fa-solid fa-spinner fa-spin cockpit-tone--warn"></i>Refreshing Pre-Earnings source data and rebuilding the Cockpit snapshot.';
        try {
            await requestJson('/api/pre-earnings/refresh', { method: 'POST', body: '{}' }, 65000);
        } catch (error) {
            if (status) status.innerHTML = `<i class="fa-solid fa-triangle-exclamation cockpit-tone--bad"></i>${esc(error && error.message ? error.message : 'Source refresh could not be started.')}`;
            setLoading(false, Boolean(model.payload));
            return;
        }
        setLoading(false, Boolean(model.payload));
        await load({ force: true });
        if (status) status.innerHTML = '<i class="fa-solid fa-rotate cockpit-tone--warn"></i>Pre-Earnings refresh accepted. Cockpit will recheck the source while the background run completes.';
        clearTimeout(model.sourceRefreshTimer);
        model.sourceRefreshTimer = setTimeout(() => load({ force: true }), 5000);
    }

    async function toggleKillSwitch() {
        const now = Date.now();
        if (now >= model.stopArmedUntil) {
            model.stopArmedUntil = now + 5000;
            clearTimeout(model.stopArmTimer);
            model.stopArmTimer = setTimeout(() => {
                model.stopArmedUntil = 0;
                renderKillSwitch();
            }, 5100);
            renderKillSwitch();
            return;
        }
        const button = byId('cockpit-kill-switch');
        // §74 safety: never derive the target state from a possibly-stale
        // snapshot — a poll may have re-rendered between arm and confirm, and
        // flipping the wrong way would RELEASE an auto-tripped kill switch.
        // Read the live control state first, then send its explicit opposite.
        let targetActive = null;
        try {
            const control = await requestJson('/api/execution/control', { method: 'GET', mutable: true }, 10000);
            const live = control && control.control ? control.control : control;
            targetActive = !Boolean(live && live.kill_switch_active);
        } catch (fetchError) {
            const status = byId('cockpit-status');
            if (status) status.innerHTML = `<i class="fa-solid fa-triangle-exclamation cockpit-tone--bad"></i>${esc('Kill Switch state could not be verified — action aborted.')}`;
            model.stopArmedUntil = 0;
            renderKillSwitch();
            if (button) button.disabled = false;
            return;
        }
        if (button) button.disabled = true;
        try {
            const result = await requestJson('/api/execution/kill-switch', {
                method: 'POST',
                mutable: true,
                body: JSON.stringify({ active: targetActive }),
            }, 15000);
            const control = result && result.control ? result.control : null;
            if (control && model.payload && model.payload.header) {
                model.payload.header.kill_switch_active = Boolean(control.kill_switch_active);
                model.payload.header.autonomy = {
                    ...(model.payload.header.autonomy || {}),
                    ready: Boolean(control.readiness && control.readiness.ready),
                    reasons: (control.readiness && control.readiness.reasons) || [],
                };
            }
            model.stopArmedUntil = 0;
            render(model.payload);
            await load({ force: true });
        } catch (error) {
            const status = byId('cockpit-status');
            if (status) status.innerHTML = `<i class="fa-solid fa-triangle-exclamation cockpit-tone--bad"></i>${esc(error && error.message ? error.message : 'Kill Switch update failed.')}`;
        } finally {
            if (button) button.disabled = false;
            renderKillSwitch();
        }
    }

    function navigate(route) {
        if (window.ViewRouter && typeof window.ViewRouter.navigate === 'function') {
            window.ViewRouter.navigate(route);
        }
    }

    function openRiskControls() {
        navigate('agents');
        let attempts = 0;
        const timer = window.setInterval(() => {
            const button = document.querySelector('[data-testid="risk-settings-open"]');
            attempts += 1;
            if (button) {
                window.clearInterval(timer);
                button.click();
            } else if (attempts >= 30) {
                window.clearInterval(timer);
            }
        }, 100);
    }

    // POST helper for cockpit buttons. fetch() resolves on HTTP 4xx/5xx, so a
    // refused action (auth, validation, server error) used to be swallowed and
    // the button simply re-enabled as if it had worked.
    async function postAction(url, body) {
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: body === undefined ? undefined : JSON.stringify(body),
        });
        let data = null;
        try { data = await response.json(); } catch (_) { data = null; }
        if (!response.ok) {
            const detail = data && typeof data.detail === 'string' ? data.detail : `Request failed (${response.status})`;
            throw new Error(detail);
        }
        return data;
    }

    function reportActionError(error) {
        const message = error && error.message ? error.message : 'The action could not be completed.';
        model.actionError = { message, at: Date.now() };
        const status = byId('cockpit-status');
        if (status) status.innerHTML = `<i class="fa-solid fa-triangle-exclamation cockpit-tone--bad"></i>${esc(`Action failed: ${message}`)}`;
        if (window.UI && typeof window.UI.showToast === 'function') window.UI.showToast(`Action failed: ${message}`, 'error');
    }

    function handleClick(event) {
        const tab = event.target.closest('[data-cockpit-tab]');
        if (tab) { selectTab(tab.dataset.cockpitTab); return; }
        const tabOpen = event.target.closest('[data-cockpit-tab-open]');
        if (tabOpen) { selectTab(tabOpen.dataset.cockpitTabOpen); byId('ai-trading-cockpit')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); return; }
        const riskControls = event.target.closest('[data-cockpit-open-risk]');
        if (riskControls) { openRiskControls(); return; }
        const route = event.target.closest('[data-cockpit-route]');
        if (route) { navigate(route.dataset.cockpitRoute); return; }
        const gates = event.target.closest('[data-cockpit-gates]');
        if (gates) {
            const detail = byId(gates.dataset.cockpitGates);
            const ticker = String(gates.dataset.cockpitGateTicker || '').toUpperCase();
            const willExpand = gates.getAttribute('aria-expanded') !== 'true';
            gates.setAttribute('aria-expanded', String(willExpand));
            if (detail) detail.hidden = !willExpand;
            if (willExpand) model.expandedEarningsGates.add(ticker);
            else model.expandedEarningsGates.delete(ticker);
            return;
        }
        const cockpitTogglePost = (url, button) => {
            button.disabled = true;
            postAction(url)
                .then(() => null, (error) => reportActionError(error))
                .finally(() => {
                    button.disabled = false;
                    load({ force: true });
                });
        };
        const labRun = event.target.closest('[data-cockpit-lab-run]');
        if (labRun) {
            labRun.disabled = true;
            postAction('/api/strategy-lab/run')
                .then(() => null, (error) => reportActionError(error))
                .finally(() => loadAutoLab(true));
            return;
        }
        const autoCycleToggle = event.target.closest('[data-cockpit-toggle-autocycle]');
        if (autoCycleToggle) {
            cockpitTogglePost(`/api/research/auto-cycle/${autoCycleToggle.dataset.cockpitToggleAutocycle}`, autoCycleToggle);
            return;
        }
        const watchdogToggle = event.target.closest('[data-cockpit-toggle-watchdog]');
        if (watchdogToggle) {
            cockpitTogglePost(`/api/development/repair-watchdog/${watchdogToggle.dataset.cockpitToggleWatchdog}`, watchdogToggle);
            return;
        }
        const repairDetails = event.target.closest('[data-cockpit-repair-details]');
        if (repairDetails) {
            const caseId = String(repairDetails.dataset.cockpitRepairDetails || '');
            const willExpand = repairDetails.getAttribute('aria-expanded') !== 'true';
            repairDetails.setAttribute('aria-expanded', String(willExpand));
            const detailRow = byId(repairDetails.getAttribute('aria-controls'));
            if (detailRow) detailRow.hidden = !willExpand;
            repairDetails.closest('.cockpit-repair-row')?.classList.toggle('is-expanded', willExpand);
            if (willExpand) model.expandedRepairCases.add(caseId);
            else model.expandedRepairCases.delete(caseId);
            return;
        }
        const repairApprove = event.target.closest('[data-cockpit-repair-approve]');
        if (repairApprove) {
            cockpitTogglePost(`/api/development/repair-cases/${encodeURIComponent(repairApprove.dataset.cockpitRepairApprove)}/approve`, repairApprove);
            return;
        }
        const repairRunCase = event.target.closest('[data-cockpit-repair-run-case]');
        if (repairRunCase) {
            const caseId = repairRunCase.dataset.cockpitRepairRunCase;
            repairRunCase.disabled = true;
            repairRunCase.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Running…';
            postAction(`/api/development/repair-cases/${encodeURIComponent(caseId)}/run`)
                .then((data) => {
                    if (window.UI && typeof window.UI.showToast === 'function') {
                        const status = data?.case?.status || 'status not reported';
                        window.UI.showToast(`Case ${caseId} run finished: ${status}`, status === 'APPLIED' || status === 'CLOSED_NO_DEFECT' ? 'success' : 'info');
                    }
                })
                .catch((err) => {
                    if (window.UI && typeof window.UI.showToast === 'function') {
                        window.UI.showToast(`Case run failed: ${err.message}`, 'error');
                    }
                    reportActionError(err);
                })
                .finally(() => {
                    load({ force: true });
                });
            return;
        }
        const repairRun = event.target.closest('[data-cockpit-repair-run]');
        if (repairRun) {
            repairRun.disabled = true;
            repairRun.textContent = 'Running…';
            postAction('/api/development/repair-watchdog/run')
                .then(() => null, (error) => reportActionError(error))
                .finally(() => {
                    // the reload re-renders the card (fresh button + last-run note)
                    load({ force: true });
                });
            return;
        }
        const earnings = event.target.closest('[data-cockpit-earnings]');
        if (earnings) {
            const ticker = String(earnings.dataset.cockpitEarnings || '').toUpperCase();
            navigate('preearnings');
            setTimeout(() => {
                if (window.UI && typeof window.UI.selectPreEarningsRow === 'function') window.UI.selectPreEarningsRow(ticker);
            }, 120);
            return;
        }
        const ticker = event.target.closest('[data-cockpit-ticker]');
        if (ticker) {
            const symbol = String(ticker.dataset.cockpitTicker || '').toUpperCase();
            if (ticker.dataset.cockpitSource === 'PRE_EARNINGS') {
                navigate('preearnings');
                setTimeout(() => window.UI && UI.selectPreEarningsRow && UI.selectPreEarningsRow(symbol), 120);
            } else if (window.UI && typeof window.UI.openWatchlistTicker === 'function') {
                UI.openWatchlistTicker(symbol);
            } else {
                navigate('watchlist');
            }
        }
    }

    async function trainMlModel(evt) {
        if (evt) evt.preventDefault();
        const btn = evt && evt.currentTarget;
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Training ML model…';
        }
        try {
            const resp = await fetch('/api/ml/train', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ force: true })
            });
            let data = null;
            try { data = await resp.json(); } catch (_) { data = null; }
            // fetch() resolves on HTTP errors: a refused training request is an
            // error, not an informational "ML Training: ..." toast.
            if (!resp.ok || !data) throw new Error(data && typeof data.detail === 'string' ? data.detail : `Request failed (${resp.status})`);
            if (data.status === 'TRAINED') {
                if (window.UI && typeof window.UI.showToast === 'function') {
                    window.UI.showToast(`ML Model trained successfully (${data.samples} samples, test accuracy: ${((data.accuracy || 0) * 100).toFixed(1)}%)`, 'success');
                } else {
                    alert(`ML Model trained: ${data.detail}`);
                }
            } else if (data.status === 'INSUFFICIENT_DATA') {
                if (window.UI && typeof window.UI.showToast === 'function') {
                    window.UI.showToast(`Insufficient trade samples: ${data.detail}`, 'warning');
                } else {
                    alert(data.detail);
                }
            } else {
                if (window.UI && typeof window.UI.showToast === 'function') {
                    window.UI.showToast(`ML Training: ${data.detail || data.status}`, 'info');
                }
            }
            await load({ force: true });
        } catch (err) {
            console.error('ML train error:', err);
            if (window.UI && typeof window.UI.showToast === 'function') {
                window.UI.showToast(`ML Training error: ${err.message}`, 'error');
            }
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = 'Train ML model';
            }
        }
    }

    function init() {
        const root = byId('ai-trading-cockpit');
        if (!root) return;
        root.addEventListener('click', handleClick);
        byId('cockpit-refresh')?.addEventListener('click', refreshSources);
        byId('cockpit-error-retry')?.addEventListener('click', () => load({ force: true }));
        byId('cockpit-kill-switch')?.addEventListener('click', toggleKillSwitch);

        const originalNavigate = window.ViewRouter && window.ViewRouter.navigate;
        if (typeof originalNavigate === 'function' && !originalNavigate.__cockpitWrapped) {
            const wrapped = function (viewName, ...args) {
                const result = originalNavigate.call(this, viewName, ...args);
                if (viewName === 'home') load();
                return result;
            };
            wrapped.__cockpitWrapped = true;
            window.ViewRouter.navigate = wrapped;
        }

        if (!window.location.hash || window.location.hash === '#home') load();
        model.interval = setInterval(() => {
            const activeView = window.ViewRouter && window.ViewRouter.activeView;
            if (!document.hidden && (!activeView || activeView === 'home')) load();
        }, 30000);
    }

    window.CockpitUI = { load, selectTab, trainMlModel };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
    else init();
})();
