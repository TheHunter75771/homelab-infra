// Reducer, SSE parser, live connection manager and action flows (no network: fetch and SSE are stubbed).
import { test, beforeEach, after } from 'node:test';
import assert from 'node:assert/strict';
import { reduce, initialState, createStore, actionsPausedReason, serverStale, SERVER_STALE_MS } from '../public/js/store.js';
import { createSSEParser } from '../public/js/api.js';
import { createLive, POLL_MS, CONTACT_TIMEOUT_MS } from '../public/js/live.js';
import { createActions, dialogContent, STEP_INDEX } from '../public/js/actions.js';
import { parseLogEvent, filterLines, levelOf } from '../public/js/logs.js';
import { parseHash, toHash, parentOf, titleOf } from '../public/js/route.js';
import { normal, badNight } from './fixtures.mjs';
import { setTimeZone } from '../public/js/format.js';

beforeEach(() => setTimeZone('UTC'));
after(() => setTimeZone(null));

const NOW = Date.parse('2026-10-24T14:08:00Z');
const tick = () => new Promise((r) => setImmediate(r));
const settle = async (n = 5) => {
  for (let i = 0; i < n; i += 1) await tick();
};

// --- reducer -----------------------------------------------------------------------------------

test('reducer: connection states connecting → live → stale, and the offline snapshot', () => {
  let s = initialState;
  assert.equal(s.connection, 'connecting');
  s = reduce(s, { type: 'unreachable', at: 1 });
  assert.equal(s.connection, 'offline', 'nothing on screen yet: offline screen');
  s = reduce(s, { type: 'snapshot', snapshot: { savedAt: 5, summary: { a: 1 } } });
  assert.deepEqual([s.summary, s.snapshotAt, s.dataAt], [{ a: 1 }, 5, 5]);
  s = reduce(s, { type: 'contact', at: 9 });
  assert.equal(s.connection, 'offline', 'a ping alone does not revive snapshot data');
  s = reduce(s, { type: 'summary', summary: { b: 2 }, at: 10 });
  assert.deepEqual([s.connection, s.snapshotAt, s.receivedAt, s.dataAt], ['live', null, 10, 10]);
  s = reduce(s, { type: 'contact', at: 20 });
  assert.deepEqual([s.receivedAt, s.dataAt], [20, 10], 'contact refreshes "N s ago", not the data time');
  s = reduce(s, { type: 'unreachable', at: 30 });
  assert.equal(s.connection, 'stale', 'live data on screen: stale banner, not the offline screen');
  assert.equal(reduce(s, { type: 'snapshot', snapshot: { savedAt: 1, summary: {} } }), s, 'a snapshot never replaces session data');
  assert.equal(actionsPausedReason(s), 'Backend unreachable. Actions are paused');
  s = reduce(s, { type: 'summary', summary: { c: 3 }, at: 40 });
  assert.equal(actionsPausedReason(s), null);
  assert.equal(reduce(s, { type: 'summary', summary: 'junk' }), s);
});

test('reducer: "Live" turns stale when the server stops refreshing, even while pings arrive', () => {
  const g = (sec) => new Date(Date.UTC(2026, 9, 24, 14, 8, sec)).toISOString();
  let s = reduce(initialState, { type: 'summary', summary: { generatedAt: g(0) }, at: 1000 });
  assert.equal(s.serverAtSeenAt, 1000);
  s = reduce(s, { type: 'contact', at: 11000, generatedAt: g(10) }); // a tick: the server refreshed
  assert.equal(s.serverAtSeenAt, 11000);
  s = reduce(s, { type: 'contact', at: 21000 }); // a ping: connection only
  s = reduce(s, { type: 'contact', at: 31000, generatedAt: g(10) }); // a 304 with the same generatedAt
  assert.equal(s.serverAtSeenAt, 11000, 'only an advance counts');
  assert.equal(serverStale(s, 11000 + SERVER_STALE_MS - 1), false);
  assert.equal(serverStale(s, 11000 + SERVER_STALE_MS + 1), true, 'pings alone don\'t keep it Live');
  assert.equal(s.connection, 'live');
  s = reduce(s, { type: 'contact', at: 60000, generatedAt: g(50) });
  assert.equal(serverStale(s, 61000), false, 'fresh again on the next refresh');
  // skew: the server clock may be far off; only the advance, timed on the client, matters
  let k = reduce(initialState, { type: 'summary', summary: { generatedAt: '2030-01-01T00:00:00Z' }, at: 5 });
  k = reduce(k, { type: 'contact', at: 6, generatedAt: '2030-01-01T00:00:10Z' });
  assert.equal(serverStale(k, 6 + 1000), false);
  assert.equal(serverStale(reduce(initialState, { type: 'summary', summary: {}, at: 5 }), 1e9), false, 'no generatedAt: no judgement');
});

test('reducer: pending, dialog, toast, settings, devices, route', () => {
  let s = initialState;
  s = reduce(s, { type: 'pending/set', name: 'radarr', value: { kind: 'restart' } });
  assert.deepEqual(s.pending, { radarr: { kind: 'restart' } });
  s = reduce(s, { type: 'pending/clear', name: 'radarr' });
  assert.deepEqual(s.pending, {});
  assert.equal(reduce(s, { type: 'pending/clear', name: 'radarr' }), s);
  s = reduce(s, { type: 'dialog', dialog: { kind: 'stop', app: 'sonarr', phase: 'confirm' } });
  s = reduce(s, { type: 'dialog/patch', patch: { busy: true } });
  assert.deepEqual(s.dialog, { kind: 'stop', app: 'sonarr', phase: 'confirm', busy: true });
  s = reduce(s, { type: 'toast', tone: 'ok', text: 'a' });
  const id = s.toast.id;
  s = reduce(s, { type: 'toast', tone: 'ok', text: 'b' });
  assert.notEqual(s.toast.id, id);
  assert.equal(reduce(s, { type: 'toast/clear', id }).toast.text, 'b', 'an old timer does not clear a newer toast');
  assert.equal(reduce(s, { type: 'toast/clear', id: s.toast.id }).toast, null);
  s = reduce(s, { type: 'devices', devices: [{ id: 'x' }] });
  assert.deepEqual(s.devices, [{ id: 'x' }]);
  assert.equal(reduce(s, { type: 'devices', devices: 'nope' }), s);
  s = reduce(s, { type: 'route', route: { name: 'app', id: 'sonarr' } });
  assert.deepEqual(s.route, { name: 'app', id: 'sonarr' });
  const store = createStore();
  let calls = 0;
  const off = store.subscribe(() => (calls += 1));
  store.dispatch({ type: 'route', route: { name: 'apps' } });
  store.dispatch({ type: 'nothing' });
  off();
  store.dispatch({ type: 'route', route: { name: 'drive' } });
  assert.equal(calls, 1, 'no-op actions and unsubscribed listeners are not called');
});

test('routes: hash ⇄ route, back targets, titles', () => {
  assert.deepEqual(parseHash(''), { name: 'overview' });
  assert.deepEqual(parseHash('#/apps'), { name: 'apps' });
  assert.deepEqual(parseHash('#/apps/wol-relay'), { name: 'app', id: 'wol-relay' });
  assert.deepEqual(parseHash('#/apps/%E0%A4'), { name: 'app', id: '%E0%A4' }, 'bad escapes do not throw');
  assert.deepEqual(parseHash('#/nope'), { name: 'overview' });
  assert.deepEqual(parseHash('#/app'), { name: 'overview' }, '"app" needs an id');
  assert.equal(toHash({ name: 'app', id: 'a b' }), '#/apps/a%20b');
  assert.equal(toHash({ name: 'overview' }), '#/');
  assert.deepEqual(parentOf({ name: 'app', id: 'x' }, false), { name: 'apps' });
  assert.deepEqual(parentOf({ name: 'drive' }, true), { name: 'more' }, 'mobile: under More');
  assert.equal(parentOf({ name: 'drive' }, false), null, 'desktop: sidebar item');
  assert.equal(titleOf({ name: 'uptime' }), 'Uptime & drills');
  assert.equal(titleOf({ name: 'app', id: 'radarr' }, 'Radarr'), 'Radarr');
});

// --- SSE ---------------------------------------------------------------------------------------

test('SSE parser: events, ids, comments, multi-line data, any line ending, split chunks', () => {
  const got = [];
  const p = createSSEParser((e) => got.push(e));
  p.push('retry: 10000\n\n');
  p.push('event: summary\nid: abc\ndata: {"a":');
  p.push('1}\n\n: ping\n\n');
  p.push('data: line one\r\ndata: line two\r');
  p.push('\n\r\n');
  p.push('event: end\ndata:{"state":"done"}\n\n');
  p.push('data: tail-without-blank-line');
  p.end();
  assert.deepEqual(got, [
    { type: 'summary', data: '{"a":1}', id: 'abc' },
    { type: 'comment', data: 'ping', id: 'abc' },
    { type: 'message', data: 'line one\nline two', id: 'abc' },
    { type: 'end', data: '{"state":"done"}', id: 'abc' },
    { type: 'message', data: 'tail-without-blank-line', id: 'abc' },
  ]);
});

test('log events: the server\'s `lines` arrays, `end`, and lenient single lines', () => {
  const r = parseLogEvent({
    type: 'lines',
    data: JSON.stringify([
      { ts: '2026-10-24T14:00:01Z', level: 'WRN', msg: 'slow', stream: 'stdout' },
      { ts: null, level: 'nope', msg: '2026-10-24T14:00:02.123456789Z [Error] boom' },
    ]),
  });
  assert.deepEqual(r.lines.map((l) => [l.t, l.lv, l.m]), [
    ['14:00:01', 'WRN', 'slow'],
    ['14:00:02', 'ERR', '[Error] boom'],
  ]);
  assert.deepEqual(parseLogEvent({ type: 'end', data: '{"reason":"error","message":"Docker isn\'t running."}' }).end, { reason: 'error', message: "Docker isn't running." });
  assert.deepEqual(parseLogEvent({ type: 'comment', data: 'ping' }), { lines: [], end: null });
  assert.equal(parseLogEvent({ type: 'message', data: 'plain DBG text' }).lines[0].lv, 'DBG');
  assert.equal(levelOf('2026-10-24 14:00:00,123::INFO::[x] ok'), 'INF');
  assert.equal(levelOf('[Warn] RssSync: nothing'), 'WRN');
  assert.equal(levelOf('time="x" level=error msg="y"'), 'ERR');
  assert.equal(levelOf(`${'x'.repeat(100)} ERROR late`), 'INF', 'only the head of the line counts');
  const lines = [{ t: '1', lv: 'ERR', m: 'Indexer C failed' }, { t: '2', lv: 'INF', m: 'ok' }];
  assert.deepEqual(filterLines(lines, ' err '), [lines[0]]);
  assert.equal(filterLines(lines, '').length, 2);
});

// --- live connection ------------------------------------------------------------------------------

function fakeTimers() {
  let fn = null;
  return { setInterval: (f) => ((fn = f), 1), clearInterval: () => (fn = null), fire: () => fn?.() };
}

function liveHarness({ summary = { n: 1 }, sse } = {}) {
  const store = createStore();
  const calls = [];
  let fail = false;
  let etag = '"e1"';
  let current = summary;
  const timers = fakeTimers();
  let clock = NOW;
  const saved = [];
  let stored = null;
  const api = {
    async getJSON(path, { etag: sent } = {}) {
      calls.push(['get', path, sent]);
      if (fail) throw new TypeError('fetch failed');
      if (sent === etag) return { status: 304, data: null, etag };
      return { status: 200, data: current, etag };
    },
    streamSSE: sse || (() => new Promise(() => {})),
  };
  const live = createLive(store, {
    api,
    timers,
    now: () => clock,
    snapshot: { save: async (s, at) => saved.push([s, at]), load: async () => stored },
  });
  return {
    store,
    live,
    calls,
    saved,
    timers,
    setFail: (v) => (fail = v),
    setSummary: (s, e) => ((current = s), (etag = e)),
    setStored: (v) => (stored = v),
    advance: (ms) => (clock += ms),
  };
}

test('live: first GET, then the SSE stream; summary, tick and ping events count as contact', async () => {
  let emit;
  let openCb;
  let finish;
  const h = liveHarness({
    sse: (path, { onEvent, onOpen }) => {
      assert.equal(path, '/api/events');
      emit = onEvent;
      openCb = onOpen;
      return new Promise((resolve) => (finish = resolve));
    },
  });
  h.live.start();
  await settle();
  assert.deepEqual(h.calls, [['get', '/api/summary', null]]);
  assert.equal(h.store.getState().connection, 'live');
  assert.deepEqual(h.store.getState().summary, { n: 1 });
  openCb();
  emit({ type: 'summary', data: JSON.stringify({ n: 2 }) });
  assert.deepEqual(h.store.getState().summary, { n: 2 });
  h.advance(5000);
  emit({ type: 'tick', data: '{"generatedAt":"x"}' });
  assert.equal(h.store.getState().receivedAt, NOW + 5000);
  emit({ type: 'comment', data: 'ping' });
  // while the stream is healthy the 10 s timer doesn't poll
  h.timers.fire();
  await settle();
  assert.equal(h.calls.length, 1);
  assert.equal(h.live.streaming, true);
  // stream drops: immediate check, then polling every tick until it reconnects
  finish();
  await settle();
  assert.equal(h.live.streaming, false);
  assert.equal(h.calls.length, 2, 'checked right away when the stream ended');
  assert.equal(h.calls[1][2], '"e1"', 'with the ETag');
  h.live.stop();
});

test('live: a silent stream is dropped after the contact timeout and polling takes over', async () => {
  let aborted = false;
  const h = liveHarness({
    sse: (path, { onOpen, signal }) => {
      onOpen();
      return new Promise((resolve) => signal.addEventListener('abort', () => ((aborted = true), resolve())));
    },
  });
  h.live.start();
  await settle();
  h.advance(CONTACT_TIMEOUT_MS + 1);
  h.timers.fire();
  await settle();
  assert.equal(aborted, true);
  assert.equal(h.live.streaming, false);
  h.live.stop();
});

test('live: unreachable → stale with session data; offline with the saved snapshot on a cold start', async () => {
  const sseDown = () => Promise.reject(new TypeError('fetch failed'));
  const h = liveHarness({ sse: sseDown });
  h.live.start();
  await settle();
  assert.equal(h.store.getState().connection, 'live');
  assert.equal(h.saved.length, 1, 'first summary saved for the offline screen');
  h.setFail(true);
  h.advance(POLL_MS);
  h.timers.fire();
  await settle();
  assert.equal(h.store.getState().connection, 'stale');
  assert.deepEqual(h.store.getState().summary, { n: 1 }, 'last-known data stays');
  h.setFail(false);
  h.setSummary({ n: 3 }, '"e3"');
  h.advance(POLL_MS);
  h.timers.fire();
  await settle();
  assert.equal(h.store.getState().connection, 'live', 'back by itself on the next poll');
  assert.deepEqual(h.store.getState().summary, { n: 3 });
  assert.equal(h.saved.length, 2, '20 s after the first save');
  h.setSummary({ n: 4 }, '"e4"');
  await h.live.refresh();
  assert.equal(h.saved.length, 2, 'snapshots are throttled to one per 15 s');
  h.advance(16000);
  h.setSummary({ n: 5 }, '"e5"');
  await h.live.refresh();
  assert.equal(h.saved.length, 3);
  assert.deepEqual(h.saved[2], [{ n: 5 }, NOW + 2 * POLL_MS + 16000]);
  h.live.stop();

  const cold = liveHarness({ sse: sseDown });
  cold.setFail(true);
  cold.setStored({ savedAt: NOW - 3600e3, summary: { n: 'snap' } });
  cold.live.start();
  await settle();
  const st = cold.store.getState();
  assert.equal(st.connection, 'offline');
  assert.deepEqual(st.summary, { n: 'snap' });
  assert.equal(st.snapshotAt, NOW - 3600e3);
  cold.setFail(false);
  await cold.live.refresh();
  assert.equal(cold.store.getState().connection, 'live', '"Try again" works');
  assert.equal(cold.store.getState().snapshotAt, null);
  cold.live.stop();
});

test('live: 304 Not Modified keeps the summary and counts as contact', async () => {
  const h = liveHarness({ sse: () => Promise.reject(new Error('no sse')) });
  h.live.start();
  await settle();
  const first = h.store.getState().summary;
  h.advance(POLL_MS);
  h.timers.fire();
  await settle();
  assert.equal(h.store.getState().summary, first);
  assert.equal(h.store.getState().receivedAt, NOW + POLL_MS);
  h.live.stop();
});

// --- actions ------------------------------------------------------------------------------------

function actionHarness(summary = normal(NOW), routes = {}) {
  const store = createStore();
  store.dispatch({ type: 'summary', summary, at: NOW });
  const sent = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (path, opts = {}) => {
    sent.push({ path, method: opts.method || 'GET', headers: opts.headers || {}, body: opts.body ? JSON.parse(opts.body) : undefined });
    const key = `${opts.method || 'GET'} ${path}`;
    const r = routes[key] || { status: 404, body: { error: 'not found' } };
    const res = typeof r === 'function' ? r() : r;
    return new Response(JSON.stringify(res.body), { status: res.status, headers: { 'Content-Type': 'application/json' } });
  };
  const streams = {};
  const timers = { setInterval: () => 1, clearInterval: () => {} };
  const pushCalls = [];
  const actions = createActions(store, {
    now: () => NOW,
    timers,
    live: { refresh: async () => {} },
    streamSSE: async (path, { onEvent }) => {
      for (const ev of streams[path] || []) onEvent(ev);
    },
    push: {
      forgetLocalSubscription: async () => pushCalls.push('forget'),
      currentDeviceId: async () => null,
    },
  });
  return { store, actions, sent, streams, pushCalls, restore: () => (globalThis.fetch = realFetch) };
}

const ev = (type, data) => ({ type, data: JSON.stringify(data) });

test('actions: every POST carries X-Homelab: 1 and a JSON body; restart follows the action stream', async () => {
  const h = actionHarness(normal(NOW), { 'POST /api/apps/sonarr/restart': { status: 202, body: { ok: true, action: { id: 'a1', kind: 'restart', state: 'running', steps: [] } } } });
  h.streams['/api/actions/a1'] = [ev('step', { step: 'restarting' }), ev('step', { step: 'waiting-health' }), ev('end', { state: 'done', error: null, detail: 'Sonarr is healthy again' })];
  const p = h.actions.restart('sonarr');
  assert.equal(h.store.getState().toast.text, 'Restarting Sonarr…');
  assert.equal(h.store.getState().pending.sonarr.kind, 'restart', 'optimistic "Starting" chip');
  await p;
  h.restore();
  const post = h.sent.find((s) => s.method === 'POST');
  assert.equal(post.headers['X-Homelab'], '1');
  assert.equal(post.headers['Content-Type'], 'application/json');
  assert.deepEqual(post.body, {});
  assert.deepEqual(h.store.getState().pending, {});
  assert.deepEqual([h.store.getState().toast.tone, h.store.getState().toast.text], ['ok', 'Sonarr is healthy again']);
});

test('actions: a refused action (409 lock) toasts the reason and clears the optimistic state', async () => {
  const h = actionHarness(normal(NOW), { 'POST /api/apps/sonarr/restart': { status: 409, body: { error: 'Busy' } } });
  await h.actions.restart('sonarr');
  h.restore();
  assert.deepEqual(h.store.getState().pending, {});
  assert.equal(h.store.getState().toast.text, "Couldn't restart Sonarr: Busy");
});

test('actions: update shows the three progress steps, then a success toast', async () => {
  const h = actionHarness(normal(NOW), { 'POST /api/apps/radarr/update': { status: 202, body: { ok: true, action: { id: 'u1' } } } });
  const steps = [];
  h.store.subscribe((s) => s.dialog?.phase === 'progress' && steps.push(s.dialog.step));
  h.streams['/api/actions/u1'] = [ev('step', { step: 'pulling' }), ev('step', { step: 'recreating' }), ev('step', { step: 'waiting-health' }), ev('end', { state: 'done', detail: 'Radarr updated and healthy' })];
  h.actions.ask('update', { app: 'radarr', target: '6.1.1.10360-ls303' });
  assert.equal(dialogContent(h.store.getState().dialog, {}).title, 'Update radarr to 6.1.1.10360-ls303?');
  await h.actions.confirm();
  h.restore();
  assert.deepEqual([...new Set(steps)], [0, 1, 2]);
  assert.equal(h.store.getState().dialog, null);
  assert.equal(h.store.getState().toast.text, 'Radarr updated to 6.1.1.10360-ls303 and healthy');
});

test('actions: update failure and 403 tailnet-only are reported, not swallowed', async () => {
  const h = actionHarness(normal(NOW), { 'POST /api/apps/radarr/update': { status: 202, body: { ok: true, action: { id: 'u2' } } } });
  h.streams['/api/actions/u2'] = [ev('step', { step: 'pulling' }), ev('end', { state: 'failed', error: 'Pull failed: manifest unknown.' })];
  h.actions.ask('update', { app: 'radarr', target: 'x' });
  await h.actions.confirm();
  assert.equal(h.store.getState().toast.text, 'Update failed: Pull failed: manifest unknown');
  h.restore();
  const r = actionHarness(normal(NOW), { 'POST /api/apps/radarr/update': { status: 403, body: { error: 'Only available on the tailnet' } } });
  r.actions.ask('update', { app: 'radarr', target: 'x' });
  await r.actions.confirm();
  r.restore();
  assert.equal(r.store.getState().toast.text, 'Update failed: Only available on the tailnet');
});

test('actions: gate re-run shows the server\'s steps (requested → running) with the corrected copy, and reports the result', async () => {
  const h = actionHarness(badNight(NOW), { 'POST /api/gate/rerun': { status: 202, body: { ok: true, action: { id: 'g1', kind: 'gate-rerun' } } } });
  const steps = [];
  h.store.subscribe((s) => s.dialog?.phase === 'progress' && steps.push(s.dialog.step));
  h.streams['/api/actions/g1'] = [ev('step', { step: 'requested' }), ev('step', { step: 'running' }), ev('end', { state: 'failed', error: 'Check failed: the drive is still not verified. Next automatic retry in 5 min.' })];
  h.actions.ask('rerun');
  const c = dialogContent(h.store.getState().dialog, { gate: { mountPath: '/media' } });
  assert.equal(c.title, 'Re-run the media-drive check?');
  assert.equal(c.body, 'Re-checks the media drive now: detect, verify and mount at /media. Never reboots. Apps stay held until it passes.');
  assert.deepEqual(c.steps, ['Requested', 'Checking the drive (detect, verify, mount /media)'], 'CORRECTIONS 3, amended');
  await h.actions.confirm();
  h.restore();
  assert.deepEqual([...new Set(steps)], [0, 1]);
  assert.deepEqual([h.store.getState().toast.tone, h.store.getState().toast.text], ['fail', 'Check failed: the drive is still not verified. Next automatic retry in 5 min.']);
  assert.deepEqual(STEP_INDEX.rerun, { requested: 0, running: 1, done: 2 });
});

test('actions: stop confirms, removes a device (and this device\'s browser subscription), saves settings as given', async () => {
  const h = actionHarness(normal(NOW), {
    'POST /api/apps/sonarr/stop': { status: 202, body: { ok: true, action: { id: 's1' } } },
    'DELETE /api/push/devices/1111aaaa2222bbbb': { status: 200, body: { ok: true, removed: true, devices: [{ id: 'x', label: 'y' }] } },
    'GET /api/push/devices': { status: 200, body: { devices: [{ id: 'x', label: 'y' }] } },
    'PUT /api/settings': { status: 200, body: { thresholds: { diskWarnPct: 85, backupMaxAgeH: 36 }, subscriptions: [], integrations: {} } },
    'POST /api/test': { status: 429, body: { error: 'wait before sending another test' } },
  });
  h.streams['/api/actions/s1'] = [ev('step', { step: 'stopping' }), ev('end', { state: 'done', detail: 'Sonarr stopped. It stays down until you start it.' })];
  h.actions.ask('stop', { app: 'sonarr' });
  const c = dialogContent(h.store.getState().dialog, { app: { label: 'Sonarr' } });
  assert.equal(c.body, 'Sonarr stays stopped until you start it again. It shows as “stopped on purpose” and won\'t raise alerts.');
  await h.actions.confirm();
  assert.equal(h.store.getState().toast.text, 'Sonarr stopped. It stays down until you start it.');
  h.store.dispatch({ type: 'me', id: '1111aaaa2222bbbb' });
  h.actions.ask('remove', { deviceId: '1111aaaa2222bbbb', label: 'iPhone' });
  assert.equal(dialogContent(h.store.getState().dialog).title, 'Remove iPhone?');
  await h.actions.confirm();
  await settle();
  assert.deepEqual(h.pushCalls, ['forget']);
  assert.equal(h.store.getState().meId, null);
  assert.deepEqual(h.store.getState().devices, [{ id: 'x', label: 'y' }]);
  const ok = await h.actions.saveSettings({ thresholds: { diskWarnPct: 85 } });
  assert.equal(ok, true);
  assert.deepEqual(h.sent.find((s) => s.method === 'PUT').body, { thresholds: { diskWarnPct: 85 } });
  assert.equal(h.store.getState().settings.thresholds.diskWarnPct, 85);
  await h.actions.sendTest();
  h.restore();
  assert.deepEqual([h.store.getState().toast.tone, h.store.getState().toast.text], ['warn', 'Wait a few seconds before sending another test.']);
  for (const s of h.sent.filter((x) => x.method !== 'GET')) assert.equal(s.headers['X-Homelab'], '1', `${s.method} ${s.path}`);
});

test('actions: the dialog can be cancelled while confirming, never during progress', () => {
  const store = createStore();
  const actions = createActions(store, { timers: { setInterval: () => 1, clearInterval: () => {} } });
  actions.ask('stop', { app: 'x' });
  actions.cancelDialog();
  assert.equal(store.getState().dialog, null);
  store.dispatch({ type: 'dialog', dialog: { kind: 'update', app: 'x', phase: 'progress', step: 1 } });
  actions.cancelDialog();
  assert.equal(store.getState().dialog.phase, 'progress');
});
