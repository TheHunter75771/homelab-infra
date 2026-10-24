// The real entry point as systemd runs it: config file in DASHBOARD_CONFIG, JSON logs on stdout, SIGTERM.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { startFakeHelper } from './helpers/fake-helper.js';
import { buildHost, helperFor, KEYS, writeWeb } from './helpers/host.js';

const MAIN = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src/server.js');

function spawnMain(t, env) {
  const child = spawn(process.execPath, [MAIN], { env: { PATH: process.env.PATH, TZ: 'UTC', ...env }, stdio: ['ignore', 'pipe', 'pipe'] });
  t.after(() => child.exitCode === null && child.kill('SIGKILL'));
  const lines = [];
  let stderr = '';
  let buf = '';
  child.stderr.on('data', (d) => (stderr += d));
  const listening = new Promise((resolve, reject) => {
    child.stdout.on('data', (d) => {
      buf += d;
      let i;
      while ((i = buf.indexOf('\n')) !== -1) {
        const line = buf.slice(0, i);
        buf = buf.slice(i + 1);
        lines.push(line);
        const entry = JSON.parse(line); // every stdout line must be JSON
        if (entry.msg === 'listening') resolve(entry.port);
      }
    });
    child.on('exit', (code) => reject(new Error(`exited early (${code}): ${stderr}`)));
  });
  return { child, lines, listening, stderr: () => stderr };
}

// What role `dashboard` renders (roles/dashboard/templates/config.json.j2), read through DASHBOARD_CONFIG.
function renderConfig(host, helperSocket) {
  const c = host.cfg;
  const file = path.join(host.root, 'etc/homelab-dashboard/config.json');
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(
    file,
    JSON.stringify({
      listen_host: '127.0.0.1',
      listen_port: 8085,
      display_name: 'testhost',
      timezone: 'UTC',
      log_level: 'info',
      remote_enabled: false,
      web_dir: c.webDir,
      state_dir: c.stateDir,
      push_dir: c.pushDir,
      data_dir: c.dataDir,
      media_mount: path.join(host.root, 'media'),
      helper_socket: helperSocket,
      gate_dir: c.gateDir,
      gate_rerun_file: c.gateRerunFile,
      gate_retry_timer: 'bootgate-retry.timer',
      gate_retry_service: 'bootgate-retry.service',
      gate_retry_minutes: 5,
      reboot_required_file: c.rebootRequiredFile,
      backup_status_file: c.backupStatusFile,
      notify_history_file: c.notifyHistoryFile,
      rehearsal_flag: c.rehearsalFlag,
      pinned_images: c.pinnedImages,
      runbook_base_url: 'https://git.example.org/homelab-infra/blob/main/docs/runbooks',
      runbooks: [
        { title: 'Restore from backup', path: 'restore-from-scratch.md' },
        { title: 'Broken', path: '../../etc/passwd' },
      ],
      // test-only: the fixture's /proc
      proc_dir: c.procDir,
      integration_timeout_ms: 300,
    }),
  );
  return file;
}

test('server: starts from the config role dashboard renders, serves API + web, JSON logs without secrets, exits 0 on SIGTERM', async (t) => {
  const helper = await startFakeHelper(t, helperFor('normal'));
  const host = buildHost(t, 'normal', { helperSocket: helper.socketPath });
  writeWeb(host.cfg.webDir);
  const p = spawnMain(t, { DASHBOARD_CONFIG: renderConfig(host, helper.socketPath), NODE_ENV: 'production', PORT: '0' });
  const port = await p.listening;
  const base = `http://127.0.0.1:${port}`;
  assert.equal((await fetch(`${base}/healthz`)).status, 200);
  assert.match(await (await fetch(`${base}/`)).text(), /Homelab dashboard/);
  let summary;
  for (let i = 0; i < 50; i++) {
    const r = await fetch(`${base}/api/summary`);
    if (r.status === 200) {
      summary = await r.json();
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  assert.equal(summary.server.name, 'testhost');
  assert.equal(summary.server.timeZone, 'UTC');
  assert.deepEqual(summary.links.runbooks, [{ title: 'Restore from backup', url: 'https://git.example.org/homelab-infra/blob/main/docs/runbooks/restore-from-scratch.md' }]);
  assert.equal(summary.gate.status, 'ok');
  assert.equal(summary.backups.status, 'ok');
  assert.equal(summary.apps.length, 8);
  assert.ok(helper.calls.some((c) => c.op === 'ps'), 'talked to the helper socket');
  for (let i = 0; i < 100 && !p.lines.some((l) => l.includes('"collectors running"')); i++) await new Promise((r) => setTimeout(r, 20));
  p.child.kill('SIGTERM');
  const [code] = await once(p.child, 'exit');
  assert.equal(code, 0);
  assert.equal(p.stderr(), '');
  const out = p.lines.join('\n');
  assert.match(out, /"msg":"vapid keys loaded"/);
  assert.doesNotMatch(out, /unknown config keys/, 'every rendered key is understood');
  assert.match(out, /"level":"warn","msg":"config","warning":"runbooks\[1\]: path must be a relative .md path; skipped"/);
  assert.match(out, /"msg":"collectors running"/);
  assert.match(out, /"msg":"shutting down","signal":"SIGTERM"/);
  for (const secret of [...Object.values(KEYS), host.vapid.privateKey, ...host.subs.map((s) => s.endpoint)]) {
    assert.ok(!out.includes(secret), 'secret in logs');
  }
});

test('server: bad env or JSON config -> exit 78 with the reason; a named but missing JSON file -> a warning', async (t) => {
  const dir = buildHost(t, 'clean').root;
  const bad = path.join(dir, 'bad.json');
  fs.writeFileSync(bad, '{nope');
  const child = spawn(process.execPath, [MAIN], { env: { PATH: process.env.PATH, DASHBOARD_CONFIG: bad }, stdio: ['ignore', 'pipe', 'pipe'] });
  let err = '';
  child.stderr.on('data', (d) => (err += d));
  const [code] = await once(child, 'exit');
  assert.equal(code, 78);
  assert.match(JSON.parse(err.trim()).error, /cannot load/);
  for (const env of [{ DASHBOARD_REMOTE_ENABLED: 'maybe' }, { PORT: '99999' }, { DASHBOARD_BOOTGATE_RETRY_MINUTES: '0' }]) {
    const c = spawn(process.execPath, [MAIN], { env: { PATH: process.env.PATH, ...env }, stdio: ['ignore', 'pipe', 'pipe'] });
    let e = '';
    c.stderr.on('data', (x) => (e += x));
    assert.equal((await once(c, 'exit'))[0], 78, JSON.stringify(env));
    assert.equal(JSON.parse(e.trim()).msg, 'config error');
  }

  const missing = spawnMain(t, {
    DASHBOARD_CONFIG: path.join(dir, 'missing.json'),
    PORT: '0',
    HOST: '127.0.0.1',
    PUSH_DIR: path.join(dir, 'srv/homelab/push'),
    DASHBOARD_WEB_DIR: path.join(dir, 'web'),
  });
  const outcome = await missing.listening.then(() => 'listening', () => 'exited');
  if (outcome === 'exited' && !missing.child.stdout.readableEnded) await once(missing.child.stdout, 'end'); // every line read
  const logs = missing.lines.map((l) => JSON.parse(l));
  assert.equal(logs[0].msg, 'config file missing; using built-in defaults');
  assert.equal(logs[0].file, path.join(dir, 'missing.json'));
  // The default state dir (/var/lib/homelab-dashboard) only exists on the host: as an ordinary user the
  // process must stop with a JSON reason, not a stack trace.
  if (process.getuid?.() !== 0 && !fs.existsSync('/var/lib/homelab-dashboard')) {
    assert.equal(outcome, 'exited');
    assert.equal(missing.child.exitCode, 1);
    assert.deepEqual([logs.at(-1)?.msg, logs.at(-1)?.code], ['startup failed', 'EACCES']);
    assert.equal(missing.stderr(), '');
  }
});
