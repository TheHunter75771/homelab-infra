// Same-origin GETs to server S (CONTRACTS §12.15.5). Paths are fixed; query values are encoded with
// URLSearchParams. A session id goes into a query only after it passes the helper's id regex.

import { validSessionId } from './state.js';

export const DATA_CACHE = 'aictl-data-v1';
export const SNAPSHOT_KEY = '/__snapshot';

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function q(params) {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') sp.set(k, String(v));
  const s = sp.toString();
  return s ? `?${s}` : '';
}

/** GET → { status, etag, data }. A 304 has data null. Non-2xx throws ApiError with the server's error text. */
export async function getJSON(path, { etag, signal, timeoutMs = 15000 } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  if (signal) signal.addEventListener('abort', () => ctrl.abort(), { once: true });
  let res;
  try {
    const headers = { Accept: 'application/json' };
    if (etag) headers['If-None-Match'] = etag;
    res = await fetch(path, { headers, signal: ctrl.signal, credentials: 'same-origin', cache: 'no-store' });
  } catch (e) {
    throw new ApiError(e && e.name === 'AbortError' ? 'timed out' : 'unreachable', 0);
  } finally {
    clearTimeout(timer);
  }
  if (res.status === 304) return { status: 304, etag, data: null };
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    const msg = data && typeof data.error === 'string' ? data.error : data && data.error && typeof data.error.message === 'string' ? data.error.message : `HTTP ${res.status}`;
    throw new ApiError(msg, res.status);
  }
  return { status: res.status, etag: res.headers.get('ETag'), data };
}

export const paths = {
  summary: () => '/api/summary',
  history: (minutes = 30) => `/api/gate/history${q({ minutes })}`,
  requests: (limit = 50) => `/api/requests${q({ limit })}`,
  logs: (source, lines) => `/api/logs${q({ source, lines })}`,
  sudo: (host, user, since, limit = 200) => `/api/audit/sudo${q({ host, user, since, limit })}`,
  sessions: (user) => `/api/audit/sessions${q({ user })}`,
  replay: (id, user, output) => {
    if (!validSessionId(id)) throw new ApiError('unexpected session id', 0);
    return `/api/audit/replay${q({ user, id, output: output ? 1 : 0 })}`;
  },
  logSources: () => '/api/logs/sources',
  localai: (since, limit = 200) => `/api/audit/localai${q({ since, limit })}`,
  events: () => '/api/events',
};

/** The last good summary the service worker saved: { savedAt, summary } or null. */
export async function readSnapshot() {
  try {
    if (typeof caches === 'undefined') return null;
    const cache = await caches.open(DATA_CACHE);
    const hit = await cache.match(SNAPSHOT_KEY);
    if (!hit) return null;
    const v = await hit.json();
    return v && typeof v === 'object' && v.summary && typeof v.savedAt === 'number' ? v : null;
  } catch {
    return null;
  }
}
