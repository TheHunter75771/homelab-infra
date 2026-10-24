// CONTRACTS §11.3 "Resource limits" and §11.2 "Concurrency" (review 2026-10-24): malformed request targets,
// caught async paths, SSE backpressure and buffer caps, /api/events slot accounting, one update at a time,
// at most 8 log follows.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { HttpError, parseTarget, SSE_MAX_BUFFER } from '../src/http/common.js';
import { startFakeHelper, container, ALL_APPS } from './helpers/fake-helper.js';
import { buildHost, helperFor, writeWeb } from './helpers/host.js';
import { captureLogger, getJson, postJson, sse, startDashboard } from './helpers/server.js';

const SERVER = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src/server.js');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// Raw HTTP/1.1 exchange: the exact request line, the status line back.
function rawStatus(port, requestLine) {
  return new Promise((resolve, reject) => {
    const sock = net.connect(port, '127.0.0.1');
    let buf = '';
    sock.setEncoding('utf8');
    sock.on('data', (d) => {
      buf += d;
      if (buf.includes('\r\n')) {
        resolve(Number(buf.split(' ')[1]));
        sock.destroy();
      }
    });
    sock.on('error', reject);
    sock.on('close', () => resolve(buf ? Number(buf.split(' ')[1]) : null));
    sock.write(`${requestLine}\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n`);
  });
}

// A client that sends a request and then never reads a byte.
function stalledClient(port, p, headers = '') {
  const sock = net.connect(port, '127.0.0.1');
  sock.on('error', () => {});
  sock.on('connect', () => {
    sock.write(`GET ${p} HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n${headers}\r\n`);
    sock.pause();
  });
  return sock;
}

// ---- BLOCKER: malformed targets ----------------------------------------------------------------------

test('parseTarget: origin-form only; everything else is a 400 HttpError', () => {
  assert.equal(parseTarget('/api/summary?x=1').pathname, '/api/summary');
  assert.equal(parseTarget('/apps/sonarr').pathname, '/apps/sonarr');
  for (const bad of ['//', '//evil.example/x', '/\\', '/\\evil', '/%', '/%zz', '/a%E0%A4%A', 'http://x/', 'http://127.0.0.1:8085/api/summary', '*', '', 'x', undefined]) {
    assert.throws(() => parseTarget(bad), (e) => e instanceof HttpError && e.status === 400, String(bad));
  }
});

test('the real server: //, /\\, /%, absolute-form and * are 400s and the process stays up', async (t) => {
  const helper = await startFakeHelper(t, helperFor('normal'));
  const host = buildHost(t, 'normal', { helperSocket: helper.socketPath });
  writeWeb(host.cfg.webDir);
  const conf = path.join(host.root, 'config.json');
  fs.writeFileSync(conf, JSON.stringify({
    state_dir: host.cfg.stateDir, push_dir: host.cfg.pushDir, web_dir: host.cfg.webDir, helper_socket: helper.socketPath,
    gate_dir: host.cfg.gateDir, proc_dir: host.cfg.procDir, data_dir: host.cfg.dataDir, timezone: 'UTC',
    backup_status_file: host.cfg.backupStatusFile, notify_history_file: host.cfg.notifyHistoryFile,
  }));
  const child = spawn(process.execPath, [SERVER], { env: { PATH: process.env.PATH, DASHBOARD_CONFIG: conf, PORT: '0', HOST: '127.0.0.1' }, stdio: ['ignore', 'pipe', 'pipe'] });
  t.after(() => child.exitCode === null && child.kill('SIGKILL'));
  let out = '';
  child.stdout.on('data', (d) => (out += d));
  let port;
  for (let i = 0; i < 100 && !port; i++) {
    port = out.match(/"msg":"listening".*"port":(\d+)/)?.[1];
    if (!port) await wait(20);
  }
  port = Number(port);
  for (const target of ['//', '/\\', '/%', 'http://x/', `http://127.0.0.1:${port}/api/summary`, '*', '//etc/passwd', '/\\..\\..']) {
    assert.equal(await rawStatus(port, `GET ${target} HTTP/1.1`), 400, target);
  }
  assert.equal(await rawStatus(port, 'OPTIONS * HTTP/1.1'), 400);
  assert.equal(child.exitCode, null, 'still running');
  assert.equal((await fetch(`http://127.0.0.1:${port}/healthz`)).status, 200);
  child.kill('SIGTERM');
  assert.equal((await once(child, 'exit'))[0], 0);
});

test('a handler that throws after answering costs that request only; the server keeps serving', async (t) => {
  const captured = captureLogger();
  const log = {
    ...captured.log,
    info: (msg, f) => {
      if (msg === 'request') throw new Error('logger exploded');
      captured.log.info(msg, f);
    },
  };
  const helper = await startFakeHelper(t, helperFor('normal'));
  const host = buildHost(t, 'normal', { helperSocket: helper.socketPath });
  const { createDashboard } = await import('../src/dashboard.js');
  const dash = createDashboard(host.cfg, { log, run: null, statfs: async () => ({ bsize: 1, blocks: 1, bfree: 1, bavail: 1 }) });
  await new Promise((r) => dash.server.listen(0, '127.0.0.1', r));
  t.after(() => new Promise((r) => dash.server.close(r)));
  const base = `http://127.0.0.1:${dash.server.address().port}`;
  // /nope is access-logged (healthy /healthz is not), so the throwing logger runs after the 404 was sent
  await fetch(`${base}/nope`).catch(() => {});
  for (let i = 0; i < 50 && !captured.text().includes('request failed'); i++) await wait(10);
  assert.match(captured.text(), /"msg":"request failed","error":"logger exploded"/);
  assert.ok(dash.server.listening, 'still listening');
  assert.equal((await fetch(`${base}/healthz`)).status, 200, 'and still serving');
});

test('process-level unhandledRejection is logged and the process keeps running', async (t) => {
  const helper = await startFakeHelper(t, helperFor('normal'));
  const host = buildHost(t, 'normal', { helperSocket: helper.socketPath });
  const conf = path.join(host.root, 'config.json');
  fs.writeFileSync(conf, JSON.stringify({ state_dir: host.cfg.stateDir, push_dir: host.cfg.pushDir, helper_socket: helper.socketPath, proc_dir: host.cfg.procDir, timezone: 'UTC' }));
  const inject = 'data:text/javascript,setTimeout(()=>{Promise.reject(new Error("stray boom"))},150)';
  const child = spawn(process.execPath, ['--import', inject, SERVER], { env: { PATH: process.env.PATH, DASHBOARD_CONFIG: conf, PORT: '0', HOST: '127.0.0.1' }, stdio: ['ignore', 'pipe', 'pipe'] });
  t.after(() => child.exitCode === null && child.kill('SIGKILL'));
  let out = '';
  child.stdout.on('data', (d) => (out += d));
  for (let i = 0; i < 100 && !out.includes('stray boom'); i++) await wait(20);
  assert.match(out, /"level":"error","msg":"unhandled rejection","error":"stray boom"/);
  await wait(100);
  assert.equal(child.exitCode, null, 'did not exit');
  const port = Number(out.match(/"msg":"listening".*"port":(\d+)/)[1]);
  assert.equal((await fetch(`http://127.0.0.1:${port}/healthz`)).status, 200);
});

// ---- MAJOR: SSE backpressure ---------------------------------------------------------------------------

test('logs: a client that never reads makes the server pause the helper stream; its buffer stays bounded', { timeout: 30_000 }, async (t) => {
  const d = await startDashboard(t, 'normal', { helper: { logsFollow: true, logs: { sonarr: [] } } });
  const client = stalledClient(d.port, '/api/apps/sonarr/logs?follow=1&tail=1');
  t.after(() => client.destroy());
  for (let i = 0; i < 100 && d.helper.openStreams() === 0; i++) await wait(10);
  assert.equal(d.helper.openStreams(), 1);
  const line = `[Info] ${'x'.repeat(2000)}`;
  // 40 MB from the "helper": without backpressure the dashboard would buffer all of it for the stalled phone
  for (let i = 0; i < 20_000; i++) {
    d.helper.pushLog({ ts: '2026-10-24T10:00:00Z', text: line });
    if (i % 1000 === 0) await wait(5);
  }
  let maxSeen = 0;
  for (let i = 0; i < 100; i++) {
    maxSeen = Math.max(maxSeen, d.dash.httpStats().maxBuffered);
    if (d.helper.streamBacklog() > 8 * 1024 * 1024) break;
    await wait(20);
  }
  assert.ok(d.helper.streamBacklog() > 8 * 1024 * 1024, `the backlog stays with the helper (${d.helper.streamBacklog()} bytes)`);
  assert.ok(maxSeen <= SSE_MAX_BUFFER + 512 * 1024, `the dashboard holds at most ~1 MB for the client (saw ${maxSeen})`);
  client.destroy();
  for (let i = 0; i < 100 && d.helper.openStreams() > 0; i++) await wait(10);
  assert.equal(d.helper.openStreams(), 0, 'closing the client releases the helper stream');
  assert.equal(d.dash.httpStats().slots.logs, 0, 'and the slot');
});

test('events: a client that never reads is dropped past the cap and its slot released; readers are unaffected', { timeout: 60_000 }, async (t) => {
  const d = await startDashboard(t, 'normal', { appOptions: { sseMaxBuffer: 256 * 1024 } });
  const reader = sse(d.port, '/api/events');
  await reader.next('summary');
  const stalled = stalledClient(d.port, '/api/events');
  t.after(() => stalled.destroy());
  for (let i = 0; i < 100 && d.dash.httpStats().slots.events < 2; i++) await wait(10);
  assert.equal(d.dash.httpStats().slots.events, 2);
  let maxSeen = 0;
  for (let i = 0; i < 4000 && d.dash.httpStats().slots.events === 2; i++) {
    d.host.write('proc/loadavg', `${(i % 97) / 10} 0.38 0.35 1/1 1\n`);
    await d.dash.summary.refreshCore();
    maxSeen = Math.max(maxSeen, d.dash.httpStats().maxBuffered);
  }
  assert.equal(d.dash.httpStats().slots.events, 1, 'the stalled stream was dropped and its slot released');
  assert.ok(maxSeen <= 256 * 1024 + 64 * 1024, `buffered at most cap + one summary (saw ${maxSeen})`);
  assert.match(d.text(), /"msg":"sse client too slow; dropped","stream":"events"/);
  const last = await reader.until('summary', () => true);
  assert.ok(last, 'the reading client still gets updates');
  reader.close();
});

// ---- MAJOR: /api/events slot leak -----------------------------------------------------------------------

test('events: clients that leave during a slow first summary give their slots back at once', { timeout: 30_000 }, async (t) => {
  const d = await startDashboard(t, 'normal', { start: false, helper: { delay: { 'docker-state': 1500 } } });
  const socks = [];
  for (let i = 0; i < 40; i++) socks.push(stalledClient(d.port, '/api/events'));
  for (let i = 0; i < 100 && d.dash.httpStats().slots.events < 32; i++) await wait(10);
  assert.equal(d.dash.httpStats().slots.events, 32, 'the cap: 32 reserved (before the await), 8 refused');
  const left = Date.now();
  for (const s of socks) s.destroy(); // all leave while the first summary is still being built
  for (let i = 0; i < 50 && d.dash.httpStats().slots.events > 0; i++) await wait(10);
  assert.equal(d.dash.httpStats().slots.events, 0, 'every slot released as soon as its client left');
  assert.ok(Date.now() - left < 1000, 'released while the slow first refresh was still running');
  assert.equal(d.dash.summary.current(), null, 'the refresh had not finished yet');
  await wait(1700); // the slow refresh finishes after they left
  assert.equal(d.dash.httpStats().slots.events, 0);
  assert.equal(d.dash.httpStats().streams, 0, 'no stream was opened for a client that had left');
  const s = sse(d.port, '/api/events');
  const ev = await s.next('summary', 8000);
  assert.equal(s.status, 200);
  assert.equal(ev.data.schema, 1);
  assert.equal(d.dash.httpStats().slots.events, 1);
  s.close();
  for (let i = 0; i < 100 && d.dash.httpStats().slots.events > 0; i++) await wait(10);
  assert.equal(d.dash.httpStats().slots.events, 0, 'released on a normal close too');
});

test('events: the cap (32) holds and slots come back', async (t) => {
  const d = await startDashboard(t, 'normal');
  const clients = [];
  for (let i = 0; i < 32; i++) {
    const c = sse(d.port, '/api/events');
    clients.push(c);
    await c.next('summary');
  }
  assert.equal((await getJson(d.base, '/api/events')).status, 503);
  for (const c of clients) c.close();
  for (let i = 0; i < 100 && d.dash.httpStats().slots.events > 0; i++) await wait(10);
  assert.equal(d.dash.httpStats().slots.events, 0);
  const again = sse(d.port, '/api/events');
  await again.next('summary');
  again.close();
});

// ---- Concurrency (§11.2) -------------------------------------------------------------------------------

test('one update at a time: a second update anywhere gets 409 with a clear message', async (t) => {
  const containers = ALL_APPS.map((n) =>
    ['sonarr', 'radarr'].includes(n) ? container(n, { image: `example/${n}:1.0.0@sha256:${'a'.repeat(64)}`, pinned: `example/${n}:1.1.0@sha256:${'b'.repeat(64)}` }) : container(n),
  );
  const d = await startDashboard(t, 'normal', { helper: { containers, updateStepMs: 150 } });
  const first = await postJson(d.base, '/api/apps/sonarr/update', {});
  assert.equal(first.status, 202);
  const second = await postJson(d.base, '/api/apps/radarr/update', {});
  assert.equal(second.status, 409);
  assert.equal(second.json.error, 'Another update is running (Sonarr). Try again when it finishes.');
  assert.equal((await postJson(d.base, '/api/apps/jellyfin/restart', {})).status, 202, 'other actions are not blocked');
  for (let i = 0; i < 200 && d.dash.actions.get(first.json.action.id)?.state === 'running'; i++) await wait(10);
  assert.equal((await postJson(d.base, '/api/apps/radarr/update', {})).status, 202, 'free again once it finished');
  for (let i = 0; i < 100 && d.helper.calls.filter((c) => c.op === 'update').length < 2; i++) await wait(10);
  assert.deepEqual(d.helper.calls.filter((c) => c.op === 'update').map((c) => c.name), ['sonarr', 'radarr'], 'the refused one never reached the helper');
});

test('log follows are capped at 8', async (t) => {
  const d = await startDashboard(t, 'normal', { helper: { logsFollow: true, logs: { sonarr: [{ ts: null, text: 'x' }] } } });
  const follows = [];
  for (let i = 0; i < 8; i++) {
    const s = sse(d.port, '/api/apps/sonarr/logs?follow=1&tail=1');
    follows.push(s);
    await s.next('lines');
  }
  assert.equal(d.dash.httpStats().slots.logs, 8);
  const ninth = await getJson(d.base, '/api/apps/sonarr/logs?follow=1');
  assert.equal(ninth.status, 503);
  assert.equal(ninth.json.error, 'too many log streams');
  follows[0].close();
  for (let i = 0; i < 100 && d.dash.httpStats().slots.logs > 7; i++) await wait(10);
  const replacement = sse(d.port, '/api/apps/sonarr/logs?follow=1&tail=1');
  await replacement.next('lines');
  for (const s of [...follows, replacement]) s.close();
  for (let i = 0; i < 100 && d.helper.openStreams() > 0; i++) await wait(10);
  assert.equal(d.helper.openStreams(), 0);
});

// ---- crash reports go through the logger's redaction (re-review) ---------------------------------------------

test('logger: every line is redacted, credential shapes always, configured keys once the dashboard set them', async () => {
  const { createLogger } = await import('../src/log.js');
  const { createSecrets } = await import('../src/redact.js');
  const { KEYS } = await import('./helpers/host.js');
  const lines = [];
  const log = createLogger({ write: (l) => lines.push(l) });
  log.error('boom', { error: `GET /api?apikey=${'q'.repeat(12)} ${KEYS.sonarr}` });
  assert.ok(!lines[0].includes('q'.repeat(12)), 'credential shapes are redacted from the start');
  const secrets = createSecrets();
  secrets.add(KEYS.sonarr);
  log.setRedactor((l) => secrets.redact(l));
  log.error('boom', { error: `GET /api?apikey=${'q'.repeat(12)} ${KEYS.sonarr}`, stack: `at x (${KEYS.sonarr})` });
  assert.ok(!lines[1].includes(KEYS.sonarr) && !lines[1].includes('q'.repeat(12)), lines[1]);
  assert.doesNotThrow(() => JSON.parse(lines[1]), 'still valid JSON');
});

async function crashingServer(t, code) {
  const { KEYS } = await import('./helpers/host.js');
  const helper = await startFakeHelper(t, helperFor('normal'));
  const host = buildHost(t, 'normal', { helperSocket: helper.socketPath });
  const conf = path.join(host.root, 'config.json');
  fs.writeFileSync(conf, JSON.stringify({ state_dir: host.cfg.stateDir, push_dir: host.cfg.pushDir, helper_socket: helper.socketPath, proc_dir: host.cfg.procDir, data_dir: host.cfg.dataDir, timezone: 'UTC' }));
  const child = spawn(process.execPath, ['--import', `data:text/javascript,${encodeURIComponent(code)}`, SERVER], {
    env: { PATH: process.env.PATH, DASHBOARD_CONFIG: conf, PORT: '0', HOST: '127.0.0.1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  t.after(() => child.exitCode === null && child.kill('SIGKILL'));
  const io = { out: '', err: '' };
  child.stdout.on('data', (d) => (io.out += d));
  child.stderr.on('data', (d) => (io.err += d));
  return { child, io, KEYS };
}

const INDEXER = 'ix'.repeat(8) + '77';
// fires after start-up, once the dashboard has read the *arr keys from their config files. The second copy of
// the key starts at index 488 of the raw message, so it straddles the 500-character clip: only redact-then-clip
// keeps all of it out.
const failure = (KEY) => {
  const head = `upstream failed: http://127.0.0.1:8989/sonarr/api/v3/health?apikey=${INDEXER}&x=1 and API Key: ${INDEXER} Bearer ${INDEXER} key ${KEY} `;
  return `new Error(${JSON.stringify(head)} + "p".repeat(${488 - head.length}) + "${KEY}")`;
};

test('unhandledRejection: the logged line holds no apikey= value and no configured key, and the process keeps running', async (t) => {
  const { KEYS } = await import('./helpers/host.js');
  const s = await crashingServer(t, `setTimeout(() => { Promise.reject(${failure(KEYS.sonarr)}); }, 1200)`);
  for (let i = 0; i < 200 && !s.io.out.includes('unhandled rejection'); i++) await wait(20);
  const line = s.io.out.split('\n').find((l) => l.includes('unhandled rejection'));
  assert.ok(line, s.io.out);
  const entry = JSON.parse(line);
  assert.match(entry.error, /^upstream failed: <url> and API Key: \[redacted\] Bearer \[redacted\] key \[redacted\]/);
  for (const secret of [INDEXER, KEYS.sonarr, KEYS.sonarr.slice(0, 10)]) assert.ok(!line.includes(secret), `leaked ${secret.slice(0, 6)}…`);
  assert.ok(entry.stack.length > 0 && !entry.stack.includes(KEYS.sonarr), 'the stack is redacted too');
  await wait(100);
  assert.equal(s.child.exitCode, null, 'still running');
  assert.equal(s.io.err, '');
});

test('uncaughtException: logged redacted (never Node\'s raw trace on stderr), then exit 1 for systemd to restart', async (t) => {
  const { KEYS } = await import('./helpers/host.js');
  const s = await crashingServer(t, `setTimeout(() => { throw ${failure(KEYS.sonarr)}; }, 1200)`);
  const [code] = await once(s.child, 'exit');
  assert.equal(code, 1);
  const line = s.io.out.split('\n').find((l) => l.includes('uncaught exception'));
  assert.ok(line, s.io.out);
  for (const secret of [INDEXER, KEYS.sonarr, KEYS.sonarr.slice(0, 10)]) assert.ok(!line.includes(secret), `leaked ${secret.slice(0, 6)}…`);
  assert.equal(s.io.err, '', 'nothing raw on stderr');
});
