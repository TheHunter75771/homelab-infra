import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { deviceId } from '../src/log.js';
import {
  fakeSubscription,
  JSON_HEADERS,
  postJson,
  pushError,
  readPng,
  readSubs,
  startApp,
  stubWebpush,
} from './helpers/fixtures.js';

const POSTS = ['/api/subscribe', '/api/unsubscribe', '/api/test'];

// Raw request so tests control every header (fetch adds its own).
function rawRequest(port, { method = 'POST', path: p, headers = {}, chunks = [] }) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, method, path: p, headers }, (res) => {
      const body = [];
      res.on('data', (c) => body.push(c));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(body).toString() }));
    });
    req.on('error', reject);
    for (const c of chunks) req.write(c);
    req.end();
  });
}

// ---- CSRF guard and body limits ------------------------------------------------------------------

for (const p of POSTS) {
  test(`${p}: missing or wrong X-Homelab header -> 403`, async (t) => {
    const app = await startApp(t);
    for (const headers of [{ 'Content-Type': 'application/json' }, { ...JSON_HEADERS, 'X-Homelab': '0' }]) {
      const r = await postJson(app.base, p, {}, headers);
      assert.equal(r.status, 403);
      assert.match(r.json.error, /X-Homelab/);
    }
  });

  test(`${p}: wrong or missing content type -> 415`, async (t) => {
    const app = await startApp(t);
    for (const type of ['text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data; boundary=x']) {
      const r = await postJson(app.base, p, '{}', { 'X-Homelab': '1', 'Content-Type': type });
      assert.equal(r.status, 415, type);
    }
    const none = await rawRequest(app.port, { path: p, headers: { 'X-Homelab': '1' }, chunks: ['{}'] });
    assert.equal(none.status, 415);
    const utf16 = await postJson(app.base, p, '{}', { 'X-Homelab': '1', 'Content-Type': 'application/json; charset=utf-16' });
    assert.equal(utf16.status, 415);
  });

  test(`${p}: body over 8 KB -> 413 (declared length and chunked)`, async (t) => {
    const app = await startApp(t);
    const big = JSON.stringify({ label: 'x'.repeat(9000) });
    const declared = await postJson(app.base, p, big);
    assert.equal(declared.status, 413);
    const chunked = await rawRequest(app.port, {
      path: p,
      headers: { ...JSON_HEADERS, 'Transfer-Encoding': 'chunked' },
      chunks: [big.slice(0, 4000), big.slice(4000)],
    });
    assert.equal(chunked.status, 413);
    assert.equal(fs.existsSync(path.join(app.dir, 'subscriptions.json')), false);
  });
}

test('oversized Content-Length is refused before the body is read (no slow-upload hold)', { timeout: 5000 }, async (t) => {
  const app = await startApp(t);
  const started = Date.now();
  const status = await new Promise((resolve, reject) => {
    const req = http.request(
      {
        host: '127.0.0.1',
        port: app.port,
        method: 'POST',
        path: '/api/subscribe',
        headers: { ...JSON_HEADERS, 'Content-Length': '1000000' },
      },
      (res) => {
        res.resume();
        resolve(res.statusCode);
        req.destroy();
      },
    );
    req.on('error', (err) => (err.code === 'ECONNRESET' ? null : reject(err)));
    req.setTimeout(2000, () => {
      req.destroy();
      reject(new Error('server waited for the body instead of answering 413'));
    });
    req.write('{"subscription":'); // never finish the body
  });
  assert.equal(status, 413);
  assert.ok(Date.now() - started < 2000, 'answered without waiting for the body');
});

test('guard order: header is checked before content type and size', async (t) => {
  const app = await startApp(t);
  const r = await postJson(app.base, '/api/subscribe', 'x'.repeat(9000), { 'Content-Type': 'text/plain' });
  assert.equal(r.status, 403);
  const r2 = await postJson(app.base, '/api/subscribe', 'x'.repeat(9000), { 'X-Homelab': '1', 'Content-Type': 'text/plain' });
  assert.equal(r2.status, 415);
});

test('body of exactly 8 KB is accepted by the guard', async (t) => {
  const app = await startApp(t);
  const base = JSON.stringify({ subscription: fakeSubscription(), label: '' });
  const padded = base.replace('"label":""', `"label":"${'a'.repeat(8192 - base.length)}"`);
  assert.equal(Buffer.byteLength(padded), 8192);
  const r = await postJson(app.base, '/api/subscribe', padded);
  assert.equal(r.status, 201);
  assert.equal(readSubs(app.dir)[0].label.length, 64);
});

test('cross-site Sec-Fetch-Site is refused; same-origin and none pass', async (t) => {
  const app = await startApp(t);
  for (const site of ['cross-site', 'same-site']) {
    const r = await postJson(app.base, '/api/test', {}, { ...JSON_HEADERS, 'Sec-Fetch-Site': site });
    assert.equal(r.status, 403, site);
  }
  for (const site of ['same-origin', 'none']) {
    const r = await postJson(app.base, '/api/test', {}, { ...JSON_HEADERS, 'Sec-Fetch-Site': site });
    assert.equal(r.status, 200, site);
  }
});

test('server-to-server prune (host notifier): no Sec-Fetch-Site header is accepted', async (t) => {
  const app = await startApp(t);
  const dead = fakeSubscription();
  const alive = fakeSubscription();
  await postJson(app.base, '/api/subscribe', { subscription: dead, label: 'dead' });
  await postJson(app.base, '/api/subscribe', { subscription: alive, label: 'alive' });
  // Exactly what the notifier sends: only these two headers (+ Host/Content-Length), as Python urllib would.
  const body = JSON.stringify({ endpoint: dead.endpoint });
  const r = await rawRequest(app.port, {
    path: '/api/unsubscribe',
    headers: { 'X-Homelab': '1', 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
    chunks: [body],
  });
  assert.equal(r.status, 200, r.body);
  assert.equal(JSON.parse(r.body).removed, true);
  assert.deepEqual(readSubs(app.dir).map((e) => e.label), ['alive']);
  // Repeating a prune (e.g. two notifier runs) is harmless.
  const again = await rawRequest(app.port, {
    path: '/api/unsubscribe',
    headers: { 'X-Homelab': '1', 'Content-Type': 'application/json' },
    chunks: [body],
  });
  assert.equal(again.status, 200);
  assert.equal(JSON.parse(again.body).removed, false);
  // The CSRF header is still mandatory server-to-server.
  const noHeader = await rawRequest(app.port, {
    path: '/api/unsubscribe',
    headers: { 'Content-Type': 'application/json' },
    chunks: [body],
  });
  assert.equal(noHeader.status, 403);
});

test('malformed JSON -> 400; non-object JSON -> 400; empty body allowed', async (t) => {
  const app = await startApp(t);
  assert.equal((await postJson(app.base, '/api/subscribe', '{"subscription":')).status, 400);
  assert.equal((await postJson(app.base, '/api/subscribe', '[1,2]')).status, 400);
  assert.equal((await postJson(app.base, '/api/subscribe', 'null')).status, 400);
  assert.equal((await postJson(app.base, '/api/test', '')).status, 200);
});

test('wrong methods -> 405 with Allow; unknown paths and traversal -> 404', async (t) => {
  const app = await startApp(t);
  const get = await fetch(`${app.base}/api/subscribe`);
  assert.equal(get.status, 405);
  assert.equal(get.headers.get('allow'), 'POST');
  const post = await fetch(`${app.base}/healthz`, { method: 'POST', headers: JSON_HEADERS, body: '{}' });
  assert.equal(post.status, 405);
  assert.equal(post.headers.get('allow'), 'GET, HEAD');
  const options = await rawRequest(app.port, { method: 'OPTIONS', path: '/api/subscribe' });
  assert.equal(options.status, 405, 'no CORS preflight is ever answered');
  assert.equal(options.headers['access-control-allow-origin'], undefined);
  const putStatic = await fetch(`${app.base}/sw.js`, { method: 'PUT', body: 'x' });
  assert.equal(putStatic.status, 405);
  for (const p of ['/nope', '/package.json', '/../package.json', '/icons/../../src/app.js', '/%2e%2e/package.json', '/src/server.js']) {
    const r = await rawRequest(app.port, { method: 'GET', path: p });
    assert.equal(r.status, 404, p);
  }
});

// ---- endpoints -----------------------------------------------------------------------------------

test('GET /api/vapid-public-key returns the public key only', async (t) => {
  const app = await startApp(t);
  const res = await fetch(`${app.base}/api/vapid-public-key`);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get('cache-control'), 'no-store');
  const body = await res.json();
  assert.deepEqual(body, { publicKey: app.keys.publicKey });
});

test('GET /api/vapid-public-key -> 503 when vapid.json is missing or invalid', async (t) => {
  const app = await startApp(t, { vapid: false });
  assert.equal((await fetch(`${app.base}/api/vapid-public-key`)).status, 503);
  fs.writeFileSync(path.join(app.dir, 'vapid.json'), '{"publicKey":"abc","privateKey":"def","subject":"x"}');
  assert.equal((await fetch(`${app.base}/api/vapid-public-key`)).status, 503);
  assert.doesNotMatch(app.text(), /def/, 'no key material in logs');
});

test('vapid.json changes are picked up without a restart', async (t) => {
  const app = await startApp(t, { vapid: false });
  assert.equal((await fetch(`${app.base}/api/vapid-public-key`)).status, 503);
  const { writeVapid } = await import('./helpers/fixtures.js');
  const keys = writeVapid(app.dir);
  const body = await (await fetch(`${app.base}/api/vapid-public-key`)).json();
  assert.equal(body.publicKey, keys.publicKey);
});

test('subscribe -> 201, again -> 200 (dedupe); responses never expose endpoints or keys', async (t) => {
  const app = await startApp(t);
  const sub = fakeSubscription();
  const first = await postJson(app.base, '/api/subscribe', { subscription: sub, label: 'Pixel' });
  assert.equal(first.status, 201);
  assert.equal(first.json.created, true);
  assert.deepEqual(first.json.device, { id: deviceId(sub.endpoint), label: 'Pixel', created: readSubs(app.dir)[0].created });
  assert.equal(first.json.devices.length, 1);
  const again = await postJson(app.base, '/api/subscribe', { subscription: sub, label: 'Pixel 9' });
  assert.equal(again.status, 200);
  assert.equal(again.json.created, false);
  assert.equal(readSubs(app.dir).length, 1);
  assert.equal(readSubs(app.dir)[0].label, 'Pixel 9');
  for (const r of [first, again]) {
    assert.ok(!r.text.includes(sub.endpoint));
    assert.ok(!r.text.includes(sub.keys.p256dh));
    assert.ok(!r.text.includes(sub.keys.auth));
  }
});

test('subscribe with an invalid subscription -> 400 and nothing written', async (t) => {
  const app = await startApp(t);
  const r = await postJson(app.base, '/api/subscribe', { subscription: { endpoint: 'https://evil.example/x', keys: {} } });
  assert.equal(r.status, 400);
  assert.match(r.json.error, /push service/);
  assert.equal(fs.existsSync(path.join(app.dir, 'subscriptions.json')), false);
});

test('subscription cap -> 409', async (t) => {
  const app = await startApp(t, { storeOptions: { maxEntries: 1 } });
  assert.equal((await postJson(app.base, '/api/subscribe', { subscription: fakeSubscription() })).status, 201);
  assert.equal((await postJson(app.base, '/api/subscribe', { subscription: fakeSubscription() })).status, 409);
});

test('unsubscribe removes the entry; unknown endpoint -> 200 removed:false; missing endpoint -> 400', async (t) => {
  const app = await startApp(t);
  const sub = fakeSubscription();
  await postJson(app.base, '/api/subscribe', { subscription: sub, label: 'a' });
  const r = await postJson(app.base, '/api/unsubscribe', { endpoint: sub.endpoint });
  assert.equal(r.status, 200);
  assert.deepEqual(r.json, { ok: true, removed: true, devices: [] });
  assert.deepEqual(readSubs(app.dir), []);
  const again = await postJson(app.base, '/api/unsubscribe', { endpoint: sub.endpoint });
  assert.equal(again.json.removed, false);
  assert.equal((await postJson(app.base, '/api/unsubscribe', {})).status, 400);
});

test('corrupt store: writes -> 503 and the file is not overwritten; healthz -> 503; page still renders', async (t) => {
  const app = await startApp(t);
  const file = path.join(app.dir, 'subscriptions.json');
  fs.writeFileSync(file, '{not json');
  const sub = fakeSubscription();
  assert.equal((await postJson(app.base, '/api/subscribe', { subscription: sub })).status, 503);
  assert.equal((await postJson(app.base, '/api/unsubscribe', { endpoint: sub.endpoint })).status, 503);
  assert.equal((await postJson(app.base, '/api/test', {})).status, 503);
  assert.equal(fs.readFileSync(file, 'utf8'), '{not json');
  const health = await fetch(`${app.base}/healthz`);
  assert.equal(health.status, 503);
  assert.equal((await health.json()).checks.store, 'corrupt');
  const page = await fetch(`${app.base}/`);
  assert.equal(page.status, 200);
  assert.match(await page.text(), /"storeError":true/);
  assert.match(app.text(), /store corrupt; refusing to write/);
});

test('unreadable store (permissions): writes -> 500, healthz -> 503 store:error, file untouched', async (t) => {
  if (process.getuid?.() === 0) return t.skip('root ignores mode bits');
  const app = await startApp(t);
  const file = path.join(app.dir, 'subscriptions.json');
  fs.writeFileSync(file, '[]');
  fs.chmodSync(file, 0o000);
  try {
    assert.equal((await postJson(app.base, '/api/subscribe', { subscription: fakeSubscription() })).status, 500);
    const health = await fetch(`${app.base}/healthz`);
    assert.equal(health.status, 503);
    assert.equal((await health.json()).checks.store, 'error');
  } finally {
    fs.chmodSync(file, 0o600);
  }
  assert.equal(fs.readFileSync(file, 'utf8'), '[]');
  assert.match(app.text(), /"code":"EACCES"/);
});

test('GET /healthz: 200 when healthy; 503 names the failing check', async (t) => {
  const app = await startApp(t);
  const ok = await fetch(`${app.base}/healthz`);
  assert.equal(ok.status, 200);
  assert.deepEqual(await ok.json(), { ok: true, checks: { vapid: 'ok', store: 'ok', writable: true } });
  fs.rmSync(path.join(app.dir, 'vapid.json'));
  const missing = await fetch(`${app.base}/healthz`);
  assert.equal(missing.status, 503);
  assert.equal((await missing.json()).checks.vapid, 'missing');
  const head = await fetch(`${app.base}/healthz`, { method: 'HEAD' });
  assert.equal(head.status, 503);
});

// ---- send path (web-push stubbed, no network) ------------------------------------------------------

test('POST /api/test sends the contract payload with the stored keys and VAPID details', async (t) => {
  const wp = stubWebpush();
  const app = await startApp(t, { webpush: wp });
  const a = fakeSubscription();
  const b = fakeSubscription('web.push.apple.com');
  await postJson(app.base, '/api/subscribe', { subscription: a, label: 'a' });
  await postJson(app.base, '/api/subscribe', { subscription: b, label: 'b' });
  const r = await postJson(app.base, '/api/test', {});
  assert.equal(r.status, 200);
  assert.deepEqual(
    { ...r.json, devices: r.json.devices.length },
    { ok: true, total: 2, sent: 2, failed: 0, removed: 0, devices: 2 },
  );
  assert.equal(wp.calls.length, 2);
  for (const [i, sub] of [a, b].entries()) {
    const call = wp.calls[i];
    assert.deepEqual(call.subscription, { endpoint: sub.endpoint, keys: sub.keys });
    assert.deepEqual(JSON.parse(call.payload), {
      title: 'Test from homelab',
      body: 'Push alerts are working on this device.',
      tag: 'test',
      url: '/',
    });
    assert.deepEqual(call.options.vapidDetails, {
      subject: app.keys.subject,
      publicKey: app.keys.publicKey,
      privateKey: app.keys.privateKey,
    });
    assert.equal(call.options.contentEncoding, 'aes128gcm');
    assert.equal(call.options.TTL, 3600);
  }
});

test('POST /api/test removes 404/410 subscriptions, keeps other failures, and never logs endpoints', async (t) => {
  const subs = { ok: fakeSubscription(), gone: fakeSubscription(), missing: fakeSubscription(), flaky: fakeSubscription() };
  const byEndpoint = new Map(Object.entries(subs).map(([k, s]) => [s.endpoint, k]));
  const wp = stubWebpush((sub) => {
    switch (byEndpoint.get(sub.endpoint)) {
      case 'gone':
        return pushError(410, `push subscription has unsubscribed or expired. ${sub.endpoint}`);
      case 'missing':
        return pushError(404, 'Not Found');
      case 'flaky':
        return pushError(500, 'internal error');
      default:
        return { statusCode: 201 };
    }
  });
  const app = await startApp(t, { webpush: wp });
  for (const [label, s] of Object.entries(subs)) await postJson(app.base, '/api/subscribe', { subscription: s, label });
  const r = await postJson(app.base, '/api/test', {});
  assert.equal(r.status, 200);
  assert.equal(r.json.total, 4);
  assert.equal(r.json.sent, 1);
  assert.equal(r.json.failed, 3);
  assert.equal(r.json.removed, 2);
  assert.deepEqual(readSubs(app.dir).map((e) => e.label).sort(), ['flaky', 'ok']);
  assert.deepEqual(r.json.devices.map((d) => d.label).sort(), ['flaky', 'ok']);

  const logs = app.text();
  for (const s of Object.values(subs)) {
    assert.ok(!logs.includes(s.endpoint), 'full endpoint must never be logged');
    assert.ok(!logs.includes(s.keys.p256dh) && !logs.includes(s.keys.auth), 'keys must never be logged');
  }
  assert.ok(!logs.includes(app.keys.privateKey), 'VAPID private key must never be logged');
  assert.match(logs, /"msg":"push failed".*"status":410.*"gone":true/);
  assert.match(logs, /fcm\.googleapis\.com#[0-9a-f]{16}/, 'endpoints are logged as host#id');
});

test('POST /api/test: every send fails -> 502 and nothing removed', async (t) => {
  const wp = stubWebpush(() => pushError(403, '{"reason":"BadJwtToken"}'));
  const app = await startApp(t, { webpush: wp });
  await postJson(app.base, '/api/subscribe', { subscription: fakeSubscription('web.push.apple.com'), label: 'iphone' });
  const r = await postJson(app.base, '/api/test', {});
  assert.equal(r.status, 502);
  assert.equal(r.json.sent, 0);
  assert.equal(readSubs(app.dir).length, 1);
  assert.match(app.text(), /BadJwtToken/, 'push service reason is logged for diagnosis');
});

test('POST /api/test with no subscriptions -> 200 total 0 without calling web-push', async (t) => {
  const wp = stubWebpush();
  const app = await startApp(t, { webpush: wp });
  const r = await postJson(app.base, '/api/test', {});
  assert.equal(r.status, 200);
  assert.equal(r.json.total, 0);
  assert.equal(wp.calls.length, 0);
});

test('POST /api/test is rate limited (429 + Retry-After) and 503 without VAPID keys', async (t) => {
  let now = 1_000_000;
  const app = await startApp(t, { appOptions: { clock: () => now, testCooldownMs: 10_000 } });
  await postJson(app.base, '/api/subscribe', { subscription: fakeSubscription(), label: 'a' });
  assert.equal((await postJson(app.base, '/api/test', {})).status, 200);
  const limited = await postJson(app.base, '/api/test', {});
  assert.equal(limited.status, 429);
  assert.equal(limited.res.headers.get('retry-after'), '10');
  now += 10_000;
  assert.equal((await postJson(app.base, '/api/test', {})).status, 200);

  now += 10_000;
  fs.rmSync(path.join(app.dir, 'vapid.json'));
  assert.equal((await postJson(app.base, '/api/test', {})).status, 503);
});

test('a web-push library exception (not a push-service response) is a failed send, not a crash', async (t) => {
  const wp = stubWebpush(() => {
    throw new Error('Vapid subject is not a valid URL');
  });
  const app = await startApp(t, { webpush: wp });
  await postJson(app.base, '/api/subscribe', { subscription: fakeSubscription(), label: 'a' });
  const r = await postJson(app.base, '/api/test', {});
  assert.equal(r.status, 502);
  assert.equal(readSubs(app.dir).length, 1);
});

// ---- PWA files -----------------------------------------------------------------------------------

test('GET / renders the PWA with the device list embedded safely and strict headers', async (t) => {
  const app = await startApp(t);
  const sub = fakeSubscription();
  await postJson(app.base, '/api/subscribe', { subscription: sub, label: '</script><script>alert(1)</script>' });
  const res = await fetch(`${app.base}/`);
  assert.equal(res.status, 200);
  assert.match(res.headers.get('content-type'), /^text\/html/);
  assert.equal(res.headers.get('cache-control'), 'no-store');
  assert.match(res.headers.get('content-security-policy'), /script-src 'self'/);
  assert.match(res.headers.get('content-security-policy'), /frame-ancestors 'none'/);
  assert.equal(res.headers.get('x-content-type-options'), 'nosniff');
  const html = await res.text();
  assert.match(html, /<title>Homelab alerts<\/title>/);
  assert.match(html, /id="subscribe"/);
  assert.match(html, /id="unsubscribe"/);
  assert.match(html, /id="test"/);
  assert.match(html, /id="label"/);
  assert.match(html, /<link rel="manifest" href="\/manifest.webmanifest">/);
  assert.ok(!html.includes('__DEVICES_JSON__'));
  assert.ok(!html.includes('<script>alert(1)'), 'label cannot break out of the JSON block');
  assert.ok(!html.includes(sub.endpoint), 'endpoints are not exposed in the page');
  const json = html.match(/<script type="application\/json" id="devices-data">([^<]*)<\/script>/)[1];
  const data = JSON.parse(json);
  assert.equal(data.devices[0].label, '</script><script>alert(1)</script>');
  assert.equal(data.devices[0].id, deviceId(sub.endpoint));
  assert.equal(data.storeError, false);
});

test('index.html loads no inline script/style (CSP script-src/style-src self)', () => {
  const html = fs.readFileSync(new URL('../public/index.html', import.meta.url), 'utf8');
  const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)];
  for (const [, attrs, body] of scripts) {
    if (/type="application\/json"/.test(attrs)) continue;
    assert.match(attrs, /src="/);
    assert.equal(body.trim(), '');
  }
  assert.doesNotMatch(html, /<style\b|\sstyle="|\son[a-z]+="/i);
});

test('manifest: name, standalone display, and every icon resolves to a real image', async (t) => {
  const app = await startApp(t);
  const res = await fetch(`${app.base}/manifest.webmanifest`);
  assert.equal(res.status, 200);
  assert.match(res.headers.get('content-type'), /^application\/manifest\+json/);
  const manifest = await res.json();
  assert.equal(manifest.name, 'Homelab alerts');
  assert.equal(manifest.display, 'standalone');
  assert.equal(manifest.start_url, '/');
  assert.ok(manifest.icons.some((i) => i.sizes === '192x192'));
  assert.ok(manifest.icons.some((i) => i.sizes === '512x512' && i.purpose === 'maskable'));
  for (const icon of manifest.icons) {
    const r = await fetch(`${app.base}${icon.src}`);
    assert.equal(r.status, 200, icon.src);
    assert.equal(r.headers.get('content-type'), icon.type);
    const buf = Buffer.from(await r.arrayBuffer());
    if (icon.type === 'image/png') {
      const png = await readPng(buf);
      assert.equal(`${png.width}x${png.height}`, icon.sizes);
    } else {
      assert.match(buf.toString(), /^<svg [^>]*viewBox="0 0 512 512"/);
    }
  }
});

test('apple-touch-icon is 180x180 opaque; notification badge is monochrome with transparency', async (t) => {
  const app = await startApp(t);
  const touch = await readPng(Buffer.from(await (await fetch(`${app.base}/icons/apple-touch-icon.png`)).arrayBuffer()));
  assert.equal(touch.width, 180);
  const badge = await readPng(Buffer.from(await (await fetch(`${app.base}/icons/badge-96.png`)).arrayBuffer()));
  assert.equal(badge.width, 96);
  assert.equal(badge.colorType, 6);
  const alphas = new Set();
  const stride = badge.width * 4 + 1;
  for (let y = 0; y < badge.height; y++) {
    for (let x = 0; x < badge.width; x++) {
      const o = y * stride + 1 + x * 4;
      assert.deepEqual([...badge.raw.subarray(o, o + 3)], [255, 255, 255]);
      alphas.add(badge.raw[o + 3]);
    }
  }
  assert.ok(alphas.has(0) && alphas.has(255), 'badge has both transparent and solid pixels');
});

test('sw.js and static assets: correct types, no-cache, ETag revalidation', async (t) => {
  const app = await startApp(t);
  const sw = await fetch(`${app.base}/sw.js`);
  assert.equal(sw.status, 200);
  assert.match(sw.headers.get('content-type'), /^text\/javascript/);
  assert.equal(sw.headers.get('cache-control'), 'no-cache');
  const etag = sw.headers.get('etag');
  assert.ok(etag);
  const revalidated = await fetch(`${app.base}/sw.js`, { headers: { 'If-None-Match': etag } });
  assert.equal(revalidated.status, 304);
  for (const [p, type] of [
    ['/app.js', /^text\/javascript/],
    ['/style.css', /^text\/css/],
    ['/icons/icon.svg', /^image\/svg\+xml/],
  ]) {
    const r = await fetch(`${app.base}${p}`);
    assert.equal(r.status, 200, p);
    assert.match(r.headers.get('content-type'), type);
  }
  const head = await fetch(`${app.base}/sw.js`, { method: 'HEAD' });
  assert.equal(head.status, 200);
  assert.equal(await head.text(), '');
});

test('access log covers requests but skips healthy /healthz probes', async (t) => {
  const app = await startApp(t);
  await fetch(`${app.base}/healthz`);
  await fetch(`${app.base}/nope`);
  const logs = app.lines.map((l) => JSON.parse(l));
  assert.ok(logs.some((l) => l.msg === 'request' && l.path === '/nope' && l.status === 404));
  assert.ok(!logs.some((l) => l.msg === 'request' && l.path === '/healthz'));
});
