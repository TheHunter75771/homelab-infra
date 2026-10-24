#!/usr/bin/env node
// Dev tool: runs the real server code (apps/dashboard/server, read-only) on its own test harness (fake host
// tree, fake Docker helper, fake media APIs) and saves GET /api/summary for each scenario to
// test/server-summaries/*.json. The web tests render these, and test/dumps.test.mjs regenerates them in
// memory to catch drift between the server and the saved copies.
//
// Deterministic: the server's clock is fixed at the harness's NOW, and the push subscriptions get fixed
// endpoints (device ids are a hash of the endpoint), so a re-dump is byte-identical.
//   npm run dump-summaries      (needs `npm ci` in apps/dashboard/server)
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const SERVER = path.resolve(ROOT, '../server');
export const OUT = path.join(ROOT, 'test/server-summaries');

// Can the server code run here? (Its only runtime dependency is web-push.)
export function serverAvailable() {
  return fs.existsSync(path.join(SERVER, 'test/helpers/server.js')) && fs.existsSync(path.join(SERVER, 'node_modules/web-push/package.json'));
}

const MEDIA_LOST_CONTAINERS = ['cloudflared', 'jellyfin', 'jellyseerr', 'prowlarr', 'radarr', 'sabnzbd', 'sonarr', 'wol-relay'].map((name) => {
  const up = ['cloudflared', 'wol-relay'].includes(name);
  const ref = name === 'wol-relay' ? 'homelab/wol-relay:2026.07' : `example/${name}:1.0.0@sha256:${'a'.repeat(64)}`;
  return { name, state: up ? 'running' : 'exited', health: up && name !== 'wol-relay' ? 'healthy' : null, started_at: '2026-10-21T10:02:00Z', restart_count: 0, image: ref, pinned: ref, stack: name === 'cloudflared' ? 'edge' : name === 'wol-relay' ? 'utils' : 'media' };
});

// Builds every scenario's summary as the exact text written to disk: {name: text}.
export async function buildSummaries() {
  const { startDashboard, getJson } = await import(pathToFileURL(path.join(SERVER, 'test/helpers/server.js')).href);
  const { NOW } = await import(pathToFileURL(path.join(SERVER, 'test/helpers/host.js')).href);
  const cleanups = [];
  const t = { after: (fn) => cleanups.push(fn) };
  const webDir = fs.mkdtempSync(path.join(os.tmpdir(), 'dash-web-')); // never public/: the harness may write a stub web build
  const out = {};

  // Fixed endpoints instead of the harness's random ones; the keys never reach the summary.
  const fixSubscriptions = (d) => {
    const file = path.join(d.host.pushDir, 'subscriptions.json');
    if (!fs.existsSync(file)) return;
    const subs = JSON.parse(fs.readFileSync(file, 'utf8')).map((s, i) => ({ ...s, endpoint: `https://fcm.googleapis.com/fcm/send/fixed-device-${i + 1}` }));
    fs.writeFileSync(file, `${JSON.stringify(subs, null, 2)}\n`);
  };

  async function dump(name, scenario, opts = {}, prepare) {
    const d = await startDashboard(t, scenario, { web: false, host: { webDir }, clock: () => NOW, ...opts, start: false });
    fixSubscriptions(d);
    prepare?.(d);
    await d.dash.start({ background: false });
    const { json } = await getJson(d.base, '/api/summary');
    out[name] = `${JSON.stringify(json, null, 2)}\n`;
  }

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
  return out;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  if (!serverAvailable()) {
    console.error('apps/dashboard/server is not installed: run npm ci there first');
    process.exit(1);
  }
  const summaries = await buildSummaries();
  fs.mkdirSync(OUT, { recursive: true });
  for (const [name, text] of Object.entries(summaries)) {
    const file = path.join(OUT, `${name}.json`);
    const same = fs.existsSync(file) && fs.readFileSync(file, 'utf8') === text;
    if (!same) fs.writeFileSync(file, text);
    const s = JSON.parse(text);
    console.log(`${same ? 'unchanged' : 'wrote    '} test/server-summaries/${name}.json (${s.apps.length} apps, gate ${s.gate.status}, ${s.issues.length} issues)`);
  }
  process.exit(0);
}
