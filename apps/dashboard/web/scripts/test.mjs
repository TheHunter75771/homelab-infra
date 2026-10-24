#!/usr/bin/env node
// npm test: the whole node:test suite under three time zones (the display must not depend on the
// machine's): UTC, America/New_York (DST, behind UTC) and Pacific/Kiritimati (UTC+14).
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const files = fs.readdirSync(path.join(ROOT, 'test')).filter((f) => f.endsWith('.test.mjs')).map((f) => path.join('test', f));
const zones = (process.env.TEST_TZS || 'UTC,America/New_York,Pacific/Kiritimati').split(',');
const results = [];
for (const tz of zones) {
  console.log(`\n=== TZ=${tz} ===`);
  const r = spawnSync(process.execPath, ['--test', '--test-reporter=spec', ...files], { cwd: ROOT, stdio: 'inherit', env: { ...process.env, TZ: tz } });
  results.push([tz, r.status]);
}
console.log('');
for (const [tz, status] of results) console.log(`${status === 0 ? 'pass' : 'FAIL'}  TZ=${tz}`);
process.exit(results.every(([, s]) => s === 0) ? 0 : 1);
