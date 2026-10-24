// The summary: every collector's output assembled into one document (GET /api/summary, SSE /api/events).
//
// Cadence (config `intervals`, seconds): core 10 (gate, host, docker/apps, backups, alerts, push, drive, uptime
// read), media 30, disk split 3600, boots 600, last-pull 600. Each refresh rebuilds the document; its content
// hash (everything except generatedAt) is the ETag and the SSE change detector: listeners get `summary` when
// the hash changed and `tick` (freshness only) when it didn't.
import { createHash } from 'node:crypto';
import { buildApps, fetchDocker, TAILNET_ONLY } from './collectors/apps.js';
import { collectAlerts } from './collectors/alerts.js';
import { collectBackups } from './collectors/backups.js';
import { collectDrive } from './collectors/drive.js';
import { collectGate, failuresByMonth, nextRetry, readBootId, readGateStatus } from './collectors/gate.js';
import { collectHost } from './collectors/host.js';
import { buildIssues } from './issues.js';
import { reminderDue } from './reminders.js';
import { computeBoxUptime } from './uptime.js';
import path from 'node:path';
import { exists, readJson, writeJsonAtomic } from './util/fsx.js';
import { addDays, daysBetween, lastMonths, localDate, localMonth } from './util/time.js';

export const SCHEMA_VERSION = 1;

export function contentHash(summary) {
  const { generatedAt, ...rest } = summary;
  void generatedAt;
  return createHash('sha256').update(JSON.stringify(rest)).digest('hex').slice(0, 24);
}

// The same document as another access level sees it: tailnet-only actions locked with the reason.
export function forAccess(summary, access) {
  if (access === 'tailnet') return summary;
  return {
    ...summary,
    access,
    apps: summary.apps.map((a) => ({ ...a, locks: { ...a.locks, update: TAILNET_ONLY } })),
    gate: { ...summary.gate, canRerun: false, rerunLock: TAILNET_ONLY },
  };
}

export function createSummaryService({
  cfg,
  docker,
  store,
  settings,
  actions,
  driveState,
  uptimeStore,
  media,
  reminders,
  run,
  statfs,
  log,
  clock = () => Date.now(),
  timeZone = cfg.timezone,
}) {
  const listeners = new Set();
  // What the deployed compose files declare, from the last ps that worked: kept on disk so that a bad night
  // right after a reboot (Docker held, no ps possible) still lists the real apps and their pins.
  const declaredFile = path.join(cfg.stateDir, 'declared.json');
  let declared = Array.isArray(readJson(declaredFile).value) ? readJson(declaredFile).value : null;
  const slow = { media: null, split: null, boots: null, lastPull: null, mediaAt: 0, splitAt: 0, bootsAt: 0, lastPullAt: 0 };
  let core = null; // latest core parts
  let current = null; // {summary, hash, etag}
  let coreRunning = null;
  const timers = [];
  let soonTimer = null;

  async function collectCore() {
    const now = clock();
    const today = localDate(now, timeZone);
    const { status: gateStatus } = readGateStatus(cfg);
    const { dockerState, psList, declared: fresh, helperError } = await fetchDocker(docker);
    if (psList) actions.reconcile(psList);
    if (fresh && JSON.stringify(fresh) !== JSON.stringify(declared)) {
      declared = fresh;
      try {
        writeJsonAtomic(declaredFile, declared, { mode: 0o640 });
      } catch (err) {
        log?.warn('could not save declared.json', { code: err.code ?? null });
      }
    }
    const apps = buildApps({ cfg, psList, declared, dockerState, gateStatus, stopped: actions.stoppedSet(), actions: actions.pending() });
    const heldCount = apps.filter((a) => a.status === 'held').length;
    const retrying = ['failed', 'media-lost', 'waiting'].includes(gateStatus);
    const nextRetryAt = retrying ? await nextRetry({ run, unit: cfg.gateRetryTimer, now }).catch(() => null) : null;
    const gate = collectGate({ cfg, timeZone, now, currentBootId: readBootId(cfg), nextRetryAt, heldCount });
    const gatePending = actions.gatePending();
    if (gatePending) {
      gate.action = { id: gatePending.id, kind: gatePending.kind, step: gatePending.steps.at(-1)?.step ?? null };
      gate.canRerun = false;
      gate.rerunLock = 'A re-run is already in progress.';
    } else gate.action = null;
    const s = settings.get();
    const host = await collectHost({ cfg, now, statfs });
    const drive = await collectDrive({ cfg, gateStatus, driveState, statfs, settings: s, today, now });
    const backups = collectBackups({ cfg, settings: s, now, lastPull: slow.lastPull });
    const { available: alertsAvailable, alerts } = collectAlerts({ cfg, now });
    let devices = [];
    let storeError = false;
    try {
      devices = store.devices();
    } catch {
      storeError = true;
    }
    const subscriptions = s.subscriptions
      .map((sub) => ({
        key: sub.key,
        name: sub.name,
        date: sub.date,
        daysLeft: daysBetween(today, sub.date),
        remindOn: addDays(sub.date, -7),
        reminderDue: reminderDue(sub, today),
        reminded: reminders?.wasSent(sub) ?? false,
      }))
      .sort((a, b) => (a.date < b.date ? -1 : 1));
    const months = lastMonths(localMonth(now, timeZone), 6);
    const uptime = {
      box: computeBoxUptime(slow.boots, { now, timeZone, gateEvents: gate._events }),
      apps: uptimeStore.monthly(
        apps.map((a) => a.name),
        months,
      ),
      driveFailures: failuresByMonth(gate._events, months, timeZone),
    };
    return {
      at: now,
      gateStatus,
      dockerState,
      helperError,
      psList,
      apps,
      gate,
      host,
      drive,
      backups,
      alerts,
      alertsAvailable,
      devices,
      storeError,
      subscriptions,
      uptime,
      rehearsal: exists(cfg.rehearsalFlag),
    };
  }

  function assemble(c) {
    const { _events, _now, ...gate } = c.gate;
    void _events;
    void _now;
    const { _usage, ...drive } = c.drive;
    void _usage;
    const mediaPart = slow.media ?? emptyMedia(c.apps);
    const summary = {
      schema: SCHEMA_VERSION,
      generatedAt: new Date(c.at).toISOString(),
      server: { name: cfg.displayName, version: cfg.version, timeZone },
      access: 'tailnet',
      rehearsal: c.rehearsal,
      helper: { reachable: c.helperError === null, error: c.helperError },
      docker: { state: c.dockerState },
      gate,
      apps: c.apps,
      host: c.host,
      drive: { ...drive, split: slow.split },
      backups: c.backups,
      uptime: c.uptime,
      media: mediaPart,
      push: { devices: c.devices, storeError: c.storeError, alerts: c.alerts, alertsAvailable: c.alertsAvailable },
      subscriptions: c.subscriptions,
      settings: { thresholds: { ...settings.get().thresholds } },
      links: { runbooks: cfg.runbookLinks ?? [] },
      issues: [],
    };
    summary.issues = buildIssues(summary, { now: c.at, timeZone });
    return summary;
  }

  function emptyMedia(apps) {
    const status = Object.fromEntries(apps.map((a) => [a.name, a.status]));
    const pending = (app) => ({ state: ['healthy', 'unhealthy', 'starting'].includes(status[app]) ? 'pending' : status[app] === 'held' ? 'held' : 'down', message: null });
    return {
      jellyfin: pending('jellyfin'),
      sabnzbd: pending('sabnzbd'),
      jellyseerr: pending('jellyseerr'),
      prowlarr: pending('prowlarr'),
      sonarr: pending('sonarr'),
      radarr: pending('radarr'),
      checks: [],
    };
  }

  function publish() {
    if (!core) return;
    const summary = assemble(core);
    const hash = contentHash(summary);
    const changed = !current || current.hash !== hash;
    current = { summary, hash, etag: `"${hash}"` };
    const event = { type: changed ? 'summary' : 'tick', summary, etag: current.etag, generatedAt: summary.generatedAt };
    for (const fn of listeners) {
      try {
        fn(event);
      } catch {
        // one bad listener must not stop the others
      }
    }
  }

  async function refreshCore() {
    coreRunning ??= (async () => {
      try {
        core = await collectCore();
        publish();
      } catch (err) {
        log?.error('summary refresh failed', { error: err.message });
      } finally {
        coreRunning = null;
      }
    })();
    await coreRunning;
    return current?.summary ?? null;
  }

  async function refreshMedia() {
    if (!core) await refreshCore();
    if (!core) return;
    const statuses = Object.fromEntries(core.apps.map((a) => [a.name, a.status]));
    try {
      slow.media = await media.collect(statuses, { driveMounted: core.drive.mounted });
      slow.mediaAt = clock();
    } catch (err) {
      log?.error('media refresh failed', { error: err.message });
    }
    publish();
  }

  // The helper runs du only when the gate is ok, /media is mounted and the sentinel is there; otherwise it
  // answers with its cached sizes marked stale, or "media-unavailable". So asking is always safe, and a held
  // night still shows the last-known split.
  async function refreshSplit() {
    try {
      const r = await docker.diskSplit();
      if (['tv', 'movies', 'downloads'].some((k) => Number.isFinite(r[k]))) {
        const n = (v) => (Number.isFinite(v) ? v : null); // du can fail for one folder
        slow.split = { tv: n(r.tv), movies: n(r.movies), downloads: n(r.downloads), computedAt: r.computedAt, stale: r.stale };
      }
    } catch (err) {
      if (err.code !== 'media-unavailable') log?.warn('disk-split unavailable', { code: err.code ?? null });
    }
  }

  async function refreshBoots() {
    try {
      slow.boots = await docker.boots();
    } catch (err) {
      log?.warn('boots unavailable', { code: err.code ?? null });
    }
  }

  async function refreshLastPull() {
    try {
      const { lastPull } = await docker.lastPull();
      slow.lastPull = lastPull && !Number.isNaN(Date.parse(lastPull)) ? new Date(lastPull).toISOString() : null;
    } catch (err) {
      log?.warn('last-pull unavailable', { code: err.code ?? null });
    }
  }

  function every(seconds, fn) {
    const t = setInterval(() => {
      fn().catch(() => {});
    }, seconds * 1000);
    t.unref();
    timers.push(t);
  }

  return {
    async start() {
      await Promise.all([refreshBoots(), refreshLastPull()]);
      await refreshCore();
      await refreshSplit();
      await refreshMedia();
      const iv = cfg.intervals;
      every(iv.core, refreshCore);
      every(iv.media, refreshMedia);
      every(iv.split, refreshSplit);
      every(iv.boots, refreshBoots);
      every(iv.lastPull, refreshLastPull);
    },
    stop() {
      for (const t of timers) clearInterval(t);
      timers.length = 0;
      if (soonTimer) clearTimeout(soonTimer);
    },
    refreshCore,
    refreshMedia,
    refreshSplit,
    refreshBoots,
    refreshLastPull,
    // Coalesced refresh shortly after something changed (action finished, settings saved).
    refreshSoon(delayMs = 300) {
      if (soonTimer) return;
      soonTimer = setTimeout(() => {
        soonTimer = null;
        refreshCore()
          .then(() => refreshMedia())
          .catch((err) => log?.error('refresh failed', { error: err?.message ?? String(err) }));
      }, delayMs);
      soonTimer.unref?.();
    },
    // Rebuild from the cached parts right away (an action step changed what `apps[].action` shows).
    touch() {
      if (!core) return;
      core.apps = buildApps({
        cfg,
        psList: core.psList,
        declared,
        dockerState: core.dockerState,
        gateStatus: core.gateStatus,
        stopped: actions.stoppedSet(),
        actions: actions.pending(),
      });
      publish();
    },
    current: () => current,
    core: () => core,
    onUpdate(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    listenerCount: () => listeners.size,
  };
}
