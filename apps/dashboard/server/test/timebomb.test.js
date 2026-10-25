// Guards against fixture time bombs (CI, 2026-10-25): tests that run on the REAL clock must build their fixtures
// from the real time, not from the fixed NOW the in-process tests are anchored to.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildHost, helperFor, NOW, shifter } from './helpers/host.js';
import { spawnServer } from './helpers/spawn.js';

const TEST_DIR = path.dirname(fileURLToPath(import.meta.url));
const testFiles = fs.readdirSync(TEST_DIR).filter((f) => f.endsWith('.test.js')).map((f) => [f, fs.readFileSync(path.join(TEST_DIR, f), 'utf8')]);

test('no test starts the server process except through helpers/spawn.js (which checks the fixture clock)', () => {
  for (const [file, src] of testFiles) {
    assert.doesNotMatch(src, /spawn\(\s*process\.execPath/, `${file}: use spawnServer() from helpers/spawn.js`);
    assert.doesNotMatch(src, /path\.(?:resolve|join)\([^)]*src\/server\.js/, `${file}: build the server path only in helpers/spawn.js`);
  }
});

test('every in-test createDashboard passes an explicit clock (no silent real clock on NOW fixtures)', () => {
  for (const [file, src] of testFiles) {
    for (const m of src.matchAll(/createDashboard\(([^;]*?)\);/gs)) {
      assert.match(m[1], /\bclock\s*:/, `${file}: createDashboard(...) without a clock: ${m[0].slice(0, 120)}`);
    }
  }
});

test('spawnServer refuses fixtures built at the fixed NOW, and accepts real-time ones', (t) => {
  const pinned = buildHost(t, 'normal'); // now = NOW: right for the injected clock, a time bomb for a real process
  assert.throws(() => spawnServer(t, { host: pinned, env: {} }), /fixtures built at the real time/);
  assert.throws(() => spawnServer(t, { host: undefined, env: {} }), /pass the buildHost\(\) result/);
  const fresh = buildHost(t, 'normal', { now: Date.now() });
  const child = spawnServer(t, { host: fresh, env: { DASHBOARD_CONFIG: path.join(fresh.root, 'missing.json'), PORT: '0', HOST: '127.0.0.1', PUSH_DIR: fresh.pushDir } });
  child.kill('SIGKILL');
});

test('fixtures: byte-identical at NOW, and every instant keeps its offset from a real now', (t) => {
  const { at, day } = shifter(NOW);
  assert.equal(at('2026-10-24T03:12:00Z'), '2026-10-24T03:12:00Z');
  assert.equal(at('2026-10-24T02:30:00.000Z'), '2026-10-24T02:30:00.000Z');
  assert.equal(day('2026-11-16'), '2026-11-16');
  const later = NOW + 200 * 86_400_000 + 3_600_000;
  const s2 = shifter(later);
  assert.equal(Date.parse(s2.at('2026-10-24T03:12:00Z')) - later, Date.parse('2026-10-24T03:12:00Z') - NOW);
  // a real-time host: the backup is as fresh relative to now as the pinned one is to NOW
  const real = Date.now();
  const host = buildHost(t, 'normal', { now: real });
  const status = JSON.parse(fs.readFileSync(host.cfg.backupStatusFile, 'utf8'));
  assert.equal(real - Date.parse(status.last_ok), NOW - Date.parse('2026-10-24T03:12:00Z'));
  const gate = fs.readFileSync(path.join(host.cfg.gateDir, 'history.jsonl'), 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  assert.ok(gate.every((e) => Date.parse(e.ts) <= real), 'no gate event from the future');
  const boots = helperFor('normal', real).boots;
  assert.equal(boots.at(-1).last_entry, new Date(real).toISOString());
});
