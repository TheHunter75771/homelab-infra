// Backups: /var/lib/homelab-backup/status.json (module E: {last_ok, archive, size_bytes, ok}, written on every
// run) and the helper's last-pull. status: ok | stale | failed | none | unknown
import path from 'node:path';
import { readJson } from '../util/fsx.js';
import { isoOrNull } from '../util/time.js';

export function collectBackups({ cfg, settings, now = Date.now(), lastPull = null }) {
  const maxAgeHours = settings.thresholds.backupMaxAgeH;
  const base = { maxAgeHours, lastPull };
  const r = readJson(cfg.backupStatusFile);
  if (r.missing) return { ...base, status: 'none', lastOk: null, archive: null, sizeBytes: null, lastRunOk: null };
  if (r.error || !r.value || typeof r.value !== 'object') {
    return { ...base, status: 'unknown', error: r.error ?? 'invalid', lastOk: null, archive: null, sizeBytes: null, lastRunOk: null };
  }
  const v = r.value;
  const lastOk = isoOrNull(v.last_ok);
  const lastRunOk = typeof v.ok === 'boolean' ? v.ok : null;
  const ageHours = lastOk ? (now - Date.parse(lastOk)) / 3_600_000 : null;
  let status = 'ok';
  if (lastRunOk === false) status = 'failed';
  else if (!lastOk) status = 'none';
  else if (ageHours > maxAgeHours) status = 'stale';
  return {
    ...base,
    status,
    lastOk,
    archive: typeof v.archive === 'string' ? path.basename(v.archive) : null,
    sizeBytes: Number.isFinite(v.size_bytes) ? v.size_bytes : null,
    lastRunOk,
  };
}
