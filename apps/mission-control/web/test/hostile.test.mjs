// Every server string is hostile markup. Proves no element and no attribute is created from it: the only tags
// are the ones the views make, no attribute carries it (an href only ever holds a parsed, normalised http(s)
// URL), and the text shows up verbatim as text. mutation.test.mjs re-runs this file against innerHTML renders.

import test from 'node:test';
import assert from 'node:assert/strict';
import { loadModules, buildState, render, fail, fx, allElements } from './render.mjs';

const mods = await loadModules();
const H = fx.HOSTILE;

// Every tag the views create on purpose. Anything else came from parsing a string.
const TAGS = new Set(['div', 'section', 'header', 'main', 'h1', 'h2', 'h3', 'p', 'span', 'strong', 'ul', 'ol', 'li', 'a', 'button', 'form', 'input', 'label', 'svg', 'path', 'circle']);
const BAD_VALUE = /onerror|javascript:|<|>|alert\(/i;

function checkHref(name, el, v) {
  if (/^#[a-z][a-z0-9_-]*$/.test(v)) return;
  let u;
  assert.doesNotThrow(() => (u = new URL(v)), `${name}: href "${v}" is a URL`);
  assert.ok(u.protocol === 'https:' || u.protocol === 'http:', `${name}: href "${v}" is http(s)`);
  assert.equal(u.href, v, `${name}: href is the URL parser's normalised form`);
  assert.doesNotMatch(v, /[<>"'\s`]/, `${name}: href "${v}" has no raw markup characters`);
  assert.equal(el.localName, 'a');
}

function check(name, root) {
  const els = allElements(root);
  assert.ok(els.length > 20, `${name}: rendered something`);
  for (const el of els) {
    assert.ok(TAGS.has(el.localName), `${name}: unexpected <${el.localName}> element`);
    for (const [k, v] of el.attributes) {
      assert.ok(!/^on/i.test(k), `${name}: event-handler attribute ${k} on <${el.localName}>`);
      assert.ok(!['src', 'style', 'srcdoc', 'action', 'formaction', 'xlink:href', 'srcset'].includes(k.toLowerCase()), `${name}: ${k} attribute on <${el.localName}>`);
      if (k === 'href') checkHref(name, el, v);
      else assert.ok(!BAD_VALUE.test(v), `${name}: attribute ${k}="${v}" on <${el.localName}> carries server text`);
    }
  }
  return root.textContent;
}

const hostileItems = () => [H, { label: H, value: H, status: H }, { text: H }, { label: { toString: () => H } }];

const variants = {
  'every card and the briefing, all strings hostile': () => buildState(mods, { cards: fx.hostile(fx.cards()), briefing: fx.hostile(fx.briefing()), push: { phase: 'on', message: { kind: 'error', text: H } } }),
  'hostile errors, items as bare strings, hostile statuses': () => {
    const c = fx.hostile(fx.cards());
    for (const k of Object.keys(c.cards)) Object.assign(c.cards[k], { error: H, items: hostileItems(), status: H, link: H });
    return buildState(mods, { cards: c, briefing: { ...fx.hostile(fx.briefing()), sections: [{ title: H, items: hostileItems(), empty: H }, { title: H, items: [], empty: H }] } });
  },
  'broken fixture, hostile': () => buildState(mods, { cards: fx.hostile(fx.cardsBroken()), briefing: fail('http', 500) }),
  'served from a hostile snapshot under the banner': () =>
    buildState(mods, { cards: fail('network'), cardsSnapshot: { savedAt: fx.NOW - 60_000, data: fx.hostile(fx.cards()) }, briefing: fail('shape', 200), briefingSnapshot: { savedAt: fx.NOW, data: fx.hostile(fx.briefing()) } }),
  'link values that are scripts or data URLs': () => {
    const c = fx.cards();
    const bad = ['javascript:alert(1)', 'data:text/html,<script>alert(1)</script>', ' JavaScript:alert(1)', 'vbscript:x', '"><img src=x onerror=alert(1)>'];
    Object.keys(c.cards).forEach((k, i) => (c.cards[k].link = bad[i % bad.length]));
    c.config.open_webui_url = 'javascript:alert(1)';
    return buildState(mods, { cards: c, briefing: fx.briefing() });
  },
};

for (const [name, make] of Object.entries(variants)) {
  test(`hostile: ${name}`, () => {
    const { root } = render(mods, make());
    const txt = check(name, root);
    if (name !== 'link values that are scripts or data URLs') assert.ok(txt.includes(H), `${name}: the hostile string is shown as text`);
  });
}

test('hostile: only the server-sent links that parse as http(s) become hrefs, and javascript: never does', () => {
  const { root } = render(mods, variants['link values that are scripts or data URLs']());
  const hrefs = allElements(root).filter((e) => e.getAttribute('href')).map((e) => e.getAttribute('href'));
  assert.deepEqual(hrefs.filter((h) => !h.startsWith('#')), [], 'no link from a non-http value');
});
