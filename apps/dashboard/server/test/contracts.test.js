// Drift guards: package pins, and the interfaces shared with module H (role dashboard + helper).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { CONFIG_KEYS, ENV_KEYS } from '../src/config.js';
import { matchRoute } from '../src/http/app.js';
import { STREAM_OPS, UNARY_OPS } from '../src/helper/client.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const REPO = path.resolve(ROOT, '../../..');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');
const repoFile = (p) => {
  const f = path.join(REPO, p);
  return fs.existsSync(f) ? fs.readFileSync(f, 'utf8') : null;
};
const pkg = JSON.parse(read('package.json'));
const lock = JSON.parse(read('package-lock.json'));

test('only runtime dependency: web-push 3.6.7 pinned exactly; lockfile agrees; MIT', () => {
  assert.deepEqual(pkg.dependencies, { 'web-push': '3.6.7' });
  assert.equal(pkg.devDependencies, undefined, 'tests use node:test only');
  assert.equal(pkg.license, 'MIT');
  assert.equal(pkg.type, 'module');
  assert.equal(pkg.main, 'src/server.js');
  assert.equal(lock.lockfileVersion, 3);
  assert.deepEqual(lock.packages[''].dependencies, pkg.dependencies);
  assert.equal(lock.packages[''].license, 'MIT');
  assert.equal(lock.packages['node_modules/web-push'].version, '3.6.7');
});

test("push-core's API is still served at the same paths", () => {
  for (const [method, p] of [
    ['GET', '/healthz'],
    ['GET', '/api/vapid-public-key'],
    ['POST', '/api/subscribe'],
    ['POST', '/api/unsubscribe'],
    ['POST', '/api/test'],
  ]) {
    assert.ok(matchRoute(method, p).route, `${method} ${p}`);
  }
});

const hDefaults = repoFile('roles/dashboard/defaults/main.yml');
const hJson = repoFile('roles/dashboard/templates/config.json.j2');
const hHelper = repoFile('roles/dashboard/files/homelab-dockerctl/homelab_dockerctl.py');

test('role dashboard runs this entry point', { skip: hDefaults === null && 'role dashboard not present' }, () => {
  const block = hDefaults.match(/^dashboard_server_entry:[^\n]*(\n {2,}[^\n]*)*/m)?.[0] ?? '';
  // either package.json "main" (with this default) or a literal path
  const literal = block.match(/^dashboard_server_entry:\s*([\w./-]+)\s*$/m)?.[1];
  if (literal) assert.equal(literal, pkg.main);
  else assert.match(block, /\.main \| default\('src\/server\.js'\)/);
  assert.ok(fs.existsSync(path.join(ROOT, pkg.main)));
});

test('every key role dashboard renders into config.json is one the server reads', { skip: hJson === null && 'config.json.j2 not present' }, () => {
  const rendered = [...hJson.matchAll(/^\s*'([a-z_]+)':/gm)].map((m) => m[1]);
  assert.ok(rendered.length >= 10, 'parsed the template');
  for (const k of rendered) assert.ok(k in CONFIG_KEYS, `${k} is rendered but the server would ignore it`);
});

test("the client's ops are exactly the helper's ops", { skip: hHelper === null && 'helper not present' }, () => {
  const block = hHelper.match(/^OPS: [^\n]*\n([\s\S]*?)^}/m)[1];
  const helperOps = [...block.matchAll(/^\s+"([a-z-]+)":/gm)].map((m) => m[1]).sort();
  assert.deepEqual([...UNARY_OPS, ...STREAM_OPS].sort(), helperOps);
  const streams = hHelper.match(/^STREAM_OPS = frozenset\(\{([^}]*)\}\)/m)[1];
  assert.deepEqual([...streams.matchAll(/"([a-z-]+)"/g)].map((m) => m[1]).sort(), [...STREAM_OPS].sort());
});

test('README documents the API, the summary schema and the helper protocol', () => {
  const readme = read('README.md');
  for (const heading of ['## HTTP API', '## Summary schema', '## Configuration', '## Helper protocol']) assert.ok(readme.includes(heading), heading);
  for (const route of ['/api/summary', '/api/events', '/api/apps/:name/logs', '/api/actions/:id', '/api/gate/rerun', '/api/push/devices', '/api/settings']) {
    assert.ok(readme.includes(route), route);
  }
  for (const v of Object.keys(ENV_KEYS)) assert.ok(readme.includes(v), `${v} documented`);
});
