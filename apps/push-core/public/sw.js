'use strict';

// Service worker for Homelab alerts.
// Payload contract (see README): JSON {"title": str, "body": str, "tag": str|null, "url": "/"}.
// Every push shows a notification (Safari revokes subscriptions that receive silent pushes).

const DEFAULT_TITLE = 'Homelab alert';
const META_CACHE = 'push-core-meta';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

function parsePayload(data) {
  if (!data) return {};
  try {
    const value = data.json();
    return value && typeof value === 'object' ? value : {};
  } catch {
    return { body: data.text() };
  }
}

// Only same-origin targets; anything else opens the app root.
function safeUrl(url) {
  try {
    const target = new URL(typeof url === 'string' && url ? url : '/', self.location.origin);
    return target.origin === self.location.origin ? target.href : new URL('/', self.location.origin).href;
  } catch {
    return new URL('/', self.location.origin).href;
  }
}

self.addEventListener('push', (event) => {
  const payload = parsePayload(event.data);
  const title = typeof payload.title === 'string' && payload.title.trim() ? payload.title : DEFAULT_TITLE;
  const options = {
    body: typeof payload.body === 'string' ? payload.body : '',
    icon: '/icons/icon-192.png',
    badge: '/icons/badge-96.png',
    data: { url: safeUrl(payload.url) },
  };
  if (typeof payload.tag === 'string' && payload.tag) {
    options.tag = payload.tag; // replaces an earlier notification with the same tag
    options.renotify = true; // ...and still alerts
  }
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = safeUrl(event.notification.data && event.notification.data.url);
  const target = new URL(url);
  event.waitUntil(
    (async () => {
      // Sonarr, Radarr, Prowlarr and SABnzbd share this origin under tailscale serve. Only reuse a window
      // that is already exactly on the target path; never navigate another tab (it may hold unsaved settings).
      const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
      for (const client of windows) {
        const current = new URL(client.url);
        if (current.origin === target.origin && current.pathname === target.pathname) return client.focus();
      }
      return self.clients.openWindow(url);
    })(),
  );
});

function b64urlToBytes(value) {
  const b64 = value.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(value.length / 4) * 4, '=');
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

async function post(path, body) {
  return fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Homelab': '1' },
    body: JSON.stringify(body),
    credentials: 'same-origin',
  });
}

// The browser rotated or expired the subscription: register the new one so alerts keep arriving.
self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil(
    (async () => {
      let sub = event.newSubscription;
      if (!sub) {
        const res = await fetch('/api/vapid-public-key', { cache: 'no-store' });
        if (!res.ok) return;
        const { publicKey } = await res.json();
        sub = await self.registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: b64urlToBytes(publicKey),
        });
      }
      let label = 'device';
      try {
        const cached = await (await caches.open(META_CACHE)).match('/__label');
        if (cached) label = (await cached.text()) || label;
      } catch {
        // keep default
      }
      await post('/api/subscribe', { subscription: sub.toJSON(), label });
      if (event.oldSubscription && event.oldSubscription.endpoint !== sub.endpoint) {
        await post('/api/unsubscribe', { endpoint: event.oldSubscription.endpoint });
      }
    })().catch(() => {}),
  );
});
