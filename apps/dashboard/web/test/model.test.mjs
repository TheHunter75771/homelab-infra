// Selectors over the summary: app status and the gate's hold, the issue list, alerts, forecast, history.
import { test, beforeEach, after } from 'node:test';
import assert from 'node:assert/strict';
import * as m from '../public/js/model.js';
import { setTimeZone, dayKey } from '../public/js/format.js';
import { SCENARIOS, NAMES, normal, badNight, mediaLost, clean, troubled, dockerDown, sabDown } from './fixtures.mjs';

const NOW = Date.parse('2026-10-24T14:08:00Z');
const view = (s, opts = {}) => m.buildView(s, { now: NOW, ...opts });
// The fixtures name UTC as the host zone (server.timeZone), so buildView displays in UTC whatever the
// process TZ. Direct calls to the selectors pin the same zone.
beforeEach(() => setTimeZone('UTC'));
after(() => setTimeZone(null));
const strip = (s) => ({ ...s, apps: s.apps.map(({ status, locks, action, ...rest }) => rest) });

test('every fixture builds a view without throwing, with all 8 watched containers', () => {
  for (const [name, make] of Object.entries(SCENARIOS)) {
    const v = view(make(NOW));
    assert.equal(v.apps.length, 8, name);
    assert.deepEqual(
      v.apps.map((a) => a.name),
      ['jellyfin', 'jellyseerr', 'sonarr', 'radarr', 'prowlarr', 'sabnzbd', 'cloudflared', 'wol-relay'],
      `${name}: design order`,
    );
    assert.ok(!v.apps.some((a) => ['dashboard', 'caddy', 'push-core'].includes(a.name)), 'CORRECTIONS 1');
  }
  assert.doesNotThrow(() => m.buildView(null, { now: NOW }));
  assert.doesNotThrow(() => m.buildView({ apps: [{}], gate: 'x', drive: [] }, { now: NOW }));
});

test('gate → app hold mapping (fallback when the server sends no status): CORRECTIONS 2', () => {
  const app = (name, state) => ({ name, state });
  // missing at boot: the gate holds all of Docker → all 8 held
  for (const n of NAMES) assert.equal(m.heldByGate(app(n, null), 'failed', 'inactive'), true, n);
  // lost while running: only the media stack; cloudflared and wol-relay keep running
  const lost = NAMES.filter((n) => m.heldByGate(app(n, ['cloudflared', 'wol-relay'].includes(n) ? 'running' : 'exited'), 'media-lost', 'active'));
  assert.deepEqual(lost.sort(), ['jellyfin', 'jellyseerr', 'prowlarr', 'radarr', 'sabnzbd', 'sonarr']);
  // a media app the watcher hasn't stopped yet is still shown as what it is
  assert.equal(m.heldByGate(app('sonarr', 'running'), 'media-lost', 'active'), false);
  // the server's mediaGated flag wins over the built-in stack list
  assert.equal(m.heldByGate({ name: 'cloudflared', state: 'exited', mediaGated: true }, 'media-lost', 'active'), true);
  // waiting: Docker waits for the gate
  assert.equal(m.heldByGate(app('jellyfin', null), 'waiting', 'inactive'), true);
  assert.equal(m.heldByGate(app('jellyfin', 'running'), 'ok', 'active'), false);
  // through appStatus, without server statuses
  const bad = view(strip(badNight(NOW)));
  assert.ok(bad.apps.every((a) => a.statusKey === 'held'), 'bad night: all held');
  const ml = view(strip(mediaLost(NOW)));
  assert.deepEqual(ml.apps.filter((a) => a.statusKey === 'held').map((a) => a.name).sort(), ['jellyfin', 'jellyseerr', 'prowlarr', 'radarr', 'sabnzbd', 'sonarr']);
  assert.deepEqual(ml.apps.filter((a) => a.statusKey === 'healthy').map((a) => a.name).sort(), ['cloudflared', 'wol-relay']);
  // Docker down while the gate is fine: unknown (live-restore may keep them serving), not held, not stopped
  const dd = view(strip(dockerDown(NOW)));
  assert.ok(dd.apps.every((a) => a.statusKey === 'unknown' && a.stopKind === 'docker-down'));
  assert.ok(dd.apps.every((a) => a.st.label === 'Unknown' && a.st.tone === 'warn'));
  assert.equal(m.appsSummary(dd.apps, dd.docker).sub, "Docker isn't running (apps may still be serving)");
  const one = normal(NOW);
  one.apps[0] = { ...one.apps[0], status: 'stopped', state: 'exited' };
  assert.equal(view(one).apps.find((a) => a.name === 'cloudflared').st.label, 'Stopped on purpose');
});

test('stopped apps: the server\'s stoppedReason wins over inferring from docker.state', () => {
  const s = normal(NOW);
  const set = (name, o) => (s.apps = s.apps.map((a) => (a.name === name ? { ...a, status: 'stopped', state: 'exited', ...o } : a)));
  set('cloudflared', { stoppedReason: 'docker-down' });
  set('wol-relay', { stoppedReason: 'gate' });
  set('sonarr', { stoppedReason: 'intentional' });
  let v = view(s);
  const label = (n) => v.apps.find((a) => a.name === n).st.label;
  assert.equal(label('cloudflared'), 'Unknown', 'docker-down is shown as unknown even from a server that says "stopped"');
  assert.equal(label('wol-relay'), 'Stopped by the gate');
  assert.equal(label('sonarr'), 'Stopped on purpose');
  assert.match(m.healthBanner({ ...v, issues: [] }).sub, /1 stopped on purpose/, 'only intentional stops count as "on purpose"');
  assert.equal(m.appsSummary(v.apps, v.docker).sub, "Docker isn't running (apps may still be serving)");
  s.docker = { state: 'inactive' };
  v = view(s);
  assert.equal(label('sonarr'), 'Stopped on purpose', 'the server\'s reason wins over the inference');
  assert.equal(m.normalizeApp({ name: 'x', stoppedReason: 'bogus' }).stoppedReason, null);
});

test('Docker down, gate not holding it: unknown, not counted as down or stopped, actions locked with that reason', async () => {
  const { appActions } = await import('../public/js/screens/apps.js');
  globalThis.location = { protocol: 'https:', hostname: 'box.example' }; // Open links are built from it
  const v = view(dockerDown(NOW));
  for (const a of v.apps) {
    assert.equal(a.statusKey, 'unknown');
    assert.deepEqual([a.st.label, a.st.tone, a.st.icon], ['Unknown', 'warn', 'question']);
    assert.equal(a.st.long, "Unknown: Docker isn't running (apps may still be serving)");
  }
  const sum = m.appsSummary(v.apps, v.docker);
  assert.equal(sum.sub, "Docker isn't running (apps may still be serving)");
  assert.equal(v.apps.filter((a) => ['down', 'stopped'].includes(a.statusKey)).length, 0, 'neither down nor stopped');
  assert.doesNotMatch(m.healthBanner({ ...v, issues: [] }).sub, /stopped on purpose/);
  const sonarr = v.apps.find((a) => a.name === 'sonarr');
  const { list, notes } = appActions(sonarr, { paused: null, remote: false, docker: v.docker });
  const byKey = Object.fromEntries(list.map((b) => [b.key, b]));
  for (const k of ['restart', 'stop']) assert.equal(byKey[k].reason, "Docker isn't running (apps may still be serving).", k);
  assert.equal(byKey.open.reason, undefined, 'Open stays: the app may still be serving');
  assert.deepEqual(notes, ["Docker isn't running (apps may still be serving)."]);
  // the fallback (no server status) reaches the same view
  const fb = view(strip(dockerDown(NOW)));
  assert.ok(fb.apps.every((a) => a.st.long === v.apps[0].st.long));
  // with the gate holding Docker, apps stay held
  assert.ok(view(badNight(NOW)).apps.every((a) => a.statusKey === 'held'));
  delete globalThis.location;
});

test('app status: the server decides; pending actions and server actions overlay it', () => {
  assert.equal(m.appStatus({ name: 'x', status: 'unhealthy', state: 'running', health: 'healthy' }), 'unhealthy', 'server status wins');
  assert.equal(m.appStatus({ name: 'x', status: 'unknown' }), 'unknown');
  assert.equal(m.appStatus({ name: 'x', state: 'running', health: 'unhealthy' }), 'unhealthy');
  assert.equal(m.appStatus({ name: 'x', state: 'running', health: 'starting' }), 'starting');
  assert.equal(m.appStatus({ name: 'x', state: 'exited' }), 'down');
  assert.equal(m.appStatus({ name: 'x', state: 'exited', stoppedOnPurpose: true }), 'stopped');
  assert.equal(m.appStatus({ name: 'x', state: 'restarting' }), 'down', 'a restart loop is down, as on the server');
  const running = { name: 'x', status: 'healthy', state: 'running' };
  assert.equal(m.appStatus(running, { pending: { kind: 'restart' } }), 'starting', 'optimistic restart');
  assert.equal(m.appStatus(running, { pending: { kind: 'update' } }), 'updating');
  assert.equal(m.appStatus(running, { pending: { kind: 'stop' } }), 'stopped');
  assert.equal(m.appStatus({ ...running, action: { kind: 'update', step: 'pulling' } }), 'updating', 'server action');
  assert.equal(m.appStatus({ ...running, status: 'held', action: { kind: 'restart' } }), 'starting');
});

test('apps: labels, lock reasons, update target and uptime come through', () => {
  const v = view(normal(NOW));
  const radarr = v.apps.find((a) => a.name === 'radarr');
  assert.equal(radarr.next, '6.1.1.10360-ls303', 'pinned tag when an update exists');
  assert.equal(radarr.st.label, 'Healthy');
  assert.ok(radarr.uptimeSeconds > 3 * 86400);
  assert.equal(v.apps.find((a) => a.name === 'sonarr').next, null);
  assert.equal(v.apps.find((a) => a.name === 'sonarr').locks.start, 'Already running');
  const bad = view(badNight(NOW));
  assert.ok(bad.apps.every((a) => a.uptimeSeconds === null && a.locks.restart === 'Held by the boot gate until the media drive is back.'));
  // fallback update detection: digests when both have one, else tags
  const d = (c) => `x/y:1@sha256:${c.repeat(64)}`;
  assert.equal(m.normalizeApp({ name: 'a', image: d('a'), pinned: d('b') }).updateAvailable, true);
  assert.equal(m.normalizeApp({ name: 'a', image: d('a'), pinned: d('a') }).updateAvailable, false);
  assert.equal(m.normalizeApp({ name: 'a', image: 'x/y:1', pinned: 'x/y:2@sha256:' + 'c'.repeat(64) }).updateAvailable, true);
  assert.equal(m.normalizeApp({ name: 'a', image: 'x/y:2', pinned: 'x/y:2@sha256:' + 'c'.repeat(64) }).updateAvailable, false);
});

test('open links: the app ports on this host (ADR 0015), strict paths, nothing else', () => {
  const loc = { protocol: 'https:', hostname: 'box.example' };
  assert.equal(m.openHref({ port: 8444, path: '/sonarr' }, loc), 'https://box.example:8444/sonarr');
  assert.equal(m.openHref({ port: 8444, path: '/watch' }, loc), 'https://box.example:8444/watch');
  assert.equal(m.openHref({ port: 8443, path: '/' }, loc), 'https://box.example:8443/');
  assert.equal(m.openHref({ port: 8443 }, loc), 'https://box.example:8443/', 'no path: the root');
  assert.equal(m.openHref({ path: '/sonarr' }, loc), '/sonarr', 'a plain path stays on this origin');
  for (const bad of ['//evil.example/x', '/\\evil.example', '/ok\\..\\x', 'javascript:alert(1)', 'sonarr', '', 42]) {
    assert.equal(m.openHref({ path: bad }, loc), null, JSON.stringify(bad));
    assert.equal(m.openHref({ port: 8444, path: bad }, loc), null, `port + ${JSON.stringify(bad)}`);
  }
  assert.equal(m.openHref({ port: 70000, path: '/' }, loc), null);
  assert.equal(m.openHref({ port: 'x', path: '/' }, loc), null);
  assert.equal(m.openHref(null, loc), null);
  // the built-in fallbacks follow ADR 0015
  assert.deepEqual(m.APP_META.sonarr.open, { port: 8444, path: '/sonarr' });
  assert.deepEqual(m.APP_META.jellyseerr.open, { port: 8443, path: '/' });
});

test('issues: the server list is used as-is, mapped to screens', () => {
  const s = normal(NOW);
  s.issues = [
    { severity: 'fail', text: 'Radarr is down.', target: 'apps', app: 'radarr' },
    { severity: 'warn', text: 'Media drive is 92% full.', target: 'drive' },
    { severity: 'fail', text: 'The last nightly backup failed.', target: 'backups' },
    { severity: 'warn', text: 'Usenet provider renews in 5 days.', target: 'settings' },
  ];
  const v = view(s);
  assert.deepEqual(
    v.issues.map((i) => [i.level, i.to]),
    [
      ['fail', { name: 'app', id: 'radarr' }],
      ['warn', { name: 'drive' }],
      ['fail', null],
      ['warn', { name: 'settings' }],
    ],
  );
  assert.equal(m.healthBanner(v).title, '4 issues');
  assert.equal(m.healthBanner(v).icon, 'warning-octagon', 'fail present');
  s.issues = [{ severity: 'warn', text: 'x', target: 'media' }];
  assert.equal(m.healthBanner(view(s)).icon, 'warning');
});

test('issues: derived on the client when the server sends none, same rules and order', () => {
  const v = view(troubled(NOW));
  assert.deepEqual(
    v.issues.map((i) => `${i.level}: ${i.text}`),
    [
      'fail: Radarr is down.',
      'fail: The last nightly backup failed.',
      'warn: Sonarr is unhealthy.',
      'warn: Media drive is 92% full.',
      'warn: Indexer C: failing since 11:02.',
      'warn: Usenet provider renews in 3 days.',
    ],
  );
  assert.deepEqual(v.issues[0].to, { name: 'app', id: 'radarr' });
  // parity with the server's texts for the gate
  const bad = badNight(NOW);
  const serverText = bad.issues[0].text;
  delete bad.issues;
  assert.equal(view(bad).issues[0].text, serverText);
  const ml = mediaLost(NOW);
  const mlText = ml.issues[0].text;
  delete ml.issues;
  assert.equal(view(ml).issues[0].text, mlText);
  const sd = sabDown(NOW);
  delete sd.issues;
  assert.deepEqual(view(sd).issues.map((i) => i.text), ["SABnzbd's API isn't answering."]);
  const dd = dockerDown(NOW);
  delete dd.issues;
  assert.deepEqual(view(dd).issues.map((i) => i.text), ["Docker isn't running."]);
  // a clean start is not an issue
  const c = clean(NOW);
  delete c.issues;
  assert.deepEqual(view(c).issues, []);
});

test('health banner: OK sub-line and the clean start', () => {
  assert.deepEqual(m.healthBanner(view(normal(NOW))), {
    tone: 'ok',
    icon: 'check',
    title: 'All systems normal',
    sub: '8 apps healthy · drive 34% · backup 3 h ago',
    issues: [],
  });
  assert.equal(m.healthBanner(view(clean(NOW))).sub, '8 apps healthy · drive empty · first backup tonight');
  assert.deepEqual(m.appsSummary(view(badNight(NOW)).apps), { big: '0 / 8 healthy', sub: '8 held by the gate' });
  assert.deepEqual(m.appsSummary(view(normal(NOW)).apps), { big: '8 / 8 healthy', sub: 'All containers up' });
});

test('nav dots: drive/more on a gate problem, alerts on an active alert, media on a media issue', () => {
  assert.deepEqual(view(normal(NOW)).dots, { drive: null, media: null, alerts: null, apps: null, more: null });
  assert.deepEqual(view(badNight(NOW)).dots, { drive: 'fail', media: null, alerts: 'fail', apps: null, more: 'fail' });
  assert.equal(view(sabDown(NOW)).dots.media, 'warn');
  const t = view(troubled(NOW)).dots;
  assert.equal(t.apps, 'fail');
  assert.equal(t.drive, 'warn', 'disk over the warning threshold');
});

test('"<app> crashed and restarted": always resolved, labelled "recovered on its own", never active', () => {
  const at = (h) => new Date(NOW - h * 3600e3).toISOString();
  const list = m.alertStates(
    [
      { ts: at(1), title: 'sonarr crashed and restarted', tag: 'alertfloor:sonarr', state: 'active' }, // title rule beats a stale "active"
      { ts: at(2), title: 'radarr crashed and restarted', tag: 'alertfloor:radarr', state: 'resolved', note: 'Recovered on its own' }, // the server's shape
      { ts: at(3), title: 'jellyfin crashed and restarted, still unhealthy', tag: 'alertfloor:jellyfin' }, // a qualifier: not self-healed
      { ts: at(4), title: 'sonarr unhealthy', tag: 'alertfloor:sonarr' },
    ],
    NOW,
  );
  assert.deepEqual(
    list.map((a) => [a.title, a.state, a.selfHealed, m.alertChip(a).label]),
    [
      ['sonarr crashed and restarted', 'resolved', true, 'Resolved · recovered on its own'],
      ['radarr crashed and restarted', 'resolved', true, 'Resolved · recovered on its own'],
      ['jellyfin crashed and restarted, still unhealthy', 'active', false, 'Active'],
      ['sonarr unhealthy', 'active', false, 'Active'],
    ],
    'a self-healed alert resolves nothing else',
  );
  const v = view(normal(NOW));
  const crash = v.alerts.find((a) => /crashed and restarted/.test(a.title));
  assert.equal(crash.state, 'resolved');
  assert.equal(v.dots.alerts, null, 'no active-alert dot for it');
});

test('alert states: the server state wins; fallback pairs lost → back by tag', () => {
  const at = (h) => new Date(NOW - h * 3600e3).toISOString();
  const raw = [
    { ts: at(100), title: 'Media drive lost', tag: 'bootgate' },
    { ts: at(99), title: 'Media drive back', tag: 'bootgate' },
    { ts: at(2), title: 'Media drive lost', tag: 'bootgate' },
    { ts: at(1), title: 'Test from homelab', tag: 'test' },
    { ts: at(3), title: 'Something odd', tag: null },
    { ts: at(24 * 9), title: 'Old problem', tag: 'host' },
  ];
  const s = m.alertStates(raw, NOW);
  assert.deepEqual(
    s.map((a) => [a.title, a.state]),
    [
      ['Test from homelab', 'info'],
      ['Media drive lost', 'active'],
      ['Something odd', 'info'],
      ['Media drive back', 'resolved'],
      ['Media drive lost', 'resolved'],
      ['Old problem', 'info'],
    ],
  );
  assert.equal(m.alertStates([{ ts: at(1), title: 'Media drive back', tag: 'x', state: 'active' }], NOW)[0].state, 'active', 'server state wins');
  assert.deepEqual(m.alertStates([{ ts: 'nope', title: 'x' }, null, 'x'], NOW), [], 'junk is dropped');
});

test('forecast: the server object is read; dates are local days', () => {
  const v = view(normal(NOW));
  const f = v.drive.forecast;
  assert.equal(f.perDayBytes, 6e9);
  assert.equal(dayKey(f.fullAt), '2028-01-07', '440 days after 2026-10-24');
  assert.equal(dayKey(f.at(97)), '2027-12-18');
  const at85 = f.at(85);
  assert.ok(at85 > new Date(NOW) && at85 < f.at(90), 'a custom warning % is projected from the slope');
  const c = v.drive.chart;
  assert.ok(c.used.split(' ').length >= 30, 'samples drawn');
  assert.match(c.proj, /^\d+(\.\d)?,\d+(\.\d)? \d+(\.\d)?,\d+(\.\d)?$/);
  assert.ok(c.dWarn && c.dFail && c.fullAt);
  assert.equal(c.yWarn, 18, '90 % line');
  assert.ok(Math.abs(c.yFail - 5.4) < 1e-9, '97 % line');
  assert.ok(c.todayPct > 5 && c.todayPct < 20, 'today sits after 60 days of history');
  // not enough samples / not growing
  const cl = view(clean(NOW));
  assert.equal(cl.drive.forecast, null);
  assert.equal(cl.drive.chart, null);
  const flat = normal(NOW);
  flat.drive.forecast = null;
  flat.drive.forecastReason = 'not-growing';
  const fv = view(flat);
  assert.equal(fv.drive.forecast.perDayBytes, 0);
  assert.equal(fv.drive.forecast.fullAt, null);
  assert.equal(fv.drive.chart.proj, null, 'no projection line when not growing');
});

test('forecast fallback: least squares over the last 30 days, 7 samples minimum', () => {
  const day = 86400000;
  const samples = (n, per) => Array.from({ length: n }, (_, i) => ({ date: new Date(NOW - (n - 1 - i) * day).toISOString().slice(0, 10), used: 1e12 + i * per }));
  assert.equal(m.computeForecast(samples(6, 5e9), 4e12, 1.025e12, NOW), null, 'fewer than 7 samples');
  const f = m.computeForecast(samples(20, 5e9), 4e12, 1.095e12, NOW);
  assert.ok(Math.abs(f.perDayBytes - 5e9) < 1, 'slope');
  const days = (f.fullAt - NOW) / day;
  assert.ok(Math.abs(days - (4e12 - 1.095e12) / 5e9) < 1);
  const old = samples(40, 5e9).slice(0, 10); // all older than 30 days
  assert.equal(m.computeForecast(old, 4e12, 1.1e12, NOW), null, 'window is 30 days');
  const shrinking = m.computeForecast(samples(10, -1e9), 4e12, 1e12, NOW);
  assert.ok(shrinking.perDayBytes < 0 && shrinking.fullAt === null);
  // used without the server's forecast fields
  const s = normal(NOW);
  delete s.drive.forecast;
  delete s.drive.forecastReason;
  assert.ok(view(s).drive.forecast.perDayBytes > 5e9);
});

test('gate history: server groups and flat history.jsonl both give boot groups, newest first', () => {
  const g = view(badNight(NOW)).gate;
  assert.deepEqual(
    g.groups.map((x) => [x.boot, x.current, x.rows.length]),
    [
      ['4e1c', true, 1],
      ['b2c3', false, 2],
      ['a91f', false, 2],
      ['77b0', false, 3],
      ['5a12', false, 2],
    ],
  );
  assert.equal(g.groups[1].rows[0].event, 'powercycle', 'newest first inside a boot');
  const flat = m.historyGroups(
    {
      history: [
        { ts: '2026-10-20T03:16:02Z', boot_id: '77b0d1e2-0000-0000-0000-000000000000', event: 'fail', detail: 'x', wait_seconds: 30 },
        { ts: '2026-10-21T06:01:22Z', boot_id: 'a91f3c7d-0000-0000-0000-000000000000', event: 'pass', detail: 'y', wait_seconds: 0 },
        { ts: '2026-10-20T03:21:40Z', boot_id: '77b0d1e2-0000-0000-0000-000000000000', event: 'retry-pass', detail: 'z', wait_seconds: 300 },
      ],
    },
    'a91f',
  );
  assert.deepEqual(
    flat.map((x) => [x.boot, x.current, x.rows.map((r) => r.event)]),
    [
      ['a91f', true, ['pass']],
      ['77b0', false, ['retry-pass', 'fail']],
    ],
  );
  assert.equal(flat[1].started.toISOString(), '2026-10-20T03:16:02.000Z');
});

test('gate: server line, retry countdown target, re-run lock', () => {
  const ok = view(normal(NOW)).gate;
  assert.equal(ok.canRerun, false);
  assert.match(ok.rerunLock, /verified and mounted/);
  assert.equal(ok.nextRetryAt, null);
  const bad = view(badNight(NOW)).gate;
  assert.equal(bad.line, 'Media drive missing since 02:31 after a power-cycle. 8 apps held. Retrying every 5 min.');
  assert.equal(bad.nextRetryAt.getTime(), NOW + 222e3);
  assert.equal(bad.canRerun, true);
  // fallback line and lock without the server's fields
  const s = badNight(NOW);
  delete s.gate.line;
  delete s.gate.canRerun;
  delete s.gate.rerunLock;
  const v = view(s).gate;
  assert.match(v.line, /^Media drive missing since \d\d:\d\d after a power-cycle\. 8 apps held\. Retrying every 5 min\.$/);
  assert.equal(v.canRerun, true);
  assert.equal(m.gateLine({ status: 'waiting' }), 'Waiting for the media drive. The boot check is still running.');
  assert.equal(m.gateStatus({ gate: { status: 'weird' } }), 'unknown');
});

test('drive failures per month: the server list, or counted from the history', () => {
  const months = ['2026-09', '2026-10'];
  assert.deepEqual(m.failuresByMonth({ failuresByMonth: [{ month: '2026-10', count: 2 }], groups: [] }, months), { '2026-09': 0, '2026-10': 2 });
  const groups = view(badNight(NOW)).gate.groups;
  // boots with a fail (4e1c, b2c3, 77b0) + the media-lost (a91f), all in October
  assert.deepEqual(m.failuresByMonth({ failuresByMonth: [], groups }, months), { '2026-09': 0, '2026-10': 4 });
});

test('backups: the server status, or derived from status.json fields', () => {
  const th = { backupMaxAgeH: 36 };
  assert.equal(m.backupView({ status: 'stale', lastOk: '2026-10-22T03:00:00Z' }, th, NOW).status, 'stale');
  assert.equal(m.backupView({ last_ok: '2026-10-24T03:12:00Z', ok: true }, th, NOW).status, 'ok');
  assert.equal(m.backupView({ last_ok: '2026-10-22T03:12:00Z', ok: true }, th, NOW).status, 'stale');
  assert.equal(m.backupView({ last_ok: '2026-10-24T03:12:00Z', ok: false }, th, NOW).status, 'failed');
  assert.equal(m.backupView({}, th, NOW).status, 'none');
});

test('renewals: countdown tones (≤7 fail, ≤30 warn), soonest first', () => {
  const r = m.renewals(
    [
      { key: 'b', name: 'B', date: '2027-03-02' },
      { key: 'a', name: 'A', date: '2026-11-16' },
      { key: 'u', name: 'U', date: '2026-10-29' },
      { key: 'x', name: 'X', date: 'not a date' },
    ],
    NOW,
  );
  assert.deepEqual(
    r.map((x) => [x.key, x.days, x.tone]),
    [
      ['u', 5, 'fail'],
      ['a', 23, 'warn'],
      ['b', 129, 'plain'],
      ['x', NaN, 'idle'],
    ],
  );
});

test('media: tiles, health rows and the overview summary', () => {
  const n = view(normal(NOW)).media;
  assert.equal(n.tiles.jellyfin.state, 'ok');
  const rows = m.mediaHealthRows(n);
  assert.deepEqual(rows.slice(0, 4).map((r) => [r.label, r.tone, r.text]), [
    ['Indexer A', 'ok', 'OK'],
    ['Indexer B', 'warn', '912 / 1000 API calls today'],
    ['Indexer C', 'ok', 'OK'],
    ['Download client reachable', 'ok', 'OK'],
  ]);
  assert.deepEqual(m.mediaHealthSummary(rows), { tone: 'warn', label: '1 near limit' });
  const bad = view(badNight(NOW)).media;
  const brows = m.mediaHealthRows(bad);
  assert.deepEqual(brows[0], { label: 'Indexers', status: 'idle', text: 'Held by gate', tone: 'idle', icon: 'pause-circle' });
  assert.deepEqual(m.mediaHealthSummary(brows), { tone: 'fail', label: '1 failing' }, 'library folders missing');
  // without a server tile, the app's status decides
  assert.equal(m.mediaTile(undefined, { statusKey: 'held' }).state, 'held');
  assert.equal(m.mediaTile(undefined, { statusKey: 'healthy' }).state, 'pending');
  const sv = m.sessionView({ title: 'T', positionSeconds: 30, durationSeconds: 120, mode: 'transcode' });
  assert.equal(sv.pct, 25);
  assert.deepEqual(sv.mode, { tone: 'info', label: 'Transcoding', icon: 'cpu' });
  assert.equal(m.sessionView({ mode: 'direct-stream' }).mode.label, 'Direct stream');
  assert.equal(m.queueItemView({ name: 'n', pct: 140, etaSeconds: null }).pct, 100);
});

test('pending actions are judged from the summary when there is no action stream', () => {
  const p = { kind: 'restart', since: NOW, startedAtBefore: '2026-10-21T06:00:00Z' };
  const app = (o) => ({ name: 'x', state: 'running', health: 'healthy', startedAt: '2026-10-21T06:00:00Z', ...o });
  assert.equal(m.pendingOutcome(p, app({}), NOW + 1000), 'wait', 'not restarted yet');
  assert.equal(m.pendingOutcome(p, app({ startedAt: '2026-10-24T14:08:05Z' }), NOW + 6000), 'healthy');
  assert.equal(m.pendingOutcome(p, app({ startedAt: '2026-10-24T14:08:05Z', health: 'starting' }), NOW + 6000), 'wait');
  assert.equal(m.pendingOutcome(p, app({ startedAt: '2026-10-24T14:08:05Z', health: 'unhealthy' }), NOW + 6000), 'unhealthy');
  assert.equal(m.pendingOutcome(p, app({ startedAt: '2026-10-24T14:08:05Z', state: 'exited' }), NOW + 6000), 'failed');
  assert.equal(m.pendingOutcome(p, app({}), NOW + 200000), 'timeout');
  assert.equal(m.pendingOutcome(p, app({ action: { kind: 'restart' } }), NOW + 6000), 'wait', 'the server is still on it');
  assert.equal(m.pendingOutcome({ kind: 'stop', since: NOW }, app({ state: 'exited' }), NOW + 1000), 'stopped');
  const up = { kind: 'update', since: NOW, startedAtBefore: '2026-10-21T06:00:00Z' };
  assert.equal(m.pendingOutcome(up, app({ startedAt: '2026-10-24T14:08:05Z', updateAvailable: true }), NOW + 6000), 'wait', 'still the old image');
  assert.equal(m.pendingOutcome(up, app({ startedAt: '2026-10-24T14:08:05Z', updateAvailable: false }), NOW + 6000), 'healthy');
});

test('runbook links: from summary.links.runbooks, http(s) only', () => {
  const s = normal(NOW);
  assert.deepEqual(view(s).runbooks.map((r) => r.title), ['Failed boot gate', 'Restore from scratch']);
  s.links.runbooks.push({ title: 'Bad', url: 'javascript:alert(1)' }, { title: 'No url' }, null);
  assert.equal(view(s).runbooks.length, 2);
  assert.deepEqual(view(clean(NOW)).runbooks, []);
});

test('the real server summaries (test/server-summaries) build views that match what the server decided', async () => {
  const { REAL } = await import('./fixtures.mjs');
  assert.deepEqual(Object.keys(REAL).sort(), ['bad-night', 'clean', 'docker-inactive', 'media-lost', 'normal', 'one-app-down']);
  for (const [name, s] of Object.entries(REAL)) {
    const v = view(s);
    assert.equal(v.apps.length, s.apps.length, name);
    for (const a of s.apps) assert.equal(v.apps.find((x) => x.name === a.name).statusKey, a.status, `${name} ${a.name}: the server's status`);
    assert.deepEqual(v.issues.map((i) => i.text), s.issues.map((i) => i.text), `${name}: the server's issues, in order`);
    assert.equal(v.gate.line, s.gate.line, `${name}: gate line as sent`);
    assert.equal(v.hostName, s.server.name);
  }
  const bad = view(REAL['bad-night']);
  assert.ok(bad.apps.every((a) => a.st.label === 'Held by gate'));
  const ml = view(REAL['media-lost']);
  assert.deepEqual(ml.apps.filter((a) => a.statusKey === 'held').map((a) => a.state), Array(6).fill('exited'), 'media-lost keeps the exited containers');
  const dd = view(REAL['docker-inactive']);
  assert.ok(dd.apps.every((a) => a.st.long === "Unknown: Docker isn't running (apps may still be serving)"), 'docker-down: unknown, never "Stopped"');
});

test('offline snapshot rows', () => {
  assert.deepEqual(m.offlineRows(view(badNight(NOW))).map((r) => [r.k, r.v, r.tone]), [
    ['Boot gate', 'failed', 'fail'],
    ['Apps', '0 running · 8 held', 'idle'],
    ['Last backup', 'Oct 24 03:08', 'ok'],
  ]);
  assert.equal(m.offlineRows(view(normal(NOW)))[1].v, '8 / 8 healthy');
  assert.equal(m.offlineRows(view(clean(NOW)))[2].v, 'none yet');
});

test('day and month keys', () => {
  assert.deepEqual(m.lastMonths(3, NOW), ['2026-08', '2026-09', '2026-10']);
  assert.deepEqual(m.lastMonths(2, Date.parse('2026-01-15T00:00:00Z')), ['2025-12', '2026-01']);
  const days = m.lastDays(30, NOW);
  assert.equal(days.length, 30);
  assert.equal(days[29], '2026-10-24');
  assert.equal(days[0], '2026-09-25');
  assert.equal(m.dayTone(100), 'ok');
  assert.equal(m.dayTone(99.72), 'warn');
  assert.equal(m.dayTone(10), 'fail');
  assert.equal(m.dayTone(null), 'none');
});
