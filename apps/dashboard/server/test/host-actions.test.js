// CONTRACTS §11.7.2: GET /api/media/updates, POST /api/media/update (kind media-update), POST /api/host/reboot
// (kind reboot) and summary.hostActions, over HTTP with the fake helper and a fake registry (no network).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { ALL_APPS, container } from './helpers/fake-helper.js';
import { fakeRegistry } from './helpers/registry.js';
import { anchoredClock, getJson, postJson, sse, startDashboard } from './helpers/server.js';

const HELD = 'Held by the boot gate until the media drive is back.';
const TAILNET = 'Only available on the tailnet';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

async function finished(d, id, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const a = d.dash.actions.get(id);
    if (a && a.state !== 'running') return a;
    if (Date.now() > deadline) throw new Error(`action ${id} still running`);
    await wait(10);
  }
}

// Media apps pinned on lscr.io / Docker Hub, with the registry holding their current and latest releases:
//   jellyfin  10.11.9ubu2404-ls33 -> 12.4ubu2604-ls61   update, major
//   jellyseerr 2.7.3 -> 2.7.3-deprecated                deprecated
//   prowlarr  latest has no version label               error
//   radarr    on latest                                 current
//   sabnzbd   the registry answers 500                  error
//   sonarr    4.0.17.2952-ls312 -> 4.0.20.3014-ls7      update
function mediaFixture() {
  const reg = fakeRegistry();
  const ls = (n, version, tags, more = {}) => reg.push('lscr.io', `linuxserver/${n}`, { version, tags, ...more });
  const cur = {
    jellyfin: ls('jellyfin', '10.11.9ubu2404-ls33', ['10.11.9ubu2404-ls33']),
    prowlarr: ls('prowlarr', '1.30.2-ls100', ['1.30.2-ls100']),
    radarr: ls('radarr', '5.20.1-ls1', ['latest', '5.20.1-ls1']),
    sabnzbd: ls('sabnzbd', '4.5.0-ls1', ['4.5.0-ls1']),
    sonarr: ls('sonarr', '4.0.17.2952-ls312', ['4.0.17.2952-ls312']),
    jellyseerr: reg.push('registry-1.docker.io', 'fallenbagel/jellyseerr', { version: '2.7.3', tags: ['2.7.3'] }),
  };
  const latest = {
    jellyfin: ls('jellyfin', '12.4ubu2604-ls61', ['latest', '12.4ubu2604-ls61']),
    sonarr: ls('sonarr', '4.0.20.3014-ls7', ['latest', '4.0.20.3014-ls7']),
    jellyseerr: reg.push('registry-1.docker.io', 'fallenbagel/jellyseerr', { version: '2.7.3-deprecated', tags: ['latest', '2.7.3-deprecated'] }),
  };
  ls('prowlarr', '1.31.0-ls101', ['latest', '1.31.0-ls101'], { label: false });
  reg.opts.status.set('lscr.io/linuxserver/sabnzbd', 500);
  const repoOf = (n) => (n === 'jellyseerr' ? 'fallenbagel/jellyseerr' : `lscr.io/linuxserver/${n}`);
  const pins = Object.fromEntries(Object.entries(cur).map(([n, img]) => [n, img.pin(repoOf(n))]));
  const latestPins = Object.fromEntries(Object.entries(latest).map(([n, img]) => [n, img.pin(repoOf(n))]));
  const containers = ALL_APPS.map((n) => (pins[n] ? container(n, { image: pins[n], pinned: pins[n] }) : container(n)));
  return { reg, pins, latestPins, containers };
}

async function setup(t, scenario = 'normal', opts = {}) {
  const fx = mediaFixture();
  let skew = 0;
  const base = anchoredClock();
  const d = await startDashboard(t, scenario, {
    clock: () => base() + skew,
    registryFetch: fx.reg.fetch,
    ...opts,
    helper: { containers: fx.containers, updateStepMs: 5, ...(opts.helper ?? {}) },
  });
  return { ...d, fx, advance: (ms) => (skew += ms) };
}

const stepsOf = (a) => a.steps.map(({ at, ...s }) => (void at, s));

test('GET /api/media/updates: every media app with status, major and message; edge/local apps left out; cached 10 min', async (t) => {
  const d = await setup(t);
  const r = await getJson(d.base, '/api/media/updates');
  assert.equal(r.status, 200);
  assert.deepEqual(Object.keys(r.json), ['checkedAt', 'apps']);
  assert.ok(Math.abs(Date.parse(r.json.checkedAt) - d.clock()) < 5000, 'checked now');
  const { pins, latestPins } = d.fx;
  assert.deepEqual(r.json.apps, [
    { name: 'jellyfin', label: 'Jellyfin', current: { version: '10.11.9ubu2404-ls33', pin: pins.jellyfin }, latest: { version: '12.4ubu2604-ls61', pin: latestPins.jellyfin }, major: true, status: 'update', message: null },
    {
      name: 'jellyseerr',
      label: 'Jellyseerr',
      current: { version: '2.7.3', pin: pins.jellyseerr },
      latest: { version: '2.7.3-deprecated', pin: latestPins.jellyseerr },
      major: false,
      status: 'deprecated',
      message: 'The image is no longer maintained (its latest release is 2.7.3-deprecated).',
    },
    { name: 'prowlarr', label: 'Prowlarr', current: { version: '1.30.2-ls100', pin: pins.prowlarr }, latest: null, status: 'error', major: false, message: "Couldn't check for updates: the image has no version label." },
    { name: 'radarr', label: 'Radarr', current: { version: '5.20.1-ls1', pin: pins.radarr }, latest: { version: '5.20.1-ls1', pin: pins.radarr }, major: false, status: 'current', message: null },
    { name: 'sabnzbd', label: 'SABnzbd', current: { version: '4.5.0-ls1', pin: pins.sabnzbd }, latest: null, status: 'error', major: false, message: "Couldn't check for updates: the registry answered 500." },
    { name: 'sonarr', label: 'Sonarr', current: { version: '4.0.17.2952-ls312', pin: pins.sonarr }, latest: { version: '4.0.20.3014-ls7', pin: latestPins.sonarr }, major: false, status: 'update', message: null },
  ]);
  assert.doesNotMatch(r.text, /https?:\/\/|tok-|Bearer/, 'no URLs or tokens reach the browser');
  assert.equal(d.helper.calls.filter((c) => c.op === 'set-pin').length, 0);
  const n = d.fx.reg.calls.length;
  await getJson(d.base, '/api/media/updates');
  assert.equal(d.fx.reg.calls.length, n, 'served from the cache');
  d.advance(11 * 60_000);
  await getJson(d.base, '/api/media/updates');
  assert.ok(d.fx.reg.calls.length > n, 'looked up again after 10 min');
  const m = d.fx.reg.calls.length;
  await getJson(d.base, '/api/media/updates?refresh=1');
  assert.ok(d.fx.reg.calls.length > m, 'refresh=1 skips the cache');
  // read guards apply
  assert.equal((await getJson(d.base, '/api/media/updates', { 'Sec-Fetch-Site': 'cross-site' })).status, 403);
});

test('GET /api/media/updates: at most 3 lookups in parallel', async (t) => {
  const fx = mediaFixture();
  let inFlight = 0;
  let most = 0;
  const slow = async (url, init) => {
    inFlight += 1;
    most = Math.max(most, inFlight);
    try {
      await wait(15);
      return await fx.reg.fetch(url, init);
    } finally {
      inFlight -= 1;
    }
  };
  const d = await startDashboard(t, 'normal', { registryFetch: slow, helper: { containers: fx.containers } });
  assert.equal((await getJson(d.base, '/api/media/updates')).status, 200);
  assert.ok(most <= 3 && most >= 2, `parallel lookups: ${most}`);
});

test('POST /api/media/update: apps one at a time with set-pin and the checked pin; step stream; done detail; cache dropped', async (t) => {
  const d = await setup(t);
  await getJson(d.base, '/api/media/updates');
  const r = await postJson(d.base, '/api/media/update', { apps: ['sonarr', 'jellyfin'] });
  assert.equal(r.status, 202);
  assert.equal(r.json.action.kind, 'media-update');
  assert.equal(r.json.action.app, null);
  const stream = sse(d.port, `/api/actions/${r.json.action.id}`);
  const end = await stream.until('end', () => true, 5000);
  const steps = stream.events.filter((e) => e.event === 'step').map(({ data: { at, ...s } }) => (assert.ok(at), s));
  const helperSteps = (app) => ['pulling', 'pinning', 'recreating', 'waiting-health'].map((step) => ({ step, app }));
  assert.deepEqual(steps, [
    { step: 'app', app: 'sonarr', index: 1, total: 2, from: '4.0.17.2952-ls312', to: '4.0.20.3014-ls7' },
    ...helperSteps('sonarr'),
    { step: 'app-done', app: 'sonarr' },
    { step: 'app', app: 'jellyfin', index: 2, total: 2, from: '10.11.9ubu2404-ls33', to: '12.4ubu2604-ls61' },
    ...helperSteps('jellyfin'),
    { step: 'app-done', app: 'jellyfin' },
  ]);
  assert.deepEqual(end.data, { state: 'done', error: null, detail: 'Updated 2 apps: Sonarr 4.0.20.3014-ls7, Jellyfin 12.4ubu2604-ls61' });
  assert.deepEqual(
    d.helper.calls.filter((c) => c.op === 'set-pin').map(({ op, name, pin }) => ({ op, name, pin })),
    [
      { op: 'set-pin', name: 'sonarr', pin: d.fx.latestPins.sonarr },
      { op: 'set-pin', name: 'jellyfin', pin: d.fx.latestPins.jellyfin },
    ],
  );
  assert.ok(d.helper.calls.filter((c) => c.op === 'set-pin').every((c) => c.dockerStateAtCall === 'active'));
  // the snapshot carries the same steps
  const snap = await getJson(d.base, `/api/actions/${r.json.action.id}`, { Accept: 'application/json' });
  assert.equal(snap.json.steps.at(-1).step, 'done');
  assert.deepEqual(snap.json.steps[0], { ...steps[0], at: snap.json.steps[0].at });
  const again = await postJson(d.base, '/api/media/update', { apps: ['sonarr'] });
  assert.equal(again.status, 409);
  assert.equal(again.json.error, 'Check for updates again', 'the check cache was dropped');
  await d.dash.summary.refreshCore();
  assert.deepEqual(d.dash.summary.current().summary.hostActions, { reboot: { lock: null }, mediaUpdate: { lock: null, running: null } });
});

test('POST /api/media/update: one app gives "Updated 1 app"', async (t) => {
  const d = await setup(t);
  await getJson(d.base, '/api/media/updates');
  const a = await finished(d, (await postJson(d.base, '/api/media/update', { apps: ['sonarr'] })).json.action.id);
  assert.equal(a.detail, 'Updated 1 app: Sonarr 4.0.20.3014-ls7');
});

test('POST /api/media/update: stops at the first failure (rolled back), the rest never starts', async (t) => {
  const d = await setup(t, 'normal', { helper: { setPinSteps: { sonarr: ['pulling', 'pinning', 'recreating', 'waiting-health', 'rolling-back', 'failed'] } } });
  await getJson(d.base, '/api/media/updates');
  const a = await finished(d, (await postJson(d.base, '/api/media/update', { apps: ['sonarr', 'jellyfin'] })).json.action.id);
  assert.equal(a.state, 'failed');
  assert.deepEqual(stepsOf(a), [
    { step: 'app', app: 'sonarr', index: 1, total: 2, from: '4.0.17.2952-ls312', to: '4.0.20.3014-ls7' },
    ...['pulling', 'pinning', 'recreating', 'waiting-health', 'rolling-back'].map((step) => ({ step, app: 'sonarr' })),
    { step: 'app-failed', app: 'sonarr', rolledBack: true },
    { step: 'failed' },
  ]);
  assert.equal(a.error, 'Sonarr failed at waiting-health: health check failed; rolled back to 4.0.17.2952-ls312. No other app was changed. Not started: Jellyfin.');
  assert.deepEqual(d.helper.calls.filter((c) => c.op === 'set-pin').map((c) => c.name), ['sonarr']);
});

test('POST /api/media/update: a later failure reports what already finished; helper text loses its URLs', async (t) => {
  const d = await setup(t, 'normal', { helper: { setPinSteps: { sonarr: ['pulling', 'failed'] } } });
  await getJson(d.base, '/api/media/updates');
  const a = await finished(d, (await postJson(d.base, '/api/media/update', { apps: ['jellyfin', 'sonarr'] })).json.action.id);
  assert.equal(a.state, 'failed');
  assert.deepEqual(stepsOf(a).filter((s) => s.step.startsWith('app')), [
    { step: 'app', app: 'jellyfin', index: 1, total: 2, from: '10.11.9ubu2404-ls33', to: '12.4ubu2604-ls61' },
    { step: 'app-done', app: 'jellyfin' },
    { step: 'app', app: 'sonarr', index: 2, total: 2, from: '4.0.17.2952-ls312', to: '4.0.20.3014-ls7' },
    { step: 'app-failed', app: 'sonarr', rolledBack: false },
  ]);
  assert.equal(a.error, 'Sonarr failed at pulling: pull failed: manifest unknown at [address]. Already updated: Jellyfin 12.4ubu2604-ls61.');
});

test('POST /api/media/update: a helper refusal before the stream fails that app with its message', async (t) => {
  const d = await setup(t, 'normal', { helper: { fail: { 'set-pin': { code: 'held-by-gate', message: 'the boot gate status is failed' } } } });
  await getJson(d.base, '/api/media/updates');
  const a = await finished(d, (await postJson(d.base, '/api/media/update', { apps: ['sonarr'] })).json.action.id);
  assert.deepEqual(stepsOf(a).slice(1), [{ step: 'app-failed', app: 'sonarr', rolledBack: false }, { step: 'failed' }]);
  assert.equal(a.error, 'Sonarr failed: the boot gate status is failed. No other app was changed.');
});

test('POST /api/media/update: bad bodies 400; not offered, never checked, stale or moved pins 409 "Check for updates again"', async (t) => {
  const d = await setup(t);
  for (const body of [{}, { apps: [] }, { apps: 'sonarr' }, { apps: ['sonarr', 'sonarr'] }, { apps: ['Bad Name'] }, { apps: [1] }, { apps: Array.from({ length: 11 }, (_, i) => `app${i}`) }]) {
    const r = await postJson(d.base, '/api/media/update', body);
    assert.equal(r.status, 400, JSON.stringify(body));
  }
  assert.equal((await postJson(d.base, '/api/media/update', { apps: ['sonarr'] })).json.error, 'Check for updates again', 'no check yet');
  await getJson(d.base, '/api/media/updates');
  for (const apps of [['radarr'], ['jellyseerr'], ['prowlarr'], ['cloudflared'], ['sonarr', 'radarr'], ['nosuchapp']]) {
    const r = await postJson(d.base, '/api/media/update', { apps });
    assert.equal(r.status, 409, apps.join());
    assert.equal(r.json.error, 'Check for updates again', apps.join());
  }
  // the pin moved since the check (a deploy, a per-app update)
  d.helper.state.containers.find((c) => c.name === 'sonarr').pinned = d.fx.latestPins.sonarr;
  const moved = await postJson(d.base, '/api/media/update', { apps: ['sonarr'] });
  assert.equal(moved.status, 409);
  assert.equal(moved.json.error, 'Check for updates again');
  d.helper.state.containers.find((c) => c.name === 'sonarr').pinned = d.fx.pins.sonarr;
  // older than 10 min
  d.advance(10 * 60_000 + 1000);
  const stale = await postJson(d.base, '/api/media/update', { apps: ['sonarr'] });
  assert.equal(stale.status, 409);
  assert.equal(stale.json.error, 'Check for updates again');
  assert.equal(d.helper.calls.filter((c) => c.op === 'set-pin').length, 0);
});

test('POST /api/media/update: gate held or Docker down (fresh state) -> 409 with the reason, nothing sent', async (t) => {
  const d = await setup(t);
  await getJson(d.base, '/api/media/updates');
  d.host.write('var/lib/bootgate/status', 'media-lost\n');
  const held = await postJson(d.base, '/api/media/update', { apps: ['sonarr'] });
  assert.equal(held.status, 409);
  assert.equal(held.json.error, HELD);
  assert.equal(d.dash.summary.current().summary.hostActions.mediaUpdate.lock, HELD);
  d.host.write('var/lib/bootgate/status', 'ok\n');
  d.helper.state.dockerState = 'inactive';
  const down = await postJson(d.base, '/api/media/update', { apps: ['sonarr'] });
  assert.equal(down.status, 409);
  assert.equal(down.json.error, "Docker isn't running.");
  await wait(1100); // the summary reuses a docker-state answer for about 1 s
  await d.dash.summary.refreshCore();
  assert.equal(d.dash.summary.current().summary.hostActions.mediaUpdate.lock, "Docker isn't running.");
  assert.equal(d.helper.calls.filter((c) => c.op === 'set-pin').length, 0);
});

test('a media update and a per-app update exclude each other; reboot and actions on its apps are Busy meanwhile', async (t) => {
  const d = await setup(t, 'normal', { helper: { updateStepMs: 60 } });
  await getJson(d.base, '/api/media/updates');
  const r = await postJson(d.base, '/api/media/update', { apps: ['sonarr', 'jellyfin'] });
  assert.equal(r.status, 202);
  const id = r.json.action.id;
  assert.deepEqual(d.dash.summary.current().summary.hostActions, { reboot: { lock: 'Busy' }, mediaUpdate: { lock: 'Busy', running: id } });
  const appUpdate = await postJson(d.base, '/api/apps/cloudflared/update', {});
  assert.equal(appUpdate.status, 409);
  assert.equal(appUpdate.json.error, 'Another update is running (Media app updates). Try again when it finishes.');
  const second = await postJson(d.base, '/api/media/update', { apps: ['sonarr'] });
  assert.equal(second.status, 409);
  assert.equal(second.json.error, 'Another update is running (Media app updates). Try again when it finishes.');
  const restart = await postJson(d.base, '/api/apps/jellyfin/restart', {});
  assert.equal(restart.status, 409);
  assert.equal(restart.json.error, 'Busy');
  const reboot = await postJson(d.base, '/api/host/reboot', {});
  assert.equal(reboot.status, 409);
  assert.equal(reboot.json.error, 'Busy');
  assert.equal(d.helper.state.rebooted, 0);
  assert.equal((await finished(d, id)).state, 'done');

  // the other way round: a per-app update running -> the media update waits
  const cf = d.helper.state.containers.find((c) => c.name === 'cloudflared');
  cf.pinned = `example/cloudflared:2.0.0@sha256:${'c'.repeat(64)}`;
  d.helper.state.updateSteps = ['pulling', 'recreating', 'waiting-health', 'done'];
  await d.dash.summary.refreshCore();
  await getJson(d.base, '/api/media/updates?refresh=1');
  const up = await postJson(d.base, '/api/apps/cloudflared/update', {});
  assert.equal(up.status, 202);
  const media = await postJson(d.base, '/api/media/update', { apps: ['prowlarr'] });
  assert.equal(media.status, 409);
  assert.equal(media.json.error, 'Another update is running (cloudflared). Try again when it finishes.');
  await finished(d, up.json.action.id);
});

test('remote access: the check, the media update and the reboot are 403; hostActions locked with the reason', async (t) => {
  const d = await setup(t, 'normal', { access: { decide: async () => 'remote' } });
  assert.equal((await getJson(d.base, '/api/media/updates')).status, 403);
  const m = await postJson(d.base, '/api/media/update', { apps: ['sonarr'] });
  assert.equal(m.status, 403);
  assert.equal(m.json.error, TAILNET);
  const r = await postJson(d.base, '/api/host/reboot', {});
  assert.equal(r.status, 403);
  assert.equal(r.json.error, TAILNET);
  const s = await getJson(d.base, '/api/summary');
  assert.deepEqual(s.json.hostActions, { reboot: { lock: TAILNET }, mediaUpdate: { lock: TAILNET, running: null } });
  assert.equal(d.helper.state.rebooted, 0);
  assert.equal(d.fx.reg.calls.length, 0, 'no registry lookup for remote');
});

test('POST /api/host/reboot: requested -> scheduled -> done with the detail; logged as a warning; nothing new starts after', async (t) => {
  const d = await setup(t);
  const r = await postJson(d.base, '/api/host/reboot', {});
  assert.equal(r.status, 202);
  assert.equal(r.json.action.kind, 'reboot');
  assert.equal(r.json.action.app, null);
  assert.equal(r.json.action.state, 'done');
  assert.deepEqual(r.json.action.steps.map((s) => s.step), ['requested', 'scheduled', 'done']);
  assert.equal(r.json.action.detail, 'Restarting in 10 s. The dashboard is back in a few minutes.');
  assert.equal(d.helper.state.rebooted, 1);
  assert.deepEqual(d.helper.calls.filter((c) => c.op === 'reboot').map(({ dockerStateAtCall, ...c }) => (void dockerStateAtCall, c)), [{ op: 'reboot' }]);
  assert.ok(d.lines.some((l) => l.includes('"msg":"host reboot scheduled"') && l.includes('"level":"warn"')));
  const end = await sse(d.port, `/api/actions/${r.json.action.id}`).until('end', () => true);
  assert.equal(end.data.detail, 'Restarting in 10 s. The dashboard is back in a few minutes.');
  for (const [p, body] of [['/api/host/reboot', {}], ['/api/apps/sonarr/restart', {}], ['/api/media/update', { apps: ['sonarr'] }]]) {
    const x = await postJson(d.base, p, body);
    assert.equal(x.status, 409, p);
    assert.equal(x.json.error, 'The host is restarting.', p);
  }
  assert.deepEqual(d.dash.summary.current().summary.hostActions, {
    reboot: { lock: 'The host is restarting.' },
    mediaUpdate: { lock: 'The host is restarting.', running: null },
  });
  assert.equal(d.helper.state.rebooted, 1);
});

test('POST /api/host/reboot: 409 while an app action runs (no helper call); the helper\'s busy / too-soon pass through and leave nothing', async (t) => {
  const d = await setup(t);
  d.helper.state.delay.restart = 300;
  const restart = await postJson(d.base, '/api/apps/sonarr/restart', {});
  assert.equal(restart.status, 202);
  assert.equal(d.dash.summary.current().summary.hostActions.reboot.lock, 'Busy');
  const busy = await postJson(d.base, '/api/host/reboot', {});
  assert.equal(busy.status, 409);
  assert.equal(busy.json.error, 'Busy');
  assert.equal(d.helper.calls.filter((c) => c.op === 'reboot').length, 0, 'refused before any helper call');
  await finished(d, restart.json.action.id);

  for (const refusal of [
    { code: 'too-soon', message: 'the box just started; try again in 4 min' },
    { code: 'busy', message: 'a reboot is already scheduled' },
  ]) {
    d.helper.state.reboot = refusal;
    const r = await postJson(d.base, '/api/host/reboot', {});
    assert.equal(r.status, 409, refusal.code);
    assert.equal(r.json.error, refusal.message);
    assert.equal(d.dash.summary.current().summary.hostActions.reboot.lock, null, 'no action left behind');
  }
  d.helper.state.reboot = { code: 'failed', message: 'systemd-run: exit 1 (unit file at /etc/x)' };
  const failed = await postJson(d.base, '/api/host/reboot', {});
  assert.equal(failed.status, 502);
  assert.equal(failed.json.error, "Couldn't schedule the restart.");
  assert.equal(d.helper.state.rebooted, 0);
});

test('a gate re-run blocks the reboot (Busy); the reboot works while Docker is held', async (t) => {
  const d = await setup(t, 'bad-night');
  assert.deepEqual(d.dash.summary.current().summary.hostActions, { reboot: { lock: null }, mediaUpdate: { lock: HELD, running: null } });
  const rerun = await postJson(d.base, '/api/gate/rerun', {});
  assert.equal(rerun.status, 202);
  const r = await postJson(d.base, '/api/host/reboot', {});
  assert.equal(r.status, 409);
  assert.equal(r.json.error, 'Busy');
  await finished(d, rerun.json.action.id, 6000);
  const ok = await postJson(d.base, '/api/host/reboot', {});
  assert.equal(ok.status, 202);
  assert.equal(d.helper.state.rebooted, 1);
  assert.equal(d.helper.dockerCalls().length, 0, 'the reboot is not a Docker op');
});

test('just after boot: both locks say when to try again; the server refuses without asking the helper', async (t) => {
  const d = await setup(t);
  d.host.write('proc/uptime', '120.50 100.00\n');
  await d.dash.summary.refreshCore();
  const lock = 'The box just started. Try again in 8 min.';
  assert.deepEqual(d.dash.summary.current().summary.hostActions, { reboot: { lock }, mediaUpdate: { lock, running: null } });
  const r = await postJson(d.base, '/api/host/reboot', {});
  assert.equal(r.status, 409);
  assert.equal(r.json.error, lock);
  await getJson(d.base, '/api/media/updates');
  const m = await postJson(d.base, '/api/media/update', { apps: ['sonarr'] });
  assert.equal(m.status, 409);
  assert.equal(m.json.error, lock);
  assert.equal(d.helper.calls.filter((c) => c.op === 'reboot' || c.op === 'set-pin').length, 0);
  d.host.write('proc/uptime', '590 100.00\n');
  await d.dash.summary.refreshCore();
  assert.equal(d.dash.summary.current().summary.hostActions.reboot.lock, 'The box just started. Try again in 1 min.');
});

test('summary.hostActions in the scenarios', async (t) => {
  const normal = await setup(t, 'normal');
  assert.deepEqual((await getJson(normal.base, '/api/summary')).json.hostActions, { reboot: { lock: null }, mediaUpdate: { lock: null, running: null } });
  assert.equal('_uptimeSec' in (await getJson(normal.base, '/api/summary')).json.host, false, 'internal field stripped');
  const inactive = await setup(t, 'docker-inactive');
  assert.deepEqual((await getJson(inactive.base, '/api/summary')).json.hostActions, { reboot: { lock: null }, mediaUpdate: { lock: "Docker isn't running.", running: null } });
});

test('a pin ahead of the registry\'s latest is "ahead": never offered as a downgrade', async (t) => {
  const fx = mediaFixture();
  // sonarr's latest moves back below the pin (pinned 4.0.17.2952-ls312); radarr's to an older linuxserver build
  fx.reg.push('lscr.io', 'linuxserver/sonarr', { version: '4.0.16.2900-ls400', tags: ['latest', '4.0.16.2900-ls400'] });
  fx.reg.push('lscr.io', 'linuxserver/radarr', { version: '5.20.1-ls0', tags: ['latest', '5.20.1-ls0'] });
  const d = await startDashboard(t, 'normal', { registryFetch: fx.reg.fetch, helper: { containers: fx.containers } });
  const apps = (await getJson(d.base, '/api/media/updates')).json.apps;
  const by = Object.fromEntries(apps.map((a) => [a.name, a]));
  for (const [name, version] of [['sonarr', '4.0.16.2900-ls400'], ['radarr', '5.20.1-ls0']]) {
    assert.equal(by[name].status, 'ahead', name);
    assert.equal(by[name].latest.version, version, name);
    assert.equal(by[name].major, false, name);
    assert.equal(by[name].message, "The pinned version is newer than the registry's latest release.", name);
  }
  assert.equal(by.jellyfin.status, 'update', 'newer is still offered');
  for (const name of ['sonarr', 'radarr']) {
    const r = await postJson(d.base, '/api/media/update', { apps: [name] });
    assert.equal(r.status, 409, name);
    assert.equal(r.json.error, 'Check for updates again', name);
  }
  assert.equal(d.helper.calls.filter((c) => c.op === 'set-pin').length, 0);
});

test('a gate re-run is refused (409 Busy) while a media update runs, and the summary says so', async (t) => {
  const d = await setup(t, 'normal', { helper: { updateStepMs: 60 } });
  await getJson(d.base, '/api/media/updates');
  const r = await postJson(d.base, '/api/media/update', { apps: ['sonarr', 'jellyfin'] });
  assert.equal(r.status, 202);
  d.host.write('var/lib/bootgate/status', 'media-lost\n'); // a state in which a re-run would act
  const rerun = await postJson(d.base, '/api/gate/rerun', {});
  assert.equal(rerun.status, 409);
  assert.equal(rerun.json.error, 'Busy');
  assert.equal(fs.existsSync(d.host.path('run/bootgate/rerun')), false, 'no trigger written');
  await d.dash.summary.refreshCore();
  const gate = d.dash.summary.current().summary.gate;
  assert.equal(gate.canRerun, false);
  assert.equal(gate.rerunLock, 'Busy');
  await finished(d, r.json.action.id);
  await d.dash.summary.refreshCore();
  assert.equal(d.dash.summary.current().summary.gate.canRerun, true, 'unlocked once it ends');
});
