import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { appStatus, buildApps, imageTag, updateAvailable, HELD_REASON } from '../src/collectors/apps.js';
import { classifyAlerts } from '../src/collectors/alerts.js';
import { collectBackups } from '../src/collectors/backups.js';
import { failuresByMonth, groupHistory, incidentStart, nextRetry, normBootId } from '../src/collectors/gate.js';
import { isMounted } from '../src/collectors/drive.js';
import { parseLoadavg, parseMeminfo, statfsTimed, statfsUsage } from '../src/collectors/host.js';
import { createMedia, parseSabTimeleft, readArrKey, readSabKey } from '../src/collectors/media.js';
import { buildConfig, loadConfig } from '../src/config.js';
import { DEFAULT_APPS } from '../src/catalog.js';
import { buildIssues } from '../src/issues.js';
import { createSettings } from '../src/settings.js';
import { computeBoxUptime, createUptimeStore, zonedMidnight } from '../src/uptime.js';
import { readJsonlTail, writeFileAtomic } from '../src/util/fsx.js';
import { daysBetween, isIsoDate, lastMonths, localDate, monotonicNowUs, parseTimespanUs } from '../src/util/time.js';
import { tempDir } from './helpers/push-fixtures.js';
import { startDashboard } from './helpers/server.js';

// ---- time / fs utilities ----------------------------------------------------------------------------

test('systemd timespans', () => {
  assert.equal(parseTimespanUs('0'), 0);
  assert.equal(parseTimespanUs('1500000'), 1500000);
  assert.equal(parseTimespanUs('4min 59.500000s'), 299.5e6);
  assert.equal(parseTimespanUs('2d 3h 4min 5s'), (2 * 86400 + 3 * 3600 + 4 * 60 + 5) * 1e6);
  assert.equal(parseTimespanUs('1w 1month'), 7 * 86400e6 + 2629800e6);
  assert.equal(parseTimespanUs('250ms 7us'), 250007);
  for (const v of ['', 'infinity', 'n/a', null, undefined, 'soon']) assert.equal(parseTimespanUs(v), null);
});

test('calendar helpers', () => {
  assert.equal(localDate(Date.parse('2026-11-09T03:00:00Z'), 'America/New_York'), '2026-11-08');
  assert.equal(daysBetween('2026-10-24', '2026-11-16'), 23);
  assert.equal(daysBetween('2026-03-07', '2026-03-09'), 2, 'DST does not skew whole days');
  assert.ok(isIsoDate('2028-02-29') && !isIsoDate('2026-02-29') && !isIsoDate('2026-1-01'));
  assert.deepEqual(lastMonths('2026-02', 3), ['2025-12', '2026-01', '2026-02']);
});

test('readJsonlTail reads only the end of a big file and skips torn lines', (t) => {
  const dir = tempDir(t);
  const file = path.join(dir, 'h.jsonl');
  const lines = Array.from({ length: 5000 }, (_, i) => JSON.stringify({ i, pad: 'x'.repeat(100) }));
  fs.writeFileSync(file, `${lines.join('\n')}\n{"torn": tr\n["array"]\n${JSON.stringify({ i: 'last' })}\n`);
  const out = readJsonlTail(file, { maxLines: 3, maxBytes: 4096 });
  assert.deepEqual(out.map((o) => o.i), [4998, 4999, 'last']);
  assert.deepEqual(readJsonlTail(path.join(dir, 'missing.jsonl')), []);
  writeFileAtomic(path.join(dir, 'a.json'), '{}', { mode: 0o600 });
  assert.equal(fs.statSync(path.join(dir, 'a.json')).mode & 0o777, 0o600);
  assert.deepEqual(fs.readdirSync(dir).sort(), ['a.json', 'h.jsonl']);
});

// ---- gate -------------------------------------------------------------------------------------------

const E = (ts, bootId, event) => ({ ts, bootId, event, detail: '', waitSeconds: 0 });

test('gate: boot ids normalise; history groups by boot newest first; incident start; failures per month', () => {
  assert.equal(normBootId('4E1C0000-0000-0000-0000-000000000001'), '4e1c0000000000000000000000000001');
  const ev = [
    E('2026-09-02T01:00:00.000Z', 'aaa1', 'fail'),
    E('2026-09-02T01:05:00.000Z', 'aaa1', 'retry-pass'),
    E('2026-10-01T06:00:00.000Z', 'bbb2', 'pass'),
    E('2026-10-20T03:00:00.000Z', 'bbb2', 'media-lost'),
    E('2026-10-20T03:10:00.000Z', 'ccc3', 'fail'),
    E('2026-10-20T03:11:00.000Z', 'ccc3', 'powercycle'),
    E('2026-10-20T03:15:00.000Z', 'ddd4', 'fail'),
    E('2026-10-20T03:16:00.000Z', 'ddd4', 'fail'),
  ];
  const groups = groupHistory(ev, 'ddd4');
  assert.deepEqual(groups.map((g) => [g.boot, g.current, g.events.length]), [['ddd4', true, 2], ['ccc3', false, 2], ['bbb2', false, 2], ['aaa1', false, 2]]);
  assert.equal(groups[1].startedAt, '2026-10-20T03:10:00.000Z');
  assert.equal(incidentStart(ev).event, 'media-lost');
  assert.equal(incidentStart(ev.slice(0, 3)), null, 'nothing after the last pass');
  assert.deepEqual(failuresByMonth(ev, ['2026-09', '2026-10'], 'UTC'), [
    { month: '2026-09', count: 1 },
    { month: '2026-10', count: 3 }, // media-lost + two boots with fails
  ]);
  assert.equal(groupHistory(ev, '', 2).length, 2);
  assert.ok(groupHistory(ev, '').every((g) => g.current === false));
});

test('gate: next retry from the realtime property, or the monotonic one, or nothing', async () => {
  const now = Date.parse('2026-10-24T14:08:00Z');
  const realtime = async () => ({ code: 0, stdout: 'NextElapseUSecRealtime=@1792850000\nNextElapseUSecMonotonic=0\n' });
  assert.equal(await nextRetry({ run: realtime, unit: 'x', now }), new Date(1792850000 * 1000).toISOString());
  const inUs = monotonicNowUs() + 90e6; // 90 s from now
  const monotonic = async () => ({ code: 0, stdout: `NextElapseUSecRealtime=\nNextElapseUSecMonotonic=${Math.floor(inUs / 60e6)}min ${((inUs % 60e6) / 1e6).toFixed(6)}s\n` });
  const at = Date.parse(await nextRetry({ run: monotonic, unit: 'x', now }));
  assert.ok(Math.abs(at - (now + 90_000)) < 2000, `~90 s ahead, got ${(at - now) / 1000}`);
  assert.equal(await nextRetry({ run: async () => ({ code: 1, stdout: '' }), unit: 'x', now }), null);
  assert.equal(await nextRetry({ run: async () => ({ code: 0, stdout: 'NextElapseUSecMonotonic=infinity\n' }), unit: 'x', now }), null);
  assert.equal(await nextRetry({ run: null, unit: 'x', now }), null);
});

test('gate: missing or garbage status/attempts/history are handled', async (t) => {
  const d = await startDashboard(t, 'normal', { start: false });
  d.host.write('var/lib/bootgate/status', 'exploded\n');
  d.host.write('var/lib/bootgate/attempts', 'lots\n');
  d.host.write('var/lib/bootgate/history.jsonl', '{"ts":"bad","event":"pass"}\n{"ts":"2026-10-24T10:00:00Z","event":"teleport"}\nnot json\n');
  await d.dash.start({ background: false });
  const g = d.dash.summary.current().summary.gate;
  assert.equal(g.status, 'unknown');
  assert.equal(g.attempts, 0);
  assert.deepEqual(g.history, []);
  assert.equal(g.line, 'Boot gate status is unknown (no status file).');
  assert.ok(d.dash.summary.current().summary.issues.some((i) => i.text === 'Boot gate status is unknown.'));
});

// ---- host / drive ------------------------------------------------------------------------------------

test('host parsing: loadavg, meminfo (MemAvailable or a fallback), statfs usage like df', async () => {
  assert.deepEqual(parseLoadavg('0.42 0.38 0.35 2/345 6789'), [0.42, 0.38, 0.35]);
  assert.equal(parseLoadavg('garbage'), null);
  assert.deepEqual(parseMeminfo('MemTotal: 1000 kB\nMemAvailable: 400 kB\n'), { total: 1024000, used: 614400 });
  assert.deepEqual(parseMeminfo('MemTotal: 1000 kB\nMemFree: 100 kB\nBuffers: 100 kB\nCached: 200 kB\n'), { total: 1024000, used: 614400 });
  assert.equal(parseMeminfo(''), null);
  // df: Use% = used / (used + avail); reserved blocks count as neither
  assert.deepEqual(statfsUsage({ bsize: 1, blocks: 100, bfree: 30, bavail: 20 }), { total: 100, used: 70, free: 20, usedPct: 77.8 });
  await assert.rejects(statfsTimed('/x', { timeoutMs: 20, statfs: () => new Promise(() => {}) }), { code: 'ETIMEDOUT' });
});

test('mountinfo: exact mount point match, octal escapes, no prefix matches', () => {
  const mi = '120 29 8:17 / /media rw - ext4 /dev/sdb1 rw\n130 29 8:33 / /mnt/my\\040drive rw - ext4 /dev/sdc1 rw\n';
  assert.equal(isMounted(mi, '/media'), true);
  assert.equal(isMounted(mi, '/mnt/my drive'), true);
  assert.equal(isMounted('120 29 8:17 / /media2 rw - ext4 /dev/sdb1 rw\n', '/media'), false);
  assert.equal(isMounted('', '/media'), false);
});

test('drive: a hung statfs on /media never hangs the summary', async (t) => {
  const { makeStatfs } = await import('./helpers/host.js');
  const d = await startDashboard(t, 'normal', { statfs: makeStatfs({ mediaHang: true }) });
  const s = d.dash.summary.current().summary;
  assert.equal(s.drive.mounted, true);
  assert.equal(s.drive.stale, true);
  assert.equal(s.drive.error, 'ETIMEDOUT');
});

// ---- apps ---------------------------------------------------------------------------------------------

test('image tags and update detection', () => {
  assert.equal(imageTag('lscr.io/linuxserver/sonarr:4.0.17.2952-ls312@sha256:abc'), '4.0.17.2952-ls312');
  assert.equal(imageTag('localhost:5000/app'), 'latest');
  assert.equal(imageTag('node:22-alpine'), '22-alpine');
  assert.equal(imageTag(null), null);
  const a = `x/y:1@sha256:${'a'.repeat(64)}`;
  assert.equal(updateAvailable(a, a), false);
  assert.equal(updateAvailable(a, `x/y:1@sha256:${'b'.repeat(64)}`), true, 'same tag, new digest');
  assert.equal(updateAvailable('x/y:1', 'x/y:2'), true);
  assert.equal(updateAvailable(a, null), false);
});

test('app status matrix', () => {
  const ps = (state, health = null) => ({ state, health });
  const cases = [
    [{ ps: ps('running', 'healthy'), dockerState: 'active', gateStatus: 'ok' }, 'healthy'],
    [{ ps: ps('running', null), dockerState: 'active', gateStatus: 'ok' }, 'healthy'],
    [{ ps: ps('running', 'unhealthy'), dockerState: 'active', gateStatus: 'ok' }, 'unhealthy'],
    [{ ps: ps('running', 'starting'), dockerState: 'active', gateStatus: 'ok' }, 'starting'],
    [{ ps: ps('exited'), dockerState: 'active', gateStatus: 'ok' }, 'down'],
    [{ ps: ps('exited'), dockerState: 'active', gateStatus: 'ok', stoppedOnPurpose: true }, 'stopped'],
    [{ ps: ps('restarting'), dockerState: 'active', gateStatus: 'ok' }, 'down'],
    [{ ps: ps('paused'), dockerState: 'active', gateStatus: 'ok' }, 'stopped'],
    [{ ps: ps('exited'), dockerState: 'active', gateStatus: 'media-lost', mediaGated: true }, 'held'],
    [{ ps: ps('exited'), dockerState: 'active', gateStatus: 'media-lost', mediaGated: false }, 'down'],
    [{ ps: null, dockerState: 'inactive', gateStatus: 'failed' }, 'held'],
    [{ ps: null, dockerState: 'inactive', gateStatus: 'ok' }, 'stopped'],
    [{ ps: null, dockerState: 'failed', gateStatus: 'ok' }, 'stopped'],
    [{ ps: null, dockerState: 'active', gateStatus: 'ok' }, 'down'],
    [{ ps: ps('running', 'healthy'), dockerState: 'active', gateStatus: 'ok', action: { kind: 'update' } }, 'updating'],
    [{ ps: ps('exited'), dockerState: 'active', gateStatus: 'ok', action: { kind: 'start' } }, 'starting'],
  ];
  for (const [input, expected] of cases) assert.equal(appStatus(input), expected, JSON.stringify(input));
});

test('buildApps: unknown containers from ps are listed; the catalog fills in while Docker is held', () => {
  const cfg = { apps: DEFAULT_APPS, pinnedImages: { sonarr: 'x/sonarr:2' } };
  const held = buildApps({ cfg, psList: null, dockerState: 'inactive', gateStatus: 'failed', stopped: new Set(), actions: new Map() });
  assert.equal(held.length, 8);
  assert.equal(held.find((a) => a.name === 'sonarr').pinnedTag, '2');
  assert.equal(held.find((a) => a.name === 'sonarr').locks.logs, HELD_REASON);
  const declared = [
    { name: 'sonarr', stack: 'media', pinned: 'x/sonarr:2', local_build: false },
    { name: 'wol-relay', stack: 'utils', pinned: 'homelab/wol-relay:2026.07', local_build: true },
    { name: 'newapp', stack: 'media', pinned: 'x/newapp:1', local_build: false },
  ];
  const ps = [
    { name: 'wol-relay', state: 'running', health: null, image: 'homelab/wol-relay:2026.06', pinned: 'homelab/wol-relay:2026.07' },
    { name: 'newapp', state: 'running', health: 'healthy', image: 'x/newapp:1', pinned: 'x/newapp:1' },
  ];
  const apps = buildApps({ cfg, psList: ps, declared, dockerState: 'active', gateStatus: 'ok', stopped: new Set(), actions: new Map() });
  assert.deepEqual(apps.map((a) => a.name), ['newapp', 'sonarr', 'wol-relay'], 'the declared list, not the catalog');
  assert.equal(apps.find((a) => a.name === 'newapp').label, 'newapp');
  assert.equal(apps.find((a) => a.name === 'newapp').mediaGated, true, 'a media-stack app is gated');
  assert.equal(apps.find((a) => a.name === 'sonarr').status, 'down', 'declared but not running');
  const wol = apps.find((a) => a.name === 'wol-relay');
  assert.equal(wol.localBuild, true);
  assert.equal(wol.updateAvailable, false, 'a host build is never updated from the dashboard');
  assert.equal(wol.locks.update, 'Built on the host. A deploy updates it.');
  // Docker held right after a reboot: the last-known declared list still names the real apps
  const afterReboot = buildApps({ cfg, psList: null, declared, dockerState: 'inactive', gateStatus: 'failed', stopped: new Set(), actions: new Map() });
  assert.deepEqual(afterReboot.map((a) => [a.name, a.status]), [['newapp', 'held'], ['sonarr', 'held'], ['wol-relay', 'held']]);
  // a deployment without cloudflared (rehearsal) never shows cloudflared as down
  const ps2 = [{ name: 'sonarr', state: 'running', health: 'healthy' }];
  assert.ok(!buildApps({ cfg, psList: ps2, declared: [{ name: 'sonarr', stack: 'media' }], dockerState: 'active', gateStatus: 'ok', stopped: new Set(), actions: new Map() }).some((a) => a.name === 'cloudflared'));
});

// ---- alerts / backups / issues --------------------------------------------------------------------------

test('alerts: lost -> back resolves earlier ones of the same tag; untagged/test/reminder are info; old ones age out', () => {
  const now = Date.parse('2026-10-24T14:00:00Z');
  const raw = [
    { ts: '2026-10-01T00:00:00Z', title: 'Backup failed', tag: 'backup' },
    { ts: '2026-10-20T03:12:00Z', title: 'Media drive lost', tag: 'bootgate' },
    { ts: '2026-10-20T03:21:00Z', title: 'Media drive back', tag: 'bootgate' },
    { ts: '2026-10-24T02:31:00Z', title: 'Media drive lost', tag: 'bootgate' },
    { ts: '2026-10-24T05:00:00Z', title: 'jellyfin unhealthy', tag: 'alertfloor-jellyfin' },
    { ts: '2026-10-24T05:10:00Z', title: 'jellyfin recovered', tag: 'alertfloor-jellyfin' },
    { ts: '2026-10-24T06:00:00Z', title: 'mediaserver back online', tag: 'heartbeat' },
    { ts: '2026-10-24T07:00:00Z', title: 'Test', tag: 'test' },
    { ts: '2026-10-24T08:00:00Z', title: 'Hello', tag: null },
    { ts: 'nonsense', title: 'x' },
    { ts: '2026-10-24T09:00:00Z' },
  ];
  const out = classifyAlerts(raw, now);
  assert.deepEqual(out.map((a) => [a.title, a.state]), [
    ['Hello', 'info'],
    ['Test', 'info'],
    ['mediaserver back online', 'resolved'],
    ['jellyfin recovered', 'resolved'],
    ['jellyfin unhealthy', 'resolved'],
    ['Media drive lost', 'active'],
    ['Media drive back', 'resolved'],
    ['Media drive lost', 'resolved'],
    ['Backup failed', 'info'],
  ]);
  assert.deepEqual(Object.keys(out[0]), ['ts', 'title', 'body', 'tag', 'delivered', 'failed', 'state']);
});

test('backups: ok / stale / failed / none / unknown', (t) => {
  const dir = tempDir(t);
  const file = path.join(dir, 'status.json');
  const cfg = { backupStatusFile: file };
  const settings = { thresholds: { backupMaxAgeH: 36 } };
  const now = Date.parse('2026-10-24T14:00:00Z');
  const run = (v) => {
    if (v === undefined) fs.rmSync(file, { force: true });
    else fs.writeFileSync(file, typeof v === 'string' ? v : JSON.stringify(v));
    return collectBackups({ cfg, settings, now }).status;
  };
  assert.equal(run(undefined), 'none');
  assert.equal(run({ last_ok: '2026-10-24T03:00:00Z', ok: true }), 'ok');
  assert.equal(run({ last_ok: '2026-10-22T03:00:00Z', ok: true }), 'stale');
  assert.equal(run({ last_ok: '2026-10-24T03:00:00Z', ok: false }), 'failed');
  assert.equal(run({ last_ok: null, ok: true }), 'none');
  assert.equal(run('{garbage'), 'unknown');
});

test('issues: backups, drive threshold, docker failed, unhealthy app, Usenet login', () => {
  const base = {
    gate: { status: 'ok' },
    helper: { reachable: true },
    docker: { state: 'active' },
    apps: [{ name: 'sonarr', status: 'unhealthy' }],
    drive: { warn: true, usedPct: 91.4, mounted: true },
    media: { checks: [{ key: 'usenet-login', status: 'fail' }], sabnzbd: { state: 'ok' } },
    backups: { status: 'stale', lastOk: '2026-10-22T03:00:00Z' },
    push: { storeError: false },
    subscriptions: [{ name: 'Indexer A', daysLeft: -1 }],
  };
  const now = Date.parse('2026-10-24T14:00:00Z');
  assert.deepEqual(buildIssues(base, { now, timeZone: 'UTC' }).map((i) => [i.severity, i.text]), [
    ['warn', 'Sonarr is unhealthy.'],
    ['warn', 'Media drive is 91% full.'],
    ['warn', 'Usenet login is failing.'],
    ['warn', 'Last good backup was 2 days ago.'],
    ['warn', 'Indexer A renewal date has passed.'],
  ]);
  const failed = buildIssues({ ...base, apps: [], backups: { status: 'failed' }, docker: { state: 'failed' } }, { now });
  assert.deepEqual(failed.slice(0, 2).map((i) => i.text), ['Docker failed to start.', 'The last nightly backup failed.']);
});

// ---- media --------------------------------------------------------------------------------------------

test('API key readers: config.xml <ApiKey>, sabnzbd.ini [misc] api_key only', () => {
  const k32 = 'f'.repeat(30) + '01';
  const k16 = 'e'.repeat(14) + '01';
  assert.equal(readArrKey(`<Config><ApiKey> ${k32} </ApiKey></Config>`), k32);
  assert.equal(readArrKey('<Config></Config>'), null);
  assert.equal(readArrKey('<ApiKey>bad key!</ApiKey>'), null);
  assert.equal(readSabKey(`[servers]\napi_key = wrongwrongwrong\n[misc]\napi_key = ${k16}\n`), k16);
  assert.equal(readSabKey(`[misc]\nnzb_key = ${k16}\n`), null);
  assert.equal(parseSabTimeleft('0:02:10'), 130);
  assert.equal(parseSabTimeleft('1:00:00:01'), 86401);
  assert.equal(parseSabTimeleft('2:05'), 125);
  assert.equal(parseSabTimeleft('unknown'), null);
});

test('media tiles: unreadable key file, HTTP errors, non-JSON answers, failing indexer; each isolated', async (t) => {
  const { makeFetch, buildHost } = await import('./helpers/host.js');
  const host = buildHost(t, 'normal');
  const sonarrKey = path.join(host.cfg.dataDir, 'sonarr/config.xml');
  fs.chmodSync(sonarrKey, 0o000);
  const base = makeFetch({ httpError: { radarr: 503 }, indexerFailing: true });
  const fetchImpl = async (url, init) => (new URL(url).port === '5055' ? new Response('<html>oops</html>', { status: 200 }) : base(url, init));
  const settings = createSettings({ stateDir: host.stateDir });
  const media = createMedia({ cfg: host.cfg, settings, fetchImpl, timeZone: 'UTC', clock: () => Date.parse('2026-10-24T14:08:00Z') });
  const statuses = Object.fromEntries(['jellyfin', 'sabnzbd', 'jellyseerr', 'prowlarr', 'sonarr', 'radarr'].map((a) => [a, 'healthy']));
  let m;
  try {
    m = await media.collect(statuses, { driveMounted: true });
  } finally {
    fs.chmodSync(sonarrKey, 0o600);
  }
  if (process.getuid?.() !== 0) {
    assert.equal(m.sonarr.state, 'unconfigured');
    assert.equal(m.sonarr.message, "Can't read Sonarr's API key from its config file.");
  }
  assert.equal(m.radarr.state, 'error');
  assert.match(m.radarr.detail, /^HTTP 503 at 14:08\./);
  assert.equal(m.jellyseerr.state, 'error');
  assert.match(m.jellyseerr.detail, /not JSON/);
  assert.equal(m.jellyfin.state, 'ok');
  assert.deepEqual(m.prowlarr.indexers.find((i) => i.name === 'Indexer C'), { name: 'Indexer C', status: 'fail', detail: 'Failing since 11:02' });
  assert.equal(m.checks.find((c) => c.key === 'radarr-health').status, 'fail');
  // a failure streak keeps its start time (stable output while it lasts)
  const later = createMedia({ cfg: host.cfg, settings, fetchImpl, timeZone: 'UTC', clock: (() => { let n = Date.parse('2026-10-24T14:08:00Z'); return () => (n += 60_000); })() });
  const one = await later.collect(statuses, { driveMounted: true });
  const two = await later.collect(statuses, { driveMounted: true });
  assert.equal(one.radarr.detail, two.radarr.detail);
  assert.equal(one.radarr.since, two.radarr.since);
});

test('a Prowlarr indexer failure becomes an overview issue', async (t) => {
  const d = await startDashboard(t, 'normal', { media: { indexerFailing: true } });
  const s = d.dash.summary.current().summary;
  assert.ok(s.issues.some((i) => i.text === 'Indexer C: failing since 11:02.' && i.target === 'media'));
});

// ---- config -------------------------------------------------------------------------------------------

test('config: snake_case file over defaults, env overrides, unknown keys reported, bad file refused', async (t) => {
  const { config, unknown } = buildConfig(
    { listen_port: 9000, remote_enabled: false, access_jwt: { team_domain: 'x.example' }, intervals: { core: 5 }, integrations: { jellyfin: { base_url: 'http://127.0.0.1:1/watch' } }, typo_key: 1 },
    { PORT: '9100', PUSH_DIR: '/tmp/p', LOG_LEVEL: 'debug' },
  );
  assert.equal(config.listenPort, 9100, 'env wins');
  assert.equal(config.pushDir, '/tmp/p');
  assert.equal(config.logLevel, 'debug');
  assert.equal(config.accessJwt.teamDomain, 'x.example');
  assert.equal(config.accessJwt.audience, '');
  assert.equal(config.intervals.core, 5);
  assert.equal(config.intervals.media, 30);
  assert.equal(config.integrations.jellyfin.baseUrl, 'http://127.0.0.1:1/watch');
  assert.equal(config.integrations.sonarr.keyFile, 'sonarr/config.xml');
  assert.equal(config.listenHost, '127.0.0.1');
  assert.equal(config.remoteEnabled, false);
  assert.equal(config.version, '2.0.0');
  assert.deepEqual(unknown, ['typo_key']);
  // the env-file form (DASHBOARD_*) works too and wins over the file
  const fromEnv = buildConfig(
    { state_dir: '/a', gate_retry_timer: 'x.timer' },
    { DASHBOARD_DATA_DIR: '/b', DASHBOARD_REMOTE_ENABLED: 'true', DASHBOARD_BOOTGATE_RETRY_MINUTES: '7', DASHBOARD_DOCKERCTL_SOCKET: '/run/h.sock', DASHBOARD_DISPLAY_NAME: 'box' },
  ).config;
  assert.deepEqual([fromEnv.stateDir, fromEnv.remoteEnabled, fromEnv.gateRetryMinutes, fromEnv.helperSocket, fromEnv.displayName], ['/b', true, 7, '/run/h.sock', 'box']);
  assert.equal(fromEnv.gateRetryService, 'x.service', 'derived from the timer');
  assert.equal(buildConfig({}, {}).config.displayName, (await import('node:os')).hostname().split('.')[0], 'the box names itself');
  assert.throws(() => buildConfig({}, { DASHBOARD_REMOTE_ENABLED: 'maybe' }), /true or false/);
  const dir = tempDir(t);
  const f = path.join(dir, 'c.json');
  fs.writeFileSync(f, '[1]');
  assert.throws(() => loadConfig({ DASHBOARD_CONFIG: f }), /JSON object/);
  fs.writeFileSync(f, '{bad');
  assert.throws(() => loadConfig({ DASHBOARD_CONFIG: f }), /cannot load/);
  assert.equal(loadConfig({ DASHBOARD_CONFIG: path.join(dir, 'missing.json') }).missing, true);
});

// ---- uptime + sampler -------------------------------------------------------------------------------

test('uptime store: per-app per-day rollups; stopped/unknown not counted; monthly percentages', (t) => {
  const dir = tempDir(t);
  const store = createUptimeStore({ stateDir: dir, timeZone: 'UTC' });
  const t0 = Date.parse('2026-10-24T10:00:00Z');
  for (let i = 0; i < 99; i++) store.record(t0 + i * 60_000, { sonarr: 'healthy', radarr: 'held', wol: 'stopped' });
  store.record(t0 + 99 * 60_000, { sonarr: 'down', radarr: 'held', wol: 'unknown' });
  const doc = JSON.parse(fs.readFileSync(path.join(dir, 'uptime/2026-10.json'), 'utf8'));
  assert.deepEqual(doc.days['2026-10-24'], { sonarr: [99, 100], radarr: [0, 100] });
  const m = createUptimeStore({ stateDir: dir, timeZone: 'UTC' }).monthly(['sonarr', 'radarr', 'wol'], ['2026-09', '2026-10']);
  assert.deepEqual(m, [
    { name: 'sonarr', months: [{ month: '2026-09', pct: null }, { month: '2026-10', pct: 99 }] },
    { name: 'radarr', months: [{ month: '2026-09', pct: null }, { month: '2026-10', pct: 0 }] },
    { name: 'wol', months: [{ month: '2026-09', pct: null }, { month: '2026-10', pct: null }] },
  ]);
});

test('box uptime from boots: per-day percentages, gaps as downs, unknown before the first boot', () => {
  const now = Date.parse('2026-10-24T12:00:00Z');
  const boots = [
    { first: '2026-10-23T00:00:00Z', last: '2026-10-23T11:00:00Z' },
    { first: '2026-10-23T12:00:00Z', last: '2026-10-24T11:00:00Z' },
  ];
  const r = computeBoxUptime(boots, { now, timeZone: 'UTC', days: 3 });
  assert.deepEqual(r.days, [
    { date: '2026-10-22', pct: null },
    { date: '2026-10-23', pct: 95.83 },
    { date: '2026-10-24', pct: 100 },
  ]);
  assert.deepEqual(r.downs, [{ start: '2026-10-23T11:00:00.000Z', end: '2026-10-23T12:00:00.000Z', seconds: 3600, reason: 'Reboot or power loss' }]);
  assert.equal(r.pct30d, Math.round((35 / 36) * 10000) / 100);
  assert.deepEqual(computeBoxUptime([], { now, timeZone: 'UTC', days: 2 }).days.map((x) => x.pct), [null, null]);
});

test('zoned midnight across DST', () => {
  assert.equal(new Date(zonedMidnight('2026-03-08', 'America/New_York')).toISOString(), '2026-03-08T05:00:00.000Z');
  assert.equal(new Date(zonedMidnight('2026-03-09', 'America/New_York')).toISOString(), '2026-03-09T04:00:00.000Z');
  assert.equal(new Date(zonedMidnight('2026-11-01', 'America/New_York')).toISOString(), '2026-11-01T04:00:00.000Z');
  assert.equal(new Date(zonedMidnight('2026-10-24', 'UTC')).toISOString(), '2026-10-24T00:00:00.000Z');
});

test('sampler: records every app each tick; one drive sample per day, only while mounted', async (t) => {
  const d = await startDashboard(t, 'clean');
  const r1 = await d.dash.sampler.tick();
  const r2 = await d.dash.sampler.tick();
  assert.deepEqual(r1, { recorded: true, sampled: true });
  assert.deepEqual(r2, { recorded: true, sampled: false }, 'second sample of the day is skipped');
  const samples = fs.readFileSync(path.join(d.host.stateDir, 'samples.jsonl'), 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  assert.equal(samples.length, 1);
  assert.equal(samples[0].date, '2026-10-24');
  assert.equal(samples[0].used, 16e9);
  const month = JSON.parse(fs.readFileSync(path.join(d.host.stateDir, 'uptime/2026-10.json'), 'utf8'));
  assert.deepEqual(month.days['2026-10-24'].sonarr, [2, 2]);
  const bad = await startDashboard(t, 'bad-night');
  assert.deepEqual(await bad.dash.sampler.tick(), { recorded: true, sampled: false }, 'no sample while the drive is offline');
  const held = JSON.parse(fs.readFileSync(path.join(bad.host.stateDir, 'uptime/2026-10.json'), 'utf8'));
  assert.deepEqual(held.days['2026-10-24'].sonarr, [0, 1], 'held by the gate counts as down');
});

test('the real command runner: exit codes, output, timeouts, never throws', async () => {
  const { createRunner, parseKeyValues } = await import('../src/util/run.js');
  const run = createRunner({ timeoutMs: 300 });
  const ok = await run(process.execPath, ['-e', 'process.stdout.write("ActiveState=inactive\\nX=1=2\\n")']);
  assert.equal(ok.code, 0);
  assert.deepEqual(parseKeyValues(ok.stdout), { ActiveState: 'inactive', X: '1=2' });
  assert.equal((await run(process.execPath, ['-e', 'process.exit(3)'])).code, 3);
  const slow = await run(process.execPath, ['-e', 'setTimeout(() => {}, 5000)']);
  assert.notEqual(slow.code, 0);
  const missing = await run('/nonexistent/systemctl', ['show']);
  assert.notEqual(missing.code, 0);
  assert.equal(missing.error, 'ENOENT');
});

// ---- time zone / runbook links (summary.server.timeZone, summary.links) -----------------------------

test('time zone: configured, then /etc/timezone, then timedatectl, then the process zone; invalid values skipped', async (t) => {
  const { resolveTimeZone, isTimeZone } = await import('../src/util/tz.js');
  const dir = tempDir(t);
  const file = path.join(dir, 'timezone');
  fs.writeFileSync(file, 'Europe/Berlin\n');
  const never = () => assert.fail('timedatectl must not run when an earlier source answers');
  assert.deepEqual(resolveTimeZone({ configured: 'America/New_York', file, timedatectl: never }), { timeZone: 'America/New_York', source: 'config' });
  assert.deepEqual(resolveTimeZone({ configured: 'Not/AZone', file, timedatectl: never }), { timeZone: 'Europe/Berlin', source: 'file' });
  assert.deepEqual(resolveTimeZone({ configured: undefined, file: path.join(dir, 'missing'), timedatectl: () => 'Asia/Tokyo' }), { timeZone: 'Asia/Tokyo', source: 'timedatectl' });
  fs.writeFileSync(file, '../../etc/passwd\n');
  const fallback = resolveTimeZone({ configured: '', file, timedatectl: () => null });
  assert.equal(fallback.source, 'process');
  assert.ok(isTimeZone(fallback.timeZone));
  for (const bad of ['', 'UTC; rm -rf /', '../x', 42, null, 'A'.repeat(70)]) assert.equal(isTimeZone(bad), false, String(bad));
  assert.equal(isTimeZone('UTC'), true);
});

test('runbook links: https base + safe relative paths; anything else dropped with a warning', async () => {
  const { runbookLinks } = await import('../src/config.js');
  const base = 'https://git.example.org/repo/blob/main/docs/runbooks';
  const ok = runbookLinks(base, [
    { title: "Media drive won't mount", path: 'media-drive-failure.md' },
    { title: 'Restore', path: 'restore/from-scratch.md#steps' },
  ]);
  assert.deepEqual(ok, {
    links: [
      { title: "Media drive won't mount", url: `${base}/media-drive-failure.md` },
      { title: 'Restore', url: `${base}/restore/from-scratch.md#steps` },
    ],
    warnings: [],
  });
  const bad = ['../secrets.md', 'a/../b.md', './x.md', '/etc/passwd', '//evil.example/x', 'javascript:alert(1)', 'https://evil.example/x', 'a\\b.md', 'a b.md', '', 'x/'];
  const r = runbookLinks(base, bad.map((p) => ({ title: 'x', path: p })));
  assert.deepEqual(r.links, []);
  assert.equal(r.warnings.length, bad.length);
  assert.equal(runbookLinks(base, [{ title: '', path: 'a.md' }, { title: 'x'.repeat(81), path: 'a.md' }]).links.length, 0);
  for (const b of ['http://git.example.org/x', 'https://user:pw@git.example.org/x', 'https://git.example.org/x?q=1', 'not a url', undefined]) {
    const res = runbookLinks(b, [{ title: 'a', path: 'a.md' }]);
    assert.deepEqual(res.links, [], String(b));
    assert.equal(res.warnings.length, 1);
  }
  assert.deepEqual(runbookLinks(undefined, undefined), { links: [], warnings: [] }, 'absent -> empty, no warning');
  assert.deepEqual(runbookLinks(base, []), { links: [], warnings: [] });
  assert.equal(runbookLinks(base, 'nope').warnings.length, 1);
  const { buildConfig } = await import('../src/config.js');
  const { config, warnings, unknown } = buildConfig({ runbook_base_url: base, runbooks: [{ title: 'A', path: 'a.md' }, { title: 'B', path: '../b.md' }] }, {});
  assert.deepEqual(config.runbookLinks, [{ title: 'A', url: `${base}/a.md` }]);
  assert.deepEqual(unknown, []);
  assert.equal(warnings.length, 1);
});

test('stoppedReason: gate (held), docker-down, intentional (stopped on purpose or paused), otherwise null', () => {
  const cfg = { apps: DEFAULT_APPS, pinnedImages: {} };
  const mk = (over) => buildApps({ cfg, psList: null, declared: null, dockerState: 'active', gateStatus: 'ok', stopped: new Set(), actions: new Map(), ...over });
  assert.ok(mk({ dockerState: 'inactive', gateStatus: 'failed' }).every((a) => a.status === 'held' && a.stoppedReason === 'gate'));
  assert.ok(mk({ dockerState: 'unknown', gateStatus: 'failed' }).every((a) => a.stoppedReason === 'gate'), 'helper down on a failed boot: still the gate');
  assert.ok(mk({ dockerState: 'inactive', gateStatus: 'ok' }).every((a) => a.status === 'stopped' && a.stoppedReason === 'docker-down'));
  assert.ok(mk({ dockerState: 'failed', gateStatus: 'ok' }).every((a) => a.stoppedReason === 'docker-down'));
  const ps = [
    { name: 'sonarr', state: 'exited', health: null },
    { name: 'radarr', state: 'exited', health: null },
    { name: 'prowlarr', state: 'paused', health: null },
    { name: 'cloudflared', state: 'running', health: 'healthy' },
    { name: 'jellyfin', state: 'running', health: 'unhealthy' },
  ];
  const by = Object.fromEntries(mk({ psList: ps, stopped: new Set(['sonarr']) }).map((a) => [a.name, [a.status, a.stoppedReason]]));
  assert.deepEqual(by.sonarr, ['stopped', 'intentional']);
  assert.deepEqual(by.radarr, ['down', null], 'crashed is an incident, not a reason');
  assert.deepEqual(by.prowlarr, ['stopped', 'intentional']);
  assert.deepEqual(by.cloudflared, ['healthy', null]);
  assert.deepEqual(by.jellyfin, ['unhealthy', null]);
  const lost = Object.fromEntries(mk({ psList: ps.map((p) => ({ ...p, state: p.name === 'cloudflared' ? 'running' : 'exited' })), gateStatus: 'media-lost' }).map((a) => [a.name, a.stoppedReason]));
  assert.equal(lost.sonarr, 'gate');
  assert.equal(lost.cloudflared, null);
});
