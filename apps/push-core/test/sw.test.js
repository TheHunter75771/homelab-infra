// Runs public/sw.js in a VM with a fake ServiceWorkerGlobalScope to check the payload contract end to end.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const SW_SOURCE = fs.readFileSync(new URL('../public/sw.js', import.meta.url), 'utf8');
const ORIGIN = 'https://homelab.example';

function loadWorker({ windows = [] } = {}) {
  const handlers = {};
  const shown = [];
  const opened = [];
  const self = {
    location: { origin: ORIGIN },
    addEventListener: (type, fn) => {
      handlers[type] = fn;
    },
    skipWaiting: () => Promise.resolve(),
    registration: {
      showNotification: async (title, options) => {
        shown.push({ title, options });
      },
    },
    clients: {
      claim: async () => {},
      matchAll: async () => windows,
      openWindow: async (url) => {
        opened.push(url);
        return { url };
      },
    },
  };
  vm.runInNewContext(SW_SOURCE, { self, URL, atob, fetch: () => Promise.reject(new Error('offline')), caches: undefined, console });
  return { handlers, shown, opened };
}

async function dispatch(handler, event) {
  const waits = [];
  handler({ ...event, waitUntil: (p) => waits.push(p) });
  await Promise.all(waits);
}

function pushData(value) {
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  return {
    json: () => JSON.parse(text),
    text: () => text,
  };
}

test('push with the contract payload shows title/body/tag and keeps the url', async () => {
  const w = loadWorker();
  await dispatch(w.handlers.push, {
    data: pushData({ title: 'Media drive failed', body: 'Docker stays down.', tag: 'bootgate', url: '/' }),
  });
  assert.equal(w.shown.length, 1);
  const { title, options } = w.shown[0];
  assert.equal(title, 'Media drive failed');
  assert.equal(options.body, 'Docker stays down.');
  assert.equal(options.tag, 'bootgate');
  assert.equal(options.renotify, true);
  assert.equal(options.data.url, `${ORIGIN}/`);
  assert.equal(options.icon, '/icons/icon-192.png');
  assert.equal(options.badge, '/icons/badge-96.png');
});

test('tag null -> no tag/renotify (renotify without tag would throw in browsers)', async () => {
  const w = loadWorker();
  await dispatch(w.handlers.push, { data: pushData({ title: 'T', body: 'B', tag: null, url: '/' }) });
  assert.equal('tag' in w.shown[0].options, false);
  assert.equal('renotify' in w.shown[0].options, false);
});

test('every push shows a notification, even empty or non-JSON ones', async () => {
  const w = loadWorker();
  await dispatch(w.handlers.push, { data: null });
  await dispatch(w.handlers.push, { data: pushData('plain text alert') });
  await dispatch(w.handlers.push, { data: pushData({ title: '', body: 42 }) });
  assert.deepEqual(
    w.shown.map((s) => [s.title, s.options.body]),
    [
      ['Homelab alert', ''],
      ['Homelab alert', 'plain text alert'],
      ['Homelab alert', ''],
    ],
  );
});

test('foreign or malformed urls are replaced by the app root', async () => {
  const w = loadWorker();
  for (const url of ['https://evil.example/phish', '//evil.example/x', 'javascript:alert(1)', 42, null]) {
    await dispatch(w.handlers.push, { data: pushData({ title: 'T', body: 'B', tag: null, url }) });
  }
  for (const s of w.shown) assert.equal(s.options.data.url, `${ORIGIN}/`);
  await dispatch(w.handlers.push, { data: pushData({ title: 'T', body: 'B', tag: null, url: '/watch' }) });
  assert.equal(w.shown.at(-1).options.data.url, `${ORIGIN}/watch`);
});

// A window spy: records focus/navigate calls.
function fakeWindow(url, log) {
  const win = {
    url,
    focus: async () => {
      log.push(['focus', url]);
      return win;
    },
    navigate: async (to) => {
      log.push(['navigate', url, to]);
      return win;
    },
  };
  return win;
}

function click(w, url) {
  let closed = 0;
  const notification = { data: url === undefined ? null : { url }, close: () => (closed += 1) };
  return dispatch(w.handlers.notificationclick, { notification }).then(() => closed);
}

test('notificationclick reuses a window already on the target path, without navigating it', async () => {
  const log = [];
  const w = loadWorker({
    windows: [
      fakeWindow('https://other.example/', log),
      fakeWindow(`${ORIGIN}/sonarr/settings/general`, log),
      fakeWindow(`${ORIGIN}/?from=home-screen`, log),
    ],
  });
  assert.equal(await click(w, `${ORIGIN}/`), 1, 'notification closed');
  assert.deepEqual(log, [['focus', `${ORIGIN}/?from=home-screen`]]);
  assert.deepEqual(w.opened, []);
});

test('notificationclick never focuses or navigates other same-origin apps (Sonarr, Radarr, SABnzbd)', async () => {
  const log = [];
  const w = loadWorker({
    windows: [
      fakeWindow(`${ORIGIN}/sonarr/settings/general`, log),
      fakeWindow(`${ORIGIN}/radarr/`, log),
      fakeWindow(`${ORIGIN}/sabnzbd/config/`, log),
      fakeWindow(`${ORIGIN}/watch/web/`, log),
    ],
  });
  await click(w, `${ORIGIN}/`);
  assert.deepEqual(log, [], 'no other tab was touched');
  assert.deepEqual(w.opened, [`${ORIGIN}/`], 'a new window is opened instead');
});

test('notificationclick with a payload url matches that path exactly', async () => {
  const log = [];
  const w = loadWorker({ windows: [fakeWindow(`${ORIGIN}/`, log), fakeWindow(`${ORIGIN}/watch/web/`, log)] });
  await click(w, `${ORIGIN}/watch/web/`);
  assert.deepEqual(log, [['focus', `${ORIGIN}/watch/web/`]]);

  const log2 = [];
  const w2 = loadWorker({ windows: [fakeWindow(`${ORIGIN}/`, log2)] });
  await click(w2, `${ORIGIN}/sonarr/`);
  assert.deepEqual(log2, [], 'the PWA window is not navigated away either');
  assert.deepEqual(w2.opened, [`${ORIGIN}/sonarr/`]);
});

test('notificationclick without data opens the app root; foreign urls never open', async () => {
  const empty = loadWorker();
  await click(empty, undefined);
  assert.deepEqual(empty.opened, [`${ORIGIN}/`]);
  const foreign = loadWorker();
  await click(foreign, 'https://evil.example/phish');
  assert.deepEqual(foreign.opened, [`${ORIGIN}/`]);
});

test('client app.js and sw.js are valid scripts', () => {
  for (const f of ['../public/app.js', '../public/sw.js']) {
    assert.doesNotThrow(() => new vm.Script(fs.readFileSync(new URL(f, import.meta.url), 'utf8'), { filename: f }));
  }
});
