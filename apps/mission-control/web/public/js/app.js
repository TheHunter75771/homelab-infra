// The controller: loads /api/cards and /api/briefing, falls back to the service worker's last good copies,
// polls while the page is visible, runs the notifications flow, and opens the card a notification points at.
// The state changes are small pure functions (exported for the tests); startApp() wires them to the browser.

import { getJSON, readSnapshot } from './api.js';
import { normalizeCards, normalizeBriefing, cardFromHash } from './model.js';
import { createView } from './view.js';
import * as push from './push.js';

const POLL_MS = 60_000; // the server caches about 60 s
const FLASH_MS = 4_000;

export function initialState() {
  return {
    cards: null,
    cardsFail: null,
    cardsFrom: null,
    cardsSavedAt: null,
    briefing: null,
    briefingFail: null,
    briefingFrom: null,
    briefingSavedAt: null,
    loading: false,
    push: { phase: 'checking', blocked: null, isIOS: false, busy: false, message: null },
    highlight: null,
  };
}

/**
 * Applies one /api/cards or /api/briefing result. On success: the live data. On failure: keep what the
 * page already has (marked stale), else the service worker's snapshot, else nothing; the failure is kept so
 * the page can say what went wrong.
 */
export function applyResult(state, which, result, snapshot, now = Date.now()) {
  const norm = which === 'cards' ? normalizeCards : normalizeBriefing;
  const k = (s) => `${which}${s}`;
  if (result.ok) return { ...state, [which]: norm(result.data), [k('Fail')]: null, [k('From')]: 'live', [k('SavedAt')]: now };
  if (state[which]) return { ...state, [k('Fail')]: result.fail, [k('From')]: 'snapshot' };
  if (snapshot) return { ...state, [which]: norm(snapshot.data), [k('Fail')]: result.fail, [k('From')]: 'snapshot', [k('SavedAt')]: snapshot.savedAt };
  return { ...state, [which]: null, [k('Fail')]: result.fail, [k('From')]: null, [k('SavedAt')]: null };
}

export function startApp(root, win = globalThis) {
  let state = initialState();
  let lastLoad = 0;
  let flashTimer = null;
  const view = createView({
    refresh: () => load(),
    openExternal: (url) => win.open(url, '_blank', 'noopener,noreferrer'),
    enablePush: () => runPush('enable'),
    testPush: () => runPush('test'),
    disablePush: () => runPush('disable'),
  });
  root.replaceChildren(view.el);
  const render = () => view.update(state);
  const set = (patch) => {
    state = { ...state, ...patch };
    render();
  };

  async function load() {
    if (state.loading) return;
    lastLoad = Date.now();
    set({ loading: true });
    const [c, b] = await Promise.all([getJSON('/api/cards'), getJSON('/api/briefing')]);
    let next = applyResult(state, 'cards', c, c.ok ? null : await readSnapshot('cards'));
    next = applyResult(next, 'briefing', b, b.ok ? null : await readSnapshot('briefing'));
    state = { ...next, loading: false };
    render();
  }

  async function checkPush() {
    const env = push.pushEnv(win);
    if (env.blocked) return set({ push: { ...state.push, phase: 'blocked', blocked: env.blocked, isIOS: env.isIOS } });
    if (!(await push.registration())) return set({ push: { ...state.push, phase: 'blocked', blocked: 'worker', isIOS: env.isIOS } });
    const sub = await push.currentSubscription();
    set({ push: { ...state.push, phase: sub ? 'on' : 'off', blocked: null, isIOS: env.isIOS } });
  }

  async function runPush(what) {
    if (state.push.busy) return;
    set({ push: { ...state.push, busy: true, message: null } });
    let message;
    try {
      if (what === 'enable') {
        await push.subscribe(state.cards?.config.vapidPublicKey ?? null);
        message = { kind: 'ok', text: 'Notifications are on. Try "Send a test".' };
      } else if (what === 'test') {
        await push.sendTest();
        message = { kind: 'ok', text: 'Test sent. It should arrive in a few seconds.' };
      } else {
        await push.unsubscribe();
        message = { kind: 'ok', text: 'Notifications are off for this device.' };
      }
    } catch (e) {
      if (e && e.gone === true) {
        // The server dropped this subscription (410/404): push.js already removed the browser side. Show Off
        // straight away, whatever getSubscription() says, so Enable makes a fresh one.
        set({ push: { ...state.push, busy: false, phase: 'off', message: { kind: 'error', text: push.EXPIRED_TEXT } } });
        return;
      }
      message = { kind: 'error', text: e && typeof e.message === 'string' ? e.message : 'That didn’t work. Try again.' };
    }
    state = { ...state, push: { ...state.push, busy: false, message } };
    await checkPush();
  }

  function openCard(id) {
    if (!id) return;
    clearTimeout(flashTimer);
    set({ highlight: id });
    view.focusCard(id);
    flashTimer = setTimeout(() => set({ highlight: null }), FLASH_MS);
  }

  // A notification tap with the app already open: the worker asks us to show a card (sw.js).
  win.navigator?.serviceWorker?.addEventListener?.('message', (event) => {
    const d = event.data;
    if (!d || d.type !== 'open-card') return;
    const id = cardFromHash(`#card-${d.card}`);
    if (!id) return;
    try {
      win.history.replaceState(null, '', `#card-${id}`);
    } catch {
      // not fatal
    }
    openCard(id);
  });
  win.addEventListener?.('hashchange', () => openCard(cardFromHash(win.location.hash)));
  win.document?.addEventListener?.('visibilitychange', () => {
    if (win.document.visibilityState === 'visible' && Date.now() - lastLoad > POLL_MS / 2) load();
  });
  win.addEventListener?.('online', () => load());
  render();
  const poll = setInterval(() => {
    if (win.document?.visibilityState !== 'hidden') load();
  }, POLL_MS);
  const first = load().then(() => openCard(cardFromHash(win.location?.hash)));
  checkPush();
  return {
    get state() {
      return state;
    },
    ready: first,
    view,
    stop: () => (clearInterval(poll), clearTimeout(flashTimer)),
  };
}
