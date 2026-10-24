// The saved real-server summaries (test/server-summaries/*.json) must match what apps/dashboard/server's
// code produces now. Regenerated in memory from the server's own test harness (read-only, deterministic);
// skipped with a message when the server's dependencies aren't installed.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { serverAvailable, buildSummaries, OUT } from '../scripts/dump-server-summaries.mjs';

const SKIP = serverAvailable() ? false : 'apps/dashboard/server is not installed here (npm ci in apps/dashboard/server to enable the drift check)';

function firstDifference(a, b) {
  const x = a.split('\n');
  const y = b.split('\n');
  for (let i = 0; i < Math.max(x.length, y.length); i += 1) {
    if (x[i] !== y[i]) return `line ${i + 1}: saved ${JSON.stringify(x[i] ?? '(none)')} vs server ${JSON.stringify(y[i] ?? '(none)')}`;
  }
  return null;
}

test('test/server-summaries/*.json match the server code (else: run npm run dump-summaries)', { skip: SKIP }, async () => {
  const fresh = await buildSummaries();
  const saved = fs
    .readdirSync(OUT)
    .filter((f) => f.endsWith('.json'))
    .map((f) => f.slice(0, -5))
    .sort();
  assert.deepEqual(saved, Object.keys(fresh).sort(), 'the set of scenarios changed: run npm run dump-summaries');
  const drift = [];
  for (const [name, text] of Object.entries(fresh)) {
    const diff = firstDifference(fs.readFileSync(path.join(OUT, `${name}.json`), 'utf8'), text);
    if (diff) drift.push(`${name}.json, ${diff}`);
  }
  assert.ok(!drift.length, `the server's summaries changed: run npm run dump-summaries\n${drift.join('\n')}`);
});

test('a re-dump is byte-identical (fixed clock, fixed device endpoints)', { skip: SKIP }, async () => {
  const a = await buildSummaries();
  const b = await buildSummaries();
  assert.deepEqual(a, b);
});
