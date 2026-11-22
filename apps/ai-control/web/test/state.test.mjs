import test from 'node:test';
import assert from 'node:assert/strict';
import { initialState, reduce, auditKey, auditSince, filterLines, normaliseLines, validSessionId, diskRows, historySeries, historyStats, connText, dataAgeMs } from '../public/js/state.js';
import { bannerText } from '../public/js/shell.js';
import * as fx from './fixtures.mjs';

test('tab: only known tabs', () => {
  const s = initialState();
  assert.equal(reduce(s, { type: 'tab', tab: 'logs' }).tab, 'logs');
  assert.equal(reduce(s, { type: 'tab', tab: 'nope' }), s);
});

test('summary loaded / unchanged / failed / snapshot', () => {
  let s = initialState();
  s = reduce(s, { type: 'summary/failed', error: 'unreachable' });
  assert.equal(s.conn, 'offline');
  s = reduce(s, { type: 'snapshot/loaded', summary: fx.normal(), savedAt: 1000 });
  assert.equal(s.fromSnapshot, true);
  assert.equal(s.summaryAt, 1000);
  s = reduce(s, { type: 'summary/loaded', summary: fx.normal(), at: 5000 });
  assert.equal(s.fromSnapshot, false);
  assert.equal(s.conn, 'polling', 'back online');
  const again = reduce(s, { type: 'snapshot/loaded', summary: fx.pcOff(), savedAt: 9000 });
  assert.equal(again, s, 'a snapshot never replaces live data');
  s = reduce(s, { type: 'summary/unchanged', at: 8000 });
  assert.equal(s.summaryAt, 8000);
  const bad = reduce(s, { type: 'summary/loaded', summary: [1, 2], at: 9000 });
  assert.equal(bad.conn, 'offline');
  assert.equal(bad.summary, s.summary, 'a malformed summary keeps the last good one');
});

test('logs: sources come from the server; nothing is picked until they load; no fallback list', () => {
  let s = initialState();
  assert.equal(s.logs.source, null);
  assert.equal(s.logs.sources.items, null);
  const failed = reduce(s, { type: 'logs/sources/failed', error: 'HTTP 502' });
  assert.equal(failed.logs.source, null, 'no fallback source');
  assert.equal(failed.logs.sources.error, 'HTTP 502');
  s = reduce(s, { type: 'logs/sources/loaded', data: [{ id: 'homelab-ai-stack', label: 'AI stack' }, { id: 'homelab-ai-stack' }, { id: 7 }, 'x', { id: 'pc:ollama' }] });
  assert.deepEqual(s.logs.sources.items, [{ id: 'homelab-ai-stack', label: 'AI stack' }, { id: 'pc:ollama', label: 'pc:ollama' }], 'deduped, junk dropped, id as label');
  assert.equal(s.logs.source, 'homelab-ai-stack', 'the first source is picked');
  s = reduce(s, { type: 'logs/options', source: 'pc:ollama' });
  const reloaded = reduce(s, { type: 'logs/sources/loaded', data: [{ id: 'pc:ollama', label: 'PC' }] });
  assert.equal(reloaded.logs.source, 'pc:ollama', 'a source still listed is kept');
  const gone = reduce(s, { type: 'logs/sources/loaded', data: [] });
  assert.equal(gone.logs.source, null);
});

test('logs: options validate, a change clears old text, stale replies dropped, filter', () => {
  let s = reduce(initialState(), { type: 'logs/sources/loaded', data: fx.logSources() });
  s = reduce(s, { type: 'logs/loaded', source: s.logs.source, lines: s.logs.lines, data: fx.logs(), at: 1 });
  assert.equal(s.logs.text.length, 3);
  const same = reduce(s, { type: 'logs/options', source: 'rm -rf', lines: 7 });
  assert.equal(same.logs.source, s.logs.source);
  assert.equal(same.logs.lines, s.logs.lines);
  s = reduce(s, { type: 'logs/options', source: 'pc:ollama' });
  assert.equal(s.logs.text, null);
  assert.equal(reduce(s, { type: 'logs/options', source: 'docker' }).logs.source, 'pc:ollama', 'a source the server did not list is refused');
  const stale = reduce(s, { type: 'logs/loaded', source: 'ollama-cpu', lines: s.logs.lines, data: fx.logs(), at: 2 });
  assert.equal(stale, s);
  s = reduce(s, { type: 'logs/options', auto: true });
  assert.equal(s.logs.auto, true);
  assert.equal(initialState().logs.auto, false, 'auto-refresh is off by default');
  s = reduce(s, { type: 'logs/filter', filter: 'ERROR' });
  assert.equal(s.logs.filter, 'ERROR');
});

test('audit: options reset lists, keys drop stale replies, replay flow', () => {
  let s = initialState();
  s = reduce(s, { type: 'audit/loaded', part: 'sudo', key: auditKey(s, 'sudo'), data: fx.sudo(), at: 1 });
  assert.equal(s.audit.sudo.items.length, 1);
  const oldKey = auditKey(s, 'sudo');
  s = reduce(s, { type: 'audit/options', host: 'pc' });
  assert.equal(s.audit.host, 'pc');
  assert.equal(s.audit.sudo.items, null);
  assert.equal(reduce(s, { type: 'audit/loaded', part: 'sudo', key: oldKey, data: fx.sudo() }), s, 'stale reply dropped');
  assert.equal(reduce(s, { type: 'audit/options', user: 'root' }), s, 'unknown user ignored');
  s = reduce(s, { type: 'audit/options', range: '7d' });
  assert.equal(s.audit.range, '7d');
  const sess = fx.sessions()[0];
  s = reduce(s, { type: 'replay/open', session: sess });
  assert.deepEqual([s.audit.replay.output, s.audit.replay.loading], [false, true], 'command line first');
  s = reduce(s, { type: 'replay/loaded', session: sess, data: fx.replay(false) });
  assert.equal(s.audit.replay.loading, false);
  s = reduce(s, { type: 'replay/output' });
  assert.equal(s.audit.replay.output, true);
  assert.equal(reduce(s, { type: 'replay/loaded', session: sess, output: false, data: { ...fx.replay(false), output: null } }), s, 'a late command-only reply cannot hide the output');
  assert.equal(reduce(s, { type: 'replay/loaded', session: { id: 'other' }, data: {} }), s, 'reply for another session dropped');
  s = reduce(s, { type: 'replay/loaded', session: sess, data: fx.replay(true) });
  assert.match(s.audit.replay.data.output, /Done/);
  s = reduce(s, { type: 'replay/close' });
  assert.equal(s.audit.replay, null);
});

test('audit lists accept arrays or wrapped arrays', () => {
  let s = initialState();
  s = reduce(s, { type: 'audit/loaded', part: 'sessions', key: auditKey(s, 'sessions'), data: fx.sessionsReply(3) });
  assert.equal(s.audit.sessions.items.length, 2);
  assert.equal(s.audit.sessions.skipped, 3);
  for (const bad of ['3', -1, 1.5, null, {}]) {
    const x = reduce(s, { type: 'audit/loaded', part: 'sessions', key: auditKey(s, 'sessions'), data: { sessions: [], skipped: bad } });
    assert.equal(x.audit.sessions.skipped, 0, `skipped ${JSON.stringify(bad)} → 0`);
  }
  s = reduce(s, { type: 'audit/loaded', part: 'localai', key: auditKey(s, 'localai'), data: 'junk' });
  assert.deepEqual(s.audit.localai.items, []);
  assert.equal(reduce(s, { type: 'audit/loaded', part: '__proto__', data: [] }), s);
});

test('auditSince is integer epoch seconds, by range', () => {
  const now = Date.parse('2026-10-26T20:00:00.750Z');
  assert.equal(auditSince('1d', now), Date.parse('2026-10-25T20:00:00Z') / 1000);
  assert.equal(auditSince('7d', now), Date.parse('2026-10-19T20:00:00Z') / 1000);
  assert.equal(auditSince('bogus', now), Date.parse('2026-10-25T20:00:00Z') / 1000);
  assert.ok(Number.isInteger(auditSince('30d', now)));
});

test('filterLines: case-insensitive, all words', () => {
  const lines = ['Error: disk', 'ok', 'error: net down'];
  assert.deepEqual(filterLines(lines, 'ERROR'), ['Error: disk', 'error: net down']);
  assert.deepEqual(filterLines(lines, 'error net'), ['error: net down']);
  assert.deepEqual(filterLines(lines, '  '), lines);
  assert.deepEqual(filterLines(null, 'x'), []);
});

test('normaliseLines: shapes', () => {
  assert.deepEqual(normaliseLines({ lines: ['a', 'b'] }), ['a', 'b']);
  assert.deepEqual(normaliseLines(['a', 7, { line: 'c' }]), ['a', '', 'c']);
  assert.deepEqual(normaliseLines({ text: 'a\nb\n' }), ['a', 'b']);
  assert.deepEqual(normaliseLines(null), []);
});

test('validSessionId', () => {
  assert.equal(validSessionId('00000A'), true);
  assert.equal(validSessionId('claude/00/00/0A'), true);
  assert.equal(validSessionId('../etc/passwd'), false);
  assert.equal(validSessionId('a b'), false);
  assert.equal(validSessionId('x'.repeat(65)), false);
  assert.equal(validSessionId(5), false);
});

test('diskRows, historySeries, historyStats', () => {
  const d = diskRows([{ mount: '/', total: 100, avail: 5 }, { mount: '/x', total: 0, avail: 0 }, null]);
  assert.equal(d.length, 2);
  assert.equal(d[0].low, true);
  assert.equal(d[1].freePct, null);
  const series = historySeries([{ ts: 1, gpu_busy: 90, ollama_share: 10, avg30: 55, available: false }, { ts: 2, gpu_busy: 5, ollama_share: 20, avg30: 120, available: true }]);
  assert.deepEqual(series.map((p) => [p.game, p.avg]), [[80, 55], [0, 100]]);
  const st = historyStats(series);
  assert.deepEqual([st.now, st.peak, st.avg, st.unavailableCount], [0, 80, 100, 1]);
});

test('connection text, data age, offline banner', () => {
  let s = initialState();
  assert.equal(connText(s), 'Connecting…');
  s = reduce(s, { type: 'summary/loaded', summary: fx.normal(), at: fx.NOW - 12 * 60_000 });
  s = reduce(s, { type: 'conn', mode: 'live' });
  assert.equal(connText(s), 'Live');
  assert.equal(bannerText(s, fx.NOW), null, 'no banner while online');
  s = reduce(s, { type: 'summary/failed', kind: 'network' });
  assert.equal(dataAgeMs(s, fx.NOW), 12 * 60_000);
  assert.match(bannerText(s, fx.NOW), /^Can't reach ai-box\. Showing what it reported 12 min ago \(15:48\)\./);
  const empty = reduce(initialState(), { type: 'summary/failed' });
  assert.match(bannerText(empty, fx.NOW), /^Can't reach ai-box\. This device has no saved copy yet/);
  const http = reduce(s, { type: 'summary/failed', kind: 'server', detail: 'HTTP 502' });
  assert.match(bannerText(http, fx.NOW), /^ai-box answered with an error \(HTTP 502\)\. Showing what it reported 12 min ago \(15:48\)\./);
  assert.equal(connText(http), 'Server error');
  assert.equal(connText(s), 'Offline');
  const junk = reduce(s, { type: 'summary/loaded', summary: 'not an object', at: fx.NOW });
  assert.match(bannerText(junk, fx.NOW), /^ai-box answered with an error \(unreadable summary\)\. Showing what it reported 12 min ago/);
  assert.equal(junk.summary, s.summary, 'the last good summary stays');
});
