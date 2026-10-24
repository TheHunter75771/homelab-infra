// Guards on every non-GET route, routing, and static serving.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { createApp, matchRoute } from '../src/http/app.js';
import { createStatic } from '../src/http/static.js';
import { fakeSubscription } from './helpers/push-fixtures.js';
import { getJson, rawRequest, request, startDashboard, WRITE_HEADERS } from './helpers/server.js';
import { tempDir, writeWeb } from './helpers/host.js';

const WRITES = [
  ['POST', '/api/subscribe'],
  ['POST', '/api/unsubscribe'],
  ['POST', '/api/test'],
  ['POST', '/api/apps/sonarr/restart'],
  ['POST', '/api/apps/sonarr/update'],
  ['POST', '/api/gate/rerun'],
  ['DELETE', `/api/push/devices/${'0'.repeat(16)}`],
  ['PUT', '/api/settings'],
];

test('every write route: missing/wrong X-Homelab -> 403, wrong type -> 415, > 8 KB -> 413, cross-site -> 403', async (t) => {
  const d = await startDashboard(t, 'normal');
  for (const [method, p] of WRITES) {
    for (const headers of [{ 'Content-Type': 'application/json' }, { ...WRITE_HEADERS, 'X-Homelab': '0' }]) {
      const r = await request(d.base, method, p, { body: '{}', headers });
      assert.equal(r.status, 403, `${method} ${p}`);
    }
    for (const type of ['text/plain', 'application/x-www-form-urlencoded', 'application/json; charset=utf-16']) {
      const r = await request(d.base, method, p, { body: '{}', headers: { 'X-Homelab': '1', 'Content-Type': type } });
      assert.equal(r.status, 415, `${method} ${p} ${type}`);
    }
    const none = await rawRequest(d.port, { method, path: p, headers: { 'X-Homelab': '1', 'Content-Length': '2' }, chunks: ['{}'] });
    assert.equal(none.status, 415, `${method} ${p} without a content type`);
    const big = JSON.stringify({ label: 'x'.repeat(9000) });
    assert.equal((await request(d.base, method, p, { body: big })).status, 413, `${method} ${p} declared`);
    const chunked = await rawRequest(d.port, { method, path: p, headers: { ...WRITE_HEADERS, 'Transfer-Encoding': 'chunked' }, chunks: [big.slice(0, 4000), big.slice(4000)] });
    assert.equal(chunked.status, 413, `${method} ${p} chunked`);
    for (const site of ['cross-site', 'same-site']) {
      assert.equal((await request(d.base, method, p, { body: '{}', headers: { ...WRITE_HEADERS, 'Sec-Fetch-Site': site } })).status, 403);
    }
    assert.equal((await request(d.base, method, p, { body: '[1]' })).status, 400, `${method} ${p} non-object`);
    assert.equal((await request(d.base, method, p, { body: '{"a":' })).status, 400, `${method} ${p} bad JSON`);
  }
  // nothing above may have acted
  assert.equal(d.helper.dockerCalls().filter((c) => c.op !== 'ps').length, 0);
  assert.equal(fs.existsSync(d.host.path('run/bootgate/rerun')), false);
});

test('guard order: header first, then content type; a body of exactly 8 KB passes', async (t) => {
  const d = await startDashboard(t, 'clean');
  assert.equal((await request(d.base, 'POST', '/api/subscribe', { body: 'x'.repeat(9000), headers: { 'Content-Type': 'text/plain' } })).status, 403);
  assert.equal((await request(d.base, 'POST', '/api/subscribe', { body: 'x'.repeat(9000), headers: { 'X-Homelab': '1', 'Content-Type': 'text/plain' } })).status, 415);
  const base = JSON.stringify({ subscription: fakeSubscription(), label: '' });
  const padded = base.replace('"label":""', `"label":"${'a'.repeat(8192 - base.length)}"`);
  assert.equal(Buffer.byteLength(padded), 8192);
  assert.equal((await request(d.base, 'POST', '/api/subscribe', { body: padded })).status, 201);
});

test('oversized Content-Length is refused before the body is read', { timeout: 5000 }, async (t) => {
  const d = await startDashboard(t, 'clean');
  const status = await new Promise((resolve, reject) => {
    const req = http.request(
      { host: '127.0.0.1', port: d.port, method: 'PUT', path: '/api/settings', headers: { ...WRITE_HEADERS, 'Content-Length': '1000000' } },
      (res) => {
        res.resume();
        resolve(res.statusCode);
        req.destroy();
      },
    );
    req.on('error', (err) => (err.code === 'ECONNRESET' ? null : reject(err)));
    req.setTimeout(2000, () => reject(new Error('server waited for the body')));
    req.write('{"thresholds":');
  });
  assert.equal(status, 413);
});

test('routing: 405 with Allow, 404 for unknown API paths and bad params, SSE routes reject HEAD', async (t) => {
  const d = await startDashboard(t, 'normal');
  const cases = [
    ['GET', '/api/subscribe', 405, 'POST'],
    ['POST', '/healthz', 405, 'GET, HEAD'],
    ['GET', '/api/apps/sonarr/restart', 405, 'POST'],
    ['PUT', '/api/summary', 405, 'GET, HEAD'],
    ['POST', '/api/settings', 405, 'GET, HEAD, PUT'],
    ['HEAD', '/api/events', 405, 'GET'],
    ['GET', '/api/push/devices/abc', 405, 'DELETE'],
  ];
  for (const [method, p, status, allow] of cases) {
    const r = await rawRequest(d.port, { method, path: p, headers: WRITE_HEADERS, chunks: method === 'GET' || method === 'HEAD' ? [] : ['{}'] });
    assert.equal(r.status, status, `${method} ${p}`);
    assert.equal(r.headers.allow, allow, `${method} ${p}`);
  }
  for (const p of ['/api/nope', '/api/apps/no-such-app/logs', '/api/apps/..%2f/logs', '/api/actions/missing', '/api/apps/sonarr/explode']) {
    const r = await rawRequest(d.port, { method: p.endsWith('explode') ? 'POST' : 'GET', path: p, headers: WRITE_HEADERS, chunks: p.endsWith('explode') ? ['{}'] : [] });
    assert.equal(r.status, 404, p);
  }
  const preflight = await rawRequest(d.port, { method: 'OPTIONS', path: '/api/settings' });
  assert.equal(preflight.status, 405, 'no CORS preflight is answered');
  assert.equal(preflight.headers['access-control-allow-origin'], undefined);
});

test('matchRoute extracts params and refuses odd segments', () => {
  assert.deepEqual(matchRoute('POST', '/api/apps/wol-relay/restart').params, { name: 'wol-relay', action: 'restart' });
  assert.equal(matchRoute('GET', `/api/apps/${'a'.repeat(65)}/logs`).route, null);
  assert.deepEqual(matchRoute('GET', '/api/apps/a b/logs').allowed, []);
});

test('static: index, assets with types and ETag, SPA fallback, missing assets 404, no traversal, security headers', async (t) => {
  const d = await startDashboard(t, 'clean');
  const index = await fetch(`${d.base}/`);
  assert.equal(index.status, 200);
  assert.match(index.headers.get('content-type'), /^text\/html/);
  assert.match(index.headers.get('content-security-policy'), /script-src 'self'/);
  assert.match(index.headers.get('content-security-policy'), /font-src 'self'/);
  assert.equal(index.headers.get('x-frame-options'), 'DENY');
  for (const [p, type] of [
    ['/sw.js', /^text\/javascript/],
    ['/assets/app.js', /^text\/javascript/],
    ['/manifest.webmanifest', /^application\/manifest\+json/],
    ['/assets/inter.woff2', /^font\/woff2/],
  ]) {
    const r = await fetch(`${d.base}${p}`);
    assert.equal(r.status, 200, p);
    assert.match(r.headers.get('content-type'), type, p);
    assert.equal(r.headers.get('cache-control'), 'no-cache');
    assert.equal((await fetch(`${d.base}${p}`, { headers: { 'If-None-Match': r.headers.get('etag') } })).status, 304);
  }
  for (const p of ['/apps', '/apps/sonarr', '/drive', '/settings']) {
    const r = await fetch(`${d.base}${p}`);
    assert.equal(r.status, 200, p);
    assert.match(await r.text(), /<title>Homelab dashboard<\/title>/, `${p} gets the app shell`);
  }
  for (const p of ['/missing.js', '/../package.json', '/%2e%2e/src/main.js', '/assets/../../src/main.js', '/.hidden']) {
    const r = await rawRequest(d.port, { method: 'GET', path: p });
    assert.equal(r.status, 404, p);
  }
});

test('static: a missing web build serves a 503 page, the API keeps working', async (t) => {
  const d = await startDashboard(t, 'clean', { web: false });
  const r = await fetch(`${d.base}/`);
  assert.equal(r.status, 503);
  assert.match(await r.text(), /web build not installed/i);
  assert.equal((await getJson(d.base, '/healthz')).status, 200);
  assert.match(d.text(), /"msg":"web build not found"/);
});

test('static: symlinks and dotfiles in the web dir are never served', (t) => {
  const dir = tempDir(t);
  writeWeb(dir);
  fs.writeFileSync(path.join(dir, '.env'), 'SECRET=1');
  fs.symlinkSync('/etc/passwd', path.join(dir, 'passwd.txt'));
  const st = createStatic({ webDir: dir });
  const res = { writeHead() {}, end() {} };
  assert.equal(st.handle({ headers: {} }, res, '/.env'), false);
  assert.equal(st.handle({ headers: {} }, res, '/passwd.txt'), false);
  assert.ok(st.available());
});

test('createApp is the only HTTP surface: unknown methods on static paths are 405', async (t) => {
  const d = await startDashboard(t, 'clean');
  assert.equal((await rawRequest(d.port, { method: 'POST', path: '/index.html', chunks: ['x'] })).status, 405);
  assert.equal(typeof createApp, 'function');
});
