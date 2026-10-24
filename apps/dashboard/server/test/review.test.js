// Review fixes (2026-10-24): ADR 0015 ports, secrets in app text, validation hardening, statfs single-flight.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { DEFAULT_APPS } from '../src/catalog.js';
import { statfsTimed } from '../src/collectors/host.js';
import { buildConfig } from '../src/config.js';
import { createStatic } from '../src/http/static.js';
import { createSecrets, parseLogItem } from '../src/redact.js';
import { createSettings } from '../src/settings.js';
import { KEYS, makeFetch, tempDir, writeWeb } from './helpers/host.js';
import { request, sse, startDashboard } from './helpers/server.js';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');
const readRepo = (p) => (fs.existsSync(path.join(REPO, p)) ? fs.readFileSync(path.join(REPO, p), 'utf8') : null);

// ---- ADR 0015: every catalog `open` has a matching tailscale serve handler --------------------------------

const opens = Object.entries(DEFAULT_APPS)
  .filter(([, a]) => a.open)
  .map(([name, a]) => ({ name, port: a.open.port, path: a.open.path }));

test('ADR 0015: the path apps open on 8444, Jellyseerr on 8443, nothing app-side on 443', () => {
  assert.deepEqual(
    Object.fromEntries(opens.map((o) => [o.name, `${o.port}${o.path}`])),
    { jellyfin: '8444/watch', jellyseerr: '8443/', sonarr: '8444/sonarr', radarr: '8444/radarr', prowlarr: '8444/prowlarr', sabnzbd: '8444/sabnzbd' },
  );
  assert.ok(opens.every((o) => Number.isInteger(o.port) && o.port !== 443), '443 is the dashboard only');
});

const contracts = readRepo('docs/CONTRACTS.md');
test('ADR 0015: catalog matches the CONTRACTS §6 serve table', { skip: !contracts && 'CONTRACTS.md not present' }, () => {
  const s6 = contracts.slice(contracts.indexOf('## 6.'), contracts.indexOf('## 7.'));
  const handlers = [];
  for (const m of s6.matchAll(/HTTPS (\d+): (.+)$/gm)) {
    for (const h of m[2].matchAll(/`(\/[^`]*)`\s*→/g)) handlers.push(`${m[1]}${h[1]}`);
  }
  assert.ok(handlers.includes('443/') && handlers.includes('8444/sonarr'), `parsed the table: ${handlers.join(' ')}`);
  for (const o of opens) assert.ok(handlers.includes(`${o.port}${o.path}`), `${o.name}: no §6 handler for ${o.port}${o.path}`);
});

const serveDefaults = readRepo('roles/tailscale_serve/defaults/main.yml');
test(
  'ADR 0015: catalog matches roles/tailscale_serve handlers',
  { skip: (!serveDefaults || !/port:\s*8444/.test(serveDefaults)) && 'role tailscale_serve not switched to 8444 yet (module C)' },
  () => {
    const handlers = [...serveDefaults.matchAll(/\{\s*port:\s*(\d+),\s*mount:\s*(\S+?),/g)].map((m) => `${m[1]}${m[2]}`);
    for (const o of opens) assert.ok(handlers.includes(`${o.port}${o.path}`), `${o.name}: no serve handler for ${o.port}${o.path}`);
  },
);

// ---- secrets in text copied from the apps ------------------------------------------------------------------

const INDEXER_KEY = 'ix'.repeat(8) + '42';
const BEARER = 'br'.repeat(8) + '99';

test('SABnzbd warnings, queue names and *arr health text reach the summary redacted', async (t) => {
  const base = makeFetch({ scenario: 'normal' });
  const json = (b) => new Response(JSON.stringify(b), { status: 200, headers: { 'Content-Type': 'application/json' } });
  const fetchImpl = async (url, init) => {
    const u = new URL(url);
    if (u.port === '8080' && u.searchParams.get('mode') === 'warnings') {
      return json({ warnings: [{ text: `Login failed for https://indexer.example/api?t=search&apikey=${INDEXER_KEY} (API Key: ${INDEXER_KEY}) Bearer ${BEARER}`, type: 'ERROR' }] });
    }
    if (u.port === '8080') {
      return json({ queue: { paused: false, kbpersec: '1', noofslots_total: 1, slots: [{ filename: `Show.S01E01?token=${INDEXER_KEY}`, percentage: '1', timeleft: '0:01:00', status: 'Downloading' }] } });
    }
    if (u.port === '8989') return json([{ source: 'IndexerCheck', type: 'error', message: `Indexer rejected api_key=${KEYS.sonarr}; also ${KEYS.radarr}` }]);
    return base(url, init);
  };
  const d = await startDashboard(t, 'normal', { fetch: fetchImpl });
  const text = JSON.stringify(d.dash.summary.current().summary);
  for (const secret of [INDEXER_KEY, BEARER, KEYS.sonarr, KEYS.radarr, KEYS.sabnzbd]) assert.ok(!text.includes(secret), `leaked ${secret.slice(0, 4)}…`);
  assert.match(d.dash.summary.current().summary.media.sabnzbd.loginProblem, /apikey=\[redacted\].*API Key: \[redacted\].*Bearer \[redacted\]/);
  assert.match(d.dash.summary.current().summary.media.sonarr.health[0].message, /api_key=\[redacted\]; also \[redacted\]/);
});

test('keys are registered from the config files on every media refresh, even with every app held', async (t) => {
  const d = await startDashboard(t, 'bad-night'); // no tile calls its app; no logs can be read
  for (const app of ['sonarr', 'radarr', 'prowlarr', 'sabnzbd']) {
    assert.equal(d.dash.secrets.redact(`x ${KEYS[app]} y`), 'x [redacted] y', app);
  }
});

test("a crash-looping app's own key is redacted from its logs even though its tile never ran", async (t) => {
  const d = await startDashboard(t, 'one-app-down', { start: false, helper: { logs: { radarr: [{ ts: null, text: `[Error] startup failed, key ${KEYS.radarr} rejected` }] } } });
  // no media refresh has run: the logs endpoint registers keys from the config files itself
  const s = sse(d.port, '/api/apps/radarr/logs?tail=10');
  const lines = await s.until('lines', () => true);
  assert.equal(lines.data[0].msg, '[Error] startup failed, key [redacted] rejected');
  s.close();
});

test('redaction runs before truncation: a key cut at 4000 characters never leaks a prefix', () => {
  const secrets = createSecrets();
  const key = 'k'.repeat(10) + 'SECRETTAIL0123456789';
  secrets.add(key);
  for (const offset of [3985, 3990, 3995, 3999]) {
    const item = parseLogItem({ ts: null, text: `${'a'.repeat(offset)}${key} tail` }, secrets);
    assert.ok(!item.msg.includes(key.slice(0, 10)), `offset ${offset}`);
    assert.ok(item.msg.length <= 4000);
  }
});

test('credential shapes: "API Key:" with a space, bare Bearer tokens, apikey=/api_key=/token= query values', () => {
  const s = createSecrets();
  const cases = [
    [`SABnzbd: API Key: ${INDEXER_KEY}`, 'SABnzbd: API Key: [redacted]'],
    [`GET /api?apikey=${INDEXER_KEY}&t=caps`, 'GET /api?apikey=[redacted]&t=caps'],
    [`GET /api?api_key=${INDEXER_KEY}`, 'GET /api?api_key=[redacted]'],
    [`callback?token=${INDEXER_KEY}`, 'callback?token=[redacted]'],
    [`sent Bearer ${BEARER} upstream`, 'sent Bearer [redacted] upstream'],
    [`X-Api-Key: ${INDEXER_KEY}`, 'X-Api-Key: [redacted]'],
    [`next=%2Fapi%3Fapikey%3D${INDEXER_KEY}`, 'next=%2Fapi%3Fapikey%3D[redacted]'],
    [`API%20Key%3A%20${INDEXER_KEY}`, 'API%20Key%3A%20[redacted]'],
    [`Bearer%20${BEARER}`, 'Bearer%20[redacted]'],
  ];
  for (const [input, expected] of cases) assert.equal(s.redact(input), expected, input);
});

// ---- validation ------------------------------------------------------------------------------------------------

test('settings: inherited names (constructor, __proto__, toString) and unknown thresholds are refused, file untouched', async (t) => {
  const dir = tempDir(t);
  const s = createSettings({ stateDir: dir });
  s.update({ thresholds: { diskWarnPct: 85 } });
  const before = fs.readFileSync(path.join(dir, 'settings.json'), 'utf8');
  for (const field of ['constructor', 'toString', 'hasOwnProperty', '__proto__']) {
    assert.throws(() => s.update(JSON.parse(`{"integrations": {"${field}": "abcdefghijkl"}}`)), /unknown integration field/, field);
  }
  assert.throws(() => s.update({ thresholds: { diskWarnPct: 80, bogus: 1 } }), /unknown threshold\(s\): bogus/);
  assert.throws(() => s.update({ thresholds: [80] }), /thresholds must be an object/);
  assert.equal(fs.readFileSync(path.join(dir, 'settings.json'), 'utf8'), before);
  const d = await startDashboard(t, 'clean');
  const r = await request(d.base, 'PUT', '/api/settings', { body: '{"integrations": {"constructor": "abcdefghijkl"}}' });
  assert.equal(r.status, 400);
});

test('static: a symlinked directory pointing outside web_dir is not served', (t) => {
  const root = tempDir(t);
  const web = path.join(root, 'web');
  const outside = path.join(root, 'outside');
  writeWeb(web);
  fs.mkdirSync(outside);
  fs.writeFileSync(path.join(outside, 'secret.txt'), 'nope');
  fs.symlinkSync(outside, path.join(web, 'linked'));
  const st = createStatic({ webDir: web });
  const res = { writeHead() {}, end() {} };
  assert.equal(st.handle({ headers: {} }, res, '/linked/secret.txt'), false);
  assert.equal(st.handle({ headers: {} }, res, '/assets/app.js'), true, 'normal files still served');
});

test('config: values are type-checked; a bad one warns and falls back to the default', () => {
  const { config, warnings } = buildConfig(
    {
      remote_enabled: 'true',
      intervals: { core: 0, media: 5, sampler: 60, split: 1, bogus: 3 },
      listen_port: '8085',
      log_level: 'loud',
      state_dir: 'relative/path',
      gate_retry_minutes: 0,
      gate_retry_timer: 'bootgate-retry.service',
      pinned_images: { sonarr: 42 },
      access_jwt: { team_domain: 5 },
      runbook_base_url: 'https://git.example.org/docs',
      runbooks: [{ title: 'Ok', path: 'ok.md' }, { title: 'Txt', path: 'notes.txt' }, { title: 'Anchor', path: 'ok.md#step-2' }],
    },
    {},
  );
  assert.equal(config.remoteEnabled, false, 'the string "true" is not a boolean');
  assert.deepEqual([config.intervals.core, config.intervals.media, config.intervals.sampler, config.intervals.split], [10, 30, 60, 3600]);
  assert.equal(config.listenPort, 8085);
  assert.equal(config.logLevel, 'info');
  assert.equal(config.stateDir, '/var/lib/homelab-dashboard');
  assert.equal(config.gateRetryMinutes, 5);
  assert.equal(config.gateRetryTimer, 'bootgate-retry.timer');
  assert.deepEqual(config.pinnedImages, {});
  assert.deepEqual(config.accessJwt, { teamDomain: '', audience: '' });
  assert.deepEqual(config.runbookLinks.map((l) => l.title), ['Ok', 'Anchor']);
  for (const w of [
    'remote_enabled: must be true or false (a JSON boolean); using the default',
    'intervals.core: must be a whole number of seconds 2-3600; using the default',
    'intervals.media: must be a whole number of seconds 10-3600; using the default',
    'intervals.split: must be a whole number of seconds 60-86400; using the default',
    'intervals.bogus: unknown interval; ignored',
    'listen_port: must be a whole number 0-65535; using the default',
    'state_dir: must be an absolute path; using the default',
    'runbooks[1]: path must be a relative .md path; skipped',
  ]) {
    assert.ok(warnings.includes(w), `missing warning: ${w}\n${warnings.join('\n')}`);
  }
  assert.equal(buildConfig({ remote_enabled: true }, {}).config.remoteEnabled, true, 'a real boolean is accepted');
  assert.equal(buildConfig({ intervals: { core: 2 } }, {}).config.intervals.core, 2, 'the minimum itself is fine');
});

// ---- statfs single-flight --------------------------------------------------------------------------------------

test('statfs: while a call on a path hangs, no second statfs on that path is started', async () => {
  let calls = 0;
  let release;
  let hang = true;
  const statfs = (p) => {
    calls += 1;
    if (p === '/media' && hang) return new Promise((r) => (release = r));
    return Promise.resolve({ bsize: 1, blocks: 2, bfree: 1, bavail: 1 });
  };
  for (let i = 0; i < 3; i++) await assert.rejects(statfsTimed('/media', { timeoutMs: 20, statfs }), { code: 'ETIMEDOUT' });
  assert.equal(calls, 1, 'one pool thread at most for the hung path');
  await statfsTimed('/', { timeoutMs: 20, statfs });
  assert.equal(calls, 2, 'other paths are independent');
  hang = false;
  release({ bsize: 1, blocks: 2, bfree: 1, bavail: 1 }); // the hung call finally returns
  await new Promise((r) => setTimeout(r, 5));
  assert.equal((await statfsTimed('/media', { timeoutMs: 20, statfs })).blocks, 2);
  assert.equal(calls, 3, 'once it settled, a new call goes through');
});

test('gate re-run: an unreadable gate status is a 503 up front, and a failed action (not a crash) mid-run', async (t) => {
  if (process.getuid?.() === 0) return t.skip('root ignores mode bits');
  const { makeRun } = await import('./helpers/host.js');
  const { postJson } = await import('./helpers/server.js');
  const d = await startDashboard(t, 'bad-night', { run: makeRun({ retryStates: ['inactive'] }) });
  const statusFile = d.host.path('var/lib/bootgate/status');
  fs.chmodSync(statusFile, 0o000);
  try {
    const r = await postJson(d.base, '/api/gate/rerun', {});
    assert.equal(r.status, 503);
    assert.equal(r.json.error, "Can't read the gate status (EACCES).");
  } finally {
    fs.chmodSync(statusFile, 0o644);
  }
  // mid-run: the gate picks up the trigger, then the status file becomes unreadable
  const trigger = d.host.path('run/bootgate/rerun');
  const gate = setInterval(() => {
    if (fs.existsSync(trigger)) {
      fs.unlinkSync(trigger);
      fs.chmodSync(statusFile, 0o000);
    }
  }, 10);
  let a;
  try {
    const r = await postJson(d.base, '/api/gate/rerun', {});
    assert.equal(r.status, 202);
    for (let i = 0; i < 300 && d.dash.actions.get(r.json.action.id)?.state === 'running'; i++) await new Promise((res) => setTimeout(res, 10));
    a = d.dash.actions.get(r.json.action.id);
  } finally {
    clearInterval(gate);
    fs.chmodSync(statusFile, 0o644);
  }
  assert.equal(a.state, 'failed');
  assert.equal(a.error, "Can't read the gate status (EACCES).");
  assert.ok(d.dash.server.listening);
});
