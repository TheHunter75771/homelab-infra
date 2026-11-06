// model.js: the deep link, link safety, and tolerant normalising of whatever the server sends.

import test from 'node:test';
import assert from 'node:assert/strict';
import { loadModules, fx } from './render.mjs';

const { model: m } = await loadModules();
const BASE = fx.OPEN_WEBUI;

/** The q and models values exactly as Open WebUI's page will read them (URLSearchParams decoding). */
function params(href) {
  const u = new URL(href);
  return { origin: u.origin, path: u.pathname, models: u.searchParams.getAll('models'), q: u.searchParams.getAll('q'), keys: [...u.searchParams.keys()], hash: u.hash };
}

test('deep link: models=agent_router.auto and the question round-trips exactly', () => {
  for (const q of [
    'hello',
    'what is 2+2 & why?',
    'ends with #hash and %41 and %',
    'a=b&models=evil.model&q=override',
    'line one\nline two\ttab',
    'émoji 🚀 日本語',
    '  padded  ',
    '/path?x=1#frag',
    "quotes \" ' < > `",
  ]) {
    const href = m.askUrl(BASE, q);
    const p = params(href);
    assert.equal(p.origin, BASE);
    assert.deepEqual(p.models, ['agent_router.auto'], `${q}: exactly one models param`);
    assert.deepEqual(p.q, [q.trim()], `${q}: q decodes to the question`);
    assert.deepEqual(p.keys, ['models', 'q'], `${q}: no extra parameters`);
    assert.equal(p.hash, '', `${q}: nothing leaks into the fragment`);
  }
});

test('deep link: every reserved character is percent-encoded (encodeURIComponent, spaces as %20)', () => {
  const href = m.askUrl(BASE, 'a b&c=d#e+f?g/h');
  assert.equal(href, `${BASE}/?models=agent_router.auto&q=a%20b%26c%3Dd%23e%2Bf%3Fg%2Fh`);
  assert.doesNotMatch(href.split('?')[1], /[ #]/);
});

test('deep link: base path kept, base query and fragment replaced, empty question → just the model', () => {
  assert.equal(m.askUrl('https://ai.example.test/webui/?old=1#x', 'hi'), 'https://ai.example.test/webui/?models=agent_router.auto&q=hi');
  assert.equal(m.askUrl(BASE, '   '), `${BASE}/?models=agent_router.auto`);
  assert.equal(m.askUrl(BASE, undefined), `${BASE}/?models=agent_router.auto`);
  assert.equal(m.askUrl(BASE, 'x'.repeat(5000)).length < 2200, true, 'question capped');
});

test('deep link: a bad base gives null (no link), whatever the question', () => {
  for (const base of [null, undefined, '', 'javascript:alert(1)', 'data:text/html,x', '//ai.example.test', '/relative', 'ftp://x.example.test', 'https://user:pw@ai.example.test', 42, {}])
    assert.equal(m.askUrl(base, 'hi'), null, String(base));
});

test('safeHref: only absolute http(s) without credentials, normalised', () => {
  assert.equal(m.safeHref('https://a.example.test/x'), 'https://a.example.test/x');
  assert.equal(m.safeHref('http://100.64.0.1:8710/'), 'http://100.64.0.1:8710/');
  assert.equal(m.safeHref(' https://a.example.test '), 'https://a.example.test/');
  assert.equal(m.safeHref('https://a.example.test/"><img src=x>'), 'https://a.example.test/%22%3E%3Cimg%20src=x%3E');
  for (const bad of ['javascript:alert(1)', 'JAVASCRIPT:alert(1)', ' javascript:alert(1)', 'vbscript:x', 'data:text/html,<script>', 'blob:https://a.example.test/1', 'file:///etc/passwd', 'mailto:a@b', '#x', '/x', 'x', 'https://u:p@a.example.test/', 'https://u@a.example.test/', null, 5, ['https://a.example.test'], 'https://' + 'a'.repeat(3000)])
    assert.equal(m.safeHref(bad), null, String(bad));
});

test('statusKey: known words and aliases; anything else is unknown', () => {
  assert.equal(m.statusKey('ok'), 'ok');
  assert.equal(m.statusKey(' WARNING '), 'warn');
  assert.equal(m.statusKey('stale'), 'warn');
  assert.equal(m.statusKey('error'), 'down');
  assert.equal(m.statusKey('setup'), 'setup');
  for (const v of ['purple', '', null, undefined, 3, {}, ['ok'], '__proto__', 'constructor', 'toString']) assert.equal(m.statusKey(v), 'unknown', String(v));
});

test('normalizeCards: every card id present; garbage tolerated; config validated', () => {
  for (const junk of [null, 'x', 5, [], {}, { cards: [] }, { cards: 'x' }, { config: 'x' }]) {
    const n = m.normalizeCards(junk);
    assert.deepEqual(Object.keys(n.cards), m.CARD_ORDER.filter((id) => id !== 'briefing'));
    for (const c of Object.values(n.cards)) assert.equal(c.present, false);
    assert.equal(n.config.openWebuiUrl, null);
    assert.equal(n.config.vapidPublicKey, null);
  }
  const good = m.normalizeCards(fx.cards());
  assert.equal(good.config.openWebuiUrl, `${fx.OPEN_WEBUI}/`);
  assert.equal(good.config.vapidPublicKey, fx.VAPID);
  assert.equal(good.cards.money.status, 'warn');
  assert.equal(good.cards.money.items.length, 3);
  assert.equal(good.cards.money.items[2].status, null, 'an item with no status has none (not "unknown")');
  assert.equal(good.generatedAt, fx.NOW);

  const b = m.normalizeCards(fx.cardsBroken());
  assert.equal(b.config.openWebuiUrl, null, 'javascript: base dropped');
  assert.equal(b.config.vapidPublicKey, null, 'malformed key dropped');
  assert.equal(b.cards.money.status, 'down');
  assert.equal(b.cards.money.error, 'HTTP 502 from Money Hub');
  assert.equal(b.cards.school.status, 'unknown');
  assert.equal(b.cards.school.headline, '42');
  assert.deepEqual(b.cards.school.items, []);
  assert.equal(b.cards.school.link, null, 'ftp link dropped');
  assert.equal(b.cards.school.error, 'unknown error', 'a non-string error still counts as an error');
  assert.equal(b.cards.school.updatedAt, null, 'string timestamps rejected');
  assert.equal(b.cards.servers.present, false);
  assert.equal(b.cards.home.present, false);
  assert.equal(b.cards.alerts.items.length, 2, 'string items kept, unusable objects dropped');
});

test('normalizeCards: an error with no status reads as down; prototype keys are not cards', () => {
  const n = m.normalizeCards({ cards: { money: { error: 'boom' } } });
  assert.equal(n.cards.money.status, 'down');
  const p = m.normalizeCards(JSON.parse('{"cards": {"__proto__": {"status": "ok"}}}'));
  assert.equal(p.cards.ai.present, false);
});

test('normalizeBriefing: tolerant; long text capped; items limited', () => {
  const b = m.normalizeBriefing(fx.briefing());
  assert.equal(b.date, '2026-10-28');
  assert.equal(b.sections.length, 4);
  assert.equal(b.sections[2].items.length, 0);
  assert.equal(b.sections[2].empty, 'No alerts.');
  const junk = m.normalizeBriefing({ date: '28/10/2026', sections: [null, 'x', { title: 5, items: Array(40).fill('a') }], summary: 'y'.repeat(2000) });
  assert.equal(junk.date, null);
  assert.equal(junk.sections.length, 1);
  assert.equal(junk.sections[0].title, '5');
  assert.equal(junk.sections[0].items.length, 12);
  assert.ok(junk.summary.length <= 500);
  assert.deepEqual(m.normalizeBriefing(null).sections, []);
});

test('cardFromHash, longDate, ago, failText', () => {
  assert.equal(m.cardFromHash('#card-money'), 'money');
  assert.equal(m.cardFromHash('#card-ai_control'), 'ai_control');
  for (const h of ['#card-nope', '#money', 'card-money', '#card-money"', '', null]) assert.equal(m.cardFromHash(h), null, String(h));
  assert.equal(m.longDate('2026-10-28'), 'Wednesday 28 October');
  assert.equal(m.longDate('2026-02-31'), '');
  assert.equal(m.ago(fx.NOW - 10_000, fx.NOW), 'just now');
  assert.equal(m.ago(fx.NOW - 12 * 60_000, fx.NOW), '12 min ago');
  assert.equal(m.ago(fx.NOW - 3 * 3600_000, fx.NOW), '3 h ago');
  assert.equal(m.failText({ kind: 'network' }), 'no answer');
  assert.equal(m.failText({ kind: 'http', status: 502 }), 'HTTP 502');
  assert.equal(m.failText({ kind: 'shape', status: 200 }), 'an unreadable reply');
});
