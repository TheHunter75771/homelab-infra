// Formatters: sizes, durations, countdowns, dates. Independent of the process TZ (npm test runs the suite
// under UTC, America/New_York and Pacific/Kiritimati): clock assertions pin the display zone.
import { test, beforeEach, after } from 'node:test';
import assert from 'node:assert/strict';
import * as f from '../public/js/format.js';

const NOW = Date.parse('2026-10-24T14:08:00Z');

beforeEach(() => f.setTimeZone('UTC'));
after(() => f.setTimeZone(null));

test('sizes: decimal units, three significant figures, as in the design', () => {
  assert.equal(f.fmtBytes(2.64e12), '2.64 TB');
  assert.equal(f.fmtBytes(4e12), '4.0 TB');
  assert.equal(f.fmtBytes(1.36e12), '1.36 TB');
  assert.equal(f.fmtBytes(812e9), '812 GB');
  assert.equal(f.fmtBytes(18.4e9), '18.4 GB');
  assert.equal(f.fmtBytes(16e9), '16 GB');
  assert.equal(f.fmtBytes(62e9), '62 GB');
  assert.equal(f.fmtBytes(3.984e12), '3.98 TB');
  assert.equal(f.fmtBytes(999.6e9), '1.0 TB', 'rounds up across a unit boundary');
  assert.equal(f.fmtBytes(512), '512 B');
  assert.equal(f.fmtBytes(0), '0 B');
  assert.equal(f.fmtBytes(0, { zeroUnit: 'GB' }), '0 GB');
  assert.equal(f.fmtBytes(-1), '—');
  assert.equal(f.fmtBytes('x'), '—');
  assert.equal(f.fmtRate(38.2e6), '38.2 MB/s');
  assert.equal(f.fmtRate(0), '0 B/s');
});

test('host figures: RAM in GiB labelled GB, load, percentages', () => {
  assert.equal(f.fmtMem(5.1 * 1024 ** 3, 15.5 * 1024 ** 3), '5.1 / 15.5 GB');
  assert.equal(f.fmtMem(null, 1), '—');
  assert.equal(f.fmtLoad([0.42, 0.38, 0.35]), '0.42 0.38 0.35');
  assert.equal(f.fmtLoad(null), '—');
  assert.equal(f.fmtPct(41), '41%');
  assert.equal(f.fmtPct(NaN), '—');
  assert.equal(f.fmtUptimePct(99.95), '99.95%');
  assert.equal(f.fmtUptimePct(99.999), '100%');
  assert.equal(f.fmtUptimePct(null), '—');
});

test('durations: compact uptime, spaced spans, precise spans', () => {
  assert.equal(f.fmtUptime((3 * 24 + 8) * 3600), '3d 8h');
  assert.equal(f.fmtUptime(4 * 3600 + 12 * 60), '4h 12m');
  assert.equal(f.fmtUptime(26 * 3600), '1d 2h');
  assert.equal(f.fmtUptime(12 * 60 + 5), '12m');
  assert.equal(f.fmtUptime(45), '45s');
  assert.equal(f.fmtUptime(-5), '—');
  assert.equal(f.fmtSpan(240), '4 m');
  assert.equal(f.fmtSpan(840), '14 m');
  assert.equal(f.fmtSpan(30), '30 s');
  assert.equal(f.fmtSpan(2 * 3600 + 5 * 60), '2 h 5 m');
  assert.equal(f.fmtSpan(3 * 3600), '3 h');
  assert.equal(f.fmtSpan(26 * 3600), '1 d 2 h');
  assert.equal(f.fmtSpanPrecise(14 * 60 + 2), '14 m 02 s');
  assert.equal(f.fmtSpanPrecise(9), '9 s');
});

test('countdowns: gate retry clock, playback, ETA, horizon, days left', () => {
  assert.equal(f.fmtCountdown(222), '3:42');
  assert.equal(f.fmtCountdown(5), '0:05');
  assert.equal(f.fmtCountdown(0.2), '0:01', 'rounds up so it never shows 0:00 early');
  assert.equal(f.fmtCountdown(-30), '0:00', 'never negative');
  assert.equal(f.fmtCountdown(3723), '1:02:03');
  assert.equal(f.fmtPlayback(4400, 6960), '1:13:20 / 1:56:00');
  assert.equal(f.fmtPlayback(65, 1500), '1:05 / 25:00');
  assert.equal(f.fmtEta(130), '2 min');
  assert.equal(f.fmtEta(30), 'under 1 min');
  assert.equal(f.fmtEta(null), 'queued');
  assert.equal(f.fmtEta(3 * 3600), '3 h');
  assert.equal(f.fmtHorizon(440), '~63 weeks');
  assert.equal(f.fmtHorizon(5), '~5 days');
  assert.equal(f.fmtHorizon(1), '~1 day');
  assert.equal(f.fmtHorizon(2000), '~5 years');
  assert.equal(f.fmtDaysLeft(23), '23 days');
  assert.equal(f.fmtDaysLeft(1), '1 day');
  assert.equal(f.fmtDaysLeft(0), 'today');
  assert.equal(f.fmtDaysLeft(-2), 'expired');
});

test('calendar days ignore the time of day; YYYY-MM-DD is a local date', () => {
  assert.equal(f.daysBetween(NOW, Date.parse('2026-11-01T00:00:00Z')), 8);
  assert.equal(f.daysBetween('2026-10-24T23:59:00Z', '2026-10-25T00:01:00Z'), 1);
  assert.equal(f.dayKey(f.parseDay('2026-11-16')), '2026-11-16');
  assert.equal(f.parseDay('nope'), null);
  assert.equal(f.parseDay('2026-02-30'), null, 'not a calendar day');
  assert.equal(f.dayKey(NOW), '2026-10-24');
});

test('relative times', () => {
  assert.equal(f.fmtAgo(NOW - 12e3, NOW), '12 s ago');
  assert.equal(f.fmtAgo(NOW - 5 * 60e3, NOW), '5 min ago');
  assert.equal(f.fmtAgo(NOW - 3 * 3600e3, NOW), '3 h ago');
  assert.equal(f.fmtAgo(NOW - 50 * 3600e3, NOW), '2 d ago');
  assert.equal(f.fmtAgo(NOW + 5000, NOW), '0 s ago', 'clock skew never goes negative');
  assert.equal(f.fmtWhen(NOW - 2 * 3600e3, NOW), '2 h ago');
  assert.equal(f.fmtWhen(NOW - 26 * 3600e3, NOW), 'yesterday');
  assert.equal(f.fmtWhen(Date.parse('2026-10-12T10:00:00Z'), NOW), 'Oct 12');
});

test('absolute times: 24 h clock, short months, stamps', () => {
  assert.equal(f.fmtClock(NOW), '14:08');
  assert.equal(f.fmtClockSec('2026-10-24T02:41:03Z'), '02:41:03');
  assert.equal(f.fmtDay(NOW), 'Oct 24');
  assert.equal(f.fmtDayTime('2026-10-24T02:41:00Z'), 'Oct 24 02:41');
  assert.equal(f.fmtDate('2028-01-06T12:00:00Z'), 'Jan 6, 2028');
  assert.equal(f.fmtDayYear('2026-09-30T10:00:00Z'), 'Sep 30 2026');
  assert.equal(f.fmtDayShort('2026-11-27T00:00:00Z', NOW), 'Nov 27');
  assert.equal(f.fmtDayShort('2028-01-28T00:00:00Z', NOW), "Jan '28");
  assert.equal(f.fmtStamp('2026-10-24T03:12:00Z', NOW), '03:12 today');
  assert.equal(f.fmtStamp('2026-10-23T21:40:00Z', NOW), 'yesterday 21:40');
  assert.equal(f.fmtStamp('2026-10-21T03:10:00Z', NOW), 'Oct 21 03:10');
  assert.equal(f.fmtStampLead('2026-10-24T03:12:00Z', NOW), 'today 03:12');
  assert.equal(f.fmtClock(null), '—');
  assert.equal(f.monthLabel('2026-05'), 'May');
});

test('image refs: registry and digest stripped for display, tag and digest extracted', () => {
  const ref = 'lscr.io/linuxserver/radarr:6.1.1.10360-ls303@sha256:' + 'a'.repeat(64);
  assert.equal(f.shortImage(ref), 'linuxserver/radarr:6.1.1.10360-ls303');
  assert.equal(f.imageTag(ref), '6.1.1.10360-ls303');
  assert.equal(f.imageDigest(ref), `sha256:${'a'.repeat(64)}`);
  assert.equal(f.shortImage('cloudflare/cloudflared:2026.5.0'), 'cloudflare/cloudflared:2026.5.0');
  assert.equal(f.shortImage('docker.io/library/node:22'), 'node:22');
  assert.equal(f.shortImage('localhost:5000/x/y:1'), 'x/y:1');
  assert.equal(f.imageTag('homelab/wol-relay'), '');
  assert.equal(f.shortImage(''), '—');
  assert.equal(f.plural(1, 'app'), '1 app');
  assert.equal(f.plural(8, 'app'), '8 apps');
});

test('display zone: times follow the host zone when the summary names it, the browser zone otherwise', () => {
  try {
    f.setTimeZone('America/New_York');
    assert.equal(f.timeZone(), 'America/New_York');
    assert.equal(f.fmtClock('2026-10-24T02:31:00Z'), '22:31', 'EDT is UTC-4');
    assert.equal(f.fmtDayTime('2026-10-24T02:31:00Z'), 'Oct 23 22:31');
    assert.equal(f.fmtClock('2026-12-24T02:31:00Z'), '21:31', 'EST in winter');
    assert.equal(f.dayKey('2026-10-24T02:31:00Z'), '2026-10-23');
    assert.equal(f.daysBetween('2026-10-24T02:00:00Z', '2026-10-24T05:00:00Z'), 1, 'crosses local midnight');
    assert.equal(f.fmtDate(f.parseDay('2026-11-16')), 'Nov 16, 2026', 'a calendar day stays that day');
    assert.equal(f.daysBetween(Date.parse('2026-10-24T14:08:00Z'), f.parseDay('2026-11-01')), 8);
    assert.equal(f.dayKeyOffset('2026-10-24T02:31:00Z', -1), '2026-10-22');
    assert.equal(f.monthKeyOffset('2026-11-01T02:00:00Z', 0), '2026-10', 'still October in New York');
    assert.equal(f.fmtStamp('2026-10-24T01:00:00Z', Date.parse('2026-10-24T14:08:00Z')), 'yesterday 21:00');
    f.setTimeZone('Asia/Tokyo');
    assert.equal(f.fmtClock('2026-10-24T02:31:00Z'), '11:31');
    assert.equal(f.fmtDate(f.parseDay('2026-11-16')), 'Nov 16, 2026');
    f.setTimeZone('Not/AZone');
    assert.equal(f.timeZone(), null, 'an unknown zone falls back to the browser zone');
  } finally {
    f.setTimeZone('UTC');
  }
});

test('date-only values keep their calendar day in every zone, UTC-12 to UTC+14', () => {
  for (const tz of ['Etc/GMT+12', 'America/New_York', 'UTC', 'Asia/Tokyo', 'Pacific/Auckland', 'Pacific/Kiritimati', 'Etc/GMT-14']) {
    f.setTimeZone(tz);
    for (const day of ['2026-11-16', '2027-01-01', '2028-02-29', '2026-03-08', '2026-11-01']) {
      const d = f.parseDay(day);
      assert.equal(f.dayKey(d), day, `${tz} ${day}`);
    }
    assert.equal(f.fmtDate(f.parseDay('2026-11-16')), 'Nov 16, 2026', tz);
    assert.equal(f.daysBetween(f.parseDay('2026-10-24'), f.parseDay('2026-11-01')), 8, tz);
    assert.equal(f.dayKeyOffset(f.parseDay('2026-03-01'), -1), '2026-02-28', tz);
  }
});

test('without a named zone, the browser\'s own zone is used (whatever TZ the process has)', () => {
  f.setTimeZone(null);
  const d = new Date('2026-10-24T02:31:05Z');
  const p2 = (n) => String(n).padStart(2, '0');
  assert.equal(f.fmtClock(d), `${p2(d.getHours())}:${p2(d.getMinutes())}`);
  assert.equal(f.fmtClockSec(d), `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`);
  assert.equal(f.dayKey(d), `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`);
  const noon = f.parseDay('2026-11-16');
  assert.deepEqual([noon.getFullYear(), noon.getMonth(), noon.getDate(), noon.getHours()], [2026, 10, 16, 12], 'local noon');
  assert.equal(f.fmtDate(noon), 'Nov 16, 2026');
});
