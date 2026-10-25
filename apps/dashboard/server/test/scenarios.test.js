// The full summary for each designed scenario, through the real HTTP API.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { getJson, startDashboard } from './helpers/server.js';
import { KEYS } from './helpers/host.js';

const summaryOf = async (d) => (await getJson(d.base, '/api/summary')).json;
const MEDIA_APPS = ['jellyfin', 'jellyseerr', 'prowlarr', 'radarr', 'sabnzbd', 'sonarr'];

function assertNoSecrets(d, text) {
  for (const key of Object.values(KEYS)) assert.ok(!text.includes(key), 'API key leaked');
  assert.ok(!text.includes(d.host.vapid.privateKey), 'VAPID private key leaked');
  for (const s of d.host.subs) {
    assert.ok(!text.includes(s.endpoint), 'push endpoint leaked');
    assert.ok(!text.includes(s.keys.auth), 'push key leaked');
  }
}

test('normal day: everything healthy, drive forecast, backups, media tiles, no issues but a renewal', async (t) => {
  const d = await startDashboard(t, 'normal');
  const r = await getJson(d.base, '/api/summary');
  const s = r.json;
  assert.equal(s.schema, 1);
  assert.equal(s.access, 'tailnet');
  assert.deepEqual(s.server, { name: 'testhost', version: '2.0.0', timeZone: 'UTC' });
  assert.deepEqual(s.links, { runbooks: [] }, 'no runbook config -> empty list');
  assert.ok(s.apps.every((a) => a.stoppedReason === null));
  assert.equal(s.gate.status, 'ok');
  assert.match(s.gate.line, /^Drive found, checked and mounted at \/media \(Oct 21 06:01\)\. Apps released\.$/);
  assert.equal(s.gate.nextRetryAt, null);
  assert.equal(s.gate.canRerun, false);
  assert.deepEqual(s.gate.history.map((g) => [g.boot, g.current, g.events.length]), [['4e1c', true, 1], ['77b0', false, 3], ['5a12', false, 2]]);
  assert.deepEqual(s.gate.history[1].events.map((e) => e.event), ['retry-pass', 'powercycle', 'fail'], 'newest first');
  assert.equal(s.apps.length, 8);
  assert.ok(s.apps.every((a) => a.status === 'healthy'));
  assert.deepEqual(s.apps.map((a) => a.name), ['cloudflared', 'jellyfin', 'jellyseerr', 'prowlarr', 'radarr', 'sabnzbd', 'sonarr', 'wol-relay']);
  assert.ok(!s.apps.some((a) => a.name === 'dashboard' || a.name === 'push-core' || a.name === 'caddy'), 'CORRECTIONS §1');
  assert.deepEqual(s.apps.find((a) => a.name === 'sonarr').locks, {
    restart: null,
    stop: null,
    start: 'Already running',
    update: 'Already on the pinned version',
    logs: null,
  });
  assert.deepEqual(s.host.load, [0.42, 0.38, 0.35]);
  assert.equal(s.host.rootUsedPct, 41);
  assert.equal(s.host.rebootRequired, false);
  assert.equal(s.drive.mounted, true);
  assert.equal(s.drive.stale, false);
  assert.equal(s.drive.usedPct, 34);
  assert.deepEqual(s.drive.split, { tv: 812e9, movies: 486e9, downloads: 62e9, computedAt: '2026-10-24T03:00:00Z', stale: false });
  assert.equal(s.drive.forecast.slopeBytesPerDay, 6e9);
  assert.equal(s.drive.samples.length, 40);
  assert.equal(s.backups.status, 'ok');
  assert.equal(s.backups.archive, 'appdata-20261024T031200Z.tar', 'basename only');
  assert.equal(s.backups.lastPull, '2026-10-23T21:40:00.000Z');
  assert.equal(s.media.jellyfin.sessions[0].title, 'Arrival (2016)');
  assert.equal(s.media.jellyfin.sessions.length, 1, 'idle sessions are not "now playing"');
  assert.equal(s.media.sabnzbd.items[0].etaSeconds, 130);
  assert.deepEqual(s.media.jellyseerr.requests.map((x) => x.title), ['Dune: Part Two (2024)', 'Shōgun · Season 1']);
  assert.deepEqual(s.media.prowlarr.indexers.map((x) => x.status), ['ok', 'warn', 'ok']);
  assert.ok(s.media.checks.every((c) => c.status === 'ok'));
  assert.equal(s.push.devices.length, 3);
  assert.deepEqual(s.push.alerts.map((a) => a.state), ['resolved', 'resolved', 'info']);
  assert.deepEqual(s.issues, [{ severity: 'warn', text: 'Usenet provider renews in 5 days.', target: 'settings' }]);
  assert.ok(Number.isFinite(s.uptime.box.pct30d));
  assertNoSecrets(d, r.text);
  assertNoSecrets(d, d.text());
  // Docker ops happened only while active
  assert.ok(d.helper.dockerCalls().every((c) => c.dockerStateAtCall === 'active'));
});

test('bad night: gate failed after a power-cycle, Docker held, every app held, drive last-known, no Docker ops at all', async (t) => {
  const d = await startDashboard(t, 'bad-night');
  const r = await getJson(d.base, '/api/summary');
  const s = r.json;
  assert.equal(s.gate.status, 'failed');
  assert.equal(s.gate.line, 'Media drive missing since 02:31 after a power-cycle. 8 apps held. Retrying every 5 min.');
  assert.equal(s.gate.since, '2026-10-24T02:31:00.000Z');
  assert.equal(s.gate.powercycled, true);
  assert.equal(s.gate.nextRetryAt, new Date(Math.floor((Date.parse('2026-10-24T14:08:00Z') + 222_000) / 1000) * 1000).toISOString());
  assert.equal(s.gate.canRerun, true);
  assert.equal(s.docker.state, 'inactive');
  assert.deepEqual(new Set(s.apps.map((a) => a.status)), new Set(['held']), 'all 8 containers held (CORRECTIONS §2)');
  assert.ok(s.apps.every((a) => a.stoppedReason === 'gate'));
  for (const a of s.apps) {
    for (const k of ['restart', 'stop', 'start', 'update', 'logs']) assert.equal(a.locks[k], 'Held by the boot gate until the media drive is back.');
  }
  assert.equal(s.drive.mounted, false);
  assert.equal(s.drive.stale, true);
  assert.equal(s.drive.lastKnownAt, '2026-10-24T02:30:00.000Z');
  assert.equal(s.drive.usedPct, 33.9);
  assert.equal(s.host.rebootRequired, true);
  assert.deepEqual(s.host.rebootPackages, ['libc6', 'linux-image-7.0.0-12-generic']);
  for (const app of MEDIA_APPS) assert.equal(s.media[app].state, 'held');
  assert.deepEqual(s.media.checks.find((c) => c.key === 'library-folders'), { key: 'library-folders', label: 'Library folders present', status: 'fail', detail: 'Missing: drive offline' });
  assert.deepEqual(s.push.alerts.map((a) => [a.title, a.state]), [
    ['Media drive failed', 'active'],
    ['Media drive lost', 'active'],
    ['Media drive back', 'resolved'],
    ['Media drive lost', 'resolved'],
  ]);
  assert.equal(s.issues[0].severity, 'fail');
  assert.equal(s.issues[0].text, 'Media drive failed after a power-cycle. 8 apps held.');
  assert.equal(s.issues[0].target, 'drive');
  assert.deepEqual(s.uptime.box.downs.map((x) => [x.start, x.reason]), [
    ['2026-10-24T02:38:20.000Z', 'Power-cycle by the boot gate'],
    ['2026-10-24T02:33:00.000Z', 'Reboot or power loss'],
  ], 'newest first');
  // The rule: Docker is down, so the helper got no Docker op, the media APIs weren't called, /media wasn't statfs'd
  assert.equal(d.helper.dockerCalls().length, 0);
  assert.ok(d.helper.calls.some((c) => c.op === 'docker-state'));
  assert.equal(d.fetch.calls.length, 0);
  assert.ok(!d.statfs.calls.some((p) => p.endsWith('/media')));
  // disk-split is asked (the helper never runs du while the drive isn't verified) and the cached split shows, stale
  assert.deepEqual(s.drive.split, { tv: 812e9, movies: 486e9, downloads: 62e9, computedAt: '2026-10-24T03:00:00Z', stale: true });
  assertNoSecrets(d, r.text);
});

test('clean start: empty drive, no forecast, no backup yet, no devices, no alerts, empty media', async (t) => {
  const d = await startDashboard(t, 'clean');
  const s = await summaryOf(d);
  assert.equal(s.gate.status, 'ok');
  assert.ok(s.apps.every((a) => a.status === 'healthy'));
  assert.equal(s.drive.usedPct, 0.4);
  assert.equal(s.drive.forecast, null);
  assert.equal(s.drive.forecastReason, 'not-enough-samples');
  assert.deepEqual(s.drive.samples, []);
  assert.equal(s.backups.status, 'none');
  assert.equal(s.backups.lastPull, null);
  assert.deepEqual(s.push.devices, []);
  assert.deepEqual(s.push.alerts, []);
  assert.equal(s.push.alertsAvailable, true);
  assert.deepEqual(s.media.jellyfin.sessions, []);
  assert.deepEqual(s.media.sabnzbd.items, []);
  assert.equal(s.media.jellyseerr.pending, 0);
  assert.ok(!s.issues.some((i) => i.target === 'backups'), 'no backup yet is not an issue on a clean start');
  assert.deepEqual(s.gate.history.map((g) => g.events.length), [1]);
});

test('one app down: Radarr exited is a fail issue; SABnzbd timing out marks only its own tile', async (t) => {
  const d = await startDashboard(t, 'one-app-down');
  const s = await summaryOf(d);
  const radarr = s.apps.find((a) => a.name === 'radarr');
  assert.equal(radarr.status, 'down');
  assert.equal(radarr.stoppedReason, null, 'down is an incident, not a stop');
  assert.equal(radarr.restartCount, 3);
  assert.equal(radarr.locks.start, null);
  assert.equal(radarr.startedAt, null);
  assert.equal(s.media.radarr.state, 'down', 'a stopped app is not called');
  assert.equal(s.media.sabnzbd.state, 'error');
  assert.equal(s.media.sabnzbd.message, "SABnzbd isn't answering");
  assert.match(s.media.sabnzbd.detail, /^Timed out after 1 s at 14:08\. The container is running, so this may clear on its own\.$/, 'a 150 ms timeout reads as 1 s, never 0 s');
  for (const app of ['jellyfin', 'jellyseerr', 'prowlarr', 'sonarr']) assert.equal(s.media[app].state, 'ok', `${app} unaffected`);
  assert.deepEqual(s.media.checks.find((c) => c.key === 'download-client').status, 'fail');
  assert.deepEqual(
    s.issues.map((i) => [i.severity, i.text]),
    [
      ['fail', 'Radarr is down.'],
      ['warn', "SABnzbd's API isn't answering."],
      ['warn', 'Usenet provider renews in 5 days.'],
    ],
  );
  assert.ok(!d.fetch.calls.some((c) => c.url.startsWith('/radarr')));
});

test('docker inactive with the gate ok (live-restore): apps "unknown"/docker-down, ONE issue, still no Docker op', async (t) => {
  const d = await startDashboard(t, 'docker-inactive');
  const s = await summaryOf(d);
  assert.equal(s.docker.state, 'inactive');
  assert.ok(s.apps.every((a) => a.status === 'unknown' && a.stoppedReason === 'docker-down'), 'never "stopped": they may still be serving');
  assert.equal(s.apps[0].locks.restart, "Docker isn't running.");
  assert.deepEqual(s.issues.filter((i) => i.target === 'apps').map((i) => [i.severity, i.text]), [['fail', "Docker isn't running."]], 'one issue, not one per app');
  assert.ok(['jellyfin', 'sabnzbd', 'sonarr'].every((a) => s.media[a].state === 'unknown'), 'media tiles make no claim either');
  assert.equal(s.media.checks.find((c) => c.key === 'download-client').detail, 'Unknown');
  assert.equal(d.helper.dockerCalls().length, 0);
  assert.equal(d.fetch.calls.length, 0, 'no media API is called while its app is not running');
  // the pinned list still comes from config while Docker is down (Settings > Pinned versions)
  assert.match(s.apps.find((a) => a.name === 'sonarr').pinned, /^example\/sonarr:1\.0\.0@sha256:/);
});

test('media-lost: the 6 media apps are held, cloudflared and wol-relay keep running', async (t) => {
  const d = await startDashboard(t, 'normal', {
    helper: {
      containers: ['cloudflared', 'jellyfin', 'jellyseerr', 'prowlarr', 'radarr', 'sabnzbd', 'sonarr', 'wol-relay'].map((name) => ({
        name,
        state: ['cloudflared', 'wol-relay'].includes(name) ? 'running' : 'exited',
        health: ['cloudflared', 'wol-relay'].includes(name) ? 'healthy' : null,
        started_at: '2026-10-21T10:02:00Z',
        restart_count: 0,
        image: `example/${name}:1.0.0`,
        pinned: `example/${name}:1.0.0`,
        stack: 'media',
      })),
    },
    start: false,
  });
  d.host.write('var/lib/bootgate/status', 'media-lost\n');
  d.host.write('var/lib/bootgate/history.jsonl', `${JSON.stringify({ ts: '2026-10-24T14:02:00Z', boot_id: '4e1c', event: 'media-lost', detail: 'verify failed', wait_seconds: 0 })}\n`);
  await d.dash.start({ background: false });
  const s = await summaryOf(d);
  const by = Object.fromEntries(s.apps.map((a) => [a.name, a]));
  assert.deepEqual(
    Object.fromEntries(Object.entries(by).map(([k, a]) => [k, a.status])),
    { cloudflared: 'healthy', jellyfin: 'held', jellyseerr: 'held', prowlarr: 'held', radarr: 'held', sabnzbd: 'held', sonarr: 'held', 'wol-relay': 'healthy' },
  );
  assert.equal(by.sonarr.locks.restart, 'Held by the boot gate until the media drive is back.');
  assert.deepEqual(
    Object.fromEntries(Object.entries(by).map(([k, a]) => [k, a.stoppedReason])),
    { cloudflared: null, jellyfin: 'gate', jellyseerr: 'gate', prowlarr: 'gate', radarr: 'gate', sabnzbd: 'gate', sonarr: 'gate', 'wol-relay': null },
  );
  assert.equal(by.sonarr.locks.logs, null, 'logs still readable: Docker is up');
  assert.equal(by.cloudflared.locks.restart, null);
  assert.equal(s.gate.line, 'Media drive lost at 14:02. Media apps stopped (6 held). Retrying every 5 min.');
  assert.equal(s.issues[0].text, 'Media drive lost at 14:02. 6 apps held.');
  assert.equal(s.drive.mounted, false, 'statfs is not trusted unless the gate is ok');
});

test('helper unreachable: status unknown with a reason, a warn issue, the rest of the summary still works', async (t) => {
  const d = await startDashboard(t, 'normal', { start: false });
  const { createDashboard } = await import('../src/dashboard.js');
  const dash = createDashboard({ ...d.dash.cfg, helperSocket: '/nonexistent/helper.sock' }, { log: d.log, clock: d.clock, statfs: d.statfs, fetch: d.fetch, run: d.run });
  await dash.start({ background: false });
  const s = dash.summary.current().summary;
  assert.equal(s.helper.reachable, false);
  assert.equal(s.helper.error, 'unavailable');
  assert.ok(s.apps.every((a) => a.status === 'unknown'));
  assert.equal(s.apps[0].locks.restart, "Can't reach the Docker helper.");
  assert.equal(s.issues[0].text, "Can't reach the Docker helper. App status is unknown.");
  assert.equal(s.gate.status, 'ok');
  assert.equal(s.drive.mounted, true);
  dash.stop();
});

test('rehearsal flag and reboot-required are read from the host', async (t) => {
  const d = await startDashboard(t, 'normal');
  assert.equal((await summaryOf(d)).rehearsal, false);
  fs.mkdirSync(d.host.path('etc/homelab'), { recursive: true });
  fs.writeFileSync(d.host.path('etc/homelab/rehearsal'), '');
  d.host.write('run/reboot-required', '*** System restart required ***\n');
  await d.dash.summary.refreshCore();
  const s = await summaryOf(d);
  assert.equal(s.rehearsal, true);
  assert.equal(s.host.rebootRequired, true);
  assert.deepEqual(s.host.rebootPackages, []);
});

test('the declared app list is kept on disk, so a held boot right after a reboot lists the real apps', async (t) => {
  const { declaredFor, container } = await import('./helpers/fake-helper.js');
  // a rehearsal deployment: no cloudflared
  const containers = ['jellyfin', 'sonarr', 'wol-relay'].map((n) => container(n));
  const d = await startDashboard(t, 'normal', { helper: { containers, declared: declaredFor(containers) } });
  assert.deepEqual(d.dash.summary.current().summary.apps.map((a) => a.name), ['jellyfin', 'sonarr', 'wol-relay']);
  assert.deepEqual(JSON.parse(fs.readFileSync(`${d.host.stateDir}/declared.json`, 'utf8')).map((x) => x.name), ['jellyfin', 'sonarr', 'wol-relay']);
  // reboot into a bad night: a new process, Docker held, no ps possible
  d.helper.state.dockerState = 'inactive';
  d.host.write('var/lib/bootgate/status', 'failed\n');
  const { createDashboard } = await import('../src/dashboard.js');
  const dash = createDashboard(d.dash.cfg, { log: d.log, clock: d.clock, statfs: d.statfs, fetch: d.fetch, run: d.run });
  await dash.start({ background: false });
  const apps = dash.summary.current().summary.apps;
  assert.deepEqual(apps.map((a) => [a.name, a.status]), [['jellyfin', 'held'], ['sonarr', 'held'], ['wol-relay', 'held']]);
  assert.equal(apps.find((a) => a.name === 'wol-relay').localBuild, true);
  dash.stop();
});

test('while Docker is held, a refresh asks the helper exactly one thing: docker-state', async (t) => {
  const dc = { now: 0 };
  const d = await startDashboard(t, 'bad-night', { dockerOptions: { clock: () => dc.now } });
  dc.now += 5000; // past the 1 s docker-state reuse
  const before = d.helper.calls.length;
  await d.dash.summary.refreshCore();
  assert.deepEqual(d.helper.calls.slice(before).map((c) => c.op), ['docker-state'], 'the summary layer does not even try ps');
  // and with Docker up: one docker-state, then ps (whose own check reuses that answer)
  const dc2 = { now: 0 };
  const ok = await startDashboard(t, 'normal', { dockerOptions: { clock: () => dc2.now } });
  dc2.now += 5000;
  const b2 = ok.helper.calls.length;
  await ok.dash.summary.refreshCore();
  assert.deepEqual(ok.helper.calls.slice(b2).map((c) => c.op), ['docker-state', 'ps']);
});

test('server.timeZone and server-side times follow the host zone when none is configured', async (t) => {
  const { tempDir: td } = await import('./helpers/host.js');
  const dir = td(t);
  const tzFile = `${dir}/timezone`;
  fs.writeFileSync(tzFile, 'America/New_York\n');
  const d = await startDashboard(t, 'normal', { host: { extraConfig: { timezone: null, timezone_file: tzFile } } });
  const s = await summaryOf(d);
  assert.equal(s.server.timeZone, 'America/New_York');
  assert.match(s.gate.line, /\(Oct 21 02:01\)/, '06:01 UTC is 02:01 in New York');
  assert.match(d.text(), /"msg":"time zone","timeZone":"America\/New_York","source":"file"/);
});

test('runbook links from config reach the summary', async (t) => {
  const base = 'https://git.example.org/homelab-infra/blob/main/docs/runbooks';
  const d = await startDashboard(t, 'clean', {
    host: { extraConfig: { runbook_base_url: base, runbooks: [{ title: "Media drive won't mount", path: 'media-drive-failure.md' }, { title: 'Nope', path: '../x' }] } },
  });
  assert.deepEqual((await summaryOf(d)).links, { runbooks: [{ title: "Media drive won't mount", url: `${base}/media-drive-failure.md` }] });
});

test('media-lost with Docker down: media apps held by the gate, the others unknown/docker-down, and both issues shown', async (t) => {
  const d = await startDashboard(t, 'normal', { helper: { dockerState: 'inactive' }, start: false });
  d.host.write('var/lib/bootgate/status', 'media-lost\n');
  d.host.write('var/lib/bootgate/history.jsonl', `${JSON.stringify({ ts: '2026-10-24T14:02:00Z', boot_id: '4e1c', event: 'media-lost', detail: 'verify failed', wait_seconds: 0 })}\n`);
  await d.dash.start({ background: false });
  const s = await summaryOf(d);
  const by = Object.fromEntries(s.apps.map((a) => [a.name, [a.status, a.stoppedReason]]));
  assert.deepEqual(by, {
    cloudflared: ['unknown', 'docker-down'],
    jellyfin: ['held', 'gate'],
    jellyseerr: ['held', 'gate'],
    prowlarr: ['held', 'gate'],
    radarr: ['held', 'gate'],
    sabnzbd: ['held', 'gate'],
    sonarr: ['held', 'gate'],
    'wol-relay': ['unknown', 'docker-down'],
  });
  assert.deepEqual(
    s.issues.filter((i) => i.severity === 'fail').map((i) => i.text),
    ['Media drive lost at 14:02. 6 apps held.', "Docker isn't running."],
    'the Docker outage is not hidden behind the drive',
  );
  assert.equal(s.apps.find((a) => a.name === 'cloudflared').locks.restart, "Docker isn't running.");
  assert.equal(s.apps.find((a) => a.name === 'sonarr').locks.restart, 'Held by the boot gate until the media drive is back.');
  assert.equal((await getJson(d.base, '/api/apps/cloudflared/logs')).json.error, "Docker isn't running.");
  assert.equal((await getJson(d.base, '/api/apps/sonarr/logs')).json.error, 'Held by the boot gate until the media drive is back.');
  assert.equal(d.helper.dockerCalls().length, 0);
});

test('failed and waiting still hold everything, Docker up or down', async (t) => {
  for (const gate of ['failed', 'waiting']) {
    const d = await startDashboard(t, 'normal', { helper: { dockerState: 'inactive' }, start: false });
    d.host.write('var/lib/bootgate/status', `${gate}\n`);
    await d.dash.start({ background: false });
    const s = await summaryOf(d);
    assert.ok(s.apps.every((a) => a.status === 'held' && a.stoppedReason === 'gate'), gate);
    assert.ok(!s.issues.some((i) => i.text === "Docker isn't running."), `${gate}: the gate explains it`);
  }
});
