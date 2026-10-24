// The helper client: wire protocol, failures, cancellation, and the docker-state guard (ADR 0013).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { createDocker, createHelperClient, DockerInactiveError, HelperError } from '../src/helper/client.js';
import { startFakeHelper } from './helpers/fake-helper.js';

// A socket server whose per-connection behaviour the test scripts directly.
async function scriptedServer(t, onConn) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dash-sock-'));
  const socketPath = path.join(dir, 's.sock');
  const conns = [];
  const server = net.createServer((sock) => {
    conns.push(sock);
    sock.setEncoding('utf8');
    let buf = '';
    sock.on('error', () => {});
    sock.on('data', (c) => {
      buf += c;
      const i = buf.indexOf('\n');
      if (i !== -1) onConn(sock, JSON.parse(buf.slice(0, i)));
    });
  });
  await new Promise((r) => server.listen(socketPath, r));
  t.after(async () => {
    for (const c of conns) c.destroy();
    await new Promise((r) => server.close(r));
    fs.rmSync(dir, { recursive: true, force: true });
  });
  return { socketPath, conns };
}

test('unary: sends one JSON line with op + args and resolves with data', async (t) => {
  let got;
  const s = await scriptedServer(t, (sock, req) => {
    got = req;
    sock.end(`${JSON.stringify({ ok: true, op: req.op, data: { containers: [{ name: 'sonarr' }] } })}\n`);
  });
  const c = createHelperClient({ socketPath: s.socketPath });
  assert.deepEqual(await c.request('ps'), { containers: [{ name: 'sonarr' }] });
  assert.deepEqual(got, { op: 'ps' });
  await c.request('restart', { name: 'sonarr' }).catch(() => {});
  assert.deepEqual(got, { op: 'restart', name: 'sonarr' });
});

test('unary failures: helper error code, no answer, garbage, missing socket, timeout, unknown op', async (t) => {
  const modes = ['refuse', 'close', 'garbage', 'no-ok', 'hang', 'inactive'];
  let i = 0;
  const s = await scriptedServer(t, (sock) => {
    const mode = modes[i++];
    if (mode === 'refuse') sock.end(`${JSON.stringify({ ok: false, error: { code: 'not-allowed', message: 'not a watched container' } })}\n`);
    if (mode === 'close') sock.end();
    if (mode === 'garbage') sock.end('this is not json\n');
    if (mode === 'no-ok') sock.end('{"data": 1}\n');
    if (mode === 'inactive') sock.end(`${JSON.stringify({ ok: false, error: { code: 'docker-inactive', message: 'docker.service is not active' } })}\n`);
  });
  const c = createHelperClient({ socketPath: s.socketPath, timeoutMs: 200 });
  await assert.rejects(c.request('ps'), (e) => e instanceof HelperError && e.code === 'not-allowed' && /watched/.test(e.message));
  await assert.rejects(c.request('ps'), (e) => e.code === 'protocol' && /without answering/.test(e.message));
  await assert.rejects(c.request('ps'), (e) => e.code === 'protocol' && /invalid JSON/.test(e.message));
  await assert.rejects(c.request('ps'), (e) => e.code === 'protocol');
  await assert.rejects(c.request('ps'), (e) => e.code === 'timeout');
  await assert.rejects(c.request('restart', { name: 'x' }), DockerInactiveError, 'the helper\'s docker-inactive maps to DockerInactiveError');
  await assert.rejects(createHelperClient({ socketPath: '/nonexistent/x.sock' }).request('ps'), (e) => e.code === 'unavailable');
  await assert.rejects(c.request('exec'), (e) => e.code === 'protocol' && /unknown unary op/.test(e.message));
  await assert.rejects(c.stream('ps', {}), (e) => /unknown stream op/.test(e.message));
});

test('stream: header, events, end event; refusal before the start; helper gone mid-stream; abort cancels', async (t) => {
  const header = (op) => `${JSON.stringify({ ok: true, op, stream: true, name: 'x' })}\n`;
  const s = await scriptedServer(t, (sock, req) => {
    if (req.mode === 'end') {
      sock.write(header('logs'));
      sock.write('{"type":"line","ts":null,"text":"a"}\n{"type":"line","ts":null,"text":"b"}\n');
      sock.end('{"type":"end","ok":true,"reason":"eof"}\n');
    } else if (req.mode === 'refused') {
      sock.end('{"ok":false,"error":{"code":"held-by-gate","message":"the boot gate status is failed"}}\n');
    } else if (req.mode === 'gone') {
      sock.write(header('logs'));
      sock.end('{"type":"line","ts":null,"text":"only"}\n'); // no end event
    } else if (req.mode === 'failed') {
      sock.write(header('update'));
      sock.end('{"type":"end","ok":false,"reason":"pulling: manifest unknown"}\n');
    } else {
      sock.write(header('logs'));
      sock.write('{"type":"line","ts":null,"text":"first"}\n'); // then stays open
    }
  });
  const c = createHelperClient({ socketPath: s.socketPath });
  const items = [];
  let started = null;
  const end = await c.stream('logs', { mode: 'end' }, { onItem: (m) => items.push(m.text), onStart: (m) => (started = m) });
  assert.deepEqual(end, { type: 'end', ok: true, reason: 'eof' });
  assert.deepEqual(items, ['a', 'b']);
  assert.equal(started.stream, true);
  await assert.rejects(c.stream('logs', { mode: 'refused' }), (e) => e instanceof HelperError && e.code === 'held-by-gate');
  assert.deepEqual(await c.stream('logs', { mode: 'gone' }, { onItem: () => {} }), { type: 'end', ok: false, reason: 'closed' });
  assert.deepEqual(await c.stream('update', { mode: 'failed' }), { type: 'end', ok: false, reason: 'pulling: manifest unknown' });

  const ctrl = new AbortController();
  const got = [];
  const p = c.stream('logs', { mode: 'open' }, { onItem: (m) => got.push(m.text), signal: ctrl.signal });
  await new Promise((r) => setTimeout(r, 50));
  const helperSide = s.conns.at(-1);
  const closed = new Promise((r) => helperSide.on('close', r));
  ctrl.abort();
  assert.equal((await p).aborted, true);
  await closed; // the helper sees the connection close: that is how a follow is cancelled
  assert.deepEqual(got, ['first']);
});

test('docker guard: no Docker op is ever sent unless docker-state says active', async (t) => {
  for (const state of ['inactive', 'failed', 'activating', 'deactivating']) {
    const h = await startFakeHelper(t, { dockerState: state });
    const docker = createDocker({ helper: createHelperClient({ socketPath: h.socketPath }) });
    await assert.rejects(docker.ps(), (e) => e instanceof DockerInactiveError && e.state === state);
    await assert.rejects(docker.control('restart', 'sonarr'), DockerInactiveError);
    await assert.rejects(docker.control('start', 'sonarr'), DockerInactiveError);
    await assert.rejects(docker.update('sonarr'), DockerInactiveError);
    await assert.rejects(docker.logs('sonarr', { follow: true }), DockerInactiveError);
    assert.deepEqual(h.calls.map((c) => c.op), Array(5).fill('docker-state'), 'only the state was asked, five times');
    assert.equal(h.dockerCalls().length, 0, `no docker ops while ${state}`);
    // non-Docker ops are fine while Docker is down
    assert.deepEqual(await docker.boots(), []);
    assert.ok(await docker.lastPull());
  }
});

test('docker guard: every Docker op does its own docker-state check, so a stop is seen at once', async (t) => {
  const h = await startFakeHelper(t);
  const docker = createDocker({ helper: createHelperClient({ socketPath: h.socketPath }) });
  await docker.ps();
  await docker.ps();
  assert.deepEqual(h.calls.map((c) => c.op), ['docker-state', 'ps', 'docker-state', 'ps']);
  h.state.dockerState = 'inactive'; // Docker goes down between two reads
  await assert.rejects(docker.ps(), DockerInactiveError);
  await assert.rejects(docker.control('restart', 'sonarr'), DockerInactiveError);
  assert.equal(h.calls.filter((c) => c.op === 'restart').length, 0);
  assert.ok(h.dockerCalls().every((c) => c.dockerStateAtCall === 'active'), 'every Docker op reached the helper while active');
});

test('typed ops pass their arguments and timeouts', async (t) => {
  const h = await startFakeHelper(t, {
    logs: { sonarr: [{ ts: '2026-10-24T10:00:00Z', text: 'hello' }] },
    boots: [{ index: 0, boot_id: 'ab'.repeat(16), first_entry: '2026-10-24T02:40:00Z', last_entry: '2026-10-24T14:00:00Z' }],
  });
  const docker = createDocker({ helper: createHelperClient({ socketPath: h.socketPath }) });
  const lines = [];
  await docker.logs('sonarr', { tail: 7, follow: false, since: '2026-10-24T00:00:00Z' }, { onLine: (l) => lines.push(l.text) });
  assert.deepEqual(lines, ['hello']);
  // exactly the fields the helper accepts for logs (unknown fields make it refuse)
  assert.deepEqual(h.calls.find((c) => c.op === 'logs'), { op: 'logs', name: 'sonarr', tail: 7, follow: false, since: '2026-10-24T00:00:00.000Z', dockerStateAtCall: 'active' });
  const steps = [];
  const final = await docker.update('sonarr', { onStep: (m) => steps.push(m.step) });
  assert.deepEqual(steps, ['pulling', 'recreating', 'waiting-health', 'done'], '"output" events are not steps');
  assert.equal(final.ok, true);
  assert.deepEqual(await docker.diskSplit(), { tv: 812e9, movies: 486e9, downloads: 62e9, computedAt: '2026-10-24T03:00:00Z', stale: false });
  assert.deepEqual(await docker.boots(), [{ bootId: 'ab'.repeat(16), first: '2026-10-24T02:40:00Z', last: '2026-10-24T14:00:00Z' }]);
  assert.deepEqual(await docker.lastPull(), { lastPull: '2026-10-23T21:40:00Z', lastAttempt: '2026-10-23T21:40:00Z' });
  const full = await docker.psFull();
  assert.equal(full.containers.length, 8);
  assert.deepEqual(full.declared.find((d) => d.name === 'wol-relay'), { name: 'wol-relay', stack: 'utils', service: 'wol-relay', pinned: 'homelab/wol-relay:2026.07', local_build: true });
  for (const op of ['ps', 'docker-state', 'last-pull', 'disk-split', 'boots', 'restart', 'stop', 'start']) {
    for (const c of h.calls.filter((x) => x.op === op)) {
      const fields = Object.keys(c).filter((k) => !['op', 'dockerStateAtCall'].includes(k));
      assert.deepEqual(fields, ['restart', 'stop', 'start'].includes(op) ? ['name'] : [], `${op} sends only its accepted fields`);
    }
  }
  await assert.rejects(docker.control('exec', 'sonarr'), /bad op/);
});
