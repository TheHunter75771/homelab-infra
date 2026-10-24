'use strict';

// Homelab alerts PWA. Talks only to its own origin; every POST carries X-Homelab: 1 (server CSRF guard).
(() => {
  const $ = (id) => document.getElementById(id);
  const els = {
    state: $('state'),
    hint: $('hint'),
    hintTitle: $('hint-title'),
    hintBody: $('hint-body'),
    label: $('label'),
    subscribe: $('subscribe'),
    unsubscribe: $('unsubscribe'),
    test: $('test'),
    status: $('status'),
    devices: $('devices'),
    empty: $('empty'),
    count: $('count'),
  };

  const LABEL_KEY = 'homelab-alerts.label';
  const META_CACHE = 'push-core-meta';

  let devices = [];
  let storeError = false;
  try {
    const data = JSON.parse($('devices-data').textContent);
    devices = Array.isArray(data.devices) ? data.devices : [];
    storeError = Boolean(data.storeError);
  } catch {
    // server always embeds valid JSON; keep defaults otherwise
  }

  const ua = navigator.userAgent;
  const isIOS = /iPad|iPhone|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const standalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;

  let registration = null;
  let currentSub = null;
  let currentId = null;
  let blocked = null; // reason subscribing is impossible on this device, or null
  let busy = false;

  // ---- helpers --------------------------------------------------------------------------------

  function setStatus(text, kind) {
    els.status.textContent = text || '';
    els.status.className = `status${kind ? ` ${kind}` : ''}`;
  }

  function showHint(title, body) {
    els.hintTitle.textContent = title;
    els.hintBody.textContent = body;
    els.hint.hidden = false;
  }

  function storageGet(key) {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function storageSet(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch {
      // private mode etc.
    }
  }

  // The service worker reads this when the browser rotates the subscription (pushsubscriptionchange).
  async function stashLabelForWorker(label) {
    try {
      const cache = await caches.open(META_CACHE);
      await cache.put('/__label', new Response(label));
    } catch {
      // Cache API unavailable; the worker falls back to "device"
    }
  }

  function guessLabel() {
    let device = 'Device';
    if (/iPhone/.test(ua)) device = 'iPhone';
    else if (isIOS) device = 'iPad';
    else if (/Android/.test(ua)) device = /Mobile/.test(ua) ? 'Android phone' : 'Android tablet';
    else if (/CrOS/.test(ua)) device = 'Chromebook';
    else if (/Macintosh/.test(ua)) device = 'Mac';
    else if (/Windows/.test(ua)) device = 'Windows PC';
    else if (/Linux/.test(ua)) device = 'Linux PC';
    if (isIOS && standalone) return device;
    let browser = '';
    if (/Edg(A|iOS)?\//.test(ua)) browser = 'Edge';
    else if (/Firefox\/|FxiOS/.test(ua)) browser = 'Firefox';
    else if (/SamsungBrowser/.test(ua)) browser = 'Samsung Internet';
    else if (/OPR\//.test(ua)) browser = 'Opera';
    else if (/Chrome\/|CriOS/.test(ua)) browser = 'Chrome';
    else if (/Safari\//.test(ua)) browser = 'Safari';
    return browser ? `${device} · ${browser}` : device;
  }

  function b64urlToBytes(value) {
    const b64 = value.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(value.length / 4) * 4, '=');
    return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  }

  function sameKey(buffer, bytes) {
    if (!buffer) return false;
    const a = new Uint8Array(buffer);
    return a.length === bytes.length && a.every((v, i) => v === bytes[i]);
  }

  // Same id the server derives: first 16 hex chars of SHA-256(endpoint).
  async function deviceIdFor(endpoint) {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(endpoint));
    return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('').slice(0, 16);
  }

  async function post(path, body) {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Homelab': '1' },
      body: JSON.stringify(body || {}),
      credentials: 'same-origin',
      cache: 'no-store',
    });
    let data = {};
    try {
      data = await res.json();
    } catch {
      // non-JSON error page
    }
    if (!res.ok) {
      const err = new Error(data.error || `Server answered ${res.status}`);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function formatDate(iso) {
    const d = iso ? new Date(iso) : null;
    if (!d || Number.isNaN(d.getTime())) return '';
    return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  }

  // ---- rendering ------------------------------------------------------------------------------

  function renderDevices() {
    els.devices.replaceChildren(
      ...devices.map((d) => {
        const li = document.createElement('li');
        const name = document.createElement('span');
        name.className = 'dev-label';
        name.textContent = d.label || 'device';
        if (currentId && d.id === currentId) {
          const badge = document.createElement('span');
          badge.className = 'badge';
          badge.textContent = 'This device';
          name.append(badge);
        }
        const meta = document.createElement('span');
        meta.className = 'dev-meta';
        meta.textContent = formatDate(d.created);
        li.append(name, meta);
        return li;
      }),
    );
    els.empty.hidden = devices.length > 0;
    els.count.textContent = devices.length ? `(${devices.length})` : '';
  }

  function render() {
    const registered = Boolean(currentSub) && devices.some((d) => d.id === currentId);
    els.state.textContent = registered ? 'Subscribed' : 'Not subscribed';
    els.state.classList.toggle('on', registered);
    els.subscribe.hidden = registered;
    els.subscribe.textContent = currentSub && !registered ? 'Re-register' : 'Subscribe';
    els.unsubscribe.hidden = !currentSub;
    els.subscribe.disabled = busy || Boolean(blocked) || storeError;
    els.unsubscribe.disabled = busy;
    els.test.disabled = busy || devices.length === 0;
    els.label.disabled = busy;
    renderDevices();
  }

  // ---- actions --------------------------------------------------------------------------------

  async function subscribe() {
    if (busy || blocked) return;
    busy = true;
    render();
    setStatus('Subscribing…');
    try {
      // iOS only shows the prompt when this runs directly from the tap, so it comes first.
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') {
        throw new Error(
          permission === 'denied'
            ? 'Notifications are blocked. Allow them in settings, then try again.'
            : 'Notification permission was not granted.',
        );
      }
      const keyRes = await fetch('/api/vapid-public-key', { cache: 'no-store' });
      if (!keyRes.ok) throw new Error('The server has no push keys configured yet.');
      const { publicKey } = await keyRes.json();
      const applicationServerKey = b64urlToBytes(publicKey);

      let sub = await registration.pushManager.getSubscription();
      if (sub && !sameKey(sub.options && sub.options.applicationServerKey, applicationServerKey)) {
        await sub.unsubscribe(); // server keys were rotated
        sub = null;
      }
      if (!sub) sub = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey });

      const label = els.label.value.trim();
      const data = await post('/api/subscribe', { subscription: sub.toJSON(), label });
      currentSub = sub;
      currentId = await deviceIdFor(sub.endpoint);
      devices = data.devices || devices;
      els.label.value = data.device ? data.device.label : label;
      storageSet(LABEL_KEY, els.label.value);
      await stashLabelForWorker(els.label.value);
      setStatus(data.created ? 'Subscribed. Tap "Send test" to check.' : 'Subscription updated.', 'ok');
    } catch (err) {
      setStatus(err.message || 'Could not subscribe.', 'error');
    } finally {
      busy = false;
      render();
    }
  }

  async function unsubscribe() {
    if (busy) return;
    busy = true;
    render();
    setStatus('Unsubscribing…');
    try {
      const sub = currentSub || (registration && (await registration.pushManager.getSubscription()));
      if (sub) {
        const data = await post('/api/unsubscribe', { endpoint: sub.endpoint });
        devices = data.devices || devices;
        try {
          await sub.unsubscribe();
        } catch {
          // server side is what matters; the browser drops it eventually
        }
      }
      currentSub = null;
      currentId = null;
      setStatus('Unsubscribed. This device will no longer get alerts.', 'ok');
    } catch (err) {
      setStatus(err.message || 'Could not unsubscribe.', 'error');
    } finally {
      busy = false;
      render();
    }
  }

  async function sendTest() {
    if (busy) return;
    busy = true;
    render();
    setStatus('Sending…');
    try {
      const data = await post('/api/test', {});
      devices = data.devices || devices;
      if (data.total === 0) {
        setStatus('No devices are subscribed yet.', 'error');
      } else {
        const removed = data.removed ? ` Removed ${data.removed} expired.` : '';
        setStatus(`Sent to ${data.sent} of ${data.total} device${data.total === 1 ? '' : 's'}.${removed}`, 'ok');
      }
    } catch (err) {
      if (err.status === 429) setStatus('Wait a few seconds before sending another test.', 'error');
      else if (err.status === 502 && err.data) {
        devices = err.data.devices || devices;
        setStatus(`Delivery failed for all ${err.data.total} devices. Check the server log.`, 'error');
      } else setStatus(err.message || 'Could not send the test.', 'error');
    } finally {
      busy = false;
      render();
    }
  }

  // ---- startup --------------------------------------------------------------------------------

  async function init() {
    els.label.value = storageGet(LABEL_KEY) || guessLabel();

    if (!window.isSecureContext) {
      blocked = 'insecure';
      showHint('Open the secure address', 'Push needs HTTPS. Open this page through its tailnet https:// address.');
    } else if (isIOS && !standalone) {
      blocked = 'ios-browser';
      showHint(
        'Add to Home Screen first',
        'On iPhone and iPad, alerts only work from the Home Screen app (iOS 16.4 or later). ' +
          'In Safari tap Share, then "Add to Home Screen", then open Homelab alerts from your Home Screen and subscribe there.',
      );
    } else if (!supported) {
      blocked = 'unsupported';
      showHint('Not supported here', 'This browser does not support Web Push. Try Chrome, Edge, Firefox or Safari.');
    } else if (Notification.permission === 'denied') {
      blocked = 'denied';
      showHint(
        'Notifications are blocked',
        'Allow notifications for this site in your browser or system settings, then reload this page.',
      );
    }

    if (storeError) {
      setStatus('The server cannot read its subscription list. Ask the admin to check subscriptions.json.', 'error');
    }

    if ('serviceWorker' in navigator && window.isSecureContext && blocked !== 'ios-browser') {
      try {
        registration = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
        await navigator.serviceWorker.ready;
        if ('PushManager' in window) currentSub = await registration.pushManager.getSubscription();
        if (currentSub) currentId = await deviceIdFor(currentSub.endpoint);
      } catch (err) {
        blocked = blocked || 'worker';
        console.warn('service worker registration failed', err);
        setStatus('Could not start the service worker, so this browser cannot receive alerts. Reload to retry.', 'error');
      }
    }

    const mine = devices.find((d) => d.id === currentId);
    if (mine && !storageGet(LABEL_KEY)) els.label.value = mine.label;
    render();
  }

  els.subscribe.addEventListener('click', subscribe);
  els.unsubscribe.addEventListener('click', unsubscribe);
  els.test.addEventListener('click', sendTest);
  init();
})();
