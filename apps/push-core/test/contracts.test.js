// Drift guards between this module and the repo's single sources of truth.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { APP_ROOT } from './helpers/fixtures.js';

const read = (p) => fs.readFileSync(path.join(APP_ROOT, p), 'utf8');
const VERSIONS = path.resolve(APP_ROOT, '..', '..', 'inventory', 'group_vars', 'all', 'versions.yml');
const versions = fs.existsSync(VERSIONS) ? fs.readFileSync(VERSIONS, 'utf8') : null;
const noRepo = versions === null ? 'versions.yml not present (module checked out alone)' : false;

function yamlValue(key) {
  const m = versions.match(new RegExp(`^\\s+${key}:\\s*"([^"]+)"\\s*$`, 'm'));
  assert.ok(m, `${key} not found in versions.yml`);
  return m[1];
}

const dockerfile = read('Dockerfile');
const pkg = JSON.parse(read('package.json'));
const lock = JSON.parse(read('package-lock.json'));

test('Dockerfile base image equals images.node_base', { skip: noRepo }, () => {
  const m = dockerfile.match(/^ARG NODE_BASE=(\S+)$/m);
  assert.ok(m, 'ARG NODE_BASE default missing');
  assert.equal(m[1], yamlValue('node_base'));
  assert.match(m[1], /@sha256:[0-9a-f]{64}$/, 'pinned by digest');
});

test('package.json version equals the local_images.push_core tag', { skip: noRepo }, () => {
  const image = yamlValue('push_core');
  assert.equal(image.split(':').at(-1), pkg.version);
});

test('Dockerfile: multi-stage, prod-only install, non-root 10101, healthcheck, no npm at runtime', () => {
  const froms = dockerfile.match(/^FROM .+$/gm);
  assert.deepEqual(froms, ['FROM ${NODE_BASE} AS deps', 'FROM ${NODE_BASE}']);
  assert.match(dockerfile, /RUN npm ci --omit=dev/);
  assert.match(dockerfile, /^USER 10101:10101$/m);
  assert.match(dockerfile, /^HEALTHCHECK .*\n\s+CMD \["node", "\/app\/src\/healthcheck\.js"\]$/m);
  assert.match(dockerfile, /^CMD \["node", "\/app\/src\/server\.js"\]$/m);
  assert.match(dockerfile, /^EXPOSE 8085$/m);
  assert.doesNotMatch(dockerfile, /^VOLUME/m, 'no anonymous volumes; compose bind-mounts /data');
  assert.doesNotMatch(dockerfile, /^#\s*syntax\s*=/im, 'no BuildKit frontend directive: it pulls an unpinned image');
  assert.match(dockerfile, /^ARG NODE_BASE=/m, 'base image arrives as build arg NODE_BASE (stacks compose wiring)');
  const ignore = read('.dockerignore');
  for (const entry of ['node_modules', 'test']) assert.match(ignore, new RegExp(`^${entry}$`, 'm'));
});

test('the only runtime dependency is web-push, pinned exactly, and the lockfile agrees', () => {
  assert.deepEqual(Object.keys(pkg.dependencies), ['web-push']);
  assert.match(pkg.dependencies['web-push'], /^\d+\.\d+\.\d+$/, 'exact version, no range');
  assert.equal(pkg.devDependencies, undefined, 'tests use node:test only');
  assert.equal(lock.lockfileVersion, 3);
  assert.deepEqual(lock.packages[''].dependencies, pkg.dependencies);
  assert.equal(lock.packages['node_modules/web-push'].version, pkg.dependencies['web-push']);
});

test('README documents the payload contract the notifier must send', () => {
  const readme = read('README.md');
  assert.match(readme, /## Payload contract/);
  assert.match(readme, /"title": "Media drive failed"/);
  for (const field of ['title', 'body', 'tag', 'url']) assert.match(readme, new RegExp(`\\| \`${field}\` \\|`));
  assert.match(readme, /aes128gcm/);
});
