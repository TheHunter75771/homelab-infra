// npm run test:time-travel [ISO-date...]: runs the whole suite as if it were each given moment (default: 40 days
// and 220 days from now). Every process (the runner, each test file, each spawned server) gets the same Date
// offset through NODE_OPTIONS (helpers/spawn.js passes it on). Exits non-zero if any run fails.
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const shim = pathToFileURL(path.join(root, 'test/tools/time-shift.mjs')).href;
const day = 86_400_000;
const targets = process.argv.slice(2).length ? process.argv.slice(2).map((d) => Date.parse(d)) : [Date.now() + 40 * day, Date.now() + 220 * day];
let failed = 0;
for (const target of targets) {
  if (!Number.isFinite(target)) throw new Error('usage: time-travel.mjs [ISO-date ...]');
  const offset = target - Date.now();
  process.stdout.write(`\n== the suite as of ${new Date(target).toISOString()}\n`);
  const r = spawnSync(process.execPath, ['--test', 'test/*.test.js'], {
    cwd: root,
    stdio: 'inherit',
    env: { ...process.env, NODE_OPTIONS: `${process.env.NODE_OPTIONS ?? ''} --import ${shim}?offset=${offset}`.trim() },
  });
  if (r.status !== 0) failed += 1;
}
process.exit(failed ? 1 : 0);
