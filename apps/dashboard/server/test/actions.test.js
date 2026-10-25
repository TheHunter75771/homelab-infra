// Actions: app restart/stop/start/update and the gate re-run, over HTTP with the fake helper.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { getJson, postJson, sse, startDashboard } from './helpers/server.js';
import { makeRun } from './helpers/host.js';

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

async function finished(d, id, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const a = d.dash.actions.get(id);
    if (a && a.state !== 'running') return a;
    if (Date.now() > deadline) throw new Error(`action ${id} still running`);
    await wait(10);
  }
}

test('restart: 202 + action; steps restarting -> waiting-health -> done once healthy; summary shows "starting" meanwhile', async (t) => {
  const d = await startDashboard(t, 'normal');
  d.helper.state.onControl = (op, name, state) => {
    const c = state.containers.find((x) => x.name === name);
    c.health = 'starting';
    setTimeout(() => {
      c.health = 'healthy';
    }, 80);
  };
  const r = await postJson(d.base, '/api/apps/sonarr/restart', {});
  assert.equal(r.status, 202);
  assert.equal(r.json.ok, true);
  assert.deepEqual(Object.keys(r.json.action), ['id', 'kind', 'app', 'state', 'steps', 'error', 'detail', 'startedAt', 'finishedAt']);
  assert.equal(r.json.action.kind, 'restart');
  assert.equal(d.dash.summary.current().summary.apps.find((a) => a.name === 'sonarr').status, 'starting', 'optimistic chip');
  const a = await finished(d, r.json.action.id);
  assert.equal(a.state, 'done');
  assert.deepEqual(a.steps.map((s) => s.step), ['restarting', 'waiting-health', 'done']);
  assert.equal(a.detail, 'Sonarr is healthy again');
  assert.deepEqual(d.helper.calls.filter((c) => c.op === 'restart').map((c) => c.name), ['sonarr']);
  assert.match(d.text(), /"msg":"action finished".*"kind":"restart","app":"sonarr","ok":true/);
});

test('restart that never gets healthy fails with a reason; one that exits fails at once', async (t) => {
  const d = await startDashboard(t, 'normal', { actionOptions: { healthTimeoutMs: 200 } });
  d.helper.state.onControl = (op, name, state) => {
    state.containers.find((x) => x.name === name).health = 'unhealthy';
  };
  const a = await finished(d, (await postJson(d.base, '/api/apps/radarr/restart', {})).json.action.id);
  assert.equal(a.state, 'failed');
  assert.match(a.error, /Radarr didn't become healthy within/);
  d.helper.state.onControl = (op, name, state) => {
    state.containers.find((x) => x.name === name).state = 'exited';
  };
  const b = await finished(d, (await postJson(d.base, '/api/apps/prowlarr/restart', {})).json.action.id);
  assert.equal(b.error, 'Prowlarr exited instead of starting.');
});

test('stop marks "stopped on purpose" (persisted, no issue); start clears it', async (t) => {
  const d = await startDashboard(t, 'normal');
  const stop = await finished(d, (await postJson(d.base, '/api/apps/sabnzbd/stop', {})).json.action.id);
  assert.equal(stop.state, 'done');
  assert.equal(stop.detail, 'SABnzbd stopped. It stays down until you start it.');
  assert.deepEqual(JSON.parse(fs.readFileSync(`${d.host.stateDir}/stopped.json`, 'utf8')), ['sabnzbd']);
  await d.dash.summary.refreshCore();
  let s = d.dash.summary.current().summary;
  const sab = s.apps.find((a) => a.name === 'sabnzbd');
  assert.equal(sab.status, 'stopped');
  assert.equal(sab.stoppedReason, 'intentional');
  assert.equal(sab.locks.restart, 'Stopped. Start it instead');
  assert.equal(sab.locks.start, null);
  assert.ok(!s.issues.some((i) => i.app === 'sabnzbd'), 'stopped on purpose is not an issue');
  const start = await finished(d, (await postJson(d.base, '/api/apps/sabnzbd/start', {})).json.action.id);
  assert.deepEqual(start.steps.map((x) => x.step), ['starting', 'waiting-health', 'done']);
  assert.deepEqual(JSON.parse(fs.readFileSync(`${d.host.stateDir}/stopped.json`, 'utf8')), []);
  await d.dash.summary.refreshCore();
  s = d.dash.summary.current().summary;
  assert.equal(s.apps.find((a) => a.name === 'sabnzbd').status, 'healthy');
});

test('an app started outside the dashboard loses its "stopped on purpose" mark', async (t) => {
  const d = await startDashboard(t, 'normal', { start: false });
  fs.writeFileSync(`${d.host.stateDir}/stopped.json`, '["sonarr"]');
  const { createDashboard } = await import('../src/dashboard.js');
  const dash = createDashboard(d.dash.cfg, { log: d.log, clock: d.clock, statfs: d.statfs, fetch: d.fetch, run: d.run });
  await dash.start({ background: false }); // sonarr is running in ps
  assert.deepEqual(JSON.parse(fs.readFileSync(`${d.host.stateDir}/stopped.json`, 'utf8')), []);
  dash.stop();
});

test('update: tailnet only; helper steps relayed; a failed pull fails with its reason; no update when on the pinned version', async (t) => {
  const d = await startDashboard(t, 'normal', {
    helper: {
      containers: ['sonarr', 'radarr'].map((name) => ({
        name,
        state: 'running',
        health: 'healthy',
        started_at: '2026-10-21T10:02:00Z',
        restart_count: 0,
        image: `example/${name}:1.0.0@sha256:${'a'.repeat(64)}`,
        pinned: name === 'sonarr' ? `example/${name}:1.1.0@sha256:${'b'.repeat(64)}` : `example/${name}:1.0.0@sha256:${'a'.repeat(64)}`,
        stack: 'media',
      })),
    },
  });
  const sonarr = d.dash.summary.current().summary.apps.find((a) => a.name === 'sonarr');
  assert.equal(sonarr.updateAvailable, true);
  assert.equal(sonarr.pinnedTag, '1.1.0');
  assert.equal(sonarr.locks.update, null);
  const r = await postJson(d.base, '/api/apps/sonarr/update', {});
  assert.equal(r.status, 202);
  const stream = sse(d.port, `/api/actions/${r.json.action.id}`);
  const end = await stream.until('end', () => true);
  assert.deepEqual(
    stream.events.filter((e) => e.event === 'step').map((e) => e.data.step),
    ['pulling', 'recreating', 'waiting-health'],
  );
  assert.deepEqual(end.data, { state: 'done', error: null, detail: 'Sonarr updated and healthy' });
  const noUpdate = await postJson(d.base, '/api/apps/radarr/update', {});
  assert.equal(noUpdate.status, 409);
  assert.equal(noUpdate.json.error, 'Already on the pinned version');
  d.helper.state.updateSteps = ['pulling', 'failed'];
  const bad = await finished(d, (await postJson(d.base, '/api/apps/sonarr/update', {})).json.action.id);
  assert.equal(bad.state, 'failed');
  assert.equal(bad.error, 'pulling: manifest unknown');
  assert.deepEqual(bad.steps.map((x) => x.step), ['pulling', 'failed']);
});

test('locks are enforced server-side: busy -> 409, held -> 409 with the reason, Docker inactive -> 409 and no op sent', async (t) => {
  const d = await startDashboard(t, 'normal');
  d.helper.state.delay.restart = 200;
  const first = await postJson(d.base, '/api/apps/sonarr/restart', {});
  assert.equal(first.status, 202);
  const busy = await postJson(d.base, '/api/apps/sonarr/stop', {});
  assert.equal(busy.status, 409);
  assert.equal(busy.json.error, 'Busy');
  await finished(d, first.json.action.id);
  assert.equal((await postJson(d.base, '/api/apps/sonarr/start', {})).json.error, 'Already running');

  const bad = await startDashboard(t, 'bad-night');
  for (const action of ['restart', 'stop', 'start', 'update']) {
    const r = await postJson(bad.base, `/api/apps/sonarr/${action}`, {});
    assert.equal(r.status, 409, action);
    assert.equal(r.json.error, 'Held by the boot gate until the media drive is back.');
  }
  assert.equal(bad.helper.dockerCalls().length, 0, 'not one Docker op while held');
  assert.equal((await postJson(bad.base, '/api/apps/postgres/restart', {})).status, 404);
});

test('GET /api/actions/:id: JSON snapshot on request; a finished action replays its steps then ends', async (t) => {
  const d = await startDashboard(t, 'normal');
  const id = (await postJson(d.base, '/api/apps/wol-relay/stop', {})).json.action.id;
  await finished(d, id);
  const snap = await getJson(d.base, `/api/actions/${id}`, { Accept: 'application/json' });
  assert.equal(snap.json.state, 'done');
  const s = sse(d.port, `/api/actions/${id}`);
  const end = await s.until('end', () => true);
  assert.deepEqual(s.events.filter((e) => e.event === 'step').map((e) => e.data.step), ['stopping']);
  assert.equal(end.data.state, 'done');
});

test('gate re-run: touches the trigger, follows the retry service, reports success', async (t) => {
  const run = makeRun({ retryStates: ['activating', 'activating', 'inactive'] });
  const d = await startDashboard(t, 'bad-night', { run });
  const trigger = d.host.path('run/bootgate/rerun');
  // the "gate": picks up the trigger, then (as a passing retry would) writes status ok
  const gate = setInterval(() => {
    if (fs.existsSync(trigger)) {
      fs.unlinkSync(trigger);
      setTimeout(() => d.host.write('var/lib/bootgate/status', 'ok\n'), 30);
    }
  }, 20);
  t.after(() => clearInterval(gate));
  const r = await postJson(d.base, '/api/gate/rerun', {});
  assert.equal(r.status, 202);
  assert.equal(r.json.action.kind, 'gate-rerun');
  assert.equal(r.json.action.app, null);
  const again = await postJson(d.base, '/api/gate/rerun', {});
  assert.equal(again.status, 409);
  assert.equal(again.json.error, 'A re-run is already in progress.');
  const a = await finished(d, r.json.action.id);
  assert.equal(a.state, 'done');
  assert.deepEqual(a.steps.map((s) => s.step), ['requested', 'running', 'done']);
  assert.equal(a.detail, 'Check passed. Drive mounted at /media.');
  assert.ok(run.calls.some((c) => c.includes('bootgate-retry.service') && c.includes('ActiveState')));
  assert.equal(d.helper.dockerCalls().length, 0, 'the re-run never touches Docker itself');
});

test('gate re-run: still failing -> failed with the next-retry copy; not picked up -> failed; ok gate -> 409', async (t) => {
  const d = await startDashboard(t, 'bad-night', { run: makeRun({ retryStates: ['inactive'] }) });
  const trigger = d.host.path('run/bootgate/rerun');
  const gate = setInterval(() => fs.existsSync(trigger) && fs.unlinkSync(trigger), 20);
  const a = await finished(d, (await postJson(d.base, '/api/gate/rerun', {})).json.action.id);
  clearInterval(gate);
  assert.equal(a.state, 'failed');
  assert.equal(a.error, 'Check failed: the drive is still not verified. Next automatic retry in 5 min.');

  const b = await finished(d, (await postJson(d.base, '/api/gate/rerun', {})).json.action.id, 4000);
  assert.equal(b.error, "The gate didn't pick up the request within 2 s.");
  fs.rmSync(trigger, { force: true });

  const ok = await startDashboard(t, 'normal');
  const r = await postJson(ok.base, '/api/gate/rerun', {});
  assert.equal(r.status, 409);
  assert.equal(r.json.error, 'The drive is verified and mounted. A re-run only acts when it has failed or been lost.');
  assert.equal(fs.existsSync(ok.host.path('run/bootgate/rerun')), false);
});

test('gate re-run: the trigger directory is not writable -> failed with the reason', async (t) => {
  if (process.getuid?.() === 0) return t.skip('root ignores mode bits');
  const d = await startDashboard(t, 'bad-night');
  fs.chmodSync(d.host.path('run/bootgate'), 0o500);
  let a;
  try {
    a = await finished(d, (await postJson(d.base, '/api/gate/rerun', {})).json.action.id);
  } finally {
    fs.chmodSync(d.host.path('run/bootgate'), 0o700);
  }
  assert.equal(a.state, 'failed');
  assert.equal(a.error, "Couldn't request the re-run (EACCES).");
});
