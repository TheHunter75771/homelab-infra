// Keeps the summary fresh: GET /api/summary once, then the /api/events SSE stream. Whenever the stream
// isn't healthy it polls every 10 s (with ETag), and retries the stream. Every good summary is saved
// on the device (throttled) for the offline screen.

import { getJSON, streamSSE, parseJSON } from './api.js';
import { saveSnapshot, loadSnapshot } from './snapshot.js';

export const POLL_MS = 10000;
export const CONTACT_TIMEOUT_MS = 45000; // a stream that's silent this long (no ping) is treated as dead
export const SNAPSHOT_EVERY_MS = 15000;

export function createLive(store, deps = {}) {
  const api = { getJSON, streamSSE, ...(deps.api || {}) };
  const snap = { save: saveSnapshot, load: loadSnapshot, ...(deps.snapshot || {}) };
  const now = deps.now || (() => Date.now());
  // Wrapped: browsers throw "Illegal invocation" when timer functions are called as methods of another object.
  const timers = deps.timers || { setInterval: (fn, ms) => setInterval(fn, ms), clearInterval: (t) => clearInterval(t) };
  let etag = null;
  let sse = null;
  let sseLive = false;
  let lastContact = 0;
  let reconnectAt = 0;
  let timer = null;
  let stopped = true;
  let lastSave = -Infinity;
  let inflight = null;
  let lastEventId = null; // sent on reconnect: the server answers with a `tick` when nothing changed
  const cleanups = [];

  const contact = () => {
    lastContact = now();
  };

  function accept(summary) {
    if (!summary || typeof summary !== 'object') return;
    contact();
    store.dispatch({ type: 'summary', summary, at: now() });
    if (now() - lastSave >= SNAPSHOT_EVERY_MS) {
      lastSave = now();
      Promise.resolve(snap.save(summary, now())).catch(() => {});
    }
  }

  async function unreachable() {
    const st = store.getState();
    if (!st.summary || st.snapshotAt) {
      const saved = await Promise.resolve(snap.load()).catch(() => null);
      if (saved) store.dispatch({ type: 'snapshot', snapshot: saved });
    }
    store.dispatch({ type: 'unreachable', at: now() });
  }

  function fetchSummary() {
    if (inflight) return inflight;
    inflight = (async () => {
      try {
        const r = await api.getJSON('/api/summary', { etag });
        if (r.status === 304) {
          contact();
          store.dispatch({ type: 'contact', at: now(), generatedAt: r.generatedAt });
        } else {
          etag = r.etag || null;
          accept(r.data);
        }
        return true;
      } catch {
        await unreachable();
        return false;
      } finally {
        inflight = null;
      }
    })();
    return inflight;
  }

  function connect() {
    if (stopped || sse) return;
    const ctl = new AbortController();
    sse = ctl;
    Promise.resolve()
      .then(() =>
        api.streamSSE('/api/events', {
          signal: ctl.signal,
          lastEventId: store.getState().summary && !store.getState().snapshotAt ? lastEventId : null,
          onOpen() {
            sseLive = true;
            contact();
          },
          onEvent(ev) {
            if (ev.id) lastEventId = ev.id;
            if (ev.type === 'summary' || ev.type === 'message') {
              const data = parseJSON(ev.data);
              if (data && typeof data === 'object') accept(data.summary && !data.apps ? data.summary : data);
              return;
            }
            contact(); // tick, ping, comment: the server is there (a tick also says it refreshed)
            const data = ev.type === 'tick' ? parseJSON(ev.data) : null;
            store.dispatch({ type: 'contact', at: now(), generatedAt: data?.generatedAt });
          },
        }),
      )
      .catch(() => {})
      .finally(() => {
        if (sse === ctl) {
          sse = null;
          if (sseLive) fetchSummary(); // the stream just dropped: check right away
          sseLive = false;
          reconnectAt = now() + POLL_MS;
        }
      });
  }

  function tick() {
    if (stopped) return;
    if (sseLive && now() - lastContact > CONTACT_TIMEOUT_MS) sse?.abort();
    if (!sseLive) {
      fetchSummary();
      if (!sse && now() >= reconnectAt) connect();
    }
  }

  function refresh() {
    const p = fetchSummary();
    if (!sse && !stopped) connect();
    return p;
  }

  return {
    start() {
      if (!stopped) return;
      stopped = false;
      fetchSummary().then(() => connect());
      timer = timers.setInterval(tick, POLL_MS);
      const doc = globalThis.document;
      const win = globalThis.window;
      if (doc?.addEventListener) {
        const onVis = () => {
          if (doc.visibilityState === 'visible') refresh();
        };
        doc.addEventListener('visibilitychange', onVis);
        cleanups.push(() => doc.removeEventListener('visibilitychange', onVis));
      }
      if (win?.addEventListener) {
        win.addEventListener('online', refresh);
        cleanups.push(() => win.removeEventListener('online', refresh));
      }
    },
    stop() {
      stopped = true;
      if (timer) timers.clearInterval(timer);
      timer = null;
      sse?.abort();
      sse = null;
      sseLive = false;
      while (cleanups.length) cleanups.pop()();
    },
    refresh,
    get streaming() {
      return sseLive;
    },
  };
}
