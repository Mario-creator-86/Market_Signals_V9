/* ==================== HotList Ticker Dossier (2026-09-03 rebuild) ==================== */
/* Master-detail intelligence: heat strip (curated + scanner) and a per-ticker
   dossier that aggregates everything the system knows — quote, news, our own
   trades, AI decision history, pre-earnings context — plus on-demand deep
   analysis (TradingAgents-style bull/bear debate judged by a manager). */

(function () {
    'use strict';

    const esc = (v) => String(v == null ? '' : v)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

    const finite = (v) => (v === null || v === undefined || Number.isNaN(Number(v)) ? null : Number(v));

    function pctText(v, digits = 2) {
        const n = finite(v);
        return n === null ? '—' : `${n >= 0 ? '+' : ''}${n.toFixed(digits)}%`;
    }

    function ageLabel(iso) {
        if (!iso) return null;
        try {
            const then = new Date(iso).getTime();
            if (Number.isNaN(then)) return null;
            const mins = Math.max(0, (Date.now() - then) / 60000);
            if (mins < 60) return `${Math.round(mins)}m`;
            if (mins < 48 * 60) return `${Math.round(mins / 60)}h`;
            return `${Math.round(mins / 1440)}d`;
        } catch (e) { return null; }
    }

    /* ---------------- heat strip ---------------- */

    // renderHotList re-runs on every state poll; without these guards the strip
    // reset to "Loading" and re-fired the fetch on each poll (2026-09-03 live test).
    let heatInflight = null;
    let heatCache = { at: 0, data: null };
    let heatFailure = { at: 0, message: '' };
    const HEAT_FRESH_MS = 60000;
    // After a failed load the strip says so and waits before retrying. It used
    // to be reset to the "Loading market heat..." spinner by every state poll
    // (each one re-fired the request), so a slow or failing endpoint looked
    // like a load that never ends (tab audit 2026-11-15).
    const HEAT_RETRY_MS = 30000;

    function renderHeatFailure(strip) {
        const waitS = Math.max(1, Math.ceil((HEAT_RETRY_MS - (Date.now() - heatFailure.at)) / 1000));
        strip.innerHTML = `<div class="hl-heat-loading hl-heat-failed"><span class="ds-chip ds-chip--warn">Unavailable</span><span>Market heat unavailable (${esc(heatFailure.message || 'no response')}) — retrying in about ${waitS}s.</span><button type="button" class="hl-heat-retry ds-btn ds-btn--sm" onclick="UI.loadHotlistHeat({ force: true })">Retry now</button></div>`;
    }

    UI.loadHotlistHeat = function (options) {
        const force = Boolean(options && options.force);
        const strip = document.getElementById('hotlist-heat-strip');
        if (!strip) return Promise.resolve(null);
        if (heatInflight) return heatInflight; // never stack fetches
        if (!force && heatCache.data && Date.now() - heatCache.at < HEAT_FRESH_MS) {
            UI.renderHotlistHeat(heatCache.data);
            return Promise.resolve(heatCache.data);
        }
        if (!force && !heatCache.data && heatFailure.at && Date.now() - heatFailure.at < HEAT_RETRY_MS) {
            renderHeatFailure(strip);
            return Promise.resolve(null);
        }
        if (heatCache.data) {
            try { UI.renderHotlistHeat(heatCache.data); } catch (e) { console.warn('heat render failed', e); } // keep stale visible
        } else {
            strip.innerHTML = `<div class="hl-heat-loading">Loading market heat…</div>`;
        }
        heatInflight = (async () => {
            try {
                const res = await API.fetchWithTimeout('/api/hotlist/heat', { timeout: 45000 }); // cold first-ever build measured 27.9s (audit 2026-09-05)
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                if (data && data.error) throw new Error(String(data.error));
                UI.renderHotlistHeat(data); // render BEFORE caching: a payload that cannot be drawn must not be kept
                heatCache = { at: Date.now(), data };
                heatFailure = { at: 0, message: '' };
                return data;
            } catch (err) {
                heatFailure = { at: Date.now(), message: String((err && err.message) || err) };
                const liveStrip = document.getElementById('hotlist-heat-strip');
                if (liveStrip && !heatCache.data) renderHeatFailure(liveStrip);
                return null;
            } finally {
                heatInflight = null;
            }
        })();
        return heatInflight;
    };

    UI.renderHotlistHeat = function (data) {
        const strip = document.getElementById('hotlist-heat-strip');
        if (!strip || !data) return;
            const heat = (data.heat || []).slice(0, 18);
            const curated = data.curated || [];
            // One strip, grouped by the kind of heat (the first scanner category
            // of each row); any further categories stay in the chip's tooltip.
            const kindLabel = (c) => {
                const words = String(c || 'heat').replaceAll('_', ' ');
                return words.charAt(0).toUpperCase() + words.slice(1);
            };
            const KIND_ORDER = ['trending', 'volume_leaders', 'gainers', 'losers', 'most_active'];
            const groups = [];
            heat.forEach((row) => {
                const kind = String((row.categories || [])[0] || 'heat');
                let group = groups.find((g) => g.kind === kind);
                if (!group) { group = { kind, rows: [] }; groups.push(group); }
                group.rows.push(row);
            });
            const kindRank = (g) => { const i = KIND_ORDER.indexOf(g.kind); return i === -1 ? KIND_ORDER.length : i; };
            groups.sort((a, b) => kindRank(a) - kindRank(b));
            const chipHtml = (row) => {
                const chg = finite(row.change_pct);
                const cls = chg === null ? 'muted' : (chg >= 0 ? 'up' : 'down');
                const sign = chg !== null && chg >= 0 ? '+' : '';
                const cats = (row.categories || []).map((c) => String(c).replaceAll('_', ' ')).join(', ');
                return `<button type="button" class="hl-heat-chip" onclick="UI.openTickerDossier('${esc(row.ticker)}')" title="Open ${esc(row.ticker)} dossier${cats ? ' — ' + esc(cats) : ''}">
                        <span class="hl-heat-ticker">${esc(row.ticker)}</span>
                        <span class="hl-heat-chg ${cls}">${chg !== null ? sign + chg.toFixed(2) + '%' : 'n/a'}</span>
                </button>`;
            };
            const chips = groups.map((g) => `<div class="hl-heat-group">
                    <span class="hl-heat-kind">${esc(kindLabel(g.kind))}</span>
                    <div class="hl-heat-group-chips">${g.rows.map(chipHtml).join('')}</div>
                </div>`).join('');
            // Only mention the build time once it is old enough to matter.
            const asOfMs = data.as_of ? Date.parse(data.as_of) : NaN;
            const asOfAge = (Number.isFinite(asOfMs) && Date.now() - asOfMs > 180000) ? ageLabel(data.as_of) : null;
            const curatedNote = curated.length
                ? `<span class="hl-heat-curated">${curated.length} in your hot list</span>`
                : `<span class="hl-heat-curated muted">Your hot list is empty — add tickers from the Watchlist table. Market heat comes from the scanner.</span>`;
            strip.innerHTML = `
                <div class="hl-heat-head">
                    <span class="hl-heat-title">Market heat</span>
                    ${curatedNote}
                    ${data.scanner_available === false ? '<span class="ds-chip ds-chip--warn">Scanner warming up</span>' : ''}
                    ${asOfAge ? `<span class="hl-heat-curated muted" title="When this heat list was last rebuilt; change % is the move at scan time.">updated ${esc(asOfAge)} ago</span>` : ''}
                    <span class="hl-heat-curated muted hl-heat-hint">Change % is the move at scan time</span>
                </div>
                <div class="hl-heat-chips">${chips || '<span class="hl-heat-curated muted">No heat right now.</span>'}</div>`;
    };

    /* ---------------- dossier overlay ---------------- */

    UI.openTickerDossier = async function (ticker) {
        if (!ticker) return;
        let overlay = document.getElementById('hl-dossier-overlay');
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.id = 'hl-dossier-overlay';
            overlay.className = 'hl-dossier-overlay';
            overlay.innerHTML = `
                <div class="hl-dossier" role="dialog" aria-label="Ticker dossier">
                    <div class="hl-dossier-head" id="hl-dossier-head"></div>
                    <div class="hl-dossier-body" id="hl-dossier-body"></div>
                </div>`;
            overlay.addEventListener('click', (e) => { if (e.target === overlay) UI.closeTickerDossier(); });
            document.body.appendChild(overlay);
        }
        overlay.classList.remove('hidden');
        document.body.classList.add('hl-dossier-open');
        const setHead = (html) => { document.getElementById('hl-dossier-head').innerHTML = html; };
        setHead(`
            <div class="hl-dossier-title">
                <strong class="hl-dossier-ticker">${esc(ticker)}</strong> <span class="hl-dossier-sub">Dossier</span></div>
            <button type="button" class="hl-dossier-close ds-btn ds-btn--ghost" onclick="UI.closeTickerDossier()" title="Close" aria-label="Close dossier"><i class="fa-solid fa-xmark"></i></button>`);
        document.getElementById('hl-dossier-body').innerHTML = `
            <div class="hl-dossier-loading">Gathering everything we know about ${esc(ticker)}… an uncached dossier can take up to a minute.</div>`;
        try {
            const res = await API.fetchWithTimeout(`/api/hotlist/ticker/${encodeURIComponent(ticker)}/dossier`, { timeout: 90000 }); // uncached builds measured 31-52s under load (audit 2026-09-05)
            if (!res.ok) throw new Error(`dossier unavailable (HTTP ${res.status})`);
            const data = await res.json();
            // A failed build answers 200 {available:false, error}: rendering it
            // printed "We never traded this ticker" / "No news" as if verified.
            if (!data || data.error || data.available === false) {
                throw new Error(String((data && data.error) || 'empty response').replaceAll('_', ' '));
            }
            UI.renderTickerDossier(data);
        } catch (err) {
            document.getElementById('hl-dossier-body').innerHTML =
                `<div class="hl-dossier-loading hl-dossier-failed">
                    <p class="hl-note-warn">Dossier unavailable: ${esc(String((err && err.message) || err))}</p>
                    <button type="button" class="ds-btn ds-btn--sm" onclick="UI.openTickerDossier('${esc(ticker)}')">Retry</button>
                </div>`;
        }
    };

    UI.closeTickerDossier = function () {
        const overlay = document.getElementById('hl-dossier-overlay');
        if (overlay) overlay.classList.add('hidden');
        document.body.classList.remove('hl-dossier-open');
    };

    /* ---------------- dossier rendering ---------------- */

    // Section titles carry no icon: the title is the label. `badge` is where a
    // count or the "AI" marker (model output, not a measurement) goes.
    function section(title, innerHtml, badge) {
        return `<section class="hl-dossier-section">
            <h4>${esc(title)} ${badge || ''}</h4>
            ${innerHtml}
        </section>`;
    }
    const AI_CHIP = '<span class="ds-chip ds-chip--ai" title="Produced by a language model, not measured.">AI</span>';

    UI.renderTickerDossier = function (data) {
        const body = document.getElementById('hl-dossier-body');
        if (!body) return;
        const t = data.ticker || '';
        const q = data.quote || {};
        const chg = finite(q.change_pct);
        const priceValid = finite(q.price) !== null && Number(q.price) > 0;
        const cats = ((data.hot_context || {}).categories || []);
        const quoteAge = ageLabel(q.price_as_of);
        const quoteChip = !priceValid ? '' : (q.price_stale
            ? `<span class="ds-chip ds-chip--warn" title="This quote is out of date: the market has traded since it was taken.">STALE${quoteAge ? ' · ' + esc(quoteAge) + ' old' : ' · age unknown'}</span>`
            : (quoteAge ? `<span class="hl-age" title="Time since this quote was taken${q.price_source ? ' (' + esc(String(q.price_source)) + ')' : ''}.">${esc(quoteAge)} ago</span>` : ''));
        const techNote = q.technicals_stale
            ? ` <span class="hl-stale-note">(trend snapshot is stale${ageLabel(q.technicals_as_of) ? ', ' + esc(ageLabel(q.technicals_as_of)) + ' old' : ''})</span>`
            : '';

        const head = document.getElementById('hl-dossier-head');
        if (head) {
            head.innerHTML = `
                <div class="hl-dossier-title">
                    <strong class="hl-dossier-ticker">${esc(t)}</strong>
                    ${priceValid ? `<span class="hl-dossier-price">$${UI.formatPrice(q.price)}</span>` : ''}
                    ${chg !== null ? `<span class="hl-dossier-chg ${q.price_stale ? 'muted' : (chg >= 0 ? 'up' : 'down')}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span>` : ''}
                    ${quoteChip}
                    ${data.in_curated_hotlist ? '<span class="hl-badge hot ds-chip">In hot list</span>' : ''}
                    ${((data.trades || {}).traded) ? '<span class="hl-badge traded ds-chip ds-chip--info">Traded by us</span>' : ''}
                </div>
                <button type="button" class="hl-dossier-close ds-btn ds-btn--ghost" onclick="UI.closeTickerDossier()" title="Close" aria-label="Close dossier"><i class="fa-solid fa-xmark"></i></button>`;
        }

        /* Why it's hot — honest contradiction handling (owner 2026-09-03:
           "apare la losers dar trend 1d BULL?"). The category is a scanner-
           snapshot fact (change at scan time), the trend is the daily
           structure. Both can be true at different horizons — show both,
           plus the live change, and flag the mismatch explicitly. */
        const catsHtml = cats.length
            ? `Appears in <strong>${esc(cats.map((c) => String(c).replaceAll('_', ' ')).join(', '))}</strong>`
            : '';
        const chgNow = chg;
        const contradiction = cats.includes('losers') && chgNow !== null && chgNow >= 0;
        const contradictionNote = contradiction
            ? `<div class="hl-note-warn">Categorised <strong>losers</strong> at scanner time, but the live change is <strong>${pctText(chgNow)}</strong> — the category describes the scan snapshot (intraday move), the 1D trend describes the daily structure. Different horizons, both real.</div>`
            : '';
        const heatHtml = (cats.length || chg !== null)
            ? `<p>${catsHtml ? catsHtml + '. ' : ''}${q.market_regime ? `Regime: <strong>${esc(String(q.market_regime))}</strong>. ` : ''}${q.trend_1d ? `1D trend <strong>${esc(String(q.trend_1d))}</strong>` : ''}${q.trend_4h ? ` · 4H ${esc(String(q.trend_4h))}` : ''}${techNote}.</p>${contradictionNote}`
            : '<p class="muted">Not in current scanner heat.</p>';

        /* Deep analysis */
        const da = data.deep_analysis || {};
        const verdictBadge = (v) => {
            const rec = String(v || '').toUpperCase();
            const tone = { APPROVE: 'ds-chip--pos', REDUCE: 'ds-chip--warn', WATCH: 'ds-chip--warn', AVOID: 'ds-chip--neg' }[rec] || '';
            return `<span class="ds-chip hl-deep-rec ${tone}">${esc(rec || '—')}</span>`;
        };
        let deepHtml;
        if (da && da.available) {
            const v = da.verdict || {};
            const conf = finite(v.confidence);
            const ageH = da.age_seconds != null ? Math.round(da.age_seconds / 360) / 10 : null;
            deepHtml = `
                <div class="hl-deep-verdict"><span class="hl-deep-k">Verdict</span>${verdictBadge(v.recommendation)}
                    ${conf !== null ? `<span class="hl-deep-conf">Model confidence ${Math.round(conf <= 1 ? conf * 100 : conf)}%</span>` : ''}
                    <span class="hl-deep-meta">bull ${esc(String(v.bull_score ?? '—'))}/10 · bear ${esc(String(v.bear_score ?? '—'))}/10${da.models && da.models.manager ? ` · judge ${esc(da.models.manager)}` : ''}</span></div>
                <div class="hl-deep-generated">Generated <strong>${esc(String(da.generated_at || '').slice(0, 16).replace('T', ' '))} UTC</strong>${ageH !== null ? ` · ${da.fresh ? 'fresh (<1h)' : '~' + ageH + 'h ago'}` : ''}${da.cached ? ' · cached' : ''} · frame: ${esc(String(da.direction_frame || '—'))}</div>
                ${v.rationale ? `<p class="hl-deep-rationale">${esc(v.rationale)}</p>` : ''}
                <details class="hl-deep-args"><summary>Bull vs bear arguments</summary>
                    <div class="hl-deep-bull"><strong>Bull case</strong> ${esc(da.bull || '—')}</div>
                    <div class="hl-deep-bear"><strong>Bear case</strong> ${esc(da.bear || '—')}</div>
                </details>`;
        } else if (da && (da.error || da.generated_at)) {
            // A failed run was stored but rendered as "No deep analysis yet":
            // the button looked like it had done nothing.
            deepHtml = `<p class="hl-note-warn">The last deep analysis did not complete${da.error ? `: <strong>${esc(String(da.error).replaceAll('_', ' '))}</strong>` : ''}${da.generated_at ? ` (${esc(String(da.generated_at).slice(0, 16).replace('T', ' '))} UTC)` : ''}. No verdict is available — run it again.</p>`;
        } else {
            deepHtml = `<p class="muted">No deep analysis yet. Run the AI debate (2 LLM researchers + a manager judge) on this ticker's current evidence.</p>`;
        }
        const deepFailed = !(da && da.available) && Boolean(da && (da.error || da.generated_at));
        const deepSection = section('Deep analysis (AI debate)', `
            ${deepHtml}
            <button type="button" class="hl-deep-btn ds-btn ds-btn--sm" id="hl-deep-btn" onclick="UI.runHotlistDeepAnalysis('${esc(t)}')">
                ${da && da.available ? 'Re-run deep analysis' : (deepFailed ? 'Retry deep analysis' : 'Run deep analysis')}
            </button>`, AI_CHIP);

        // A section the server could not build says so (with its reason) and
        // offers a retry, instead of reading like a verified "nothing here".
        const failNote = (what, reason) => `<p class="hl-note-warn">${what}${reason ? `: <strong>${esc(String(reason).replaceAll('_', ' '))}</strong>` : ''}.</p>
                <button type="button" class="ds-btn ds-btn--sm" onclick="UI.openTickerDossier('${esc(t)}')">Retry</button>`;

        /* News */
        const news = data.news || {};
        const newsHtml = news.available
            ? `<ul class="hl-news-list">${(news.articles || []).map((a) => {
                const age = ageLabel(a.published_at);
                return `<li><span class="hl-news-title">${esc(a.title)}</span>
                <span class="hl-news-meta">${esc(a.source || '—')}${age ? ` · ${esc(age)} ago` : ''}${a.sentiment ? ` · ${esc(String(a.sentiment))}` : ''}</span></li>`;
              }).join('')}</ul>
                <p class="muted hl-count">${news.count} articles in our bucket</p>`
            : (news.reason
                ? failNote('News could not be loaded', news.reason)
                : '<p class="muted">No news in our bucket for this ticker.</p>');

        /* Trades */
        const tr = data.trades || {};
        const totals = tr.totals || {};
        let tradesHtml;
        if (!tr.available) {
            tradesHtml = failNote('Trade store unavailable — our positions and history for this ticker could not be read', tr.reason);
        } else if (!tr.traded) {
            tradesHtml = '<p class="muted">We never traded this ticker — no open position, no history.</p>';
        } else {
            const openRows = (tr.open_positions || []).map((p) => `
                <tr><td>${esc(String(p.strategy_type || '').replaceAll('_', ' '))}</td><td>${esc(String(p.direction))}</td>
                <td class="num">${p.entry_price != null ? '$' + Number(p.entry_price).toFixed(2) : '—'}</td><td class="num muted">running</td></tr>`).join('');
            const closedRows = (tr.closed_trades || []).slice(0, 8).map((p) => {
                const pnl = finite(p.pnl_pct);
                const cls = pnl === null ? 'muted' : (pnl > 0 ? 'good' : 'bad');
                return `<tr><td>${esc(String(p.strategy_type || '').replaceAll('_', ' '))}</td><td>${esc(String(p.direction))}</td>
                <td class="num ${cls}">${pctText(p.pnl_pct)}</td>
                <td>${esc(String(p.close_reason || '').replaceAll('_', ' ') || '—')}</td>
                <td class="num muted">${esc(String((p.closed_at || '').slice(0, 10)))}</td></tr>`;
            }).join('');
            const stratRows = (tr.strategies || []).map((s) => `
                <span class="hl-strat-chip">${esc(String(s.strategy).replaceAll('_', ' '))}: ${s.n} trades · ${s.win_rate_pct}% win · avg ${pctText(s.avg_pnl_pct)}</span>`).join('');
            tradesHtml = `
                ${totals.trades ? `<p class="hl-totals">Realized: <strong>${totals.trades} trades</strong>, win rate <strong>${totals.win_rate_pct}%</strong>, avg <strong>${pctText(totals.avg_pnl_pct)}</strong>, total <strong>${pctText(totals.total_pnl_pct)}</strong>${totals.best_pnl_pct != null ? ` (best ${pctText(totals.best_pnl_pct, 1)} / worst ${pctText(totals.worst_pnl_pct, 1)})` : ''}</p>` : ''}
                ${stratRows ? `<div class="hl-strat-chips">${stratRows}</div>` : ''}
                ${openRows ? `<div class="hl-table-wrap"><table class="hl-table"><thead><tr><th>Open position</th><th>Direction</th><th class="num">Entry</th><th class="num">P&amp;L</th></tr></thead><tbody>${openRows}</tbody></table></div>` : ''}
                ${closedRows ? `<div class="hl-table-wrap"><table class="hl-table"><thead><tr><th>Closed trade</th><th>Direction</th><th class="num">P&amp;L</th><th>Exit</th><th class="num">Date</th></tr></thead><tbody>${closedRows}</tbody></table></div>` : ''}`;
        }

        /* AI decisions — with the REASON (owner: "avem rejected, dar de ce?
           Trebuie sa pot sa vad asta!") */
        const dec = data.decisions || {};
        const decHtml = dec.available && (dec.decisions || []).length
            ? `<div class="hl-table-wrap"><table class="hl-table hl-dec-table"><thead><tr><th>When (UTC)</th><th>Status</th><th>Action</th><th>Model</th><th class="num">Conf.</th></tr></thead><tbody>${(dec.decisions || []).map((d) => {
                const reason = String(d.reason || '').trim();
                const reasonHtml = reason
                    ? `<details class="hl-dec-reason"><summary>Why?</summary><div>${esc(reason)}</div></details>`
                    : '';
                const extras = `${d.debate_verdict ? `<span class="hl-badge debate ds-chip ds-chip--ai">Debate: ${esc(String(d.debate_verdict))}</span>` : ''}
                    ${d.afterward ? `<span class="hl-badge afterward ds-chip">${esc(String(d.afterward))}</span>` : ''}
                    ${reasonHtml}`;
                const status = String(d.status || '').toLowerCase();
                return `<tr class="hl-dec-row${extras.trim() ? ' has-extra' : ''}">
                    <td class="hl-when">${esc(String((d.timestamp || '').slice(0, 16)).replace('T', ' ')) || '—'}</td>
                    <td><span class="hl-dec-status ds-chip ${esc(status)}">${esc(status ? status.charAt(0).toUpperCase() + status.slice(1) : '—')}</span></td>
                    <td>${esc(String(d.action || '—').replaceAll('_', ' '))}</td>
                    <td class="muted">${d.model ? esc(d.model) : '—'}</td>
                    <td class="num">${d.confidence != null && finite(d.confidence) !== null ? `${Math.round((finite(d.confidence) <= 1 ? finite(d.confidence) * 100 : finite(d.confidence)))}%` : '<span class="muted">—</span>'}</td>
                </tr>${extras.trim() ? `<tr class="hl-dec-extra"><td colspan="5">${extras}</td></tr>` : ''}`;
              }).join('')}</tbody></table></div>`
            : (dec.reason
                ? failNote('The AI decision ledger could not be read', dec.reason)
                : '<p class="muted">No AI decisions recorded for this ticker.</p>');

        /* Pre-earnings — show the watchlist-known date even outside the
           scoring universe (owner: "stim cand raporteaza, de ce nu avem
           data aici?") */
        const pe = data.pre_earnings || {};
        let peHtml;
        const peDays = finite(pe.days_until);
        const pePast = pe.is_past === true || (peDays !== null && peDays < 0);
        if (pe.available && (pe.source === 'watchlist_state' || pe.source === 'last_known') && pePast) {
            // "Next earnings: 2026-11-03 · in -12 days" (tab audit 2026-11-15).
            peHtml = `<p>Last earnings: <strong>${esc(String(pe.earnings_date || '—'))}</strong>${pe.earnings_when ? ` (${esc(String(pe.earnings_when))})` : ''}${peDays !== null ? ` · <strong>${esc(String(Math.abs(peDays)))} days ago</strong>` : ''}.</p>
                <p class="muted">The next earnings date has not been announced yet.</p>`;
        } else if (pe.available && (pe.source === 'watchlist_state' || pe.source === 'last_known')) {
            peHtml = `<p>Next earnings: <strong>${esc(String(pe.earnings_date || '—'))}</strong>${pe.earnings_when ? ` (${esc(String(pe.earnings_when))})` : ''}${peDays !== null ? (peDays === 0 ? ' · <strong>today</strong>' : ` · in <strong>${esc(String(peDays))} day${peDays === 1 ? '' : 's'}</strong>`) : ''}.</p>
                <p class="muted">Outside the pre-earnings scoring universe — date from ${pe.source === 'last_known' ? 'the last value the watchlist reported (the calendar has no entry right now)' : 'your watchlist state'}.</p>`;
        } else if (pe.available) {
            peHtml = `<p><strong>${esc(String(pe.earnings_time_utc || '').slice(0, 16).replace('T', ' '))} UTC</strong> (${esc(String(pe.session || '?'))}) · gap risk ${esc(String(pe.gap_risk ?? '—'))} (${esc(String(pe.gap_risk_level || '?'))}) · bias ${esc(String(pe.direction_label || '—'))}</p>`;
        } else {
            peHtml = '<p class="muted">No upcoming earnings event tracked.</p>';
        }

        body.innerHTML = `
            ${section('Why it is hot', heatHtml)}
            ${deepSection}
            ${section('News', newsHtml, news.available ? `<span class="hl-badge hl-count-chip ds-chip">${news.count}</span>` : '')}
            ${section('Our trades', tradesHtml)}
            ${section('AI decision history', decHtml, AI_CHIP)}
            ${section('Earnings context', peHtml)}
            <p class="hl-dossier-asof">Snapshot ${esc(String(data.as_of || '').slice(0, 19).replace('T', ' '))} UTC</p>`;
    };

    /* ---------------- deep analysis action ---------------- */

    UI.runHotlistDeepAnalysis = async function (ticker) {
        const btn = document.getElementById('hl-deep-btn');
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = 'Debating bull vs bear… (up to ~60s)';
        }
        try {
            const res = await API.fetchWithTimeout(`/api/hotlist/ticker/${encodeURIComponent(ticker)}/deep-analysis`, {
                method: 'POST', timeout: 120000,
            });
            if (!res.ok) throw new Error(`deep analysis failed (HTTP ${res.status})`);
            const result = await res.json();
            if (result && result.available === false && typeof UI.showToast === 'function') {
                UI.showToast(`Deep analysis for ${ticker} did not complete${result.error ? ': ' + String(result.error).replaceAll('_', ' ') : ''}`, 'error');
            }
        } catch (err) {
            console.warn('deep analysis failed:', err);
            if (typeof UI.showToast === 'function') {
                UI.showToast(`Deep analysis for ${ticker} failed: ${String((err && err.message) || err)}`, 'error');
            }
        }
        UI.openTickerDossier(ticker); // re-render with the fresh analysis
    };
})();
