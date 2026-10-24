// Media drive: statfs {{ media_mount }} only when the gate says `ok` AND the mount table shows it mounted
// (statfs of an unmounted /media would report the root disk). Otherwise last-known values with stale=true.
// Daily samples live in samples.jsonl (written by the sampler); the forecast is computed from them.
import path from 'node:path';
import { appendLine, readJson, readJsonlTail, readText, writeJsonAtomic } from '../util/fsx.js';
import { forecastDrive } from '../forecast.js';
import { statfsTimed, statfsUsage } from './host.js';

const unescapeMount = (s) => s.replace(/\\([0-7]{3})/g, (_, o) => String.fromCharCode(Number.parseInt(o, 8)));

export function isMounted(mountinfoText, mountPoint) {
  for (const line of String(mountinfoText ?? '').split('\n')) {
    const fields = line.split(' ');
    if (fields.length > 4 && unescapeMount(fields[4]) === mountPoint) return true;
  }
  return false;
}

export function createDriveState({ stateDir }) {
  const lastFile = path.join(stateDir, 'drive-last.json');
  const samplesFile = path.join(stateDir, 'samples.jsonl');
  let last = readJson(lastFile).value ?? null;
  let lastWrite = 0;
  return {
    samplesFile,
    lastKnown: () => last,
    remember(usage, now) {
      last = { ...usage, at: new Date(now).toISOString() };
      if (now - lastWrite > 10 * 60_000) {
        lastWrite = now;
        try {
          writeJsonAtomic(lastFile, last, { mode: 0o640 });
        } catch {
          // best effort; the in-memory copy still serves this process
        }
      }
    },
    samples: (days = 60) => readJsonlTail(samplesFile, { maxLines: days }),
    // One sample per local day. Returns true when written.
    addSample({ date, used, total, now }) {
      const prev = readJsonlTail(samplesFile, { maxLines: 1 })[0];
      if (prev?.date === date) return false;
      appendLine(samplesFile, JSON.stringify({ date, ts: new Date(now).toISOString(), used, total }), { mode: 0o640 });
      return true;
    },
  };
}

export async function collectDrive({ cfg, gateStatus, driveState, statfs, settings, today, now = Date.now() }) {
  const mountinfo = readText(path.join(cfg.procDir, 'self/mountinfo'));
  const mounted = gateStatus === 'ok' && isMounted(mountinfo, cfg.mediaMount);
  let usage = null;
  let error = null;
  if (mounted) {
    try {
      usage = statfsUsage(await statfsTimed(cfg.mediaMount, { statfs }));
      driveState.remember(usage, now);
    } catch (err) {
      error = err.code || 'statfs-failed';
    }
  }
  const last = usage ? null : driveState.lastKnown();
  const shown = usage ?? last;
  const samples = driveState
    .samples(60)
    .filter((s) => typeof s.date === 'string' && Number.isFinite(s.used) && Number.isFinite(s.total))
    .map((s) => ({ date: s.date, used: s.used, total: s.total }));
  const fc = forecastDrive(samples, today);
  const usedPct = shown ? Math.round((shown.used / (shown.used + shown.free || 1)) * 1000) / 10 : null;
  return {
    mountPath: cfg.mediaMount,
    mounted,
    stale: !usage,
    lastKnownAt: usage ? null : (last?.at ?? null), // only for stale (last-known) values; live data is as of generatedAt
    error,
    total: shown?.total ?? null,
    used: shown?.used ?? null,
    free: shown?.free ?? null,
    usedPct,
    warnPct: settings.thresholds.diskWarnPct,
    warn: usedPct !== null && usedPct >= settings.thresholds.diskWarnPct,
    split: null, // filled in by the summary from the helper's disk-split (hourly)
    samples,
    forecast: fc.forecast,
    forecastReason: fc.reason,
    samplesInWindow: fc.samplesInWindow,
    _usage: usage,
  };
}
