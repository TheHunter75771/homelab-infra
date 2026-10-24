import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {
  createStore,
  DEFAULT_ALLOWED_HOSTS,
  normalizeLabel,
  parseAllowedHosts,
  StoreCorruptError,
  StoreFullError,
  validateSubscription,
  ValidationError,
} from '../src/push/store.js';
import { deviceId } from '../src/log.js';
import { captureLogger, fakeSubscription, readSubs, tempDir } from './helpers/push-fixtures.js';

const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;

function setup(t, opts = {}) {
  const dir = tempDir(t);
  const captured = captureLogger();
  const store = createStore({ dir, log: captured.log, ...opts });
  return { dir, store, file: path.join(dir, 'subscriptions.json'), ...captured };
}

test('missing file reads as an empty list and is not created by reads', (t) => {
  const { store, file } = setup(t);
  assert.deepEqual(store.list(), []);
  assert.deepEqual(store.devices(), []);
  assert.equal(fs.existsSync(file), false);
});

test('subscribe writes exactly the contract-4 shape with mode 0640', (t) => {
  const { store, dir, file } = setup(t, { now: () => new Date('2026-10-23T12:00:00.000Z') });
  const sub = fakeSubscription();
  const result = store.subscribe(sub, '  Kitchen   iPad ');
  assert.equal(result.created, true);
  const saved = readSubs(dir);
  assert.deepEqual(saved, [
    { endpoint: sub.endpoint, keys: sub.keys, label: 'Kitchen iPad', created: '2026-10-23T12:00:00.000Z' },
  ]);
  assert.deepEqual(Object.keys(saved[0]), ['endpoint', 'keys', 'label', 'created']);
  assert.equal(fs.statSync(file).mode & 0o777, 0o640);
  assert.ok(fs.readFileSync(file, 'utf8').endsWith('\n'));
});

test('dedupe by endpoint: resubscribe updates keys and label, keeps created, never duplicates', (t) => {
  let clock = new Date('2026-01-01T00:00:00.000Z');
  const { store, dir } = setup(t, { now: () => clock });
  const sub = fakeSubscription();
  store.subscribe(sub, 'phone');
  clock = new Date('2026-06-01T00:00:00.000Z');
  const rotated = { ...sub, keys: fakeSubscription().keys };
  const second = store.subscribe(rotated, 'renamed phone');
  assert.equal(second.created, false);
  const saved = readSubs(dir);
  assert.equal(saved.length, 1);
  assert.equal(saved[0].label, 'renamed phone');
  assert.deepEqual(saved[0].keys, rotated.keys);
  assert.equal(saved[0].created, '2026-01-01T00:00:00.000Z');
});

test('identical resubscribe does not rewrite the file', (t) => {
  const { store, file } = setup(t);
  const sub = fakeSubscription();
  store.subscribe(sub, 'phone');
  const before = fs.statSync(file);
  store.subscribe(sub, 'phone');
  const after = fs.statSync(file);
  assert.equal(after.ino, before.ino, 'no rename happened');
  assert.equal(after.mtimeMs, before.mtimeMs);
});

test('duplicates already in the file are collapsed on the next write (first wins)', (t) => {
  const { store, dir, file } = setup(t);
  const a = fakeSubscription();
  const b = fakeSubscription();
  const entry = { endpoint: a.endpoint, keys: a.keys, label: 'first', created: '2026-01-01T00:00:00.000Z' };
  fs.writeFileSync(file, JSON.stringify([entry, { ...entry, label: 'dupe' }]));
  assert.equal(store.devices().length, 1, 'devices view is deduped too');
  store.subscribe(b, 'other');
  const saved = readSubs(dir);
  assert.deepEqual(
    saved.map((e) => e.label),
    ['first', 'other'],
  );
});

test('unsubscribe removes only the matching endpoint; unknown endpoint is a no-op without a write', (t) => {
  const { store, dir, file } = setup(t);
  const a = fakeSubscription();
  const b = fakeSubscription();
  store.subscribe(a, 'a');
  store.subscribe(b, 'b');
  assert.equal(store.unsubscribe(a.endpoint).removed, true);
  assert.deepEqual(
    readSubs(dir).map((e) => e.endpoint),
    [b.endpoint],
  );
  const before = fs.statSync(file);
  assert.equal(store.unsubscribe('https://fcm.googleapis.com/fcm/send/nope').removed, false);
  assert.equal(fs.statSync(file).ino, before.ino);
  assert.throws(() => store.unsubscribe(''), ValidationError);
  assert.throws(() => store.unsubscribe(42), ValidationError);
});

test('removeEndpoints drops dead entries and preserves fields written by other tools', (t) => {
  const { store, dir, file } = setup(t);
  const a = fakeSubscription();
  const b = fakeSubscription();
  const c = fakeSubscription();
  fs.writeFileSync(
    file,
    JSON.stringify([
      { endpoint: a.endpoint, keys: a.keys, label: 'a', created: '2026-01-01T00:00:00Z', extra: { by: 'notifier' } },
      { endpoint: b.endpoint, keys: b.keys, label: 'b', created: '2026-01-01T00:00:00Z' },
      { endpoint: c.endpoint, keys: c.keys, label: 'test', created: '2026-01-01T00:00:00Z' },
    ]),
  );
  assert.equal(store.removeEndpoints([b.endpoint, 'https://not-there.example/x']), 1);
  const saved = readSubs(dir);
  assert.deepEqual(
    saved.map((e) => e.label),
    ['a', 'test'],
  );
  assert.deepEqual(saved[0].extra, { by: 'notifier' });
  assert.equal(store.removeEndpoints([]), 0);
});

test('every mutation re-reads the file, so concurrent edits by the notifier are kept', (t) => {
  const { store, dir, file } = setup(t);
  const a = fakeSubscription();
  const b = fakeSubscription();
  store.subscribe(a, 'a');
  // Host notifier removes `a` behind our back...
  fs.writeFileSync(file, '[]\n');
  store.subscribe(b, 'b');
  // ...and push-core must not resurrect it from a stale cache.
  assert.deepEqual(
    readSubs(dir).map((e) => e.label),
    ['b'],
  );
});

test('atomic write: tmp file in the same dir, fsync before rename, dir fsync after, target never partial', (t) => {
  const dir = tempDir(t);
  const file = path.join(dir, 'subscriptions.json');
  const events = [];
  const old = fakeSubscription();
  fs.writeFileSync(
    file,
    JSON.stringify([{ endpoint: old.endpoint, keys: old.keys, label: 'old', created: '2026-01-01T00:00:00Z' }]),
  );
  const originalText = fs.readFileSync(file, 'utf8');
  const fdPaths = new Map();
  const spyFs = {
    ...fs,
    openSync(p, flags, mode) {
      const fd = fs.openSync(p, flags, mode);
      fdPaths.set(fd, p);
      events.push(['open', path.basename(p), flags]);
      return fd;
    },
    fsyncSync(fd) {
      events.push(['fsync', fdPaths.get(fd) === dir ? '<dir>' : path.basename(fdPaths.get(fd))]);
      return fs.fsyncSync(fd);
    },
    renameSync(from, to) {
      assert.equal(path.dirname(from), dir, 'tmp file lives in the same directory');
      assert.equal(fs.readFileSync(to, 'utf8'), originalText, 'target untouched until rename');
      assert.equal(JSON.parse(fs.readFileSync(from, 'utf8')).length, 2, 'tmp holds the complete new list');
      events.push(['rename', path.basename(from), path.basename(to)]);
      return fs.renameSync(from, to);
    },
  };
  const store = createStore({ dir, fs: spyFs });
  store.subscribe(fakeSubscription(), 'new');

  const tmpName = events.find((e) => e[0] === 'open')[1];
  assert.match(tmpName, /^\.subscriptions\.json\.\d+\.[0-9a-f]{12}\.tmp$/);
  assert.deepEqual(
    events.map((e) => e[0] + (e[1] === '<dir>' ? ':dir' : '')),
    ['open', 'fsync', 'rename', 'open', 'fsync:dir'],
  );
  assert.equal(events[0][2], 'wx', 'tmp is created exclusively');
  assert.deepEqual(fs.readdirSync(dir), ['subscriptions.json'], 'no temp files left behind');
});

for (const failing of ['writeFileSync', 'fsyncSync', 'renameSync']) {
  test(`a crash during ${failing} leaves the original file intact and no temp file`, (t) => {
    const dir = tempDir(t);
    const file = path.join(dir, 'subscriptions.json');
    const old = fakeSubscription();
    fs.writeFileSync(
      file,
      JSON.stringify([{ endpoint: old.endpoint, keys: old.keys, label: 'old', created: '2026-01-01T00:00:00Z' }]),
    );
    const originalText = fs.readFileSync(file, 'utf8');
    const brokenFs = {
      ...fs,
      [failing]: () => {
        throw Object.assign(new Error(`simulated ${failing} failure`), { code: 'EIO' });
      },
    };
    const { log, text } = captureLogger();
    const store = createStore({ dir, fs: brokenFs, log });
    assert.throws(() => store.subscribe(fakeSubscription(), 'new'), /simulated/);
    assert.equal(fs.readFileSync(file, 'utf8'), originalText);
    assert.deepEqual(fs.readdirSync(dir), ['subscriptions.json']);
    assert.match(text(), /store write failed/);
  });
}

const CORRUPT_CASES = {
  'invalid JSON': '[{"endpoint": "https://fcm.googleapis.com/x", ',
  'empty file': '',
  'top-level object': '{"endpoint": "x"}',
  'entry without keys': '[{"endpoint": "https://fcm.googleapis.com/x", "label": "a"}]',
  'non-object entry': '["https://fcm.googleapis.com/x"]',
};

for (const [name, content] of Object.entries(CORRUPT_CASES)) {
  test(`corrupt store (${name}) fails closed: reads throw, writes refuse, bytes untouched`, (t) => {
    const { store, file, text } = setup(t);
    fs.writeFileSync(file, content);
    const before = fs.statSync(file);
    assert.throws(() => store.list(), StoreCorruptError);
    assert.throws(() => store.subscribe(fakeSubscription(), 'x'), StoreCorruptError);
    assert.throws(() => store.unsubscribe('https://fcm.googleapis.com/x'), StoreCorruptError);
    assert.throws(() => store.removeEndpoints(['https://fcm.googleapis.com/x']), StoreCorruptError);
    assert.equal(fs.readFileSync(file, 'utf8'), content);
    assert.equal(fs.statSync(file).ino, before.ino);
    assert.match(text(), /store corrupt; refusing to write/);
  });
}

test('validation rejects malformed or foreign subscriptions before touching the file', (t) => {
  const { store, file } = setup(t);
  const good = fakeSubscription();
  const bad = [
    null,
    [],
    { ...good, endpoint: undefined },
    { ...good, endpoint: 'http://fcm.googleapis.com/fcm/send/x' },
    { ...good, endpoint: 'https://evil.example.com/fcm/send/x' },
    { ...good, endpoint: 'https://fcm.googleapis.com.evil.example/x' },
    { ...good, endpoint: 'https://127.0.0.1/x' },
    { ...good, endpoint: 'https://user:pw@fcm.googleapis.com/x' },
    { ...good, endpoint: 'https://fcm.googleapis.com:8443/x' },
    { ...good, endpoint: 'https://permanently-removed.invalid/fcm/send/x' },
    { ...good, endpoint: `https://fcm.googleapis.com/${'a'.repeat(2100)}` },
    { ...good, keys: undefined },
    { ...good, keys: { ...good.keys, p256dh: 'not base64!' } },
    { ...good, keys: { ...good.keys, p256dh: Buffer.alloc(65, 1).toString('base64url') } },
    { ...good, keys: { ...good.keys, auth: Buffer.alloc(8).toString('base64url') } },
  ];
  for (const sub of bad) assert.throws(() => store.subscribe(sub, 'x'), ValidationError, JSON.stringify(sub)?.slice(0, 80));
  assert.equal(fs.existsSync(file), false);
});

test('real push-service endpoints are accepted', () => {
  for (const host of [
    'fcm.googleapis.com',
    'updates.push.services.mozilla.com',
    'web.push.apple.com',
    'wns2-by3p.notify.windows.com',
  ]) {
    assert.doesNotThrow(() => validateSubscription(fakeSubscription(host)));
  }
});

test('padded base64url keys are normalised to unpadded', () => {
  const sub = fakeSubscription();
  const padded = { ...sub, keys: { p256dh: `${sub.keys.p256dh}=`, auth: `${sub.keys.auth}==` } };
  assert.deepEqual(validateSubscription(padded).keys, sub.keys);
});

test('PUSH_ENDPOINT_HOSTS parsing', () => {
  assert.equal(parseAllowedHosts(undefined), DEFAULT_ALLOWED_HOSTS);
  assert.equal(parseAllowedHosts(' '), DEFAULT_ALLOWED_HOSTS);
  assert.deepEqual(parseAllowedHosts('Push.Example.org, other.net'), ['push.example.org', 'other.net']);
  assert.equal(parseAllowedHosts('*'), null);
  assert.doesNotThrow(() => validateSubscription(fakeSubscription('anything.example'), null));
  assert.doesNotThrow(() => validateSubscription(fakeSubscription('a.push.example.org'), ['push.example.org']));
});

test('labels are cleaned, clipped and defaulted', () => {
  assert.equal(normalizeLabel('  a\tb\nc  '), 'a b c');
  assert.equal(normalizeLabel('x\u0000y\u2028z'), 'x y z');
  assert.equal(normalizeLabel(''), 'device');
  assert.equal(normalizeLabel(undefined), 'device');
  assert.equal(normalizeLabel(42), 'device');
  assert.equal(normalizeLabel('é'.repeat(100)).length, 64);
  assert.equal(normalizeLabel('📱'.repeat(70)), '📱'.repeat(64), 'clips by code point, never splits a surrogate pair');
});

test('subscription cap returns StoreFullError and leaves the file alone', (t) => {
  const { store, dir } = setup(t, { maxEntries: 2 });
  store.subscribe(fakeSubscription(), 'a');
  const keep = fakeSubscription();
  store.subscribe(keep, 'b');
  assert.throws(() => store.subscribe(fakeSubscription(), 'c'), StoreFullError);
  assert.equal(readSubs(dir).length, 2);
  assert.doesNotThrow(() => store.subscribe(keep, 'b renamed'), 'updating an existing entry is still allowed');
});

test('devices view exposes id/label/created only', (t) => {
  const { store } = setup(t);
  const sub = fakeSubscription();
  store.subscribe(sub, 'phone');
  const [d] = store.devices();
  assert.deepEqual(Object.keys(d), ['id', 'label', 'created']);
  assert.equal(d.id, deviceId(sub.endpoint));
  assert.match(d.created, ISO);
});

test('cleanupStaleTmp removes only this service temp files', (t) => {
  const { store, dir } = setup(t);
  fs.writeFileSync(path.join(dir, '.subscriptions.json.123.abcdef012345.tmp'), 'partial');
  fs.writeFileSync(path.join(dir, 'vapid.json'), '{}');
  fs.writeFileSync(path.join(dir, '.notifier-tmp-xyz'), 'not ours');
  assert.equal(store.cleanupStaleTmp(), 1);
  assert.deepEqual(fs.readdirSync(dir).sort(), ['.notifier-tmp-xyz', 'vapid.json']);
});

test('writable() reflects directory permissions', (t) => {
  const { store, dir } = setup(t);
  assert.equal(store.writable(), true);
  if (process.getuid?.() === 0) return; // root ignores mode bits
  fs.chmodSync(dir, 0o500);
  try {
    assert.equal(store.writable(), false);
  } finally {
    fs.chmodSync(dir, 0o700);
  }
});

test('unreadable file (e.g. root 0600 left by another writer) fails closed, never treated as empty', (t) => {
  if (process.getuid?.() === 0) return t.skip('root ignores mode bits');
  const { store, file } = setup(t);
  const old = fakeSubscription();
  fs.writeFileSync(
    file,
    JSON.stringify([{ endpoint: old.endpoint, keys: old.keys, label: 'old', created: '2026-01-01T00:00:00Z' }]),
  );
  fs.chmodSync(file, 0o000);
  try {
    assert.throws(() => store.list(), { code: 'EACCES' });
    assert.throws(() => store.subscribe(fakeSubscription(), 'new'), { code: 'EACCES' });
    assert.throws(() => store.removeEndpoints([old.endpoint]), { code: 'EACCES' });
  } finally {
    fs.chmodSync(file, 0o600);
  }
  assert.equal(JSON.parse(fs.readFileSync(file, 'utf8'))[0].label, 'old', 'file was not replaced');
});

test('removeById removes exactly the device whose public id matches; bad ids are rejected', (t) => {
  const { store, dir } = setup(t);
  const a = fakeSubscription();
  const b = fakeSubscription();
  store.subscribe(a, 'a');
  store.subscribe(b, 'b');
  assert.equal(store.removeById(deviceId(a.endpoint)).removed, true);
  assert.deepEqual(readSubs(dir).map((e) => e.label), ['b']);
  assert.equal(store.removeById('0'.repeat(16)).removed, false);
  for (const bad of ['', 'xyz', 'A'.repeat(16), '0'.repeat(15), 42]) assert.throws(() => store.removeById(bad), ValidationError);
});
