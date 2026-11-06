// The page rendered in the DOM shim: normal cards, every error state, the ask box, the notifications panel,
// and the controller opening the card a notification points at.

import test from 'node:test';
import assert from 'node:assert/strict';
import { loadModules, buildState, render, card, find, fail, fx, allElements } from './render.mjs';
import { installDom } from './dom-shim.mjs';

const mods = await loadModules();
const ORDER = ['briefing', 'ai', 'ai_control', 'money', 'school', 'home', 'servers', 'alerts'];
const word = (sec) => find(sec, (e) => e.getAttribute('class') === 'badge-word').map((e) => e.textContent)[0];
const text = (el) => el.textContent.replace(/\s+/g, ' ');
const links = (el) => find(el, (e) => e.localName === 'a' && /^https?:/.test(e.getAttribute('href') || ''));

test('normal: eight cards in order, each with a status word and its tap-through', () => {
  const { root } = render(mods, buildState(mods, { cards: fx.cards(), briefing: fx.briefing() }));
  const ids = find(root, (e) => e.localName === 'section' && /^card-/.test(e.getAttribute('id'))).map((e) => e.getAttribute('id'));
  assert.deepEqual(ids, ORDER.map((i) => `card-${i}`));
  assert.equal(word(card(root, 'money')), 'Warning');
  assert.equal(word(card(root, 'home')), 'Set up');
  assert.match(text(card(root, 'home')), /Connect Home Assistant/);
  assert.match(text(card(root, 'alerts')), /No active alerts\./);
  assert.match(text(card(root, 'briefing')), /Wednesday 28 October.*1 deadline, 2 bills, no alerts.*School: next 48 h.*MATH 111 quiz 4.*Alerts.*No alerts\./);
  const expect = { ai: `${fx.OPEN_WEBUI}/`, ai_control: 'https://ai-box.example.test/', money: 'http://money.example.test:8710/', school: 'https://erau.example.test/', home: 'http://ha.example.test:8123/', servers: 'https://dash.example.test/' };
  for (const [id, href] of Object.entries(expect)) {
    const [a] = links(card(root, id));
    assert.ok(a, `${id} has a tap-through`);
    assert.equal(a.getAttribute('href'), href);
    assert.equal(a.getAttribute('target'), '_blank');
    assert.equal(a.getAttribute('rel'), 'noopener noreferrer');
    assert.match(text(a), /\(opens in a new window\)/);
  }
  assert.equal(find(root, (e) => e.getAttribute('class') === 'banner')[0].hidden, true, 'no banner when the server answers');
});

test('error states: each card shows its own failure; the rest of the page still works', () => {
  const { root } = render(mods, buildState(mods, { cards: fx.cardsBroken(), briefing: fail('http', 500) }));
  assert.equal(word(card(root, 'briefing')), 'Down');
  assert.match(text(card(root, 'briefing')), /Couldn't load today's briefing \(HTTP 500\)\./);
  assert.equal(word(card(root, 'money')), 'Down');
  assert.match(text(card(root, 'money')), /Couldn't read this source: HTTP 502 from Money Hub/);
  assert.match(text(card(root, 'ai')), /Couldn't read this source: Open WebUI \/health timed out after 4 s/);
  assert.equal(word(card(root, 'school')), 'Unknown', 'an unknown status word reads as Unknown');
  assert.match(text(card(root, 'school')), /Couldn't read this source: unknown error/);
  assert.equal(links(card(root, 'school')).length, 0, 'an ftp: link is not rendered');
  for (const id of ['servers', 'home']) {
    assert.equal(word(card(root, id)), 'Unknown');
    assert.match(text(card(root, id)), /The server didn't report this card\./);
  }
  assert.equal(word(card(root, 'alerts')), 'Down');
  assert.match(text(card(root, 'alerts')), /Bank sync failing for 52 h.*Disk \/ at 93%/);
  // still usable: AI Control rendered normally
  assert.equal(word(card(root, 'ai_control')), 'OK');
  assert.equal(links(card(root, 'ai_control')).length, 1);
  // the ask box is off (the base was javascript:) and says why
  const btn = find(root, (e) => e.getAttribute('id') === 'ask-go')[0];
  assert.equal(btn.getAttribute('aria-disabled'), 'true');
  assert.match(text(card(root, 'ai')), /Open in AI is off: the server hasn't sent a usable Open WebUI address\./);
  assert.equal(links(card(root, 'ai')).length, 0, 'no Open WebUI link from a javascript: base');
});

test('server unreachable with no saved copy: banner plus a per-card note, every status Unknown', () => {
  const { root } = render(mods, buildState(mods, { cards: fail('network'), briefing: fail('network') }));
  const banner = find(root, (e) => e.getAttribute('class') === 'banner')[0];
  assert.equal(banner.hidden, false);
  assert.match(text(banner), /Can't reach Mission Control\. Cards fill in when it's back\./);
  for (const id of ORDER.slice(1)) {
    assert.equal(word(card(root, id)), 'Unknown', id);
    assert.match(text(card(root, id)), /Can't reach Mission Control's server \(no answer\)/, id);
  }
  assert.match(text(card(root, 'briefing')), /Couldn't load today's briefing \(no answer\)/);
});

test('server error with a saved copy: the copy is shown under a banner with its age', () => {
  const st = buildState(mods, {
    cards: fail('http', 502),
    cardsSnapshot: { savedAt: fx.NOW - 12 * 60_000, data: fx.cards() },
    briefing: fail('http', 502),
    briefingSnapshot: { savedAt: fx.NOW - 30 * 60_000, data: fx.briefing() },
  });
  const { root } = render(mods, st);
  assert.match(text(find(root, (e) => e.getAttribute('class') === 'banner')[0]), /Mission Control answered with an error \(HTTP 502\)\. Showing what it reported 12 min ago\./);
  assert.equal(word(card(root, 'money')), 'Warning', 'the snapshot data is drawn');
  assert.equal(word(card(root, 'briefing')), 'Warning');
  assert.match(text(card(root, 'briefing')), /Couldn't refresh the briefing \(HTTP 502\)\. This is the copy from 30 min ago\./);
});

test('a later failure keeps what the page already had (stale, not blanked)', () => {
  const { applyResult, initialState } = mods.app;
  let s = applyResult(initialState(), 'cards', { ok: true, data: fx.cards() }, null, fx.NOW - 60_000);
  s = applyResult(s, 'cards', { ok: false, fail: { kind: 'network', status: null } }, { savedAt: 1, data: {} }, fx.NOW);
  assert.equal(s.cards.cards.money.status, 'warn');
  assert.equal(s.cardsFrom, 'snapshot');
  assert.equal(s.cardsSavedAt, fx.NOW - 60_000);
  s = applyResult(s, 'cards', { ok: true, data: fx.cardsBroken() }, null, fx.NOW);
  assert.equal(s.cardsFail, null);
  assert.equal(s.cardsFrom, 'live');
});

test('AI card wording: "Open in AI", ready to send; never claims to ask or send', () => {
  const { root } = render(mods, buildState(mods, { cards: fx.cards(), briefing: fx.briefing() }));
  const form = find(root, (e) => e.localName === 'form')[0];
  const btn = find(root, (e) => e.getAttribute('id') === 'ask-go')[0];
  assert.equal(text(btn).trim(), 'Open in AI');
  assert.equal(find(root, (e) => e.getAttribute('id') === 'ask-q')[0].getAttribute('enterkeyhint'), 'go', 'the keyboard key says Go, not Send');
  assert.match(text(form), /ready to send/);
  const words = [text(form), form.getAttribute('aria-label')].join(' ').replace(/ready to send/g, '');
  assert.doesNotMatch(words, /\b(asks?|asking|asked|sends?|sent|sending)\b/i, 'no claim that it asks or sends');
});

test('ask box: submit opens the encoded Open WebUI link; the typed text survives a refresh', () => {
  const opened = [];
  const st = buildState(mods, { cards: fx.cards(), briefing: fx.briefing() });
  const { root, view } = render(mods, st, { openExternal: (u) => opened.push(u) });
  const input = find(root, (e) => e.getAttribute('id') === 'ask-q')[0];
  const form = find(root, (e) => e.localName === 'form')[0];
  input.value = 'is the backup ok? & what about #2';
  form.dispatchEvent({ type: 'submit', preventDefault() {} });
  assert.deepEqual(opened, [`${fx.OPEN_WEBUI}/?models=agent_router.auto&q=is%20the%20backup%20ok%3F%20%26%20what%20about%20%232`]);
  view.update(buildState(mods, { cards: fx.cards(), briefing: fx.briefing() }), fx.NOW);
  assert.equal(find(root, (e) => e.getAttribute('id') === 'ask-q')[0], input, 'same input element');
  assert.equal(input.value, 'is the backup ok? & what about #2');
  // without a base, submitting does nothing
  view.update(buildState(mods, { cards: fx.cardsBroken() }), fx.NOW);
  form.dispatchEvent({ type: 'submit', preventDefault() {} });
  assert.equal(opened.length, 1);
});

test('notifications panel: iPhone in Safari gets the Add to Home Screen steps, not a button', () => {
  const { root } = render(mods, buildState(mods, { cards: fx.cards(), push: { phase: 'blocked', blocked: 'ios-browser', isIOS: true } }));
  const panel = find(root, (e) => e.getAttribute('id') === 'notify')[0];
  assert.match(text(panel), /Home Screen \(iOS 16\.4 or later\)/);
  const steps = find(panel, (e) => e.localName === 'li').map(text);
  assert.equal(steps.length, 4);
  assert.match(steps[0], /Share/);
  assert.match(steps[1], /Add to Home Screen/);
  assert.match(steps[2], /from your Home Screen/);
  assert.match(steps[3], /Enable notifications/);
  assert.equal(find(panel, (e) => e.localName === 'button').length, 0);
});

test('notifications panel: off → Enable (off without a key), on → test + turn off; buttons call actions; busy blocks', () => {
  const calls = [];
  const actions = { enablePush: () => calls.push('enable'), testPush: () => calls.push('test'), disablePush: () => calls.push('disable') };
  let { root } = render(mods, buildState(mods, { cards: fx.cards(), push: { phase: 'off' } }), actions);
  const on = find(root, (e) => e.getAttribute('id') === 'push-on')[0];
  assert.match(text(on), /Enable notifications/);
  on.click();
  assert.deepEqual(calls, ['enable']);

  ({ root } = render(mods, buildState(mods, { cards: fx.cardsBroken(), push: { phase: 'off' } }), actions));
  const off = find(root, (e) => e.getAttribute('id') === 'push-on')[0];
  assert.equal(off.getAttribute('aria-disabled'), 'true');
  off.click();
  assert.deepEqual(calls, ['enable'], 'no key: the button does nothing');
  assert.match(text(root), /hasn't sent its push key yet/);

  ({ root } = render(mods, buildState(mods, { cards: fx.cards(), push: { phase: 'on', message: { kind: 'ok', text: 'Test sent.' } } }), actions));
  find(root, (e) => e.getAttribute('id') === 'push-test')[0].click();
  find(root, (e) => e.getAttribute('id') === 'push-off')[0].click();
  assert.deepEqual(calls, ['enable', 'test', 'disable']);
  assert.equal(find(root, (e) => /push-msg/.test(e.getAttribute('class') || ''))[0].textContent, 'Test sent.');

  ({ root } = render(mods, buildState(mods, { cards: fx.cards(), push: { phase: 'on', busy: true } }), actions));
  find(root, (e) => e.getAttribute('id') === 'push-test')[0].click();
  assert.deepEqual(calls, ['enable', 'test', 'disable'], 'busy: clicks ignored');

  for (const [blocked, re] of [['denied', /Settings → Notifications → Mission Control/], ['insecure', /https:\/\//], ['unsupported', /can't receive web push/], ['worker', /background worker didn't start/]]) {
    ({ root } = render(mods, buildState(mods, { cards: fx.cards(), push: { phase: 'blocked', blocked } })));
    assert.match(text(find(root, (e) => e.getAttribute('id') === 'notify')[0]), re, blocked);
  }
});

test('focus stays on a control that is re-rendered (same id)', () => {
  const st = buildState(mods, { cards: fx.cards(), push: { phase: 'on' } });
  const { doc, root, view } = render(mods, st, {});
  find(root, (e) => e.getAttribute('id') === 'push-test')[0].focus();
  view.update({ ...st, push: { ...st.push, busy: true } }, fx.NOW);
  assert.equal(doc.activeElement.getAttribute('id'), 'push-test');
  assert.notEqual(doc.activeElement.parentNode, null, 'focus is on the new, mounted button');
});

test('controller: a worker "open-card" message (notification tap) highlights and focuses that card', async () => {
  const doc = installDom();
  const listeners = {};
  const replaced = [];
  const win = {
    document: doc,
    location: { hash: '' },
    history: { replaceState: (_s, _t, u) => replaced.push(u) },
    navigator: { serviceWorker: { addEventListener: (t, fn) => (listeners[t] = fn) } },
    addEventListener() {},
    open() {},
  };
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (p) => new Response(JSON.stringify(p === '/api/cards' ? fx.cards() : fx.briefing()), { status: 200 });
  const rootEl = doc.createElement('div');
  doc.body.appendChild(rootEl);
  const app = mods.app.startApp(rootEl, win);
  try {
    await app.ready;
    assert.equal(app.state.cards.cards.money.status, 'warn');
    listeners.message({ data: { type: 'open-card', card: 'money' } });
    assert.deepEqual(replaced, ['#card-money']);
    assert.equal(app.state.highlight, 'money');
    assert.ok(card(rootEl, 'money').classList.contains('flash'));
    assert.equal(doc.activeElement.getAttribute('id'), 'card-money-h', 'focus on the card heading');
    listeners.message({ data: { type: 'open-card', card: '"><img src=x>' } });
    listeners.message({ data: { type: 'other', card: 'school' } });
    assert.deepEqual(replaced, ['#card-money'], 'unknown cards and messages ignored');
  } finally {
    app.stop();
    globalThis.fetch = realFetch;
  }
});

test('every card section is labelled by its heading; status words never colour alone', () => {
  const { root } = render(mods, buildState(mods, { cards: fx.cardsBroken(), briefing: fx.briefing() }));
  for (const sec of find(root, (e) => e.localName === 'section')) {
    const id = sec.getAttribute('aria-labelledby');
    assert.ok(allElements(root).some((e) => e.getAttribute('id') === id && e.localName === 'h2'), `${sec.getAttribute('id')} labelled`);
  }
  for (const b of find(root, (e) => (e.getAttribute('class') || '').split(' ').includes('badge'))) {
    assert.ok(b.children.some((c) => c.localName === 'svg'), 'icon');
    assert.ok(b.textContent.trim(), 'word');
  }
});
