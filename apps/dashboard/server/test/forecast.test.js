import { test } from 'node:test';
import assert from 'node:assert/strict';
import { forecastDrive, leastSquares, MIN_SAMPLES, WINDOW_DAYS } from '../src/forecast.js';
import { addDays } from '../src/util/time.js';

const TODAY = '2026-10-24';
const GB = 1e9;
const TB = 1e12;

// n daily samples ending `endAgo` days before TODAY, used(d) = base + slope*d (d = 0 for the oldest).
function series(n, { base = 1 * TB, slope = 6 * GB, total = 4 * TB, endAgo = 0, noise = () => 0 } = {}) {
  return Array.from({ length: n }, (_, i) => ({ date: addDays(TODAY, -endAgo - (n - 1 - i)), used: base + slope * i + noise(i), total }));
}

test('least squares: exact on a line, the textbook answer on scattered points, null without spread', () => {
  const line = leastSquares([0, 1, 2, 3].map((x) => ({ x, y: 5 + 2 * x })));
  assert.equal(line.slope, 2);
  assert.equal(line.intercept, 5);
  const fit = leastSquares([
    { x: 1, y: 2 },
    { x: 2, y: 4 },
    { x: 3, y: 5 },
    { x: 4, y: 4 },
    { x: 5, y: 5 },
  ]);
  assert.ok(Math.abs(fit.slope - 0.6) < 1e-12);
  assert.ok(Math.abs(fit.intercept - 2.2) < 1e-12);
  assert.equal(leastSquares([{ x: 1, y: 1 }]), null);
  assert.equal(leastSquares([{ x: 3, y: 1 }, { x: 3, y: 9 }]), null);
});

test('fewer than 7 samples in the window: no forecast (the design shows the empty panel)', () => {
  assert.equal(MIN_SAMPLES, 7);
  const r = forecastDrive(series(6), TODAY);
  assert.equal(r.forecast, null);
  assert.equal(r.reason, 'not-enough-samples');
  assert.equal(r.samplesInWindow, 6);
  assert.notEqual(forecastDrive(series(7), TODAY).forecast, null);
});

test('only the last 30 days count: old samples are ignored, and duplicates per day collapse', () => {
  assert.equal(WINDOW_DAYS, 30);
  // 20 old samples (31..50 days ago) that would pull the slope negative, plus 10 recent growing ones
  const old = series(20, { base: 3.9 * TB, slope: -50 * GB, endAgo: 31 });
  const recent = series(10, { slope: 6 * GB });
  const r = forecastDrive([...old, ...recent], TODAY);
  assert.equal(r.samplesInWindow, 10);
  assert.equal(r.forecast.slopeBytesPerDay, 6 * GB);
  const dup = forecastDrive([...recent, { ...recent.at(-1), used: recent.at(-1).used }], TODAY);
  assert.equal(dup.samplesInWindow, 10);
  // future-dated samples (clock skew) are ignored
  assert.equal(forecastDrive([...recent, { date: '2026-12-01', used: 3 * TB, total: 4 * TB }], TODAY).samplesInWindow, 10);
});

test('threshold dates follow the fitted slope from the latest sample', () => {
  // latest used = 1 TB + 6 GB * 29 = 1.174 TB of 4 TB; 6 GB/day
  const r = forecastDrive(series(30), TODAY).forecast;
  const latestUsed = 1 * TB + 6 * GB * 29;
  const days = (frac) => Math.ceil((frac * 4 * TB - latestUsed) / (6 * GB));
  assert.equal(r.full.inDays, days(1));
  assert.equal(r.full.date, addDays(TODAY, days(1)));
  assert.equal(r.at90.inDays, days(0.9));
  assert.equal(r.at97.inDays, days(0.97));
  assert.equal(r.method, 'least-squares');
  assert.equal(r.windowDays, 30);
  assert.ok(r.at90.inDays < r.at97.inDays && r.at97.inDays < r.full.inDays);
});

test('noisy daily deltas still give the underlying slope', () => {
  const noise = (i) => (i % 2 ? 4 : -4) * GB; // +-4 GB alternating
  const r = forecastDrive(series(30, { noise }), TODAY).forecast;
  assert.ok(Math.abs(r.slopeBytesPerDay - 6 * GB) < 0.1 * GB, `slope ${r.slopeBytesPerDay}`);
});

test('flat or shrinking usage: no fill date', () => {
  for (const slope of [0, -2 * GB]) {
    const r = forecastDrive(series(14, { slope }), TODAY);
    assert.equal(r.forecast, null);
    assert.equal(r.reason, 'not-growing');
  }
});

test('a threshold already crossed is reported as reached, dated at the latest sample', () => {
  const r = forecastDrive(series(10, { base: 3.7 * TB, slope: 1 * GB }), TODAY).forecast;
  assert.equal(r.at90.reached, true);
  assert.equal(r.at90.date, TODAY);
  assert.equal(r.at90.inDays, 0);
  assert.equal(r.at97.reached, false);
});

test('samples that stopped a few days ago (drive offline) still forecast, with inDays relative to today', () => {
  const r = forecastDrive(series(20, { endAgo: 3 }), TODAY).forecast;
  const latest = series(20, { endAgo: 3 }).at(-1);
  const fromLatest = Math.ceil((4 * TB - latest.used) / (6 * GB));
  assert.equal(r.full.date, addDays(latest.date, fromLatest));
  assert.equal(r.full.inDays, fromLatest - 3);
});

test('bad samples are skipped', () => {
  const good = series(7);
  const r = forecastDrive([...good, { date: 'nope', used: 1, total: 1 }, { date: TODAY, used: 'x', total: 4 * TB }, { date: TODAY, used: 1, total: 0 }], TODAY);
  assert.equal(r.samplesInWindow, 7);
});
