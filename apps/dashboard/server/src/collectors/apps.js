// Apps: the helper's `ps` + `docker-state`, the gate status, pending actions and "stopped on purpose" marks
// become one status per app, plus a lock reason for every action (the UI shows disabled-with-reason).
//
// status: healthy | unhealthy | starting | updating | held | stopped | down | unknown
import { DockerInactiveError } from '../helper/client.js';

export const HELD_REASON = 'Held by the boot gate until the media drive is back.';
export const DOCKER_DOWN_REASON = "Docker isn't running.";
export const TAILNET_ONLY = 'Only available on the tailnet';
export const HELPER_DOWN_REASON = "Can't reach the Docker helper.";

// "lscr.io/linuxserver/sonarr:4.0.17.2952-ls312@sha256:..." -> "4.0.17.2952-ls312"
export function imageTag(ref) {
  if (typeof ref !== 'string' || !ref) return null;
  const noDigest = ref.split('@')[0];
  const lastSlash = noDigest.lastIndexOf('/');
  const colon = noDigest.lastIndexOf(':');
  return colon > lastSlash ? noDigest.slice(colon + 1) : 'latest';
}

function digestOf(ref) {
  const i = typeof ref === 'string' ? ref.indexOf('@sha256:') : -1;
  return i === -1 ? null : ref.slice(i + 1);
}

// Pinned differs from running: compare digests when both have one, else the full refs.
export function updateAvailable(image, pinned) {
  if (!image || !pinned) return false;
  const a = digestOf(image);
  const b = digestOf(pinned);
  if (a && b) return a !== b;
  return image !== pinned;
}

export function appStatus({ ps, dockerState, gateStatus, mediaGated, stoppedOnPurpose, action }) {
  if (action && ['update', 'restart', 'start'].includes(action.kind)) return action.kind === 'update' ? 'updating' : 'starting';
  if (dockerState !== 'active') return gateStatus === 'failed' ? 'held' : 'stopped';
  if (!ps) return gateStatus === 'media-lost' && mediaGated ? 'held' : 'down';
  const running = ps.state === 'running';
  if (!running && gateStatus === 'media-lost' && mediaGated) return 'held';
  if (running) {
    if (ps.health === 'unhealthy') return 'unhealthy';
    if (ps.health === 'starting') return 'starting';
    return 'healthy';
  }
  if (ps.state === 'restarting') return 'down';
  if (stoppedOnPurpose || ps.state === 'paused') return 'stopped';
  return 'down';
}

export const LOCAL_BUILD_REASON = 'Built on the host. A deploy updates it.';

function locksFor({ status, dockerState, gateStatus, mediaGated, running, busy, hasUpdate, localBuild }) {
  const held = (dockerState !== 'active' && gateStatus === 'failed') || (gateStatus === 'media-lost' && mediaGated);
  const notActive = dockerState === 'unknown' ? HELPER_DOWN_REASON : DOCKER_DOWN_REASON;
  const blocked = held ? HELD_REASON : dockerState !== 'active' ? notActive : null;
  return {
    restart: blocked ?? (busy ? 'Busy' : !running ? 'Stopped. Start it instead' : null),
    stop: blocked ?? (busy ? 'Busy' : !running ? 'Already stopped' : null),
    start: blocked ?? (busy ? 'Busy' : running ? 'Already running' : null),
    update: blocked ?? (busy ? 'Busy' : localBuild ? LOCAL_BUILD_REASON : !hasUpdate ? 'Already on the pinned version' : null),
    logs: dockerState !== 'active' ? (gateStatus === 'failed' ? HELD_REASON : notActive) : null,
    _status: status,
  };
}

// The watched apps: what the deployed compose files declare (the helper's `ps`, or its last-known copy while
// Docker is held), plus anything running with the watch label. The catalog only supplies labels; it is the list
// itself only before the helper has ever answered (so an undeployed app, like cloudflared in rehearsal, never
// shows as "down").
export function appNames({ cfg, psList, declared }) {
  const names = new Set();
  if (declared) for (const d of declared) names.add(d.name);
  for (const p of psList ?? []) names.add(p.name);
  if (!declared && !psList) for (const n of Object.keys(cfg.apps)) names.add(n);
  return [...names].sort();
}

// psList: array or null (helper unreachable / docker inactive); declared: array or null.
export function buildApps({ cfg, psList, declared = null, dockerState, gateStatus, stopped, actions }) {
  const byName = new Map((psList ?? []).map((p) => [p.name, p]));
  const declByName = new Map((declared ?? []).map((d) => [d.name, d]));
  return appNames({ cfg, psList, declared }).map((name) => {
    const meta = cfg.apps[name] ?? {};
    const ps = byName.get(name) ?? null;
    const decl = declByName.get(name) ?? null;
    const action = actions?.get(name) ?? null;
    const mediaGated = meta.mediaGated ?? (decl?.stack ?? ps?.stack) === 'media';
    const image = typeof ps?.image === 'string' ? ps.image : null;
    const pinned = ps?.pinned || decl?.pinned || cfg.pinnedImages?.[name] || null;
    const localBuild = decl?.local_build === true;
    const hasUpdate = !localBuild && updateAvailable(image, pinned);
    let status;
    if (dockerState === 'unknown' && !action) status = gateStatus === 'failed' ? 'held' : 'unknown'; // helper unreachable
    else if (psList === null && dockerState === 'active' && !action) status = 'unknown';
    else status = appStatus({ ps, dockerState, gateStatus, mediaGated, stoppedOnPurpose: stopped.has(name), action });
    const running = ps?.state === 'running';
    // Why an app is down without being an incident (non-null only for held / stopped).
    const stoppedReason = status === 'held' ? 'gate' : status === 'stopped' ? (dockerState !== 'active' ? 'docker-down' : 'intentional') : null;
    const locks = locksFor({ status, dockerState, gateStatus, mediaGated, running, busy: Boolean(action), hasUpdate, localBuild });
    delete locks._status;
    return {
      name,
      label: meta.label ?? name,
      purpose: meta.purpose ?? '',
      open: meta.open ?? null,
      stack: ps?.stack || decl?.stack || meta.stack || null,
      mediaGated,
      status,
      stoppedReason,
      state: typeof ps?.state === 'string' ? ps.state : null,
      health: typeof ps?.health === 'string' ? ps.health : null,
      startedAt: running && typeof ps?.started_at === 'string' ? ps.started_at : null,
      restartCount: Number.isInteger(ps?.restart_count) ? ps.restart_count : null,
      image,
      tag: imageTag(image ?? pinned),
      pinned,
      pinnedTag: imageTag(pinned),
      updateAvailable: hasUpdate,
      localBuild,
      action: action ? { id: action.id, kind: action.kind, step: action.steps.at(-1)?.step ?? null } : null,
      locks,
    };
  });
}

// Fetches docker-state then ps, honouring the rule: no ps unless Docker is active.
export async function fetchDocker(docker) {
  let dockerState = 'unknown';
  let psList = null;
  let declared = null;
  let helperError = null;
  try {
    dockerState = await docker.dockerState();
    if (dockerState === 'active') ({ containers: psList, declared } = await docker.psFull()); // re-checks docker-state
  } catch (err) {
    if (err instanceof DockerInactiveError) dockerState = err.state;
    else helperError = err.code || 'error';
  }
  return { dockerState, psList, declared, helperError };
}
