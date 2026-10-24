// Web Push on this device, carried over from push-core's page: environment checks, subscribe and
// unsubscribe through the push-core API (CONTRACTS §5, kept as-is in §11.3), and this device's id.

import { getJSON, post } from './api.js';

export const LABEL_KEY = 'homelab-alerts.label';
const META_CACHE = 'push-core-meta'; // the service worker reads /__label from here on pushsubscriptionchange

export function pushEnv(win = globalThis) {
  const nav = win.navigator || {};
  const ua = nav.userAgent || '';
  const isIOS = /iPad|iPhone|iPod/.test(ua) || (nav.platform === 'MacIntel' && nav.maxTouchPoints > 1);
  const standalone = Boolean(win.matchMedia?.('(display-mode: standalone)')?.matches) || nav.standalone === true;
  const supported = Boolean(nav.serviceWorker) && 'PushManager' in win && 'Notification' in win;
  let blocked = null;
  if (!win.isSecureContext) blocked = 'insecure';
  else if (isIOS && !standalone) blocked = 'ios-browser';
  else if (!supported) blocked = 'unsupported';
  else if (win.Notification?.permission === 'denied') blocked = 'denied';
  return { ua, isIOS, standalone, supported, blocked };
}

export const BLOCKED_REASON = {
  insecure: 'Push needs the https:// tailnet address',
  'ios-browser': 'Add to Home Screen first, then subscribe from there',
  unsupported: "This browser can't receive Web Push",
  denied: 'Notifications are blocked for this site in settings',
  worker: "The service worker didn't start. Reload to retry",
};

export function guessLabel(env = pushEnv()) {
  const ua = env.ua;
  let device = 'Device';
  if (/iPhone/.test(ua)) device = 'iPhone';
  else if (env.isIOS) device = 'iPad';
  else if (/Android/.test(ua)) device = /Mobile/.test(ua) ? 'Android phone' : 'Android tablet';
  else if (/CrOS/.test(ua)) device = 'Chromebook';
  else if (/Macintosh/.test(ua)) device = 'Mac';
  else if (/Windows/.test(ua)) device = 'Windows PC';
  else if (/Linux/.test(ua)) device = 'Linux PC';
  if (env.isIOS && env.standalone) return device;
  let browser = '';
  if (/Edg(A|iOS)?\//.test(ua)) browser = 'Edge';
  else if (/Firefox\/|FxiOS/.test(ua)) browser = 'Firefox';
  else if (/SamsungBrowser/.test(ua)) browser = 'Samsung Internet';
  else if (/OPR\//.test(ua)) browser = 'Opera';
  else if (/Chrome\/|CriOS/.test(ua)) browser = 'Chrome';
  else if (/Safari\//.test(ua)) browser = 'Safari';
  return browser ? `${device} · ${browser}` : device;
}

// Icon for a device row, from its label.
export function deviceIcon(label) {
  const l = String(label || '').toLowerCase();
  if (/ipad|tablet/.test(l)) return 'device-tablet';
  if (/iphone|phone|android|pixel|galaxy/.test(l)) return 'device-mobile';
  if (/mac|pc|laptop|windows|linux|chromebook|book/.test(l)) return 'laptop';
  return 'desktop';
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

// Same id the server derives: first 16 hex chars of SHA-256(endpoint).
export async function deviceIdFor(endpoint) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(endpoint));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0'))
    .join('')
    .slice(0, 16);
}

export function storageGet(key) {
  try {
    return globalThis.localStorage?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

export function storageSet(key, value) {
  try {
    globalThis.localStorage?.setItem(key, value);
  } catch {
    // private mode etc.
  }
}

async function stashLabelForWorker(label) {
  try {
    const cache = await caches.open(META_CACHE);
    await cache.put('/__label', new Response(label));
  } catch {
    // Cache API unavailable; the worker falls back to "device"
  }
}

export async function registration() {
  if (!globalThis.navigator?.serviceWorker) return null;
  try {
    return await navigator.serviceWorker.ready;
  } catch {
    return null;
  }
}

// This device's subscription id (or null).
export async function currentDeviceId() {
  const reg = await registration();
  if (!reg || !('pushManager' in reg)) return null;
  try {
    const sub = await reg.pushManager.getSubscription();
    return sub ? await deviceIdFor(sub.endpoint) : null;
  } catch {
    return null;
  }
}

// Ask permission (must run straight from the tap on iOS), subscribe, register with the server.
export async function subscribeThisDevice(label) {
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') {
    throw new Error(
      permission === 'denied'
        ? 'Notifications are blocked. Allow them in settings, then try again.'
        : 'Notification permission was not granted.',
    );
  }
  const reg = await registration();
  if (!reg) throw new Error("The service worker isn't running. Reload and try again.");
  const { data } = await getJSON('/api/vapid-public-key');
  if (!data?.publicKey) throw new Error('The server has no push keys configured yet.');
  const applicationServerKey = b64urlToBytes(data.publicKey);
  let sub = await reg.pushManager.getSubscription();
  if (sub && !sameKey(sub.options && sub.options.applicationServerKey, applicationServerKey)) {
    await sub.unsubscribe(); // the server keys were rotated
    sub = null;
  }
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey });
  const clean = String(label || '').trim();
  const res = await post('/api/subscribe', { subscription: sub.toJSON(), label: clean });
  const finalLabel = res.device?.label || clean;
  storageSet(LABEL_KEY, finalLabel);
  await stashLabelForWorker(finalLabel);
  return { id: await deviceIdFor(sub.endpoint), created: Boolean(res.created), devices: res.devices };
}

// Drop the browser side of this device's subscription (after the server removed it).
export async function forgetLocalSubscription() {
  const reg = await registration();
  if (!reg) return;
  try {
    const sub = await reg.pushManager.getSubscription();
    if (sub) await sub.unsubscribe();
  } catch {
    // the server side is what matters
  }
}
