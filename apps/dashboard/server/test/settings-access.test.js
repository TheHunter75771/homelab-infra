import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createSign, generateKeyPairSync } from 'node:crypto';
import { AccessDenied, createAccess, verifyAccessJwt } from '../src/access.js';
import { createSettings, DEFAULT_SETTINGS, SettingsError } from '../src/settings.js';
import { forAccess } from '../src/summary.js';
import { tempDir } from './helpers/push-fixtures.js';
import { getJson, postJson, request, startDashboard } from './helpers/server.js';
import { KEYS } from './helpers/host.js';

// fake keys built at runtime, so no literal in the repo looks like a credential
const JF = 'j'.repeat(12) + '6789';
const JS = 'M'.repeat(28);

// ---- settings -------------------------------------------------------------------------------------

test('settings: defaults, partial updates, 0600 file, keys masked in the view', (t) => {
  const dir = tempDir(t);
  const s = createSettings({ stateDir: dir });
  assert.deepEqual(s.get(), DEFAULT_SETTINGS);
  const view = s.update({ thresholds: { diskWarnPct: 85 }, integrations: { jellyfinApiKey: JF } });
  assert.deepEqual(view.thresholds, { diskWarnPct: 85, backupMaxAgeH: 36 });
  assert.deepEqual(view.integrations, { jellyfin: { configured: true, last4: '6789' }, jellyseerr: { configured: false, last4: null } });
  assert.ok(!JSON.stringify(view).includes(JF));
  const file = path.join(dir, 'settings.json');
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).integrations.jellyfinApiKey, JF);
  assert.equal(createSettings({ stateDir: dir }).apiKey('jellyfin'), JF, 'persisted');
  s.update({ integrations: { jellyfinApiKey: null } });
  assert.equal(s.apiKey('jellyfin'), '');
  s.update({ integrations: { jellyseerrApiKey: JS } });
  assert.equal(s.view().integrations.jellyseerr.configured, true);
});

test('settings: subscriptions normalised (keys, names, dates); invalid input rejected without writing', (t) => {
  const dir = tempDir(t);
  const s = createSettings({ stateDir: dir });
  const v = s.update({
    subscriptions: [
      { name: '  Indexer A ', date: '2026-11-16' },
      { name: 'Indexer A', date: '2027-01-01' },
      { key: 'usenet', name: 'Usenet\nprovider', date: '2026-11-01' },
    ],
  });
  assert.deepEqual(v.subscriptions, [
    { key: 'indexer-a', name: 'Indexer A', date: '2026-11-16' },
    { key: 'indexer-a-1', name: 'Indexer A', date: '2027-01-01' },
    { key: 'usenet', name: 'Usenet provider', date: '2026-11-01' },
  ]);
  const before = fs.readFileSync(path.join(dir, 'settings.json'), 'utf8');
  const bad = [
    { thresholds: { diskWarnPct: 49 } },
    { thresholds: { diskWarnPct: 100 } },
    { thresholds: { diskWarnPct: 90.5 } },
    { thresholds: { backupMaxAgeH: 0 } },
    { thresholds: { backupMaxAgeH: '36' } },
    { subscriptions: [{ name: 'x', date: '2026-02-30' }] },
    { subscriptions: [{ name: '', date: '2026-02-01' }] },
    { subscriptions: [{ name: 'x'.repeat(65), date: '2026-02-01' }] },
    { subscriptions: Array.from({ length: 21 }, (_, i) => ({ name: `s${i}`, date: '2026-02-01' })) },
    { subscriptions: 'nope' },
    { integrations: { jellyfinApiKey: 'short' } },
    { integrations: { jellyfinApiKey: 'has space in it' } },
    { integrations: { sonarrApiKey: 'abcdefghijkl' } },
    { remote: true },
    [],
  ];
  for (const patch of bad) assert.throws(() => s.update(patch), SettingsError, JSON.stringify(patch));
  assert.equal(fs.readFileSync(path.join(dir, 'settings.json'), 'utf8'), before);
});

test('settings: a corrupt settings.json falls back to defaults and is logged', (t) => {
  const dir = tempDir(t);
  fs.writeFileSync(path.join(dir, 'settings.json'), '{oops');
  const logs = [];
  const s = createSettings({ stateDir: dir, log: { error: (msg) => logs.push(msg) } });
  assert.deepEqual(s.get(), DEFAULT_SETTINGS);
  assert.deepEqual(logs, ['settings.json unreadable; using defaults until it is fixed']);
});

test('GET/PUT /api/settings over HTTP: never returns keys; 400 with the reason; summary thresholds follow', async (t) => {
  const d = await startDashboard(t, 'normal');
  const g = await getJson(d.base, '/api/settings');
  assert.equal(g.status, 200);
  assert.deepEqual(g.json.integrations, { jellyfin: { configured: true, last4: KEYS.jellyfin.slice(-4) }, jellyseerr: { configured: true, last4: KEYS.jellyseerr.slice(-4) } });
  assert.ok(!g.text.includes(KEYS.jellyfin) && !g.text.includes(KEYS.jellyseerr));
  const bad = await request(d.base, 'PUT', '/api/settings', { body: { thresholds: { diskWarnPct: 20 } } });
  assert.equal(bad.status, 400);
  assert.equal(bad.json.error, 'diskWarnPct must be a whole number from 50 to 99');
  const ok = await request(d.base, 'PUT', '/api/settings', { body: { thresholds: { diskWarnPct: 30 + 3 * 10 } } });
  assert.equal(ok.status, 200);
  await d.dash.summary.refreshCore();
  const s = d.dash.summary.current().summary;
  assert.equal(s.settings.thresholds.diskWarnPct, 60);
  assert.ok(!d.text().includes(KEYS.jellyfin));
});

test('a new Jellyfin key from Settings is used for the next media refresh and redacted from logs', async (t) => {
  const d = await startDashboard(t, 'normal');
  await request(d.base, 'PUT', '/api/settings', { body: { integrations: { jellyfinApiKey: null } } });
  await d.dash.summary.refreshMedia();
  assert.equal(d.dash.summary.current().summary.media.jellyfin.state, 'unconfigured');
  assert.equal(d.dash.summary.current().summary.media.jellyfin.message, 'Add the Jellyfin API key in Settings.');
  await request(d.base, 'PUT', '/api/settings', { body: { integrations: { jellyfinApiKey: KEYS.jellyfin } } });
  await d.dash.summary.refreshMedia();
  assert.equal(d.dash.summary.current().summary.media.jellyfin.state, 'ok');
  assert.equal(d.dash.secrets.redact(`x ${KEYS.jellyfin}`), 'x [redacted]');
});

// ---- access ---------------------------------------------------------------------------------------

function makeJwt({ kid = 'k1', alg = 'RS256', claims, key }) {
  const enc = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
  const h = enc({ alg, kid, typ: 'JWT' });
  const p = enc(claims);
  const sig = createSign('RSA-SHA256').update(`${h}.${p}`).sign(key).toString('base64url');
  return `${h}.${p}.${sig}`;
}

test('Access JWT verification (the path that stays off in v2.0)', () => {
  const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
  const other = generateKeyPairSync('rsa', { modulusLength: 2048 });
  const jwk = { ...publicKey.export({ format: 'jwk' }), kid: 'k1' };
  const now = Date.parse('2026-10-24T14:00:00Z');
  const claims = { aud: ['aud-123'], iss: 'https://team.example', exp: now / 1000 + 600, nbf: now / 1000 - 10, email: 'x' };
  const opts = { keys: [jwk], audience: 'aud-123', issuer: 'https://team.example', now };
  assert.equal(verifyAccessJwt(makeJwt({ claims, key: privateKey }), opts).aud[0], 'aud-123');
  const bad = [
    ['wrong key', makeJwt({ claims, key: other.privateKey })],
    ['unknown kid', makeJwt({ claims, key: privateKey, kid: 'k2' })],
    ['alg none', makeJwt({ claims, key: privateKey, alg: 'none' })],
    ['wrong aud', makeJwt({ claims: { ...claims, aud: 'other' }, key: privateKey })],
    ['wrong iss', makeJwt({ claims: { ...claims, iss: 'https://evil.example' }, key: privateKey })],
    ['expired', makeJwt({ claims: { ...claims, exp: now / 1000 - 3600 }, key: privateKey })],
    ['not yet', makeJwt({ claims: { ...claims, nbf: now / 1000 + 3600 }, key: privateKey })],
    ['garbage', 'a.b.c'],
    ['two parts', 'a.b'],
  ];
  for (const [why, token] of bad) assert.throws(() => verifyAccessJwt(token, opts), AccessDenied, why);
});

test('access: remote_enabled=false -> always tailnet, even with Cloudflare headers', async () => {
  const access = createAccess({ cfg: { remoteEnabled: false, accessJwt: {} } });
  assert.equal(await access.decide({ headers: {} }), 'tailnet');
  assert.equal(await access.decide({ headers: { 'cf-ray': 'x', 'cf-access-jwt-assertion': 'junk' } }), 'tailnet');
});

test('access: remote_enabled=true -> Cloudflare requests need a valid token and are "remote"; others are tailnet', async () => {
  const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
  const jwk = { ...publicKey.export({ format: 'jwk' }), kid: 'k1' };
  let fetched = 0;
  const fetchImpl = async (url) => {
    fetched += 1;
    assert.equal(url, 'https://team.example/cdn-cgi/access/certs');
    return new Response(JSON.stringify({ keys: [jwk] }), { status: 200 });
  };
  const now = Date.now();
  const access = createAccess({ cfg: { remoteEnabled: true, accessJwt: { teamDomain: 'team.example', audience: 'aud-1' } }, fetchImpl, clock: () => now });
  const token = makeJwt({ claims: { aud: 'aud-1', iss: 'https://team.example', exp: now / 1000 + 60 }, key: privateKey });
  assert.equal(await access.decide({ headers: {} }), 'tailnet');
  assert.equal(await access.decide({ headers: { 'cf-ray': 'x', 'cf-access-jwt-assertion': token } }), 'remote');
  assert.equal(await access.decide({ headers: { 'cf-connecting-ip': '1.2.3.4', 'cf-access-jwt-assertion': token } }), 'remote');
  assert.equal(fetched, 1, 'signing keys cached');
  await assert.rejects(access.decide({ headers: { 'cf-ray': 'x' } }), AccessDenied);
});

test('remote access: tailnet-only actions -> 403; summary marks them locked with the reason; ETag differs', async (t) => {
  const d = await startDashboard(t, 'bad-night', { access: { decide: async () => 'remote' } });
  const s = await getJson(d.base, '/api/summary');
  assert.equal(s.json.access, 'remote');
  assert.ok(s.json.apps.every((a) => a.locks.update === 'Only available on the tailnet'));
  assert.equal(s.json.gate.canRerun, false);
  assert.equal(s.json.gate.rerunLock, 'Only available on the tailnet');
  assert.match(s.headers.get('etag'), /-remote"$/);
  const rerun = await postJson(d.base, '/api/gate/rerun', {});
  assert.equal(rerun.status, 403);
  assert.equal(rerun.json.error, 'Only available on the tailnet');
  assert.equal(fs.existsSync(d.host.path('run/bootgate/rerun')), false);
  const upd = await postJson(d.base, '/api/apps/sonarr/update', {});
  assert.equal(upd.status, 403);
  const denied = await startDashboard(t, 'clean', {
    access: {
      decide: async () => {
        throw new AccessDenied('no token');
      },
    },
  });
  assert.equal((await getJson(denied.base, '/api/summary')).status, 403);
  assert.equal((await getJson(denied.base, '/healthz')).status, 403);
});

test('forAccess leaves the tailnet view untouched', () => {
  const s = { access: 'tailnet', apps: [{ locks: { update: null } }], gate: { canRerun: true, rerunLock: null } };
  assert.equal(forAccess(s, 'tailnet'), s);
});
