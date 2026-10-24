// Fan-out of one payload to many subscriptions through web-push (injected, so tests need no network).
import { redactEndpoint, sanitizeText } from './log.js';

// Payload contract shared with the host notifier (pywebpush) and the heartbeat: see README.md.
export function buildPayload({ title, body, tag = null, url = '/' }) {
  return JSON.stringify({ title: String(title), body: String(body), tag: tag === null ? null : String(tag), url });
}

export const TEST_MESSAGE = Object.freeze({
  title: 'Test from homelab',
  body: 'Push alerts are working on this device.',
  tag: 'test',
  url: '/',
});

export const SEND_OPTIONS = Object.freeze({
  TTL: 3600, // seconds a push service keeps an undelivered message
  urgency: 'high',
  contentEncoding: 'aes128gcm', // RFC 8291; required by Apple
  timeout: 10000, // ms per request
});

// Push services answer 404/410 for subscriptions that no longer exist; anything else may be transient.
export const GONE_STATUS = new Set([404, 410]);

export async function sendToAll({ entries, payload, vapid, webpush, log, options = SEND_OPTIONS }) {
  const vapidDetails = { subject: vapid.subject, publicKey: vapid.publicKey, privateKey: vapid.privateKey };
  const results = await Promise.allSettled(
    entries.map(async (e) =>
      webpush.sendNotification({ endpoint: e.endpoint, keys: e.keys }, payload, { ...options, vapidDetails }),
    ),
  );
  const gone = [];
  let sent = 0;
  results.forEach((r, i) => {
    const device = redactEndpoint(entries[i].endpoint);
    if (r.status === 'fulfilled') {
      sent += 1;
      log?.info('push delivered', { device, status: r.value?.statusCode ?? null });
      return;
    }
    const status = Number.isInteger(r.reason?.statusCode) ? r.reason.statusCode : null;
    if (GONE_STATUS.has(status)) gone.push(entries[i].endpoint);
    log?.warn('push failed', {
      device,
      status,
      gone: GONE_STATUS.has(status),
      reason: sanitizeText(r.reason?.body || r.reason?.message),
    });
  });
  return { total: entries.length, sent, failed: entries.length - sent, gone };
}
