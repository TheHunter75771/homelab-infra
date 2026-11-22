// Every server string in every tab is hostile markup. Proves that no element and no attribute is created
// from it: the only tags are the ones the views create, no attribute name or value carries it, and the
// text shows up verbatim as text. mutation.test.mjs re-runs this file against an innerHTML render.

import test from 'node:test';
import assert from 'node:assert/strict';
import { loadModules, buildState, renderAll, fx } from './render.mjs';
import { allElements } from './dom-shim.mjs';

const mods = await loadModules();
const H = fx.HOSTILE;

// Every tag the views create on purpose. Anything else came from parsing a string.
const TAGS = new Set(['div', 'section', 'header', 'nav', 'main', 'h1', 'h2', 'h3', 'p', 'span', 'ul', 'ol', 'li', 'dl', 'dt', 'dd', 'button', 'select', 'option', 'input', 'label', 'pre', 'figure', 'figcaption', 'svg', 'path', 'line', 'polyline']);
const BAD_VALUE = /onerror|javascript:|<|>|alert\(/i;

function hostileState(extra = {}) {
  const sessions = { sessions: fx.hostile(fx.sessions()), skipped: H }; // a hostile `skipped` too
  return buildState(mods, fx.hostileSummary(), {
    history: fx.hostile(fx.history()),
    requests: fx.hostile(fx.requests()),
    logSources: fx.hostile(fx.logSources()).map((x, i) => ({ ...x, id: `${x.id}${i}` })),
    logs: fx.hostile(fx.logs()),
    sudo: fx.hostile(fx.sudo()),
    sessions,
    localai: fx.hostile(fx.localai()),
    user: 'localai',
    ...extra,
  });
}

function check(name, root) {
  const els = allElements(root);
  assert.ok(els.length > 3, `${name}: rendered something`);
  for (const el of els) {
    assert.ok(TAGS.has(el.localName), `${name}: unexpected <${el.localName}> element`);
    for (const [k, v] of el.attributes) {
      assert.ok(!/^on/i.test(k), `${name}: event-handler attribute ${k} on <${el.localName}>`);
      assert.ok(!['href', 'src', 'style', 'srcdoc', 'action', 'formaction', 'xlink:href'].includes(k.toLowerCase()), `${name}: ${k} attribute on <${el.localName}>`);
      assert.ok(!BAD_VALUE.test(v), `${name}: attribute ${k}="${v}" on <${el.localName}> carries server text`);
    }
  }
  return root.textContent;
}

const variants = {
  'lists': () => hostileState(),
  'replay, command only': () => hostileState({ replay: { session: fx.hostile(fx.sessions())[0], data: fx.hostile(fx.replay(false)) } }),
  'replay with output': () => hostileState({ replay: { session: fx.hostile(fx.sessions())[0], output: true, data: fx.hostile(fx.replay(true)) } }),
  'offline with snapshot': () => hostileState({ conn: 'offline' }),
  'sudo rows refused / wrong password, hostile text and a hostile event': () =>
    hostileState({ sudo: [...fx.sudoFlagged().map((c) => ({ ...fx.hostile(c), event: c.event })), { ...fx.hostile(fx.sudo()[0]), event: H }] }),
  'gate down, sections failed': () => {
    const s = fx.hostile(fx.everythingDown());
    s.routing.reason = fx.HOSTILE;
    return buildState(mods, s, { history: fx.HOSTILE, requests: { error: fx.HOSTILE }, logs: { text: fx.HOSTILE }, sudo: { items: fx.HOSTILE }, sessions: fx.HOSTILE });
  },
  'every request failed with a hostile error': () => {
    const { reduce, initialState } = mods.state;
    let s = reduce(initialState(), { type: 'summary/loaded', summary: fx.hostile(fx.everythingDown()), at: fx.NOW });
    s = reduce(s, { type: 'history/failed', error: H });
    s = reduce(s, { type: 'requests/failed', error: H });
    s = reduce(s, { type: 'logs/failed', source: s.logs.source, error: H });
    for (const part of ['sudo', 'sessions', 'localai']) s = reduce(s, { type: 'audit/failed', part, error: H });
    s = reduce(s, { type: 'logs/sources/failed', error: H });
    return s;
  },
  'ambiguous rows with hostile text, and hostile ambiguous values': () =>
    hostileState({ sudo: [...fx.sudoAmbiguous().map((c) => ({ ...fx.hostile(c), event: c.event, ambiguous: c.ambiguous })), { ...fx.hostile(fx.sudo()[0]), ambiguous: H }, { ...fx.hostile(fx.sudo()[0]), ambiguous: { valueOf: H } }] }),
  'sessions reply with a hostile skipped count and a huge one': () => hostileState({ sessions: { sessions: fx.hostile(fx.sessions()), skipped: { toString: H } } }),
  'sessions reply that is a hostile string': () => hostileState({ sessions: H }),
  'replay failed with a hostile error': () => {
    const { reduce } = mods.state;
    const session = fx.hostile(fx.sessions())[0];
    let s = hostileState();
    s = reduce(s, { type: 'replay/open', session });
    s = reduce(s, { type: 'replay/output' });
    return reduce(s, { type: 'replay/failed', session, error: H });
  },
};

for (const [vname, make] of Object.entries(variants)) {
  test(`hostile strings stay text: ${vname}`, () => {
    const roots = renderAll(mods, make());
    let seen = 0;
    for (const [name, root] of Object.entries(roots)) {
      const text = check(`${vname}/${name}`, root);
      seen += text.split(H).length - 1;
    }
    assert.ok(seen > 0, 'the hostile text is on the page as text');
  });
}

test('hostile text is rendered verbatim where it is shown', () => {
  const roots = renderAll(mods, hostileState({ replay: { session: fx.hostile(fx.sessions())[0], output: true, data: fx.hostile(fx.replay(true)) } }));
  assert.ok(roots.audit.textContent.includes(H), 'replay output/command shown as text');
  const roots2 = renderAll(mods, hostileState());
  assert.ok(roots2.logs.textContent.includes(H), 'log lines and source labels shown as text');
  const opts = allElements(roots2.logs).filter((e) => e.localName === 'option' && e.parentNode.id === 'log-source');
  assert.equal(opts.length, 3);
  assert.deepEqual(opts.map((o) => o.getAttribute('value')), ['0', '1', '2'], 'option values are indexes, not server ids');
  assert.ok(opts.every((o) => o.textContent === H), 'labels are option text');
  assert.ok(roots2.overview.textContent.includes(H), 'alerts/services shown as text');
  assert.ok(roots2.routing.textContent.includes(H), 'the game name shown as text');
  assert.ok(roots2.models.textContent.includes(H), 'model names shown as text');
  assert.ok(roots2.shell.textContent.includes(H), 'identity shown as text');
});

test('a hostile session id never gets a Replay button', () => {
  const roots = renderAll(mods, hostileState());
  const buttons = allElements(roots.audit).filter((e) => e.localName === 'button' && e.textContent === 'Replay');
  assert.equal(buttons.length, 0);
});
