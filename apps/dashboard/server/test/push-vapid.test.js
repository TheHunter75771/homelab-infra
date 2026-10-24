import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import webpush from 'web-push';
import { createVapidSource, parseVapid, VapidError } from '../src/push/vapid.js';
import { tempDir } from './helpers/push-fixtures.js';

const keys = webpush.generateVAPIDKeys();
const good = { publicKey: keys.publicKey, privateKey: keys.privateKey, subject: 'mailto:admin@localhost' };

test('parses the contract-4 vapid.json', () => {
  assert.deepEqual({ ...parseVapid(JSON.stringify(good)) }, good);
  assert.doesNotThrow(() => parseVapid(JSON.stringify({ ...good, subject: 'https://example.org/contact' })));
});

test('rejects malformed vapid.json without echoing key material', () => {
  const cases = [
    'not json',
    '[]',
    JSON.stringify({ ...good, publicKey: undefined }),
    JSON.stringify({ ...good, publicKey: keys.privateKey }),
    JSON.stringify({ ...good, privateKey: keys.publicKey }),
    JSON.stringify({ ...good, privateKey: `${keys.privateKey}==` }),
    JSON.stringify({ ...good, subject: 'admin@localhost' }),
    JSON.stringify({ ...good, subject: 'http://example.org' }),
  ];
  for (const text of cases) {
    assert.throws(
      () => parseVapid(text),
      (e) =>
        e instanceof VapidError &&
        e.reason === 'invalid' &&
        !e.message.includes(keys.privateKey) &&
        !e.message.includes(keys.publicKey),
      text,
    );
  }
});

test('source reports missing / unreadable and reloads when the file changes', (t) => {
  const dir = tempDir(t);
  const file = path.join(dir, 'vapid.json');
  const source = createVapidSource({ file });
  assert.throws(() => source.get(), (e) => e instanceof VapidError && e.reason === 'missing');

  fs.writeFileSync(file, JSON.stringify(good));
  assert.equal(source.get().publicKey, good.publicKey);
  assert.equal(source.get(), source.get(), 'cached while unchanged');

  const rotated = webpush.generateVAPIDKeys();
  const tmp = `${file}.new`;
  fs.writeFileSync(tmp, JSON.stringify({ ...good, ...rotated }));
  fs.renameSync(tmp, file);
  assert.equal(source.get().publicKey, rotated.publicKey);

  if (process.getuid?.() !== 0) {
    fs.chmodSync(file, 0o000);
    try {
      assert.throws(() => source.get(), (e) => e.reason === 'unreadable');
    } finally {
      fs.chmodSync(file, 0o600);
    }
  }
});
