import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createDecipheriv, createECDH, hkdfSync, randomBytes } from 'node:crypto';
import webpush from 'web-push';
import { buildPayload, GONE_STATUS, SEND_OPTIONS, sendToAll, TEST_MESSAGE } from '../src/push/sender.js';
import { captureLogger, pushError, stubWebpush } from './helpers/push-fixtures.js';

// A browser-side subscription whose private key we keep, so we can decrypt what web-push produces.
function browserSubscription(host = 'fcm.googleapis.com') {
  const ecdh = createECDH('prime256v1');
  ecdh.generateKeys();
  const auth = randomBytes(16);
  return {
    ecdh,
    auth,
    subscription: {
      endpoint: `https://${host}/fcm/send/${randomBytes(16).toString('base64url')}`,
      keys: { p256dh: ecdh.getPublicKey().toString('base64url'), auth: auth.toString('base64url') },
    },
  };
}

// RFC 8291 / RFC 8188 (aes128gcm) decryption, single record: what the browser does on receipt.
function decryptAes128gcm(body, ecdh, auth) {
  const salt = body.subarray(0, 16);
  const idlen = body[20];
  const serverPublic = body.subarray(21, 21 + idlen);
  const ciphertext = body.subarray(21 + idlen);
  const shared = ecdh.computeSecret(serverPublic);
  const keyInfo = Buffer.concat([Buffer.from('WebPush: info\0'), ecdh.getPublicKey(), serverPublic]);
  const ikm = Buffer.from(hkdfSync('sha256', shared, auth, keyInfo, 32));
  const cek = Buffer.from(hkdfSync('sha256', ikm, salt, Buffer.from('Content-Encoding: aes128gcm\0'), 16));
  const nonce = Buffer.from(hkdfSync('sha256', ikm, salt, Buffer.from('Content-Encoding: nonce\0'), 12));
  const decipher = createDecipheriv('aes-128-gcm', cek, nonce);
  decipher.setAuthTag(ciphertext.subarray(-16));
  const padded = Buffer.concat([decipher.update(ciphertext.subarray(0, -16)), decipher.final()]);
  const end = padded.lastIndexOf(2); // last-record delimiter, then zero padding
  return padded.subarray(0, end).toString('utf8');
}

test('payload contract: exactly {title, body, tag, url} with tag nullable', () => {
  assert.deepEqual(JSON.parse(buildPayload(TEST_MESSAGE)), {
    title: 'Test from homelab',
    body: 'Push alerts are working on this device.',
    tag: 'test',
    url: '/',
  });
  const noTag = JSON.parse(buildPayload({ title: 'T', body: 'B' }));
  assert.deepEqual(noTag, { title: 'T', body: 'B', tag: null, url: '/' });
  assert.deepEqual(Object.keys(noTag), ['title', 'body', 'tag', 'url']);
});

test('real web-push accepts our options and produces a request a browser can decrypt (no network)', () => {
  // Deployed form (group var vapid_subject): an https URL, which Apple accepts.
  const vapid = { ...webpush.generateVAPIDKeys(), subject: 'https://example.org/homelab-alerts' };
  const { ecdh, auth, subscription } = browserSubscription('web.push.apple.com');
  const payload = buildPayload({ title: 'Media drive failed', body: 'Docker stays down.', tag: 'bootgate' });
  const details = webpush.generateRequestDetails(subscription, payload, {
    ...SEND_OPTIONS,
    vapidDetails: vapid,
  });
  assert.equal(details.method, 'POST');
  assert.equal(details.endpoint, subscription.endpoint);
  assert.equal(details.headers['Content-Encoding'], 'aes128gcm');
  assert.equal(details.headers.TTL, 3600);
  assert.equal(details.headers.Urgency, 'high');
  assert.equal(details.timeout, 10000);

  const auth_ = details.headers.Authorization;
  const [, jwt, k] = auth_.match(/^vapid t=([^,]+), k=(.+)$/);
  assert.equal(k, vapid.publicKey);
  const claims = JSON.parse(Buffer.from(jwt.split('.')[1], 'base64url').toString());
  assert.equal(claims.aud, 'https://web.push.apple.com');
  assert.equal(claims.sub, 'https://example.org/homelab-alerts');
  assert.ok(claims.exp > Date.now() / 1000);

  const plaintext = decryptAes128gcm(details.body, ecdh, auth);
  assert.deepEqual(JSON.parse(plaintext), {
    title: 'Media drive failed',
    body: 'Docker stays down.',
    tag: 'bootgate',
    url: '/',
  });
});

test('sendToAll counts results and reports only 404/410 as gone', async () => {
  const subs = [201, 404, 410, 400, 403, 413, 429, 500].map((code) => ({
    code,
    ...browserSubscription().subscription,
  }));
  const wp = stubWebpush((sub) => {
    const { code } = subs.find((s) => s.endpoint === sub.endpoint);
    return code < 300 ? { statusCode: code } : pushError(code);
  });
  const { log, text } = captureLogger();
  const vapid = { ...webpush.generateVAPIDKeys(), subject: 'mailto:admin@localhost' };
  const r = await sendToAll({ entries: subs, payload: buildPayload(TEST_MESSAGE), vapid, webpush: wp, log });
  assert.equal(r.total, 8);
  assert.equal(r.sent, 1);
  assert.equal(r.failed, 7);
  assert.deepEqual(
    r.gone,
    subs.filter((s) => GONE_STATUS.has(s.code)).map((s) => s.endpoint),
  );
  for (const s of subs) assert.ok(!text().includes(s.endpoint));
  assert.ok(!text().includes(vapid.privateKey));
});

test('sendToAll survives network errors (no statusCode) and sends in parallel', async () => {
  let inFlight = 0;
  let peak = 0;
  const wp = {
    async sendNotification() {
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await new Promise((r) => setTimeout(r, 10));
      inFlight -= 1;
      throw Object.assign(new Error('getaddrinfo ENOTFOUND fcm.googleapis.com'), { code: 'ENOTFOUND' });
    },
  };
  const entries = [browserSubscription().subscription, browserSubscription().subscription];
  const { log, text } = captureLogger();
  const r = await sendToAll({
    entries,
    payload: '{}',
    vapid: { publicKey: 'p', privateKey: 'k', subject: 'mailto:a@b' },
    webpush: wp,
    log,
  });
  assert.deepEqual(r, { total: 2, sent: 0, failed: 2, gone: [] });
  assert.equal(peak, 2);
  assert.match(text(), /"status":null/);
});
