// settings.json (0600, state dir): thresholds, subscription renewal dates, and the Jellyfin/Jellyseerr API keys.
// Keys are never returned in full: the client sees {configured, last4}.
import path from 'node:path';
import { readJson, writeJsonAtomic } from './util/fsx.js';
import { isIsoDate } from './util/time.js';

export const DEFAULT_SETTINGS = Object.freeze({
  thresholds: { diskWarnPct: 90, backupMaxAgeH: 36 },
  subscriptions: [],
  integrations: { jellyfinApiKey: '', jellyseerrApiKey: '' },
});

export const LIMITS = { diskWarnPct: [50, 99], backupMaxAgeH: [1, 720], subscriptions: 20, name: 64 };
const KEY_FIELDS = { jellyfinApiKey: 'jellyfin', jellyseerrApiKey: 'jellyseerr' };
const API_KEY_RE = /^[\x21-\x7e]{8,256}$/; // printable ASCII, no spaces

export class SettingsError extends Error {
  constructor(message) {
    super(message);
    this.name = 'SettingsError';
  }
}

const slug = (s) =>
  s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 40) || 'item';

function normalizeSubscriptions(list) {
  if (!Array.isArray(list)) throw new SettingsError('subscriptions must be an array');
  if (list.length > LIMITS.subscriptions) throw new SettingsError(`at most ${LIMITS.subscriptions} subscriptions`);
  const seen = new Set();
  return list.map((s, i) => {
    if (!s || typeof s !== 'object') throw new SettingsError(`subscription ${i} must be an object`);
    const name = typeof s.name === 'string' ? s.name.replace(/[\u0000-\u001f\u007f]/g, ' ').trim() : '';
    if (!name || name.length > LIMITS.name) throw new SettingsError(`subscription ${i} needs a name of 1-${LIMITS.name} characters`);
    if (!isIsoDate(s.date)) throw new SettingsError(`subscription ${i} needs a date as YYYY-MM-DD`);
    let key = typeof s.key === 'string' && /^[a-z0-9-]{1,40}$/.test(s.key) ? s.key : slug(name);
    while (seen.has(key)) key = `${key}-${i}`;
    seen.add(key);
    return { key, name, date: s.date };
  });
}

function intIn(value, [lo, hi], label) {
  if (!Number.isInteger(value) || value < lo || value > hi) throw new SettingsError(`${label} must be a whole number from ${lo} to ${hi}`);
  return value;
}

export function createSettings({ stateDir, log }) {
  const file = path.join(stateDir, 'settings.json');
  let current = structuredClone(DEFAULT_SETTINGS);
  const loaded = readJson(file);
  if (loaded.error) log?.error('settings.json unreadable; using defaults until it is fixed', { reason: loaded.error });
  else if (loaded.value) {
    try {
      current = merge(current, loaded.value, { fromDisk: true });
    } catch (err) {
      log?.error('settings.json invalid; using defaults until it is fixed', { reason: err.message });
    }
  }
  const listeners = new Set();

  function merge(base, patch, { fromDisk = false } = {}) {
    if (!patch || typeof patch !== 'object' || Array.isArray(patch)) throw new SettingsError('settings must be an object');
    const unknown = Object.keys(patch).filter((k) => !['thresholds', 'subscriptions', 'integrations'].includes(k));
    if (unknown.length && !fromDisk) throw new SettingsError(`unknown setting(s): ${unknown.join(', ')}`);
    const next = structuredClone(base);
    if (patch.thresholds !== undefined) {
      const t = patch.thresholds;
      if (!t || typeof t !== 'object' || Array.isArray(t)) throw new SettingsError('thresholds must be an object');
      const unknownT = Object.keys(t).filter((k) => k !== 'diskWarnPct' && k !== 'backupMaxAgeH');
      if (unknownT.length) throw new SettingsError(`unknown threshold(s): ${unknownT.join(', ')}`);
      if (t.diskWarnPct !== undefined) next.thresholds.diskWarnPct = intIn(t.diskWarnPct, LIMITS.diskWarnPct, 'diskWarnPct');
      if (t.backupMaxAgeH !== undefined) next.thresholds.backupMaxAgeH = intIn(t.backupMaxAgeH, LIMITS.backupMaxAgeH, 'backupMaxAgeH');
    }
    if (patch.subscriptions !== undefined) next.subscriptions = normalizeSubscriptions(patch.subscriptions);
    if (patch.integrations !== undefined) {
      const it = patch.integrations;
      if (!it || typeof it !== 'object' || Array.isArray(it)) throw new SettingsError('integrations must be an object');
      for (const [field, value] of Object.entries(it)) {
        if (!Object.hasOwn(KEY_FIELDS, field)) throw new SettingsError(`unknown integration field ${field}`);
        if (value === null || value === '') next.integrations[field] = '';
        else if (typeof value === 'string' && API_KEY_RE.test(value)) next.integrations[field] = value;
        else throw new SettingsError(`${field} must be 8-256 printable characters, or null to clear it`);
      }
    }
    return next;
  }

  return {
    file,
    get: () => current,
    apiKey: (app) => current.integrations[`${app}ApiKey`] || '',
    // What GET /api/settings returns.
    view() {
      const integrations = {};
      for (const [field, app] of Object.entries(KEY_FIELDS)) {
        const key = current.integrations[field];
        integrations[app] = key ? { configured: true, last4: key.slice(-4) } : { configured: false, last4: null };
      }
      return { thresholds: { ...current.thresholds }, subscriptions: current.subscriptions.map((s) => ({ ...s })), integrations };
    },
    update(patch) {
      const next = merge(current, patch);
      writeJsonAtomic(file, next, { mode: 0o600 });
      current = next;
      for (const fn of listeners) fn(current);
      return this.view();
    },
    onChange(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}
