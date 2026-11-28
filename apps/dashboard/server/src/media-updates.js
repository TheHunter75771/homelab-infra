// Media-app update check (CONTRACTS §11.7.2, GET /api/media/updates): for every media-stack app the deployed
// compose files declare (the helper's ps.declared: stack "media", not a local build, with a pin), the newest
// stable release from its registry (src/registry.js), compared with the pin.
//
// status: update (a different digest, not older) | current (the pin's digest) | ahead (the pin is newer than the
// registry's latest: a downgrade, never offered) | deprecated (the latest version contains "deprecated": never
// offered) | error (the lookup failed; the message says why, without URLs or tokens).
// major: the first number of the version differs ("10.11.9…" -> "12.4…").
// Results are cached for 10 min (refresh skips the cache); concurrent checks share one run; at most 3 lookups
// run in parallel. POST /api/media/update only accepts apps that are `update` in a check at most 10 min old.
import { DEFAULT_APPS } from './catalog.js';
import { parsePin, RegistryError } from './registry.js';

export const CHECK_TTL_MS = 10 * 60_000;
export const LOOKUP_CONCURRENCY = 3;

// The first run of digits: "10.11.9ubu2404-ls33" -> 10, "4.0.17.2952-ls312" -> 4.
export function majorOf(version) {
  const m = String(version ?? '').match(/\d+/);
  return m ? Number(m[0]) : null;
}

// Orders two versions by their leading dotted-numeric part ("4.0.20.3014" of "4.0.20.3014-ls7", "10.11.9" of
// "10.11.9ubu2404-ls33"; missing parts count as 0), then, when that is equal, by the linuxserver build "-lsN".
// -> -1 | 0 | 1, or null when either can't be parsed or the order can't be told (then the digest decides).
export function compareVersions(a, b) {
  const num = (v) => String(v ?? '').match(/^v?(\d+(?:\.\d+)*)/)?.[1]?.split('.').map(Number) ?? null;
  const na = num(a);
  const nb = num(b);
  if (!na || !nb) return null;
  for (let i = 0; i < Math.max(na.length, nb.length); i++) {
    const x = na[i] ?? 0;
    const y = nb[i] ?? 0;
    if (x !== y) return x < y ? -1 : 1;
  }
  const ls = (v) => {
    const m = String(v).match(/-ls(\d+)$/);
    return m ? Number(m[1]) : null;
  };
  const la = ls(a);
  const lb = ls(b);
  if (la === null || lb === null) return null;
  return la === lb ? 0 : la < lb ? -1 : 1;
}

export const AHEAD_MESSAGE = "The pinned version is newer than the registry's latest release.";

export function isMajor(current, latest) {
  const a = majorOf(current);
  const b = majorOf(latest);
  return a !== null && b !== null && a !== b;
}

// The apps a media update may touch, from the helper's declared list.
export function mediaApps(declared) {
  return (declared ?? [])
    .filter((d) => d && d.stack === 'media' && d.local_build !== true && typeof d.pinned === 'string' && d.pinned)
    .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
}

async function pool(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i]);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

export function createMediaUpdates({ cfg, registry, declared, clock = () => Date.now(), log, ttlMs = CHECK_TTL_MS, concurrency = LOOKUP_CONCURRENCY }) {
  let cache = null; // {at, result}
  let running = null;
  const label = (name) => cfg.apps?.[name]?.label ?? DEFAULT_APPS[name]?.label ?? name;

  async function lookup(d) {
    let current;
    try {
      const p = parsePin(d.pinned);
      current = { version: p.tag ?? 'latest', pin: d.pinned, digest: p.digest };
    } catch {
      current = { version: null, pin: d.pinned, digest: null };
    }
    const base = { name: d.name, label: label(d.name), current: { version: current.version, pin: current.pin } };
    try {
      if (!current.digest) throw new RegistryError('the pin has no sha256 digest');
      const latest = await registry.latest(d.pinned);
      const entry = { ...base, latest: { version: latest.version, pin: latest.pin }, major: isMajor(current.version, latest.version) };
      if (/deprecated/i.test(latest.version)) {
        return { ...entry, status: 'deprecated', message: `The image is no longer maintained (its latest release is ${latest.version}).` };
      }
      // Same image: the pin may name the index or the amd64 manifest directly.
      if (latest.digest === current.digest || latest.platformDigest === current.digest) return { ...entry, status: 'current', message: null };
      // Never offer a downgrade: app databases (Sonarr, …) may not start on an older version.
      if (compareVersions(latest.version, current.version) === -1) return { ...entry, status: 'ahead', major: false, message: AHEAD_MESSAGE };
      return { ...entry, status: 'update', message: null };
    } catch (err) {
      const why = err instanceof RegistryError ? err.reason : 'the lookup failed';
      if (!(err instanceof RegistryError)) log?.warn('media update lookup failed', { app: d.name, error: err?.message ?? String(err) });
      return { ...base, latest: null, status: 'error', major: false, message: `Couldn't check for updates: ${why}.` };
    }
  }

  async function run() {
    const apps = mediaApps(declared());
    const at = clock();
    const list = await pool(apps, concurrency, lookup);
    const result = { checkedAt: new Date(at).toISOString(), apps: list };
    cache = { at, result };
    log?.info('media update check', { apps: list.length, updates: list.filter((a) => a.status === 'update').length, errors: list.filter((a) => a.status === 'error').length });
    return result;
  }

  return {
    async check({ refresh = false } = {}) {
      if (!refresh && cache && clock() - cache.at <= ttlMs) return cache.result;
      running ??= run().finally(() => {
        running = null;
      });
      return running;
    },
    // The last check if it is at most ttlMs old, else null.
    fresh() {
      return cache && clock() - cache.at <= ttlMs ? cache.result : null;
    },
    drop() {
      cache = null;
    },
  };
}
