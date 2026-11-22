// The service worker in a fake worker scope (node:vm): precache, offline shell, data snapshots, push,
// notificationclick and pushsubscriptionchange.

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { PUBLIC, fx } from './render.mjs';

const SRC = fs.readFileSync(path.join(PUBLIC, 'sw.js'), 'utf8');
const ORIGIN = 'https://ai-box.example.test:8443';

function fakeWorker(network = async (r) => new Response(`file ${new URL(r.url).pathname}`, { status: 200 }), { windows = [] } = {}) {
  const stores = new Map();
  const keyOf = (k) => new URL(typeof k === 'string' ? k : k.url, ORIGIN).pathname;
  const caches = {
    async open(name) {
      if (!stores.has(name)) stores.set(name, new Map());
      const m = stores.get(name);
      return {
        async match(k) {
          const r = m.get(keyOf(k));
          return r ? r.clone() : undefined;
        },
        async put(k, res) {
          m.set(keyOf(k), res);
        },
        async addAll(reqs) {
          for (const r of reqs) {
            const res = await network(r);
            if (!res.ok) throw new Error(`precache failed: ${r.url}`);
            m.set(keyOf(r), res);
          }
        },
      };
    },
    async keys() {
      return [...stores.keys()];
    },
    async delete(n) {
      return stores.delete(n);
    },
  };
  const listeners = {};
  const shown = [];
  const opened = [];
  const posts = [];
  const self = {
    location: new URL(ORIGIN),
    addEventListener: (t, fn) => (listeners[t] = fn),
    skipWaiting: async () => {},
    clients: {
      claim: async () => {},
      matchAll: async (opts) => {
        assert.equal(opts.type, 'window');
        return windows;
      },
      openWindow: async (url) => {
        opened.push(url);
        return null;
      },
    },
    registration: {
      showNotification: async (title, options) => shown.push({ title, options }),
      pushManager: { subscribe: async () => ({ endpoint: 'https://push.example.test/new', toJSON: () => ({ endpoint: 'https://push.example.test/new' }) }) },
    },
  };
  const fetchFn = async (r, init) => {
    if (init?.method === 'POST') {
      posts.push({ path: r, init });
      return new Response('{"ok":true}', { status: 200 });
    }
    return network(typeof r === 'string' ? new Request(new URL(r, ORIGIN)) : r);
  };
  const ctx = vm.createContext({
    self, caches, fetch: fetchFn, atob, Uint8Array,
    Request: class extends Request {
      constructor(input, init) {
        super(new URL(input, ORIGIN), init);
      }
    },
    Response, URL, Set, Object, Promise, JSON, Date,
  });
  vm.runInContext(SRC, ctx);
  const run = async (type, event) => {
    let p;
    assert.ok(listeners[type], `the worker listens for ${type}`);
    listeners[type]({ ...event, waitUntil: (x) => (p = x) });
    await p;
  };
  return {
    stores, listeners, shown, opened, posts,
    install: () => run('install', {}),
    activate: () => run('activate', {}),
    async fetch(pathname, { mode = 'cors', method = 'GET' } = {}) {
      let answer = null;
      const url = /^https?:\/\//.test(pathname) ? pathname : ORIGIN + pathname;
      listeners.fetch({ request: { url, method, mode }, respondWith: (p) => (answer = p) });
      return answer ? await answer : null;
    },
    async push(data) {
      const ev = data === undefined ? { data: null } : { data: { json: () => JSON.parse(data), text: () => data } };
      await run('push', ev);
      return shown.at(-1);
    },
    async click(notificationData) {
      let closed = false;
      await run('notificationclick', { notification: { data: notificationData, close: () => (closed = true) } });
      return closed;
    },
    run,
  };
}

const win = (url) => {
  const w = { url, focused: 0, messages: [], focus: async () => (w.focused++, w), postMessage: (m) => w.messages.push(m) };
  return w;
};
const tick = () => new Promise((r) => setTimeout(r, 10));

test('install precaches the shell into a versioned cache (icons included); activate drops old versions only', async () => {
  const w = fakeWorker();
  w.stores.set('mc-shell-old', new Map());
  w.stores.set('mc-data-v1', new Map([['/__snapshot/cards', new Response('{}')]]));
  w.stores.set('mc-meta', new Map());
  await w.install();
  await w.activate();
  const shells = [...w.stores.keys()].filter((n) => n.startsWith('mc-shell-'));
  assert.equal(shells.length, 1);
  assert.ok(w.stores.has('mc-data-v1') && w.stores.has('mc-meta'), 'data and meta caches survive');
  const shell = w.stores.get(shells[0]);
  for (const p of ['/', '/js/app.js', '/css/app.css', '/manifest.webmanifest', '/icons/icon-192.png', '/icons/badge-96.png']) assert.ok(shell.has(p), p);
});

test('offline shell: the page and its files come from the cache even when the network returns 502', async () => {
  let down = false;
  const w = fakeWorker(async (r) => (down ? new Response('bad gateway', { status: 502 }) : new Response(`file ${new URL(r.url).pathname}`)));
  await w.install();
  down = true;
  assert.equal(await (await w.fetch('/', { mode: 'navigate' })).text(), 'file /');
  assert.equal(await (await w.fetch('/js/view.js')).text(), 'file /js/view.js');
  assert.equal(await w.fetch('/api/push/subscribe', { method: 'POST' }), null, 'POSTs untouched');
  assert.equal(await w.fetch('/other'), null, 'unknown paths untouched');
});

test('cross-origin requests are left to the network: never answered from the shell, never saved as a snapshot', async () => {
  const seen = [];
  const w = fakeWorker(async (r) => {
    seen.push(r.url);
    return new Response(JSON.stringify({ evil: true }), { status: 200 });
  });
  await w.install();
  seen.length = 0;
  for (const u of [
    'https://evil.example.test/api/cards',
    'https://evil.example.test/api/briefing',
    'https://evil.example.test/js/app.js',
    'https://evil.example.test/',
    'https://ai-box.example.test/api/cards', // same host, other port: another origin
    'http://ai-box.example.test:8443/js/app.js', // other scheme: another origin
  ]) {
    assert.equal(await w.fetch(u), null, `${u} is not answered by the worker`);
    assert.equal(await w.fetch(u, { mode: 'navigate' }), null, `${u} (navigation) is not answered by the worker`);
  }
  await tick();
  assert.deepEqual(seen, [], 'the worker did not fetch them itself either');
  assert.equal(w.stores.get('mc-data-v1'), undefined, 'no snapshot from another origin');
});

for (const [api, key] of [['/api/cards', '/__snapshot/cards'], ['/api/briefing', '/__snapshot/briefing']]) {
  test(`${api}: a 200 object is saved with savedAt; errors, 304s and non-objects never replace it`, async () => {
    let reply = () => new Response(JSON.stringify({ v: 1 }), { status: 200 });
    const w = fakeWorker(async (r) => (new URL(r.url).pathname === api ? reply() : new Response('x')));
    assert.equal((await w.fetch(api)).status, 200);
    await tick();
    const read = async () => JSON.parse(await w.stores.get('mc-data-v1').get(key).clone().text());
    const snap = await read();
    assert.deepEqual(snap.data, { v: 1 });
    assert.equal(typeof snap.savedAt, 'number');
    for (const status of [500, 502, 503, 404]) {
      reply = () => new Response(JSON.stringify({ error: 'x' }), { status });
      assert.equal((await w.fetch(api)).status, status, 'the error reaches the page');
    }
    reply = () => new Response(null, { status: 304 });
    await w.fetch(api);
    reply = () => new Response('[1,2]', { status: 200 });
    await w.fetch(api);
    reply = () => new Response('not json', { status: 200 });
    await w.fetch(api);
    await tick();
    assert.deepEqual(await read(), snap);
  });
}

test('push: shows the notification with title, body, icons, tag, and the card to open', async () => {
  const w = fakeWorker();
  const n = await w.push(JSON.stringify({ title: 'Morning briefing', body: 'Briefing ready: 3 deadlines, 2 bills, 1 alert', tag: 'briefing-2026-10-28', card: 'briefing' }));
  assert.equal(n.title, 'Morning briefing');
  assert.equal(n.options.body, 'Briefing ready: 3 deadlines, 2 bills, 1 alert');
  assert.equal(n.options.icon, '/icons/icon-192.png');
  assert.equal(n.options.badge, '/icons/badge-96.png');
  assert.equal(n.options.tag, 'briefing-2026-10-28');
  assert.equal(n.options.renotify, true);
  assert.deepEqual({ ...n.options.data }, { url: `${ORIGIN}/#card-briefing`, card: 'briefing' });
});

test('push: odd payloads still show something sensible, and never point at another origin', async () => {
  const w = fakeWorker();
  let n = await w.push(undefined);
  assert.equal(n.title, 'Mission Control');
  assert.equal(n.options.data.url, `${ORIGIN}/`);
  assert.equal(n.options.tag, undefined);
  n = await w.push('plain text, not JSON');
  assert.equal(n.options.body, 'plain text, not JSON');
  n = await w.push(JSON.stringify({ title: '  ', body: 5, card: 'nope', url: 'https://evil.example.test/#card-money' }));
  assert.equal(n.title, 'Mission Control');
  assert.equal(n.options.body, '');
  assert.equal(n.options.data.url, `${ORIGIN}/`);
  n = await w.push(JSON.stringify({ url: 'javascript:alert(1)' }));
  assert.equal(n.options.data.url, `${ORIGIN}/`);
  n = await w.push(JSON.stringify({ url: '/#card-alerts' }));
  assert.deepEqual({ ...n.options.data }, { url: `${ORIGIN}/#card-alerts`, card: 'alerts' });
  n = await w.push(JSON.stringify({ title: 'x'.repeat(500), body: 'y'.repeat(5000) }));
  assert.ok(n.title.length <= 120 && n.options.body.length <= 400);
  n = await w.push('[1,2,3]');
  assert.equal(n.title, 'Mission Control');
});

test('notificationclick with no window open: opens the app at the card', async () => {
  const w = fakeWorker();
  const n = await w.push(JSON.stringify({ title: 'Bank sync failing', card: 'money' }));
  const closed = await w.click(n.options.data);
  assert.equal(closed, true, 'the notification is closed');
  assert.deepEqual(w.opened, [`${ORIGIN}/#card-money`]);
});

test('notificationclick with the app open: focuses it and asks it to show the card (no second window)', async () => {
  const other = win('https://elsewhere.example.test/');
  const app = win(`${ORIGIN}/#card-school`);
  const w = fakeWorker(undefined, { windows: [other, app] });
  await w.click({ url: `${ORIGIN}/#card-alerts`, card: 'alerts' });
  assert.deepEqual(JSON.parse(JSON.stringify(app.messages)), [{ type: 'open-card', card: 'alerts' }]);
  assert.equal(app.focused, 1);
  assert.equal(other.focused, 0, 'a window on another origin is never used');
  assert.deepEqual(w.opened, []);
});

test('notificationclick: forged notification data still can only open this origin', async () => {
  const w = fakeWorker();
  await w.click({ url: 'https://evil.example.test/', card: '"><img>' });
  await w.click(null);
  await w.click({ url: 'javascript:alert(1)' });
  assert.deepEqual(w.opened, [`${ORIGIN}/`, `${ORIGIN}/`, `${ORIGIN}/`]);
});

test('pushsubscriptionchange: registers the new subscription with the saved label and drops the old one', async () => {
  const w = fakeWorker();
  w.stores.set('mc-meta', new Map([['/__label', new Response('iPhone')]]));
  await w.run('pushsubscriptionchange', {
    oldSubscription: { endpoint: 'https://push.example.test/old' },
    newSubscription: { endpoint: 'https://push.example.test/new', toJSON: () => ({ endpoint: 'https://push.example.test/new' }) },
  });
  assert.deepEqual(w.posts.map((p) => p.path), ['/api/push/subscribe', '/api/push/unsubscribe']);
  for (const p of w.posts) assert.equal(p.init.headers['X-Mission-Control'], '1');
  assert.deepEqual(JSON.parse(w.posts[0].init.body), { subscription: { endpoint: 'https://push.example.test/new' }, label: 'iPhone' });
  assert.deepEqual(JSON.parse(w.posts[1].init.body), { endpoint: 'https://push.example.test/old' });
});

test('pushsubscriptionchange without a new subscription: subscribes with the key from /api/cards', async () => {
  const w = fakeWorker(async (r) => (new URL(r.url).pathname === '/api/cards' ? new Response(JSON.stringify(fx.cards())) : new Response('x')));
  await w.run('pushsubscriptionchange', {});
  assert.deepEqual(w.posts.map((p) => p.path), ['/api/push/subscribe']);
  assert.equal(JSON.parse(w.posts[0].init.body).label, 'Device');
});
