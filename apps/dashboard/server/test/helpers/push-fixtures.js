// Push fixtures migrated from push-core's test helpers (store, VAPID, sender, logs).
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createECDH, randomBytes } from 'node:crypto';
import webpush from 'web-push';
import { createLogger } from '../../src/log.js';

export function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dash-push-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

export function writeVapid(dir, overrides = {}) {
  const keys = webpush.generateVAPIDKeys();
  const vapid = { publicKey: keys.publicKey, privateKey: keys.privateKey, subject: 'https://example.org/homelab-alerts', ...overrides };
  fs.writeFileSync(path.join(dir, 'vapid.json'), JSON.stringify(vapid));
  return vapid;
}

export function fakeSubscription(host = 'fcm.googleapis.com') {
  const ecdh = createECDH('prime256v1');
  ecdh.generateKeys();
  return {
    endpoint: `https://${host}/fcm/send/${randomBytes(24).toString('base64url')}`,
    expirationTime: null,
    keys: { p256dh: ecdh.getPublicKey().toString('base64url'), auth: randomBytes(16).toString('base64url') },
  };
}

export function captureLogger() {
  const lines = [];
  const log = createLogger({ level: 'debug', write: (line) => lines.push(line) });
  return { log, lines, text: () => lines.join('\n') };
}

// Stub web-push: records calls; `respond(sub)` returns {statusCode} or an Error to throw.
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

export function readSubs(dir) {
  return JSON.parse(fs.readFileSync(path.join(dir, 'subscriptions.json'), 'utf8'));
}
