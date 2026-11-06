// Same-origin calls to the Mission Control server (README "API"). Every call resolves (never throws):
//   {ok: true, status, data}  or  {ok: false, fail: {kind: 'network'|'http'|'shape', status}, data}
// `data` on a failure is the parsed error body when there was one (`{"error": "..."}`), else null.

export const SNAPSHOT_CACHE = 'mc-data-v1'; // written by sw.js on each 200, read here when offline
export const SNAPSHOT_KEYS = { cards: '/__snapshot/cards', briefing: '/__snapshot/briefing' };
const TIMEOUT_MS = 15_000;

async function call(path, init) {
  const ctl = typeof AbortController === 'function' ? new AbortController() : null;
  const timer = ctl ? setTimeout(() => ctl.abort(), TIMEOUT_MS) : null;
  let res;
  try {
    res = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...init, signal: ctl?.signal });
  } catch {
    return { ok: false, fail: { kind: 'network', status: null }, data: null };
  } finally {
    if (timer) clearTimeout(timer);
  }
  let data = null;
  let parsed = true;
  try {
    data = await res.json();
  } catch {
    parsed = false;
  }
  if (!res.ok) return { ok: false, fail: { kind: 'http', status: res.status }, data };
  if (!parsed || data === null || typeof data !== 'object' || Array.isArray(data)) return { ok: false, fail: { kind: 'shape', status: res.status }, data: null };
  return { ok: true, status: res.status, data };
}

export function getJSON(path) {
  return call(path, { method: 'GET' });
}

/** POST with a JSON body. `X-Mission-Control: 1` lets the server refuse cross-site form posts (README). */
export function postJSON(path, body) {
  return call(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Mission-Control': '1' },
    body: JSON.stringify(body ?? {}),
  });
}

/** The service worker's last good copy of /api/cards or /api/briefing: {savedAt (ms), data} or null. */
export async function readSnapshot(which) {
  try {
    if (typeof caches === 'undefined') return null;
    const cache = await caches.open(SNAPSHOT_CACHE);
    const hit = await cache.match(SNAPSHOT_KEYS[which]);
    if (!hit) return null;
    const snap = await hit.json();
    return snap && typeof snap.savedAt === 'number' && snap.data && typeof snap.data === 'object' ? snap : null;
  } catch {
    return null;
  }
}
