const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'frontend/app.js'), 'utf8');
const start = source.indexOf('UI.loadPreEarningsEvidenceLab = async function');
const end = source.indexOf('UI.renderPreEarningsTrackRecord = function', start);
assert(start >= 0 && end > start, 'Evidence Lab renderer must exist before the track-record renderer');

const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const box = {innerHTML: ''};
const context = {
    UI: {_escapeHtml: escapeHtml},
    API: {_warnNetworkOnce() {}, getPreEarningsLearningReport: async () => context.__report},
    document: {getElementById: (id) => (id === 'pe-evidence-lab' ? box : null)},
    Number, Array, Object, String,
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);

// Loading state before any report exists.
assert(context.UI._peEvidenceLabHtml(null).includes('Loading out-of-sample evidence'));

// Real report measured on the stored outcomes: no claim may be shown as proven.
const real = JSON.parse(fs.readFileSync(path.join(root, 'docs/product_audit_2026-09-06/learning_report_2026-09-15.json'), 'utf8'));
let html = context.UI._peEvidenceLabHtml(real);
assert(html.includes(escapeHtml(real.headline)), 'Headline must be shown verbatim');
assert(!html.includes('PROVEN EDGE'), 'No claim is proven in the real report');
assert(html.includes('NO EDGE'), 'Measured no-edge verdicts must be visible');
assert(html.includes(String(real.dataset.validated_events)), 'Validated sample size must be visible');
assert(html.includes('always LONG'), 'Baselines must be named');

// A proven claim renders as proven; hostile text is escaped.
const proven = JSON.parse(JSON.stringify(real));
proven.direction_edge_proven = true;
proven.headline = 'Edge <script>alert(1)</script>';
proven.direction_claims[0].verdict = 'EDGE_PROVEN';
proven.direction_claims[0].label = '<img src=x onerror=1>';
html = context.UI._peEvidenceLabHtml(proven);
assert(html.includes('PROVEN EDGE'));
assert(!html.includes('<script>') && !html.includes('<img src=x'), 'Report text must be escaped');

// Loader paints the container.
context.__report = real;
context.UI.loadPreEarningsEvidenceLab(true).then(() => {
    assert(box.innerHTML.includes('Evidence Lab'), 'Loader must render into #pe-evidence-lab');
    console.log('Evidence Lab: loading, real no-edge report, proven claim, escaping and loader: PASS');
});
