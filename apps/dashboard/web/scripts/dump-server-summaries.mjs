#!/usr/bin/env node
// Dev tool: runs the real server code (apps/dashboard/server, read-only) on its own test harness (fake host
// tree, fake Docker helper, fake media APIs) and saves GET /api/summary for each scenario to
// test/server-summaries/*.json. The web tests render these, so the client is checked against what the
// server really sends, not only against hand-written fixtures.
//   npm run dump-summaries      (needs `npm ci` in apps/dashboard/server)
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SERVER = path.resolve(ROOT, '../server');
const OUT = path.join(ROOT, 'test/server-summaries');
const { startDashboard, getJson } = await import(pathToFileURL(path.join(SERVER, 'test/helpers/server.js')).href);

const cleanups = [];
const t = { after: (fn) => cleanups.push(fn) };
const webDir = fs.mkdtempSync(path.join(os.tmpdir(), 'dash-web-')); // never public/: the harness may write a stub web build

const MEDIA_LOST_CONTAINERS = ['cloudflared', 'jellyfin', 'jellyseerr', 'prowlarr', 'radarr', 'sabnzbd', 'sonarr', 'wol-relay'].map((name) => {
  const up = ['cloudflared', 'wol-relay'].includes(name);
  const ref = name === 'wol-relay' ? 'homelab/wol-relay:2026.07' : `example/${name}:1.0.0@sha256:${'a'.repeat(64)}`;
  return { name, state: up ? 'running' : 'exited', health: up && name !== 'wol-relay' ? 'healthy' : null, started_at: '2026-10-21T10:02:00Z', restart_count: 0, image: ref, pinned: ref, stack: name === 'cloudflared' ? 'edge' : name === 'wol-relay' ? 'utils' : 'media' };
});

async function dump(name, scenario, opts = {}, prepare) {
  const d = await startDashboard(t, scenario, { web: false, host: { webDir }, ...opts, start: !prepare });
  if (prepare) {
    prepare(d);
    await d.dash.start({ background: false });
  }
  const { json } = await getJson(d.base, '/api/summary');
  fs.writeFileSync(path.join(OUT, `${name}.json`), `${JSON.stringify(json, null, 2)}\n`);
  console.log(`wrote test/server-summaries/${name}.json (${json.apps.length} apps, gate ${json.gate.status}, ${json.issues.length} issues)`);
}

fs.mkdirSync(OUT, { recursive: true });
try {
  for (const s of ['normal', 'bad-night', 'clean', 'one-app-down', 'docker-inactive']) await dump(s, s);
  await dump('media-lost', 'normal', { helper: { containers: MEDIA_LOST_CONTAINERS } }, (d) => {
    d.host.write('var/lib/bootgate/status', 'media-lost\n');
    d.host.write('var/lib/bootgate/history.jsonl', `${JSON.stringify({ ts: '2026-10-24T14:02:00Z', boot_id: '4e1c', event: 'media-lost', detail: 'verify failed', wait_seconds: 0 })}\n`);
  });
} finally {
  for (const fn of cleanups.reverse()) await fn();
  fs.rmSync(webDir, { recursive: true, force: true });
}
process.exit(0);
