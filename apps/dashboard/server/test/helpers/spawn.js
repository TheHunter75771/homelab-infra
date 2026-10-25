// The ONLY way tests start the real server process (src/server.js). A spawned server runs on the real clock, so
// its fixtures must be built relative to the real time: buildHost(t, s, { now: Date.now() }). This helper refuses
// a host whose fixtures were built more than a minute away from now (the 2026-10-25 CI time bomb: fixtures pinned
// to a fixed NOW drifted into "backup stale" once the calendar moved on). test/timebomb.test.js checks that no
// test spawns the server any other way.
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const SERVER = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../src/server.js');
export const FIXTURE_CLOCK_TOLERANCE_MS = 60_000;

// host: a buildHost() result whose fixtures the process will read, or null when it reads none (config errors).
export function spawnServer(t, { host, args = [], env = {} }) {
  if (host !== null) {
    if (!host || !Number.isFinite(host.now)) throw new Error('spawnServer: pass the buildHost() result (or host: null)');
    if (Math.abs(host.now - Date.now()) > FIXTURE_CLOCK_TOLERANCE_MS) {
      throw new Error('spawnServer: a real server process needs fixtures built at the real time: buildHost(t, s, { now: Date.now() })');
    }
  }
  const child = spawn(process.execPath, [...args, SERVER], {
    // NODE_OPTIONS passes through so a whole run can be time-shifted for checks (see the README, "Tests").
    env: { PATH: process.env.PATH, ...(process.env.NODE_OPTIONS ? { NODE_OPTIONS: process.env.NODE_OPTIONS } : {}), ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  t?.after(() => child.exitCode === null && child.kill('SIGKILL'));
  return child;
}
