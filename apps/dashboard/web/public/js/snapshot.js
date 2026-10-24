// The last good /api/summary, saved on this device with its timestamp, for the offline screen.
// The service worker writes the same entry when /api/summary passes through it (see sw.js).

export const DATA_CACHE = 'homelab-data-v1';
export const SNAPSHOT_KEY = '/__snapshot';

const hasCaches = () => typeof caches !== 'undefined' && caches && typeof caches.open === 'function';

export async function saveSnapshot(summary, savedAt = Date.now()) {
  if (!hasCaches() || !summary) return false;
  try {
    const cache = await caches.open(DATA_CACHE);
    await cache.put(
      SNAPSHOT_KEY,
      new Response(JSON.stringify({ savedAt, summary }), { headers: { 'Content-Type': 'application/json' } }),
    );
    return true;
  } catch {
    return false;
  }
}

export async function loadSnapshot() {
  if (!hasCaches()) return null;
  try {
    const cache = await caches.open(DATA_CACHE);
    const res = await cache.match(SNAPSHOT_KEY);
    if (!res) return null;
    const data = await res.json();
    if (!data || typeof data.savedAt !== 'number' || !data.summary || typeof data.summary !== 'object') return null;
    return data;
  } catch {
    return null;
  }
}
