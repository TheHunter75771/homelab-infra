// Subscription store (contract 4): ${PUSH_DIR}/subscriptions.json =
//   [{"endpoint": str, "keys": {"p256dh": str, "auth": str}, "label": str, "created": ISO8601}]
//
// - Missing file  -> empty list.
// - Corrupt file  -> StoreCorruptError on every read; writes fail closed (the file is never overwritten).
// - Writes        -> tmp file in the same directory, fsync, rename over the target, fsync the directory.
// - Dedupe        -> by endpoint (first occurrence wins; duplicates are dropped on the next write).
// - Every mutation re-reads the file, so entries removed or added by the host notifier are respected.
//   All filesystem calls are synchronous, so mutations never interleave inside this process.
import nodeFs from 'node:fs';
import path from 'node:path';
import { randomBytes } from 'node:crypto';
import { deviceId } from './log.js';

export const FILE_NAME = 'subscriptions.json';
export const FILE_MODE = 0o640;
export const MAX_ENDPOINT_LENGTH = 2048;
export const MAX_LABEL_LENGTH = 64;
export const DEFAULT_MAX_ENTRIES = 100;
export const DEFAULT_LABEL = 'device';

// Push services that real browsers hand out endpoints for. Subscribing any other host is refused, so the
// service (and the host notifier, which sends to every entry) cannot be pointed at arbitrary URLs.
export const DEFAULT_ALLOWED_HOSTS = Object.freeze([
  'fcm.googleapis.com', // Chrome, Edge (Chromium), Android, Samsung Internet, Opera, Brave
  'android.googleapis.com', // legacy Chrome endpoints
  'push.services.mozilla.com', // Firefox (updates.push.services.mozilla.com)
  'push.apple.com', // Safari / iOS Home Screen apps (web.push.apple.com)
  'notify.windows.com', // Windows push notification service
]);

const TMP_PATTERN = /^\.subscriptions\.json\.\d+\.[0-9a-f]{12}\.tmp$/;
const B64URL = /^[A-Za-z0-9_-]+={0,2}$/;

export class StoreCorruptError extends Error {
  constructor(message) {
    super(message);
    this.name = 'StoreCorruptError';
  }
}

export class ValidationError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ValidationError';
  }
}

export class StoreFullError extends Error {
  constructor(max) {
    super(`subscription limit reached (${max})`);
    this.name = 'StoreFullError';
  }
}

// `PUSH_ENDPOINT_HOSTS` env: comma-separated host suffixes, or "*" to allow any https host.
export function parseAllowedHosts(value) {
  if (value === undefined || value === null || String(value).trim() === '') return DEFAULT_ALLOWED_HOSTS;
  const list = String(value)
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
  return list.includes('*') ? null : Object.freeze(list);
}

function hostAllowed(hostname, allowedHosts) {
  if (allowedHosts === null) return true;
  const host = hostname.toLowerCase();
  return allowedHosts.some((suffix) => host === suffix || host.endsWith(`.${suffix}`));
}

function b64urlBytes(value) {
  if (typeof value !== 'string' || value.length > 256 || !B64URL.test(value)) return null;
  return Buffer.from(value.replace(/=+$/, ''), 'base64url');
}

export function validateEndpoint(endpoint, allowedHosts = DEFAULT_ALLOWED_HOSTS) {
  if (typeof endpoint !== 'string' || endpoint.length === 0) {
    throw new ValidationError('subscription.endpoint must be a non-empty string');
  }
  if (endpoint.length > MAX_ENDPOINT_LENGTH) throw new ValidationError('subscription.endpoint is too long');
  let url;
  try {
    url = new URL(endpoint);
  } catch {
    throw new ValidationError('subscription.endpoint is not a valid URL');
  }
  if (url.protocol !== 'https:') throw new ValidationError('subscription.endpoint must use https');
  if (url.username || url.password || url.port) {
    throw new ValidationError('subscription.endpoint must not contain credentials or a port');
  }
  if (!hostAllowed(url.hostname, allowedHosts)) {
    throw new ValidationError('subscription.endpoint is not a known push service');
  }
  return endpoint;
}

// Returns {endpoint, keys: {p256dh, auth}} with base64url padding stripped. Extra fields (expirationTime) dropped.
export function validateSubscription(subscription, allowedHosts = DEFAULT_ALLOWED_HOSTS) {
  if (!subscription || typeof subscription !== 'object' || Array.isArray(subscription)) {
    throw new ValidationError('subscription must be an object');
  }
  const endpoint = validateEndpoint(subscription.endpoint, allowedHosts);
  const keys = subscription.keys;
  if (!keys || typeof keys !== 'object') throw new ValidationError('subscription.keys must be an object');
  const p256dh = b64urlBytes(keys.p256dh);
  if (!p256dh || p256dh.length !== 65 || p256dh[0] !== 0x04) {
    throw new ValidationError('subscription.keys.p256dh must be a base64url P-256 public key');
  }
  const auth = b64urlBytes(keys.auth);
  if (!auth || auth.length !== 16) {
    throw new ValidationError('subscription.keys.auth must be a base64url 16-byte secret');
  }
  return {
    endpoint,
    keys: { p256dh: keys.p256dh.replace(/=+$/, ''), auth: keys.auth.replace(/=+$/, '') },
  };
}

export function normalizeLabel(label) {
  if (typeof label !== 'string') return DEFAULT_LABEL;
  const cleaned = label
    .replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  const clipped = Array.from(cleaned).slice(0, MAX_LABEL_LENGTH).join('').trim();
  return clipped || DEFAULT_LABEL;
}

function isEntry(e) {
  return (
    e !== null &&
    typeof e === 'object' &&
    !Array.isArray(e) &&
    typeof e.endpoint === 'string' &&
    e.endpoint.length > 0 &&
    e.keys !== null &&
    typeof e.keys === 'object' &&
    typeof e.keys.p256dh === 'string' &&
    typeof e.keys.auth === 'string'
  );
}

function isIsoDate(value) {
  return typeof value === 'string' && !Number.isNaN(Date.parse(value));
}

function dedupe(entries) {
  const seen = new Set();
  return entries.filter((e) => {
    if (seen.has(e.endpoint)) return false;
    seen.add(e.endpoint);
    return true;
  });
}

// What the PWA may see: never endpoints or keys.
export function toDevice(entry) {
  return {
    id: deviceId(entry.endpoint),
    label: typeof entry.label === 'string' ? entry.label : '',
    created: isIsoDate(entry.created) ? entry.created : null,
  };
}

export function createStore({
  dir,
  fs = nodeFs,
  log,
  now = () => new Date(),
  allowedHosts = DEFAULT_ALLOWED_HOSTS,
  maxEntries = DEFAULT_MAX_ENTRIES,
}) {
  const file = path.join(dir, FILE_NAME);

  function read() {
    let text;
    try {
      text = fs.readFileSync(file, 'utf8');
    } catch (err) {
      if (err.code === 'ENOENT') return [];
      throw err;
    }
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      throw new StoreCorruptError(`${FILE_NAME} is not valid JSON`);
    }
    if (!Array.isArray(data)) throw new StoreCorruptError(`${FILE_NAME} must contain a JSON array`);
    const bad = data.findIndex((e) => !isEntry(e));
    if (bad !== -1) throw new StoreCorruptError(`${FILE_NAME} entry ${bad} is malformed`);
    return data;
  }

  function fsyncDir() {
    let fd;
    try {
      fd = fs.openSync(dir, 'r');
      fs.fsyncSync(fd);
    } catch (err) {
      log?.debug('directory fsync skipped', { code: err.code });
    } finally {
      if (fd !== undefined) fs.closeSync(fd);
    }
  }

  function write(entries) {
    const tmp = path.join(dir, `.${FILE_NAME}.${process.pid}.${randomBytes(6).toString('hex')}.tmp`);
    const data = `${JSON.stringify(entries, null, 2)}\n`;
    let fd;
    try {
      fd = fs.openSync(tmp, 'wx', FILE_MODE);
      fs.fchmodSync(fd, FILE_MODE); // umask-independent
      fs.writeFileSync(fd, data);
      fs.fsyncSync(fd);
      fs.closeSync(fd);
      fd = undefined;
      fs.renameSync(tmp, file);
    } catch (err) {
      if (fd !== undefined) {
        try {
          fs.closeSync(fd);
        } catch {
          // already failing
        }
      }
      try {
        fs.unlinkSync(tmp);
      } catch {
        // tmp may not exist
      }
      log?.error('store write failed', { code: err.code ?? null, error: err.message });
      throw err;
    }
    fsyncDir();
  }

  // fn(entries) -> {changed, entries, ...result}. Writes only when changed.
  function mutate(op, fn) {
    let current;
    try {
      current = read();
    } catch (err) {
      if (err instanceof StoreCorruptError) {
        log?.error('store corrupt; refusing to write', { op, reason: err.message });
      }
      throw err;
    }
    const result = fn(current);
    if (result.changed) write(dedupe(result.entries));
    return result;
  }

  return {
    file,

    list() {
      return read();
    },

    devices() {
      return dedupe(read()).map(toDevice);
    },

    subscribe(subscription, label) {
      const sub = validateSubscription(subscription, allowedHosts);
      const cleanLabel = normalizeLabel(label);
      return mutate('subscribe', (entries) => {
        const idx = entries.findIndex((e) => e.endpoint === sub.endpoint);
        if (idx !== -1) {
          const prev = entries[idx];
          const next = {
            ...prev,
            endpoint: sub.endpoint,
            keys: sub.keys,
            label: cleanLabel,
            created: isIsoDate(prev.created) ? prev.created : now().toISOString(),
          };
          const changed =
            JSON.stringify(prev) !== JSON.stringify(next) ||
            entries.some((e, i) => i > idx && e.endpoint === sub.endpoint);
          const updated = entries.slice();
          updated[idx] = next;
          return { changed, entries: updated, created: false, entry: next };
        }
        if (dedupe(entries).length >= maxEntries) throw new StoreFullError(maxEntries);
        const entry = { endpoint: sub.endpoint, keys: sub.keys, label: cleanLabel, created: now().toISOString() };
        return { changed: true, entries: [...entries, entry], created: true, entry };
      });
    },

    unsubscribe(endpoint) {
      if (typeof endpoint !== 'string' || endpoint.length === 0 || endpoint.length > MAX_ENDPOINT_LENGTH) {
        throw new ValidationError('endpoint must be a non-empty string');
      }
      return mutate('unsubscribe', (entries) => {
        const kept = entries.filter((e) => e.endpoint !== endpoint);
        return { changed: kept.length !== entries.length, entries: kept, removed: kept.length !== entries.length };
      });
    },

    // Drop subscriptions the push service reported as gone (404/410). Returns how many were removed.
    removeEndpoints(endpoints) {
      const gone = new Set(endpoints);
      if (gone.size === 0) return 0;
      const result = mutate('remove-dead', (entries) => {
        const kept = entries.filter((e) => !gone.has(e.endpoint));
        return { changed: kept.length !== entries.length, entries: kept, removed: entries.length - kept.length };
      });
      return result.removed;
    },

    // Delete temp files left behind by a crash mid-write (only this service's naming pattern).
    cleanupStaleTmp() {
      let removed = 0;
      let names = [];
      try {
        names = fs.readdirSync(dir);
      } catch {
        return 0;
      }
      for (const name of names) {
        if (!TMP_PATTERN.test(name)) continue;
        try {
          fs.unlinkSync(path.join(dir, name));
          removed += 1;
        } catch {
          // ignore
        }
      }
      if (removed) log?.warn('removed stale temp files', { count: removed });
      return removed;
    },

    writable() {
      try {
        fs.accessSync(dir, nodeFs.constants.W_OK);
        return true;
      } catch {
        return false;
      }
    },
  };
}
