// Web Push on this device: what the browser allows, and subscribe / unsubscribe / test through the server
// (README "API"). The server key comes from /api/cards → config.vapid_public_key.

import { postJSON } from './api.js';
import { failText } from './model.js';

/**
 * What stops push on this device, or null when nothing does.
 *   insecure     not a secure context (the tailnet address on port 8443 is)
 *   ios-browser  iPhone/iPad Safari tab: iOS only allows push for an app added to the Home Screen
 *   unsupported  no service worker / PushManager / Notification
 *   denied       the user blocked notifications for this site
 */
export function pushEnv(win = globalThis) {
  const nav = win.navigator || {};
  const ua = String(nav.userAgent || '');
  const isIOS = /iPad|iPhone|iPod/.test(ua) || (nav.platform === 'MacIntel' && nav.maxTouchPoints > 1);
  const standalone = Boolean(win.matchMedia?.('(display-mode: standalone)')?.matches) || nav.standalone === true;
  const supported = Boolean(nav.serviceWorker) && 'PushManager' in win && 'Notification' in win;
  let blocked = null;
  if (!win.isSecureContext) blocked = 'insecure';
  else if (isIOS && !standalone) blocked = 'ios-browser';
  else if (!supported) blocked = 'unsupported';
  else if (win.Notification?.permission === 'denied') blocked = 'denied';
  return { isIOS, standalone, supported, blocked };
}

export function deviceLabel(ua = String(globalThis.navigator?.userAgent || '')) {
  if (/iPhone/.test(ua)) return 'iPhone';
  if (/iPad|Macintosh/.test(ua) && globalThis.navigator?.maxTouchPoints > 1) return 'iPad';
  if (/Android/.test(ua)) return /Mobile/.test(ua) ? 'Android phone' : 'Android tablet';
  if (/Macintosh/.test(ua)) return 'Mac';
  if (/Windows/.test(ua)) return 'Windows PC';
  if (/Linux|CrOS/.test(ua)) return 'Linux PC';
  return 'Device';
}

export function b64urlToBytes(value) {
  const b64 = value.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(value.length / 4) * 4, '=');
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

function sameKey(buffer, bytes) {
  if (!buffer) return false;
  const a = new Uint8Array(buffer);
  return a.length === bytes.length && a.every((v, i) => v === bytes[i]);
}

/** The active worker registration, or null if there's none within `ms` (it failed to install). */
export async function registration(ms = 8000) {
  if (!globalThis.navigator?.serviceWorker) return null;
  let timer;
  try {
    return await Promise.race([navigator.serviceWorker.ready, new Promise((r) => (timer = setTimeout(() => r(null), ms)))]);
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/** This device's current subscription, or null. */
export async function currentSubscription() {
  const reg = await registration();
  if (!reg?.pushManager) return null;
  try {
    return await reg.pushManager.getSubscription();
  } catch {
    return null;
  }
}

export const META_CACHE = 'mc-meta';

const serverSaid = (r, what) => `The server couldn't ${what} (${failText(r.fail)}).`;

export const EXPIRED_TEXT = "This device's notifications expired. Turn them on again.";

/** The server says this subscription no longer exists (410 Gone, or 404). */
export function isGone(r) {
  return !r.ok && r.fail.kind === 'http' && (r.fail.status === 410 || r.fail.status === 404);
}

/**
 * Thrown when the server says the subscription is gone. Before throwing, the browser side is dropped too
 * (errors ignored), so the panel goes back to Off and "Enable notifications" makes a fresh one.
 */
export class SubscriptionGone extends Error {
  constructor() {
    super(EXPIRED_TEXT);
    this.gone = true;
  }
}

async function dropLocal(sub) {
  try {
    await sub?.unsubscribe();
  } catch {
    // ignored: the panel goes to Off either way
  }
}

/** Ask permission (must run straight from the tap on iOS), subscribe, register with the server. */
export async function subscribe(vapidPublicKey) {
  if (!vapidPublicKey) throw new Error("The server hasn't sent its push key yet. Refresh and try again.");
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') {
    throw new Error(permission === 'denied' ? 'Notifications are blocked. Allow them in Settings, then try again.' : 'Notification permission was not granted.');
  }
  const reg = await registration();
  if (!reg) throw new Error("The service worker isn't running. Reload and try again.");
  const key = b64urlToBytes(vapidPublicKey);
  let sub = await reg.pushManager.getSubscription();
  if (sub && !sameKey(sub.options?.applicationServerKey, key)) {
    await sub.unsubscribe(); // the server's keys changed
    sub = null;
  }
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
  const label = deviceLabel();
  const r = await postJSON('/api/push/subscribe', { subscription: sub.toJSON(), label });
  if (isGone(r)) {
    await dropLocal(sub);
    throw new SubscriptionGone();
  }
  if (!r.ok) throw new Error(serverSaid(r, 'save this device'));
  try {
    // sw.js reuses the label if the browser replaces the subscription (pushsubscriptionchange)
    await (await caches.open(META_CACHE)).put('/__label', new Response(label));
  } catch {
    // the worker falls back to "Device"
  }
  return sub;
}

/** Remove this device on the server, then in the browser. */
export async function unsubscribe() {
  const sub = await currentSubscription();
  if (!sub) return;
  const r = await postJSON('/api/push/unsubscribe', { endpoint: sub.endpoint });
  // 410/404: the server has already forgotten this device, which is the goal. Any other failure keeps the
  // browser subscription (the panel stays On) so Noah sees the error and can try again.
  if (!r.ok && !isGone(r)) throw new Error(serverSaid(r, 'remove this device'));
  await dropLocal(sub);
}

/** Ask the server to push a test notification to this device. */
export async function sendTest() {
  const sub = await currentSubscription();
  if (!sub) throw new Error("This device isn't subscribed any more. Turn notifications on again.");
  const r = await postJSON('/api/push/test', { endpoint: sub.endpoint });
  if (isGone(r)) {
    await dropLocal(sub);
    throw new SubscriptionGone();
  }
  if (!r.ok) throw new Error(serverSaid(r, 'send a test'));
}
