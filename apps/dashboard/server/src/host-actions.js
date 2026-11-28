// summary.hostActions (CONTRACTS §11.7.2): whether "Restart host" and "Update media apps" can run now, and why not.
//   {reboot: {lock: string|null}, mediaUpdate: {lock: string|null, running: <action id>|null}}
// The same reasons are enforced server-side when the POST arrives (src/actions.js), from fresh state.
import { DOCKER_DOWN_REASON, HELD_REASON, HELPER_DOWN_REASON, heldByGate, TAILNET_ONLY } from './collectors/apps.js';

export const BUSY_REASON = 'Busy';
export const MIN_UPTIME_S = 600;
// Not in the contract's list: a reboot has been scheduled, so nothing else should start (the box goes down in
// seconds). Shown until the dashboard itself is restarted, or for 5 min after the scheduled time.
export const RESTARTING_REASON = 'The host is restarting.';

export function justStartedReason(uptimeSec, minUptimeSec = MIN_UPTIME_S) {
  if (!Number.isFinite(uptimeSec) || uptimeSec >= minUptimeSec) return null;
  return `The box just started. Try again in ${Math.max(1, Math.ceil((minUptimeSec - uptimeSec) / 60))} min.`;
}

// The media lock's Docker part: the gate's held reason (media apps are gated), else Docker not active.
export function mediaDockerReason(gateStatus, dockerState) {
  if (heldByGate(gateStatus, true)) return HELD_REASON;
  if (dockerState === 'unknown') return HELPER_DOWN_REASON;
  if (dockerState !== 'active') return DOCKER_DOWN_REASON;
  return null;
}

export function hostActionsFor({ access = 'tailnet', busy, restarting, uptimeSec, gateStatus, dockerState, mediaRunning = null }) {
  const first = access !== 'tailnet' ? TAILNET_ONLY : restarting ? RESTARTING_REASON : busy ? BUSY_REASON : null;
  const justStarted = justStartedReason(uptimeSec);
  return {
    reboot: { lock: first ?? justStarted },
    mediaUpdate: { lock: first ?? mediaDockerReason(gateStatus, dockerState) ?? justStarted, running: mediaRunning },
  };
}
