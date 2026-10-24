// SSE: summary change detection, ETag/304, log streaming.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { contentHash } from '../src/summary.js';
import { logLevel, parseLogItem, createSecrets } from '../src/redact.js';
import { getJson, sse, startDashboard } from './helpers/server.js';
import { KEYS } from './helpers/host.js';

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
// fake credentials built at runtime, so no literal in the repo looks like one
const TOKEN = 't'.repeat(6) + '123456';
const BEARER = 'b'.repeat(6) + '654321';

test('content hash ignores generatedAt only', () => {
  const a = { schema: 1, generatedAt: '2026-10-24T14:00:00Z', x: 1 };
  assert.equal(contentHash(a), contentHash({ ...a, generatedAt: '2026-10-24T15:00:00Z' }));
  assert.notEqual(contentHash(a), contentHash({ ...a, x: 2 }));
});

test('GET /api/summary: ETag, 304 on If-None-Match, a new ETag after a real change', async (t) => {
  const d = await startDashboard(t, 'normal');
  const first = await getJson(d.base, '/api/summary');
  const etag = first.headers.get('etag');
  assert.match(etag, /^"[0-9a-f]{24}"$/);
  assert.equal(first.headers.get('cache-control'), 'no-cache');
  assert.ok(first.headers.get('x-generated-at'));
  await d.dash.summary.refreshCore(); // nothing changed on the host
  const same = await fetch(`${d.base}/api/summary`, { headers: { 'If-None-Match': etag } });
  assert.equal(same.status, 304);
  assert.equal(same.headers.get('etag'), etag);
  d.host.write('proc/loadavg', '3.10 2.00 1.00 1/1 1\n');
  await d.dash.summary.refreshCore();
  const changed = await fetch(`${d.base}/api/summary`, { headers: { 'If-None-Match': etag } });
  assert.equal(changed.status, 200);
  assert.notEqual(changed.headers.get('etag'), etag);
  assert.deepEqual((await changed.json()).host.load, [3.1, 2, 1]);
});

test('GET /api/events: full summary first, then `tick` when nothing changed and `summary` when it did', async (t) => {
  const d = await startDashboard(t, 'normal');
  const s = sse(d.port, '/api/events');
  const first = await s.next('summary');
  assert.equal(s.status, 200);
  assert.match(s.headers['content-type'], /^text\/event-stream/);
  assert.equal(s.headers['cache-control'], 'no-store');
  assert.equal(first.data.schema, 1);
  assert.match(first.id, /^[0-9a-f]{24}$/);
  await d.dash.summary.refreshCore();
  const tick = await s.next('tick');
  assert.deepEqual(Object.keys(tick.data), ['generatedAt']);
  assert.equal(tick.id, first.id);
  d.host.write('var/lib/bootgate/status', 'media-lost\n');
  await d.dash.summary.refreshCore();
  const changed = await s.next('summary');
  assert.equal(changed.data.gate.status, 'media-lost');
  assert.notEqual(changed.id, first.id);
  assert.equal(s.events.filter((e) => e.event === 'summary').length, 2, 'no duplicate summaries');
  s.close();
  await wait(50);
  assert.equal(d.dash.summary.listenerCount(), 0, 'listener removed on disconnect');
});

test('GET /api/events: reconnecting with a current Last-Event-ID skips the full resend', async (t) => {
  const d = await startDashboard(t, 'normal');
  const a = sse(d.port, '/api/events');
  const first = await a.next('summary');
  a.close();
  const b = sse(d.port, '/api/events', { 'Last-Event-ID': first.id });
  const ev = await b.next();
  assert.equal(ev.event, 'tick');
  b.close();
  const c = sse(d.port, '/api/events', { 'Last-Event-ID': 'stale' });
  assert.equal((await c.next()).event, 'summary');
  c.close();
});

test('GET /api/events: keepalive comments and a retry hint', async (t) => {
  const d = await startDashboard(t, 'clean');
  const raw = await new Promise((resolve) => {
    import('node:http').then(({ default: http }) => {
      const req = http.get({ host: '127.0.0.1', port: d.port, path: '/api/events' }, (res) => {
        let buf = '';
        res.on('data', (c) => {
          buf += c;
          if (buf.includes('event: summary')) {
            req.destroy();
            resolve(buf);
          }
        });
      });
    });
  });
  assert.match(raw, /^retry: 10000\n\n/);
});

test('log level parsing covers every app format here', () => {
  const cases = [
    ['[Info] RssSyncService: RSS Sync Completed', 'INF'],
    ['[Warn] DownloadClient: slow', 'WRN'],
    ['[Error] Indexer C request failed: HTTP 503', 'ERR'],
    ['2026-10-24 11:02:10,123::INFO::[downloader:123] Connected', 'INF'],
    ['2026-10-24 11:02:10,123::WARNING::[newsunpack] retry', 'WRN'],
    ['[11:02:10] [INF] [12] Main: Jellyfin version 10.11', 'INF'],
    ['[11:02:10] [ERR] [12] Transcode failed', 'ERR'],
    ['2026-10-24T11:02:10.123Z [debug][Jobs]: tick', 'DBG'],
    ['2026-10-24T11:02:10Z INF Starting tunnel tunnelID=x', 'INF'],
    ['2026-10-24T11:02:10Z ERR Failed to dial', 'ERR'],
    ['[migrations] started', 'INF'],
  ];
  for (const [line, level] of cases) assert.equal(logLevel(line), level, line);
});

test('log items: ANSI stripped, secrets and credential shapes redacted, timestamps normalised', () => {
  const secrets = createSecrets();
  secrets.add(KEYS.sonarr);
  const item = parseLogItem({ type: 'line', ts: '2026-10-24T10:00:00.123456789Z', text: `\x1b[32m[Info]\x1b[0m GET /api/v3/system?apikey=${KEYS.sonarr} token=${TOKEN} Authorization: Bearer ${BEARER}` }, secrets);
  assert.equal(item.ts, '2026-10-24T10:00:00.123Z');
  assert.deepEqual(Object.keys(item), ['ts', 'level', 'msg']);
  assert.equal(item.level, 'INF');
  assert.ok(!item.msg.includes(KEYS.sonarr));
  assert.ok(!item.msg.includes(TOKEN));
  assert.ok(!item.msg.includes(BEARER));
  assert.ok(!item.msg.includes('\x1b'));
  assert.match(item.msg, /apikey=\[redacted\]/);
});

test('GET /api/apps/:name/logs: tail streamed in batches, redacted, then `end`', async (t) => {
  const lines = Array.from({ length: 450 }, (_, i) => ({ ts: `2026-10-24T10:00:${String(i % 60).padStart(2, '0')}Z`, text: `[Info] line ${i} key=${KEYS.radarr}` }));
  const d = await startDashboard(t, 'normal', { helper: { logs: { radarr: lines } } });
  await d.dash.start({ background: false }); // read the keys so they are known secrets
  const s = sse(d.port, '/api/apps/radarr/logs?tail=400');
  const end = await s.until('end', () => true);
  assert.deepEqual(end.data, { reason: 'ended' });
  const got = s.events.filter((e) => e.event === 'lines').flatMap((e) => e.data);
  assert.equal(got.length, 400);
  assert.ok(s.events.filter((e) => e.event === 'lines').length >= 2, 'batched (<= 200 per event)');
  assert.ok(s.events.filter((e) => e.event === 'lines').every((e) => e.data.length <= 200));
  assert.deepEqual(Object.keys(got[0]), ['ts', 'level', 'msg']);
  assert.equal(got[0].msg, '[Info] line 50 key=[redacted]');
  assert.ok(!JSON.stringify(got).includes(KEYS.radarr));
  const call = d.helper.calls.find((c) => c.op === 'logs');
  assert.deepEqual([call.name, call.tail, call.follow], ['radarr', 400, false]);
});

test('logs follow: live lines arrive; closing the browser closes the helper stream', async (t) => {
  const d = await startDashboard(t, 'normal', { helper: { logsFollow: true, logs: { sonarr: [{ ts: '2026-10-24T10:00:00Z', text: '[Info] start' }] } } });
  const s = sse(d.port, '/api/apps/sonarr/logs?follow=1&tail=10');
  await s.until('lines', (e) => e.data.some((l) => l.msg === '[Info] start'));
  d.helper.pushLog({ ts: '2026-10-24T10:00:05Z', text: '[Error] Indexer C request failed: HTTP 503' });
  const live = await s.until('lines', (e) => e.data.some((l) => l.level === 'ERR'));
  assert.equal(live.data.at(-1).msg, '[Error] Indexer C request failed: HTTP 503');
  assert.equal(d.helper.openStreams(), 1);
  s.close();
  for (let i = 0; i < 50 && d.helper.openStreams() > 0; i++) await wait(10);
  assert.equal(d.helper.openStreams(), 0, 'follow cancelled on the helper side');
});

test('logs: parameter validation, unknown app, Docker held -> 409 without calling the helper', async (t) => {
  const d = await startDashboard(t, 'normal');
  for (const q of ['tail=0', 'tail=2001', 'tail=abc', 'since=yesterday']) {
    assert.equal((await getJson(d.base, `/api/apps/sonarr/logs?${q}`)).status, 400, q);
  }
  assert.equal((await getJson(d.base, '/api/apps/postgres/logs')).status, 404);
  const bad = await startDashboard(t, 'bad-night');
  const r = await getJson(bad.base, '/api/apps/sonarr/logs?follow=1');
  assert.equal(r.status, 409);
  assert.equal(r.json.error, 'Held by the boot gate until the media drive is back.');
  assert.equal(bad.helper.dockerCalls().length, 0);
});
