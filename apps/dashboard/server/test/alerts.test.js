// Alert states: a recovery with a failure qualifier is not a recovery (bootgate.py sends
// "Media drive back, Docker did not start" when the drive is back but Docker failed).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classifyAlerts, isRecovery, isSelfRecovered } from '../src/collectors/alerts.js';

const NOW = Date.parse('2026-10-24T14:00:00Z');
let minute = 0;
const at = () => new Date(Date.parse('2026-10-24T02:00:00Z') + 60_000 * minute++).toISOString();
const alert = (title, tag = 'bootgate') => ({ ts: at(), title, body: '', tag, delivered: 1, failed: 0 });
// classifyAlerts returns newest first; map back to send order
const states = (raw) => classifyAlerts(raw, NOW).reverse().map((a) => [a.title, a.state]);

test('lost -> "back, Docker did not start": that alert is active and the earlier "lost" stays unresolved', () => {
  assert.deepEqual(states([alert('Media drive lost'), alert('Media drive back, Docker did not start')]), [
    ['Media drive lost', 'active'],
    ['Media drive back, Docker did not start', 'active'],
  ]);
});

test('lost -> "back, Docker did not start" -> "back": everything with that tag resolved', () => {
  assert.deepEqual(states([alert('Media drive lost'), alert('Media drive back, Docker did not start'), alert('Media drive back')]), [
    ['Media drive lost', 'resolved'],
    ['Media drive back, Docker did not start', 'resolved'],
    ['Media drive back', 'resolved'],
  ]);
});

test('failed -> "back, Docker did not start" -> lost: nothing resolved', () => {
  assert.deepEqual(
    states([alert('Media drive failed'), alert('Media drive back, Docker did not start'), alert('Media drive lost')]).map((x) => x[1]),
    ['active', 'active', 'active'],
  );
});

test('"[REHEARSAL] " prefixes: the same rules, prefix or not', () => {
  assert.deepEqual(states([alert('[REHEARSAL] Media drive lost'), alert('[REHEARSAL] Media drive back, Docker did not start')]), [
    ['[REHEARSAL] Media drive lost', 'active'],
    ['[REHEARSAL] Media drive back, Docker did not start', 'active'],
  ]);
  assert.deepEqual(
    states([alert('[REHEARSAL] Media drive lost'), alert('[REHEARSAL] Media drive back, Docker did not start'), alert('[REHEARSAL] Media drive back')]).map((x) => x[1]),
    ['resolved', 'resolved', 'resolved'],
  );
  for (const t of ['[REHEARSAL] Media drive back', '[rehearsal]   Media drive back', ' [REHEARSAL]Media drive back']) assert.equal(isRecovery(t), true, t);
  assert.equal(isRecovery('[REHEARSAL] Media drive back, Docker did not start'), false);
});

test('a recovery only resolves alerts with its own tag', () => {
  assert.deepEqual(states([alert('Media drive lost'), alert('jellyfin unhealthy', 'alertfloor-jellyfin'), alert('jellyfin recovered', 'alertfloor-jellyfin')]), [
    ['Media drive lost', 'active'],
    ['jellyfin unhealthy', 'resolved'],
    ['jellyfin recovered', 'resolved'],
  ]);
});

// Every recovery title the system sends today, and titles that must never count as one.
const RECOVERIES = [
  'Media drive back', // bootgate.py, Docker started
  'jellyfin recovered', // homelab_alertfloor.py: "<subject> recovered"
  'Docker recovered',
  'mediaserver back online', // homelab_heartbeat.py
  'Incident resolved',
  'sonarr healthy again',
  'Backups OK again',
  'Media drive back.',
];
const NOT_RECOVERIES = [
  // caught by the qualifier guard (they end with a recovery word)
  'Docker not recovered',
  'Media drive still not back',
  'jellyfin never recovered',
  "Docker hasn't recovered",
  'Media drive partially back',
  'Backups failed, not resolved',
  // caught by the end guard (no qualifier word, but more follows the recovery word)
  'Media drive back, Docker start later',
  'Media drive back; Docker held',
  // both
  'Media drive back, Docker did not start',
  'Media drive back but Docker failed',
  'Media drive back (Docker didn’t start)',
  // plain problems
  'Media drive lost',
  'Media drive failed',
  'mediaserver offline',
  'Backup failed',
  'jellyfin unhealthy',
  '',
];

test('isRecovery: every real recovery title is one; nothing with a qualifier or trailing clause is', () => {
  for (const t of RECOVERIES) assert.equal(isRecovery(t), true, t);
  for (const t of NOT_RECOVERIES) assert.equal(isRecovery(t), false, t);
  assert.equal(isRecovery(undefined), false);
});

test('each guard is needed on its own (the lists hold cases only one guard catches)', () => {
  // qualifier-only cases end with a recovery word...
  for (const t of ['Docker not recovered', 'Media drive still not back', 'jellyfin never recovered', "Docker hasn't recovered"]) {
    assert.match(t, /(?:back|recovered|online|resolved|again)$/, t);
  }
  // ...and end-only cases carry no qualifier word
  for (const t of ['Media drive back, Docker start later', 'Media drive back; Docker held']) {
    assert.doesNotMatch(t, /\b(?:not|no|still|but|fail\w*)\b|n't/i, t);
    assert.equal(isRecovery(t), false, t);
  }
});

test('every qualifier word on its own blocks a recovery', () => {
  const words = ['not', 'no', 'never', 'unable', 'cannot', 'fail', 'fails', 'failed', 'failing', 'failure', 'error', 'errors', 'still', 'but',
    'however', 'except', 'partial', 'partially', 'degraded', 'without', 'pending', "didn't", "isn't", "won't", 'can’t'];
  for (const w of words) {
    const title = `Docker ${w}, media drive back`; // ends with a recovery word; the only qualifier is `w`
    assert.equal(isRecovery(title), false, title);
  }
  assert.equal(isRecovery('Docker down, media drive back'), true, 'control: no qualifier word, so a recovery');
});

// ---- "<app> crashed and restarted" (alert floor, CONTRACTS §4 amendments) ---------------------------------------
// B's real format (roles/notifier/files/homelab_alertfloor.py): incidents use per-condition tags
// "alertfloor:<app>:unhealthy|down|restarts", recoveries "<app> recovered" on the same tag, and the crash notice
// "<app> crashed and restarted" on "alertfloor:<app>:crash".

const at2 = (title, tag, body = '') => ({ ...alert(title, tag), body });
const crash = (prefix = '', body = 'Exit code 137 (out of memory); Docker restarted it.') =>
  at2(`${prefix}jellyfin crashed and restarted`, 'alertfloor:jellyfin:crash', body);
const full = (raw) => classifyAlerts(raw, NOW).reverse().map((a) => [a.title, a.state, a.note]);

test('"crashed and restarted" is informational: resolved, "Recovered on its own", never active', () => {
  assert.deepEqual(full([crash()]), [['jellyfin crashed and restarted', 'resolved', 'Recovered on its own']]);
  assert.deepEqual(full([crash('[REHEARSAL] ', 'Exit code 1; Docker restarted it. 2 crashes since the last check.')]), [
    ['[REHEARSAL] jellyfin crashed and restarted', 'resolved', 'Recovered on its own'],
  ]);
  assert.equal(isSelfRecovered('Docker crashed and restarted.'), true, 'by title alone');
  assert.equal(isSelfRecovered('jellyfin restart notice', 'alertfloor:jellyfin:crash'), true, 'by the :crash tag alone');
});

test('"crashed and restarted" neither resolves an open incident nor opens one', () => {
  const unhealthy = () => at2('jellyfin unhealthy', 'alertfloor:jellyfin:unhealthy', 'Health check failing on 2 consecutive checks.');
  const recovered = () => at2('jellyfin recovered', 'alertfloor:jellyfin:unhealthy', 'Cleared: jellyfin unhealthy.');
  // B's real tags: the open incident stays open
  assert.deepEqual(full([unhealthy(), crash()]).map((x) => x[1]), ['active', 'resolved']);
  assert.deepEqual(full([unhealthy(), crash(), recovered()]).map((x) => x[1]), ['resolved', 'resolved', 'resolved']);
  assert.deepEqual(full([crash(), recovered()]).map((x) => x.slice(1)), [
    ['resolved', 'Recovered on its own'],
    ['resolved', null],
  ]);
  // even sharing a tag (defensive), it resolves nothing and opens nothing
  const same = (t) => alert(t, 'alertfloor:jellyfin');
  assert.deepEqual(full([same('jellyfin unhealthy'), same('jellyfin crashed and restarted')]).map((x) => x[1]), ['active', 'resolved']);
  // with the rehearsal prefix
  assert.deepEqual(full([at2('[REHEARSAL] jellyfin unhealthy', 'alertfloor:jellyfin:unhealthy'), crash('[REHEARSAL] ')]).map((x) => x[1]), ['active', 'resolved']);
});

test('"crashed and restarted" with a qualifier or a trailing clause follows the normal rules (stays active)', () => {
  for (const t of ['jellyfin crashed and restarted, still failing', 'jellyfin crashed and restarted but not healthy', 'jellyfin crashed and restarted twice, now down']) {
    assert.equal(isSelfRecovered(t), false, t);
    assert.deepEqual(full([alert(t, 'alertfloor:jellyfin:down')]).map((x) => x[1]), ['active'], t);
  }
  // ends with the phrase but carries a qualifier: only the qualifier guard catches it (also on the :crash tag)
  assert.equal(isSelfRecovered('jellyfin (unhealthy, not recovered) crashed and restarted'), false);
  assert.equal(isSelfRecovered('jellyfin crash loop, not recovered', 'alertfloor:jellyfin:crash'), false);
  assert.equal(isSelfRecovered('jellyfin crashed'), false);
  assert.equal(isSelfRecovered('jellyfin restarted'), false);
  assert.equal(isSelfRecovered('jellyfin unhealthy', 'alertfloor:jellyfin:unhealthy'), false);
});

test('every alert has a note field (null unless self-recovered)', () => {
  const out = classifyAlerts([alert('Media drive lost'), alert('Media drive back'), alert('Test', 'test')], NOW);
  assert.ok(out.every((a) => 'note' in a && a.note === null));
});
