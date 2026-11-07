// The service worker in a fake worker scope (node:vm): precache, shell answers, the summary snapshot.

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const PUBLIC = process.env.AICTL_WEB_PUBLIC ? path.resolve(process.env.AICTL_WEB_PUBLIC) : path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'public');
const SRC = fs.readFileSync(path.join(PUBLIC, 'sw.js'), 'utf8');
const ORIGIN = 'https://ai-box.example.test';

function fakeWorker(network) {
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
  const self = {
    location: new URL(ORIGIN),
    addEventListener: (t, fn) => (listeners[t] = fn),
    skipWaiting: async () => {},
    clients: { claim: async () => {} },
  };
  const ctx = vm.createContext({ self, caches, fetch: (r) => network(r), Request: class extends Request {
    constructor(input, init) {
      super(new URL(input, ORIGIN), init);
    }
  }, Response, URL, Set, Promise, JSON, Date });
  vm.runInContext(SRC, ctx);
  return {
    stores,
    async install() {
      let p;
      listeners.install({ waitUntil: (x) => (p = x) });
      await p;
    },
    async activate() {
      let p;
      listeners.activate({ waitUntil: (x) => (p = x) });
      await p;
    },
    /** Dispatches a fetch; returns the Response the worker answered with, or null when it didn't. */
    async fetch(pathname, { mode = 'cors', method = 'GET' } = {}) {
      let answer = null;
      const request = { url: ORIGIN + pathname, method, mode };
      listeners.fetch({ request, respondWith: (p) => (answer = p) });
      return answer ? await answer : null;
    },
  };
}

const shellNetwork = async (r) => new Response(`file ${new URL(r.url).pathname}`, { status: 200 });

test('install precaches the whole shell into a versioned cache; activate drops old versions', async () => {
  const w = fakeWorker(shellNetwork);
  w.stores.set('aictl-shell-old', new Map());
  await w.install();
  await w.activate();
  const names = [...w.stores.keys()];
  assert.equal(names.filter((n) => n.startsWith('aictl-shell-')).length, 1);
  const shell = w.stores.get(names.find((n) => n.startsWith('aictl-shell-')));
  assert.ok(shell.has('/') && shell.has('/js/app.js') && shell.has('/css/app.css'));
});

test('shell files and navigations are served from the cache, even when the network returns 502', async () => {
  let down = false;
  const w = fakeWorker(async (r) => (down ? new Response('bad gateway', { status: 502 }) : shellNetwork(r)));
  await w.install();
  down = true;
  assert.equal(await (await w.fetch('/js/app.js')).text(), 'file /js/app.js');
  assert.equal(await (await w.fetch('/', { mode: 'navigate' })).text(), 'file /');
  assert.equal(await w.fetch('/api/logs?source=docker&lines=200'), null, 'other API calls are left alone');
  assert.equal(await w.fetch('/api/events'), null);
});

test('a 200 summary is saved with savedAt; a 5xx or 304 never replaces it', async () => {
  let reply = () => new Response(JSON.stringify({ server: { version: '1' } }), { status: 200 });
  const w = fakeWorker(async (r) => (new URL(r.url).pathname === '/api/summary' ? reply() : shellNetwork(r)));
  const res = await w.fetch('/api/summary');
  assert.equal(res.status, 200);
  await new Promise((r) => setTimeout(r, 10));
  const snap = JSON.parse(await w.stores.get('aictl-data-v1').get('/__snapshot').clone().text());
  assert.equal(snap.summary.server.version, '1');
  assert.equal(typeof snap.savedAt, 'number');
  // A JSON error body (what S sends) must not become the snapshot: only a 200 is saved.
  for (const status of [500, 502, 503, 404]) {
    reply = () => new Response(JSON.stringify({ error: 'x' }), { status, headers: { 'Content-Type': 'application/json' } });
    assert.equal((await w.fetch('/api/summary')).status, status, 'the error reaches the page');
  }
  reply = () => new Response(null, { status: 304 });
  await w.fetch('/api/summary');
  await new Promise((r) => setTimeout(r, 10));
  const again = JSON.parse(await w.stores.get('aictl-data-v1').get('/__snapshot').clone().text());
  assert.equal(again.savedAt, snap.savedAt);
  assert.deepEqual(again.summary, { server: { version: '1' } }, 'the snapshot is still the 200 summary');
});
