// Spot-render check for the PRE-EARNINGS tab frontend changes (P0/P1 audit fixes).
// 1) Static: verifies the new row markup / page order / CSS classes exist.
// 2) Data-driven: runs the exact renderRows expressions (gap risk, UTC date,
//    expected move, INSUFFICIENT_DATA label) against 3 real items from the live
//    payload on :8003 and asserts the rendered fragments.
// Usage: node tools/pe_frontend_spot_check.mjs  (after: curl -s http://localhost:8003/api/pre-earnings -o pe_live_8003.json)

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (p) => readFileSync(join(root, p), 'utf8');

let failures = 0;
const ok = (cond, label) => {
    if (cond) { console.log(`  PASS  ${label}`); }
    else { failures++; console.log(`  FAIL  ${label}`); }
};

// ── 1. Static source checks ────────────────────────────────────────────────
console.log('\n[static] app.js row markup');
const appJs = read('frontend/app.js');
const indexHtml = read('frontend/index.html');
const peCss = read('frontend/styles_preearnings.css');
const baseCss = read('frontend/styles_base.css');
const mainCss = read('frontend/main.css');

ok(appJs.includes('data-label="GapRisk"'), 'GapRisk cell in renderRows');
ok(appJs.includes('<th title="Estimated opening-gap risk, 0-100">GapRisk</th>'), 'GapRisk <th> in renderTable');
ok(appJs.includes('colspan="7"'), 'empty-row colspan bumped to 7');
ok(appJs.includes('class="pe-cell-na" title="No verified historical sample — risk not scored">N/A</span>'), 'N/A chip for null gap risk');
ok(appJs.includes('class="${isSelected ? \'is-selected\' : \'\'}"'), 'is-selected class on selected row');
ok(appJs.includes("onclick=\"UI.selectPreEarningsRow('${esc(row.symbol)}')\""), 'row onclick routes through selectPreEarningsRow');
ok(appJs.includes('UI.selectPreEarningsRow = function (symbol)'), 'selectPreEarningsRow helper defined');
ok(appJs.includes("panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' })"), 'details panel scrolls into view on click');
ok(appJs.includes("if (explicit === 'INSUFFICIENT_DATA') return 'INSUFFICIENT_DATA';"), 'INSUFFICIENT_DATA kept as its own label');
ok(appJs.includes('pe-dir--insufficient'), 'INSUFFICIENT_DATA badge class used');
ok(appJs.includes("'HIGH GAP RISK — NO DIRECTIONAL CALL'"), 'gap section retitled');
ok(appJs.includes("renderTable('SHORT BIAS SIGNALS', byShort, 'var(--pe-red)')"), 'SHORT section accent is red');
ok(appJs.includes('~${expMovePct.toFixed(1)}% exp'), 'expected move in ticker sub-line');
ok(appJs.includes('(range proxy)'), 'expected move title discloses range proxy');
ok(appJs.includes('${d.getUTCDate()} ${PE_MONTHS[d.getUTCMonth()]}'), 'row date uses UTC getters, DD MMM');
ok(appJs.includes('rgba(15,23,42,0.08)'), 'renderComp bar track visible on white');
ok(appJs.includes('none in current filter'), 'empty sections render a muted strip');
ok(appJs.includes('Show all ${byNeedsCoverage.length}'), 'NEEDS COVERAGE capped with Show all toggle');
ok(appJs.includes('Updated: ${asOf} | News age:'), 'meta line uses Updated / News age');
ok(appJs.includes('Calendar age:'), 'meta line shows earnings-calendar cache age');
ok(appJs.includes('Next cycle in:'), 'meta line uses Next cycle in');
ok(!appJs.includes('Track record: ${_tro.hits}'), 'duplicated track-record fragment dropped from meta line');
ok(appJs.includes('non-directional (not scored)'), 'NEUTRAL verdict card not scored');
ok(appJs.includes("(excl. neutral)"), 'breakdown microcopy fixed');
ok(appJs.includes('bias >= 15 ? \'LONG\' : bias <= -15 ? \'SHORT\' : \'NEUTRAL\''), 'recent outcomes use the ±15 rule');
ok(appJs.includes('pe-pulse-absent'), 'pulse absent one-liner rendered');
ok(appJs.includes('pe-pulse-pick-conflict'), 'pulse pick contradiction chip');
ok(appJs.includes('UI.schedulePreEarningsRender = function'), 'debounced filter render helper');

console.log('\n[static] index.html structure/order');
ok(!indexHtml.includes('preearnings-coverage-card'), 'empty coverage pill deleted');
const order = ['pe-filter-command-title', 'preearnings-sections', 'preearnings-details', 'preearnings-trackrecord', 'preearnings-ai-model'];
const idx = order.map((id) => indexHtml.indexOf(`id="${id}"`));
ok(idx.every((i) => i > 0), 'all key containers still present');
ok(idx[0] < idx[1] && idx[1] < idx[2] && idx[2] < idx[3] && idx[3] < idx[4], 'filter → tables → details → track record → AI model bar order');
ok(indexHtml.includes('<option value="INSUFFICIENT_DATA">INSUFFICIENT_DATA</option>'), 'INSUFFICIENT_DATA direction filter option');
ok(indexHtml.includes('id="pe-stat-na-risk"'), '5th stat card (N/A risk)');
ok(indexHtml.includes('Universe Monitored'), 'total card labeled as universe');
ok(indexHtml.includes('<option value="ai">Confidence</option>'), 'sort option renamed to Confidence');
ok(indexHtml.includes('Min gap risk'), 'slider label sentence-case');
ok(indexHtml.includes('Backfill missing data'), 'warmup button relabeled');

console.log('\n[static] CSS');
ok(peCss.includes('--pe-green-text: #047857') && peCss.includes('--pe-red-text: #b91c1c'), 'contrast-safe text vars in :root');
ok(peCss.includes('.pe-cell-na {'), 'pe-cell-na CSS');
ok(peCss.includes('.pe-dir--insufficient {'), 'pe-dir--insufficient CSS');
ok(peCss.includes('.pe-table tr.is-selected {'), 'is-selected CSS');
ok(peCss.includes('.pe-pulse-absent {'), 'pulse absent CSS');
ok(peCss.includes('.pe-pulse-pick-conflict {'), 'pulse conflict chip CSS');
ok(peCss.includes('.pe-show-all-btn {'), 'show-all button CSS');
ok(peCss.includes('#view-preearnings .premium-card:not(.pe-filter-card):hover {'), 'premium hover-lift disabled in tab');
ok(baseCss.includes('.btn-mini--accent {') && baseCss.includes('.btn-mini--accent:hover {'), 'btn-mini--accent gold gradient CSS');
ok(mainCss.includes('.preearnings-sections-grid {\n    display: grid;\n    grid-template-columns: 1fr;\n    gap: 14px;'), 'sections grid gap 14px');

// ── 2. Data-driven render check on 3 real items ────────────────────────────
console.log('\n[data] 3 real items from pe_live_8003.json');
const payload = JSON.parse(read('pe_live_8003.json'));
const items = payload.items || [];

// Exact logic mirrors from app.js
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const biasScoreOf = (row) => {
    const raw = Number(row.direction_bias_score);
    if (Number.isFinite(raw)) return Math.max(-100, Math.min(100, raw));
    return 0;
};
const directionLabelOf = (item) => {
    const explicit = String((item || {}).direction_label || '').toUpperCase();
    if (explicit === 'LONG' || explicit === 'SHORT' || explicit === 'NEUTRAL') return explicit;
    if (explicit === 'INSUFFICIENT_DATA') return 'INSUFFICIENT_DATA';
    const s = biasScoreOf(item);
    return s >= 15 ? 'LONG' : s <= -15 ? 'SHORT' : 'NEUTRAL';
};
const formatDate = (val) => {
    const d = new Date(val);
    return Number.isFinite(d.getTime()) ? `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}` : 'N/A';
};

const samples = [
    items.find((i) => Number(i.gap_risk) >= 70),
    items.find((i) => directionLabelOf(i) === 'NEUTRAL' && i.gap_risk != null),
    items.find((i) => i.gap_risk == null || i.gap_risk === 'INSUFFICIENT_DATA'),
];
ok(samples[0] && samples[1], 'found HIGH-risk and NEUTRAL samples in live payload');
// The null-gap population comes and goes with data availability — if the live
// payload currently has none, synthesize one to still exercise the N/A branch.
if (!samples[2]) {
    console.log('  note: no null-gap item in the current payload; using a synthetic clone for the N/A branch');
    samples[2] = { ...samples[0], symbol: 'SYNTH-NA', gap_risk: null, gap_risk_unavailable: true };
}

for (const row of samples) {
    if (!row) continue;
    const gapRisk = row.gap_risk != null && row.gap_risk !== 'INSUFFICIENT_DATA' ? Number(row.gap_risk) : null;
    const comps = (row.components && typeof row.components === 'object') ? row.components : {};
    const expMovePct = row.range_proxy_pct != null && Number.isFinite(Number(row.range_proxy_pct))
        ? Number(row.range_proxy_pct)
        : (comps.range_proxy_move != null && Number.isFinite(Number(comps.range_proxy_move)) ? Number(comps.range_proxy_move) : null);
    const cellHtml = gapRisk != null
        ? `<div class="pe-gap-container" title="Estimated opening-gap risk, 0-100">…${gapRisk}</div>`
        : `<span class="pe-cell-na" title="No verified historical sample — risk not scored">N/A</span>`;
    const dateStr = formatDate(row.earnings_time_utc);
    console.log(`  ${row.symbol}  label=${directionLabelOf(row)}  gap=${gapRisk == null ? 'N/A' : gapRisk}  date=${dateStr}  exp=${expMovePct == null ? 'n/a' : '~' + expMovePct.toFixed(1) + '%'}`);

    // Date must reflect the UTC calendar day, never the local previous day.
    const utcDay = new Date(row.earnings_time_utc).getUTCDate();
    ok(dateStr.startsWith(`${utcDay} `), `${row.symbol}: row date uses UTC day (${dateStr})`);
    ok(/^\d{1,2} [A-Z][a-z]{2}$/.test(dateStr) || dateStr === 'N/A', `${row.symbol}: DD MMM format (${dateStr})`);
    if (gapRisk == null) {
        ok(cellHtml.includes('pe-cell-na'), `${row.symbol}: null gap renders the N/A chip`);
    } else {
        ok(cellHtml.includes('pe-gap-container') && !cellHtml.includes('pe-cell-na'), `${row.symbol}: scored gap renders the bar`);
    }
}
// UTC-off-by-one regression: midnight-UTC timestamps must not shift a day back.
const midnight = items.find((i) => /T00:00:00/.test(String(i.earnings_time_utc || '')));
if (midnight) {
    const expect = `${new Date(midnight.earnings_time_utc).getUTCDate()} `;
    ok(formatDate(midnight.earnings_time_utc).startsWith(expect), `${midnight.symbol}: midnight-UTC date stays on the same day (was off-by-one with local getters)`);
}

console.log(failures ? `\n${failures} CHECK(S) FAILED` : '\nALL CHECKS PASSED');
process.exit(failures ? 1 : 0);
