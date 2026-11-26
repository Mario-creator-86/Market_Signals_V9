// The "N new stories" notification must lead somewhere: the News tab's "New" focus.
// Runs the real code from frontend/app.js in a vm with a minimal fake DOM.
//   node tests/frontend/news_new_arrivals.cjs
const fs = require('fs'), vm = require('vm'), path = require('path'), assert = require('assert/strict');
const root = path.resolve(__dirname, '../../frontend');
const source = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');

const start = source.indexOf('// Stories announced by the "N new stories" notification');
const end = source.indexOf('UI.setNewsFocusMode = function', start);
assert(start >= 0 && end > start, 'new-arrivals block not found in app.js');

const badge = { textContent: '', hidden: true };
const calls = [];
const context = {
    UI: { setNewsFocusMode: (mode) => calls.push(['focus', mode]) },
    ViewRouter: { activeView: 'home', navigate: (view) => { calls.push(['navigate', view]); context.ViewRouter.activeView = view; } },
    document: { getElementById: (id) => (id === 'news-new-count' ? badge : null) },
    Date, Map, Array, String,
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
const UI = context.UI;

// 1. arrivals are remembered once, by article id, and counted on the badge
UI.rememberNewNews([{ id: 'a1', title: 'One' }, { id: 'a2', title: 'Two' }, { title: 'no id' }]);
UI.rememberNewNews([{ id: 'a1', title: 'One again' }]);
assert.equal(UI._newNewsArrivals.size, 2);
assert.equal(badge.textContent, '2');
assert.equal(badge.hidden, false);
assert.equal(UI.isNewNewsArrival({ id: 'a1' }), true);
assert.equal(UI.isNewNewsArrival({ id: 'zz' }), false);
assert.equal(UI.isNewNewsArrival(null), false);

// 2. entries older than 24 hours are dropped the next time something arrives
UI._newNewsArrivals.set('old', Date.now() - 25 * 60 * 60 * 1000);
UI.rememberNewNews([{ id: 'a3' }]);
assert.equal(UI._newNewsArrivals.has('old'), false);
assert.equal(badge.textContent, '3');

// 3. the notification's action opens the News tab on the "New" focus
UI.showNewNews();
assert.deepEqual(calls, [['navigate', 'news'], ['focus', 'new']]);
calls.length = 0;
UI.showNewNews();                                   // already on News: no second navigation
assert.deepEqual(calls, [['focus', 'new']]);

// 4. an empty set hides the badge
UI._newNewsArrivals.clear();
UI.updateNewNewsBadge();
assert.equal(badge.hidden, true);
assert.equal(badge.textContent, '');

// 5. wiring in the rest of the page
assert(/focusMode === 'new' && !UI\.isNewNewsArrival\(article\)/.test(source), 'the feed filter must know the "new" focus');
assert(/new Set\(\['top', 'all', 'new',/.test(source), 'setNewsFocusMode must accept "new"');
assert(/action: \{ label: 'View', onClick: \(\) => UI\.showNewNews\(\) \}/.test(source), 'the notification must carry a View action');
assert(!/new news detected!/.test(source), 'the old dead-end notification text must be gone');
assert(/data-news-focus="new"[^>]*>New <span id="news-new-count"/.test(html), 'the News focus rail must have a New button with a count');

console.log('news_new_arrivals: all assertions passed');
