// Actions: restart | stop | start | update an app, and re-run the gate check. Each runs in the background with
// progress steps that GET /api/actions/:id streams. The same lock reasons the UI shows are enforced here
// (from a fresh summary), so a stale or crafted client can't act on a held app.
//
// Steps
//   restart: restarting -> waiting-health -> done | failed
//   start:   starting   -> waiting-health -> done | failed
//   stop:    stopping   -> done | failed
//   update:  the helper's steps: pulling -> recreating -> waiting-health -> done | failed
//   gate-rerun: requested -> running -> done | failed
//     requested: /run/bootgate/rerun created (contract 2); running: bootgate-retry.service picked it up (the
//     file is gone); done/failed: the service finished and the gate status is / isn't ok. A failed retry writes
//     no history event, so the service state is what tells us it finished.
import path from 'node:path';
import nodeFs from 'node:fs';
import { randomBytes } from 'node:crypto';
import { DEFAULT_APPS } from './catalog.js';
import { readGateStatus } from './collectors/gate.js';
import { heldByGate, HELD_REASON, TAILNET_ONLY } from './collectors/apps.js';
import { readJson, writeJsonAtomic } from './util/fsx.js';
import { parseKeyValues } from './util/run.js';

export const APP_ACTIONS = ['restart', 'stop', 'start', 'update'];
export const TAILNET_ONLY_ACTIONS = new Set(['update', 'gate-rerun']);
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
}) {
  const stoppedFile = path.join(cfg.stateDir, 'stopped.json');
  const loaded = readJson(stoppedFile).value;
  const stopped = new Set(Array.isArray(loaded) ? loaded.filter((n) => typeof n === 'string') : []);
  const all = new Map(); // id -> action
  const byApp = new Map(); // app -> running action
  const starting = new Set(); // apps whose action is being checked (before it runs)
  let startingUpdate = null; // the app whose update is being checked
  let gateAction = null;
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

  function step(a, name) {
    const s = { step: name, at: new Date(clock()).toISOString() };
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
    log?.info('action finished', { id: a.id, kind: a.kind, app: a.app, ok, error });
    emit(a, { type: 'end', state: a.state, error, detail });
    listeners.delete(a.id);
    setTimeout(() => all.delete(a.id), KEEP_FINISHED_MS).unref();
    summary?.refreshSoon?.();
  }

  const nice = (name) => cfg.apps[name]?.label ?? DEFAULT_APPS[name]?.label ?? name;

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
      if (byApp.has(name) || starting.has(name)) throw new ActionError(409, 'Busy');
      // CONTRACTS §11.2 Concurrency: one update at a time (each one pulls an image and holds a helper slot).
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
      }
      log?.info('action started', { id: a.id, kind, app: name });
      summary.touch?.();
      runApp(a).catch((err) => finish(a, false, { error: String(err?.message ?? 'The action failed.').slice(0, 200) }));
      return a;
    },

    async startGateRerun({ access }) {
      if (access !== 'tailnet') throw new ActionError(403, TAILNET_ONLY);
      if (gateAction) throw new ActionError(409, 'A re-run is already in progress.');
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
