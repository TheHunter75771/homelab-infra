import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createReminders, reminderDue, reminderMessage } from '../src/reminders.js';
import { createSettings } from '../src/settings.js';
import { createPushService } from '../src/push/service.js';
import { createStore } from '../src/push/store.js';
import { createVapidSource } from '../src/push/vapid.js';
import { fakeSubscription, stubWebpush, tempDir, writeVapid } from './helpers/push-fixtures.js';

const at = (iso) => Date.parse(iso);

function setup(t, { subscriptions = [{ key: 'indexer-a', name: 'Indexer A', date: '2026-11-16' }], timeZone = 'UTC', labels = ['phone'], respond } = {}) {
  const dir = tempDir(t);
  writeVapid(dir);
  const store = createStore({ dir });
  for (const l of labels) store.subscribe(fakeSubscription(), l);
  const settings = createSettings({ stateDir: dir });
  settings.update({ subscriptions });
  const wp = stubWebpush(respond);
  const push = createPushService({ store, vapid: createVapidSource({ file: path.join(dir, 'vapid.json') }), webpush: wp });
  let now = at('2026-11-01T12:00:00Z');
  let rehearsal = false;
  const make = () => createReminders({ stateDir: dir, settings, push, isRehearsal: () => rehearsal, clock: () => now, timeZone });
  return {
    dir,
    wp,
    settings,
    make,
    rem: make(),
    setNow: (iso) => {
      now = at(iso);
    },
    setRehearsal: (v) => {
      rehearsal = v;
    },
  };
}

test('due window: from 7 days before the date up to and including the date', () => {
  const sub = { key: 'a', name: 'A', date: '2026-11-16' };
  assert.equal(reminderDue(sub, '2026-11-08'), false);
  assert.equal(reminderDue(sub, '2026-11-09'), true);
  assert.equal(reminderDue(sub, '2026-11-16'), true);
  assert.equal(reminderDue(sub, '2026-11-17'), false);
});

test('message wording counts down to the date', () => {
  const sub = { key: 'usenet', name: 'Usenet provider', date: '2026-11-16' };
  assert.deepEqual(reminderMessage(sub, '2026-11-09'), {
    title: 'Usenet provider renews in 7 days',
    body: 'Renewal date: Nov 16, 2026. Edit dates in Settings.',
    tag: 'renewal-usenet',
    url: '/',
  });
  assert.equal(reminderMessage(sub, '2026-11-15').title, 'Usenet provider renews tomorrow');
  assert.equal(reminderMessage(sub, '2026-11-16').title, 'Usenet provider renews today');
});

test('fires once, at the first check on the reminder day (not the day before), never twice', async (t) => {
  const s = setup(t);
  s.setNow('2026-11-08T23:59:59Z');
  await s.rem.check();
  assert.equal(s.wp.calls.length, 0, 'day 8 before: nothing');
  s.setNow('2026-11-09T00:00:01Z');
  await s.rem.check();
  assert.equal(s.wp.calls.length, 1);
  assert.equal(JSON.parse(s.wp.calls[0].payload).title, 'Indexer A renews in 7 days');
  s.setNow('2026-11-10T09:00:00Z');
  await s.rem.check();
  await s.rem.check();
  assert.equal(s.wp.calls.length, 1, 'once per (subscription, date)');
  // survives a restart
  await s.make().check();
  assert.equal(s.wp.calls.length, 1);
  assert.ok(fs.existsSync(path.join(s.dir, 'reminders.json')));
});

test('the day boundary is local to the host time zone', async (t) => {
  const s = setup(t, { timeZone: 'America/New_York' });
  s.setNow('2026-11-09T03:00:00Z'); // still Nov 8 in New York
  await s.rem.check();
  assert.equal(s.wp.calls.length, 0);
  s.setNow('2026-11-09T05:30:00Z'); // 00:30 Nov 9 in New York
  await s.rem.check();
  assert.equal(s.wp.calls.length, 1);
});

test('dashboard down on the reminder day: fires late (still before the date), says how many days are left', async (t) => {
  const s = setup(t);
  s.setNow('2026-11-13T10:00:00Z');
  await s.rem.check();
  assert.equal(JSON.parse(s.wp.calls[0].payload).title, 'Indexer A renews in 3 days');
});

test('after the date passed: no reminder; changing the date re-arms it', async (t) => {
  const s = setup(t);
  s.setNow('2026-11-17T10:00:00Z');
  await s.rem.check();
  assert.equal(s.wp.calls.length, 0);
  s.setNow('2026-11-09T10:00:00Z');
  await s.rem.check();
  assert.equal(s.wp.calls.length, 1);
  s.settings.update({ subscriptions: [{ key: 'indexer-a', name: 'Indexer A', date: '2027-11-16' }] });
  s.setNow('2027-11-09T10:00:00Z');
  await s.rem.check();
  assert.equal(s.wp.calls.length, 2, 'new date, new reminder');
});

test('not delivered (no device, or every send failed): retried on the next check', async (t) => {
  let fail = true;
  const s = setup(t, { respond: () => (fail ? Object.assign(new Error('x'), { statusCode: 500 }) : { statusCode: 201 }) });
  s.setNow('2026-11-09T10:00:00Z');
  await s.rem.check();
  assert.equal(s.wp.calls.length, 1);
  fail = false;
  await s.rem.check();
  assert.equal(s.wp.calls.length, 2);
  await s.rem.check();
  assert.equal(s.wp.calls.length, 2, 'delivered now: done');
});

test('rehearsal: test devices only and a [REHEARSAL] prefix (it is an automated push)', async (t) => {
  const s = setup(t, { labels: ['phone', 'test'] });
  s.setRehearsal(true);
  s.setNow('2026-11-09T10:00:00Z');
  await s.rem.check();
  assert.equal(s.wp.calls.length, 1);
  assert.equal(JSON.parse(s.wp.calls[0].payload).title, '[REHEARSAL] Indexer A renews in 7 days');
});

test('overlapping checks share one run (no double send)', async (t) => {
  const s = setup(t);
  s.setNow('2026-11-09T10:00:00Z');
  await Promise.all([s.rem.check(), s.rem.check(), s.rem.check()]);
  assert.equal(s.wp.calls.length, 1);
});

test('summary exposes the schedule; saving settings triggers a check', async (t) => {
  const { startDashboard, request } = await import('./helpers/server.js');
  const wp = stubWebpush();
  const d = await startDashboard(t, 'normal', { webpush: wp });
  const s = d.dash.summary.current().summary;
  const usenet = s.subscriptions.find((x) => x.key === 'usenet');
  assert.deepEqual(usenet, { key: 'usenet', name: 'Usenet provider', date: '2026-10-29', daysLeft: 5, remindOn: '2026-10-22', reminderDue: true, reminded: false });
  const r = await request(d.base, 'PUT', '/api/settings', { body: { thresholds: { diskWarnPct: 85 } } });
  assert.equal(r.status, 200);
  await d.dash.reminders.check();
  assert.equal(wp.calls.length, 3, 'usenet reminder sent to the 3 devices');
  assert.equal(d.dash.reminders.wasSent({ key: 'usenet', date: '2026-10-29' }), true);
});
