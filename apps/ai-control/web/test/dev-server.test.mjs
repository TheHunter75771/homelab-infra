// The preview server (scripts/dev-server.mjs) picks the fake summary by the scenario name from the query
// string. Every valid name serves its fixture; any other name, including an Object.prototype member like
// "constructor" or "__proto__", is just unknown and serves the normal summary. The old object-literal lookup
// called the prototype member instead, or crashed the server (CodeQL js/unvalidated-dynamic-method-call).

import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import * as fx from './fixtures.mjs';

const SERVER = fileURLToPath(new URL('../scripts/dev-server.mjs', import.meta.url));
const SCENARIOS = { normal: fx.normal, gaming: fx.gamingCpu, pcoff: fx.pcOff, down: fx.everythingDown, hostile: fx.hostileSummary };
const PROTO = ['constructor', '__proto__', 'toString', 'hasOwnProperty', 'valueOf', 'isPrototypeOf', 'propertyIsEnumerable', 'toLocaleString', '__defineGetter__', '__lookupGetter__'];

/** Starts the preview server on a free port; it is stopped when the test ends. */
async function start(t) {
  const child = spawn(process.execPath, [SERVER, '--port', '0'], { stdio: ['ignore', 'pipe', 'pipe'] });
  t.after(() => child.kill());
  let log = '';
  child.stderr.on('data', (d) => (log += d));
  const port = await new Promise((resolve, reject) => {
    child.stdout.on('data', (d) => {
      log += d;
      const m = /127\.0\.0\.1:(\d+)/.exec(log);
      if (m) resolve(Number(m[1]));
    });
    child.on('exit', (code, signal) => reject(new Error(`the preview server exited (${code ?? signal}): ${log}`)));
  });
  const get = async (p) => {
    const r = await fetch(`http://127.0.0.1:${port}${p}`);
    return { status: r.status, body: await r.json() };
  };
  return { child, get, log: () => log };
}

// The server moves epoch-second timestamps to now, so those may differ; every other value must be equal.
function same(a, b) {
  if (typeof a === 'number' && typeof b === 'number') return a === b || (a >= 1e9 && b >= 1e9);
  if (Array.isArray(a) || Array.isArray(b)) return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((x, i) => same(x, b[i]));
  if (a && b && typeof a === 'object' && typeof b === 'object') {
    const keys = Object.keys(a);
    return keys.length === Object.keys(b).length && keys.every((k) => Object.hasOwn(b, k) && same(a[k], b[k]));
  }
  return a === b;
}
const json = (f) => JSON.parse(JSON.stringify(f()));

test('the comparison tells every scenario apart (so the checks below mean something)', () => {
  const names = Object.keys(SCENARIOS);
  for (const a of names) for (const b of names) assert.equal(same(json(SCENARIOS[a]), json(SCENARIOS[b])), a === b, `${a} vs ${b}`);
});

test('preview server: each scenario serves its summary; unknown and Object.prototype names serve the normal one', { timeout: 30_000 }, async (t) => {
  const { child, get, log } = await start(t);
  const use = async (name) => assert.deepEqual((await get(`/__scenario?name=${encodeURIComponent(name)}`)).body, { scenario: name });
  for (const [name, fixture] of Object.entries(SCENARIOS)) {
    await use(name);
    const r = await get('/api/summary');
    assert.equal(r.status, 200, name);
    assert.ok(same(r.body, json(fixture)), `${name}: got ${JSON.stringify(r.body).slice(0, 200)}`);
  }
  for (const name of ['no-such-scenario', ...PROTO]) {
    await use(name);
    const r = await get('/api/summary');
    assert.equal(r.status, 200, `${name}: ${log()}`);
    assert.ok(same(r.body, json(fx.normal)), `${name}: got ${JSON.stringify(r.body).slice(0, 200)}`);
  }
  assert.equal(child.exitCode, null, `the server is still running: ${log()}`);
});
