// Shared test fixtures: temp push dirs, real VAPID keys, realistic subscriptions, a running app.
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { createECDH, randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import webpush from 'web-push';
import { createApp } from '../../src/app.js';
import { createLogger } from '../../src/log.js';
import { createStore } from '../../src/store.js';
import { createVapidSource } from '../../src/vapid.js';

export const APP_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
export const PUBLIC_DIR = path.join(APP_ROOT, 'public');

export function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'push-core-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

export function writeVapid(dir, overrides = {}) {
  const keys = webpush.generateVAPIDKeys();
  const vapid = { publicKey: keys.publicKey, privateKey: keys.privateKey, subject: 'https://example.org/homelab-alerts', ...overrides };
  fs.writeFileSync(path.join(dir, 'vapid.json'), JSON.stringify(vapid));
  return vapid;
}

// A subscription shaped exactly like PushSubscription.toJSON() from a browser.
export function fakeSubscription(host = 'fcm.googleapis.com') {
  const ecdh = createECDH('prime256v1');
  ecdh.generateKeys();
  return {
    endpoint: `https://${host}/fcm/send/${randomBytes(24).toString('base64url')}`,
    expirationTime: null,
    keys: {
      p256dh: ecdh.getPublicKey().toString('base64url'),
      auth: randomBytes(16).toString('base64url'),
    },
  };
}

export function captureLogger() {
  const lines = [];
  const log = createLogger({ level: 'debug', write: (line) => lines.push(line) });
  return { log, lines, text: () => lines.join('\n') };
}

// Stub web-push: records calls; `respond(sub)` returns {statusCode} or throws {statusCode, body}.
export function stubWebpush(respond = () => ({ statusCode: 201 })) {
  const calls = [];
  return {
    calls,
    async sendNotification(subscription, payload, options) {
      calls.push({ subscription, payload, options });
      const r = await respond(subscription, payload, options);
      if (r instanceof Error) throw r;
      return r;
    },
  };
}

export function pushError(statusCode, body = '') {
  return Object.assign(new Error('Received unexpected response code'), { statusCode, body });
}

export async function startApp(t, { dir, webpush: wp = stubWebpush(), vapid = true, ...opts } = {}) {
  const pushDir = dir ?? tempDir(t);
  const keys = vapid ? writeVapid(pushDir) : null;
  const captured = captureLogger();
  const store = createStore({ dir: pushDir, log: captured.log, ...(opts.storeOptions ?? {}) });
  const handler = createApp({
    store,
    vapid: createVapidSource({ file: path.join(pushDir, 'vapid.json') }),
    webpush: wp,
    log: captured.log,
    publicDir: PUBLIC_DIR,
    ...(opts.appOptions ?? {}),
  });
  const server = http.createServer(handler);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const base = `http://127.0.0.1:${server.address().port}`;
  return { base, port: server.address().port, dir: pushDir, store, keys, webpush: wp, ...captured };
}

export const JSON_HEADERS = { 'Content-Type': 'application/json', 'X-Homelab': '1' };

export async function postJson(base, p, body, headers = JSON_HEADERS) {
  const res = await fetch(`${base}${p}`, {
    method: 'POST',
    headers,
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
  const text = await res.text();
  let json = null;
  try {
    json = JSON.parse(text);
  } catch {
    // not JSON
  }
  return { res, status: res.status, json, text };
}

export function readSubs(dir) {
  return JSON.parse(fs.readFileSync(path.join(dir, 'subscriptions.json'), 'utf8'));
}

// Minimal PNG reader: validates signature + chunk CRCs, returns {width, height, colorType, rgba}.
export async function readPng(buf) {
  const zlib = await import('node:zlib');
  const sig = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  if (!buf.subarray(0, 8).equals(sig)) throw new Error('bad PNG signature');
  let off = 8;
  let ihdr = null;
  const idat = [];
  while (off < buf.length) {
    const len = buf.readUInt32BE(off);
    const type = buf.toString('ascii', off + 4, off + 8);
    const data = buf.subarray(off + 8, off + 8 + len);
    const crc = buf.readUInt32BE(off + 8 + len);
    if (zlib.crc32(buf.subarray(off + 4, off + 8 + len)) !== crc) throw new Error(`bad CRC in ${type}`);
    if (type === 'IHDR') ihdr = data;
    if (type === 'IDAT') idat.push(data);
    off += 12 + len;
  }
  const width = ihdr.readUInt32BE(0);
  const height = ihdr.readUInt32BE(4);
  const raw = zlib.inflateSync(Buffer.concat(idat));
  return { width, height, bitDepth: ihdr[8], colorType: ihdr[9], raw };
}
