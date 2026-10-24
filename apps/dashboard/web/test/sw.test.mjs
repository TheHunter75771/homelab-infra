// public/sw.js in a VM with a fake ServiceWorkerGlobalScope: the versioned atomic shell, 5xx from the proxy
// never replacing it, /api/summary snapshots, push and notification clicks. The worker must stay at
// /sw.js with scope / (existing phone subscriptions belong to that registration).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { createCacheStorage } from './helpers.mjs';

const PUBLIC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../public');
const SW_SOURCE = fs.readFileSync(path.join(PUBLIC, 'sw.js'), 'utf8');
const VERSION = /const VERSION = '([^']+)'/.exec(SW_SOURCE)[1];
const SHELL = JSON.parse(`[${/const SHELL = \[([\s\S]*?)\];/.exec(SW_SOURCE)[1].replace(/'/g, '"').replace(/,\s*$/, '')}]`);
const SHELL_CACHE = `homelab-shell-${VERSION}`;
const ORIGIN = 'https://homelab.example';
const plain = (v) => JSON.parse(JSON.stringify(v)); // objects made in the VM have another realm's prototypes

// The network: a function (url, init) -> Response, or a status for every request.
function loadWorker({ windows = [], net = () => new Response('net', { status: 200 }), caches } = {}) {
  const handlers = {};
  const shown = [];
  const opened = [];
  const fetched = [];
  let skipped = 0;
  const fetchFn = async (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    fetched.push({ url, cache: input?.cache ?? init?.cache });
    return net(new URL(url, ORIGIN).pathname, init);
  };
  const store = caches || createCacheStorage({ fetcher: fetchFn });
  const self = {
    location: { origin: ORIGIN },
    addEventListener: (type, fn) => {
      handlers[type] = fn;
    },
    skipWaiting: async () => {
      skipped += 1;
    },
    registration: {
      showNotification: async (title, options) => shown.push({ title, options }),
      pushManager: { subscribe: async () => ({ endpoint: 'https://push.example/new', toJSON: () => ({ endpoint: 'https://push.example/new', keys: {} }) }) },
    },
    clients: {
      claim: async () => {},
      matchAll: async () => windows,
      openWindow: async (url) => {
        opened.push(url);
        return { url };
      },
    },
  };
  const RequestShim = class extends Request {
    constructor(input, init) {
      super(new URL(input, ORIGIN), init);
    }
  };
  vm.runInNewContext(SW_SOURCE, { self, URL, atob, fetch: fetchFn, caches: store, Request: RequestShim, Response, Promise, setTimeout, clearTimeout, console, Date, JSON });
  return { handlers, shown, opened, fetched, caches: store, skipped: () => skipped };
}

async function dispatch(handler, event) {
  const waits = [];
  let responded = null;
  handler({ ...event, waitUntil: (p) => waits.push(p), respondWith: (p) => (responded = Promise.resolve(p)) });
  await Promise.all(waits);
  return responded;
}
const req = (pathname, { method = 'GET', mode = 'cors' } = {}) => ({ request: { url: `${ORIGIN}${pathname}`, method, mode } });
const data = (v) => {
  const text = typeof v === 'string' ? v : JSON.stringify(v);
  return { json: () => JSON.parse(text), text: () => text };
};
const body = (p) => `body of ${p}`;
const goodNet = (p) => new Response(body(p), { status: 200 });

test('the worker file is /sw.js, registered with scope /, and stamped with the current shell', () => {
  const boot = fs.readFileSync(path.join(PUBLIC, 'js/boot.js'), 'utf8');
  assert.match(boot, /register\('\/sw\.js', \{ scope: '\/' \}\)/);
  assert.match(VERSION, /^[0-9a-f]{16}$/, 'run npm run stamp');
  assert.ok(SHELL.includes('/') && SHELL.includes('/vendor/preact.mjs') && SHELL.includes('/js/app.js'));
});

test('install precaches the whole shell into the versioned cache, bypassing the HTTP cache, and waits', async () => {
  const w = loadWorker({ net: goodNet });
  await dispatch(w.handlers.install, {});
  const keys = await (await w.caches.open(SHELL_CACHE)).keys();
  assert.deepEqual([...keys].sort(), [...SHELL].sort());
  assert.ok(w.fetched.every((f) => f.cache === 'reload'), 'cache: reload');
  assert.equal(w.skipped(), 0, 'a new version waits until the page lets it take over');
  await dispatch(w.handlers.message, { data: { type: 'skip-waiting' } });
  assert.equal(w.skipped(), 1);
  await dispatch(w.handlers.message, { data: { type: 'something-else' } });
  assert.equal(w.skipped(), 1);
});

test('install is atomic: one failed file (404, 502) fails the install and caches nothing', async () => {
  for (const status of [404, 502]) {
    const w = loadWorker({ net: (p) => (p === '/vendor/hooks.mjs' ? new Response('no', { status }) : goodNet(p)) });
    await assert.rejects(dispatch(w.handlers.install, {}), /addAll/);
    assert.deepEqual(await (await w.caches.open(SHELL_CACHE)).keys(), [], `${status}: nothing half-cached`);
  }
});

test('dashboard down behind the proxy: 502/503/504 never replace the cached shell, vendor files or index', async () => {
  for (const status of [502, 503, 504]) {
    let online = true;
    const w = loadWorker({ net: (p) => (online ? goodNet(p) : new Response(`${status} from tailscale serve`, { status })) });
    await dispatch(w.handlers.install, {});
    online = false;
    const before = w.fetched.length;
    for (const p of ['/js/app.js', '/vendor/preact.mjs', '/vendor/hooks.mjs', '/css/app.css', '/fonts/inter-latin-400-normal.woff2', '/icons/phosphor.svg']) {
      const res = await dispatch(w.handlers.fetch, req(p));
      assert.equal(res.status, 200, `${status} ${p}`);
      assert.equal(await res.text(), body(p));
    }
    const nav = await dispatch(w.handlers.fetch, req('/', { mode: 'navigate' }));
    assert.equal(await nav.text(), body('/'), 'the index comes from the cache');
    const navQuery = await dispatch(w.handlers.fetch, req('/?from=push#/drive', { mode: 'navigate' }));
    assert.equal(await navQuery.text(), body('/'));
    assert.equal(w.fetched.length, before, 'a cached shell file never goes to the network');
  }
});

test('a shell file missing from the cache: the network answer is used, but only a 2xx is kept', async () => {
  for (const status of [502, 503, 504]) {
    let answer = status;
    const caches = createCacheStorage();
    const w = loadWorker({ caches, net: (p) => new Response(answer === 200 ? body(p) : 'proxy error', { status: answer }) });
    const bad = await dispatch(w.handlers.fetch, req('/js/ui.js'));
    assert.equal(bad.status, status, 'nothing cached: the network answer as it is');
    assert.equal(await (await caches.open(SHELL_CACHE)).match('/js/ui.js'), undefined, `${status} is not stored`);
    answer = 200;
    const good = await dispatch(w.handlers.fetch, req('/js/ui.js'));
    assert.equal(await good.text(), body('/js/ui.js'));
    assert.ok(await (await caches.open(SHELL_CACHE)).match('/js/ui.js'), 'a 200 is stored');
  }
  const w = loadWorker({ caches: createCacheStorage(), net: () => Promise.reject(new TypeError('offline')) });
  const res = await dispatch(w.handlers.fetch, req('/js/ui.js'));
  assert.equal(res.type, 'error');
});

test('/api/summary: a 200 is saved with its time; a 502/503/504 passes through and never replaces it', async () => {
  let status = 200;
  const caches = createCacheStorage();
  const w = loadWorker({ caches, net: () => (status === 200 ? new Response(JSON.stringify({ schema: 1, gate: { status: 'failed' } }), { status }) : new Response('<html>proxy</html>', { status })) });
  const before = Date.now();
  assert.equal((await dispatch(w.handlers.fetch, req('/api/summary'))).status, 200);
  await new Promise((r) => setTimeout(r, 20));
  const read = async () => (await (await caches.open('homelab-data-v1')).match('/__snapshot')).json();
  const first = await read();
  assert.deepEqual(first.summary, { schema: 1, gate: { status: 'failed' } });
  assert.ok(first.savedAt >= before);
  for (status of [502, 503, 504]) {
    const res = await dispatch(w.handlers.fetch, req('/api/summary'));
    assert.equal(res.status, status, 'the page sees the error and falls back to the snapshot itself');
    await new Promise((r) => setTimeout(r, 10));
    assert.deepEqual(await read(), first, `${status} kept the last-known summary`);
  }
});

test('only the shell paths and /api/summary are answered; other apps, streams and writes pass through', async () => {
  const w = loadWorker({ net: goodNet });
  for (const [p, o] of [
    ['/sonarr/api/v3/queue'],
    ['/sonarr/', { mode: 'navigate' }],
    ['/watch/web/index.html', { mode: 'navigate' }],
    ['/watch/main.js'],
    ['/api/events'],
    ['/api/apps/sonarr/logs?follow=1'],
    ['/api/settings', { method: 'PUT' }],
  ]) {
    assert.equal(await dispatch(w.handlers.fetch, req(p, o)), null, p);
  }
  assert.equal(await dispatch(w.handlers.fetch, { request: { url: 'https://cdn.example/x.js', method: 'GET', mode: 'cors' } }), null);
});

test('activate drops the old shell versions only, and claims the page', async () => {
  const caches = createCacheStorage();
  await (await caches.open('homelab-shell-0123456789abcdef')).put('/x', new Response('old'));
  await (await caches.open('push-core-meta')).put('/__label', new Response('iPhone'));
  await (await caches.open('homelab-data-v1')).put('/__snapshot', new Response('{}'));
  await (await caches.open(SHELL_CACHE)).put('/', new Response('new'));
  const w = loadWorker({ caches });
  await dispatch(w.handlers.activate, {});
  assert.deepEqual((await caches.keys()).sort(), ['homelab-data-v1', SHELL_CACHE, 'push-core-meta'].sort());
});

test('push: contract payload → notification; tag null → no tag; junk still shows something', async () => {
  const w = loadWorker();
  await dispatch(w.handlers.push, { data: data({ title: 'Media drive failed', body: 'Docker stays down.', tag: 'bootgate', url: '/' }) });
  assert.deepEqual(plain(w.shown[0]), {
    title: 'Media drive failed',
    options: { body: 'Docker stays down.', icon: '/icons/icon-192.png', badge: '/icons/badge-96.png', data: { url: `${ORIGIN}/` }, tag: 'bootgate', renotify: true },
  });
  await dispatch(w.handlers.push, { data: data({ title: 'T', body: 'B', tag: null, url: '/' }) });
  assert.equal('tag' in w.shown[1].options, false, 'renotify without a tag throws in browsers');
  await dispatch(w.handlers.push, { data: null });
  await dispatch(w.handlers.push, { data: data('plain text alert') });
  await dispatch(w.handlers.push, { data: data({ title: '  ', body: 42, url: 'https://evil.example/x' }) });
  assert.deepEqual(
    w.shown.slice(2).map((s) => [s.title, s.options.body, s.options.data.url]),
    [
      ['Homelab alert', '', `${ORIGIN}/`],
      ['Homelab alert', 'plain text alert', `${ORIGIN}/`],
      ['Homelab alert', '', `${ORIGIN}/`],
    ],
  );
});

test('notification click: reuse only a window exactly on the target path; never navigate other tabs', async () => {
  const focused = [];
  const navigated = [];
  const win = (url) => ({ url, focus: async () => focused.push(url), navigate: async (u) => navigated.push(u), postMessage: () => {} });
  const w = loadWorker({ windows: [win(`${ORIGIN}/sonarr/settings`), win(`${ORIGIN}/#/apps`)] });
  await dispatch(w.handlers.notificationclick, { notification: { close() {}, data: { url: `${ORIGIN}/` } } });
  assert.deepEqual(focused, [`${ORIGIN}/#/apps`], 'the dashboard window (path /) is reused');
  assert.deepEqual(w.opened, []);
  const w2 = loadWorker({ windows: [win(`${ORIGIN}/sonarr/`), win(`${ORIGIN}/watch/web/index.html`)] });
  await dispatch(w2.handlers.notificationclick, { notification: { close() {}, data: { url: `${ORIGIN}/` } } });
  assert.deepEqual(w2.opened, [`${ORIGIN}/`]);
  assert.deepEqual(navigated, []);
  const msgs = [];
  const w3 = loadWorker({ windows: [{ ...win(`${ORIGIN}/`), postMessage: (m) => msgs.push(m) }] });
  await dispatch(w3.handlers.notificationclick, { notification: { close() {}, data: { url: `${ORIGIN}/#/drive` } } });
  assert.deepEqual(plain(msgs), [{ type: 'open-route', hash: '#/drive' }]);
  assert.deepEqual(navigated, []);
});

test('pushsubscriptionchange re-registers with the stored label, X-Homelab and JSON', async () => {
  const caches = createCacheStorage();
  await (await caches.open('push-core-meta')).put('/__label', new Response('Kitchen iPad'));
  const posts = [];
  const w = loadWorker({
    caches,
    net: (p, init) => {
      if (init?.method === 'POST') posts.push({ p, headers: init.headers, body: JSON.parse(init.body) });
      return new Response('{}');
    },
  });
  const sub = { endpoint: 'https://push.example/new', toJSON: () => ({ endpoint: 'https://push.example/new', keys: { p256dh: 'p', auth: 'a' } }) };
  await dispatch(w.handlers.pushsubscriptionchange, { newSubscription: sub, oldSubscription: { endpoint: 'https://push.example/old' } });
  assert.deepEqual(
    posts.map((x) => [x.p, x.headers['X-Homelab'], x.headers['Content-Type'], x.body.label ?? x.body.endpoint]),
    [
      ['/api/subscribe', '1', 'application/json', 'Kitchen iPad'],
      ['/api/unsubscribe', '1', 'application/json', 'https://push.example/old'],
    ],
  );
});
