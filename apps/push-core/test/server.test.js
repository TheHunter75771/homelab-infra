// Process-level: the real entry point, the Docker healthcheck script, and graceful shutdown.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { once } from 'node:events';
import { APP_ROOT, fakeSubscription, postJson, tempDir, writeVapid } from './helpers/fixtures.js';

function startServer(t, dir, env = {}) {
  const child = spawn(process.execPath, [path.join(APP_ROOT, 'src/server.js')], {
    env: { PATH: process.env.PATH, PUSH_DIR: dir, PORT: '0', HOST: '127.0.0.1', ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  t.after(() => child.exitCode === null && child.kill('SIGKILL'));
  const lines = [];
  let stderr = '';
  child.stderr.on('data', (d) => (stderr += d));
  let buffer = '';
  const ready = new Promise((resolve, reject) => {
    child.stdout.on('data', (d) => {
      buffer += d;
      let i;
      while ((i = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, i);
        buffer = buffer.slice(i + 1);
        lines.push(line);
        const entry = JSON.parse(line); // every stdout line must be JSON
        if (entry.msg === 'listening') resolve(entry.port);
      }
    });
    child.on('exit', (code) => reject(new Error(`server exited early (${code}): ${stderr}`)));
  });
  return { child, lines, ready, stderr: () => stderr };
}

function runHealthcheck(port) {
  const child = spawn(process.execPath, [path.join(APP_ROOT, 'src/healthcheck.js')], {
    env: { PATH: process.env.PATH, PORT: String(port) },
    stdio: 'ignore',
  });
  return once(child, 'exit').then(([code]) => code);
}

test('server starts, serves, passes the healthcheck, logs JSON without secrets, exits 0 on SIGTERM', async (t) => {
  const dir = tempDir(t);
  const vapid = writeVapid(dir);
  const srv = startServer(t, dir);
  const port = await srv.ready;
  const base = `http://127.0.0.1:${port}`;

  assert.equal((await fetch(`${base}/healthz`)).status, 200);
  assert.equal(await runHealthcheck(port), 0);

  const sub = fakeSubscription();
  assert.equal((await postJson(base, '/api/subscribe', { subscription: sub, label: 'phone' })).status, 201);
  const saved = JSON.parse(fs.readFileSync(path.join(dir, 'subscriptions.json'), 'utf8'));
  assert.equal(saved[0].endpoint, sub.endpoint);

  srv.child.kill('SIGTERM');
  const [code] = await once(srv.child, 'exit');
  assert.equal(code, 0);
  assert.equal(srv.stderr(), '');

  const out = srv.lines.join('\n');
  assert.match(out, /"msg":"vapid keys loaded"/);
  assert.match(out, /"msg":"shutting down","signal":"SIGTERM"/);
  assert.ok(!out.includes(vapid.privateKey));
  assert.ok(!out.includes(sub.endpoint));
  assert.ok(!out.includes(sub.keys.auth));
});

test('healthcheck fails when the server is unhealthy or down', async (t) => {
  const dir = tempDir(t);
  writeVapid(dir);
  fs.writeFileSync(path.join(dir, 'subscriptions.json'), 'garbage');
  fs.writeFileSync(path.join(dir, '.subscriptions.json.99.abcdefabcdef.tmp'), 'stale');
  const srv = startServer(t, dir);
  const port = await srv.ready;
  assert.equal(await runHealthcheck(port), 1);
  srv.child.kill('SIGTERM');
  await once(srv.child, 'exit');
  assert.equal(await runHealthcheck(port), 1, 'nothing listening');
  const out = srv.lines.join('\n');
  assert.match(out, /store unreadable; writes disabled until fixed/);
  assert.match(out, /removed stale temp files/);
  assert.equal(fs.readFileSync(path.join(dir, 'subscriptions.json'), 'utf8'), 'garbage');
  assert.deepEqual(fs.readdirSync(dir).sort(), ['subscriptions.json', 'vapid.json']);
});

test('missing vapid.json: server still starts, logs an error, healthz is 503', async (t) => {
  const dir = tempDir(t);
  const srv = startServer(t, dir);
  const port = await srv.ready;
  const res = await fetch(`http://127.0.0.1:${port}/healthz`);
  assert.equal(res.status, 503);
  assert.match(srv.lines.join('\n'), /"level":"error","msg":"vapid keys unavailable"/);
  srv.child.kill('SIGINT');
  assert.equal((await once(srv.child, 'exit'))[0], 0);
});
