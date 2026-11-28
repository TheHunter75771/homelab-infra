// Host restart and media-app updates (CONTRACTS §11.7.3): dialog content, the step mapping, the summary's
// hostActions and the action flows (no network: fetch and SSE are stubbed).
import { test, beforeEach, after } from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from '../public/js/store.js';
import {
  createActions,
  dialogContent,
  STEP_INDEX,
  MEDIA_STEP_LABEL,
  MAJOR_NOTE,
  AHEAD_NOTE,
  applyMediaStep,
  mediaRowStatus,
  versionOf,
  defaultSelection,
  selectedApps,
} from '../public/js/actions.js';
import { buildView, hostActionsView, HOST_ACTIONS_MISSING } from '../public/js/model.js';
import { rebootLock } from '../public/js/screens/overview.js';
import { mediaUpdateLock } from '../public/js/screens/apps.js';
import { normal, badNight, clean, mediaCheck, SCENARIOS } from './fixtures.mjs';
import { setTimeZone } from '../public/js/format.js';

beforeEach(() => setTimeZone('UTC'));
after(() => setTimeZone(null));

const NOW = Date.parse('2026-10-24T14:08:00Z');
const ev = (type, data) => ({ type, data: JSON.stringify(data) });

function harness(summary = normal(NOW), routes = {}) {
  const store = createStore();
  const sent = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (path, opts = {}) => {
    sent.push({ path, method: opts.method || 'GET', headers: opts.headers || {}, body: opts.body ? JSON.parse(opts.body) : undefined });
    const r = routes[`${opts.method || 'GET'} ${path}`] || { status: 404, body: { error: 'not found' } };
    const res = typeof r === 'function' ? r() : r;
    return new Response(JSON.stringify(res.body), { status: res.status, headers: { 'Content-Type': 'application/json' } });
  };
  const streams = {};
  const followed = [];
  const actions = createActions(store, {
    now: () => NOW,
    timers: { setInterval: () => 1, clearInterval: () => {} },
    live: { refresh: async () => {} },
    streamSSE: async (path, { onEvent }) => {
      followed.push(path);
      for (const e of streams[path] || []) onEvent(e);
    },
    push: { forgetLocalSubscription: async () => {}, currentDeviceId: async () => null },
  });
  store.dispatch({ type: 'summary', summary, at: NOW });
  return { store, actions, sent, streams, followed, restore: () => (globalThis.fetch = realFetch) };
}

// --- reboot dialog -----------------------------------------------------------------------------

test('reboot dialog: title, body, confirm label, fail tone and icon', () => {
  const c = dialogContent({ kind: 'reboot', phase: 'confirm' }, { view: buildView(clean(NOW), { now: NOW }) });
  assert.equal(c.title, 'Restart the media server?');
  assert.match(c.body, /Jellyfin, requests, downloads and this dashboard/);
  assert.match(c.body, /about 2–5 minutes/);
  assert.match(c.body, /boot gate checks the media drive before the apps start/);
  assert.equal(c.confirm, 'Restart host');
  assert.equal(c.tone, 'fail');
  assert.equal(c.icon, 'arrows-clockwise');
  assert.deepEqual(c.lines, [], 'nobody watching, no pending updates: no extra lines');
});

test('reboot dialog: the watching count line (singular and plural) and the pending-packages line', () => {
  const one = dialogContent({ kind: 'reboot' }, { view: buildView(normal(NOW), { now: NOW }) });
  assert.deepEqual(one.lines.map((l) => l.text), ['1 person is watching right now.']);
  const s = normal(NOW);
  s.media.jellyfin.sessions = [...s.media.jellyfin.sessions, { ...s.media.jellyfin.sessions[0], user: 'Guest 2' }, { ...s.media.jellyfin.sessions[0], user: 'Guest 3' }];
  s.host = { ...s.host, rebootRequired: true, rebootPackages: ['linux-image-generic', 'libc6'] };
  const many = dialogContent({ kind: 'reboot' }, { view: buildView(s, { now: NOW }) });
  assert.deepEqual(many.lines.map((l) => l.text), ['3 people are watching right now.', 'Installs the pending updates: linux-image-generic, libc6.']);
  // reboot needed without a package list; Jellyfin held (no sessions to count)
  const b = badNight(NOW);
  b.host.rebootPackages = [];
  assert.deepEqual(dialogContent({ kind: 'reboot' }, { view: buildView(b, { now: NOW }) }).lines.map((l) => l.text), ['Installs the pending updates.']);
  // every line carries an icon: never colour alone
  for (const l of many.lines) assert.ok(l.icon);
});

// --- media-update dialog -------------------------------------------------------------------------

test('media-update review: one row per app, major unticked by default, no checkbox for current/deprecated/error', () => {
  const check = mediaCheck(NOW);
  const selected = defaultSelection(check);
  assert.deepEqual(selected, { jellyfin: false, sabnzbd: true, sonarr: true }, 'majors start unticked');
  const c = dialogContent({ kind: 'media-update', phase: 'review', check, selected });
  assert.equal(c.title, 'Media app updates');
  const rows = Object.fromEntries(c.rows.map((r) => [r.key, r]));
  assert.deepEqual(c.rows.map((r) => r.key), ['jellyfin', 'jellyseerr', 'prowlarr', 'radarr', 'sabnzbd', 'sonarr']);
  assert.equal(rows.sonarr.versions, '4.0.17.2952-ls312 → 4.0.20.3014-ls7');
  assert.equal(rows.sonarr.checkbox, true);
  assert.equal(rows.sonarr.checked, true);
  assert.equal(rows.sonarr.note, null);
  assert.equal(rows.jellyfin.checkbox, true);
  assert.equal(rows.jellyfin.checked, false);
  assert.equal(rows.jellyfin.note, MAJOR_NOTE);
  assert.equal(MAJOR_NOTE, 'Major version: read the release notes first');
  assert.equal(rows.radarr.checkbox, undefined);
  assert.equal(rows.radarr.status, 'Up to date');
  assert.equal(rows.jellyseerr.checkbox, undefined);
  assert.equal(rows.jellyseerr.status, 'This image is no longer maintained.');
  assert.equal(rows.prowlarr.checkbox, undefined);
  assert.equal(rows.prowlarr.status, "The registry didn't answer in time.");
  for (const r of c.rows.filter((x) => !x.checkbox)) assert.ok(r.icon && r.status, `${r.key}: status has an icon and a word`);
  assert.equal(c.confirm, 'Update 2 apps');
  assert.equal(c.confirmReason, null);
  assert.equal(c.secondary.label, 'Check again');
});

test('media-update review: "Update N apps" follows the ticks and is disabled at 0; loading and errors', () => {
  const check = mediaCheck(NOW);
  const d = { kind: 'media-update', phase: 'review', check, selected: { sonarr: true, sabnzbd: false, jellyfin: false } };
  assert.equal(dialogContent(d).confirm, 'Update 1 app');
  assert.deepEqual(selectedApps(d), ['sonarr']);
  const none = { ...d, selected: { sonarr: false } };
  assert.equal(dialogContent(none).confirm, 'Update 0 apps');
  assert.ok(dialogContent(none).confirmReason, 'disabled with a reason at 0');
  // a tick on a row that isn't an update is never sent
  assert.deepEqual(selectedApps({ ...d, selected: { radarr: true, prowlarr: true } }), []);
  const loading = dialogContent({ kind: 'media-update', phase: 'review', loading: true });
  assert.ok(loading.confirmReason);
  assert.deepEqual(loading.rows, []);
  const err = dialogContent({ kind: 'media-update', phase: 'review', error: 'the request timed out' });
  assert.equal(err.body, "Couldn't check for updates: the request timed out.");
  const all = dialogContent({ kind: 'media-update', phase: 'review', check: { apps: check.apps.filter((a) => a.status === 'current') }, selected: {} });
  assert.equal(all.body, 'Every media app is on its newest stable release.');
});

test('media-update step mapping: per-app rows from the raw step events', () => {
  assert.deepEqual(MEDIA_STEP_LABEL, {
    pulling: 'Pulling',
    pinning: 'Saving the new version',
    recreating: 'Recreating',
    'waiting-health': 'Waiting for health check',
    'rolling-back': 'Rolling back',
  });
  const events = [
    { step: 'app', app: 'sonarr', index: 1, total: 2, from: '4.0.17.2952-ls312', to: 'lscr.io/linuxserver/sonarr:4.0.20.3014-ls7@sha256:' + 'c'.repeat(64) },
    { step: 'pulling', app: 'sonarr' },
  ];
  let rows = [];
  for (const e of events) rows = applyMediaStep(rows, e);
  assert.equal(rows.length, 1);
  assert.deepEqual([rows[0].state, rows[0].from, rows[0].to, mediaRowStatus(rows[0])], ['running', '4.0.17.2952-ls312', '4.0.20.3014-ls7', 'Pulling']);
  rows = applyMediaStep(rows, { step: 'pinning', app: 'sonarr' });
  assert.equal(mediaRowStatus(rows[0]), 'Saving the new version');
  rows = applyMediaStep(rows, { step: 'waiting-health', app: 'sonarr' });
  assert.equal(mediaRowStatus(rows[0]), 'Waiting for health check');
  rows = applyMediaStep(rows, { step: 'app-done', app: 'sonarr' });
  assert.equal(mediaRowStatus(rows[0]), 'Updated');
  rows = applyMediaStep(rows, { step: 'app', app: 'radarr', index: 2, total: 2, from: 'a', to: 'b' });
  rows = applyMediaStep(rows, { step: 'rolling-back', app: 'radarr' });
  assert.equal(mediaRowStatus(rows[1]), 'Rolling back');
  rows = applyMediaStep(rows, { step: 'app-failed', app: 'radarr', rolledBack: true });
  assert.deepEqual(rows.map((r) => [r.name, r.state, mediaRowStatus(r)]), [
    ['sonarr', 'done', 'Updated'],
    ['radarr', 'failed', 'Failed, rolled back'],
  ]);
  // the action's own steps (no app) and unknown steps leave the rows alone
  assert.equal(applyMediaStep(rows, { step: 'done' }), rows);
  assert.equal(applyMediaStep(rows, { step: 'something-new', app: 'sonarr' }), rows);
  // progress content: a done/failed mark per row, never colour alone
  const c = dialogContent({ kind: 'media-update', phase: 'progress', progress: rows });
  assert.equal(c.title, 'Updating media apps…');
  assert.deepEqual(c.rows.map((r) => [r.status, r.icon, r.tone]), [
    ['Updated', 'check-circle', 'ok'],
    ['Failed, rolled back', 'x-circle', 'fail'],
  ]);
  assert.equal(c.confirm, undefined, 'no confirm while it runs');
  assert.deepEqual(STEP_INDEX.reboot, { requested: 0, scheduled: 1, done: 2 });
});

test('media-update review: `ahead` renders like current with its message, info tone, no checkbox, never counted', () => {
  const check = mediaCheck(NOW);
  check.apps.push(
    { name: 'sonarr-4k', label: 'Sonarr 4K', current: { version: '4.0.21-ls8' }, latest: { version: '4.0.20-ls7' }, status: 'ahead', major: false, message: "Pinned 4.0.21-ls8 is newer than the registry's latest (4.0.20-ls7)." },
    { name: 'radarr-4k', label: 'Radarr 4K', current: { version: '6.2.0-ls1' }, latest: { version: '6.1.1-ls303' }, status: 'ahead', major: false, message: null },
  );
  assert.equal(defaultSelection(check)['sonarr-4k'], undefined, 'not ticked by default');
  const d = { kind: 'media-update', phase: 'review', check, selected: { ...defaultSelection(check), 'sonarr-4k': true, 'radarr-4k': true } };
  const c = dialogContent(d);
  const rows = Object.fromEntries(c.rows.map((r) => [r.key, r]));
  assert.deepEqual(
    [rows['sonarr-4k'].checkbox, rows['sonarr-4k'].status, rows['sonarr-4k'].tone, rows['sonarr-4k'].versions],
    [undefined, "Pinned 4.0.21-ls8 is newer than the registry's latest (4.0.20-ls7).", 'info', '4.0.21-ls8'],
  );
  assert.equal(rows['radarr-4k'].status, AHEAD_NOTE, 'fallback text');
  assert.equal(AHEAD_NOTE, "The pinned version is newer than the registry's latest release.");
  assert.ok(rows['radarr-4k'].icon, 'never colour alone');
  assert.deepEqual(selectedApps(d), ['sabnzbd', 'sonarr'], 'a tick on an ahead row is never sent');
  assert.equal(c.confirm, 'Update 2 apps');
});

test('versionOf: a pin shows its tag, a version stays as it is', () => {
  assert.equal(versionOf('lscr.io/linuxserver/sonarr:4.0.20.3014-ls7@sha256:' + 'c'.repeat(64)), '4.0.20.3014-ls7');
  assert.equal(versionOf('4.0.20.3014-ls7'), '4.0.20.3014-ls7');
  assert.equal(versionOf({ version: '1.2.3', pin: 'x/y:9@sha256:abc' }), '1.2.3');
  assert.equal(versionOf({ pin: 'registry.example:5000/x/y:9@sha256:abc' }), '9');
  assert.equal(versionOf(null), '');
});

// --- summary hostActions --------------------------------------------------------------------------

test('hostActions: the server\'s locks; a server without the field keeps both locked; remote is tailnet-only', () => {
  assert.deepEqual(hostActionsView(normal(NOW)), { reboot: { lock: null }, mediaUpdate: { lock: null, running: null } });
  const old = normal(NOW);
  delete old.hostActions;
  assert.deepEqual(hostActionsView(old), { reboot: { lock: HOST_ACTIONS_MISSING }, mediaUpdate: { lock: HOST_ACTIONS_MISSING, running: null } });
  const remote = SCENARIOS.remote(NOW);
  remote.hostActions = { reboot: { lock: null }, mediaUpdate: { lock: null, running: null } };
  assert.equal(hostActionsView(remote).reboot.lock, 'Only available on the tailnet');
  assert.equal(hostActionsView(remote).mediaUpdate.lock, 'Only available on the tailnet');
  assert.equal(hostActionsView(SCENARIOS['just-booted'](NOW)).reboot.lock, 'The box just started. Try again in 7 min.');
  assert.equal(hostActionsView(badNight(NOW)).mediaUpdate.lock, 'Held by the boot gate until the media drive is back.');
  const running = normal(NOW);
  running.hostActions.mediaUpdate.running = 'abc123';
  assert.equal(hostActionsView(running).mediaUpdate.running, 'abc123');
  // paused (backend unreachable) wins over the server's (old) lock
  const v = buildView(normal(NOW), { now: NOW });
  assert.equal(rebootLock(v, { paused: 'Backend unreachable. Actions are paused' }), 'Backend unreachable. Actions are paused');
  assert.equal(mediaUpdateLock(v, {}), null);
});

// --- flows -----------------------------------------------------------------------------------------

test('reboot: POST /api/host/reboot with X-Homelab, follows the action, toasts the server\'s detail', async () => {
  const h = harness(normal(NOW), { 'POST /api/host/reboot': { status: 202, body: { ok: true, action: { id: 'r1', kind: 'reboot' } } } });
  h.streams['/api/actions/r1'] = [ev('step', { step: 'requested' }), ev('step', { step: 'scheduled' }), ev('end', { state: 'done', detail: 'Restarting in 10 s. The dashboard is back in a few minutes.' })];
  h.actions.ask('reboot');
  const p = h.actions.confirm();
  assert.equal(h.store.getState().dialog.busy, true, 'busy while the request runs');
  h.actions.cancelDialog();
  assert.ok(h.store.getState().dialog, 'no cancel while busy');
  await p;
  h.restore();
  const post = h.sent.find((x) => x.method === 'POST');
  assert.equal(post.path, '/api/host/reboot');
  assert.equal(post.headers['X-Homelab'], '1');
  assert.equal(h.store.getState().dialog, null);
  assert.deepEqual([h.store.getState().toast.tone, h.store.getState().toast.text], ['ok', 'Restarting in 10 s. The dashboard is back in a few minutes.']);
});

test('reboot: a refusal (409 too soon, 403 remote) is reported', async () => {
  const h = harness(normal(NOW), { 'POST /api/host/reboot': { status: 409, body: { error: 'The box just started. Try again in 7 min.' } } });
  h.actions.ask('reboot');
  await h.actions.confirm();
  h.restore();
  assert.equal(h.store.getState().toast.text, "Couldn't restart the host: The box just started. Try again in 7 min.");
  const r = harness(normal(NOW), { 'POST /api/host/reboot': { status: 403, body: { error: 'Only available on the tailnet' } } });
  r.actions.ask('reboot');
  await r.actions.confirm();
  r.restore();
  assert.equal(r.store.getState().toast.text, "Couldn't restart the host: Only available on the tailnet");
});

test('media update: check, untick, confirm sends the ticked apps in order, progress rows, then the toast', async () => {
  const check = mediaCheck(NOW);
  const h = harness(normal(NOW), {
    'GET /api/media/updates': { status: 200, body: check },
    'GET /api/media/updates?refresh=1': { status: 200, body: { ...check, apps: check.apps.filter((a) => a.name !== 'sabnzbd') } },
    'POST /api/media/update': { status: 202, body: { ok: true, action: { id: 'm1', kind: 'media-update' } } },
  });
  const seen = [];
  h.store.subscribe((s) => s.dialog?.phase === 'progress' && seen.push(JSON.stringify(s.dialog.progress.map((r) => [r.name, r.state, r.step]))));
  h.streams['/api/actions/m1'] = [
    ev('step', { step: 'app', app: 'sabnzbd', index: 1, total: 2, from: '5.0.3-ls255', to: '5.0.4-ls256' }),
    ev('step', { step: 'pulling', app: 'sabnzbd' }),
    ev('step', { step: 'pinning', app: 'sabnzbd' }),
    // a stray per-app "done" must not end the whole action
    ev('step', { step: 'done', app: 'sabnzbd' }),
    ev('step', { step: 'app-done', app: 'sabnzbd' }),
    ev('step', { step: 'app', app: 'jellyfin', index: 2, total: 2, from: 'a', to: 'b' }),
    ev('step', { step: 'app-done', app: 'jellyfin' }),
    ev('end', { state: 'done', detail: 'Updated 2 apps: SABnzbd 5.0.4-ls256, Jellyfin 12.0.1ubu2404-ls1' }),
  ];
  await h.actions.checkMediaUpdates();
  let d = h.store.getState().dialog;
  assert.equal(d.kind, 'media-update');
  assert.equal(d.phase, 'review');
  assert.equal(dialogContent(d).confirm, 'Update 2 apps');
  h.actions.toggleMediaApp('sonarr', false);
  h.actions.toggleMediaApp('jellyfin', true);
  d = h.store.getState().dialog;
  assert.deepEqual(selectedApps(d), ['jellyfin', 'sabnzbd']);
  h.actions.cancelDialog();
  assert.equal(h.store.getState().dialog, null, 'review can be cancelled');
  await h.actions.checkMediaUpdates();
  h.actions.toggleMediaApp('sonarr', false);
  h.actions.toggleMediaApp('jellyfin', true);
  await h.actions.secondary('recheck');
  assert.ok(h.sent.some((x) => x.path === '/api/media/updates?refresh=1'), 'Check again re-runs with refresh=1');
  assert.deepEqual(selectedApps(h.store.getState().dialog), ['sonarr'], 'a new check resets the ticks to the defaults');
  h.actions.toggleMediaApp('sonarr', false);
  h.actions.toggleMediaApp('jellyfin', true);
  h.store.dispatch({ type: 'dialog/patch', patch: { check } });
  h.actions.toggleMediaApp('sabnzbd', true);
  await h.actions.confirm();
  h.restore();
  const post = h.sent.find((x) => x.method === 'POST');
  assert.equal(post.path, '/api/media/update');
  assert.deepEqual(post.body, { apps: ['jellyfin', 'sabnzbd'] });
  assert.equal(post.headers['X-Homelab'], '1');
  assert.ok(seen.length > 0, 'progress phase shown');
  assert.ok(seen.some((x) => x.includes('"sabnzbd","running","pinning"')));
  assert.equal(h.store.getState().dialog, null);
  assert.deepEqual([h.store.getState().toast.tone, h.store.getState().toast.text], ['ok', 'Updated 2 apps: SABnzbd 5.0.4-ls256, Jellyfin 12.0.1ubu2404-ls1']);
});

test('media update: no cancel during progress; failure toasts the reason', async () => {
  const check = mediaCheck(NOW);
  const h = harness(normal(NOW), {
    'GET /api/media/updates': { status: 200, body: check },
    'POST /api/media/update': { status: 202, body: { ok: true, action: { id: 'm2' } } },
  });
  let cancelTried = false;
  h.store.subscribe((s) => {
    if (s.dialog?.phase === 'progress' && !cancelTried) {
      cancelTried = true;
      h.actions.cancelDialog();
    }
  });
  h.streams['/api/actions/m2'] = [
    ev('step', { step: 'app', app: 'sonarr', index: 1, total: 1, from: 'a', to: 'b' }),
    ev('step', { step: 'rolling-back', app: 'sonarr' }),
    ev('step', { step: 'app-failed', app: 'sonarr', rolledBack: true }),
    ev('end', { state: 'failed', error: 'Sonarr failed its health check and was rolled back to 4.0.17.2952-ls312.' }),
  ];
  await h.actions.checkMediaUpdates();
  await h.actions.confirm();
  h.restore();
  assert.ok(cancelTried);
  assert.deepEqual([h.store.getState().toast.tone, h.store.getState().toast.text], ['fail', 'Sonarr failed its health check and was rolled back to 4.0.17.2952-ls312.']);
});

test('media update: a stale check (409) keeps the review open and says so', async () => {
  const h = harness(normal(NOW), {
    'GET /api/media/updates': { status: 200, body: mediaCheck(NOW) },
    'POST /api/media/update': { status: 409, body: { error: 'Check for updates again' } },
  });
  await h.actions.checkMediaUpdates();
  await h.actions.confirm();
  h.restore();
  const d = h.store.getState().dialog;
  assert.equal(d.phase, 'review');
  assert.equal(d.busy, false);
  assert.equal(h.store.getState().toast.text, "Couldn't start the update: Check for updates again");
});

test('media update: one already running when the page loads is followed again, once', async () => {
  const s = normal(NOW);
  s.hostActions.mediaUpdate = { lock: 'Busy', running: 'm9' };
  const store = createStore();
  const streams = {
    '/api/actions/m9': [
      ev('step', { step: 'app', app: 'radarr', index: 1, total: 1, from: 'a', to: 'b' }),
      ev('step', { step: 'recreating', app: 'radarr' }),
      ev('step', { step: 'app-done', app: 'radarr' }),
      ev('end', { state: 'done', detail: 'Updated 1 app: Radarr b' }),
    ],
  };
  const followed = [];
  const progress = [];
  createActions(store, {
    now: () => NOW,
    timers: { setInterval: () => 1, clearInterval: () => {} },
    live: { refresh: async () => {} },
    streamSSE: async (path, { onEvent }) => {
      followed.push(path);
      await new Promise((r) => setImmediate(r));
      for (const e of streams[path] || []) onEvent(e);
    },
  });
  store.subscribe((st) => st.dialog?.phase === 'progress' && progress.push(st.dialog.progress.map((r) => mediaRowStatus(r)).join()));
  store.dispatch({ type: 'summary', summary: s, at: NOW });
  assert.equal(store.getState().dialog.kind, 'media-update');
  assert.equal(store.getState().dialog.phase, 'progress');
  store.dispatch({ type: 'summary', summary: { ...s, generatedAt: 'later' }, at: NOW + 1 });
  for (let i = 0; i < 5; i += 1) await new Promise((r) => setImmediate(r));
  assert.deepEqual(followed, ['/api/actions/m9'], 'followed once');
  assert.ok(progress.includes('Recreating'));
  assert.equal(store.getState().dialog, null);
  assert.equal(store.getState().toast.text, 'Updated 1 app: Radarr b');
  store.dispatch({ type: 'summary', summary: { ...s, generatedAt: 'later still' }, at: NOW + 2 });
  assert.equal(store.getState().dialog, null, 'never re-followed after it ended');
});
