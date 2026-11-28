// Actions: restart | stop | start | update an app, re-run the gate check, update media apps (media-update) and
// restart the host (reboot). Each runs in the background with progress steps that GET /api/actions/:id streams.
// The same lock reasons the UI shows are enforced here (from a fresh summary), so a stale or crafted client can't
// act on a held app.
//
// Steps ({step, at}, plus the extra fields listed)
//   restart: restarting -> waiting-health -> done | failed
//   start:   starting   -> waiting-health -> done | failed
//   stop:    stopping   -> done | failed
//   update:  the helper's steps: pulling -> recreating -> waiting-health -> done | failed
//   gate-rerun: requested -> running -> done | failed
//     requested: /run/bootgate/rerun created (contract 2); running: bootgate-retry.service picked it up (the
//     file is gone); done/failed: the service finished and the gate status is / isn't ok. A failed retry writes
//     no history event, so the service state is what tells us it finished.
//   media-update (CONTRACTS §11.7.2, app null): per app, in the order asked, one at a time with the helper's set-pin:
//     {step: "app", app, index (1-based), total, from, to}   from/to: versions (tags)
//     the helper's pulling | pinning | recreating | waiting-health | rolling-back, each with {app}
//     {step: "app-done", app} | {step: "app-failed", app, rolledBack}   the first failure ends the action
//     -> done ("Updated N apps: Sonarr 4.0.20.3014-ls7, …") | failed (the reason and what already finished)
//   reboot (app null): requested -> scheduled -> done ("Restarting in 10 s. …"). The helper call happens before
//     the 202: its busy / too-soon refusal is a 409 and leaves no action behind.
//
// Exclusion: one update at a time (§11.2 Concurrency): a per-app update and a media update exclude each other
// (409), and no per-app action runs on an app inside a running media update. A reboot needs nothing running
// (409 Busy), and once one is scheduled nothing new starts.
import path from 'node:path';
import nodeFs from 'node:fs';
import { randomBytes } from 'node:crypto';
import { DEFAULT_APPS } from './catalog.js';
import { readGateStatus } from './collectors/gate.js';
import { heldByGate, HELD_REASON, TAILNET_ONLY } from './collectors/apps.js';
import { readUptimeSeconds } from './collectors/host.js';
import { APP_NAME_RE } from './catalog.js';
import { DockerInactiveError } from './helper/client.js';
import { BUSY_REASON, justStartedReason, mediaDockerReason, RESTARTING_REASON } from './host-actions.js';
import { redactCredentials } from './redact.js';
import { readJson, writeJsonAtomic } from './util/fsx.js';
import { parseKeyValues } from './util/run.js';

export const APP_ACTIONS = ['restart', 'stop', 'start', 'update'];
export const TAILNET_ONLY_ACTIONS = new Set(['update', 'gate-rerun', 'media-update', 'reboot']);
export const MEDIA_UPDATE_MAX_APPS = 10;
export const CHECK_AGAIN = 'Check for updates again';
const MEDIA_LABEL = 'Media app updates';
const RESTART_GRACE_MS = 5 * 60_000;
const KEEP_FINISHED_MS = 10 * 60_000;
const RUNNING_STATES = new Set(['activating', 'active', 'reloading', 'deactivating']);

export class ActionError extends Error {
  constructor(status, message) {
    super(message);
    this.name = 'ActionError';
    this.status = status;
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function createActions({
  cfg,
  docker,
  summary,
  run,
  log,
  clock = () => Date.now(),
  fs = nodeFs,
  pollMs = 2000,
  healthTimeoutMs = 120_000,
  rerunPickupMs = 30_000,
  rerunTimeoutMs = 180_000,
  mediaUpdates = null,
  readUptime = () => readUptimeSeconds(cfg.procDir),
}) {
  const stoppedFile = path.join(cfg.stateDir, 'stopped.json');
  const loaded = readJson(stoppedFile).value;
  const stopped = new Set(Array.isArray(loaded) ? loaded.filter((n) => typeof n === 'string') : []);
  const all = new Map(); // id -> action
  const byApp = new Map(); // app -> running action
  const starting = new Set(); // apps whose action is being checked (before it runs)
  let startingUpdate = null; // the app whose update is being checked
  let gateAction = null;
  let mediaAction = null; // the running media update
  let mediaStarting = null; // Set of app names while a media update is being checked (before it runs)
  let rebootAction = null; // the reboot being requested from the helper
  let restartingUntil = 0; // a reboot was scheduled: nothing new starts until then
  const listeners = new Map(); // id -> Set(fn)

  function saveStopped() {
    try {
      writeJsonAtomic(stoppedFile, [...stopped].sort(), { mode: 0o640 });
    } catch (err) {
      log?.error('could not save stopped.json', { code: err.code ?? null });
    }
  }

  function emit(a, event) {
    for (const fn of listeners.get(a.id) ?? []) {
      try {
        fn(event);
      } catch {
        // a broken listener must not break the action
      }
    }
  }

  function create(kind, app) {
    const a = { id: randomBytes(8).toString('hex'), kind, app, state: 'running', steps: [], error: null, detail: null, startedAt: new Date(clock()).toISOString(), finishedAt: null };
    all.set(a.id, a);
    return a;
  }

  function step(a, name, extra = {}) {
    const s = { step: name, ...extra, at: new Date(clock()).toISOString() };
    a.steps.push(s);
    emit(a, { type: 'step', ...s });
    summary?.touch?.();
  }

  function finish(a, ok, { error = null, detail = null } = {}) {
    if (a.state !== 'running') return;
    a.state = ok ? 'done' : 'failed';
    a.error = error;
    a.detail = detail;
    a.finishedAt = new Date(clock()).toISOString();
    a.steps.push({ step: ok ? 'done' : 'failed', at: a.finishedAt });
    if (a.app) byApp.delete(a.app);
    if (a.kind === 'gate-rerun') gateAction = null;
    if (a.kind === 'media-update') {
      mediaAction = null;
      mediaUpdates?.drop();
    }
    if (a.kind === 'reboot') rebootAction = null;
    log?.info('action finished', { id: a.id, kind: a.kind, app: a.app, ok, error });
    emit(a, { type: 'end', state: a.state, error, detail });
    listeners.delete(a.id);
    setTimeout(() => all.delete(a.id), KEEP_FINISHED_MS).unref();
    summary?.refreshSoon?.();
  }

  const nice = (name) => cfg.apps[name]?.label ?? DEFAULT_APPS[name]?.label ?? name;
  const restarting = () => clock() < restartingUntil;
  const mediaBusy = () => Boolean(mediaAction || mediaStarting);
  const mediaNames = () => mediaAction?.plan ?? mediaStarting ?? new Set();
  // Anything running or being checked, other than a media update / reboot reservation the caller holds itself.
  const appsBusy = () => byApp.size > 0 || starting.size > 0;
  const anyBusy = () => appsBusy() || Boolean(gateAction) || mediaBusy() || Boolean(rebootAction);
  // Text from the helper can quote Docker's stderr: no credentials, no URLs (registry addresses) in the browser.
  const clean = (text, max = 200) => redactCredentials(String(text ?? '')).replace(/\b[a-z][a-z0-9+.-]*:\/\/\S+/gi, '[address]').slice(0, max);

  async function waitHealthy(a, name) {
    const deadline = clock() + healthTimeoutMs;
    while (clock() < deadline) {
      await sleep(pollMs);
      let list;
      try {
        list = await docker.ps();
      } catch (err) {
        return finish(a, false, { error: `Lost contact with Docker (${err.code || err.message}).` });
      }
      const ps = list.find((p) => p?.name === name);
      if (ps?.state === 'running' && (ps.health === 'healthy' || !ps.health || ps.health === 'none')) {
        return finish(a, true, { detail: `${nice(name)} is healthy again` });
      }
      if (ps && ['exited', 'dead'].includes(ps.state)) {
        return finish(a, false, { error: `${nice(name)} exited instead of starting.` });
      }
    }
    return finish(a, false, { error: `${nice(name)} didn't become healthy within ${Math.round(healthTimeoutMs / 60_000)} min.` });
  }

  async function runApp(a) {
    const name = a.app;
    try {
      if (a.kind === 'stop') {
        step(a, 'stopping');
        await docker.control('stop', name);
        stopped.add(name);
        saveStopped();
        return finish(a, true, { detail: `${nice(name)} stopped. It stays down until you start it.` });
      }
      if (a.kind === 'restart' || a.kind === 'start') {
        step(a, a.kind === 'restart' ? 'restarting' : 'starting');
        await docker.control(a.kind, name);
        if (stopped.delete(name)) saveStopped();
        step(a, 'waiting-health');
        return await waitHealthy(a, name);
      }
      // update: the helper streams pulling -> recreating -> waiting-health -> done | failed
      let failure = null;
      const final = await docker.update(name, {
        onStep: (m) => {
          if (m.step === 'failed') failure = m;
          else if (m.step !== 'done') step(a, m.step);
        },
      });
      if (failure || final.ok !== true) {
        const why = failure?.message ?? final.message ?? final.reason ?? 'The update failed.';
        const where = failure?.failed_step ? `${failure.failed_step}: ` : '';
        return finish(a, false, { error: `${where}${why}`.slice(0, 200) });
      }
      if (stopped.delete(name)) saveStopped();
      return finish(a, true, { detail: `${nice(name)} updated and healthy` });
    } catch (err) {
      return finish(a, false, { error: String(err.message || 'The helper refused the action.').slice(0, 200) });
    }
  }

  async function runMediaUpdate(a, plan) {
    const done = [];
    for (let i = 0; i < plan.length; i++) {
      const item = plan[i];
      const app = item.name;
      step(a, 'app', { app, index: i + 1, total: plan.length, from: item.current.version, to: item.latest.version });
      let failure = null;
      let final = null;
      try {
        final = await docker.setPin(app, item.latest.pin, {
          onStep: (m) => {
            if (m.step === 'failed') failure = m;
            else if (m.step !== 'done' && typeof m.step === 'string') step(a, m.step, { app });
          },
        });
      } catch (err) {
        failure = { message: err instanceof DockerInactiveError ? "Docker isn't running." : err?.message, rolled_back: false };
      }
      if (!failure && final?.ok !== true) failure = { message: final?.message ?? final?.reason ?? 'The update failed.', rolled_back: false };
      if (failure) {
        step(a, 'app-failed', { app, rolledBack: failure.rolled_back === true });
        const where = failure.failed_step ? ` at ${failure.failed_step}` : '';
        const why = clean(failure.message || 'the update failed', 200).replace(/\.$/, '');
        const rest = plan.slice(i + 1).map((x) => x.label);
        let error = `${item.label} failed${where}: ${why}.`;
        error += done.length ? ` Already updated: ${done.join(', ')}.` : ' No other app was changed.';
        if (rest.length) error += ` Not started: ${rest.join(', ')}.`;
        return finish(a, false, { error: error.slice(0, 600) });
      }
      if (stopped.delete(app)) saveStopped();
      step(a, 'app-done', { app });
      done.push(`${item.label} ${item.latest.version}`);
    }
    return finish(a, true, { detail: `Updated ${done.length} app${done.length === 1 ? '' : 's'}: ${done.join(', ')}` });
  }

  async function retryServiceState() {
    if (!run) return null;
    const r = await run('systemctl', ['show', cfg.gateRetryService, '--no-pager', '-p', 'ActiveState']);
    return r.code === 0 ? (parseKeyValues(r.stdout).ActiveState ?? null) : null;
  }

  async function runRerun(a) {
    try {
      fs.writeFileSync(cfg.gateRerunFile, `${new Date(clock()).toISOString()}\n`, { mode: 0o660 });
    } catch (err) {
      return finish(a, false, { error: `Couldn't request the re-run (${err.code || 'error'}).` });
    }
    step(a, 'requested');
    const pickupBy = clock() + rerunPickupMs;
    while (fs.existsSync(cfg.gateRerunFile)) {
      if (clock() > pickupBy) {
        return finish(a, false, { error: `The gate didn't pick up the request within ${Math.round(rerunPickupMs / 1000)} s.` });
      }
      await sleep(Math.min(pollMs, 500));
    }
    step(a, 'running');
    // The retry service is a oneshot: "activating" while it runs, then "inactive" (or "failed"). The trigger file
    // is removed when it starts, so from here on it is either running or already finished.
    const doneBy = clock() + rerunTimeoutMs;
    for (;;) {
      const st = await retryServiceState();
      if (st === null || !RUNNING_STATES.has(st)) break; // finished, or no systemctl: settle on the gate status
      if (clock() > doneBy) {
        return finish(a, false, { error: `The check is still running after ${Math.round(rerunTimeoutMs / 60_000)} min. Watch the gate status.` });
      }
      await sleep(Math.min(pollMs, 1000));
    }
    let status = 'unknown';
    try {
      ({ status } = readGateStatus(cfg));
    } catch (err) {
      return finish(a, false, { error: `Can't read the gate status (${err.code || 'error'}).` });
    }
    summary?.refreshSoon?.();
    if (status === 'ok') return finish(a, true, { detail: `Check passed. Drive mounted at ${cfg.mediaMount}.` });
    return finish(a, false, {
      error: `Check failed: the drive is still not verified. Next automatic retry in ${cfg.gateRetryMinutes} min.`,
    });
  }

  return {
    stoppedSet: () => stopped,
    pending: () => byApp,
    gatePending: () => gateAction,
    get: (id) => all.get(id) ?? null,
    // For summary.hostActions.
    hostState: () => ({ busy: anyBusy(), restarting: restarting(), mediaRunning: mediaAction?.id ?? null }),

    // POST /api/media/update {apps: [name, …]} (CONTRACTS §11.7.2).
    async startMediaUpdate(names, { access }) {
      if (access !== 'tailnet') throw new ActionError(403, TAILNET_ONLY);
      if (
        !Array.isArray(names) ||
        names.length < 1 ||
        names.length > MEDIA_UPDATE_MAX_APPS ||
        !names.every((n) => typeof n === 'string' && APP_NAME_RE.test(n)) ||
        new Set(names).size !== names.length
      ) {
        throw new ActionError(400, `apps must be a list of 1-${MEDIA_UPDATE_MAX_APPS} different app names`);
      }
      // Cheap refusals before any helper call.
      if (restarting()) throw new ActionError(409, RESTARTING_REASON);
      if (mediaBusy()) throw new ActionError(409, `Another update is running (${MEDIA_LABEL}). Try again when it finishes.`);
      const otherUpdate = [...byApp.values()].find((x) => x.kind === 'update')?.app ?? startingUpdate;
      if (otherUpdate) throw new ActionError(409, `Another update is running (${nice(otherUpdate)}). Try again when it finishes.`);
      if (anyBusy()) throw new ActionError(409, BUSY_REASON);
      const check = mediaUpdates?.fresh() ?? null;
      const plan = names.map((n) => check?.apps.find((x) => x.name === n) ?? null);
      if (plan.some((x) => !x || x.status !== 'update' || !x.latest?.pin)) throw new ActionError(409, CHECK_AGAIN);
      const justStarted = justStartedReason(readUptime());
      if (justStarted) throw new ActionError(409, justStarted);
      mediaStarting = new Set(names);
      let a;
      try {
        const fresh = await summary.refreshCore();
        if (!fresh) throw new ActionError(503, 'the dashboard is still starting');
        let dockerNow;
        try {
          dockerNow = await docker.dockerState({ fresh: true });
        } catch {
          throw new ActionError(503, "Can't reach the Docker helper.");
        }
        const blocked = mediaDockerReason(fresh.gate.status, dockerNow);
        if (blocked) throw new ActionError(409, blocked);
        // The pins may have moved since the check (a per-app update, a deploy): then the check is stale.
        const declared = new Map((summary.declared?.() ?? []).map((d) => [d.name, d]));
        if (plan.some((x) => declared.get(x.name)?.pinned !== x.current.pin)) throw new ActionError(409, CHECK_AGAIN);
        a = create('media-update', null);
        a.plan = new Set(names);
        mediaAction = a;
      } finally {
        mediaStarting = null;
        if (!a) summary.touch?.(); // refused: hostActions showed this check as Busy
      }
      log?.info('action started', { id: a.id, kind: 'media-update', apps: names });
      summary.touch?.();
      runMediaUpdate(a, plan).catch((err) => finish(a, false, { error: clean(err?.message ?? 'The media update failed.') }));
      return a;
    },

    // POST /api/host/reboot (CONTRACTS §11.7.2).
    async startReboot({ access }) {
      if (access !== 'tailnet') throw new ActionError(403, TAILNET_ONLY);
      if (restarting()) throw new ActionError(409, RESTARTING_REASON);
      if (anyBusy()) throw new ActionError(409, BUSY_REASON);
      const justStarted = justStartedReason(readUptime());
      if (justStarted) throw new ActionError(409, justStarted);
      const a = create('reboot', null);
      rebootAction = a;
      step(a, 'requested');
      let scheduledIn;
      try {
        ({ scheduledIn } = await docker.reboot());
      } catch (err) {
        rebootAction = null;
        all.delete(a.id);
        summary?.touch?.();
        if (err?.code === 'busy' || err?.code === 'too-soon') throw new ActionError(409, clean(err.message || BUSY_REASON));
        if (err?.code === 'timeout') throw new ActionError(503, "The helper didn't answer. The restart may still happen.");
        if (err?.code === 'unavailable') throw new ActionError(503, "Can't reach the Docker helper.");
        log?.error('reboot refused', { code: err?.code ?? null });
        throw new ActionError(502, "Couldn't schedule the restart.");
      }
      const secs = scheduledIn ?? 10;
      restartingUntil = clock() + secs * 1000 + RESTART_GRACE_MS;
      step(a, 'scheduled');
      log?.warn('host reboot scheduled', { id: a.id, inSeconds: secs });
      finish(a, true, { detail: `Restarting in ${secs} s. The dashboard is back in a few minutes.` });
      return a;
    },

    subscribe(id, fn) {
      if (!listeners.has(id)) listeners.set(id, new Set());
      listeners.get(id).add(fn);
      return () => listeners.get(id)?.delete(fn);
    },

    // Observed running again (started outside the dashboard): drop the "stopped on purpose" mark.
    reconcile(psList) {
      let changed = false;
      for (const p of psList ?? []) {
        if (p?.state === 'running' && stopped.delete(p.name)) changed = true;
      }
      if (changed) saveStopped();
    },

    async startApp(kind, name, { access }) {
      if (!APP_ACTIONS.includes(kind)) throw new ActionError(404, 'unknown action');
      if (TAILNET_ONLY_ACTIONS.has(kind) && access !== 'tailnet') throw new ActionError(403, TAILNET_ONLY);
      // Cheap refusals and reservations before any helper call (the refresh below talks to the helper).
      if (restarting()) throw new ActionError(409, RESTARTING_REASON);
      if (byApp.has(name) || starting.has(name) || rebootAction) throw new ActionError(409, BUSY_REASON);
      // CONTRACTS §11.2 Concurrency: one update at a time (each one pulls an image and holds a helper slot); a media
      // update counts as one (§11.7.2).
      if (kind === 'update' && mediaBusy()) throw new ActionError(409, `Another update is running (${MEDIA_LABEL}). Try again when it finishes.`);
      if (mediaNames().has(name)) throw new ActionError(409, BUSY_REASON);
      const otherUpdate = kind === 'update' ? ([...byApp.values()].find((x) => x.kind === 'update')?.app ?? startingUpdate) : null;
      if (otherUpdate) throw new ActionError(409, `Another update is running (${nice(otherUpdate)}). Try again when it finishes.`);
      starting.add(name);
      if (kind === 'update') startingUpdate = name;
      let a;
      try {
        const fresh = await summary.refreshCore();
        const app = fresh?.apps.find((x) => x.name === name);
        if (!app) throw new ActionError(404, 'no such app');
        const lock = app.locks[kind];
        if (lock) throw new ActionError(409, lock);
        // The refresh above may have been one already in flight; ask once more, fresh, right before acting.
        let dockerNow;
        try {
          dockerNow = await docker.dockerState({ fresh: true });
        } catch {
          throw new ActionError(503, "Can't reach the Docker helper.");
        }
        if (dockerNow !== 'active') {
          throw new ActionError(409, heldByGate(fresh.gate.status, app.mediaGated) ? HELD_REASON : "Docker isn't running.");
        }
        a = create(kind, name);
        byApp.set(name, a);
      } finally {
        starting.delete(name);
        if (startingUpdate === name) startingUpdate = null;
        if (!a) summary.touch?.(); // refused: hostActions showed this check as Busy
      }
      log?.info('action started', { id: a.id, kind, app: name });
      summary.touch?.();
      runApp(a).catch((err) => finish(a, false, { error: String(err?.message ?? 'The action failed.').slice(0, 200) }));
      return a;
    },

    async startGateRerun({ access }) {
      if (access !== 'tailnet') throw new ActionError(403, TAILNET_ONLY);
      if (restarting()) throw new ActionError(409, RESTARTING_REASON);
      if (gateAction) throw new ActionError(409, 'A re-run is already in progress.');
      if (rebootAction || mediaBusy()) throw new ActionError(409, BUSY_REASON);
      let status;
      let mediaStopped;
      try {
        ({ status, mediaStopped } = readGateStatus(cfg));
      } catch (err) {
        throw new ActionError(503, `Can't read the gate status (${err.code || 'error'}).`);
      }
      const can = ['failed', 'media-lost', 'waiting'].includes(status) || (status === 'ok' && mediaStopped);
      if (!can) {
        throw new ActionError(
          409,
          status === 'ok' ? 'The drive is verified and mounted. A re-run only acts when it has failed or been lost.' : 'The gate status is unknown.',
        );
      }
      const a = create('gate-rerun', null);
      gateAction = a;
      log?.info('action started', { id: a.id, kind: 'gate-rerun' });
      runRerun(a).catch((err) => finish(a, false, { error: `The re-run failed (${err?.code || err?.message || 'error'}).`.slice(0, 200) }));
      return a;
    },
  };
}

export function actionView(a) {
  return {
    id: a.id,
    kind: a.kind,
    app: a.app,
    state: a.state,
    steps: a.steps.map((s) => ({ ...s })),
    error: a.error,
    detail: a.detail,
    startedAt: a.startedAt,
    finishedAt: a.finishedAt,
  };
}
