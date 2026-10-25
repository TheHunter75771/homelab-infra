'use strict';
// Tests for roles/stacks/files/wol-relay/relay.js (the listen address and the bind retry).
//   node --test roles/stacks/tests/
// Uses loopback addresses only (127.0.0.2 needs no setup on Linux). The "address appears later" case
// runs in a private network namespace (`unshare -rn`) and is skipped where that isn't allowed.

const { test } = require('node:test');
const assert = require('node:assert');
const { spawn, spawnSync } = require('node:child_process');
const net = require('node:net');
const path = require('node:path');

const RELAY = path.join(__dirname, '..', 'files', 'wol-relay', 'relay.js');

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.once('error', reject);
    s.listen(0, '127.0.0.1', () => { const { port } = s.address(); s.close(() => resolve(port)); });
  });
}

function startRelay(env) {
  const child = spawn(process.execPath, [RELAY], {
    env: { PATH: process.env.PATH, WOL_RELAY_TOKEN: 'test-token-0123456789', ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.output = '';
  child.stdout.on('data', (d) => { child.output += d; });
  child.stderr.on('data', (d) => { child.output += d; });
  child.exited = new Promise((resolve) => child.on('exit', (code) => resolve(code)));
  return child;
}

async function waitFor(fn, ms = 5000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (fn()) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return false;
}

async function health(host, port) {
  try { return (await fetch(`http://${host}:${port}/health`)).status; } catch (e) { return e.cause?.code || 'error'; }
}

test('no LISTEN_ADDR: listens on every interface (the old behaviour)', async () => {
  const port = await freePort();
  const relay = startRelay({ PORT: String(port) });
  try {
    // Behaviour only (no log wording), so this also passes against the pre-LISTEN_ADDR relay.
    let status;
    assert.ok(await waitFor(() => { health('127.0.0.1', port).then((s) => { status = s; }); return status === 200; }), relay.output);
    assert.strictEqual(await health('127.0.0.1', port), 200);
    assert.strictEqual(await health('127.0.0.2', port), 200);
  } finally { relay.kill(); await relay.exited; }
});

test('LISTEN_ADDR: reachable on that address only', async () => {
  const port = await freePort();
  const relay = startRelay({ PORT: String(port), LISTEN_ADDR: '127.0.0.2' });
  try {
    assert.ok(await waitFor(() => relay.output.includes(`listening on 127.0.0.2:${port}`)), relay.output);
    assert.strictEqual(await health('127.0.0.2', port), 200);
    assert.strictEqual(await health('127.0.0.1', port), 'ECONNREFUSED');
  } finally { relay.kill(); await relay.exited; }
});

test('LISTEN_ADDR not on the host: waits and retries, does not exit, logs once', async () => {
  const port = await freePort();
  const relay = startRelay({ PORT: String(port), LISTEN_ADDR: '192.0.2.123' });
  try {
    assert.ok(await waitFor(() => relay.output.includes('192.0.2.123 is not on this host yet')), relay.output);
    const outcome = await Promise.race([relay.exited.then((c) => `exited ${c}`), new Promise((r) => setTimeout(() => r('alive'), 5000))]);
    assert.strictEqual(outcome, 'alive', relay.output);
    assert.strictEqual(relay.output.split('is not on this host yet').length - 1, 1, 'log is throttled');
    assert.ok(!relay.output.includes('listening on'), relay.output);
  } finally { relay.kill(); await relay.exited; }
});

test('any other bind error is fatal (exit 1), e.g. port already taken', async () => {
  const port = await freePort();
  const blocker = net.createServer();
  await new Promise((r) => blocker.listen(port, '127.0.0.2', r));
  const relay = startRelay({ PORT: String(port), LISTEN_ADDR: '127.0.0.2' });
  try {
    const code = await Promise.race([relay.exited, new Promise((r) => setTimeout(() => r('timeout'), 5000))]);
    assert.strictEqual(code, 1, relay.output);
    assert.match(relay.output, /cannot listen on 127\.0\.0\.2:\d+: EADDRINUSE/);
  } finally { relay.kill(); blocker.close(); }
});

const canNetns = spawnSync('unshare', ['-rn', 'true']).status === 0;
test('address appears after start (boot order): binds by itself and serves', { skip: !canNetns && 'unshare -rn not allowed here' }, () => {
  // In a private netns: start the relay on an address that doesn't exist yet, add it after 3 s,
  // then probe /health from inside the namespace.
  const script = `
    ip link set lo up
    PORT=9999 LISTEN_ADDR=10.9.9.9 WOL_RELAY_TOKEN=test-token-0123456789 "${process.execPath}" "${RELAY}" > relay.log 2>&1 &
    RELAY_PID=$!
    sleep 3
    ip addr add 10.9.9.9/32 dev lo
    for i in $(seq 1 25); do
      "${process.execPath}" -e "fetch('http://10.9.9.9:9999/health').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))" && { echo HEALTH_OK; break; }
      sleep 0.2
    done
    kill $RELAY_PID
    cat relay.log`;
  const r = spawnSync('unshare', ['-rn', 'sh', '-c', script], { cwd: require('node:os').tmpdir(), encoding: 'utf8', timeout: 20000 });
  const out = r.stdout + r.stderr;
  assert.match(out, /10\.9\.9\.9 is not on this host yet/);
  assert.match(out, /listening on 10\.9\.9\.9:9999/);
  assert.match(out, /HEALTH_OK/);
});
