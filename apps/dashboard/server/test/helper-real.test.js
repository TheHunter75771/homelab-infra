// The real helper (module H's homelab_dockerctl.py) against this client, over a real Unix socket. A tiny
// accept-shim hands each connection to the helper on stdin, as homelab-dockerctl.socket (Accept=yes) does.
// systemctl/journalctl are fake scripts; `docker` is a trap that leaves a marker if it is ever executed.
// Docker is "inactive", which is the case that matters: the client must stop at docker-state, and the helper
// must never run docker.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createDocker, createHelperClient, DockerInactiveError, HelperError } from '../src/helper/client.js';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');
const HELPER = path.join(REPO, 'roles/dashboard/files/homelab-dockerctl/homelab_dockerctl.py');
const STACKS = path.join(REPO, 'roles/dashboard/files/homelab-dockerctl/tests/fixtures/stacks');
const python = spawnSync('python3', ['--version']).status === 0;
const skip = !python ? 'python3 not available' : !fs.existsSync(HELPER) ? 'helper not present' : false;

const SHIM = `
import os, socket, subprocess, sys
path, helper, cfg = sys.argv[1:4]
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(path)
srv.listen(16)
print("ready", flush=True)
while True:
    conn, _ = srv.accept()
    subprocess.Popen([sys.executable, "-I", helper], stdin=conn.fileno(),
                     env={"HOMELAB_DOCKERCTL_CONFIG": cfg, "PATH": "/usr/bin:/bin"})
    conn.close()
`;

async function startRealHelper(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dash-real-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const script = (name, body) => {
    const p = path.join(dir, name);
    fs.writeFileSync(p, `#!/bin/sh\n${body}\n`, { mode: 0o755 });
    return p;
  };
  const usec = (iso) => Date.parse(iso) * 1000;
  const boots = [
    { index: -1, boot_id: 'a91f'.padEnd(32, '0'), first_entry: usec('2026-10-21T06:00:00Z'), last_entry: usec('2026-10-24T02:33:00Z') },
    { index: 0, boot_id: '4e1c'.padEnd(32, '0'), first_entry: usec('2026-10-24T02:40:00Z'), last_entry: usec('2026-10-24T14:00:00Z') },
  ];
  const marker = path.join(dir, 'DOCKER-WAS-RUN');
  fs.writeFileSync(path.join(dir, 'status'), 'failed\n');
  const cfg = path.join(dir, 'dockerctl.json');
  fs.writeFileSync(
    cfg,
    JSON.stringify({
      allowed_uid: process.getuid(),
      trusted_owner_uid: fs.statSync(STACKS).uid,
      stacks_dir: STACKS,
      media_mount: path.join(dir, 'media'),
      bootgate_status_file: path.join(dir, 'status'),
      cache_dir: path.join(dir, 'cache'),
      systemctl: script('systemctl', 'if [ "$1" = is-active ]; then echo inactive; exit 3; fi; exit 1'),
      journalctl: script('journalctl', `case "$*" in *--list-boots*) printf '%s' '${JSON.stringify(boots)}';; *) exit 1;; esac`),
      docker: script('docker', `touch '${marker}'; exit 1`),
      request_timeout: 5,
    }),
  );
  const socketPath = path.join(dir, 'h.sock');
  const shim = spawn('python3', ['-c', SHIM, socketPath, HELPER, cfg], { stdio: ['ignore', 'pipe', 'pipe'] });
  t.after(() => shim.kill('SIGKILL'));
  let stderr = '';
  shim.stderr.on('data', (d) => (stderr += d));
  await new Promise((resolve, reject) => {
    shim.stdout.on('data', (d) => String(d).includes('ready') && resolve());
    shim.on('exit', (code) => reject(new Error(`shim exited ${code}: ${stderr}`)));
  });
  return { socketPath, marker };
}

test('real helper: Docker inactive -> docker-state only; no Docker op is sent; docker never runs', { skip }, async (t) => {
  const h = await startRealHelper(t);
  const helper = createHelperClient({ socketPath: h.socketPath, timeoutMs: 10_000 });
  const docker = createDocker({ helper });
  assert.equal(await docker.dockerState(), 'inactive');
  await assert.rejects(docker.ps(), DockerInactiveError);
  await assert.rejects(docker.control('restart', 'sonarr'), DockerInactiveError);
  await assert.rejects(docker.update('sonarr'), DockerInactiveError);
  await assert.rejects(docker.logs('sonarr', { follow: true }), DockerInactiveError);
  assert.equal(fs.existsSync(h.marker), false, 'docker was never executed');
});

test('real helper: boots, last-pull and disk-split map onto the client shapes', { skip }, async (t) => {
  const h = await startRealHelper(t);
  const docker = createDocker({ helper: createHelperClient({ socketPath: h.socketPath, timeoutMs: 10_000 }) });
  assert.deepEqual(await docker.boots(), [
    { bootId: `a91f${'0'.repeat(28)}`, first: '2026-10-21T06:00:00Z', last: '2026-10-24T02:33:00Z' },
    { bootId: `4e1c${'0'.repeat(28)}`, first: '2026-10-24T02:40:00Z', last: '2026-10-24T14:00:00Z' },
  ]);
  assert.deepEqual(await docker.lastPull(), { lastPull: null, lastAttempt: null });
  await assert.rejects(docker.diskSplit(), (e) => e instanceof HelperError && e.code === 'media-unavailable', 'no du while the gate is not ok');
  assert.equal(fs.existsSync(h.marker), false);
});

test("real helper: ps's declared list (from the deployed compose files) maps onto the client shape", { skip }, async (t) => {
  const h = await startRealHelper(t);
  const helper = createHelperClient({ socketPath: h.socketPath, timeoutMs: 10_000 });
  // The helper answers ps without Docker (it reads compose files only). The dashboard never asks while Docker
  // is down; this test does, once, to check the shape against the real implementation.
  const forced = { request: (op, ...a) => (op === 'docker-state' ? Promise.resolve({ state: 'active' }) : helper.request(op, ...a)), stream: helper.stream };
  const { containers, declared } = await createDocker({ helper: forced }).psFull();
  assert.deepEqual(containers, [], 'Docker is down on the helper side: no containers');
  const by = Object.fromEntries(declared.map((d) => [d.name, d]));
  assert.deepEqual(Object.keys(by).sort(), ['cloudflared', 'jellyfin', 'jellyseerr', 'prowlarr', 'radarr', 'sabnzbd', 'sonarr', 'wol-relay']);
  assert.deepEqual(by['wol-relay'], { name: 'wol-relay', stack: 'utils', service: 'wol-relay', pinned: 'homelab/wol-relay:2026.07', local_build: true });
  assert.equal(by.sonarr.stack, 'media');
  assert.match(by.sonarr.pinned, /^lscr\.io\/linuxserver\/sonarr:.+@sha256:[0-9a-f]{64}$/);
  assert.equal(fs.existsSync(h.marker), false);
});
