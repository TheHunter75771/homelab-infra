import test from 'node:test';
import assert from 'node:assert/strict';
import { bytes, duration, relTime, tokS, pct, toMs, clock, str, windowText } from '../public/js/format.js';

test('bytes: decimal units like Ollama', () => {
  assert.equal(bytes(0), '0 B');
  assert.equal(bytes(999), '999 B');
  assert.equal(bytes(1000), '1.0 KB');
  assert.equal(bytes(4.7e9), '4.7 GB');
  assert.equal(bytes(512e9), '512 GB');
  assert.equal(bytes(2e12), '2.0 TB');
  assert.equal(bytes(-1), '—');
  assert.equal(bytes(null), '—');
  assert.equal(bytes('5'), '—');
  assert.equal(bytes(NaN), '—');
});

test('duration: ms to words', () => {
  assert.equal(duration(0), '0 ms');
  assert.equal(duration(850), '850 ms');
  assert.equal(duration(2349), '2.3 s');
  assert.equal(duration(14_200), '14 s');
  assert.equal(duration(60_000), '1 min');
  assert.equal(duration(252_000), '4 min 12 s');
  assert.equal(duration(3_600_000), '1 h');
  assert.equal(duration(11_100_000), '3 h 5 min');
  assert.equal(duration(187_200_000), '2 d 4 h');
  assert.equal(duration(undefined), '—');
  assert.equal(duration(-5), '—');
});

test('relTime: past, future, units, bad input', () => {
  const now = Date.parse('2026-10-26T20:00:00Z');
  assert.equal(relTime(now - 2000, now), 'just now');
  assert.equal(relTime(now - 12_000, now), '12 s ago');
  assert.equal(relTime(now - 5 * 60_000, now), '5 min ago');
  assert.equal(relTime(now - 3 * 3_600_000, now), '3 h ago');
  assert.equal(relTime(now - 2 * 86_400_000, now), '2 d ago');
  assert.equal(relTime(now + 3 * 60_000, now), 'in 3 min');
  assert.equal(relTime('2026-10-26T19:55:00Z', now), '—', 'ISO strings are not timestamps any more');
  assert.equal(relTime((now - 60_000) / 1000, now), '1 min ago', 'epoch seconds (the gate) are understood');
  assert.equal(relTime('not a date', now), '—');
  assert.equal(relTime(null, now), '—');
});

test('toMs: epoch seconds from the server, epoch ms from the page; nothing else', () => {
  assert.equal(toMs(1_700_000_000), 1_700_000_000_000);
  assert.equal(toMs(1_700_000_000.5), 1_700_000_000_500, 'fractional seconds');
  assert.equal(toMs(1793054921), 1793054921000, 'integer seconds (S is moving to ints)');
  assert.equal(toMs(1_700_000_000_000), 1_700_000_000_000);
  assert.equal(toMs('2026-10-26T20:00:00Z'), null);
  assert.equal(toMs('1700000000'), null);
  assert.equal(toMs(-5), null);
  assert.equal(toMs({}), null);
});

test('windowText: minutes or hours', () => {
  assert.equal(windowText(1800), '30 min');
  assert.equal(windowText(3600), '1 h');
  assert.equal(windowText(5400), '90 min');
  assert.equal(windowText(7200), '2 h');
  assert.equal(windowText(45), '1 min');
  assert.equal(windowText(0), null);
  assert.equal(windowText('1800'), null);
});

test('tokS', () => {
  assert.equal(tokS(41.66), '41.7 tok/s');
  assert.equal(tokS(7.3), '7.3 tok/s');
  assert.equal(tokS(150.4), '150 tok/s');
  assert.equal(tokS(0), '0.0 tok/s');
  assert.equal(tokS(null), '—');
  assert.equal(tokS(-1), '—');
});

test('pct, str', () => {
  assert.equal(pct(61.6), '62%');
  assert.equal(pct(null), '—');
  assert.equal(str('x'), 'x');
  assert.equal(str({ a: 1 }), '—', 'objects never print as [object Object]');
  assert.equal(str('', 'none'), 'none');
  assert.equal(str(3), '3');
});

test('clock: time today, date + time otherwise, in the given zone', () => {
  const now = Date.parse('2026-10-26T20:00:00Z');
  const s = (iso) => Date.parse(iso) / 1000;
  assert.equal(clock(s('2026-10-26T19:05:00Z'), { timeZone: 'UTC', now }), '19:05');
  assert.equal(clock(s('2026-10-26T19:05:00Z'), { timeZone: 'America/New_York', now }), '15:05');
  assert.equal(clock(s('2026-10-24T19:05:00Z'), { timeZone: 'UTC', now }), '24 Oct 19:05');
  assert.ok(clock(s('2026-10-26T19:05:00Z'), { timeZone: 'Not/AZone', now }).length > 1, 'a bad zone falls back');
  assert.equal(clock('2026-10-26T19:05:00Z', { timeZone: 'UTC', now }), '—');
  assert.equal(clock(null), '—');
});
