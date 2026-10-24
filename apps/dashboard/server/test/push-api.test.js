// push-core's HTTP API, migrated: the notifier and installed phones depend on these exact shapes.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { deviceId } from '../src/log.js';
import { fakeSubscription, pushError, readSubs, stubWebpush, writeVapid } from './helpers/push-fixtures.js';
import { getJson, postJson, rawRequest, request, startDashboard } from './helpers/server.js';

// 'clean' starts with an empty push store.
const start = (t, opts = {}) => startDashboard(t, 'clean', opts);

test('GET /api/vapid-public-key returns the public key only; 503 when vapid.json is missing or invalid', async (t) => {
  const d = await start(t);
  const r = await getJson(d.base, '/api/vapid-public-key');
  assert.equal(r.status, 200);
  assert.equal(r.text, JSON.stringify({ publicKey: d.host.vapid.publicKey }), 'byte-identical body');
  assert.equal(r.headers.get('cache-control'), 'no-store');
  fs.writeFileSync(path.join(d.host.pushDir, 'vapid.json'), '{"publicKey":"abc","privateKey":"def","subject":"x"}');
  assert.equal((await getJson(d.base, '/api/vapid-public-key')).status, 503);
  fs.rmSync(path.join(d.host.pushDir, 'vapid.json'));
  assert.equal((await getJson(d.base, '/api/vapid-public-key')).status, 503);
  assert.doesNotMatch(d.text(), new RegExp(d.host.vapid.privateKey), 'no key material in logs');
  const keys = writeVapid(d.host.pushDir);
  assert.equal((await getJson(d.base, '/api/vapid-public-key')).json.publicKey, keys.publicKey, 'reloaded without a restart');
});

test('subscribe -> 201 then 200 (dedupe); body shape and key order match push-core exactly', async (t) => {
  const d = await start(t);
  const sub = fakeSubscription();
  const first = await postJson(d.base, '/api/subscribe', { subscription: sub, label: 'Pixel' });
  assert.equal(first.status, 201);
  const saved = readSubs(d.host.pushDir);
  const device = { id: deviceId(sub.endpoint), label: 'Pixel', created: saved[0].created };
  assert.equal(first.text, JSON.stringify({ ok: true, created: true, device, devices: [device] }));
  const again = await postJson(d.base, '/api/subscribe', { subscription: sub, label: 'Pixel 9' });
  assert.equal(again.status, 200);
  assert.equal(again.json.created, false);
  assert.equal(readSubs(d.host.pushDir).length, 1);
  for (const r of [first, again]) {
    assert.ok(!r.text.includes(sub.endpoint) && !r.text.includes(sub.keys.p256dh) && !r.text.includes(sub.keys.auth));
  }
  assert.deepEqual(Object.keys(saved[0]), ['endpoint', 'keys', 'label', 'created'], 'contract 4 store format');
});

test('subscribe: invalid subscription -> 400; cap -> 409', async (t) => {
  const d = await start(t);
  const r = await postJson(d.base, '/api/subscribe', { subscription: { endpoint: 'https://evil.example/x', keys: {} } });
  assert.equal(r.status, 400);
  assert.match(r.json.error, /push service/);
  const full = Array.from({ length: 100 }, () => {
    const s = fakeSubscription();
    return { endpoint: s.endpoint, keys: s.keys, label: 'x', created: '2026-01-01T00:00:00.000Z' };
  });
  fs.writeFileSync(path.join(d.host.pushDir, 'subscriptions.json'), JSON.stringify(full));
  assert.equal((await postJson(d.base, '/api/subscribe', { subscription: fakeSubscription() })).status, 409);
});

test('unsubscribe: removes, is idempotent, needs an endpoint; server-to-server call without Sec-Fetch-Site works', async (t) => {
  const d = await start(t);
  const dead = fakeSubscription();
  const alive = fakeSubscription();
  await postJson(d.base, '/api/subscribe', { subscription: dead, label: 'dead' });
  await postJson(d.base, '/api/subscribe', { subscription: alive, label: 'alive' });
  const body = JSON.stringify({ endpoint: dead.endpoint });
  // exactly what the notifier's prune sends (CONTRACTS §9c)
  const r = await rawRequest(d.port, {
    path: '/api/unsubscribe',
    headers: { 'X-Homelab': '1', 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
    chunks: [body],
  });
  assert.equal(r.status, 200);
  assert.deepEqual(JSON.parse(r.body).removed, true);
  assert.deepEqual(readSubs(d.host.pushDir).map((e) => e.label), ['alive']);
  const again = await postJson(d.base, '/api/unsubscribe', { endpoint: dead.endpoint });
  assert.equal(again.text, JSON.stringify({ ok: true, removed: false, devices: [{ id: deviceId(alive.endpoint), label: 'alive', created: readSubs(d.host.pushDir)[0].created }] }));
  assert.equal((await postJson(d.base, '/api/unsubscribe', {})).status, 400);
});

test('corrupt store: writes -> 503 and the file is untouched; healthz -> 503 store:corrupt; summary flags it', async (t) => {
  const d = await start(t);
  const file = path.join(d.host.pushDir, 'subscriptions.json');
  fs.writeFileSync(file, '{not json');
  const sub = fakeSubscription();
  assert.equal((await postJson(d.base, '/api/subscribe', { subscription: sub })).status, 503);
  assert.equal((await postJson(d.base, '/api/unsubscribe', { endpoint: sub.endpoint })).status, 503);
  assert.equal((await postJson(d.base, '/api/test', {})).status, 503);
  assert.equal((await request(d.base, 'DELETE', `/api/push/devices/${'0'.repeat(16)}`)).status, 503);
  assert.equal((await getJson(d.base, '/api/push/devices')).status, 503);
  assert.equal(fs.readFileSync(file, 'utf8'), '{not json');
  const health = await getJson(d.base, '/healthz');
  assert.equal(health.status, 503);
  assert.equal(health.json.checks.store, 'corrupt');
  await d.dash.summary.refreshCore();
  const s = (await getJson(d.base, '/api/summary')).json;
  assert.equal(s.push.storeError, true);
  assert.ok(s.issues.some((i) => i.severity === 'fail' && i.target === 'alerts'));
});

test('GET /healthz keeps push-core shape: {ok, checks:{vapid, store, writable}}', async (t) => {
  const d = await start(t);
  const ok = await getJson(d.base, '/healthz');
  assert.equal(ok.status, 200);
  assert.equal(ok.text, JSON.stringify({ ok: true, checks: { vapid: 'ok', store: 'ok', writable: true } }));
  fs.rmSync(path.join(d.host.pushDir, 'vapid.json'));
  const bad = await getJson(d.base, '/healthz');
  assert.equal(bad.status, 503);
  assert.equal(bad.json.checks.vapid, 'missing');
  assert.equal((await fetch(`${d.base}/healthz`, { method: 'HEAD' })).status, 503);
});

test('POST /api/test: contract payload to every subscription with stored keys and VAPID details', async (t) => {
  const wp = stubWebpush();
  const d = await start(t, { webpush: wp });
  const a = fakeSubscription();
  const b = fakeSubscription('web.push.apple.com');
  await postJson(d.base, '/api/subscribe', { subscription: a, label: 'a' });
  await postJson(d.base, '/api/subscribe', { subscription: b, label: 'test' });
  fs.mkdirSync(d.host.path('etc/homelab'), { recursive: true });
  fs.writeFileSync(d.host.path('etc/homelab/rehearsal'), ''); // rehearsal on: the test button is exempt
  const r = await postJson(d.base, '/api/test', {});
  assert.equal(r.status, 200);
  assert.deepEqual({ ...r.json, devices: r.json.devices.length }, { ok: true, total: 2, sent: 2, failed: 0, removed: 0, devices: 2 });
  assert.deepEqual(Object.keys(r.json), ['ok', 'total', 'sent', 'failed', 'removed', 'devices']);
  for (const [i, sub] of [a, b].entries()) {
    const call = wp.calls[i];
    assert.deepEqual(call.subscription, { endpoint: sub.endpoint, keys: sub.keys });
    assert.deepEqual(JSON.parse(call.payload), { title: 'Test from homelab', body: 'Push alerts are working on this device.', tag: 'test', url: '/' });
    assert.deepEqual(call.options.vapidDetails, { subject: d.host.vapid.subject, publicKey: d.host.vapid.publicKey, privateKey: d.host.vapid.privateKey });
    assert.equal(call.options.contentEncoding, 'aes128gcm');
  }
});

test('POST /api/test removes 404/410, keeps other failures, never logs endpoints or keys', async (t) => {
  const subs = { ok: fakeSubscription(), gone: fakeSubscription(), missing: fakeSubscription(), flaky: fakeSubscription() };
  const byEndpoint = new Map(Object.entries(subs).map(([k, s]) => [s.endpoint, k]));
  const wp = stubWebpush((sub) => {
    const k = byEndpoint.get(sub.endpoint);
    if (k === 'gone') return pushError(410, `expired ${sub.endpoint}`);
    if (k === 'missing') return pushError(404, 'Not Found');
    if (k === 'flaky') return pushError(500, 'internal error');
    return { statusCode: 201 };
  });
  const d = await start(t, { webpush: wp });
  for (const [label, s] of Object.entries(subs)) await postJson(d.base, '/api/subscribe', { subscription: s, label });
  const r = await postJson(d.base, '/api/test', {});
  assert.equal(r.status, 200);
  assert.deepEqual([r.json.total, r.json.sent, r.json.failed, r.json.removed], [4, 1, 3, 2]);
  assert.deepEqual(readSubs(d.host.pushDir).map((e) => e.label).sort(), ['flaky', 'ok']);
  const logs = d.text();
  for (const s of Object.values(subs)) {
    assert.ok(!logs.includes(s.endpoint));
    assert.ok(!logs.includes(s.keys.p256dh) && !logs.includes(s.keys.auth));
  }
  assert.ok(!logs.includes(d.host.vapid.privateKey));
  assert.match(logs, /fcm\.googleapis\.com#[0-9a-f]{16}/);
});

test('POST /api/test: all fail -> 502, nothing removed; none subscribed -> 200 total 0; cooldown -> 429', async (t) => {
  const wp = stubWebpush(() => pushError(403, '{"reason":"BadJwtToken"}'));
  const d = await start(t, { webpush: wp });
  const empty = await postJson(d.base, '/api/test', {});
  assert.equal(empty.text, JSON.stringify({ ok: true, total: 0, sent: 0, failed: 0, removed: 0, devices: [] }));
  assert.equal(wp.calls.length, 0);
  await postJson(d.base, '/api/subscribe', { subscription: fakeSubscription('web.push.apple.com'), label: 'iphone' });
  const r = await postJson(d.base, '/api/test', {});
  assert.equal(r.status, 502);
  assert.equal(readSubs(d.host.pushDir).length, 1);
  assert.match(d.text(), /BadJwtToken/);
  const limited = await postJson(d.base, '/api/test', {});
  assert.equal(limited.status, 429);
  assert.ok(Number(limited.headers.get('retry-after')) >= 9);
});

test('GET /api/push/devices lists {id,label,created}; DELETE /api/push/devices/:id removes one', async (t) => {
  const d = await start(t);
  const a = fakeSubscription();
  const b = fakeSubscription();
  await postJson(d.base, '/api/subscribe', { subscription: a, label: 'a' });
  await postJson(d.base, '/api/subscribe', { subscription: b, label: 'b' });
  const list = await getJson(d.base, '/api/push/devices');
  assert.equal(list.status, 200);
  assert.deepEqual(list.json.devices.map((x) => Object.keys(x)), [['id', 'label', 'created'], ['id', 'label', 'created']]);
  assert.ok(!list.text.includes(a.endpoint));
  const del = await request(d.base, 'DELETE', `/api/push/devices/${deviceId(a.endpoint)}`);
  assert.equal(del.status, 200);
  assert.deepEqual(del.json.devices.map((x) => x.label), ['b']);
  assert.equal((await request(d.base, 'DELETE', `/api/push/devices/${deviceId(a.endpoint)}`)).status, 404);
  assert.equal((await request(d.base, 'DELETE', '/api/push/devices/not-hex')).status, 400);
  const noGuard = await request(d.base, 'DELETE', `/api/push/devices/${deviceId(b.endpoint)}`, { headers: {} });
  assert.equal(noGuard.status, 403, 'DELETE needs the CSRF header too');
  assert.match(d.text(), /"msg":"device removed","device":"[0-9a-f]{16}"/);
});
