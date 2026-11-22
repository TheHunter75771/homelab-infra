// push.js: what blocks push on a device (iOS in a Safari tab, denied, insecure), and the subscribe /
// test / unsubscribe calls with fake browser APIs.

import test from 'node:test';
import assert from 'node:assert/strict';
import { imp, fx } from './render.mjs';
import { installDom, allElements } from './dom-shim.mjs';

const push = await imp('js/push.js');
const app = await imp('js/app.js');

const IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1';
const ANDROID = 'Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36';

function fakeWin({ ua = ANDROID, secure = true, standalone = false, sw = true, permission = 'default', platform = 'Linux', touch = 5 } = {}) {
  const w = { isSecureContext: secure, navigator: { userAgent: ua, platform, maxTouchPoints: touch }, matchMedia: (q) => ({ matches: standalone && q.includes('standalone') }) };
  if (sw) {
    w.navigator.serviceWorker = {};
    w.PushManager = function PushManager() {};
    w.Notification = { permission };
  }
  return w;
}

test('pushEnv: iPhone in a Safari tab must add to the Home Screen first; standalone is fine', () => {
  assert.equal(push.pushEnv(fakeWin({ ua: IPHONE, sw: false })).blocked, 'ios-browser');
  assert.equal(push.pushEnv(fakeWin({ ua: IPHONE, sw: true })).blocked, 'ios-browser', 'even when the APIs exist');
  const ok = push.pushEnv(fakeWin({ ua: IPHONE, standalone: true }));
  assert.equal(ok.blocked, null);
  assert.equal(ok.isIOS, true);
  const ipad = push.pushEnv(fakeWin({ ua: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15', platform: 'MacIntel', touch: 5 }));
  assert.equal(ipad.blocked, 'ios-browser', 'iPadOS reports as a Mac with touch');
});

test('pushEnv: insecure, unsupported and denied', () => {
  assert.equal(push.pushEnv(fakeWin({ secure: false })).blocked, 'insecure');
  assert.equal(push.pushEnv(fakeWin({ sw: false })).blocked, 'unsupported');
  assert.equal(push.pushEnv(fakeWin({ permission: 'denied' })).blocked, 'denied');
  assert.equal(push.pushEnv(fakeWin({ permission: 'granted' })).blocked, null);
  assert.equal(push.pushEnv({}).blocked, 'insecure', 'an empty window is blocked, not a crash');
});

test('b64urlToBytes decodes the VAPID key shape (65 bytes, uncompressed point)', () => {
  const bytes = push.b64urlToBytes(fx.VAPID);
  assert.equal(bytes.length, 65);
  assert.deepEqual([...push.b64urlToBytes('AQID')], [1, 2, 3]);
  assert.deepEqual([...push.b64urlToBytes('-_8')], [251, 255]);
});

function installBrowser({ permission = 'granted', existing = null, postStatus = 200, startSubscribed = false, unsubscribeThrows = false } = {}) {
  const posts = [];
  const meta = new Map();
  let subscribed = existing;
  const sub = (key) => ({
    endpoint: 'https://push.example.test/abc',
    options: { applicationServerKey: key.buffer },
    toJSON: () => ({ endpoint: 'https://push.example.test/abc', keys: { p256dh: 'x', auth: 'y' } }),
    unsubscribe: async () => {
      if (unsubscribeThrows) throw new Error('browser refused');
      subscribed = null;
      return true;
    },
  });
  if (startSubscribed) subscribed = sub(push.b64urlToBytes(fx.VAPID));
  const reg = {
    pushManager: {
      getSubscription: async () => subscribed,
      subscribe: async ({ applicationServerKey, userVisibleOnly }) => {
        assert.equal(userVisibleOnly, true);
        subscribed = sub(applicationServerKey);
        return subscribed;
      },
    },
  };
  const saved = {};
  for (const k of ['navigator', 'Notification', 'fetch', 'caches']) saved[k] = Object.getOwnPropertyDescriptor(globalThis, k);
  Object.defineProperty(globalThis, 'navigator', { value: { serviceWorker: { ready: Promise.resolve(reg) }, userAgent: IPHONE, maxTouchPoints: 5 }, configurable: true, writable: true });
  globalThis.Notification = { requestPermission: async () => permission };
  globalThis.fetch = async (p, init) => {
    if (init?.method !== 'POST') return new Response(JSON.stringify(p === '/api/cards' ? fx.cards() : fx.briefing()), { status: 200 });
    posts.push({ path: p, headers: init.headers, body: JSON.parse(init.body) });
    return new Response(postStatus === 200 ? '{"ok":true}' : '{"error":"x"}', { status: postStatus });
  };
  globalThis.caches = { open: async () => ({ put: async (k, r) => meta.set(k, await r.text()) }) };
  return {
    posts,
    meta,
    get sub() {
      return subscribed;
    },
    restore() {
      for (const [k, d] of Object.entries(saved)) {
        if (d) Object.defineProperty(globalThis, k, d);
        else delete globalThis[k];
      }
    },
  };
}

test('subscribe: asks permission, subscribes with the server key, registers with the server, saves the label', async () => {
  const b = installBrowser();
  try {
    await push.subscribe(fx.VAPID);
    assert.equal(b.posts.length, 1);
    assert.equal(b.posts[0].path, '/api/push/subscribe');
    assert.equal(b.posts[0].headers['X-Mission-Control'], '1');
    assert.equal(b.posts[0].headers['Content-Type'], 'application/json');
    assert.deepEqual(b.posts[0].body, { subscription: { endpoint: 'https://push.example.test/abc', keys: { p256dh: 'x', auth: 'y' } }, label: 'iPhone' });
    assert.equal(b.meta.get('/__label'), 'iPhone');

    await push.sendTest();
    assert.deepEqual(b.posts[1], { path: '/api/push/test', headers: b.posts[0].headers, body: { endpoint: 'https://push.example.test/abc' } });

    await push.unsubscribe();
    assert.deepEqual(b.posts[2].path, '/api/push/unsubscribe');
    assert.deepEqual(b.posts[2].body, { endpoint: 'https://push.example.test/abc' });
    assert.equal(b.sub, null, 'browser side removed too');
  } finally {
    b.restore();
  }
});

test('subscribe: a rotated server key replaces the old browser subscription', async () => {
  let unsubscribed = false;
  const old = { endpoint: 'https://push.example.test/old', options: { applicationServerKey: new Uint8Array([1, 2, 3]).buffer }, unsubscribe: async () => (unsubscribed = true) };
  const b = installBrowser({ existing: old });
  try {
    await push.subscribe(fx.VAPID);
    assert.equal(unsubscribed, true);
    assert.equal(b.sub.endpoint, 'https://push.example.test/abc');
  } finally {
    b.restore();
  }
});

test('subscribe / test failures give readable errors', async () => {
  let b = installBrowser({ permission: 'denied' });
  try {
    await assert.rejects(push.subscribe(fx.VAPID), /blocked/);
    await assert.rejects(push.subscribe(null), /push key/);
    assert.equal(b.posts.length, 0);
  } finally {
    b.restore();
  }
  b = installBrowser({ postStatus: 503 });
  try {
    await assert.rejects(push.subscribe(fx.VAPID), /The server couldn't save this device \(HTTP 503\)\./);
    await assert.rejects(push.sendTest(), /The server couldn't send a test \(HTTP 503\)\./);
  } finally {
    b.restore();
  }
  b = installBrowser();
  try {
    await assert.rejects(push.sendTest(), /isn't subscribed/);
  } finally {
    b.restore();
  }
});

for (const status of [410, 404]) {
  test(`test push answered ${status}: subscription gone, browser side dropped, readable error`, async () => {
    const b = installBrowser({ startSubscribed: true, postStatus: status });
    try {
      await assert.rejects(push.sendTest(), (e) => e.gone === true && e.message === push.EXPIRED_TEXT);
      assert.equal(b.sub, null, 'pushManager subscription removed locally');
    } finally {
      b.restore();
    }
  });
}

test('subscribe answered 410: also "gone"; a local unsubscribe that throws is ignored', async () => {
  let b = installBrowser({ postStatus: 410 });
  try {
    await assert.rejects(push.subscribe(fx.VAPID), (e) => e.gone === true);
    assert.equal(b.sub, null);
  } finally {
    b.restore();
  }
  b = installBrowser({ startSubscribed: true, postStatus: 410, unsubscribeThrows: true });
  try {
    await assert.rejects(push.sendTest(), (e) => e.gone === true && e.message === push.EXPIRED_TEXT);
  } finally {
    b.restore();
  }
  b = installBrowser({ startSubscribed: true, postStatus: 500 });
  try {
    await assert.rejects(push.sendTest(), (e) => !e.gone && /HTTP 500/.test(e.message), 'other errors are not "gone"');
    assert.notEqual(b.sub, null, 'and keep the subscription');
  } finally {
    b.restore();
  }
});

async function until(cond, what) {
  for (let i = 0; i < 200; i++) {
    if (cond()) return;
    await new Promise((r) => setTimeout(r, 1));
  }
  assert.fail(`timed out waiting for ${what}`);
}

for (const unsubscribeThrows of [false, true]) {
  test(`panel: "Send a test" answered 410 switches the panel to Off with the expired line${unsubscribeThrows ? ' (even if the local unsubscribe throws)' : ''}`, async () => {
    const b = installBrowser({ startSubscribed: true, postStatus: 410, unsubscribeThrows });
    const doc = installDom();
    const win = {
      isSecureContext: true,
      document: doc,
      location: { hash: '' },
      history: { replaceState() {} },
      navigator: { userAgent: ANDROID, platform: 'Linux', maxTouchPoints: 5, serviceWorker: { addEventListener() {} } },
      matchMedia: () => ({ matches: false }),
      PushManager: function PushManager() {},
      Notification: { permission: 'granted' },
      addEventListener() {},
      open() {},
    };
    const rootEl = doc.createElement('div');
    doc.body.appendChild(rootEl);
    const a = app.startApp(rootEl, win);
    const byId = (id) => allElements(rootEl).find((e) => e.getAttribute('id') === id);
    try {
      await a.ready;
      await until(() => a.state.push.phase === 'on', 'the panel to show On');
      byId('push-test').click();
      await until(() => a.state.push.busy === false && a.state.push.message, 'the test to finish');
      assert.equal(a.state.push.phase, 'off', 'panel is Off, not On');
      assert.deepEqual(a.state.push.message, { kind: 'error', text: push.EXPIRED_TEXT });
      assert.ok(byId('push-on'), '"Enable notifications" is offered again');
      assert.equal(byId('push-test'), undefined, 'no "Send a test" any more');
      assert.match(rootEl.textContent, /This device's notifications expired\. Turn them on again\./);
      if (!unsubscribeThrows) assert.equal(b.sub, null);
    } finally {
      a.stop();
      b.restore();
    }
  });
}

test('unsubscribe: a server 500 is an error and keeps the subscription; 410/404 drop it locally', async () => {
  let b = installBrowser({ startSubscribed: true, postStatus: 500 });
  try {
    await assert.rejects(push.unsubscribe(), /The server couldn't remove this device \(HTTP 500\)\./);
    assert.notEqual(b.sub, null, 'kept, so the panel stays On and Turn off can be retried');
  } finally {
    b.restore();
  }
  for (const status of [410, 404]) {
    b = installBrowser({ startSubscribed: true, postStatus: status });
    try {
      await push.unsubscribe();
      assert.equal(b.sub, null, `${status}: the server already forgot it, the browser side is dropped`);
    } finally {
      b.restore();
    }
  }
  b = installBrowser({ startSubscribed: true, postStatus: 410, unsubscribeThrows: true });
  try {
    await push.unsubscribe(); // a local unsubscribe that throws after a 410 is ignored
  } finally {
    b.restore();
  }
});

function appWin(doc, opened = []) {
  return {
    isSecureContext: true,
    document: doc,
    location: { hash: '' },
    history: { replaceState() {} },
    navigator: { userAgent: ANDROID, platform: 'Linux', maxTouchPoints: 5, serviceWorker: { addEventListener() {} } },
    matchMedia: () => ({ matches: false }),
    PushManager: function PushManager() {},
    Notification: { permission: 'granted' },
    addEventListener() {},
    open: (...args) => (opened.push(args), null),
  };
}

test('panel: "Turn off" answered 500 shows the error and stays On', async () => {
  const b = installBrowser({ startSubscribed: true, postStatus: 500 });
  const doc = installDom();
  const rootEl = doc.createElement('div');
  doc.body.appendChild(rootEl);
  const a = app.startApp(rootEl, appWin(doc));
  const byId = (id) => allElements(rootEl).find((e) => e.getAttribute('id') === id);
  try {
    await a.ready;
    await until(() => a.state.push.phase === 'on', 'the panel to show On');
    byId('push-off').click();
    await until(() => a.state.push.busy === false && a.state.push.message, 'turn off to finish');
    assert.equal(a.state.push.phase, 'on');
    assert.deepEqual(a.state.push.message, { kind: 'error', text: "The server couldn't remove this device (HTTP 500)." });
    assert.ok(byId('push-off'), 'Turn off is still there to retry');
  } finally {
    a.stop();
    b.restore();
  }
});

test('every external open is noopener,noreferrer in a new context; every rendered link has rel noopener noreferrer', async () => {
  const b = installBrowser();
  const doc = installDom();
  const rootEl = doc.createElement('div');
  doc.body.appendChild(rootEl);
  const opened = [];
  const a = app.startApp(rootEl, appWin(doc, opened));
  try {
    await a.ready;
    const input = allElements(rootEl).find((e) => e.getAttribute('id') === 'ask-q');
    const form = allElements(rootEl).find((e) => e.localName === 'form');
    for (const q of ['first question', '']) {
      input.value = q;
      form.dispatchEvent({ type: 'submit', preventDefault() {} });
    }
    assert.equal(opened.length, 2);
    for (const [url, target, features] of opened) {
      assert.match(url, /^https:\/\/ai-box\.example\.test\/\?models=agent_router\.auto/);
      assert.equal(target, '_blank');
      assert.deepEqual(String(features).split(',').map((x) => x.trim()).sort(), ['noopener', 'noreferrer']);
    }
    const ext = allElements(rootEl).filter((e) => e.localName === 'a' && !String(e.getAttribute('href')).startsWith('#'));
    assert.ok(ext.length >= 6, 'the tap-throughs are rendered');
    for (const l of ext) {
      assert.equal(l.getAttribute('rel'), 'noopener noreferrer', l.getAttribute('href'));
      assert.equal(l.getAttribute('target'), '_blank', l.getAttribute('href'));
    }
  } finally {
    a.stop();
    b.restore();
  }
});
